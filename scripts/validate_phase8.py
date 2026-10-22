"""Owned source/frozen JSON-RPC proof of actual child inference and durable waits.

Temporary app home, synthetic project and loopback SSE model only. Reports contain
content-free request metadata, checks and hashes; no prompts, source or credentials.
"""

import argparse
import hashlib
import json
import select
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


def tool(name, ident, args, index=0):
    return {"index": index, "id": ident, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def rejected(service, method, **params):
    try:
        service.rpc(method, **params)
    except RuntimeError:
        return
    raise AssertionError("Foreign or active-state mutation was admitted: " + method)


def until(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError("The owned service fixture did not reach its expected state.")


def tool_results(messages):
    for message in messages:
        if message.get("role") == "tool" and isinstance(message.get("content"), str):
            try:
                value = json.loads(message["content"])
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def host_inputs(messages):
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and content.startswith("HOST INPUT (agent-origin;"):
            try:
                yield json.loads(content.split("\n", 1)[1])
            except (ValueError, IndexError):
                continue


def wait_run(service, cid, rid, expected, steps, errors):
    try:
        return until(lambda: (run if (run := service.rpc("run_get", id=cid, run_id=rid))["state"] == expected else None))
    except AssertionError:
        run = service.rpc("run_get", id=cid, run_id=rid)
        tree = service.rpc("agent_list", chat_id=cid, run_id=rid)
        raise AssertionError({"expected": expected, "state": run["state"], "error": run.get("error"),
            "steps": steps, "fixture_errors": errors,
            "tools": [{"name": receipt["name"], "state": receipt["state"],
                       "status": (receipt.get("result") or {}).get("status"),
                       "error": (receipt.get("result") or {}).get("error")}
                      for receipt in run["tools"]],
            "nodes": [{"name": node["name"], "status": node["status"], "review": node["review"],
                       "error": (node.get("result") or {}).get("error")} for node in tree["nodes"]]}) from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / "backend/main.py")]
    checks, records, errors = [], [], []
    condition = threading.Condition(threading.RLock())
    steps, active = {}, set()
    peak = 0
    starts = {key: threading.Event() for key in ("root-2", "cancel-child-1", "read-child-1")}
    gates = {key: threading.Event() for key in starts}
    closed = {key: threading.Event() for key in starts}
    known_agents = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            nonlocal peak
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body["messages"]
            assignment = next((m["content"] for m in messages if isinstance(m.get("content"), str)
                               and m["content"].startswith("HOST ASSIGNMENT (task data, no new authorization):\n")), None)
            task = json.loads(assignment.split("\n", 1)[1]) if assignment else None
            role = task["task"] if task else "root"
            with condition:
                step = steps[role] = steps.get(role, 0) + 1
                key = f"{role}-{step}"
                active.add(key)
                peak = max(peak, len(active))
                if task:
                    known_agents[role] = task["id"]
                record = {"role": role, "step": step, "model": body["model"],
                          "tool_names": sorted(t["function"]["name"] for t in body.get("tools", []))}
                records.append(record)
                condition.notify_all()
            try:
                assert body["model"] == "phase8-owned-fixture"
                assert self.path == "/v1/chat/completions"
                assert "_pebble_" not in json.dumps(body)
                offered = set(record["tool_names"])
                if task:
                    assert offered == {"Read"}, "Child acquired tools outside its pin."
                    assert role in {"cancel-child", "read-child"}
                else:
                    assert {"SpawnAgent", "ReadAgentTree", "WaitForAgents", "ReviewAgentResult"} <= offered
                    assert "fixture-child-followup" not in json.dumps(messages), "Child context leaked into the root."
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(event):
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    self.wfile.flush()

                def hold():
                    emit({"choices": [{"delta": {}}]})
                    starts[key].set()
                    deadline = time.monotonic() + 20
                    while not gates[key].wait(.03):
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable and not self.connection.recv(1, socket.MSG_PEEK):
                            closed[key].set()
                            return False
                        if time.monotonic() >= deadline:
                            raise AssertionError("Streaming gate did not resolve.")
                    return True

                if key in starts and not hold():
                    return
                if role == "cancel-child":
                    raise AssertionError("Cancelled child unexpectedly reached completion.")
                if role == "read-child" and step == 1:
                    calls = [tool("Read", "child-read", {"file_path": "fixture.txt"})]
                elif role == "read-child":
                    assert step == 2
                    assert any(value.get("path") == "fixture.txt" and value.get("file_hash")
                               for value in tool_results(messages))
                    record["human_followup_seen"] = any(
                        "HOST CHILD FOLLOW-UP (human-origin; never permission):" in str(m.get("content", ""))
                        and "fixture-child-followup" in str(m.get("content", "")) for m in messages)
                    assert record["human_followup_seen"]
                    calls = []
                elif step == 1:
                    calls = [tool("SpawnAgent", "spawn-cancel", {
                        "task": "cancel-child", "name": "Cancel fixture", "tools": json.dumps(["Read"]),
                        "max_steps": 4, "max_seconds": 40, "model_tokens": 100000}),
                        tool("SpawnAgent", "spawn-read", {"task": "read-child", "name": "Read fixture",
                        "tools": json.dumps(["Read"]), "max_steps": 4, "max_seconds": 40,
                        "model_tokens": 100000}, 1)]
                elif step == 2:
                    nodes = [value for value in tool_results(messages) if value.get("task") == "read-child"]
                    assert len(nodes) == 1
                    calls = [tool("WaitForAgents", "root-wait", {"agent_ids": json.dumps([nodes[0]["id"]])})]
                elif step == 3:
                    record["agent_wait_ready_seen"] = any(value.get("kind") == "agent_wait_ready"
                        and value.get("payload", {}).get("status") == "ready"
                        and value.get("payload", {}).get("reason") == "child_settled"
                        for value in host_inputs(messages))
                    assert record["agent_wait_ready_seen"], "Root did not receive its durable agent-origin ready input."
                    calls = [tool("ReadAgentTree", "root-tree", {})]
                elif step == 4:
                    tree = next(value for value in tool_results(messages) if "nodes" in value and "budget" in value)
                    nodes = {node["task"]: node for node in tree["nodes"]}
                    done, stopped = nodes["read-child"], nodes["cancel-child"]
                    assert done["status"] == "completed" and done["evidence_ids"]
                    assert stopped["status"] == "stopped"
                    assert done["result"]["usage"]["input_tokens"] == 202
                    calls = [tool("ReviewAgentResult", "review-read", {"agent_id": done["id"],
                        "generation": done["generation"], "evidence_ids": json.dumps(done["evidence_ids"])}),
                        tool("ReviewAgentResult", "dismiss-cancel", {"agent_id": stopped["id"],
                        "generation": stopped["generation"], "decision": "dismiss"}, 1)]
                else:
                    assert step == 5, "Root dispatched extra inference instead of settling."
                    reviews = list(tool_results(messages))
                    assert any(item.get("review") == "accepted" for item in reviews)
                    assert any(item.get("review") == "dismissed" for item in reviews)
                    calls = []
                delta = {"tool_calls": calls} if calls else {"content": "Verified the owned child fixture."}
                emit({"choices": [{"delta": delta}]})
                input_tokens = 1000 + step if role == "root" else 200 + step
                record["input_tokens"] = input_tokens
                emit({"choices": [], "usage": {"prompt_tokens": input_tokens, "completion_tokens": 7,
                                                "prompt_tokens_details": {"cached_tokens": 0}}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                closed.get(key, threading.Event()).set()
            except Exception as error:
                with condition:
                    errors.append(type(error).__name__ + ": " + str(error)[:200])
            finally:
                with condition:
                    active.discard(key)
                    condition.notify_all()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="pebble-phase8-smoke-") as directory:
            fixture_root = Path(directory)
            home, project = fixture_root / "home", fixture_root / "project"
            project.mkdir()
            (project / "fixture.txt").write_text("Owned synthetic child read evidence.\n")
            service = Service(command, home)
            try:
                profile = service.rpc("model_save", name="Owned Phase 8 fixture",
                    url=f"http://127.0.0.1:{server.server_port}", model="phase8-owned-fixture", context=32768)
                cid = service.rpc("new_chat", workspace=str(project))["id"]
                rid = service.rpc("send", id=cid, text="Delegate two child agents to this scoped project, wait for their results and review their evidence.")["run_id"]
                for event in starts.values():
                    if not event.wait(15):
                        diagnostic = service.rpc("agent_list", chat_id=cid, run_id=rid)
                        raise AssertionError({"steps": steps, "fixture_errors": errors,
                            "nodes": [{"name": node["name"], "status": node["status"],
                                       "error": (node.get("result") or {}).get("error")}
                                      for node in diagnostic["nodes"]]})
                assert peak == 3 and {"root-2", "cancel-child-1", "read-child-1"} <= active
                tree = service.rpc("agent_list", chat_id=cid, run_id=rid)
                nodes = {node["task"]: node for node in tree["nodes"]}
                assert len(nodes) == 2 and all(node["status"] == "running" for node in nodes.values())
                checks.append("actual root and two pinned cloud children stream concurrently within the shared three-request limit")
                rejected(service, "model_select", profile_id=profile["profile_id"])
                other = service.rpc("new_chat")["id"]
                rejected(service, "agent_get", chat_id=other, run_id=rid, agent_id=nodes["read-child"]["id"])
                rejected(service, "agent_cancel", chat_id=other, run_id=rid, agent_id=nodes["read-child"]["id"])
                checks.append("active native profile mutation and foreign-chat agent inspection/cancellation fail before dispatch")
                cancelled = service.rpc("agent_cancel", chat_id=cid, run_id=rid, agent_id=nodes["cancel-child"]["id"])
                assert nodes["cancel-child"]["id"] in cancelled["agent_ids"]
                assert closed["cancel-child-1"].wait(3), "Child socket was not interrupted."
                assert not closed["root-2"].is_set() and not closed["read-child-1"].is_set()
                assert {"root-2", "read-child-1"} <= active
                assert service.rpc("run_get", id=cid, run_id=rid)["state"] == "running"
                checks.append("native child cancellation interrupts only its socket; root and sibling remain active")
                steered = service.rpc("agent_steer", chat_id=cid, run_id=rid,
                    agent_id=nodes["read-child"]["id"], text="fixture-child-followup", input_id="owned-followup")
                assert steered["origin"] == "human" and steered["authorization"] == "none"
                gates["root-2"].set()
                waiting = wait_run(service, cid, rid, "waiting_input", steps, errors)
                assert any(t["name"] == "WaitForAgents" and t.get("result", {}).get("required") for t in waiting["tools"])
                assert not service.rpc("state")["active"]
                with sqlite3.connect(home / "pebble.sqlite") as db:
                    assert db.execute("SELECT count(*) FROM agent_waits WHERE run_id=? AND status='waiting'", (rid,)).fetchone()[0] == 1
                checks.append("root WaitForAgents creates a durable required wait and releases the root executor while child inference continues")
                gates["read-child-1"].set()
                completed = wait_run(service, cid, rid, "completed", steps, errors)
                tree = service.rpc("agent_list", chat_id=cid, run_id=rid)
                nodes = {node["task"]: node for node in tree["nodes"]}
                assert nodes["read-child"]["review"] == "accepted" and nodes["cancel-child"]["review"] == "dismissed"
                assert nodes["read-child"]["dispatches"] == 2 and nodes["cancel-child"]["dispatches"] == 1
                assert steps == {"root": 5, "cancel-child": 1, "read-child": 2}
                assert not errors, errors
                checks.append("settled child evidence wakes the same root automatically; parent accepts real read evidence and dismisses the stopped child")
                transcript = service.rpc("get_chat", id=cid)
                final_usage = transcript["messages"][-1]["usage"]
                assert final_usage["input_tokens"] == sum(item["input_tokens"] for item in records if item["role"] == "root")
                assert nodes["read-child"]["result"]["usage"]["input_tokens"] == 202
                assert not nodes["cancel-child"].get("result") or "usage" not in nodes["cancel-child"]["result"]
                checks.append("child usage, projection and human follow-up remain independent from root transcript and usage accounting")
                with sqlite3.connect(home / "pebble.sqlite") as db:
                    assert not db.execute("SELECT 1 FROM agent_claims LIMIT 1").fetchone()
                    dispatches, charged = db.execute("SELECT dispatches,charged_tokens FROM agent_trees WHERE run_id=?", (rid,)).fetchone()
                    estimated = db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND status='estimated'", (rid,)).fetchone()[0]
                    assert dispatches - estimated == 7 and charged > 0
                    assert estimated == tree["budget"]["pre_tree_estimate"]["dispatches"]
                    assert not db.execute("SELECT 1 FROM agent_dispatches WHERE status='running' LIMIT 1").fetchone()
                    assert db.execute("SELECT count(*) FROM agent_inputs WHERE status='delivered'", ()).fetchone()[0] == 1
                checks.append("all actual attempts share one durable charged budget, consume the follow-up once, and release worker claims")
                attempts = len(records)
                service.close()
                service = Service(command, home)
                restored = service.rpc("agent_list", chat_id=cid, run_id=rid)
                assert [n["review"] for n in restored["nodes"]] == ["dismissed", "accepted"]
                assert service.rpc("run_get", id=cid, run_id=rid)["state"] == "completed"
                assert len(records) == attempts
                checks.append("actual service restart preserves settled tree/review/wait history without inference or child replay")
                service.rpc("delete_chat", id=cid)
                with sqlite3.connect(home / "pebble.sqlite") as db:
                    for table in ("agent_nodes", "agent_trees", "agent_claims", "agent_dispatches", "agent_waits", "agent_inputs", "agent_checkpoints"):
                        assert not db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                service.rpc("delete_chat", id=other)
                checks.append("chat deletion cascades the owning tree, receipts, claims, waits, child inputs and checkpoints")
            finally:
                for gate in gates.values():
                    gate.set()
                service.close()
    finally:
        for gate in gates.values():
            gate.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
    files = ["backend/main.py", "backend/pebble/app.py", "backend/pebble/runner.py",
             "backend/pebble/agent_manager.py", "backend/pebble/agent_provider.py", "backend/pebble/agent_tree.py",
             "backend/pebble/interaction.py", "backend/pebble/runtime.py", "backend/pebble/tool_catalogue.py"]
    hashes = {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in files}
    report = {"date": datetime.now(timezone.utc).isoformat(), "kind": "frozen" if args.backend else "source",
              "passed": True, "checks": checks, "synthetic_provider_attempts": len(records),
              "request_metadata": records, "maximum_concurrent_streams": peak,
              "shared_budget": tree["budget"],
              "backend_sha256": hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else hashes["backend/main.py"],
              "source_sha256": hashes,
              "limits": "Owned JSON-RPC and loopback SSE fixtures; no external accounts, provider billing, model quality, installed native UI or frontier evaluation."}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
