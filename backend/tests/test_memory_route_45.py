"""Phase 45 (1.9.8, audit round R4, F102): a message Pebble answers by saving a memory, or by offering the Forget card,
keeps no task record of its own, so forgetting the fact leaves its words nowhere else; a request that reaches the
model still gets its task record."""
import pytest

from pebble.app import App


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield instance
    instance.close()


def scripted(app, reply='Here is the answer.'):
    def complete(messages, available, cancel, token, deliberate):
        token(reply)
        return {'role': 'assistant', 'content': reply}
    app.model.complete = complete


def episodes(app, chat):
    rids = {r['id'] for r in app.runs.list(chat['id'])}
    return [m for m in app.memory.list('episodic') if m['id'].removeprefix('episode-') in rids]


def test_remembering_and_offering_forget_keep_no_task_record_of_their_own(app):
    app.setting('memory_enabled', True)
    scripted(app)
    chat = app.new_chat()
    app.work(chat, 'Remember that my UXTEST favourite colour is teal.', False, [])
    assert 'Saved this in' in app.chat(chat['id'])['messages'][-1]['content']
    assert [r['state'] for r in app.runs.list(chat['id'])] == ['completed']
    assert not episodes(app, chat), 'the preference is the only copy of the words'
    app.work(chat, 'Forget that my UXTEST favourite colour is teal.', False, [])
    assert 'Forget' in app.chat(chat['id'])['messages'][-1]['content']
    assert not episodes(app, chat)
    assert not [m for m in app.memory.list() if 'teal' in str(m.get('content', '')) and m['kind'] != 'profile'], \
        'no other layer keeps the words'


def test_a_request_answered_by_the_model_keeps_its_task_record(app):
    app.setting('memory_enabled', True)
    scripted(app, 'Paris.')
    chat = app.new_chat()
    app.work(chat, 'What is the capital of France?', False, [])
    assert [e['id'] for e in episodes(app, chat)] == ['episode-' + app.runs.list(chat['id'])[0]['id']]


@pytest.mark.parametrize('text', ['UXTEST: Remember that my UXTEST favourite colour is teal.',
                                  'Hey Pebble, remember that my UXTEST favourite colour is teal.',
                                  'Note: please remember that my UXTEST favourite colour is teal.'])
def test_a_short_tag_or_address_before_remember_is_still_the_persons_own_request(app, text):
    # F163 (audit round R5): with a "UXTEST:" prefix the host missed it, the model's memory tool refused a reworded fact,
    # and nothing was saved
    app.setting('memory_enabled', True)
    scripted(app, 'The model should not be asked.')
    chat = app.new_chat()
    app.work(chat, text, False, [])
    assert app.chat(chat['id'])['messages'][-1]['content'].startswith('Saved this in **your preferences**')
    assert [m['content'] for m in app.memory.list('profile')] == ['my UXTEST favourite colour is teal.'], \
        'the statement as written, as without a prefix'


@pytest.mark.parametrize('text', ['Note: remember to buy milk', 'UXTEST: remember when we met?', 'Q: remember the time we met'])
def test_a_tagged_reminder_or_recollection_is_not_saved(app, text):
    app.setting('memory_enabled', True)
    assert app.memory.capture_user(text, app.new_chat()['id']) is None


def test_a_tagged_forget_request_finds_the_subject():
    from pebble.memory import forget_request
    assert forget_request('UXTEST: Forget that my UXTEST favourite colour is teal') == 'my UXTEST favourite colour is teal'


def test_remember_and_forget_leave_no_working_record_and_the_lookup_says_so(app):
    # F168, F169 (audit round R5): a working record "Current task: Forget that ..." stayed, marked running, and the
    # reply said "1 saved memory that match"
    app.setting('memory_enabled', True)
    scripted(app)
    chat = app.new_chat()
    app.work(chat, 'Remember that my UXTEST favourite colour is teal.', False, [])
    app.work(chat, 'Forget that my UXTEST favourite colour is teal.', False, [])
    assert 'saved memory that matches' in app.chat(chat['id'])['messages'][-1]['content']
    assert not app.store.query('SELECT 1 FROM checkpoints WHERE chat_id=?', (chat['id'],)), 'no working record'
