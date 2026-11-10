"""Read-only tools exposed by the PC Optimizer MCP bridge.

Everything published here already exists in the GUI: drive capacity from the OS,
the audit cache written by the scanner (findings, directory tree, projects) and
the export helper used by "hand off to an AI".  Nothing in this module deletes,
moves or modifies files, and no handler accepts a path that is not already part
of a scanned tree.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.advisor import build_drive_advice
from core.cleaner import get_disk_usage
from core.classifier import Finding, classify_scan, findings_from_duplicates
from core.duplicates import find_duplicates
from core.executor import execute_findings, recycle_block_reason
from core.handoff import export_handoff, restore_command
from core.paths import get_data_dir
from core.projects import finalize_projects
from core.safety import deletion_allowed, is_cache_clean_path
from core.scanner import (
    load_audit_cache,
    result_to_dict,
    save_audit_cache,
    scan_path,
)
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


# ---------------------------------------------------------------- 4.5 cleanup_plan
def _finding_from_row(row: dict) -> Finding:
    return Finding(
        id=str(row.get("id") or ""),
        path=str(row.get("path") or ""),
        label=str(row.get("label") or ""),
        category=str(row.get("category") or ""),
        action=str(row.get("action") or ""),
        risk=str(row.get("risk") or ""),
        size=_size(row.get("size")),
        reason=str(row.get("reason") or ""),
        suggestion=str(row.get("suggestion") or ""),
        selectable=bool(row.get("selectable")),
        selected=bool(row.get("selected")),
    )


def _plan_candidates(
    cache: dict, candidate_ids: list[str], protected_paths: list[str]
) -> tuple[list[dict], list[dict]]:
    """Split candidate ids into executable rows and skipped rows with a reason."""
    raw_findings = cache.get("findings")
    rows = (
        [row for row in raw_findings if isinstance(row, dict)]
        if isinstance(raw_findings, list)
        else []
    )
    by_id = {str(row.get("id")): row for row in rows if row.get("id")}
    missing = [candidate for candidate in candidate_ids if candidate not in by_id]
    if missing:
        raise ToolError("candidate not in the audit cache: " + ", ".join(missing))

    executable: list[dict] = []
    skipped: list[dict] = []
    for candidate in candidate_ids:
        row = by_id[candidate]
        label = str(row.get("label") or candidate)
        action = str(row.get("action") or "")
        path = row.get("path")
        if action not in ("cache_clean", "recycle_suggest"):
            skipped.append({"id": candidate, "label": label, "reason": "仅建议项，不能执行"})
            continue
        if not isinstance(path, str) or not path:
            skipped.append({"id": candidate, "label": label, "reason": "没有可执行路径"})
            continue
        allowed, reason = deletion_allowed(path, protected_paths)
        if not allowed:
            skipped.append({"id": candidate, "label": label, "reason": reason or "安全校验拒绝"})
            continue
        if action == "cache_clean" and not is_cache_clean_path(path):
            skipped.append({"id": candidate, "label": label, "reason": "不在可清理的缓存目录内"})
            continue
        executable.append(row)
    return executable, skipped


def _cleanup_plan(args: dict[str, Any]) -> str:
    _reject_unknown(args, {"candidate_ids", "min_age_days"})
    raw_ids = args.get("candidate_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ToolError("candidate_ids must be a non-empty array of finding ids")
    candidate_ids: list[str] = []
    for item in raw_ids:
        if not isinstance(item, str) or not item.strip():
            raise ToolError("candidate_ids must contain non-empty strings")
        if item not in candidate_ids:
            candidate_ids.append(item)
    min_age_days = _int_arg(args, "min_age_days", 1, 0, 30)

    cache = load_audit_cache()
    if not isinstance(cache, dict):
        raise ToolError("no audit cache found; run a disk scan first")
    protected_paths = list(Store().protected_paths)
    rows, skipped = _plan_candidates(cache, candidate_ids, protected_paths)

    steps: list[dict[str, Any]] = []
    if rows:
        findings = [_finding_from_row(row) for row in rows]
        # dry_run keeps execute_findings read-only: no store write, no recycle
        # call and no directory cleanup happen on this path.
        results = execute_findings(
            findings,
            protected_paths,
            dry_run=True,
            store=None,
            min_age_days=min_age_days,
        )
        for row, result in zip(rows, results):
            action = str(row.get("action") or "")
            path = str(row.get("path") or "")
            size = _size(row.get("size"))
            warning: str | None = None
            if action == "cache_clean":
                warning = "直接清理，不能从回收站还原"
            elif action == "recycle_suggest":
                try:
                    blocked = recycle_block_reason(path, size)
                except (OSError, ValueError):
                    blocked = "无法查询回收站状态"
                if blocked:
                    warning = "回收站不可用，确认执行会永久删除：" + blocked
            steps.append(
                {
                    "id": str(row.get("id") or ""),
                    "label": str(row.get("label") or ""),
                    "method": action,
                    "paths": [path] if path else [],
                    "bytes": size,
                    "would_delete": 0,
                    "reason": result.message,
                    "warning": warning,
                }
            )

    stale, scan_age_hours = _freshness(cache)
    plan = {
        "plan_id": "plan-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
        "steps": steps,
        "skipped": skipped,
        "note": "仅演练：本工具不会删除任何文件。执行仍需在 PC Optimizer 界面勾选并确认。",
        "stale": stale,
        "scan_age_hours": scan_age_hours,
    }
    return json.dumps(plan, ensure_ascii=False)


# ---------------------------------------------------------------- 4.6 handoff_export
def _count_export_items(folder: Path) -> int:
    try:
        data = json.loads((folder / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return 0
    items = data.get("items") if isinstance(data, dict) else None
    return len(items) if isinstance(items, list) else 0


def _handoff_export(args: dict[str, Any]) -> str:
    _reject_unknown(args, {"dest"})
    dest = _string_arg(args, "dest", required=False)
    dest_root: Path | None = None
    if dest is not None:
        raw = dest.strip()
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.drive or raw.startswith(("\\\\", "//")):
            raise ToolError("dest must be a relative path under the exports directory")
        exports_root = (get_data_dir() / "exports").resolve()
        resolved = (exports_root / raw).resolve()
        try:
            resolved.relative_to(exports_root)
        except ValueError:
            raise ToolError("dest must stay under the exports directory") from None
        if resolved == exports_root:
            raise ToolError("dest must name a subdirectory, not the exports root")
        dest_root = resolved

    cache = load_audit_cache()
    if not isinstance(cache, dict):
        raise ToolError("no audit cache found; run a disk scan first")
    raw_projects = cache.get("projects")
    projects = [item for item in raw_projects if isinstance(item, dict)] if isinstance(raw_projects, list) else []
    try:
        folder = Path(export_handoff(projects, get_disk_usage(), dest_root=dest_root))
    except OSError as exc:
        raise ToolError(f"export failed: {exc}") from exc

    return json.dumps(
        {
            "folder": str(folder),
            "report": str(folder / "REPORT.md"),
            "inventory": str(folder / "inventory.json"),
            "items": _count_export_items(folder),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- P3 scan jobs
SCAN_TOOL_NAMES = frozenset({"scan_start", "scan_status"})
DEFAULT_SCAN_TIMEOUT = 120
MIN_SCAN_TIMEOUT = 1
MAX_SCAN_TIMEOUT = 300
SCAN_LOCK_MAX_AGE = MAX_SCAN_TIMEOUT + 120


@dataclass
class _ScanJob:
    job_id: str
    drives: list[str]
    duplicates: bool
    timeout_seconds: int
    started: float
    state: str = "running"
    current: str = ""
    message: str = ""
    drives_done: int = 0
    findings: int = 0
    projects: int = 0
    cache_saved: bool = False
    error: str = ""
    timed_out: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)


_SCAN_LOCK = threading.Lock()
_SCAN_JOB: _ScanJob | None = None


def _scan_lock_path() -> Path:
    return get_data_dir() / "mcp_scan.lock"


def _release_scan_lock() -> None:
    try:
        _scan_lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def _finding_objects(rows: list[dict]) -> list[Finding]:
    return [
        Finding(**{key: row.get(key) for key in Finding.__dataclass_fields__ if key in row})
        for row in rows
    ]


def _run_scan_job(job: _ScanJob) -> None:
    """Scan the requested drives in the background; the process stays read-only
    apart from writing the audit cache on success."""
    try:
        results = []
        findings: list[dict] = []
        categories: dict[str, int] = {}
        projects: list[dict] = []
        for index, drive in enumerate(job.drives, 1):
            if job.cancel.is_set():
                break
            job.current = f"{drive}:\\"

            def progress(message: str, drive: str = drive) -> None:
                job.message = f"{drive}:\\ {message}"
                if time.monotonic() - job.started > job.timeout_seconds:
                    job.timed_out = True
                    job.cancel.set()

            result = scan_path(
                f"{drive}:\\",
                cancel=job.cancel,
                progress=progress,
                collect_duplicates=job.duplicates,
            )
            results.append(result)
            findings.extend(asdict(item) for item in classify_scan(result))
            projects.extend(result.projects)
            for name, size in result.categories.items():
                categories[name] = categories.get(name, 0) + int(size)
            if job.duplicates and result.dup_candidates and not job.cancel.is_set():
                findings.extend(
                    asdict(item) for item in findings_from_duplicates(find_duplicates(result.dup_candidates))
                )
            job.drives_done = index
            if job.cancel.is_set():
                break
        if job.cancel.is_set():
            job.state = "timeout" if job.timed_out else "cancelled"
            return
        projects = finalize_projects(projects)
        advice = [
            asdict(item)
            for item in build_drive_advice(get_disk_usage(), _finding_objects(findings), categories)
        ]
        save_audit_cache(
            {
                "results": [result_to_dict(item) for item in results],
                "findings": findings,
                "projects": projects,
                "advice": advice,
                "categories": categories,
            }
        )
        job.findings = len(findings)
        job.projects = len(projects)
        job.cache_saved = True
        _release_scan_lock()
        job.state = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced through scan_status
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        _release_scan_lock()


def _scan_start(args: dict[str, Any]) -> str:
    _reject_unknown(args, {"drives", "duplicates", "timeout_seconds"})
    raw_drives = args.get("drives")
    if not isinstance(raw_drives, list) or not raw_drives:
        raise ToolError("drives must be a non-empty array of drive letters")
    drives: list[str] = []
    for item in raw_drives:
        drive = _drive_arg(item, required=True)
        if drive not in drives:
            drives.append(drive)
    for drive in drives:
        if not Path(f"{drive}:\\").exists():
            raise ToolError(f"drive {drive}: is not available on this machine")
    duplicates = _bool_arg(args, "duplicates", False)
    timeout_seconds = _int_arg(
        args, "timeout_seconds", DEFAULT_SCAN_TIMEOUT, MIN_SCAN_TIMEOUT, MAX_SCAN_TIMEOUT
    )

    global _SCAN_JOB
    with _SCAN_LOCK:
        if _SCAN_JOB is not None and _SCAN_JOB.state == "running":
            raise ToolError(f"a scan is already running: {_SCAN_JOB.job_id}")
        lock = _scan_lock_path()
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"cannot prepare the scan lock: {exc}") from exc
        if lock.exists():
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0.0
            if age > SCAN_LOCK_MAX_AGE:
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ToolError(
                "another PC Optimizer process is already scanning (mcp_scan.lock)"
            ) from None
        except OSError as exc:
            raise ToolError(f"cannot create the scan lock: {exc}") from exc
        job_id = "scan-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            os.write(handle, (job_id + "\n").encode("utf-8"))
        finally:
            os.close(handle)

        job = _ScanJob(
            job_id=job_id,
            drives=drives,
            duplicates=duplicates,
            timeout_seconds=timeout_seconds,
            started=time.monotonic(),
        )
        _SCAN_JOB = job
        thread = threading.Thread(target=_run_scan_job, args=(job,), name="pcopt-mcp-scan", daemon=True)
        thread.start()

    return json.dumps(
        {
            "job_id": job.job_id,
            "state": "running",
            "drives": drives,
            "duplicates": duplicates,
            "timeout_seconds": timeout_seconds,
            "note": "盘查在后台进行，完成后结果写入 scan_cache.json；用 scan_status 轮询。",
        },
        ensure_ascii=False,
    )


def _scan_status(args: dict[str, Any]) -> str:
    _reject_unknown(args, {"job_id"})
    job_id = _string_arg(args, "job_id", required=False)
    with _SCAN_LOCK:
        job = _SCAN_JOB
        if job is None:
            raise ToolError("no scan has been started in this MCP session")
        if job_id is not None and job_id != job.job_id:
            raise ToolError(f"unknown job_id: {job_id}")
        drives_total = len(job.drives) or 1
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "state": job.state,
            "drives": list(job.drives),
            "duplicates": job.duplicates,
            "timeout_seconds": job.timeout_seconds,
            "elapsed_seconds": round(time.monotonic() - job.started, 1),
            "drives_done": job.drives_done,
            "progress_percent": round(job.drives_done * 100 / drives_total),
            "current": job.current,
            "message": job.message,
            "findings": job.findings,
            "projects": job.projects,
            "cache_saved": job.cache_saved,
        }
        if job.error:
            payload["error"] = job.error
        if job.state in ("done", "timeout", "cancelled", "error"):
            payload["note"] = "任务已结束；findings_query 可用 cache_saved=true 的新缓存。"
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- registry
_HANDLERS: dict[str, Any] = {
    "drives_list": _drives_list,
    "findings_query": _findings_query,
    "dir_children": _dir_children,
    "projects_list": _projects_list,
    "cleanup_plan": _cleanup_plan,
    "handoff_export": _handoff_export,
    "scan_start": _scan_start,
    "scan_status": _scan_status,
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
    {
        "name": "cleanup_plan",
        "description": (
            "Dry-run plan for candidate findings from the last audit scan. It "
            "explains what each cleanup would do (cache clean or recycle) and "
            "predicts whether the recycle bin would permanently delete instead. "
            "Nothing is deleted, moved or written: the user still has to select "
            "and confirm the items in the PC Optimizer window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Finding ids to plan, as returned by findings_query.",
                },
                "min_age_days": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 30,
                    "default": 1,
                    "description": "Age filter applied to temporary-file cleanup.",
                },
            },
            "required": ["candidate_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "handoff_export",
        "description": (
            "Write the same 'hand off to an AI' inventory the GUI exports: project "
            "rebuildable directories with restore commands, under the app's "
            "exports directory. dest may name a relative subdirectory of exports. "
            "It only writes inventory.json/REPORT.md there; it never deletes or "
            "modifies scanned files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dest": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Relative subdirectory under exports (optional).",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "scan_start",
        "description": (
            "Start a background disk scan (the same scan the GUI runs) and write "
            "the result to the audit cache. Only available when the server is "
            "started with --mcp-allow-scan. One scan at a time; timeout_seconds "
            "cancels a long scan and returns partial state through scan_status. "
            "It never deletes or modifies scanned files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "drives": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[A-Za-z]$"},
                    "minItems": 1,
                    "description": "Drive letters to scan.",
                },
                "duplicates": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also look for duplicate files (slower).",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": MIN_SCAN_TIMEOUT,
                    "maximum": MAX_SCAN_TIMEOUT,
                    "default": DEFAULT_SCAN_TIMEOUT,
                },
            },
            "required": ["drives"],
            "additionalProperties": False,
        },
    },
    {
        "name": "scan_status",
        "description": (
            "Poll the background scan started by scan_start: state, progress, "
            "current path, findings/project counts and whether the audit cache "
            "was updated. Only available when the server is started with "
            "--mcp-allow-scan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job id; defaults to the most recent scan.",
                }
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
