"""Phase 41 (1.7.0, Pebble's Workshop part 2: agent-made tools; D67-D70, D90 and the research's changes).

Inside a RunCode script, pebble.make_tool(name=, purpose=, code=, tests=, inputs=) makes a tool. It is checked here and
in a fresh sandboxed worker, and on success it is callable at once as pebble.mine.<name>(**args). pebble.keep_tool(
name=, reason=) asks to keep it in Pebble's Toolbox. What makes these tools safe to keep:
- pure by construction: the static check (ast only: one run(args); imports from a pure allowlist; no exec, eval, open,
  dunder access or pebble) and the worker's sandbox (no network, no files, no processes). Purity is what the sandbox
  enforces, never what the model says;
- validated independently and fast: the maker's tests, frozen with the code under one digest, and about 50 inputs
  generated from the input schema with a fixed seed, each of which must return JSON or deliberately raise ValueError or
  TypeError within a second, and return the same thing twice;
- made from experience: it is kept only after it was used successfully in this task;
- D69: Pebble keeps it on its own only when it is validated, used, untainted (nothing untrusted was read before it was
  made) and a blind check passes (a separate model call writes tests from the purpose and the schema, never the code;
  at least 4 of 5 must pass). Pebble then always tells the person: a "Kept in Toolbox" card with View code and Remove.
  Every other keep asks the person;
- a supply chain: each kept tool stores its digest, origin and validation. It is verified whenever it is loaded, and
  re-validated (its stored tests and generated inputs) before the first script in an app session that names it;
- small and searched: at most 50 kept tools, found with FindTool; a keep whose name is already kept asks to replace it.
Storage: the `toolbox` setting (no new tables): {'tools': [record], 'pending': [{id, record, why, chat_id, run_id}]}.
PEBBLE_TOOL_MAKING=0 turns the Workshop off (1.6.0's RunCode exactly).
"""

import ast
import hashlib
import json
import keyword
import os
import random
import re
import threading
import time
import uuid
from collections import OrderedDict

from .policy import PolicyError

ENV_SWITCH = 'PEBBLE_TOOL_MAKING'
SETTING = 'toolbox'
NAME = re.compile(r'^[a-z][a-z0-9_]{2,39}$')
CODE_LIMIT = 12_000
PURPOSE_LIMIT = 200
MAX_TESTS = 20
MIN_TESTS = 3
MAX_KEPT = 50
UNDO_SECONDS = 600.0   # Phase 45 (F178): a removed tool can be put back for ten minutes
MAX_MAKES = 3          # per name per run
CASES = 50
CASE_SECONDS = 1.0
BLIND_TESTS = 5
BLIND_PASS = 4
RUNS_REMEMBERED = 20
PURE_MODULES = frozenset({
    'json', 're', 'math', 'cmath', 'statistics', 'datetime', 'decimal', 'fractions', 'numbers', 'collections',
    'itertools', 'functools', 'operator', 'string', 'textwrap', 'unicodedata', 'difflib', 'heapq', 'bisect', 'base64',
    'binascii', 'hashlib', 'zlib', 'csv', 'io', 'dataclasses', 'enum', 'typing', 'calendar', 'copy', 'ipaddress',
    'colorsys', 'html', 'urllib.parse', 'uuid', 'struct', 'array', 'pprint', 'shlex',
})
# Names that reach outside a computation, or around this check. random and time are left out: a tool's result must not
# depend on when or how often it runs (the determinism check would catch it later; this says why first).
FORBIDDEN_NAMES = frozenset({
    'exec', 'eval', 'compile', '__import__', 'open', 'input', 'breakpoint', 'globals', 'locals', 'vars', 'getattr',
    'setattr', 'delattr', 'memoryview', 'exit', 'quit', 'help', 'pebble', '__builtins__', '__loader__', '__spec__',
})
RESERVED = frozenset({'run', 'print', 'list', 'dict', 'str', 'int', 'float', 'len', 'range', 'map', 'filter', 'tools',
                      'help', 'mine', 'make_tool', 'keep_tool', 'refused'})
