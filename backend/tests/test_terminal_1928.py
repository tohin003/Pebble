"""Phase 46 (1.9.28, w9-git follow-up 1): the Phase 10 terminal contracts.

T1 (on): the CHMOD, RUNTIME, SHELL and SUBSTITUTION hints no longer point at a way round that turns a task into a false
completion (private case d-rc-105: the right count through python3, the exec bit the task needed still off); when the
task needs the thing itself, the model says so plainly instead of reporting the task done.

T2 (follow-up 2, D178: the person's "Allow both, reviewed", on unless PEBBLE_1928_TERMINAL=0): a reviewed `chmod
+x|u+x|a+x` of one regular project file and leading plain `NAME=value` words outside a deny list, with values free of $,
backticks and newlines and held to the project's paths; the card says the change in words (payload.effect). Everything
else the Phase 10 matrix refuses (test_phase10_security) stays refused with the switch on, and sh script.sh stays out.
"""

import os
import sys
import threading

import pytest

from pebble import terminal
from pebble.policy import PolicyError
from pebble.terminal import inspect_command, run_command

SANDBOX = sys.platform == 'darwin' and os.path.exists('/usr/bin/sandbox-exec')


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    (root / 'bin').mkdir(parents=True)
    (root / 'bin' / 'tool').write_text('#!/usr/bin/env python3\nprint("tool ran")\n')
    (root / 'show.py').write_text('import os\nprint("REPORT_DATE=" + os.environ.get("REPORT_DATE", "unset"))\n')
    return root


@pytest.fixture
def switched(monkeypatch):
    monkeypatch.delenv('PEBBLE_1928_TERMINAL', raising=False)  # D178: on by default


# -- T1 (the switch off) ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('command, hint', [('chmod +x bin/tool', terminal.CHMOD_HINT), ('sh run.sh', terminal.SHELL_HINT),
                                           ('REPORT_DATE=1 python3 show.py', terminal.SUBSTITUTION_HINT)])
def test_each_hint_says_to_report_the_limit_plainly_instead_of_a_way_round(project, command, hint, monkeypatch):
    monkeypatch.setenv('PEBBLE_1928_TERMINAL', '0')
    with pytest.raises(PolicyError) as refused:
        inspect_command(command, project)
    assert hint in str(refused.value) and terminal.PLAINLY.strip() in str(refused.value)
    assert 'instead of reporting the task done' in terminal.RUNTIME_HINT
    with pytest.raises(PolicyError, match='say so plainly'):
        terminal.resolve_executable('bin/tool')


# -- T2 off: unchanged -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('command', ['chmod +x bin/tool', 'REPORT_DATE=2026-09-30 python3 show.py'])
def test_without_the_switch_the_contracts_stand(project, command, monkeypatch):
    monkeypatch.setenv('PEBBLE_1928_TERMINAL', '0')
    with pytest.raises(PolicyError, match='blocked'):
        inspect_command(command, project)


# -- T2 on (D178: the default) -------------------------------------------------------------------------------------------

@pytest.mark.parametrize('value', [None, '1', 'yes'])
def test_d178_the_switch_is_on_unless_it_is_0(project, value, monkeypatch):
    if value is None:
        monkeypatch.delenv('PEBBLE_1928_TERMINAL', raising=False)
    else:
        monkeypatch.setenv('PEBBLE_1928_TERMINAL', value)
    assert terminal.terminal_switch() and inspect_command('chmod +x bin/tool', project)['effect']
    with pytest.raises(PolicyError, match='chmod \\+x FILE` adds it under review') as refused:
        terminal.resolve_executable('bin/tool')  # a script by path: the hint names the fix, not a way round
    assert str(refused.value).startswith('Use a runtime') and 'say so plainly' not in str(refused.value)


@pytest.mark.usefixtures('switched')
def test_d178_only_plain_assignments_set_variables_and_their_values_keep_to_the_project(project):
    for command in ("'FOO=1' ls", '"FOO=1" ls', 'FOO=a\\ b ls', 'FOO="a b" python3 show.py', "FOO='x' python3 show.py"):
        with pytest.raises(PolicyError, match='Quoted or escaped environment overrides are blocked'):
            inspect_command(command, project)
    for command, match in (('DATA=../notes.txt python3 show.py', 'stay in the selected project'),
                           ('DATA=.. python3 show.py', 'stay in the selected project'),
                           ('A=1 sh run.sh', 'Privileged'), ('A=1; ls', 'one command at a time'),
                           ('A=1 && ls', 'one command at a time')):
        with pytest.raises(PolicyError, match=match):
            inspect_command(command, project)
    assert inspect_command('TZ=UTC  NO_COLOR=1 python3 show.py', project)['env'] == {'TZ': 'UTC', 'NO_COLOR': '1'}

