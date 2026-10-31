"""Phase 46 (1.9.29, golden dev d-cd-01, 2026-10-30 02:20): the Companion's shell parser reads a heredoc's body as the
input of its command, not as commands (shell_policy._scan, parse, Heredoc).

Before: every body line was split off as a command and the whole line was opaque, so the denied-word scan read the
body, and a Python heredoc script whose comment said "at" was refused as the `at` scheduler. Now a body is data, code
(an interpreter reads it: asked as `python3 -c` is) or commands (a shell reads it: parsed and refused as any command).
Every refusal the parser makes for a real command is kept; the fuzz below checks that against zsh itself.
"""
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from pebble import shell_policy as sp
from test_shell_policy_40 import decide, home  # noqa: F401 (fixture)

ZSH = Path('/bin/zsh')
# a heredoc script of the d-cd-01 kind: words that are denied commands appear only as Python text
SCRIPT = ("cd ~/Desktop && python3 - <<'EOF'\n"
          "import os\n"
          "# sort the receipts at the end, then log in the summary\n"
          "for name in sorted(os.listdir('.')):\n"
          "    if name.startswith('receipt'):\n"
          "        print('security deposit', name)  # at login: nothing\n"
          "EOF")


# -- the body is input --------------------------------------------------------------------------------------------

def test_a_python_heredoc_script_is_one_command_reading_code(home):
    parsed = sp.parse(SCRIPT)
    assert [p.argv for p in parsed.parts] == [('cd', '~/Desktop'), ('python3', '-')]
    assert parsed.opaque == () and [(d.word, d.quoted, d.kind) for d in parsed.heredocs] == [('EOF', True, 'code')]
    assert parsed.heredocs[0].body.startswith('import os\n') and parsed.heredocs[0].body.endswith('nothing\n')
    d = decide(SCRIPT, home)
    assert d.action == 'allow', d.reasons  # 1.9.28: "at changes the system or other apps"
    # one that moves files asks for the folders it names, as `python3 -c` does; it is never refused for its words
    moving = SCRIPT.replace('import os\n', 'import os, shutil\n').replace(
        "print('security deposit', name)", "shutil.move(name, os.path.expanduser('~/Documents/' + name))")
    d = decide(moving, home)
    assert d.action == 'ask' and str(home / 'Documents') in d.folders and d.always is False, d.reasons


@pytest.mark.parametrize('line, argvs, kind', [
    ("cat > notes.md <<'EOF'\nsudo at login security\nEOF", [('cat',)], 'data'),
    ('cat <<EOF\nx\nEOF', [('cat',)], 'data'),
    ('cat <<-EOF\n\tindented\n\tEOF', [('cat',)], 'data'),
    ('cat <<\\EOF\n$(not run)\nEOF', [('cat',)], 'data'),
    ('cat <<"E O"\nbody\nE O\necho after', [('cat',), ('echo', 'after')], 'data'),
    ('cat <<A <<B && echo x\na\nA\nb\nB\necho done', [('cat',), ('echo', 'x'), ('echo', 'done')], 'data'),
    ("tee out.txt <<'EOF' | wc -l\nline\nEOF", [('tee', 'out.txt'), ('wc', '-l')], 'data'),
    ("node <<'EOF'\nconsole.log('at')\nEOF", [('node',)], 'code'),
    ("cat <<'EOF' | python3\nprint('login')\nEOF", [('cat',), ('python3',)], 'code'),
])
def test_a_body_is_never_split_into_commands(line, argvs, kind):
    parsed = sp.parse(line)
    assert [p.argv for p in parsed.parts] == argvs and parsed.opaque == ()
    assert {d.kind for d in parsed.heredocs} == {kind}


def test_a_here_string_is_one_word_of_input():
    parsed = sp.parse('cat <<< "sudo id"')
    assert [p.argv for p in parsed.parts] == [('cat',)] and parsed.opaque == () and parsed.heredocs == ()


