"""Phase 20 T5: scripts/release_inventory.py on owned temporary stages, plus the repository's real lockfiles.

The stage, notice folders and cargo registry are built under pytest's tmp_path; build-environment metadata
is injected. Nothing is downloaded and nothing under src-tauri/resources is written.
"""

import hashlib
import importlib.util
import json
from email.message import Message
from pathlib import Path

import pytest

from pebble.release_readiness import Root, bundle_digest, bundle_entries

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_inventory', ROOT / 'scripts/release_inventory.py')
inv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inv)

LOCK = {'name': 'fixture', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True, 'packages': {
    '': {'name': 'fixture', 'version': '1.0.0', 'dependencies': {'vue': '^3', '@tauri-apps/api': '^2', 'sucrase': '3'}},
    'node_modules/vue': {'version': '3.5.22', 'license': 'MIT'},
    'node_modules/@tauri-apps/api': {'version': '2.8.0', 'license': 'Apache-2.0 OR MIT'},
    'node_modules/sucrase': {'version': '3.35.0', 'license': 'MIT'},
    'node_modules/vite': {'version': '7.1.9', 'dev': True, 'license': 'MIT'},
    'node_modules/@esbuild/darwin-arm64': {'version': '0.28.2', 'dev': True, 'optional': True, 'license': 'MIT'},
    'node_modules/fsevents': {'version': '2.3.3', 'devOptional': True, 'license': 'MIT'},
}}
CARGO_LOCK = '''# fixture
version = 4

[[package]]
name = "pebble"
version = "0.4.2"
dependencies = [
 "tauri",
]

[[package]]
name = "serde"
version = "1.0.228"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aa"

[[package]]
name = "tauri"
version = "2.8.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bb"
'''
REQUIREMENTS = 'pydantic==2.13.5\nPyYAML==6.0.3\n\nPyJWT[crypto]==2.10.1\npillow-heif==1.8.0\n'
NOTICE_MD = '# notices\nHEIC: the wheel-supplied license texts are included under resources/licenses/HEIF.\n'


def metadata(name, version, licence=None, classifiers=()):
    message = Message()
    message['Name'], message['Version'] = name, version
    if licence:
        message['License-Expression'] = licence
    for item in classifiers:
        message['Classifier'] = item
    return message


ENVIRONMENT = {'pyyaml': metadata('PyYAML', '6.0.3', classifiers=('License :: OSI Approved :: MIT License',)),
               'pydantic': metadata('pydantic', '2.13.5', 'MIT')}


def lookup(name):
    return ENVIRONMENT.get(inv.normalize(name))


