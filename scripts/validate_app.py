"""Real packaged service + model + AgentGuard + draft/apply/test smoke."""

import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {"checks": {}, "events": []}
    with tempfile.TemporaryDirectory(prefix="pebble-e2e-") as temp:
        home = Path(temp) / "data"
        project = Path(temp) / "project"
        project.mkdir()
        (project / "maths.py").write_text(
            'def clamp(x, lo, hi):\n    """Clamp a value to inclusive bounds."""\n    return x\n'
        )
        (project / "test_maths.py").write_text(
            "import unittest\nfrom maths import clamp\nclass ClampTests(unittest.TestCase):\n def test_above(self): self.assertEqual(clamp(9,0,5),5)\n def test_below(self): self.assertEqual(clamp(-2,0,5),0)\n def test_inside(self): self.assertEqual(clamp(3,0,5),3)\n"
        )
        exe = ROOT / "src-tauri/resources/backend/pebble-backend"
        process = subprocess.Popen(
            [
                str(exe),
                "--home",
                str(home),
                "--resources",
                str(ROOT / "src-tauri/resources"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lines = queue.Queue()

        def reader():
            for line in process.stdout:
                try:
                    lines.put(json.loads(line))
                except ValueError:
                    print("Invalid protocol", line[:100], flush=True)

        threading.Thread(target=reader, daemon=True).start()
        seq = 0

        def receive(timeout=180):
            item = lines.get(timeout=timeout)
            if "event" in item:
                if item["event"] != "token":
                    report["events"].append(item)
                    if item["event"] in ("error", "progress"):
                        print(item, flush=True)
            return item

        def rpc(method, **params):
            nonlocal seq
            seq += 1
            id = str(seq)
            process.stdin.write(
                json.dumps({"id": id, "method": method, "params": params}) + "\n"
            )
            process.stdin.flush()
            while True:
                item = receive()
                if item.get("id") == id:
                    if "error" in item:
                        raise RuntimeError(item["error"])
                    return item["result"]

        def task(cid, text):
            start = time.monotonic()
            rpc("send", id=cid, text=text)
            while True:
                item = receive()
                if item.get("event") == "error":
                    raise RuntimeError(item["data"]["message"])
                if item.get("event") == "idle":
                    break
            return rpc("get_chat", id=cid), round(time.monotonic() - start, 2)

        try:
            state = rpc("state")
            assert state["guard"]["available"], state["guard"]
            report["checks"]["packaged_guard"] = True
            rpc("set_model", path=str(ROOT / "models/FrogNano-4B-2609-Q4_K_M.gguf"))
            c = rpc("new_chat", workspace=str(project))
            c, elapsed = task(
                c["id"],
                "Read maths.py and test_maths.py. Fix clamp so it returns x restricted to inclusive lo/hi bounds. Stage an exact edit, and explain the change briefly. Do not request tests yet.",
            )
            edits = [
                a
                for a in c["actions"]
                if a["kind"] == "edit" and a["status"] == "pending"
            ]
            assert edits, "Model did not produce a draft"
            assert (project / "maths.py").read_text().endswith("return x\n")
            original_source = (project / "maths.py").read_text()
            report["checks"]["model_read_and_draft"] = {
                "passed": True,
                "seconds": elapsed,
                "draft": edits[-1]["payload"]["diff"],
            }
            print("DRAFT PRODUCED", flush=True)
            applied = rpc("approve", id=c["id"], action_id=edits[-1]["id"])
            assert (project / "maths.py").read_text() != original_source
            report["checks"]["explicit_apply"] = {"passed": True, "result": applied}
            report["checks"]["generation_metrics"] = c["messages"][-1].get("usage")
            c, elapsed = task(
                c["id"],
                "Now request the python-unittest test recipe for this project. Do not claim tests passed before approval.",
            )
            tests = [
                a
                for a in c["actions"]
                if a["kind"] == "tests" and a["status"] == "pending"
            ]
            assert tests, "Model did not request tests"
            result = rpc("approve", id=c["id"], action_id=tests[-1]["id"])
            assert result["exit_code"] == 0, result
            report["checks"]["isolated_tests"] = result
            rpc("undo", id=c["id"], action_id=edits[-1]["id"])
            assert (project / "maths.py").read_text().endswith("return x\n")
            report["checks"]["undo"] = True
            c2 = rpc("new_chat")
            rpc(
                "send",
                id=c2["id"],
                text="Write a very detailed tutorial explaining all sorting algorithms in Python.",
            )
            time.sleep(1)
            stopped = time.monotonic()
            rpc("cancel")
            while True:
                item = receive()
                if item.get("event") == "idle":
                    break
            report["checks"]["cancellation_seconds"] = round(
                time.monotonic() - stopped, 2
            )
            rpc("model_unload")
            report["checks"]["unload"] = rpc("state")["model"]["state"] == "unloaded"
            report["passed"] = True
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            report["service_exit"] = process.returncode
            (ROOT / "reports/app-smoke.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report["checks"], indent=2), flush=True)


if __name__ == "__main__":
    main()
