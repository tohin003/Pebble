"""Phase 46 (D154, D164): the files a run will most likely read, read by the host before its first model request.

Measured first (golden dev split, gpt-6.1-sol and Opus): a run's first requests read the files its request names or, in
a follow-up turn, the files the conversation already changed (on sol about 5 s a request over the WebSocket, and a
follow-up turn of dev d-lh-91 read the project again every time: "Recorded action results" carries what earlier turns
did, not what they read); Codex's own first step on dev d-lh-01 read every file of the small project at once.

`choose` picks, bounded, the files the request names that exist in the project, the files earlier runs of the
conversation changed or read, and, in a tiny project, all of its text files. The runner reads them through App.execute (the same
policy, receipts, untrusted wrapping and taint registration as a model's own Read) as the run's first exchange, above 8K
only. Nothing that looks like a secret or local configuration is ever chosen, and a file the policy refuses is skipped.
"""

import os
from pathlib import Path

from .requirements import CODE, NEVER_GENERATED, PATH_TOKEN, _path

MAX_FILES = 40            # files prefetched for one run (the byte bounds below are what costs)
FILE_BYTES = 24_000       # a larger file is left to the model, which may read the part it needs
TOTAL_BYTES = 60_000      # all prefetched files together (and at most the window's budget, counted in characters)
TINY_FILES = 40           # a project of at most this many text files ... (dev d-lh-01: 23 files, 8 KB)
TINY_BYTES = 24_000       # ... and this many bytes in all is read whole
EARLIER_FILES = 12        # files earlier turns changed, newest first
WALK_LIMIT = 400          # entries looked at when deciding whether a project is tiny
# A tiny project is read whole only for its code and configuration (named files of any text kind still come):
# documents and data stay where they are unless the request names them, since they may hold decoys or injected text the
# model should meet only when it chooses to read them (dev d-rf-02 on Opus: with every saved page prefetched, the reply
# repeated a rumoured figure while explaining why it was not official).
TINY_TYPES = frozenset(CODE) | {'.toml', '.yaml', '.yml', '.ini', '.cfg'}
SKIP_PARTS = frozenset({'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.tox', '.mypy_cache', '.pytest_cache',
                        '.ruff_cache', 'dist', 'build', 'htmlcov', '.idea', '.vscode'})


def _readable(root, rel, limit=FILE_BYTES):
    """The file's size when it is a project text file worth prefetching (UTF-8, no NUL, not empty, within limit)."""
    path = Path(root) / rel
    try:
        resolved = path.resolve()
        if path.is_symlink() or not path.is_file() or Path(root).resolve() not in resolved.parents:
            return None
        size = path.stat().st_size
        if not 0 < size <= limit:
            return None
        data = path.read_bytes()
        if b'\0' in data:
            return None
        data.decode('utf-8')
        return size
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _allowed(rel):
    parts = rel.split('/')
    return (bool(rel) and not rel.startswith('/') and '..' not in parts and not NEVER_GENERATED.search(rel)
            and not any(part in SKIP_PARTS for part in parts[:-1]))


def named_files(root, text):
    """Project files the request names (`app/parse.py`, tests/test_x.py), in the order it names them."""
    out = []
    for match in PATH_TOKEN.finditer(text if isinstance(text, str) else ''):
        rel = _path(match.group(0).rstrip('.,;:'))
        if rel and _allowed(rel) and rel not in out:
            out.append(rel)
    return out


def tiny_project(root):
    """Every text file of a tiny project (TINY_FILES, TINY_BYTES), sorted; [] for anything larger."""
    found, total, seen = [], 0, 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS and not d.startswith('.')
                         and not os.path.islink(os.path.join(folder, d)))
        rel_folder = Path(folder).relative_to(root).as_posix()
        for name in sorted(files):
            seen += 1
            if seen > WALK_LIMIT or name.startswith('.'):
                if seen > WALK_LIMIT:
                    return []
                continue
            rel = name if rel_folder == '.' else rel_folder + '/' + name
            if not _allowed(rel) or Path(name).suffix.lower() not in TINY_TYPES:
                continue
            size = _readable(root, rel, TINY_BYTES)
            if size is None:
                continue
            found.append(rel)
            total += size
            if len(found) > TINY_FILES or total > TINY_BYTES:
                return []
    return sorted(found)


def choose(root, text, earlier=(), budget=32768):
    """The files to prefetch for a run: named in the request, changed or read earlier in the conversation (newest
    first), and in a tiny project all of them; each a readable text file within FILE_BYTES, together within TOTAL_BYTES
    and the window's budget (in characters), at most MAX_FILES."""
    candidates = []
    for rel in [*named_files(root, text), *earlier, *tiny_project(root)]:
        if isinstance(rel, str) and _allowed(rel) and rel not in candidates:
            candidates.append(rel)
    chosen, total, limit = [], 0, min(TOTAL_BYTES, max(0, int(budget)))
    for rel in candidates:
        size = _readable(root, rel)
        if size is None or total + size > limit:
            continue
        chosen.append(rel)
        total += size
        if len(chosen) >= MAX_FILES:
            break
    return chosen


def earlier_reads(tool_lists, limit=EARLIER_FILES):
    """Project paths that earlier runs of the conversation read (completed Reads, the host's own included), newest run
    first and within a run its last read first. Phase 46 (dev d-wr-01 on sol: every follow-up turn spent a request
    reading ibans/lengths.txt again, which turn 1 had read; "Recorded action results" carry what earlier turns did, not
    what they read)."""
    out = []
    for tools in tool_lists:
        for tool in reversed(list(tools or ())):
            arguments = tool.get('arguments') if isinstance(tool.get('arguments'), dict) else {}
            path = arguments.get('file_path')
            if tool.get('name') == 'Read' and tool.get('state') == 'completed' and isinstance(path, str) and path not in out:
                out.append(path)
                if len(out) >= limit:
                    return out
    return out


def earlier_edits(actions, limit=EARLIER_FILES):
    """Project paths that applied edits of the conversation changed, newest first (the chat's broker actions)."""
    out = []
    for action in reversed(list(actions or ())):
        payload = action.get('payload') if isinstance(action.get('payload'), dict) else {}
        path = payload.get('path')
        if action.get('kind') == 'edit' and action.get('status') == 'applied' and isinstance(path, str) and path not in out:
            out.append(path)
            if len(out) >= limit:
                break
    return out
