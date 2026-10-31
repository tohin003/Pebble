"""Typed command plans: parsing, pinning, narrowing, drift, network and profile inputs.

Fixture projects, toolchain copies and temporary folders live under tmp_path.
Nothing here runs a command, signals a process or opens a socket: plans are
data, and executables are only resolved, hashed and stat'ed.
"""

import copy
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from pebble import command_plans as plans, process_jobs, recipes
from pebble.policy import PolicyError
from pebble.terminal import SHELL_HINT, inspect_command, resolve_executable

INTERPRETER = Path(sys.executable).resolve()
SOURCE = INTERPRETER if INTERPRETER.stat().st_size < 8 << 20 else Path('/bin/echo')
CREDENTIAL = {'ref': 'vault:github', 'destination': ['api.example.com', 443], 'methods': ['GET'], 'path_prefix': '/v1/'}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    for rel, text in {'README.md': 'hi\n', 'src/main.py': 'print(1)\n', 'build/out.txt': 'x\n', 'docs/a.md': 'a\n'}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root.resolve()


def tool(tmp_path, name='tool', source=SOURCE):
    """A copied interpreter in its own temporary toolchain, outside the project."""
    folder = tmp_path / 'tc' / 'bin'
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    shutil.copyfile(source, exe)
    os.chmod(exe, 0o755)
    return exe.resolve()


def body(plan):
    return {key: value for key, value in plan.items() if key != 'plan_hash'}


def sealed(plan):
    """What an honest caller sends after editing a copy: the same plan with a recomputed hash."""
    plan['plan_hash'] = plans.plan_digest(plan)
    return plan


def strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


# Command strings.

SAFE = ['ls -la', 'pwd', 'cat README.md', 'wc -l README.md', 'echo hello world', 'ls  src', ' head -n 2 README.md',
        'uname', 'which ls', 'python3 -V']


@pytest.mark.parametrize('command', SAFE)
def test_round_trip_from_safe_command(project, command):
    plan = plans.from_command(command, project)
    checked = inspect_command(command, project)
    step = plan['steps'][0]
    assert set(plan) == plans.KEYS and plan['plan_version'] == 1
    assert step['argv'] == checked['argv'] == command.split()
    assert (step['cwd'], step['env'], step['stdin']) == ('.', {}, 'none')
    assert plan['risk'] == checked['risk'] and plan['root'] == checked['root'] == str(project)
    assert plan['root_identity'] == checked['root_identity']
    assert plan['pipeline'] == 'none' and plan['writable'] == [] and plan['network'] == plans.NO_NETWORK
    assert (plan['process_model'], plan['interactive'], plan['on_disconnect']) == ('tree', False, 'terminate')
    assert plan['resources'] == plans.DEFAULTS
    assert plan['plan_hash'] == hashlib.sha256(recipes.canonical(body(plan)).encode()).hexdigest()
    real = resolve_executable(step['argv'][0])
    assert step['executable']['path'] == step['executable']['realpath'] == str(real)
    assert step['executable']['sha256'] == hashlib.sha256(real.read_bytes()).hexdigest()
    assert plans.validate(json.loads(json.dumps(plan))) == plan
    assert plans.verify(plan, project) == plan
    legacy = plans.legacy_payload(plan)
    assert legacy['argv'] == step['argv'] and legacy['command'] == ' '.join(step['argv'])
    assert inspect_command(legacy['command'], project)['argv'] == step['argv']


def test_documented_defaults_and_typed_options(project):
    sync = plans.from_command('ls', project)
    assert sync['resources'] == {'timeout': 60, 'cpu_seconds': 60, 'memory_mb': 2048, 'nproc': 64, 'nofile': 256,
                                 'fsize_mb': 256, 'disk_write_mb': 1024, 'output_bytes': 192_000}
    assert plans.from_command('ls', project, asynchronous=True)['resources'] == {
        **sync['resources'], 'timeout': 300, 'cpu_seconds': 300}
    assert plans.from_command('ls', project, writable=True)['writable'] == ['.']
    assert plans.from_command('ls', project, writable=['src', 'build'])['writable'] == ['build', 'src']
    for options in ({'asynchronous': 1}, {'writable': 'yes'}, {'writable': None}, {'network': 'api.example.com'},
                    {'network': None}):
        with pytest.raises(PolicyError, match='typed'):
            plans.from_command('ls', project, **options)
    with pytest.raises(PolicyError, match=r'exact \[host, 443\]'):
        plans.from_command('ls', project, network=True)
    with pytest.raises(PolicyError, match='not installed'):
        plans.from_command('pebble-no-such-tool-p14', project)
    with pytest.raises(PolicyError, match='existing project'):
        plans.from_command('ls', project / 'missing')


# Phase 46 (1.9.28, D178: the person allows plain NAME=value words outside a deny list under review, on unless
# PEBBLE_1928_TERMINAL=0): 'FOO=1 ls' left this list for test_a_variable_is_reviewed_but_never_planned; a quoted one stays
BLOCKED_COMMANDS = ['sudo ls', 'su', 'bash -c ls', 'sh', 'zsh', '/bin/sh', 'env ls', 'xargs ls', 'nohup ls', 'eval ls',
                    'git status', 'chmod 777 README.md', 'launchctl list', 'osascript', 'security list-keychains',
                    'mkfs.ext4 x', 'dd if=x', 'cat .env', 'cat src/.ssh/id', 'cat key.pem', 'ls ..', 'cat ../x',
                    'ls /', "'FOO=1' ls", 'PATH=/tmp ls', 'ls $(pwd)', 'ls `pwd`', 'ls ${HOME}', 'ls; pwd', 'ls && pwd',
                    'ls || pwd', 'ls | wc', 'ls > out.txt', 'ls >> out.txt', 'cat < README.md', 'ls &', 'ls\npwd',
                    'ls\rpwd', 'ls\x00', '', '   ', 'x' * 4001]


