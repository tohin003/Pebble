import pytest
from pebble.model import Model


def synthetic_model():
    model = Model.__new__(Model)

    def request(path, body):
        if path == "/apply-template":
            return {
                "prompt": "".join(str(m.get("content") or "") for m in body["messages"])
            }
        return {"tokens": [0] * len(body["content"])}

    model.json_request = request
    return model


def test_budget_keeps_current_task_and_tool_pairs():
    model = synthetic_model()
    body = {
        "max_tokens": 768,
        "messages": [
            {"role": "system", "content": "trusted rules"},
            {"role": "user", "content": "old history " * 150},
            {"role": "assistant", "content": "older response " * 100},
            {"role": "user", "content": "CURRENT TASK", "_pebble_current": True},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "old"}]},
            {
                "role": "tool",
                "tool_call_id": "old",
                "content": '{"source":"' + ("x" * 2200) + '"}',
            },
            {"role": "assistant", "content": None, "tool_calls": [{"id": "new"}]},
            {
                "role": "tool",
                "tool_call_id": "new",
                "content": '{"status":"awaiting_user_approval"}',
            },
        ],
    }
    result = model.budget(body)
    assert any(m.get("content") == "CURRENT TASK" for m in result["messages"])
    assert all("_pebble_current" not in m for m in result["messages"])
    calls = {c["id"] for m in result["messages"] for c in m.get("tool_calls", [])}
    assert {
        m["tool_call_id"] for m in result["messages"] if m["role"] == "tool"
    } <= calls
    assert (
        len("".join(str(m.get("content") or "") for m in result["messages"])) + 768
        <= 4000
    )
    assert "_pebble_current" in body["messages"][3]  # working history was not mutated


def test_oversized_current_task_fails_without_silent_truncation():
    model = synthetic_model()
    with pytest.raises(ValueError, match="too large"):
        model.budget(
            {
                "max_tokens": 768,
                "messages": [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "x" * 5000, "_pebble_current": True},
                ],
            }
        )


def test_compaction_prefers_old_tool_groups_and_preserves_checkpoint():
    model = synthetic_model()
    messages = [
        {"role": "system", "content": "rules"},
        {
            "role": "user",
            "content": "Read a.py; draft pending; tests not run",
            "_pebble_checkpoint": True,
        },
        {"role": "user", "content": "CURRENT GOAL", "_pebble_current": True},
        {"role": "user", "content": "Relevant saved context", "_pebble_memory": True},
    ]
    messages += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "old1"}, {"id": "old2"}],
        },
        {"role": "tool", "tool_call_id": "old1", "content": json_source("a" * 1700)},
        {"role": "tool", "tool_call_id": "old2", "content": json_source("b" * 1700)},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "latest1"}, {"id": "latest2"}],
        },
        {"role": "tool", "tool_call_id": "latest1", "content": '{"status":"pending"}'},
        {"role": "tool", "tool_call_id": "latest2", "content": '{"status":"not_run"}'},
    ]
    result = model.budget({"max_tokens": 768, "messages": messages})["messages"]
    ids = {c["id"] for m in result for c in m.get("tool_calls", [])}
    assert ids == {"latest1", "latest2"}
    assert any(m.get("content") == "Relevant saved context" for m in result)
    assert any(
        m.get("content") == "Read a.py; draft pending; tests not run" for m in result
    )
    assert all(not any(k.startswith("_pebble_") for k in m) for m in result)


def json_source(text):
    import json

    return json.dumps({"untrusted_source": text})


def test_large_nested_tool_results_remain_valid_json_with_status():
    import json
    from pebble.context import encode_result

    result = {
        "status": "applied",
        "action_id": "opaque-id",
        "result": {
            "exit_code": 0,
            "output": "a very long line of tool output\n" * 1000,
        },
        "url": "https://example.com/source?exact=yes",
    }
    encoded = encode_result(result)
    assert len(encoded) <= 6500
    restored = json.loads(encoded)
    assert restored["result"]["exit_code"] == 0 and restored["status"] == "applied"
    assert restored["url"] == result["url"] and "context_note" in restored
