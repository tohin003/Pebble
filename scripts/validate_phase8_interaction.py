"""Actual source/frozen native clarification, patch inspection, Guard, broker and test proof.

Everything runs in an owned temporary project against a loopback synthetic model.
No fake Guard, provider accounts or real model billing are used. Reports retain
only content-free metadata, receipt statuses, guard decisions and fingerprints.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service
from validate_phase8 import rejected, tool, tool_results, until, wait_run


ORIGINAL = "def add(a, b):\n    return a - b\n"
PATCH = "def add(a, b):\n    return a + b\n"
TEST = """import unittest
from calculator import add

class CalculatorTest(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(add(2, 3), 5)
"""
QUESTION = [{"id": "operation", "title": "Which operation should the function perform?",
             "options": [{"id": "addition", "label": "Addition"},
                         {"id": "subtraction", "label": "Subtraction"}]}]
ANSWER = {"operation": {"option_id": "addition"}}


def latest(messages, predicate):
    return next(value for value in reversed(list(tool_results(messages))) if predicate(value))


def host_envelopes(messages, kind):
    """Durable inbox rows as RunInteractions.deliver appends them: the header carries the stored origin."""
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str) or not content.startswith("HOST INPUT ("):
            continue
        header, _, body = content.partition("\n")
        origin = header[len("HOST INPUT ("):].split("-origin;", 1)[0]
        try:
            item = json.loads(body)
        except ValueError:
            continue
        if item.get("kind") == kind:
            yield origin, item


def host_resolutions(messages, kind):
    for origin, item in host_envelopes(messages, kind):
        assert origin == "host", kind + " arrived with " + origin + " origin."
        yield item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / "backend/main.py")]
    checks, records, errors = [], [], []
    lock = threading.RLock()
    steps = {}
    child_ids = []
    original_hash = hashlib.sha256(ORIGINAL.encode()).hexdigest()
    patched_hash = hashlib.sha256(PATCH.encode()).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body["messages"]
            assignment = next((m["content"] for m in messages if isinstance(m.get("content"), str)
                and m["content"].startswith("HOST ASSIGNMENT (task data, no new authorization):\n")), None)
            node = json.loads(assignment.split("\n", 1)[1]) if assignment else None
            role = "child" if node else "root"
            with lock:
                step = steps[role] = steps.get(role, 0) + 1
                record = {"role": role, "step": step, "model": body["model"],
                          "tools": sorted(t["function"]["name"] for t in body.get("tools", []))}
                records.append(record)
                if node and node["id"] not in child_ids:
                    child_ids.append(node["id"])
            try:
                assert body["model"] == "phase8-interaction-fixture"
                assert not any(any(key.startswith("_pebble_") for key in message) for message in messages), "Internal context markers leaked as provider message fields."
                offered = set(record["tools"])
                if node:
                    assert offered == {"AskParentQuestion", "Read", "ProposePatch"}
                    assert node["write_paths"] == ["calculator.py"]
                    if step == 1:
                        calls = [tool("AskParentQuestion", "child-question", {"questions": json.dumps(QUESTION)})]
                    elif step == 2:
                        answered = latest(messages, lambda item: item.get("status") == "answered")
                        assert answered["authorization"] == "none" and answered["origin"] == "agent"
                        assert answered["answers"] == {"operation": {"option_id": "addition", "text": "Addition"}}, "Native answer did not retain its normalized option label."
                        record["human_origin_seen"] = any("HOST CHILD FOLLOW-UP (human-origin; never permission):" in str(m.get("content"))
                            and "Native clarification answer:" in str(m.get("content")) for m in messages)
                        assert record["human_origin_seen"], "Native input was not delivered with human provenance."
                        calls = [tool("Read", "child-read", {"file_path": "calculator.py"})]
                    elif step == 3:
                        read = latest(messages, lambda item: item.get("path") == "calculator.py" and item.get("file_hash"))
                        assert read["file_hash"] == original_hash
                        calls = [tool("ProposePatch", "outside-proposal", {"file_path": "outside.py", "content": PATCH}),
                                 tool("ProposePatch", "child-patch", {"file_path": "calculator.py", "content": PATCH}, 1)]
                    else:
                        assert step == 4
                        proposal = latest(messages, lambda item: item.get("patch_id"))
                        assert proposal["status"] == "proposed" and proposal["applied"] is False
                        assert proposal["authorization"] == "none" and proposal["baseline"] == original_hash
                        assert any(item.get("status") == "rejected" and "lease" in item.get("error", "") for item in tool_results(messages))
                        record["out_of_lease_rejected"] = True
                        calls = []
                else:
                    assert {"SpawnAgent", "PromoteAgentQuestion", "ReadAgentPatch", "ReviewAgentResult", "IntegrateAgentPatch", "RunTests"} <= offered, {"step": step, "tools": sorted(offered)}
                    if step == 1:
                        calls = [tool("SpawnAgent", "spawn-patch", {"task": "clarify and propose the addition fix",
                            "name": "Clarifying patch fixture", "tools": json.dumps(["AskParentQuestion", "Read", "ProposePatch"]),
                            "write_paths": json.dumps(["calculator.py"]), "max_steps": 6, "max_seconds": 120,
                            "model_tokens": 250000})]
                    elif step == 2:
                        child = latest(messages, lambda item: item.get("agent_id") and item.get("status") == "queued")
                        calls = [tool("WaitForAgents", "wait-question", {"agent_ids": json.dumps([child["id"]])})]
                    elif step == 3:
                        calls = [tool("ReadAgentTree", "read-parent-question", {})]
                    elif step == 4:
                        tree = latest(messages, lambda item: item.get("questions"))
                        question = tree["questions"][0]
                        assert question["status"] == "pending"
                        calls = [tool("PromoteAgentQuestion", "promote-question", {"question_id": question["id"]}),
                            tool("WaitForAgents", "wait-child-patch", {"agent_ids": json.dumps([question["agent_id"]])}, 1)]
                    elif step == 5:
                        promoted = latest(messages, lambda item: item.get("requester_agent_id") and item.get("agent_question_id"))
                        answers = list(host_envelopes(messages, "question_answer"))
                        ready = list(host_envelopes(messages, "agent_wait_ready"))
                        record["native_answer_seen"] = bool(answers)
                        record["ready_reasons"] = [item["payload"].get("reason") for _, item in ready]
                        # Only the native answer route writes human-origin rows; tree wakes are agent-origin.
                        assert answers and all(origin == "human" for origin, _ in answers), "Native answer lacked human-origin HOST INPUT."
                        assert ready and all(origin == "agent" for origin, _ in ready), "Wait readiness lacked agent-origin HOST INPUT."
                        answer = next(item for _, item in answers if item["payload"]["question_id"] == promoted["question_id"])
                        assert answer["payload"]["answers"] == {"operation": {"option_id": "addition", "text": "Addition"}}
                        followups = [item["payload"] for _, item in ready
                            if answer["input_id"] in item["payload"].get("input_ids", [])]
                        assert followups and followups[-1]["reason"] == "human_followup"
                        assert followups[-1]["required"] is False and followups[-1]["authorization"] == "none"
                        calls = [tool("WaitForAgents", "wait-after-native-answer", {"agent_ids": json.dumps([child_ids[0]])})]
                    elif step == 6:
                        ready_child = latest(messages, lambda item: str(item.get("call_id", "")).endswith(":wait-after-native-answer"))
                        assert ready_child["status"] == "ready" and child_ids[0] in ready_child["agent_ids"]
                        calls = [tool("ReadAgentTree", "read-child-patch", {"agent_id": child_ids[0]})]
                    elif step == 7:
                        tree = latest(messages, lambda item: item.get("detail", {}).get("patches"))
                        child = tree["detail"]
                        assert child["status"] == "completed" and child["review"] == "pending", {"status": child["status"], "review": child["review"]}
                        assert len(child["patches"]) == 1 and child["patches"][0]["status"] == "proposed", {"patch_count": len(child["patches"]), "patch_statuses": [p["status"] for p in child["patches"]]}
                        calls = [tool("ReadAgentPatch", "inspect-patch", {"patch_id": child["patches"][0]["id"]})]
                    elif step == 8:
                        page = latest(messages, lambda item: "untrusted_proposal" in item)
                        child = latest(messages, lambda item: item.get("detail", {}).get("patches"))["detail"]
                        assert page["patch_id"] == child["patches"][0]["id"] and page["agent_id"] == child["id"]
                        assert page["generation"] == child["generation"] and page["file_path"] == "calculator.py"
                        assert page["baseline"] == original_hash and page["proposed_hash"] == patched_hash
                        assert page["untrusted_proposal"] == PATCH and page["next_offset"] is None
                        assert page["total_characters"] == len(PATCH)
                        assert page["applied"] is False and page["authorization"] == "none"
                        record["patch_inspected"] = True
                        valid = [receipt["call_id"] for receipt in child["receipts"] if receipt["name"] == "Read" or
                            receipt["name"] == "ProposePatch" and (receipt.get("result") or {}).get("patch_id")]
                        assert len(valid) == 2 and set(valid) <= set(child["evidence_ids"])
                        calls = [tool("ReviewAgentResult", "review-patch-evidence", {"agent_id": child["id"],
                            "generation": child["generation"], "evidence_ids": json.dumps(valid)})]
                    elif step == 9:
                        accepted = latest(messages, lambda item: item.get("review") == "accepted")
                        tree = latest(messages, lambda item: item.get("detail", {}).get("patches"))
                        assert accepted["agent_id"] == tree["detail"]["id"]
                        calls = [tool("IntegrateAgentPatch", "integrate-patch", {"patch_id": tree["detail"]["patches"][0]["id"]})]
                    elif step == 10:
                        proposed = latest(messages, lambda item: item.get("patch_id") and item.get("action_id"))
                        assert proposed["status"] == "awaiting_user_approval", proposed.get("error")
                        calls = []
                    elif step == 11:
                        resolutions = list(host_resolutions(messages, "action_resolution"))
                        assert any(item["payload"].get("kind") == "edit" and item["payload"].get("status") == "applied" for item in resolutions)
                        calls = [tool("RunTests", "run-project-tests", {"recipe": "python-unittest"})]
                    else:
                        # Phase 46 (D154): the run waits at the staged RunTests (runner.STAGED_STOP), so this request
                        # follows the native test approval; the pending request is checked from the journal in main
                        assert step == 12, "Root did not settle after actual approved test evidence."
                        resolutions = list(host_resolutions(messages, "action_resolution"))
                        tested = next(item["payload"] for item in resolutions if item["payload"].get("kind") == "tests")
                        assert tested["status"] == "applied" and tested["result"]["exit_code"] == 0
                        assert "Ran 1 test" in tested["result"]["output"] and "\nOK" in tested["result"]["output"]
                        record["actual_test_evidence_seen"] = True
                        calls = []
                text = ("The scoped child proposal is ready for parent review." if role == "child" else
                        "The broker action awaits native review; I am not claiming an applied effect." if step < 12 else
                        "Native review applied the source change and the recorded isolated unittest run passed one test.")
                delta = {"tool_calls": calls} if calls else {"content": text}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in ({"choices": [{"delta": delta}]}, {"choices": [],
                    "usage": {"prompt_tokens": 100 + step, "completion_tokens": 5,
                              "prompt_tokens_details": {"cached_tokens": 0}}}):
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception as error:
                with lock:
                    errors.append(f"{role}:{step} " + type(error).__name__ + ": " + str(error)[:250])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    guard_metadata = []
    try:
        with tempfile.TemporaryDirectory(prefix="pebble-phase8-interaction-") as folder:
            base = Path(folder)
            home, project = base / "home", base / "project"
            project.mkdir()
            (project / "calculator.py").write_text(ORIGINAL)
            (project / "test_calculator.py").write_text(TEST)
            service = Service(command, home)
            try:
                assert service.rpc("state")["guard"]["available"], "The actual vendored AgentGuard is required."
                service.rpc("model_save", name="Owned Phase 8 interaction fixture", url=f"http://127.0.0.1:{server.server_port}",
                    model="phase8-interaction-fixture", context=32768)
                policy = service.rpc("state")["settings"]["permissions"]
                assert policy["mode"] == "ask"
                cid = service.rpc("new_chat", workspace=str(project))["id"]
                rid = service.rpc("send", id=cid,
                    text="Fix calculator.py add using a delegated child. Clarify the operation in a native question, propose the exact file patch, apply it through native review and run isolated Python tests.")["run_id"]
                waiting = wait_run(service, cid, rid, "waiting_input", steps, errors)
                # The first wait can settle on the child question; require its promoted mapping.
                native = until(lambda: next((question for question in service.rpc("run_get", id=cid, run_id=rid)["questions"]
                    if question.get("requester_agent_id") and question["status"] == "pending"), None))
                waiting = wait_run(service, cid, rid, "waiting_input", steps, errors)
                child_id = native["requester_agent_id"]
                assert native["required"] is False and native["requester_agent_name"] == "Clarifying patch fixture"
                assert service.rpc("agent_get", chat_id=cid, run_id=rid, agent_id=child_id)["status"] == "waiting_input"
                assert (project / "calculator.py").read_text() == ORIGINAL
                assert not service.rpc("get_chat", id=cid)["actions"]
                with sqlite3.connect(home / "pebble.sqlite") as db:
                    assert db.execute("SELECT status FROM agent_parent_questions WHERE agent_id=?", (child_id,)).fetchone()[0] == "promoted"
                    assert db.execute("SELECT question_id FROM agent_native_questions WHERE agent_id=?", (child_id,)).fetchone()[0] == native["id"]
                checks.append("actual child question is promoted to a requester-labelled native optional form while the root durably waits")
                other = service.rpc("new_chat")["id"]
                rejected(service, "question_answer", id=other, question_id=native["id"], revision=1, answers=ANSWER)
                rejected(service, "question_answer", id=cid, question_id=native["id"], revision=2, answers=ANSWER)
                answered = service.rpc("question_answer", id=cid, question_id=native["id"], revision=1, answers=ANSWER)
                assert answered["status"] == "answered"
                assert service.rpc("question_answer", id=cid, question_id=native["id"], revision=1, answers=ANSWER)["status"] == "answered"
                checks.append("foreign and stale native answers are rejected; exact answer replay is idempotent and grants no permission")
                proposed_run = wait_run(service, cid, rid, "waiting_approval", steps, errors)
                until(lambda: not service.rpc("state")["active"])
                detail = service.rpc("agent_get", chat_id=cid, run_id=rid, agent_id=child_id)
                assert detail["status"] == "completed" and detail["review"] == "accepted"
                assert len(detail["patches"]) == 1 and detail["patches"][0]["status"] == "broker_proposed"
                assert not (project / "outside.py").exists() and (project / "calculator.py").read_text() == ORIGINAL
                actions = service.rpc("get_chat", id=cid)["actions"]
                assert len(actions) == 1 and actions[0]["kind"] == "edit" and actions[0]["status"] == "pending"
                edit = actions[0]
                assert edit["payload"]["baseline"] == original_hash and edit["payload"]["after"] == PATCH
                assert edit["payload"]["permission_mode"] == "ask"
                assert any(receipt["name"] == "Read" and receipt["call_id"].endswith(":readback") for receipt in proposed_run["tools"])
                assert any(receipt["name"] == "Write" and receipt["call_id"].endswith(":write") for receipt in proposed_run["tools"])
                checks.append("child out-of-lease proposal is rejected; valid patch stays data until accepted evidence and admitted root Read/Write create one pending broker edit")
                inspected = [receipt for receipt in proposed_run["tools"] if receipt["name"] == "ReadAgentPatch"]
                assert len(inspected) == 1 and inspected[0]["state"] == "completed"
                page = inspected[0]["result"]
                assert page["proposed_hash"] == patched_hash and page["baseline"] == original_hash
                assert page["applied"] is False and page["authorization"] == "none"
                assert any(record.get("patch_inspected") for record in records)
                checks.append("root reads the exact stored proposal through ReadAgentPatch before acceptance; the page is hash-bound, unapplied and grants no authorization")
                applied = service.rpc("approve", id=cid, action_id=edit["id"])
                assert applied["applied"] == "calculator.py" and (project / "calculator.py").read_text() == PATCH
                checks.append("native Ask-mode approval performs the sole source write and commits its owning root action receipt")
                test_run = wait_run(service, cid, rid, "waiting_approval", steps, errors)
                until(lambda: not service.rpc("state")["active"])
                tests = until(lambda: next((action for action in service.rpc("get_chat", id=cid)["actions"]
                    if action["kind"] == "tests" and action["status"] == "pending"), None))
                # The previous edit's waiting state can be observed before the
                # resumed inference publishes its test action. Wait for that
                # action first, then require the owning root to yield.
                wait_run(service, cid, rid, "waiting_approval", steps, errors)
                until(lambda: not service.rpc("state")["active"])
                # Phase 46 (D154): the model's view of the requested tests (a twelfth step before the stop) is the run's
                # journal now: the RunTests result is a tests proposal awaiting native review
                requested = [t["result"] for t in service.rpc("run_get", id=cid, run_id=rid)["tools"] if t["name"] == "RunTests"]
                assert len(requested) == 1 and requested[0].get("kind") == "tests" and requested[0].get("action_id") \
                    and requested[0]["status"] == "awaiting_user_approval", requested
                tested = service.rpc("approve", id=cid, action_id=tests["id"])
                assert tested["exit_code"] == 0 and "Ran 1 test" in tested["output"] and "\nOK" in tested["output"]
                assert "network denied" in tested["isolation"]
                checks.append("native test approval executes one actual unittest in a macOS-confined project snapshot with network denied")
                finished = wait_run(service, cid, rid, "completed", steps, errors)
                assert not errors, errors
                assert steps == {"root": 12, "child": 4}  # Phase 46 (D154): no held turn at the staged tests
                assert service.rpc("state")["settings"]["permissions"] == policy
                final_actions = service.rpc("get_chat", id=cid)["actions"]
                assert len(final_actions) == 2 and all(action["status"] == "applied" for action in final_actions)
                assert any(receipt.get("result", {}).get("action_id") == edit["id"]
                    and receipt["result"].get("status") == "applied" for receipt in finished["tools"])
                assert any(receipt["name"] == "RunTests" and receipt.get("result", {}).get("status") == "applied" for receipt in finished["tools"])
                with sqlite3.connect(home / "pebble.sqlite") as db:
                    assert db.execute("SELECT status FROM agent_parent_questions WHERE agent_id=?", (child_id,)).fetchone()[0] == "answered"
                    assert db.execute("SELECT origin,status FROM agent_inputs WHERE agent_id=?", (child_id,)).fetchall() == [("human", "delivered")]
                    assert not db.execute("SELECT 1 FROM agent_claims LIMIT 1").fetchone()
                    assert not db.execute("SELECT 1 FROM agent_path_leases WHERE status='held' LIMIT 1").fetchone()
                with sqlite3.connect(home / "guard/agentguard.db") as db:
                    guard_metadata = [{"event": row[0], "tool": row[1], "action": row[2], "level": row[3]}
                        for row in db.execute("SELECT e.type,e.tool,d.action,d.level FROM events e JOIN decisions d ON d.event_id=e.id WHERE e.session_id=? ORDER BY e.ts", (rid,))]
                    assert any(item["event"] == "pre_tool_use" and item["tool"] == "Write" and item["action"] == "allow" for item in guard_metadata)
                    assert any(item["event"] == "stop" and item["action"] == "allow" for item in guard_metadata)
                    assert db.execute("SELECT 1 FROM verifications WHERE status='passed' LIMIT 1").fetchone()
                checks.append("the actual vendored Guard records write admission, passed test verification and allowed completion; root settles honestly with released leases")
                attempts = len(records)
                service.close()
                service = Service(command, home)
                assert service.rpc("run_get", id=cid, run_id=rid)["state"] == "completed"
                assert service.rpc("agent_get", chat_id=cid, run_id=rid, agent_id=child_id)["review"] == "accepted"
                assert len(records) == attempts and (project / "calculator.py").read_text() == PATCH
                checks.append("actual restart preserves clarification, accepted patch and applied receipts without inference or effect replay")
                service.rpc("delete_chat", id=cid)
                service.rpc("delete_chat", id=other)
                with sqlite3.connect(home / "pebble.sqlite") as db:
                    for table in ("agent_native_questions", "agent_parent_questions", "agent_patches", "agent_inputs", "agent_path_leases"):
                        assert not db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                checks.append("chat cleanup removes promoted mappings, child answers, proposals and leases while retaining the natively approved project file")
            finally:
                service.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    files = ["backend/main.py", "backend/pebble/app.py", "backend/pebble/agent_manager.py", "backend/pebble/agent_provider.py",
             "backend/pebble/agent_tree.py", "backend/pebble/runner.py", "backend/pebble/interaction.py", "backend/pebble/runtime.py",
             "backend/pebble/guard.py", "backend/pebble/provenance.py", "backend/pebble/policy.py", "backend/pebble/testing.py",
             "backend/pebble/tool_catalogue.py"]
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
    report = {"date": datetime.now(timezone.utc).isoformat(), "kind": "frozen" if args.backend else "source", "passed": True,
              "checks": checks, "synthetic_provider_attempts": len(records), "request_metadata": records,
              "actual_guard_metadata": guard_metadata, "approved_source_sha256": patched_hash,
              "backend_sha256": hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else hashes["backend/main.py"],
              "source_sha256": hashes,
              "limits": "Owned JSON-RPC/loopback SSE, actual vendored Guard and approved macOS test snapshot only; no external accounts, provider billing, model quality or installed native UI proof."}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
