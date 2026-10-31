"""Phase 37 (1.5.0, Pebble's Workshop part 1): Code Mode. The model can write one short Python script (RunCode) that
calls this run's own tools through a `pebble` module; only what the script prints returns to the model.

Why: a task that reads, searches and filters many things pays for every intermediate result twice when each call is a
separate model step (the result enters the window, then the model reasons over it). A script keeps intermediate
results out of the window and runs loops and filters as code (Anthropic's "code execution with MCP" and Cloudflare's
"Code Mode" describe the pattern; .runtime/knapping-plan.md has the research).

What a script can and cannot do:
- It runs in a separate worker (`--code-worker`, code_worker.py) under a deny-by-default sandbox: no network, no
  fork or exec, no reads of the project or the data home, writes only to its own temporary folder; CPU 20 s, wall
  WALL_SECONDS, sampled memory RSS_BYTES, file size 8 MB.
- Every pebble.<name>(...) call comes back here, to the run's own thread (the run's ownership lives in a ContextVar a
  broker thread would not see), and goes through exactly the path a direct call takes: the offered schema
  (App.validate), the schema layer of App.authorize, then App.execute (tool budget, receipt, catalogue, route, actor and
  policy layers, permission groups, hooks, AgentGuard, edit_scope, approvals). Each call has its own receipt,
  `<RunCode receipt>:c<n>`, reserved atomically so a model-chosen call id can never adopt it.
- Only tools this step offers, from INNER, are callable: reads, edits and plugin calls; never agents, questions,
  commands, tests, git effects, memory or another RunCode. An edit in Ask mode returns "awaiting approval" to the
  script exactly as it would to the model, and the person approves it as usual.
- A refused call raises pebble.Refused in the script with the host's message; an uncertain effect stops the script and
  the run, as it would stop a direct call.
"""
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from .policy import PolicyError
from .zone import person_zone

ENV_SWITCH = 'PEBBLE_CODE_MODE'  # '0' keeps 1.4.0's requests: RunCode is never offered
CODE_LIMIT = 20_000              # characters of script
PURPOSE_LIMIT = 200
WALL_SECONDS = 60
MAX_CALLS = 200
RESULT_BYTES = 1_000_000         # one tool result sent to the script
INNER_BYTES = 8_000_000          # all tool results sent to the script
LINE_BYTES = 1_000_000           # one request line from the script
WIRE_BYTES = 16_000_000          # everything the script writes on the wire
RSS_BYTES = 768 * 1024 * 1024
SHOWN = 16_000                   # printed characters returned to the model
RECEIPTS_SHOWN = 20
POLL_SECONDS = .02
INNER = {
    'read': 'Read', 'grep': 'Grep', 'glob': 'Glob', 'search_files': 'SearchFiles', 'code_references': 'CodeReferences',
    'git_status': 'ReadGitStatus', 'git_diff': 'ReadGitDiff', 'git_history': 'ReadGitHistory',
    'read_run_evidence': 'ReadRunEvidence', 'read_attachment': 'ReadAttachment',
    'web_search': 'WebSearch', 'read_url': 'ReadURL',
    'write': 'Write', 'edit': 'Edit', 'append': 'Append', 'plugin_call': 'PluginCall',
    # Phase 41 (1.7.0): the Companion's tools, when the step offers them (a conversation without a project)
    'read_file': 'ReadFile', 'write_file': 'WriteFile', 'edit_file': 'EditFile', 'shell': 'Shell',
}
WORKSHOP = ('make_tool', 'keep_tool')  # Phase 41: answered by workshop.py, never through INNER
# Phase 46 (1.9.28, golden dev d-wr-01 on Opus: 11 of 12 turns spent a first script printing what pebble.read returned,
# or calling open(), which the sandbox refuses; about 10 s of 34 s): the example names the text's key and the limits
# name open(). Above 8K only, as RunCode is (runner.py). Folded into the sentences, 10 tokens a request rather than a
# 31-token sentence of its own, which took the shipped offer's context-cost bound (test_context_cost_24) to 2.76.
DESCRIPTION = (
    "Run a short Python script that calls this run's tools through `pebble`, e.g. "
    "pebble.read(file_path='a.py')['untrusted_source'] (the text), "
    "pebble.grep(pattern='TODO'); pebble.tools() lists them, pebble.Refused is raised on a refusal. Only printed "
    "output returns. For many reads, searches or filters in one step; same approvals as direct calls; 60 s; no network "
    "or open().")
