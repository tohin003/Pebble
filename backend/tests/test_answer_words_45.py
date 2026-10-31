"""Phase 45 (1.9.17, F249 from audit round R11): the person's answer to Pebble's question showed in the conversation
as "Clarification answer (native form): {"open": {"option_id": "yes", "text": "Yes, open Chess"}}"; it reads as they
chose it."""
from pebble.interaction import answer_words

ITEMS = [{'id': 'open', 'title': 'Open Chess?', 'options': [{'id': 'yes', 'label': 'Yes, open Chess'}]},
         {'id': 'note', 'title': 'Anything else?', 'allow_free_text': True}]


def test_one_answer_reads_as_chosen_and_several_carry_their_titles():
    assert answer_words(ITEMS[:1], {'open': {'option_id': 'yes', 'text': 'Yes, open Chess'}}) == 'Yes, open Chess'
    assert answer_words(ITEMS, {'open': {'option_id': 'yes', 'text': 'Yes, open Chess'}, 'note': {'text': 'Play fast'}}) \
        == 'Open Chess?: Yes, open Chess\nAnything else?: Play fast'
    assert answer_words(ITEMS, {}) == 'Answered.'
    assert '{' not in answer_words(ITEMS, {'open': {'option_id': 'yes', 'text': 'Yes, open Chess'}})


def test_a_declined_app_step_is_named_by_its_window():
    """F256 (audit round R11): Past experiences read "The user declined a proposed action for ScreenAct"."""
    from pebble import memory_facts as mf
    action = {'kind': 'screen', 'payload': {'review': {'window': {'title': 'Game 1'}}}}
    assert mf._target('ScreenAct', {}, action) == 'the app window “Game 1”'
    assert mf._target('ScreenAct', {}, {'kind': 'screen', 'payload': {}}) == 'an app window'
    assert mf._ACTION_NOUNS['screen'] == 'step'
