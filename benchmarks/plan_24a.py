"""Benchmark plans for corpus 24a: digests, registration, the person's go-ahead stamp, results and analysis.

Three plan kinds (docs/PEBBLE_V1_INSPECTION.md §7.2-§7.3; Phase 27 adds ``claude``):
- ``harness``: Pebble against the baseline harness on every 24a task (the claim set; thresholds T1-T11 unchanged);
- ``codex``: Pebble against Codex CLI on the compared families, with delegation descriptive (a ChatGPT model:
  gpt-6-luna by default, or gpt-6-asta / gpt-6.1-sol for the small GPT sample, D20);
- ``claude``: Pebble against Claude Code CLI on the same families and the same Claude model (D20/D21). Pebble uses the
  person's Anthropic key through its evaluation provider profile and Claude Code the person's existing Claude
  subscription login (D25), isolated per process; the plan carries the budget (D20: USD 100, the run stops at the plan's stop line) and the
  per-million-token prices it is metered at, with their source.

A plan is a dry run: building it reads no profile, ledger or credential and sends nothing. ``plan_sha256`` covers the
corpus (prompts, paraphrases, follow-ups, fixtures, oracles, grader source), the seed, products, repetitions, limits,
fairness rules, statistics, metrics and thresholds. The schedule and paraphrase assignment are derived from the seed
and re-derived on every check, so an edited plan is refused, and registration refuses a corpus that changed since
the plan was made. Live cells also need the person's go-ahead stamp (D13), bound to the registered plan.

Results are content-free and HMAC-signed by the private ledger; every run is begun and ended in the hash-chained
registry before and after its first cell, and the analysis counts every planned cell once (intention to treat).
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections import Counter
from datetime import datetime, timezone

from pebble import eval_stats as st
from pebble.eval_registry import EvidenceRegistry, RegistryRefused
from pebble.evaluation import THRESHOLDS_18, EvidenceLedger, EvidenceRefused, source_fingerprint

from . import corpus_24a as corpus
from .corpus_24a.graders import FLAGS, GRADED_FLAGS, TAXONOMY
from .corpus_24a.tasks import COMPARED, DESCRIPTIVE, PHRASINGS

PLAN_SCHEMA = 'pebble.bench_plan/1'
RESULTS_SCHEMA = 'pebble.bench_results/1'
ANALYSIS_SCHEMA = 'pebble.bench_analysis/1'
STAMP_SCHEMA = 'pebble.bench_go_ahead/1'
KINDS = {'harness': ('pebble', 'baseline'), 'codex': ('pebble', 'codex'), 'claude': ('pebble', 'claude_code')}
# The registry's two arm slots (eval_registry.ARMS_18): in a codex plan the second slot stands for Codex CLI.
REGISTRY_ARMS = {'pebble': 'pebble', 'baseline': 'baseline', 'codex': 'baseline', 'claude_code': 'baseline'}
# The models each live kind may name (D20): the ChatGPT models on the person's Plus plan, the Claude models on the
# person's Anthropic key and Claude subscription.
MODELS = {'codex': ('gpt-6-luna', 'gpt-6-asta', 'gpt-6.1-sol'),
          'claude': ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5')}
BUDGET_CAP_USD = 100  # D20: the person's hard budget for Anthropic API spend across every claude plan
CLAIM_SETS = {'pilot': 1, 'claim': 3}
PLAN_DERIVED = ('plan_sha256', 'schedule')
LIMITS = {'cell_wall_seconds': 900, 'turn_max_steps': 24, 'turn_max_seconds': 300}
STATISTICS = {'pairing': 'task x repetition x paraphrase (both products get the same phrasing in a pair)',
              'interval': 'task-cluster percentile bootstrap, 95%', 'bootstrap_resamples': 10_000,
              'paired_binary': 'exact McNemar on run-level discordant pairs', 'sign_flips': 100_000, 'alpha': 0.05,
              'counting': 'intention to treat: every planned cell once; only passed is success',
              'retention': 'one attempt per cell; crashes count; a repeat never replaces a first attempt',
              'interim_looks': 0}
METRICS = {'R1': 'paraphrase robustness: the worst paraphrase success is within 0.05 of the canonical success',
           'R2': 'multi-turn success: success on tasks with fixed follow-up turns',
           'R3': 'requests per run and wall seconds per cell (median, p95)',
           'R4': 'cached input share (published next to T9 as uncached input tokens)'}
CODEX_CLAIMS = {'rule': 'Pebble is better than Codex CLI on a scope only where the pre-registered 95% lower bound of '
                        'the paired success difference (task-cluster bootstrap) is above 0',
                'scopes': ['overall'] + [f for f, p in corpus.PRODUCTS.items() if p == COMPARED],
                'descriptive': [f for f, p in corpus.PRODUCTS.items() if p == DESCRIPTIVE],
                'not_claimed': ['other products or models', 'live web search', 'UI quality', 'monetary cost',
                                'any task outside 24a', 'any security property']}
FAIRNESS = (
    'Same model and sign-in: gpt-6-luna, medium effort, on the person\'s ChatGPT account. Pebble uses its separate '
    'evaluation connection (V1-015); Codex CLI uses its own sign-in in a scratch CODEX_HOME.',
    'Same autonomy: Pebble in Auto mode in the project with a driver that approves only the case\'s declared '
    'in-scope proposals; Codex non-interactive with an approval policy that never escalates. Approvals and '
    'escalations count as review burden (T7) for both.',
    'Same budget: 15 minutes of wall time per cell; each product keeps only its own automatic retries; follow-ups are '
    'identical fixed text.',
    'Same information: identical throwaway task folders under /private/tmp/pebble-bench-<plan>/ (no AGENTS.md, '
    'CLAUDE.md, GEMINI.md, PEBBLE.md or prior memory); research pages are files; no live web.',
    'Same grading: graders see a normalised output (the workspace and the replies), never the product name.',
    'Ordering: a seeded, interleaved, randomised schedule that alternates products per task; network status logged.',
    'Retention: one attempt per cell; crashes count; repeats never replace a first attempt.',
    'Tokens and time are compared only where both products report them.',
    'Synthetic data only; the person\'s approval (D13) is recorded as a go-ahead stamp for each plan.',
)


CLAUDE_CLAIMS = {'rule': 'Pebble is better than Claude Code CLI on a scope only where the pre-registered 95% lower bound '
                         'of the paired success difference (task-cluster bootstrap) is above 0',
                 'scopes': CODEX_CLAIMS['scopes'], 'descriptive': CODEX_CLAIMS['descriptive'],
                 'not_claimed': ['other products or models', 'live web search', 'UI quality',
                                 'monetary cost (reported, never claimed)', 'any task outside 24a',
                                 'any security property']}


def fairness(kind, model=None):
    """The fairness rules a plan states. ``harness`` and ``codex`` on gpt-6-luna keep the 24a text unchanged."""
    if kind != 'claude':
        if model in (None, 'gpt-6-luna'):
            return list(FAIRNESS)
        return [FAIRNESS[0].replace('gpt-6-luna', model)] + list(FAIRNESS[1:])
    return [
        'Same model: %s at medium effort for both. Pebble uses the person\'s Anthropic API key through its evaluation '
        'provider profile (its own Keychain record, never the installed app\'s); Claude Code CLI uses the person\'s '
        'existing Claude subscription login (D25) with its sub-agents pinned to the same model. Every Claude Code '
        'reply\'s model is recorded; a reply from any other model makes the cell model_drift.' % model,
        'Same autonomy: Pebble in Auto mode in the project with a driver that approves only the case\'s declared '
        'in-scope proposals; Claude Code non-interactive and never asking (bypassPermissions) under a Seatbelt profile '
        'that confines its writes to the cell. Approvals count as review burden (T7).',
        'Same budget: 15 minutes of wall time per cell; each product keeps only its own automatic retries; follow-ups are '
        'identical fixed text.',
        'Same information: identical throwaway task folders under /private/tmp/pebble-bench-<plan>/ (no AGENTS.md, '
        'CLAUDE.md, GEMINI.md, PEBBLE.md or prior memory); for Claude Code only the project settings source loads '
        '(none exists), with no MCP server and a scratch HOME, so none of the person\'s CLAUDE.md, hooks, plugins, '
        'skills or MCP servers loads, and every turn\'s init event is checked; web tools are off for both; research '
        'pages are files.',
        'Same grading: graders see a normalised output (the workspace and the replies), never the product name.',
        'Ordering: a seeded, interleaved, randomised schedule that alternates products per task; network status logged.',
        'Retention: one attempt per cell; crashes count; repeats never replace a first attempt.',
        'Tokens are compared in the same terms (input counts cache reads and writes; cached is the cache-read part); '
        'cost is reported at the plan\'s prices for both, never claimed.',
        'Budget: Pebble\'s API spend is metered at the plan\'s prices and the run stops before the plan\'s stop line; '
        'cells not run because of it count as not_run.',
        'Synthetic data only; the person\'s approval (D13) is recorded as a go-ahead stamp for each plan.',
    ]


def check_budget(budget):
    """A claude plan's budget: the cap (at most D20's USD 100), the stop line below it, and the prices it is metered at
    (USD per million tokens for input, cache reads and output), with where and when they were read."""
    if not isinstance(budget, dict):
        raise PlanRefused('budget_required')
    cap, stop = budget.get('usd_cap'), budget.get('stop_at_usd')
    prices = budget.get('prices_per_mtok')
    if not (isinstance(cap, (int, float)) and 0 < cap <= BUDGET_CAP_USD):
        raise PlanRefused('budget_cap_invalid')
    if not (isinstance(stop, (int, float)) and 0 < stop < cap):
        raise PlanRefused('budget_stop_invalid')
    if not (isinstance(prices, dict) and set(prices) == {'input', 'cache_read', 'output'}
            and all(isinstance(v, (int, float)) and 0 < v < 1000 for v in prices.values())):
        raise PlanRefused('budget_prices_invalid')
    if not (isinstance(budget.get('prices_source'), str) and budget['prices_source'].startswith('https://')
            and isinstance(budget.get('prices_retrieved'), str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',
                                                                                budget['prices_retrieved'])):
        raise PlanRefused('budget_source_invalid')
    return {'usd_cap': cap, 'stop_at_usd': stop, 'prices_per_mtok': dict(prices),
            'prices_source': budget['prices_source'], 'prices_retrieved': budget['prices_retrieved']}


def cell_cost_usd(usage, prices):
    """A cell's cost at the plan's prices: uncached input, cache reads and output; None when usage is unknown."""
    if not isinstance(usage, dict):
        return None
    total, cached, output = usage.get('input_tokens'), usage.get('cached_tokens'), usage.get('output_tokens')
    if type(total) is not int or type(output) is not int:
        return None
    cached = cached if type(cached) is int and 0 <= cached <= total else 0
    return round(((total - cached) * prices['input'] + cached * prices['cache_read'] + output * prices['output'])
                 / 1_000_000, 6)


class PlanRefused(ValueError):
    def __init__(self, reasons):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__(', '.join(self.reasons))


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()


def _canonical(value):
    return corpus.canonical(value)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _draw(seed, *parts):
    return int(_sha('%d:%s' % (seed, ':'.join(str(p) for p in parts))), 16)


def bench_source_fingerprint():
    """The benchmark code itself (drivers, graders, plans and the bench scripts), which source_fingerprint() does
    not cover: a plan pins it, and live cells refuse when it changed."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    digest, count = hashlib.sha256(), 0
    paths = sorted((root / 'benchmarks').rglob('*.py')) + sorted((root / 'scripts').glob('bench_*.py'))
    for path in paths:
        if '__pycache__' in path.parts or not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
        count += 1
    return {'sha256': digest.hexdigest(), 'files': count}


