"""Phase 24 (t24-build): generated third-party notices and the inventory's 'complete' gate (V1-046), no GPL or LGPL
in the bundle (V1-045), and Pebble's own Apache-2.0 licence (V1-047).

Synthetic stages, node_modules, registries and distributions are built under tmp_path; the repository's real lock
files and node_modules are read, never written. Nothing is downloaded. The checked-in notices file is
src-tauri/resources/licenses/THIRD-PARTY-NOTICES.txt (regenerated whenever a lock file changes, as at each version).
"""
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from pebble.release_readiness import Root, bundle_digest, bundle_entries
from pebble.token_budget import TOKENIZER_SHA256

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_inventory_24', ROOT / 'scripts/release_inventory.py')
inv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inv)
APACHE = ROOT / 'backend/vendor/AGENTGUARD-LICENSE'  # the canonical Apache-2.0 text (GitHub's, 11,357 bytes)
MIT = 'MIT License\n\nCopyright (c) 2026 %s\n\nPermission is hereby granted, free of charge...\n'

LOCK = {'name': 'fixture', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True, 'packages': {
    '': {'name': 'fixture', 'version': '1.0.0'},
    'node_modules/vue': {'version': '3.5.22', 'license': 'MIT'},
    'node_modules/@vue/shared': {'version': '3.5.22', 'license': 'MIT'},
    'node_modules/@tauri-apps/api': {'version': '2.8.0', 'license': 'Apache-2.0 OR MIT'},
    'node_modules/dompurify': {'version': '3.3.0', 'license': '(MPL-2.0 OR Apache-2.0)'},
    'node_modules/vite': {'version': '7.1.9', 'dev': True, 'license': 'MIT'},
}}
CARGO_LOCK = '''version = 4

[[package]]
name = "pebble"
version = "1.0.0"

[[package]]
name = "serde"
version = "1.0.228"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "windows-sys"
version = "0.59.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
'''
REQUIREMENTS_LOCK = '# [runtime]\nPyJWT==2.15.0\ncertifi==2026.10.19\n# [build]\npyinstaller==6.22.3\n'
PYTHON = {'PyJWT': ('2.15.0', [('licenses/LICENSE', MIT % 'José Padilla')]),
          'certifi': ('2026.10.19', [('licenses/LICENSE', 'Mozilla Public License Version 2.0\n...\n')]),
          'pyinstaller': ('6.22.3', [('licenses/COPYING.txt', 'GNU GENERAL PUBLIC LICENSE\n... bootloader exception\n')])}
CPYTHON = ('3.14.5', [('LICENSE.txt', 'PSF LICENSE AGREEMENT FOR PYTHON\n... OpenSSL ... zstd ...\n')])


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def restage(resources, manifest):
    with Root(str(resources), 'stage') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems
    manifest.write_text(json.dumps({'files': [dict(e, mode='0644', source=None) for e in entries], 'links': links,
                                    'bundle_sha256': bundle_digest(entries)}))


def inputs(tmp_path):
    """The lock files, a node_modules and a cargo registry with licence files (serde's are dual)."""
    write(tmp_path / 'package-lock.json', json.dumps(LOCK))
    write(tmp_path / 'node_modules/vue/LICENSE', MIT % 'Evan You')
    write(tmp_path / 'node_modules/@vue/shared/LICENSE', MIT % 'Evan You')  # the same text as vue's
    write(tmp_path / 'node_modules/@tauri-apps/api/LICENSE_MIT', MIT % 'Tauri')
    write(tmp_path / 'node_modules/@tauri-apps/api/LICENSE_APACHE-2.0', APACHE.read_text())
    write(tmp_path / 'node_modules/dompurify/LICENSE', 'DOMPurify\nApache-2.0 and MPL-2.0\n')
    write(tmp_path / 'node_modules/vite/LICENSE.md', MIT % 'Vite')
    write(tmp_path / 'Cargo.lock', CARGO_LOCK)
    registry = tmp_path / 'cargo/registry/src/index.crates.io-0000'
    write(registry / 'serde-1.0.228/Cargo.toml', '[package]\nname = "serde"\nlicense = "MIT OR Apache-2.0"\n')
    write(registry / 'serde-1.0.228/LICENSE-MIT', MIT % 'serde')
    write(registry / 'serde-1.0.228/LICENSE-APACHE', APACHE.read_text())
    write(registry / 'windows-sys-0.59.0/Cargo.toml', '[package]\nname = "windows-sys"\nlicense = "MIT OR Apache-2.0"\n')
    write(registry / 'windows-sys-0.59.0/license-apache-2.0', APACHE.read_text())
    write(tmp_path / 'requirements.lock', REQUIREMENTS_LOCK)
    write(tmp_path / 'requirements.txt', 'PyJWT[crypto]==2.15.0\ncertifi==2026.10.19\n')
    return {'package_lock': tmp_path / 'package-lock.json', 'node_root': tmp_path, 'cargo_lock': tmp_path / 'Cargo.lock',
            'cargo_home': tmp_path / 'cargo', 'requirements_lock': tmp_path / 'requirements.lock'}


