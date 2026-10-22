"""Install one pinned GGUF and a checksum-verified native llama.cpp runtime.

Uses the system curl (/usr/bin/curl) and its trust store; no Python packages or administrator access.
Only writes inside this project. Does not upload anything or modify Ollama.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, path: Path, size: int, expected: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size == size and checksum(path) == expected:
            print(f"Verified existing {path.name}", flush=True)
            return
        raise SystemExit(f"Existing file failed verification: {path}")
    partial = path.with_suffix(path.suffix + ".part")
    remaining = max(0, size - (partial.stat().st_size if partial.exists() else 0))
    if shutil.disk_usage(path.parent).free < remaining + 700_000_000:
        raise SystemExit("Insufficient free disk space for download plus 700 MB reserve.")
    print(f"Downloading {path.name} ({size / 1e9:.2f} GB)", flush=True)
    log_path = ROOT / ".runtime" / "download.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        process = subprocess.Popen(
            # V1-133 (bandit B607): the system curl by its absolute path, never one found through PATH.
            ["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
             "--retry", "3", "--retry-delay", "2", "--connect-timeout", "20",
             "--max-time", "1200", "--continue-at", "-", "--output", str(partial), url],
            stdout=log, stderr=log,
        )
        previous = -1
        try:
            while process.poll() is None:
                current = partial.stat().st_size if partial.exists() else 0
                percent = min(100, int(current * 100 / size))
                if percent // 10 > previous:
                    print(f"  {percent}% ({current / 1e9:.2f} GB)", flush=True)
                    previous = percent // 10
                time.sleep(2)
        except BaseException:
            process.terminate()
            process.wait(timeout=10)
            raise
    if process.returncode:
        raise SystemExit(f"Download failed; resumable partial retained. See {log_path}")
    print("Checking SHA-256...", flush=True)
    if partial.stat().st_size != size or checksum(partial) != expected:
        raise SystemExit(f"Checksum/size mismatch: {partial}. File will not be used.")
    partial.replace(path)
    print(f"Verified {path.name}", flush=True)


def main() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This bootstrap installs the macOS ARM64 runtime only.")
    config = json.loads((ROOT / "config/model.json").read_text())
    runtime = config["runtime"]
    archive = ROOT / ".runtime" / runtime["asset"]
    download(
        f"https://github.com/{runtime['project']}/releases/download/{runtime['release']}/{runtime['asset']}",
        archive, runtime["size_bytes"], runtime["sha256"],
    )
    target = ROOT / ".runtime" / runtime["release"]
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(target, filter="data")
    archive.unlink()
    url = (f"https://huggingface.co/{config['repository']}/resolve/"
           f"{config['revision']}/{config['filename']}")
    download(url, ROOT / "models" / config["filename"], config["size_bytes"], config["sha256"])
    print("Native runtime and quantized model are ready.", flush=True)


if __name__ == "__main__":
    main()
