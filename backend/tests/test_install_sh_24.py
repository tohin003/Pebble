"""install.sh, the one-line installer (Phase 24, t24-publish; V1-022's publishing plan).

Every run is /bin/bash (macOS's bash 3.2, what `curl … | bash` uses) on the real install.sh, inside sandbox-exec:
  - USER_ONLY denies every write to /Applications and to the real home folder, so /Applications is not writable and
    the installer chooses ~/Applications in the test's own HOME;
  - SYSTEM_LOOKS_WRITABLE denies every write operation in /Applications except plain data writes, so
    `[ -w /Applications ]` is true while nothing there can be created, renamed or removed (used with --dry-run only).
Fakes first in PATH stand in for curl, uname, sw_vers, sysctl, ps, id, codesign and ditto, and record sudo, spctl
and xattr if they are ever called. They are bash shims that run one Python file, configured by FAKE_* variables the
test sets; install.sh reads none of them (a static test checks it reads only HOME and TMPDIR).
"""

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'install.sh'
REAL_HOME = os.path.realpath(os.path.expanduser('~'))
ZIP = 'Pebble-macos-arm64.zip'
LATEST = 'https://github.com/tohin003/Pebble/releases/latest/download/'
WRITE_OPS_BUT_DATA = ('file-write-create file-write-unlink file-write-mode file-write-flags file-write-xattr '
                      'file-write-times file-write-owner')
USER_ONLY = '(version 1)(allow default)(deny file-write* (subpath "/Applications") (subpath "%s"))' % REAL_HOME
SYSTEM_LOOKS_WRITABLE = ('(version 1)(allow default)(deny %s (subpath "/Applications"))(deny file-write* (subpath "%s"))'
                         % (WRITE_OPS_BUT_DATA, REAL_HOME))
TOOLS = ('curl', 'uname', 'sw_vers', 'sysctl', 'ps', 'id', 'codesign', 'ditto', 'sudo', 'spctl', 'xattr')
FAKE = r'''
import json, os, shutil, sys, zipfile
tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'tool': tool, 'argv': args}) + '\n')
env = os.environ.get
if tool == 'uname':
    print(env('FAKE_UNAME_M', 'arm64') if args == ['-m'] else env('FAKE_UNAME_S', 'Darwin'))
elif tool == 'sw_vers':
    print(env('FAKE_SW_VERS', '27.0'))
elif tool == 'sysctl':
    if args != ['-n', 'sysctl.proc_translated']:
        sys.exit(1)
    print(env('FAKE_TRANSLATED', '0'))
elif tool == 'ps':
    sys.stdout.write(env('FAKE_PS', '/sbin/launchd\n/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n'))
elif tool == 'id':
    if env('FAKE_UID') is not None and args == ['-u']:
        print(env('FAKE_UID'))
    else:
        os.execv('/usr/bin/id', ['id'] + args)
elif tool == 'curl':
    out = args[args.index('--output') + 1]
    served = json.load(open(env('FAKE_SERVE'))).get(args[-1])
    if served is None:
        sys.stderr.write('curl: (22) The requested URL returned error: 404\n')
        sys.exit(22)
    shutil.copyfile(served, out)
elif tool == 'codesign':
    counter = env('FAKE_LOG') + '.codesign'
    count = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
    open(counter, 'w').write(str(count))
    sys.exit(1 if str(count) in env('FAKE_CODESIGN_FAIL', '').split(',') else 0)
elif tool == 'ditto':
    if args[:2] != ['-x', '-k'] or len(args) != 4:
        sys.exit(1)
    with zipfile.ZipFile(args[2]) as archive:
        for info in archive.infolist():
            path = archive.extract(info, args[3])
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                os.chmod(path, mode & 0o7777)
else:  # sudo, spctl, xattr: recorded above, and refused
    sys.exit(1)
'''


