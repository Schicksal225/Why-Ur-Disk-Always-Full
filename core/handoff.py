"""Export a Markdown report and a JSON inventory for another agent."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.cleaner import format_bytes
from core.paths import get_data_dir
from core.safety import _system_prefixes

SCHEMA_VERSION = 1

_RULES = """# 给接手 agent 的操作规则

1. 不得删除硬拒绝路径：Windows、Program Files、ProgramData、各盘根目录、System Volume Information、$Recycle.Bin、Recovery、Boot、pagefile.sys、hiberfil.sys、swapfile.sys，以及 junction / 符号链接。
2. 删除前重新核对路径仍然存在、大小没有明显变化，并再看一遍 git 状态。
3. 优先处理 `regenerable` 里的可再生目录（如 node_modules、target、dist）。不要删除项目源码。
4. 删除整个项目之前必须先问用户。
5. 删除用户文件时优先移入回收站，不要直接永久删除。
6. 本文件只包含路径和元数据，不包含任何文件内容。
"""


def safety_rules() -> dict:
    return {
        "never_delete_prefixes": [str(path) for path in _system_prefixes()],
        "never_delete_root_dirs": ["System Volume Information", "$Recycle.Bin", "Recovery", "Boot"],
        "never_delete_files": ["pagefile.sys", "hiberfil.sys", "swapfile.sys"],
        "notes": [
            "重新解析点（junction、symlink）不能删除，扫描时也不跟随。",
            "带系统属性的文件不能删除。",
            "项目源码不能由清理工具直接删除；可再生目录见每个项目的 regenerable 字段。",
        ],
    }


def export_handoff(
    projects: list[dict],
    findings: list[dict],
    drives: list[dict],
    dest_root: Path | None = None,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = (dest_root or (get_data_dir() / "exports")) / f"handoff-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "drives": drives,
        "safety_rules": safety_rules(),
        "projects": projects,
        "findings": [_public_finding(item) for item in findings],
    }
    (folder / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "REPORT.md").write_text(_render_report(inventory), encoding="utf-8")
    return folder


def _public_finding(item: dict) -> dict:
    keys = ("path", "label", "category", "action", "risk", "size", "reason", "suggestion")
    return {key: item.get(key) for key in keys}


def _render_report(inventory: dict) -> str:
    lines = [_RULES.rstrip(), "", "## 磁盘", ""]
    for drive in inventory["drives"]:
        lines.append(
            f"- {drive.get('drive')}: 已用 {drive.get('used_gb')} / {drive.get('total_gb')} GB，剩余 {drive.get('free_gb')} GB"
        )
    lines.extend(["", "## 项目（按可再生空间从大到小）", ""])
    ordered = sorted(inventory["projects"], key=lambda item: sum((item.get("regenerable") or {}).values()), reverse=True)
    if not ordered:
        lines.append("这次盘查没有识别到项目目录。")
    for project in ordered:
        lines.extend(_project_section(project, 3))
    lines.extend(["", "## JSON 字段", ""])
    lines.append("- `projects[].regenerable`：目录名到字节数，这些目录可以重建。")
    lines.append("- `projects[].git.dirty`：true 表示有未提交改动；null 表示没能运行 git。")
    lines.append("- `projects[].risk`：high 时不要删除该项目。")
    lines.append("- `findings[].action`：`cache_clean` 可直接清理，`recycle_suggest` 应进回收站，`advice_only` 和 `readonly` 不要执行。")
    lines.append("")
    return "\n".join(lines)


def _project_section(project: dict, level: int) -> list[str]:
    regenerable = project.get("regenerable") or {}
    regen_bytes = sum(regenerable.values())
    git = project.get("git") or {}
    title = "#" * level
    lines = [
        f"{title} {project.get('name')} ({project.get('project_type')})",
        "",
        f"- 路径：`{project.get('path')}`",
        f"- 大小：{format_bytes(int(project.get('size') or 0))}，文件 {project.get('file_count') or 0} 个",
        f"- 可再生：{format_bytes(regen_bytes)}" + (f"（{', '.join(f'{name} {format_bytes(size)}' for name, size in regenerable.items())}）" if regenerable else ""),
        f"- Git：remote={git.get('remote') or '无'}，最近提交={git.get('last_commit') or '未知'}，未提交={'是' if git.get('dirty') else '否' if git.get('dirty') is False else '未知'}",
        f"- 风险：{project.get('risk')}。{project.get('risk_note')}",
        "",
    ]
    for child in project.get("subprojects") or []:
        lines.extend(_project_section(child, min(level + 1, 6)))
    return lines
