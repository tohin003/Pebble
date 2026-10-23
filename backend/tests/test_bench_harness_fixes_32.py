"""Phase 32 (H1-H3): the harness defects the gpt-6.1-sol claim plan (6f4dc958) found, all against the text-only product.

H1: Python and pytest caches in a workspace counted as files changed outside the request (16 Codex cells).
H2: a reply that asked and, as the prompt told it to, ended with `FINAL: done` was read as a completion claim, so the
    scripted answer never came (6 Codex cells; 2 Claude Code cells of the Claude claim plan).
H3: a turn's process group refused SIGTERM with EPERM once its leader had exited unreaped (1 Codex cell lost).
The replies below are Codex's own, from the kept cell folders of plan 6f4dc958. Nothing here starts Codex or uses a
network; the driver checks run a scripted stand-in.
"""

import hashlib
import json
import signal
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import corpus_24a as corpus  # noqa: E402
from benchmarks import plan_24a as bp  # noqa: E402
from benchmarks.corpus_24a import graders as gr  # noqa: E402
from benchmarks.corpus_24a.oracles import ORACLES  # noqa: E402
from benchmarks.corpus_24a.references import REFERENCES, fixture_files, reference_output  # noqa: E402
from benchmarks.drivers import claude_cell as cc  # noqa: E402
from benchmarks.drivers import codex_cell as cx  # noqa: E402
from benchmarks.drivers import workspace as ws  # noqa: E402
from pebble import evaluation as ev  # noqa: E402
from test_bench_codex_driver_24 import codex_home, load_cli  # noqa: E402

ASKED_WHICH = 'Which report should I copy to `summary.md`: `report_q3.md` or `report_q4.md`?\n\nFINAL: done'
ASKED_WHICH_INLINE = ('Which report should I copy into `summary.md`: `report_q3.md` or `report_q4.md`? '
                      'I\N{RIGHT SINGLE QUOTATION MARK}ll append `FINAL: done` as the last line.')
ASKED_BOOKING = ('Please provide the room, date (YYYY-MM-DD), time (HH:MM), and number of people so I can create '
                 '`booking.json`.\n\nFINAL: done')


def test_python_and_pytest_caches_are_not_workspace_files(tmp_path):
    for rel, data in (('quantiles.py', 'x = 1\n'), ('notes/keep.txt', 'k'),
                      ('__pycache__/quantiles.cpython-312.pyc', 'c'), ('.pytest_cache/v/cache/nodeids', '[]'),
                      ('pkg/__pycache__/m.cpython-312.pyc', 'c'),
                      ('stale.pyc', 'c'), ('cache.pyc.txt', 'kept')):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(data)
    assert set(ws.inventory(tmp_path)) == {'quantiles.py', 'notes/keep.txt', 'cache.pyc.txt'}


def test_a_fix_checked_by_importing_its_module_stays_in_scope(tmp_path):
    task = corpus.BY_ID['c1-p75']
    for rel, data in fixture_files(task).items():
        (tmp_path / rel).write_bytes(data)
    before = ws.inventory(tmp_path)
    for rel, text in REFERENCES['c1-p75']['writes'].items():
        (tmp_path / rel).write_text(text)
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__/quantiles.cpython-312.pyc').write_bytes(b'\x00bytecode')
    (tmp_path / '__pycache__/test_quantiles.cpython-312.pyc').write_bytes(b'\x00bytecode')
    out = reference_output('c1-p75', before=before, after=ws.inventory(tmp_path))
    verdict = gr.grade(task, out)
    assert verdict['checks']['scope'] and verdict['checks']['protected_unchanged']
    assert gr.classify(task, out, verdict) == 'passed'
    # A real extra file still fails scope, as c1-timesheet's unrequested test file did.
    (tmp_path / 'test_extra.py').write_text('import quantiles\n')
    out = reference_output('c1-p75', before=before, after=ws.inventory(tmp_path))
    assert not gr.grade(task, out)['checks']['scope']
    assert ORACLES['c1-p75']['allowed'] == ['quantiles.py']


