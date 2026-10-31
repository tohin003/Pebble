#!/usr/bin/env python3
"""Phase 47 service validator: 2.0.0, the first public release since 1.5.0 (D219, D220; .runtime/phase47-plan.md).

2.0.0 is 1.9.29's source with its six version declarations, the third-party notices and the release notes changed
(D219). The release notes report the held-out comparison registered before its first cell (golden set 2.0,
registered-plan-2.0.0.json amended entry 15; results in .runtime/phase46-dev/golden/rerun-1.9.29-results.md). These
checks read the checkout's release text, the same in source and frozen modes:
  - every version declaration and pebble.VERSION say 2.0.0;
  - the notes carry the measured figures exactly as the gate reports give them (fixed here, so a changed figure cannot
    pass by changing both; backend/tests/test_validate_phase47.py holds them to the reports where the records exist);
  - "beats" is claimed only where the plan's rule allows it: a family whose paired 95 % lower bound is above 0;
  - the notes name every family where Pebble trails, the one unsafe cell, the release rule (D219), gate 6 and the probes;
  - no pending placeholder, no saving or speed claim beyond the measured figures, no Context Warping (D55), Stealth
    Lockdown with its honest line;
  - About's What's new is 2.0.0's Highlights.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402

RELEASE = '2.0.0'
PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'release_version_is_2_0_0_everywhere',
    'the_notes_carry_the_measured_figures',
    'beats_is_claimed_only_where_the_plan_allows',
    'the_notes_name_what_trails_the_unsafe_cell_and_the_release_rule',
    'the_notes_claim_nothing_unmeasured',
    'whats_new_is_2_0_0s_highlights',
    'backend_inputs_stable_during_campaign',
)
READS = ('CHANGELOG.md', 'src/App.vue', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json', 'scripts/validate_phase47.py')
# The registered results (A-pebble-4.gates.json, B-pebble-5.gates.json and the analysis behind them, 2026-10-31), as the
# notes print them: each string must appear in the 2.0.0 entry, whitespace collapsed.
FIGURES = (
    '| Tasks done correctly | 235 of 255 (92.2 %) | 213 of 255 (83.5 %) |',
    '| Said done when it was not | 6.3 % | 13.7 % |',
    '| Cost per task | $0.091 | $0.117 |',
    '| Time per task: median, 95th percentile | 12.1 s, 46.5 s | 12.9 s, 40.2 s |',
    '| Tasks done correctly | 243 of 255 (95.3 %) | 231 of 255 (90.6 %) |',
    '| Said done when it was not | 3.9 % | 7.1 % |',
    '| Cost per task | $0.042 | $0.050 |',
    '| Time per task: median, 95th percentile | 20.0 s, 62.9 s | 21.1 s, 54.6 s |',
    '+8.6 points (+3.1 to +14.9) against Claude Code',
    '+4.7 points (+0.4 to +9.4) against Codex',
)
# The plan's rule ("beats" only family by family where the paired 95 % lower bound is above 0), per competitor.
BEATS = {'Claude Code': ('research_frozen', 'workshop_repeat'), 'Codex': ()}
FAMILIES = ('clarification', 'coding_build', 'coding_fix', 'companion_desktop', 'documents_data', 'git_workflow',
            'long_horizon', 'memory', 'multi_turn', 'parallel_work', 'recovery', 'repo_navigation', 'research_frozen',
            'research_live', 'safety_actions', 'safety_injection', 'workshop_repeat')
TRAILS = ('`memory`, 14 of 15 against 15', '`long_horizon`, 11 against 12')
PENDING = ('GATE6-PENDING', 'PROBES-PENDING', '<RESULTS')
CLAIM = re.compile(r'\d+(?:\.\d+)?\s?(?:%|x|times)\s*(?:cheaper|less|lower(?!\s+bound)|faster|quicker|saving|saved|off|smaller|fewer)\b'
                   r'|\bcheaper\b|\bsav(?:es|ing|ings)\s+(?:you\s+)?(?:money|costs?|time|tokens)\b', re.I)
PRAISE = re.compile(r'\boutperform\w*|\bsuperior\b|\bbetter than\b|\bbest\b|\bunbeat\w*|\bundetectable\b', re.I)


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def flat(text):
    return re.sub(r'\s+', ' ', text)


def changelog_entry(version, text=None):
    text = read('CHANGELOG.md') if text is None else text
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return match.group(1)


def section(entry, heading):
    """The text under a "### heading" of an entry, up to its next "### " heading."""
    start = entry.index('### ' + heading + '\n')
    end = entry.find('\n### ', start + 1)
    return entry[start:end if end >= 0 else len(entry)]


def bullet(entry, lead):
    """A top-level bullet of an entry that starts with **lead**, with its nested lines, whitespace collapsed."""
    match = re.search(r'^- \*\*' + re.escape(lead) + r'.*?(?=^- |^### |\Z)', entry, re.M | re.S)
    assert match, 'the notes have no bullet starting ' + lead
    return flat(match.group(0))


def check_figures(entry):
    text = flat(entry)
    missing = [figure for figure in FIGURES if figure not in text]
    assert not missing, ('the 2.0.0 notes lack the measured figures', missing)
    return {'figures': len(FIGURES)}


def check_beats(entry):
    text = flat(entry)
    words = re.findall(r'\bbeat\w*', text, re.I)
    assert len(words) == 1, ('"beats" must be claimed once, in its bullet', words)
    claim = bullet(entry, 'Where Pebble beats a product.')
    assert 'The plan allows the word only for a family whose 95 % lower bound is above zero.' in claim, claim[:200]
    for rival, families in BEATS.items():
        line = re.search(r'Against ' + re.escape(rival) + r':(.*?)(?=Against |\Z)', claim)
        assert line, ('the beats bullet has no line for', rival)
        named = {f for f in FAMILIES if '`' + f + '`' in line.group(1)}
        assert named == set(families), (rival, sorted(named), families)
        assert families or 'no single family' in line.group(1), (rival, line.group(1))
    praise = PRAISE.findall(text)
    assert not praise, ('the notes praise beyond the plan', praise)
    return {'claimed': {rival: list(f) for rival, f in BEATS.items()}}


def check_named(entry):
    text = flat(entry)
    trails = bullet(entry, 'Where Pebble trails')
    assert all(t in trails for t in TRAILS), ('the notes do not name every family where Pebble trails', trails)
    safety = bullet(entry, 'Safety.')
    assert 'Neither product had an unsafe outcome in the two safety families' in safety, safety
    assert 'graded unsafe for Pebble; Claude Code had none' in safety, safety
    rule = bullet(entry, 'The release rule.')
    assert 'D219' in rule and 'every family within three tasks' in rule, rule
    gates = bullet(entry, "The plan's gates.")
    assert 'All five gates measured on these runs were met against both products' in gates, gates
    assert 'corpus 24a' in text and 'Pebble 1.0.4' in text, 'gate 6 (corpus 24a) is not reported'
    assert re.search(r'\bprobes\b', text), 'the probes are not reported'
    pending = [p for p in PENDING if p in entry]
    assert not pending, ('the notes still hold a placeholder', pending)
    return {'trails': list(TRAILS), 'rule': 'D219'}


def check_claims(entry):
    text = flat(entry)
    claims = CLAIM.findall(text)
    assert not claims, ('the 2.0.0 notes claim a saving or speed beyond the measured figures', claims)
    assert 'Context Warping' not in text, 'Context Warping is named before its gate (D55)'
    assert 'not from a camera' in text, 'Stealth Lockdown is described without its honest line'
    return {'checked': ['saving or speed claims', 'Context Warping', 'Stealth Lockdown']}


def check_whats_new(entry, app):
    version = re.search(r'const WHATS_NEW_VERSION = "([^"]+)";', app)
    listed = re.search(r'const WHATS_NEW = Object\.freeze\(\[([\s\S]*?)\]\);', app)
    assert version and version.group(1) == RELEASE, ("About's What's new names", version and version.group(1))
    lines = [json.loads('"' + m + '"') for m in re.findall(r'"((?:[^"\\]|\\.)*)"', listed.group(1))]
    highlights = re.findall(r'^- (.+)$', section(entry, 'Highlights'), re.M)
    assert len(lines) == 3 and lines == [h.strip() for h in highlights], (lines, highlights)
    return {'lines': len(lines)}


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch, backend=None, only=None):
        self.scratch, self.backend, self.results, self.only = scratch, backend, [], only

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        if self.only is not None and name not in self.only:
            return
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def version():
            current = checkout_version()
            assert current == RELEASE, current
            declared = declared_versions()
            assert declared and set(declared.values()) == {RELEASE}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_2_0_0_everywhere', version)

        entry = changelog_entry(RELEASE)
        self.check('the_notes_carry_the_measured_figures', lambda: check_figures(entry))
        self.check('beats_is_claimed_only_where_the_plan_allows', lambda: check_beats(entry))
        self.check('the_notes_name_what_trails_the_unsafe_cell_and_the_release_rule', lambda: check_named(entry))
        self.check('the_notes_claim_nothing_unmeasured', lambda: check_claims(entry))
        self.check('whats_new_is_2_0_0s_highlights', lambda: check_whats_new(entry, read('src/App.vue')))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase47-', dir='/private/tmp')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before == after and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
