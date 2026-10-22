"""Phase 19 H4: where a run's input tokens go, request by request (measurement only; no model, no network).

Reads requests captured from scripted runs as JSON lines, one per model request:

    {"arm": "pebble"|"baseline", "task": "<task id>", "family": "<family>", "actor": "root"|"baseline"|"child:..",
     "step": <n>, "messages": [...], "tools": [...]}

and counts every part of each request with Pebble's own TokenBudget (the bundled o200k_base ranks; without
them, its one-token-per-byte upper bound, reported as the counter). Messages are classified by the host's own
`_pebble_*` markers, then counted in their wire form (markers stripped), as the provider adapter sends them.

    python scripts/measure_context_cost.py requests.jsonl [--json report.json] [--before before.jsonl]

With --before, a second capture of the same tasks (an earlier source or offer) is compared with the first: input
tokens per family and per part, before and after, and the median ratio against the baseline arm (Phase 20 H20).

Parts: system prompt, tool schemas, the person's request, earlier chat history, the constraints unit, recalled
memory, checkpoints (task checkpoint, source audit, workflow), plan and goal, inbox (host action updates and
mail), skills and plugins, sources (file listing, plugin index, attachments), host notes (completion checks,
resume, recovery), the model's own turns, tool results, and framing (TokenBudget's per-message overhead and
margin, the difference between its request total and the parts).
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from pebble.runner import AGENT_BRIEF, SMALL_BRIEF  # noqa: E402
from pebble.token_budget import TokenBudget  # noqa: E402
from pebble.tool_catalogue import AGENT_ROOT_TOOLS, CORE_TOOLS  # noqa: E402

PARTS = ('system', 'tool_schemas', 'request', 'history', 'constraints', 'memory', 'checkpoint', 'plan_goal',
         'inbox', 'skills_plugins', 'sources', 'host_notes', 'assistant', 'tool_results', 'framing')
MARKERS = (('_pebble_constraints', 'constraints'), ('_pebble_memory', 'memory'), ('_pebble_checkpoint', 'checkpoint'),
           ('_pebble_plan', 'plan_goal'), ('_pebble_goal', 'plan_goal'), ('_pebble_inbox', 'inbox'),
           ('_pebble_required_skill', 'skills_plugins'), ('_pebble_required_plugin', 'skills_plugins'),
           ('_pebble_skill', 'skills_plugins'), ('_pebble_source', 'sources'), ('_pebble_current', 'request'))
HOST_PREFIXES = ('HOST ', 'Recorded action results', 'RETRIEVED MEMORY')
_BUDGET = TokenBudget(ROOT / 'src-tauri' / 'resources')


def counter_name():
    """'o200k_base' with the bundled ranks, else 'byte_estimate' (an upper bound)."""
    return getattr(TokenBudget.encoding(), 'name', 'o200k_base')


def tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(TokenBudget.encoding().encode(text, disallowed_special=()))


def wire(message):
    return {k: v for k, v in message.items() if not k.startswith('_pebble_')}


def part_of(message, before_request):
    """The part one message belongs to. ``before_request``: no request message has been seen yet."""
    for marker, part in MARKERS:
        if message.get(marker):
            return part
    role, content = message.get('role'), message.get('content')
    if role == 'system':
        return 'system'
    if role == 'tool':
        return 'tool_results'
    if role == 'assistant':
        return 'history' if before_request else 'assistant'
    if isinstance(content, str) and content.startswith(HOST_PREFIXES):
        return 'memory' if content.startswith('RETRIEVED MEMORY') else 'host_notes'
    return 'history' if before_request else 'request'


def composition(messages, tools):
    """{'total', 'parts': {part: tokens}} for one request; framing makes the parts add up to TokenBudget's total.
    Without a host request marker (the baseline arm), the first user message is the request."""
    parts = dict.fromkeys(PARTS, 0)
    marked = any(m.get('_pebble_current') for m in messages)
    seen_request = False
    for message in messages:
        part = part_of(message, not seen_request)
        if not marked and not seen_request and message.get('role') == 'user':
            part = 'request'
        if part == 'request':
            seen_request = True
        parts[part] += tokens(wire(message))
    parts['tool_schemas'] = tokens(tools) if tools else 0
    total = _BUDGET.count([wire(m) for m in messages], tools or [])
    parts['framing'] = total - sum(v for k, v in parts.items() if k != 'framing')
    return {'total': total, 'parts': parts}


def tool_tokens(tools):
    """{tool name: tokens of its schema} for one request's offer."""
    return {t['function']['name']: tokens(t) for t in tools or () if isinstance(t, dict) and 'function' in t}


