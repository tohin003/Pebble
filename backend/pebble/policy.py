"""Deterministic authority boundary. Deliberately independent of AgentGuard/LLM."""

import difflib
import hashlib
import os
import stat
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

PRIVATE = {
    ".env",
    ".ssh",
    ".aws",
    ".gnupg",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git",
    ".agentguard",
    ".pebble",
    "credentials",
    "secrets",
}
EXCLUDED = PRIVATE | {
    "node_modules",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".runtime",
    "models",
}
MAX_FILE = 96_000


class PolicyError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


MAGIC = ((b'\x1f\x8b', 'gzip-compressed data'), (b'PK\x03\x04', 'a ZIP archive'), (b'%PDF', 'a PDF document'),
         (b'\x89PNG', 'a PNG image'), (b'\xff\xd8\xff', 'a JPEG image'), (b'GIF8', 'a GIF image'),
         (b'SQLite format 3\x00', 'an SQLite database'), (b'BZh', 'bzip2-compressed data'),
         (b'\xfd7zXZ\x00', 'xz-compressed data'), (b'\x7fELF', 'an ELF executable'),
         (b'\xcf\xfa\xed\xfe', 'a Mach-O executable'))


def binary_note(name, data):
    """Why Read shows no text for a file that is not UTF-8, and how to look at it instead."""
    kind = next((label for magic, label in MAGIC if data.startswith(magic)), None)
    if kind is None and data[257:262] == b'ustar':
        kind = 'a tar archive'
    return (f'{name} is not UTF-8 text ({len(data):,} bytes' + (f', starting like {kind}' if kind else '') + '), so Read '
            f'cannot show it. Look at it with a read-only command such as `file {name}`, or `xxd -l 256 {name}` for its '
            'first bytes.')


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve(strict=True)
        if (
            not self.root.is_dir()
            or self.root == Path.home()
            or self.root == Path(self.root.anchor)
        ):
            raise PolicyError(
                "Choose a project folder rather than your home folder or disk root."
            )
        # Phase 22 (s22 P4, narrowed at merge): Pebble's own data folders are outside tool access, except the trees
        # Pebble's reviewed features keep there (managed worktrees, environments, the person's skill copies).
        from .data_home import project_folder
        # f22 follow-up: tool_refused and protected_below in one pass; self.root is resolved above (strict), so data_home
        # does not resolve it twice more.
        refused, self._protected = project_folder(self.root, resolved=True)
        if refused:
            # Phase 24 (V1-003, t24-data): the Documents copies are refused too; the prefix graders match is unchanged.
            raise PolicyError("Pebble's own data folders (~/.pebble, its old app-data folder and your Documents copies) "
                              "are outside tool access.")
        self.drafts = {}
        root_stat = self.root.stat()
        self.identity = (root_stat.st_dev, root_stat.st_ino)

    def path(self, name, create=False):
        if not isinstance(name, str) or not name or len(name) > 512:
            raise PolicyError("A relative project file path is required.")
        rel = Path(name)
        if rel.is_absolute():
            # Phase 45 (F067, audit R2): the model named a file inside the project by its absolute path and was refused
            # ("Paths must stay inside the selected project" for a file that was). Inside the root it becomes the same
            # relative path; every check below still applies to it, symlinks included.
            raw, root = os.path.normpath(name), str(self.root).rstrip("/")
            if not raw.startswith(root + "/"):
                raise PolicyError("Paths must stay inside the selected project.")
            rel = Path(raw[len(root) + 1:])
        if rel.is_absolute() or ".." in rel.parts:
            raise PolicyError("Paths must stay inside the selected project.")
        for part in rel.parts:
            lower = part.lower()
            if (
                lower in PRIVATE
                or lower.startswith(".env")
                or lower.endswith((".pem", ".key", ".p12", ".pfx", ".gguf"))
            ):
                raise PolicyError(
                    "Private credentials and model weights are outside tool access."
                )
        target = self.root / rel
        # Reject every symlink, including in parent components; never follow links in tools.
        cursor = self.root
        for part in rel.parts:
            cursor = cursor / part
            if cursor.is_symlink() or cursor.is_junction():
                raise PolicyError("Symbolic links are outside project tool access.")
        resolved = target.resolve(strict=not create)
        if not resolved.is_relative_to(self.root):
            raise PolicyError("Path escapes the selected project.")
        protected = getattr(self, '_protected', None)
        if protected:  # only a project that holds one of Pebble's data folders pays for this check
            from .data_home import inside_any
            if inside_any(resolved, protected):
                raise PolicyError("Pebble's own data folders (~/.pebble, its old app-data folder and your Documents copies) "
                                  "are outside tool access.")
        if resolved.exists() and not resolved.is_file():
            raise PolicyError("Select a regular file.")
        return target

    def read(self, name):
        self.path(name, create=True)
        name = Path(name).as_posix()
        if name in self.drafts:
            current = self.snapshot(name, missing_ok=True)
            if (digest(current) if current is not None else None) == self.drafts[name]['baseline']:
                return self.drafts[name]["after"]
            # The old staged view no longer describes its baseline. Keep the
            # broker proposal for review, but return the current disk view.
            self.drafts.pop(name)
        return self.snapshot(name).decode('utf-8')

    def snapshot(self, name, missing_ok=False):
        """Read one bounded file through anchored no-follow descriptors.

        Platforms without directory-relative opens retain checked-path reads;
        their race-resistant native file provider remains a platform gate.
        """
        target = self.path(name, create=missing_ok)
        descriptors = []
        file_fd = None
        try:
            if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                root_fd = os.open(self.root, flags)
                descriptors.append(root_fd)
                root_stat = os.fstat(root_fd)
                if (root_stat.st_dev, root_stat.st_ino) != self.identity:
                    raise PolicyError('The selected workspace was replaced. Start a fresh task.')
                relative = target.relative_to(self.root)
                parent_fd = root_fd
                for part in relative.parts[:-1]:
                    parent_fd = os.open(part, flags, dir_fd=parent_fd)
                    descriptors.append(parent_fd)
                file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent_fd)
            else:
                file_fd = os.open(target, os.O_RDONLY)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
                raise PolicyError('File exceeds the 96 KB source limit or is not a regular file.')
            with os.fdopen(file_fd, 'rb') as source:
                file_fd = None
                data = source.read(MAX_FILE + 1)
                after = os.fstat(source.fileno())
            if len(data) > MAX_FILE or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise PolicyError('File changed during the read. Request a fresh source view.')
            return data
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for fd in reversed(descriptors):
                os.close(fd)

    def files(self):
        return self.inventory()['files']

    def inventory(self, limit=1500):
        if type(limit) is not int or not 1 <= limit <= 1500:
            raise ValueError('Choose an inventory limit from 1 to 1500.')
        found = []
        errors = []
        for base, dirs, files in os.walk(self.root, followlinks=False, onerror=lambda error: errors.append(type(error).__name__)):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in EXCLUDED
                and not d.startswith(".")
                and not (Path(base) / d).is_symlink()
            )
            for file in sorted(files):
                name = str((Path(base) / file).relative_to(self.root))
                try:
                    self.path(name)
                    if len(found) == limit:
                        return {'files': found, 'truncated': True, 'limit': limit, 'errors': len(errors)}
                    found.append(name)
                except (PolicyError, OSError):
                    pass
        return {'files': found, 'truncated': False, 'limit': limit, 'errors': len(errors)}

    def draft(self, name, content):
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise PolicyError("Draft exceeds the source file limit.")
        self.path(name, create=True)
        name = Path(name).as_posix()
        path = self.path(name, create=True)
        if path.exists() and path.stat().st_size > MAX_FILE:
            raise PolicyError("File exceeds the source file limit.")
        previous = self.drafts.get(name)
        snapshot = self.snapshot(name, missing_ok=True)
        before = previous["before"] if previous else snapshot.decode('utf-8') if snapshot is not None else ''
        baseline = (
            previous["baseline"]
            if previous
            else (digest(snapshot) if snapshot is not None else None)
        )
        data = {
            "path": name,
            "before": before,
            "after": content,
            "baseline": baseline,
            "root": str(self.root),
            "root_identity": list(self.identity),
            "diff": "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    content.splitlines(True),
                    fromfile=f"a/{name}",
                    tofile=f"b/{name}",
                )
            ),
        }
        self.drafts[name] = data
        return data

    @staticmethod
    def apply(payload):
        ws = Workspace(payload["root"])
        if payload.get('root_identity') is not None and payload['root_identity'] != list(ws.identity):
            raise PolicyError('The selected workspace was replaced after this draft. Read the new workspace before preparing another edit.')
        path = ws.path(payload["path"], create=True)
        snapshot = ws.snapshot(payload['path'], missing_ok=True)
        current = digest(snapshot) if snapshot is not None else None
        if current != payload["baseline"]:
            raise PolicyError(
                "This file changed since the draft. Ask Pebble to read it again and prepare a fresh edit."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same-directory replacement, keeping executable bits, with baseline checked again.
        fd, tmp = tempfile.mkstemp(prefix=".pebble-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload["after"].encode('utf-8'))
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
            ws.path(payload["path"], create=True)
            snapshot = ws.snapshot(payload['path'], missing_ok=True)
            if (digest(snapshot) if snapshot is not None else None) != current:
                raise PolicyError("File changed during approval. No edit was applied.")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"applied": payload["path"], "hash": digest(ws.snapshot(payload['path']))}


def installed_apps():
    result = []
    if sys.platform == "darwin":
        for root in (
            Path("/Applications"),
            Path("/System/Applications"),
            Path("/System/Applications/Utilities"),
            Path("/System/Library/CoreServices"),
            Path("/System/Library/CoreServices/Applications"),
            Path.home() / "Applications",
        ):
            if not root.exists():
                continue
            for path in sorted(root.glob("*.app")):
                if (
                    root == Path("/System/Library/CoreServices")
                    and path.stem != "Finder"
                ):
                    continue
                try:
                    info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
                    if info.get("LSBackgroundOnly"):
                        continue
                    result.append(
                        {
                            "id": str(path),
                            "name": info.get("CFBundleDisplayName") or path.stem,
                        }
                    )
                except (OSError, ValueError):
                    continue
    elif sys.platform == "win32":
        roots = [
            Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
            / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs",
        ]
        for root in roots:
            for path in root.rglob("*.lnk"):
                result.append({"id": str(path), "name": path.stem})
    return sorted(
        {r["id"]: r for r in result}.values(), key=lambda r: r["name"].lower()
    )


# Phase 45 (F259): the installed apps' display names, read at most every APP_NAMES_SECONDS (a request names an app)
APP_NAMES_SECONDS = 600.0
_app_names = {"at": None, "names": ()}


OWN_NAMES = frozenset({'Pebble'})  # Pebble never opens itself (and "my Pebble test inbox" names no app to use)


def installed_app_names(clock=None):
    """The installed apps' display names of 4 or more characters, longest first, Pebble's own left out; read again
    after APP_NAMES_SECONDS."""
    import time
    now = (clock or time.monotonic)()
    if _app_names["at"] is None or now - _app_names["at"] > APP_NAMES_SECONDS:
        try:
            names = {a["name"] for a in installed_apps() if len(a["name"]) >= 4 and a["name"] not in OWN_NAMES}
        except (OSError, KeyError):
            names = set()
        _app_names.update(at=now, names=tuple(sorted(names, key=lambda n: (-len(n), n))))
    return _app_names["names"]


def names_installed_app(text, names=None):
    """The first installed app the text names by its exact display name (as written: "Use Calculator to ..."), as a
    whole word or words, or None."""
    import re
    text = text or ""
    for name in installed_app_names() if names is None else names:
        if name in text and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", text):
            return name
    return None


def launch_app(app_id):
    known = {a["id"] for a in installed_apps()}
    if app_id not in known:
        raise PolicyError("App is not in the installed app inventory.")
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", "-a", app_id], check=True, timeout=10)
    elif sys.platform == "win32":
        os.startfile(app_id)
    else:
        raise PolicyError("App launching is supported on macOS and Windows.")
    return {"opened": Path(app_id).stem}
