"""Phase 18 source/frozen JSON-RPC proof for E06: telemetry privacy and the resource sampler.

Drives the actual service over its JSON-RPC stdio contract, launched from the
main.rs environment allowlist, with an owned loopback fake provider and owned
temporary homes outside the checkout (scripts/measure_resources.py supplies the
launcher, provider, sampler and probes). Every model turn is scripted; every
telemetry change is the native main-window route a person uses. No real model,
network beyond loopback, account, credential or user data is used.

Checks (each recorded once under a planned unique name; source and frozen runs
record the same names):

  telemetry_routes_native_only        The four telemetry routes are in
      tool_authz.NATIVE_ONLY and the main.rs ALLOWED list, never in the pet
      allowlist or the model catalogue, and TelemetrySettings.vue reaches only them.
  candidate_ships_no_evaluation_module   G15/F7: no oracle or grader module (pebble.eval*,
      pebble.memory_eval) ships. Frozen: the candidate's own PYZ table of contents;
      source: every pebble module the entry point and the App import in a clean interpreter.
  resource_sampler_self_test          libproc tree walk, footprint, ri_diskio and
      the CPU time unit against wait4 on short-lived children this validator owns.
  r3_cold_start_first_response_and_loopback_only   Two fresh-home cold starts
      answer their first RPC; lsof shows loopback sockets only; no telemetry bytes.
  telemetry_off_by_default            telemetry_get on a fresh home: off, no consent,
      no destination, no events.
  r1_idle_loopback_only_and_sampler_reads_service  A short idle: the sampler reads
      the live service tree; every lsof snapshot is loopback only.
  r2_scripted_loopback_only           Scripted coding (with a native approval),
      research from local documents and memory runs settle; loopback only.
  telemetry_zero_bytes_by_default     After R1 and R2 with telemetry untouched, the
      home has no telemetry folder.
  telemetry_model_cannot_enable       A scripted model calls telemetry_set and
      telemetry_export as tools: neither is offered, both are refused, it stays off.
  telemetry_stale_consent_refused     An older consent version cannot enable it.
  telemetry_consent_recorded          The current version enables it and records the
      version and time, in a 0700 folder with 0600 files.
  telemetry_run_events_bounded_and_redacted  Runs whose prompt, file, memory,
      vault-like value, URL and output carry canaries leave run_settled events
      that validate against pebble.telemetry/1, within the bounds, with no canary.
  telemetry_export_redacted           An export through a native single-use
      destination token is a new 0600 file with no canary and no upload.
  telemetry_disable_clears_queue      Turning it off deletes the queue and state.
  backend_inputs_stable_during_campaign  The checkout (and executable) did not change.

A check that waits for a named parent integration (the native telemetry routes,
the run-settlement hook or the native destination token) is recorded as pending,
never as passed, and the report then does not pass.

Usage:
  python scripts/validate_phase18.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file. --backend is an explicit frozen executable owned by
the current user; the default runs backend/main.py from this checkout. In both
modes the schema validator and constants come from this checkout's telemetry.py.

This is fixture evidence on this machine: no live model, real account, network,
battery measurement, installed or signed desktop, other platform, the frozen
resource numbers themselves (scripts/measure_resources.py, parent step) or an
independent human audit.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_resources as mr  # noqa: E402

ROOT = mr.ROOT
sys.path.insert(0, str(ROOT / 'backend'))
from pebble import telemetry  # noqa: E402  (the checkout's schema, used to grade what the service wrote)

VERIFIER_SECONDS = 240
IDLE_SECONDS = 5
COLD_TRIALS = 2
CANARY_RUNS = 3
STALE_CONSENT = 'pebble.telemetry.consent/0'
TOKEN = os.urandom(4).hex()
CANARY_PROMPT = 'phase18-prompt-canary-' + TOKEN
CANARY_FILE = 'phase18-file-canary-' + TOKEN
CANARY_MEMORY = 'phase18-memory-canary-' + TOKEN
CANARY_OUTPUT = 'phase18-output-canary-' + TOKEN
CANARY_HOST = 'phase18-' + TOKEN + '.invalid'
CANARY_SECRET = 'sk-live-phase18' + TOKEN * 4
CANARIES = (CANARY_PROMPT, CANARY_FILE, CANARY_MEMORY, CANARY_OUTPUT, CANARY_HOST, CANARY_SECRET)
# The native single-use destination route of the Phase 17 export pattern; the parent aligns this name with
# the merged route. The shell calls it after the Save dialog; the validator calls it as the shell does.
DESTINATION_ROUTE = 'telemetry_destination_bind'  # merged at the Phase 18 parent integration

PLANNED = (
    'telemetry_routes_native_only',
    'candidate_ships_no_evaluation_module',
    'resource_sampler_self_test',
    'r3_cold_start_first_response_and_loopback_only',
    'telemetry_off_by_default',
    'r1_idle_loopback_only_and_sampler_reads_service',
    'r2_scripted_loopback_only',
    'telemetry_zero_bytes_by_default',
    'telemetry_model_cannot_enable',
    'telemetry_stale_consent_refused',
    'telemetry_consent_recorded',
    'telemetry_run_events_bounded_and_redacted',
    'telemetry_export_redacted',
    'telemetry_disable_clears_queue',
    'backend_inputs_stable_during_campaign',
)
GAPS = {
    'routes': 'Parent patch 2: telemetry_get, telemetry_set, telemetry_export and telemetry_clear in app.py dispatch, '
              'tool_authz.NATIVE_ONLY and the main.rs ALLOWED list (main window only).',
    'hook': 'Parent patch 2: the run-settlement hook calling telemetry.observe_run(telemetry.run_record(...)).',
    'destination': 'Phase 17 export pattern: the native single-use destination token route (' + DESTINATION_ROUTE + ').',
}


class Pending(AssertionError):
    """A check waiting for a named parent integration; never a pass."""

    def __init__(self, gap, detail=''):
        super().__init__(GAPS[gap] + (' ' + detail if detail else ''))
        self.gap = gap


def bounded(exc, limit=1500):
    return (type(exc).__name__ + ': ' + str(exc))[:limit]


def script_probe(messages):
    """The model tries the native telemetry routes as tools, one after the other."""
    done = mr.tool_results(messages)
    if done == 0:
        return mr.tool_call('telemetry_set', {'enabled': True, 'consent_version': telemetry.CONSENT_VERSION}, 'probe-set')
    if done == 1:
        return mr.tool_call('telemetry_export', {'destination_token': 'p' * 32}, 'probe-export')
    return {'content': 'FINAL: telemetry is the person\'s choice.'}


def script_canary(messages):
    if mr.tool_results(messages) == 0:
        return mr.tool_call('Read', {'file_path': 'secret.md'}, 'canary-read')
    return {'content': 'Summary %s from https://%s/path?key=%s. FINAL: done' % (CANARY_OUTPUT, CANARY_HOST, CANARY_SECRET)}


def tree_bytes(folder):
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink():
        return b''
    return b''.join(p.read_bytes() for p in sorted(folder.rglob('*')) if p.is_file() and not p.is_symlink())


def checkout_digests():
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', 'src/components/TelemetrySettings.vue', 'scripts/validate_phase18.py',
             'scripts/measure_resources.py']
    digests = {}
    for path in paths:
        try:
            digests[path] = mr.sha(ROOT / path)
        except OSError:
            digests[path] = None
    return digests


def native_route_lists():
    """(ALLOWED, pet allowlist) of src-tauri/src/main.rs."""
    source = (ROOT / 'src-tauri/src/main.rs').read_text()
    allowed = re.search(r'const ALLOWED: &\[&str\] = &\[([^\]]*)\]', source)
    pet = re.search(r'window\.label\(\)=="pet" && !\[([^\]]*)\]', source)
    if not allowed or not pet:
        raise AssertionError('main.rs no longer has the expected ALLOWED and pet lists.')
    names = lambda match: set(re.findall(r'"([a-z_]+)"', match.group(1)))
    return names(allowed), names(pet)


def component_routes():
    source = (ROOT / 'src/components/TelemetrySettings.vue').read_text()
    return {m.strip() for m in re.findall(r'\brpc(?:<[^>]*>)?\(([^,)]*)', source)}


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.checks, self.names, self.gaps = [], set(), set()
        self.observations = {}
        self.fatal = None
        self.stderr_tails = []
        self.libproc = mr.LibProc()
        self.provider = mr.Provider(scripts={'probe': script_probe, 'canary': script_canary})
        self.trap = mr.EgressTrap()

    def check(self, name, fn):
        assert name in PLANNED, 'Unplanned check name: ' + name
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Pending as exc:
            self.gaps.add(exc.gap)
            self.checks.append({'name': name, 'passed': False, 'pending': str(exc)[:600], 'gap': exc.gap})
            return False
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def finish(self):
        for name in PLANNED:
            if name in self.names or name == 'backend_inputs_stable_during_campaign':
                continue
            self.names.add(name)
            self.checks.append({'name': name, 'passed': False, 'not_run': True,
                                'error': 'Not run: ' + (self.fatal or 'an earlier step stopped the campaign.')[:600]})

    def call(self, service, method, **params):
        """A native telemetry route; a route the service does not have yet is pending."""
        try:
            return service.rpc(method, **params)
        except RuntimeError as exc:
            if str(exc) == 'Unknown request.':
                raise Pending('destination' if method == DESTINATION_ROUTE else 'routes',
                              '(the service answered Unknown request. for ' + method + ')') from None
            raise

    def refused(self, service, method, **params):
        try:
            self.call(service, method, **params)
        except Pending:
            raise
        except RuntimeError as exc:
            return str(exc)[:300]
        raise AssertionError(method + ' was accepted.')

    # --- campaign ----------------------------------------------------------------------------

    def run_all(self, scratch):
        self.observations['resources'] = mr.resources_preflight()
        self.trap.start()
        try:
            self._run_all(scratch)
        finally:
            self.trap.stop()
            self.observations['egress_trap'] = self.trap.report()

    def _run_all(self, scratch):
        self.check('telemetry_routes_native_only', self.routes_native_only)
        self.check('candidate_ships_no_evaluation_module', self.no_evaluation_module)
        self.check('resource_sampler_self_test', lambda: self.sampler_self_test(scratch))
        self.check('r3_cold_start_first_response_and_loopback_only', lambda: self.cold_starts(scratch))
        self.provider.start()
        home = scratch / 'home'
        home.mkdir(mode=0o700)
        service = None
        try:
            service = mr.Backend(self.command, home, trap=self.trap)
            service.rpc('command_catalogue')
            self.observations['first_response_seconds'] = round(service.first_response_seconds, 3)
            self.check('telemetry_off_by_default', lambda: self.off_by_default(service, home))
            # Fixture provider, permissions and memory notes are set before the idle window; nothing runs in it.
            self.setup(service)
            self.check('r1_idle_loopback_only_and_sampler_reads_service', lambda: self.idle(service))
            self.check('r2_scripted_loopback_only', lambda: self.scripted(service, scratch))
            self.check('telemetry_zero_bytes_by_default', lambda: self.zero_bytes(home))
            self.check('telemetry_model_cannot_enable', lambda: self.model_cannot_enable(service, scratch, home))
            self.check('telemetry_stale_consent_refused', lambda: self.stale_consent(service, home))
            self.check('telemetry_consent_recorded', lambda: self.consent(service, home))
            self.check('telemetry_run_events_bounded_and_redacted', lambda: self.redacted(service, scratch, home))
            self.check('telemetry_export_redacted', lambda: self.export(service, scratch))
            self.check('telemetry_disable_clears_queue', lambda: self.disable(service, home))
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            if service is not None:
                try:
                    exit_record = service.close()
                    self.observations['service_exit'] = exit_record
                    if exit_record['exit_code'] != 0 or self.fatal:
                        self.stderr_tails.append(service.stderr_tail(4000))
                except Exception as exc:
                    self.fatal = self.fatal or bounded(exc)
            self.provider.stop()
        self.finish()

    # --- checks ------------------------------------------------------------------------------

    def no_evaluation_module(self):
        """G15/F7 (parent step): the evaluation stack holds oracles and graders, so none of it may ship."""
        def oracle(name):
            return name.startswith('pebble.eval') or name == 'pebble.memory_eval'
        if self.backend:
            from PyInstaller.archive.readers import CArchiveReader
            names, where = set(CArchiveReader(str(self.backend)).open_embedded_archive('PYZ.pyz').toc), 'frozen PYZ'
        else:
            code = ("import sys; sys.path.insert(0, 'backend'); import main, pebble.app; "
                    "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('pebble'))))")
            done = subprocess.run([sys.executable, '-I', '-c', code], cwd=ROOT, capture_output=True, text=True,
                                  timeout=120, check=True)
            names, where = set(done.stdout.split()), 'source import closure'
        assert 'pebble.app' in names, where + ' does not contain pebble.app'
        shipped = sorted(n for n in names if oracle(n))
        assert not shipped, (where, shipped)
        return {'where': where, 'pebble_modules': sum(n.startswith('pebble') for n in names), 'evaluation_modules': []}

    def routes_native_only(self):
        from pebble import tool_authz
        from pebble.tool_catalogue import TOOLS
        routes = set(telemetry.ROUTES)
        catalogue = {t['function']['name'].lower() for t in TOOLS}
        assert not catalogue & routes, ('A model tool is named like a telemetry route.', sorted(catalogue & routes))
        allowed, pet = native_route_lists()
        assert not pet & routes, ('The pet window can reach telemetry routes.', sorted(pet & routes))
        reached = component_routes()
        assert reached and reached <= {"'" + r + "'" for r in routes}, ('TelemetrySettings.vue reaches', sorted(reached))
        missing = sorted(routes - set(tool_authz.NATIVE_ONLY)) + sorted('main.rs:' + r for r in routes - allowed)
        if missing:
            raise Pending('routes', 'Missing: ' + ', '.join(missing))
        return {'routes': sorted(routes), 'pet_reaches': [], 'component_reaches': sorted(reached)}

    def sampler_self_test(self, scratch):
        folder = scratch / 'sampler'
        folder.mkdir(mode=0o700)
        result = mr.self_test(folder, libproc=self.libproc)
        assert result['passed'] is True, result
        return result['evidence'] | {'checks': sorted(result['checks'])}

    def lsof(self, service, label):
        """An lsof snapshot with only loopback sockets, and no egress attempt refused by the trap so far."""
        snapshot = mr.lsof_snapshot(self.libproc.tree(service.pid), label)
        assert snapshot['status'] == 'measured', ('lsof could not inspect the service.', snapshot.get('reason'))
        assert snapshot['non_loopback'] == 0, ('Non-loopback sockets.', snapshot['non_loopback_names'])
        self.no_egress()
        return snapshot

    def no_egress(self):
        egress = self.trap.report()
        assert egress['attempts'] == 0, ('The service tried to reach the network (refused by the trap).',
                                         egress['targets'], self.observations.get('resources'))

    def cold_starts(self, scratch):
        rows = []
        for trial in range(COLD_TRIALS):
            home = scratch / ('cold-%d' % trial)
            home.mkdir(mode=0o700)
            service, row = mr.Backend(self.command, home, trap=self.trap), {'trial': trial + 1}
            try:
                service.rpc('command_catalogue')
                row['first_response_seconds'] = round(service.first_response_seconds, 3)
                row['lsof'] = self.lsof(service, 'cold start %d' % trial)
            finally:
                row['exit'] = service.close()
                rows.append(row)
            footprint = mr.telemetry_footprint(home)
            assert footprint == {'exists': False, 'bytes': 0}, ('Telemetry bytes after a cold start.', footprint)
        assert all(r['exit']['exit_code'] == 0 for r in rows), [r['exit'] for r in rows]
        return {'trials': len(rows), 'first_response_seconds': [r['first_response_seconds'] for r in rows],
                'connections': [r['lsof']['connections'] for r in rows],
                'maxrss_bytes': [r['exit']['maxrss_bytes'] for r in rows]}

    def off_by_default(self, service, home):
        status = self.call(service, 'telemetry_get')
        assert status['enabled'] is False and status['consent_version'] is None and status['events'] == 0, status
        assert status['destination'] is None and status['network_upload'] is False, status
        assert status['current_consent_version'] == telemetry.CONSENT_VERSION, status['current_consent_version']
        assert not os.path.lexists(home / 'telemetry'), 'Reading the status created telemetry files.'
        return {'enabled': False, 'current_consent_version': status['current_consent_version']}

    def idle(self, service):
        sampler = mr.Sampler(service.pid, libproc=self.libproc).start()
        snapshots = [self.lsof(service, 'idle 0s')]
        time.sleep(IDLE_SECONDS)
        snapshots.append(self.lsof(service, 'idle %ds' % IDLE_SECONDS))
        summary = sampler.stop()
        assert summary['samples'] >= 3 and summary['footprint_bytes']['peak'] > 0, summary
        assert summary['cpu_seconds'] >= 0 and summary['members_max'] >= 1, summary
        return {'samples': summary['samples'], 'footprint_peak_bytes': summary['footprint_bytes']['peak'],
                'cpu_percent_of_one_core': summary['cpu_percent_of_one_core'], 'snapshots': len(snapshots)}

    def setup(self, service):
        service.rpc('model_save', name='Phase 18 fixture', url=self.provider.url, model='phase18-fixture', context=32768)
        service.rpc('setting', key='permissions', value=mr.PERMISSIONS)
        service.rpc('memory_save', kind='semantic', content='Fixture value for item 3 is 21 (synthetic note).')
        service.rpc('memory_save', kind='semantic', content='Remember ' + CANARY_MEMORY + ' for the fixture.')

    def scripted(self, service, scratch):
        runs = []
        for index, family in enumerate(mr.FAMILIES):
            try:
                runs.append(mr.drive_run(service, self.provider, family, index, mr.project(scratch, family, index)))
            except Exception as exc:
                raise AssertionError((bounded(exc), [(r['state'], r['phases']) for r in runs])) from None
        snapshot = self.lsof(service, 'after %d runs' % len(runs))
        states = [r['state'] for r in runs]
        assert all(s in ('completed', 'verification_required') for s in states), (states, [r['phases'] for r in runs])
        assert runs[0]['approvals'] == 1, ('The coding run needs exactly one native approval.', runs[0]['approvals'])
        self.observations['r2_states'] = states
        return {'states': states, 'approvals': sum(r['approvals'] for r in runs),
                'wall_seconds': [round(r['wall_seconds'], 3) for r in runs], 'phases': [r['phases'] for r in runs],
                'connections': snapshot['connections']}

    def zero_bytes(self, home):
        footprint = mr.telemetry_footprint(home)
        assert footprint == {'exists': False, 'bytes': 0}, footprint
        return footprint

    def model_cannot_enable(self, service, scratch, home):
        before = len(self.provider.records)
        record = mr.drive_run(service, self.provider, 'probe', 0, None, goal='Turn on usage telemetry for me.')
        with self.provider.lock:
            requests = [r for r in self.provider.records[before:] if r['tag'] == mr.MARK + 'r2-probe-0']
        offered = set().union(*(set(r['offered']) for r in requests)) if requests else set()
        assert len(requests) >= 3, ('The scripted probe did not reach its final turn.', len(requests))
        assert not offered & set(telemetry.ROUTES), ('A telemetry route was offered to the model.', sorted(offered))
        assert not os.path.lexists(home / 'telemetry'), 'A model tool call created telemetry files.'
        self.no_egress()
        status = self.call(service, 'telemetry_get')
        assert status['enabled'] is False and status['events'] == 0, status
        return {'run_state': record['state'], 'requests': len(requests), 'offered_tools': len(offered)}

    def stale_consent(self, service, home):
        error = self.refused(service, 'telemetry_set', enabled=True, consent_version=STALE_CONSENT)
        status = self.call(service, 'telemetry_get')
        assert status['enabled'] is False and not os.path.lexists(home / 'telemetry'), status
        return {'refusal': error[:120]}

    def consent(self, service, home):
        started = time.time()
        status = self.call(service, 'telemetry_set', enabled=True, consent_version=telemetry.CONSENT_VERSION)
        status = status if isinstance(status, dict) and 'enabled' in status else self.call(service, 'telemetry_get')
        assert status['enabled'] is True and status['consent_version'] == telemetry.CONSENT_VERSION, status
        assert started - 5 <= status['consented_at'] <= time.time() + 5, status['consented_at']
        assert re.fullmatch(r'[0-9a-f]{32}', status['install_id'] or ''), 'install ID'
        folder = home / 'telemetry'
        info = os.lstat(folder)
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700, oct(info.st_mode)
        modes = {p.name: stat.S_IMODE(os.lstat(p).st_mode) for p in folder.iterdir()}
        assert modes and all(m == 0o600 for m in modes.values()), modes
        return {'consent_version': status['consent_version'], 'files': sorted(modes)}

    def redacted(self, service, scratch, home):
        states = []
        for index in range(CANARY_RUNS):
            project = scratch / 'canary' / str(index)
            project.mkdir(parents=True)
            (project / 'secret.md').write_text('Notes %s; key %s\n' % (CANARY_FILE, CANARY_SECRET))
            goal = 'Summarise secret.md %s and https://%s/q?token=%s.' % (CANARY_PROMPT, CANARY_HOST, CANARY_SECRET)
            states.append(mr.drive_run(service, self.provider, 'canary', index, project.resolve(), goal=goal)['state'])
        self.no_egress()
        folder = home / 'telemetry'
        data = tree_bytes(folder)
        for canary in CANARIES:
            assert canary.encode() not in data, 'A canary reached the telemetry folder.'
        queue = (folder / 'queue.jsonl').read_bytes() if (folder / 'queue.jsonl').is_file() else b''
        events = [json.loads(line) for line in queue.splitlines() if line]
        for event in events:
            assert telemetry.validate_event(event) == event, 'A queued event is outside pebble.telemetry/1.'
        settled = [e for e in events if e['kind'] == 'run_settled']
        assert len(queue) <= telemetry.MAX_BYTES and len(events) <= telemetry.MAX_EVENTS
        status = self.call(service, 'telemetry_get')
        assert status['events'] == len(events), (status['events'], len(events))
        if not settled:
            raise Pending('hook', '(no run_settled event after %d runs)' % CANARY_RUNS)
        assert len(settled) == CANARY_RUNS, ('One event per settled run.', len(settled))
        assert all(e['data']['tools'].get('Read') == 1 for e in settled), [e['data']['tools'] for e in settled]
        return {'run_states': states, 'events': len(events), 'queue_bytes': len(queue),
                'states_recorded': sorted({e['data']['state'] for e in settled})}

    def export(self, service, scratch):
        target = scratch / 'exports' / 'telemetry.json'
        target.parent.mkdir(mode=0o700)
        minted = self.call(service, DESTINATION_ROUTE, path=str(target))
        token = minted.get('destination_token') if isinstance(minted, dict) else minted
        receipt = self.call(service, 'telemetry_export', destination_token=token)
        assert receipt['verified'] is True and receipt['published'] is False and receipt['network_upload'] is False
        data = target.read_bytes()
        assert stat.S_IMODE(os.lstat(target).st_mode) == 0o600
        for canary in CANARIES:
            assert canary.encode() not in data, 'A canary reached the export.'
        document = json.loads(data)
        assert document['schema'] == telemetry.EXPORT_SCHEMA and document['destination'] is None
        assert all(telemetry.validate_event(e) == e for e in document['events'])
        again = self.refused(service, 'telemetry_export', destination_token=token)
        return {'events': len(document['events']), 'bytes': len(data), 'reuse_refused': again[:80]}

    def disable(self, service, home):
        status = self.call(service, 'telemetry_set', enabled=False)
        status = status if isinstance(status, dict) and 'enabled' in status else self.call(service, 'telemetry_get')
        assert status['enabled'] is False and status['events'] == 0, status
        assert not os.path.lexists(home / 'telemetry'), 'Disabling left telemetry files.'
        return {'enabled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary homes even on success')
    args = parser.parse_args()
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = mr.owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_before = mr.sha(backend) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase18-')).resolve()
    if scratch.is_relative_to(ROOT):
        parser.error('The temporary folder is inside this checkout; set TMPDIR elsewhere.')
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run_all(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)
        sources_after = checkout_digests()
        backend_after = mr.sha(backend) if backend else None
    results = campaign.checks
    changed = sorted(p for p in set(sources) | set(sources_after) if sources.get(p) != sources_after.get(p))
    stable = not changed and backend_after == backend_before
    stability = {'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                 'evidence': {'checkout_files_compared': len(sources), 'executable_compared': bool(backend)}}
    if not stable:
        stability['error'] = ('Backend inputs changed during the campaign: '
                              + json.dumps({'checkout_files': changed[:20],
                                            'executable': bool(backend) and backend_after != backend_before}))
    results.append(stability)
    results.sort(key=lambda c: PLANNED.index(c['name']))
    complete = [c['name'] for c in results] == list(PLANNED)
    passed = complete and campaign.fatal is None and all(c['passed'] for c in results)
    keep = args.keep_scratch or not passed
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed'] and 'pending' not in c],
        'pending': [c['name'] for c in results if 'pending' in c],
        'planned_checks': list(PLANNED), 'all_planned_checks_recorded': complete,
        'integration_gaps': {key: GAPS[key] for key in sorted(campaign.gaps)},
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration, 'duration_budget_seconds': VERIFIER_SECONDS,
        'synthetic_provider_requests': len(campaign.provider.records),
        'provider_errors': list(campaign.provider.errors),
        'backend_sha256': backend_before,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately. The telemetry schema used to grade '
                        'the service output is this checkout\'s telemetry.py in both modes.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else [],
        'limits': ('Owned loopback synthetic provider, owned temporary homes outside the checkout, actual service '
                   'JSON-RPC launched from the main.rs environment allowlist plus proxy variables that point at an '
                   'owned loopback egress trap (proxy-honouring egress is refused and recorded; raw sockets are '
                   'seen only by lsof snapshots), and native main-window routes. Fixture '
                   'evidence on this machine: no live model, account, network, battery, installed or signed desktop '
                   'or other-platform evidence; lsof sees sockets open at each snapshot, not every connection between '
                   'snapshots; the frozen resource numbers come from scripts/measure_resources.py (a parent step); '
                   'not an independent human security audit.'),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'x', encoding='utf-8') as out:
        out.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'pending': report['pending'], 'fatal_error': campaign.fatal,
                      'duration_seconds': duration, 'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