def briefed(tools):
    """The offer as the 4K route sends it (runner.py: a tool's brief replaces its description and field help goes;
    names, parameters and every admission constraint stay)."""
    out = []
    for tool in tools or ():
        tool = json.loads(json.dumps(tool))
        function = tool.get('function') or {}
        brief = AGENT_BRIEF.get(function.get('name')) or SMALL_BRIEF.get(function.get('name'))
        if brief:
            function['description'] = brief
            for field in (function.get('parameters') or {}).get('properties', {}).values():
                if isinstance(field, dict):
                    field.pop('description', None)
        out.append(tool)
    return out


def group_of(name):
    return 'agent' if name in AGENT_ROOT_TOOLS else 'core' if name in CORE_TOOLS else 'task'


def called(records):
    """{(arm, task): tool names the model called in that run}, read from its own turns in every request."""
    out = {}
    for record in records:
        names = out.setdefault((record['arm'], record['task']), set())
        for message in record['messages']:
            for call in message.get('tool_calls') or () if message.get('role') == 'assistant' else ():
                names.add(((call or {}).get('function') or {}).get('name'))
    return out


def measure(records):
    """One row per captured request: its metadata, total, parts and per-tool schema tokens (with whether the run
    ever called that tool)."""
    used, rows = called(records), []
    for record in records:
        found = composition(record['messages'], record.get('tools') or [])
        schemas = tool_tokens(record.get('tools'))
        calls = used.get((record['arm'], record['task']), set())
        rows.append({'arm': record['arm'], 'task': record['task'], 'family': record.get('family'),
                     'actor': record['actor'], 'step': record['step'], 'tools_offered': len(record.get('tools') or ()),
                     'messages': len(record['messages']), 'schemas': schemas,
                     'unused': sorted(name for name in schemas if name not in calls),
                     'unused_schema_tokens': sum(v for name, v in schemas.items() if name not in calls),
                     'brief_saving': found['parts']['tool_schemas'] - (tokens(briefed(record.get('tools')))
                                                                       if record.get('tools') else 0), **found})
    return rows


def schema_costs(rows, arm='pebble'):
    """Schema tokens of one arm over all its requests, by group (agent coordination, core, task tools) and by tool,
    with the part spent on tools the run never called (what offering on demand could save, at most)."""
    groups, tools = {}, {}
    for row in rows:
        if row['arm'] != arm:
            continue
        unused = set(row['unused'])
        for name, value in row['schemas'].items():
            group = groups.setdefault(group_of(name), {'offered': 0, 'unused': 0})
            tool = tools.setdefault(name, {'group': group_of(name), 'offered': 0, 'unused': 0, 'requests': 0})
            for entry in (group, tool):
                entry['offered'] += value
                entry['unused'] += value if name in unused else 0
            tool['requests'] += 1
    requests = sum(1 for row in rows if row['arm'] == arm) or 1
    return {'requests': requests, 'groups': groups,
            'tools': dict(sorted(tools.items(), key=lambda item: -item[1]['offered'])),
            'unused_per_request': round(sum(g['unused'] for g in groups.values()) / requests)}


# Reductions estimated by what_if (the track note's proposals). The 'drop' scenarios remove only schemas the run
# never called, so they are upper bounds for these runs: a live model may need a dropped tool and pay one more turn.
# agent_tools_by_need is agents.root_tools' staged discovery (ReadAgentTree and SpawnAgent until a child exists),
# which small routes already use; briefs_everywhere sends every route the 4K route's tool descriptions. Phase 20 H20
# applies the first two above 8K (runner.large_needs), so on that source they estimate only what is left of them.
SCENARIOS = {
    'agent_tools_by_need': lambda name: name in AGENT_ROOT_TOOLS and name not in ('SpawnAgent', 'ReadAgentTree'),
    'core_tools_by_need': lambda name: name in ('MemoryPropose', 'ReadRunEvidence'),
    'task_tools_on_demand': lambda name: name not in AGENT_ROOT_TOOLS and name not in CORE_TOOLS,
}
ON_DEMAND_TOKENS = 150  # FindTools + LoadTool (D19 §8.1), offered instead of the never-called task schemas