# Phase 41 (1.7.0, Pebble's Workshop part 2): one sentence more while tool making is on; PEBBLE_TOOL_MAKING=0 keeps
# 1.6.0's description (the catalogue is fixed when Pebble starts).
WORKSHOP_SENTENCE = (" After a task needed a calculation, pebble.make_tool(name, purpose, code, tests, inputs) makes a "
                     "tested pure tool, used as pebble.mine.<name>(**args); pebble.keep_tool(name, reason) keeps it.")
if os.environ.get('PEBBLE_TOOL_MAKING', '1') != '0':
    DESCRIPTION += WORKSHOP_SENTENCE
SPEC = {'type': 'function', 'function': {'name': 'RunCode', 'description': DESCRIPTION, 'parameters': {
    'type': 'object', 'properties': {
        'code': {'type': 'string', 'maxLength': CODE_LIMIT},
        'purpose': {'type': 'string', 'maxLength': PURPOSE_LIMIT},
    }, 'required': ['code', 'purpose'], 'additionalProperties': False}}}


def enabled(app):
    """Whether RunCode is offered: the App's switch (PEBBLE_CODE_MODE=0 turns it off)."""
    return getattr(app, 'code_mode_enabled', True) is not False


def _help(spec):
    function = spec['function']
    params = function['parameters'].get('properties', {})
    required = set(function['parameters'].get('required', []))
    names = ', '.join(k + ('' if k in required else '=...') for k in params)
    return f"{function['name']}({names}): {function.get('description', '')[:600]}"


def _worker_command():
    exe = Path(sys.executable).resolve()
    frozen = bool(getattr(sys, 'frozen', False))
    framework_exe = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
    if not frozen and framework_exe.is_file():
        exe = framework_exe.resolve()
    main = Path(__file__).resolve().parents[1] / 'main.py'
    command = [str(exe), '--code-worker'] if frozen else [str(exe), '-s', str(main), '--code-worker']
    runtime = [Path(getattr(sys, '_MEIPASS', sys.prefix)).resolve(), Path(sys.base_prefix).resolve()]
    if not frozen:
        runtime.append(Path(__file__).resolve().parent)
    return exe, command, runtime, (None if frozen else main)


def profile(exe, runtime, temp, main=None):
    """The worker's sandbox: the hook worker's profile (Phase 13) for a longer-lived child. Deny by default; exec only
    the backend itself; no fork; read only the runtime and system libraries; read and write only its own folder."""
    quote = lambda path: json.dumps(str(path))  # noqa: E731
    read = ' '.join('(subpath ' + quote(p) + ')' for p in runtime)
    handles = ' '.join('(literal ' + quote(p) + ')' for p in {*exe.parents, *temp.parents})
    source = '' if main is None else '(literal ' + quote(main) + ') (literal ' + quote(main.parent) + ')'
    return f'''(version 1) (deny default)
      (allow process-exec (literal {quote(exe)})) (deny process-fork)
      (allow sysctl-read) (allow file-read-metadata)
      (allow file-read* (literal {quote(exe)}) {handles} {source} {read} (subpath "/System") (subpath "/usr/lib") (subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))
      (allow file-read* file-write* (subpath {quote(temp)}))
      (deny file-read-data file-write* (regex #"(^|/)(\\.env[^/]*|\\.ssh|\\.aws|\\.gnupg|\\.pebble|credentials|secrets)(/|$)"))'''


class _Wire:
    """The worker's stdout as bounded JSON lines."""

    def __init__(self, stream):
        self.stream, self.buffer, self.total = stream, b'', 0

    def feed(self):
        chunk = os.read(self.stream.fileno(), 65536)
        self.total += len(chunk)
        if self.total > WIRE_BYTES:
            raise PolicyError('The script wrote more than its output budget; it was stopped.')
        self.buffer += chunk
        return bool(chunk)

    def lines(self):
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            yield line
        if len(self.buffer) > LINE_BYTES:
            raise PolicyError('A request from the script was longer than its limit; it was stopped.')


def _reserve(app, rid, call_id, name, args):
    """Reserve a nested receipt as `planned`, refusing an id that already exists (AgentEffects.execute's rule)."""
    from .runtime import encoded
    with app.store.lock, app.store.db as db:
        if db.execute('SELECT 1 FROM run_tools WHERE run_id=? AND call_id=?', (rid, call_id)).fetchone():
            raise PolicyError('The nested receipt collides with another tool proposal; this call did not run.')
        db.execute('INSERT INTO run_tools VALUES(?,?,?,?,?,NULL)', (rid, call_id, name, encoded(args), 'planned'))


