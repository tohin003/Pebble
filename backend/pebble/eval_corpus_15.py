"""Phase 15 evaluation corpus 15a.1: synthetic tasks with independent, deterministic graders.

Six tasks for context, memory, code and delegation behaviour: long-constraint
retention, stale-source detection, memory paraphrase recall, memory conflict,
cross-file Python code and two independent files (a fair delegation case).
Every task declares ``requires_live_model``: a scripted run of it checks
plumbing and scoring only and is never quality evidence.

Graders read host records and final files, never assistant claims. Retained
failure: every required check is evaluated and recorded on every run; a check
that cannot be evaluated (a missing file, an unparsable module) is recorded as
failed rather than omitted, and any failed check fails the task. Each task's
``required_checks`` floor is part of its fingerprint, so a grader cannot be
lowered without changing the corpus fingerprint. Verdicts carry check names,
booleans and counts only; never file bodies, answers or oracle values.

Oracles stay in this module. They never enter a prompt, a workspace, the
grading child or a public report. The code task reuses the oracle-free
interpreter child: it receives sources and inputs, never expected values.
"""

import ast
import json
import re
from dataclasses import dataclass, fields
from pathlib import Path

from .evaluation import (APPROVABLE, EFFECT_TOOLS, GENERIC_ANSWER, GRANTABLE, NUMBER, PERMISSION_GROUPS,
                         MinimalPath, _broker_linked, _canonical, _changed, _interpret_in_child, _meets, _sha,
                         _strict_json, _verdict, final_value, static_gate)

CORPUS_VERSION = '15a.1'
CATEGORIES = ('long_constraint', 'stale_source', 'memory_paraphrase', 'memory_conflict', 'cross_file_code',
              'independent_files')
LIMITATIONS = (
    'Corpus 15a.1 tasks require a live model for any quality claim; scripted runs of them check plumbing and scoring only.',
    'The stale-source task applies exactly one host-simulated edit after the first successful read of its file; '
    'it does not model concurrent editors in general.',
    'Memory tasks seed synthetic semantic notes in a disposable home; results depend on the recall path in use.',
    'The long-constraint task fits comfortably in large windows; small-window compaction pressure is not forced.',
)


@dataclass(frozen=True)
class Task:
    """One 15a.1 task. Mirrors evaluation.Case so the same driver runs it."""
    id: str
    version: int
    category: str
    prompt: str
    grader: str
    files: tuple = ()             # ((relative path, text), ...) in the model-visible workspace
    tools: tuple = ('files',)     # granted permission groups; everything else is denied
    allowed_kinds: tuple = ()     # broker kinds the host helper may approve
    allowed_paths: tuple = ()     # exact relative paths an approved effect may touch
    allowed_recipes: tuple = ()
    answer: str = GENERIC_ANSWER  # fixed typed answer for every clarification form
    delegation: str = 'supported'
    memory: tuple = ()            # seeded semantic memories (synthetic)
    workspace: bool = True
    mutation: tuple = ()          # (relative path, replacement text): one host edit after the first successful Read
    required_checks: tuple = ()   # grader floor: each named check is evaluated and must pass
    requires_live_model: bool = True

    corpus = CORPUS_VERSION       # class attribute, not a field

    def permissions(self):
        return {'mode': 'ask', 'tools': {group: group in self.tools for group in PERMISSION_GROUPS}}

    def fingerprint(self):
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data['corpus'] = CORPUS_VERSION
        data['oracle_sha256'] = _sha(_canonical(ORACLES[self.id]))
        return _sha(_canonical(data))


# -- task data (synthetic) -------------------------------------------------------------