def generate(paths, **changes):
    options = dict(python_licences=lambda name: PYTHON.get(name, (None, [])), python_runtime=lambda: CPYTHON)
    options.update(changes)
    return inv.third_party_notices(**paths, **options)


def stage_with(tmp_path, notices_text, extra=()):
    """Source resources holding the generated notices and every named licence, and a stage that equals them."""
    source = tmp_path / 'src-resources'
    write(source / 'licenses' / inv.NOTICES, notices_text)
    write(source / 'licenses/bloub-LICENSE', MIT % 'Jérémy Perret')
    write(source / 'licenses/llama.cpp-LICENSE', MIT % 'The ggml authors')
    write(source / 'NOTICE.md', '# notices\nSee resources/licenses/bloub-LICENSE.\n')
    write(source / 'AGENTGUARD-LICENSE', APACHE.read_bytes())
    write(source / 'LICENSE', APACHE.read_bytes())
    resources = tmp_path / 'C/stage/resources'
    for path in source.rglob('*'):
        if path.is_file():
            write(resources / path.relative_to(source), path.read_bytes())
    for rel, data in {'backend/pebble-backend': b'backend', 'backend/_internal/Python': b'python',
                      'backend/_internal/libssl.3.dylib': b'ssl', 'backend/_internal/libcrypto.3.dylib': b'crypto',
                      'backend/_internal/PyJWT-2.15.0.dist-info/METADATA':
                          b'Metadata-Version: 2.4\nName: PyJWT\nVersion: 2.15.0\nLicense-Expression: MIT\n\n',
                      'backend/_internal/certifi-2026.10.19.dist-info/METADATA':
                          b'Metadata-Version: 2.4\nName: certifi\nVersion: 2026.10.19\nLicense: MPL-2.0\n\n',
                      'llama/llama-server': b'llama', **dict(extra)}.items():
        write(resources / rel, data)
    restage(resources, tmp_path / 'C/stage/stage-manifest.json')
    return source, resources


def run_inventory(tmp_path, paths, source, **changes):
    options = dict(stage_manifest=tmp_path / 'C/stage/stage-manifest.json', resources=source,
                   requirements=tmp_path / 'requirements.txt', package_lock=paths['package_lock'],
                   cargo_lock=paths['cargo_lock'], cargo_home=paths['cargo_home'],
                   requirements_lock=paths['requirements_lock'], metadata_lookup=lambda name: None,
                   top_levels=lambda: {})
    options.update(changes)
    return inv.inventory(**options)


def test_the_generated_notices_make_the_inventory_complete(tmp_path):
    paths = inputs(tmp_path)
    text, report = generate(paths)
    assert report['missing'] == [] and report['copyleft'] == []
    assert report['by_ecosystem'] == {'cargo': 2, 'native': 4, 'npm': 4, 'python': 2}
    assert generate(paths)[0] == text  # deterministic: no time, no path
    assert str(tmp_path) not in text
    index = inv.parse_notices(text)
    assert index['inputs']['requirements.lock'] == hashlib.sha256(REQUIREMENTS_LOCK.encode()).hexdigest()
    assert ('npm', 'vite') not in index['components']  # development packages never ship
    # Identical texts are written once: vue and @vue/shared share one notice. Each component's own set of files is
    # one text, so the Apache-2.0 text appears once per distinct set (tauri-apps/api, serde, windows-sys); the CPython
    # text also covers the OpenSSL and zstd it names.
    assert index['components'][('npm', 'vue')] == index['components'][('npm', 'vue-shared')]
    assert text.count('Evan You') == 1 and text.count('TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION') == 3
    assert index['components'][('native', 'openssl')] == index['components'][('native', 'zstd')] == \
        {None: index['components'][('native', 'cpython')]['3.14.5']}

    source, _ = stage_with(tmp_path, text)
    result = run_inventory(tmp_path, paths, source)
    assert result['findings'] == [] and result['result'] == 'complete'
    components = {(item['ecosystem'], item['name']): item for item in result['components']}
    assert components[('npm', 'vue')]['notice']['files'] == ['licenses/' + inv.NOTICES]
    assert components[('native', 'OpenSSL')]['presence'] == 'staged'
    assert components[('first-party', 'Pebble')]['notice']['files'] == ['LICENSE']
    assert components[('vendored', 'bloub')]['notice']['files'] == ['licenses/bloub-LICENSE']
    assert components[('python', 'certifi')]['notice']['sections']