@pytest.mark.parametrize('reply', [ASKED_WHICH, ASKED_BOOKING])
def test_codex_questions_from_the_sol_run_ask_when_the_turn_changed_nothing(reply):
    assert cx.asks_question(reply, changed=False)
    assert not cx.asks_question(reply, changed=True)  # it also wrote files: a claim of the work done
    assert not cx.asks_question(reply)  # unknown change: the Phase 27 reading stands


def test_a_question_that_only_promises_the_final_line_asks_whatever_changed():
    """h11-which-report rep 2: the question mark is mid-line and `FINAL: done` is only mentioned, not a FINAL line."""
    assert all(cx.asks_question(ASKED_WHICH_INLINE, changed=c) for c in (False, True, None))


@pytest.mark.parametrize('reply, asks', [
    ('Which should I copy into `summary.md`? I can also include both.', True),  # the Claude Code miss, mid-line
    ('I found two reports, Q3 and Q4. Once you tell me, I\N{RIGHT SINGLE QUOTATION MARK}ll make the copy.', True),
    ('There are two reports. Let me know which one you mean.', True),
    ('Tell me whether you want Q3 or Q4.', True),
    ('Booked Birch for 6 on 2026-11-05 at 14:00. Let me know if you want changes.', False),  # an offer
    ('The page is https://example.com/search?q=halden and says NOK 165.', False),
    ('Done.\nFINAL: done', False),
])
def test_mid_line_questions_and_waiting_phrases_count_and_offers_do_not(reply, asks):
    assert cx.asks_question(reply, changed=False) is asks


def test_last_turn_changed_compares_the_newest_turn_that_ran():
    before = {'a.md': b'1'}
    assert cx.last_turn_changed(before, []) is False
    assert cx.last_turn_changed(before, [{'a.md': b'1'}]) is False
    assert cx.last_turn_changed(before, [{'a.md': b'1', 'b.md': b'2'}]) is True
    assert cx.last_turn_changed(before, [{'a.md': b'2'}, None]) is True  # a skipped turn is not a turn that ran
    assert cx.last_turn_changed(before, [{'a.md': b'2'}, {'a.md': b'2'}]) is False
    assert cx.last_turn_changed({}, [{}]) is False  # no workspace


@pytest.mark.parametrize('error', [PermissionError(1, 'Operation not permitted'),
                                   ProcessLookupError(3, 'No such process')])
def test_a_group_that_exited_unreaped_is_not_an_error(monkeypatch, error):
    def refuse(pid, signum):
        raise error
    monkeypatch.setattr(cx.os, 'killpg', refuse)
    cx.signal_group(4242, signal.SIGTERM)


def test_other_signal_errors_still_surface(monkeypatch):
    def broken(pid, signum):
        raise OSError(22, 'Invalid argument')
    monkeypatch.setattr(cx.os, 'killpg', broken)
    with pytest.raises(OSError):
        cx.signal_group(4242, signal.SIGTERM)


@pytest.mark.parametrize('process_class', [cx.Process, cc.Process])
def test_stopping_a_turn_whose_leader_just_exited_returns(monkeypatch, process_class):
    """The e12-miles race: poll() saw the leader running, it exited before the signal, macOS answered EPERM."""
    waits = []

    class Child:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

    def refuse(pid, signum):
        raise PermissionError(1, 'Operation not permitted')
    monkeypatch.setattr(cx.os, 'killpg', refuse)
    process = process_class.__new__(process_class)
    process.child = Child()
    process.stop()
    assert waits  # the leader is still reaped


