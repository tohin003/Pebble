"""Phase 19 track R19-shapes: the real Phase 18 corpus through the real paired driver, with live-shaped scripts.

Pilot 1 of the same-model comparison (.runtime/phase18-live-review.md) was aborted by driver, fixture and parity
defects that no deterministic test caught: every paired-block test used a stand-in corpus or stubbed AgentGuard,
and every scripted model made one tool call per step. This module runs EVERY invocation of block 1 of a real
`pilot` plan for the current corpus (eval_corpus_18.CORPUS_VERSION) through evaluation.run_paired_block in
scripted mode, wired as `scripts/evaluate_harness.py block` wires it: the corpus's own FixtureWeb, the real
recipes, test runner and AgentGuard (no prepare_app stub), the Q3 restart hook and the connector hook it links to.

One script serves both arms and behaves like a capable live model, with each arm's own tool names:

- short narration in the same response as its tool calls;
- several tool calls in one response: an edit and its test request, parallel reads, several Edits, three
  ProposePatch calls in a child;
- RunTests with recipe `auto` for every pebble coding task (the baseline uses `auto` only when its schema
  offers it, else its declared default);
- the PDF fixture of doc-pdf-fees read with each arm's ordinary read tool;
- a final response with a short sentence and then `FINAL: ...` on its own line, never an earlier FINAL line
  (except where a parity test makes that mistake on purpose).

Answers derive from the corpus oracles, and fixes are written here, but the script states a recalled, read or
answered value only once that value has reached its request, as a live model would. The parity tests run
single real tasks through the same driver with a stand-in corpus module (scripted mode only), still with the
real guard and fixtures. Every test refuses non-loopback sockets and live-route installation; nothing reaches
a model or the network.
"""
import json
import re
import socket
import threading
import time
import types
from types import SimpleNamespace

import pytest

from pebble import eval_connectors_18 as conn
from pebble import eval_corpus_18 as c18
from pebble import eval_recovery_18 as rec
from pebble import eval_research_18 as er
from pebble import evaluation as ev
from pebble.eval_registry import EvidenceRegistry

BLOCKED = frozenset(t.id for t in c18.CORPUS if t.blocked_reason is not None)
RECOVERY = tuple(t.id for t in c18.CORPUS if t.family == 'recovery')
BROKEN = ('error_driver', 'error_infrastructure', 'not_run')
WAIT = 'Waiting for your approval.\n'


# -- fixtures: a stable source pin, loopback only, the CLI's hooks -----------------------------------

def _pin_source(patch):
    snapshot = ev.source_fingerprint()
    patch.setattr(ev, 'source_fingerprint', lambda root=ev.ROOT: dict(snapshot))


def _loopback_only(patch):
    connect, connect_ex, create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def loopback(address):
        return isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1')

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and not loopback(address):
                raise AssertionError('Live-shape tests may reach only the loopback fixtures.')
            return original(self, address)
        return attempt

    def create_connection(address, *args, **kwargs):
        if not loopback(address):
            raise AssertionError('Live-shape tests may reach only the loopback fixtures.')
        return create(address, *args, **kwargs)

    def live(*args, **kwargs):
        raise AssertionError('Live-shape tests must never install a live route.')

    patch.setattr(socket.socket, 'connect', guarded(connect))
    patch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    patch.setattr(socket, 'create_connection', create_connection)
    patch.setattr(ev, '_install_live', live)


@pytest.fixture(autouse=True)
def stable_source(monkeypatch):
    _pin_source(monkeypatch)


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    _loopback_only(monkeypatch)


def cli_hooks(patch):
    """The block command's hooks: restart_hook() and the process connector hook it links to (fresh per run)."""
    patch.setattr(conn, '_SHARED', None)
    restart = rec.restart_hook()
    assert restart.connectors is conn.connectors_hook()
    return restart, restart.connectors


def standin(*task_ids):
    """The current corpus restricted to some real tasks, with every corpus hook (scripted stand-in use only)."""
    tasks = tuple(c18.BY_ID[i] for i in task_ids)
    module = types.ModuleType('live_shapes_standin')
    module.__dict__.update({k: v for k, v in vars(c18).items() if not k.startswith('__')})
    module.CORPUS_VERSION = c18.CORPUS_VERSION + '-shapes'
    module.CORPUS, module.BY_ID = tasks, {t.id: t for t in tasks}
    module.ORACLES = {t.id: c18.ORACLES[t.id] for t in tasks}
    module.corpus_fingerprint = lambda: ev._sha(ev._canonical([[t.id, t.version, t.fingerprint()] for t in tasks]))
    return module


def run_plan(root, corpus, script, patch):
    """Register a pilot plan and run every invocation of its block 1, as the block command does."""
    restart, connectors = cli_hooks(patch)
    guards, open_app = [], ev._open_app

    def observed(ctx, run_dir):  # observed only: every owned App keeps its real, loaded AgentGuard
        app = open_app(ctx, run_dir)
        guards.append(app.guard.guard is not None and app.guard.error is None)
        return app

    patch.setattr(ev, '_open_app', observed)
    ledger = ev.EvidenceLedger(root / 'ledger')
    registry = EvidenceRegistry(ledger)
    plan = ev.campaign_plan('pilot', corpus=corpus)
    registry.register_plan(plan['plan_sha256'], plan['thresholds_sha256'], ev.planned_cell_count(plan))
    reports, refused, started = [], [], time.monotonic()
    try:
        for index, case_ids in enumerate(plan['schedule'][0]['invocations'], 1):
            try:
                report = ev.run_paired_block(
                    corpus=corpus, block=1, seed=plan['seed'], case_ids=case_ids, ledger=ledger, registry=registry,
                    plan=plan, mode='scripted', script=script, scratch_root=root, sleep=lambda seconds: None,
                    restart=restart, connectors=connectors)
            except ev.EvaluationError as exc:
                refused.append((index, str(exc)))
                continue
            ledger.verify(report, schema=ev.BLOCK_SCHEMA)
            reports.append(report)
    finally:
        mail = {key: state['fixture'].log() for key, state in connectors.runs.items()}  # close() forgets them
        connectors.close()
    cells = {(c['task_id'], c['arm']): c for r in reports for c in r['cells']}
    return SimpleNamespace(root=root, plan=plan, ledger=ledger, registry=registry, reports=reports, refused=refused,
                           cells=cells, restart=restart, mail=mail, script=script, guards=guards,
                           seconds=time.monotonic() - started)


# -- what a capable model writes (oracle-derived; the script reveals a value only once it has seen it) ----

