"""Protocol-level tests for the read-only MCP stdio bridge.

The server is driven through in-memory streams only: no subprocesses, no
network and no temporary directories.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from core.mcp_server import serve
from core.paths import get_bundle_dir


def _request(method: str, params: dict | None = None, request_id=1) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _run_raw(raw: str, log_path=None) -> tuple[int, list[dict], str]:
    stdin = io.StringIO(raw)
    stdout = io.StringIO()
    code = serve(stdin=stdin, stdout=stdout, log_path=log_path)
    text = stdout.getvalue()
    replies = [json.loads(line) for line in text.splitlines()]
    return code, replies, text


def _run(messages: list[dict], log_path=None) -> tuple[int, list[dict], str]:
    raw = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
    return _run_raw(raw, log_path=log_path)


def test_initialize_echoes_supported_protocol_version():
    request = _request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    code, replies, _ = _run([request])

    assert code == 0
    assert len(replies) == 1
    result = replies[0]["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "pcoptimizer"
    expected_version = (get_bundle_dir() / "VERSION").read_text(encoding="utf-8").strip()
    assert result["serverInfo"]["version"] == expected_version
    assert "read-only" in result["instructions"].lower()


def test_initialize_newer_version_falls_back_to_baseline():
    request = _request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    _, replies, _ = _run([request])

    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"


def test_initialize_without_version_falls_back_to_baseline():
    _, replies, _ = _run([_request("initialize", {"capabilities": {}})])

    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"


def test_ping_returns_empty_result():
    _, replies, _ = _run([_request("ping", request_id="ping-1")])

    assert replies == [{"jsonrpc": "2.0", "id": "ping-1", "result": {}}]


def test_tools_list_contains_drives_list_with_valid_schema():
    _, replies, _ = _run([_request("tools/list")])

    tools = replies[0]["result"]["tools"]
    assert any(tool["name"] == "drives_list" for tool in tools)
    drive_tool = next(tool for tool in tools if tool["name"] == "drives_list")
    assert drive_tool["description"].startswith("Read-only")
    assert drive_tool["inputSchema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_tools_call_drives_list_returns_parseable_json():
    request = _request(
        "tools/call", {"name": "drives_list", "arguments": {}}
    )
    _, replies, _ = _run([request])

    result = replies[0]["result"]
    assert result["isError"] is False
    content = result["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert set(payload) == {"drives"}
    assert isinstance(payload["drives"], list)
    for drive in payload["drives"]:
        assert set(drive) == {
            "drive",
            "total_gb",
            "used_gb",
            "free_gb",
            "percent",
            "scanned_at",
            "scan_age_hours",
            "stale",
        }


def test_unknown_method_returns_method_not_found_with_same_id():
    _, replies, _ = _run([_request("resources/list", request_id=7)])

    assert replies[0]["id"] == 7
    assert "result" not in replies[0]
    assert replies[0]["error"]["code"] == -32601


def test_malformed_json_returns_parse_error_with_null_id():
    _, replies, _ = _run_raw('{"jsonrpc": "2.0", "id": 1,\n')

    assert replies[0]["jsonrpc"] == "2.0"
    assert replies[0]["id"] is None
    assert replies[0]["error"]["code"] == -32700


def test_notification_gets_no_reply():
    raw = (
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }
        )
        + "\n"
    )
    code, replies, text = _run_raw(raw)

    assert code == 0
    assert replies == []
    assert text == ""


def test_unknown_tool_is_invalid_params():
    request = _request(
        "tools/call", {"name": "delete_everything", "arguments": {}}, request_id=9
    )
    _, replies, _ = _run([request])

    assert replies[0]["id"] == 9
    assert "result" not in replies[0]
    assert replies[0]["error"]["code"] == -32602


def test_unknown_argument_is_a_tool_error():
    request = _request(
        "tools/call", {"name": "drives_list", "arguments": {"drive": "C"}}
    )
    _, replies, _ = _run([request])

    result = replies[0]["result"]
    assert result["isError"] is True
    assert "unknown argument" in result["content"][0]["text"]


def test_stdout_contains_only_jsonrpc_lines():
    valid = [
        _request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        ),
        _request("ping", request_id=2),
        _request("tools/list", request_id=3),
        _request("tools/call", {"name": "drives_list", "arguments": {}}, request_id=4),
        _request("resources/list", request_id=5),
    ]
    raw = "".join(json.dumps(message) + "\n" for message in valid) + "not json\n"
    code, replies, text = _run_raw(raw)

    lines = text.splitlines()
    assert code == 0
    assert len(lines) == 6
    assert len(replies) == 6
    for line in lines:
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"


def test_invalid_request_returns_invalid_request():
    _, replies, _ = _run([{"jsonrpc": "2.0", "id": 11}])

    assert replies[0]["id"] == 11
    assert replies[0]["error"]["code"] == -32600
    assert "result" not in replies[0]


def test_log_path_none_creates_no_file(monkeypatch):
    # Isolate the tool from real caches so the only possible file access while
    # serving is the optional call log.
    monkeypatch.setattr("core.mcp_tools.load_audit_cache", lambda: None)
    monkeypatch.setattr("core.mcp_tools.get_disk_usage", lambda: [])
    opened: list[str] = []

    def fail_open(path, *args, **kwargs):
        opened.append(str(path))
        raise AssertionError("log_path=None must not open files")

    monkeypatch.setattr(Path, "open", fail_open)
    request = _request("tools/call", {"name": "drives_list", "arguments": {}})
    code, replies, _ = _run([request])

    assert code == 0
    assert opened == []
    assert replies[0]["result"]["isError"] is False


def test_internal_error_returns_internal_error(monkeypatch):
    def broken_tool(name, args):
        raise RuntimeError("boom")

    monkeypatch.setattr("core.mcp_server.call_tool", broken_tool)
    request = _request("tools/call", {"name": "drives_list", "arguments": {}}, 13)
    _, replies, _ = _run([request])

    assert replies[0]["id"] == 13
    assert replies[0]["error"]["code"] == -32603
    assert "result" not in replies[0]


def test_missing_stdout_returns_2_without_raising(monkeypatch):
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", errors)

    code = serve(stdin=io.StringIO(""), stdout=None, log_path=None)

    assert code == 2
    assert "stdout" in errors.getvalue()

