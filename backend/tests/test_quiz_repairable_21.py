"""Phase 21 track F21-quiz: a refused quiz call says exactly what to fix, so one corrected call publishes it.

Claim set C1 (corpus 18a.3, ui-quiz): in two of three runs Pebble's model called UIRender twice, got back only what
it called a formatting error, and reported honestly that the quiz was not ready. The baseline wrote quiz.json and
passed. These are the refusals a reasonable quiz call could meet on the Phase 20 source, in the order it meets them:

- runner, App.validate: a missing or undeclared argument (a flattened call), or a value of the wrong type (data as
  a JSON object, evidence_ids as a list): "Tool request does not match its allowed schema." or "Tool argument is
  invalid.", with no argument named (parent patch P1);
- App.execute, json.loads(evidence_ids): a blank optional argument gives the parser's "Expecting value: line 1
  column 1 (char 0)" (P2);
- publish: a long title gives "Invalid interactive view text." (P4). The data is checked by json_object (this track)
  and by the quiz shape in artifacts.validate (P3). There, an extra key such as data.title gave "Quiz duration must
  be 1–180 minutes." even with duration_minutes 5, and numeric choices gave "Invalid interactive view text.".

What stays fixed:
- The accepted set is unchanged, because the corpus grader calls artifacts.validate and keys stay host-held.
- Each refusal names its 0-based JSON path and the expected shape.
- No refusal quotes a question, choice, answer or explanation: refusals reach the progress line and the run records
  both windows read, and the corrected call usually repeats the same answer key.

The tests for P1 and P3 run the note's exact patch code through the `merged` fixture until the parent merges them;
after the merge that fixture changes nothing. No model calls and no network: every model turn is a local script.
"""
import ast
import copy
import json
import re

import pytest

import test_eval_live_shapes_18 as shapes
from pebble import artifact_inputs, artifacts, eval_corpus_18 as c18, mods, quiz_grading
from pebble.app import App
from pebble.artifact_schema import (REJECTED_TEXT, SCHEMAS, UI_BRIEF, _fold, evidence_argument, json_object,
                                    schema_error, validate_quiz)
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from pebble.runner import SMALL_BRIEF
from pebble.tool_catalogue import TOOLS

VIEWS = ('UIRender', 'UIUpdate')
REQUEST = 'Make a three-question multiple-choice quiz view about the observatory with a five-minute time limit.'
# The UIRender quiz shape in the Phase 19/20 catalogue, and this track's (types, the duration range, a 0-based answer).
PHASE19_QUIZ_TEXT = 'quiz:{duration_minutes,questions:[{question,choices,answer,explanation}]}'
QUIZ_TEXT = 'quiz:{duration_minutes:1-180,questions:[{question,choices:[strings],answer:0-based choice index,explanation}]}'
GROWTH = len(QUIZ_TEXT) - len(PHASE19_QUIZ_TEXT)
PHASE19_UI_BRIEF = ('Save a native view; data is a JSON object string. Kinds: form table chart plan evidence quiz '
                    'diagram html. A schema error returns that kind\'s exact schema.')
# test_phase19_merge pins (compact bytes of the offered copies): at 4K they must not move; above 4K UIRender grows by
# GROWTH and UIUpdate (whose description names no shape) does not.
PINNED_4K_BYTES = {'UIRender': 515, 'UIUpdate': 518}
PHASE19_WIDE_BYTES = {'ordinary': {'UIRender': 1198, 'UIUpdate': 702},
                      'dashboard': {'UIRender': 1676, 'UIUpdate': 1180},
                      'prototype': {'UIRender': 1632, 'UIUpdate': 1136}}
WIDE_TEXTS = {'ordinary': 'Make a table of the fruits I like.', 'dashboard': 'Build a dashboard of my runs this week.',
              'prototype': 'Sketch a clickable prototype of a recipe app.'}
# Explanations carry this word; the host never shows an explanation before a submit, so no refusal or record may.
CANARY = 'kestrel'


def quiz():
    """What a capable model writes for ui-quiz (the live-shape driver's facts), with every choice already text."""
    return {'duration_minutes': 5, 'questions': [
        {'question': 'In which year did the Calder Hill observatory open?', 'choices': ['1925', '1931', '1948', '1962'],
         'answer': 1, 'explanation': 'The notes say it opened in 1931 (' + CANARY + ' one).'},
        {'question': 'What is the diameter of its main mirror, in metres?', 'choices': ['0.8', '1.2', '1.6', '2.4'],
         'answer': 1, 'explanation': 'The main telescope has a 1.2 metre mirror (' + CANARY + ' two).'},
        {'question': 'Who refurbished the dome?',
         'choices': ['Halden Works', 'Ellwood and Sons', 'Northgate Builders', 'Arlow Engineering'], 'answer': 3,
         'explanation': 'Arlow Engineering refurbished the dome in 1998 (' + CANARY + ' three).'}]}


def phase19_quiz(data):
    """artifacts.validate('quiz', ...) as it was on the Phase 20 source, verbatim: the accepted set this track keeps."""
    def text(value, maximum):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError('Invalid interactive view text.')
        return value
    data = json_object(data)
    if set(data) != {'duration_minutes', 'questions'} or type(data['duration_minutes']) is not int or not 1 <= data['duration_minutes'] <= 180:
        raise ValueError('Quiz duration must be 1–180 minutes.')
    qs = data['questions']
    if not isinstance(qs, list) or not 1 <= len(qs) <= 50:
        raise ValueError('A quiz needs 1–50 questions.')
    for q in qs:
        if not isinstance(q, dict) or set(q) != {'question', 'choices', 'answer', 'explanation'}:
            raise ValueError('Quiz questions need question, choices, answer index and explanation.')
        text(q['question'], 1500)
        text(q['explanation'], 1500)
        if not isinstance(q['choices'], list) or not 2 <= len(q['choices']) <= 6:
            raise ValueError('Provide 2–6 choices per question.')
        for choice in q['choices']:
            text(choice, 700)
        if type(q['answer']) is not int or not 0 <= q['answer'] < len(q['choices']):
            raise ValueError('Quiz answer index does not match its choices.')
    return data