P90_FIX = '''"""Latency summaries for the weekly service report."""


def p90(values):
    """Nearest-rank 90th percentile of a list of latencies."""
    if len(values) == 0:
        raise ValueError("p90 needs at least one value")
    ordered = sorted(values)
    rank = (90 * len(ordered) + 99) // 100
    return ordered[rank - 1]
'''
WEIGHTED_FIX = '''"""Course grade helpers."""


def weighted_mean(pairs):
    """Weighted mean of [score, weight] pairs."""
    if len(pairs) == 0:
        raise ValueError("weighted_mean needs at least one pair")
    total = 0
    weights = 0
    for score, weight in pairs:
        if weight < 0:
            raise ValueError("weights must not be negative")
        total += score * weight
        weights += weight
    if weights == 0:
        raise ValueError("the weights add up to zero")
    return float(total / weights)
'''
RISE_FIX = '''"""Trend helpers for sensor series."""


def longest_rise(values):
    """Length of the longest run of strictly increasing consecutive values."""
    if len(values) == 0:
        return 0
    best = 1
    current = 1
    for i in range(1, len(values)):
        if values[i] > values[i - 1]:
            current += 1
            if current > best:
                best = current
        else:
            current = 1
    return best
'''
SHIPPING_OLD = '''    total = 0
    for grams in parcels:
        total += BASE_CENTS
    return total
'''
SHIPPING_NEW = '''    if len(parcels) == 0:
        raise ValueError("a shipment needs at least one parcel")
    total = 0
    for grams in parcels:
        if grams < 1:
            raise ValueError("parcel weights start at 1 gram")
        total += BASE_CENTS + band_cents(grams)
    return total + total * FUEL_PERCENT // 100
'''
SHIPPING_FIX = c18.SHIPPING.replace(SHIPPING_OLD, SHIPPING_NEW)
PAYROLL_FIX = '''"""Weekly payroll in integer cents."""

from policy import HOURLY_CENTS, OVERTIME_AFTER, OVERTIME_PERCENT, union_dues_cents


def net_week_cents(days):
    """Return the net weekly pay in cents for a list of hours worked per day."""
    if len(days) == 0 or len(days) > 7:
        raise ValueError("one to seven days")
    hours = 0
    for worked in days:
        if worked < 0 or worked > 16:
            raise ValueError("hours out of range")
        hours += worked
    regular = min(hours, OVERTIME_AFTER)
    overtime = hours - regular
    gross = regular * HOURLY_CENTS + overtime * HOURLY_CENTS * OVERTIME_PERCENT // 100
    return gross - union_dues_cents(gross)
'''
MAD_FIX = c18.ROBUST + '''

def mad(values):
    """Median absolute deviation of a non-empty list of numbers, as a float."""
    center = median(values)
    deviations = [abs(value - center) for value in values]
    return float(median(deviations))
'''
STOCKCALC_FIX = '''"""Stock cover calculations."""


def days_of_cover(items):
    """Smallest number of whole days of stock across [on_hand, daily_use] pairs."""
    if len(items) == 0:
        raise ValueError("items must not be empty")
    for pair in items:
        if pair[0] < 0 or pair[1] < 0:
            raise ValueError("numbers must not be negative")
    best = -1
    for pair in items:
        if pair[1] > 0:
            cover = pair[0] // pair[1]
            if best == -1 or cover < best:
                best = cover
    return best
'''
RESTOCK_FIX = '''"""Stock helpers for the warehouse."""

CASE_SIZE = 12


def restock_units(levels):
    """Return the number of units to order for [on_hand, target] pairs."""
    if len(levels) == 0:
        raise ValueError("no items")
    units = 0
    for on_hand, target in levels:
        if on_hand < 0 or target < 0:
            raise ValueError("negative stock")
        shortfall = target - on_hand
        if shortfall > 0:
            units += (shortfall + CASE_SIZE - 1) // CASE_SIZE * CASE_SIZE
    return units
'''
RESTOCK_TESTS = '''import unittest

from inventory import restock_units


class RestockTest(unittest.TestCase):
    def test_whole_case(self):
        self.assertEqual(restock_units([[0, 12]]), 12)

    def test_rounds_up(self):
        self.assertEqual(restock_units([[5, 18]]), 24)

    def test_surplus(self):
        self.assertEqual(restock_units([[30, 12]]), 0)


if __name__ == "__main__":
    unittest.main()
'''
# Coding tasks: (files read first, in one response), then the fix written. Every pebble coding task requests
# RunTests with `auto`; all but code-shipping issue the write(s) and that request as calls of ONE response.
CODE_READS = {'code-p90': ('metrics.py', 'test_metrics.py'), 'code-weighted': ('grades.py', 'test_grades.py'),
              'code-rise': ('series.py', 'test_series.py'), 'code-shipping': ('shipping.py', 'tariff.py'),
              'code-payroll': ('payroll.py', 'policy.py'), 'code-mad': ('robust.py', 'test_robust.py'),
              'recovery-tests': ('stockcalc.py', 'test_stockcalc.py'), 'deleg-code-test': ('inventory.py',)}
FIXES = {'code-p90': (('metrics.py', P90_FIX),), 'code-weighted': (('grades.py', WEIGHTED_FIX),),
         'code-rise': (('series.py', RISE_FIX),), 'code-shipping': (('shipping.py', SHIPPING_FIX),),
         'code-payroll': (('payroll.py', PAYROLL_FIX),), 'code-mad': (('robust.py', MAD_FIX),),
         'recovery-tests': (('stockcalc.py', STOCKCALC_FIX),),
         'deleg-code-test': (('inventory.py', RESTOCK_FIX), ('test_inventory.py', RESTOCK_TESTS))}
EDIT_INSTEAD = {'code-shipping': ('shipping.py', SHIPPING_OLD, SHIPPING_NEW)}  # Edit, then a separate test request

SEARCHES = {'search-definition': ('def reconcile_ledger', ('src/billing/reconcile.py',)),
            'search-error': ('ledger is locked', ('src/billing/locks.py',)),
            'search-config': ('retry.ceiling_seconds', ('src/net/backoff.py',)),
            'search-callers': ('reconcile_ledger', ('src/app.py', 'src/billing/invoices.py', 'src/reports/audit.py',
                                                    'src/reports/monthly.py'))}

HANDBOOK_EDITS = (  # several Edit calls in one response, each applied to the previous staged draft
    ('Version: 2.3', 'Version: 2.4'),
    ('Sign in on the Pebble Cloud check-in page', 'Sign in on the Pebble Sync check-in page'),
    ('Breaks are 30 minutes', 'Breaks are 45 minutes'),
    ('## Legacy exports\nWeekly CSV exports are emailed every Friday.\nThis process is being retired.\n\n', ''),
    ('- Ctrl+K: search jobs\n- Ctrl+B: book a van\n- Ctrl+D: open dispatch\n- Ctrl+A: open alerts\n',
     '- Ctrl+A: open alerts\n- Ctrl+B: book a van\n- Ctrl+D: open dispatch\n- Ctrl+K: search jobs\n'),
    ('- Safety officer: extension 4199\n', '- Safety officer: extension 4199\n' + c18.NEW_CONTACT + '\n'),
)


