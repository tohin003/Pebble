"""Phase 37 (1.5.0, Pebble's Workshop, Code Mode): the child entry of RunCode. Never imported for execution in the
service; `main.py --code-worker` runs it inside the sandbox code_mode.run builds (no network, no fork, no reads of the
project or the data home, writes only to its own temporary folder).

Protocol, JSON lines: the host writes one header line {code, tools: {name: help}, limits}; the child writes a request
line {"op": "call", "id": n, "tool": name, "args": {...}} for each pebble.<name>(...) call and reads one answer line
{"id": n, "result": ...} or {"id": n, "error": "..."}; it ends with one {"op": "done", ...} line. The host treats every
child line as untrusted: a request is authorised as if the model had made it directly, and the done line only reports
what the script printed and returned.
"""
import io
import json
import os
import resource
import sys
import traceback
import types

PRINT_LIMIT = 64_000   # characters of printed output kept in the child; the host shows at most 16,000


class Refused(Exception):
    """Raised in a script when the host refuses or fails one tool call; the message is the host's."""


class _HostGone(BaseException):
    """The host stopped answering or answered out of turn: end without a done line."""


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


def _module(tools, call):
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
    return module


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
    tools = {str(k): str(v) for k, v in header['tools'].items()}
    counter = [0]

    def call(name, args):
        counter[0] += 1
        wire_out.write(json.dumps({'op': 'call', 'id': counter[0], 'tool': name, 'args': args}, ensure_ascii=True,
                                  allow_nan=False) + '\n')
        wire_out.flush()
        line = wire_in.readline()
        if not line:
            raise _HostGone()
        answer = json.loads(line)
        if answer.get('id') != counter[0]:
            raise _HostGone()
        if 'error' in answer:
            raise Refused(str(answer['error']))
        return answer.get('result')

    pebble = _module(tools, call)
    sys.modules['pebble'] = pebble
    scope = {'__name__': '__main__', 'pebble': pebble}
    error = None
    try:
        exec(compile(header['code'], '<RunCode>', 'exec'), scope)
    except _HostGone:
        raise SystemExit(3)
    except BaseException:  # the script's own errors, sys.exit() included, are reported to the model
        error = ''.join(traceback.format_exception(*sys.exc_info())[-12:])[-4000:]
    wire_out.write(json.dumps({'op': 'done', 'printed': printed.value(), 'printed_truncated': printed.truncated,
                               'error': error}, ensure_ascii=True) + '\n')
    wire_out.flush()