def refusal(data):
    with pytest.raises(ValueError) as caught:
        validate_quiz(data)
    return str(caught.value)


def quiz_texts(data):
    """Every question, choice and explanation string in a payload (whatever its shape)."""
    if isinstance(data, dict):
        return [t for key, value in data.items() if key != 'duration_minutes' for t in quiz_texts(value)]
    if isinstance(data, list):
        return [t for item in data for t in quiz_texts(item)]
    return [data] if isinstance(data, str) and len(data) >= 3 else []


def assert_quotes_nothing(message, payload):
    assert CANARY not in message.casefold(), message
    leaked = [t for t in quiz_texts(payload) if t in message]
    assert not leaked, (leaked, message)


# -- the mechanical model: it repairs only what a refusal names ------------------------------------------------------

def _selected(selector, count):
    return range(count) if selector == '*' else [int(i) for i in selector.split(',')]


def repair_data(data, error):
    """What a model does with a quiz refusal, done mechanically: it changes only what the refusal names."""
    data = copy.deepcopy(data)
    removed = {}
    named = re.search(r'((?:data\.\w+(?:, )?)+) (?:is not a quiz field|are not quiz fields)', error)
    for name in re.findall(r'data\.(\w+)', named.group(1)) if named else ():
        removed[name] = data.pop(name)
    if 'data.questions is required' in error:
        for value in removed.values():
            if isinstance(value, dict) and 'questions' in value:
                data.update(value)
    if 'data.duration_minutes is required' in error and 'duration_minutes' not in data:
        for name, value in removed.items():
            if 'second' in name:
                data['duration_minutes'] = value // 60
            elif re.search(r'minute|time|duration', name):
                data['duration_minutes'] = value
    if 'data.duration_minutes must be whole minutes' in error:
        data['duration_minutes'] = int(re.search(r'\d+', str(data['duration_minutes'])).group())
    seconds = re.search(r'data\.duration_minutes counts whole minutes from 1 to 180; it is (\d+)', error)
    if seconds:
        data['duration_minutes'] = int(seconds.group(1)) // 60
    if 'data.questions must be a list of 1 to 50 question objects, not an object' in error:
        data['questions'] = list(data['questions'].values())
    questions = data.get('questions') or []
    for selector, field in re.findall(r'data\.questions\[([*0-9,]+)\]\.(\w+) is not a question field', error):
        for i in _selected(selector, len(questions)):
            value = questions[i].pop(field)
            if re.search(r'answer|correct', field) and 'answer' not in questions[i]:
                questions[i]['answer'] = value
    for selector, indices in re.findall(r'data\.questions\[([*0-9,]+)\]\.choices\[([0-9,]+)\] must be strings', error):
        for i in _selected(selector, len(questions)):
            for j in map(int, indices.split(',')):
                choice = questions[i]['choices'][j]
                questions[i]['choices'][j] = choice.get('label') if isinstance(choice, dict) else str(choice)
    for selector in re.findall(r'data\.questions\[([*0-9,]+)\]\.answer must be the 0-based index', error):
        for i in _selected(selector, len(questions)):
            answer, choices = questions[i]['answer'], questions[i]['choices']
            answer = answer[0] if isinstance(answer, list) else answer
            if isinstance(answer, str):
                answer = choices.index(answer) if answer in choices else 'ABCDEF'.index(answer.strip().upper())
            questions[i]['answer'] = int(answer)
    if re.search(r'data\.questions\[\*\]\.answer: \d+ answers? falls? outside the choices', error):
        for question in questions:  # the model sees it counted from 1
            question['answer'] -= 1
    for selector in re.findall(r'data\.questions\[([*0-9,]+)\]\.explanation (?:is required|must be non-empty)', error):
        for i in _selected(selector, len(questions)):
            questions[i]['explanation'] = 'From the notes.'
    for selector in re.findall(r'data\.questions\[([*0-9,]+)\]\.choices must hold 2 to 6 choices', error):
        for i in _selected(selector, len(questions)):
            questions[i]['choices'] = questions[i]['choices'][:6]
    return data


def repair(args, error):
    """A whole UIRender call repaired from its refusal (P1 wording, json_object, then the quiz shape)."""
    args = copy.deepcopy(args)
    flattened = re.search(r'Tool request does not match its allowed schema: (.*?)\. Its arguments are', error)
    if flattened:
        undeclared = re.findall(r'(\w+) is not an argument of UIRender', flattened.group(1))
        moved = {name: args.pop(name) for name in undeclared}
        if 'data is required' in flattened.group(1):
            args['data'] = json.dumps(moved)
        return args
    if re.search(r'Tool argument data is invalid: .* not a JSON object', error):
        args['data'] = json.dumps(args['data'])
    elif 'Tool argument evidence_ids is invalid' in error or error.startswith('evidence_ids must be'):
        args.pop('evidence_ids')
    elif 'Expecting property name enclosed in double quotes' in error:
        args['data'] = json.dumps(ast.literal_eval(args['data']))
    elif 'write line breaks and tabs as \\n and \\t' in error:
        args['data'] = args['data'].replace('\n', '\\n').replace('\t', '\\t')
    elif 'encoded twice' in error:
        args['data'] = json.loads(args['data'])
    elif error.startswith('Quiz data was refused: '):
        args['data'] = json.dumps(repair_data(json.loads(args['data']), error))
    return args


# -- the accepted set is Phase 19's, exactly ---------------------------------------------------------------------