@pytest.mark.parametrize('line', [
    "bash <<'EOF'\nsudo id\nEOF", "sh -s <<EOF\nlaunchctl list\nEOF", "zsh <<'EOF'\necho hi; security dump-keychain\nEOF",
    "cat <<'EOF' | sh\nsudo id\nEOF", "env bash <<'EOF'\nkillall Finder\nEOF",
    "cat <<'EOF'\nfine\nEOF\nsudo id",                       # a command after the body
    "cat <<EOF && sudo id\nbody\nEOF",                         # a command on the heredoc's own line
    "cat <<EOF\n$(sudo id)\nEOF",                              # an unquoted end word: the shell runs the substitution
    "cat <<EOF\n`launchctl list`\nEOF",
    "cat <<EOF\nsudo id",                                      # no end line: opaque, read whole as before
    "echo ${a//<<x/y}\nsudo id\nx/y}",                         # not a heredoc to the shell: its lines stay commands
    "echo $[1<<2]\nsudo id\n2]", "echo $((1<<2))\nsudo id\n2))",
    "$(cat <<'EOF'\nsudo id\nEOF\n)",                          # a body that becomes a command
    "bash <<'A'\nbash <<'B'\nsudo id\nB\nA",                  # nested
    # readers that may run what they read keep 1.9.28's reading: the body's words are commands, the line opaque
    "xargs -I{} sh -c '{}' <<'EOF'\nsudo id\nEOF", "sqlite3 notes.db <<'EOF'\n.shell sudo id\nEOF",
    "parallel <<'EOF'\nlaunchctl list\nEOF", "sh -c 'eval \"$(cat)\"' <<'EOF'\nsudo id\nEOF",
    "sh -c 'cat' <<'EOF'\nsudo id\nEOF",
])
def test_every_refusal_of_a_real_command_is_kept(line, home):
    assert decide(line, home).action == 'deny', line


@pytest.mark.parametrize('line', ["awk -f - <<'EOF'\n{ print }\nEOF", "sed -f - in.txt <<'EOF'\ns/a/b/\nEOF",
                                  "xargs echo <<'EOF'\na\nEOF", "patch -p1 <<'EOF'\n--- a\nEOF",
                                  "sqlite3 x.db <<'EOF'\nselect 1;\nEOF"])
def test_a_reader_not_known_to_take_data_reads_it_as_1928_did(line):
    parsed = sp.parse(line)
    assert [d.kind for d in parsed.heredocs] == ['unread'] and 'a heredoc' in parsed.opaque


@pytest.mark.parametrize('line', [
    "python3 - <<'EOF'\nprint(open(os.path.expanduser('~/.ssh/id_ed25519')).read())\nEOF",
    "cat > list.txt <<'EOF'\n~/.ssh/id_ed25519\nEOF",
    "cat <<'EOF'\n~/Library/Messages\nEOF",
])
def test_a_protected_path_in_any_body_is_still_refused(line, home):
    assert decide(line, home).action == 'deny', line


def test_code_in_a_heredoc_asks_for_what_it_writes_and_never_always(home):
    line = ("python3 - <<'EOF'\nimport os, shutil\nshutil.move(os.path.expanduser('~/Documents/notes.txt'), "
            "os.path.expanduser('~/Desktop/notes.txt'))\nEOF")
    d = decide(line, home)
    assert d.action == 'ask' and d.always is False
    assert set(d.folders) == {str(home / 'Documents'), str(home / 'Desktop')}


def test_a_list_fed_to_xargs_asks_for_the_folders_it_names(home):
    d = decide("xargs rm <<'EOF'\n~/Documents/notes.txt\nEOF", home)
    assert d.action == 'ask' and str(home / 'Documents') in d.folders


def test_data_is_not_judged_by_agentguard_as_a_command(home):
    d = decide("cat > ~/Pebble\\ Workbench/howto.md <<'EOF'\nNever run rm -rf / or git push --force.\nEOF", home)
    assert d.action == 'allow', d.reasons


# -- fuzz: zsh is the oracle --------------------------------------------------------------------------------------

DENIED_WORDS = ('sudo', 'at', 'login', 'security', 'launchctl', 'killall')
SAFE_WORDS = ('cat', 'echo', 'printf', 'true', 'python3', 'wc', 'tee')
SHELL_WORDS = ('sh', 'bash', 'zsh')


def _stubs(folder, log):
    """A PATH where every command is a stub that logs its name and drains its input; the shells, xargs and env are the
    real ones (so `xargs -I{} sh -c '{}'` and `env bash` run what they are given, through the stubs)."""
    folder.mkdir()
    for name in DENIED_WORDS + SAFE_WORDS:
        stub = folder / name
        stub.write_text(f'#!/bin/sh\necho {name} >> "{log}"\nexec /bin/cat > /dev/null\n')
        stub.chmod(0o755)
    for name in SHELL_WORDS:
        os.symlink(shutil.which(name, path='/bin') or f'/bin/{name}', folder / name)
    for name in ('xargs', 'env'):
        os.symlink(f'/usr/bin/{name}', folder / name)