def plan_tasks(kind):
    if kind == 'harness':
        return list(corpus.TASKS)
    return [t for t in corpus.TASKS if t.products in (COMPARED, DESCRIPTIVE)]


def thresholds(kind):
    return THRESHOLDS_18 if kind == 'harness' else (CLAUDE_CLAIMS if kind == 'claude' else CODEX_CLAIMS)


def baseline_definition():
    """The baseline arm's published 24a definition, pinned by digest in every harness plan."""
    from pebble import eval_baseline as eb
    from .drivers import baseline_cell as bc
    return {'system_prompt_sha256': _sha(eb.SYSTEM_PROMPT),
            'tools_sha256': _sha(_canonical([*eb.TOOLS, *eb.TEST_TOOLS])),
            'transcript_sha256': _sha(bc.TRANSCRIPT), 'notes_note_sha256': _sha(bc.NOTES_NOTE),
            'view_note_sha256': _sha(_canonical(bc.VIEW_NOTE)),
            'rules': ['one baseline run per human turn; follow-ups carry a plain transcript',
                      'memory is a notes.md file it may read and write',
                      'generated views are written as view.json or view.html',
                      'a reply without a FINAL line ends the turn (format_only applies to every product)']}


# -- the plan ----------------------------------------------------------------------------------------------------------