def variants():
    """A valid quiz and well over a hundred single changes to it, each at the top level or in one question."""
    yield 'valid', quiz()
    base = quiz()
    for key in ('duration_minutes', 'questions'):
        yield 'drop ' + key, {k: v for k, v in base.items() if k != key}
    for key in ('title', 'version', 'time_limit', 'id', 'answers'):
        yield 'add ' + key, {**base, key: 1}
    for minutes in (0, 1, 180, 181, -5, 5.0, '5', '5 minutes', True, None, 300, [5]):
        yield 'duration %r' % (minutes,), {**base, 'duration_minutes': minutes}
    one = base['questions'][0]
    for questions in ([], [one] * 50, [one] * 51, 'x', {}, None, {'1': one}):
        yield 'questions %s' % type(questions).__name__, {**base, 'questions': questions}
    values = {
        'question': ('', ' ', 'x' * 1500, 'x' * 1501, 5, None, ['x']),
        'explanation': ('', '\n', 'x' * 1500, 'x' * 1501, 0, None, {'text': 'x'}),
        'choices': ([], ['a'], ['a', 'b'], ['a'] * 6, ['a'] * 7, 'a,b', [1931, 1925, 1948], ['a', ''], ['a', ' '],
                    ['a', 'x' * 700], ['a', 'x' * 701], [{'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}],
                    [None, 'b'], [True, 'b'], [1.5, 'b']),
        'answer': (-1, 0, 1, 3, 4, 5, '1', 'B', True, False, 1.0, [1], None, 10 ** 30),
    }
    for index in (0, 2):
        for key in ('question', 'choices', 'answer', 'explanation'):
            questions = copy.deepcopy(base['questions'])
            del questions[index][key]
            yield f'q{index} drop {key}', {**base, 'questions': questions}
        for key in ('id', 'type', 'correct_answer'):
            questions = copy.deepcopy(base['questions'])
            questions[index][key] = 'x'
            yield f'q{index} add {key}', {**base, 'questions': questions}
        for key, options in values.items():
            for value in options:
                questions = copy.deepcopy(base['questions'])
                questions[index][key] = value
                yield f'q{index} {key}={str(value)[:20]!r}', {**base, 'questions': questions}
        for whole in ('x', [], None, 5):
            questions = copy.deepcopy(base['questions'])
            questions[index] = whole
            yield f'q{index} is {whole!r}', {**base, 'questions': questions}
    public, _ = quiz_grading.split(base)
    yield 'public v2 shape', public
    for raw in ('[]', '5', '"x"', 'null', '{', "{'duration_minutes': 5}", json.dumps(json.dumps(base)),
                json.dumps(base), json.dumps(base).replace('Halden', 'Hal\nden')):
        yield 'string ' + raw[:24], raw


def outcome(validator, data):
    try:
        return 'accepted', validator(copy.deepcopy(data))
    except ValueError:
        return 'refused', None


def test_the_accepted_set_and_the_stored_data_are_exactly_phase19s():
    cases = list(variants())
    assert len(cases) > 140
    accepted = 0
    for label, data in cases:
        expected = outcome(phase19_quiz, data)
        assert outcome(validate_quiz, data) == expected, label
        # The live artifacts.validate (Phase 19 code before P3, validate_quiz after it) agrees: graders read it.
        assert outcome(lambda d: artifacts.validate('quiz', d), data) == expected, label
        accepted += expected[0] == 'accepted'
    assert accepted >= 10  # the valid boundaries (1 and 180 minutes, 6 choices, 50 questions, 1500 characters...)


def test_mod_quizzes_still_refuse_the_public_shape_and_a_version_key():
    public, _ = quiz_grading.split(quiz())
    for data in (public, {**quiz(), 'version': 2}):
        with pytest.raises(PolicyError):
            mods.validate_artifacts([{'id': 'pane', 'title': 'Pane', 'kind': 'quiz', 'data': data}])
    assert mods.validate_artifacts([{'id': 'pane', 'title': 'Pane', 'kind': 'quiz', 'data': quiz()}])[0]['data'] == quiz()


# -- every realistic wrong shape names its paths, and the named repair is accepted ----------------------------------

def _with(change):
    data = quiz()
    change(data)
    return data


def _each(data, change):
    for question in data['questions']:
        change(question)


WRONG = {
    'title inside data': (lambda d: d.update(title='Observatory quiz'),
                          ['data.title is not a quiz field (quiz data has exactly duration_minutes and questions)']),
    'wrapped under quiz': (lambda d: d.update(quiz={'duration_minutes': d.pop('duration_minutes'),
                                                    'questions': d.pop('questions')}),
                           ['data.quiz is not a quiz field', 'data.duration_minutes is required: whole minutes',
                            'data.questions is required']),
    'duration as text': (lambda d: d.update(duration_minutes='5 minutes'),
                         ['data.duration_minutes must be whole minutes from 1 to 180 as a JSON integer, not text']),
    'duration in seconds': (lambda d: d.update(duration_minutes=300),
                            ['data.duration_minutes counts whole minutes from 1 to 180; it is 300']),
    'time limit in seconds': (lambda d: d.update(time_limit_seconds=d.pop('duration_minutes') * 60),
                              ['data.time_limit_seconds is not a quiz field', 'data.duration_minutes is required']),
    'question ids': (lambda d: _each(d, lambda q: q.update(id='q')), ['data.questions[*].id is not a question field']),
    'numeric choices': (lambda d: [q.update(choices=[float(c) if '.' in c else int(c) for c in q['choices']])
                                   for q in d['questions'][:2]],
                        ['data.questions[0,1].choices[0,1,2,3] must be strings: write every choice as JSON text, '
                         'numbers too (for example "12", not 12)']),
    'form-style options': (lambda d: _each(d, lambda q: q.update(choices=[{'id': 'o%d' % i, 'label': c}
                                                                          for i, c in enumerate(q['choices'])])),
                           ['data.questions[*].choices[0,1,2,3] must be strings']),
    'letter answers': (lambda d: _each(d, lambda q: q.update(answer='ABCD'[q['answer']])),
                       ['data.questions[*].answer must be the 0-based index of the one correct choice as a JSON '
                        'integer (0 for the first choice), not text']),
    'answer as its text': (lambda d: _each(d, lambda q: q.update(answer=q['choices'][q['answer']])),
                           ['data.questions[*].answer must be the 0-based index', 'not text']),
    'answer in a list': (lambda d: _each(d, lambda q: q.update(answer=[q['answer']])),
                         ['data.questions[*].answer must be the 0-based index', 'not a list']),
    'renamed answer': (lambda d: _each(d, lambda q: q.update(correct_answer=q.pop('answer'))),
                       ['data.questions[*].correct_answer is not a question field', 'data.questions[*].answer is required']),
    'no explanations': (lambda d: _each(d, lambda q: q.pop('explanation')), ['data.questions[*].explanation is required']),
    'blank explanation': (lambda d: d['questions'][1].update(explanation=' '),
                          ['data.questions[1].explanation must be non-empty text of at most 1500 characters']),
    'one-based answers': (lambda d: _each(d, lambda q: q.update(answer=q['answer'] + 1)),
                          ['data.questions[*].answer: 1 answer falls outside the choices; an answer counts from 0']),
    'seven choices': (lambda d: d['questions'][0]['choices'].extend(['1970', '1980', '1990']),
                      ['data.questions[0].choices must hold 2 to 6 choices; it has 7']),
    'questions keyed by number': (lambda d: d.update(questions={str(i + 1): q for i, q in enumerate(d['questions'])}),
                                  ['data.questions must be a list of 1 to 50 question objects, not an object']),
    'three mistakes at once': (lambda d: (d.update(title='Observatory'), _each(d, lambda q: q.update(answer='ABCD'[q['answer']])),
                                          d['questions'][0].update(choices=[int(c) for c in d['questions'][0]['choices']])),
                               ['data.title is not a quiz field', 'data.questions[0].choices[0,1,2,3] must be strings',
                                'data.questions[*].answer must be the 0-based index']),
}


@pytest.mark.parametrize('case', sorted(WRONG))
def test_a_realistic_wrong_quiz_names_every_problem_and_one_repair_restores_the_intended_quiz(case):
    change, fragments = WRONG[case]
    payload = _with(change)
    message = refusal(payload)
    assert message.startswith('Quiz data was refused: ') and message.endswith('.')
    for fragment in fragments:
        assert fragment in message, (fragment, message)
    assert_quotes_nothing(message, payload)
    # The publish path appends this kind's exact schema (failure-only cost).
    assert schema_error('quiz', ValueError(message)) == message + ' Schema: ' + SCHEMAS['quiz']
    repaired = repair_data(payload, message)
    expected = quiz()
    if case == 'blank explanation':
        expected['questions'][1]['explanation'] = 'From the notes.'
    elif case == 'no explanations':
        _each(expected, lambda q: q.update(explanation='From the notes.'))
    elif case == 'seven choices':
        expected['questions'][0]['choices'] += ['1970', '1980']
    assert repaired == expected, case
    assert validate_quiz(repaired) == phase19_quiz(repaired) == repaired


def test_an_answer_outside_its_choices_is_counted_never_located():
    # A person watching the progress line must not learn which question's answer is the last choice.
    payload = _with(lambda d: _each(d, lambda q: q.update(answer=q['answer'] + 1)))
    message = refusal(payload)
    assert '[*].answer: 1 answer falls outside' in message
    assert not re.search(r'questions\[[0-9,]+\]', message), message
    two = _with(lambda d: [q.update(answer=4) for q in d['questions'][:2]])
    assert 'data.questions[*].answer: 2 answers fall outside the choices' in refusal(two)


def test_no_refusal_quotes_quiz_text_and_long_or_odd_input_stays_bounded():
    for label, data in variants():
        if outcome(phase19_quiz, data)[0] == 'refused':
            message = refusal(data)
            assert_quotes_nothing(message, data if isinstance(data, (dict, list)) else quiz())
            assert len(message) <= 1500, (label, len(message))
    many = {'duration_minutes': 5, 'questions': [{'question': 'q', 'choices': ['a', 'b'], 'answer': 0,
                                                   'explanation': 'e', 'note%d' % i: 1} for i in range(50)]}
    message = refusal(many)
    assert message.endswith('; and 42 more.') and len(message) <= 1500  # at most eight problems, then a count
    odd = {**quiz(), 'the answer is 1931': True}
    assert 'an unnamed field is not a quiz field' in refusal(odd) and '1931' not in refusal(odd)


@pytest.mark.parametrize('raw,fragments,recovers', [
    (repr(quiz()),
     ['View must contain valid JSON: data stops at line 1 column 2 (Expecting property name enclosed in double quotes).'],
     True),
    (json.dumps(quiz()).replace('Halden Works', 'Halden\nWorks'),
     ['View must contain valid JSON: data stops at line 1 column', '(Invalid control character at)',
      'Inside JSON strings, write line breaks and tabs as \\n and \\t.'], True),
    (json.dumps(json.dumps(quiz())), ['View must be a JSON object: data holds a JSON string, so it was encoded twice.'],
     True),
    (json.dumps(quiz()['questions']), ['View must be a JSON object {...}: data is a list.'], False),
    ('5', ['View must be a JSON object {...}: data is an integer.'], False),
    (json.dumps(quiz())[:-3], ['View must contain valid JSON: data stops at line 1 column'], False),
], ids=['python quotes', 'raw line break', 'encoded twice', 'questions only', 'a number', 'cut off'])
def test_data_that_is_not_one_json_object_says_where_and_what(raw, fragments, recovers):
    message = refusal(raw)
    for fragment in fragments:
        assert fragment in message, (fragment, message)
    assert_quotes_nothing(message, quiz())
    if recovers:  # otherwise the model must write the object again; the kind's schema rides the failure
        data = validate_quiz(repair({'title': 'Observatory', 'kind': 'quiz', 'data': raw}, message)['data'])
        assert data == phase19_quiz(data) and data['questions'][1] == quiz()['questions'][1]


def test_the_96_kb_bound_and_its_message_are_unchanged():
    with pytest.raises(ValueError, match=r'^View must be a JSON object smaller than 96 KB\.$'):
        json_object('"' + 'x' * 96001 + '"')


def test_evidence_ids_accepts_none_blank_null_or_a_json_array_and_names_its_shape_otherwise():
    for value in (None, '', '  ', 'null', '[]'):
        assert evidence_argument(value) == []
    assert evidence_argument('["2:call_1"]') == ['2:call_1']
    for value in ('notes.md', '2:call_1', '{}', '"x"', '[', 5, ['2:call_1'], {'id': 'x'}):
        with pytest.raises(ValueError, match=r'^evidence_ids must be a JSON array string of receipt_id values'):
            evidence_argument(value)


# -- the description and briefs: nothing grows at 4K; above 4K only the quiz types ------------------------------------

def test_the_catalogue_names_the_quiz_types_and_the_4k_brief_is_phase19s():
    description = next(t for t in TOOLS if t['function']['name'] == 'UIRender')['function']['description']
    assert QUIZ_TEXT in description and PHASE19_QUIZ_TEXT not in description
    assert GROWTH == 37
    assert UI_BRIEF == PHASE19_UI_BRIEF and SMALL_BRIEF['UIRender'] == UI_BRIEF
    assert SMALL_BRIEF['UIUpdate'] == 'Update your view by id+revision; same data; pending forms first.'


def test_the_failure_only_quiz_schema_is_inert_bounded_and_its_example_is_accepted():
    text = SCHEMAS['quiz']
    # Paid only by a refused quiz call; an exact pin, so growth needs review.
    phase19 = ('quiz:{duration_minutes:1-180,questions:[{question,choices:[2-6 texts],answer:choice index,explanation}]} '
               '(1-50). The host keeps answer and explanation out of the view and grades attempts.')
    assert (len(phase19), len(text)) == (187, 374) and not REJECTED_TEXT.search(_fold(text))
    example, _ = json.JSONDecoder().raw_decode(text, text.index('Example: ') + len('Example: '))
    assert validate_quiz(example) == phase19_quiz(example) == example
    for words in ('whole minutes 1-180', 'choices:[2-6 strings]', 'answer:0-based index', 'exactly these keys'):
        assert words in text


@pytest.fixture
def app(tmp_path):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    value.model.complete = lambda *args, **kwargs: pytest.fail('No model inference here.')
    value.events = events
    groups = value.store.setting('permissions', DEFAULT)['tools']
    value.setting('permissions', {'mode': 'ask', 'tools': dict.fromkeys(groups, True)})
    yield value
    value.active = value.active_run = None
    value.close()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def call(args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': 'UIRender', 'arguments': json.dumps(args)}}]}


def run(app, budget, text, script):
    """One task on a local fixture model; returns the chat, its run and every request (tools by name, messages)."""
    app.model_save(name='Quiz fixture', url='http://127.0.0.1:9', model='fixture-%d' % budget, context=budget)
    assert app.model.current()['budget'] == budget
    seen = []

    def complete(messages, tools, cancel, token, effort):
        seen.append(({t['function']['name']: copy.deepcopy(t) for t in tools}, copy.deepcopy(messages)))
        reply = script(len(seen), messages)
        if reply is None:
            token('The quiz is ready.')
            return {'role': 'assistant', 'content': 'The quiz is ready.'}
        return reply
    app.model.complete = complete
    chat = app.new_chat()
    app.work(app.chat(chat['id']), text, False, [])
    assert seen, 'the fixture model was never asked'
    [run_row] = app.runs.list(chat['id'])
    return chat['id'], run_row['id'], seen


def test_offered_view_bytes_do_not_grow_at_4k_and_grow_by_the_quiz_types_above(app):
    for budget, text, expected in ((4096, REQUEST, PINNED_4K_BYTES),
                                   *((16384, WIDE_TEXTS[case], {'UIRender': PHASE19_WIDE_BYTES[case]['UIRender'] + GROWTH,
                                                                'UIUpdate': PHASE19_WIDE_BYTES[case]['UIUpdate']})
                                     for case in WIDE_TEXTS)):
        _, _, seen = run(app, budget, text, lambda step, messages: None)
        for tools, _ in seen:
            assert {name: len(compact(tools[name]).encode()) for name in VIEWS} == expected, (budget, text)


# -- parent patches P1 and P3, run from the note's exact code until merged ---------------------------------------------

def p1_argument_refusal(name, spec, args, key=None):
    """Phase 21 (F21-quiz): name each refused argument and its expected shape, never its value."""
    if key is None:
        def label(k):
            return k if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', k) else json.dumps(k[:64])
        problems = [f'{k} is required' for k in spec['required'] if k not in args]
        problems += [f'{label(k)} is not an argument of {name}' for k in args if k not in spec['properties']][:8]
        return ('Tool request does not match its allowed schema: ' + '; '.join(problems)
                + '. Its arguments are ' + ', '.join(spec['properties']) + '.')
    prop, value = spec['properties'][key], args[key]
    if prop['type'] == 'string':
        expected = 'a string of at most 96000 characters'
        if isinstance(value, (dict, list)):
            shape = 'object' if isinstance(value, dict) else 'array'
            expected += f', not a JSON {shape}: send the {shape} JSON-encoded as one string'
    elif prop['type'] == 'boolean':
        expected = 'true or false'
    else:
        expected = f"an integer from {prop.get('minimum', 1)} to {prop.get('maximum', 10000)}"
    return f'Tool argument {key} is invalid: it must be {expected}.'


def p1_validate(name, args, available):
    spec = next((t['function']['parameters'] for t in available if t['function']['name'] == name), None)
    if not spec or not isinstance(args, dict):
        raise PolicyError('Tool request does not match its allowed schema.')
    if not set(spec['required']).issubset(args) or not set(args).issubset(spec['properties']):
        raise PolicyError(App._argument_refusal(name, spec, args))
    for key, value in args.items():
        prop = spec['properties'][key]
        valid = ((isinstance(value, str) and len(value) <= 96000) if prop['type'] == 'string' else (
            type(value) is bool if prop['type'] == 'boolean'
            else type(value) == int and prop.get('minimum', 1) <= value <= prop.get('maximum', 10000)))
        if 'enum' in prop and value not in prop['enum']:
            raise PolicyError(f'Tool argument {key} must be one of: ' + ', '.join(map(str, prop['enum']))
                              + '. Use the required value on your next call.')
        if not valid:
            raise PolicyError(App._argument_refusal(name, spec, args, key))


@pytest.fixture
def merged(monkeypatch):
    """Parent patches P1 (App.validate wording) and P3 (artifacts.validate's quiz branch), from the note's exact code,
    applied only while the merged source lacks them; after the merge this changes nothing."""
    if not hasattr(App, '_argument_refusal'):
        monkeypatch.setattr(App, '_argument_refusal', staticmethod(p1_argument_refusal), raising=False)
        monkeypatch.setattr(App, 'validate', staticmethod(p1_validate))
    if not hasattr(artifacts, 'validate_quiz'):
        original = artifacts.validate

        def p3_validate(kind, data):
            return validate_quiz(json_object(data)) if kind == 'quiz' else original(kind, data)
        for module in (artifacts, artifact_inputs, mods):
            monkeypatch.setattr(module, 'validate', p3_validate)


def uirender_spec():
    return next(t for t in TOOLS if t['function']['name'] == 'UIRender')


@pytest.mark.parametrize('args,expected', [
    ({'title': 'Quiz', 'kind': 'quiz', 'data': quiz()},
     'Tool argument data is invalid: it must be a string of at most 96000 characters, not a JSON object: send the '
     'object JSON-encoded as one string.'),
    ({'title': 'Quiz', 'kind': 'quiz', 'data': json.dumps(quiz()), 'evidence_ids': []},
     'Tool argument evidence_ids is invalid: it must be a string of at most 96000 characters, not a JSON array: send '
     'the array JSON-encoded as one string.'),
    ({'title': 'Quiz', 'kind': 'quiz', **quiz()},
     'Tool request does not match its allowed schema: data is required; duration_minutes is not an argument of '
     'UIRender; questions is not an argument of UIRender. Its arguments are title, kind, data, evidence_ids.'),
    ({'title': 'Quiz', 'kind': 'quiz', 'data': '{}', 'a b': 1},
     'Tool request does not match its allowed schema: "a b" is not an argument of UIRender. Its arguments are '
     'title, kind, data, evidence_ids.'),
])
def test_p1_names_the_argument_and_its_shape_never_its_value(merged, args, expected):
    with pytest.raises(PolicyError) as caught:
        App.validate('UIRender', args, [uirender_spec()])
    assert str(caught.value) == expected
    assert_quotes_nothing(str(caught.value), args)
    # The prefixes earlier validators and tests match stay (validate_phase14/16/17, test_agent_context).
    assert str(caught.value).startswith(('Tool argument ', 'Tool request does not match its allowed schema'))
    revision = next(t for t in TOOLS if t['function']['name'] == 'UIUpdate')
    with pytest.raises(PolicyError, match=r'^Tool argument revision is invalid: it must be an integer from 1 to 15\.$'):
        App.validate('UIUpdate', {'id': 'v', 'revision': True, 'title': 'Q', 'kind': 'quiz', 'data': '{}'}, [revision])
    with pytest.raises(PolicyError, match=r'^Tool request does not match its allowed schema\.$'):
        App.validate('UIRender', args, [])  # a tool that is not offered keeps its exact Phase 16 refusal


# -- one retry through the real runner, at 4K and above ---------------------------------------------------------------

def drive(app, budget, first):
    """A model that sends `first`, then repairs each refusal from its text alone (at most three calls)."""
    calls, errors = [first], []

    def script(step, messages):
        results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        if step == 1:
            return call(first, 'c1')
        if results and 'error' in results[-1] and len(calls) < 3:
            errors.append(results[-1]['error'])
            calls.append(repair(calls[-1], results[-1]['error']))
            return call(calls[-1], 'c%d' % len(calls))
        return None
    cid, rid, seen = run(app, budget, REQUEST, script)
    return cid, rid, seen, calls, errors


FIRST = {
    'data as a JSON object': lambda: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': quiz()},
    'flattened arguments': lambda: {'title': 'Observatory quiz', 'kind': 'quiz', **quiz()},
    'title and numeric choices': lambda: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': json.dumps(
        _with(lambda d: (d.update(title='Observatory'), d['questions'][0].update(choices=[1925, 1931, 1948, 1962]))))},
    'letter answers': lambda: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': json.dumps(
        _with(lambda d: _each(d, lambda q: q.update(answer='ABCD'[q['answer']]))))},
    'python quotes': lambda: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': repr(quiz())},
    'one-based answers': lambda: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': json.dumps(
        _with(lambda d: _each(d, lambda q: q.update(answer=q['answer'] + 1))))},
}


