"""Full-disk storage scan. Read-only: it never deletes."""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from core.paths import get_data_dir
from core.projects import empty_project, regenerable_name
from core.safety import (
    PROJECT_MARKER_NAMES,
    get_advice_only_locations,
    get_cache_clean_roots,
    is_reparse_point,
    is_system_tree,
    marker_kind,
)

_ATTR_DIRECTORY = 0x10
_ATTR_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT

ProgressCallback = Callable[[str], None]

EXT_CATEGORY = {
    ".mp4": "视频",
    ".mkv": "视频",
    ".avi": "视频",
    ".mov": "视频",
    ".wmv": "视频",
    ".zip": "压缩包",
    ".rar": "压缩包",
    ".7z": "压缩包",
    ".tar": "压缩包",
    ".gz": "压缩包",
    ".exe": "程序",
    ".msi": "程序",
    ".iso": "镜像",
    ".vhdx": "镜像",
    ".vhd": "镜像",
    ".vmdk": "镜像",
    ".pdf": "文档",
    ".doc": "文档",
    ".docx": "文档",
    ".xls": "文档",
    ".xlsx": "文档",
    ".ppt": "文档",
    ".pptx": "文档",
    ".txt": "文档",
    ".mp3": "音频",
    ".flac": "音频",
    ".wav": "音频",
    ".jpg": "图片",
    ".jpeg": "图片",
    ".png": "图片",
    ".gif": "图片",
    ".webp": "图片",
}

_CHAT_MARKERS = (
    ("wechat files", "微信接收的文件"),
    ("xwechat_files", "微信接收的文件"),
    ("tencent files", "QQ 接收的文件"),
)


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


@dataclass
class FileRecord:
    path: str
    size: int
    mtime: float
    atime: float
    ext: str
    inside_project: bool = False


@dataclass
class DirNode:
    path: str
    name: str
    size: int
    file_count: int
    protected: str = ""
    children: list["DirNode"] = field(default_factory=list)


@dataclass
class ScanResult:
    root: str
    total_bytes: int
    file_count: int
    dir_count: int
    skipped_errors: int
    skipped_reparse: int
    cancelled: bool
    tree: DirNode | None
    categories: dict[str, int]
    large_files: list[FileRecord]
    stale_files: list[FileRecord]
    cache_dirs: list[tuple]
    advice_dirs: list[tuple]
    empty_dirs: list[str]
    dup_candidates: list[tuple]
    projects: list
    scanned_at: str


def category_for(name: str) -> str:
    return EXT_CATEGORY.get(Path(name).suffix.lower(), "其他")


def _chat_label(path: str) -> str | None:
    folded = os.path.normcase(path)
    for marker, label in _CHAT_MARKERS:
        if marker in folded:
            return label
    return None


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


@dataclass
class _State:
    cancel: CancelToken | None
    progress: ProgressCallback | None
    large_min_bytes: int
    large_limit: int
    stale_days: int
    stale_min_bytes: int
    stale_limit: int
    dup_min_bytes: int
    collect_duplicates: bool
    store_depth: int
    max_children: int
    empty_limit: int
    file_count: int = 0
    dir_count: int = 0
    skipped_errors: int = 0
    skipped_reparse: int = 0
    cancelled: bool = False
    categories: dict[str, int] = field(default_factory=dict)
    large: list[FileRecord] = field(default_factory=list)
    stale: list[FileRecord] = field(default_factory=list)
    cache_dirs: list[tuple] = field(default_factory=list)
    advice_dirs: list[tuple] = field(default_factory=list)
    empty_dirs: list[str] = field(default_factory=list)
    dup_candidates: list[tuple] = field(default_factory=list)
    projects: list = field(default_factory=list)
    _cache_keys: set[str] = field(default_factory=set)
    _advice_keys: set[str] = field(default_factory=set)
    _visited: int = 0

    def stop(self) -> bool:
        if self.cancel is not None and self.cancel.is_set():
            self.cancelled = True
            return True
        return False


def _trim(records: list[FileRecord], limit: int) -> None:
    if len(records) > limit:
        records.sort(key=lambda item: item.size, reverse=True)
        del records[limit:]