def test_a_stale_or_incomplete_notices_file_is_a_gap(tmp_path):
    paths = inputs(tmp_path)
    (tmp_path / 'node_modules/dompurify/LICENSE').unlink()  # a package that ships no licence file
    text, report = generate(paths)
    assert report['missing'] == ['npm:dompurify@3.3.0']
    source, _ = stage_with(tmp_path, text)
    result = run_inventory(tmp_path, paths, source)
    assert {'code': 'notice_missing', 'subject': 'npm:dompurify@3.3.0'} in result['findings']
    assert result['result'] == 'gaps'

    # The shipped certifi moves on while the notices still name the old one.
    write(tmp_path / 'node_modules/dompurify/LICENSE', 'DOMPurify\n')
    text, _ = generate(paths)
    source, resources = stage_with(tmp_path, text)
    old = 'backend/_internal/certifi-2026.10.19.dist-info/METADATA'
    (resources / old).unlink()
    write(resources / 'backend/_internal/certifi-2026.11.1.dist-info/METADATA',
          b'Metadata-Version: 2.4\nName: certifi\nVersion: 2026.11.1\nLicense: MPL-2.0\n\n')
    restage(resources, tmp_path / 'C/stage/stage-manifest.json')
    result = run_inventory(tmp_path, paths, source)
    assert {'code': 'notice_version_differs', 'subject': 'python:certifi@2026.11.1'} in result['findings']
    assert {'code': 'notice_missing', 'subject': 'python:certifi@2026.11.1'} in result['findings']

    # A lock that changed after the notices were generated.
    write(paths['package_lock'], json.dumps(dict(LOCK, version='1.0.1')))
    result = run_inventory(tmp_path, paths, stage_with(tmp_path, text)[0])
    assert {'code': 'notices_inputs_differ', 'subject': 'package-lock.json'} in result['findings']

    # A distribution whose installed version is not the lock's is refused by the generator, not guessed.
    _, report = generate(paths, python_licences=lambda name: ('2026.7.22', PYTHON[name][1]) if name == 'certifi'
                         else PYTHON.get(name, (None, [])))
    assert report['missing'] == ['python:certifi@2026.10.19 (this interpreter has 2026.7.22; run with the build environment)']


def test_no_gpl_or_lgpl_library_ships(tmp_path):
    assert inv.blocks_distribution('GPL-3.0-only') and inv.blocks_distribution('LGPL-2.1-or-later')
    assert inv.blocks_distribution('GNU General Public License v2 (GPLv2)')
    assert not inv.blocks_distribution('GPL-2.0-or-later WITH Bootloader-exception')
    assert not inv.blocks_distribution('MIT OR Apache-2.0 OR LGPL-2.1-or-later')  # r-efi: a permissive choice
    assert not inv.blocks_distribution('(MPL-2.0 OR Apache-2.0)') and not inv.blocks_distribution('MPL-2.0')
    assert not inv.blocks_distribution('FTL / GNU General Public License v2 (GPLv2)')  # FreeType's choice
    paths = inputs(tmp_path)
    text, _ = generate(paths)
    source, _ = stage_with(tmp_path, text, extra=[('backend/_internal/pillow_heif/.dylibs/libx265.216.dylib', b'x265'),
                                                   ('backend/_internal/libde265.0.dylib', b'de265')])
    result = run_inventory(tmp_path, paths, source)
    assert [f for f in result['findings'] if f['code'] == 'copyleft_binary'] == [
        {'code': 'copyleft_binary', 'subject': 'backend/_internal/libde265.0.dylib'},
        {'code': 'copyleft_binary', 'subject': 'backend/_internal/pillow_heif/.dylibs/libx265.216.dylib'}]
    lock = json.loads(paths['package_lock'].read_text())
    lock['packages']['node_modules/readline-gpl'] = {'version': '1.0.0', 'license': 'GPL-3.0-only'}
    write(paths['package_lock'], json.dumps(lock))
    write(tmp_path / 'node_modules/readline-gpl/COPYING', 'GNU GENERAL PUBLIC LICENSE\n')
    text, report = generate(paths)
    assert report['copyleft'] == ['npm:readline-gpl@1.0.0']
    result = run_inventory(tmp_path, paths, stage_with(tmp_path, text)[0])
    assert {'code': 'copyleft_licence', 'subject': 'npm:readline-gpl@1.0.0'} in result['findings']


