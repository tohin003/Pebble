#!/usr/bin/env python3
"""Phase 26 service validator: the 1.0.1 release ships no component without its notice.

Phase 25 proved the 1.0 release build (reports/harness-phase25-review.json). Its publish probe then refused: the frozen
backend carried setuptools (MIT) and its runtime hook with no notice, because urllib3's optional `backports.zstd`
import resolved to setuptools' vendored backports; and release_inventory counted every crate of Cargo.lock, while the
notices file is generated from the macOS build's crates only, so 191 crates never built for macOS (and 8 differing
versions) read as missing notices. The fix changed build/pebble-backend.spec, scripts/package_backend.py and
scripts/release_inventory.py (tested sources), so it is proved in its own namespace. Checks, the same in source and
frozen modes:
  - the backend leaves the setuptools family out: in source mode the spec and the packaging flags exclude it (and keep
    `backports` importable, which PyInstaller's alias hook requires); in frozen mode, as well, the executable's archive
    holds no module of the family and no setuptools runtime hook, and its _internal folder no setuptools data;
  - the inventory, given the same `cargo metadata --filter-platform` output the notices are generated from, counts only
    the crates the macOS build resolves, names the rest as off-platform, and still requires a notice for every crate it
    counts (a synthetic stage in this process; in frozen mode it proves the source, as validate_phase25's guard check);
  - the release version is after 1.0.0 (never published) and every declaration agrees.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import importlib.util
import json
import os
import re
import runpy
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

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'backend_ships_no_setuptools_family',
    'inventory_counts_only_the_macos_build',
    'a_counted_crate_still_needs_its_notice',
    'release_version_is_after_1_0_0',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('build/pebble-backend.spec', 'scripts/package_backend.py', 'scripts/release_inventory.py',
         'scripts/validate_phase26.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/tauri.conf.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
FAMILY = ('setuptools', 'pkg_resources', '_distutils_hack', 'distutils')
MIT = 'MIT License\n\nCopyright (c) 2026 %s\n\nPermission is hereby granted, free of charge...\n'
APACHE = 'Apache License\nVersion 2.0, January 2004\n\nTERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION\n'
CARGO_LOCK = ('version = 4\n\n[[package]]\nname = "pebble"\nversion = "1.0.1"\n\n'
              '[[package]]\nname = "serde"\nversion = "1.0.228"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n\n'
              '[[package]]\nname = "windows-sys"\nversion = "0.59.0"\n'
              'source = "registry+https://github.com/rust-lang/crates.io-index"\n')
REQUIREMENTS_LOCK = '# [runtime]\nPyJWT==2.15.0\ncertifi==2026.10.19\n# [build]\npyinstaller==6.22.3\n'
PYTHON = {'PyJWT': ('2.15.0', [('licenses/LICENSE', MIT % 'PyJWT')]),
          'certifi': ('2026.10.19', [('licenses/LICENSE', 'Mozilla Public License Version 2.0\n...\n')]),
          'pyinstaller': ('6.22.3', [('licenses/COPYING.txt', 'GNU GENERAL PUBLIC LICENSE\n... bootloader exception\n')])}
CPYTHON = ('3.14.5', [('LICENSE.txt', 'PSF LICENSE AGREEMENT FOR PYTHON\n... OpenSSL ... zstd ...\n')])


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def spec_excluded():
    text = (ROOT / 'build/pebble-backend.spec').read_text()
    return set(re.findall(r"'([A-Za-z0-9_.]+)'", text.split('EXCLUDED =')[1].split('LANGUAGES')[0]))


def flag_excluded():
    flags = runpy.run_path(str(ROOT / 'scripts/package_backend.py'))['PYINSTALLER_FLAGS']
    return {flags[i + 1] for i, flag in enumerate(flags) if flag == '--exclude-module'}


def frozen_contents(executable):
    """(runtime hooks, archive modules) of a PyInstaller onedir executable, read with PyInstaller's own readers."""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    archive = CArchiveReader(str(executable))
    names = list(archive.toc)
    pyz = [name for name in names if name.endswith('.pyz')]
    assert pyz, 'the executable holds no Python archive'
    modules = set()
    with tempfile.TemporaryDirectory(prefix='pebble-phase26-pyz-') as folder:
        for name in pyz:
            path = Path(folder) / 'archive.pyz'
            path.write_bytes(archive.extract(name))
            modules |= set(ZlibArchiveReader(str(path)).toc)
    return [name for name in names if name.startswith('pyi_rth')], modules


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def inventory_fixture(inv, scratch):
    """Lock files, a cargo registry, notices generated with the macOS filter (serde only), and a stage holding them."""
    write(scratch / 'package-lock.json', json.dumps({'name': 'fixture', 'version': '1.0.1', 'lockfileVersion': 3,
                                                     'requires': True, 'packages': {'': {'name': 'fixture'}}}))
    write(scratch / 'Cargo.lock', CARGO_LOCK)
    registry = scratch / 'cargo/registry/src/index.crates.io-0000'
    write(registry / 'serde-1.0.228/Cargo.toml', '[package]\nname = "serde"\nlicense = "MIT OR Apache-2.0"\n')
    write(registry / 'serde-1.0.228/LICENSE-MIT', MIT % 'serde')
    write(registry / 'windows-sys-0.59.0/Cargo.toml', '[package]\nname = "windows-sys"\nlicense = "MIT OR Apache-2.0"\n')
    write(registry / 'windows-sys-0.59.0/license-apache-2.0', APACHE)
    write(scratch / 'requirements.lock', REQUIREMENTS_LOCK)
    write(scratch / 'requirements.txt', 'PyJWT[crypto]==2.15.0\ncertifi==2026.10.19\n')
    macos = write(scratch / 'macos-metadata.json', json.dumps({'version': 1, 'packages': [
        {'name': 'pebble', 'version': '1.0.1'}, {'name': 'serde', 'version': '1.0.228'}]}))
    both = write(scratch / 'listing-metadata.json', json.dumps({'version': 1, 'packages': [
        {'name': 'pebble', 'version': '1.0.1'}, {'name': 'serde', 'version': '1.0.228'},
        {'name': 'windows-sys', 'version': '0.59.0'}]}))
    text, report = inv.third_party_notices(
        package_lock=scratch / 'package-lock.json', node_root=scratch, cargo_lock=scratch / 'Cargo.lock',
        cargo_home=scratch / 'cargo', requirements_lock=scratch / 'requirements.lock', cargo_metadata=macos,
        python_licences=lambda name: PYTHON.get(name, (None, [])), python_runtime=lambda: CPYTHON)
    assert report['missing'] == [] and report['cargo_filtered_by_metadata'], report
    source = scratch / 'src-resources'
    write(source / 'licenses' / inv.NOTICES, text)
    write(source / 'licenses/bloub-LICENSE', MIT % 'bloub')
    write(source / 'licenses/llama.cpp-LICENSE', MIT % 'The ggml authors')
    write(source / 'NOTICE.md', '# notices\nSee resources/licenses/bloub-LICENSE.\n')
    write(source / 'AGENTGUARD-LICENSE', APACHE)
    write(source / 'LICENSE', APACHE)
    resources = scratch / 'C/stage/resources'
    for path in source.rglob('*'):
        if path.is_file():
            write(resources / path.relative_to(source), path.read_bytes())
    for rel, data in {'backend/pebble-backend': b'backend', 'backend/_internal/Python': b'python',
                      'backend/_internal/libssl.3.dylib': b'ssl', 'backend/_internal/libcrypto.3.dylib': b'crypto',
                      'backend/_internal/PyJWT-2.15.0.dist-info/METADATA':
                          b'Metadata-Version: 2.4\nName: PyJWT\nVersion: 2.15.0\nLicense-Expression: MIT\n\n',
                      'backend/_internal/certifi-2026.10.19.dist-info/METADATA':
                          b'Metadata-Version: 2.4\nName: certifi\nVersion: 2026.10.19\nLicense: MPL-2.0\n\n',
                      'llama/llama-server': b'llama'}.items():
        write(resources / rel, data)
    from pebble.release_readiness import Root, bundle_digest, bundle_entries  # on the path once inv is loaded
    with Root(str(resources), 'stage') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems, problems
    write(scratch / 'C/stage/stage-manifest.json', json.dumps({
        'files': [dict(e, mode='0644', source=None) for e in entries], 'links': links,
        'bundle_sha256': bundle_digest(entries)}))

    def run(metadata=None):
        return inv.inventory(scratch / 'C/stage/stage-manifest.json', resources=source,
                             requirements=scratch / 'requirements.txt', package_lock=scratch / 'package-lock.json',
                             cargo_lock=scratch / 'Cargo.lock', cargo_home=scratch / 'cargo',
                             requirements_lock=scratch / 'requirements.lock', metadata_lookup=lambda name: None,
                             top_levels=lambda: {}, cargo_metadata=metadata)
    return run, macos, both


