"""Bounded declarative display data, never host controls or authority.

Native forms collect ordinary task input. A field called ``approval`` still
produces a string; it cannot manufacture a permission, input origin or RPC.
"""

import json
import math
import re
import unicodedata

from .interaction import ID, RESERVED, SECRET, SECRET_REQUEST, bounded_text


NATIVE_KINDS = frozenset({'form', 'table', 'chart', 'plan', 'evidence', 'dashboard', 'prototype'})
MAX_DATA_BYTES = 96000
_CREDENTIAL_FIELD = re.compile(r'\b(?:password|passphrase|api[ _-]?key|secret[ _-]?key|access[ _-]?token|private[ _-]?key|credentials?)\b', re.I)

# The UIRender/UIUpdate enum offered at a 4K budget stays exactly these kinds.
LEGACY_KINDS = ('quiz', 'diagram', 'html', 'form', 'table', 'chart', 'plan', 'evidence')
NEW_KINDS = ('dashboard', 'prototype')
PANEL_KINDS = ('metric', 'table', 'chart', 'plan', 'evidence')
MAX_PANELS, MAX_SCREENS, MAX_COMPONENTS = 8, 12, 30
VIEWPORTS = ('phone', 'tablet', 'desktop')
THEME = {'accent': ('teal', 'blue', 'indigo', 'violet', 'rose', 'orange', 'amber', 'green', 'slate'),
         'density': ('compact', 'cozy', 'comfortable'), 'radius': (0, 4, 8, 12, 16)}
ASPECTS = ('1:1', '4:3', '3:2', '16:9', '9:16', '3:4', '21:9')
# Component type -> (required keys, optional keys). Exports use the same shapes.
COMPONENTS = {'heading': ({'type', 'text'}, set()), 'button': ({'type', 'label'}, {'goto'}),
              'input': ({'type', 'label'}, {'placeholder'}), 'toggle': ({'type', 'label'}, set()),
              'list': ({'type', 'items'}, set()), 'card': ({'type', 'title', 'body'}, set()),
              'image_placeholder': ({'type', 'label'}, {'aspect'}), 'nav': ({'type', 'items'}, set())}
# Keys that would name a link, resource, style, markup, handler or authority. Matched after NFKC,
# casefolding and removal of control, format and space characters, so full-width or zero-width
# spellings of 'href' or 'onclick' are refused too.
REJECTED_KEYS = frozenset({'href', 'url', 'uri', 'src', 'srcset', 'srcdoc', 'action', 'formaction', 'style', 'css',
                           'html', 'innerhtml', 'outerhtml', 'script', 'iframe', 'xlink:href', 'background', 'poster',
                           'target', 'rpc', 'invoke', 'approve', 'permission', 'origin'})
# Text that only matters if something interprets it: URLs with an authority, executable or data
# schemes, markup, numeric character references, CSS loaders, template interpolation, inline
# handlers and the native bridge. Checked on the NFKC/casefolded text without control characters.
REJECTED_TEXT = re.compile(
    r'[a-z][a-z0-9+.-]*://|^//|(?:java|vb|live)script:\S|(?:mailto|tel|sms|blob|filesystem|about|intent|file):\S'
    r'|data:(?:[a-z]+/|,)|<[a-z!/?]|&#|&(?:colon|tab|newline);|url\(|expression\(|@import|-moz-binding'
    r'|behavior\s*:|\{\{|\$\{|\bon[a-z]{2,}\s*=|__tauri|postmessage')