def _state(app, rid, call_id):
    rows = app.store.query('SELECT state FROM run_tools WHERE run_id=? AND call_id=?', (rid, call_id))
    return rows[0]['state'] if rows else None


def run(app, args, ws, cid, call_id, step_tools):
    """Run one RunCode script for the run that owns `cid`; returns the model-facing result."""
    from .tool_authz import Actor
    code, purpose = args.get('code'), args.get('purpose', '')
    if not isinstance(code, str) or not code.strip() or len(code) > CODE_LIMIT:
        raise PolicyError(f'Send a Python script of at most {CODE_LIMIT} characters.')
    if not isinstance(purpose, str) or len(purpose) > PURPOSE_LIMIT:
        raise PolicyError(f'Describe the script in at most {PURPOSE_LIMIT} characters.')
    try:
        compile(code, '<RunCode>', 'exec')
    except SyntaxError as exc:
        raise PolicyError(f'The script is not valid Python: {exc.msg} (line {exc.lineno}). Nothing ran.') from None
    execution = app.controller.current()
    rid = app.active_run
    if not rid or app.active != cid or not execution or execution.run_id != rid or not isinstance(call_id, str):
        raise PolicyError('RunCode runs only inside its owning root run.')
    from . import copy_check
    if copy_check.enabled() and ws is not None:
        # Phase 46 (1.9.28, b-new C1, behind PEBBLE_1928_COPY_CHECK): a file's lines typed in by hand with a slip are
        # refused once per run, before anything runs; a check that cannot run never stops the script
        try:
            from .requirements import seen_paths
            refusal = None if app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind='copy_slip_refused' "
                                              'LIMIT 1', (rid,)) else copy_check.check(
                ws.root, seen_paths(ws.root, app.runs.tools(rid), app.current_text), code)
        except Exception:
            refusal = None
        if refusal:
            app.runs.note(rid, 'copy_slip_refused', {'call_id': call_id})
            raise PolicyError(refusal)
    offered ={t['function']['name']: t for t in step_tools or ()}
    tools = {fn: _help(offered[name]) for fn, name in INNER.items() if name in offered}
    from . import workshop
    tool_making = workshop.enabled()
    mine = workshop.mine_for_run(app, rid, code, validate_tool) if tool_making else None
    reported = {}  # uses already recorded from this script (keep requests report them mid-script, the done line at the end)

    def sync(counts):
        if not isinstance(counts, dict):
            return
        delta = {}
        for name, pair in counts.items():
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(n, int) and n >= 0 for n in pair):
                seen = reported.get(name, [0, 0])
                if pair[0] >= seen[0] and pair[1] >= seen[1]:
                    delta[name] = [pair[0] - seen[0], pair[1] - seen[1]]
                    reported[name] = list(pair)
        workshop.record_uses(app, rid, {k: v for k, v in delta.items() if v != [0, 0]})
    exe, command, runtime, main = _worker_command()
    started, calls, refused, receipts, sent = time.monotonic(), Counter(), 0, [], 0
    done = None
    with tempfile.TemporaryDirectory(prefix='pebble-code-') as folder:
        temp = Path(folder).resolve()
        env = {'HOME': str(temp), 'TMPDIR': str(temp) + '/', 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
               'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
               **person_zone()}  # Phase 46: the person's TZ (zone.py)
        process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile(exe, runtime, temp, main), *command],
                                   cwd=temp, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        wire = _Wire(process.stdout)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        stop = None
        try:
            from .mod_worker import _memory_reader
            resident = _memory_reader()
        except Exception:
            resident = None
        sampled = 0.0
        try:
            header = {'code': code, 'tools': tools}
            if tool_making:
                header.update(mine=mine, workshop=True)
            process.stdin.write((json.dumps(header, ensure_ascii=True) + '\n').encode())
            process.stdin.flush()
            expected = 1
            while done is None and stop is None:
                if app.cancel.is_set():
                    raise InterruptedError('Stopped while the script was running.')
                now = time.monotonic()
                if now - started > WALL_SECONDS:
                    stop = f'The script ran past its {WALL_SECONDS}-second limit and was stopped.'
                    break
                if resident and now - sampled > .25 and process.poll() is None:
                    sampled = now
                    try:
                        if resident(process.pid) > RSS_BYTES:
                            stop = 'The script used more memory than its limit and was stopped.'
                            break
                    except Exception:
                        pass
                if not selector.select(POLL_SECONDS):
                    continue
                more = wire.feed()
                for raw in wire.lines():
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        raise PolicyError('The script broke its protocol; it was stopped.') from None
                    if not isinstance(message, dict):
                        raise PolicyError('The script broke its protocol; it was stopped.')
                    if message.get('op') == 'done':
                        done = message
                        break
                    if message.get('op') != 'call' or message.get('id') != expected:
                        raise PolicyError('The script broke its protocol; it was stopped.')
                    expected += 1
                    if tool_making and message.get('tool') in WORKSHOP:
                        if message.get('tool') == 'keep_tool':
                            sync(message.get('uses'))
                        answer = _workshop(app, message, rid, cid, calls)
                    else:
                        answer = _serve(app, message, tools, offered, step_tools, ws, cid, rid, call_id, calls,
                                        receipts, Actor)
                    if 'error' in answer:
                        refused += 1
                    else:
                        body = json.dumps(answer['result'], ensure_ascii=True, allow_nan=False, default=str)
                        if len(body) > RESULT_BYTES or sent + len(body) > INNER_BYTES:
                            answer = {'error': 'The result is larger than a script may receive; use a narrower call '
                                               '(a line range, a pattern or a smaller page).'}
                            refused += 1
                        else:
                            sent += len(body)
                    process.stdin.write((json.dumps({'id': message['id'], **answer}, ensure_ascii=True,
                                                    allow_nan=False, default=str) + '\n').encode())
                    process.stdin.flush()
                if not more and done is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    stop = f'The script ended without finishing (exit status {process.returncode}).'
        except BrokenPipeError:
            stop = 'The script ended without finishing.'
        finally:
            selector.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:  # an exited leader needs no signal (the Phase 34 EPERM race)
                    process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()
    if tool_making and isinstance((done or {}).get('mine'), dict):
        sync(done['mine'])
    printed = (done or {}).get('printed') or ''
    if not isinstance(printed, str):
        printed = ''
    error = stop or (done or {}).get('error')
    return {
        'origin': 'run_code', 'authorization': 'none', 'purpose': purpose,
        'script_sha256': hashlib.sha256(code.encode()).hexdigest(),
        'untrusted_printed': printed[:SHOWN],
        'printed_truncated': len(printed) > SHOWN or bool((done or {}).get('printed_truncated')),
        'error': error[-4000:] if isinstance(error, str) else None,
        'calls': sum(calls.values()), 'by_tool': dict(calls), 'refused': refused,
        'receipts': receipts[:RECEIPTS_SHOWN], 'seconds': round(time.monotonic() - started, 2),
        'note': ("Only what the script printed is shown, as untrusted data. Each call it made has its own receipt "
                 "(ReadRunEvidence); edits and plugin calls were approved or left awaiting approval as direct calls "
                 "are."),
    }


