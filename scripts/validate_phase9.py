"""Owned source/frozen scheduled-job and managed-Git service verification.

Uses a temporary home/repository and an owned loopback SSE fixture. No installed
service, user repository, live account or external destination is modified.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


def until(predicate, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(.08)
    raise AssertionError("Owned Phase 9 fixture did not settle.")


def rejected(service, method, **params):
    try:
        service.rpc(method, **params)
    except RuntimeError:
        return
    raise AssertionError("Invalid native request was admitted: " + method)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    requests, checks = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append({'model': body.get('model'), 'tools': len(body.get('tools', []))})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': {'content': 'Stable synthetic scheduled result.'}}]}).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-phase9-service-') as scratch:
            home, repo = Path(scratch) / 'home', Path(scratch) / 'repo'
            home.mkdir(); repo.mkdir()
            env = {'PATH': '/usr/bin:/bin', 'HOME': scratch, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                   'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                   'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}

            def git(*argv):
                return subprocess.run(['/usr/bin/git', '-C', str(repo), *argv], env=env, check=True, capture_output=True, timeout=10).stdout.decode()

            git('init', '-q')
            source = repo / 'source.txt'
            source.write_text('committed baseline\n')
            git('add', '--', 'source.txt'); git('commit', '-qm', 'Fixture baseline')
            base = git('rev-parse', 'HEAD').strip()
            source.write_text('existing uncommitted human work\n')
            dirty_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            service = Service(command, home)
            try:
                service.rpc('model_save', name='Phase 9 synthetic', url=f'http://127.0.0.1:{server.server_port}', model='fixture')
                cid = service.rpc('new_chat')['id']
                foreign = service.rpc('new_chat')['id']
                assert service.rpc('job_list', id=cid)['jobs'] == []
                schedule, limits = {'kind': 'once', 'at': int(time.time()) + 4}, {'max_steps': 4, 'max_seconds': 30}
                pin = service.rpc('job_preview', id=cid, schedule=schedule, limits=limits)
                job = service.rpc('job_create', id=cid, name='Fixture monitor', task='Report the synthetic monitor result.',
                                  schedule=schedule, monitor=True, limits=limits, config_fingerprint=pin['config_fingerprint'])
                job_id = job['id']
                rejected(service, 'job_pause', id=foreign, job_id=job_id, revision=job['revision'])
                rejected(service, 'job_create', id=cid, name='Forged config', task='Inspect.', schedule={'kind': 'interval', 'seconds': 60}, permissions={'mode': 'full'})
                checks.append('native schedule scope and computed permissions')
            finally:
                service.close()
            service = Service(command, home)
            try:
                packet = until(lambda: (p if (p := service.rpc('job_list', id=cid))['jobs'][0].get('last_run_id') else None))
                rid = packet['jobs'][0]['last_run_id']
                run = until(lambda: (r if (r := service.rpc('run_get', id=cid, run_id=rid))['state'] == 'completed' else None))
                events = service.rpc('run_events', id=cid, run_id=rid)
                assert any(e['kind'] == 'scheduled_trigger' for e in events), events
                assert len(requests) == 1, requests
                transcript = service.rpc('get_chat', id=cid)
                assert not any(m['role'] == 'user' for m in transcript['messages'])
                checks += ['due native-reviewed job survives service restart', 'one scheduled run with host trigger and no fabricated human input']
                packet = until(lambda: (p if (p := service.rpc('job_list', id=cid))['notifications'] else None))
                notice = packet['notifications'][0]
                service.rpc('notification_ack', id=cid, notification_id=notice['id'])
                service.rpc('notification_ack', id=cid, notification_id=notice['id'])
                assert not service.rpc('job_list', id=cid)['notifications']
                checks.append('durable native notification and idempotent acknowledgement')
                pin = service.rpc('job_preview', id=cid, schedule={'kind': 'interval', 'seconds': 60}, limits=limits)
                repeat = service.rpc('job_create', id=cid, name='Paused fixture', task='Inspect the monitor.', schedule={'kind': 'interval', 'seconds': 60}, monitor=True,
                                     limits=limits, config_fingerprint=pin['config_fingerprint'])
                paused = service.rpc('job_pause', id=cid, job_id=repeat['id'], revision=repeat['revision'])
                rejected(service, 'job_preview_resume', id=cid, job_id=repeat['id'], revision=repeat['revision'])
                pin = service.rpc('job_preview_resume', id=cid, job_id=repeat['id'], revision=paused['revision'])
                resumed = service.rpc('job_resume', id=cid, job_id=repeat['id'], revision=paused['revision'], config_fingerprint=pin['config_fingerprint'])
                service.rpc('job_delete', id=cid, job_id=repeat['id'], revision=resumed['revision'])
                service.rpc('send', id=cid, text='/jobs')
                assert len(requests) == 1
                checks.append('revision-bound native pause/review/resume/delete and no-inference command')
                gid = service.rpc('new_chat')['id']
                service.rpc('set_workspace', id=gid, path=str(repo))
                status = service.rpc('git_status', id=gid)
                assert status, status
                review = service.rpc('git_preview_create', id=gid)
                assert review['base'] == base, review
                rejected(service, 'git_create_worktree', id=foreign, review_id=review['review_id'])
                review = service.rpc('git_preview_create', id=gid)
                worktree = service.rpc('git_create_worktree', id=gid, review_id=review['review_id'])
                assert hashlib.sha256(source.read_bytes()).hexdigest() == dirty_hash
                worktree_id, worktree_path = worktree['worktree_id'], Path(worktree['path'])
                assert (worktree_path / 'source.txt').read_text() == 'committed baseline\n'
                assert worktree['base'] == base
                checks += ['typed Git inspection and scoped explicit worktree review', 'detached exact-commit worktree preserves dirty source']
                (worktree_path / 'source.txt').write_text('proposed worktree result\n')
                exported = service.rpc('git_export', id=gid, worktree_id=worktree_id, file_paths=['source.txt'])
                assert exported['proposals'] == [] and exported['skipped'][0]['reason'] == 'stale_parent_changed'
                assert hashlib.sha256(source.read_bytes()).hexdigest() == dirty_hash
                rejected(service, 'git_export', id=foreign, worktree_id=worktree_id, file_paths=['source.txt'])
                rejected(service, 'git_export', id=gid, worktree_id=worktree_id, file_paths=['.git/config'])
                rejected(service, 'git_preview_remove', id=gid, worktree_id=worktree_id)
                checks.append('scoped bounded file export is data only and dirty removal is refused')
                (worktree_path / 'source.txt').write_text('committed baseline\n')
                remove = service.rpc('git_preview_remove', id=gid, worktree_id=worktree_id)
                service.rpc('git_remove_worktree', id=gid, review_id=remove['review_id'])
                assert not worktree_path.exists() and hashlib.sha256(source.read_bytes()).hexdigest() == dirty_hash
                checks.append('reviewed clean removal owns only the managed worktree')
                service.rpc('send', id=gid, text='/git')
                assert len(requests) == 1
                checks.append('typed Git host command uses no inference')
            finally:
                service.close()
            service = Service(command, home)
            try:
                assert len(service.rpc('run_list', id=cid)) == 1
                assert len(requests) == 1
                service.rpc('delete_chat', id=cid)
                rejected(service, 'job_list', id=cid)
                checks += ['settled schedule does not replay on second restart', 'chat deletion removes job ownership']
            finally:
                service.close()
    finally:
        server.shutdown(); server.server_close()
    paths = ['backend/main.py', *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'backend/pebble').glob('*.py'))], 'scripts/validate_phase9.py']
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source', 'passed': True,
              'checks': checks, 'synthetic_provider_attempts': len(requests),
              'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
              'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
              'limits': 'Owned loopback provider and macOS temporary repository only; no installed/other-platform/real-model/signing/production proof.'}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
