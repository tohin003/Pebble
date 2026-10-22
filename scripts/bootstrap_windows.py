"""Pinned Windows CPU runtime setup. Model is selected through desktop Settings."""

import hashlib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "llama-b11499-bin-win-cpu-x64.zip"
SHA = "5e4b71e4f48780b354d2c30668c2b4a1b6d21b441e969a18b5028b886179dfa8"
dest = ROOT / ".runtime/b11499/windows"
dest.mkdir(parents=True, exist_ok=True)
archive = dest / NAME
if not archive.exists():
    urllib.request.urlretrieve(
        "https://github.com/ggml-org/llama.cpp/releases/download/b11499/" + NAME,
        archive,
    )
if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA:
    raise ValueError("Runtime checksum does not match the pinned release.")
with zipfile.ZipFile(archive) as z:
    for member in z.infolist():
        if member.is_dir():
            continue
        name = Path(member.filename).name
        if name == "llama-server.exe" or name.endswith(".dll"):
            (dest / name).write_bytes(z.read(member))
print(dest)
