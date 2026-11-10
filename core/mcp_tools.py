"""Read-only tools exposed by the PC Optimizer MCP bridge.

The MCP server only publishes facts that already exist in the GUI: drive
capacity from the OS and the audit cache written by the scanner.  Nothing in
this module deletes, moves or modifies files, and no handler accepts a path
from the client.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.cleaner import get_disk_usage
from core.scanner import load_audit_cache

# A cached scan older than this is reported as stale so agents can suggest a
# fresh scan instead of acting on old numbers.
STALE_AFTER_HOURS = 24


class ToolError(Exception):
    """A tool-level failure returned to the client inside an isError result."""


def _parse_scanned_at(value: object) -> datetime | None:
    """Parse the cache's ISO-8601 timestamp, tolerating unsupported input."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Python 3.10's fromisoformat does not accept the UTC "Z" suffix.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _root_matches(root: str, drive: str) -> bool:
    """True when a scan root belongs to a drive letter, case-insensitively."""
    letter = drive.strip().rstrip(":\\/").upper()
    if not letter:
        return False
    normalized = root.strip().replace("/", "\\").upper()
    return normalized.startswith(letter + ":")


def _scan_entries(cache: dict | None) -> list[dict]:
    """Return {root, scanned_at} scan records from the audit cache.

    The documented cache shape keeps them under ``scans``.  The cache written
    by the current GUI stores the same two fields on every entry of
    ``results``, so that shape is accepted too; without the fallback no drive
    would ever report a scan timestamp.
    """
    if not isinstance(cache, dict):
        return []
    scans = cache.get("scans")
    if isinstance(scans, list):
        entries = [item for item in scans if isinstance(item, dict)]
        if entries:
            return entries
    results = cache.get("results")
    if isinstance(results, list):
        return [
            {"root": item.get("root"), "scanned_at": item.get("scanned_at")}
            for item in results
            if isinstance(item, dict)
        ]
    return []


def _latest_scan(scans: list[dict], drive: str) -> tuple[datetime, str] | None:
    """Latest parseable scan for a drive, as (datetime, original string)."""
    best_stamp: float | None = None
    best: tuple[datetime, str] | None = None
    for entry in scans:
        root = entry.get("root")
        if not isinstance(root, str) or not _root_matches(root, drive):
            continue
        raw = entry.get("scanned_at")
        parsed = _parse_scanned_at(raw)
        if parsed is None or not isinstance(raw, str):
            continue
        try:
            stamp = parsed.timestamp()
        except (OSError, OverflowError, ValueError):
            continue
        if best_stamp is None or stamp > best_stamp:
            best_stamp = stamp
            best = (parsed, raw)
    return best


def _drives_list(args: dict[str, Any]) -> str:
    if args:
        raise ToolError(f"unknown argument: {sorted(args)[0]}")

    scans = _scan_entries(load_audit_cache())
    drives: list[dict[str, Any]] = []
    for usage in get_disk_usage():
        drive = str(usage.get("drive", ""))
        latest = _latest_scan(scans, drive)
        scanned_at: str | None = None
        scan_age_hours: float | None = None
        age_hours: float | None = None
        if latest is not None:
            scanned, scanned_at = latest
            reference = datetime.now(scanned.tzinfo) if scanned.tzinfo else datetime.now()
            age_hours = (reference - scanned).total_seconds() / 3600.0
            scan_age_hours = round(age_hours, 1)
        drives.append(
            {
                "drive": drive,
                "total_gb": usage.get("total_gb"),
                "used_gb": usage.get("used_gb"),
                "free_gb": usage.get("free_gb"),
                "percent": usage.get("percent"),
                "scanned_at": scanned_at,
                "scan_age_hours": scan_age_hours,
                "stale": age_hours is None or age_hours > STALE_AFTER_HOURS,
            }
        )
    return json.dumps({"drives": drives}, ensure_ascii=False)


_HANDLERS: dict[str, Any] = {"drives_list": _drives_list}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "drives_list",
        "description": (
            "Read-only drive capacity and free space from the OS, plus the "
            "timestamp, age and staleness of the last PC Optimizer audit scan "
            "for each drive. Does not scan, delete, move or modify files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
]


def call_tool(name: str, args: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Run a read-only tool and return MCP text content blocks.

    Raises:
        ToolError: for an unknown tool or invalid/unknown arguments.  The
            server turns this into an ``isError`` tool result, not a JSON-RPC
            error.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    text = handler(args)
    return [{"type": "text", "text": text}]
