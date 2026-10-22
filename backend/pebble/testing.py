"""Tests run only in a filtered snapshot, under an OS sandbox, after approval.

A plan pinned by the host at proposal time (recipes.resolve) is re-derived
from the built snapshot and its executable identity is re-verified immediately
before launch. Legacy payloads without a plan resolve python-unittest or
node-test at run time, as before Phase 13; every other recipe needs a plan.

The leader's exit is observed without reaping it, so its process-group ID
stays owned while descendants are torn down. `(allow process*)` remains: any
readable binary may run inside the same confinement, and a descendant that
leaves the group is beyond killpg. Independent containment is a Phase 14 limit.
"""

import os
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
import time
from pathlib import Path

from . import diagnostics, recipes
from .terminal_text import sanitize as sanitize_terminal
from .policy import MAX_FILE, PolicyError, Workspace, digest

LEGACY = ("python-unittest", "node-test")
FILE_LIMIT = 2_000_000
SNAPSHOT_LIMIT = 24_000_000
DISPLAY_LIMIT = 16_000
SKIPPED_LIMIT = 100
ISOLATION = "macOS sandbox · temporary project snapshot · network denied"
STOPPED = "Test stopped, timed out, or exceeded the output limit."
_waitid = getattr(os, "waitid", None)


def _read(ws, name):
    """Bounded no-follow read of one inventoried file; data is None over the size bound."""
    source = ws.path(name)
    changed = "A project file changed during the test snapshot. Run the tests again."
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise PolicyError(changed) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PolicyError(changed)
        if info.st_size > FILE_LIMIT:
            return None, info.st_mode
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(FILE_LIMIT + 1)
    finally:
        if fd is not None:
            os.close(fd)
    return (None if len(data) > FILE_LIMIT else data), info.st_mode