TYPES = ('string', 'integer', 'number', 'boolean', 'array', 'object')
MAX_PROPERTIES = 12
# Results from outside the person's own request: a tool made after any of these may encode what such content asked for,
# so only the person can keep it (D69's "untainted"). The 1.6.0 Companion tools' outputs count too.
UNTRUSTED_SOURCES = frozenset({
    'ReadURL', 'WebSearch', 'ReadSource', 'PluginCall', 'FetchPluginData', 'ReadPluginData', 'SearchPluginData',
    'ReadPluginMedia', 'ReadAttachment', 'LoadSkill', 'ScreenObserve', 'ReadAgentMessage', 'ReadAgentPatch',
    'ReadProcess', 'WaitProcess', 'ReadFile', 'Shell', 'Screenshot',
})
BLIND_PROMPT = (
    'Write {n} test cases for a function, from its description and input schema only (you will not see its code). '
    'Each case is {{"args": <an input object valid for the schema>, "expect": <the exact JSON result>}}, or '
    '{{"args": ..., "raises": "ValueError"}} for an input it should refuse. Prefer cases whose result is unambiguous. '
    'Reply with a JSON array only.\n\nName: {name}\nDescription: {purpose}\nInput schema: {inputs}')


def enabled():
    return os.environ.get(ENV_SWITCH, '1') != '0'


# -- checks ------------------------------------------------------------------------------------------------------

def static_findings(name, purpose, code):
    """The static check (ast only; the code is parsed, never run, here). Empty when the tool passes."""
    findings = []
    if not isinstance(name, str) or not NAME.match(name) or keyword.iskeyword(name) or name in RESERVED:
        findings.append('Name the tool with 3-40 lowercase letters, digits or underscores, starting with a letter, '
                        'and not a Python keyword or one of pebble\'s own names.')
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > PURPOSE_LIMIT:
        findings.append(f'Describe what the tool does in at most {PURPOSE_LIMIT} characters.')
    if not isinstance(code, str) or not code.strip() or len(code) > CODE_LIMIT:
        findings.append(f'Send the tool as Python of at most {CODE_LIMIT} characters.')
        return findings
    try:
        tree = ast.parse(code, '<tool>')
    except SyntaxError as exc:
        return findings + [f'The tool is not valid Python: {exc.msg} (line {exc.lineno}).']
    runs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run']
    if len(runs) != 1 or len(runs[0].args.args) != 1 or runs[0].args.vararg or runs[0].args.kwarg:
        findings.append('Define exactly one top-level function run(args) taking one argument, the input object.')
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom, ast.Assign,
                                 ast.AnnAssign, ast.Expr)) or (isinstance(node, ast.Expr)
                                                               and not isinstance(node.value, ast.Constant)):
            findings.append(f'Line {node.lineno}: only definitions, imports and constants at the top level.')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in PURE_MODULES:
                    findings.append(f'Line {node.lineno}: import {alias.name} is not allowed in a pure tool.')
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or '') not in PURE_MODULES:
                findings.append(f'Line {node.lineno}: from {node.module} import is not allowed in a pure tool.')
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            findings.append(f'Line {node.lineno}: {node.id} is not allowed in a pure tool.')
        elif isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            findings.append(f'Line {node.lineno}: dunder attribute access ({node.attr}) is not allowed.')
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            findings.append(f'Line {node.lineno}: global and nonlocal state are not allowed in a pure tool.')
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            findings.append(f'Line {node.lineno}: async code is not allowed in a pure tool.')
    return list(dict.fromkeys(findings))


def schema_findings(schema):
    if not isinstance(schema, dict) or schema.get('type') != 'object' or not isinstance(schema.get('properties'), dict):
        return ['Declare inputs as {"type": "object", "properties": {...}, "required": [...]}.']
    props = schema['properties']
    if not props or len(props) > MAX_PROPERTIES:
        return [f'Declare 1-{MAX_PROPERTIES} input properties.']
    findings = []
    for key, spec in props.items():
        if not isinstance(spec, dict) or spec.get('type') not in TYPES:
            findings.append(f'Input {key!r}: type must be one of {", ".join(TYPES)}.')
        elif spec['type'] == 'array' and (not isinstance(spec.get('items', {}), dict)
                                          or spec.get('items', {}).get('type', 'string') not in TYPES[:4]):
            findings.append(f'Input {key!r}: array items must be string, integer, number or boolean.')
    required = schema.get('required', [])
    if not isinstance(required, list) or any(r not in props for r in required):
        findings.append('"required" must list declared inputs.')
    return findings