def test_the_notices_command_replaces_only_a_generated_file(tmp_path, capsys):
    paths = inputs(tmp_path)
    out = tmp_path / 'licenses' / inv.NOTICES
    out.parent.mkdir()
    argv = ['notices', '--out', str(out), '--package-lock', str(paths['package_lock']),
            '--node-modules', str(tmp_path / 'node_modules'), '--cargo-lock', str(paths['cargo_lock']),
            '--cargo-home', str(paths['cargo_home']), '--lock', str(paths['requirements_lock'])]
    fakes = dict(python_licences=lambda name: PYTHON.get(name, (None, [])), python_runtime=lambda: CPYTHON)
    assert inv.notices_main(argv[1:], **fakes) == 0
    first = out.read_text()
    assert json.loads(capsys.readouterr().out)['sha256'] == hashlib.sha256(first.encode()).hexdigest()
    assert inv.notices_main(argv[1:], **fakes) == 0 and out.read_text() == first  # regenerated in place
    other = tmp_path / 'licenses/hand-written.txt'
    other.write_text('a hand-written notice\n')
    assert inv.notices_main(['--out', str(other)] + argv[3:], **fakes) == 2
    assert other.read_text() == 'a hand-written notice\n'
    # With a cargo metadata file only the crates built for macOS are listed.
    metadata = write(tmp_path / 'metadata.json', json.dumps({'packages': [{'name': 'serde', 'version': '1.0.228'}]}))
    _, report = generate(paths, cargo_metadata=metadata)
    assert report['by_ecosystem']['cargo'] == 1 and report['cargo_filtered_by_metadata']


# The repository's own inputs ----------------------------------------------------------------------------------

def test_every_production_npm_package_ships_a_licence_file_in_node_modules(tmp_path):
    """The real package-lock.json production closure against the real node_modules (read only)."""
    if not (ROOT / 'node_modules').is_dir():
        pytest.skip('node_modules is not installed in this checkout')
    write(tmp_path / 'requirements.lock', '# [runtime]\n')
    common = dict(package_lock=ROOT / 'package-lock.json', node_root=ROOT,
                  cargo_lock=write(tmp_path / 'Cargo.lock', 'version = 4\n'), cargo_home=tmp_path / 'no-cargo',
                  requirements_lock=tmp_path / 'requirements.lock', python_licences=lambda name: (None, []),
                  python_runtime=lambda: CPYTHON)
    # eastasianwidth (sucrase -> glob -> jackspeak -> @isaacs/cliui -> string-width) ships no licence file; its MIT
    # text is the resources override licenses/eastasianwidth-LICENSE.
    _, bare = inv.third_party_notices(**common)
    assert [m for m in bare['missing'] if m.startswith('npm:')] == ['npm:eastasianwidth@0.3.0']
    text, report = inv.third_party_notices(**common, licenses=ROOT / 'src-tauri/resources/licenses')
    production = inv.npm_packages((ROOT / 'package-lock.json').read_bytes())
    assert [m for m in report['missing'] if m.startswith('npm:')] == []
    assert report['by_ecosystem']['npm'] == len(production) > 40
    assert [m for m in report['copyleft'] if m.startswith('npm:')] == []
    assert '\tnpm\teastasianwidth\t0.3.0\tMIT\teastasianwidth-LICENSE' in text


def test_every_shipped_python_distribution_has_a_licence_file_and_none_is_copyleft():
    """The runtime section of backend/requirements.lock against this interpreter's distributions (by name: the
    build environment holds the locked versions of the same distributions)."""
    pins = inv.lock_runtime_pins((ROOT / 'backend/requirements.lock').read_text())
    assert len(pins) == 30 and ('certifi', '2026.10.19') in pins and ('Pillow', '13.0.0') in pins
    assert ('PyJWT', '2.15.0') in pins and not [name for name, _ in pins if 'heif' in name.lower()]
    without = [name for name, _ in pins if not inv.environment_licences(name)[1]]
    assert without == []
    blocked = [name for name, _ in pins if inv.blocks_distribution(inv.declared(inv.environment_metadata(name))[0])]
    assert blocked == []