def tops():
    return {'yaml': ['PyYAML'], 'pillow_heif': ['pillow_heif'], 'jwt': ['PyJWT']}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def build(tmp_path, notices=('PyJWT-LICENSE', 'Sucrase-LICENSE')):
    """A staged resources folder, its manifest, the source notice folder, lockfiles and a cargo registry."""
    source = tmp_path / 'src-resources'
    for name in notices:
        write(source / 'licenses' / name, f'{name} text\n')
    write(source / 'NOTICE.md', NOTICE_MD)
    write(source / 'AGENTGUARD-LICENSE', 'Apache-2.0\n')
    write(source / 'LICENSE', 'MIT (bloub)\n')
    stage = tmp_path / 'C' / 'stage'
    resources = stage / 'resources'
    files = {
        'backend/pebble-backend': b'fake backend',
        'backend/_internal/Python': b'fake libpython',
        'backend/_internal/PyJWT-2.10.1.dist-info/METADATA':
            b'Metadata-Version: 2.4\nName: PyJWT\nVersion: 2.10.1\nLicense-Expression: MIT\n\nbody\n',
        'backend/_internal/PyJWT-2.10.1.dist-info/licenses/LICENSE': b'MIT\n',
        'backend/_internal/pillow_heif-1.8.0.dist-info/METADATA':
            b'Metadata-Version: 2.1\nName: pillow_heif\nVersion: 1.8.0\nLicense: BSD-3-Clause\n\n',
        'backend/_internal/pillow_heif/_pillow_heif.cpython-314-darwin.so': b'so',
        'backend/_internal/yaml/_yaml.cpython-314-darwin.so': b'so',
        'llama/llama-server': b'llama',
        'llama/libggml.dylib': b'ggml',
    }
    for rel, data in files.items():
        write(resources / rel, data)
    for path in (source / 'licenses').rglob('*'):
        if path.is_file():
            write(resources / 'licenses' / path.relative_to(source / 'licenses'), path.read_bytes())
    for name in ('NOTICE.md', 'AGENTGUARD-LICENSE', 'LICENSE'):
        write(resources / name, (source / name).read_bytes())
    manifest = stage / 'stage-manifest.json'
    restage(resources, manifest)
    write(tmp_path / 'requirements.txt', REQUIREMENTS)
    write(tmp_path / 'package-lock.json', json.dumps(LOCK))
    write(tmp_path / 'Cargo.lock', CARGO_LOCK)
    write(tmp_path / 'cargo/registry/src/index.crates.io-0000/tauri-2.8.5/Cargo.toml',
          '[package]\nname = "tauri"\nversion = "2.8.5"\nlicense = "Apache-2.0 OR MIT"\n')
    return {'stage_manifest': manifest, 'resources': source, 'requirements': tmp_path / 'requirements.txt',
            'package_lock': tmp_path / 'package-lock.json', 'cargo_lock': tmp_path / 'Cargo.lock',
            'cargo_home': tmp_path / 'cargo'}


def restage(resources, manifest):
    """The shape T1 (scripts/build_candidate_app.py) writes: entries carry mode and source as well."""
    with Root(str(resources), 'stage') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems
    files = [dict(entry, mode='0644', source=None) for entry in entries]
    manifest.write_text(json.dumps({'schema': 'pebble.candidate-stage/1', 'files': files, 'links': links,
                                    'bundle_sha256': bundle_digest(entries)}))


def run_inventory(paths, **changes):
    return inv.inventory(**{**paths, 'metadata_lookup': lookup, 'top_levels': tops, **changes})


def by_name(report):
    return {(item['ecosystem'], item['name']): item for item in report['components']}


def missing(report):
    return {finding['subject'] for finding in report['findings'] if finding['code'] == 'notice_missing'}


def test_the_inventory_finds_a_planted_missing_notice_and_never_reports_a_gap_as_a_pass(tmp_path):
    paths = build(tmp_path)
    report = run_inventory(paths)
    components = by_name(report)
    assert report['result'] == 'gaps' and report['format'] == inv.FORMAT

    jwt = components[('python', 'PyJWT')]
    assert jwt['notice'] == {'status': 'present', 'files': ['licenses/PyJWT-LICENSE']}
    assert jwt['licence'] == 'MIT' and jwt['presence'] == 'bundle_metadata' and jwt['pinned'] == '2.10.1'
    assert jwt['bundled_notice_files'] == ['backend/_internal/PyJWT-2.10.1.dist-info/licenses/LICENSE']
    heif = components[('python', 'pillow_heif')]
    assert heif['notice']['status'] == 'missing' and heif['licence'] == 'BSD-3-Clause'
    assert 'python:pillow_heif@1.8.0' in missing(report)
    yaml = components[('python', 'PyYAML')]
    assert yaml['presence'] == 'files_in_bundle' and yaml['licence'] == 'MIT License' and yaml['modules'] == ['yaml']
    assert components[('python', 'pydantic')]['presence'] == 'not_evidenced_in_bundle'
    assert {'python:PyYAML@6.0.3', 'python:pydantic@2.13.5', 'native:llama.cpp@b11499', 'npm:vue@3.5.22',
            'npm:@tauri-apps/api@2.8.0', 'cargo:tauri@2.8.5', 'cargo:serde@1.0.228'} <= missing(report)
    assert components[('npm', 'sucrase')]['notice']['status'] == 'present'
    assert not {('npm', 'vite'), ('npm', '@esbuild/darwin-arm64'), ('npm', 'fsevents'), ('cargo', 'pebble')} & set(components)
    assert components[('cargo', 'tauri')]['licence'] == 'Apache-2.0 OR MIT'
    assert components[('cargo', 'serde')]['licence'] is None
    assert {'code': 'licence_unknown', 'subject': 'cargo:serde@1.0.228'} in report['findings']
    assert components[('native', 'llama.cpp')]['presence'] == 'staged'
    assert components[('vendored', 'AgentGuard')]['notice']['files'] == ['AGENTGUARD-LICENSE']
    assert {'code': 'notice_reference_missing', 'subject': 'licenses/HEIF'} in report['findings']

    # No component is counted as noticed without a file, and every missing notice is a finding.
    for item in report['components']:
        subject = f'{item["ecosystem"]}:{item["name"]}@{item["version"]}'
        assert (item['notice']['status'] == 'present') == bool(item['notice']['files']), subject
        assert (item['notice']['status'] == 'missing') == (subject in missing(report)), subject
        for name in item['notice']['files']:
            assert (paths['resources'] / name).is_file()
    summary = report['summary']['by_ecosystem']
    assert summary['python'] == {'components': 4, 'notice_present': 1, 'notice_missing': 3, 'licence_unknown': 0}