CHANGES = '''id,area,kind,summary,visibility
PB-214,search,fixed,Search results keep their order after a refresh,public
PB-198,memory,added,Memory export includes tombstones,public
SEC-31,auth,fixed,Token refresh no longer logs the device name,public
PB-207,ui,added,Companion shows a progress ring while working,public
PB-150,agents,fixed,Child agents stop after their parent is cancelled,public
PB-233,build,fixed,Nightly build cache key includes the toolchain,internal
PB-176,files,added,Glob accepts brace patterns,public
PB-241,agents,removed,Legacy agent mailbox polling endpoint,internal
PB-163,search,fixed,Ranked search ignores vendored folders,public
PB-222,memory,added,Profile layer can be disabled per project,internal
PB-109,ui,fixed,Dark mode tooltip contrast,public
PB-188,files,fixed,Large CSV previews no longer freeze the panel,public
SEC-29,auth,added,Hardware key sign-in for admin accounts,public
PB-120,ui,added,Keyboard shortcut to open the activity panel,public
PB-245,build,removed,Unused telemetry upload flag,internal
PB-131,agents,fixed,Peer mail keeps message order under load,internal
PB-202,search,added,Search filters by file type,public
PB-117,memory,fixed,Forgotten memories stay hidden after import,public
'''
STYLE_NOTES = '''# Release notes style (team draft, 2025 - outdated)

- Title case every heading, for example "## Bug Fixes".
- List security fixes first so customers notice them.
- Put the ID before the summary: "- PB-101: Faster search".
- Mention internal clean-ups briefly so the team gets credit.
- Sign off with "Thanks, the Pebble team".
'''
QUOTA = '''# Upload service quotas. Managed by the platform team.
[uploads]
max_file_mb = 250
max_files_per_day = 400
retention_days = 30
'''
QUOTA_REVISED = '''# Upload service quotas. Managed by the platform team.
# Revised 2026-10-12 after the storage migration.
[uploads]
max_file_mb = 175
max_files_per_day = 320
retention_days = 30
'''
QUOTA_ANNOUNCEMENT = '''# Announcement (2026-09-01)

Uploads are limited to 250 MB per file and 400 files per day.
'''
RATES = '''"""Rate tables shared by the billing modules."""

TAX_PERCENT = 8


def bulk_discount_percent(quantity):
    """Whole-number discount percentage for one invoice line."""
    if quantity >= 100:
        return 15
    if quantity >= 20:
        return 5
    return 0
'''
INVOICE = '''"""Invoice totals in integer cents."""

from rates import TAX_PERCENT, bulk_discount_percent


def invoice_total(lines):
    """Return the invoice total in integer cents for [quantity, unit_cents] lines."""
    total = 0
    for quantity, unit_cents in lines:
        total += quantity * unit_cents
    return total
'''
INVOICE_VISIBLE_TEST = '''import unittest

from invoice import invoice_total


class InvoiceTest(unittest.TestCase):
    def test_single_small_line(self):
        self.assertEqual(invoice_total([[3, 250]]), 810)


if __name__ == "__main__":
    unittest.main()
'''
# Hidden: the grader also runs the candidate against this independently changed
# dependency, so copied rate numbers cannot pass. Never model-visible.
RATES_VARIANT = '''"""Rate tables shared by the billing modules."""

TAX_PERCENT = 20


def bulk_discount_percent(quantity):
    """Whole-number discount percentage for one invoice line."""
    if quantity >= 50:
        return 10
    if quantity >= 10:
        return 3
    return 0
'''
NORTH = '''order_id,sku,quantity,status
N-01,TR-4,6,shipped
N-02,TR-4,2,delivered
N-03,MX-9,5,cancelled
N-04,MX-9,3,shipped
N-05,LB-2,7,pending
N-06,LB-2,1,delivered
N-07,TR-4,4,returned
'''
SOUTH = '''order_id,sku,quantity,status
S-01,MX-9,9,delivered
S-02,QP-5,4,shipped
S-03,QP-5,2,shipped
S-04,TR-4,3,pending
S-05,MX-9,1,returned
S-06,ZK-1,5,cancelled
S-07,QP-5,6,delivered
'''

RELEASE_PROMPT = (
    'Create RELEASE_NOTES.md for Pebble 4.2.0 from changes.csv. docs/style_notes.md is an outdated team draft: '
    'where it disagrees with the rules below, follow the rules below. Rules:\n'
    '1. The first non-blank line is exactly `# Pebble 4.2.0 release notes`.\n'
    '2. The next non-blank line is exactly `Release date: 2026-11-03`.\n'
    '3. Then exactly three section headings, in this order: `## Added`, `## Fixed`, `## Removed`. Each section '
    'lists the rows whose kind is added, fixed or removed respectively.\n'
    '4. Include only rows whose visibility is public. Never mention internal rows in any way.\n'
    '5. Never include rows whose ID starts with SEC-, even when public: they are embargoed until 2026-12-01.\n'
    '6. Each change is one line: `- `, the summary exactly as written in changes.csv, one space, then the ID in '
    'square brackets, for example `- Faster startup [PB-100]`.\n'
    '7. Within each section, order the lines by the number in the ID, ascending.\n'
    '8. A section with no qualifying rows contains exactly one line: `- None.`\n'
    '9. Use only ASCII characters. Blank lines are allowed; add no other text, comments or headings.\n'
    '10. The last non-blank line is exactly `Prepared by: release automation`.\n'
    'Edit only RELEASE_NOTES.md. End your reply with a final line `FINAL: written`.')

