"""Versioned deny-only policy composition and sandboxed pure decision workers."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .policy import PolicyError

POLICY_EVENTS = {'run_start', 'model_attempt', 'before_tool', 'before_command', 'task_end'}
OBSERVERS = {'context_assembled', 'after_tool', 'compaction', 'approval_resolved', 'run_settled'}
EVENTS = POLICY_EVENTS | OBSERVERS


class HookInterrupted(InterruptedError):
    """Cancelled before effect admission; this worker has no external effects."""


def supported():
    return sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def validate_config(config):
    allowed = {'version', 'event', 'deny_patterns', 'review_patterns', 'require_tests', 'worker'}
    if not isinstance(config, dict) or set(config) - allowed or type(config.get('version', 1)) is not int or config.get('version', 1) != 1 or config.get('event') not in EVENTS:
        raise PolicyError('Use a supported version 1 hook event and declarative rules or Python worker.')
    for key in ('deny_patterns', 'review_patterns'):
        patterns = config.get(key, [])
        if not isinstance(patterns, list) or len(patterns) > 20 or any(not isinstance(p, str) or not p.strip() or len(p) > 200 for p in patterns):
            raise PolicyError('Hook rules support twenty short literal patterns.')
        if patterns and config['event'] not in {'before_tool', 'before_command'}:
            raise PolicyError('Literal deny/review rules apply only to tool and command admission.')
    if 'require_tests' in config and (type(config['require_tests']) is not bool or config['event'] != 'task_end'):
        raise PolicyError('Boolean require_tests applies only to task_end.')
    worker = config.get('worker')
    if worker is not None:
        if not isinstance(worker, dict) or set(worker) != {'language', 'source', 'timeout_ms'} or worker['language'] != 'python' or not isinstance(worker['source'], str) or not 1 <= len(worker['source'].encode()) <= 16_000 or type(worker['timeout_ms']) is not int or not 100 <= worker['timeout_ms'] <= 2000:
            raise PolicyError('A Python decision worker requires source under 16 KB and a 100–2000 ms timeout.')
        try:
            compile(worker['source'], '<reviewed-hook>', 'exec')
        except SyntaxError:
            raise PolicyError('Hook Python source is not syntactically valid.') from None
    return {**config, 'version': 1}


def run_worker(worker, event, cancel, timeout):
    if not supported():
        raise PolicyError('Executable hook confinement is unavailable on this platform.')
    if cancel.is_set():
        raise HookInterrupted('Stopped before hook admission.')
    exe = Path(sys.executable).resolve()
    frozen = bool(getattr(sys, 'frozen', False))
    framework_exe = Path(sys.base_prefix)/'Resources/Python.app/Contents/MacOS/Python'
    if not frozen and framework_exe.is_file():
        exe = framework_exe.resolve()
    command = [str(exe), '--hook-worker'] if frozen else [str(exe), '-s', str(Path(__file__).resolve().parents[1]/'main.py'), '--hook-worker']
    runtime = [Path(getattr(sys, '_MEIPASS', sys.prefix)).resolve(), Path(sys.base_prefix).resolve()]
    if not frozen:
        runtime.append(Path(__file__).resolve().parent)
    quote = lambda path: json.dumps(str(path))
    with tempfile.TemporaryDirectory(prefix='pebble-hook-') as folder:
        temp = Path(folder).resolve()
        read = ' '.join('(subpath ' + quote(p) + ')' for p in runtime)
        handles = ' '.join('(literal ' + quote(p) + ')' for p in {*exe.parents, *temp.parents})
        main = '' if frozen else '(literal ' + quote(Path(__file__).resolve().parents[1]/'main.py') + ') (literal ' + quote(Path(__file__).resolve().parents[1]) + ')'
        profile = f'''(version 1) (deny default)
          (allow process-exec (literal {quote(exe)})) (deny process-fork)
          (allow sysctl-read) (allow file-read-metadata)
          (allow file-read* (literal {quote(exe)}) {handles} {main} {read} (subpath "/System") (subpath "/usr/lib") (subpath "/private/var/db/dyld") (literal "/dev/null"))
          (allow file-read* file-write* (subpath {quote(temp)}))
          (deny file-read-data file-write* (regex #"(^|/)(\\.env[^/]*|\\.ssh|\\.aws|\\.gnupg|\\.pebble|credentials|secrets)(/|$)"))'''
        out, err = temp/'out', temp/'err'
        env = {'HOME': str(temp), 'TMPDIR': str(temp), 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1'}
        with out.open('wb') as stdout, err.open('wb') as stderr:
            process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, *command], cwd=temp, env=env,
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                process.stdin.write(json.dumps({'source': worker['source'], 'event': event}, ensure_ascii=True).encode())
                process.stdin.close()
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if cancel.is_set():
                        raise HookInterrupted('Stopped before hook admission.')
                    if time.monotonic() >= deadline or out.stat().st_size + err.stat().st_size > 32_768:
                        raise PolicyError('Hook worker exceeded its time/output budget.')
                    time.sleep(.02)
                if process.returncode != 0:
                    raise PolicyError('Hook worker failed in its isolated runtime.')
                result = json.loads(out.read_bytes())
                if not isinstance(result, dict) or set(result) - {'decision', 'require_tests'} or result.get('decision') not in {'allow', 'review', 'deny'} or 'require_tests' in result and type(result['require_tests']) is not bool:
                    raise PolicyError('Hook worker returned an invalid decision contract.')
                return result
            except HookInterrupted:
                raise
            except (ValueError, OSError) as exc:
                if isinstance(exc, PolicyError):
                    raise
                raise PolicyError('Hook worker failed its decision contract.') from None
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()


class HookEngine:
    def __init__(self, store):
        self.store = store
        store.query('CREATE TABLE IF NOT EXISTS extension_hook_audit(id INTEGER PRIMARY KEY,run_id TEXT,extension_id TEXT,digest TEXT,event TEXT,decision TEXT,status TEXT,elapsed REAL,created REAL)')
        store.query('CREATE TABLE IF NOT EXISTS extension_hook_budget(run_id TEXT PRIMARY KEY,calls INTEGER,seconds REAL)')

    def audit(self, rid, item, event, result, elapsed):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Hook audit requires a separate transaction boundary.')
            with db:
                db.execute('INSERT INTO extension_hook_audit VALUES(NULL,?,?,?,?,?,?,?,?)', (rid, item['id'], fingerprint(item['config']), event, result['decision'], result['status'], elapsed, time.time()))
                db.execute('DELETE FROM extension_hook_audit WHERE id NOT IN (SELECT id FROM extension_hook_audit ORDER BY id DESC LIMIT 5000)')

    def reserve(self, rid, seconds):
        if not rid:
            raise PolicyError('An executable hook needs an owning run.')
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Hook budget requires a separate transaction boundary.')
            with db:
                db.execute('INSERT OR IGNORE INTO extension_hook_budget VALUES(?,0,0)', (rid,))
                used = db.execute('SELECT calls,seconds FROM extension_hook_budget WHERE run_id=?', (rid,)).fetchone()
                if used[0] >= 64 or used[1] + seconds > 20:
                    raise PolicyError('The run exhausted its executable hook budget.')
                db.execute('UPDATE extension_hook_budget SET calls=calls+1,seconds=seconds+? WHERE run_id=?', (seconds, rid))

    def dispatch(self, items, event, text='', *, run_id=None, cancel=None, data=None, workers_allowed=True):
        if event not in EVENTS or not isinstance(text, str) or len(text.encode()) > 200_000:
            raise PolicyError('Unsupported or oversized host hook event.')
        cancel = cancel or threading.Event()
        packet = {'version': 1, 'event': event, 'origin': 'host', 'run_id': run_id, 'authorization': 'none', 'data': data or {}}
        fields = {'tool', 'call_id', 'arguments_hash', 'state', 'attempt', 'generation', 'input_tokens', 'context_status', 'action_id', 'kind'}
        if not isinstance(packet['data'], dict) or set(packet['data']) - fields or any(type(v) not in (str, int, bool) or isinstance(v, str) and len(v) > 320 for v in packet['data'].values()):
            raise PolicyError('Hook events accept only bounded host metadata fields.')
        if len(json.dumps(packet).encode()) > 4000:
            raise PolicyError('Hook metadata exceeded its envelope budget.')
        decision, blocked_by, require_tests = 'allow', '', False
        deadline = time.monotonic() + 3
        for item in items:
            if item['kind'] != 'hook' or not item['enabled'] or item['config'].get('event') != event:
                continue
            config = validate_config(item['config'])
            result = {'decision': 'allow', 'status': 'evaluated'}
            started = time.monotonic()
            if any(p.casefold() in text.casefold() for p in config.get('review_patterns', [])):
                result['decision'] = 'review'
            if any(p.casefold() in text.casefold() for p in config.get('deny_patterns', [])):
                result['decision'] = 'deny'
            require_tests |= config.get('require_tests', False)
            if config.get('worker') and result['decision'] != 'deny':
                reserved = 0
                try:
                    if not workers_allowed:
                        raise PolicyError('Executable hooks are disabled by current terminal permission.')
                    timeout = min(config['worker']['timeout_ms']/1000, deadline-time.monotonic())
                    if timeout <= 0:
                        raise PolicyError('Hook dispatch exhausted its time budget.')
                    self.reserve(run_id, timeout)
                    reserved = timeout
                    worker_result = run_worker(config['worker'], packet, cancel, timeout)
                    # A worker allow cannot weaken a declarative review.
                    if worker_result['decision'] == 'deny' or result['decision'] == 'allow':
                        result['decision'] = worker_result['decision']
                    require_tests |= event == 'task_end' and worker_result.get('require_tests', False)
                except InterruptedError:
                    raise
                except Exception:
                    result = {'decision': 'deny', 'status': 'worker_failed_closed'}
                finally:
                    if reserved:
                        refund = max(0, reserved - (time.monotonic()-started))
                        self.store.query('UPDATE extension_hook_budget SET seconds=max(0,seconds-?) WHERE run_id=?', (refund, run_id))
            if event in OBSERVERS:
                result['status'] = 'observer_ignored_' + result['decision'] + '_' + result['status']
                result['decision'] = 'allow'
            self.audit(run_id, item, event, result, time.monotonic()-started)
            if result['decision'] == 'deny':
                decision, blocked_by = 'deny', item['name']
                break  # Already denied; later allow or review cannot reopen it.
            if result['decision'] == 'review':
                decision, blocked_by = 'review', item['name']
        return {'version': 1, 'decision': decision, 'blocked_by': blocked_by, 'require_tests': require_tests, 'authorization': 'none'}
