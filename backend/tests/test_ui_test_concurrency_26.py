"""Phase 26: the interface tests run one file at a time wherever they are graded.

Phase 26 trial 1 failed one of 171 interface tests: the avatar's E1 frame fixture (scripts/measure_companion_frames.mjs)
measured a longest frame of 52.27 ms against its < 50 ms bar while 63 other test files ran beside it. The fixture
measures real time per frame in its Node process, so the longest frame took the pauses of a busy machine: four parallel
runs gave 28.97 to 58.95 ms, four serial runs 1.32 to 1.63 ms. The bar is unchanged; the runner gives the fixture the
uncontended process it assumes.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_npm_test_ui_runs_one_file_at_a_time():
    script = json.loads((ROOT / 'package.json').read_text())['scripts']['test:ui']
    assert script.startswith('node --test --test-concurrency=1 ')


def test_the_clean_clone_gate_runs_node_tests_one_file_at_a_time():
    source = (ROOT / 'scripts/publish_tree.py').read_text()
    assert "runner([node, '--test', '--test-concurrency=1'] + node_tests" in source
    assert "runner([node, '--test'] + node_tests" not in source


def test_the_frame_fixture_keeps_its_bar():
    text = (ROOT / 'scripts/measure_companion_frames.mjs').read_text()
    assert 'long_frame_ms: 50,' in text and 'pass: longest < BARS.long_frame_ms' in text