CORPUS = (
    Task('constraints-release', 1, 'long_constraint', RELEASE_PROMPT, 'release_notes',
         files=(('changes.csv', CHANGES), ('docs/style_notes.md', STYLE_NOTES)), tools=('files', 'edits'),
         allowed_kinds=('edit',), allowed_paths=('RELEASE_NOTES.md',),
         required_checks=('exists', 'utf8_text', 'broker_linked', 'scope', 'sources_unchanged', 'ascii_only',
                          'title_first', 'date_second', 'sections_in_order', 'no_internal_rows', 'embargo_respected',
                          'bullet_format', 'id_order', 'empty_section_marker', 'only_allowed_lines', 'footer_last',
                          'section_contents_exact', 'document_exact')),
    Task('stale-quota', 1, 'stale_source',
         'What are the current upload limits? Use config/quota.toml, which is the source of truth; the announcement '
         'in docs/ may be out of date. The platform team may change that file while you work, so report the values '
         'that are current when you finish. Do not edit files. End your reply with a final line exactly in the form '
         '`FINAL: <max_file_mb>; <max_files_per_day>`.',
         'stale_answer', files=(('config/quota.toml', QUOTA), ('docs/quota_announcement.md', QUOTA_ANNOUNCEMENT)),
         mutation=('config/quota.toml', QUOTA_REVISED),
         required_checks=('final_line', 'host_change_applied', 'workspace_unchanged_by_run', 'current_values',
                          'no_stale_values', 'no_effect_proposals', 'no_effect_tools')),
    Task('memory-paraphrase', 1, 'memory_paraphrase',
         'How often does our service agreement with the Lindqvist haulage client come up for renewal, and what is '
         'its reference code? Answer from my saved notes. End your reply with a final line exactly in the form '
         '`FINAL: <months>; <reference>`.',
         'memory_paraphrase', tools=(), delegation='unsupported', workspace=False, memory=(
             'Lindqvist Freight: the maintenance contract renews every 14 months; renewal reference LQ-5583.',
             'Lindgren Freight: the maintenance contract renews every 12 months; renewal reference LG-5538.',
             'Lindqvist Freight: the cancelled 2024 pilot agreement renewed every 18 months under reference LQ-3855; '
             'it no longer applies.',
             'Lindqvist Freight: the warehouse lease renews every 36 months; reference WL-0914.',
             'Lindqvist Freight: the billing contact is the accounts desk, extension 2210.',
             'Office plants: water the ficus every 9 days.'),
         required_checks=('final_line', 'workspace_unchanged', 'two_answers', 'months', 'reference',
                          'no_distractors')),
    Task('memory-conflict', 1, 'memory_conflict',
         'From my saved memory notes: (1) what is the Atlas lab server room door code, and (2) when does the Atlas '
         'lab nightly backup window start? If my notes conflict on an answer and no note supersedes the other, say '
         'so instead of choosing one. End your reply with a final line exactly in the form '
         '`FINAL: <door code or CONFLICT>; <backup start as HH:MM or CONFLICT>`.',
         'memory_conflict', tools=(), delegation='unsupported', workspace=False, memory=(
             'Atlas lab: the server room door code is 4417 (set by facilities on 2026-09-14).',
             'Atlas lab: the server room door code is 7144 (set by IT security on 2026-09-14).',
             'Atlas lab: the nightly backup window starts at 01:30 (scheduled on 2026-03-02).',
             'Atlas lab: the nightly backup window was moved to 02:45 on 2026-08-20, replacing the earlier 01:30 '
             'schedule.',
             'Birch lab: the server room door code is 4471.',
             'Atlas lab: the visitor badge desk opens at 08:15.'),
         required_checks=('final_line', 'workspace_unchanged', 'two_answers', 'conflict_reported',
                          'no_single_code_claimed', 'superseding_time', 'no_stale_time', 'no_distractor_code')),
    Task('code-invoice', 1, 'cross_file_code',
         'Fix invoice_total in invoice.py. Requirements: lines is a non-empty list or tuple of [quantity, unit_cents] '
         'integer pairs. For each line the gross amount is quantity times unit_cents; subtract that line\'s bulk '
         'discount, which is bulk_discount_percent(quantity) percent of the line\'s gross amount rounded down to '
         'whole cents. Add the discounted line amounts, then add tax of TAX_PERCENT percent of that sum, rounded '
         'down to whole cents. Return the total as an integer number of cents. Raise ValueError for an empty input, '
         'a quantity below 1 or a negative unit_cents. Do not modify the input. bulk_discount_percent and '
         'TAX_PERCENT live in rates.py: keep importing them with the existing `from rates import ...` line and do '
         'not copy their numbers, because rates.py changes independently. Edit only invoice.py. The module must '
         'contain only plain function code: no printing, file access, classes, decorators or `if __name__` block; '
         'any other import may come only from math, statistics or functools. Then run the python-unittest tests. '
         'End your reply with a final line `FINAL: done`.',
         'python_cross_file', files=(('rates.py', RATES), ('invoice.py', INVOICE), ('test_invoice.py', INVOICE_VISIBLE_TEST)),
         tools=('files', 'edits', 'tests'), allowed_kinds=('edit', 'tests'), allowed_paths=('invoice.py',),
         allowed_recipes=('python-unittest',),
         required_checks=('changed', 'broker_linked', 'scope', 'sources_unchanged', 'cross_file_assembly',
                          'static_gate', 'hidden_behaviour', 'tracks_dependency_changes')),
    Task('files-regions', 1, 'independent_files',
         'Create two files from two independent order exports: north_summary.json from north.csv and '
         'south_summary.json from south.csv. Each is a JSON object that maps each SKU to the total quantity of its '
         'orders whose status is shipped or delivered, ignoring every other status. Use integer totals and include '
         'only SKUs with a non-zero total. Each summary uses only its own CSV file. Do not modify the CSV files. End '
         'your reply with a final line `FINAL: written`.',
         'json_exact_multi', files=(('north.csv', NORTH), ('south.csv', SOUTH)), tools=('files', 'edits'),
         allowed_kinds=('edit',), allowed_paths=('north_summary.json', 'south_summary.json'),
         required_checks=('outputs_exist', 'outputs_broker_linked', 'scope', 'sources_unchanged',
                          'outputs_valid_json', 'outputs_exact')),
)
BY_ID = {task.id: task for task in CORPUS}

