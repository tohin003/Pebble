"""D20 §1.6 (track T1): the isolated candidate app builder, scripts/build_candidate_app.py.

Every case builds an owned checkout under pytest's tmp_path and injects a command runner that records argv and
fakes PyInstaller, vue-tsc, vite, ditto, tauri and codesign inside the build's own paths. No real build, signature,
bundle launch, network or model call happens, and nothing outside tmp_path is written.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import token_budget
from pebble.release_readiness import Root, bundle_digest, bundle_entries, read_manifest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('build_candidate_app_under_test', REPO / 'scripts' / 'build_candidate_app.py')
bca = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bca)

C_REL = '.runtime/phase21-candidate'
TOKENIZER = REPO / 'src-tauri/resources/tokenizer' / token_budget.TOKENIZER_FILE
BACKEND_BYTES = b'\xcf\xfa\xed\xfe pebble-backend'
# Phase 24 adds the reproducible-build names: a fixed hash seed and archive time (V1-076) and rustc's path remaps
# (CARGO_ENCODED_RUSTFLAGS, V1-023).
ENV_NAMES = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'PYINSTALLER_CONFIG_DIR', 'PYTHONDONTWRITEBYTECODE',
             'PYTHONNOUSERSITE', 'TIKTOKEN_CACHE_DIR', 'CARGO_HOME', 'RUSTUP_HOME', 'RUSTUP_TOOLCHAIN', 'RUSTC',
             'CARGO_TARGET_DIR', 'CARGO_BUILD_JOBS', 'PYTHONHASHSEED', 'SOURCE_DATE_EPOCH', 'CARGO_ENCODED_RUSTFLAGS'}
# Phase 24 (V1-076): the fixture's hashed lock; the fake build environment lists exactly these distributions.
LOCK_PINS = (('runtime', 'Pillow', '13.0.0'), ('runtime', 'certifi', '2026.10.19'), ('build', 'pyinstaller', '6.22.3'))
LOCK_TEXT = ''.join(('# [%s]\n' % section if index == 0 or LOCK_PINS[index - 1][0] != section else '')
                    + '%s==%s \\\n    --hash=sha256:%s\n' % (name, version, hashlib.sha256(name.encode()).hexdigest())
                    for index, (section, name, version) in enumerate(LOCK_PINS))
BUILD_ENV = [[name, version] for _, name, version in LOCK_PINS] + [['pip', '26.1.1']]
# The links the stage keeps. Phase 21 (P21-build): the stage flattens backend/'s links, which Tauri's resource copy
# drops, so only the fixture's llama alias is left; the backend's links are listed in FLATTENED instead.
LINKS = ['llama/libllama.dylib']
PYTHON = 'backend/_internal/Python.framework/Versions/3.12/Python'
FLATTENED = [{'path': 'backend/_internal/Python', 'target': PYTHON, 'kind': 'file'},
             {'path': 'backend/_internal/Python.framework/Python', 'target': PYTHON, 'kind': 'file'},
             {'path': 'backend/_internal/Python.framework/Versions/Current',
              'target': 'backend/_internal/Python.framework/Versions/3.12', 'kind': 'directory'}]
# FLATTENED plus more_links' shapes, in bundle_entries' path order.
FRAMEWORK_LINKS = FLATTENED[:2] + [
    {'path': 'backend/_internal/Python.framework/Resources',
     'target': 'backend/_internal/Python.framework/Versions/3.12/Resources', 'kind': 'directory'},
    {'path': 'backend/_internal/Python.framework/Versions/3.12/lib/libpython3.12.dylib', 'target': PYTHON, 'kind': 'file'},
    FLATTENED[2],
    {'path': 'backend/_internal/libwebp.7.dylib', 'target': 'backend/_internal/PIL/.dylibs/libwebp.7.dylib',
     'kind': 'file'}]
# V1-045: the dylib alias shape the first real build had (then pillow_heif's libheif) is now Pillow's own libwebp:
# the HEIF stack no longer ships, and a backend carrying it is refused (test_a_backend_with_the_heif_stack_...).
DYLIB_BYTES = b'\xcf\xfa\xed\xfe libwebp'


def put(path, data=b'', mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    return path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(path):
    """Every entry below path with its kind, mode and content, for exact before/after comparisons."""
    found = {}
    for directory, folders, files in os.walk(path):
        for name in folders + files:
            entry = Path(directory) / name
            info = entry.lstat()
            content = (os.readlink(entry) if stat.S_ISLNK(info.st_mode) else
                       entry.read_bytes() if stat.S_ISREG(info.st_mode) else None)
            found[str(entry.relative_to(path))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), content)
    return found


def snap(checkout):
    """Write the tested-source snapshot of the fixture checkout with the real verifier's sources()."""
    verifier = bca.load_verifier(checkout.root)
    for _ in range(64):
        try:
            sources = verifier.sources()
            break
        except FileNotFoundError as error:  # a fixed input of the real verifier that this fixture does not model
            put(Path(error.filename), '# fixed snapshot input\n')
    else:
        raise AssertionError('the verifier keeps requiring new inputs')
    checkout.snapshot.write_text(json.dumps(sources, indent=1))
    return sources


def make_checkout(tmp_path):
    tmp_path = tmp_path.resolve()
    root = tmp_path / 'checkout'
    for rel in bca.IMPORTED:  # the code the tool runs, byte-identical to this repository's
        put(root / rel, (REPO / rel).read_bytes())
    put(root / 'backend/entry.py', 'print("pebble")\n')
    put(root / 'backend/vendor/agentguard/__init__.py', '')
    put(root / 'backend/requirements.txt', 'Pillow==13.0.0\ncertifi==2026.10.19\n')
    put(root / bca.LOCK, LOCK_TEXT)
    put(root / bca.TOOLCHAIN_PIN, '[toolchain]\nchannel = "%s"\n' % bca.TOOLCHAIN.split('-')[0])
    put(root / 'src/main.ts', 'export {}\n')
    put(root / 'scripts/fixtures/case.json', '{}\n')
    for name in ('package.json', 'package-lock.json'):
        put(root / name, '{"name": "pebble-fixture"}\n')
    tauri = root / 'src-tauri'
    put(tauri / 'tauri.conf.json', (REPO / 'src-tauri/tauri.conf.json').read_bytes())
    put(tauri / 'Cargo.toml', '[package]\nname = "pebble"\n')
    put(tauri / 'Cargo.lock', '# lock\n')
    put(tauri / 'build.rs', 'fn main() {}\n')
    put(tauri / 'src/main.rs', 'fn main() {}\n')
    put(tauri / 'capabilities/main.json', '{}\n')
    put(tauri / 'icons/icon.icns', b'icns')
    put(tauri / 'permissions/autogenerated/rpc.toml', '# generated\n')
    put(tauri / 'gen/schemas/acl-manifests.json', '{"acl": 1}\n')
    resources = tauri / 'resources'
    put(resources / 'tokenizer' / token_budget.TOKENIZER_FILE, TOKENIZER.read_bytes())
    put(resources / 'model.json', '{"model": "fixture"}\n')
    put(resources / 'NOTICE.md', 'notice\n')
    put(resources / 'AGENTGUARD-LICENSE', 'agentguard license\n')
    put(resources / 'LICENSE', 'license\n')
    put(resources / 'licenses/tiktoken-LICENSE', 'tiktoken license\n')
    put(resources / 'backend/pebble-backend', b'old shared backend', 0o755)  # never staged
    put(resources / 'llama/llama-server', b'\xcf\xfa\xed\xfe llama-server', 0o755)
    put(resources / 'llama/libllama.0.dylib', b'\xcf\xfa\xed\xfe libllama', 0o755)
    put(resources / 'llama/README.md', 'not shipped\n')
    (resources / 'llama/libllama.dylib').symlink_to('libllama.0.dylib')
    put(root / 'dist/index.html', '<old/>')
    put(root / 'dist/assets/app.js', 'old();')
    put(tauri / 'target/release/pebble', b'old shell', 0o755)
    put(tauri / 'target/release/bundle/macos/Pebble.app/Contents/Info.plist', b'old plist')
    put(root / bca.SPEC, "a = Analysis(['%s/backend/entry.py'], pathex=['%s/backend/vendor'])\n"
                         "exe = EXE(pyz, name='pebble-backend', upx=True)\n" % (root, root))
    for tool in (bca.PYTHON, 'node_modules/.bin/vue-tsc', 'node_modules/.bin/vite', 'node_modules/.bin/tauri',
                 '.runtime/toolchain/rustup/toolchains/%s/bin/cargo' % bca.TOOLCHAIN,
                 '.runtime/toolchain/rustup/toolchains/%s/bin/rustc' % bca.TOOLCHAIN):
        put(root / tool, '#!/bin/sh\nexit 99\n', 0o755)
    node = put(tmp_path / 'node-bin/node', '#!/bin/sh\nexit 99\n', 0o755).parent
    checkout = SimpleNamespace(root=root, out=root / C_REL, snapshot=tmp_path / 'tested-source.json', node=node)
    snap(checkout)
    return checkout


def fake_dist(dist):
    """A PyInstaller onedir output with its usual links: a framework-like Versions/Current and the Python aliases."""
    put(dist / 'pebble-backend', BACKEND_BYTES, 0o755)
    framework = dist / '_internal/Python.framework'
    put(framework / 'Versions/3.12/Python', b'\xcf\xfa\xed\xfe python', 0o755)
    (framework / 'Versions/Current').symlink_to('3.12')
    (framework / 'Python').symlink_to('Versions/Current/Python')
    (dist / '_internal/Python').symlink_to('Python.framework/Versions/3.12/Python')
    put(dist / '_internal/PIL/_imaging.so', b'\xcf\xfa\xed\xfe imaging', 0o755)  # V1-045: was pillow_heif's
    put(dist / '_internal/base_library.zip', b'zip')