KEY_OUTSIDE_DATA = {'flattened arguments'}  # the refused call carries its key outside data.questions[].answer


def redaction_allowlisted():
    """Whether parent patch P6 (F21-Q1) is in: window records keep only a quiz call's public fields."""
    probe = quiz_grading.redact_quiz_arguments('UIRender', {'title': 't', 'kind': 'quiz', 'questions': [
        {'question': 'q', 'choices': ['a', 'b'], 'answer': 0, 'explanation': 'x'}]})
    return 'questions' not in probe


PUBLIC_FIELDS = {'title', 'kind', 'id', 'revision', 'evidence_ids', 'data', 'duration_minutes', 'questions',
                 'question', 'choices'}
REFUSED_CALLS = {  # reasonable refused quiz calls whose answer key sits outside data.questions[].answer
    'flattened arguments': {'title': 'Q', 'kind': 'quiz', **quiz()},
    'renamed answer': {'title': 'Q', 'kind': 'quiz', 'data': json.dumps(
        _with(lambda d: _each(d, lambda q: q.update(correct_answer=q.pop('answer')))))},
    'answers beside the questions': {'title': 'Q', 'kind': 'quiz', 'data': json.dumps({**quiz(), 'answers': [1, 1, 3]})},
    'renamed explanation': {'title': 'Q', 'kind': 'quiz', 'data': json.dumps(
        _with(lambda d: _each(d, lambda q: q.update(rationale=q.pop('explanation')))))},
}


