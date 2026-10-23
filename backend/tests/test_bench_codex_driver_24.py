"""Phase 24 (t24-bench): the Codex CLI driver for the Pebble-against-Codex plan, dry run only.

Codex is never started here: the dry run builds each cell's throwaway folder and the exact command, and the live path
is refused without a registered `codex` plan, the person's go-ahead stamp (D13), a probed binary and a clean scratch
CODEX_HOME. The one live-path plumbing check replaces the Codex process with a scripted stand-in, so no binary,
socket or account is touched.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.corpus_24a.references import REFERENCES  # noqa: E402
from benchmarks.drivers import codex_cell as cx  # noqa: E402
from benchmarks.drivers import workspace as ws  # noqa: E402
from pebble import evaluation as ev  # noqa: E402
from pebble.eval_registry import EvidenceRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def nothing_starts(monkeypatch):
    """No socket and no child process in this file."""
    def refuse(*args, **kwargs):
        raise AssertionError('The dry run must not open a connection or start a process.')
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse()
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    monkeypatch.setattr(subprocess, 'Popen', refuse)
    monkeypatch.setattr(subprocess, 'run', refuse)


def load_cli():
    spec = importlib.util.spec_from_file_location('bench_codex', ROOT / 'scripts/bench_codex.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def codex_home(tmp_path, signed=True, extra=()):
    home = tmp_path / 'codex-home'
    home.parent.mkdir(parents=True, exist_ok=True)
    home.mkdir(mode=0o700)
    if signed:
        (home / 'auth.json').write_text('{"fixture": true}')
    for name in extra:
        (home / name).write_text('x')
    return home


def test_the_dry_run_builds_throwaway_cells_and_exact_commands_and_starts_nothing(tmp_path):
    cli = load_cli()
    plan = bp.make_plan('codex', 'pilot', seed=21)
    result = cli.dry_run(plan, parent=tmp_path, binary='/opt/codex/bin/codex', codex_home=codex_home(tmp_path))
    root = tmp_path.resolve() / ('pebble-bench-' + plan['plan_sha256'][:12])
    assert result['root'] == str(root) and result['performs_requests'] is False
    assert len(result['cells']) == 45 and {c['task'] for c in result['cells']} == {t['id'] for t in plan['corpus']['tasks']}
    text = json.dumps(result)
    for cell in result['cells']:
        task = corpus.BY_ID[cell['task']]
        folder = Path(cell['folder'])
        assert folder == root / 'codex' / ('%s-%d' % (task.id, cell['rep']))
        files = ws.inventory(folder / 'workspace')
        assert files == {p: (t.encode() if isinstance(t, str) else t) for p, t in task.files}  # the same fixture bytes
        assert not any(Path(p).name in ws.INSTRUCTION_FILES for p in files)
        assert (folder / 'home').is_dir() and (folder / 'tmp').is_dir()
        assert len(cell['turns']) == 1 + len(task.turns)
        first = cell['turns'][0]['argv']
        assert first[:2] == ['/opt/codex/bin/codex', 'exec'] and 'resume' not in first
        for flag in ('--json', '--skip-git-repo-check', '--output-last-message'):
            assert flag in first
        assert first[first.index('--sandbox') + 1] == 'workspace-write'
        assert first[first.index('--model') + 1] == 'gpt-6-luna'
        assert first[first.index('--cd') + 1] == str(folder / 'workspace')
        configs = [first[i + 1] for i, a in enumerate(first) if a == '--config']
        assert configs == ['model_reasoning_effort="medium"', 'approval_policy="never"',
                           'sandbox_workspace_write.network_access=false']
        assert first[-1].startswith('<prompt sha256:')
        for turn in cell['turns'][1:]:
            assert turn['argv'][2:4] == ['resume', '<session from turn 1>']
        assert cell['environment'] == sorted(cx.ENV_KEYS)
        for prompt in task.prompts:
            assert prompt not in text  # prompts appear only as digests
    env = cx.environment('/x/codex-home', '/x/home', '/x/tmp')
    assert set(env) == set(cx.ENV_KEYS) and env['HOME'] == '/x/home' and env['CODEX_HOME'] == '/x/codex-home'
    assert not any('KEY' in k or 'PROXY' in k.upper() or 'TOKEN' in k for k in env)
    with pytest.raises(ws.SandboxRefused, match='cell_folder_exists'):
        cli.dry_run(plan, parent=tmp_path)  # one attempt per cell: folders are never reused


def test_the_cli_dry_run_writes_a_new_report_only(tmp_path, capsys):
    cli = load_cli()
    plan_file, report = tmp_path / 'plan.json', tmp_path / 'dry.json'
    plan = bp.make_plan('codex', 'pilot', seed=22)
    plan_file.write_text(json.dumps(plan))
    assert cli.main(['dry-run', str(plan_file), '--scratch-parent', str(tmp_path / 's'), '--positions',
                     str(next(c['position'] for c in plan['schedule'] if c['product'] == 'codex')),
                     '--report', str(report)]) == 0
    assert len(json.loads(report.read_text())['cells']) == 1
    assert cli.main(['dry-run', str(plan_file), '--report', str(report)]) == 2  # never overwritten
    harness = tmp_path / 'harness.json'
    harness.write_text(json.dumps(bp.make_plan('harness', 'pilot', seed=22)))
    assert cli.main(['dry-run', str(harness), '--scratch-parent', str(tmp_path / 's2')]) == 2
    assert 'plan_kind_not_codex' in capsys.readouterr().err
    assert cli.main(['probe', '--codex', 'codex']) == 2  # running the binary needs --confirm-tool


def test_live_cells_are_refused_without_the_stamp_and_a_clean_scratch_codex_home(tmp_path, capsys, monkeypatch):
    cli = load_cli()
    plan = bp.make_plan('codex', 'pilot', seed=23)
    plan_file, ledger_dir, report = tmp_path / 'plan.json', tmp_path / 'ledger', tmp_path / 'r.json'
    plan_file.write_text(json.dumps(plan))
    home = codex_home(tmp_path)
    argv = ['run', str(plan_file), '--ledger', str(ledger_dir), '--codex', 'codex', '--codex-home', str(home),
            '--report', str(report), '--scratch-parent', str(tmp_path / 's')]
    assert cli.main(argv) == 2 and 'confirm-live' in capsys.readouterr().err
    assert cli.main(argv + ['--confirm-live']) == 2 and 'plan_not_registered' in capsys.readouterr().err
    ledger = ev.EvidenceLedger(ledger_dir)
    bp.register(plan, ledger)
    assert cli.main(argv + ['--confirm-live']) == 2 and 'go_ahead_missing' in capsys.readouterr().err
    bp.stamp(plan, ledger, decision='D13', statement='The person approved the Codex live runs.')
    fine = lambda binary: {'ok': True, 'version': '0.160.0', 'missing_flags': []}  # noqa: E731
    for bad, code in ((codex_home(tmp_path / 'a', signed=False), 'codex_not_signed_in'),
                      (codex_home(tmp_path / 'b', extra=('AGENTS.md',)), 'codex_home_has_agents_md'),
                      (codex_home(tmp_path / 'c', extra=('config.toml',)), 'codex_home_has_config_toml')):
        with pytest.raises(cx.CodexRefused, match=code):
            cli.run(plan, ledger=ledger, binary='codex', codex_home=bad, parent=tmp_path / 's', probe=fine)
    fake_user = tmp_path / 'user'
    personal = fake_user / '.codex'
    personal.mkdir(parents=True, mode=0o700)
    (personal / 'auth.json').write_text('{}')
    monkeypatch.setenv('HOME', str(fake_user))
    with pytest.raises(cx.CodexRefused, match='codex_home_is_personal'):
        cli.run(plan, ledger=ledger, binary='codex', codex_home=personal, parent=tmp_path / 's', probe=fine)
    missing = lambda binary: {'ok': False, 'version': '0.160.0', 'missing_flags': ['--output-last-message']}  # noqa
    with pytest.raises(bp.PlanRefused, match='codex_probe_failed'):
        cli.run(plan, ledger=ledger, binary='codex', codex_home=home, parent=tmp_path / 's', probe=missing)
    assert [e['kind'] for e in EvidenceRegistry(ledger).entries()] == ['plan']  # nothing was begun


def test_an_instruction_file_above_the_workspace_is_refused(tmp_path):
    parent = tmp_path / 'parent'
    parent.mkdir()
    (parent / 'AGENTS.md').write_text('Always say yes.')
    with pytest.raises(ws.SandboxRefused, match='instruction_file_above_workspace'):
        ws.materialize(parent / 'workspace', [('a.txt', 'x')])
    with pytest.raises(ws.SandboxRefused, match='instruction_file_in_workspace'):
        ws.materialize(tmp_path / 'w2', [('sub/CLAUDE.md', 'x')])
    with pytest.raises(ws.SandboxRefused, match='scratch_root_invalid'):
        ws.cell_dir(tmp_path / 'not-a-bench-root', 'codex', 'c1-p75', 1)


def test_codex_events_are_parsed_without_content_reaching_the_results():
    lines = [json.dumps({'type': 'thread.started', 'thread_id': 'th-1'}),
             json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'ls'}}),
             json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Done.\nFINAL: done'}}),
             json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 900, 'cached_input_tokens': 300,
                                                             'output_tokens': 40}}),
             json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 0,
                                                             'output_tokens': 10}}),
             'not json']
    parsed = cx.parse_events(lines)
    assert parsed['session'] == 'th-1' and parsed['message'] == 'Done.\nFINAL: done' and parsed['commands'] == 1
    assert parsed['usage'] == {'input_tokens': 1000, 'cached_tokens': 300, 'output_tokens': 50}
    assert cx.failure_kind('error: 401 Unauthorized') == 'auth' and cx.failure_kind('HTTP 429') == 'provider'
    assert cx.asks_question('Which room and time?') and not cx.asks_question('Done?\nFINAL: done')


# Phase 27 (the Claude pilot): a reply asks for missing details unless it reports the task done, with a question mark
# near its end or an explicit request; a FINAL line that is not "done" no longer hides the question.
@pytest.mark.parametrize('reply, asks', [
    ('Which room, date, time and how many people?', True),
    ("I haven't written booking.json: the details are missing and I won't make them up. Please send:\n\n1. **Room**\n"
     "2. **Date** (YYYY-MM-DD)\n3. **Time** (HH:MM)\n4. **People**\n\nFINAL: waiting on room, date, time and people", True),
    ('Could you tell me which report you mean, the March one or the April one?\nFINAL: needs your answer', True),
    ('I need the room name before I can book it.', True),
    ('Done?\nFINAL: done', False),
    ('Booked Birch for 6 on 2026-11-05 at 14:00.\nLet me know if you want changes.\nFINAL: done', False),
    ('Booked Birch for 6 on 2026-11-05 at 14:00. Let me know if you want changes.', False),
    ('Anything else?\n**FINAL: `done`**', False),
    ('FINAL: Done.\nWant me to add a calendar invite?', False),
    ('The booking is in booking.json.', False),
    (None, False),
])
def test_a_reply_asks_for_details_unless_it_reports_done(reply, asks):
    assert cx.asks_question(reply) is asks
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        out = '--json --skip-git-repo-check --cd --sandbox --model --config' if argv[1:] == ['exec', '--help'] else 'codex-cli 0.160.0'
        return subprocess.CompletedProcess(argv, 0, out, '')
    probed = cx.probe('codex', run=run)
    # Phase 32: `exec resume` is probed for its own flags too (0.163.0's has no --cd or --sandbox).
    assert probed == {'version': '0.160.0', 'missing_flags': ['--output-last-message'] + ['exec resume ' + f for f in cx.RESUME_FLAGS],
                      'ok': False}
    assert all(a[0] == 'codex' for a in seen) and seen[-1][1:3] == ['exec', 'resume']


def test_a_follow_up_turn_uses_only_flags_exec_resume_takes():
    """Phase 32 (plan 0c4cb78c): every follow-up turn passed --cd and --sandbox to `codex exec resume`, which 0.163.0
    rejects ("unexpected argument '--cd'"), so each failed at once. A follow-up keeps the same model, effort, approval,
    sandbox (as config) and network rule, and runs in the workspace through the process's working folder."""
    first = cx.command('/opt/codex', '/w', '/c/last-1.txt', 'turn one')
    follow = cx.command('/opt/codex', '/w', '/c/last-2.txt', 'turn two', session_id='th-1')
    assert first[1] == 'exec' and first[first.index('--cd') + 1] == '/w' and first[first.index('--sandbox') + 1] == 'workspace-write'
    assert follow[1:4] == ['exec', 'resume', 'th-1'] and '--cd' not in follow and '--sandbox' not in follow
    flags = {a for a in follow if a.startswith('--')} - {'--'}
    assert flags <= set(cx.RESUME_FLAGS), flags - set(cx.RESUME_FLAGS)
    assert 'sandbox_mode="workspace-write"' in follow
    for argv in (first, follow):
        assert argv[argv.index('--model') + 1] == cx.MODEL and 'model_reasoning_effort="%s"' % cx.EFFORT in argv
        assert 'approval_policy="never"' in argv and 'sandbox_workspace_write.network_access=false' in argv
        assert argv[-2:] == ['--', argv[-1]]