def more_links(internal):
    """The other shapes of the first real build: Python.framework/Resources, a link inside a linked directory, and a
    dylib alias into a package's .dylibs."""
    version = internal / 'Python.framework/Versions/3.12'
    put(version / 'Resources/Info.plist', b'<plist/>')
    (version / 'lib').mkdir()
    (version / 'lib/libpython3.12.dylib').symlink_to('../Python')
    (internal / 'Python.framework/Resources').symlink_to('Versions/Current/Resources')
    put(internal / 'PIL/.dylibs/libwebp.7.dylib', DYLIB_BYTES, 0o755)
    (internal / 'libwebp.7.dylib').symlink_to('PIL/.dylibs/libwebp.7.dylib')


def framework(argv, cwd, env, stream):
    more_links(Path(argv[argv.index('--distpath') + 1]) / 'pebble-backend/_internal')


def flatten_copy(source, target):
    """The resource copy suspected of Tauri: file links become files, directory links are dropped."""
    target.mkdir(parents=True)
    for entry in sorted(source.iterdir()):
        if entry.is_symlink() and entry.is_dir():
            continue
        if entry.is_dir():
            flatten_copy(entry, target / entry.name)
        else:
            shutil.copy2(entry, target / entry.name)


def drop_copy(source, target):
    """The resource copy Tauri made in the first real build (2026-10-14): every symlink is dropped."""
    target.mkdir(parents=True)
    for entry in sorted(source.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            drop_copy(entry, target / entry.name)
        else:
            shutil.copy2(entry, target / entry.name)


class FakeRunner:
    """Records every command and fakes its effect inside the build's own paths."""

    def __init__(self, checkout, fail=(), hooks=None, flatten=False, drop=False):
        self.checkout, self.fail, self.hooks, self.flatten = checkout, set(fail), hooks or {}, flatten
        self.drop, self.calls = drop, []

    def kind(self, argv):
        if '--version' in argv:
            return 'version'
        if argv[1:3] == ['-I', '-c']:
            return 'distributions'
        if argv[0] in (bca.DITTO, bca.CODESIGN):
            return os.path.basename(argv[0])
        if argv[1:3] == ['-m', 'PyInstaller']:
            return 'pyinstaller'
        if os.path.basename(argv[0]) == 'tauri':
            config = json.loads(Path(argv[argv.index('--config') + 1]).read_text())
            return 'tauri-test' if config['identifier'] == 'com.pebble.candidate' else 'tauri-release'
        return os.path.basename(argv[0])

    def __call__(self, argv, cwd, env, stream):
        assert all(isinstance(arg, str) for arg in argv) and os.path.isdir(cwd)
        kind = self.kind(argv)
        self.calls.append({'kind': kind, 'argv': argv, 'cwd': cwd, 'env': env})
        if kind in self.fail:
            stream.write(b'fake failure\n')
            return 1
        handlers = {'version': self.version, 'pyinstaller': self.pyinstaller, 'vue-tsc': self.nothing,
                    'vite': self.vite, 'ditto': self.ditto, 'codesign': self.nothing, 'tauri-release': self.tauri,
                    'tauri-test': self.tauri, 'distributions': self.distributions}
        handlers[kind](argv, cwd, env, stream)
        if kind in self.hooks:
            self.hooks[kind](argv, cwd, env, stream)
        return 0

    def nothing(self, argv, cwd, env, stream):
        stream.write(b'ok\n')

    def version(self, argv, cwd, env, stream):
        stream.write(b'fake 1.0\n')

    def distributions(self, argv, cwd, env, stream):
        assert argv[3] == bca.DISTRIBUTIONS
        stream.write(json.dumps(self.environment).encode() + b'\n')

    environment = BUILD_ENV

    def pyinstaller(self, argv, cwd, env, stream):
        assert argv[-1] == str(self.checkout.root / bca.SPEC) and '--noconfirm' in argv
        Path(argv[argv.index('--workpath') + 1]).mkdir(parents=True)
        fake_dist(Path(argv[argv.index('--distpath') + 1]) / 'pebble-backend')
        stream.write(b'INFO: UPX is not available.\n')

    def vite(self, argv, cwd, env, stream):
        out = Path(argv[argv.index('--outDir') + 1])
        assert argv[1] == 'build' and '--emptyOutDir' in argv and not out.exists()
        put(out / 'index.html', '<new/>')
        put(out / 'assets/index.js', 'new();')

    def ditto(self, argv, cwd, env, stream):
        assert len(argv) == 3 and not os.path.lexists(argv[2])
        shutil.copytree(argv[1], argv[2], symlinks=True)

    def tauri(self, argv, cwd, env, stream):
        assert argv[1:5] == ['build', '--bundles', 'app', '--config'] and argv[-3:] == ['--', '--locked', '--offline']
        config = json.loads(Path(argv[5]).read_text())
        name = config.get('productName', 'Pebble')
        resources = config.get('bundle', {}).get('resources')
        if resources:  # merge mode builds from the checkout; build.rs rewrites identical bytes into src-tauri/gen
            assert Path(cwd) == self.checkout.root
            source = Path(next(key for key, value in resources.items() if value == 'resources/' and key != 'resources/'))
            for path in (self.checkout.root / 'src-tauri/gen').rglob('*'):
                if path.is_file():
                    path.write_bytes(path.read_bytes())
        else:  # copy mode builds from C/shell-src, where build.rs writes its own gen/
            source = Path(cwd) / 'src-tauri/resources'
            shutil.copytree(self.checkout.root / 'src-tauri/gen', Path(cwd) / 'src-tauri/gen', dirs_exist_ok=True)
        app = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos' / (name + '.app')
        assert not app.exists()
        put(app / 'Contents/Info.plist', plistlib.dumps({'CFBundleIdentifier': config['identifier'], 'CFBundleName': name,
                                                         'CFBundleExecutable': 'pebble', 'CFBundleShortVersionString': '0.4.2'}))
        put(app / 'Contents/MacOS/pebble', b'\xcf\xfa\xed\xfe shell', 0o755)
        put(app / 'Contents/_CodeSignature/CodeResources', b'<plist/>')
        if self.drop:
            drop_copy(source, app / 'Contents/Resources/resources')
        elif self.flatten:
            flatten_copy(source, app / 'Contents/Resources/resources')
        else:
            shutil.copytree(source, app / 'Contents/Resources/resources', symlinks=True)
        stream.write(b'Compiling pebble v0.4.2\n')


def build(checkout, runner=None, **options):
    return bca.Build(checkout.root, checkout.out, snapshot=checkout.snapshot, node_bin=checkout.node,
                     runner=runner if runner is not None else FakeRunner(checkout), **options)


def refused(call):
    with pytest.raises(bca.Refused) as caught:
        call()
    return caught.value


def inputs(checkout):
    return json.loads((checkout.out / 'inputs.json').read_text())


def release_config(out, root):
    # Phase 25: frontendDist is relative to src-tauri (Tauri embeds the config in the shell; an absolute path leaked the
    # checkout into the binary). Still compared exactly.
    return {'identifier': 'com.pebble.local',
            'build': {'beforeBuildCommand': '', 'frontendDist': os.path.relpath(out / 'frontend/dist', root / 'src-tauri')},
            'bundle': {'resources': {'resources/': None, str(out / 'stage/resources') + '/': 'resources/'}}}


def test_plan_is_the_default_and_writes_nothing(tmp_path, capsys):
    checkout = make_checkout(tmp_path)
    before = tree(tmp_path)

    def forbidden(*args):
        raise AssertionError('plan mode ran a command')
    arguments = ['--root', str(checkout.root), '--out', str(checkout.out), '--snapshot', str(checkout.snapshot),
                 '--node-bin', str(checkout.node)]
    assert bca.main(arguments, runner=forbidden) == 0
    plan = json.loads(capsys.readouterr().out)
    assert bca.main(arguments + ['--plan'], runner=forbidden) == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert tree(tmp_path) == before and not checkout.out.exists()

    C, R = checkout.out, checkout.root
    assert plan['mode'] == 'plan' and plan['problems'] == [] and plan['shell_mode'] == 'merge-config'
    assert [step['step'] for step in plan['steps']] == list(bca.STEPS)
    commands = {command['label']: command for step in plan['steps'] for command in step['commands']}
    for command in commands.values():
        assert set(command) == {'label', 'argv', 'cwd', 'env', 'log'}
        assert Path(command['log']).parent == C / 'logs' and set(command['env']) <= ENV_NAMES
        assert command['env']['HOME'] == str(C / 'home') and command['env']['TMPDIR'] == str(C / 'tmp') + '/'
    assert commands['pyinstaller']['argv'] == [str(R / bca.PYTHON), '-m', 'PyInstaller', '--noconfirm', '--distpath',
                                               str(C / 'backend/dist'), '--workpath', str(C / 'backend/work'), str(R / bca.SPEC)]
    assert commands['vue-tsc']['argv'] == [str(R / 'node_modules/.bin/vue-tsc'), '--noEmit']
    assert commands['vite-build']['argv'][1:] == ['build', '--outDir', str(C / 'frontend/dist'), '--emptyOutDir']
    assert commands['ditto-backend']['argv'] == [bca.DITTO, str(C / 'backend/dist/pebble-backend'),
                                                 str(C / 'stage/resources/backend')]
    shells = [command for step in plan['steps'] for command in step['commands'] if command['label'] == 'tauri-build']
    assert [shell['argv'][1:] for shell in shells] == [
        ['build', '--bundles', 'app', '--config', str(C / name), '--', '--locked', '--offline']
        for name in ('tauri.release.json', 'tauri.test.json')]
    toolchain = R / '.runtime/toolchain/rustup/toolchains' / bca.TOOLCHAIN / 'bin'
    assert shells[0]['cwd'] == str(R) and shells[0]['env']['CARGO_TARGET_DIR'] == str(C / 'target')
    assert shells[0]['env']['RUSTC'] == str(toolchain / 'rustc')
    assert shells[0]['env']['CARGO_HOME'] == str(R / '.runtime/toolchain/cargo')
    assert shells[0]['env']['PATH'].split(':')[:2] == [str(checkout.node), str(toolchain)]
    assert commands['codesign-test']['argv'] == [bca.CODESIGN, '--verify', '--deep', '--strict',
                                                 str(C / 'test/Pebble Candidate.app')]
    stage = next(step for step in plan['steps'] if step['step'] == 'stage')['writes']
    assert str(C / 'stage/resources/tokenizer' / token_budget.TOKENIZER_FILE) in stage
    assert str(C / 'stage/resources/llama/libllama.dylib') in stage and str(C / 'stage/resources/llama/README.md') not in stage
    assert all(bca.within(path, C) for step in plan['steps'] for path in step['writes'])
    assert plan['merge_configs'][str(C / 'tauri.release.json')] == release_config(C, R)
    assert not any(str(R) in json.dumps(config['build']) for config in plan['merge_configs'].values()), 'no machine path is embedded'

    # A C inside a shared path is reported, and so is every command that would write there.
    inside = bca.Build(R, R / 'src-tauri/resources/candidate', snapshot=checkout.snapshot, node_bin=checkout.node,
                       runner=forbidden).plan()
    codes = [problem['refused'] for problem in inside['problems']]
    assert codes[0] == 'out_inside_checkout' and 'unsafe_command' in codes
    assert tree(tmp_path) == before


def test_execute_builds_c_and_its_manifests(tmp_path):
    checkout = make_checkout(tmp_path)
    C = checkout.out
    planned = [command['argv'] for step in build(checkout).plan()['steps'] for command in step['commands']]
    runner = FakeRunner(checkout)
    result = build(checkout, runner).execute()
    assert result['result'] == 'built' and result['ran'] == list(bca.STEPS)
    assert stat.S_IMODE(C.stat().st_mode) == 0o700
    assert [call['argv'] for call in runner.calls] == planned
    assert len(list((C / 'logs').iterdir())) == len(runner.calls)

    record = inputs(checkout)
    assert all(record['steps'][step]['status'] == 'done' for step in bca.STEPS) and record['failure'] is None
    assert record['guard']['after'] == record['guard']['before']
    assert record['source_snapshot']['sha256'] == sha(checkout.snapshot) and record['tool_versions']['tauri'] == 'fake 1.0'
    assert set(record['guard']['before']['hashes']) >= {'src-tauri/resources/model.json', 'dist/index.html',
                                                        'src-tauri/gen/schemas/acl-manifests.json', bca.SPEC,
                                                        'src-tauri/permissions/autogenerated/rpc.toml'}
    assert 'src-tauri/target/release/pebble' in record['guard']['before']['stats']
    backend = record['steps']['backend']['attempts'][-1]['evidence']
    assert backend['executable_sha256'] == hashlib.sha256(BACKEND_BYTES).hexdigest()
    # V1-045: no pillow_heif file and no GPL/LGPL HEIF library is collected any more.
    assert backend['upx_log'] == 'not_available' and backend['pillow_heif_files'] == 0
    assert backend['copyleft_binaries'] == 0
    assert record['steps']['guard-before']['attempts'][-1]['evidence']['build_env_matches_lock'] == {
        'lock': bca.LOCK, 'pins': len(LOCK_PINS)}
    candidate = json.loads((C / 'backend/candidate-manifest.json').read_text())
    assert candidate['bundle_sha256'] == backend['candidate_bundle_sha256']

    # The stage manifest is the walker's own entries, so release_readiness.read_manifest accepts it (T5 reads it).
    data = (C / 'stage/stage-manifest.json').read_bytes()
    stage = read_manifest(data)
    with Root(str(C / 'stage/resources'), 'test') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems and stage['files'] == entries and stage['links'] == links
    assert stage['bundle_sha256'] == bundle_digest(entries) == record['steps']['stage']['attempts'][-1]['evidence']['bundle_sha256']
    files = {entry['path']: entry for entry in stage['files']}
    assert set(stage['modes']) == set(files) and set(stage['sources']) == set(files) | {link['path'] for link in links}
    tokenizer = 'tokenizer/' + token_budget.TOKENIZER_FILE
    assert files[tokenizer]['sha256'] == token_budget.TOKENIZER_SHA256
    assert stage['sources'][tokenizer] == 'src-tauri/resources/' + tokenizer
    assert files['backend/pebble-backend'] == {'path': 'backend/pebble-backend', 'bytes': len(BACKEND_BYTES),
                                               'sha256': backend['executable_sha256']}
    assert stage['modes']['backend/pebble-backend'] == '0755' and stage['modes']['model.json'] == '0644'
    assert stage['sources']['backend/pebble-backend'] == C_REL + '/backend/dist/pebble-backend/pebble-backend'
    assert stage['sources']['llama/llama-server'] == 'src-tauri/resources/llama/llama-server'
    assert 'llama/README.md' not in files and stage['llama']['skipped'] == ['README.md']
    # Phase 21 (P21-build): the backend's links are flattened at stage time and recorded; only llama's link is left.
    assert [link['path'] for link in stage['links']] == LINKS and stage['flattened_links'] == FLATTENED

    # The app manifest: one app object per bundle under `apps`, each a read_manifest manifest (the install tool's
    # seam), and the release bundle's subject at the top level (the trust channel's seam), with no top-level files.
    manifest = json.loads((C / 'app-manifest.json').read_text())
    assert 'files' not in manifest and 'bundle_sha256' not in manifest and set(manifest['apps']) == set(bca.APPS)
    assert manifest['subject'] == manifest['apps']['release']['subject']
    assert manifest['source_snapshot_sha256'] == sha(checkout.snapshot)
    assert manifest['candidate_executable_sha256'] == backend['executable_sha256']
    for kind, (name, identifier, _) in bca.APPS.items():
        app = C / kind / (name + '.app')
        with Root(str(app), 'test') as root:
            entries, links, problems = bundle_entries(root)
        with Root(str(app / 'Contents/Resources/resources/backend'), 'test') as root:
            bundled = bundle_entries(root)[0]
        bundle = manifest['apps'][kind]
        assert read_manifest(json.dumps(bundle).encode())['files'] == entries and bundle['links'] == links
        assert manifest[kind + '_app_bundle_sha256'] == bundle['bundle_sha256'] == bundle['app_bundle_sha256']
        assert not problems and bundle['files'] == entries and bundle['app_bundle_sha256'] == bundle_digest(entries)
        assert bundle['identifier'] == identifier and bundle['launched'] is False
        assert bundle['backend_executable_sha256'] == backend['executable_sha256']
        assert result[kind + '_app_bundle_sha256'] == bundle['app_bundle_sha256']
        assert bundle['subject'] == {'source_snapshot_sha256': sha(checkout.snapshot),
                                     'candidate_executable_sha256': backend['executable_sha256'],
                                     'candidate_bundle_sha256': bundle_digest(bundled),
                                     'app_bundle_sha256': bundle_digest(entries), 'platform': bca.platform_label()}
        # Phase 21 (P21-build): the bundled backend is the dist with its links followed, so Versions/Current's files
        # count and it equals the backend step's flattened digest, not the walker's digest of the dist (links unfollowed).
        assert bundle_digest(bundled) == backend['flattened_bundle_sha256'] != backend['candidate_bundle_sha256']
    assert manifest['candidate_bundle_sha256'] == manifest['subject']['candidate_bundle_sha256']
    assert manifest['dist_bundle_sha256'] == backend['candidate_bundle_sha256']
    assert manifest['apps']['test']['subject']['app_bundle_sha256'] != manifest['subject']['app_bundle_sha256']
    assert json.loads((C / 'tauri.release.json').read_text()) == release_config(C, checkout.root)
    assert json.loads((C / 'tauri.test.json').read_text()) == {
        'identifier': 'com.pebble.candidate', 'productName': 'Pebble Candidate',
        'build': {'beforeBuildCommand': '', 'frontendDist': os.path.relpath(C / 'frontend/dist', checkout.root / 'src-tauri')},
        'bundle': {'resources': {'resources/': None, str(C / 'stage/resources') + '/': 'resources/'}}}


def test_an_existing_out_is_refused_untouched(tmp_path):
    checkout = make_checkout(tmp_path)
    put(checkout.out / 'evidence.txt', 'keep')
    before = tree(checkout.out)
    runner = FakeRunner(checkout)
    error = refused(lambda: build(checkout, runner).execute())
    assert error.code == 'out_exists' and error.paths == [C_REL]
    assert tree(checkout.out) == before and runner.calls == []
    assert build(checkout).plan()['problems'][0]['refused'] == 'out_exists'


GUARD_CASES = {
    'resources': ('pyinstaller', 'backend', lambda root: (root / 'src-tauri/resources/model.json').write_text('{}\n'),
                  ['src-tauri/resources/model.json']),
    'spec': ('pyinstaller', 'backend',
             lambda root: (root / bca.SPEC).write_text((root / bca.SPEC).read_text() + '# regenerated\n'), [bca.SPEC]),
    'dist': ('vite', 'frontend', lambda root: put(root / 'dist/assets/new.js', 'new();'), ['dist/assets/new.js']),
    'gen': ('tauri-release', 'shell-release',
            lambda root: (root / 'src-tauri/gen/schemas/acl-manifests.json').write_text('{"acl": 2}\n'),
            ['src-tauri/gen/schemas/acl-manifests.json']),
    'target': ('tauri-test', 'shell-test', lambda root: put(root / 'src-tauri/target/release/pebble', b'rebuilt', 0o755),
               ['src-tauri/target/release/pebble']),
}


@pytest.mark.parametrize('case', sorted(GUARD_CASES))
def test_the_guard_refuses_any_write_to_a_shared_path(tmp_path, case):
    kind, step, change, paths = GUARD_CASES[case]
    checkout = make_checkout(tmp_path)
    runner = FakeRunner(checkout, hooks={kind: lambda *args: change(checkout.root)})
    error = refused(lambda: build(checkout, runner).execute())
    assert error.code == 'shared_paths_changed' and error.paths == paths
    record = inputs(checkout)
    assert record['failure']['step'] == step and record['failure']['paths'] == paths
    assert record['steps'][step]['status'] == 'failed' and record['result'] is None
    assert all(record['steps'][later]['status'] == 'pending' for later in bca.STEPS[bca.STEPS.index(step) + 1:])


def test_a_tokenizer_mismatch_is_refused_before_anything_runs(tmp_path):
    checkout = make_checkout(tmp_path)
    altered = checkout.root / 'src-tauri/resources/tokenizer' / token_budget.TOKENIZER_FILE
    altered.write_bytes(altered.read_bytes() + b'\n')
    runner = FakeRunner(checkout)
    error = refused(lambda: build(checkout, runner).execute())
    assert error.code == 'tokenizer_mismatch'
    assert error.paths == ['src-tauri/resources/tokenizer/' + token_budget.TOKENIZER_FILE]
    assert runner.calls == [] and not checkout.out.exists()
    # The same check, against the imported token_budget constant, also guards the staged copy.
    assert bca.TOKENIZER_SHA256 is token_budget.TOKENIZER_SHA256
    assert bca.tokenizer_problem(altered.parent) == altered
    assert bca.tokenizer_problem(tmp_path / 'absent') == tmp_path / 'absent' / token_budget.TOKENIZER_FILE
    assert bca.tokenizer_problem(put(tmp_path / 'good' / token_budget.TOKENIZER_FILE, TOKENIZER.read_bytes()).parent) is None


def test_symlinks_that_leave_the_stage_are_refused_by_their_source_paths(tmp_path):
    checkout = make_checkout(tmp_path)
    licenses = checkout.root / 'src-tauri/resources/licenses'
    (licenses / 'absolute-LICENSE').symlink_to('/etc/hosts')
    (licenses / 'escape-LICENSE').symlink_to('../../../../outside')
    llama = tmp_path.resolve() / 'llama'
    put(llama / 'llama-server', b'server', 0o755)
    (llama / 'libggml.dylib').symlink_to(str(tmp_path.resolve() / 'elsewhere.dylib'))
    error = refused(lambda: build(checkout, llama_from=llama).execute())
    assert error.code == 'stage_unsafe'
    assert error.paths == sorted(['src-tauri/resources/licenses/absolute-LICENSE',
                                  'src-tauri/resources/licenses/escape-LICENSE', str(llama / 'libggml.dylib')])
    assert {item['path']: item['code'] for item in error.detail['problems']} == {
        'licenses/absolute-LICENSE': 'link_absolute', 'licenses/escape-LICENSE': 'link_escapes_root',
        'llama/libggml.dylib': 'link_absolute'}
    assert inputs(checkout)['failure']['step'] == 'stage'


def test_bundle_resources_must_equal_the_stage_file_for_file(tmp_path):
    checkout = make_checkout(tmp_path)

    def tamper(argv, cwd, env, stream):
        resources = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos/Pebble.app/Contents/Resources/resources'
        (resources / 'licenses/tiktoken-LICENSE').write_text('changed\n')
        put(resources / 'extra.txt', 'extra')
        (resources / 'llama/llama-server').chmod(0o700)
    error = refused(lambda: build(checkout, FakeRunner(checkout, hooks={'tauri-release': tamper})).execute())
    assert error.code == 'bundle_resources_differ'
    assert error.paths == ['extra.txt', 'licenses/tiktoken-LICENSE', 'llama/llama-server']
    assert {item['path']: item['problem'] for item in error.detail['differences']} == {
        'extra.txt': 'extra', 'licenses/tiktoken-LICENSE': 'content_differs', 'llama/llama-server': 'mode_differs'}
    assert error.detail['bundle'] == C_REL + '/release/Pebble.app'
    assert not (checkout.out / 'app-manifest.json').exists()


def test_the_bundled_and_staged_backend_must_equal_the_backend_step(tmp_path):
    checkout = make_checkout(tmp_path)

    def swap(argv, cwd, env, stream):
        app = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos/Pebble Candidate.app'
        put(app / 'Contents/Resources/resources/backend/pebble-backend', b'another backend', 0o755)
    error = refused(lambda: build(checkout, FakeRunner(checkout, hooks={'tauri-test': swap})).execute())
    assert error.code == 'bundled_backend_differs'
    assert error.paths == [C_REL + '/test/Pebble Candidate.app/Contents/Resources/resources/backend/pebble-backend']
    assert error.detail == {'expected': hashlib.sha256(BACKEND_BYTES).hexdigest(),
                            'actual': hashlib.sha256(b'another backend').hexdigest()}

    second = make_checkout(tmp_path / 'second')

    def corrupt(argv, cwd, env, stream):
        if argv[2].endswith('/stage/resources/backend'):
            put(Path(argv[2]) / '_internal/base_library.zip', b'other')
    error = refused(lambda: build(second, FakeRunner(second, hooks={'ditto': corrupt})).execute())
    assert error.code == 'staged_backend_differs' and error.paths == ['backend/_internal/base_library.zip']


def test_commands_never_write_shared_paths_or_inherit_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('APPLE_SIGNING_IDENTITY', 'Developer ID Application: Someone')
    monkeypatch.setenv('TAURI_SIGNING_PRIVATE_KEY', 'secret')
    checkout = make_checkout(tmp_path)
    C, shared = checkout.out, checkout.root / 'src-tauri/resources'
    plan = build(checkout).plan()
    runner = FakeRunner(checkout)
    build(checkout, runner).execute()
    argvs = [command['argv'] for step in plan['steps'] for command in step['commands']] + [call['argv'] for call in runner.calls]
    for argv in argvs:
        assert not any('package_backend' in arg or arg.startswith(('--specpath', '--clean')) for arg in argv)
        assert '.app/' not in argv[0]
        targets = [argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg in ('--distpath', '--workpath', '--outDir')]
        targets += [argv[-1]] if argv[0] == bca.DITTO else []
        assert all(bca.within(target, C) and not bca.within(target, shared) for target in targets)
    for call in runner.calls:
        assert set(call['env']) <= ENV_NAMES and bca.within(call['env']['HOME'], C)
        assert call['env'].get('CARGO_TARGET_DIR', str(C / 'target')) == str(C / 'target')
    assert all(bca.within(path, C) for step in plan['steps'] for path in step['writes'])

    unsafe = [
        {'argv': ['python', str(checkout.root / 'scripts/package_backend.py')]},
        {'argv': ['python', '-m', 'PyInstaller', '--specpath', str(checkout.root / '.runtime'), 'x.spec']},
        {'argv': ['python', '-m', 'PyInstaller', '--clean', '--distpath', str(C / 'backend/dist'), 'x.spec']},
        {'argv': ['python', '-m', 'PyInstaller', '--distpath', str(shared / 'backend'), 'x.spec']},
        {'argv': [bca.DITTO, str(C / 'stage/resources'), str(shared / 'backend')]},
        {'argv': [str(checkout.root / 'node_modules/.bin/vite'), 'build'], 'writes': [str(checkout.root / 'dist')]},
        {'argv': ['tauri', 'build'], 'env': {'CARGO_TARGET_DIR': str(checkout.root / 'src-tauri/target')}},
        {'argv': [str(C / 'release/Pebble.app/Contents/MacOS/pebble')]},
    ]
    for command in unsafe:
        error = refused(lambda: bca.check_command(dict({'label': 'x', 'env': {}, 'writes': []}, **command),
                                                  checkout.root, C))
        assert error.code == 'unsafe_command' and error.detail['reasons']


def test_resume_runs_only_unfinished_steps_and_moves_failed_outputs_aside(tmp_path):
    checkout = make_checkout(tmp_path)
    C = checkout.out
    error = refused(lambda: build(checkout, FakeRunner(checkout, fail={'tauri-test'})).execute())
    assert error.code == 'command_failed' and error.paths == [C_REL + '/logs/shell-test.1.tauri-build.log']
    record = inputs(checkout)
    assert record['failure']['step'] == 'shell-test' and record['failure']['detail']['exit_code'] == 1
    assert [step for step in bca.STEPS if record['steps'][step]['status'] == 'done'] == list(bca.STEPS[:5])
    assert refused(lambda: build(checkout).execute()).code == 'out_exists'

    planned = build(checkout).plan(resume=True)
    assert [step['step'] for step in planned['steps']] == ['shell-test', 'copy-out', 'guard-after']
    assert planned['steps'][0]['commands'][0]['log'] == str(C / 'logs/shell-test.2.tauri-build.log')
    resumed = FakeRunner(checkout)
    result = build(checkout, resumed).execute(resume=True)
    assert result['ran'] == ['shell-test', 'copy-out', 'guard-after'] and result['result'] == 'built'
    assert [call['kind'] for call in resumed.calls] == ['tauri-test', 'ditto', 'codesign', 'ditto', 'codesign']
    attempts = inputs(checkout)['steps']['shell-test']['attempts']
    assert [attempt['status'] for attempt in attempts] == ['failed', 'done']
    assert attempts[1]['moved_aside'] == [{'from': C_REL + '/tauri.test.json',
                                           'to': C_REL + '/aside/shell-test.attempt1/tauri.test.json'}]
    assert (C / 'aside/shell-test.attempt1/tauri.test.json').is_file()  # moved aside, never deleted
    assert (C / 'logs/shell-test.1.tauri-build.log').read_bytes() == b'fake failure\n'

    assert build(checkout, FakeRunner(checkout)).execute(resume=True)['ran'] == []
    error = refused(lambda: build(checkout, shell_from_copy=True).execute(resume=True))
    assert error.code == 'resume_mismatch' and 'shell_mode' in error.paths


def test_steps_run_in_order_one_build_at_a_time(tmp_path):
    checkout = make_checkout(tmp_path)
    error = refused(lambda: build(checkout).execute(steps=('backend',)))
    assert error.code == 'steps_out_of_order' and error.paths == ['guard-before'] and not checkout.out.exists()
    stray = put(checkout.root / 'src-tauri/resources/stray.bin', b'x')
    error = refused(lambda: build(checkout).execute())
    assert error.code == 'unexpected_resource' and error.paths == ['src-tauri/resources/stray.bin']
    stray.unlink()

    assert build(checkout).execute(steps=('guard-before',))['ran'] == ['guard-before']
    lock = os.open(checkout.out / '.build.lock', os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert refused(lambda: build(checkout).execute(resume=True)).code == 'build_locked'
    finally:
        os.close(lock)
    runner = FakeRunner(checkout)
    assert build(checkout, runner).execute(steps=('backend',), resume=True)['ran'] == ['backend']
    assert [call['kind'] for call in runner.calls] == ['pyinstaller']
    (checkout.root / 'dist/index.html').write_text('<rebuilt/>')  # a shared write between invocations
    error = refused(lambda: build(checkout).execute(resume=True))
    assert error.code == 'shared_paths_changed' and error.paths == ['dist/index.html']


def test_the_snapshot_binds_the_sources_the_spec_and_the_tool(tmp_path):
    checkout = make_checkout(tmp_path)
    (checkout.root / 'backend/entry.py').write_text('print("changed")\n')
    error = refused(lambda: build(checkout).execute())
    assert error.code == 'source_differs_from_snapshot' and error.paths == ['backend/entry.py']
    assert not checkout.out.exists()

    verifier = checkout.root / 'scripts/verify_harness_candidate.py'
    verifier.write_text(verifier.read_text() + '# edited\n')
    snap(checkout)
    error = refused(lambda: build(checkout).execute())
    assert error.code == 'tool_differs_from_snapshot' and error.paths == ['scripts/verify_harness_candidate.py']

    verifier.write_bytes((REPO / 'scripts/verify_harness_candidate.py').read_bytes())
    (checkout.root / bca.SPEC).write_text("a = Analysis(['/elsewhere/checkout/backend/main.py'])\n")
    snap(checkout)
    error = refused(lambda: build(checkout).execute())
    assert error.code == 'spec_outside_root' and error.paths == ['/elsewhere/checkout/backend/main.py']
    assert not checkout.out.exists()


def test_shell_from_copy_builds_from_files_equal_to_the_snapshot(tmp_path):
    checkout = make_checkout(tmp_path)
    C, shell = checkout.out, checkout.out / 'shell-src'
    runner = FakeRunner(checkout)
    assert build(checkout, runner, shell_from_copy=True).execute()['result'] == 'built'
    assert [call['cwd'] for call in runner.calls if call['kind'].startswith('tauri')] == [str(shell)] * 2
    snapshot = json.loads(checkout.snapshot.read_text())
    record = json.loads((C / 'shell-src-manifest.json').read_text())
    for rel, digest in record['files'].items():
        assert (shell / rel).read_bytes() == (checkout.root / rel).read_bytes() and digest == sha(checkout.root / rel)
        assert snapshot.get(rel, digest) == digest
    assert {'src-tauri/tauri.conf.json', 'src-tauri/Cargo.lock', 'src-tauri/src/main.rs', 'package.json'} <= set(record['in_snapshot'])
    assert 'src-tauri/icons/icon.icns' in record['not_in_snapshot']
    assert not any(rel.startswith(('src-tauri/target', 'src-tauri/resources', 'src-tauri/gen')) for rel in record['files'])
    assert json.loads((C / 'tauri.release.json').read_text()) == {'identifier': 'com.pebble.local',
                                                                  'build': {'beforeBuildCommand': ''}}
    assert json.loads((C / 'tauri.test.json').read_text()) == {'identifier': 'com.pebble.candidate',
                                                               'productName': 'Pebble Candidate',
                                                               'build': {'beforeBuildCommand': ''}}
    manifest = json.loads((C / 'app-manifest.json').read_text())
    assert manifest['shell_mode'] == 'shell-from-copy' and manifest['apps']['release']['resources_equal_stage']

    second = make_checkout(tmp_path / 'second')

    def regenerate(argv, cwd, env, stream):
        (Path(cwd) / 'src-tauri/gen/schemas/acl-manifests.json').write_text('{"acl": 3}\n')
    error = refused(lambda: build(second, FakeRunner(second, hooks={'tauri-release': regenerate}),
                                  shell_from_copy=True).execute())
    assert error.code == 'generated_files_differ' and error.paths == ['src-tauri/gen/schemas/acl-manifests.json']


def test_flattened_links_are_refused_unless_explicitly_accepted(tmp_path):
    # Phase 21 (P21-build): the stage has no backend links any more, so this covers the remaining shapes (llama's).
    checkout = make_checkout(tmp_path)
    error = refused(lambda: build(checkout, FakeRunner(checkout, flatten=True)).execute())
    assert error.code == 'bundle_resources_differ' and error.paths == LINKS
    assert {item['problem'] for item in error.detail['differences']} == {'link_differs'}

    second = make_checkout(tmp_path / 'second')
    result = build(second, FakeRunner(second, flatten=True), allow_flattened_links=True).execute()
    assert result['result'] == 'built'
    manifest = json.loads((second.out / 'app-manifest.json').read_text())
    assert manifest['apps']['release']['flattened_links'] == LINKS


def plain_llama(tmp_path):
    """A --llama-from folder without links, so that every stage link is the backend's, as in the first real build."""
    llama = tmp_path.resolve() / 'llama-plain'
    put(llama / 'llama-server', b'\xcf\xfa\xed\xfe llama-server', 0o755)
    put(llama / 'libllama.0.dylib', b'\xcf\xfa\xed\xfe libllama', 0o755)
    return llama


def links_in(path):
    return sorted(rel for rel, (kind, _mode, _content) in tree(path).items() if kind == stat.S_IFLNK)


def test_the_stage_flattens_backend_links_so_a_link_dropping_bundler_matches_it(tmp_path):
    """The first real build: Tauri's resource copy dropped all 22 backend links, Python included, and copy-out
    refused. The stage now holds what each link names, so the bundle equals it exactly, with no flag."""
    checkout = make_checkout(tmp_path)
    C, llama = checkout.out, plain_llama(tmp_path)
    runner = FakeRunner(checkout, hooks={'pyinstaller': framework}, drop=True)
    assert build(checkout, runner, llama_from=llama).execute()['result'] == 'built'

    dist, staged = C / 'backend/dist/pebble-backend', C / 'stage/resources/backend'
    assert links_in(C / 'stage/resources') == [] and len(links_in(dist)) == len(FRAMEWORK_LINKS)  # the dist keeps them
    after = tree(staged)
    python = (dist / '_internal/Python.framework/Versions/3.12/Python').read_bytes()
    for rel in ('_internal/Python', '_internal/Python.framework/Python', '_internal/Python.framework/Versions/3.12/Python',
                '_internal/Python.framework/Versions/Current/Python',
                '_internal/Python.framework/Versions/3.12/lib/libpython3.12.dylib',
                '_internal/Python.framework/Versions/Current/lib/libpython3.12.dylib'):
        assert after[rel] == (stat.S_IFREG, 0o755, python), rel  # a byte-identical copy with the target's mode
    for rel in ('_internal/Python.framework/Versions/Current', '_internal/Python.framework/Resources'):
        assert after[rel][0] == stat.S_IFDIR  # a real directory, as in the installed 0.4.2 app
    assert after['_internal/Python.framework/Resources/Info.plist'] == (stat.S_IFREG, 0o644, b'<plist/>')
    assert after['_internal/libwebp.7.dylib'] == (stat.S_IFREG, 0o755, DYLIB_BYTES)

    record = inputs(checkout)
    backend = record['steps']['backend']['attempts'][-1]['evidence']
    stage = json.loads((C / 'stage/stage-manifest.json').read_text())
    assert stage['links'] == [] and stage['flattened_links'] == FRAMEWORK_LINKS
    assert record['steps']['stage']['attempts'][-1]['evidence']['flattened_links'] == len(FRAMEWORK_LINKS)
    assert (backend['links'], backend['directory_links']) == (len(FRAMEWORK_LINKS), 2)
    assert backend['links_sha256'] == bca.links_digest([dict(link, path=link['path'][len('backend/'):],
                                                             target=link['target'][len('backend/'):])
                                                        for link in FRAMEWORK_LINKS])
    with Root(str(staged), 'test') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems and not links
    assert bundle_digest(entries) == backend['flattened_bundle_sha256'] == stage['backend']['flattened_bundle_sha256']
    assert len(entries) == backend['flattened_file_count'] > backend['file_count']

    manifest = json.loads((C / 'app-manifest.json').read_text())
    assert record['allow_flattened_links'] is False
    for kind in bca.APPS:
        app = manifest['apps'][kind]
        assert app['resources_equal_stage'] is True and app['flattened_links'] == [] and app['links'] == []
        assert app['subject']['candidate_bundle_sha256'] == backend['flattened_bundle_sha256']
    assert manifest['candidate_bundle_sha256'] == backend['flattened_bundle_sha256']
    assert manifest['dist_bundle_sha256'] == backend['candidate_bundle_sha256']

    # An existing C is still refused, untouched.
    before = tree(C)
    error = refused(lambda: build(checkout, llama_from=llama).execute())
    assert error.code == 'out_exists' and error.paths == [C_REL] and tree(C) == before

    # Other link shapes stay strict: a dropped file link outside backend/ is refused, with or without the flag.
    for allow in (False, True):
        other = make_checkout(tmp_path / ('other-%s' % allow))
        error = refused(lambda: build(other, FakeRunner(other, drop=True), allow_flattened_links=allow).execute())
        assert error.code == 'bundle_resources_differ' and set(error.paths) == set(LINKS)
        assert {item['problem'] for item in error.detail['differences']} == {'missing', 'link_differs'}


def test_a_tampered_flattened_file_is_refused_with_its_path(tmp_path):
    checkout = make_checkout(tmp_path)
    current = 'backend/_internal/Python.framework/Versions/Current/Python'  # exists only because Current was flattened

    def tamper(argv, cwd, env, stream):
        resources = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos/Pebble.app/Contents/Resources/resources'
        (resources / current).write_bytes(b'\xcf\xfa\xed\xfe another python')
    error = refused(lambda: build(checkout, FakeRunner(checkout, hooks={'tauri-release': tamper})).execute())
    assert error.code == 'bundle_resources_differ' and error.paths == [current]
    assert error.detail['differences'] == [{'path': current, 'problem': 'content_differs'}]

    # Changed in the stage between steps: the shell step refuses before it builds anything.
    second = make_checkout(tmp_path / 'second')
    assert build(second).execute(steps=bca.STEPS[:bca.STEPS.index('stage') + 1])['result'] is None
    (second.out / 'stage/resources/backend/_internal/Python').write_bytes(b'\xcf\xfa\xed\xfe another python')
    runner = FakeRunner(second)
    error = refused(lambda: build(second, runner).execute(resume=True))
    assert error.code == 'stage_changed' and error.paths == ['backend/_internal/Python'] and runner.calls == []
    assert os.readlink(second.out / 'backend/dist/pebble-backend/_internal/Python') == 'Python.framework/Versions/3.12/Python'


def test_backend_links_that_leave_the_stage_or_cycle_are_refused_before_any_write(tmp_path):
    checkout = make_checkout(tmp_path)

    def plant(argv, cwd, env, stream):
        if argv[2].endswith('/stage/resources/backend'):
            internal = Path(argv[2]) / '_internal'
            (internal / 'escape.dylib').symlink_to('../../outside.dylib')
            (internal / 'absolute.dylib').symlink_to('/etc/hosts')
            # V1-045: the fixture backend no longer has a pillow_heif folder; the cycle sits in a package folder.
            (internal / 'tiktoken').mkdir()
            (internal / 'tiktoken/up').symlink_to('..')  # a directory that holds itself
    error = refused(lambda: build(checkout, FakeRunner(checkout, hooks={'ditto': plant})).execute())
    assert error.code == 'stage_link_unsafe'
    assert error.paths == ['backend/_internal/absolute.dylib', 'backend/_internal/escape.dylib',
                           'backend/_internal/tiktoken/up']
    assert error.detail == {'root': C_REL + '/stage/resources', 'problems': [
        {'code': 'link_absolute', 'path': 'backend/_internal/absolute.dylib'},
        {'code': 'link_escapes_root', 'path': 'backend/_internal/escape.dylib'},
        {'code': 'link_cycle', 'path': 'backend/_internal/tiktoken/up'}]}
    assert inputs(checkout)['failure']['step'] == 'stage'
    # Refused before any write: the staged backend still holds every link ditto copied.
    staged = checkout.out / 'stage/resources/backend/_internal'
    assert links_in(staged) == ['Python', 'Python.framework/Python', 'Python.framework/Versions/Current',
                                'absolute.dylib', 'escape.dylib', 'tiktoken/up']


def test_a_stage_that_differs_from_the_flattened_dist_is_refused(tmp_path):
    checkout = make_checkout(tmp_path)

    def rewrite(argv, cwd, env, stream):
        if argv[2].endswith('/stage/resources/backend'):
            (Path(argv[2]) / '_internal/Python.framework/Versions/3.12/Python').write_bytes(b'\xcf\xfa\xed\xfe other')
    error = refused(lambda: build(checkout, FakeRunner(checkout, hooks={'ditto': rewrite})).execute())
    assert error.code == 'staged_backend_differs'
    assert error.paths == ['backend/_internal/Python', 'backend/_internal/Python.framework/Python', PYTHON,
                           'backend/_internal/Python.framework/Versions/Current/Python']
    assert {item['problem'] for item in error.detail['differences']} == {'content_differs'}

    second = make_checkout(tmp_path / 'second')

    def relink(argv, cwd, env, stream):
        if argv[2].endswith('/stage/resources/backend'):
            link = Path(argv[2]) / '_internal/Python'
            link.unlink()
            link.symlink_to('base_library.zip')
    error = refused(lambda: build(second, FakeRunner(second, hooks={'ditto': relink})).execute())
    assert error.code == 'staged_backend_differs' and error.paths == ['backend/_internal/Python']
    assert [item['problem'] for item in error.detail['differences']] == ['content_differs', 'flattened_link_differs']
    assert error.detail['differences'][1]['actual'] == {'path': '_internal/Python', 'target': '_internal/base_library.zip',
                                                         'kind': 'file'}

    # The dist itself changed after the backend step in a way only its flattened view shows: a directory link
    # retargeted to another real directory whose Python has the same bytes. The walker's digest does not follow it.
    third = make_checkout(tmp_path / 'third')

    def spare(argv, cwd, env, stream):
        versions = Path(argv[argv.index('--distpath') + 1]) / 'pebble-backend/_internal/Python.framework/Versions'
        put(versions / 'spare/Python', (versions / '3.12/Python').read_bytes(), 0o755)
        put(versions / 'spare/extra.txt', 'spare\n')
    steps = bca.STEPS[:bca.STEPS.index('frontend') + 1]
    assert build(third, FakeRunner(third, hooks={'pyinstaller': spare})).execute(steps=steps)['result'] is None
    current = third.out / 'backend/dist/pebble-backend/_internal/Python.framework/Versions/Current'
    current.unlink()
    current.symlink_to('spare')
    assert build(third)._candidate_manifest()['bundle_sha256'] == (
        inputs(third)['steps']['backend']['attempts'][-1]['evidence']['candidate_bundle_sha256'])
    error = refused(lambda: build(third).execute(resume=True))
    assert error.code == 'backend_output_changed' and error.paths == [C_REL + '/backend/dist/pebble-backend']


def test_flattening_gives_the_tree_package_backend_copytree_shipped(tmp_path):
    """package_backend.py's shutil.copytree followed every link; the installed 0.4.2 app it built has no links and
    passes codesign --verify --deep --strict. Flattening a ditto-like copy gives that tree, entry for entry."""
    base = tmp_path.resolve()
    dist = base / 'dist'
    fake_dist(dist)
    more_links(dist / '_internal')
    shipped, staged = base / 'copytree', base / 'stage'
    shutil.copytree(dist, shipped)  # package_backend.py: links followed
    shutil.copytree(dist, staged, symlinks=True)  # ditto: links kept
    replaced = bca.flatten_links(staged, 'test', 'stage')
    assert links_in(staged) == links_in(shipped) == [] and tree(staged) == tree(shipped)
    expected = bca.tree_manifest(shipped, 'test')
    for manifest in (bca.tree_manifest(staged, 'test'), bca.flat_manifest(dist, 'test', 'dist')):
        assert (manifest['files'], manifest['modes'], manifest['links']) == (expected['files'], expected['modes'], [])
        assert manifest['bundle_sha256'] == expected['bundle_sha256']
    assert replaced == bca.flat_manifest(dist, 'test', 'dist')['flattened'] == [
        dict(link, path=link['path'][len('backend/'):], target=link['target'][len('backend/'):]) for link in FRAMEWORK_LINKS]

    # Two directory links that hold each other are a cycle, refused with both paths before anything is written.
    loop = base / 'loop'
    put(loop / 'a/file', b'a')
    put(loop / 'b/file', b'b')
    (loop / 'a/to-b').symlink_to('../b')
    (loop / 'b/to-a').symlink_to('../a')
    before = tree(loop)
    error = refused(lambda: bca.flatten_links(loop, 'test', 'loop'))
    assert error.code == 'test' and error.paths == ['a/to-b', 'b/to-a']
    assert {item['code'] for item in error.detail['problems']} == {'link_cycle'} and tree(loop) == before

    # Private paths, and links to them, are refused as the release_readiness walker refuses them.
    private = base / 'private'
    put(private / 'secrets/token', b'never copied')
    (private / 'token').symlink_to('secrets/token')
    error = refused(lambda: bca.flat_manifest(private, 'test', 'private'))
    assert error.paths == ['secrets', 'secrets/token', 'token']
    assert {item['path']: item['code'] for item in error.detail['problems']} == {
        'secrets': 'private_path', 'secrets/token': 'private_path', 'token': 'link_to_private_path'}


def test_the_real_runner_passes_only_the_given_environment_and_logs_output(tmp_path, monkeypatch):
    monkeypatch.setenv('PEBBLE_INHERITED', 'leaked')
    log = tmp_path / 'command.log'
    with open(log, 'wb') as stream:
        code = bca.run_command(['/bin/sh', '-c', 'echo "$HOME:${PEBBLE_INHERITED:-none}"; echo oops >&2; exit 3'],
                               str(tmp_path), {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)}, stream)
    assert code == 3 and log.read_text() == str(tmp_path) + ':none\noops\n'


def pack(argv, cwd, env, stream):
    packed = b'\xcf\xfa\xed\xfe' + bytes(64) + b'UPX!' + bytes(8)
    put(Path(argv[argv.index('--distpath') + 1]) / 'pebble-backend/_internal/libpacked.dylib', packed, 0o755)


def mislabel(argv, cwd, env, stream):
    plist = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos/Pebble Candidate.app/Contents/Info.plist'
    plist.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.pebble.local'}))


def announce(argv, cwd, env, stream):
    stream.write(b'Running beforeBuildCommand `npm run build`\n')


def heif_stack(argv, cwd, env, stream):
    """V1-045: a backend that collected pillow-heif's GPL x265."""
    put(Path(argv[argv.index('--distpath') + 1]) / 'pebble-backend/_internal/pillow_heif/.dylibs/libx265.216.dylib',
        b'\xcf\xfa\xed\xfe x265', 0o755)


def leak_checkout(argv, cwd, env, stream):
    """V1-023: a backend library that kept the checkout's absolute path (the spec is <root>/build/...)."""
    root = Path(argv[-1]).parents[1]
    put(Path(argv[argv.index('--distpath') + 1]) / 'pebble-backend/_internal/libleak.dylib',
        b'\xcf\xfa\xed\xfe panic at ' + str(root / 'src/lib.rs').encode(), 0o755)


def leak_home(argv, cwd, env, stream):
    """V1-023: a shell binary that kept a path in the builder's home folder."""
    shell = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos/Pebble.app/Contents/MacOS/pebble'
    shell.write_bytes(shell.read_bytes() + (str(Path.home()) + '/.cargo/registry/src/x.rs').encode())


REFUSALS = {
    'copyleft': ({'hooks': {'pyinstaller': heif_stack}}, 'copyleft_binary_in_backend',
                 [C_REL + '/backend/dist/pebble-backend/_internal/pillow_heif/.dylibs/libx265.216.dylib']),
    'machine-paths-backend': ({'hooks': {'pyinstaller': leak_checkout}}, 'machine_paths_in_stage',
                              ['backend/_internal/libleak.dylib']),
    'machine-paths-shell': ({'hooks': {'tauri-release': leak_home}}, 'machine_paths_in_shell',
                            [C_REL + '/release/Pebble.app/Contents/MacOS/pebble']),
    'upx': ({'hooks': {'pyinstaller': pack}}, 'upx_packed',
            [C_REL + '/backend/dist/pebble-backend/_internal/libpacked.dylib']),
    'before-build': ({'hooks': {'tauri-release': announce}}, 'before_build_command_ran',
                     [C_REL + '/logs/shell-release.1.tauri-build.log']),
    'identity': ({'hooks': {'tauri-test': mislabel}}, 'bundle_identity_mismatch',
                 [C_REL + '/test/Pebble Candidate.app/Contents/Info.plist']),
    'codesign': ({'fail': {'codesign'}}, 'command_failed', [C_REL + '/logs/copy-out.1.codesign-release.log']),
}


@pytest.mark.parametrize('case', sorted(REFUSALS))
def test_build_findings_stop_the_build(tmp_path, case):
    options, code, paths = REFUSALS[case]
    checkout = make_checkout(tmp_path)
    error = refused(lambda: build(checkout, FakeRunner(checkout, **options)).execute())
    assert error.code == code and error.paths == paths
    assert inputs(checkout)['failure']['refused'] == code and not (checkout.out / 'app-manifest.json').exists()


# Phase 24 (t24-build) -------------------------------------------------------------------------------------------


def test_the_spec_is_relative_and_leaves_out_the_heif_stack_and_unused_agentguard():
    """V1-048, V1-076, V1-143, V1-045: the release spec builds from any clone and ships only what Pebble imports."""
    text = (REPO / bca.SPEC).read_text()
    assert bca.SPEC == 'build/pebble-backend.spec' and 'SPECPATH' in text
    assert not __import__('re').findall(r"""['"](/[^'"\n]+)['"]""", text)  # no absolute path at all
    for module in ('agentguard.daemon.app', 'agentguard.cli', 'agentguard.adapters', 'agentguard.bench',
                   'agentguard.census', 'fastapi', 'starlette', 'uvicorn', 'pillow_heif'):
        assert "'%s'" % module in text, module
    # agentguard.daemon.lifecycle stays: core/config.py imports it lazily.
    assert "'agentguard.daemon'" not in text
    flags = __import__('runpy').run_path(str(REPO / 'scripts/package_backend.py'))['PYINSTALLER_FLAGS']
    excluded = {flags[i + 1] for i, flag in enumerate(flags) if flag == '--exclude-module'}
    assert excluded == {m.strip("'") for m in __import__('re').findall(r"'[A-Za-z0-9_.]+'", text.split('EXCLUDED =')[1]
                                                                      .split('LANGUAGES')[0])} | {
        'agentguard.daemon.app', 'agentguard.daemon.__main__', 'agentguard.cli', 'agentguard.adapters',
        'agentguard.bench', 'agentguard.census'}


def test_the_rust_toolchain_pin_names_the_toolchain_the_build_uses():
    assert bca.toolchain_channel((REPO / bca.TOOLCHAIN_PIN).read_text()) == bca.TOOLCHAIN.split('-')[0]
    assert bca.toolchain_channel('not toml [') is None


def test_the_lock_and_the_build_environment_are_checked_before_anything_runs(tmp_path):
    """V1-076: an unhashed lock, a lock that misses a requirement and a toolchain pin that differs are refused in
    preflight; a build environment that differs from the lock stops guard-before."""
    checkout = make_checkout(tmp_path)
    lock = checkout.root / bca.LOCK
    lock.write_text('# [runtime]\nPillow==13.0.0\ncertifi==2026.10.19\n')
    snap(checkout)  # the lock is a tested source: each variant is snapshotted, so only the lock check refuses
    runner = FakeRunner(checkout)
    error = refused(lambda: build(checkout, runner).execute())
    assert error.code == 'lock_unhashed' and error.paths == [bca.LOCK] and runner.calls == []

    lock.write_text(LOCK_TEXT.replace('certifi==2026.10.19', 'certifi==2026.7.22'))
    snap(checkout)
    error = refused(lambda: build(checkout, runner).execute())
    assert error.code == 'lock_differs_from_requirements'
    assert error.detail['differences'] == [{'name': 'certifi', 'requirement': '2026.10.19', 'lock': '2026.7.22'}]

    lock.write_text(LOCK_TEXT)
    (checkout.root / bca.TOOLCHAIN_PIN).write_text('[toolchain]\nchannel = "1.90.0"\n')
    snap(checkout)
    error = refused(lambda: build(checkout, runner).execute())
    assert error.code == 'toolchain_pin_differs' and error.detail['channel'] == '1.90.0'
    assert runner.calls == [] and not checkout.out.exists()

    second = make_checkout(tmp_path / 'second')
    developer = FakeRunner(second)
    developer.environment = BUILD_ENV + [['fastapi', '0.143.0'], ['pillow_heif', '1.8.0']]
    error = refused(lambda: build(second, developer).execute())
    assert error.code == 'build_env_differs_from_lock'
    assert error.detail['differences'] == [{'name': 'fastapi', 'lock': None, 'installed': '0.143.0'},
                                           {'name': 'pillow-heif', 'lock': None, 'installed': '1.8.0'}]
    assert inputs(second)['failure']['step'] == 'guard-before'


def test_rust_paths_are_remapped_and_the_environment_is_reproducible(tmp_path):
    """V1-023: the shell is compiled with every local prefix remapped, ordered so the deepest wins."""
    checkout = make_checkout(tmp_path)
    plan = build(checkout).plan()
    shells = [command for step in plan['steps'] for command in step['commands'] if command['label'] == 'tauri-build']
    flags = shells[0]['env']['CARGO_ENCODED_RUSTFLAGS'].split('\x1f')
    R, C = checkout.root, checkout.out
    assert all(flag.startswith('--remap-path-prefix=') for flag in flags)
    remaps = dict(flag[len('--remap-path-prefix='):].rsplit('=', 1) for flag in flags)
    assert remaps == {str(R): '/pebble', str(R / '.runtime/toolchain/rustup'): '/rustup',
                      str(R / '.runtime/toolchain/cargo'): '/cargo', str(C): '/build'}
    # rustc applies the last matching remap: the checkout comes before the folders inside it.
    sources = list(remaps)
    assert sources[0] == str(R) and [len(s) for s in sources] == sorted(len(s) for s in sources)
    assert shells[0]['env']['SOURCE_DATE_EPOCH'] == bca.SOURCE_DATE_EPOCH
    pyinstaller = next(command for step in plan['steps'] for command in step['commands'] if command['label'] == 'pyinstaller')
    assert pyinstaller['env']['PYTHONHASHSEED'] == '0' and pyinstaller['argv'][-1] == str(R / 'build/pebble-backend.spec')
    # A clean clone names its own toolchain homes.
    own = bca.Build(R, C, snapshot=checkout.snapshot, node_bin=checkout.node, rustup_home=tmp_path / 'rustup',
                    cargo_home=tmp_path / 'cargo').env('rust')
    assert own['RUSTUP_HOME'] == str(tmp_path / 'rustup') and own['CARGO_HOME'] == str(tmp_path / 'cargo')
    assert own['RUSTC'] == str(tmp_path / 'rustup/toolchains' / bca.TOOLCHAIN / 'bin/rustc')


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name + '_under_test_24', REPO / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_lock_tools_read_hash_and_install_only_through_the_runner(tmp_path):
    """scripts/package_backend.py (V1-076): its lock reader, the hash step and the build environment, with a fake
    pip; it never writes src-tauri/resources any more."""
    pb = load_tool('package_backend')
    pins = pb.read_lock((REPO / bca.LOCK).read_text())
    runtime = {pb.normalize(pin['name']): pin['version'] for pin in pins if pin['section'] == 'runtime'}
    assert runtime['certifi'] == '2026.10.19' and runtime['pillow'] == '13.0.0' and runtime['pyjwt'] == '2.15.0'
    assert 'pillow-heif' not in runtime and {'pyinstaller', 'pyinstaller-hooks-contrib'} <= {
        pb.normalize(pin['name']) for pin in pins if pin['section'] == 'build'}
    facts = pb.lock_check(REPO / bca.LOCK, REPO / 'backend/requirements.txt')
    assert facts['differences'] == [] and facts['runtime'] == 30 and facts['build'] == 6
    # f24-locked: the parent's release step (`package_backend.py lock-hashes --write`) hashed the repository lock, so the
    # released lock is complete: every one of its 36 pins carries exactly the one wheel SHA-256 that step writes. Before
    # that step this line asserted the opposite (unhashed, incomplete); that reading is kept below on the same pins
    # with their hashes removed, and so is build_env's refusal of such a lock.
    assert facts['pins'] == 36 and facts['unhashed'] == [] and facts['complete'] is True
    assert all(len(pin['hashes']) == 1 and pb.HASH.fullmatch('--hash=sha256:' + pin['hashes'][0]) for pin in pins)
    unhashed_lock = put(tmp_path / 'unhashed.lock', ''.join(
        f'# [{section}]\n' + ''.join(pin['name'] + '==' + pin['version'] + '\n' for pin in pins if pin['section'] == section)
        for section in ('runtime', 'build')))
    before = pb.lock_check(unhashed_lock, REPO / 'backend/requirements.txt')
    assert before['differences'] == [] and before['unhashed'] == sorted(pin['name'] for pin in pins)
    assert before['complete'] is False

    lock = put(tmp_path / 'requirements.lock', '# head\n# [runtime]\nalpha==1.0\nbeta==2.0 \\\n    --hash=sha256:%s\n'
               % ('b' * 64))
    calls = []

    def pip(argv):
        calls.append(argv)
        if argv[1:4] == ['-m', 'pip', 'download']:
            put(Path(argv[argv.index('--dest') + 1]) / (argv[-1].replace('==', '-') + '-py3-none-any.whl'), argv[-1])
        return 0, ''
    assert pb.lock_hashes(lock, python='/usr/bin/python3', runner=pip, write=True) == {'pins': 2, 'hashed': 2, 'written': True}
    hashed = pb.read_lock(lock.read_text())
    assert [pin['hashes'] for pin in hashed] == [[hashlib.sha256(b'alpha==1.0').hexdigest()],
                                                 [hashlib.sha256(b'beta==2.0').hexdigest()]]
    assert lock.read_text().startswith('# head\n# [runtime]\n')
    assert all('--no-deps' in argv and '--only-binary=:all:' in argv for argv in calls)
    calls.clear()
    result = pb.build_env(tmp_path / 'venv', lock, python='/usr/bin/python3', runner=lambda argv: calls.append(argv) or (0, ''))
    assert result['pins'] == 2 and [argv[1:3] for argv in calls] == [['-m', 'venv'], ['-m', 'pip']]
    assert {'--require-hashes', '--no-deps', '--only-binary=:all:'} <= set(calls[1])
    put(tmp_path / 'venv/bin/python', '')
    with pytest.raises(pb.Refused) as error:
        pb.build_env(tmp_path / 'venv', lock, runner=pip)
    assert error.value.code == 'venv_exists'
    # f24-locked: a lock with an unhashed pin is still refused before any command (the repository lock was that lock
    # until the release step hashed it); the released repository lock installs through the runner alone.
    calls.clear()
    with pytest.raises(pb.Refused) as error:
        pb.build_env(tmp_path / 'other', unhashed_lock, runner=pip)
    assert error.value.code == 'lock_unhashed' and error.value.detail == before['unhashed']
    assert calls == [] and not (tmp_path / 'other').exists()
    released = pb.build_env(tmp_path / 'other', REPO / bca.LOCK, runner=lambda argv: calls.append(argv) or (0, ''))
    assert released['pins'] == 36 and not (tmp_path / 'other').exists()  # the fake runner made nothing
    assert [argv[1:3] for argv in calls] == [['-m', 'venv'], ['-m', 'pip']] and calls[1][-1] == str(REPO / bca.LOCK)
    assert {'--require-hashes', '--no-deps', '--only-binary=:all:'} <= set(calls[1])
    assert pb.main([]) == 2  # the retired shared-resource build


def test_the_dmg_is_made_only_from_the_verified_candidate_with_system_tools(tmp_path):
    """scripts/package_dmg.py (V1-133): the release app must be the one its app manifest records."""
    dmg = load_tool('package_dmg')
    candidate = tmp_path / 'C'
    app = candidate / 'release/Pebble.app'
    put(app / 'Contents/Info.plist', plistlib.dumps({'CFBundleShortVersionString': '1.0.0'}))
    put(app / 'Contents/MacOS/pebble', b'\xcf\xfa\xed\xfe shell', 0o755)
    with Root(str(app), 'test') as root:
        digest = bundle_digest(bundle_entries(root)[0])
    manifest = {'release_app_bundle_sha256': digest,
                'apps': {'release': {'app_bundle_sha256': digest, 'info_plist': {'CFBundleShortVersionString': '1.0.0'}}}}
    put(candidate / 'app-manifest.json', json.dumps(manifest))
    out, calls = tmp_path / 'release', []

    def tools(argv):
        calls.append(argv)
        assert argv[0] in (dmg.CODESIGN, dmg.DITTO, dmg.HDIUTIL)
        if argv[0] == dmg.DITTO:
            shutil.copytree(argv[1], argv[2], symlinks=True)
        if argv[:2] == [dmg.HDIUTIL, 'create']:
            put(Path(argv[-1]), b'fake dmg')
        return 0, ''
    plan = dmg.package(candidate, out, runner=tools, arch='arm64')
    assert plan['mode'] == 'plan' and calls == [] and not out.exists()
    assert all(command[0].startswith('/usr/bin/') for command in plan['commands'])
    done = dmg.package(candidate, out, execute=True, runner=tools, arch='arm64')
    target = out / 'Pebble_1.0.0_arm64.dmg'
    assert done['sha256'] == hashlib.sha256(b'fake dmg').hexdigest() and done['app_bundle_sha256'] == digest
    assert done['developer_id'] is False and done['notarized'] is False
    assert json.loads((out / 'Pebble_1.0.0_arm64.dmg.json').read_text())['sha256'] == sha(target)
    assert [argv[:2] for argv in calls] == [[dmg.CODESIGN, '--verify'], [dmg.DITTO, str(app)],
                                            [dmg.HDIUTIL, 'create'], [dmg.HDIUTIL, 'verify']]
    with pytest.raises(dmg.Refused) as error:
        dmg.package(candidate, out, execute=True, runner=tools, arch='arm64')
    assert error.value.code == 'target_exists'
    put(app / 'Contents/Resources/extra.txt', 'changed after the build')
    with pytest.raises(dmg.Refused) as error:
        dmg.package(candidate, tmp_path / 'other', execute=True, runner=tools, arch='arm64')
    assert error.value.code == 'candidate_differs_from_manifest'


def test_the_verifier_has_a_phase24_namespace_and_covers_the_new_build_inputs(tmp_path):
    """verify_harness_candidate (api for t24-validator): phase24 runs every earlier suite plus validate_phase24.py;
    the spec, lock, developer requirements and toolchain pin are tested sources; the .runtime names earlier proofs
    hash are covered whenever they exist."""
    verify = load_tool('verify_harness_candidate')
    # Phase 25 follows phase24 in the campaign; phase24 still follows phase23 directly.
    at = verify.PHASES.index('phase24')
    assert verify.SUITES['phase24'] == 'validate_phase24.py' and verify.PHASES[at - 1:at + 1] == ('phase23', 'phase24')
    assert verify.default_suites('phase24') == verify.default_suites('phase23') + ['phase24']
    assert 'phase24' not in verify.default_suites('phase23')
    checkout = make_checkout(tmp_path)
    verify.ROOT = checkout.root
    names = set(verify.sources())
    assert {'build/pebble-backend.spec', 'backend/requirements.lock', 'rust-toolchain.toml'} <= names
    assert '.runtime/pebble-backend.spec' not in names
    put(checkout.root / '.runtime/pebble-backend.spec', '# historical\n')
    put(checkout.root / '.runtime/phase19-baseline/build_epoch8_fixture.py', '# historical\n')
    assert {'.runtime/pebble-backend.spec', '.runtime/phase19-baseline/build_epoch8_fixture.py'} <= set(verify.sources())


def test_the_machine_path_scan_reads_zip_members_and_reports_only_counts(tmp_path):
    import zipfile
    folder = tmp_path / 'stage'
    put(folder / 'backend/pebble-backend', b'\xcf\xfa\xed\xfe clean')
    with zipfile.ZipFile(put(folder / 'backend/_internal/base_library.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('os.pyc', b'compiled /Users/someone-else/lib and ' + str(tmp_path / 'checkout').encode())
    needles = bca.machine_needles(tmp_path / 'checkout')
    found = bca.machine_path_scan(folder, needles)
    assert found['files'] == 2 and found['paths'] == ['backend/_internal/base_library.zip']
    assert found['machine'] == {str(tmp_path / 'checkout'): 1} and found['users_prefix'] == 1
    assert bca.machine_needles('/Users/sam/Projects/x') == [b'/Users/sam/', b'/Users/sam/Projects/x']