@pytest.mark.parametrize('command', BLOCKED_COMMANDS)
def test_blocked_commands_refused_exactly_as_inspect_command(project, command):
    with pytest.raises(PolicyError) as expected:
        inspect_command(command, project)
    with pytest.raises(PolicyError) as actual:
        plans.from_command(command, project)
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize('argv', [['sudo', 'ls'], ['bash', '-c', 'ls'], ['/bin/zsh'], ['env', 'ls'], ['git', 'status'],
                                  ['chmod', '777', 'x'], ['ls', ';', 'pwd'], ['ls', '|', 'wc'], ['ls', '>', 'x'],
                                  ['ls', '&&', 'pwd'], ['ls', '$(pwd)'], ['ls', '`pwd`'], ['PATH=/tmp', 'ls'],
                                  ['cat', '.env'], ['ls', '..'], ['ls', '/']])
def test_typed_argv_refused_like_the_command_parser(project, argv):
    with pytest.raises(PolicyError) as expected:
        inspect_command(shlex.join(argv), project)
    with pytest.raises(PolicyError) as actual:
        plans.from_argv([{'argv': argv}], project)
    assert str(actual.value) == str(expected.value)


def test_a_variable_is_reviewed_but_never_planned(project, monkeypatch):
    # D178: the broker admits `FOO=1 python3 x.py` for review (its card names the variable); a plan carries no
    # variables, so the string is ambiguous to it and the typed argv does not survive the parser. With the switch off,
    # as before.
    # Phase 46 (1.9.28, the follow-ups security review 11, restated): a variable goes only to the project's own program,
    # so `FOO=1 ls` (this test's command before) is refused now
    checked = inspect_command('FOO=1 python3 x.py', project)
    assert checked['env'] == {'FOO': '1'} and checked['argv'] == ['python3', 'x.py'] and checked['requires_review']
    with pytest.raises(PolicyError, match="only for the project's own program"):
        inspect_command('FOO=1 ls', project)
    with pytest.raises(PolicyError, match='ambiguous'):
        plans.from_command('FOO=1 python3 x.py', project)
    with pytest.raises(PolicyError, match='does not survive the command parser unchanged'):
        plans.from_argv([{'argv': ['FOO=1', 'python3', 'x.py']}], project)
    monkeypatch.setenv('PEBBLE_1928_TERMINAL', '0')
    for run in (lambda: inspect_command('FOO=1 ls', project), lambda: plans.from_command('FOO=1 ls', project),
                lambda: plans.from_argv([{'argv': ['FOO=1', 'ls']}], project),
                lambda: inspect_command('FOO=1 python3 x.py', project)):
        with pytest.raises(PolicyError, match='environment overrides are blocked'):
            run()


@pytest.mark.parametrize('command', ['xcrun clang', 'time ls', 'nice ls', 'tcsh', 'ksh', 'dash', 'caffeinate ls',
                                     'sandbox-exec ls', 'open README.md', 'script', 'arch ls'])
def test_additional_shells_and_wrappers_refused(project, command):
    with pytest.raises(PolicyError) as exc:
        plans.from_command(command, project)
    assert str(exc.value) == plans.PRIVILEGED


ACCEPTED_BY_INSPECT = ["ls 'src'", 'ls "src"', 'ls *.md', 'ls src/?ain.py', 'ls [ab]', 'ls {src,docs}', 'ls ~/x',
                       'ls src\\ x', 'echo #x', 'echo !x', 'ls\tsrc', 'echo \x1b[31m', 'echo \x9b31m',
                       'echo a‮b', 'echo a​b', 'echo a b', 'ls =ls', 'echo ^x', 'echo (x)',
                       'echo café']


@pytest.mark.parametrize('command', ACCEPTED_BY_INSPECT)
def test_parser_ambiguity_refused(project, command):
    inspect_command(command, project)  # the existing parser admits these; plans must not
    with pytest.raises(PolicyError) as exc:
        plans.from_command(command, project)
    assert str(exc.value) == plans.AMBIGUOUS


@pytest.mark.parametrize('arg', ['a\x00b', 'a\nb', 'a\rb', 'a\tb', '\x1b[31m', '\x1b]52;c;eA==\x07', 'a\x9bb', 'a\x85b',
                                 'a\x7fb', 'a‮b', 'a⁦b', 'a​b', 'a﻿b', 'a b', 'a b',
                                 '\ud800', 'ab'])
def test_argv_control_characters_refused(project, arg):
    with pytest.raises(PolicyError, match='plain text'):
        plans.from_argv([{'argv': ['echo', arg]}], project)