def test_a_complete_notice_folder_is_complete_and_a_changed_stage_is_a_finding(tmp_path):
    # V1-047: bloub's notice is licenses/bloub-LICENSE now (resources/LICENSE is Pebble's own licence).
    notices = ('PyJWT-LICENSE', 'Sucrase-LICENSE', 'pillow-heif-LICENSE', 'PyYAML-LICENSE', 'pydantic-LICENSE',
               'vue-LICENSE', 'tauri-apps-api-LICENSE', 'serde-LICENSE-MIT', 'tauri-LICENSE_APACHE-2.0',
               'llama.cpp-LICENSE', 'Python-LICENSE', 'PyInstaller-COPYING.txt', 'HEIF/libheif-COPYING',
               'bloub-LICENSE')
    paths = build(tmp_path, notices)
    write(paths['cargo_home'] / 'registry/src/index.crates.io-0000/serde-1.0.228/Cargo.toml',
          '[package]\nname = "serde"\nlicense = "MIT OR Apache-2.0"\n')
    report = run_inventory(paths)
    assert report['findings'] == [] and report['result'] == 'complete'
    assert by_name(report)[('python', 'pillow_heif')]['notice']['files'] == ['licenses/HEIF/libheif-COPYING',
                                                                              'licenses/pillow-heif-LICENSE']

    stage = paths['stage_manifest'].parent / 'resources'
    write(stage / 'licenses/vue-LICENSE', 'edited\n')
    restage(stage, paths['stage_manifest'])
    report = run_inventory(paths)
    assert report['result'] == 'gaps'
    assert report['findings'] == [{'code': 'stage_licenses_differ', 'subject': 'licenses/vue-LICENSE'}]

    # Staged metadata that no longer matches its manifest entry is never read as evidence.
    write(stage / 'backend/_internal/PyJWT-2.10.1.dist-info/METADATA', b'Name: PyJWT\nVersion: 9\n')
    report = run_inventory(paths)
    assert {'code': 'stage_file_differs', 'subject': 'backend/_internal/PyJWT-2.10.1.dist-info/METADATA'} in report['findings']


def test_the_cli_help_renders(capsys):
    with pytest.raises(SystemExit) as done:
        inv.main(['--help'])
    assert done.value.code == 0 and '--stage-manifest' in capsys.readouterr().out


