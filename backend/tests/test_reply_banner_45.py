"""Phase 45 (1.9.10, D134): a banner for a task in a conversation that ended or waits, for the shell to show while Pebble
is in the background: only with notifications and replies on, outside quiet hours, never for a scheduled run, and with
the conversation's title (never the reply's text); settings saved before 1.9.10 read replies as on."""
import pytest

from pebble.app import App
from pebble.notifications import DEFAULT_SETTINGS, REPLY_TITLES, validate_settings


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def chat_with_title(app, title):
    chat = app.new_chat()
    app.store.query('UPDATE chats SET title=? WHERE id=?', (title, chat['id']))
    return chat


def test_older_settings_read_replies_as_on_and_a_bad_value_is_refused():
    older = {k: v for k, v in DEFAULT_SETTINGS.items() if k != 'replies'}
    assert validate_settings(older)['replies'] is True
    assert validate_settings({**older, 'replies': False})['replies'] is False
    with pytest.raises(ValueError):
        validate_settings({**older, 'replies': 'yes'})


def test_a_banner_follows_the_settings_and_carries_the_title_only(app):
    chat = chat_with_title(app, 'UXTEST: play the navior song https://youtube.com/x')
    run = 'a' * 32
    notices = app.notifications
    assert notices.reply_banner(chat['id'], run, 'completed') is None, 'notifications are off by default'
    notices.save_settings({**DEFAULT_SETTINGS, 'enabled': True})
    banner = notices.reply_banner(chat['id'], run, 'completed')
    assert banner == {'chat_id': chat['id'], 'run_id': run, 'title': REPLY_TITLES['completed'],
                      'body': 'UXTEST: play the navior song [link removed]'}
    assert notices.reply_banner(chat['id'], run, 'waiting_approval')['title'] == 'Pebble needs your review'
    assert notices.reply_banner(chat['id'], run, 'stopped') is None, 'a task the person stopped needs none'
    notices.save_settings({**DEFAULT_SETTINGS, 'enabled': True, 'preview': 'none'})
    assert notices.reply_banner(chat['id'], run, 'completed')['body'] == ''
    notices.save_settings({**DEFAULT_SETTINGS, 'enabled': True, 'replies': False})
    assert notices.reply_banner(chat['id'], run, 'completed') is None