def put(path, data=b'', mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    return path


def snapshot(folder):
    """Every entry under folder with its type, size, mode and mtime (a change anywhere shows)."""
    found = []
    for path in sorted(Path(folder).rglob('*')):
        info = os.lstat(path)
        found.append((path.relative_to(folder).as_posix(), stat.S_IFMT(info.st_mode), info.st_size,
                      stat.S_IMODE(info.st_mode), info.st_mtime_ns))
    return found


def applications_state():
    """Only what the tests could change: whether /Applications/Pebble.app exists, and the folder's own mtime."""
    return os.path.lexists('/Applications/Pebble.app'), os.stat('/Applications').st_mtime_ns


class Env:
    def __init__(self, tmp_path, python):
        if os.path.realpath(tmp_path).startswith(REAL_HOME + '/'):
            pytest.skip('The test folder is inside the real home folder, which the sandbox profile denies.')
        self.root = tmp_path
        self.home, self.tmp, self.bin = tmp_path / 'home', tmp_path / 'tmp', tmp_path / 'bin'
        for folder in (self.home, self.tmp, self.bin):
            folder.mkdir()
        self.log, self.serve_map = tmp_path / 'calls.jsonl', tmp_path / 'serve.json'
        self.log.write_text('')
        self.served = {}
        self.serve_map.write_text('{}')
        impl = put(tmp_path / 'fake_tool.py', FAKE)
        for tool in TOOLS:
            put(self.bin / tool, '#!/bin/bash\nexec "$FAKE_PYTHON" "$FAKE_IMPL" %s "$@"\n' % tool, 0o755)
        self.base_env = {'PATH': '%s:/usr/bin:/bin:/usr/sbin:/sbin' % self.bin, 'HOME': str(self.home),
                         'TMPDIR': str(self.tmp) + '/', 'LANG': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
                         'FAKE_LOG': str(self.log), 'FAKE_SERVE': str(self.serve_map), 'FAKE_PYTHON': python,
                         'FAKE_IMPL': str(impl)}

    def serve(self, url, path):
        self.served[url] = str(path)
        self.serve_map.write_text(json.dumps(self.served))

    def release(self, version='1.0.0', *, bundle_version=None, identifier='com.pebble.local', minimum='12.0',
                extra=None, sums=None, latest=True):
        """A release whose zip holds Pebble.app at its top (as ditto --keepParent makes it), and its SHA256SUMS."""
        folder = self.root / 'releases' / version
        archive = folder / ZIP
        archive.parent.mkdir(parents=True, exist_ok=True)
        info = {'CFBundleIdentifier': identifier, 'CFBundleShortVersionString': bundle_version or version,
                'CFBundleExecutable': 'pebble'}
        if minimum:
            info['LSMinimumSystemVersion'] = minimum
        files = {'Pebble.app/Contents/Info.plist': (plistlib.dumps(info), 0o644),
                 'Pebble.app/Contents/MacOS/pebble': (b'#!/bin/sh\necho fake shell\n', 0o755),
                 'Pebble.app/Contents/Resources/resources/release.txt': (('release ' + version).encode(), 0o644)}
        files.update(extra or {})
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
            for name, (data, mode) in files.items():
                entry = zipfile.ZipInfo(name)
                entry.external_attr = (stat.S_IFREG | mode) << 16
                zipped.writestr(entry, data)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        put(folder / 'SHA256SUMS', sums if sums is not None else '%s  %s\n' % (digest, ZIP))
        for base in ([LATEST] if latest else []) + ['https://github.com/tohin003/Pebble/releases/download/v%s/' % version]:
            self.serve(base + ZIP, archive)
            self.serve(base + 'SHA256SUMS', folder / 'SHA256SUMS')
        return digest

    def run(self, *args, profile=USER_ONLY, **fake):
        env = dict(self.base_env, **{key: str(value) for key, value in fake.items()})
        return subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, '/bin/bash', str(SCRIPT)] + list(args),
                              env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def calls(self, tool=None):
        entries = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [e['argv'] for e in entries if tool is None or e['tool'] == tool]

    def tools_called(self):
        return {json.loads(line)['tool'] for line in self.log.read_text().splitlines() if line}

    def leftover_temp(self):
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith('pebble-install.'))

    def user_app(self):
        return self.home / 'Applications' / 'Pebble.app'

    def backups(self, kind='pre-install'):
        root = self.home / 'PebbleBackups'
        return sorted(root.glob('*-%s.noindex' % kind)) if root.is_dir() else []


