"""Phase 24 (t24-core) V1-010: backend.log is timestamped and rotated, refusals are not logged as crashes, a dying
thread says when, and the RPC error payload carries a content-free code.

Service tests run the real backend/main.py in a child process (flat --home, so backend.log is in the home); the
graders' own readers (scripts/pebble_install.py) judge the log it writes.
"""

import importlib.util
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from pebble.policy import PolicyError
from pebble.store import DISK_FULL, STORE_MESSAGES, StoreRefusal, friendly_error

BACKEND = Path(__file__).resolve().parents[1]
STAMP = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d'
TAIL = re.compile(r' \[(?P<thread>[^\[\]\n]+) (?P<time>' + STAMP + r')\]$')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend_main = load('pebble_backend_main_24', BACKEND / 'main.py')

# Routes that fail the ways the classes in error_reply describe: a sign-in class with a code, a full disk, a code
# that is not content-free (an int, as on HTTPError) and an error with no code at all.
FAILURES = '''
import sqlite3
from pebble.app import App
class _SignInFailed(ValueError):
    code = 'offline'
class _NumberCode(ValueError):
    code = 403
def _start(self, profile_id=None):
    raise _SignInFailed('You seem to be offline; private-detail-24. Your current sign-in is unchanged.')
def _workspace(self, id, path):
    raise sqlite3.OperationalError('database or disk is full')
def _effort(self, effort):
    raise _NumberCode('Refused with private-detail-24.')
App.chatgpt_start, App.set_workspace, App.model_effort = _start, _workspace, _effort
# 1.9.29 (the lead): App.dispatch fetches ChatGPT's discovery document (prefetch) before chatgpt_start, so without this
# stub the test reached the network and failed when auth.openai.com was unreachable (errno 65, impact run 68)
from pebble.chatgpt import ChatGPT
ChatGPT.prefetch = lambda self: {}
'''


def log_lines(home):
    return (Path(home) / 'backend.log').read_text(encoding='utf-8').splitlines()


def recent(stamp):
    moment = datetime.fromisoformat(stamp)
    assert moment.utcoffset() is not None
    return abs((datetime.now().astimezone() - moment).total_seconds()) < 300


@pytest.fixture
def restore_logging():
    """configure_logging changes this process's logging and threading.excepthook; put both back."""
    root, pebble = logging.getLogger(), logging.getLogger('pebble')
    handlers, levels, hook = list(root.handlers), (root.level, pebble.level), threading.excepthook
    yield
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(levels[0])
    pebble.setLevel(levels[1])
    threading.excepthook = hook


def test_an_unexpected_failure_keeps_its_line_and_traceback_and_ends_with_the_time(tmp_path, pebble_service):
    home = tmp_path / 'data'
    service = pebble_service(home)
    reply = service.call('get_chat', timeout=60)  # no id: a KeyError, which is a defect and not a refusal
    # Phase 45 (F239, audit round R10, deliberate): the person reads one sentence, never the defect's own words ("'id'")
    assert reply['error'] == backend_main.DEFECT_TEXT and 'code' not in reply
    assert service.close() == 0
    lines = log_lines(home)
    first = lines[0]
    assert first.startswith('ERROR:root:Request failed: get_chat [')
    tail = TAIL.search(first)
    assert tail and recent(tail['time'])
    assert lines[1] == 'Traceback (most recent call last):'
    # The install tool's readback still counts this as an answered request failure, not a crash.
    install = load('pebble_install_24', BACKEND.parent / 'scripts' / 'pebble_install.py')
    scan = install.new_tracebacks(home, {name: {'bytes': 0} for name in install.LOGS})
    assert scan['backend.log']['handled_request_failures'] == 1 and scan['backend.log']['tracebacks'] == 0


