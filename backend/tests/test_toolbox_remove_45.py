"""Phase 45 (1.9.11, F162): a short request to remove a kept tool is answered by the host with the tool's Remove card;
nothing is removed until the person presses it."""
import pytest

from pebble import workshop


class Box:
    def __init__(self, value):
        self.value = value

    def setting(self, key, default=None):
        return self.value if key == workshop.SETTING else default


class App:
    def __init__(self, value):
        self.store = Box(value)


def record(name):
    return {'spec': {'name': name, 'purpose': 'Converts Celsius to Fahrenheit.'}}


APP = App({'tools': [record('uxtest_c_to_f')], 'pending': [{'id': 'p1', 'record': record('uxtest_waiting')}]})


@pytest.mark.parametrize('text', [
    'UXTEST: Please remove uxtest_c_to_f from your Toolbox.',
    'remove the uxtest_c_to_f tool',
    'Pebble, delete `uxtest_c_to_f` from the toolbox please',
    'Could you remove the tool uxtest_c_to_f?',
])
def test_a_short_removal_request_names_the_kept_tool(text):
    name, kept, asked = workshop.removal_request(APP, text)
    assert name == 'uxtest_c_to_f' and kept is not None and asked is None


def test_a_waiting_tool_and_an_unknown_name_are_told_apart():
    assert workshop.removal_request(APP, 'Remove uxtest_waiting from your Toolbox')[1:] == (None, APP.store.value['pending'][0])
    assert workshop.removal_request(APP, 'Remove nothing_here from your Toolbox') == ('nothing_here', None, None)


@pytest.mark.parametrize('text', [
    'Remove uxtest_c_to_f',  # no "tool": may be a file or anything else; the model answers
    'Use uxtest_c_to_f to convert 30 degrees, then remove it from your toolbox',
    'Remove the tests folder from the toolbox project and run them again',
    'How do I remove a tool from your Toolbox?',
    'Remove it from your Toolbox.',
    'Remove the tool',
])
def test_anything_else_goes_to_the_model(text):
    assert workshop.removal_request(APP, text) is None


def test_the_host_answers_with_the_remove_card_and_removes_nothing(tmp_path):
    from pebble.app import App as Pebble
    events = []
    app = Pebble(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(args))
    try:
        def complete(*args, **kwargs):
            raise AssertionError('the host answers; no model call')
        app.model.complete = complete
        app.store.set_setting(workshop.SETTING, {'tools': [record('uxtest_c_to_f')], 'pending': []})
        chat = app.new_chat()
        app.work(chat, 'UXTEST: Please remove uxtest_c_to_f from your Toolbox.', False, [])
        reply = app.chat(chat['id'])['messages'][-1]['content']
        assert 'Choose **Remove**' in reply and 'Nothing is removed until you do' in reply
        assert [t['spec']['name'] for t in workshop.toolbox(app)['tools']] == ['uxtest_c_to_f'], 'nothing removed'
        cards = [e[1] for e in events if len(e) > 1 and e[0] == 'toolbox']
        assert cards and cards[-1]['kind'] == 'remove' and cards[-1]['name'] == 'uxtest_c_to_f'
        assert not app.store.query('SELECT 1 FROM checkpoints WHERE chat_id=?', (chat['id'],)), 'no working record'
    finally:
        app.close()
