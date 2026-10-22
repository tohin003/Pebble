"""Exercise the frozen service with disposable data and local HTTP fixtures.

Run with .runtime/agent-env/bin/python scripts/validate_companion.py after
scripts/package_backend.py. The final weather check calls public Open-Meteo.
"""

import json, queue, subprocess, tempfile, threading, sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
sys.path.insert(0, str(root / "backend/tests"))
from test_protocols import server
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

fixture = server.__wrapped__()
base, records = next(fixture)
report = {
    "checks": {},
    "note": "API and MCP use a real local HTTP protocol fixture; no paid API account was used.",
}
with tempfile.TemporaryDirectory(prefix="pebble-companion-") as tmp:
    data = Path(tmp) / "data"
    project = Path(tmp) / "project"
    project.mkdir()
    (project / "notes.txt").write_text("fixture")
    exe = root / "src-tauri/resources/backend/pebble-backend"
    process = subprocess.Popen(
        [
            str(exe),
            "--home",
            str(data),
            "--resources",
            str(root / "src-tauri/resources"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    items = queue.Queue()

    def read():
        for line in process.stdout:
            items.put(json.loads(line))

    threading.Thread(target=read, daemon=True).start()
    seq = 0

    def rpc(method, **params):
        global seq
        seq += 1
        ident = str(seq)
        process.stdin.write(
            json.dumps({"id": ident, "method": method, "params": params}) + "\n"
        )
        process.stdin.flush()
        while True:
            item = items.get(timeout=90)
            if item.get("id") == ident:
                if "error" in item:
                    raise RuntimeError(item["error"])
                return item["result"]

    def task(cid, text):
        rpc("send", id=cid, text=text)
        while True:
            item = items.get(timeout=90)
            if item.get("event") == "error":
                raise RuntimeError(item["data"]["message"])
            if item.get("event") == "idle":
                break
        return rpc("get_chat", id=cid)

    try:
        state = rpc("state")
        assert len(state["memory"]["types"]) == 6 and state["guard"]["available"]
        report["checks"]["six_memory_layers_and_guard"] = True
        c = rpc("new_chat")
        c = task(c["id"], "Remember that I am a CSE graduate and AI engineer")
        assert c["messages"][-1]["usage"]["route"] == "memory"
        report["checks"]["explicit_user_memory"] = True
        for kind in ("openai", "anthropic"):
            assert rpc("model_discover", url=base, kind=kind)[0]["id"] == "fixture-chat"
            selected = rpc(
                "model_save",
                name="Fixture " + kind,
                url=base,
                model="fixture-chat",
                kind=kind,
                api_key="test-only-vault-key",
            )
            assert rpc(
                "model_discover", url=base, kind=kind, profile_id=selected["profile_id"]
            )
            c = rpc("new_chat")
            c = task(c["id"], "Explain a stack briefly")
            assert "fixture reply" in c["messages"][-1]["content"]
            assert c["messages"][-1]["usage"]["cached_tokens"] == 10
            rpc("model_remove", profile_id=selected["profile_id"])
            report["checks"][kind + "_catalogue_vault_stream_usage"] = True
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 72 720 Td (Engineering fixture document) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        pdf = Path(tmp) / "fixture.pdf"
        writer.write(pdf)
        c = rpc("new_chat")
        assert rpc("attach", id=c["id"], path=str(pdf))["characters"] > 10
        report["checks"]["frozen_confined_pdf"] = True
        pid = rpc(
            "extension_add",
            kind="plugin",
            name="Fixture docs",
            url=base + "/mcp",
            content='{"tools":["lookup"],"read_only":true}',
        )
        rpc("extension_toggle", id=pid, enabled=True)
        assert rpc("extensions")[0]["enabled"]
        report["checks"]["frozen_mcp_discovery_and_schema"] = True
        rpc("extension_remove", id=pid)
        c = rpc("new_chat", workspace=str(project))
        result = rpc("terminal_propose", id=c["id"], command='python3 -c "print(1+2)"')
        assert result["status"] == "awaiting_user_approval"
        result = rpc("approve", id=c["id"], action_id=result["action_id"])
        assert result["exit_code"] == 0 and result["output"].strip() == "3"
        report["checks"]["frozen_terminal_review_and_execution"] = True
        report["local_protocols_passed"] = True
        c = rpc("new_chat")
        try:
            c = task(c["id"], "What's the weather in Agartala today?")
        except RuntimeError as exc:
            report["checks"]["frozen_live_weather_without_model_or_app"] = False
            report["live_weather_error"] = str(exc)
            raise
        reply = c["messages"][-1]
        assert reply["usage"]["route"] == "weather" and not c["actions"]
        assert "°C" in reply["content"] and "Open-Meteo" in reply["content"]
        report["checks"]["frozen_live_weather_without_model_or_app"] = True
        report["passed"] = True
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        report["service_exit"] = process.returncode
        report.setdefault("passed", False)
        (root / "reports/companion-smoke.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps(report, indent=2))
try:
    next(fixture)
except StopIteration:
    pass