def test_the_cli_writes_one_new_report_outside_the_resources(tmp_path, capsys):
    paths = build(tmp_path)
    report = tmp_path / 'inventory.json'
    argv = ['--stage-manifest', str(paths['stage_manifest']), '--resources', str(paths['resources']),
            '--requirements', str(paths['requirements']), '--package-lock', str(paths['package_lock']),
            '--cargo-lock', str(paths['cargo_lock']), '--cargo-home', str(paths['cargo_home'])]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths['resources'].rglob('*') if path.is_file()}
    assert inv.main(argv + ['--report', str(report)], metadata_lookup=lookup, top_levels=tops) == 10
    assert json.loads(report.read_text())['result'] == 'gaps'
    assert json.loads(capsys.readouterr().out)['result'] == 'gaps'
    assert inv.main(argv + ['--report', str(report)], metadata_lookup=lookup, top_levels=tops) == 2
    inside = paths['resources'] / 'licenses' / 'inventory.json'
    staged = paths['stage_manifest'].parent / 'resources' / 'inventory.json'
    for target in (inside, staged):
        assert inv.main(argv + ['--report', str(target)], metadata_lookup=lookup, top_levels=tops) == 2
        assert not target.exists()
    refusals = [json.loads(line)['refused'] for line in capsys.readouterr().err.strip().splitlines()]
    assert refusals == ['report_exists', 'report_inside_resources', 'report_inside_resources']
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths['resources'].rglob('*') if path.is_file()}
    assert before == after
    missing_manifest = tmp_path / 'absent' / 'stage-manifest.json'
    assert inv.main(['--stage-manifest', str(missing_manifest), '--report', str(tmp_path / 'other.json')]
                    + argv[2:], metadata_lookup=lookup, top_levels=tops) == 2