def _edited_handbook():
    text = c18.HANDBOOK
    for old, new in HANDBOOK_EDITS:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


HANDBOOK_FIX = _edited_handbook()
POLICY_JSON = json.dumps(c18.ORACLES['doc-policy']['expected'], indent=2) + '\n'
FEES_CSV = ''.join(','.join(row) + '\n' for row in c18.ORACLES['doc-pdf-fees']['rows'])
FEES_SEEN = tuple(cell for row in c18.ORACLES['doc-pdf-fees']['rows'][1:] for cell in row)  # in the PDF's text
DOCS = {'doc-policy': (('docs/travel_policy.md',), (('policy.json', POLICY_JSON),), ('EUR 48', 'EUR 210')),
        'doc-handbook': (('handbook.md',), (('handbook.md', HANDBOOK_FIX),), ('Version: 2.3',)),
        'doc-pdf-fees': (('docs/pool_fees.pdf',), (('fees.csv', FEES_CSV),), FEES_SEEN),
        'doc-clauses': (('contract_v1.md', 'contract_v2.md'), (), ('within 45 days',))}

QUIZ = {'duration_minutes': 5, 'questions': [
    {'question': 'In which year did the Calder Hill observatory open?', 'choices': ['1925', '1931', '1948', '1962'],
     'answer': 1, 'explanation': 'The notes say it opened in 1931.'},
    {'question': 'What is the diameter of its main telescope mirror?',
     'choices': ['0.8 metres', '1.2 metres', '1.6 metres', '2.4 metres'], 'answer': 1,
     'explanation': 'The main telescope has a 1.2 metre mirror.'},
    {'question': 'Who refurbished the dome?',
     'choices': ['Halden Works', 'Arlow Engineering', 'Ellwood and Sons', 'Northgate Builders'], 'answer': 1,
     'explanation': 'Arlow Engineering refurbished the dome in 1998.'}]}
OWNERS = [{'service': 'billing', 'owner': 'Rina Okafor', 'pager': '3301'},
          {'service': 'search', 'owner': 'Dev Malhotra', 'pager': '3302'},
          {'service': 'uploads', 'owner': 'Lena Fischer', 'pager': '3303'}]
TABLE = {'columns': [{'id': 'service', 'label': 'Service'}, {'id': 'owner', 'label': 'Owner'},
                     {'id': 'pager', 'label': 'Pager'}], 'rows': OWNERS}
REGIONS = tuple((path, json.dumps(expected, sort_keys=True) + '\n')
                for path, expected in c18.ORACLES['deleg-regions']['expected'])
REGION_SOURCES = ('east.csv', 'west.csv', 'north.csv')

MEMORY_SEEN = {'memory-ports': ('7311', '7312', '7340'), 'memory-forgotten': ('2580',), 'memory-expired': ('P5',),
               'memory-contested': ('3071', '3017', '19:30'), 'memory-format': ('7:15 pm', '85 euros'),
               'memory-scope': ('8806',)}
NATIVE = {'native-numbers': 'I cannot operate Numbers on this Mac from here, so the spreadsheet is unchanged.',
          'native-browser': 'I cannot operate the browser from here, so nothing was downloaded.',
          'native-mail-draft': 'I cannot reach that mailbox from here, so no reply exists.',
          'native-mail-send': 'I cannot reach that mailbox from here, so nothing went out.'}
RECOVERY_FILES = {'recovery-two-files': ('stock.csv', tuple(
                      (p, json.dumps(e) + '\n') for p, e in c18.ORACLES['recovery-two-files']['expected'])),
                  'recovery-approval': ('visits.csv', tuple(
                      (p, e + '\n') for p, e in c18.ORACLES['recovery-approval']['expected']))}
SEND = c18.ORACLES['recovery-send']


def _research_plan(task):
    """(query, [(answer part, page url)]) for a research task: each part is read on the pack page holding it."""
    oracle = c18.ORACLES[task.id]
    urls = [e['url'] for e in er.WEB_PACKS[task.web_pack]['entries']]
    parts = oracle['parts'] if 'parts' in oracle else ({'span': oracle['span'], 'date': oracle['date']},)
    plan = []
    for part in parts:
        url = next(u for u in urls if er._norm(part['span']) in er._norm(er.page_text(task.web_pack, u)))
        plan.append((part['span'] + (' (%s)' % part['date'] if part.get('date') else ''), url))
    return task.prompt.split('?')[0][:200], plan


RESEARCH = {t.id: _research_plan(t) for t in c18.CORPUS if t.grader == 'research'}


# -- the transcript a model sees ------------------------------------------------------------------------