# Oracles never enter a prompt, a workspace, the grading child or a public report.
ORACLES = {
    'constraints-release': {
        'file': 'RELEASE_NOTES.md', 'unchanged': ('changes.csv', 'docs/style_notes.md'),
        'title': '# Pebble 4.2.0 release notes', 'date': 'Release date: 2026-11-03',
        'footer': 'Prepared by: release automation', 'empty': '- None.',
        'sections': (
            ('## Added', ('- Keyboard shortcut to open the activity panel [PB-120]',
                          '- Glob accepts brace patterns [PB-176]',
                          '- Memory export includes tombstones [PB-198]',
                          '- Search filters by file type [PB-202]',
                          '- Companion shows a progress ring while working [PB-207]')),
            ('## Fixed', ('- Dark mode tooltip contrast [PB-109]',
                          '- Forgotten memories stay hidden after import [PB-117]',
                          '- Child agents stop after their parent is cancelled [PB-150]',
                          '- Ranked search ignores vendored folders [PB-163]',
                          '- Large CSV previews no longer freeze the panel [PB-188]',
                          '- Search results keep their order after a refresh [PB-214]')),
            ('## Removed', ('- None.',))),
        'internal': (('PB-233', 'Nightly build cache key includes the toolchain'),
                     ('PB-241', 'Legacy agent mailbox polling endpoint'),
                     ('PB-222', 'Profile layer can be disabled per project'),
                     ('PB-245', 'Unused telemetry upload flag'),
                     ('PB-131', 'Peer mail keeps message order under load')),
        'embargoed': (('SEC-31', 'Token refresh no longer logs the device name'),
                      ('SEC-29', 'Hardware key sign-in for admin accounts')),
    },
    'stale-quota': {'values': ('175', '320'), 'stale': ('250', '400'), 'no_effects': True},
    'memory-paraphrase': {'months': '14', 'reference': 'LQ-5583',
                          'distractor_numbers': ('12', '18', '36', '9', '2210'),
                          'distractor_references': ('LG-5538', 'LQ-3855', 'WL-0914')},
    'memory-conflict': {'conflicting': ('4417', '7144'), 'distractor_codes': ('4471',), 'time': '02:45',
                        'stale_times': ('01:30', '08:15')},
    'code-invoice': {
        'module': 'invoice.py', 'function': 'invoice_total', 'dependency': ('rates.py', 'rates'),
        'unchanged': ('rates.py', 'test_invoice.py'),
        'checks': (
            {'name': 'hidden_small_line', 'args': [[4, 125]], 'expect': 'equal', 'value': 540},
            {'name': 'hidden_tier_five', 'args': [[25, 400]], 'expect': 'equal', 'value': 10260},
            {'name': 'hidden_mixed_tiers', 'args': [[100, 199], [2, 1050]], 'expect': 'equal', 'value': 20536},
            {'name': 'hidden_tier_boundary', 'args': [[20, 333]], 'expect': 'equal', 'value': 6833},
            {'name': 'hidden_below_boundary', 'args': [[19, 350]], 'expect': 'equal', 'value': 7182},
            {'name': 'hidden_per_line_rounding', 'args': [[21, 333], [21, 333]], 'expect': 'equal', 'value': 14351},
            {'name': 'hidden_tier_per_line', 'args': [[15, 100], [15, 100]], 'expect': 'equal', 'value': 3240},
            {'name': 'hidden_empty_raises_value_error', 'args': [], 'expect': 'raises_value_error'},
            {'name': 'hidden_zero_quantity_raises', 'args': [[0, 100]], 'expect': 'raises_value_error'},
            {'name': 'hidden_negative_price_raises', 'args': [[2, -5]], 'expect': 'raises_value_error'},
            {'name': 'hidden_input_not_mutated', 'args': [[25, 400], [1, 99]], 'expect': 'unmutated', 'value': 10366},
            {'name': 'hidden_tuple_input', 'args': [[3, 250], [25, 400]], 'tuple': True, 'expect': 'equal',
             'value': 11070},
        ),
        'variant': {'source': RATES_VARIANT, 'checks': (
            {'name': 'variant_tier_low', 'args': [[25, 400]], 'expect': 'equal', 'value': 11640},
            {'name': 'variant_mixed_tiers', 'args': [[100, 199], [2, 1050]], 'expect': 'equal', 'value': 24012},
            {'name': 'variant_untiered', 'args': [[9, 1000]], 'expect': 'equal', 'value': 10800},
            {'name': 'variant_boundary_rounding', 'args': [[10, 777]], 'expect': 'equal', 'value': 9044},
        )},
    },
    'files-regions': {'expected': (('north_summary.json', {'LB-2': 1, 'MX-9': 3, 'TR-4': 8}),
                                   ('south_summary.json', {'MX-9': 9, 'QP-5': 12})),
                      'unchanged': ('north.csv', 'south.csv')},
}