def field_names(value):
    if isinstance(value, str) and value[:1] in '{[':
        try:
            value = json.loads(value)
        except ValueError:
            return set()
    if isinstance(value, dict):
        return set(value) | set().union(*(field_names(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(field_names(v) for v in value)) if value else set()
    return set()


@pytest.mark.parametrize('case', sorted(REFUSED_CALLS))
def test_f21_q1_window_records_of_a_refused_quiz_call_keep_only_public_fields(case):
    args = REFUSED_CALLS[case]
    # Every one is refused, and every call of a committed response is journaled with its arguments (Runs.response).
    with pytest.raises((ValueError, PolicyError)):
        validate_quiz(args['data']) if 'data' in args else App.validate('UIRender', args, [uirender_spec()])
    extra = field_names(quiz_grading.redact_quiz_arguments('UIRender', args)) - PUBLIC_FIELDS
    if redaction_allowlisted():
        assert not extra and CANARY not in json.dumps(quiz_grading.redact_quiz_arguments('UIRender', args)).casefold()
    else:
        # Finding F21-Q1, pinned until parent patch P6: the denylist keeps these answer-bearing fields.
        assert extra & {'answer', 'explanation', 'correct_answer', 'answers', 'rationale'}, (case, extra)
    # A valid call is redacted exactly as in Phase 19 either way.
    valid = {'title': 'Q', 'kind': 'quiz', 'data': json.dumps(quiz())}
    public, _ = quiz_grading.split(quiz())
    assert json.loads(quiz_grading.redact_quiz_arguments('UIRender', valid)['data']) == {
        'duration_minutes': 5, 'questions': [{k: q[k] for k in ('question', 'choices')} for q in public['questions']]}


@pytest.mark.parametrize('budget', [4096, 32768])
@pytest.mark.parametrize('case', sorted(FIRST))
def test_a_refused_quiz_is_published_by_one_corrected_call(app, merged, budget, case):
    cid, rid, seen, calls, errors = drive(app, budget, FIRST[case]())
    assert len(errors) == 1 and len(calls) == 2, (case, errors)
    if errors[0].startswith(('Quiz data was refused', 'View must')):
        assert errors[0].endswith(' Schema: ' + SCHEMAS['quiz'])  # the kind's schema rides the failure, at 4K too
    [view] = app.artifacts.list(cid)
    assert view['kind'] == 'quiz' and quiz_grading.is_public(view['data'])
    with app.store.lock:
        key = quiz_grading.export_key(app.store.db, cid, view['id'], view['revision'], view['data'])
    assert key == quiz_grading.split(quiz())[1]  # the person's quiz is graded against the intended key
    results = [json.loads(m['content']) for m in seen[-1][1] if m['role'] == 'tool']
    assert 'error' in results[0] and results[1]['grading'] == 'host' and results[1]['question_ids'] == ['q1', 'q2', 'q3']
    # Nothing a window reads carries an explanation: the refusal, the progress line and the redacted call records.
    blocked = [e[1]['label'] for e in app.events if e[0] == 'progress' and e[1].get('kind') == 'blocked']
    assert blocked == errors and CANARY not in json.dumps(blocked).casefold()
    records = json.dumps(app.run_get(cid, rid)['tools'], ensure_ascii=False).casefold()
    if case in KEY_OUTSIDE_DATA and not redaction_allowlisted():
        # Finding F21-Q1 (the note, parent patch P6): redact_quiz_arguments removes answer and explanation from
        # data.questions only, so a refused call whose key sits elsewhere stays readable in both windows' records.
        # Pinned here so it is visible; after P6 the branch below applies to every case.
        assert CANARY in records
    else:
        assert CANARY not in records


# -- the real ui-quiz task through the live-shape driver and the real grader ------------------------------------------

class RepairingShapes(shapes.LiveShapes):
    """The live-shape model, except that Pebble's first quiz call is wrong in one realistic way, then repaired."""

    def __init__(self, first):
        super().__init__()
        self.first, self.errors = first, []

    def task_ui_quiz(self, view, task, baseline):
        renders = view.results_of('UIRender')
        if baseline or not view.called('Read') or not view.seen('1931', '1.2 metre', 'Arlow Engineering'):
            return super().task_ui_quiz(view, task, baseline)
        if not renders:
            return view.say('Making the quiz view.', ('UIRender', self.first(shapes.QUIZ)))
        args, result = renders[-1]
        if isinstance(result, dict) and 'error' in result and len(renders) < 3:
            self.errors.append(result['error'])
            return view.say('Fixing the quiz data.', ('UIRender', repair(args, result['error'])))
        return self.finish(view, task, 'The three-question quiz is ready.', 'FINAL: quiz ready')


LIVE_FIRST = {
    'numeric choices': lambda q: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': json.dumps(
        {**q, 'questions': [{**q['questions'][0], 'choices': [int(c) for c in q['questions'][0]['choices']]},
                            *q['questions'][1:]]})},
    'data as a JSON object': lambda q: {'title': 'Observatory quiz', 'kind': 'quiz', 'data': copy.deepcopy(q)},
}