def _line(rng, depth=0):
    """A random command line from heredocs, expansions, pipes, lists and nested shells."""
    def word():
        return rng.choice(DENIED_WORDS + SAFE_WORDS)

    def command():
        roll = rng.random()
        if roll < 0.12 and depth < 2:
            return rng.choice(SHELL_WORDS) + " -c '" + word() + "'"
        if roll < 0.2:
            return 'echo ' + rng.choice(('${a//<<x/y}', '$[1<<2]', '$((1<<2))', '"<<EOF"', "'<<'"))
        return word() + rng.choice(('', ' a', ' -n', ' > out.txt'))

    end = rng.choice(('EOF', 'END', 'x/y}', '2]'))
    if rng.random() < 0.15:
        # `<<` that is no here-document to zsh, then real commands and a line that would end one if it were
        fake = rng.choice(('echo ${a:-<<' + end + ' }', 'echo $[1<<' + end + ' ]', 'echo ${a//<<' + end + '}'))
        return '\n'.join([fake] + [command() for _ in range(rng.randint(1, 3))] + [end])
    op = rng.choice(('<<' + end, "<<'" + end + "'", '<<-' + end, '<<\\' + end, '<<"' + end + '"'))
    body = [rng.choice((word(), word() + ' x', '# at login', '$(' + word() + ')', '`' + word() + '`', '\t' + end,
                        end + ' ', 'print("sudo")')) for _ in range(rng.randint(0, 3))]
    if rng.random() < 0.15 and depth < 2:
        body.append(_line(rng, depth + 1))
    reader = rng.choice(SAFE_WORDS + SHELL_WORDS + (rng.choice(SHELL_WORDS) + ' -s', 'xargs echo',
                                                    "xargs -I{} sh -c '{}'", 'env bash', 'xargs -n1 sh -c'))
    head = rng.choice((reader + ' ' + op, command() + ' | ' + reader + ' ' + op, 'cat ' + op + ' | ' + reader))
    closing = [] if rng.random() < 0.1 else [rng.choice((end, '\t' + end if '<<-' in op else end))]
    tail = [command() for _ in range(rng.randint(0, 2))]
    sep = rng.choice(('\n', ' && ', '; ', ' || '))
    return sep.join([head + ('\n' + '\n'.join(body + closing) if body or closing else '')] + tail) if sep != '\n' \
        else '\n'.join([head] + body + closing + tail)


@pytest.mark.skipif(not ZSH.is_file(), reason='zsh is the oracle')
def test_fuzz_no_denied_command_zsh_runs_is_allowed(tmp_path, home):
    rng = random.Random(1929)
    log, bin_ = tmp_path / 'ran.log', tmp_path / 'bin'
    _stubs(bin_, log)
    work = tmp_path / 'work'
    work.mkdir()
    checked = ran_denied = 0
    for _ in range(400):
        line = _line(rng)
        try:
            d = decide(line, home)
        except sp.PolicyError:
            continue
        log.write_text('')
        subprocess.run([str(ZSH), '-f', '-c', line], cwd=work, env={'PATH': str(bin_), 'HOME': str(work)},
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        ran = set(log.read_text().split())
        checked += 1
        if ran & set(DENIED_WORDS):
            ran_denied += 1
            assert d.action == 'deny', (line, sorted(ran), d.action, d.reasons)
    assert checked > 300 and ran_denied > 40, (checked, ran_denied)  # the grammar reaches denied commands often


def test_fuzz_parse_is_bounded_and_never_raises_anything_but_policy_errors():
    rng = random.Random(46)
    alphabet = ['<<', '<<-', '<<<', "'", '"', '\\', '\n', '\t', ' ', 'EOF', '$(', '${', '$[', '}', ']', ')', '(', '`',
                '|', '&&', ';', '#', 'cat', 'sudo', 'x']
    worst = 0.0
    for _ in range(3000):
        line = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 120)))
        began = time.perf_counter()
        try:
            sp.parse(line)
        except sp.PolicyError:
            pass
        worst = max(worst, time.perf_counter() - began)
    hostile = '<<EOF\n' * 1300 + 'x'                     # many pending bodies, under the 8,000-character limit
    began = time.perf_counter()
    sp.parse(hostile)
    assert worst < 0.05 and time.perf_counter() - began < 0.5, worst