def scan_path(
    root: str | Path,
    *,
    cancel: CancelToken | None = None,
    progress: ProgressCallback | None = None,
    large_min_bytes: int = 100 * 1024 * 1024,
    large_limit: int = 100,
    stale_days: int = 180,
    stale_min_bytes: int = 50 * 1024 * 1024,
    stale_limit: int = 100,
    dup_min_bytes: int = 1024 * 1024,
    collect_duplicates: bool = False,
    store_depth: int = 4,
    max_children: int = 40,
    empty_limit: int = 30,
) -> ScanResult:
    state = _State(
        cancel=cancel,
        progress=progress,
        large_min_bytes=large_min_bytes,
        large_limit=large_limit,
        stale_days=stale_days,
        stale_min_bytes=stale_min_bytes,
        stale_limit=stale_limit,
        dup_min_bytes=dup_min_bytes,
        collect_duplicates=collect_duplicates,
        store_depth=store_depth,
        max_children=max_children,
        empty_limit=empty_limit,
        _cache_keys={_norm_key(path) for path, _label in get_cache_clean_roots()},
        _advice_keys={_norm_key(path) for path, _label, _reason, _suggestion in get_advice_only_locations()},
    )
    root_text = str(root)
    total, _files, tree = _walk(root_text, 0, False, False, False, False, None, state)
    _trim(state.large, large_limit)
    _trim(state.stale, stale_limit)
    return ScanResult(
        root=root_text,
        total_bytes=total,
        file_count=state.file_count,
        dir_count=state.dir_count,
        skipped_errors=state.skipped_errors,
        skipped_reparse=state.skipped_reparse,
        cancelled=state.cancelled,
        tree=tree,
        categories=dict(state.categories),
        large_files=state.large,
        stale_files=state.stale,
        cache_dirs=state.cache_dirs,
        advice_dirs=state.advice_dirs,
        empty_dirs=state.empty_dirs,
        dup_candidates=state.dup_candidates,
        projects=state.projects,
        scanned_at=datetime.now().isoformat(timespec="seconds"),
    )


def _marker_names(names: list[str]) -> list[str]:
    found = []
    for name in names:
        lowered = name.lower()
        if lowered in PROJECT_MARKER_NAMES or lowered.endswith(".sln"):
            found.append(name)
    return found


def _walk(
    path: str,
    depth: int,
    inside_project: bool,
    inside_chat: bool,
    inside_system: bool,
    inside_regenerable: bool,
    owner: dict | None,
    state: _State,
) -> tuple[int, int, DirNode | None]:
    if state.stop():
        return 0, 0, None
    if depth == 0 and is_reparse_point(path):
        state.skipped_reparse += 1
        return 0, 0, None
    if not inside_system and is_system_tree(path):
        inside_system = True

    size = 0
    file_count = 0
    child_nodes: list[DirNode] = []
    saw_dir = False
    try:
        entries = list(os.scandir(path))
    except OSError:
        state.skipped_errors += 1
        entries = []

    names = [entry.name for entry in entries]
    kind = "" if inside_system or inside_regenerable else marker_kind(path, names)
    created = None
    if kind:
        created = empty_project(path, _marker_names(names), kind)
        if owner is None:
            state.projects.append(created)
        else:
            owner["subprojects"].append(created)
    here_project = inside_project or kind == "project"
    active = created or owner
    protected = "system" if inside_system else "project" if here_project else ""
    chat_label = None if inside_chat else _chat_label(path)
    in_chat = inside_chat or chat_label is not None

    state.dir_count += 1
    for entry in entries:
        if state.stop():
            break
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            state.skipped_errors += 1
            continue
        attrs = getattr(info, "st_file_attributes", 0)
        if attrs & _ATTR_REPARSE:
            state.skipped_reparse += 1
            continue
        is_dir = bool(attrs & _ATTR_DIRECTORY) or stat.S_ISDIR(info.st_mode)
        if is_dir:
            saw_dir = True
            regen = regenerable_name(entry.name, active["project_type"]) if active else None
            child_size, child_files, child_node = _walk(
                entry.path,
                depth + 1,
                here_project,
                in_chat,
                inside_system,
                inside_regenerable or bool(regen),
                active,
                state,
            )
            size += child_size
            file_count += child_files
            if regen and active is not None:
                active["regenerable"][regen] = active["regenerable"].get(regen, 0) + child_size
            if child_node is not None:
                child_nodes.append(child_node)
            continue
        file_size = int(info.st_size)
        size += file_size
        file_count += 1
        state.file_count += 1
        state._visited += 1
        if state.progress and state._visited % 400 == 0:
            state.progress(f"正在盘查 {entry.path}")
        if active is not None and not inside_regenerable and info.st_mtime > active.get("source_mtime", 0):
            active["source_mtime"] = info.st_mtime
        category = category_for(entry.name)
        state.categories[category] = state.categories.get(category, 0) + file_size
        record = FileRecord(
            path=entry.path,
            size=file_size,
            mtime=info.st_mtime,
            atime=info.st_atime,
            ext=Path(entry.name).suffix.lower(),
            inside_project=here_project,
        )
        if file_size >= state.large_min_bytes:
            state.large.append(record)
            if len(state.large) > state.large_limit * 4:
                _trim(state.large, state.large_limit)
        if file_size >= state.stale_min_bytes and (time.time() - info.st_atime) >= state.stale_days * 86400:
            state.stale.append(record)
            if len(state.stale) > state.stale_limit * 4:
                _trim(state.stale, state.stale_limit)
        blocked = protected == "system" or here_project
        if state.collect_duplicates and not blocked and file_size >= state.dup_min_bytes:
            if len(state.dup_candidates) < 200_000:
                state.dup_candidates.append((entry.path, file_size, info.st_mtime))

    if created is not None:
        created["size"] = size
        created["file_count"] = file_count

    if depth >= state.store_depth:
        _note_special(path, size, chat_label, state)
        return size, file_count, None

    if file_count == 0 and not saw_dir and depth > 0 and not protected and len(state.empty_dirs) < state.empty_limit:
        state.empty_dirs.append(path)

    _note_special(path, size, chat_label, state)
    child_nodes.sort(key=lambda node: node.size, reverse=True)
    node = DirNode(
        path=path,
        name=Path(path).name or path,
        size=size,
        file_count=file_count,
        protected=protected,
        children=child_nodes[: state.max_children],
    )
    return size, file_count, node