def test_every_locked_crate_has_a_licence_file_in_the_registry():
    """The real Cargo.lock against the parent's offline cargo registry (absent from a worker's copy)."""
    cargo_home = ROOT / '.runtime/toolchain/cargo'
    if not (cargo_home / 'registry/src').is_dir():
        pytest.skip('no local cargo registry in this checkout (the parent runs this)')
    crates, _ = inv.cargo_crates((ROOT / 'src-tauri/Cargo.lock').read_bytes(), cargo_home)
    assert [c['name'] for c in crates if not c['licence']] == []
    assert [c['name'] for c in crates if inv.blocks_distribution(c['licence'])] == []


def test_the_generated_notices_file_when_present_matches_the_current_locks():
    path = ROOT / 'src-tauri/resources/licenses' / inv.NOTICES
    if not path.is_file():
        pytest.skip('THIRD-PARTY-NOTICES.txt is generated by the parent (scripts/release_inventory.py notices)')
    index = inv.parse_notices(path.read_text())
    assert index is not None and index['components']
    for label, rel in (('package-lock.json', 'package-lock.json'), ('Cargo.lock', 'src-tauri/Cargo.lock'),
                       ('requirements.lock', 'backend/requirements.lock')):
        assert index['inputs'][label] == hashlib.sha256((ROOT / rel).read_bytes()).hexdigest(), label


def test_pebble_ships_its_own_apache_licence_and_bloub_keeps_its_mit_text():
    resources = ROOT / 'src-tauri/resources'
    licence = (resources / 'LICENSE').read_bytes()
    assert licence == APACHE.read_bytes() == (resources / 'AGENTGUARD-LICENSE').read_bytes() and len(licence) == 11357
    assert licence.startswith(b'                                 Apache License\n') and b'Version 2.0, January 2004' in licence
    if (ROOT / 'LICENSE').is_file():  # t24-publish's root LICENSE: the same bytes
        assert (ROOT / 'LICENSE').read_bytes() == licence
    assert (resources / 'licenses/bloub-LICENSE').read_bytes() == (ROOT / 'src/vendor/bloub/LICENSE').read_bytes()
    assert 'Jérémy Perret' not in licence.decode()


def gate_inputs(tmp_path, **changes):
    """Reports as the build and the scanners write them: everything clean and bound to one lock."""
    lock = write(tmp_path / 'requirements.lock', REQUIREMENTS_LOCK)
    lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    scan = {'files_scanned': 412, 'machine_specific': 0, 'users_prefix': 0}
    docs = {
        'inventory': {'format': 'pebble.release-inventory/1', 'result': 'complete', 'findings': [],
                      'summary': {'components': 600, 'by_ecosystem': {'cargo': {'components': 444}}},
                      'inputs': {'third_party_notices': {'inputs': {'requirements.lock': lock_sha}}}},
        'audit': {'dependencies': [{'name': 'requests', 'version': '2.34.2', 'vulns': []},
                                   {'name': 'pyjwt', 'version': '2.15.0',
                                    'vulns': [{'id': 'GHSA-xxxx-pyjwk', 'aliases': ['CVE-2026-0001'], 'fix_versions': []}]}]},
        'osv': {'results': [{'packages': [{'package': {'name': 'Pillow', 'version': '13.0.0', 'ecosystem': 'PyPI'},
                                           'vulnerabilities': [{'id': 'PYSEC-2026-9999', 'aliases': []}]}]}]},
        'triage': {'format': 'pebble.audit-triage/1', 'lock_sha256': lock_sha, 'entries': {
            'CVE-2026-0001': {'verdict': 'not_affected', 'reason': 'PyJWKClient is never used (chatgpt.py decodes with a pinned key).'},
            'PYSEC-2026-9999': {'verdict': 'accepted_risk', 'reason': 'Reachable only in the confined parser.'}}},
        'gitleaks': [],
        'stage': {'schema': 'pebble.candidate-stage/1', 'machine_paths': scan},
        'app': {'apps': {'release': {'shell_machine_paths': scan}, 'test': {'shell_machine_paths': scan}}},
    }
    docs.update(changes)
    paths = {name: write(tmp_path / (name + '.json'), json.dumps(doc)) for name, doc in docs.items()}
    return dict(inventory_report=paths['inventory'], lock=lock, audit_reports=(paths['audit'], paths['osv']),
                audit_triage=paths['triage'], gitleaks_report=paths['gitleaks'], stage_manifest=paths['stage'],
                app_manifest=paths['app'])