SCHEMAS = {
    'form': 'form:{fields:[{id,label,type:"text"|"choice",required:true,options?:[{id,label}]}],description?,submit_label?} '
            '(1-3 required ordinary questions; free text always available).',
    'table': 'table:{columns:[{id,label}] (1-12),rows:[objects keyed by column id] (<=200),caption?}.',
    'chart': 'chart:{type:"bar"|"line",x_key,y_keys:[1-6 keys],rows:[objects] (1-200),labels?,caption?}.',
    'plan': 'plan:{steps:[{id,title,status:"pending"|"in_progress"|"blocked"|"completed",evidence_ids?}]} (1-20); '
            'completed steps need resolved receipt IDs.',
    'evidence': 'evidence:{items:[{id,label,evidence_id,summary?}] (1-30),caption?}.',
    # Phase 21 (F21-quiz): paid only on a failure, so it spells out the types and carries one short example.
    'quiz': 'quiz:{duration_minutes:whole minutes 1-180,questions:[{question,choices:[2-6 strings],answer:0-based '
            'index of the one correct choice,explanation}] (1-50)}, exactly these keys. Example: '
            '{"duration_minutes":5,"questions":[{"question":"2+2?","choices":["3","4"],"answer":1,'
            '"explanation":"2+2 is 4."}]}. The host keeps answer and explanation out of the view and grades attempts.',
    'diagram': 'diagram:{nodes:[{id,label}] (1-40),edges:[{from,to,label}] (<=80)}.',
    'html': 'html:{html,css?}: bounded static preview; no scripts.',
    'dashboard': 'dashboard:{panels:[{id,title,kind:"metric"|"table"|"chart"|"plan"|"evidence",data:{...} OR '
                 'binding:{source,params?}}] (1-8),columns?:1|2|3}. metric data:{value:number,unit?,'
                 'delta?:number,caption?}; other panel data uses that kind\'s schema. Binding sources (counts only, '
                 'this chat): runs.recent{limit?:1-50}, runs.usage, jobs.fires{days?:1-90}, agents.tree, '
                 'artifacts.revisions, memory.stats, git.summary, goals.progress; params may add field for a metric.',
    'prototype': 'prototype:{viewport?:"phone"|"tablet"|"desktop",start:screen id,theme?:{accent?,density?,radius?},'
                 'screens:[{id,title,components:[...] (<=30)}] (1-12)}. Components: heading{text}, button{label,goto?}, '
                 'input{label,placeholder?}, toggle{label}, list{items:[texts]}, card{title,body}, '
                 'image_placeholder{label,aspect?}, nav{items:[{label,goto}]}; goto names a screen. '
                 'No links, URLs, styles, markup, scripts or handlers.',
}
UI_BRIEF = ('Save a native view; data is a JSON object string. Kinds: form table chart plan evidence quiz diagram '
            'html. A schema error returns that kind\'s exact schema.')
DASHBOARD_WORDS = re.compile(r'\b(?:dashboards?|kpis?|scorecards?|status boards?)\b', re.I)
PROTOTYPE_WORDS = re.compile(r'\b(?:prototypes?|mock-?ups?|wireframes?|app demos?|click-?through)\b', re.I)


def offered_kinds(budget, text):
    """Kinds for this request's UIRender/UIUpdate enum: nothing new at a budget of 4096 or less."""
    kinds = list(LEGACY_KINDS)
    if type(budget) is int and budget > 4096 and isinstance(text, str):
        kinds += [kind for kind, words in (('dashboard', DASHBOARD_WORDS), ('prototype', PROTOTYPE_WORDS)) if words.search(text)]
    return tuple(kinds)


def schema_error(kind, error):
    """A validation failure carries the exact schema for that one kind, so it is paid only on failure."""
    message = str(error)
    return message + ' Schema: ' + SCHEMAS[kind] if kind in SCHEMAS and 'Schema: ' not in message else message