def _check_corpus():
    if len(CORPUS) != 6 or len(BY_ID) != len(CORPUS) or set(BY_ID) != set(ORACLES):
        raise AssertionError('Evaluation corpus 15a.1 is incomplete.')
    if {task.category for task in CORPUS} != set(CATEGORIES):
        raise AssertionError('Evaluation corpus 15a.1 lost a required category.')
    for task in CORPUS:
        if (set(task.tools) - GRANTABLE or set(task.allowed_kinds) - APPROVABLE
                or task.delegation not in {'supported', 'unsupported'}):
            raise AssertionError('Evaluation task grants an unreviewed capability: ' + task.id)
        if task.requires_live_model is not True or not task.required_checks or task.grader not in GRADERS:
            raise AssertionError('Evaluation task lacks its live-model declaration or grader floor: ' + task.id)
        if task.mutation and (len(task.mutation) != 2 or task.mutation[0] not in dict(task.files)):
            raise AssertionError('Evaluation task mutates an undeclared file: ' + task.id)


def corpus_fingerprint():
    return _sha(_canonical([[task.id, task.version, task.fingerprint()] for task in CORPUS]))


def corpus_manifest():
    """Public corpus listing: identities and fingerprints, no prompts or oracles."""
    return {'version': CORPUS_VERSION, 'fingerprint': corpus_fingerprint(),
            'cases': [{'id': t.id, 'version': t.version, 'category': t.category, 'grader': t.grader,
                       'delegation': t.delegation, 'requires_live_model': t.requires_live_model,
                       'required_checks': len(t.required_checks), 'fingerprint': t.fingerprint()} for t in CORPUS]}


def oracle_markers(task_id):
    """Distinctive oracle strings that must never become model-visible."""
    oracle = ORACLES[task_id]
    if task_id == 'constraints-release':
        return [line for _, lines in oracle['sections'] for line in lines if line != oracle['empty']]
    if task_id == 'stale-quota':
        return ['FINAL: ' + '; '.join(oracle['values'])]
    if task_id == 'memory-paraphrase':
        return ['FINAL: ' + oracle['months'] + '; ' + oracle['reference']]
    if task_id == 'memory-conflict':
        return ['FINAL: CONFLICT; ' + oracle['time']]
    if task_id == 'code-invoice':
        checks = oracle['checks'] + oracle['variant']['checks']
        return ([c['name'] for c in checks] + [json.dumps(c['args']) for c in checks if len(c['args']) >= 2]
                + ['TAX_PERCENT = 20'])
    return [_canonical(expected) for _, expected in oracle['expected']] + [json.dumps(expected) for _, expected in oracle['expected']]


# -- graders: pure functions of host evidence ----------------------------------------

def _text(data):
    if data is None:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def _unchanged(ev, paths):
    return all(ev.after.get(p) == ev.before.get(p) is not None for p in paths)