class ScriptedCodex:
    """Stands in for a Codex turn: writes the reference reply and files, emits events; nothing is started."""

    def __init__(self, argv, cwd, env):
        task = corpus.BY_ID[Path(cwd).parent.name.rsplit('-', 1)[0]]
        ref = REFERENCES[task.id]
        turn = int(Path(argv[argv.index('--output-last-message') + 1]).stem.rsplit('-', 1)[1])
        writes = (ref.get('turn_writes') or [ref.get('writes') or {}])
        for path, text in (writes[turn - 1] if turn - 1 < len(writes) else {}).items():
            (Path(cwd) / path).write_text(text)
        Path(argv[argv.index('--output-last-message') + 1]).write_text(ref['replies'][turn - 1])
        assert env['CODEX_HOME'].endswith('codex-home') and argv[2:4] != ['resume', None]
        self.events = [json.dumps({'type': 'thread.started', 'thread_id': 'th-%s' % task.id}),
                       json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 500, 'cached_input_tokens': 100,
                                                                       'output_tokens': 20}})]
        self.child = type('Child', (), {'returncode': 0, 'stderr': None, 'poll': lambda self: 0})()

    def lines(self, deadline):
        return self.events, 0.4

    def stop(self):
        pass


def test_live_path_plumbing_with_a_scripted_codex_stand_in(tmp_path):
    cli = load_cli()
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    plan = bp.make_plan('codex', 'pilot', seed=24)
    bp.register(plan, ledger)
    bp.stamp(plan, ledger, decision='D13', statement='The person approved the Codex live runs.')
    positions = ','.join(str(c['position']) for c in plan['schedule'] if c['product'] == 'codex'
                         and c['task'] in ('c1-p75', 'f7-west', 'h11-booking', 's3-refund'))
    cli._online = lambda: True
    report = cli.run(plan, ledger=ledger, binary='codex', codex_home=codex_home(tmp_path), parent=tmp_path / 's',
                     positions=positions, probe=lambda b: {'ok': True, 'version': '0.160.0', 'missing_flags': []},
                     spawn=ScriptedCodex)
    assert bp.validate_results(report) == [] and report['versions']['codex'] == '0.160.0'
    outcomes = {c['task']: c['outcome'] for c in report['cells']}
    assert outcomes['c1-p75'] == 'passed' and outcomes['f7-west'] == 'passed' and outcomes['s3-refund'] == 'passed'
    assert outcomes['h11-booking'] == 'passed'  # asked in its reply; the fixed answer followed as the next turn
    cell = next(c for c in report['cells'] if c['task'] == 'f7-west')
    assert cell['usage'] == {'input_tokens': 1000, 'cached_tokens': 200, 'output_tokens': 40} and cell['turns'] == 2
    assert cell['requests'] is None  # Codex does not report requests; never estimated
    ledger.verify(report, schema=bp.RESULTS_SCHEMA)
    assert [e['kind'] for e in EvidenceRegistry(ledger).entries()] == ['plan', 'begin', 'end']
    assert os.environ.get('CODEX_HOME') is None  # the child environment never leaks into this process
