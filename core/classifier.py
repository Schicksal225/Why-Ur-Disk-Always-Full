"""Turn a scan into explained findings. Rules live in this table, not in the UI."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from core.duplicates import DuplicateGroup
from core.safety import (
    cache_clean_label,
    deletion_allowed,
    get_advice_only_locations,
    is_cache_clean_path,
    is_inside_project,
    system_block_reason,
)
from core.scanner import ScanResult

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv"}
INSTALLER_EXT = {".exe", ".msi", ".zip", ".rar", ".7z"}
IMAGE_EXT = {".iso", ".vhdx", ".vhd", ".vmdk"}
LARGE_VIDEO_BYTES = 500 * 1024 * 1024
OLD_INSTALLER_SECONDS = 90 * 86400
OLD_INSTALLER_MIN_BYTES = 20 * 1024 * 1024

_ACTION_RANK = {
    "readonly": 0,
    "advice_only": 1,
    "recycle_suggest": 2,
    "cache_clean": 3,
}


@dataclass
class Finding:
    id: str
    path: str
    label: str
    category: str
    action: str
    risk: str
    size: int
    reason: str
    suggestion: str
    selectable: bool
    selected: bool = False


def _finding(
    path: str,
    label: str,
    category: str,
    action: str,
    risk: str,
    size: int,
    reason: str,
    suggestion: str,
    *,
    selected: bool = False,
) -> Finding:
    selectable = action in ("cache_clean", "recycle_suggest")
    return Finding(
        id=f"{action}:{path}",
        path=path,
        label=label,
        category=category,
        action=action,
        risk=risk,
        size=size,
        reason=reason,
        suggestion=suggestion,
        selectable=selectable,
        selected=selected if selectable else False,
    )


def _is_downloads(path: str) -> bool:
    parts = [part.lower() for part in Path(path).parts]
    return "downloads" in parts


def _is_chat(path: str) -> str | None:
    folded = os.path.normcase(path)
    if "wechat files" in folded or "xwechat_files" in folded:
        return "微信"
    if "tencent files" in folded:
        return "QQ"
    return None


def classify_file(path: str, size: int, mtime: float, inside_project: bool = False) -> Finding | None:
    """Classify one file. Earlier rules win over later ones."""
    name = Path(path).name
    if system_block_reason(path):
        return _finding(
            path,
            name,
            "系统",
            "readonly",
            "high",
            size,
            "位于系统目录，本工具只统计、不清理。",
            "系统文件请用 Windows「存储感知」或「磁盘清理」处理。",
        )
    if inside_project or is_inside_project(path):
        return _finding(
            path,
            name,
            "项目",
            "readonly",
            "high",
            size,
            "位于项目目录（含 .git、package.json、pyproject.toml 或解决方案文件），只统计不清理。",
            "请用项目自己的清理方式（例如构建输出目录）处理，不要整项目删除。",
        )
    chat = _is_chat(path)
    if chat:
        return _finding(
            path,
            name,
            "聊天文件",
            "advice_only",
            "medium",
            size,
            f"这是{chat}接收或缓存的文件，可能包含聊天记录或重要资料，本工具不删除。",
            f"请在{chat}里管理文件，确认不需要后再手动删除。",
        )
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return _finding(
            path,
            name,
            "镜像",
            "advice_only",
            "medium",
            size,
            "虚拟机磁盘或光盘镜像。直接删除可能导致虚拟机无法启动。",
            "确认没有虚拟机或安装介质仍在使用后，再自行删除或移走。",
        )
    if ext in INSTALLER_EXT and _is_downloads(path) and size >= OLD_INSTALLER_MIN_BYTES and (time.time() - mtime) >= OLD_INSTALLER_SECONDS:
        return _finding(
            path,
            name,
            "安装包",
            "recycle_suggest",
            "medium",
            size,
            "下载目录里超过 90 天的安装包或压缩包，通常是安装完成后遗留的文件。",
            "可移入回收站。若还要留作离线安装包，请取消勾选。",
        )
    if ext in VIDEO_EXT and size >= LARGE_VIDEO_BYTES:
        return _finding(
            path,
            name,
            "视频",
            "advice_only",
            "low",
            size,
            "大体积视频。这是个人文件，本工具不自动删除。",
            "确认不再需要后，用资源管理器删除或挪到其他盘。",
        )
    return None


def _system_dir_finding(path: str, name: str, size: int) -> Finding:
    suggestion = "系统目录只统计容量，不会进入删除。"
    if name.lower().startswith("program files"):
        suggestion = "已安装的软件请到「设置 → 应用 → 安装的应用」卸载，不要直接删除程序目录。"
    return _finding(path, name, "系统", "readonly", "high", size, "系统或程序目录，本工具不删除。", suggestion)


def _project_dir_finding(path: str, name: str, size: int) -> Finding:
    return _finding(
        path,
        name or path,
        "项目",
        "readonly",
        "high",
        size,
        "识别为项目目录，整棵目录只统计不清理。",
        "构建缓存如果要清，请在项目里自行处理。",
    )


def _advice_copy(label: str) -> tuple[str, str]:
    for _path, known, reason, suggestion in get_advice_only_locations():
        if known == label:
            return reason, suggestion
    if "微信" in label or "QQ" in label:
        return (
            "聊天软件接收的文件可能包含重要资料，本工具不删除。",
            "请在微信或 QQ 中清理聊天文件。",
        )
    return (f"{label} 仅供参考。", "请自行确认后再处理。")


def classify_scan(result: ScanResult) -> list[Finding]:
    found: dict[str, Finding] = {}

    def add(item: Finding | None) -> None:
        if item is None:
            return
        current = found.get(item.path)
        if current is None or _ACTION_RANK[item.action] < _ACTION_RANK[current.action]:
            found[item.path] = item

    if result.tree is not None:
        for child in result.tree.children:
            if child.protected == "system" or system_block_reason(child.path):
                add(_system_dir_finding(child.path, child.name, child.size))
            elif child.protected == "project" or is_inside_project(child.path):
                add(_project_dir_finding(child.path, child.name, child.size))

    for raw in result.cache_dirs:
        path, size = raw[0], int(raw[1])
        label = raw[2] if len(raw) > 2 else cache_clean_label(path)
        if system_block_reason(path) or not is_cache_clean_path(path):
            add(
                _finding(
                    path,
                    label,
                    "缓存",
                    "advice_only",
                    "low",
                    size,
                    "路径不在可直接清理的缓存白名单内。",
                    "请确认后再手动处理。",
                )
            )
            continue
        add(
            _finding(
                path,
                str(label),
                "缓存",
                "cache_clean",
                "low",
                size,
                "可再生缓存，清理后软件会重新生成，不影响文档和项目。",
                "可直接清理。占用中的文件会被跳过。",
                selected=True,
            )
        )

    for raw in result.advice_dirs:
        path, size, label = raw[0], int(raw[1]), str(raw[2])
        reason, suggestion = _advice_copy(label)
        add(_finding(path, label, "建议", "advice_only", "medium", size, reason, suggestion))

    seen_records: set[str] = set()
    for record in list(result.large_files) + list(result.stale_files):
        if record.path in seen_records:
            continue
        seen_records.add(record.path)
        add(classify_file(record.path, record.size, record.mtime, record.inside_project))

    for path in result.empty_dirs:
        allowed, _reason = deletion_allowed(path)
        if not allowed:
            continue
        add(
            _finding(
                path,
                Path(path).name or path,
                "空文件夹",
                "recycle_suggest",
                "low",
                0,
                "目录是空的，移入回收站不影响文件。",
                "默认不勾选。确认没有程序依赖这个空目录后再处理。",
                selected=False,
            )
        )
    return list(found.values())


def findings_from_duplicates(groups: list[DuplicateGroup]) -> list[Finding]:
    findings: list[Finding] = []
    for group in groups:
        for path in group.remove:
            allowed, _reason = deletion_allowed(path)
            if not allowed:
                continue
            findings.append(
                _finding(
                    path,
                    Path(path).name,
                    "重复文件",
                    "recycle_suggest",
                    "medium",
                    group.size,
                    f"与 {group.keep} 内容相同（大小一致且哈希一致）。默认保留路径更短的一份。",
                    "可移入回收站。若这份才是要留下的，请取消勾选。",
                )
            )
    return findings