def _grade_release(task, oracle, ev, workdir):
    path = oracle['file']
    data = ev.files.get(path)
    text = _text(data)
    lines = [line.rstrip('\r') for line in (text or '').split('\n')]
    nonblank = [line for line in lines if line.strip()]
    headings = [title for title, _ in oracle['sections']]
    expected = ([oracle['title'], oracle['date']]
                + [line for title, items in oracle['sections'] for line in (title, *items)] + [oracle['footer']])

    sections, current = {}, None
    for line in nonblank:
        if line in headings:
            current = line
            sections.setdefault(current, [])
        elif current is not None and line.startswith('-'):
            sections[current].append(line)
    bullets = [line for line in nonblank if line.startswith('-')]
    lowered = (text or '').lower()

    def absent(rows):
        return text is not None and not any(ident.lower() in lowered or summary.lower() in lowered
                                            for ident, summary in rows)

    def ordered(items):
        numbers = [int(m.group(1)) for m in (re.search(r'\[[A-Z]+-(\d+)\]$', item) for item in items) if m]
        return len(numbers) == len(items) and numbers == sorted(numbers)

    allowed = {oracle['title'], oracle['date'], oracle['footer'], *headings}
    checks = {
        'exists': data is not None,
        'utf8_text': text is not None,
        'broker_linked': _broker_linked(ev, path),
        'scope': set(_changed(ev)) <= set(task.allowed_paths),
        'sources_unchanged': _unchanged(ev, oracle['unchanged']),
        'ascii_only': text is not None and text.isascii(),
        'title_first': nonblank[:1] == [oracle['title']],
        'date_second': nonblank[1:2] == [oracle['date']],
        'sections_in_order': [line for line in nonblank if line.startswith('##')] == headings,
        'no_internal_rows': absent(oracle['internal']),
        'embargo_respected': absent(oracle['embargoed']),
        'bullet_format': bool(bullets) and all(line == oracle['empty']
                                               or re.fullmatch(r'- \S.*\S \[[A-Z]+-\d+\]', line) for line in bullets),
        'id_order': bool(sections) and all(ordered([i for i in items if i != oracle['empty']])
                                           for items in sections.values()),
        'empty_section_marker': sections.get(headings[-1]) == [oracle['empty']],
        'only_allowed_lines': bool(nonblank) and all(line in allowed or line.startswith('- ') for line in nonblank),
        'footer_last': nonblank[-1:] == [oracle['footer']],
        'section_contents_exact': all(sections.get(title) == list(items) for title, items in oracle['sections']),
        'document_exact': nonblank == expected,
    }
    return _verdict('release_notes', checks)


def _grade_stale(task, oracle, ev, workdir):
    relative, replacement = task.mutation
    expected = dict(ev.before)
    expected[relative] = _sha(replacement.encode('utf-8'))
    value = final_value(ev.answer)
    parts = [part.strip() for part in (value or '').split(';')]
    numbers = [NUMBER.findall(part) for part in parts]
    found = {n for group in numbers for n in group}
    checks = {
        'final_line': value is not None,
        # The single host edit happened, so the task actually observed the file before it changed.
        'host_change_applied': ev.host_mutations == 1,
        'workspace_unchanged_by_run': ev.host_mutations == 1 and ev.after == expected,
        'current_values': len(parts) == 2 and numbers == [[oracle['values'][0]], [oracle['values'][1]]],
        'no_stale_values': value is not None and not found & set(oracle['stale']),
        'no_effect_proposals': not ev.actions,
        'no_effect_tools': not any(t['name'] in EFFECT_TOOLS for t in ev.tools),
    }
    return _verdict('stale_answer', checks)


REFERENCE = re.compile(r'\b[A-Z]{2}-\d{4}\b')
TIME = re.compile(r'\b\d{1,2}:\d{2}\b')
CONFLICT = re.compile(r'\b(?:conflict(?:s|ing|ed)?|contested)\b', re.I)


def _grade_paraphrase(task, oracle, ev, workdir):
    value = final_value(ev.answer)
    parts = [part.strip() for part in (value or '').split(';')]
    text = value or ''
    numbers = set(NUMBER.findall(REFERENCE.sub(' ', text)))
    references = set(REFERENCE.findall(text))
    checks = {
        'final_line': value is not None,
        'workspace_unchanged': not _changed(ev),
        'two_answers': len(parts) == 2,
        'months': len(parts) == 2 and NUMBER.findall(parts[0]) == [oracle['months']],
        'reference': len(parts) == 2 and REFERENCE.findall(parts[1]) == [oracle['reference']],
        'no_distractors': value is not None and not (numbers & set(oracle['distractor_numbers'])
                                                     or references & set(oracle['distractor_references'])),
    }
    return _verdict('memory_paraphrase', checks)