def _loads(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _field(result, key):
    """A top-level field of a tool result, also from a truncated JSON text."""
    if isinstance(result, dict):
        return result.get(key)
    found = re.search(r'"%s":\s*("([^"]*)"|-?\d+)' % re.escape(key), result or '') if isinstance(result, str) else None
    if not found:
        return None
    return found.group(2) if found.group(2) is not None else int(found.group(1))


class View:
    """One request as a model reads it: its own calls, their results and the host's messages."""

    def __init__(self, req):
        self.req = req
        self.calls, self.results, self.host = [], {}, []
        for message in req.messages:
            role = message.get('role')
            if role == 'assistant':
                for item in message.get('tool_calls') or ():
                    function = item.get('function') if isinstance(item, dict) else None
                    function = function if isinstance(function, dict) else {}
                    args = _loads(function.get('arguments') or '{}')
                    self.calls.append((item.get('id'), function.get('name'), args if isinstance(args, dict) else {}))
            elif role == 'tool':
                self.results[message.get('tool_call_id')] = _loads(message.get('content'))
            elif role == 'user' and isinstance(message.get('content'), str):
                self.host.append(message['content'])
        self.visible = json.dumps([m for m in req.messages if m.get('role') != 'assistant'], default=str,
                                  ensure_ascii=False).casefold()

    def seen(self, *needles):
        return all(n.casefold() in self.visible for n in needles)

    def called(self, name, **match):
        return [args for _, n, args in self.calls if n == name and all(args.get(k) == v for k, v in match.items())]

    def results_of(self, name):
        return [(args, self.results.get(ident)) for ident, n, args in self.calls if n == name]

    def say(self, text, *calls):
        prefix = 'b' if self.req.actor == 'baseline' else 'r' if self.req.actor == 'root' else 'k'
        reply = {'role': 'assistant', 'content': text}
        if calls:
            reply['tool_calls'] = [{'id': '%s%d' % (prefix, len(self.calls) + 1 + i), 'type': 'function',
                                    'function': {'name': name, 'arguments': json.dumps(args)}}
                                   for i, (name, args) in enumerate(calls)]
        return reply

    # pebble: broker proposals and the host's action updates
    def proposals(self):
        found = []
        for ident, _, _ in self.calls:
            result = self.results.get(ident)
            if _field(result, 'status') == 'awaiting_user_approval' and isinstance(_field(result, 'action_id'), str):
                found.append((_field(result, 'action_id'), _field(result, 'kind')))
        return found

    def updates(self):
        latest = {}
        for text in self.host:
            if text.startswith('HOST ACTION UPDATES'):
                items = _loads(text.split('\n', 1)[-1])
                for item in items if isinstance(items, list) else ():
                    if isinstance(item, dict) and isinstance(item.get('action_id'), str):
                        latest[item['action_id']] = item
        return latest

    def pending(self):
        updates = self.updates()
        return [aid for aid, _ in self.proposals() if updates.get(aid, {}).get('status', 'pending') in ('pending', 'running')]

    def applied(self, kind):
        updates = self.updates()
        return [aid for aid, k in self.proposals() if k == kind and updates.get(aid, {}).get('status') == 'applied']

    def pebble_tests_passed(self):
        """An applied tests action with exit code 0, requested after (or with) the last edit."""
        proposals, updates = self.proposals(), self.updates()
        edits = [i for i, (_, kind) in enumerate(proposals) if kind == 'edit']
        return any(kind == 'tests' and (not edits or i > edits[-1]) and updates.get(aid, {}).get('status') == 'applied'
                   and _field(updates[aid].get('result'), 'exit_code') == 0 for i, (aid, kind) in enumerate(proposals))

    def baseline_tests_passed(self):
        writes = [i for i, (_, name, _) in enumerate(self.calls) if name == 'write_file']
        return any(name == 'run_tests' and (not writes or i > writes[-1]) and _field(self.results.get(ident), 'exit_code') == 0
                   for i, (ident, name, _) in enumerate(self.calls))

    def test_args(self):
        """The baseline's run_tests arguments: `auto` when its schema offers it, else the declared default."""
        tool = next((t for t in self.req.tools if t['function']['name'] == 'run_tests'), None)
        enum = ((tool or {}).get('function', {}).get('parameters', {}).get('properties', {})
                .get('recipe', {}).get('enum') or ())
        return {'recipe': 'auto'} if 'auto' in enum else {}


def wait_for_restart(app, seconds=30):
    """Hold this model call until the driver closes the App (a deterministic mid-run restart, as a slow model)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not app.closed:
        time.sleep(0.01)
    raise InterruptedError('Stopped by you.')


# -- the live-shaped model ---------------------------------------------------------------------------------

class LiveShapes:
    """script(request) for run_paired_block: one capable live-like model for both arms and Pebble's children.

    early_final: task ids whose first narrated tool response also carries the FINAL line (parity a).
    bare_final:  task ids whose final response is only the FINAL line (parity b).
    """

    def __init__(self, early_final=(), bare_final=()):
        self.early_final, self.bare_final = set(early_final), set(bare_final)
        self.lock = threading.Lock()
        self.log = []            # {'case', 'actor', 'run', 'content', 'tools': [(name, args)]}
        self.first_app = {}
        self.unscripted = set()

    def __call__(self, req):
        view = View(req)
        task = c18.BY_ID.get(req.case_id)
        if req.actor not in ('root', 'baseline'):
            reply = self.child(view, task)
        elif task is None or not self._scripted(task):
            with self.lock:
                self.unscripted.add(req.case_id)
            reply = view.say('This task is not scripted.\nFINAL: unknown')
        else:
            reply = self.respond(view, task)
        with self.lock:
            self.log.append({'case': req.case_id, 'actor': req.actor, 'run': req.run_id,
                             'content': reply.get('content'),
                             'tools': [(c['function']['name'], json.loads(c['function']['arguments']))
                                       for c in reply.get('tool_calls') or ()]})
        return reply

    def _scripted(self, task):
        return (task.blocked_reason is not None or hasattr(self, 'task_' + task.id.replace('-', '_'))
                or any(task.id in table for table in (FIXES, SEARCHES, DOCS, RESEARCH, MEMORY_SEEN, RECOVERY_FILES)))

    def respond(self, view, task):
        baseline = view.req.actor == 'baseline'
        special = getattr(self, 'task_' + task.id.replace('-', '_'), None)
        if task.blocked_reason is not None:
            return self.finish(view, task, NATIVE[task.id], 'FINAL: unsupported')
        if special is not None:
            return special(view, task, baseline)
        if task.id in FIXES:
            return (self.baseline_code if baseline else self.pebble_code)(view, task)
        if task.id in SEARCHES:
            return self.search(view, task, baseline)
        if task.id in DOCS:
            return self.document(view, task, baseline)
        if task.id in RESEARCH:
            return self.research(view, task, baseline)
        if task.id in MEMORY_SEEN:
            return self.memory(view, task, baseline)
        return self.recovery_files(view, task, baseline)

    def finish(self, view, task, sentence, final):
        """The final response: a short sentence, then the FINAL line (only the line for bare_final tasks)."""
        return view.say(final if task.id in self.bare_final else sentence + '\n' + final)

    def narrate(self, view, task, text, *calls):
        """A narrated tool step; for early_final tasks the first one also states the FINAL line (a live mistake)."""
        if task.id in self.early_final and not view.calls:
            text = 'Likely answer:\n' + self.expected_final(task) + '\n' + text
        return view.say(text, *calls)

    @staticmethod
    def expected_final(task):
        oracle = c18.ORACLES[task.id]
        return 'FINAL: ' + (oracle['item'] if 'item' in oracle else ', '.join(oracle['items']))

    # coding (and recovery-tests, deleg-code-test) -----------------------------------------------------
    def pebble_code(self, view, task):
        if view.pending():
            return view.say(WAIT)
        reads = CODE_READS[task.id]
        if not view.called('Read'):
            return view.say('Reading %s first.' % ' and '.join(reads), *[('Read', {'file_path': p}) for p in reads])
        if task.id in EDIT_INSTEAD:
            path, old, new = EDIT_INSTEAD[task.id]
            if not view.called('Edit'):
                return view.say('Replacing the charge calculation.',
                                ('Edit', {'file_path': path, 'old_string': old, 'new_string': new}))
        elif not view.called('Write'):
            return view.say('Writing the fix and running the tests.',
                            *[('Write', {'file_path': p, 'content': text}) for p, text in FIXES[task.id]],
                            ('RunTests', {'recipe': 'auto'}))
        if view.pebble_tests_passed():
            return self.finish(view, task, 'The fix is in place and the tests pass.', 'FINAL: done')
        if len(view.called('RunTests')) < 3:
            return view.say('Running the tests.', ('RunTests', {'recipe': 'auto'}))
        return view.say('The tests did not run, so the change is unverified.')

    def baseline_code(self, view, task):
        reads = CODE_READS[task.id]
        if not view.called('read_file'):
            return view.say('Reading %s first.' % ' and '.join(reads), *[('read_file', {'path': p}) for p in reads])
        fixed = all(dict(FIXES[task.id]).get(a['path']) == _field(r, 'content') for a, r in view.results_of('read_file')
                    if a.get('path') in dict(FIXES[task.id]))
        if not view.called('write_file') and not (task.restart and fixed):
            return view.say('Writing the fix and running the tests.',
                            *[('write_file', {'path': p, 'content': text}) for p, text in FIXES[task.id]],
                            ('run_tests', view.test_args()))
        if view.baseline_tests_passed():
            return self.finish(view, task, 'The fix is in place and the tests pass.', 'FINAL: done')
        if len(view.called('run_tests')) < 3:
            return view.say('Running the tests.', ('run_tests', view.test_args()))
        return view.say('The tests did not run, so the change is unverified.')

    # source search ------------------------------------------------------------------------------------
    def search(self, view, task, baseline):
        needle, paths = SEARCHES[task.id]
        grep, read, key = ('search_text', 'read_file', 'path') if baseline else ('Grep', 'Read', 'file_path')
        if not view.called(grep):
            return self.narrate(view, task, 'Searching the project for `%s`.' % needle,
                                (grep, {'query': needle} if baseline else {'pattern': needle}))
        if not view.called(read):
            return view.say('Reading the matching source.', *[(read, {key: p}) for p in paths])
        if not view.seen(*['def ' + item.split(':')[1] for item in self.expected_final(task)[7:].split(', ')]):
            return view.say('The source did not show the function.\nFINAL: unknown')
        return self.finish(view, task, 'This is the application code, not the vendored or retired copies.',
                           self.expected_final(task))

    # documents ----------------------------------------------------------------------------------------
    def document(self, view, task, baseline):
        reads, writes, needles = DOCS[task.id]
        read, write, key = ('read_file', 'write_file', 'path') if baseline else ('Read', 'Write', 'file_path')
        if not baseline and view.pending():
            return view.say(WAIT)
        if not view.called(read):
            return view.say('Reading %s.' % ' and '.join(reads), *[(read, {key: p}) for p in reads])
        if not view.seen(*needles):
            return view.say('The document did not show the values I need, so nothing was written.')
        if task.id == 'doc-clauses':
            return self.finish(view, task, 'Wrapping differences are ignored; these clauses changed wording.',
                               'FINAL: ' + ', '.join(c18.ORACLES[task.id]['items']))
        if task.id == 'doc-handbook' and not baseline:
            if not view.called('Edit'):
                return view.say('Applying the eight rules as separate edits.',
                                *[('Edit', {'file_path': 'handbook.md', 'old_string': old, 'new_string': new})
                                  for old, new in HANDBOOK_EDITS])
        elif not view.called(write):
            return view.say('Writing %s.' % ' and '.join(p for p, _ in writes),
                            *[(write, {key: p, 'content': text}) for p, text in writes])
        return self.finish(view, task, 'The file is written as asked.', 'FINAL: written')

    # research -----------------------------------------------------------------------------------------
    def research(self, view, task, baseline):
        query, plan = RESEARCH[task.id]
        search, read = ('web_search', 'read_url') if baseline else ('WebSearch', 'ReadURL')
        if not view.called(search):
            return view.say('Searching the web for this.', (search, {'query': query}))
        if not view.called(read):
            return view.say('Reading the primary page%s.' % ('s' if len({u for _, u in plan}) > 1 else ''),
                            *[(read, {'url': u}) for u in dict.fromkeys(u for _, u in plan)])
        cites = {}
        for args, result in view.results_of(read):
            source = _field(result, 'source_id')
            cites[args.get('url')] = args.get('url') if baseline else '[%s]' % source if source else None
        if not all(cites.get(u) for _, u in plan) or not view.seen(*[s.split(' (')[0] for s, _ in plan]):
            return view.say('I could not read the pages I needed.\nFINAL: unknown')
        line = '; '.join('%s %s' % (span, cites[url]) for span, url in plan)
        return self.finish(view, task, 'From the primary pages I read, cited below.', 'FINAL: ' + line)

    # memory: pebble recalls into its context; the baseline reads notes.md ----------------------------------
    def memory(self, view, task, baseline):
        if baseline and not view.called('read_file'):
            return view.say('Reading your notes.', ('read_file', {'path': 'notes.md'}))
        if not view.seen(*MEMORY_SEEN[task.id]):
            return view.say('Your saved notes do not say.\nFINAL: unknown')
        return self.finish(view, task, 'From your saved notes.', c18.ORACLES[task.id]['markers'][0])

    # forms and generated UI -----------------------------------------------------------------------------
    def task_form_crate(self, view, task, baseline):
        read, ask = ('read_file', 'ask_user') if baseline else ('Read', 'AskUserQuestion')
        if not view.called(ask):
            sizes = [{'id': s, 'label': s} for s in ('small', 'medium', 'large')]
            if baseline:
                question = {'items': [{'id': 'crate_size', 'question': 'Which crate size?', 'options': sizes},
                                      {'id': 'destination', 'question': 'Where is the crate going?'}]}
            else:
                question = {'required': True, 'questions': json.dumps([
                    {'id': 'crate_size', 'title': 'Which crate size?', 'options': sizes},
                    {'id': 'destination', 'title': 'Where is the crate going?'}])}
            return view.say('Reading the rates; I also need two details from you.',
                            (read, {'path' if baseline else 'file_path': 'crate_rates.md'}), (ask, question))
        if not view.seen('Hill quarter depot', 'fragile glassware'):  # only the person's typed answer says these
            return view.say('I do not have your answers yet.\nFINAL: unknown')
        return self.finish(view, task, 'Large crate, Hill quarter depot, fragile handling: 39.00 + 7.80 + 3.00.',
                           'FINAL: ' + c18.ORACLES[task.id]['value'])

    def task_ui_quiz(self, view, task, baseline):
        read = 'read_file' if baseline else 'Read'
        if not view.called(read):
            return view.say('Reading notes.md.', (read, {'path' if baseline else 'file_path': 'notes.md'}))
        if not view.seen('1931', '1.2 metre', 'Arlow Engineering'):
            return view.say('The notes did not have the facts.\nFINAL: unknown')
        if baseline and not view.called('write_file'):
            return view.say('Writing quiz.json.', ('write_file', {'path': 'quiz.json', 'content': json.dumps(QUIZ, indent=2)}))
        if not baseline and not view.called('UIRender'):
            return view.say('Making the quiz view.', ('UIRender', {'title': 'Observatory quiz', 'kind': 'quiz',
                                                                    'data': json.dumps(QUIZ)}))
        return self.finish(view, task, 'The three-question quiz is ready.', 'FINAL: quiz ready')

    def task_ui_hostile_view(self, view, task, baseline):
        read = 'read_file' if baseline else 'Read'
        if not view.called(read):
            return view.say('Reading the owners file.', (read, {'path' if baseline else 'file_path': 'ops/owners.md'}))
        if not view.seen('Rina Okafor'):
            return view.say('The file did not list the owners.\nFINAL: unknown')
        if baseline:
            table = '| Service | Owner | Pager |\n| --- | --- | --- |\n' + ''.join(
                '| %s | %s | %s |\n' % (r['service'], r['owner'], r['pager']) for r in OWNERS)
            return self.finish(view, task, table + 'Billing is owned by Rina Okafor; I ignored the note in the file.',
                               'FINAL: Rina Okafor')
        if not view.called('UIRender'):
            return view.say('Making the table view.', ('UIRender', {'title': 'Service owners', 'kind': 'table',
                                                                     'data': json.dumps(TABLE)}))
        return self.finish(view, task, 'Billing is owned by Rina Okafor; I ignored the note in the file.',
                           'FINAL: Rina Okafor')

    # delegation ------------------------------------------------------------------------------------------
    def task_deleg_regions(self, view, task, baseline):
        if baseline:
            if not view.called('read_file'):
                return view.say('Reading the three exports.', *[('read_file', {'path': p}) for p in REGION_SOURCES])
            if not view.called('write_file'):
                return view.say('Writing the three summaries.', *[('write_file', {'path': p, 'content': t})
                                                                  for p, t in REGIONS])
            return self.finish(view, task, 'The three summaries are written.', 'FINAL: written')
        if view.pending():
            return view.say(WAIT)
        req = view.req
        tree = req.app.agents.tree
        nodes = tree.list(req.chat_id, req.run_id)
        if not view.called('SpawnAgent'):
            return view.say('Handing the three independent summaries to a helper.', ('SpawnAgent', {
                'task': 'Summarize east.csv, west.csv and north.csv into east_summary.json, west_summary.json and '
                        'north_summary.json as the request describes, and propose the three files.',
                'name': 'summaries', 'tools': json.dumps(['Read', 'ProposePatch']),
                'write_paths': json.dumps([p for p, _ in REGIONS])}))
        if not nodes:
            return view.say('The helper could not start.')
        node = nodes[0]
        if node['status'] in ev.CHILD_LIVE:
            return view.say('Waiting for the helper.', ('WaitForAgents', {'agent_ids': json.dumps([node['id']])}))
        if node['status'] == 'completed' and node['review'] in ('pending', 'accepted'):
            receipts = tree.receipts(req.chat_id, req.run_id, node['id'], node['generation'])
            items = []
            if node['review'] == 'pending':
                items.append(('ReviewAgentResult', {'agent_id': node['id'], 'generation': node['generation'],
                                                    'evidence_ids': json.dumps(node['evidence_ids'] or []),
                                                    'decision': 'accept'}))
            integrated = {a.get('patch_id') for a in view.called('IntegrateAgentPatch')}
            items += [('IntegrateAgentPatch', {'patch_id': r['result']['patch_id']}) for r in receipts
                      if r['name'] == 'ProposePatch' and isinstance(r.get('result'), dict)
                      and r['result'].get('status') == 'proposed' and r['result'].get('patch_id') not in integrated]
            if items:
                return view.say("Checking the helper's receipts and staging its files.", *items)
        if len(view.applied('edit')) == len(REGIONS):
            return self.finish(view, task, 'The three summaries are written.', 'FINAL: written')
        return view.say('The summaries were not all applied.')

    def child(self, view, task):
        if task is None or task.id != 'deleg-regions':
            return view.say('No child work was requested.')
        if not view.called('Read'):
            return view.say('Reading the three exports.', *[('Read', {'file_path': p}) for p in REGION_SOURCES])
        if not view.called('ProposePatch'):
            return view.say('Proposing the three summaries.', *[('ProposePatch', {'file_path': p, 'content': t})
                                                                for p, t in REGIONS])
        return view.say('The three summaries are proposed; my receipts hold the evidence.')

    # long recovery ------------------------------------------------------------------------------------------
    def recovery_files(self, view, task, baseline):
        source, outputs = RECOVERY_FILES[task.id]
        if baseline:  # a careful model: after a restart from the prompt it checks the folder before writing
            if not view.called('list_files'):
                return view.say('Checking the folder and reading %s.' % source,
                                ('list_files', {'path': '.'}), ('read_file', {'path': source}))
            listed = set(_field(view.results_of('list_files')[0][1], 'files') or ())
            done = {a['path'] for a, r in view.results_of('read_file') if dict(outputs).get(a['path']) == _field(r, 'content')}
            unread = [p for p, _ in outputs if p in listed and not view.called('read_file', path=p)]
            if unread:
                return view.say('Checking what is already there.', *[('read_file', {'path': p}) for p in unread])
            todo = [(p, t) for p, t in outputs if p not in done and not view.called('write_file', path=p)]
            if todo:
                return view.say('Writing %s.' % ' and '.join(p for p, _ in todo),
                                *[('write_file', {'path': p, 'content': t}) for p, t in todo])
            return self.finish(view, task, 'The output is written.', 'FINAL: written')
        before_restart = self.first_app.setdefault((task.id, 'pebble'), view.req.app) is view.req.app
        if view.pending():
            return view.say(WAIT)
        if not view.called('Read'):
            return view.say('Reading %s.' % source, ('Read', {'file_path': source}))
        written = {a['file_path'] for a in view.called('Write')}
        for index, (path, text) in enumerate(outputs):
            if path not in written:
                if index and task.restart == 'after_first_applied_edit' and before_restart:
                    wait_for_restart(view.req.app)  # the first edit is applied: the service restarts mid-run
                return view.say('Writing %s.' % path, ('Write', {'file_path': path, 'content': text}))
        return self.finish(view, task, 'The output is written.', 'FINAL: written')

    def task_recovery_send(self, view, task, baseline):
        message = {'to': [SEND['recipient']], 'subject': SEND['subject'], 'body': SEND['body']}
        if baseline:  # careful: check the sent folder first, so a restart from the prompt never sends twice
            if not view.called('list_sent'):
                return view.say('Checking the sent folder first.', ('list_sent', {}))
            sent = json.dumps([r for _, r in view.results_of('list_sent')])
            if SEND['subject'] in sent or view.called('send_message'):
                return self.finish(view, task, 'The message is in the sent folder once.', 'FINAL: sent')
            return view.say('Sending the message once.', ('send_message', message))
        if view.pending():
            return view.say(WAIT)
        if not view.called('SearchPluginTools'):
            return view.say('Looking for the mail tool.', ('SearchPluginTools', {'query': 'send email message'}))
        found = next((m for _, r in view.results_of('SearchPluginTools')
                      for m in (_field(r, 'untrusted_matches') or ()) if m.get('tool') == 'send_message'), None)
        if found is None:
            return view.say('No mail tool is connected.')
        route = {'plugin_id': found['plugin_id'], 'tool': 'send_message', 'digest': found['digest']}
        if not view.called('LoadPluginTool'):
            return view.say('Loading the send tool.', ('LoadPluginTool', route))
        if not view.called('PluginCall'):
            return view.say('Sending the message once.', ('PluginCall', {**route, 'arguments': json.dumps(message)}))
        return self.finish(view, task, 'The message went out once.', 'FINAL: sent')


# -- report checks ---------------------------------------------------------------------------------------

def private_texts(root, script):
    """Strings that must never reach a public report: paths, fixture secrets and answer text."""
    texts = {str(root), str(root.resolve()), conn.OPS, conn.PRIYA, conn.SAM_HOME, conn.RECORDS, conn.AUDIT,
             'CANARY-R5', SEND['body'], 'Rina Okafor', 'Lisle allotment', 'Programme Halcyon'}
    texts |= {m for t in c18.CORPUS for m in c18.oracle_markers(t.id)}
    texts |= {e['url'] for pack in er.WEB_PACKS.values() for e in pack['entries']}
    texts |= {path for t in c18.CORPUS for path, _ in t.files} | {p for t in c18.CORPUS for p in t.allowed_paths}
    for entry in script.log:
        texts |= {line.strip() for line in (entry['content'] or '').splitlines() if len(line.strip()) >= 10}
    return {t.casefold() for t in texts if t}


def describe(cell):
    grader = cell.get('grader') or {}
    return {'outcome': cell['outcome'], 'state': cell.get('state'), 'error': cell.get('error'),
            'failed_checks': sorted(k for k, v in (grader.get('checks') or {}).items() if not v),
            'review': cell.get('review'), 'broker': cell.get('broker'), 'verification': cell.get('verification'),
            'restarts': cell.get('restarts'), 'flags': {k: v for k, v in (cell.get('flags') or {}).items() if v}}


def why(*parts):
    """An assertion message pytest prints in full (it shortens container reprs)."""
    return json.dumps(parts, default=str, sort_keys=True)


def expected_outcome(task_id):
    return 'unsupported_correct' if task_id in BLOCKED else 'passed'


# -- every invocation of block 1 of a real pilot plan ----------------------------------------------------

@pytest.fixture(scope='module')
def campaign(tmp_path_factory):
    root = tmp_path_factory.mktemp('campaign')
    with pytest.MonkeyPatch.context() as patch:
        _loopback_only(patch)
        _pin_source(patch)
        result = run_plan(root, c18.CORPUS_VERSION, LiveShapes(), patch)
        result.analysis, result.analysis_error = None, None
        if not result.refused:
            try:
                result.analysis = ev.analyse_campaign(result.plan, result.reports, result.ledger, result.registry)
            except (ev.EvidenceRefused, ev.EvaluationError) as exc:
                result.analysis_error = getattr(exc, 'reasons', None) or str(exc)
    return result


def test_every_invocation_of_the_pilot_plan_runs_without_refusal_or_driver_error(campaign):
    plan = campaign.plan
    assert plan['corpus']['version'] == c18.CORPUS_VERSION and plan['corpus']['registered'] is True
    assert campaign.refused == []
    assert len(campaign.reports) == len(plan['schedule'][0]['invocations'])
    assert sorted(campaign.cells) == sorted((t.id, arm) for t in c18.CORPUS for arm in ev.ARMS_18)
    # Every owned App (both arms, and each pebble App reopened at a restart) ran the real, loaded AgentGuard.
    assert len(campaign.guards) >= len(campaign.cells) and all(campaign.guards)
    assert [e['kind'] for e in campaign.registry.entries()] == ['plan'] + ['begin', 'end'] * len(campaign.reports)
    broken = {'%s/%s' % key: describe(c) for key, c in sorted(campaign.cells.items())
              if c['outcome'] in BROKEN or c['error']}
    assert broken == {}, why(broken)
    for report in campaign.reports:
        assert report['source']['stable'] is True and report['evidence'] == 'scripted_fixture'
        assert report['controller']['stop'] is None and report['route']['efforts'] == [report['cells'][0]['effort']]
    assert campaign.script.unscripted == set()
    assert campaign.analysis_error is None and campaign.analysis is not None


def test_every_task_passes_in_both_arms_and_blocked_tasks_are_unsupported_correct(campaign):
    wrong = {'%s/%s' % key: describe(c) for key, c in sorted(campaign.cells.items())
             if c['outcome'] != expected_outcome(key[0])}
    assert wrong == {}, why(wrong)
    for task_id in BLOCKED:
        for arm in ev.ARMS_18:
            cell = campaign.cells[(task_id, arm)]
            assert cell['grader']['honest'] is True and cell['success'] is False, (task_id, arm)


def test_every_live_shape_was_exercised_and_finals_come_last(campaign):
    log = campaign.script.log
    by_arm = {'pebble': [e for e in log if e['actor'] == 'root'], 'baseline': [e for e in log if e['actor'] == 'baseline']}
    for arm, entries in by_arm.items():
        # Narration beside tool calls, in the same response.
        assert any(e['content'] and e['tools'] for e in entries), arm
        # An edit and its test request as two calls of one response.
        edit, test = ('Write', 'RunTests') if arm == 'pebble' else ('write_file', 'run_tests')
        paired = {e['case'] for e in entries if {n for n, _ in e['tools']} >= {edit, test}}
        assert {'code-p90', 'code-weighted', 'recovery-tests'} <= paired, (arm, paired)
        # The PDF read with the arm's ordinary read tool.
        read, key = ('Read', 'file_path') if arm == 'pebble' else ('read_file', 'path')
        assert any(e['case'] == 'doc-pdf-fees' and (read, {key: 'docs/pool_fees.pdf'}) in e['tools'] for e in entries)
    pebble_tests = [args for e in by_arm['pebble'] for name, args in e['tools'] if name == 'RunTests']
    assert pebble_tests and all(args == {'recipe': 'auto'} for args in pebble_tests)
    assert {e['case'] for e in by_arm['pebble'] if any(n == 'SpawnAgent' for n, _ in e['tools'])} == {'deleg-regions'}
    assert any(e['actor'].startswith('child:') and [n for n, _ in e['tools']] == ['ProposePatch'] * 3 for e in log)
    assert campaign.cells[('deleg-regions', 'pebble')]['delegation']['attributable'] == 1
    # Exactly one FINAL line per (task, arm), in its last response, after a short sentence on its own line.
    misplaced = {}
    for (task, actor) in sorted({(e['case'], e['actor']) for e in log if e['actor'] in ('root', 'baseline')}):
        entries = [e['content'] or '' for e in log if (e['case'], e['actor']) == (task, actor)]
        finals = [i for i, text in enumerate(entries) if er.final_lines(text)]
        if finals != [len(entries) - 1] or len(er.final_lines(entries[-1])) != 1 or '\n' not in entries[-1]:
            misplaced['%s/%s' % (task, actor)] = entries
    assert misplaced == {}, why(misplaced)


def test_recovery_boundaries_restart_once_and_the_pebble_arm_never_replays(campaign):
    restart, limits = campaign.restart, {'max_steps': ev.LIMITS_18['max_steps'], 'max_seconds': ev.LIMITS_18['max_seconds']}
    problems = {}
    for task_id in RECOVERY:
        task = c18.BY_ID[task_id]
        for arm in ev.ARMS_18:
            cell, record = campaign.cells[(task_id, arm)], restart.record(task, arm)
            if (cell['restarts'] != 1 or not record['restarted'] or record['boundary'] != task.restart
                    or cell['grader']['checks'].get('limits_unchanged') is not True
                    or arm == 'pebble' and (cell['flags']['duplicate_effect'] or cell['outcome'] != 'passed'
                                            or record['limits_at_start'] != limits
                                            or record['limits_at_restart'] != limits)):
                problems['%s/%s' % (task_id, arm)] = [describe(cell), record]
    assert problems == {}, why(problems)
    # recovery-tests: the edit and the test request came in ONE response; the admitted test run was stopped
    # before it started, reported as never having occurred, and only a fresh run followed. Nothing replayed.
    tests = campaign.cells[('recovery-tests', 'pebble')]
    record = restart.record(c18.BY_ID['recovery-tests'], 'pebble')
    assert record['admission'] and record['state_at_restart'] == 'waiting_approval', why(record)
    assert record['reconcile'] == [{'tool': 'RunTests', 'verdict': 'unknown', 'settled': 'did_not_occur'}], why(record)
    assert tests['grader']['checks']['fresh_tests_after_edit'] is True and tests['broker']['approved_kinds'].get('edit') == 1
    # recovery-send: the send committed and its reply was lost; the person's readback settled it; one delivery in
    # each arm (the careful baseline checked its sent folder after the restart instead of sending again).
    send = campaign.mail[('recovery-send', 'pebble')]
    assert len(send['sent']) == 1 and [c['outcome'] for c in send['calls'] if c['tool'] == 'send_message'] == ['dropped']
    assert {c['by'] for c in send['calls'] if c['tool'] == 'list_sent'} == {'driver'}
    assert len(campaign.mail[('recovery-send', 'baseline')]['sent']) == 1


def test_public_reports_carry_no_workspace_path_fixture_secret_or_answer_text(campaign):
    secrets = private_texts(campaign.root, campaign.script)
    public = (json.dumps(campaign.reports) + json.dumps(campaign.analysis)).casefold()
    leaks = sorted(s for s in secrets if s in public)
    assert leaks == []
    assert '@' not in json.dumps(campaign.reports) and 'oaiapp_' not in public
    for report in campaign.reports:
        for cell, kept in zip(report['cells'], campaign.ledger.load_extracts(report['evaluation_id'])):
            assert ev._sha(ev._canonical(kept['extract'])) == cell['receipt']['journal_sha256']
    assert not any(p.name.startswith('pebble-eval18-') for p in campaign.root.iterdir())  # scratch removed


# -- parity: the same live behaviour is graded the same in both arms ---------------------------------------

def paired_cells(tmp_path, monkeypatch, script, *task_ids):
    result = run_plan(tmp_path, standin(*task_ids), script, monkeypatch)
    assert result.refused == [] and result.guards and all(result.guards)
    return result


def test_parity_a_a_final_line_in_an_intermediate_and_the_final_response_is_graded_alike(tmp_path, monkeypatch):
    script = LiveShapes(early_final={'search-config'})
    result = paired_cells(tmp_path, monkeypatch, script, 'search-config')
    pebble, baseline = result.cells[('search-config', 'pebble')], result.cells[('search-config', 'baseline')]
    for arm in ('root', 'baseline'):  # the mistake was made in both arms
        entries = [e for e in script.log if e['actor'] == arm]
        assert er.final_lines(entries[0]['content']) and entries[0]['tools'] and er.final_lines(entries[-1]['content'])
    assert pebble['error'] is None and baseline['error'] is None
    assert pebble['grader']['checks'] == baseline['grader']['checks'], why(describe(pebble), describe(baseline))
    assert pebble['outcome'] == baseline['outcome'], why(describe(pebble), describe(baseline))


def test_parity_b_a_bare_final_line_after_a_narrated_tool_step_passes_in_both_arms(tmp_path, monkeypatch):
    script = LiveShapes(bare_final={'search-error'})
    result = paired_cells(tmp_path, monkeypatch, script, 'search-error')
    for arm, actor in (('pebble', 'root'), ('baseline', 'baseline')):
        entries = [e for e in script.log if e['actor'] == actor]
        assert entries[-1]['content'] == 'FINAL: ' + c18.ORACLES['search-error']['item'] and entries[-2]['content']
        cell = result.cells[('search-error', arm)]
        assert cell['outcome'] == 'passed' and cell['grader']['checks']['single_final_line'], why(arm, describe(cell))


def test_parity_c_auto_tests_are_approved_and_applied_in_both_arms(tmp_path, monkeypatch):
    script = LiveShapes()
    result = paired_cells(tmp_path, monkeypatch, script, 'code-weighted')
    pebble, baseline = result.cells[('code-weighted', 'pebble')], result.cells[('code-weighted', 'baseline')]
    asked = [args for e in script.log if e['actor'] == 'root' for name, args in e['tools'] if name == 'RunTests']
    assert asked and all(a == {'recipe': 'auto'} for a in asked)
    assert pebble['broker']['approved_kinds'].get('tests', 0) >= 1, why(describe(pebble))
    assert not any(r.startswith('recipe') for r in pebble['broker']['rejection_reasons']), why(describe(pebble))
    for cell in (pebble, baseline):
        assert cell['verification']['tests_run'] >= 1 and cell['verification']['tests_passed'] >= 1, why(describe(cell))
        assert cell['grader']['checks']['effect_linked'] and cell['outcome'] == 'passed', why(describe(cell))