def test_findings(tests, minimum=MIN_TESTS):
    if not isinstance(tests, list) or not minimum <= len(tests) <= MAX_TESTS:
        return [f'Give {minimum}-{MAX_TESTS} tests: [{{"args": {{...}}, "expect": value}} or '
                '{"args": {...}, "raises": "ValueError"}].']
    findings = []
    for n, test in enumerate(tests, 1):
        if not isinstance(test, dict) or not isinstance(test.get('args'), dict) or \
                ('expect' in test) == ('raises' in test) or \
                ('raises' in test and test['raises'] not in ('ValueError', 'TypeError')):
            findings.append(f'Test {n}: give args (an object) and exactly one of expect or raises '
                            '("ValueError" or "TypeError").')
    try:
        json.dumps(tests, allow_nan=False)
    except (TypeError, ValueError):
        findings.append('Tests must be plain JSON.')
    return findings


def _edges(spec):
    kind, enum = spec.get('type'), spec.get('enum')
    if isinstance(enum, list) and enum:
        return list(enum)
    if kind == 'string':
        return ['', ' ', 'a', 'Ünïcødé ✓ 日本語', 'x' * 10_000, '0', '-1', 'null', '\n\t', 'a,b;c']
    if kind == 'integer':
        low, high = spec.get('minimum'), spec.get('maximum')
        values = [0, 1, -1, 2, 10 ** 12, -(10 ** 12)] + [v for v in (low, high) if isinstance(v, int)]
        return [v for v in values if (low is None or v >= low) and (high is None or v <= high)] or [low or 0]
    if kind == 'number':
        low, high = spec.get('minimum'), spec.get('maximum')
        values = [0.0, 1.5, -1.5, 1e-9, 1e12, -1e12, 0.1 + 0.2]
        return [v for v in values if (low is None or v >= low) and (high is None or v <= high)] or [low or 0.0]
    if kind == 'boolean':
        return [False, True]
    if kind == 'array':
        edges = _edges(spec.get('items', {'type': 'string'}))
        return [[], edges[:1], edges[:3], edges[:1] * 2, [edges[-1]] * 200]
    if kind == 'object':
        return [{}, {'a': 1}, {'nested': {'x': [1, 2]}}]
    return [None]


def generate(schema, seed=41):
    """Schema-valid inputs the maker did not write: every property's edge values with the others at their first edge,
    then seeded mixtures; optional properties also left out. The same schema and seed give the same inputs."""
    props, required = schema['properties'], set(schema.get('required', []))
    edges = {key: _edges(spec) for key, spec in props.items()}
    base = {key: values[0] for key, values in edges.items()}
    cases, seen = [], set()

    def add(case):
        mark = json.dumps(case, sort_keys=True)
        if mark not in seen and len(cases) < CASES:
            seen.add(mark)
            cases.append(case)
    add(dict(base))
    for key, values in edges.items():
        for value in values:
            add({**base, key: value})
        if key not in required:
            add({k: v for k, v in base.items() if k != key})
    rng = random.Random(seed)
    for _ in range(CASES * 4):
        if len(cases) >= CASES:
            break
        add({key: rng.choice(values) for key, values in edges.items() if key in required or rng.random() < .7})
    return cases