def test_the_release_gates_pass_only_on_clean_reports_bound_to_the_lock(tmp_path):
    from pebble import release_readiness as rr
    result = rr.release_gates(**gate_inputs(tmp_path))
    assert result['passed'] and {gate: item['status'] for gate, item in result['gates'].items()} == dict.fromkeys(rr.RELEASE_GATES, 'passed')
    assert result['gates']['dependency_audit']['observed'] == {'reports': 2, 'vulnerabilities': 2,
                                                              'triaged': {'accepted_risk': 1, 'not_affected': 1}}
    # Readiness results keep their shape: the release gates are not readiness gates.
    assert not set(rr.RELEASE_GATES) & set(rr.GATE_VERSIONS)

    def statuses(**changes):
        found = rr.release_gates(**gate_inputs(tmp_path / str(len(list(tmp_path.iterdir()))), **changes))
        return found, {gate: item for gate, item in found['gates'].items() if item['status'] != 'passed'}

    found, failed = statuses(triage={'format': 'pebble.audit-triage/1', 'lock_sha256': '0' * 64, 'entries': {}})
    assert not found['passed'] and set(failed) == {'dependency_audit'}
    codes = {issue['code']: issue['subjects'] for issue in failed['dependency_audit']['issues']}
    assert codes == {'triage_for_another_lock': [],
                     'untriaged_vulnerability': ['Pillow@13.0.0:PYSEC-2026-9999', 'pyjwt@2.15.0:GHSA-xxxx-pyjwk']}

    leak = [{'RuleID': 'generic-api-key', 'File': '/Users/someone/stage/resources/model.json', 'Secret': 'sk-live-REAL',
             'Match': 'key=sk-live-REAL', 'Line': 'key=sk-live-REAL'}]
    found, failed = statuses(gitleaks=leak)
    assert set(failed) == {'secret_scan'} and failed['secret_scan']['issues'] == [
        {'code': 'secret_found', 'count': 1, 'subjects': ['generic-api-key:model.json']}]
    assert 'sk-live' not in json.dumps(found) and '/Users/' not in json.dumps(found)

    bad = {'files_scanned': 412, 'machine_specific': 1, 'users_prefix': 3}
    found, failed = statuses(app={'apps': {'release': {'shell_machine_paths': bad}}})
    assert set(failed) == {'machine_paths'}
    assert failed['machine_paths']['issues'] == [{'code': 'machine_paths_present', 'count': 1, 'subjects': ['release']}]
    found, failed = statuses(stage={'schema': 'pebble.candidate-stage/1'})
    assert failed['machine_paths']['issues'][0]['code'] == 'machine_scan_missing'

    gaps = {'format': 'pebble.release-inventory/1', 'result': 'gaps', 'summary': {},
            'findings': [{'code': 'notice_missing', 'subject': 'npm:vue@3.5.22'}], 'inputs': {}}
    found, failed = statuses(inventory=gaps)
    assert {issue['code'] for issue in failed['third_party_notices']['issues']} == {
        'inventory_not_complete', 'inventory_notice_missing', 'notices_not_from_this_lock'}

    nothing = rr.release_gates()
    assert not nothing['passed'] and {item['status'] for item in nothing['gates'].values()} == {'unverified'}


def test_the_notice_names_the_licence_tokenizer_and_base_model_and_no_missing_file():
    text = (ROOT / 'src-tauri/resources/NOTICE.md').read_text()
    assert 'Copyright 2026 Tohin Majumder' in text and 'Apache License, Version 2.0' in text
    assert TOKENIZER_SHA256 in text and 'o200k_base.tiktoken' in text and 'Licence basis' in text
    assert 'Qwen3.5-4B' in text and 'FrogNano-4B-2609' in text
    assert 'tohin03' not in text and 'licenses/HEIF' not in text and 'pillow-heif' not in text  # D10, V1-045
    for reference in re.findall(r'resources/licenses/([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])', text):
        assert (ROOT / 'src-tauri/resources/licenses' / reference).is_file(), reference
    assert '/Users/' not in text