def _note_special(path: str, size: int, chat_label: str | None, state: _State) -> None:
    key = _norm_key(path)
    if key in state._cache_keys and size > 0:
        label = next(label for root, label in get_cache_clean_roots() if _norm_key(root) == key)
        state.cache_dirs.append((path, size, label))
    if key in state._advice_keys:
        label = next(
            label
            for root, label, _reason, _suggestion in get_advice_only_locations()
            if _norm_key(root) == key
        )
        state.advice_dirs.append((path, size, label))
    elif chat_label and size > 0:
        state.advice_dirs.append((path, size, chat_label))


def _node_from_dict(data: dict) -> DirNode:
    return DirNode(
        path=data["path"],
        name=data["name"],
        size=data["size"],
        file_count=data["file_count"],
        protected=data.get("protected", ""),
        children=[_node_from_dict(child) for child in data.get("children", [])],
    )


def _record_from_dict(data: dict) -> FileRecord:
    return FileRecord(
        path=data["path"],
        size=data["size"],
        mtime=data["mtime"],
        atime=data["atime"],
        ext=data.get("ext", ""),
        inside_project=bool(data.get("inside_project", False)),
    )


def result_to_dict(result: ScanResult) -> dict:
    payload = asdict(result)
    payload["dup_candidates"] = []
    return payload


def result_from_dict(data: dict) -> ScanResult:
    tree = _node_from_dict(data["tree"]) if data.get("tree") else None
    return ScanResult(
        root=data["root"],
        total_bytes=data["total_bytes"],
        file_count=data["file_count"],
        dir_count=data["dir_count"],
        skipped_errors=data["skipped_errors"],
        skipped_reparse=data["skipped_reparse"],
        cancelled=data["cancelled"],
        tree=tree,
        categories=dict(data.get("categories") or {}),
        large_files=[_record_from_dict(item) for item in data.get("large_files") or []],
        stale_files=[_record_from_dict(item) for item in data.get("stale_files") or []],
        cache_dirs=[tuple(item) for item in data.get("cache_dirs") or []],
        advice_dirs=[tuple(item) for item in data.get("advice_dirs") or []],
        empty_dirs=list(data.get("empty_dirs") or []),
        dup_candidates=[],
        projects=list(data.get("projects") or []),
        scanned_at=data.get("scanned_at", ""),
    )


def audit_cache_path() -> Path:
    return get_data_dir() / "scan_cache.json"


def save_audit_cache(payload: dict) -> Path:
    path = audit_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_audit_cache() -> dict | None:
    path = audit_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