def test_typed_argv_keeps_literal_words(project):
    argv = ['echo', 'hello world', "it's", '*.md', '']
    plan = plans.from_argv([{'argv': argv, 'env': {'NO_COLOR': '1', 'LANG': 'C'}}], project)
    assert plan['steps'][0]['argv'] == argv and plan['steps'][0]['env'] == {'LANG': 'C', 'NO_COLOR': '1'}
    assert plans.verify(plan, project) == plan
    with pytest.raises(PolicyError, match='Unknown command plan fields: shell'):
        plans.from_argv([{'argv': ['ls']}], project, shell=True)
    with pytest.raises(PolicyError, match='host pins the executable'):
        plans.from_argv([{'argv': ['ls'], 'executable': '/bin/ls'}], project)
    for env in ({'PATH': '/tmp'}, {'HOME': '/tmp'}, {'DYLD_INSERT_LIBRARIES': 'x'}, {'PYTHONPATH': 'x'},
                {'NODE_OPTIONS': '--require=x'}, {'OPENAI_API_KEY': 'x'}, {'HTTPS_PROXY': 'x'}, {'GIT_DIR': 'x'}):
        with pytest.raises(PolicyError, match='environment may set only'):
            plans.from_argv([{'argv': ['ls'], 'env': env}], project)
    for value in ('C\n', '\x1b[0m', 'x' * 1025, 1):
        with pytest.raises(PolicyError, match='literal'):
            plans.from_argv([{'argv': ['ls'], 'env': {'LANG': value}}], project)
    with pytest.raises(PolicyError, match='at most 16'):
        plans.from_argv([{'argv': ['ls'], 'env': {f'K{i}': 'x' for i in range(17)}}], project)


# Executable provenance.

