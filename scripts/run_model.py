"""Run the model on loopback with one worker and a private local API key."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def server_command() -> list[str]:
    config = json.loads((ROOT / "config/model.json").read_text())
    candidates = list((ROOT / ".runtime" / config["runtime"]["release"]).rglob("llama-server"))
    if len(candidates) != 1:
        raise SystemExit("Run python3 scripts/bootstrap_model.py first.")
    model = ROOT / "models" / config["filename"]
    if not model.is_file():
        raise SystemExit("The configured model file is missing; run bootstrap_model.py.")
    key_file = ROOT / ".runtime/server.key"
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    key_file.chmod(0o600)
    settings = config["serving"]
    return [str(candidates[0]), "--model", str(model), "--alias", "buddy-local",
            "--host", settings["host"], "--port", str(settings["port"]),
            "--ctx-size", str(settings["context_tokens"]),
            "--parallel", str(settings["parallel_requests"]),
            "--threads", str(settings["threads"]),
            "--batch-size", str(settings["batch_size"]),
            "--ubatch-size", str(settings["micro_batch_size"]),
            "--n-gpu-layers", "all", "--flash-attn", "on", "--jinja",
            "--no-webui", "--reasoning-budget", "512",
            "--api-key-file", str(key_file)]


if __name__ == "__main__":
    raise SystemExit(subprocess.call(server_command(), cwd=ROOT))