def _collect(ws, drafts, dest=None, hashes_out=None):
    """Snapshot files (copied to dest when given) and the digest of exactly those bytes."""
    listed = ws.inventory()
    hashes, skipped, total = {}, [], 0
    for name in listed["files"]:
        data, mode = _read(ws, name)
        if data is None:
            skipped.append(name)
            continue
        total += len(data)
        if total > SNAPSHOT_LIMIT:
            raise PolicyError("Project snapshot exceeds the 24 MB isolated test limit.")
        hashes[name] = digest(data)
        if dest is not None:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if mode & stat.S_IXUSR:
                os.chmod(target, 0o755)
    included = []
    for draft in drafts or ():
        if not isinstance(draft, dict) or not isinstance(draft.get("path"), str) or not isinstance(draft.get("after"), str):
            raise PolicyError("A staged draft is malformed.")
        name = Path(draft["path"]).as_posix()
        data = draft["after"].encode("utf-8")
        if dest is not None:
            Workspace(dest).draft(draft["path"], draft["after"])
            target = dest / draft["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        else:
            ws.path(name, create=True)
            if len(data) > MAX_FILE:
                raise PolicyError("Draft exceeds the source file limit.")
        hashes[name] = digest(data)
        if name in skipped:
            skipped.remove(name)
        included.append(name)
    entries = sorted([name, sha] for name, sha in hashes.items())
    if hashes_out is not None:
        hashes_out.update(hashes)  # Phase 20 G20-diag: the per-file SHA-256s diagnostics bind to.
    return {
        "snapshot_digest": digest(recipes.canonical(entries).encode()),
        "snapshot_files": len(entries),
        "skipped_large": skipped[:SKIPPED_LIMIT],
        "skipped_large_count": len(skipped),
        "snapshot_truncated": listed["truncated"],
        "drafts_included": sorted(set(included)),
    }


def snapshot_manifest(ws, drafts=()):
    """The snapshot fields a test run of ws plus drafts would record, without copying."""
    return _collect(ws, drafts)


def snapshot_digest(ws, drafts=()):
    """Digest of the current project (plus drafts) as a test snapshot would record it."""
    return _collect(ws, drafts)["snapshot_digest"]


def _quote(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(plan, mount, temp):
    """The unified Phase 14 profile: own-child signals, mach allowlist, exec limited
    to the pinned toolchain, mount and the snapshot (tests run project code)."""
    from .sandbox_profiles import build
    reads = [Path(p) for p in plan["toolchain_reads"] + (mount["reads"] if mount else [])]
    executables = [Path(plan["executable"]["realpath"]).parent, Path(plan["argv"][0]).parent]
    return build(executable=Path(plan["executable"]["realpath"]), exec_paths=[*reads, *executables, temp],
                 read_paths=reads, project_root=temp, temp=temp, writable_paths=(), network="none",
                 job_marker=uuid.uuid4().hex, process_model="tree")


def _exited(process):
    """Observe the leader's exit without reaping it, so its group ID stays owned."""
    if _waitid is None:
        return process.poll() is not None
    try:
        return _waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        # Reaped elsewhere: the status is lost and the group ID is no longer
        # owned, so mark it settled (nothing is signalled) and fail closed.
        process.returncode = -1
        raise PolicyError("The test process exit could not be observed. Its outcome is unknown.") from None


def _kill_group(process):
    # Signal only while the leader is unreaped. Without waitid the legacy order
    # (reap, then signal the group) remains.
    if process.returncode is None or _waitid is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _launch(plan, profile, snapshot, env, output, cancel):
    with output.open("wb") as log:
        process = subprocess.Popen(
            ["/usr/bin/sandbox-exec", "-p", profile, *plan["argv"]],
            cwd=snapshot,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + plan["timeout"]
        try:
            while not _exited(process):
                if cancel.is_set() or time.monotonic() > deadline or output.stat().st_size > plan["output_limit"]:
                    _kill_group(process)
                    process.wait()
                    raise PolicyError(STOPPED)
                time.sleep(0.1)
            # Late descendant output must not follow the framework summary.
            _kill_group(process)
            process.wait()
        finally:
            # Descendants must not outlive the test, even if the parent has exited.
            _kill_group(process)
            if process.returncode is None:
                process.wait()
    size = output.stat().st_size
    if size > plan["output_limit"]:
        raise PolicyError(STOPPED)
    with output.open("rb") as handle:
        return process.returncode, handle.read(plan["output_limit"]), size


def _display(text):
    if len(text) <= DISPLAY_LIMIT:
        return text
    head, tail = 6000, DISPLAY_LIMIT - 6100
    return text[:head] + f"\n… {len(text) - head - tail} characters omitted …\n" + text[-tail:]


def run_tests(payload, cancel):
    from .platform_providers import require as require_platform
    require_platform('tests')
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise PolicyError(
            "Isolated test execution is currently available on macOS. Windows execution stays disabled until its sandbox is implemented."
        )
    recipe = payload["recipe"]
    if not isinstance(recipe, str) or recipe not in recipes.REGISTRY:
        raise PolicyError("Test recipe is not supported.")
    plan, mount = payload.get("plan"), payload.get("mount")
    if plan is None and recipe not in LEGACY:
        raise PolicyError("This test recipe needs a plan pinned when it was proposed. Propose the tests again.")
    if plan is None and mount is not None:
        raise PolicyError("A managed environment mount needs a pinned test plan.")
    if plan is not None and (not isinstance(plan, dict) or plan.get("recipe") != recipe):
        raise PolicyError("The pinned test plan does not match the requested recipe.")
    ws = Workspace(payload["root"])
    if payload.get('root_identity') is not None and list(ws.identity) != payload['root_identity']:
        raise PolicyError('The reviewed project folder was replaced before test execution.')
    pinned = plan is not None
    if not pinned:
        # Legacy pending actions predate pinned plans: resolve now, as before.
        plan = recipes.resolve(recipe, ws)
    recipes.shape(plan)
    with tempfile.TemporaryDirectory(prefix="pebble-test-") as temp, tempfile.TemporaryDirectory(prefix="pebble-test-log-") as logs:
        temp = Path(temp).resolve()
        snapshot = temp / "project"
        snapshot.mkdir()
        file_hashes = {}
        manifest = _collect(ws, payload.get("drafts", []), snapshot, file_hashes)
        # Phase 20 G20-recipes: the manifests and lockfiles that run are the ones reviewed (app.py records them).
        # X20 F20-03: per file, so one unbindable file (a lockfile over 2 MB, or a link) never voids the others' check.
        reviewed, bound = payload.get('reviewed_files'), payload.get('bound_files')
        if isinstance(reviewed, dict):
            now = recipes.review_bindings(plan['recipe'], Workspace(snapshot))
            if any(value != 'unbindable' and now.get(name) != value for name, value in reviewed.items()):
                raise PolicyError('A project manifest or lockfile changed after review. Propose the recipe again.')
        elif isinstance(bound, dict) and recipes.bindings(plan['recipe'], Workspace(snapshot))[0] != bound:
            raise PolicyError('A project manifest or lockfile changed after review. Propose the recipe again.')
        mount = recipes.matches(plan, Workspace(snapshot), mount, ws.root)
        for name, target in (mount["links"] if mount else {}).items():
            try:
                (snapshot / name).symlink_to(target, target_is_directory=True)
            except FileExistsError:
                raise PolicyError(f"The snapshot already contains {name}; the managed environment cannot be linked.") from None
        profile = _profile(plan, mount, temp)
        env = {
            key: str(temp) + value[len(recipes.TEMP):] if value.startswith(recipes.TEMP) else value
            for key, value in plan["env"].items()
        }
        recipes.verify(plan, ws.root)
        # The output log lives outside the sandbox-writable folder.
        code, data, size = _launch(plan, profile, snapshot, env, Path(logs) / "output.log", cancel)
        # Phase 20 G20-diag: hash-bound diagnostics for the host broker's open capture only; the result is unchanged
        # and offer never raises. A stopped launch raised above, so it records nothing.
        diagnostics.offer(plan, code, data, snapshot, file_hashes, ws)
    text = data.decode("utf-8", errors="replace")
    outcome, tests = recipes.classify(plan["kind"], plan["recipe"], code, text)
    shown, removed, _ = sanitize_terminal(data, final=True)
    return {
        "exit_code": code,
        # Classification reads the raw text; the model and UI get the terminal_text projection.
        "output": _display(shown),
        "output_projection": "terminal_text/1",
        "controls_removed": removed,
        "output_bytes": size,
        "output_truncated": len(text) > DISPLAY_LIMIT,
        "outcome": outcome,
        "tests": tests,
        "recipe": plan["recipe"],
        "kind": plan["kind"],
        **manifest,
        "executable_sha256": plan["executable"]["sha256"],
        "pinned": pinned,
        "environment": mount and {key: mount[key] for key in ("kind", "environment_id", "tree_digest")},
        "isolation": ISOLATION,
    }