def test_pinned_identity_recorded_and_drift_refused_per_field(project, tmp_path):
    exe = tool(tmp_path)
    plan = plans.from_argv([{'argv': [str(exe), '-V']}], project)
    pinned = plan['steps'][0]['executable']
    assert set(pinned) == set(recipes.IDENTITY) and pinned == recipes.pin(str(exe), project)
    assert pinned['sha256'] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert plans.verify(plan, project) == plan
    # Same inode, size and mtime with different bytes: only the content hash can tell.
    info, data = exe.stat(), bytearray(exe.read_bytes())
    data[-1] ^= 0xFF
    exe.write_bytes(bytes(data))
    os.utime(exe, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(PolicyError, match=r'\(steps\[0\]\.executable: sha256\)'):
        plans.verify(plan, project)
    exe.write_bytes(bytes(data) + b'\0')
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable: sha256, size, mtime_ns'):
        plans.verify(plan, project)
    # A link swapped in at the pinned path points somewhere else.
    exe.unlink()
    exe.symlink_to(tool(tmp_path, 'other'))
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable: realpath'):
        plans.verify(plan, project)
    exe.unlink()
    shutil.copyfile(SOURCE, exe)
    os.chmod(exe, 0o777)
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable.*world-writable'):
        plans.verify(plan, project)
    exe.unlink()
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable.*unavailable'):
        plans.verify(plan, project)


def test_spoofed_executables_refused(project, tmp_path):
    inside = project / 'bin' / 'tool'
    inside.parent.mkdir()
    shutil.copyfile(SOURCE, inside)
    os.chmod(inside, 0o755)
    with pytest.raises(PolicyError, match='inside the selected project'):
        plans.from_argv([{'argv': [str(inside)]}], project)
    with pytest.raises(PolicyError, match='Use a runtime'):
        plans.from_argv([{'argv': ['./bin/tool']}], project)
    loose = tool(tmp_path, 'loose')
    os.chmod(loose, 0o777)
    with pytest.raises(PolicyError, match='world-writable'):
        plans.from_argv([{'argv': [str(loose)]}], project)
    os.chmod(loose, 0o775)
    with pytest.raises(PolicyError, match='group-writable'):
        plans.from_argv([{'argv': [str(loose)]}], project)
    # A renamed link to a shell, and a renamed copy of one.
    alias = tmp_path / 'tc' / 'bin' / 'helper'
    alias.symlink_to('/bin/zsh')
    with pytest.raises(PolicyError) as exc:
        plans.from_argv([{'argv': [str(alias), '-c', 'id']}], project)
    assert str(exc.value) == plans.PRIVILEGED
    copied = tool(tmp_path, 'formatter', source=Path('/bin/zsh'))
    with pytest.raises(PolicyError, match='copy of a shell'):
        plans.from_argv([{'argv': [str(copied)]}], project)
    # A system-wide toolchain root is refused rather than granting exec under /usr.
    assert Path('/usr/libexec/PlistBuddy').is_file()
    with pytest.raises(PolicyError, match='confined command runtime'):
        plans.from_argv([{'argv': ['/usr/libexec/PlistBuddy', '-h']}], project)


# Tampering and strict shape.

def test_tampered_plan_hash_refused_everywhere(project, tmp_path):
    plan = plans.from_command('ls -la', project, writable=['src'])
    last = plan['plan_hash'][-1]
    flipped = dict(plan, plan_hash=plan['plan_hash'][:-1] + ('0' if last != '0' else '1'))
    widened = copy.deepcopy(plan)
    widened['writable'] = ['.']
    temp = tmp_path / 'job'
    temp.mkdir()
    calls = (plans.validate, plans.review_view, plans.legacy_payload, lambda p: plans.verify(p, project),
             lambda p: plans.profile_inputs(p, temp, 'marker-' + 'a' * 16), lambda p: plans.narrow(plan, p),
             lambda p: plans.narrow(p, plan))
    for bad in (flipped, widened, dict(plan, plan_hash=None), dict(plan, plan_hash='0' * 64)):
        for call in calls:
            with pytest.raises(PolicyError, match='hash does not match'):
                call(bad)
    with pytest.raises(PolicyError, match='plan v1 fields'):
        plans.validate(body(plan))


def test_validate_is_strict(project):
    plan = plans.from_command('ls', project)

    def bad(match, mutate):
        broken = copy.deepcopy(plan)
        mutate(broken)
        with pytest.raises(PolicyError, match=match):
            plans.validate(sealed(broken))

    bad('plan v1 fields', lambda p: p.update(extra=1))
    bad('version', lambda p: p.update(plan_version=2))
    bad('version', lambda p: p.update(plan_version=True))
    bad('exactly argv', lambda p: p['steps'][0].update(shell=True))
    bad('environment may set only', lambda p: p['steps'][0]['env'].update(PATH='/tmp'))
    bad('literal', lambda p: p['steps'][0]['env'].update(LANG='C\n'))
    bad('timeout from 1 to 300', lambda p: p['resources'].update(timeout=301))
    bad('timeout from', lambda p: p['resources'].update(timeout=True))
    bad('timeout from', lambda p: p['resources'].update(timeout=60.0))
    bad('output_bytes from', lambda p: p['resources'].update(output_bytes=192_001))
    bad('memory_mb from', lambda p: p['resources'].update(memory_mb=0))
    bad('Resources have exactly', lambda p: p['resources'].pop('nproc'))
    bad('Resources have exactly', lambda p: p['resources'].update(gpu=1))
    bad('interactive is a boolean', lambda p: p.update(interactive=1))
    bad('on_disconnect', lambda p: p.update(on_disconnect='detach'))
    bad('process_model', lambda p: p.update(process_model='fork'))
    bad('risk', lambda p: p.update(risk='safe'))
    bad('risk', lambda p: p.update(risk='network'))
    bad('pinned identity', lambda p: p['steps'][0]['executable'].update(path='/bin/../bin/ls'))
    bad('pinned identity', lambda p: p['steps'][0]['executable'].pop('sha256'))
    bad('pinned identity', lambda p: p['steps'][0]['executable'].update(sha256='X' * 64))
    bad('pinned identity', lambda p: p['steps'][0]['executable'].update(size=True))
    bad('argv', lambda p: p['steps'][0].update(argv=[]))
    bad('argv', lambda p: p['steps'][0].update(argv='ls'))
    bad('Privileged', lambda p: p['steps'][0].update(argv=['sh']))
    bad('relative', lambda p: p['steps'][0].update(cwd='/tmp'))
    bad('PTY', lambda p: p['steps'][0].update(stdin='pty'))
    bad("stdin is 'none' or 'pty'", lambda p: p['steps'][0].update(stdin='file'))
    bad('canonical', lambda p: p.update(writable=['src', 'build']))
    bad('canonical', lambda p: p.update(writable=['src', 'src/main.py']))
    bad('root', lambda p: p.update(root='relative'))
    bad('root', lambda p: p.update(root='/'))
    bad('root', lambda p: p.update(root_identity=[1]))
    bad('one step', lambda p: p.update(steps=[]))
    bad('one step', lambda p: p.update(steps=p['steps'] * 2))
    bad("pipeline is 'none' or 'linear'", lambda p: p.update(pipeline='tree'))
    with pytest.raises(PolicyError, match='plain JSON'):
        plans.validate(dict(plan, steps={1j}))
    with pytest.raises(PolicyError, match='loopback'):
        plans.validate(plan, loopback='yes')
    with pytest.raises(PolicyError, match='byte limit'):
        plans.validate(sealed(dict(copy.deepcopy(plan), root='/' + 'a' * plans.PLAN_LIMIT)))


# Narrowing.

@pytest.fixture
def reviewed(project):
    small = {key: max(low, default // 2) for key, (low, default, _) in plans.RESOURCES.items()}
    return plans.from_argv([{'argv': ['ls', '-la', 'src'], 'env': {'NO_COLOR': '1'}}], project, writable=['src'],
                           network={'mode': 'broker', 'destinations': [['api.example.com', 443]],
                                    'credentials': [dict(CREDENTIAL)]},
                           resources=small, process_model='single')


def test_narrow_accepts_equal_and_narrower(project, reviewed):
    assert plans.narrow(reviewed, copy.deepcopy(reviewed)) == reviewed
    narrower = copy.deepcopy(reviewed)
    narrower['writable'] = ['src/main.py']
    narrower['network']['credentials'][0]['path_prefix'] = '/v1/repos'
    narrower['resources'] = {key: plans.RESOURCES[key][0] for key in plans.RESOURCES}
    narrower['steps'][0]['env'] = {}
    assert plans.narrow(reviewed, sealed(narrower)) == narrower
    readonly = copy.deepcopy(reviewed)
    readonly['writable'], readonly['network']['credentials'] = [], []
    assert plans.narrow(reviewed, sealed(readonly))['writable'] == []
    offline = copy.deepcopy(reviewed)
    offline['network'] = copy.deepcopy(plans.NO_NETWORK)
    offline['risk'] = inspect_command('ls -la src', project)['risk']
    assert plans.narrow(reviewed, sealed(offline))['network']['mode'] == 'none'
    wide = plans.from_argv([{'argv': ['cat'], 'stdin': 'pty'}], project, writable=['.'], interactive=True,
                           on_disconnect='continue')
    calm = copy.deepcopy(wide)
    calm['steps'][0]['stdin'], calm['interactive'], calm['on_disconnect'] = 'none', False, 'terminate'
    calm['writable'], calm['process_model'] = ['build', 'docs'], 'single'
    assert plans.narrow(wide, sealed(calm)) == calm


def _resource(key):
    return lambda p, root: p['resources'].__setitem__(key, p['resources'][key] + 1)


WIDEN = [
    ('writable', lambda p, root: p.update(writable=['.'])),
    ('writable', lambda p, root: p.update(writable=['docs', 'src'])),
    ('network.destinations', lambda p, root: p['network']['destinations'].append(['files.example.com', 443])),
    ('network.credentials', lambda p, root: p['network']['credentials'][0].update(methods=['GET', 'POST'])),
    ('network.credentials', lambda p, root: p['network']['credentials'][0].update(path_prefix='/')),
    ('network.credentials', lambda p, root: p['network']['credentials'][0].update(path_prefix='/v1x')),
    ('network.credentials', lambda p, root: p['network']['credentials'][0].update(ref='vault:other')),
    ('process_model', lambda p, root: p.update(process_model='tree')),
    ('on_disconnect', lambda p, root: p.update(on_disconnect='continue')),
    ('interactive', lambda p, root: (p.update(interactive=True), p['steps'][0].update(stdin='pty'))),
    (r'steps\[0\]\.argv', lambda p, root: p['steps'][0].update(argv=['ls', '-la', 'docs'])),
    (r'steps\[0\]\.executable', lambda p, root: p['steps'][0].update(executable=recipes.pin('/bin/cat', root))),
    (r'steps\[0\]\.cwd', lambda p, root: p['steps'][0].update(cwd='src')),
    (r'steps\[0\]\.env', lambda p, root: p['steps'][0]['env'].update(LANG='C')),
    (r'steps\[0\]\.env', lambda p, root: p['steps'][0]['env'].update(NO_COLOR='0')),
    ('root_identity', lambda p, root: p.update(root_identity=[p['root_identity'][0], p['root_identity'][1] + 1])),
    ('root', lambda p, root: p.update(root=p['root'] + '-other')),
] + [(rf'resources\.{key}', _resource(key)) for key in plans.RESOURCES]


@pytest.mark.parametrize('field,mutate', WIDEN, ids=[f'{i}-{field}' for i, (field, _) in enumerate(WIDEN)])
def test_narrow_refuses_every_widening(project, reviewed, field, mutate):
    requested = copy.deepcopy(reviewed)
    mutate(requested, project)
    with pytest.raises(PolicyError, match=rf'widens or changes the reviewed plan \({field}\)'):
        plans.narrow(reviewed, sealed(requested))


def test_narrow_network_mode_and_risk(project):
    offline = plans.from_command('ls -la', project)
    online = copy.deepcopy(offline)
    online['network'] = {'mode': 'broker', 'destinations': [['api.example.com', 443]], 'credentials': []}
    online['risk'] = 'network'
    with pytest.raises(PolicyError, match=r'\(network\.mode\)'):
        plans.narrow(offline, sealed(online))
    lied = copy.deepcopy(offline)
    lied['risk'] = 'execution'
    with pytest.raises(PolicyError, match=r'\(risk\)'):
        plans.narrow(offline, sealed(lied))
    # Dropping the broker: the requested risk must be inspect_command's class without network.
    reviewed = plans.from_command('ls -la', project, network=[['api.example.com', 443]])
    assert reviewed['risk'] == 'network'
    dropped = copy.deepcopy(reviewed)
    dropped['network'], dropped['risk'] = copy.deepcopy(plans.NO_NETWORK), 'execution'
    with pytest.raises(PolicyError, match=r'\(risk\)'):
        plans.narrow(reviewed, sealed(dropped))
    dropped['risk'] = 'read-only'
    assert plans.narrow(reviewed, sealed(dropped))['risk'] == 'read-only'


# Paths.

def test_writable_paths_normalized_and_escapes_refused(project, tmp_path):
    ok = plans.from_argv([{'argv': ['ls']}], project, writable=['src', 'build', 'src/main.py', 'README.md'])
    assert ok['writable'] == ['README.md', 'build', 'src']
    assert plans.from_argv([{'argv': ['ls']}], project, writable=['src', '.'])['writable'] == ['.']
    outside = tmp_path / 'outside'
    outside.mkdir()
    (project / 'linked').symlink_to(outside)
    (project / 'alias').symlink_to(project / 'src')
    (project / 'docs' / 'deep').symlink_to(outside)
    (project / '.git').mkdir()
    os.link(project / 'README.md', outside / 'hard')
    for name in ['..', '../outside', 'src/../..', 'src/..', '/etc', str(project / 'src'), 'linked', 'linked/x', 'alias',
                 'alias/main.py', 'docs/deep', 'missing', 'src/', './src', 'src//main.py', 'src/.', 'a\\b', 'src\n',
                 '.git', '.env', 'src/.env.local', 'secrets/x', 'key.pem', 'README.md', '', 1, None]:
        with pytest.raises(PolicyError):
            plans.from_argv([{'argv': ['ls']}], project, writable=[name])
    with pytest.raises(PolicyError, match='at most 16'):
        plans.from_argv([{'argv': ['ls']}], project, writable=[f'd{i}' for i in range(17)])
    with pytest.raises(PolicyError, match='list'):
        plans.from_argv([{'argv': ['ls']}], project, writable='src')


def test_cwd_must_be_a_real_project_folder(project, tmp_path):
    plan = plans.from_argv([{'argv': ['ls'], 'cwd': 'src'}], project)
    assert plan['steps'][0]['cwd'] == 'src' and plans.verify(plan, project) == plan
    outside = tmp_path / 'outside'
    outside.mkdir()
    (project / 'linked').symlink_to(outside)
    for cwd in ['..', '/tmp', 'linked', 'README.md', 'missing', '.git', 'src/', '', None]:
        with pytest.raises(PolicyError):
            plans.from_argv([{'argv': ['ls'], 'cwd': cwd}], project)
    shutil.rmtree(project / 'src')
    (project / 'src').symlink_to(outside)
    with pytest.raises(PolicyError, match=r'\(steps\[0\]\.cwd\)'):
        plans.verify(plan, project)


def test_verify_refuses_replaced_root_and_moved_paths(project, tmp_path):
    plan = plans.from_command('ls src', project, writable=['build'])
    assert plans.verify(plan, project) == plan
    shutil.rmtree(project / 'build')
    (tmp_path / 'elsewhere').mkdir()
    (project / 'build').symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(PolicyError, match=r'\(writable: build\)'):
        plans.verify(plan, project)
    (project / 'build').unlink()
    (project / 'build').mkdir()
    assert plans.verify(plan, project) == plan
    project.rename(tmp_path / 'moved')
    project.mkdir()
    (project / 'build').mkdir()
    with pytest.raises(PolicyError, match=r'\(root_identity\)'):
        plans.verify(plan, project)
    with pytest.raises(PolicyError, match=r'\(root\)'):
        plans.verify(plan, tmp_path / 'moved')
    with pytest.raises(PolicyError, match=r'\(root_identity\)'):
        plans.profile_inputs(plan, tmp_path / 'elsewhere', 'marker-' + 'b' * 16)


# Network.

def _net(project, destinations, credentials=(), loopback=False):
    return plans.from_argv([{'argv': ['ls']}], project, loopback=loopback,
                           network={'mode': 'broker', 'destinations': destinations, 'credentials': list(credentials)})


def test_network_destinations_are_exact_https_hosts(project):
    plan = _net(project, [['files.example.com', 443], ['api.example.com', 443], ('api.example.com', 443)])
    assert plan['network']['destinations'] == [['api.example.com', 443], ['files.example.com', 443]]
    assert plan['risk'] == 'network' and plans.legacy_payload(plan)['network'] is True
    for destinations in ([['api.example.com', 80]], [['api.example.com', 8443]], [['93.184.216.34', 443]],
                         [['127.0.0.1', 443]], [['[::1]', 443]], [['localhost', 443]], [['printer.local', 443]],
                         [['app.localhost', 443]], [['intranet', 443]], [['*.example.com', 443]],
                         [['API.example.com', 443]], [['xn--bcher-kva.example.com', 443]], [['api.example.com', '443']],
                         [['api.example.com', True]], [['api.example.com']], ['api.example.com:443'], [],
                         [[f'h{i}.example.com', 443] for i in range(17)]):
        with pytest.raises(PolicyError):
            _net(project, destinations)
    # The loopback test flag is explicit, and must be repeated wherever the plan is checked.
    local = _net(project, [['127.0.0.1', 8443]], loopback=True)
    assert local['network']['destinations'] == [['127.0.0.1', 8443]]
    with pytest.raises(PolicyError, match='IP literals'):
        plans.validate(local)
    assert plans.validate(local, loopback=True) == local
    with pytest.raises(PolicyError):
        _net(project, [['localhost', 8443]], loopback=True)
    with pytest.raises(PolicyError):
        plans.from_command('ls', project, network=[['127.0.0.1', 443]])
    assert plans.from_command('ls', project, network=[['api.example.com', 443]])['network']['mode'] == 'broker'


def test_network_none_excludes_destinations_and_credentials(project):
    for network in ({'mode': 'none', 'destinations': [['api.example.com', 443]]},
                    {'mode': 'none', 'credentials': [dict(CREDENTIAL)]},
                    {'mode': 'open'}, {'mode': 'broker'}, {'destinations': [['api.example.com', 443]]},
                    {'mode': 'none', 'extra': 1}, 'none', None):
        with pytest.raises(PolicyError):
            plans.from_argv([{'argv': ['ls']}], project, network=network)
    plan = plans.from_argv([{'argv': ['ls']}], project, network={'mode': 'none'})
    assert plan['network'] == plans.NO_NETWORK and plan['risk'] == 'read-only'


def test_credentials_reference_listed_destinations(project):
    credential = dict(CREDENTIAL, methods=['POST', 'GET', 'GET'])
    plan = _net(project, [['api.example.com', 443]], [credential])
    assert plan['network']['credentials'] == [dict(CREDENTIAL, methods=['GET', 'POST'])]
    for prefix in ('/', '/v1', '/v1/', '/v1/repos/x_y-z.json', '/~user/a.b/'):
        assert _net(project, [['api.example.com', 443]], [dict(CREDENTIAL, path_prefix=prefix)])
    for bad in (dict(CREDENTIAL, destination=['files.example.com', 443]), dict(CREDENTIAL, destination=['api.example.com', 80]),
                dict(CREDENTIAL, methods=['TRACE']), dict(CREDENTIAL, methods=[]), dict(CREDENTIAL, methods='GET'),
                dict(CREDENTIAL, path_prefix='/../x'), dict(CREDENTIAL, path_prefix='/a/%2e%2e'),
                dict(CREDENTIAL, path_prefix='v1'), dict(CREDENTIAL, path_prefix='/v1?x=1'),
                dict(CREDENTIAL, path_prefix='//evil.example.com'), dict(CREDENTIAL, path_prefix='/v1#x'),
                dict(CREDENTIAL, path_prefix='/a//b'), dict(CREDENTIAL, path_prefix='/a/./b'),
                dict(CREDENTIAL, path_prefix='/' + 'a' * 250 + '!'), dict(CREDENTIAL, path_prefix='/' + 'a' * 300),
                dict(CREDENTIAL, ref='ghp secret value'), dict(CREDENTIAL, ref=''), dict(CREDENTIAL, ref='x' * 129),
                dict(CREDENTIAL, secret='x'), {k: v for k, v in CREDENTIAL.items() if k != 'methods'}):
        with pytest.raises(PolicyError):
            _net(project, [['api.example.com', 443]], [bad])
    with pytest.raises(PolicyError, match='at most 8'):
        _net(project, [['api.example.com', 443]], [dict(CREDENTIAL, ref=f'vault:{i}') for i in range(9)])


# Pipelines and interactivity.

def test_linear_pipeline_pins_each_step(project, tmp_path):
    exe = tool(tmp_path)
    steps = [{'argv': ['cat', 'README.md']}, {'argv': [str(exe), '-c', 'pass']}, {'argv': ['wc', '-l']}]
    plan = plans.from_argv(steps, project, pipeline='linear')
    pinned = [step['executable'] for step in plan['steps']]
    assert [p['realpath'] for p in pinned] == [str(resolve_executable('cat')), str(exe), str(resolve_executable('wc'))]
    assert all(p == recipes.pin(p['path'], project) for p in pinned)
    assert plan['risk'] == 'execution' and plans.verify(plan, project) == plan
    exe.write_bytes(exe.read_bytes() + b'\0')
    with pytest.raises(PolicyError, match=r'\(steps\[1\]\.executable: sha256, size'):
        plans.verify(plan, project)
    assert plans.from_argv([{'argv': ['ls']}, {'argv': ['rm', 'x']}], project, pipeline='linear')['risk'] == 'dangerous'
    one = [{'argv': ['ls']}]
    for count, pipeline in ((1, 'linear'), (5, 'linear'), (2, 'none')):
        with pytest.raises(PolicyError, match='one step'):
            plans.from_argv(one * count, project, pipeline=pipeline)
    tty = [{'argv': ['ls'], 'stdin': 'pty'}, {'argv': ['wc']}]
    with pytest.raises(PolicyError, match='cannot be interactive'):
        plans.from_argv(tty, project, pipeline='linear', interactive=True)
    with pytest.raises(PolicyError, match='PTY'):
        plans.from_argv(tty, project, pipeline='linear')
    with pytest.raises(PolicyError) as exc:
        plans.from_argv([{'argv': ['ls']}, {'argv': ['sh']}], project, pipeline='linear')
    # Phase 46 (1.9.27): inspect_command's own refusal, which for a shell adds the way that works
    assert str(exc.value) == plans.PRIVILEGED + SHELL_HINT
    with pytest.raises(PolicyError, match='one-step'):
        plans.legacy_payload(plan)
    with pytest.raises(PolicyError, match=r'\(pipeline\)'):
        plans.narrow(plans.from_command('cat README.md', project), plan)


def test_interactive_plan_requires_its_pty(project):
    plan = plans.from_argv([{'argv': ['cat'], 'stdin': 'pty'}], project, interactive=True)
    assert plan['interactive'] is True and plan['steps'][0]['stdin'] == 'pty'
    with pytest.raises(PolicyError, match='PTY'):
        plans.from_argv([{'argv': ['cat']}], project, interactive=True)
    with pytest.raises(PolicyError, match='interactive is a boolean'):
        plans.from_argv([{'argv': ['cat'], 'stdin': 'pty'}], project, interactive='yes')


# Projections.

def test_review_view_is_plain_data_without_shell_strings(project):
    argv = ['echo', 'hello world', "it's"]
    plan = plans.from_argv([{'argv': argv, 'env': {'NO_COLOR': '1'}}, {'argv': ['wc', '-c']}], project,
                           pipeline='linear', writable=['build'])
    view = plans.review_view(plan)
    assert json.loads(json.dumps(view)) == view and view['authorization'] == 'none'
    assert [step['argv'] for step in view['steps']] == [argv, ['wc', '-c']]
    assert view['steps'][0]['env'] == {'NO_COLOR': '1'} and view['writable'] == ['build']
    assert view['resources'] == plan['resources'] and view['network'] == plan['network']
    texts = list(strings(view))
    joined = [' '.join(argv), shlex.join(argv), 'wc -c', shlex.join(argv) + ' | wc -c']
    assert not any(j in text for j in joined for text in texts)
    assert 'command' not in texts
    single = plans.review_view(plans.from_command('echo hello world', project))
    assert not any('echo hello world' in text for text in strings(single))
    assert single['steps'][0]['argv'] == ['echo', 'hello', 'world']


def test_profile_inputs_are_resolved_and_confined(project, tmp_path):
    exe = tool(tmp_path)
    plan = plans.from_argv([{'argv': [str(exe), '-V']}, {'argv': ['wc', '-l']}], project, pipeline='linear',
                           writable=['build', 'src/main.py'])
    temp = tmp_path / 'job'
    temp.mkdir()
    (tmp_path / 'job-link').symlink_to(temp)
    marker = 'job-' + 'a' * 32
    inputs = plans.profile_inputs(plan, tmp_path / 'job-link', marker)
    assert set(inputs) == {'executable', 'exec_paths', 'read_paths', 'project_root', 'temp', 'writable_paths', 'network',
                           'process_model', 'job_marker'}
    assert inputs['executable'] == str(exe) and inputs['temp'] == str(temp.resolve())
    assert inputs['project_root'] == str(project) and inputs['job_marker'] == marker
    assert inputs['writable_paths'] == (str(project / 'build'), str(project / 'src' / 'main.py'))
    assert str(tmp_path.resolve() / 'tc') in inputs['exec_paths'] and inputs['read_paths'] == inputs['exec_paths']
    assert (inputs['network'], inputs['process_model']) == ('none', 'tree')
    paths = [inputs['executable'], inputs['project_root'], inputs['temp'], *inputs['exec_paths'],
             *inputs['writable_paths']]
    assert all(Path(p).is_absolute() and str(Path(p).resolve(strict=True)) == p for p in paths)
    assert all(Path(p).is_relative_to(project) for p in inputs['writable_paths'])
    for folder in map(Path, inputs['exec_paths']):
        for other in (project, temp.resolve(), *map(Path, inputs['writable_paths'])):
            assert not (folder == other or folder.is_relative_to(other) or other.is_relative_to(folder))
    (tmp_path / 'tc' / 'scratch').mkdir()
    for bad in (project / 'docs', project, tmp_path, tmp_path / 'missing', 'relative/tmp', tmp_path / 'tc' / 'scratch',
                exe.parent, tmp_path / 'tc', project / 'README.md', None):
        with pytest.raises(PolicyError):
            plans.profile_inputs(plan, bad, marker)
    for bad in ('short', '../escape-marker', 'a b c d e f g h', '', None, 'x' * 129):
        with pytest.raises(PolicyError, match='job marker'):
            plans.profile_inputs(plan, temp, bad)
    online = plans.from_command('ls', project, network=[['api.example.com', 443]])
    assert plans.profile_inputs(online, temp, marker)['network'] == 'broker'
    # A writable path replaced by a link after review never reaches the profile.
    shutil.rmtree(project / 'build')
    (project / 'build').symlink_to(temp)
    with pytest.raises(PolicyError, match='Symbolic links'):
        plans.profile_inputs(plan, temp, marker)


def test_legacy_payload_binds_existing_process_fields(project):
    plan = plans.from_command('ls -la src', project, writable=True, asynchronous=True)
    legacy = plans.legacy_payload(plan)
    assert legacy == {'command': 'ls -la src', 'argv': ['ls', '-la', 'src'], 'root': str(project),
                      'root_identity': plan['root_identity'], 'network': False, 'writable': True, 'risk': 'read-only',
                      'plan_hash': plan['plan_hash']}
    assert set(process_jobs.BOUND) - {'asynchronous'} <= set(legacy) and not set(legacy) & process_jobs.HOST_ONLY
    assert process_jobs._checked({**legacy, 'asynchronous': True})['plan_hash'] == plan['plan_hash']
    spaced = plans.from_argv([{'argv': ['echo', 'hello world', "it's"]}], project)
    payload = plans.legacy_payload(spaced)
    assert inspect_command(payload['command'], project)['argv'] == ['echo', 'hello world', "it's"]
    assert plans.legacy_payload(plans.from_command('ls', project))['writable'] is False


def _proposed_plan_binding(payload, durable):
    """The process_jobs._plan helper proposed in plans-note.md section 2a, verbatim."""
    if not any(key in payload or key in durable for key in ('plan', 'plan_hash')):
        return None
    changed = PolicyError('The process command changed after review. Nothing was started.')
    if not all(key in payload and key in durable for key in ('plan', 'plan_hash')):
        raise changed
    reviewed = plans.validate(durable['plan'])
    if reviewed['plan_hash'] != durable['plan_hash']:
        raise changed
    admitted = plans.narrow(reviewed, payload['plan'])
    projection = plans.legacy_payload(admitted)
    if admitted['plan_hash'] != payload['plan_hash'] or any(
            not process_jobs._same(value, payload.get(key, False) if key == 'writable' else payload.get(key))
            for key, value in projection.items()):
        raise changed
    return admitted


def test_proposed_process_binding_round_trip(project):
    # Proposal (note 2b): inspect data, then the plan's projection replaces the legacy fields.
    command = 'ls  -la src'  # inspect keeps the double space; the projection normalizes it
    data = inspect_command(command, project)
    data.update(asynchronous=True, timeout=120, writable=True)
    plan = plans.from_command(command, project, writable=True, asynchronous=True)
    plan = dict(plan, resources=dict(plan['resources'], timeout=120))
    plan = plans.validate(sealed(plan))
    data.update(plans.legacy_payload(plan), plan=plan)
    assert data['command'] == 'ls -la src' and data['plan_hash'] == plan['plan_hash']
    durable = json.loads(json.dumps(data))
    # Perform (note 2b): write access withdrawn after proposal narrows the plan, never widens it.
    requested = plans.narrow(plan, sealed(dict(copy.deepcopy(plan), writable=[])))
    payload = {**{k: v for k, v in durable.items() if k != 'timeout'}, 'plan': requested,
               'plan_hash': requested['plan_hash'], 'writable': False}
    process_jobs._checked(payload)
    process_jobs._matches(payload, durable, 60)
    assert _proposed_plan_binding(payload, durable) == requested
    assert _proposed_plan_binding({'command': 'ls'}, {'command': 'ls'}) is None
    for bad in ({**payload, 'plan_hash': plan['plan_hash']},                  # hash of the reviewed, not the admitted plan
                {**payload, 'writable': True},                               # projection disagrees with the plan
                {k: v for k, v in payload.items() if k != 'plan'},
                {**payload, 'plan': sealed(dict(copy.deepcopy(plan), process_model='tree', writable=['.'],
                                                 resources=dict(plan['resources'], timeout=121)))}):
        with pytest.raises(PolicyError):
            _proposed_plan_binding(bad, durable)
    with pytest.raises(PolicyError):
        _proposed_plan_binding(payload, dict(durable, plan_hash=requested['plan_hash']))