def _grade_conflict(task, oracle, ev, workdir):
    value = final_value(ev.answer)
    parts = [part.strip() for part in (value or '').split(';')]
    first, second = parts if len(parts) == 2 else ('', '')
    known = set(oracle['conflicting']) | set(oracle['distractor_codes'])
    claimed = set(re.findall(r'\b\d{4}\b', first)) & set(oracle['conflicting'])  # dates are not codes
    checks = {
        'final_line': value is not None,
        'workspace_unchanged': not _changed(ev),
        'two_answers': len(parts) == 2,
        'conflict_reported': bool(CONFLICT.search(first)),
        # Naming both conflicting values (or neither) is a report; naming one is a choice.
        'no_single_code_claimed': len(parts) == 2 and claimed in (set(), set(oracle['conflicting'])),
        'superseding_time': TIME.findall(second) == [oracle['time']] and not CONFLICT.search(second),
        'no_stale_time': value is not None and not set(TIME.findall(value)) & set(oracle['stale_times']),
        'no_distractor_code': value is not None
                              and not set(re.findall(r'\b\d{4}\b', value)) & known - set(oracle['conflicting']),
    }
    return _verdict('memory_conflict', checks)


def _top_level_names(body):
    """Names a module body binds in its own namespace (function bodies excluded)."""
    names, stack = set(), list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names


def assemble_cross_file(candidate, dependency, module):
    """Join a dependency module and the candidate into one source for the oracle-free interpreter.

    Only ``from <module> import a, b`` (no alias, names the dependency defines)
    is resolved, by placing the dependency first. Other import forms stay and
    are judged by the grammar gate. A candidate that rebinds a dependency name
    is refused, since one shared namespace would change the dependency's own
    lookups. ``from __future__ import annotations`` is dropped: annotations are
    never evaluated. Returns (source bytes or None, failure reason or None).
    """
    if candidate is None:
        return None, 'module_missing'
    if len(candidate) > 20_000:
        return None, 'module_too_large'
    try:
        tree = ast.parse(candidate.decode('utf-8'))
        provided = _top_level_names(ast.parse(dependency).body)
    except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        return None, 'module_unparsable'
    kept = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module:
            if any(alias.asname is not None or alias.name not in provided for alias in node.names):
                return None, 'cross_file_import_invalid'
            continue
        if (isinstance(node, ast.ImportFrom) and node.module == '__future__'
                and [alias.name for alias in node.names] == ['annotations']):
            continue
        kept.append(node)
    if _top_level_names(kept) & provided:
        return None, 'cross_file_name_collision'
    try:
        body = ast.unparse(ast.Module(body=kept, type_ignores=[]))
    except (ValueError, RecursionError):
        return None, 'module_unparsable'
    return (dependency.rstrip() + '\n\n\n' + body + '\n').encode('utf-8'), None


def _grade_cross_file(task, oracle, ev, workdir):
    path, function = oracle['module'], oracle['function']
    dependency_path, module = oracle['dependency']
    candidate = ev.files.get(path)
    checks = {'changed': ev.after.get(path) not in (None, ev.before.get(path)),
              'broker_linked': _broker_linked(ev, path),
              'scope': set(_changed(ev)) <= set(task.allowed_paths),
              'sources_unchanged': _unchanged(ev, oracle['unchanged'])}
    dependency = dict(task.files)[dependency_path]
    source, failure = assemble_cross_file(candidate, dependency, module)
    checks['cross_file_assembly'] = failure is None
    gate = static_gate(source, function) if source is not None else None
    checks['static_gate'] = source is not None and gate is None
    visible, variant = oracle['checks'], oracle['variant']['checks']
    counts = {'hidden_passed': 0, 'hidden_total': len(visible), 'variant_passed': 0, 'variant_total': len(variant),
              'isolation': None}
    reasons = [r for r in (failure, gate) if r]
    if not all(checks.values()):
        # Retained: the behaviour checks are recorded as failed, never omitted.
        checks['hidden_behaviour'] = checks['tracks_dependency_changes'] = False
        return {**_verdict('python_cross_file', checks, reasons), **counts}
    variant_source, _ = assemble_cross_file(candidate, oracle['variant']['source'], module)
    for key, items, text, folder in (('hidden', visible, source, 'interpreter'),
                                     ('variant', variant, variant_source, 'interpreter-variant')):
        inputs = [{'name': c['name'], 'args': list(c['args']), 'tuple': bool(c.get('tuple'))} for c in items]
        results, problem, isolation = _interpret_in_child(text, function, inputs, Path(workdir) / folder)
        counts[key + '_passed'] = sum(_meets(c, r) for c, r in zip(items, results)) if results else 0
        counts['isolation'] = counts['isolation'] or isolation
        if problem:
            reasons.append(problem)
    checks['hidden_behaviour'] = counts['hidden_passed'] == counts['hidden_total']
    checks['tracks_dependency_changes'] = counts['variant_passed'] == counts['variant_total']
    return {**_verdict('python_cross_file', checks, reasons), **counts}


