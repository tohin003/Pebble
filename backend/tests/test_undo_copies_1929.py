"""Phase 46 (1.9.29, D209, the person: "keep Auto mode's undo copies for a day"): a card-free command's copy (D184)
stays a day after its task ends, so its Undo still works the next morning; D189 removed it when the task ended.

confined.Copies.end stamps the end, Copies.sweep removes an ended task's copies a day later (and the oldest beyond
KEPT_BUDGET, one task's 3 GB), and confined.end_run / copies mark their Undo receipts as gone. The App-level path (a
real command, its Undo after the task ended, the sweep a day later) is test_confined_commands_1928's
test_a_confined_command_runs_without_a_card_with_its_copy_and_undo (restated for D209).
"""
import json
import time

from pebble import confined
from pebble.confined import Copies


def store(base, run_id, held=10, copies=1):
    for index in range(copies):
        folder = base / run_id / f'{index:032x}'
        (folder / 'tree').mkdir(parents=True)
        (folder / 'receipt.json').write_text(json.dumps({'held': held, 'undo': 'available'}))
    return base / run_id


def test_an_ended_tasks_copies_stay_a_day_then_go(tmp_path):
    copies = Copies(tmp_path)
    store(tmp_path, 'r1')
    copies.end('r1', now=1000.0)
    assert copies.sweep(lambda run: True, now=1000.0 + confined.KEEP_SECONDS - 1) == {}
    assert (tmp_path / 'r1').is_dir()
    assert copies.sweep(lambda run: True, now=1000.0 + confined.KEEP_SECONDS) == {'r1': ['0' * 32]}
    assert not (tmp_path / 'r1').exists()


def test_an_open_task_is_never_swept_and_a_resumed_one_starts_its_day_again(tmp_path):
    copies = Copies(tmp_path)
    store(tmp_path, 'r1')
    copies.end('r1', now=0.0)
    assert copies.sweep(lambda run: False, now=10.0 * confined.KEEP_SECONDS) == {}  # running again: kept
    copies.end('r1', now=5.0 * confined.KEEP_SECONDS)  # it ended again
    assert copies.sweep(lambda run: True, now=5.5 * confined.KEEP_SECONDS) == {}
    assert copies.sweep(lambda run: True, now=6.0 * confined.KEEP_SECONDS) == {'r1': ['0' * 32]}


def test_a_task_that_ended_while_pebble_was_closed_starts_its_day_when_seen(tmp_path):
    copies = Copies(tmp_path)
    store(tmp_path, 'r1')  # no stamp: Pebble quit before the task's end was recorded
    assert copies.sweep(lambda run: True, now=500.0) == {}
    assert float((tmp_path / 'r1' / confined.ENDED).read_text()) == 500.0
    assert copies.sweep(lambda run: True, now=500.0 + confined.KEEP_SECONDS) == {'r1': ['0' * 32]}


def test_ended_copies_together_hold_one_tasks_budget_the_oldest_going_first(tmp_path):
    copies = Copies(tmp_path)
    for index, run_id in enumerate(('old', 'mid', 'new')):
        store(tmp_path, run_id, held=confined.KEPT_BUDGET // 2)
        copies.end(run_id, now=100.0 + index)
    assert copies.sweep(lambda run: True, now=200.0) == {'old': ['0' * 32]}
    assert sorted(p.name for p in tmp_path.iterdir()) == ['mid', 'new']


def test_an_unreadable_receipt_counts_as_a_whole_tasks_copies(tmp_path):
    copies = Copies(tmp_path)
    (store(tmp_path, 'r1') / ('0' * 32) / 'receipt.json').write_text('not json')
    store(tmp_path, 'r2', held=1)
    copies.end('r1', now=1.0)
    copies.end('r2', now=2.0)
    assert copies.sweep(lambda run: True, now=3.0) == {'r1': ['0' * 32]}


def test_a_link_in_the_clones_folder_is_never_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep.txt').write_text('mine')
    base = tmp_path / 'clones'
    base.mkdir()
    (base / 'r1').symlink_to(outside)
    copies = Copies(base)
    copies.end('r1', now=0.0)
    assert copies.sweep(lambda run: True, now=10.0 * confined.KEEP_SECONDS) == {}
    assert (outside / 'keep.txt').read_text() == 'mine' and not (outside / confined.ENDED).exists()


def test_the_sweep_is_cheap():
    # per task end, a walk of the clones folder's top level and each kept task's receipts: measured, not a bound per step
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        for index in range(50):
            store(base, f'r{index}', copies=3)
        copies = Copies(base)
        for index in range(50):
            copies.end(f'r{index}', now=0.0)
        began = time.perf_counter()
        copies.sweep(lambda run: True, now=1.0)
        assert time.perf_counter() - began < 0.5
