"""Throwaway cell folders: /private/tmp/pebble-bench-<plan_sha256[:12]>/<product>/<task>-<rep>/workspace.

Every cell starts from the same fixture bytes in a fresh folder. No instruction file (AGENTS.md, CLAUDE.md, GEMINI.md,
PEBBLE.md) may exist in the workspace or any folder above it, since Codex CLI and Pebble read such files from the
project and its parents. Inventories are bounded and kept in memory for the graders only.
"""

import os
import stat
from pathlib import Path

TMP = Path('/private/tmp')
PREFIX = 'pebble-bench-'
INSTRUCTION_FILES = ('AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md', 'GEMINI.md', 'PEBBLE.md')
INVENTORY_LIMIT, FILE_LIMIT = 2000, 2_000_000


class SandboxRefused(RuntimeError):
    """A cell folder outside its sandbox, or an instruction file in reach."""


def plan_root(plan_sha256, parent=TMP):
    """The plan's scratch root; its name always carries the plan, and it always resolves under /private/tmp."""
    root = Path(parent).resolve() / (PREFIX + plan_sha256[:12])
    if not root.is_relative_to(TMP.resolve()):
        raise SandboxRefused('scratch_outside_private_tmp')
    return root


def cell_dir(root, product, task_id, rep):
    root = Path(root)
    if not root.name.startswith(PREFIX) or not root.resolve().is_relative_to(TMP.resolve()):
        raise SandboxRefused('scratch_root_invalid')
    folder = root / product / ('%s-%d' % (task_id, rep))
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.mkdir(mode=0o700)  # never reused: one attempt per cell
    except FileExistsError:
        raise SandboxRefused('cell_folder_exists') from None
    return folder


def materialize(workspace, files):
    workspace.mkdir(mode=0o700)
    for name, text in files:
        target = workspace / name
        if not target.resolve().is_relative_to(workspace.resolve()):
            raise SandboxRefused('fixture_path_escapes')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode('utf-8') if isinstance(text, str) else bytes(text))
    instruction_files_absent(workspace)
    return workspace.resolve()


def instruction_files_absent(workspace):
    """Refuse when any instruction file is in the workspace (any depth) or in a folder above it."""
    workspace = Path(workspace).resolve()
    for base, dirs, files in os.walk(workspace):
        hits = [f for f in files if f in INSTRUCTION_FILES]
        if hits:
            raise SandboxRefused('instruction_file_in_workspace:' + hits[0])
    for parent in workspace.parents:
        for name in INSTRUCTION_FILES:
            if (parent / name).exists():
                raise SandboxRefused('instruction_file_above_workspace:' + name)
    return True


def inventory(root, limit=INVENTORY_LIMIT):
    """{relative path: bytes} of regular files (symlinks never followed; large files as None)."""
    found = {}
    if root is None or not Path(root).is_dir():
        return found
    root = Path(root)
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                continue
            found[path.relative_to(root).as_posix()] = path.read_bytes() if info.st_size <= FILE_LIMIT else None
            if len(found) >= limit:
                return found
    return found