def phrasing_assignment(seed, task_ids, repetitions):
    """{(task, rep): phrasing}, stratified: tasks are dealt the three phrasings in a seeded order, and each repetition
    shifts every task to its next phrasing, so with three repetitions every task runs every phrasing exactly once."""
    order = sorted(task_ids, key=lambda task: _draw(seed, 'phrase', task))
    offset = {task: index % len(PHRASINGS) for index, task in enumerate(order)}
    return {(task, rep): PHRASINGS[(offset[task] + rep - 1) % len(PHRASINGS)]
            for task in task_ids for rep in range(1, repetitions + 1)}


def schedule(body):
    """Interleaved, randomised cells: (task, repetition) units in a seeded shuffle; within each unit both products back
    to back, the first product alternating across repetitions from a seeded start (ABBA)."""
    seed, products = body['seed'], body['products']
    tasks = [t['id'] for t in body['corpus']['tasks']]
    phrasing = phrasing_assignment(seed, tasks, body['repetitions'])
    units = [(task, rep) for rep in range(1, body['repetitions'] + 1) for task in tasks]
    for index in range(len(units) - 1, 0, -1):
        other = _draw(seed, 'order', index) % (index + 1)
        units[index], units[other] = units[other], units[index]
    cells, position = [], 0
    for task, rep in units:
        first = (_draw(seed, 'first', task) + rep) % 2
        for product in (products[first], products[1 - first]):
            position += 1
            cells.append({'position': position, 'task': task, 'rep': rep, 'phrasing': phrasing[(task, rep)],
                          'product': product})
    return cells


def _derive(body):
    return {'plan_sha256': _sha(_canonical(body)), 'schedule': schedule(body)}


def make_plan(kind='harness', claim_set='pilot', *, seed=None, candidate_sha256=None, repetitions=None, model=None,
              budget=None):
    if kind not in KINDS:
        raise PlanRefused('plan_kind_unknown')
    if kind == 'harness' and model not in (None, 'gpt-6-luna'):
        raise PlanRefused('model_not_for_kind')
    if kind in MODELS:
        model = MODELS[kind][0] if model is None and kind == 'codex' else model
        if model not in MODELS[kind]:
            raise PlanRefused('model_not_for_kind')
    if kind != 'claude' and budget is not None:
        raise PlanRefused('budget_not_for_kind')
    budget = check_budget(budget) if kind == 'claude' else None
    if claim_set not in CLAIM_SETS:
        raise PlanRefused('claim_set_unknown')
    repetitions = CLAIM_SETS[claim_set] if repetitions is None else repetitions
    if type(repetitions) is not int or not 1 <= repetitions <= 5:
        raise PlanRefused('repetitions_invalid')
    seed = secrets.randbits(63) if seed is None else seed
    if type(seed) is not int or not 0 <= seed < 2 ** 63:
        raise PlanRefused('seed_invalid')
    if candidate_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', str(candidate_sha256)):
        raise PlanRefused('candidate_sha256_invalid')
    tasks = plan_tasks(kind)
    source = source_fingerprint()
    bar = thresholds(kind)
    if kind == 'claude':
        route = {'kind': 'anthropic', 'model': model, 'effort': 'medium',
                 'pebble': 'the evaluation provider profile (scripts/bench_claude.py eval-provider): the person\'s '
                           'Anthropic key in its own Keychain record',
                 'claude_code': 'the person\'s existing Claude subscription login (D25), isolated per process'}
    else:
        route = {'kind': 'chatgpt', 'model': model or 'gpt-6-luna', 'effort': 'medium',
                 'pebble': 'the separate evaluation connection (scripts/evaluate_harness.py eval-connection)',
                 'codex': 'its own ChatGPT sign-in in a scratch CODEX_HOME' if kind == 'codex' else None}
    body = {
        'schema': PLAN_SCHEMA, 'kind': kind, 'claim_set': claim_set, 'performs_requests': False,
        'corpus': {'version': corpus.CORPUS_VERSION, 'fingerprint': corpus.corpus_fingerprint(), **corpus.digests(),
                   'tasks': [{'id': t.id, 'family': t.family, 'products': t.products, 'turns': 1 + len(t.turns),
                              'blocked': t.blocked, 'fingerprint': corpus.task_fingerprint(t)} for t in tasks]},
        'products': list(KINDS[kind]), 'repetitions': repetitions, 'seed': seed,
        'source': {'sha256': source['sha256'], 'files': source['files']},
        'bench_source': bench_source_fingerprint(),
        'candidate': {'backend_sha256': candidate_sha256,
                      'rule': 'Pebble runs the frozen 1.0 candidate backend, SHA-256-equal to the installed one'},
        'route': route,
        'limits': dict(LIMITS), 'fairness': fairness(kind, model), 'statistics': dict(STATISTICS), 'metrics': dict(METRICS),
        'thresholds': bar, 'thresholds_sha256': _sha(_canonical(bar)),
        'registry_arms': {p: REGISTRY_ARMS[p] for p in KINDS[kind]},
        'baseline': baseline_definition() if kind == 'harness' else None,
        'workspaces': '/private/tmp/pebble-bench-<plan_sha256[:12]>/<product>/<task>-<rep>/',
        'totals': {'tasks': len(tasks), 'cells': len(tasks) * repetitions * 2,
                   'runs_per_product': len(tasks) * repetitions,
                   'turns_per_product': sum(1 + len(t.turns) for t in tasks) * repetitions},
        **({'budget': budget} if budget else {}),
        'authorization': {'registration': 'register this plan in the ledger registry before any live call',
                          'go_ahead': 'the person\'s go-ahead (D13) recorded as a stamp bound to this plan',
                          'flag': '--confirm-live'},
    }
    return {**body, **_derive(body)}


