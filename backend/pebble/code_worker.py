"""Phase 37 (1.5.0, Pebble's Workshop, Code Mode): the child entry of RunCode. Never imported for execution in the
service; `main.py --code-worker` runs it inside the sandbox code_mode.run builds (no network, no fork, no reads of the
project or the data home, writes only to its own temporary folder).

Protocol, JSON lines: the host writes one header line {code, tools: {name: help}, limits}; the child writes a request
line {"op": "call", "id": n, "tool": name, "args": {...}} for each pebble.<name>(...) call and reads one answer line
{"id": n, "result": ...} or {"id": n, "error": "..."}; it ends with one {"op": "done", ...} line. The host treats every
child line as untrusted: a request is authorised as if the model had made it directly, and the done line only reports
what the script printed and returned.

Phase 41 (1.7.0, Pebble's Workshop part 2, agent-made tools; D67-D70, D90):
- The header may carry `mine` ({name: code}: validated tools, made in this run or kept in Pebble's Toolbox), callable
  as pebble.mine.<name>(**args) in this process, and `workshop` (pebble.make_tool and pebble.keep_tool exist). An
  answer to make_tool may carry `mine` too: a tool that just passed validation, callable at once. The done line gains
  `mine`, the uses and errors of each tool.
- A header with "mode": "validate" runs one tool's tests and the host's generated cases instead of a script, each case
  under a timer and twice (determinism), and answers with one {"op": "validated", ...} line (validate()).
"""
import io
import json
import math
import os
import resource
import signal
import sys
import time
import traceback
import types

PRINT_LIMIT = 64_000   # characters of printed output kept in the child; the host shows at most 16,000


class Refused(Exception):
    """Raised in a script when the host refuses or fails one tool call; the message is the host's."""


class _HostGone(BaseException):
    """The host stopped answering or answered out of turn: end without a done line."""


class _CaseTimeout(BaseException):
    """A tool case ran past its time limit (validate mode)."""


class _Bounded(io.TextIOBase):
    def __init__(self, limit):
        self.parts, self.size, self.limit, self.truncated = [], 0, limit, False

    def writable(self):
        return True

    def write(self, text):
        text = str(text)
        room = self.limit - self.size
        if room > 0:
            self.parts.append(text[:room])
            self.size += min(len(text), room)
        if len(text) > room:
            self.truncated = True
        return len(text)

    def value(self):
        return ''.join(self.parts)


def _load_tool(code):
    """A made tool's run(args), executed in a namespace of its own (the host's static check ran first)."""
    scope = {'__name__': 'pebble_tool'}
    exec(compile(code, '<tool>', 'exec'), scope)
    run = scope.get('run')
    if not callable(run):
        raise TypeError('the tool defines no run(args)')
    return run


class _Mine(types.SimpleNamespace):
    """pebble.mine: one function per validated tool; uses and errors counted for the done line."""

    def __init__(self, counts):
        super().__init__()
        object.__setattr__(self, '_counts', counts)
        object.__setattr__(self, '_unmade', {})

    def __getattr__(self, name):
        # Phase 46 (golden dev d-wr-01 on gpt-6.1-sol, 3 of 3 runs): a script made a tool, make_tool answered made False
        # with findings the script never printed, and its next line failed with a bare AttributeError. Say why.
        unmade = self.__dict__.get('_unmade', {})
        if name in unmade:
            raise AttributeError(f'pebble.mine.{name} does not exist: make_tool did not make it ({unmade[name]}). '
                                 'Fix the tool and call make_tool again, or compute without it.')
        raise AttributeError(f'pebble.mine has no tool {name!r}.')

    def _add(self, name, code):
        run = _load_tool(code)
        counts = self._counts

        def tool(**args):
            counts.setdefault(name, [0, 0])[0] += 1
            try:
                return run(json.loads(json.dumps(args)))  # a fresh copy: a tool cannot keep the caller's objects
            except Exception:
                counts[name][1] += 1
                raise
        tool.__name__ = name
        setattr(self, name, tool)


def _module(tools, call, mine=None, workshop=False):
    module = types.ModuleType('pebble', 'Pebble tools for this run. help(name) describes one; tools() lists them.')
    module.Refused = Refused

    def bind(name):
        def function(**args):
            return call(name, args)
        function.__name__ = name
        function.__doc__ = tools[name]
        return function

    for name in tools:
        setattr(module, name, bind(name))
    module.tools = lambda: sorted(tools)
    module.help = lambda name: tools.get(name, 'No tool named %r in this run.' % (name,))
    if mine is not None:
        module.mine = mine
    if workshop:
        module.make_tool = _workshop(call, 'make_tool')
        module.keep_tool = _workshop(call, 'keep_tool')
    return module


# Phase 45 (F147, audit round R4): the model called pebble.keep_tool('uxtest_c_to_f', '...') as the Workshop's own words
# write it (code_mode.WORKSHOP_SENTENCE), the functions took keywords only, the TypeError meant no keep, and the reply
# said the tool was kept. Both take their arguments in that order too.
WORKSHOP_ORDER = {'make_tool': ('name', 'purpose', 'code', 'tests', 'inputs'), 'keep_tool': ('name', 'reason')}


def _workshop(call, name):
    order = WORKSHOP_ORDER[name]

    def function(*positional, **args):
        if len(positional) > len(order):
            raise TypeError(f'pebble.{name} takes at most {len(order)} arguments: {", ".join(order)}.')
        for key, value in zip(order, positional):
            if key in args:
                raise TypeError(f'pebble.{name} got {key} twice.')
            args[key] = value
        return call(name, args)
    function.__name__ = name
    return function


def _alarm(signum, frame):
    raise _CaseTimeout()