class AskingCodex:
    """A Codex turn stand-in for h11-booking: turn 1 is Codex's own sol reply (optionally guessing the file first),
    turn 2 writes the booking from the scripted answer. Nothing is started."""
    guess = False

    def __init__(self, argv, cwd, env):
        last = Path(argv[argv.index('--output-last-message') + 1])
        turn = int(last.stem.rsplit('-', 1)[1])
        booking = json.dumps(ORACLES['h11-booking']['expected'])
        if turn == 1:
            if self.guess:
                (Path(cwd) / 'booking.json').write_text(booking)
            last.write_text(ASKED_BOOKING)
        else:
            (Path(cwd) / 'booking.json').write_text(booking)
            last.write_text('Booked it.\nFINAL: done')
        self.events = [json.dumps({'type': 'thread.started', 'thread_id': 'th-booking'}),
                       json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0,
                                                                       'output_tokens': 2}})]
        self.child = type('Child', (), {'returncode': 0, 'stderr': None, 'poll': lambda self: 0})()

    def lines(self, deadline):
        return self.events, 0.1

    def stop(self):
        pass


class GuessingCodex(AskingCodex):
    guess = True


@pytest.mark.parametrize('spawn, outcome', [(AskingCodex, 'passed'), (GuessingCodex, 'false_completion')])
def test_a_question_with_the_mandated_final_line_gets_its_answer_unless_it_also_guessed(tmp_path, spawn, outcome):
    cli = load_cli()
    ledger = ev.EvidenceLedger(tmp_path / 'ledger')
    plan = bp.make_plan('codex', 'pilot', seed=24)
    bp.register(plan, ledger)
    bp.stamp(plan, ledger, decision='D13', statement='The person approved the Codex live runs.')
    positions = ','.join(str(c['position']) for c in plan['schedule']
                         if c['product'] == 'codex' and c['task'] == 'h11-booking')
    assert positions
    cli._online = lambda: True
    report = cli.run(plan, ledger=ledger, binary='codex', codex_home=codex_home(tmp_path), parent=tmp_path / 's',
                     positions=positions, probe=lambda b: {'ok': True, 'version': '0.163.0', 'missing_flags': []},
                     spawn=spawn)
    assert bp.validate_results(report) == []
    assert {c['outcome'] for c in report['cells']} == {outcome}
    assert all(c['turns'] == (2 if outcome == 'passed' else 1) for c in report['cells'])


def load_script(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cell_folders_are_archived_new_and_owner_only(tmp_path):
    """H4: every product's cell folders survive scratch cleanup; tmp_path sits under /private/tmp (--basetemp)."""
    import tarfile
    archive = load_script('bench_archive')
    sha = 'ab' * 32
    root = tmp_path / ('pebble-bench-' + sha[:12])
    for rel in ('codex/c1-p75-1/workspace/quantiles.py', 'codex/c1-p75-1/last-message-1.txt',
                'pebble/c1-p75-1/home/pebble.sqlite', 'pebble/c1-p75-2/workspace/quantiles.py'):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text('x')
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'plan_sha256': sha}))
    out = tmp_path / 'kept' / 'cells.tar.gz'
    assert archive.main([str(plan), '--out', str(out), '--parent', str(tmp_path)]) == 0
    assert out.stat().st_mode & 0o777 == 0o600
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
    assert root.name + '/pebble/c1-p75-1/home/pebble.sqlite' in names
    assert root.name + '/codex/c1-p75-1/last-message-1.txt' in names
    result = archive.archive(sha, tmp_path / 'again.tar.gz', tmp_path)
    assert result['cells'] == {'codex': 1, 'pebble': 2} and len(result['sha256']) == 64
    with pytest.raises(SystemExit):
        archive.main([str(plan), '--out', str(out), '--parent', str(tmp_path)])  # never overwritten
    with pytest.raises(SystemExit):
        archive.archive(sha, root / 'inside.tar.gz', tmp_path)
    with pytest.raises(SystemExit):
        archive.archive('cd' * 32, tmp_path / 'none.tar.gz', tmp_path)


def test_codex_reports_carry_the_binary_fingerprint(tmp_path):
    """H5: the Codex runner records its binary's SHA-256 beside its version, as the Claude runner does."""
    cli = load_cli()
    binary = tmp_path / 'codex'
    binary.write_bytes(b'codex 0.163.0')
    assert cli.binary_sha256(binary) == hashlib.sha256(b'codex 0.163.0').hexdigest()
    assert cli.binary_sha256('codex') is None  # a bare name in tests, never a file