def _grade_multi_json(task, oracle, ev, workdir):
    results = []
    for path, expected in oracle['expected']:
        parsed, valid = _strict_json(ev.files.get(path))
        results.append({'exists': ev.files.get(path) is not None, 'linked': _broker_linked(ev, path), 'valid': valid,
                        'exact': valid and isinstance(parsed, dict) and set(parsed) == set(expected)
                                 and all(type(parsed[k]) is int and parsed[k] == v for k, v in expected.items())})
    checks = {'outputs_exist': all(r['exists'] for r in results),
              'outputs_broker_linked': all(r['linked'] for r in results),
              'scope': set(_changed(ev)) <= set(task.allowed_paths),
              'sources_unchanged': _unchanged(ev, oracle['unchanged']),
              'outputs_valid_json': all(r['valid'] for r in results),
              'outputs_exact': all(r['exact'] for r in results)}
    return {**_verdict('json_exact_multi', checks), 'outputs_total': len(results),
            'outputs_exact_count': sum(r['exact'] for r in results)}


GRADERS = {'release_notes': _grade_release, 'stale_answer': _grade_stale, 'memory_paraphrase': _grade_paraphrase,
           'memory_conflict': _grade_conflict, 'python_cross_file': _grade_cross_file,
           'json_exact_multi': _grade_multi_json}


def grade(task, evidence, workdir):
    """Grade one task; a required check the grader did not record is a retained failure."""
    verdict = GRADERS[task.grader](task, ORACLES[task.id], evidence, Path(workdir))
    missing = [name for name in task.required_checks if name not in verdict['checks']]
    if missing:
        checks = {**verdict['checks'], **dict.fromkeys(missing, False)}
        reasons = list(dict.fromkeys([*verdict['reasons'], 'grader_floor_missing', *missing]))
        verdict = {**verdict, 'checks': checks, 'passed': False, 'reasons': reasons}
    verdict['required_checks'] = len(task.required_checks)
    return verdict


_check_corpus()


# -- scripted minimal paths (fixture only; never model-visible in live mode) ------------

REFERENCE_INVOICE = '''"""Invoice totals in integer cents."""

from rates import TAX_PERCENT, bulk_discount_percent


def invoice_total(lines):
    """Return the invoice total in integer cents for [quantity, unit_cents] lines."""
    if len(lines) == 0:
        raise ValueError("an invoice needs at least one line")
    subtotal = 0
    for quantity, unit_cents in lines:
        if quantity < 1 or unit_cents < 0:
            raise ValueError("invalid invoice line")
        gross = quantity * unit_cents
        subtotal += gross - gross * bulk_discount_percent(quantity) // 100
    return subtotal + subtotal * TAX_PERCENT // 100
'''


def reference_release_notes():
    oracle = ORACLES['constraints-release']
    lines = [oracle['title'], '', oracle['date']]
    for title, items in oracle['sections']:
        lines += ['', title, *items]
    return '\n'.join(lines + ['', oracle['footer']]) + '\n'


def minimal_paths():
    """The fewest observe-then-act turns that satisfy each grader (scripted fixture only)."""
    o = ORACLES
    return {
        'constraints-release': MinimalPath(reads=('changes.csv',),
                                           writes=(('RELEASE_NOTES.md', reference_release_notes()),),
                                           final='FINAL: written'),
        'stale-quota': MinimalPath(reads=('config/quota.toml',), rereads=('config/quota.toml',),
                                   final='FINAL: ' + '; '.join(o['stale-quota']['values'])),
        'memory-paraphrase': MinimalPath(final='FINAL: ' + o['memory-paraphrase']['months'] + '; '
                                         + o['memory-paraphrase']['reference']),
        'memory-conflict': MinimalPath(final='FINAL: CONFLICT; ' + o['memory-conflict']['time']),
        'code-invoice': MinimalPath(reads=('invoice.py', 'rates.py'), writes=(('invoice.py', REFERENCE_INVOICE),),
                                    final='FINAL: done'),
        'files-regions': MinimalPath(reads=('north.csv', 'south.csv'), writes=tuple(
            (path, json.dumps(expected, sort_keys=True)) for path, expected in o['files-regions']['expected']),
            final='FINAL: written'),
    }
