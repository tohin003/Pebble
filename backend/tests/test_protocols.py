"""Actual HTTP fixtures exercise wire formats, discovery, keys and MCP scopes."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from pebble.app import App
from pebble.extensions import Extensions
from pebble.permissions import DEFAULT


@pytest.fixture
def server():
    records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "data": [
                            {
                                "id": "fixture-chat",
                                "display_name": "Fixture chat",
                                "max_input_tokens": 8192,
                            }
                        ]
                    }
                ).encode()
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            records.append((body, dict(self.headers)))
            if self.path == "/mcp":
                self.send_response(202 if "id" not in body else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("MCP-Session-Id", "fixture-session")
                self.end_headers()
                if "id" not in body:
                    return
                method = body["method"]
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "Read a fixture",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    }
                elif method == "tools/call":
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "Result: "
                                + body["params"]["arguments"]["query"],
                            }
                        ],
                        "isError": False,
                    }
                else:
                    result = {}
                self.wfile.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": body["id"], "result": result}
                    ).encode()
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if self.path.endswith("/messages"):
                events = [
                    {
                        "type": "message_start",
                        "message": {
                            "usage": {
                                "input_tokens": 40,
                                "cache_read_input_tokens": 10,
                                "cache_creation_input_tokens": 0,
                            }
                        },
                    },
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "text_delta",
                            "text": "Anthropic fixture reply",
                        },
                    },
                    {"type": "message_delta", "usage": {"output_tokens": 6}},
                    {"type": "message_stop"},
                ]
            else:
                events = [
                    {"choices": [{"delta": {"content": "OpenAI fixture reply"}}]},
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 50,
                            "completion_tokens": 6,
                            "prompt_tokens_details": {"cached_tokens": 10},
                        },
                    },
                ]
            for event in events:
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
            if not self.path.endswith("/messages"):
                self.wfile.write(b"data: [DONE]\n\n")

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(http.server_port), records
    http.shutdown()
    http.server_close()


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_discovery_streaming_and_reported_usage(tmp_path, server, kind):
    base, records = server
    a = App(tmp_path / "data", tmp_path / "resources", lambda *args: None)
    try:
        assert a.model.discover(base, kind)[0]["id"] == "fixture-chat"
        a.model_save(name="Fixture", url=base, model="fixture-chat", kind=kind)
        c = a.new_chat()
        a.work(c, "Explain a stack briefly", False, [])
        m = a.chat(c["id"])["messages"][-1]
        assert "fixture reply" in m["content"]
        assert m["usage"]["output_tokens"] == 6
        assert m["usage"]["cached_tokens"] == 10
        body, headers = records[-1]
        assert "_pebble_current" not in json.dumps(body)
        assert "_pebble_memory" not in json.dumps(body)
        if kind == "anthropic":
            assert {k.lower(): v for k, v in headers.items()}[
                "anthropic-version"
            ] == "2023-06-01" and "system" in body
        else:
            assert body["stream_options"]["include_usage"] is True
    finally:
        a.close()


def test_mcp_initialize_review_scope_and_call(tmp_path, server):
    base, records = server
    a = App(tmp_path / "data", tmp_path / "resources", lambda *args: None)
    try:
        pid = a.extension_add(
            kind="plugin",
            name="Fixture docs",
            content='{"tools":["lookup"],"read_only":true}',
            url=base + "/mcp",
        )
        assert not a.extensions.plugin_menu()
        a.extension_toggle(pid, True)
        assert a.extensions.plugin_menu()[0]["tools"][0]["name"] == "lookup"
        c = a.new_chat()
        result = a.execute(
            "PluginCall",
            {"plugin_id": pid, "tool": "lookup", "arguments": '{"query":"queues"}'},
            None,
            c["id"],
        )
        assert result["status"] == "awaiting_user_approval"
        assert not any(b["method"] == "tools/call" for b, _ in records)
        result = a.approve(c["id"], result["action_id"])
        assert "queues" in result["untrusted_plugin_output"]
        for body, headers in records:
            if body["method"] != "initialize":
                assert headers["Mcp-Session-Id"] == "fixture-session"
        from pebble.policy import PolicyError

        with pytest.raises(PolicyError):
            a.extensions.plugin_payload(pid, "forged", "{}")
        a.extension_toggle(pid, False)
        with pytest.raises(PolicyError):
            a.extensions.plugin_payload(pid, "lookup", '{"query":"x"}')
        with pytest.raises(ValueError):
            Extensions.local_refs({"$ref": "https://attacker.example/schema"})
    finally:
        a.close()