class Campaign:
    def __init__(self, scratch, backend=None):
        self.scratch, self.backend, self.results = scratch, backend, []

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
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

        def no_setuptools():
            spec, flags = spec_excluded(), flag_excluded()
            assert set(FAMILY) <= spec and set(FAMILY) <= flags, ('not excluded', sorted(set(FAMILY) - (spec & flags)))
            assert 'backports' not in spec and 'backports' not in flags, 'PyInstaller refuses an excluded alias target'
            evidence = {'spec_excludes': sorted(FAMILY), 'flags_exclude': sorted(FAMILY)}
            if self.backend is not None:
                hooks, modules = frozen_contents(self.backend)
                family = sorted(m for m in modules if m.split('.')[0] in FAMILY)
                assert not family, ('the frozen archive holds', family[:10])
                assert 'pyi_rth_setuptools' not in hooks, hooks
                internal = self.backend.parent / '_internal'
                data = sorted(p.name for p in internal.iterdir() if p.name.split('-')[0] in FAMILY) if internal.is_dir() else []
                assert not data, ('the _internal folder holds', data)
                evidence.update(archive_modules=len(modules), runtime_hooks=sorted(hooks), family_modules=0)
            return evidence
        self.check('backend_ships_no_setuptools_family', no_setuptools)

        inv = load('release_inventory_for_phase26', 'scripts/release_inventory.py')
        holder = {}

        def macos_only():
            run, macos, both = inventory_fixture(inv, scratch / 'inventory')
            holder.update(run=run, both=both)
            unfiltered = run()
            assert unfiltered['findings'] == [{'code': 'notice_missing', 'subject': 'cargo:windows-sys@0.59.0'}], \
                unfiltered['findings']
            filtered = run(macos)
            assert filtered['result'] == 'complete' and filtered['findings'] == [], filtered['findings']
            cargo = sorted(c['name'] for c in filtered['components'] if c['ecosystem'] == 'cargo')
            record = filtered['inputs']['cargo_metadata']
            assert cargo == ['serde'] and record['off_platform'] == ['windows-sys@0.59.0'], (cargo, record)
            assert record['sha256'] == inv.sha_bytes(macos.read_bytes())
            return {'unfiltered_gap': 'windows-sys@0.59.0', 'filtered': 'complete', 'off_platform': 1}
        self.check('inventory_counts_only_the_macos_build', macos_only)

        def still_needs_notice():
            assert holder, 'the inventory fixture was not built'
            listed = holder['run'](holder['both'])
            assert listed['result'] == 'gaps', listed['result']
            assert {'code': 'notice_missing', 'subject': 'cargo:windows-sys@0.59.0'} in listed['findings']
            assert listed['inputs']['cargo_metadata']['off_platform'] == []
            return {'listed_crate_without_notice': 'reported'}
        self.check('a_counted_crate_still_needs_its_notice', still_needs_notice)

        def version():
            current = checkout_version()
            parts = tuple(int(x) for x in current.split('.'))
            assert parts > (1, 0, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_after_1_0_0', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase26-')).resolve()
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
