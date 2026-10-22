"""Bounded terminal broker: argv execution, risk review and OS confinement."""

import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from .policy import Workspace, PolicyError
from .process_exit import LeaderWatch
from .terminal_text import sanitize as sanitize_terminal

BLOCKED = {
    "sudo",
    "su",
    "doas",
    "shutdown",
    "reboot",
    "halt",
    "diskutil",
    "fdisk",
    "mkfs",
    "dd",
    "launchctl",
    "osascript",
    "security",
    "reg",
    "powershell",
    "pwsh",
    "cmd",
    "bash",
    "sh",
    "zsh",
    "fish",
    "csh",
    "chmod",
    "chown",
    "chflags",
}
READ_ONLY = {
    "pwd",
    "ls",
    "rg",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "uname",
    "which",
    "echo",
}


class CommandOutcomeUncertain(RuntimeError):
    """An admitted process stopped before its external outcome was observed."""


def inspect_command(command, root, network=False):
    ws = Workspace(root)
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > 4000
        or type(network) is not bool
        or "\n" in command
        or "\r" in command
        or "\x00" in command
    ):
        raise PolicyError("Enter a single command shorter than 4000 characters.")
    if re.search(r"\$\(|`|\$\{|(?:^|\s)(?:[A-Za-z_][A-Za-z0-9_]*=)", command):
        raise PolicyError(
            "Command substitutions and environment overrides are blocked."
        )
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = list(lexer)
    except ValueError as exc:
        raise PolicyError("Check the command's quoting.") from exc
    if not argv or any(re.fullmatch(r"[;&|<>]+", a) for a in argv):
        raise PolicyError(
            "Run one command at a time. Pipes, redirections and chained shell commands are blocked."
        )
    executable = Path(argv[0]).name.casefold()
    if executable == 'git':
        raise PolicyError('Use the typed Git inspection tools and reviewed worktree controls. Raw Git commands are outside the generic terminal broker.')
    if (
        executable in BLOCKED
        or executable.startswith("mkfs")
        or executable in {"env", "xargs", "eval", "exec", "nohup"}
    ):
        raise PolicyError(
            "Privileged commands, shell wrappers and system administration are blocked. Run them yourself outside Pebble."
        )
    if re.search(
        r"(?:^|[/\\\s])(?:\.git|\.env(?:\.[\w-]+)?|\.ssh|\.aws|\.gnupg|\.netrc|\.npmrc|\.pypirc|\.pebble|credentials|secrets)(?:[/\\\s]|$)|\.(?:pem|key|p12|pfx)(?:\s|$)",
        command,
        re.I,
    ):
        raise PolicyError("Credential access is blocked.")
    from .data_home import tool_refused
    for arg in argv[1:]:
        if arg in {"/", str(Path.home()), "~", ".."} or arg.startswith("../"):
            raise PolicyError("Command paths must stay in the selected project.")
        # Phase 22 (s22 P5): ~/.pebble, PEBBLE_HOME and the old app-data folder are outside command access.
        if arg.startswith(("/", "~")) and tool_refused(arg):
            raise PolicyError("Pebble's own data folders are outside command access.")
    dangerous = (
        executable
        in {
            "rm",
            "rmdir",
            "mv",
            "install",
            "brew",
            "pip",
            "pip3",
            "npm",
            "npx",
            "yarn",
            "pnpm",
            "uv",
            "cargo",
            "python",
            "python3",
            "node",
            "ruby",
            "perl",
            "curl",
            "wget",
            "ssh",
            "scp",
            "docker",
            "make",
        }
        or executable == "git"
        and any(
            a
            in {
                "reset",
                "clean",
                "push",
                "checkout",
                "restore",
                "rebase",
                "commit",
                "apply",
                "clone",
                "pull",
                "fetch",
            }
            for a in argv[1:]
        )
        or executable == "find"
        and any(a in {"-delete", "-exec", "-execdir"} for a in argv)
    )
    readonly = (
        executable in READ_ONLY
        or executable == "git"
        and len(argv) > 1
        and argv[1] in {"status", "diff", "log", "show", "branch", "rev-parse"}
        and not any(
            a in {"-D", "-d", "--delete", "-c", "--exec", "--output"} for a in argv
        )
    )
    return {
        "root": str(ws.root),
        'root_identity': list(ws.identity),
        "command": command,
        "argv": argv,
        "network": network,
        "risk": "network"
        if network
        else "dangerous"
        if dangerous
        else "read-only"
        if readonly
        else "execution",
        "requires_review": network or dangerous or not readonly,
    }