def what_if(rows, scenarios=SCENARIOS, arm='pebble', baseline='baseline'):
    """{scenario: {median_input_tokens, ratio, saved_per_request}} when the pebble arm stops offering, in each run,
    the never-called schemas a scenario names, or (briefs_everywhere) sends the 4K briefs; 'combined' applies the
    three drop scenarios."""
    def runs(drop, extra, briefs=False):
        totals = {}
        for row in rows:
            if row['arm'] != arm:
                continue
            removed = sum(row['schemas'][name] for name in row['unused'] if drop(name))
            removed += row['brief_saving'] if briefs else 0
            totals[row['task']] = totals.get(row['task'], 0) + row['total'] - removed + (extra if removed else 0)
        return totals

    base = {}
    for row in rows:
        if row['arm'] == baseline:
            base[row['task']] = base.get(row['task'], 0) + row['total']
    before = runs(lambda name: False, 0)
    requests = sum(1 for row in rows if row['arm'] == arm) or 1
    out = {}
    items = list(scenarios.items()) + [('briefs_everywhere', None),
                                       ('combined', lambda name: any(test(name) for test in scenarios.values()))]
    for name, drop in items:
        extra = ON_DEMAND_TOKENS if name in ('task_tools_on_demand', 'combined') else 0
        after = runs(drop or (lambda tool: False), extra, briefs=drop is None)
        median = statistics.median(after.values()) if after else 0
        out[name] = {'median_input_tokens': median,
                     'ratio': round(median / statistics.median(base.values()), 2) if base else None,
                     'saved_per_request': round((sum(before.values()) - sum(after.values())) / requests)}
    return out


def summarize(rows):
    """Per (arm, task): requests, input tokens over the run, per-part totals and the first request; then per arm."""
    runs = {}
    for row in rows:
        run = runs.setdefault((row['arm'], row['task']), {
            'arm': row['arm'], 'task': row['task'], 'family': row['family'], 'requests': 0, 'input_tokens': 0,
            'parts': dict.fromkeys(PARTS, 0), 'first': None, 'max_request': 0, 'tools_offered': 0})
        run['requests'] += 1
        run['input_tokens'] += row['total']
        run['max_request'] = max(run['max_request'], row['total'])
        run['tools_offered'] = max(run['tools_offered'], row['tools_offered'])
        for part, value in row['parts'].items():
            run['parts'][part] += value
        if run['first'] is None or row['step'] < run['first']['step']:
            run['first'] = {'step': row['step'], 'total': row['total'], 'parts': dict(row['parts'])}
    arms = {}
    for run in runs.values():
        arm = arms.setdefault(run['arm'], {'runs': 0, 'input_tokens': [], 'requests': [], 'parts': dict.fromkeys(PARTS, 0)})
        arm['runs'] += 1
        arm['input_tokens'].append(run['input_tokens'])
        arm['requests'].append(run['requests'])
        for part, value in run['parts'].items():
            arm['parts'][part] += value
    for arm in arms.values():
        arm['median_input_tokens'] = statistics.median(arm['input_tokens'])
        arm['median_requests'] = statistics.median(arm['requests'])
        total = sum(arm['parts'].values()) or 1
        arm['shares'] = {part: round(value / total, 4) for part, value in arm['parts'].items()}
    ratio = None
    if 'pebble' in arms and 'baseline' in arms and arms['baseline']['median_input_tokens']:
        ratio = round(arms['pebble']['median_input_tokens'] / arms['baseline']['median_input_tokens'], 2)
    return {'counter': counter_name(), 'runs': sorted(runs.values(), key=lambda r: (r['task'], r['arm'])),
            'arms': arms, 'median_ratio': ratio, 'schemas': {arm: schema_costs(rows, arm) for arm in arms},
            'what_if': what_if(rows) if 'pebble' in arms else {}}


def table(summary, parts=('system', 'tool_schemas', 'request', 'constraints', 'memory', 'checkpoint', 'inbox',
                          'sources', 'host_notes', 'assistant', 'tool_results', 'framing')):
    """A markdown table: one row per run, the input tokens of the whole run split by part."""
    head = ['task', 'arm', 'requests', 'tools', 'input tokens'] + list(parts)
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    for run in summary['runs']:
        cells = [run['task'], run['arm'], run['requests'], run['tools_offered'], run['input_tokens']]
        cells += [run['parts'][p] for p in parts]
        lines.append('| ' + ' | '.join(str(c) for c in cells) + ' |')
    for name, arm in sorted(summary['arms'].items()):
        shares = ', '.join(f'{p} {arm["shares"][p]:.0%}' for p in sorted(arm['shares'], key=arm['shares'].get,
                                                                         reverse=True)[:6])
        lines.append(f'\n{name}: {arm["runs"]} runs, median {arm["median_input_tokens"]} input tokens over a median '
                     f'{arm["median_requests"]} requests; largest parts: {shares}.')
    if summary['median_ratio'] is not None:
        lines.append(f'Median input tokens per run, pebble / baseline: {summary["median_ratio"]}x '
                     f'(counter: {summary["counter"]}).')
    costs = summary.get('schemas', {}).get('pebble')
    if costs:
        groups = ', '.join(f'{name} {g["offered"]} offered / {g["unused"]} never called'
                           for name, g in sorted(costs['groups'].items()))
        top = ', '.join(f'{name} {t["offered"]}' for name, t in list(costs['tools'].items())[:8])
        lines.append(f'Pebble schema tokens over {costs["requests"]} requests by group: {groups}; never-called schemas '
                     f'per request: {costs["unused_per_request"]}. Largest tools: {top}.')
    for name, found in summary.get('what_if', {}).items():
        lines.append(f'What if {name}: median {found["median_input_tokens"]} input tokens per run '
                     f'({found["ratio"]}x the baseline), {found["saved_per_request"]} fewer per pebble request.')
    return '\n'.join(lines)