@pytest.fixture
def env(tmp_path):
    import sys
    return Env(tmp_path, sys.executable)


def assert_never_escalates(env):
    """No sudo, no Gatekeeper or quarantine change, and the installer's own temporary folder is gone."""
    assert not env.tools_called() & {'sudo', 'spctl', 'xattr'}
    assert env.leftover_temp() == []


# ---- The script itself -------------------------------------------------------------------------------------------


def test_the_script_has_no_test_hooks_never_escalates_and_matches_the_release_it_installs():
    text = SCRIPT.read_text()
    assert text.startswith('#!/bin/bash\n') and '\nset -euo pipefail\n' in text
    assert text.rstrip().splitlines()[-1] == 'main "$@"'  # a download cut short runs nothing
    code = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
    commands = re.sub(r'"(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\'', '""', code)  # messages and patterns removed
    for banned in ('sudo', 'spctl', 'xattr', 'pgrep', 'pkill', 'killall', 'kill ', 'osascript', 'launchctl', 'eval ',
                   'chown', 'chflags', 'lsregister'):
        assert not re.search(r'\b' + banned, commands), banned
    assert 'http://' not in text and text.count('https://') >= 3
    # Environment: only HOME and TMPDIR come from outside; every other variable is the script's own (assigned, a
    # local, a loop variable or read into).
    assigned = set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)=', code))
    for declared in re.findall(r'\blocal\s+(?:-a\s+)?([^\n]+)', code):
        assigned |= set(re.findall(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)', declared))
    assigned |= set(re.findall(r'\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b', code))
    assigned |= set(re.findall(r'\bread\s+-r(?:\s+-a)?\s+([A-Za-z_][A-Za-z0-9_]*)', code))
    used = set(re.findall(r'\$\{?#?([A-Za-z_][A-Za-z0-9_]*)', code))
    assert used - assigned == {'HOME', 'TMPDIR'}, used - assigned
    # The asset, the checksum line, the identifier, the minimum macOS and the repository match what is released.
    import importlib.util
    spec = importlib.util.spec_from_file_location('package_release_for_install_test', REPO / 'scripts/package_release.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    conf = json.loads((REPO / 'src-tauri/tauri.conf.json').read_text())
    assert 'ASSET="%s"' % release.ZIP in text and 'REPO="%s"' % release.REPOSITORY in text
    assert 'IDENTIFIER="%s"' % conf['identifier'] in text and release.IDENTIFIER == conf['identifier']
    assert 'MIN_MACOS="%s"' % conf['bundle']['macOS']['minimumSystemVersion'] in text
    line = re.search(r"SUMS_LINE='([^']+)'", text).group(1)
    assert re.fullmatch(line, 'a' * 64 + '  ' + release.ZIP) and not re.fullmatch(line, 'a' * 64 + ' ' + release.ZIP)
    assert subprocess.run(['/bin/bash', '-n', str(SCRIPT)], capture_output=True).returncode == 0


# ---- Install -----------------------------------------------------------------------------------------------------


def test_it_verifies_before_it_installs_and_uses_https_only(env):
    digest = env.release('1.0.0')
    done = env.run()
    assert done.returncode == 0, done.stderr
    app = env.user_app()
    assert (app / 'Contents/Resources/resources/release.txt').read_text() == 'release 1.0.0'
    assert os.access(app / 'Contents/MacOS/pebble', os.X_OK)
    assert 'SHA-256 matches SHA256SUMS: ' + digest in done.stdout
    assert 'Pebble 1.0.0 is installed: %s' % app in done.stdout
    # Only the release's two files, over HTTPS only (redirects included), with TLS 1.2 or later.
    fetched = env.calls('curl')
    assert [argv[-1] for argv in fetched] == [LATEST + ZIP, LATEST + 'SHA256SUMS']
    for argv in fetched:
        assert argv[argv.index('--proto') + 1] == '=https' and argv[argv.index('--proto-redir') + 1] == '=https'
        assert '--tlsv1.2' in argv and '--fail' in argv
        assert argv[argv.index('--output') + 1].startswith(str(env.tmp) + '/pebble-install.')
    # The unpacked app was checked before it moved, and the installed one after.
    signed = env.calls('codesign')
    assert len(signed) == 2 and all(argv[:3] == ['--verify', '--deep', '--strict'] for argv in signed)
    assert signed[0][3].startswith(str(env.tmp) + '/pebble-install.') and signed[0][3].endswith('/unpacked/Pebble.app')
    assert signed[1][3] == str(app)
    assert env.calls('ps') == [['-axo', 'comm='], ['-axo', 'comm=']]  # before the download and before the move
    assert env.backups() == []  # nothing to back up
    # The first-run steps and the honest statements.
    for words in ('First run:', 'Continue with ChatGPT', 'Always Allow', 'not an Apple Developer ID',
                  'not notarized', 'does not prove who made the file', 'never turn', 'Open Anyway'):
        assert words in done.stdout, words
    assert_never_escalates(env)


def test_an_existing_app_is_moved_to_the_backups_never_deleted(env):
    old = put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    env.release('1.0.0')
    done = env.run()
    assert done.returncode == 0, done.stderr
    [root] = env.backups()
    assert re.fullmatch(r'\d{8}T\d{6}Z-pre-install\.noindex', root.name)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert (root / 'Pebble.app.backup/Contents/Resources/old.txt').read_text() == 'the previous version'
    assert not old.exists() and (env.user_app() / 'Contents/Resources/resources/release.txt').is_file()
    assert str(root / 'Pebble.app.backup') in done.stdout and 'Nothing was deleted' in done.stdout
    assert_never_escalates(env)


@pytest.mark.parametrize('sums', [
    pytest.param(lambda digest: '0' * 64 + '  ' + ZIP + '\n', id='wrong digest'),
    pytest.param(lambda digest: '', id='no line'),
    # The right digest in a line that is not exactly "<sha256>  Pebble-macos-arm64.zip" is still refused.
    pytest.param(lambda digest: '%s  %s\n%s  %s\n' % (digest, ZIP, digest, ZIP), id='two lines'),
    pytest.param(lambda digest: '%s %s\n' % (digest, ZIP), id='one space'),
    pytest.param(lambda digest: '%s  %s.old\n' % (digest, ZIP), id='another name'),
    pytest.param(lambda digest: '%s  %s\n' % (digest.upper(), ZIP), id='upper case'),
    pytest.param(lambda digest: '%s  ./%s\n' % (digest, ZIP), id='a path'),
])
def test_a_download_that_does_not_match_its_sha256sums_line_installs_nothing(env, sums):
    previous = put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    before = snapshot(env.home)
    digest = env.release('1.0.0')
    env.release('1.0.0', sums=sums(digest))
    done = env.run()
    assert done.returncode == 1
    assert 'nothing was installed' in done.stderr and 'SHA256SUMS' in done.stderr
    assert snapshot(env.home) == before and previous.read_text() == 'the previous version'
    assert env.calls('ditto') == [] and env.calls('codesign') == []  # verified before anything is unpacked
    assert_never_escalates(env)


@pytest.mark.parametrize('fake, words', [
    ({'FAKE_UNAME_M': 'x86_64'}, 'needs a Mac with Apple silicon (this Mac reports x86_64)'),
    ({'FAKE_UNAME_M': 'x86_64', 'FAKE_TRANSLATED': '1'}, 'runs under Rosetta'),
    ({'FAKE_TRANSLATED': '1'}, 'runs under Rosetta'),
    ({'FAKE_SW_VERS': '11.7.10'}, 'needs macOS 12.0 or later (this Mac has 11.7.10)'),
    ({'FAKE_SW_VERS': '9.10'}, 'needs macOS 12.0 or later (this Mac has 9.10)'),  # compared as numbers, not text
    ({'FAKE_UNAME_S': 'Linux'}, 'runs on macOS only'),
    ({'FAKE_UID': '0'}, 'without sudo'),
])
def test_it_refuses_another_platform_rosetta_old_macos_and_root(env, fake, words):
    env.release('1.0.0')
    before = snapshot(env.home)
    done = env.run(**fake)
    assert done.returncode == 1 and words in done.stderr, done.stderr
    assert env.calls('curl') == [] and snapshot(env.home) == before
    assert_never_escalates(env)


@pytest.mark.parametrize('macos', ['12.0', '12', '12.0.1', '27.0'])
def test_the_minimum_macos_and_later_versions_are_accepted(env, macos):
    env.release('1.0.0')
    done = env.run(FAKE_SW_VERS=macos)
    assert done.returncode == 0, done.stderr
    assert_never_escalates(env)


@pytest.mark.parametrize('release, words', [
    ({'identifier': 'com.example.other'}, 'not Pebble (identifier "com.example.other")'),
    ({'minimum': '28.0'}, 'needs macOS 28.0 or later (this Mac has 27.0)'),
    ({'extra': {'Extra.app/Contents/Info.plist': (b'x', 0o644)}}, 'something besides Pebble.app (Extra.app)'),
    ({'bundle_version': 'one'}, 'no readable version'),
])
def test_a_download_that_is_not_the_expected_app_installs_nothing(env, release, words):
    previous = put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    env.release('1.0.0', **release)
    done = env.run()
    assert done.returncode == 1 and words in done.stderr and 'nothing was installed' in done.stderr, done.stderr
    assert previous.read_text() == 'the previous version' and env.backups() == []
    assert_never_escalates(env)


def test_a_signature_that_does_not_verify_installs_nothing(env):
    previous = put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    env.release('1.0.0')
    done = env.run(FAKE_CODESIGN_FAIL='1')
    assert done.returncode == 1 and "code signature does not verify; nothing was installed" in done.stderr
    assert previous.read_text() == 'the previous version' and env.backups() == []
    assert_never_escalates(env)


def test_an_installed_copy_that_fails_its_check_is_set_aside_and_the_previous_app_comes_back(env):
    put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    env.release('1.0.0')
    done = env.run(FAKE_CODESIGN_FAIL='2')  # the unpacked app verifies; the installed copy does not
    assert done.returncode == 1 and 'did not verify' in done.stderr
    [root] = env.backups()
    assert (env.user_app() / 'Contents/Resources/old.txt').read_text() == 'the previous version'
    assert (root / 'Pebble.app.failed/Contents/Resources/resources/release.txt').is_file()
    assert not (root / 'Pebble.app.backup').exists()
    assert_never_escalates(env)


def test_it_refuses_while_pebble_runs_and_never_stops_it(env):
    env.release('1.0.0')
    before = snapshot(env.home)
    running = '/sbin/launchd\n/Applications/Pebble.app/Contents/Resources/resources/backend/pebble-backend\n'
    done = env.run(FAKE_PS=running)
    assert done.returncode == 1 and 'Quit Pebble first' in done.stderr and 'pebble-backend' in done.stdout
    assert env.calls('curl') == [] and snapshot(env.home) == before
    assert env.tools_called() <= {'uname', 'sysctl', 'sw_vers', 'ps', 'id'}  # nothing that could stop a process
    # An executable outside any Pebble.app never counts, whatever its arguments name (ps reports executables only).
    other = env.run(FAKE_PS='/usr/bin/vim\n/Applications/Pebble Candidate.app/Contents/MacOS/pebble\n')
    assert other.returncode == 0, other.stderr
    assert_never_escalates(env)


def test_a_version_is_fetched_from_its_own_release_and_must_match(env):
    env.release('1.0.0', latest=False)
    done = env.run('--version', '1.0.0')
    assert done.returncode == 0, done.stderr
    assert [argv[-1] for argv in env.calls('curl')] == [
        'https://github.com/tohin003/Pebble/releases/download/v1.0.0/' + ZIP,
        'https://github.com/tohin003/Pebble/releases/download/v1.0.0/SHA256SUMS']
    env.release('1.0.1', bundle_version='1.0.0', latest=False)
    mismatch = env.run('--version=1.0.1')
    assert mismatch.returncode == 1 and 'The release v1.0.1 holds Pebble 1.0.0' in mismatch.stderr
    for bad in (['--version', 'latest'], ['--version'], ['--frobnicate']):
        assert env.run(*bad).returncode == 2
    assert_never_escalates(env)


# ---- Where it installs, and the dry run ---------------------------------------------------------------------------


def test_the_dry_run_names_applications_when_it_is_writable_and_changes_nothing(env):
    env.release('1.0.0')
    put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    before, apps = snapshot(env.home), applications_state()
    done = env.run('--dry-run', profile=SYSTEM_LOOKS_WRITABLE)
    assert done.returncode == 0, done.stderr
    assert 'Would install: /Applications/Pebble.app' in done.stdout
    assert 'Another copy stays where it is: %s' % env.user_app() in done.stdout
    assert snapshot(env.home) == before and applications_state() == apps
    assert env.calls('curl') == [] and env.calls('ditto') == [] and env.calls('codesign') == []
    assert_never_escalates(env)


def test_the_dry_run_names_the_home_applications_folder_otherwise_and_changes_nothing(env):
    env.release('1.0.0')
    put(env.user_app() / 'Contents/Resources/old.txt', 'the previous version')
    before = snapshot(env.home)
    done = env.run('--dry-run', FAKE_PS='/Applications/Pebble.app/Contents/MacOS/pebble\n')
    assert done.returncode == 0, done.stderr
    assert 'Would install: %s' % env.user_app() in done.stdout
    assert 'Would move the existing %s to ~/PebbleBackups/' % env.user_app() in done.stdout
    assert 'Pebble is running now' in done.stdout and 'Would download: ' + LATEST + ZIP in done.stdout
    assert snapshot(env.home) == before and env.calls('curl') == []
    assert_never_escalates(env)


# ---- Uninstall ---------------------------------------------------------------------------------------------------


def test_uninstall_moves_the_app_aside_and_lists_what_is_left(env):
    put(env.user_app() / 'Contents/Resources/old.txt', 'the installed version')
    data = put(env.home / '.pebble/data/pebble.sqlite', 'conversations')
    copy = put(env.home / 'Documents/Pebble/Conversations/a.md', 'a copy')
    put(env.home / 'Library/WebKit/com.pebble.local/x', 'window storage')
    put(env.home / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json', '{}')
    dry_before = snapshot(env.home)
    dry = env.run('--uninstall', '--dry-run')
    assert dry.returncode == 0 and snapshot(env.home) == dry_before
    assert 'Would move %s' % env.user_app() in dry.stdout
    done = env.run('--uninstall')
    assert done.returncode == 0, done.stderr
    [root] = env.backups('uninstall')
    assert (root / 'Pebble.app.backup/Contents/Resources/old.txt').read_text() == 'the installed version'
    assert not env.user_app().exists()
    assert data.read_text() == 'conversations' and copy.read_text() == 'a copy'  # the data is never touched
    for present in (env.home / '.pebble', env.home / 'Documents/Pebble', env.home / 'Library/WebKit/com.pebble.local',
                    env.home / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json',
                    env.home / 'PebbleBackups'):
        assert '  %s\n' % present in done.stdout, present
    assert 'Chrome Canary' not in done.stdout  # only what exists is listed
    for service in ('com.pebble.local.vault', 'com.pebble.local.providers', 'com.pebble.local.connectors',
                    'com.pebble.local.plan-credentials', 'com.pebble.local.browser-pairing'):
        assert service in done.stdout
    assert 'never reads them' in done.stdout and 'Remove Pebble completely' in done.stdout
    running = env.run('--uninstall', FAKE_PS='/Applications/Pebble.app/Contents/MacOS/pebble\n')
    assert running.returncode == 1 and 'Quit Pebble first' in running.stderr
    assert_never_escalates(env)