@pytest.mark.parametrize('case', sorted(LIVE_FIRST))
def test_the_live_shaped_ui_quiz_passes_its_grader_after_one_refusal(tmp_path, monkeypatch, merged, case):
    shapes._loopback_only(monkeypatch)
    shapes._pin_source(monkeypatch)
    script = RepairingShapes(LIVE_FIRST[case])
    result = shapes.paired_cells(tmp_path, monkeypatch, script, 'ui-quiz')
    pebble, baseline = result.cells[('ui-quiz', 'pebble')], result.cells[('ui-quiz', 'baseline')]
    assert len(script.errors) == 1, script.errors
    calls = [args for e in script.log if e['actor'] == 'root' for name, args in e['tools'] if name == 'UIRender']
    assert len(calls) == 2
    assert pebble['outcome'] == 'passed', shapes.why(shapes.describe(pebble))
    assert pebble['grader']['checks'] == baseline['grader']['checks'], shapes.why(shapes.describe(pebble))
    assert c18.ORACLES['ui-quiz']['duration'] == shapes.QUIZ['duration_minutes']


# -- after the merge (I21): P1-P4 and P6 are in the source, so `merged` changes nothing ---------------------------------

def test_merged_patches_are_wired():
    from pebble import app as app_module
    assert hasattr(App, '_argument_refusal') and hasattr(artifacts, 'validate_quiz')
    assert getattr(app_module, 'evidence_argument', None) is evidence_argument and redaction_allowlisted()
    with pytest.raises(ValueError, match=re.escape('data.questions[0].choices[0,1,2,3] must be strings')):
        artifacts.validate('quiz', _with(lambda d: d['questions'][0].update(choices=[1925, 1931, 1948, 1962])))


