"""Stream a local response. This client gives the model no execution tools."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from run_model import ROOT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args()
    if not 1 <= args.max_tokens <= 1536:
        parser.error("--max-tokens must be between 1 and 1536 for the 4K profile.")
    config = json.loads((ROOT / "config/model.json").read_text())
    settings = config["serving"]
    key_file = ROOT / ".runtime/server.key"
    if not key_file.is_file():
        raise SystemExit("Start python3 scripts/run_model.py first.")
    body = {"model": "buddy-local", "stream": True,
            "messages": [{"role": "system", "content": "You are a concise personal "
                "engineering assistant. Be accurate about uncertainty and tool availability."},
                {"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens, **config["sampling"],
            "chat_template_kwargs": {"enable_thinking": args.thinking}}
    query = urllib.request.Request(
        f"http://{settings['host']}:{settings['port']}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
        "Authorization": "Bearer " + key_file.read_text().strip()})
    try:
        with urllib.request.urlopen(query, timeout=180) as stream:
            for line in stream:
                if not line.startswith(b"data: "):
                    continue
                data = line[6:].strip()
                if data == b"[DONE]":
                    break
                choice = json.loads(data).get("choices", [])
                if choice:
                    content = choice[0].get("delta", {}).get("content")
                    if content:
                        print(content, end="", flush=True)
        print()
    except urllib.error.URLError as error:
        raise SystemExit(f"Local model request failed: {error.reason}")


if __name__ == "__main__":
    main()