def test_refusals_are_one_info_line_with_their_class_and_code_and_no_traceback(tmp_path, pebble_service):
    home = tmp_path / 'data'
    service = pebble_service(home, FAILURES)
    assert service.call('no_such_route', timeout=60) == {'id': 'r1', 'error': 'Unknown request.'}
    signin = service.call('chatgpt_start')
    assert signin['code'] == 'offline' and signin['error'].endswith('Your current sign-in is unchanged.')
    full = service.call('set_workspace', id='x', path='/nowhere')
    assert full['code'] == DISK_FULL and full['error'] == STORE_MESSAGES[DISK_FULL]
    numbered = service.call('model_effort', effort='high')
    assert 'code' not in numbered
    odd = service.call('odd\nname')
    assert odd['error'] == 'Unknown request.'
    assert service.close() == 0
    text = (home / 'backend.log').read_text(encoding='utf-8')
    assert 'Traceback' not in text and 'private-detail-24' not in text
    lines = text.splitlines()
    assert [TAIL.sub('', line) for line in lines] == [
        'INFO:pebble.rpc:Request refused: no_such_route (ValueError)',
        'INFO:pebble.rpc:Request refused: chatgpt_start (_SignInFailed, offline)',
        'INFO:pebble.rpc:Request refused: set_workspace (OperationalError, disk_full)',
        'INFO:pebble.rpc:Request refused: model_effort (_NumberCode)',
        'INFO:pebble.rpc:Request refused: odd?name (ValueError)',
    ]
    assert all(TAIL.search(line) and recent(TAIL.search(line)['time']) for line in lines)


def test_backend_log_rotates_at_5_mb_and_keeps_three_older_files(tmp_path, restore_logging):
    handler = backend_main.configure_logging(tmp_path)
    assert (handler.maxBytes, handler.backupCount) == (5 * 1024 * 1024, 3)
    noisy = logging.getLogger('pebble.rotation_24')
    for n in range(23_000):
        noisy.warning('Synthetic line %d %s', n, 'x' * 1000)
    handler.flush()
    names = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith('backend.log'))
    assert names == ['backend.log', 'backend.log.1', 'backend.log.2', 'backend.log.3']
    assert all((tmp_path / name).stat().st_size <= handler.maxBytes for name in names)
    last = log_lines(tmp_path)[-1]
    assert last.startswith('WARNING:pebble.rotation_24:Synthetic line 22999 ') and TAIL.search(last)


def test_a_thread_that_dies_is_logged_with_its_time_and_traceback(tmp_path, restore_logging):
    backend_main.configure_logging(tmp_path)
    assert threading.excepthook is backend_main.log_thread_exception

    def fail():
        raise RuntimeError('boom 24')
    doomed = threading.Thread(target=fail, name='doomed-24')
    doomed.start()
    doomed.join(5)
    for handler in logging.getLogger().handlers:
        handler.flush()
    lines = log_lines(tmp_path)
    first = next(i for i, line in enumerate(lines) if line.startswith('ERROR:pebble.thread:'))
    assert TAIL.sub('', lines[first]) == 'ERROR:pebble.thread:Thread doomed-24 stopped by RuntimeError'
    assert TAIL.search(lines[first])['thread'] == 'doomed-24' and recent(TAIL.search(lines[first])['time'])
    assert lines[first + 1] == 'Traceback (most recent call last):' and 'RuntimeError: boom 24' in lines[first + 2:]


def test_the_error_payload_carries_only_content_free_codes():
    def coded(code):
        exc = ValueError('Refused.')
        exc.code = code
        return exc
    reply = backend_main.error_reply
    assert reply('r', coded('handoff_required'), friendly_error)[0] == {'id': 'r', 'error': 'Refused.',
                                                                         'code': 'handoff_required'}
    assert reply('r', coded('http_403'), friendly_error)[0]['code'] == 'http_403'
    for bad in ('Bad Code', 'x' * 65, '', 403, None, 'trailing\n'):
        assert 'code' not in reply('r', coded(bad), friendly_error)[0]
    full, shown = reply('r', sqlite3.OperationalError('database or disk is full'), friendly_error)
    assert full == {'id': 'r', 'error': STORE_MESSAGES[DISK_FULL], 'code': DISK_FULL} and isinstance(shown, StoreRefusal)
    assert reply('r', ValueError('Stop the current task first.'), friendly_error)[0] == {
        'id': 'r', 'error': 'Stop the current task first.'}
    assert backend_main.refusal(ValueError()) and backend_main.refusal(PolicyError('No.'))
    assert not backend_main.refusal(KeyError('id')) and not backend_main.refusal(TypeError())
    assert not backend_main.refusal(json.JSONDecodeError('bad', '{', 0))
    assert not backend_main.refusal(UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid'))
    assert backend_main.method_label('odd\nname') == 'odd?name' and backend_main.method_label('') == '?'
    # Phase 45 (F239, audit round R10): a defect never shows its own words to the person
    for defect in (TypeError("App._route_table.<locals>.<lambda>() got an unexpected keyword argument 'id'"),
                   KeyError('id'), AttributeError("'NoneType' object has no attribute 'x'")):
        payload, shown = reply('r', defect, friendly_error)
        assert payload == {'id': 'r', 'error': backend_main.DEFECT_TEXT} and shown is defect
