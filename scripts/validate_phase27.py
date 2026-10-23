#!/usr/bin/env python3
"""Phase 27 service validator: 1.0.2 caches Claude requests, and the Claude Code benchmark arm stays isolated.

Phase 26 proved the 1.0.1 release (reports/harness-phase26-review.json). Preparing the Pebble-against-Claude-Code
benchmark (D20) found that Pebble's Anthropic route sent no cache_control, so every request of a task paid the full
input price on the whole conversation again (D26). The fix (backend/pebble/providers.py) adds one top-level
{"type": "ephemeral"} marker, which turns on the API's automatic caching, for api.anthropic.com only. The benchmark's
Claude Code arm now runs on the person's existing login (D25): isolated per process, checked on every turn, paced by the
quota windows, and its residue in the person's configuration removable by name. Checks, the same in source and frozen
modes:
  - Anthropic's own API gets exactly one cache marker: a request built by the backend's provider code, its transport a
    stub that records the body, carries the top-level marker and no block-level one, and the reply's cache reads are
    recorded; in frozen mode the executable's archived pebble.providers also carries the marker and the host gate (an
    owned service cannot reach api.anthropic.com, so its request is shown on the source code the archive was built from);
  - an Anthropic-compatible gateway gets none: an owned service (the frozen executable in frozen mode) talks to a
    loopback Anthropic fixture, completes the run, and no request carries cache_control;
  - Claude Code cells on an existing login stay isolated: the person's folder is used as it is (nothing written),
    their skills and agents are known by name, a scratch HOME links only Library/Keychains, Seatbelt allows writes only
    to the configuration and the Keychains, sub-agents are pinned to the plan model, and every breach an init event can
    show (an MCP server, a plugin that is not built in, a CLAUDE.md, a personal skill, a web tool, another model) is
    caught, while the shape Claude Code 2.1.308 printed in the D25 probe passes;
  - the quota paces and stops the cells: a window below 80% starts the next cell, one at or above waits for its reset,
    a reset more than six hours away or unknown stops the run, and a refusal waits for its reset;
  - restoring the person's configuration removes only the benchmark's own project folders: the earlier Pebble worker
    folders, the person's projects, settings, skills and plugins stay, and other differences are named, not touched;
  - the release version is after 1.0.1 and every declaration agrees.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import contextlib
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import Service, backend_path, write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'anthropic_api_requests_carry_one_cache_marker',
    'compatible_gateways_get_no_cache_marker',
    'claude_code_cells_isolated_on_the_existing_login',
    'quota_paces_and_stops_claude_code_cells',
    'restore_removes_only_benchmark_residue',
    'release_version_is_after_1_0_1',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('backend/pebble/providers.py', 'backend/pebble/stats.py', 'benchmarks/drivers/claude_cell.py',
         'benchmarks/drivers/workspace.py', 'scripts/bench_claude.py', 'benchmarks/plan_24a.py',
         'scripts/validate_phase27.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/tauri.conf.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
MODEL = 'claude-opus-5-5'
REPLY = 'Phase 27 fixture reply.'
USAGE = {'input_tokens': 12, 'cache_read_input_tokens': 900, 'cache_creation_input_tokens': 300}
LIMITS = {'max_steps': 4, 'max_seconds': 60}
TRANSIENT = frozenset({'queued', 'running', 'retry_wait'})
# The init event's shape as Claude Code 2.1.308 printed it in the D25 probe (lists shortened; names as printed).
INIT = {'model': MODEL, 'mcp_servers': [],
        'plugins': [{'name': 'cc-plugin-sec-default', 'path': 'builtin', 'source': 'cc-plugin-sec-default@builtin'},
                    {'name': 'cc-plugin-agents-md', 'path': 'builtin', 'source': 'cc-plugin-agents-md@builtin'}],
        'skills': ['deep-research', 'debug', 'verify', 'loop', 'claude-api'],
        'agents': ['claude', 'Explore', 'general-purpose', 'Plan', 'statusline-setup'],
        'slash_commands': ['debug', 'verify'], 'tools': ['Task', 'Bash', 'Edit', 'Read', 'Write', 'Skill'],
        'memory_paths': {'auto': '/Users/person/.claude/projects/-private-tmp-pebble-bench-x-workspace/memory/'}}


def sse(events):
    return [b'data: ' + json.dumps(event).encode() + b'\n' for event in events]


def anthropic_events(text):
    return [{'type': 'message_start', 'message': {'usage': dict(USAGE)}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 6}},
            {'type': 'message_stop'}]


class AnthropicFixture:
    """An owned loopback Anthropic-compatible gateway: /v1/models and streamed /v1/messages, every body recorded."""

    def __init__(self):
        self.lock, self.records = threading.Lock(), []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_GET(self):
                body = json.dumps({'data': [{'id': MODEL, 'display_name': 'Fixture Opus', 'max_input_tokens': 200000}]})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body.encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.records.append({'path': self.path, 'body': body,
                                            'headers': {k.lower(): v for k, v in self.headers.items()}})
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for line in sse(anthropic_events(REPLY)):
                    self.wfile.write(line + b'\n')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/v1' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def frozen_module(executable, name):
    """The raw marshalled code of one module in a PyInstaller executable's archive."""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    archive = CArchiveReader(str(executable))
    with tempfile.TemporaryDirectory(prefix='pebble-phase27-pyz-') as folder:
        for entry in [n for n in archive.toc if n.endswith('.pyz')]:
            path = Path(folder) / 'archive.pyz'
            path.write_bytes(archive.extract(entry))
            reader = ZlibArchiveReader(str(path))
            if name in reader.toc:
                return reader.extract(name, raw=True)
    raise AssertionError('the archive holds no ' + name)