def _fold(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(c for c in value if unicodedata.category(c) not in ('Cc', 'Cf'))


def rejected_keys(value):
    """Refuse link, style, markup, handler and authority keys at any depth, NFKC first."""
    if type(value) is dict:
        for key, item in value.items():
            folded = ''.join(c for c in _fold(key) if not c.isspace())
            if folded in REJECTED_KEYS or folded.startswith('on'):
                raise ValueError('Views cannot carry links, styles, markup, scripts or event handlers.')
            rejected_keys(item)
    elif type(value) is list:
        for item in value:
            rejected_keys(item)


def inert_text(value, limit):
    """Bounded display text that nothing could interpret as a link, markup, script or template."""
    value = _label(value, limit)
    if REJECTED_TEXT.search(_fold(value)):
        raise ValueError('Views show plain text only: links, URLs, markup, scripts and templates are refused.')
    return value


def _json_type(value):
    """The JSON type of a value, for a refusal that must not quote the value itself."""
    return next((name for kind, name in ((bool, 'true or false'), (int, 'an integer'), (float, 'a decimal number'),
                                         (str, 'text'), (list, 'a list'), (dict, 'an object'), (type(None), 'null'))
                 if type(value) is kind), 'another value')


def json_object(value):
    """Copy strict JSON and bound nesting before any persisted/generated use."""
    if isinstance(value, str):
        if len(value.encode()) > MAX_DATA_BYTES:
            raise ValueError('View must be a JSON object smaller than 96 KB.')
        try:
            value = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Use finite JSON numbers.')))
        except json.JSONDecodeError as exc:
            # Phase 21 (F21-quiz): where the parser stopped, so one corrected call can succeed. The decoder's
            # message is fixed text (never the data), and a position names no value.
            hint = (' Inside JSON strings, write line breaks and tabs as \\n and \\t.'
                    if exc.msg.startswith('Invalid control character') else '')
            raise ValueError(f'View must contain valid JSON: data stops at line {exc.lineno} column {exc.colno} '
                             f'({exc.msg}).{hint}') from None
        except (TypeError, RecursionError):
            raise ValueError('View must contain valid JSON.') from None
        if isinstance(value, str):
            raise ValueError('View must be a JSON object: data holds a JSON string, so it was encoded twice. '
                             'Encode the object once.')
    if type(value) is not dict and type(value) in (list, int, float, bool, type(None)):
        raise ValueError(f'View must be a JSON object {{...}}: data is {_json_type(value)}.')
    nodes = 0

    def check(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 8 or nodes > 12000:
            raise ValueError('View data is too deeply nested or complex.')
        if type(item) is dict:
            if len(item) > 200 or any(type(key) is not str or len(key) > 120 for key in item):
                raise ValueError('View objects need bounded string keys.')
            for key, val in item.items():
                if key in RESERVED:
                    raise ValueError('Reserved object keys are not allowed in views.')
                check(val, depth + 1)
        elif type(item) is list:
            if len(item) > 1000:
                raise ValueError('View arrays are too large.')
            for val in item:
                check(val, depth + 1)
        elif type(item) not in (str, bool, int, float, type(None)):
            raise ValueError('Views accept JSON text and primitives only.')
        elif type(item) in (int, float) and (abs(item) > 1e100 or not math.isfinite(item)):
            raise ValueError('Use bounded finite JSON numbers.')
    if type(value) is not dict:
        raise ValueError('View must be a JSON object smaller than 96 KB.')
    check(value, 0)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > MAX_DATA_BYTES:
        raise ValueError('View must be a JSON object smaller than 96 KB.')
    return json.loads(raw)


def identifier(value, label='Field'):
    if not isinstance(value, str) or not ID.fullmatch(value) or value in RESERVED:
        raise ValueError(f'{label} IDs must be short stable identifiers.')
    return value


def _shape(value, required, optional=()):
    if type(value) is not dict or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError('Unsupported declarative view fields.')


def _label(value, limit=240):
    value = bounded_text(value, limit)
    if SECRET.search(value):
        raise ValueError('Credentials belong in native connection settings, not views.')
    return value


def _optional_text(data, key, limit):
    if key in data:
        data[key] = _label(data[key], limit)


def validate_native_title(kind, title):
    title = inert_text(title, 120) if kind in NEW_KINDS else _label(title, 120)
    if kind == 'form' and (SECRET_REQUEST.search(title) or _CREDENTIAL_FIELD.search(title)):
        raise ValueError('Credentials belong in native connection settings, not forms.')
    return title


def _unique(items, label):
    ids = [identifier(item.get('id'), label) for item in items if type(item) is dict]
    if len(ids) != len(items) or len(set(ids)) != len(ids):
        raise ValueError(f'{label} IDs must be unique.')
    return set(ids)


def _primitive(value):
    if type(value) not in (str, bool, int, float, type(None)) or isinstance(value, str) and len(value) > 2000:
        raise ValueError('Table cells must be bounded JSON primitives.')


def validate_native(kind, value):
    data = json_object(value)
    # Scan every display string, including captions, cell values and IDs.
    if SECRET.search(json.dumps(data, ensure_ascii=False)):
        raise ValueError('Credentials belong in native connection settings, not views.')
    if kind == 'form':
        _shape(data, {'fields'}, {'description', 'submit_label'})
        fields = data['fields']
        if type(fields) is not list or not 1 <= len(fields) <= 3:
            raise ValueError('Forms need one to three required fields.')
        _unique(fields, 'Field')
        _optional_text(data, 'description', 1500)
        _optional_text(data, 'submit_label', 80)
        for field in fields:
            _shape(field, {'id', 'label', 'type'}, {'required', 'options', 'placeholder'})
            field['label'] = _label(field['label'], 240)
            field.setdefault('required', True)
            if field['required'] is not True or field['type'] not in ('text', 'choice'):
                raise ValueError('Form fields accept required text or choice input.')
            _optional_text(field, 'placeholder', 240)
            if field['type'] == 'text':
                if 'options' in field:
                    raise ValueError('Text fields do not accept options.')
            else:
                choices = field.get('options')
                if type(choices) is not list or not 2 <= len(choices) <= 6:
                    raise ValueError('Choice fields need two to six options.')
                _unique(choices, 'Option')
                for option in choices:
                    _shape(option, {'id', 'label'})
                    option['label'] = _label(option['label'], 120)
        prompt = json.dumps(data, ensure_ascii=False)
        if SECRET_REQUEST.search(prompt) or _CREDENTIAL_FIELD.search(prompt):
            raise ValueError('Credentials belong in native connection settings, not forms.')
    elif kind == 'table':
        _shape(data, {'columns', 'rows'}, {'caption'})
        columns, rows = data['columns'], data['rows']
        if type(columns) is not list or not 1 <= len(columns) <= 12 or type(rows) is not list or len(rows) > 200:
            raise ValueError('Tables need one to twelve columns and at most 200 rows.')
        ids = _unique(columns, 'Column')
        for col in columns:
            _shape(col, {'id', 'label'})
            col['label'] = _label(col['label'], 120)
        for row in rows:
            _shape(row, ids)
            for cell in row.values():
                _primitive(cell)
        _optional_text(data, 'caption', 1000)
    elif kind == 'chart':
        _shape(data, {'type', 'x_key', 'y_keys', 'rows'}, {'labels', 'caption'})
        if data['type'] not in ('bar', 'line'):
            raise ValueError('Charts support bar or line displays.')
        x = identifier(data['x_key'], 'Chart axis')
        ys = data['y_keys']
        if type(ys) is not list or not 1 <= len(ys) <= 6 or len(set(identifier(y, 'Series') for y in ys)) != len(ys) or x in ys:
            raise ValueError('Charts need one to six unique numeric series.')
        rows = data['rows']
        if type(rows) is not list or not 1 <= len(rows) <= 200:
            raise ValueError('Charts need one to 200 rows.')
        for row in rows:
            _shape(row, {x, *ys})
            _primitive(row[x])
            for key in ys:
                if type(row[key]) not in (int, float) or not math.isfinite(row[key]) or abs(row[key]) > 1e15:
                    raise ValueError('Chart series must contain bounded finite numbers.')
        if 'labels' in data:
            _shape(data['labels'], (), {x, *ys})
            data['labels'] = {key: _label(label, 120) for key, label in data['labels'].items()}
        _optional_text(data, 'caption', 1000)
    elif kind == 'plan':
        _shape(data, {'steps'})
        steps = data['steps']
        if type(steps) is not list or not 1 <= len(steps) <= 20:
            raise ValueError('Plans need one to twenty steps.')
        _unique(steps, 'Step')
        active = 0
        for step in steps:
            _shape(step, {'id', 'title', 'status'}, {'evidence_ids'})
            step['title'] = _label(step['title'], 240)
            if step['status'] not in ('pending', 'in_progress', 'blocked', 'completed'):
                raise ValueError('Unsupported plan step status.')
            active += step['status'] == 'in_progress'
            step.setdefault('evidence_ids', [])
            evidence_ids(step['evidence_ids'])
            if step['status'] == 'completed' and not step['evidence_ids']:
                raise ValueError('Completed steps need resolved tool evidence.')
        if active > 1:
            raise ValueError('Keep one active step in this serial run.')
    elif kind == 'evidence':
        _shape(data, {'items'}, {'caption'})
        items = data['items']
        if type(items) is not list or not 1 <= len(items) <= 30:
            raise ValueError('Evidence displays need one to thirty items.')
        _unique(items, 'Evidence item')
        for item in items:
            _shape(item, {'id', 'label', 'evidence_id'}, {'summary'})
            item['label'] = _label(item['label'], 240)
            evidence_ids([item['evidence_id']])
            _optional_text(item, 'summary', 2000)
        _optional_text(data, 'caption', 1000)
    elif kind == 'dashboard':
        data = _dashboard(data)
    elif kind == 'prototype':
        data = validate_prototype(data)
    else:
        raise ValueError('Unsupported declarative view type.')
    return data


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError(label + ' must be a bounded finite number.')
    return value


def metric(value):
    data = json_object(value)
    _shape(data, {'value'}, {'unit', 'delta', 'caption'})
    _number(data['value'], 'A metric value')
    if 'delta' in data:
        _number(data['delta'], 'A metric delta')
    if 'unit' in data:
        data['unit'] = inert_text(data['unit'], 24)
    if 'caption' in data:
        data['caption'] = inert_text(data['caption'], 240)
    return data


def panel_data(kind, value):
    """Inline or host-resolved data for one dashboard panel, validated by the panel kind."""
    if kind not in PANEL_KINDS:
        raise ValueError('Dashboard panels are metric, table, chart, plan or evidence.')
    return metric(value) if kind == 'metric' else validate_native(kind, value)


def _dashboard(data):
    # Imported here: bindings validate resolved data with this module.
    from .view_bindings import validate_binding
    rejected_keys({key: None for key in data})
    _shape(data, {'panels'}, {'columns'})
    if 'columns' in data and (type(data['columns']) is not int or data['columns'] not in (1, 2, 3)):
        raise ValueError('Dashboards use one, two or three columns.')
    panels = data['panels']
    if type(panels) is not list or not 1 <= len(panels) <= MAX_PANELS:
        raise ValueError('Dashboards need one to eight panels.')
    _unique(panels, 'Panel')
    for panel in panels:
        rejected_keys({key: None for key in panel})
        _shape(panel, {'id', 'title', 'kind'}, {'data', 'binding'})
        if ('data' in panel) == ('binding' in panel):
            raise ValueError('Each dashboard panel needs exactly one of inline data or a host binding.')
        panel['title'] = inert_text(panel['title'], 120)
        if panel['kind'] not in PANEL_KINDS:
            raise ValueError('Dashboard panels are metric, table, chart, plan or evidence.')
        if 'data' in panel:
            panel['data'] = panel_data(panel['kind'], panel['data'])
        else:
            panel['binding'] = validate_binding(panel['binding'], panel['kind'])
    return data


def validate_prototype(value):
    """An app-interface mock-up: enumerated tokens and plain text; controls only switch screens."""
    data = json_object(value)
    rejected_keys(data)
    _shape(data, {'start', 'screens'}, {'viewport', 'theme'})
    if 'viewport' in data and data['viewport'] not in VIEWPORTS:
        raise ValueError('Prototype viewports are phone, tablet or desktop.')
    if 'theme' in data:
        _shape(data['theme'], (), set(THEME))
        for key, choice in data['theme'].items():
            if type(choice) not in (str, int) or type(choice) is bool or choice not in THEME[key]:
                raise ValueError('Prototype themes use the enumerated accent, density and radius tokens.')
    screens = data['screens']
    if type(screens) is not list or not 1 <= len(screens) <= MAX_SCREENS:
        raise ValueError('Prototypes need one to twelve screens.')
    ids = _unique(screens, 'Screen')
    if type(data['start']) is not str or data['start'] not in ids:
        raise ValueError('The prototype start must name one of its screens.')

    def goto(target):
        if type(target) is not str or target not in ids:
            raise ValueError('Prototype controls can only go to one of its screens.')
        return target

    for screen in screens:
        _shape(screen, {'id', 'title', 'components'})
        screen['title'] = inert_text(screen['title'], 120)
        components = screen['components']
        if type(components) is not list or len(components) > MAX_COMPONENTS:
            raise ValueError('Prototype screens hold at most thirty components.')
        for item in components:
            shape = COMPONENTS.get(item.get('type')) if type(item) is dict and type(item.get('type')) is str else None
            if not shape:
                raise ValueError('Unsupported prototype component.')
            _shape(item, shape[0], shape[1])
            for key in sorted(set(item) - {'type', 'items', 'goto', 'aspect'}):
                item[key] = inert_text(item[key], 2000 if key == 'body' else 240)
            if 'goto' in item:
                goto(item['goto'])
            if 'aspect' in item and item['aspect'] not in ASPECTS:
                raise ValueError('Image placeholders use an enumerated aspect ratio.')
            if item['type'] == 'list':
                if type(item['items']) is not list or len(item['items']) > 30:
                    raise ValueError('Prototype lists hold at most thirty items.')
                item['items'] = [inert_text(entry, 240) for entry in item['items']]
            elif item['type'] == 'nav':
                if type(item['items']) is not list or not 1 <= len(item['items']) <= 8:
                    raise ValueError('Prototype navigation holds one to eight links.')
                for link in item['items']:
                    _shape(link, {'label', 'goto'})
                    link['label'] = inert_text(link['label'], 120)
                    goto(link['goto'])
    return data


QUIZ_FIELDS = ('duration_minutes', 'questions')
QUESTION_FIELDS = ('question', 'choices', 'answer', 'explanation')
_FIELD_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,39}')
_MAX_PROBLEMS = 8


def _quiz_text(value, limit):
    # The Phase 19 rule (artifacts.text): a string with visible text, at most `limit` characters.
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _extra_field(key):
    return f'.{key} is not a question field' if _FIELD_NAME.fullmatch(key) else ' has an unnamed field that is not a question field'


def _positions(indices, total):
    return '[*]' if total > 1 and len(indices) == total else '[' + ','.join(map(str, indices)) + ']'


def _question_problems(question):
    """One quiz question's problems as path suffixes, and whether its integer answer falls outside its choices."""
    if type(question) is not dict:
        return [' must be an object {question, choices, answer, explanation}, not ' + _json_type(question)], False
    found = [_extra_field(key) for key in question if key not in QUESTION_FIELDS]
    found += [f'.{key} is required' for key in QUESTION_FIELDS if key not in question]
    for key in ('question', 'explanation'):
        if key in question and not _quiz_text(question[key], 1500):
            found.append(f'.{key} must be non-empty text of at most 1500 characters')
    choices = question.get('choices')
    counted = type(choices) is list and 2 <= len(choices) <= 6
    if 'choices' in question:
        if type(choices) is not list:
            found.append('.choices must be a list of 2 to 6 choice strings, not ' + _json_type(choices))
        else:
            if not counted:
                found.append(f'.choices must hold 2 to 6 choices; it has {len(choices)}')
            not_text = [str(j) for j, choice in enumerate(choices) if not isinstance(choice, str)]
            long_or_blank = [str(j) for j, choice in enumerate(choices) if isinstance(choice, str) and not _quiz_text(choice, 700)]
            if not_text:
                found.append(f'.choices[{",".join(not_text)}] must be strings: write every choice as JSON text, '
                             'numbers too (for example "12", not 12)')
            if long_or_blank:
                found.append(f'.choices[{",".join(long_or_blank)}] must be non-empty text of at most 700 characters')
    outside = False
    if 'answer' in question:
        answer = question['answer']
        if type(answer) is not int:
            found.append('.answer must be the 0-based index of the one correct choice as a JSON integer '
                         '(0 for the first choice), not ' + _json_type(answer))
        elif counted and not 0 <= answer < len(choices):
            outside = True
    return list(dict.fromkeys(found)), outside


def validate_quiz(value):
    """Quiz input (a model's UIRender/UIUpdate, or a mod's self-check), accepted exactly as in Phase 19.

    Phase 21 (F21-quiz): a refusal lists every problem by its 0-based JSON path with the expected shape, so one
    corrected call can succeed. It never quotes a question, choice, answer or explanation: the refusal also
    reaches the run's progress line and records, and the corrected call usually carries the same answer key.
    For the same reason an answer index outside its choices is counted, never located.
    """
    data = json_object(value)
    extra = list(dict.fromkeys('data.' + key if _FIELD_NAME.fullmatch(key) else 'an unnamed field'
                               for key in data if key not in QUIZ_FIELDS))
    problems = [', '.join(extra) + (' are not quiz fields' if len(extra) > 1 else ' is not a quiz field')
                + ' (quiz data has exactly duration_minutes and questions)'] if extra else []
    minutes = data.get('duration_minutes')
    if 'duration_minutes' not in data:
        problems.append('data.duration_minutes is required: whole minutes from 1 to 180')
    elif type(minutes) is not int:
        problems.append('data.duration_minutes must be whole minutes from 1 to 180 as a JSON integer, not '
                        + _json_type(minutes))
    elif not 1 <= minutes <= 180:
        problems.append(f'data.duration_minutes counts whole minutes from 1 to 180; it is {minutes}')
    questions = data.get('questions')
    if 'questions' not in data:
        problems.append('data.questions is required: a list of 1 to 50 question objects')
    elif type(questions) is not list:
        problems.append('data.questions must be a list of 1 to 50 question objects, not ' + _json_type(questions))
    elif not 1 <= len(questions) <= 50:
        problems.append(f'data.questions must hold 1 to 50 questions; it has {len(questions)}')
    if type(questions) is list and len(questions) <= 50:
        grouped, outside = {}, 0
        for index, question in enumerate(questions):
            found, missed = _question_problems(question)
            outside += missed
            for suffix in found:
                grouped.setdefault(suffix, []).append(index)
        problems += [f'data.questions{_positions(indices, len(questions))}{suffix}' for suffix, indices in grouped.items()]
        if outside:
            problems.append(f'data.questions[*].answer: {outside} answer{"s fall" if outside > 1 else " falls"} outside '
                            'the choices; an answer counts from 0 (the first choice) to one less than the number of '
                            'choices')
    if problems:
        more = len(problems) - _MAX_PROBLEMS
        raise ValueError('Quiz data was refused: ' + '; '.join(problems[:_MAX_PROBLEMS])
                         + (f'; and {more} more' if more > 0 else '') + '.')
    return data


def evidence_argument(value):
    """The UIRender/UIUpdate `evidence_ids` argument: a JSON array string of this run's receipt IDs.

    Absent or JSON null meant none in Phase 19; a blank string (common for an optional argument in non-strict
    tool calls, and refused with a bare JSON parser error before) now means none too. Anything else that is not a
    JSON array is refused with the argument's shape. The IDs themselves are checked where the view is stored
    (Artifacts._evidence), unchanged.
    """
    if value is None or isinstance(value, str) and not value.strip():
        return []
    shape = ('evidence_ids must be a JSON array string of receipt_id values from this run\'s tool results, for '
             'example ["2:call_1"]; leave it out when the view cites none.')
    if not isinstance(value, str):
        raise ValueError(shape)
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise ValueError(shape) from None
    if parsed is None:
        return []
    if type(parsed) is not list:
        raise ValueError(shape)
    return parsed


def evidence_ids(value):
    if type(value) is not list or len(value) > 32 or any(not isinstance(v, str) or not v.strip() or len(v) > 350 or SECRET.search(v) for v in value) or len(set(value)) != len(value):
        raise ValueError('Evidence must be a bounded list of unique tool receipt IDs.')
    return list(value)


def referenced_evidence(kind, data):
    if kind == 'plan':
        return list(dict.fromkeys(e for step in data['steps'] for e in step['evidence_ids']))
    if kind == 'evidence':
        return list(dict.fromkeys(item['evidence_id'] for item in data['items']))
    if kind == 'dashboard':
        # Inline plan and evidence panels cite the same resolved receipts as their standalone kinds.
        return list(dict.fromkeys(e for panel in data['panels'] if 'data' in panel and panel['kind'] in ('plan', 'evidence')
                                  for e in referenced_evidence(panel['kind'], panel['data'])))
    return []


def validate_form_input(data, values):
    """Validate ordinary data only; the host chooses its origin and destination."""
    data = validate_native('form', data)
    if type(values) is not dict or set(values) - {f['id'] for f in data['fields']}:
        raise ValueError('Submit only the fields offered by this form.')
    result = {}
    for field in data['fields']:
        value = values.get(field['id'], '')
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError('Form answers must be text under 2000 characters.')
        if field['required'] and not value.strip():
            raise ValueError('Complete each required form field.')
        if field['type'] == 'choice' and value and value not in {o['id'] for o in field['options']}:
            raise ValueError('Choose an offered form option.')
        if SECRET.search(value):
            raise ValueError('Credentials belong in native connection settings. This input was not saved.')
        result[field['id']] = value.strip() if field['type'] == 'text' else value
    return result