def compare(before, after, arm='pebble', baseline='baseline'):
    """Two summaries of the same tasks, before and after: per task (one per family in the H19 plan) the arm's input
    tokens over the run, its schema and system tokens, tools offered and its ratio to that side's own baseline run;
    the arm's totals per part; and the median input tokens and ratios. Tasks on one side only are listed, not paired."""
    old = {(r['arm'], r['task']): r for r in before['runs']}
    new = {(r['arm'], r['task']): r for r in after['runs']}
    mine = [{t for a, t in side if a == arm} for side in (old, new)]
    rows = []
    for task in sorted(mine[0] & mine[1]):
        pair = [old[(arm, task)], new[(arm, task)]]
        bases = [side.get((baseline, task)) for side in (old, new)]
        rows.append({'family': pair[1]['family'] or pair[0]['family'], 'task': task,
                     'baseline': [b['input_tokens'] if b else None for b in bases],
                     'input_tokens': [r['input_tokens'] for r in pair], 'requests': [r['requests'] for r in pair],
                     'tools_offered': [r['tools_offered'] for r in pair],
                     'tool_schemas': [r['parts']['tool_schemas'] for r in pair],
                     'system': [r['parts']['system'] for r in pair],
                     'ratio': [round(r['input_tokens'] / b['input_tokens'], 2) if b and b['input_tokens'] else None
                               for r, b in zip(pair, bases)]})
    arms = [side['arms'].get(arm, {}) for side in (before, after)]
    requests = [sum(a.get('requests', ())) for a in arms]
    return {'arm': arm, 'rows': rows, 'missing': sorted(mine[0] ^ mine[1]),
            'parts': {p: [a.get('parts', {}).get(p, 0) for a in arms] for p in PARTS},
            'median_input_tokens': [a.get('median_input_tokens') for a in arms],
            'median_ratio': [before['median_ratio'], after['median_ratio']], 'requests': requests,
            'per_request': [round(sum(a.get('input_tokens', ())) / n) if n else None for a, n in zip(arms, requests)]}


def comparison_table(comparison):
    """A markdown table of compare(): one row per family, then the part totals and the median ratios."""
    def pair(values):
        return ' → '.join('-' if v is None else str(v) for v in values)

    head = ['family', 'task', 'baseline', 'before', 'after', 'change', 'schemas', 'system', 'tools', 'requests',
            'ratio to baseline']
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    for row in comparison['rows']:
        before, after = row['input_tokens']
        change = f'{(after - before) / before:+.0%}' if before else '-'
        lines.append('| ' + ' | '.join(str(c) for c in (
            row['family'], row['task'], pair(dict.fromkeys(row['baseline'])), before, after, change,
            pair(row['tool_schemas']), pair(row['system']), pair(row['tools_offered']), pair(row['requests']),
            pair(row['ratio']))) + ' |')
    parts = ', '.join(f'{p} {pair(v)}' for p, v in comparison['parts'].items() if any(v))
    lines.append(f'\n{comparison["arm"]} input tokens by part over all runs, before → after: {parts}.')
    lines.append(f'Per request: {pair(comparison["per_request"])} over {pair(comparison["requests"])} requests. '
                 f'Median input tokens per run: {pair(comparison["median_input_tokens"])}; median ratio to the '
                 f'baseline arm: {pair(comparison["median_ratio"])}.')
    if comparison['missing']:
        lines.append('Not paired (one side only): ' + ', '.join(comparison['missing']) + '.')
    return '\n'.join(lines)


def load(path):
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('requests', help='captured requests, one JSON object per line')
    parser.add_argument('--json', help='also write the summary as JSON to this path')
    parser.add_argument('--before', help='an earlier capture of the same tasks; adds a before/after comparison')
    args = parser.parse_args(argv)
    summary = summarize(measure(load(args.requests)))
    print(table(summary))
    if args.before:
        summary = {**summary, 'comparison': compare(summarize(measure(load(args.before))), summary)}
        print('\n' + comparison_table(summary['comparison']))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
