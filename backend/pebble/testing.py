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

import contextlib
import os
import re
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
from .zone import person_zone

LEGACY = ("python-unittest", "node-test")
FILE_LIMIT = 2_000_000
SNAPSHOT_LIMIT = 24_000_000
DISPLAY_LIMIT = 16_000
SKIPPED_LIMIT = 100
ISOLATION = "macOS sandbox · temporary project snapshot · network denied"
STOPPED = "Test stopped, timed out, or exceeded the output limit."
_waitid = getattr(os, "waitid", None)

# Phase 34 (W1): an ad hoc check is one extra test module the model writes, added to the isolated snapshot only, under
# a name the recipe's own discovery runs. The project never sees it, and its run is kind 'check': never test evidence
# for the completion gate, AgentGuard, diagnostics or memory lessons.
CHECK_FILES = {"python-unittest": "test_pebble_check.py", "node-test": "pebble-check.test.mjs"}
CHECK_LIMIT = 16_000
CHECK_NOTE = ("Ad hoc check: {path} was added to an isolated copy for this run only. The project did not change, and "
              "this run does not count as the project's tests; run RunTests without check to verify the project.")
# Phase 46 (1.9.28, golden dev d-cf-01 and d-lh-91 on Opus: auto chose pytest and refused the check, then a python3 -c
# probe and a second test run followed, 3-10 s a turn). With auto, one RunTests call is two launches, each in its own
# isolated copy: the project's own tests exactly as reviewed, with no code of the check in that copy or process (the
# run that counts; w10's security review, F2-1), then the check ALONE under the first recipe that takes one (the app's
# check_run), whose result is reported beside and never counts. A check with no test of its own never reads as passed.
CHECK_ALONE = {"python-unittest": lambda path: ["-p", path], "node-test": lambda path: [path]}
CHECK_RUN_NOTE = ("Ad hoc check: the project's own tests ran without {path} in one isolated copy, and their result is "
                  "this run's. {path} then ran alone with {recipe} in a second copy; its result is here and never "
                  "counts as the project's tests. The project did not change.")
CHECK_EMPTY = "Ran 0 tests from {path}: it holds no test {recipe} collects, so nothing was checked. "
CHECK_SHOWN = 6000  # characters of the check's own output, shown beside the project's run
UNITTEST_OWN = re.compile(r"^\w+ \(test_pebble_check\.[\w.]+\)", re.M)  # `-v` lines of the check module's own tests
# Node runs a test file that defines no test as one passing test named after the file (v24.14: "✔ pebble-check.test.mjs
# (47ms)", or TAP "ok 1 - pebble-check.test.mjs"); a file with tests shows only its tests (w10's review, F2-2)
NODE_FILE_ONLY = re.compile(r"^(?:✔ |ok \d+ - )pebble-check\.test\.mjs\b", re.M)


def _own_tests(recipe, alone, check, tests, text):
    """How many of the check's own tests ran: alone, every test the run counted, except Node's stand-in for a file that
    defines none; beside python-unittest's discovery, the check module's `-v` lines; else None (node-test does not name
    the file of each test it runs beside others)."""
    if alone:
        if not tests:
            return None
        return 0 if recipe == "node-test" and NODE_FILE_ONLY.search(recipes.clean(text)) else tests["total"]
    if recipe == "python-unittest" and check["path"] == CHECK_FILES[recipe]:
        return len(UNITTEST_OWN.findall(recipes.clean(text)))
    return None


def check_payload(recipe, content):
    """The reviewed check for a proposal, or PolicyError. The host names the file; the model supplies only text."""
    if recipe not in CHECK_FILES:
        raise PolicyError("An ad hoc check runs with the python-unittest or node-test recipe; this project's tests use "
                          f"{recipe}. Check with the project's tests instead.")
    if not isinstance(content, str) or not content.strip():
        raise PolicyError("An ad hoc check needs the test module's source text.")
    if len(content) > CHECK_LIMIT or "\x00" in content:
        raise PolicyError(f"An ad hoc check is limited to {CHECK_LIMIT} characters of text.")
    return {"path": CHECK_FILES[recipe], "content": content, "sha256": digest(content.encode("utf-8"))}


def _check(payload, recipe):
    """The approved payload's check, re-validated against its recipe at run time, or None."""
    check = payload.get("check")
    if check is None:
        return None
    if not isinstance(check, dict) or set(check) != {"path", "content", "sha256"}:
        raise PolicyError("The reviewed check is malformed.")
    fresh = check_payload(recipe, check["content"])
    if fresh != check:
        raise PolicyError("The reviewed check does not match its recipe or its text.")
    return check


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