def packed(spec):
    return json.dumps(spec, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(spec):
    return hashlib.sha256(packed(spec).encode()).hexdigest()


def verdict(report, tests):
    """(passed, findings) from a validate-mode report (untrusted data: read defensively)."""
    if not isinstance(report, dict) or report.get('op') != 'validated':
        return False, ['The validation worker did not report.']
    if report.get('load_error'):
        return False, ['The tool did not load: ' + str(report['load_error'])[:300]]
    findings = []
    results = report.get('tests') if isinstance(report.get('tests'), list) else []
    if len(results) != len(tests):
        findings.append('Not every test ran.')
    for n, result in enumerate(results, 1):
        if not (isinstance(result, dict) and result.get('passed') is True):
            findings.append(f'Test {n} failed ({result.get("outcome") if isinstance(result, dict) else "?"}: '
                            f'got {str((result or {}).get("got", ""))[:200]}).')
    cases = report.get('cases') if isinstance(report.get('cases'), dict) else {}
    for item in (cases.get('findings') or [])[:5]:
        findings.append(f'A generated input {str(item.get("args", ""))[:120]} gave {item.get("outcome")}'
                        + (f' ({str(item.get("detail"))[:120]})' if item.get('detail') else '') +
                        '; return JSON, or raise ValueError or TypeError for inputs the tool does not handle.')
    if cases.get('run', 0) and not cases.get('returned', 0):
        findings.append('The tool refused every generated input; it must work for ordinary valid inputs.')
    return not findings, findings


# -- state -------------------------------------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.made = OrderedDict()      # run id -> {name: record}
        self.revalidated = {}          # digest -> True/False this app session


def state(app):
    if getattr(app, 'workshop', None) is None:
        app.workshop = State()
    return app.workshop


def _made(app, rid):
    with state(app).lock:
        made = state(app).made
        if rid not in made:
            made[rid] = {}
            while len(made) > RUNS_REMEMBERED:
                made.popitem(last=False)
        made.move_to_end(rid)
        return made[rid]


def toolbox(app):
    value = app.store.setting(SETTING, None)
    if not isinstance(value, dict):
        value = {}
    # Phase 45 (F178): tools the person removed in the last ten minutes, for Put back; older ones are dropped here, so
    # the next save forgets them
    now = time.time()
    return {'tools': [t for t in value.get('tools', []) if isinstance(t, dict)],
            'pending': [p for p in value.get('pending', []) if isinstance(p, dict)],
            'removed': [r for r in value.get('removed', []) if isinstance(r, dict) and isinstance(r.get('record'), dict)
                        and isinstance(r['record'].get('spec'), dict)
                        and 0 <= now - float(r.get('removed_at') or 0) < UNDO_SECONDS][-5:]}


def _save(app, value):
    app.store.set_setting(SETTING, value)


def verified(record):
    return isinstance(record, dict) and isinstance(record.get('spec'), dict) and digest(record['spec']) == record.get('digest')


# -- make ----------------------------------------------------------------------------------------------------------

PY_TYPES = {'str': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean', 'dict': 'object', 'list': 'array'}
TEST_KEYS = (('args', ('inputs', 'input', 'kwargs')), ('expect', ('expected', 'output', 'result', 'returns')))
MAKE_MIN_TESTS = 1  # Phase 46: usable in its task with one test of its own; Pebble keeps it on its own only with MIN_TESTS


def natural(name, code, tests, inputs):
    """Phase 46 (golden dev d-wr-01 on gpt-6.1-sol: both first make_tool calls failed on their shapes alone, each costing
    a 12-20 s request): the shapes a model writes unprompted, turned into the canonical ones before any check. One
    top-level function named after the tool (keyword arguments, or one `args` object) gains run(args) calling it; inputs
    given as {name: type} become the object schema with every name required; a test's inputs and expected become args
    and expect. Anything else is left as it was, for the checks to name."""
    if isinstance(inputs, dict) and inputs and 'properties' not in inputs and 'type' not in inputs:
        props = {}
        for key, kind in inputs.items():
            kind = kind.get('type') if isinstance(kind, dict) and len(kind) == 1 else kind
            if not isinstance(kind, str):
                props = None
                break
            props[key] = {'type': PY_TYPES.get(kind, kind)}
        if props:
            inputs = {'type': 'object', 'properties': props, 'required': list(props)}
    if isinstance(tests, list):
        renamed = []
        for test in tests:
            if isinstance(test, dict):
                test = dict(test)
                for canonical, aliases in TEST_KEYS:
                    alias = next((a for a in aliases if a in test), None)
                    if canonical not in test and alias is not None:
                        test[canonical] = test.pop(alias)
            renamed.append(test)
        tests = renamed
    if isinstance(code, str) and isinstance(name, str) and NAME.match(name):
        try:
            body = ast.parse(code, '<tool>').body
        except SyntaxError:
            body = []
        functions = [n for n in body if isinstance(n, ast.FunctionDef)]
        named = [f for f in functions if f.name == name]
        if body and not any(f.name == 'run' for f in functions) and len(named) == 1:
            params = [a.arg for a in named[0].args.args]
            call = f'{name}(args)' if params == ['args'] else f'{name}(**args)'
            code = code.rstrip() + f'\n\n\ndef run(args):\n    return {call}\n'
    return code, tests, inputs


def make(app, rid, args, validate_tool, receipts=()):
    """pebble.make_tool. Returns (result for the script, {name: code} when it passed)."""
    name, purpose = args.get('name'), args.get('purpose')
    code, tests, inputs = natural(name, args.get('code'), args.get('tests'), args.get('inputs'))
    findings = static_findings(name, purpose, code) + schema_findings(inputs) + test_findings(tests, MAKE_MIN_TESTS)
    made = _made(app, rid)
    previous = made.get(name) if isinstance(name, str) else None
    if previous and previous.get('makes', 0) >= MAX_MAKES:
        findings.append(f'{name} was made {MAX_MAKES} times in this task; describe what blocks it instead.')
    if findings:
        return {'made': False, 'findings': findings[:12]}, None
    spec = {'version': 1, 'name': name, 'purpose': purpose.strip(), 'code': code, 'tests': tests, 'inputs': inputs,
            'capability': 'none'}
    cases = generate(inputs)
    report = validate_tool(code, tests, cases, CASE_SECONDS)
    passed, problems = verdict(report, tests)
    record = {'spec': spec, 'digest': digest(spec), 'validated': passed, 'uses': 0, 'errors': 0,
              'makes': (previous or {}).get('makes', 0) + 1, 'made_after': list(receipts),
              'validation': {'tests': len(tests), 'cases_run': (report.get('cases') or {}).get('run', 0)
                             if isinstance(report, dict) else 0,
                             'seconds': report.get('seconds') if isinstance(report, dict) else None,
                             'findings': problems[:8]}}
    with state(app).lock:
        made[name] = record
    app.runs.note(rid, 'workshop_made', {'name': name, 'digest': record['digest'], 'validated': passed,
                                         'findings': problems[:3]})
    if not passed:
        return {'made': False, 'name': name, 'findings': problems[:12],
                'note': 'Fix the tool and call make_tool again (at most %d times for one name).' % MAX_MAKES}, None
    return {'made': True, 'name': name, 'digest': record['digest'][:16], 'tests_passed': len(tests),
            'generated_inputs': record['validation']['cases_run'],
            'note': f'Use it as pebble.mine.{name}(**args). Keep it with pebble.keep_tool(name=..., reason=...) only '
                    'after it helped with this task' + ('.' if len(tests) >= MIN_TESTS else
                                                       f' (Pebble keeps it on its own only with {MIN_TESTS}+ tests).')}, \
        {name: code}


def record_uses(app, rid, counts):
    """The done line's uses and errors per tool, for this run's made tools and the Toolbox."""
    if not isinstance(counts, dict) or not counts:
        return
    made = _made(app, rid)
    box = toolbox(app)
    changed = False
    for name, pair in counts.items():
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(n, int) and n >= 0 for n in pair)):
            continue
        if name in made:
            made[name]['uses'] += pair[0]
            made[name]['errors'] += pair[1]
        for tool in box['tools']:
            if tool['spec']['name'] == name:
                tool['uses'] = tool.get('uses', 0) + pair[0]
                tool['last_used'] = time.time()
                changed = True
    if changed:
        _save(app, box)


