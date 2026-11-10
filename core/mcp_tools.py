"""Read-only tools exposed by the PC Optimizer MCP bridge.

Everything published here already exists in the GUI: drive capacity from the OS,
the audit cache written by the scanner (findings, directory tree, projects) and
the export helper used by "hand off to an AI".  Nothing in this module deletes,
moves or modifies files, and no handler accepts a path that is not already part
of a scanned tree.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from core.cleaner import get_disk_usage
from core.handoff import restore_command
from core.safety import deletion_allowed, is_cache_clean_path
from core.scanner import load_audit_cache
from core.store import Store

# A cached scan older than this is reported as stale so agents can suggest a
# fresh scan instead of acting on old numbers.
STALE_AFTER_HOURS = 24

MB = 1024**2

FINDING_ACTIONS = ("cache_clean", "recycle_suggest", "advice_only", "readonly")
FINDING_FIELDS = (
    "id",
    "label",
    "action",
    "risk",
    "size",
    "path",
    "reason",
    "suggestion",
    "selectable",
    "selected",
)

PROJECT_SORTS = ("regen", "size", "idle")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# The stored tree is capped at 40 children per level and depth 4 (~66k nodes).
MAX_NODES = 200_000
MAX_STRING = 32768
MAX_SUBPROJECT_DEPTH = 3


class ToolError(Exception):
    """A tool-level failure returned to the client inside an isError result."""


# ---------------------------------------------------------------- shared helpers
def _reject_unknown(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ToolError(f"unknown argument: {unknown[0]}")


def _int_arg(
    args: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be an integer")
    if value < minimum:
        raise ToolError(f"{key} must be >= {minimum}")
    if maximum is not None:
        value = min(value, maximum)
    return value


def _number_arg(
    args: dict[str, Any], key: str, default: float, minimum: float
) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{key} must be a number")
    if value < minimum:
        raise ToolError(f"{key} must be >= {minimum}")
    return float(value)


def _bool_arg(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be a boolean")
    return value


def _drive_arg(value: object, required: bool) -> str | None:
    if value is None:
        if required:
            raise ToolError("drive is required")
        return None
    if not isinstance(value, str):
        raise ToolError("drive must be a single drive letter")
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z]", text):
        raise ToolError("drive must be a single drive letter")
    return text.upper()


def _string_arg(args: dict[str, Any], key: str, required: bool) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"{key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} must be a non-empty string")
    if len(value) > MAX_STRING:
        raise ToolError(f"{key} is too long")
    return value


def _size(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _path_on_drive(path: object, drive: str | None) -> bool:
    if not drive:
        return True
    if not isinstance(path, str):
        return False
    normalized = path.replace("/", "\\").lstrip()
    return normalized.upper().startswith(drive.upper() + ":")


def _norm_path(path: object) -> str:
    if not isinstance(path, str):
        return ""
    return path.replace("/", "\\").rstrip("\\").lower()


# ---------------------------------------------------------------- cache helpers
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


def _root_matches(root: object, drive: str) -> bool:
    """True when a scan root belongs to a drive letter, case-insensitively."""
    if not isinstance(root, str):
        return False
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


def _freshness(cache: dict | None, drive: str | None = None) -> tuple[bool, float | None]:
    """(stale, scan_age_hours) for one drive or for the whole cache."""
    stamps: list[float] = []
    for entry in _scan_entries(cache):
        root = entry.get("root")
        if drive is not None and not _root_matches(root, drive):
            continue
        parsed = _parse_scanned_at(entry.get("scanned_at"))
        if parsed is None:
            continue
        try:
            stamps.append(parsed.timestamp())
        except (OSError, OverflowError, ValueError):
            continue
    if not stamps:
        return True, None
    age_hours = (time.time() - max(stamps)) / 3600.0
    return age_hours > STALE_AFTER_HOURS, round(age_hours, 1)


# ---------------------------------------------------------------- 4.1 drives_list
def _drives_list(args: dict[str, Any]) -> str:
    if args:
        raise ToolError(f"unknown argument: {sorted(args)[0]}")

    cache = load_audit_cache()
    scans = _scan_entries(cache)
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


# ---------------------------------------------------------------- 4.2 findings_query
def _current_selectable(item: dict, protected_paths: list[str]) -> bool:
    """Recompute selectability from the safety gate instead of trusting the cache."""
    if not item.get("selectable"):
        return False
    path = item.get("path")
    if not isinstance(path, str) or not path:
        return False
    allowed, _reason = deletion_allowed(path, protected_paths)
    if not allowed:
        return False
    if item.get("action") == "cache_clean":
        return bool(is_cache_clean_path(path))
    return True


def _findings_query(args: dict[str, Any]) -> str:
    _reject_unknown(
        args, {"action", "min_size_mb", "drive", "include_locked", "limit", "offset"}
    )
    action = args.get("action")
    if action is not None and action not in FINDING_ACTIONS:
        raise ToolError("action must be one of: " + ", ".join(FINDING_ACTIONS))
    min_size_bytes = int(_number_arg(args, "min_size_mb", 0.0, 0.0) * MB)
    drive = _drive_arg(args.get("drive"), required=False)
    include_locked = _bool_arg(args, "include_locked", False)
    limit = _int_arg(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _int_arg(args, "offset", 0, 0)

    cache = load_audit_cache()
    findings = cache.get("findings") if isinstance(cache, dict) else None
    protected_paths = list(Store().protected_paths)

    filtered: list[dict[str, Any]] = []
    for item in findings if isinstance(findings, list) else []:
        if not isinstance(item, dict):
            continue
        if action is not None and item.get("action") != action:
            continue
        if not _path_on_drive(item.get("path"), drive):
            continue
        if _size(item.get("size")) < min_size_bytes:
            continue
        if not include_locked and not item.get("selectable"):
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: _size(item.get("size")), reverse=True)

    total = len(filtered)
    page = filtered[offset : offset + limit]
    stale, scan_age_hours = _freshness(cache, drive)
    payload = {
        "findings": [
            {field: item.get(field) for field in FINDING_FIELDS} for item in page
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
        "stale": stale,
        "scan_age_hours": scan_age_hours,
    }
    for row in payload["findings"]:
        row["selectable"] = _current_selectable(row, protected_paths)
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- 4.3 dir_children
def _find_node(tree: object, target: str) -> dict | None:
    if not isinstance(tree, dict):
        return None
    stack: list[dict] = [tree]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > MAX_NODES:
            return None
        if _norm_path(node.get("path")) == target:
            return node
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return None


def _project_node(node: dict, levels: int) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": node.get("name"),
        "path": node.get("path"),
        "size": _size(node.get("size")),
        "protected": node.get("protected") or "",
        "children": [],
    }
    if levels > 0:
        children = node.get("children")
        rows = [child for child in children if isinstance(child, dict)] if isinstance(children, list) else []
        rows.sort(key=lambda child: _size(child.get("size")), reverse=True)
        item["children"] = [_project_node(child, levels - 1) for child in rows]
    return item


def _dir_children(args: dict[str, Any]) -> str:
    _reject_unknown(args, {"drive", "path", "depth"})
    drive = _drive_arg(args.get("drive"), required=True)
    path = _string_arg(args, "path", required=True)
    depth = _int_arg(args, "depth", 1, 1, 2)

    cache = load_audit_cache()
    results = cache.get("results") if isinstance(cache, dict) else None
    match: dict | None = None
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        if _root_matches(item.get("root"), drive or ""):
            match = item
            break
    if match is None:
        raise ToolError(f"drive {drive}: has not been scanned; run a disk scan first")

    node = _find_node(match.get("tree"), _norm_path(path))
    if node is None:
        raise ToolError("path is outside the scanned tree for this drive")
    return json.dumps({"node": _project_node(node, depth)}, ensure_ascii=False)


# ---------------------------------------------------------------- 4.4 projects_list
def _idle_days(project: dict, now: float) -> int | None:
    mtime = project.get("source_mtime")
    if isinstance(mtime, bool) or not isinstance(mtime, (int, float)) or mtime <= 0:
        return None
    return int((now - float(mtime)) / 86400.0)


def _collect_regen_dirs(
    project_path: str, project: dict, depth: int = 0
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    entries = project.get("regenerable_dirs")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        kind = entry.get("kind")
        kind_text = kind if isinstance(kind, str) else ""
        rows.append(
            {
                "path": path,
                "kind": kind_text,
                "bytes": _size(entry.get("bytes")),
                "restore": restore_command(project_path, kind_text),
            }
        )
    subprojects = project.get("subprojects")
    if depth < MAX_SUBPROJECT_DEPTH:
        for sub in subprojects if isinstance(subprojects, list) else []:
            if not isinstance(sub, dict):
                continue
            sub_path = sub.get("path")
            if not isinstance(sub_path, str) or not sub_path:
                sub_path = project_path
            rows.extend(_collect_regen_dirs(sub_path, sub, depth + 1))
    return rows


def _regen_bytes(project: dict) -> int:
    regenerable = project.get("regenerable")
    if not isinstance(regenerable, dict):
        return 0
    return sum(_size(value) for value in regenerable.values())


def _projects_list(args: dict[str, Any]) -> str:
    _reject_unknown(args, {"sort", "min_regen_mb"})
    sort = args.get("sort", "regen")
    if sort not in PROJECT_SORTS:
        raise ToolError("sort must be one of: " + ", ".join(PROJECT_SORTS))
    min_regen_bytes = int(_number_arg(args, "min_regen_mb", 0.0, 0.0) * MB)

    cache = load_audit_cache()
    projects = cache.get("projects") if isinstance(cache, dict) else None
    now = time.time()
    rows: list[dict[str, Any]] = []
    for project in projects if isinstance(projects, list) else []:
        if not isinstance(project, dict):
            continue
        regen_total = _regen_bytes(project)
        if regen_total < min_regen_bytes:
            continue
        path = project.get("path") if isinstance(project.get("path"), str) else ""
        git = project.get("git") if isinstance(project.get("git"), dict) else {}
        rows.append(
            {
                "name": project.get("name"),
                "path": path,
                "project_type": project.get("project_type"),
                "size": _size(project.get("size")),
                "regen_bytes": regen_total,
                "regenerable_dirs": _collect_regen_dirs(path, project),
                "git": {
                    "remote": git.get("remote") or "",
                    "last_commit": git.get("last_commit") or "",
                    "dirty": git.get("dirty"),
                },
                "risk": project.get("risk"),
                "risk_note": project.get("risk_note") or "",
                "_idle": _idle_days(project, now),
            }
        )

    if sort == "size":
        rows.sort(key=lambda row: row["size"], reverse=True)
    elif sort == "idle":
        rows.sort(key=lambda row: (row["_idle"] is None, -(row["_idle"] or 0)))
    else:
        rows.sort(key=lambda row: row["regen_bytes"], reverse=True)
    for row in rows:
        del row["_idle"]

    stale, scan_age_hours = _freshness(cache)
    payload = {
        "projects": rows,
        "total": len(rows),
        "stale": stale,
        "scan_age_hours": scan_age_hours,
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- registry
_HANDLERS: dict[str, Any] = {
    "drives_list": _drives_list,
    "findings_query": _findings_query,
    "dir_children": _dir_children,
    "projects_list": _projects_list,
}

_ADDITIONAL_PROPERTIES_FALSE = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "drives_list",
        "description": (
            "Read-only drive capacity and free space from the OS, plus the "
            "timestamp, age and staleness of the last PC Optimizer audit scan "
            "for each drive. Does not scan, delete, move or modify files."
        ),
        "inputSchema": _ADDITIONAL_PROPERTIES_FALSE,
    },
    {
        "name": "findings_query",
        "description": (
            "Read-only query over the findings of the last audit scan, with the "
            "same fields the GUI shows (id/label/action/risk/size/path/reason/"
            "suggestion/selectable/selected). Filter by action, minimum size, "
            "drive or locked rows, paginate with limit/offset, and check the "
            "cache freshness. Returns no file contents and cannot delete anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(FINDING_ACTIONS),
                    "description": "Only findings with this action.",
                },
                "min_size_mb": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Skip findings smaller than this many MiB.",
                },
                "drive": {
                    "type": "string",
                    "pattern": "^[A-Za-z]$",
                    "description": "Only findings whose path is on this drive.",
                },
                "include_locked": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include non-selectable advice-only/readonly rows.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dir_children",
        "description": (
            "Read-only children of a directory node from the last audit scan's "
            "stored tree (max 40 children per level, stored to depth 4). depth "
            "selects how many levels below the node to return and is capped at "
            "2. Paths outside the scanned tree for that drive are rejected. "
            "Does not read file contents or modify anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "drive": {
                    "type": "string",
                    "pattern": "^[A-Za-z]$",
                    "description": "Drive letter the path belongs to.",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_STRING,
                    "description": "Directory path inside the scanned tree, e.g. C:\\Users\\A.",
                },
                "depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2,
                    "default": 1,
                },
            },
            "required": ["drive", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "projects_list",
        "description": (
            "Read-only projects from the last audit scan with their rebuildable "
            "directories (node_modules, target, .venv, ...), the command that "
            "recreates each directory, git metadata and risk note. sort is "
            "regen (default), size or idle; min_regen_mb drops projects whose "
            "own rebuildable bytes are below the threshold. Does not delete or "
            "modify anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort": {
                    "type": "string",
                    "enum": list(PROJECT_SORTS),
                    "default": "regen",
                },
                "min_regen_mb": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Minimum own rebuildable bytes in MiB.",
                },
            },
            "additionalProperties": False,
        },
    },
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