def _display(text, limit=DISPLAY_LIMIT):
    if len(text) <= limit:
        return text
    head, tail = min(6000, limit // 2), limit - min(6000, limit // 2) - 100
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
    drafts, reviewed, bound = payload.get("drafts", []), payload.get('reviewed_files'), payload.get('bound_files')
    check_run = payload.get("check_run")
    if check_run is None:
        check = _check(payload, plan["recipe"])
        with _copy(ws, drafts, plan, mount, reviewed, bound, check) as (launch, manifest, mount):
            # a check's failures are the model's own probe, not the project's diagnostics
            code, data, size = launch(cancel, offer=check is None)
        result, text = _result(plan, code, data, size, manifest, pinned, mount)
        if check is not None:
            own = _own_tests(plan["recipe"], False, check, result["tests"], text)
            if own == 0 and result["outcome"] == "passed":
                result["outcome"] = "no_tests"  # Phase 46 (1.9.28): the check checked nothing; never shown as a pass
            note = CHECK_NOTE.format(path=check["path"])
            empty = own == 0 and result["outcome"] == "no_tests"
            result.update(kind="check", check={"path": check["path"], "sha256": check["sha256"], "note": (
                CHECK_EMPTY.format(path=check["path"], recipe=plan["recipe"]) + note if empty else note),
                **({"tests_run": own} if own is not None else {})})
        return result
    # Phase 46 (1.9.28): the project's own tests and the check alone, each in its own copy (CHECK_ALONE). The check's
    # copy is prepared first, so a check that cannot be added stops the call before anything runs.
    if (not pinned or not isinstance(check_run, dict) or set(check_run) != {"recipe", "plan", "mount", "reviewed_files"}
            or check_run["recipe"] not in CHECK_ALONE or not isinstance(check_run["plan"], dict)
            or check_run["plan"].get("recipe") != check_run["recipe"] or payload.get("check") is None):
        raise PolicyError("The reviewed check is malformed.")
    check = _check(payload, check_run["recipe"])
    recipes.shape(check_run["plan"])
    with _copy(ws, drafts, check_run["plan"], check_run["mount"], check_run["reviewed_files"], None, check,
               alone=True) as (probe, _, _):
        with _copy(ws, drafts, plan, mount, reviewed, bound) as (launch, manifest, mount):
            code, data, size = launch(cancel, offer=True)
        result, _ = _result(plan, code, data, size, manifest, pinned, mount)
        checked = {"path": check["path"], "sha256": check["sha256"], "recipe": check_run["recipe"],
                   "note": CHECK_RUN_NOTE.format(path=check["path"], recipe=check_run["recipe"])}
        try:
            probed, text = _result(check_run["plan"], *probe(cancel), {}, True, None)
        except PolicyError as exc:  # the project's run above stands; the check's own failure is reported beside it
            checked["error"] = str(exc)
        else:
            own = _own_tests(check_run["recipe"], True, check, probed["tests"], text)
            outcome = "no_tests" if own == 0 and probed["outcome"] == "passed" else probed["outcome"]
            checked.update(exit_code=probed["exit_code"], outcome=outcome, tests=probed["tests"],
                           output=_display(probed["output"], CHECK_SHOWN),
                           **({"tests_run": own} if own is not None else {}))
            if own == 0 and outcome == "no_tests":
                checked["note"] = CHECK_EMPTY.format(path=check["path"], recipe=check_run["recipe"]) + checked["note"]
    result["check"] = checked
    return result


def _result(plan, code, data, size, manifest, pinned, mount):
    """One launch's result as the broker records it, and the raw text it was classified from."""
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
    }, text


@contextlib.contextmanager
def _copy(ws, drafts, plan, mount, reviewed, bound, check=None, alone=False):
    """One isolated copy of the project (plus drafts), ready to launch: the reviewed manifests, the plan re-derived from
    the copy, the check written into it when given (alone: the recipe told to run only it). Yields (launch, manifest,
    mount); launch(cancel, offer) verifies the pinned identities immediately before it starts and returns (code, data,
    size). Phase 46 (1.9.28): one call may hold two copies, and nothing passes between them."""
    with tempfile.TemporaryDirectory(prefix="pebble-test-") as temp, tempfile.TemporaryDirectory(prefix="pebble-test-log-") as logs:
        temp = Path(temp).resolve()
        snapshot = temp / "project"
        snapshot.mkdir()
        file_hashes = {}
        manifest = _collect(ws, drafts, snapshot, file_hashes)
        # Phase 20 G20-recipes: the manifests and lockfiles that run are the ones reviewed (app.py records them).
        # X20 F20-03: per file, so one unbindable file (a lockfile over 2 MB, or a link) never voids the others' check.
        if isinstance(reviewed, dict):
            now = recipes.review_bindings(plan['recipe'], Workspace(snapshot))
            if any(value != 'unbindable' and now.get(name) != value for name, value in reviewed.items()):
                raise PolicyError('A project manifest or lockfile changed after review. Propose the recipe again.')
        elif isinstance(bound, dict) and recipes.bindings(plan['recipe'], Workspace(snapshot))[0] != bound:
            raise PolicyError('A project manifest or lockfile changed after review. Propose the recipe again.')
        mount = recipes.matches(plan, Workspace(snapshot), mount, ws.root)
        if check is not None:
            # Added after the plan was re-derived, so the reviewed plan is exactly the project's own.
            target = snapshot / check["path"]
            if target.exists() or target.is_symlink():
                raise PolicyError(f"The project already has {check['path']}, so the check could not be added. "
                                  "Nothing ran.")
            target.write_text(check["content"], encoding="utf-8")
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
        env = {**person_zone(), **env}  # Phase 46: the person's TZ, as their own test runs have it (zone.py)
        # Phase 46 (1.9.29): a test that runs /usr/bin's git, make or cc under a framework or Homebrew Python or Node
        # died with Xcode selected ("xcrun: error: unable to load libxcrun": the profile reads only the Command Line
        # Tools); the Command Line Tools, as RunCommand and a CLT recipe's own plan already have them
        from .terminal import developer_environment
        env = {**developer_environment(), **env}
        # Phase 46 (1.9.28): a check run alone names its module after the reviewed argv (the plan stays the recipe's own)
        argv = {**plan, "argv": plan["argv"] + CHECK_ALONE[plan["recipe"]](check["path"])} if alone else plan

        def launch(cancel, offer=False):
            recipes.verify(plan, ws.root)
            # The output log lives outside the sandbox-writable folder.
            code, data, size = _launch(argv, profile, snapshot, env, Path(logs) / "output.log", cancel)
            # Phase 20 G20-diag: hash-bound diagnostics for the host broker's open capture only; the result is
            # unchanged and offer never raises. A stopped launch raised above, so it records nothing.
            if offer:
                diagnostics.offer(plan, code, data, snapshot, file_hashes, ws)
            return code, data, size
        yield launch, manifest, mount