# -- keep ----------------------------------------------------------------------------------------------------------

def tainted(receipts_before):
    for name in receipts_before:
        if name in UNTRUSTED_SOURCES:
            return 'the task read ' + name + ' output before the tool was made'
    return None


def blind_check(app, spec, validate_tool, complete=None):
    """A separate model call writes tests from the purpose and schema only; returns {'run', 'passed'} or None."""
    prompt = BLIND_PROMPT.format(n=BLIND_TESTS, name=spec['name'], purpose=spec['purpose'],
                                 inputs=json.dumps(spec['inputs'], sort_keys=True))
    try:
        reply = (complete or _side_complete)(app, [{'role': 'user', 'content': prompt}])
        text = reply if isinstance(reply, str) else (reply or {}).get('content') or ''
        match = re.search(r'\[.*\]', text, re.S)
        tests = json.loads(match.group(0)) if match else []
    except Exception:
        return None
    tests = [t for t in tests if isinstance(t, dict) and isinstance(t.get('args'), dict)
             and (('expect' in t) != ('raises' in t))][:BLIND_TESTS]
    if len(tests) < BLIND_PASS:
        return None
    report = validate_tool(spec['code'], tests, [], CASE_SECONDS)
    results = report.get('tests') if isinstance(report, dict) and isinstance(report.get('tests'), list) else []
    return {'run': len(tests), 'passed': sum(1 for r in results if isinstance(r, dict) and r.get('passed') is True)}


def _side_complete(app, messages):
    from .agent_provider import PinnedProvider
    provider = PinnedProvider(app, app.model.current(), 'workshop:' + uuid.uuid4().hex[:12], app.agents.lane)
    try:
        result = provider.complete(messages, [], app.cancel, lambda *a, **k: None)
    finally:
        provider.close()
    return result