@pytest.mark.usefixtures('switched')
def test_with_the_switch_chmod_adds_only_the_exec_bit_of_one_project_file_under_review(project):
    checked = inspect_command('chmod +x bin/tool', project)
    assert checked['argv'] == ['chmod', '+x', 'bin/tool'] and checked['requires_review'] and checked['risk'] == 'dangerous'
    assert checked['effect'].startswith('Adds the exec bit to bin/tool') and 'env' not in checked
    assert inspect_command('chmod u+x bin/tool', project)['argv'][1] == 'u+x'
    (project / 'bin' / 'dir').mkdir()
    (project / 'link').symlink_to(project / 'bin' / 'tool')
    (project / '.env').write_text('K=1\n')
    for command in ('chmod 777 bin/tool', 'chmod -x bin/tool', 'chmod +x bin/tool show.py', 'chmod -R +x bin',
                    'chmod +x bin/missing', 'chmod +x bin/dir', 'chmod +x link', 'chmod +x .env', 'chmod +x ../x',
                    'chmod g+w bin/tool', 'chmod +s bin/tool'):
        with pytest.raises(PolicyError):
            inspect_command(command, project)


@pytest.mark.usefixtures('switched')
def test_with_the_switch_a_variable_is_set_for_the_command_only_and_shown_in_words(project):
    checked = inspect_command('REPORT_DATE=2026-09-30 LANG=C python3 show.py', project)
    assert checked['argv'] == ['python3', 'show.py'] and checked['env'] == {'REPORT_DATE': '2026-09-30', 'LANG': 'C'}
    assert checked['effect'] == 'Sets REPORT_DATE=2026-09-30, LANG=C for this command only (nothing else in its environment changes)'
    assert checked['requires_review'] and checked['command'].startswith('REPORT_DATE=2026-09-30')
    for command in ('PATH=/tmp python3 show.py', 'PYTHONPATH=. python3 show.py', 'DYLD_INSERT_LIBRARIES=x python3 show.py',
                    'GIT_DIR=x python3 show.py', 'HTTPS_PROXY=http://x python3 show.py', 'NODE_OPTIONS=--x node a.js',
                    'A=$HOME python3 show.py', 'A=1', 'A=1 A=2 python3 show.py', 'HOME=/tmp python3 show.py',
                    'env A=1 python3 show.py'):
        with pytest.raises(PolicyError):
            inspect_command(command, project)


@pytest.mark.usefixtures('switched')
@pytest.mark.parametrize('command,match', [
    ('sudo rm -rf build', 'Privileged'), ('bash -c "echo hi"', 'Privileged'), ('sh run.sh', 'Privileged'),
    ('cat .env', 'Credential'), ('cat ../notes.txt', 'stay in the selected project'), ('git push origin main', 'typed Git'),
    ('ls && rm src.py', 'one command at a time'), ('echo $(whoami)', 'substitution'),
    ('A=1 sudo ls', 'Privileged'), ('A=1 cat .env', 'Credential'),
])
def test_the_phase_10_deny_matrix_holds_with_the_switch_on(project, command, match):
    # test_phase10_security's terminal matrix (section 8), restated with the switch on (D178: the default)
    with pytest.raises(PolicyError, match=match):
        inspect_command(command, project)


@pytest.mark.skipif(not SANDBOX, reason='actual macOS confinement')
@pytest.mark.usefixtures('switched')
def test_the_reviewed_change_runs_in_the_same_sandbox_and_a_changed_variable_is_refused(project):
    payload = {**inspect_command('chmod +x bin/tool', project), 'writable': True}
    assert run_command(payload, threading.Event())['exit_code'] == 0
    assert os.stat(project / 'bin' / 'tool').st_mode & 0o100
    payload = {**inspect_command('REPORT_DATE=2026-09-30 python3 show.py', project), 'writable': True}
    assert run_command(payload, threading.Event())['output'].strip() == 'REPORT_DATE=2026-09-30'
    with pytest.raises(PolicyError, match='changed after review'):
        run_command({**payload, 'env': {'REPORT_DATE': '1999-01-01'}}, threading.Event())