def claude_cell():
    """The benchmark's Claude Code driver, imported as the package it is (it imports the corpus and workspace)."""
    backend_path()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from benchmarks.drivers import claude_cell as module
    return module


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch, backend=None):
        self.scratch, self.backend, self.results = scratch, backend, []

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def command(self):
        return [str(self.backend)] if self.backend else [sys.executable, str(ROOT / 'backend/main.py')]

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def cached():
            backend_path()
            from pebble.app import App
            from pebble.providers import Providers
            sent = []

            @contextlib.contextmanager
            def request(provider, target, headers, body=None, timeout=30):
                sent.append((target, copy.deepcopy(body)))
                yield sse(anthropic_events(REPLY))

            saved = Providers.request, Providers.key
            Providers.request, Providers.key = request, lambda provider, p: 'phase27-fixture-key'
            app = App(scratch / 'in-process' / 'data', scratch / 'in-process' / 'resources', lambda *args: None)
            try:
                app.store.set_setting('model_profiles', [{
                    'id': 'p27', 'kind': 'anthropic', 'name': 'Phase 27 Anthropic', 'url': 'https://api.anthropic.com/v1',
                    'model': MODEL, 'context': 200000, 'budget': 200000, 'budget_version': 2, 'context_mode': 'full',
                    'tools': True}])
                app.store.set_setting('active_profile', 'p27')
                chat = app.new_chat()
                app.work(chat, 'Explain a stack briefly', False, [])
                message = app.chat(chat['id'])['messages'][-1]
            finally:
                app.close()
                Providers.request, Providers.key = saved
            assert sent, 'no request was built'
            for target, body in sent:
                assert target == 'https://api.anthropic.com/v1/messages', target
                assert body.get('cache_control') == {'type': 'ephemeral'}, body.get('cache_control')
                assert json.dumps(body).count('cache_control') == 1, 'a block-level breakpoint was added'
            assert REPLY in message['content'] and message['usage']['cached_tokens'] == USAGE['cache_read_input_tokens']
            evidence = {'requests': len(sent), 'marker': {'type': 'ephemeral'}, 'markers_per_request': 1,
                        'cached_tokens_recorded': message['usage']['cached_tokens']}
            if self.backend is not None:
                raw = frozen_module(self.backend, 'pebble.providers')
                for needle in (b'cache_control', b'ephemeral', b'api.anthropic.com'):
                    assert needle in raw, ('the frozen providers module lacks', needle.decode())
                evidence['frozen_providers_module'] = {'bytes': len(raw), 'carries': ['cache_control', 'ephemeral',
                                                                                      'api.anthropic.com']}
            return evidence
        self.check('anthropic_api_requests_carry_one_cache_marker', cached)

        def gateway():
            fixture = AnthropicFixture()
            service = Service(self.command(), scratch / 'service-home', scratch / 'service', 'phase27-gateway')
            try:
                profile = service.rpc('model_save', name='Phase 27 gateway fixture', url=fixture.url, model=MODEL,
                                      kind='anthropic', context=200000)['profile_id']
                service.rpc('model_select', profile_id=profile)
                cid = service.rpc('new_chat')['id']
                rid = service.rpc('send', id=cid, text='Say hello.', source_ids=[], limits=dict(LIMITS))['run_id']
                deadline = time.monotonic() + 60
                while True:
                    run = service.rpc('run_get', id=cid, run_id=rid)
                    if run['state'] not in TRANSIENT and not service.rpc('state')['active']:
                        break
                    assert time.monotonic() < deadline, ('the run did not settle', run['state'])
                    time.sleep(0.1)
                chat = service.rpc('get_chat', id=cid)
            finally:
                service.close()
                fixture.close()
            assert run['state'] == 'completed', ('the run settled as', run['state'], str(run.get('error'))[:300])
            with fixture.lock:
                records = [dict(r) for r in fixture.records]
            messages = [r for r in records if r['path'].endswith('/messages')]
            assert messages, 'no request reached the gateway'
            assert all('cache_control' not in json.dumps(r['body']) for r in messages), 'a gateway got cache_control'
            assert all(r['headers'].get('anthropic-version') == '2023-06-01' and 'system' in r['body'] for r in messages)
            assert any(REPLY in (m.get('content') or '') for m in chat['messages'] if m.get('role') == 'assistant')
            return {'service': 'frozen' if self.backend else 'source', 'requests': len(messages),
                    'cache_markers': 0, 'run_state': run['state']}
        self.check('compatible_gateways_get_no_cache_marker', gateway)

        cc = claude_cell()

        def isolated():
            home = scratch / 'person'
            config = home / '.claude'
            (config / 'skills/synced/abc/morning').mkdir(parents=True)
            (config / 'skills/synced/abc/morning/SKILL.md').write_text('---\nname: morning\n---\n')
            (config / 'agents').mkdir()
            (config / 'agents/reviewer.md').write_text('an agent')
            (config / 'plugins/cache/vercel').mkdir(parents=True)
            (config / 'settings.json').write_text(json.dumps({'enabledPlugins': {'vercel@x': True}, 'hooks': {}}))
            before = sorted((str(p.relative_to(config)), p.stat().st_mtime_ns) for p in config.rglob('*'))
            try:
                cc.check_claude_config(config, user_home=home)
                raise AssertionError('a personal folder was accepted as a scratch configuration')
            except cc.ClaudeRefused:
                pass
            assert cc.check_claude_config(config, user_home=home, existing_login=True) == config.resolve()
            personal = cc.personal_names(config)
            assert personal == {'morning', 'reviewer'}, personal
            assert sorted((str(p.relative_to(config)), p.stat().st_mtime_ns) for p in config.rglob('*')) == before
            fake = cc.scratch_home(scratch / 'cell-home')
            assert sorted(str(p.relative_to(fake)) for p in fake.rglob('*')) == ['Library', 'Library/Keychains']
            assert os.readlink(fake / 'Library/Keychains') == str(cc.keychains())
            profile = cc.sandbox_profile(config)
            assert profile.count('(allow file-write*') == 2 and '(deny file-write* (subpath "/Users"))' in profile
            assert json.dumps(str(config.resolve())) in profile and json.dumps(str(cc.keychains().resolve())) in profile
            env = cc.environment(config, fake, scratch, '/opt/claude', MODEL)
            assert env['CLAUDE_CODE_SUBAGENT_MODEL'] == MODEL and env['HOME'] == str(fake)
            assert env['USER'] == cc.account() and not {'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'} & set(env)
            argv = cc.command('/opt/claude', MODEL, 'Fix it.', config)
            for flag, value in (('--setting-sources', 'project'), ('--permission-mode', 'bypassPermissions'),
                                ('--mcp-config', '{"mcpServers":{}}'), ('--model', MODEL)):
                assert argv[argv.index(flag) + 1] == value, flag
            assert '--strict-mcp-config' in argv and {'WebFetch', 'WebSearch'} <= set(argv)
            assert cc.isolation_breaches(INIT, MODEL, personal) == [], cc.isolation_breaches(INIT, MODEL, personal)
            cases = {'mcp_servers': {'mcp_servers': [{'name': 'graph'}]},
                     'plugins': {'plugins': [{'name': 'vercel', 'path': str(config / 'plugins/cache/vercel')}]},
                     'memory_files': {'memory_paths': {'auto': '/a/', 'project': ['/Users/person/CLAUDE.md']}},
                     'personal_skills': {'skills': ['debug', 'morning']},
                     'web_tools': {'tools': ['Bash', 'WebSearch']}, 'model': {'model': 'claude-opus-4-8'}}
            for breach, change in cases.items():
                assert breach in cc.isolation_breaches({**INIT, **change}, MODEL, personal), breach
            assert cc.isolation_breaches(None, MODEL, personal) == ['init_missing']
            return {'config_untouched': True, 'personal_names': 2, 'scratch_home': ['Library/Keychains'],
                    'sandbox_writes': ['config', 'keychains'], 'breaches_caught': sorted(cases) + ['init_missing']}
        self.check('claude_code_cells_isolated_on_the_existing_login', isolated)

        def paced():
            now = 2_000_000_000
            info = lambda u, at, status='allowed': {'status': status, 'resetsAt': at, 'rateLimitType': 'five_hour',  # noqa: E731
                                                    'unifiedWindows': {'five_hour': {'utilization': u, 'resetsAt': at}}}
            low, high = cc.rate_summary(info(0.13, now + 3600)), cc.rate_summary(info(0.8, now + 3600))
            far = cc.rate_summary(info(0.95, now + 7 * 3600))
            refused = cc.rate_summary(info(0.4, now + 600, 'rejected'))
            assert cc.quota_wait(None, now) == 0 and cc.quota_wait(low, now) == 0
            assert cc.quota_wait(high, now) == 3660 and cc.quota_wait(far, now) is None
            assert cc.quota_wait(refused, now) == 660
            assert cc.quota_wait({'status': 'rejected', 'resets_at': None, 'windows': {}}, now) is None
            assert cc.failure_kind("You've hit your limit · resets 3am") == 'quota'
            assert cc.failure_kind('Not logged in · Please run /login') == 'auth'
            parsed = cc.parse_events([json.dumps({'type': 'rate_limit_event', 'rate_limit_info': info(0.5, now)})])
            assert parsed['rate_limit']['windows']['five_hour']['utilization'] == 0.5
            return {'pause_at': cc.PAUSE_AT, 'max_pause_seconds': cc.MAX_PAUSE, 'below': 0, 'at_80_percent': 3660,
                    'reset_beyond_six_hours': 'stop', 'refusal': 660}
        self.check('quota_paces_and_stops_claude_code_cells', paced)

        def restored():
            config = scratch / 'restore' / '.claude'
            for rel in ('projects/-private-tmp-pebble-p15-ws', 'projects/-Users-person-Projects-Mine',
                        'skills/synced/abc/morning', 'plugins/cache'):
                (config / rel).mkdir(parents=True)
            (config / 'settings.json').write_text(json.dumps({'model': 'opus'}))
            before = cc.snapshot(config, '2026-10-23T00:00:00+00:00')
            bench = ['-private-tmp-pebble-bench-0123456789ab-claude-code-c1-p75-0-workspace',
                     '-private-tmp-pebble-bench-preflight-x1y2-workspace', '-private-tmp-pebble-claude-probe2-R9-ws']
            for name in bench + ['-Users-person-Projects-New']:
                (config / 'projects' / name).mkdir()
                (config / 'projects' / name / 'session.jsonl').write_text('{}')
            found = cc.residue(before, config)
            assert sorted(found['remove']) == sorted(bench), found['remove']
            assert found['other_new_projects'] == ['-Users-person-Projects-New'] and found['settings_unchanged']
            done = cc.remove_residue(before, config)
            left = sorted(p.name for p in (config / 'projects').iterdir())
            assert sorted(done['removed']) == sorted(bench) and done['after']['remove'] == []
            assert left == ['-Users-person-Projects-Mine', '-Users-person-Projects-New', '-private-tmp-pebble-p15-ws']
            assert (config / 'skills/synced/abc/morning').is_dir() and (config / 'plugins/cache').is_dir()
            return {'removed': len(done['removed']), 'kept': left, 'reported_not_touched': found['other_new_projects']}
        self.check('restore_removes_only_benchmark_residue', restored)

        def version():
            current = checkout_version()
            parts = tuple(int(x) for x in current.split('.'))
            assert parts > (1, 0, 1), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_after_1_0_1', version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase27-')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before == after and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