def keep(app, rid, cid, args, validate_tool, complete=None):
    """pebble.keep_tool: ('kept' and a notice the person always sees) or ('asked': a card the person decides)."""
    name, reason = args.get('name'), args.get('reason', '')
    made = _made(app, rid)
    if not isinstance(name, str) or name not in made:
        return {'kept': False, 'error': 'Make the tool with pebble.make_tool in this task first.'}
    if not isinstance(reason, str) or len(reason) > PURPOSE_LIMIT:
        reason = str(reason)[:PURPOSE_LIMIT]
    record = made[name]
    box = toolbox(app)
    # Phase 45 (F203, audit round R7): the same tool kept twice in one task asked the person twice
    if any(t['spec']['name'] == name and t.get('digest') == record['digest'] for t in box['tools']):
        return {'kept': True, 'already': True,
                'note': f"This exact tool is already in Pebble's Toolbox as {name}; nothing more to do."}
    if any(p['record']['spec']['name'] == name and p['record'].get('digest') == record['digest'] for p in box['pending']):
        return {'kept': False, 'asked': True, 'already_asked': True,
                'note': 'The person has not decided on this tool yet; do not ask again.'}
    why = []
    if not record['validated']:
        why.append('it did not pass validation')
    elif record['validation']['tests'] < MIN_TESTS:  # Phase 46 (MAKE_MIN_TESTS): made with fewer tests of its own
        why.append('it has only %d test(s) of its own (Pebble keeps a tool on its own with at least %d)'
                   % (record['validation']['tests'], MIN_TESTS))
    if record['uses'] - record['errors'] < 1:
        why.append('it has not been used successfully in this task yet')
    taint = tainted(record.get('made_after', []))
    if taint:
        why.append('it may be shaped by untrusted content (' + taint + ')')
    if any(t['spec']['name'] == name for t in box['tools']):
        why.append('a tool with this name is already kept; keeping this one replaces it')
    if len(box['tools']) >= MAX_KEPT:
        why.append(f'the Toolbox already holds {MAX_KEPT} tools')
    blind = None
    if not why:
        blind = blind_check(app, record['spec'], validate_tool, complete)
        if blind is None:
            why.append('the independent check could not run')
        elif blind['passed'] < BLIND_PASS:
            why.append('the independent check disagreed (%d of %d tests passed)' % (blind['passed'], blind['run']))
    kept = {'spec': record['spec'], 'digest': record['digest'], 'validation': record['validation'], 'blind': blind,
            'provenance': {'run_id': rid, 'chat_id': cid, 'reason': reason, 'made_at': time.time(),
                           'model': (app.model.current() or {}).get('model')},
            'uses': record['uses'], 'last_used': time.time(), 'kept_at': time.time(), 'state': 'ok'}
    if not why:
        kept['kept_by'] = 'pebble'
        box['tools'].append(kept)
        _save(app, box)
        notice = {'kind': 'kept', 'name': name, 'purpose': record['spec']['purpose'], 'reason': reason,
                  'tests': record['validation']['tests'], 'blind': blind}
        app.runs.note(rid, 'workshop_kept', {'name': name, 'digest': record['digest'], 'by': 'pebble'})
        app.emit('toolbox', {'chat_id': cid, 'run_id': rid, **notice})
        return {'kept': True, 'by': 'pebble', 'notice': f'Kept in Pebble\'s Toolbox: {name}. Tell the person you kept '
                                                         'it and that they can remove it.'}
    pending = {'id': uuid.uuid4().hex, 'record': kept, 'why': why, 'chat_id': cid, 'run_id': rid, 'asked_at': time.time()}
    box['pending'] = [p for p in box['pending'] if p['record']['spec']['name'] != name][-19:] + [pending]
    _save(app, box)
    app.runs.note(rid, 'workshop_asked', {'name': name, 'digest': record['digest'], 'why': why})
    app.emit('toolbox', {'chat_id': cid, 'run_id': rid, 'kind': 'asked', 'name': name, 'pending_id': pending['id'],
                         'purpose': record['spec']['purpose'], 'why': why})
    return {'kept': False, 'asked': True, 'why': why,
            'note': 'The person decides whether to keep it; say why you asked.'}


# -- the Toolbox for scripts, FindTool and Settings ---------------------------------------------------------------