def _workshop(app, message, rid, cid, calls):
    """Phase 41: pebble.make_tool and pebble.keep_tool, answered by workshop.py (a made tool is callable at once)."""
    from . import workshop
    args = message.get('args')
    if not isinstance(args, dict):
        return {'error': 'Give make_tool and keep_tool keyword arguments.'}
    if sum(calls.values()) >= MAX_CALLS:
        return {'error': f'A script may make at most {MAX_CALLS} calls.'}
    calls['MakeTool' if message['tool'] == 'make_tool' else 'KeepTool'] += 1
    try:
        if message['tool'] == 'make_tool':
            before = [t['name'] for t in app.runs.tools(rid)]
            result, made = workshop.make(app, rid, args, validate_tool, before)
            return {'result': result, **({'mine': made} if made else {})}
        return {'result': workshop.keep(app, rid, cid, args, validate_tool)}
    except PolicyError as exc:
        return {'error': str(exc)}


def validate_tool(code, tests, cases, case_seconds=1.0):
    """Phase 41: run one tool's tests and generated cases in a fresh worker under the script sandbox (validate mode);
    returns the worker's report (untrusted data, read by workshop.verdict)."""
    exe, command, runtime, main = _worker_command()
    limit = min(WALL_SECONDS, 5 + case_seconds * (len(tests) + 2 * len(cases)) * 1.2)
    with tempfile.TemporaryDirectory(prefix='pebble-tool-') as folder:
        temp = Path(folder).resolve()
        env = {'HOME': str(temp), 'TMPDIR': str(temp) + '/', 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8',
               'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
               **person_zone()}  # Phase 46: the person's TZ (zone.py)
        process = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile(exe, runtime, temp, main), *command],
                                   cwd=temp, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            header = {'mode': 'validate', 'code': code, 'tests': tests, 'cases': cases, 'case_seconds': case_seconds}
            out, _ = process.communicate((json.dumps(header, ensure_ascii=True) + '\n').encode(), timeout=limit)
        except subprocess.TimeoutExpired:
            return {'op': 'validated', 'load_error': f'validation ran past its {int(limit)}-second limit'}
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=5)
    try:
        return json.loads(out.decode('utf-8', 'replace').strip().splitlines()[-1][:LINE_BYTES])
    except (ValueError, IndexError):
        return {'op': 'validated', 'load_error': 'the validation worker gave no report'}