@pytest.fixture
def owner(app):
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Quiz.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    app.active, app.active_run = chat['id'], rid
    yield chat['id']
    app.active = app.active_run = None


def render(app, cid, n, **args):
    return app.execute('UIRender', {'title': 'Quiz', 'kind': 'quiz', 'data': json.dumps(quiz()), **args}, None, cid,
                       call_id=f'{n}:uirender')


def test_merged_p2_blank_or_null_evidence_ids_publish_and_a_file_name_is_named(app, owner):
    for n, evidence in enumerate(('', ' ', 'null', '[]'), 1):
        assert render(app, owner, n, evidence_ids=evidence)['grading'] == 'host'
    with pytest.raises(PolicyError, match=r'^evidence_ids must be a JSON array string of receipt_id values'):
        render(app, owner, 9, evidence_ids='notes.md')


def test_merged_p4_a_long_title_names_the_argument(app, owner):
    with pytest.raises(PolicyError, match=r'^Tool argument title must be non-empty text of at most 120 characters\.$'):
        render(app, owner, 1, title='x' * 121)


@pytest.mark.parametrize('case', sorted(REFUSED_CALLS))
def test_merged_p6_no_window_record_of_a_refused_quiz_call_carries_its_key(app, case):
    # F21-Q1 through the real runner: the refused call is journaled with its arguments (recovery reads them), and
    # every record either window reads keeps only the call's public fields.
    cid, rid, seen = run(app, 32768, REQUEST,
                         lambda step, messages: call(REFUSED_CALLS[case], 'c1') if step == 1 else None)
    results = [json.loads(m['content']) for m in seen[-1][1] if m['role'] == 'tool']
    assert len(results) == 1 and 'error' in results[0] and not app.artifacts.list(cid)
    for method, params in (('run_get', {'id': cid, 'run_id': rid}), ('run_events', {'id': cid, 'run_id': rid}),
                           ('get_chat', {'id': cid}), ('state', {})):
        assert CANARY not in compact(app.dispatch(method, params)).casefold(), method
    raw = app.store.query('SELECT payload FROM run_events WHERE run_id=?', (rid,))
    assert CANARY in compact(raw).casefold()  # the stored journal keeps the model's own call


def test_merged_p6_a_non_finite_number_withholds_the_data_and_never_leaves_the_call_unredacted():
    # json.loads accepts NaN and Infinity inside a data string (the Responses route refuses them only at the top level
    # of the arguments). Encoding them raised outside the redaction's try, so redact_response kept the whole call.
    withheld = {'title': 'Q', 'kind': 'quiz', 'data': '[quiz data withheld: answer key]'}
    for minutes in (float('nan'), float('inf')):
        for data in ({**quiz(), 'duration_minutes': minutes}, json.dumps({**quiz(), 'duration_minutes': minutes})):
            arguments = {'title': 'Q', 'kind': 'quiz', 'data': data}
            assert quiz_grading.redact_quiz_arguments('UIRender', arguments) == withheld
            response = call(arguments, 'c1')
            shown = quiz_grading.redact_archive({'response': response})['response']
            assert json.loads(shown['tool_calls'][0]['function']['arguments']) == withheld
            assert CANARY not in compact(shown).casefold() and CANARY in compact(response).casefold()