def conversation_tools(app, rid):
    """Phase 46 (dev d-wr-01 on gpt-6.1-sol: every follow-up turn wrote and validated the same tool again, about 20 s a
    turn): {name: record} for the validated tools earlier runs of this run's conversation made in this app session,
    newest first winning. A follow-up turn is a new run; a tool its conversation made stays usable in it."""
    try:
        cid = app.runs.get(rid)['chat_id']
        earlier = [r['id'] for r in app.runs.list(cid) if r['id'] != rid]
    except Exception:
        return {}
    found = {}
    with state(app).lock:
        for other in earlier:  # newest first (Runs.list)
            for name, record in (state(app).made.get(other) or {}).items():
                if record.get('validated') and name not in found:
                    found[name] = record
    return found


def mine_for_run(app, rid, code, validate_tool):
    """{name: code} for a script: this run's validated tools, the conversation's (conversation_tools: shared into this
    run, so its uses and keeps count as for its own), and kept tools the script names (re-validated once per app session
    before use; a changed or failing one is marked needs_attention and left out)."""
    made = _made(app, rid)
    for name, record in conversation_tools(app, rid).items():
        with state(app).lock:
            made.setdefault(name, record)
    tools = {name: r['spec']['code'] for name, r in made.items() if r['validated']}
    box = toolbox(app)
    changed = False
    for tool in box['tools']:
        name = tool.get('spec', {}).get('name')
        if not name or name in tools or ('mine.' + name) not in (code or ''):
            continue
        if not verified(tool):
            tool['state'], changed = 'needs_attention', True
            continue
        ok = state(app).revalidated.get(tool['digest'])
        if ok is None:
            spec = tool['spec']
            ok, _ = verdict(validate_tool(spec['code'], spec['tests'], generate(spec['inputs']), CASE_SECONDS),
                            spec['tests'])
            state(app).revalidated[tool['digest']] = ok
            if not ok:
                tool['state'], changed = 'needs_attention', True
        if ok and tool.get('state') == 'ok':
            tools[name] = tool['spec']['code']
    if changed:
        _save(app, box)
    return tools


def search(app, query, limit=3):
    words = {w for w in re.findall(r'[a-z0-9]+', (query or '').lower()) if len(w) > 2}
    found = []
    for tool in toolbox(app)['tools']:
        spec = tool.get('spec', {})
        text = (spec.get('name', '').replace('_', ' ') + ' ' + spec.get('purpose', '')).lower()
        score = sum(1 for w in words if w in text)
        if score and tool.get('state') == 'ok':
            found.append((score, spec['name'], spec))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [{'name': 'pebble.mine.' + name, 'description': spec['purpose'], 'arguments': sorted(spec['inputs']['properties']),
             'use': 'inside RunCode'} for _, name, spec in found[:limit]]


def listing(app):
    box = toolbox(app)
    rows = [{'name': t['spec']['name'], 'purpose': t['spec']['purpose'], 'kept_by': t.get('kept_by', 'person'),
             'kept_at': t.get('kept_at'), 'uses': t.get('uses', 0), 'last_used': t.get('last_used'),
             'state': t.get('state', 'ok') if verified(t) else 'needs_attention', 'digest': t['digest'][:16]}
            for t in box['tools'] if isinstance(t.get('spec'), dict)]
    pending = [{'id': p['id'], 'name': p['record']['spec']['name'], 'purpose': p['record']['spec']['purpose'],
                'why': p['why'], 'chat_id': p.get('chat_id')} for p in box['pending']]
    now = time.time()
    removed = [{'name': r['record']['spec'].get('name'), 'purpose': r['record']['spec'].get('purpose'),
                'seconds_left': int(UNDO_SECONDS - (now - float(r['removed_at'])))} for r in box['removed']]
    stored = app.store.setting(SETTING, None)
    if isinstance(stored, dict) and len(stored.get('removed') or []) != len(box['removed']):
        _save(app, box)  # F178: a removed tool older than ten minutes is forgotten for good
    return {'tools': rows, 'pending': pending, 'removed': removed}


def detail(app, name):
    box = toolbox(app)
    for tool in box['tools'] + [p['record'] for p in box['pending']]:
        if tool['spec']['name'] == name:
            return {'name': name, 'purpose': tool['spec']['purpose'], 'code': tool['spec']['code'],
                    'tests': tool['spec']['tests'], 'inputs': tool['spec']['inputs'],
                    'validation': tool.get('validation'), 'blind': tool.get('blind'),
                    'provenance': {k: v for k, v in (tool.get('provenance') or {}).items() if k != 'chat_id'},
                    'verified': verified(tool)}
    raise PolicyError('No kept tool has that name.')