def _serve(app, message, tools, offered, step_tools, ws, cid, rid, call_id, calls, receipts, Actor):
    """One script call through the direct-call path; returns {'result': ...} or {'error': ...}."""
    function, args = message.get('tool'), message.get('args')
    if function not in tools or not isinstance(args, dict):
        return {'error': f'pebble.{function} is not available in this run; pebble.tools() lists what is.'}

    def one(name, args):
        if sum(calls.values()) >= MAX_CALLS:
            return {'error': f'A script may make at most {MAX_CALLS} calls.'}
        inner = f'{call_id}:c{sum(calls.values()) + 1}'
        calls[name] += 1
        try:
            app.validate(name, args, step_tools)
            app.authorize(name, args, Actor.root(rid), rid, call_id=inner, layers=('schema',))
            _reserve(app, rid, inner, name, args)
            receipts.append(inner)
            app.emit('progress', {'chat_id': cid, 'run_id': rid, 'kind': 'done', 'activity': None,
                                  'label': 'Script: ' + app.tool_label(name, args)})
            return {'result': app.execute(name, args, ws, cid, call_id=inner)}
        except PolicyError as exc:
            return {'error': str(exc)}
        except InterruptedError:
            raise
        except Exception as exc:
            if _state(app, rid, inner) in ('running', 'uncertain'):
                raise  # an effect whose outcome is unknown stops the script and the run, as a direct call would
            return {'error': f'The tool failed ({type(exc).__name__}).'}
    answer = one(INNER[function], args)
    return _whole_read(one, args, answer) if function == 'read' and 'result' in answer else answer


# Phase 46 (1.9.28, w10's security review, F1-1): above 8K a Read returns one page (200 lines unless the script gave a
# limit, and at most a character budget of that span), while the description names ['untrusted_source'] as the text,
# so a script could total a column over the first page and print a confident wrong number. A pebble.read with no line
# range returns the whole file, and one with a range returns the whole range: the rest is read page by page through the
# same path (each page its own Read and receipt, bound to the first page's file_hash), within the bytes a script may
# receive. A larger file is refused, never cut. A script that pages itself (char_offset) gets the page it asked for.
WHOLE_READ_TOO_LARGE = ('{path} is larger than a script may receive in one call; read it in parts with offset and limit '
                        '(at most 2000 lines each) and combine them in the script.')


def _whole_read(one, args, answer):
    first = answer['result']
    if 'char_offset' in args or not isinstance(first, dict) or not isinstance(first.get('untrusted_source'), str) \
            or type(first.get('total_lines')) is not int or type(first.get('start_line')) is not int:
        return answer
    whole = 'offset' not in args and 'limit' not in args
    span = {key: args[key] for key in ('file_path', 'offset', 'limit') if key in args}
    start, text, page, parts = first['start_line'], first['untrusted_source'], first, [first['untrusted_source']]
    while True:
        if page.get('next_char_offset') is not None:  # the line range continues by characters
            nxt = {**span, 'char_offset': page['next_char_offset'], 'expected_hash': first['file_hash']}
        elif whole and start - 1 + len(text.splitlines(keepends=True)) < first['total_lines']:  # more lines follow
            start += len(text.splitlines(keepends=True))
            span, text = {'file_path': args['file_path'], 'offset': start, 'limit': 2000}, ''
            nxt = {**span, 'expected_hash': first['file_hash']}
        else:
            break
        got = one('Read', nxt)
        if 'error' in got:
            return got
        page = got['result']
        if not isinstance(page, dict) or not isinstance(page.get('untrusted_source'), str) \
                or page.get('file_hash') != first['file_hash'] or not page['untrusted_source']:
            return {'error': 'The file changed while the script read it; read it again.'}
        text += page['untrusted_source']
        parts.append(page['untrusted_source'])
        if sum(len(part) for part in parts) > RESULT_BYTES:
            return {'error': WHOLE_READ_TOO_LARGE.format(path=args.get('file_path'))}
    if len(parts) == 1:
        return answer  # one page held all of it: the result is the Read's own, unchanged
    return {'result': {**first, 'untrusted_source': ''.join(parts), 'next_char_offset': None, 'excerpt_complete': True,
                       'pages': len(parts)}}
