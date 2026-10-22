"""Live Pebble and baseline cells of a registered, stamped 24a plan (scripts/bench_harness.py run).

Order of checks, all before the first owned App: the plan verifies, its corpus is unchanged, it is registered and
stamped with the person's go-ahead (D13); the metadata is the ledger's recorded evaluation connection, never the
installed app's (V1-015). Then the run is begun in the registry, its cells run one attempt each inside an egress
allowlist (only the ChatGPT hosts and loopback resolve), and the signed, content-free results are ended in the
registry. A crash is a retained cell (error_driver), never a retry.
"""

import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from pebble import evaluation as ev
from pebble.eval_recovery_18 import RecoveryHook

from .. import corpus_24a as corpus
from .. import plan_24a as bp
from ..corpus_24a import graders as gr
from . import baseline_cell, pebble_cell, rpc_cell
from . import workspace as ws

ALLOWED_HOSTS = frozenset({'chatgpt.com', 'api.openai.com', 'auth.openai.com', 'localhost', '127.0.0.1', '::1'})


class HostAllowlist:
    """In-process egress allowlist: name resolution outside ``allowed`` is refused and recorded (host names only)."""

    def __init__(self, allowed=ALLOWED_HOSTS):
        self.allowed, self.refused, self.lock = frozenset(allowed), [], threading.Lock()

    @contextmanager
    def active(self):
        original = socket.getaddrinfo

        def guarded(host, *args, **kwargs):
            name = host.decode() if isinstance(host, bytes) else str(host)
            if name not in self.allowed and not name.endswith('.chatgpt.com'):
                with self.lock:
                    if len(self.refused) < 100:
                        self.refused.append(name[:120])
                raise socket.gaierror('egress refused during the benchmark: ' + name[:120])
            return original(host, *args, **kwargs)
        socket.getaddrinfo = guarded
        try:
            yield self
        finally:
            socket.getaddrinfo = original

    def report(self):
        with self.lock:
            return {'refused_lookups': len(self.refused), 'hosts': sorted(set(self.refused))[:10],
                    'allowed': sorted(self.allowed)}


def run_cells(plan, product, cells, route, *, root, resources=None, progress=None, backend_command=None, trap=None):
    """Run planned cells of one product; returns content-free result cells. Shared by live and scripted runs.
    With ``backend_command`` Pebble cells run over JSON-RPC against that backend (the frozen candidate)."""
    driver = pebble_cell if product == 'pebble' else baseline_cell
    hook = RecoveryHook()
    limits = plan['limits']
    rows = []
    for planned in cells:
        task = corpus.BY_ID[planned['task']]
        try:
            if product == 'pebble' and backend_command:
                out, metrics = rpc_cell.run_cell(task, planned, root, command=backend_command, route=route,
                                                  resources=resources, limits=limits, trap=trap)
            else:
                out, metrics = driver.run_cell(task, planned, root, route, resources=resources, limits=limits,
                                               restart=hook)
            verdict = gr.grade(task, out) if out.state not in ('not_run', 'timeout') and not out.error else None
            outcome = gr.classify(task, out, verdict)
        except Exception as exc:  # one attempt per cell: a crash is counted, never retried
            out, verdict, outcome = None, None, 'error_driver'
            metrics = {'review': {}, 'requests': None, 'usage': None, 'wall': None, 'first': None,
                       'error': 'driver_' + type(exc).__name__.lower()}
        rows.append(bp.result_cell(planned, task, out, verdict, outcome, review=metrics['review'],
                                   requests=metrics['requests'], wall=metrics['wall'], first=metrics['first'],
                                   usage=metrics['usage'], error=metrics['error'], restarts=metrics.get('restarts')))
        if progress:
            progress(planned, outcome)
    return rows


def candidate_command(plan, backend):
    """The frozen candidate's command, only when its SHA-256 equals the one the plan pinned (§7.2)."""
    import hashlib
    from pathlib import Path
    pinned = (plan.get('candidate') or {}).get('backend_sha256')
    if not pinned:
        raise bp.PlanRefused('candidate_not_pinned')
    path = Path(backend)
    if not path.is_file() or path.is_symlink():
        raise bp.PlanRefused('candidate_missing')
    if hashlib.sha256(path.read_bytes()).hexdigest() != pinned:
        raise bp.PlanRefused('candidate_sha256_mismatch')
    return [str(path.resolve())]


def run_product(plan, product, *, ledger, metadata_path, resources=None, scratch_parent=ws.TMP,
                installed_homes=None, positions=None, auth_factory=None, progress=None, backend=None):
    if product not in ('pebble', 'baseline') or product not in plan.get('products', ()):
        raise bp.PlanRefused('product_not_in_plan')
    bp.require_go_ahead(plan, ledger)
    command = None
    if product == 'pebble' and (plan['kind'] == 'codex' or backend is not None):
        if backend is None:
            raise bp.PlanRefused('frozen_candidate_required')  # §7.2: Pebble 1.0 is the frozen candidate
        command = candidate_command(plan, backend)
    metadata = ev.load_live_metadata(metadata_path)
    ev.require_evaluation_connection(metadata, ledger, installed=ev.installed_connection_identities(installed_homes))
    cells = [c for c in plan['schedule'] if c['product'] == product]
    if positions:
        cells = [c for c in cells if c['position'] in set(positions)]
        if len(cells) != len(set(positions)):
            raise bp.PlanRefused('positions_not_cells_of_product')
    source = ev.source_fingerprint()
    if source['sha256'] != plan['source']['sha256']:
        raise bp.PlanRefused('source_drift')
    route = {'mode': 'live', 'profile': ev.validate_live_profile(metadata['profile']),
             'host_id': metadata['chatgpt_host_id'], 'auth_factory': auth_factory}
    root = ws.plan_root(plan['plan_sha256'], scratch_parent)
    evaluation_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    bp.begin(ledger, plan, cells, evaluation_id)
    allowlist, trap = HostAllowlist(), None
    if command:
        from .rpc_cell import _measure
        trap = _measure().EgressTrap().start()
    try:
        with allowlist.active():
            rows = run_cells(plan, product, cells, route, root=root, resources=resources, progress=progress,
                             backend_command=command, trap=trap)
    finally:
        network = {**allowlist.report(), **({'backend_proxy_attempts': trap.report()['attempts']} if trap else {})}
        if trap:
            trap.stop()
    if ev.source_fingerprint()['sha256'] != source['sha256']:
        rows = [{**row, 'outcome': 'error_driver', 'success': False, 'error': 'source_drift'} for row in rows]
    from pebble import VERSION
    report = bp.sign_results(ledger, plan, product, rows, evaluation_id=evaluation_id, evidence='live_model',
                             versions={'pebble': VERSION, 'source_sha256': source['sha256'],
                                       'candidate_sha256': (plan.get('candidate') or {}).get('backend_sha256')
                                       if command else None, 'driver': 'json_rpc' if command else 'owned_app',
                                       'model': route['profile']['model'], 'effort': 'medium'},
                             started_at=started_at, network=network)
    bp.end(ledger, report)
    return report