def remove(app, name):
    box = toolbox(app)
    gone = [t for t in box['tools'] if t['spec']['name'] == name]
    if not gone:
        raise PolicyError('No kept tool has that name.')
    box['tools'] = [t for t in box['tools'] if t['spec']['name'] != name]
    # Phase 45 (F178, audit round R6): removed at once, and kept for ten minutes so the person can put it back
    box['removed'] = [r for r in box['removed'] if r['record']['spec'].get('name') != name][-4:] + [
        {'record': gone[-1], 'removed_at': time.time()}]
    _save(app, box)
    return listing(app)


def undo_remove(app, name):
    """Phase 45 (F178): puts back a tool the person removed in the last ten minutes. The record is Pebble's own copy,
    re-checked against its digest; nothing from the window is stored."""
    box = toolbox(app)
    item = next((r for r in reversed(box['removed']) if r['record']['spec'].get('name') == name), None)
    if item is None:
        raise PolicyError('That tool can no longer be put back (after ten minutes it is gone for good).')
    if any(t['spec']['name'] == name for t in box['tools']):
        raise PolicyError('A tool with that name is in the Toolbox again.')
    if not verified(item['record']):
        raise PolicyError('That tool changed after it was removed; it was not put back.')
    box['removed'] = [r for r in box['removed'] if r is not item]
    box['tools'] = box['tools'][-(MAX_KEPT - 1):] + [item['record']]
    _save(app, box)
    return listing(app)


# Phase 45 (F162, audit round R5): "Please remove uxtest_c_to_f from your Toolbox" took 38 s and five steps of looking
# for a removal action no model has. A short request that names a tool (with "tool" or "Toolbox" in it) is answered by
# the host with that tool's Remove card; only the person's own click removes it.
REMOVAL = re.compile(
    r"^(?:[A-Za-z0-9_-]{1,24}:\s+)?(?:(?:hey\s+)?pebble[,:]?\s+)?(?:please\s+)?(?:(?:can|could|would) you\s+)?"
    r"(?:remove|delete|forget|drop|uninstall|discard)\s+(?:the\s+)?(?:(?:kept\s+)?tool\s+)?[`'\"\N{LEFT DOUBLE QUOTATION MARK}\N{LEFT SINGLE QUOTATION MARK}]?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]{0,39})[`'\"\N{RIGHT DOUBLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}]?(?:\s+tool)?"
    r"(?:\s+from\s+(?:your|the|my|pebble['\N{RIGHT SINGLE QUOTATION MARK}]s)\s+toolbox)?(?:,?\s+please)?\s*[.!?]?$", re.I)


def removal_request(app, text):
    """Phase 45 (F162): (name, the kept record or None, the waiting keep or None) for a short request to remove a tool;
    None for anything else (the model answers it)."""
    words = (text or '').strip()
    match = REMOVAL.match(words) if len(words) <= 160 and 'tool' in words.lower() else None
    if match is None:
        return None
    name = match.group('name')
    if name.lower() in ('it', 'this', 'that', 'them', 'tool', 'tools'):
        return None  # "remove it from your Toolbox": the model knows which tool the conversation means
    box = toolbox(app)
    kept = next((t for t in box['tools'] if t['spec']['name'] == name), None)
    asked = next((p for p in box['pending'] if p['record']['spec']['name'] == name), None)
    return name, kept, asked


def decide(app, pending_id, keep):
    """The person's answer to a keep card: keep (replacing a kept tool of the same name) or discard."""
    box = toolbox(app)
    item = next((p for p in box['pending'] if p['id'] == pending_id), None)
    if item is None:
        raise PolicyError('That request is no longer waiting.')
    box['pending'] = [p for p in box['pending'] if p['id'] != pending_id]
    if keep is True:
        if not verified(item['record']):
            raise PolicyError('That tool changed after Pebble asked; it was not kept.')
        name = item['record']['spec']['name']
        box['tools'] = [t for t in box['tools'] if t['spec']['name'] != name][-(MAX_KEPT - 1):] + [
            {**item['record'], 'kept_by': 'person', 'kept_at': time.time(), 'state': 'ok'}]
    _save(app, box)
    return listing(app)
