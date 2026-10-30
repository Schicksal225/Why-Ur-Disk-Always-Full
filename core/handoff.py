"""Export a short list of rebuildable directories for another agent.

Only projects on D: and E: are listed. Each entry is one directory (node_modules,
target, ...) with its size, whether the project has uncommitted changes, and how
to rebuild it. The list states facts; the agent decides what to delete.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from core.cleaner import format_bytes
from core.paths import get_data_dir

SCHEMA_VERSION = 2
EXPORT_DRIVES = ("D", "E")
MIN_ENTRY_BYTES = 100 * 1024 * 1024
MAX_ENTRIES = 30

RULES = [
    "只处理本清单列出的目录，不动项目源码，不动清单以外的任何路径。",
    "删除前确认路径仍然存在、目录名是清单里的种类。dirty 为 true 或 null 的项目，先看 git 状态再决定。",
    "删除后项目要先按 restore 恢复才能运行。本文件只有路径和元数据，没有文件内容。",
]

_ENTRY_KEYS = ("path", "bytes", "kind", "project_path", "dirty", "restore")
_DRIVE_KEYS = ("drive", "total_gb", "used_gb", "free_gb")


def _on_export_drive(path: str) -> bool:
    drive = os.path.splitdrive(str(path))[0]
    return drive[:1].upper() in EXPORT_DRIVES


def export_scope_projects(projects: list[dict]) -> list[dict]:
    """Projects that live on an exported drive."""
    return [item for item in projects if _on_export_drive(item.get("path", ""))]


def needs_rescan(projects: list[dict]) -> bool:
    """True when a saved scan predates rebuildable-directory paths.

    Such a project has rebuildable bytes but no `regenerable_dirs` key, so an
    export from it would be empty without saying why.
    """
    stack = export_scope_projects(projects)
    while stack:
        project = stack.pop()
        stack.extend(project.get("subprojects") or [])
        if project.get("regenerable") and "regenerable_dirs" not in project:
            return True
    return False


def restore_command(project_path: str, kind: str) -> str:
    root = Path(project_path)
    if kind == "node_modules":
        if (root / "pnpm-lock.yaml").is_file():
            return "在项目根目录执行 pnpm install"
        if (root / "yarn.lock").is_file():
            return "在项目根目录执行 yarn install"
        if (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
            return "在项目根目录执行 npm ci"
        return "在项目根目录执行 npm install"
    if kind in (".venv", "venv"):
        return "按项目说明重建虚拟环境"
    if kind == "target":
        return "在项目根目录执行 cargo build"
    if kind in ("dist", "build", ".next"):
        return "按该项目的构建命令重新生成"
    if kind == "__pycache__":
        return "Python 会自动重新生成"
    if kind == ".gradle":
        return "在项目根目录执行 gradle build"
    if kind in ("bin", "obj"):
        return "在项目根目录执行 dotnet build"
    return "按项目说明重新生成"


def _candidates(projects: list[dict]) -> list[dict]:
    found: list[dict] = []
    stack = list(projects)
    while stack:
        project = stack.pop()
        stack.extend(project.get("subprojects") or [])
        dirty = (project.get("git") or {}).get("dirty")
        for item in project.get("regenerable_dirs") or []:
            found.append(
                {
                    "path": item["path"],
                    "bytes": int(item.get("bytes") or 0),
                    "kind": item["kind"],
                    "project_path": project["path"],
                    "dirty": dirty,
                }
            )
    found.sort(key=lambda entry: entry["bytes"], reverse=True)
    return found


def build_handoff(projects: list[dict], drives: list[dict]) -> dict:
    found = _candidates(export_scope_projects(projects))
    big = [entry for entry in found if entry["bytes"] >= MIN_ENTRY_BYTES]
    shown = big[:MAX_ENTRIES]
    rest = found[len(shown):]
    items = [{**entry, "restore": restore_command(entry["project_path"], entry["kind"])} for entry in shown]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "drives": [
            {key: drive.get(key) for key in _DRIVE_KEYS}
            for drive in drives
            if str(drive.get("drive", "")).upper() in EXPORT_DRIVES
        ],
        "rules": list(RULES),
        "items": [{key: entry[key] for key in _ENTRY_KEYS} for entry in items],
        "rolled_up": {"count": len(rest), "bytes": sum(entry["bytes"] for entry in rest)},
    }


def export_handoff(projects: list[dict], drives: list[dict], dest_root: Path | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = (dest_root or (get_data_dir() / "exports")) / f"handoff-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    inventory = build_handoff(projects, drives)
    (folder / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
    (folder / "REPORT.md").write_text(_render_report(inventory), encoding="utf-8")
    return folder


def _dirty_text(value) -> str:
    return "是" if value is True else "否" if value is False else "未知"


def _render_report(inventory: dict) -> str:
    lines = ["# 给接手 agent 的目录清单", ""]
    lines.extend(f"{number}. {rule}" for number, rule in enumerate(inventory["rules"], start=1))
    lines.extend(["", "## 磁盘", ""])
    for drive in inventory["drives"]:
        lines.append(f"- {drive.get('drive')}: 已用 {drive.get('used_gb')} / {drive.get('total_gb')} GB，剩余 {drive.get('free_gb')} GB")
    lines.extend(["", "## 项目里的可重建目录（按体积从大到小）", ""])
    items = inventory["items"]
    rolled = inventory["rolled_up"]
    if not items and not rolled["count"]:
        lines.append("这次盘查没有识别到可重建目录。")
    else:
        lines.append("| 体积 | 路径 | 种类 | 未提交改动 | 恢复 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in items:
            lines.append(
                f"| {format_bytes(item['bytes'])} | `{item['path']}` | {item['kind']} | {_dirty_text(item['dirty'])} | {item['restore']} |"
            )
        if rolled["count"]:
            lines.extend(["", f"另有 {rolled['count']} 个更小的目录未列出，合计 {format_bytes(rolled['bytes'])}。"])
    lines.append("")
    return "\n".join(lines)