def resolve_executable(name):
    path = Path(name)
    if path.is_absolute():
        if not path.is_file():
            raise PolicyError("Command executable does not exist.")
        return path.resolve()
    if "/" in name or "\\" in name:
        raise PolicyError(
            "Use a runtime to execute project scripts, e.g. python3 script.py."
        )
    if name in {"python", "python3"}:
        options = [
            Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"),
            Path("/usr/bin/python3"),
        ]
    elif name == "node":
        options = sorted(
            (Path.home() / ".nvm/versions/node").glob("*/bin/node"), reverse=True
        )
    else:
        options = [
            Path(p) / name
            for p in ("/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin")
        ]
    executable = next((p.resolve() for p in options if p.is_file()), None)
    if not executable:
        found = shutil.which(name)
        executable = Path(found).resolve() if found else None
    if not executable:
        raise PolicyError("This command is not installed on your device.")
    return executable


def run_command(payload, cancel):
    from .platform_providers import require as require_platform
    require_platform('command')
    checked = inspect_command(
        payload["command"], payload["root"], payload.get("network", False)
    )
    if checked["argv"] != payload["argv"]:
        raise PolicyError("Command changed after review.")
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise PolicyError(
            "Confined terminal execution currently requires macOS. Windows terminal remains disabled until its OS sandbox is available."
        )
    exe = resolve_executable(checked["argv"][0])
    plan = payload.get('plan')
    if plan is not None:
        from .command_plans import verify
        plan = verify(plan, payload['root'])  # re-pins the executable and root identity right before spawn
        step = plan['steps'][0]
        if (plan['pipeline'] != 'none' or plan['network']['mode'] != 'none' or plan['writable'] not in ([], ['.'])
                or bool(plan['writable']) != bool(payload.get('writable', False)) or step['argv'] != payload['argv']
                or plan['process_model'] != 'tree' or plan['interactive'] or step['cwd'] != '.' or step['env']):
            raise PolicyError('This command plan needs the contained plan worker. Nothing was run.')
        exe = Path(step['executable']['realpath'])  # the reviewed pin, not a fresh PATH lookup
    root = Path(checked["root"])
    if payload.get('root_identity') is not None and checked['root_identity'] != payload['root_identity']:
        raise PolicyError('The reviewed project folder was replaced before terminal execution.')
    quote = lambda p: json_quote(str(p))
    with tempfile.TemporaryDirectory(prefix="pebble-command-") as temp:
        temp = Path(temp).resolve()
        toolchain = exe.parent.parent
        if exe.is_relative_to("/usr/bin") or exe.is_relative_to("/bin"):
            toolchain = Path("/Library/Developer/CommandLineTools")
        elif "Python.framework" in str(exe):
            toolchain = Path("/Library/Frameworks/Python.framework")
        if toolchain == Path("/") or toolchain == Path.home():
            raise PolicyError("Cannot establish a confined command runtime.")
        # The unified Phase 14 profile: own-child signals, mach allowlist, exec
        # limited to system paths, the toolchain and the project, the private-path
        # deny and (legacy) open network only when this command was approved for it.
        from .sandbox_profiles import build
        profile = build(executable=exe, exec_paths=[toolchain, root], read_paths=[toolchain], project_root=root,
                        temp=temp, writable_paths=[root] if payload.get("writable", False) else (),
                        network="open" if checked["network"] else "none", job_marker=uuid.uuid4().hex,
                        process_model="tree")
        env = {
            "PATH": str(exe.parent) + ":/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(temp),
            "TMPDIR": str(temp),
            "LANG": "en_US.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        output = temp / "output.log"
        with output.open("wb") as log:
            process = subprocess.Popen(
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    profile,
                    str(exe),
                    *checked["argv"][1:],
                ],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 60
            # The group is signalled only while its leader is unreaped (a zombie
            # still reserves the PID), so the sweep can never reach a reused group.
            watch = LeaderWatch(process.pid)
            try:
                while not watch.exited(0.1 if watch.reliable else 0):
                    if (
                        cancel.is_set()
                        or time.monotonic() > deadline
                        or output.stat().st_size > 192000
                    ):
                        _sweep(process)
                        process.wait()
                        raise CommandOutcomeUncertain(
                            "Command stopped, timed out or exceeded the output limit after starting. Its outcome is uncertain; inspect it before retrying."
                        )
                    if not watch.reliable:
                        if process.poll() is not None:
                            break
                        time.sleep(0.1)
                # Descendants left in the leader's group must not outlive the command.
                _sweep(process)
                process.wait()
                raw = output.read_bytes()[:16000]
                shown, removed, _ = sanitize_terminal(raw, final=True)
                return {
                    "exit_code": process.returncode,
                    # The model and UI get the terminal_text projection; the raw prefix is attested by hash.
                    "output": shown,
                    "output_projection": "terminal_text/1",
                    "controls_removed": removed,
                    "raw_output_sha256": hashlib.sha256(raw).hexdigest(),
                    "command": checked["command"],
                    "isolation": "macOS sandbox · selected project · "
                    + ("network approved" if checked["network"] else "network denied"),
                }
            finally:
                watch.close()
                if process.returncode is None:
                    _sweep(process)
                    process.wait()


def _sweep(process):
    """SIGKILL the command's group; only ever called before the leader is reaped."""
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def json_quote(value):
    # SBPL string syntax, not shell interpolation.
    import json

    return json.dumps(value)