def verify_plan(plan):
    """plan_sha256 after re-deriving it and the schedule; refuses an edited plan."""
    if not isinstance(plan, dict) or plan.get('schema') != PLAN_SCHEMA:
        raise PlanRefused('plan_invalid')
    body = {k: v for k, v in plan.items() if k not in PLAN_DERIVED}
    try:
        derived = _derive(body)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise PlanRefused('plan_invalid') from None
    if any(plan.get(k) != derived[k] for k in PLAN_DERIVED):
        raise PlanRefused('plan_tampered')
    if body.get('thresholds_sha256') != _sha(_canonical(body.get('thresholds'))):
        raise PlanRefused('plan_tampered')
    return derived['plan_sha256']


def corpus_unchanged(plan):
    """Refuses when the corpus on disk differs from the one the plan pinned (any prompt, paraphrase, follow-up,
    fixture, oracle or grader)."""
    current = {'fingerprint': corpus.corpus_fingerprint(), **corpus.digests()}
    changed = sorted(k for k, v in current.items() if plan['corpus'].get(k) != v)
    if changed or plan['corpus']['version'] != corpus.CORPUS_VERSION:
        raise PlanRefused(['corpus_changed'] + ['changed:' + k for k in changed])
    tasks = {t.id: t for t in corpus.TASKS}
    for item in plan['corpus']['tasks']:
        if item['id'] not in tasks or corpus.task_fingerprint(tasks[item['id']]) != item['fingerprint']:
            raise PlanRefused(['corpus_changed', 'task:' + str(item['id'])])
    return True


def planned_cells(plan):
    return len(plan['schedule'])


def register(plan, ledger):
    """Pre-register a verified plan on an unchanged corpus. Returns the registry entry and head (to witness)."""
    plan_sha256 = verify_plan(plan)
    corpus_unchanged(plan)
    registry = EvidenceRegistry(ledger)
    try:
        entry = registry.register_plan(plan_sha256, plan['thresholds_sha256'], planned_cells(plan))
    except RegistryRefused as exc:
        raise PlanRefused(exc.reasons) from None
    return {'registered': entry['kind'], 'plan_sha256': plan_sha256, 'seq': entry['seq'],
            'entry_sha256': entry['hash'], 'planned_cells': entry['planned_cells'], 'witness': registry.head()}


def _registered(plan_sha256, ledger):
    try:
        registry = EvidenceRegistry(ledger)
        registry.verify_chain()
        entries = registry.entries()
    except RegistryRefused as exc:
        raise PlanRefused(exc.reasons) from None
    found = [e for e in entries if e['kind'] == 'plan' and e['plan_sha256'] == plan_sha256]
    if not found:
        raise PlanRefused('plan_not_registered')
    return found[0], entries


# -- the person's go-ahead (D13) ------------------------------------------------------------------------------------------

def _stamp_path(ledger, plan_sha256):
    folder = ledger.root / 'go-ahead'
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder / (plan_sha256 + '.json')


def stamp(plan, ledger, *, decision, statement):
    """Record the person's go-ahead for one registered plan (written by the parent after asking the person).

    The statement is kept only as a digest; the stamp binds the plan, its registry entry and the head at stamping."""
    plan_sha256 = verify_plan(plan)
    corpus_unchanged(plan)
    return stamp_registered(plan_sha256, ledger, decision=decision, statement=statement)


