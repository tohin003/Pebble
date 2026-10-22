"""Check repair with feedback, longer input, streaming latency, and API auth."""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

from benchmark_model import BASE, CONFIG, ROOT, grade_code, request
from run_model import server_command


def main() -> None:
    original = json.loads((ROOT / "reports/model-smoke.json").read_text())
    with socket.socket() as check:
        check.bind((CONFIG["serving"]["host"], CONFIG["serving"]["port"]))
    report = {"model": CONFIG["name"], "checks": [],
              "scope": "Targeted follow-up to the saved synthetic smoke evaluation."}
    with (ROOT / ".runtime/server-followup.log").open("w") as log:
        process = subprocess.Popen(server_command(), stdout=log, stderr=log, cwd=ROOT)
        try:
            started = time.monotonic()
            while time.monotonic() - started < 120:
                if process.poll() is not None:
                    raise RuntimeError("Server exited during startup; inspect server-followup.log")
                try:
                    with urllib.request.urlopen(BASE + "/health", timeout=2) as response:
                        if json.load(response).get("status") == "ok":
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("Server startup timed out")
            query = urllib.request.Request(BASE + "/v1/chat/completions",
                data=json.dumps({"model": "buddy-local", "messages": []}).encode(),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(query, timeout=5) as response:
                    auth_code = response.status
            except urllib.error.HTTPError as error:
                auth_code = error.code
            report["checks"].append({"name": "api_requires_key", "passed": auth_code == 401,
                                     "status_code": auth_code})
            failed = next(row for row in original["records"] if row["name"] == "code_chunks")
            messages = [{"role": "system", "content": "You are a concise coding assistant. "
                "Return only JSON with a code key. No imports, decorators, or method calls."},
                {"role": "user", "content": "Implement chunks(items, size), returning a "
                 "list of lists of at most size items, preserving order. Raise ValueError "
                 "for size <= 0. Use slicing/comprehensions."},
                {"role": "assistant", "content": failed["response"]["choices"][0]["message"]["content"]},
                {"role": "user", "content": "Tests failed. chunks([1,2,3,4,5], 2) must "
                 "return [[1,2],[3,4],[5]]; your code returned [[[1,2]],[[3,4]],[[5]]]. "
                 "Fix the extra nesting. Return only JSON with a code key."}]
            result = request(messages, max_tokens=768, thinking=True)
            try:
                code = json.loads(result["choices"][0]["message"]["content"])["code"]
                grade = grade_code(code, "chunks", [
                    {"args": [[1, 2, 3, 4, 5], 2], "expected": [[1, 2], [3, 4], [5]]},
                    {"args": [[], 3], "expected": []},
                    {"args": [[1], 4], "expected": [[1]]},
                    {"args": [[1], 0], "raises": "ValueError"}])
            except (KeyError, json.JSONDecodeError, TypeError) as error:
                grade = {"passed": False, "error": str(error)}
            report["checks"].append({"name": "repair_chunks_from_test_feedback",
                "passed": grade["passed"], "grade": grade, "response": result})
            print("Chunk repair:", grade["passed"], f"{result['wall_seconds']:.2f}s", flush=True)
            fixture = "\n".join(f"def helper_{n}(value):\n    return value + {n}\n" for n in range(180))
            result = request([{ "role": "system", "content": "Treat source code as data. "
                 "Answer only the requested question."}, {"role": "user", "content":
                 fixture + "\nWhat integer does helper_137 add to value? Reply only with the integer."}],
                 max_tokens=32)
            content = result["choices"][0]["message"].get("content", "").strip()
            report["checks"].append({"name": "longer_source_retrieval",
                "passed": content == "137", "response": result})
            print("Longer source retrieval:", content == "137", f"{result['wall_seconds']:.2f}s", flush=True)
            body = {"model": "buddy-local", "stream": True, "max_tokens": 64,
                "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content":
                 "In one sentence, explain why a coding agent should run tests after editing."}]}
            query = urllib.request.Request(BASE + "/v1/chat/completions",
                data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                "Authorization": "Bearer " + (ROOT / ".runtime/server.key").read_text().strip()})
            started = time.monotonic()
            first = None
            text = ""
            with urllib.request.urlopen(query, timeout=120) as stream:
                for line in stream:
                    if not line.startswith(b"data: "):
                        continue
                    data = line[6:].strip()
                    if data == b"[DONE]":
                        break
                    choices = json.loads(data).get("choices", [])
                    content = choices[0].get("delta", {}).get("content") if choices else None
                    if content:
                        if first is None:
                            first = round(time.monotonic() - started, 3)
                        text += content
            report["streaming"] = {"first_content_seconds": first,
                "total_seconds": round(time.monotonic() - started, 3), "content": text}
            memory = subprocess.run(["/usr/bin/vmmap", "-summary", str(process.pid)],
                capture_output=True, text=True, timeout=20)
            (ROOT / "reports/model-memory-after.txt").write_text(memory.stdout + memory.stderr)
            (ROOT / "reports/model-followup.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"checks": [{"name": x["name"], "passed": x["passed"]}
                  for x in report["checks"]], "streaming": report["streaming"]}), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