def _case(run, args, seconds):
    """(outcome, value): 'ok' with the result's JSON text, 'raised' with the exception, 'timeout', or 'not_json'."""
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        value = run(json.loads(json.dumps(args)))
    except _CaseTimeout:
        return 'timeout', None
    except Exception as exc:  # SystemExit and the like end the worker; they are not a tool's outcome
        return 'raised', type(exc).__name__ + ': ' + str(exc)[:200]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        return 'ok', json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return 'not_json', type(value).__name__


def _same(got, expect):
    """Phase 45 (F097, audit round R3): a result equals what a test expects as a value, numbers as numbers (77 and 77.0,
    98.6 and 98.60000000000001). On JSON text alone the independent check failed a correct Celsius-to-Fahrenheit tool 0
    of 5, so the simplest pure calculation was never kept on its own. A boolean is never a number here."""
    if isinstance(got, bool) or isinstance(expect, bool):
        return type(got) is type(expect) and got == expect
    if isinstance(got, (int, float)) and isinstance(expect, (int, float)):
        return math.isclose(got, expect, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(got, list) and isinstance(expect, list):
        return len(got) == len(expect) and all(_same(a, b) for a, b in zip(got, expect))
    if isinstance(got, dict) and isinstance(expect, dict):
        return got.keys() == expect.keys() and all(_same(got[k], expect[k]) for k in got)
    return got == expect


def validate(header):
    """One tool's own tests, then the host's generated cases (each twice), every case under its timer."""
    began = time.monotonic()
    seconds = float(header.get('case_seconds', 1))
    try:
        run = _load_tool(header['code'])
    except BaseException as exc:
        return {'op': 'validated', 'load_error': (type(exc).__name__ + ': ' + str(exc))[:300]}
    tests = []
    for test in header.get('tests', []):
        outcome, value = _case(run, test.get('args', {}), seconds)
        if 'raises' in test:
            passed = outcome == 'raised' and value.split(':', 1)[0] == test['raises']
        else:
            passed = outcome == 'ok' and _same(json.loads(value), test.get('expect'))
        tests.append({'passed': passed, 'outcome': outcome, 'got': (value or '')[:300]})
    cases = {'run': 0, 'returned': 0, 'rejected': 0, 'findings': []}
    for args in header.get('cases', []):
        cases['run'] += 1
        first = _case(run, args, seconds)
        if first[0] == 'raised' and first[1].split(':', 1)[0] in ('ValueError', 'TypeError'):
            cases['rejected'] += 1  # a deliberate refusal of an input it does not handle
            continue
        if first[0] != 'ok':
            cases['findings'].append({'args': json.dumps(args)[:300], 'outcome': first[0],
                                      'detail': (first[1] or '')[:200]})
            continue
        if _case(run, args, seconds) != first:
            cases['findings'].append({'args': json.dumps(args)[:300], 'outcome': 'nondeterministic'})
            continue
        cases['returned'] += 1
    return {'op': 'validated', 'tests': tests, 'cases': cases, 'seconds': round(time.monotonic() - began, 3)}


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (20, 21))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8_388_608, 8_388_608))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    wire_in = sys.stdin
    wire_out = os.fdopen(os.dup(1), 'w', encoding='utf-8')
    printed = _Bounded(PRINT_LIMIT)
    sys.stdout = sys.stderr = printed
    header = json.loads(wire_in.readline())
    if header.get('mode') == 'validate':
        wire_out.write(json.dumps(validate(header), ensure_ascii=True) + '\n')
        wire_out.flush()
        return
    tools = {str(k): str(v) for k, v in header['tools'].items()}
    counter, counts = [0], {}
    mine = _Mine(counts) if isinstance(header.get('mine'), dict) else None
    for name, code in (header.get('mine') or {}).items():
        try:
            mine._add(str(name), str(code))
        except BaseException:
            pass  # a kept tool that no longer loads is simply absent; the host re-validates kept tools

    def call(name, args):
        counter[0] += 1
        request = {'op': 'call', 'id': counter[0], 'tool': name, 'args': args}
        if name == 'keep_tool' and mine is not None:
            request['uses'] = counts  # the uses so far: a tool used earlier in this script counts for its keep
        wire_out.write(json.dumps(request, ensure_ascii=True, allow_nan=False) + '\n')
        wire_out.flush()
        line = wire_in.readline()
        if not line:
            raise _HostGone()
        answer = json.loads(line)
        if answer.get('id') != counter[0]:
            raise _HostGone()
        if 'error' in answer:
            raise Refused(str(answer['error']))
        if mine is not None and isinstance(answer.get('mine'), dict):
            for made, code in answer['mine'].items():
                mine._add(str(made), str(code))
        result = answer.get('result')
        if (name == 'make_tool' and mine is not None and isinstance(result, dict) and result.get('made') is False
                and isinstance(args.get('name'), str)):
            findings = result.get('findings') if isinstance(result.get('findings'), list) else []
            mine._unmade[args['name']] = '; '.join(str(f) for f in findings)[:400] or 'no reason given'
        return result

    pebble = _module(tools, call, mine, bool(header.get('workshop')))
    sys.modules['pebble'] = pebble
    scope = {'__name__': '__main__', 'pebble': pebble}
    error = None
    try:
        exec(compile(header['code'], '<RunCode>', 'exec'), scope)
    except _HostGone:
        raise SystemExit(3)
    except BaseException:  # the script's own errors, sys.exit() included, are reported to the model
        error = ''.join(traceback.format_exception(*sys.exc_info())[-12:])[-4000:]
    done = {'op': 'done', 'printed': printed.value(), 'printed_truncated': printed.truncated, 'error': error}
    if mine is not None:
        done['mine'] = counts
    wire_out.write(json.dumps(done, ensure_ascii=True) + '\n')
    wire_out.flush()
