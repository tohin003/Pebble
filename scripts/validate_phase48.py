#!/usr/bin/env python3
"""Phase 48 service validator: 2.0.0's About credit and the iPhone app's wording (D222; .runtime/phase48-plan.md).

Phase 47 proved 2.0.0. With the go for the push, the person asked (D222) that About's line under Pebble's version keep
AgentGuard but drop bloub's name, with bloub credited in smaller type in a corner, and that the notes say the iPhone app
will not be on the App Store and is offered only as source code. These checks read the checkout's release text, the
same in source and frozen modes:
  - About's line under the version names AgentGuard and not bloub;
  - bloub is credited once, in smaller type than that line but not under the app's 11 px floor (V1-119), at the bottom
    right of About's row;
  - the 2.0.0 notes say in the intro, the Pebble Remote section and Known limitations that the iPhone app won't be on
    the App Store and will be offered only as source code, that the code is not published yet, and nothing else about
    the App Store or TestFlight.
validate_phase47 (the version, the figures, "beats", What's new as the Highlights) still runs beside these.
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
from validate_phase47 import RELEASE, changelog_entry, flat, read, section  # noqa: E402

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'about_names_agentguard_under_the_version',
    'bloub_is_credited_in_smaller_type_in_the_corner',
    'the_iphone_app_is_offered_only_as_source_code',
    'backend_inputs_stable_during_campaign',
)
READS = ('CHANGELOG.md', 'src/App.vue', 'src/style.css', 'scripts/validate_phase47.py', 'scripts/validate_phase48.py')
LINE = '<small>Reliability: AgentGuard (Apache 2.0).</small>'
CREDIT = ('<small class="about-credit">Avatar engine: bloub by J\N{LATIN SMALL LETTER E WITH ACUTE}r'
          '\N{LATIN SMALL LETTER E WITH ACUTE}my Perret (MIT)</small>')
NOT_ON_STORE = "won't be on the App Store"
ONLY_CODE = 'offered only as source code'
# The app's smallest type (V1-119: no type under 11 px in the owned surfaces, src/Phase24Shell.test.mjs).
FLOOR = 11


def about_block(app):
    """About's row in Settings > General: from its class to the What's new details after it."""
    match = re.search(r'class="about-pebble">(.*?)</div>\s*(?:<!--[^\n]*-->\s*)?<details[^>]*class="whats-new"', app, re.S)
    assert match, 'App.vue has no About row before What\'s new'
    return match.group(1)


def rule(css, selector):
    """A top-level rule's declarations in style.css, by its exact selector."""
    match = re.search(r'^' + re.escape(selector) + r' \{\n(.*?)^\}', css, re.M | re.S)
    assert match, 'style.css has no rule for ' + selector
    return dict(re.findall(r'^\s*([a-z-]+):\s*([^;]+);', match.group(1), re.M))


def pixels(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)px', value.strip())
    assert match, ('not a pixel size', value)
    return float(match.group(1))


def check_line(app):
    block = about_block(app)
    column = re.search(r'<div>\s*<strong>Pebble (.*?)</div>', block, re.S)
    assert column, 'About has no text column under the version'
    text = column.group(1)
    assert LINE in text, ('the line under the version does not name AgentGuard', flat(text)[-160:])
    assert not re.search(r'bloub|Perret', text, re.I), ('bloub is still named under the version', flat(text)[-160:])
    return {'line': LINE}


def check_credit(app, css):
    block = about_block(app)
    column_end = block.index('</div>')
    after = block[column_end + len('</div>'):]
    assert CREDIT in after, ('bloub is not credited after the text column', flat(after)[:200])
    assert after.rstrip().endswith(CREDIT), 'the credit is not the row\'s last element'
    named = len(re.findall(r'bloub', app, re.I))
    assert named == 1, ('App.vue names bloub other than in the credit', named)
    panel, line, credit = rule(css, '.about-pebble'), rule(css, '.about-pebble small'), rule(css, '.about-pebble .about-credit')
    assert panel.get('display') == 'flex', ('About\'s row is not a flex row', panel)
    small, smaller = pixels(line['font-size']), pixels(credit['font-size'])
    assert smaller < small, ('the credit is not in smaller type', smaller, small)
    assert smaller >= FLOOR, ('the credit is under the app\'s smallest type (V1-119)', smaller)
    margin = credit.get('margin', '').split()
    assert credit.get('align-self') == 'flex-end' and credit.get('text-align') == 'right', credit
    assert credit.get('margin-left') == 'auto' or (len(margin) == 4 and margin[3] == 'auto'), ('not pushed right', credit)
    return {'line_px': small, 'credit_px': smaller, 'corner': 'bottom right'}


def check_iphone(entry):
    intro = flat(entry[:entry.index('### Highlights')])
    start = entry.index('#### Pebble Remote\n')
    remote = flat(entry[start:entry.index('\n#### ', start + 1)])
    limits = flat(section(entry, 'Known limitations'))
    for name, part in (('the intro', intro), ('Pebble Remote', remote), ('Known limitations', limits)):
        assert NOT_ON_STORE in part and ONLY_CODE in part, (name, part[-300:])
    assert 'not published yet' in remote and 'not published yet' in limits, 'the notes do not say the code is unpublished'
    text = flat(entry)
    assert text.count('App Store') == text.count(NOT_ON_STORE) == 3, ('another App Store mention', text.count('App Store'))
    assert not re.search(r'TestFlight', text, re.I), 'the notes offer the app through TestFlight'
    return {'places': ['intro', 'Pebble Remote', 'Known limitations']}


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

        app, css = read('src/App.vue'), read('src/style.css')
        self.check('about_names_agentguard_under_the_version', lambda: check_line(app))
        self.check('bloub_is_credited_in_smaller_type_in_the_corner', lambda: check_credit(app, css))
        entry = changelog_entry(RELEASE, read('CHANGELOG.md'))
        self.check('the_iphone_app_is_offered_only_as_source_code', lambda: check_iphone(entry))


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase48-', dir='/private/tmp')).resolve()
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