def stamp_registered(plan_sha256, ledger, *, decision, statement):
    """The stamp for any registered plan digest (24a plans and the ablation's plan alike)."""
    if not isinstance(decision, str) or not re.fullmatch(r'D\d{1,3}', decision):
        raise PlanRefused('decision_invalid')
    if not isinstance(statement, str) or not 8 <= len(statement.strip()) <= 2000:
        raise PlanRefused('statement_invalid')
    entry, entries = _registered(plan_sha256, ledger)
    body = {'schema': STAMP_SCHEMA, 'plan_sha256': plan_sha256, 'plan_entry_sha256': entry['hash'],
            'registry_entries': len(entries), 'decision': decision, 'statement_sha256': _sha(statement.strip()),
            'stamped_at': _now()}
    record = {**body, 'mac': ledger.fingerprint('go-ahead:' + _canonical(body))}
    try:
        fd = os.open(_stamp_path(ledger, plan_sha256), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        raise PlanRefused('go_ahead_already_stamped') from None
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(_canonical(record) + '\n')
    return {'stamped': plan_sha256, 'decision': decision}


def require_go_ahead(plan, ledger):
    """Refuse a live cell unless the plan is verified, unchanged, registered and stamped by the person's go-ahead."""
    if not isinstance(ledger, EvidenceLedger):
        raise PlanRefused('ledger_invalid')
    plan_sha256 = verify_plan(plan)
    corpus_unchanged(plan)
    if (plan.get('bench_source') or {}).get('sha256') != bench_source_fingerprint()['sha256']:
        raise PlanRefused('bench_source_drift')
    return go_ahead_for(plan_sha256, ledger)


def go_ahead_for(plan_sha256, ledger):
    """The verified go-ahead stamp of a registered plan digest, or a refusal."""
    if not isinstance(ledger, EvidenceLedger):
        raise PlanRefused('ledger_invalid')
    entry, entries = _registered(plan_sha256, ledger)
    path = ledger.root / 'go-ahead' / (plan_sha256 + '.json')
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise PlanRefused('go_ahead_missing') from None
    except OSError:
        raise PlanRefused('go_ahead_invalid') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise PlanRefused('go_ahead_invalid')
        raw = os.read(fd, 8193)
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode('utf-8'))
        body = {k: v for k, v in record.items() if k != 'mac'}
        valid = (record.get('schema') == STAMP_SCHEMA and record.get('plan_sha256') == plan_sha256
                 and record.get('plan_entry_sha256') == entry['hash']
                 and type(record.get('registry_entries')) is int and entry['seq'] < record['registry_entries'] <= len(entries)
                 and hmac.compare_digest(str(record.get('mac')), ledger.fingerprint('go-ahead:' + _canonical(body))))
    except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise PlanRefused('go_ahead_invalid')
    return {'plan_sha256': plan_sha256, 'decision': record['decision'], 'stamped_at': record['stamped_at']}


# -- results ---------------------------------------------------------------------------------------------------------------

CELL_KEYS = ('task', 'rep', 'phrasing', 'product', 'position', 'outcome', 'success', 'state', 'checks', 'flags',
             'review', 'requests', 'wall_seconds', 'first_event_seconds', 'usage', 'turns', 'restarts', 'error')


def result_cell(planned, task, out, verdict, outcome, *, review=None, requests=None, wall=None, first=None,
                usage=None, error=None, restarts=None):
    """One content-free result cell: names, booleans, counts and timings; never a reply, file body or value."""
    from .corpus_24a.graders import public_verdict
    public = public_verdict(verdict) or {'checks': {}, 'flags': {name: 0 for name in FLAGS}}
    return {'task': task.id, 'rep': planned['rep'], 'phrasing': planned['phrasing'], 'product': planned['product'],
            'position': planned['position'], 'outcome': outcome, 'success': outcome == 'passed',
            'state': out.state if out is not None else 'not_run', 'checks': public['checks'], 'flags': public['flags'],
            'review': {k: int((review or {}).get(k, 0)) for k in ('approvals', 'rejections', 'answers', 'escalations')},
            'requests': requests, 'wall_seconds': wall, 'first_event_seconds': first,
            'usage': ({k: usage.get(k) for k in ('input_tokens', 'cached_tokens', 'output_tokens')}
                      if isinstance(usage, dict) else None),
            'turns': sum(isinstance(r, str) for r in (out.replies if out is not None else [])),
            # A recovery drill whose restart never happened is visible here (the hook watches between polls).
            'restarts': restarts if type(restarts) is int else None,
            'error': error if error is None or re.fullmatch(r'[a-z0-9_:.-]{1,80}', str(error)) else 'redacted'}


def validate_results(report):
    """Structural check of a results report (benchmarks/schema/results.schema.json, without a schema library)."""
    problems = []
    if not isinstance(report, dict) or report.get('schema') != RESULTS_SCHEMA:
        return ['schema']
    for key, kind in (('evaluation_id', str), ('plan_sha256', str), ('product', str), ('evidence', str),
                      ('cells', list), ('versions', dict), ('started_at', str), ('finished_at', str)):
        if not isinstance(report.get(key), kind):
            problems.append(key)
    for cell in report.get('cells') or ():
        if not isinstance(cell, dict) or set(cell) != set(CELL_KEYS):
            problems.append('cell_keys')
            break
        if cell['outcome'] not in TAXONOMY or cell['phrasing'] not in PHRASINGS or type(cell['success']) is not bool:
            problems.append('cell_values')
            break
        if set(cell['flags']) != set(FLAGS) or not all(type(v) is bool for v in cell['checks'].values()):
            problems.append('cell_checks')
            break
    text = json.dumps(report)
    if re.search(r'FINAL\s*:', text) or '/Users/' in text:
        problems.append('content')
    return problems


def sign_results(ledger, plan, product, cells, *, evaluation_id, evidence, versions, started_at, network=None):
    body = {'schema': RESULTS_SCHEMA, 'evaluation_id': evaluation_id, 'plan_sha256': plan['plan_sha256'],
            'product': product, 'evidence': evidence, 'versions': dict(versions), 'cells': cells,
            'network': network or {}, 'started_at': started_at, 'finished_at': _now()}
    problems = validate_results(body)
    if problems:
        raise PlanRefused(['results_invalid'] + problems)
    return ledger.sign(body)


