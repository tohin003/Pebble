"""Phase 20 parent fix: the development-worker audit matched a worker's files by plain prefix, so one worker's audit
also took the streams and artifacts of another whose name extends it (d20-install / d20-installed, d20-faults /
d20-faults2). Names are now matched exactly."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('audit_dev_workers', ROOT / 'scripts' / 'audit_dev_workers.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_a_worker_whose_name_extends_another_is_never_merged_into_it(tmp_path):
    for name in ('d20-install-stream.jsonl', 'd20-install-task.md', 'd20-install-account', 'd20-install-session-id',
                 'd20-install-stderr.log', 'd20-installed-stream.jsonl', 'd20-installed-task.md',
                 'd20-installed-stderr.log', 'd20-faults-stream.jsonl', 'd20-faults2-stream.jsonl',
                 'i20-merge-stream.jsonl', 'i20-merge-followup-1.md', 'i20-merge-followup1-stream.jsonl',
                 'i20-merge-followup1-stderr.log', 'i20-merge-followup10-stream.jsonl', 'i20-merge-governor-resume-1.md'):
        (tmp_path / name).write_text('x')
    names = lambda w, **k: [p.name for p in audit.worker_files(tmp_path, w, **k)]
    assert names('d20-install', streams_only=True) == ['d20-install-stream.jsonl']
    assert names('d20-installed', streams_only=True) == ['d20-installed-stream.jsonl']
    assert names('d20-faults', streams_only=True) == ['d20-faults-stream.jsonl']
    assert names('d20-install') == ['d20-install-account', 'd20-install-session-id', 'd20-install-stderr.log',
                                    'd20-install-stream.jsonl', 'd20-install-task.md']
    assert names('i20-merge', streams_only=True) == ['i20-merge-followup1-stream.jsonl', 'i20-merge-followup10-stream.jsonl',
                                                     'i20-merge-stream.jsonl']
    assert set(names('i20-merge')) == {'i20-merge-stream.jsonl', 'i20-merge-followup-1.md', 'i20-merge-followup1-stream.jsonl',
                                       'i20-merge-followup1-stderr.log', 'i20-merge-followup10-stream.jsonl',
                                       'i20-merge-governor-resume-1.md'}
