"""Phase 19: the development-worker audit counts what a session actually did.

A file tool whose result is an error (the CLI's --restricted refusal of a path outside the checkout, a failed Edit)
read or wrote nothing; Bash still counts when it fails; permission denials from every result record are honoured.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('audit_dev_workers', ROOT / 'scripts/audit_dev_workers.py')
audit_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_mod)


def stream(tmp_path, events):
    path = tmp_path / 'w1-stream.jsonl'
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
    return path


def call(ident, name, **arguments):
    return {'type': 'assistant', 'message': {'model': 'claude-opus-5-5', 'content': [
        {'type': 'tool_use', 'id': ident, 'name': name, 'input': arguments}]}}


def result_for(ident, error):
    return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': ident, 'is_error': error,
                                                     'content': 'refused' if error else 'ok'}]}}


def test_refused_and_failed_file_calls_are_not_performed_but_failing_bash_is(tmp_path):
    root = str(audit_mod.ROOT)
    events = [{'type': 'system', 'subtype': 'init', 'cwd': root, 'model': 'claude-opus-5-5', 'tools': ['Read', 'Glob', 'Edit', 'Bash'],
               'mcp_servers': []},
              call('g1', 'Glob', pattern='x', path='/Users/someone/.cargo/registry/src'), result_for('g1', True),
              call('e1', 'Edit', file_path=root + '/not_owned.py', old_string='a', new_string='b'), result_for('e1', True),
              call('b1', 'Bash', command='false | cat'), result_for('b1', True),
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'permission_denials': [{'tool_use_id': 'r1'}]},
              call('r1', 'Read', file_path='/Users/someone/secret.txt'),
              call('r2', 'Read', file_path='/Users/someone/other.txt'), result_for('r2', False),
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'permission_denials': []}]
    item = audit_mod.audit(stream(tmp_path, events), [])
    assert '/Users/someone/.cargo/registry/src' not in item['reads_outside_checkout_performed']   # refused
    assert '/Users/someone/secret.txt' not in item['reads_outside_checkout_performed']            # denied, first record
    assert '/Users/someone/other.txt' in item['reads_outside_checkout_performed']                 # actually read
    assert 'not_owned.py' not in (item.get('unowned_writes') or [])                               # the Edit failed
    assert [c['command'] for c in item['non_read_only_bash']] == ['false | cat']                  # Bash still counts