def begin(ledger, plan, cells, evaluation_id):
    """Register a run's planned cells before its first cell; cells are schedule entries of one product."""
    registry = EvidenceRegistry(ledger)
    rows = [[c['task'], REGISTRY_ARMS[c['product']], c['rep']] for c in cells]
    try:
        return registry.begin(evaluation_id, plan['plan_sha256'], rows)
    except RegistryRefused as exc:
        raise PlanRefused(exc.reasons) from None


def end(ledger, report):
    try:
        return EvidenceRegistry(ledger).end(report['evaluation_id'], _sha(_canonical(report)))
    except RegistryRefused as exc:
        raise PlanRefused(exc.reasons) from None


# -- analysis ---------------------------------------------------------------------------------------------------------------

def _median(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _rate(rows):
    return sum(r['success'] for r in rows) / len(rows) if rows else None


def _product_summary(rows, seed, resamples):
    outcomes = Counter(r['outcome'] for r in rows)
    successes = sum(r['success'] for r in rows)
    interval = st.cluster_bootstrap(rows, _rate, cluster='task', resamples=resamples, seed=seed)
    known = [r for r in rows if isinstance(r.get('usage'), dict) and isinstance(r['usage'].get('input_tokens'), int)]
    cached = [r for r in known if isinstance(r['usage'].get('cached_tokens'), int)]
    blocked = [r for r in rows if r['blocked']]
    canonical = _rate([r for r in rows if r['phrasing'] == 'canonical'])
    paraphrases = {p: _rate([r for r in rows if r['phrasing'] == p]) for p in PHRASINGS[1:]}
    worst = min((v for v in paraphrases.values() if v is not None), default=None)
    multi = [r for r in rows if r['multi_turn']]
    return {
        'cells': len(rows), 'passed': successes, 'success': _rate(rows), 'success_wilson95': st.wilson(successes, len(rows)),
        'success_cluster95': [interval['low'], interval['high']],
        'outcomes': dict(sorted(outcomes.items())), 'false_completions': outcomes['false_completion'],
        'format_only': outcomes['format_only'],
        'flags': {name: sum(r['flags'].get(name, 0) for r in rows) for name in FLAGS},
        'review_burden_mean': (sum(sum(r['review'].values()) for r in rows) / len(rows)) if rows else None,
        'honesty_on_blocked': (sum(r['outcome'] == 'unsupported_correct' for r in blocked) / len(blocked)) if blocked else None,
        'R1': {'canonical': canonical, **paraphrases, 'worst_paraphrase': worst,
               'within_0_05': None if canonical is None or worst is None else worst >= canonical - 0.05},
        'R2': {'multi_turn_cells': len(multi), 'success': _rate(multi)},
        'R3': {'requests_median': _median(r['requests'] for r in rows),
               'requests_p95': st.nearest_rank([r['requests'] for r in rows if isinstance(r['requests'], int)], 0.95),
               'wall_median': _median(r['wall_seconds'] for r in rows),
               'wall_p95': st.nearest_rank([r['wall_seconds'] for r in rows if isinstance(r['wall_seconds'], (int, float))], 0.95),
               'first_event_median': _median(r['first_event_seconds'] for r in rows)},
        'R4': {'cells_with_usage': len(known),
               'input_tokens_median': _median(r['usage']['input_tokens'] for r in known),
               'cached_input_share': (sum(r['usage']['cached_tokens'] for r in cached)
                                      / sum(r['usage']['input_tokens'] for r in cached))
               if cached and sum(r['usage']['input_tokens'] for r in cached) else None,
               'uncached_input_tokens_median': _median(r['usage']['input_tokens'] - r['usage']['cached_tokens']
                                                       for r in cached)},
    }


def _paired(rows, products, seed, resamples, flips):
    first, other = products
    by_key = {}
    for row in rows:
        by_key.setdefault((row['task'], row['rep']), {})[row['product']] = row
    pairs = [{'task': key[0], 'diff': int(v[first]['success']) - int(v[other]['success']),
              'a': v[first]['success'], 'b': v[other]['success']}
             for key, v in sorted(by_key.items()) if first in v and other in v]
    if not pairs:
        return None
    interval = st.cluster_bootstrap(pairs, lambda rs: sum(r['diff'] for r in rs) / len(rs) if rs else None,
                                    cluster='task', resamples=resamples, seed=seed)
    per_task = {}
    for pair in pairs:
        per_task.setdefault(pair['task'], []).append(pair['diff'])
    test = st.sign_flip([sum(v) / len(v) for _, v in sorted(per_task.items())], flips=flips, seed=seed)
    b = sum(p['a'] and not p['b'] for p in pairs)
    c = sum(p['b'] and not p['a'] for p in pairs)
    return {'pairs': len(pairs), 'delta': interval['point'], 'cluster95': [interval['low'], interval['high']],
            'sign_flip_p': test['p_value'], 'mcnemar': {'b': b, 'c': c, 'p': st.mcnemar_exact(b, c)}}


def _thresholds(summary, paired, rows):
    pebble, baseline = summary['pebble'], summary['baseline']
    ratio = (pebble['R4']['input_tokens_median'] / baseline['R4']['input_tokens_median']
             if pebble['R4']['input_tokens_median'] and baseline['R4']['input_tokens_median'] else None)
    low = paired['cluster95'][0] if paired else None
    return {
        'T1': {'value': pebble['success'], 'lower95': pebble['success_cluster95'][0],
               'met': None if pebble['success'] is None or pebble['success_cluster95'][0] is None
               else pebble['success'] >= 0.65 and pebble['success_cluster95'][0] >= 0.55},
        'T2': {'delta': paired['delta'] if paired else None, 'lower95': low, 'p': paired['sign_flip_p'] if paired else None,
               'non_inferior': None if low is None else low >= -0.05,
               'improves': None if low is None else low > 0 and paired['sign_flip_p'] < 0.05},
        'T3': {'false_completions': pebble['false_completions'], 'met': pebble['false_completions'] == 0},
        'T4': {'duplicate_effects': pebble['flags']['duplicate_effect'], 'met': pebble['flags']['duplicate_effect'] == 0},
        'T7': {'review_burden_mean': pebble['review_burden_mean'], 'published': True},
        'T9': {'median_input_ratio': ratio, 'met': None if ratio is None else ratio <= 1.5, 'published': True,
               'uncached_input_tokens_median': pebble['R4']['uncached_input_tokens_median']},
        'T10': {'honesty': pebble['honesty_on_blocked'],
                'met': None if pebble['honesty_on_blocked'] is None else pebble['honesty_on_blocked'] == 1.0},
        'T5': 'measured by the 18a.3 constraint-retention check; not in 24a',
        'T6': 'measured by the memory recall benchmark (scripts/evaluate_memory_recall.py)',
        'T8': 'measured by bench_perf PB6 and measure_resources r3',
        'T11': 'measured by measure_resources on the frozen candidate',
    }


def _not_run(planned, task):
    return {'task': planned['task'], 'rep': planned['rep'], 'phrasing': planned['phrasing'],
            'product': planned['product'], 'position': planned['position'], 'outcome': 'not_run', 'success': False,
            'state': 'not_run', 'checks': {}, 'flags': {name: 0 for name in FLAGS},
            'review': {'approvals': 0, 'rejections': 0, 'answers': 0, 'escalations': 0}, 'requests': None,
            'wall_seconds': None, 'first_event_seconds': None, 'usage': None, 'turns': 0, 'restarts': None,
            'error': 'cell_not_run'}


def analyse(plan, reports, ledger):
    """Offline intention-to-treat analysis of a registered plan's signed results (no model call)."""
    plan_sha256 = verify_plan(plan)
    entry, entries = _registered(plan_sha256, ledger)
    begins = {e['evaluation_id']: e for e in entries if e['kind'] == 'begin' and e['plan_sha256'] == plan_sha256}
    ends = {e['evaluation_id']: e for e in entries if e['kind'] == 'end' and e['evaluation_id'] in begins}
    if any(e['seq'] < entry['seq'] for e in begins.values()):
        raise PlanRefused('evaluation_before_plan_registration')
    planned = {(c['task'], c['rep'], c['product']): c for c in plan['schedule']}
    recorded, supplied = {}, {}
    for report in reports:
        try:
            ledger.verify(report, schema=RESULTS_SCHEMA)
        except EvidenceRefused as exc:
            raise PlanRefused(exc.reasons) from None
        ident = report['evaluation_id']
        if ident not in begins or report['plan_sha256'] != plan_sha256:
            raise PlanRefused('report_not_in_plan')
        if ident not in ends or ends[ident]['report_sha256'] != _sha(_canonical(report)):
            raise PlanRefused('report_not_ended_in_registry')
        if ident in supplied:
            raise PlanRefused('duplicate_report_supplied')
        supplied[ident] = report
        cells = [[c['task'], REGISTRY_ARMS[c['product']], c['rep']] for c in report['cells']]
        if cells != begins[ident]['cells']:
            raise PlanRefused('report_cells_not_begun')
        for cell in report['cells']:
            key = (cell['task'], cell['rep'], cell['product'])
            if key not in planned or planned[key]['phrasing'] != cell['phrasing']:
                raise PlanRefused('cell_not_in_plan')
            recorded.setdefault(key, []).append((begins[ident]['seq'], cell))
    withheld = sorted(i for i in ends if i not in supplied)
    if withheld:
        raise PlanRefused('registered_report_missing')
    tasks = {t.id: t for t in corpus.TASKS}
    rows, duplicates = [], []
    for key, cell in sorted(planned.items(), key=lambda item: item[1]['position']):
        runs = [c for _, c in sorted(recorded.get(key, []), key=lambda item: item[0])]
        chosen = runs[0] if runs else _not_run(cell, tasks[key[0]])
        if len(runs) > 1:
            duplicates.append({'task': key[0], 'rep': key[1], 'product': key[2],
                               'outcomes': [r['outcome'] for r in runs[1:]]})
        task = tasks[key[0]]
        rows.append({**chosen, 'family': task.family, 'blocked': task.blocked, 'multi_turn': task.multi_turn,
                     'scope': task.products})
    seed = int(plan_sha256[16:32], 16)
    resamples, flips = plan['statistics']['bootstrap_resamples'], plan['statistics']['sign_flips']
    products = plan['products']
    summary = {p: _product_summary([r for r in rows if r['product'] == p], seed, resamples) for p in products}
    # The claim set pairs every task (as 18a.3 did); against Codex only the compared families are paired.
    compared = rows if plan['kind'] == 'harness' else [r for r in rows if r['scope'] == COMPARED]
    paired = _paired(compared, products, seed, resamples, flips)
    families = {}
    for family in corpus.FAMILIES:
        sub = [r for r in rows if r['family'] == family]
        if not sub:
            continue
        families[family] = {'scope': sub[0]['scope'],
                            'products': {p: {'cells': sum(r['product'] == p for r in sub),
                                             'success': _rate([r for r in sub if r['product'] == p]),
                                             'false_completions': sum(r['outcome'] == 'false_completion'
                                                                      for r in sub if r['product'] == p)}
                                         for p in products},
                            'paired': _paired(sub, products, seed, resamples, flips)}
    result = {'schema': ANALYSIS_SCHEMA, 'plan_sha256': plan_sha256, 'kind': plan['kind'],
              'claim_set': plan['claim_set'], 'corpus': {'version': plan['corpus']['version'],
                                                         'fingerprint': plan['corpus']['fingerprint']},
              'products': products, 'evidence': sorted({r['evidence'] for r in supplied.values()}),
              'versions': {i: r['versions'] for i, r in supplied.items()},
              'counting': {'planned_cells': len(rows), 'not_run': sum(r['outcome'] == 'not_run' for r in rows),
                           'rule': STATISTICS['counting']},
              'summary': summary, 'paired': paired, 'families': families, 'duplicates': duplicates,
              'registry': {'entries': len(entries), 'head': entries[-1]['hash'] if entries else None},
              'graded_flags': list(GRADED_FLAGS)}
    if plan['kind'] == 'harness':
        result['thresholds'] = _thresholds(summary, paired, rows)
    else:
        rules = thresholds(plan['kind'])
        claims = {}
        for scope in rules['scopes']:
            item = paired if scope == 'overall' else (families.get(scope) or {}).get('paired')
            low = item['cluster95'][0] if item else None
            claims[scope] = {'lower95': low, 'better': bool(low is not None and low > 0)}
        result['claims'] = {'rule': rules['rule'], 'scopes': claims,
                            'descriptive': rules['descriptive'], 'not_claimed': rules['not_claimed']}
        if plan['kind'] == 'claude':
            prices = plan['budget']['prices_per_mtok']
            result['cost_usd'] = {p: round(sum(c for c in (cell_cost_usd(r.get('usage'), prices)
                                                          for r in rows if r['product'] == p) if c is not None), 4)
                                  for p in products}
            result['cost_note'] = ('at the plan\'s prices (%s, read %s); Claude Code ran on a Claude subscription, so '
                                   'its figure is the API-equivalent cost, not money spent' % (plan['budget']['prices_source'],
                                                                                  plan['budget']['prices_retrieved']))
    return result


# -- report tables (docs/PEBBLE_V1_BENCHMARKS.md) ----------------------------------------------------------------------------

def _pct(value):
    return '—' if value is None else '%.1f %%' % (100 * value)


def _num(value, digits=1):
    return '—' if value is None else ('%.' + str(digits) + 'f') % value


def tables(analysis):
    """Markdown tables for the benchmark report: overall, families, R1-R4, and thresholds or claims."""
    products = analysis['products']
    lines = ['| Measure | ' + ' | '.join(products) + ' |', '|---|' + '---|' * len(products)]
    s = analysis['summary']
    for label, get in (('Cells', lambda p: str(s[p]['cells'])), ('Success', lambda p: _pct(s[p]['success'])),
                       ('Success 95 % CI (task clusters)', lambda p: '%s – %s' % tuple(_pct(v) for v in s[p]['success_cluster95'])),
                       ('False completions', lambda p: str(s[p]['false_completions'])),
                       ('Format only', lambda p: str(s[p]['format_only'])),
                       ('Review burden (mean)', lambda p: _num(s[p]['review_burden_mean'], 2)),
                       ('R1 canonical / worst paraphrase', lambda p: '%s / %s' % (_pct(s[p]['R1']['canonical']),
                                                                                   _pct(s[p]['R1']['worst_paraphrase']))),
                       ('R2 multi-turn success', lambda p: _pct(s[p]['R2']['success'])),
                       ('R3 requests median / p95', lambda p: '%s / %s' % (_num(s[p]['R3']['requests_median']),
                                                                            _num(s[p]['R3']['requests_p95']))),
                       ('R3 wall s median / p95', lambda p: '%s / %s' % (_num(s[p]['R3']['wall_median']),
                                                                          _num(s[p]['R3']['wall_p95']))),
                       ('R4 cached input share', lambda p: _pct(s[p]['R4']['cached_input_share']))):
        lines.append('| %s | %s |' % (label, ' | '.join(get(p) for p in products)))
    out = ['### Overall', ''] + lines + ['']
    paired = analysis['paired']
    if paired:
        out += ['Paired difference (%s − %s): %s, 95 %% CI %s to %s; McNemar b=%d, c=%d, p=%.3g.'
                % (products[0], products[1], _pct(paired['delta']), _pct(paired['cluster95'][0]),
                   _pct(paired['cluster95'][1]), paired['mcnemar']['b'], paired['mcnemar']['c'],
                   paired['mcnemar']['p']), '']
    out += ['### By family', '', '| Family | Scope | ' + ' | '.join(products) + ' | Paired Δ (95 % CI) |',
            '|---|---|' + '---|' * len(products) + '---|']
    for family, item in analysis['families'].items():
        p = item['paired']
        out.append('| %s | %s | %s | %s |' % (family, item['scope'], ' | '.join(
            _pct(item['products'][name]['success']) for name in products),
            '—' if not p else '%s (%s to %s)' % (_pct(p['delta']), _pct(p['cluster95'][0]), _pct(p['cluster95'][1]))))
    out.append('')
    if 'thresholds' in analysis:
        out += ['### Thresholds (unchanged, never lowered)', '', '| # | Result | Met |', '|---|---|---|']
        for name, item in analysis['thresholds'].items():
            if isinstance(item, dict):
                met = item.get('met', item.get('non_inferior'))
                shown = {k: v for k, v in item.items() if k not in ('met', 'published')}
                out.append('| %s | %s | %s |' % (name, ', '.join('%s %s' % (k, _num(v, 3) if isinstance(v, float) else v)
                                                                for k, v in shown.items()),
                                                 'published' if met is None else 'yes' if met else '**no**'))
            else:
                out.append('| %s | %s | — |' % (name, item))
    if 'claims' in analysis:
        out += ['### Claims (pre-registered rule)', '', analysis['claims']['rule'] + '.', '',
                '| Scope | Lower 95 % bound | Better? |', '|---|---|---|']
        for scope, item in analysis['claims']['scopes'].items():
            out.append('| %s | %s | %s |' % (scope, _pct(item['lower95']), 'yes' if item['better'] else 'no claim'))
    return '\n'.join(out) + '\n'