def test_the_repository_inputs_show_the_design_study_gaps_as_findings(tmp_path):
    """D20 section 1.4 on this checkout: the name-matched notice files cover 10 Python distributions and Sucrase,
    nothing more. Phase 24: measured without the generated THIRD-PARTY-NOTICES.txt (which closes these gaps,
    test_release_inventory_24.py), and with pillow-heif gone from the app (V1-045) and bloub's licence in
    licenses/bloub-LICENSE (V1-047)."""
    stage = tmp_path / 'C' / 'stage'
    resources = stage / 'resources'
    source = ROOT / 'src-tauri/resources'
    copy = tmp_path / 'source-resources'
    for path in source.rglob('*'):
        if path.is_file() and not path.is_symlink() and 'tokenizer' not in path.parts and path.name != inv.NOTICES:
            write(resources / path.relative_to(source), path.read_bytes())
            write(copy / path.relative_to(source), path.read_bytes())
    write(resources / 'backend/pebble-backend', b'fake backend')
    write(resources / 'llama/llama-server', b'fake llama')
    restage(resources, stage / 'stage-manifest.json')
    report = inv.inventory(stage / 'stage-manifest.json', resources=copy, cargo_home=tmp_path / 'no-cargo-home',
                           metadata_lookup=lambda name: None, top_levels=lambda: {})
    assert report['result'] == 'gaps'
    components = by_name(report)
    present = {name for (ecosystem, name), item in components.items()
               if ecosystem == 'python' and item['notice']['status'] == 'present'}
    assert {'PyJWT', 'cryptography', 'tiktoken', 'Pillow'} <= present
    # V1-045: pillow-heif is no longer a pin, so it is no longer a component to notice.
    assert ('python', 'pillow-heif') not in components
    for name in ('pydantic', 'PyYAML', 'pypdf', 'jsonschema', 'tree-sitter', 'tree-sitter-language-pack',
                 'certifi', 'tzdata'):
        assert components[('python', name)]['notice']['status'] == 'missing', name
        assert f'python:{name}@{components[("python", name)]["version"]}' in missing(report)
    # Phase 24 adds llama.cpp's own licence file and bloub's (V1-046, V1-047).
    assert components[('native', 'llama.cpp')]['notice']['files'] == ['licenses/llama.cpp-LICENSE']
    assert components[('vendored', 'bloub')]['notice']['files'] == ['licenses/bloub-LICENSE']
    assert components[('first-party', 'Pebble')]['notice']['files'] == ['LICENSE']
    assert components[('npm', 'sucrase')]['notice']['status'] == 'present'
    assert components[('npm', 'vue')]['notice']['status'] == 'missing'
    assert any(ecosystem == 'cargo' and name == 'tauri' and item['notice']['status'] == 'missing'
               for (ecosystem, name), item in components.items())
    # NOTICE.md no longer names the licenses/HEIF folder that never existed.
    assert not [f for f in report['findings'] if f['code'] == 'notice_reference_missing']
    assert report['summary']['by_ecosystem']['cargo']['components'] > 100
    # Phase 24 adds bloub-LICENSE, llama.cpp-LICENSE and eastasianwidth-LICENSE (it ships no licence file).
    # f24-locked: then the parent's release step added 18 upstream licence texts (the name-matched overrides that
    # THIRD-PARTY-NOTICES.txt uses for components shipping none of their own): OpenSSL's, zstd's and 16 crates'. So the
    # released folder holds 36 files besides the generated one, by name: the 18 counted here before that step (0.6.1's
    # 15, docs/HARNESS_INSTALLED_VALIDATION.md, and Phase 24's three) and the 18 it added.
    before_release = {'PyJWT-AUTHORS.rst', 'PyJWT-LICENSE', 'cryptography-LICENSE', 'cryptography-LICENSE.APACHE',
                      'cryptography-LICENSE.BSD', 'cffi-LICENSE', 'pycparser-LICENSE', 'tiktoken-LICENSE', 'Pillow-LICENSE',
                      'regex-LICENSE.txt', 'requests-LICENSE', 'requests-NOTICE', 'urllib3-LICENSE.txt',
                      'charset-normalizer-LICENSE', 'Sucrase-LICENSE', 'bloub-LICENSE', 'llama.cpp-LICENSE',
                      'eastasianwidth-LICENSE'}
    release_crates = {'alloc-stdlib', 'block2', 'defmt-parser', 'dispatch2', 'objc2', 'objc2-app-kit',
                      'objc2-core-foundation', 'objc2-core-graphics', 'objc2-encode', 'objc2-exception-helper',
                      'objc2-foundation', 'objc2-io-surface', 'objc2-quartz-core', 'objc2-web-kit', 'selectors',
                      'tauri-plugin'}
    release_step = {'OpenSSL-LICENSE', 'zstd-LICENSE'} | {name + '-LICENSE' for name in release_crates}
    files = report['inputs']['licenses']['files']
    assert len(before_release) == len(release_step) == 18 and not before_release & release_step
    assert set(files) == before_release | release_step and len(files) == 36
    assert all(digest == hashlib.sha256((source / 'licenses' / name).read_bytes()).hexdigest()
               for name, digest in files.items())
    # The gaps this measurement still reports: every D20 one above, and nothing but missing notices and unknown
    # licences (no registry, no metadata). The crates the release texts name are noticed by them, with regex (the
    # Python regex's text matches the crate's name, as before the release step); no other crate, npm package or
    # Python distribution is noticed by a name-matched file.
    assert {finding['code'] for finding in report['findings']} == {'notice_missing', 'licence_unknown'}
    noticed = {(ecosystem, name) for (ecosystem, name), item in components.items() if item['notice']['status'] == 'present'}
    assert {name for ecosystem, name in noticed if ecosystem == 'cargo'} == release_crates | {'regex'}
    assert all(components[('cargo', name)]['notice']['files'] == ['licenses/' + name + '-LICENSE'] for name in release_crates)
    assert {name for ecosystem, name in noticed if ecosystem == 'npm'} == {'sucrase', 'eastasianwidth'}
    assert {name for ecosystem, name in noticed if ecosystem == 'python'} == {'PyJWT', 'cryptography', 'tiktoken', 'Pillow'}
