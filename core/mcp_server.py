"""Minimal read-only MCP stdio bridge for PC Optimizer.

The bridge speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and exposes
only read-only tools.  stdout is reserved for protocol replies; all logging
goes to stderr and, optionally, to a bounded log file.

Run it through the desktop executable (``PCOptimizer.exe --mcp``) or, during
development, ``python -m core.mcp_server``.
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.mcp_tools import TOOLS, ToolError, call_tool
from core.paths import get_bundle_dir, get_data_dir

# Supported protocol versions, oldest first.  A client asking for one of these
# gets it echoed; anything else is negotiated down to the newest entry.
PROTOCOL_VERSIONS = ("2024-11-05",)
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[-1]

SERVER_NAME = "pcoptimizer"
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)

INSTRUCTIONS = (
    "Read-only access to PC Optimizer audit data: drive capacity, free space "
    "and scan freshness. This server cannot delete, move or modify files."
)

# Keep the on-disk call log small; one line per tools/call is enough.
LOG_MAX_BYTES = 512 * 1024


def _stderr(message: str) -> None:
    """Write a diagnostic line to stderr without ever touching stdout."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        print(message, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def _force_utf8(stream: Any, mode: str, owned: list[io.TextIOWrapper]) -> Any:
    """Wrap a real stdio stream in a UTF-8 text layer.

    The frozen windowed executable reports the console encoding as gbk, which
    would mangle protocol traffic.  Injected streams (tests) are left alone.
    """
    if stream is None:
        return None
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            wrapper = io.TextIOWrapper(
                buffer,
                encoding="utf-8",
                errors="strict",
                newline="\n",
                write_through=mode == "w",
            )
        except (AttributeError, ValueError, OSError):
            pass
        else:
            owned.append(wrapper)
            return wrapper
    return stream


def _prepare_streams(
    stdin: Any, stdout: Any
) -> tuple[Any, Any, list[io.TextIOWrapper]]:
    owned: list[io.TextIOWrapper] = []
    in_stream = stdin
    if in_stream is None:
        in_stream = _force_utf8(sys.stdin, "r", owned)
    out_stream = stdout
    if out_stream is None:
        out_stream = _force_utf8(sys.stdout, "w", owned)
    return in_stream, out_stream, owned


def _release_streams(streams: list[io.TextIOWrapper]) -> None:
    """Flush and detach wrappers so the process-owned buffers stay open."""
    for stream in streams:
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
        try:
            stream.detach()
        except (AttributeError, OSError, ValueError):
            pass


def _server_version() -> str:
    try:
        version = (get_bundle_dir() / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "unknown"
    return version or "unknown"


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _initialize(message: dict) -> dict:
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(message.get("id"), -32602, "invalid params")
    requested = params.get("protocolVersion")
    protocol_version = (
        requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    )
    return _result(
        message.get("id"),
        {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
            "instructions": INSTRUCTIONS,
        },
    )


def _log_call(log_path: Any, tool: str, ok: bool, duration_ms: float) -> None:
    """Log one tools/call.  Never includes a path or client-supplied text."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = (
        f"{timestamp} tool={tool} "
        f"ok={'true' if ok else 'false'} duration_ms={duration_ms}"
    )
    _stderr(line)
    if log_path is not None:
        _append_log_file(log_path, line)


def _append_log_file(log_path: Any, line: str) -> None:
    try:
        path = Path(log_path)
        try:
            if path.stat().st_size >= LOG_MAX_BYTES:
                path.write_text("", encoding="utf-8")
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, TypeError, ValueError):
        # Logging must never take the protocol down.
        pass


def _call(message: dict, log_path: Any) -> dict:
    started = time.perf_counter()
    tool = "<unknown>"
    ok = False
    try:
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(message.get("id"), -32602, "invalid params")
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(name, str) or name not in TOOL_NAMES:
            return _error(message.get("id"), -32602, f"unknown tool: {name}")
        tool = name
        if not isinstance(args, dict):
            return _error(
                message.get("id"), -32602, "invalid params: arguments must be an object"
            )
        try:
            content = call_tool(name, args)
        except ToolError as exc:
            return _result(
                message.get("id"),
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        ok = True
        return _result(
            message.get("id"), {"content": content, "isError": False}
        )
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        _log_call(log_path, tool, ok, duration_ms)


def _dispatch(message: Any, log_path: Any) -> dict | None:
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request")
    if "id" not in message:
        # JSON-RPC notifications (including notifications/initialized) never
        # receive a reply, even when they are malformed.
        return None
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0":
        return _error(request_id, -32600, "invalid request")
    method = message.get("method")
    if not isinstance(method, str):
        return _error(request_id, -32600, "invalid request")
    if method == "initialize":
        return _initialize(message)
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _call(message, log_path)
    return _error(request_id, -32601, f"method not found: {method}")


def _handle_line(text: str, log_path: Any) -> dict | None:
    try:
        message = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return _error(None, -32700, "parse error")
    try:
        return _dispatch(message, log_path)
    except Exception as exc:  # noqa: BLE001 - keep the protocol stream alive
        _stderr(f"pcoptimizer mcp: internal error ({type(exc).__name__})")
        request_id = message.get("id") if isinstance(message, dict) else None
        return _error(request_id, -32603, "internal error")


def serve(stdin=None, stdout=None, log_path=None) -> int:
    """Serve newline-delimited JSON-RPC until stdin reaches EOF.

    Args:
        stdin: Text input; defaults to a UTF-8 wrapper around ``sys.stdin``.
        stdout: Text output; defaults to a UTF-8 wrapper around ``sys.stdout``.
        log_path: Optional call-log file.  ``None`` disables file logging.

    Returns:
        0 at EOF, 2 when no usable stdout/stream exists, 1 on a read failure.
    """
    in_stream, out_stream, owned = _prepare_streams(stdin, stdout)
    try:
        if out_stream is None or not callable(getattr(out_stream, "write", None)):
            _stderr("pcoptimizer mcp: no usable stdout")
            return 2
        if in_stream is not None and not callable(getattr(in_stream, "readline", None)):
            in_stream = None
        while True:
            try:
                line = in_stream.readline() if in_stream is not None else ""
            except (OSError, UnicodeError, ValueError):
                _stderr("pcoptimizer mcp: cannot read stdin")
                return 1
            if line == "":
                return 0
            text = line.strip()
            if not text:
                continue
            reply = _handle_line(text, log_path)
            if reply is None:
                continue
            try:
                payload = json.dumps(reply, ensure_ascii=False)
                out_stream.write(payload + "\n")
                out_stream.flush()
            except (OSError, TypeError, ValueError):
                _stderr("pcoptimizer mcp: cannot write stdout")
                return 2
    finally:
        _release_streams(owned)


if __name__ == "__main__":
    raise SystemExit(serve(log_path=get_data_dir() / "mcp.log"))
