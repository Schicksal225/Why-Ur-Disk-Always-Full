"""Safety guards for path deletion and process termination.

System locations are a hard deny: user settings cannot opt them back in.
Data drives are not blanket-protected; project trees are.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Iterable

from core.paths import get_user_local_appdata, get_user_profile, get_user_roaming_appdata

# Path segments that must never be deleted even inside an otherwise allowed tree.
_ALWAYS_PROTECTED_PARTS = (
    "node_modules",
    ".git",
    ".pnpm-store",
    "dockerdata",
)

# Immediate children of a drive root that Windows reserves.
_ROOT_SYSTEM_DIRS = frozenset(
    {
        "system volume information",
        "$recycle.bin",
        "recovery",
        "boot",
    }
)

_SYSTEM_FILE_NAMES = frozenset(
    {
        "pagefile.sys",
        "hiberfil.sys",
        "swapfile.sys",
    }
)

PROJECT_MARKER_NAMES = frozenset(
    {
        ".git",
        "package.json",
        "pyproject.toml",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "composer.json",
    }
)

# System-critical processes – never kill
CRITICAL_PROCESS_NAMES = frozenset(
    {
        "system",
        "system idle process",
        "registry",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "dwm.exe",
        "explorer.exe",
        "fontdrvhost.exe",
        "sihost.exe",
        "taskhostw.exe",
        "runtimebroker.exe",
        "searchindexer.exe",
        "securityhealthservice.exe",
        "msmpeng.exe",
        "nissrv.exe",
        "spoolsv.exe",
        "audiodg.exe",
        "conhost.exe",
        "dllhost.exe",
        "python.exe",
        "pythonw.exe",
        "pcoptimizer.exe",
    }
)

_pnpm_store_cache: str | None = None
_protected_prefixes_cache: list[str] | None = None
_safe_target_prefixes_cache: list[str] | None = None
_cache_clean_roots_cache: list[tuple[str, str]] | None = None
_project_root_cache: dict[str, bool] = {}


def reset_safety_caches() -> None:
    """Clear process-wide path caches. Tests call this between cases."""
    global _pnpm_store_cache, _protected_prefixes_cache, _safe_target_prefixes_cache, _cache_clean_roots_cache
    _pnpm_store_cache = None
    _protected_prefixes_cache = None
    _safe_target_prefixes_cache = None
    _cache_clean_roots_cache = None
    _project_root_cache.clear()


def _strip_extended(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _norm_literal(path: str | Path) -> str:
    """Normalize without resolving junctions, so `..` collapses but links stay put."""
    text = _strip_extended(str(path))
    return os.path.normcase(os.path.normpath(text))


def _norm(path: str | Path) -> str:
    text = _strip_extended(str(path))
    try:
        resolved = str(Path(text).resolve())
    except (OSError, ValueError):
        resolved = text
    return os.path.normcase(os.path.normpath(resolved))


def _with_sep(path: str) -> str:
    if not path.endswith(os.sep):
        return path + os.sep
    return path


def _is_under(norm_path: str, norm_prefix: str) -> bool:
    if norm_path == norm_prefix:
        return True
    return norm_path.startswith(_with_sep(norm_prefix))


def system_root() -> str:
    return os.environ.get("SystemRoot", r"C:\Windows")


def _system_prefixes() -> list[str]:
    root = system_root()
    drive = os.path.splitdrive(root)[0] or "C:"
    base = drive + "\\"
    return [
        root,
        os.path.join(base, "Program Files"),
        os.path.join(base, "Program Files (x86)"),
        os.path.join(base, "ProgramData"),
    ]


def is_drive_root(path: str | Path) -> bool:
    norm = _norm_literal(path)
    drive, tail = os.path.splitdrive(norm)
    if not drive:
        return False
    return tail in ("\\", "/", "")


def _system_block_reason_norm(norm: str) -> str | None:
    if is_drive_root(norm):
        return "磁盘根目录"
    _drive, tail = os.path.splitdrive(norm)
    parts = [part for part in tail.split("\\") if part]
    if parts and parts[0] in _ROOT_SYSTEM_DIRS:
        return "系统保留目录"
    if parts and parts[-1] in _SYSTEM_FILE_NAMES:
        return "系统页面/休眠文件"
    for prefix in _system_prefixes():
        if _is_under(norm, _norm_literal(prefix)):
            return "系统目录"
    return None


def system_block_reason_literal(path: str | Path) -> str | None:
    """System check without resolving junctions. Used while scanning."""
    return _system_block_reason_norm(_norm_literal(path))


def is_system_tree(path: str | Path) -> bool:
    """True for Windows / Program Files and similar trees, not for a drive root."""
    reason = system_block_reason_literal(path)
    return bool(reason) and reason != "磁盘根目录"


def system_block_reason(path: str | Path) -> str | None:
    """Hard-deny reason for a path, or None.

    Both the literal path and the resolved path are checked, so `..`,
    mixed case, `\\\\?\\` prefixes, and junctions into Windows are rejected.
    """
    literal = _norm_literal(path)
    resolved = _norm(path)
    for candidate in (literal, resolved):
        reason = _system_block_reason_norm(candidate)
        if reason:
            return reason
    return None


def is_reparse_point(path: str | Path) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def has_system_attribute(path: str | Path) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_SYSTEM)


def marker_kind(path: str | Path, names: Iterable[str]) -> str:
    """Classify a directory from its immediate child names.

    Returns "project", "git", or "". A lone .git on the user profile or on a
    first-level folder of a drive is a git repo, not a protected project tree.
    """
    lowered = [name.lower() for name in names]
    strong = any(
        ((name in PROJECT_MARKER_NAMES) and name != ".git") or name.endswith(".sln")
        for name in lowered
    )
    if strong:
        return "project"
    if ".git" not in lowered:
        return ""
    norm = _norm_literal(path)
    if norm == _norm_literal(get_user_profile()):
        return "git"
    _drive, tail = os.path.splitdrive(norm)
    parts = [part for part in tail.split("\\") if part]
    if len(parts) == 1:
        return "git"
    return "project"


def directory_is_project_root(path: str | Path) -> bool:
    key = _norm_literal(path)
    cached = _project_root_cache.get(key)
    if cached is not None:
        return cached
    result = False
    try:
        if not is_reparse_point(path):
            with os.scandir(path) as entries:
                names = [entry.name for entry in entries]
            result = marker_kind(path, names) == "project"
    except OSError:
        result = False
    _project_root_cache[key] = result
    return result


def is_inside_project(path: str | Path) -> bool:
    """True when this path or an ancestor directory looks like a source project."""
    current = Path(_norm_literal(path))
    if current.is_file() or not current.exists():
        current = current.parent
    seen: set[str] = set()
    while True:
        key = _norm_literal(current)
        if key in seen:
            return False
        seen.add(key)
        if directory_is_project_root(current):
            return True
        if is_drive_root(current):
            return False
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _discover_conda_roots(profile: str) -> list[str]:
    roots: list[str] = []
    for name in ("miniconda3", "anaconda3", "Miniconda3", "Anaconda3"):
        candidate = os.path.join(profile, name)
        if os.path.isdir(candidate):
            roots.append(candidate)
    return roots


def _build_builtin_protected_prefixes() -> list[str]:
    prefixes = list(_system_prefixes())
    prefixes.extend(_discover_conda_roots(get_user_profile()))
    return prefixes


def _firefox_cache_dirs() -> list[str]:
    profiles = Path(os.path.expanduser(r"~\AppData\Local\Mozilla\Firefox\Profiles"))
    found: list[str] = []
    if profiles.exists():
        try:
            for profile in profiles.iterdir():
                cache = profile / "cache2"
                if cache.exists():
                    found.append(str(cache))
        except OSError:
            pass
    return found


def get_cache_clean_roots() -> list[tuple[str, str]]:
    """Directories that may be wiped outright. Excludes system paths and broad parents."""
    global _cache_clean_roots_cache
    if _cache_clean_roots_cache is not None:
        return list(_cache_clean_roots_cache)
    local = get_user_local_appdata()
    roaming = get_user_roaming_appdata()
    roots = [
        (os.environ.get("TEMP", os.path.join(local, "Temp")), "用户临时文件"),
        (os.path.join(local, "CrashDumps"), "崩溃转储"),
        (os.path.join(local, r"Microsoft\Windows\WER"), "错误报告"),
        (os.path.join(local, r"Microsoft\Windows\DeliveryOptimization"), "传递优化缓存"),
        (os.path.join(local, r"Google\Chrome\User Data\Default\Cache"), "Chrome 缓存"),
        (os.path.join(local, r"Google\Chrome\User Data\Default\Code Cache"), "Chrome 代码缓存"),
        (os.path.join(local, r"Microsoft\Edge\User Data\Default\Cache"), "Edge 缓存"),
        (os.path.join(local, r"Microsoft\Edge\User Data\Default\Code Cache"), "Edge 代码缓存"),
        (os.path.join(local, "pip", "cache"), "pip 缓存"),
        (os.path.join(local, "npm-cache"), "npm 缓存"),
        (os.path.join(roaming, "npm-cache"), "npm 缓存"),
        (os.path.join(local, r"conda\conda\pkgs"), "conda 下载缓存"),
    ]
    for cache in _firefox_cache_dirs():
        roots.append((cache, "Firefox 缓存"))
    _cache_clean_roots_cache = roots
    return list(roots)


def is_cache_clean_path(path: str | Path) -> bool:
    norm = _norm(path)
    for root, _label in get_cache_clean_roots():
        if _is_under(norm, _norm(root)):
            return True
    return False


def cache_clean_label(path: str | Path) -> str:
    norm = _norm(path)
    for root, label in get_cache_clean_roots():
        if _is_under(norm, _norm(root)):
            return label
    return "缓存"


def get_advice_only_locations() -> list[tuple[str, str, str, str]]:
    """System locations we measure and explain, and never delete.

    Each item is (path, label, reason, suggestion).
    """
    root = system_root()
    return [
        (
            os.path.join(root, "Temp"),
            "Windows 临时目录",
            "该目录在 Windows 系统目录内，本工具不删除系统文件。",
            "打开「设置 → 系统 → 存储 → 临时文件」清理。",
        ),
        (
            os.path.join(root, r"SoftwareDistribution\Download"),
            "Windows 更新下载缓存",
            "该目录在 Windows 系统目录内，本工具不删除系统文件。",
            "运行系统「磁盘清理」，勾选「Windows 更新清理」。",
        ),
    ]


def _build_safe_target_prefixes() -> list[str]:
    local = get_user_local_appdata()
    roaming = get_user_roaming_appdata()
    prefixes = [root for root, _label in get_cache_clean_roots()]
    # Thumbcache files live beside other Explorer data; only the matching files are targets.
    prefixes.append(os.path.join(local, r"Microsoft\Windows\Explorer"))
    prefixes.append(os.path.join(local, r"Mozilla\Firefox\Profiles"))
    prefixes.append(os.path.join(roaming, "npm-cache"))
    return prefixes


def get_pnpm_store_path() -> str | None:
    global _pnpm_store_cache
    if _pnpm_store_cache is not None:
        return _pnpm_store_cache or None
    try:
        result = subprocess.run(
            ["pnpm", "store", "path"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            _pnpm_store_cache = _norm(result.stdout.strip())
            return _pnpm_store_cache
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    for drive in "EDCB":
        fallback = f"{drive}:\\.pnpm-store"
        if os.path.exists(fallback):
            _pnpm_store_cache = _norm(fallback)
            return _pnpm_store_cache
    _pnpm_store_cache = ""
    return None


def get_protected_prefixes(extra: Iterable[str] | None = None) -> list[str]:
    global _protected_prefixes_cache
    if _protected_prefixes_cache is None:
        prefixes = _build_builtin_protected_prefixes()
        pnpm = get_pnpm_store_path()
        if pnpm:
            prefixes.append(pnpm)
        for drive in "EDCB":
            docker = f"{drive}:\\DockerData"
            if os.path.exists(docker):
                prefixes.append(_norm(docker))
        _protected_prefixes_cache = [_with_sep(_norm(p)) for p in prefixes]
    result = list(_protected_prefixes_cache)
    if extra:
        for item in extra:
            result.append(_with_sep(_norm(item)))
    return result


def get_safe_target_prefixes() -> list[str]:
    global _safe_target_prefixes_cache
    if _safe_target_prefixes_cache is None:
        _safe_target_prefixes_cache = [_with_sep(_norm(p)) for p in _build_safe_target_prefixes()]
    return _safe_target_prefixes_cache


def is_under_prefix(path: str, prefixes: Iterable[str]) -> bool:
    norm = _with_sep(_norm(path))
    for prefix in prefixes:
        if norm.startswith(_with_sep(prefix)):
            return True
    return False


def contains_protected_part(path: str) -> bool:
    lower = _norm_literal(path).lower()
    for part in _ALWAYS_PROTECTED_PARTS:
        token = os.sep + part + os.sep
        if token in lower or lower.endswith(os.sep + part):
            return True
    return False


def deletion_allowed(
    path: str | Path,
    user_protected_paths: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Final delete/recycle gate. Callers must not trust an earlier scan."""
    reason = system_block_reason(path)
    if reason:
        return False, reason
    if is_reparse_point(path):
        return False, "重解析点（junction/symlink），不跟随也不删除"
    if has_system_attribute(path):
        return False, "带系统属性的文件"
    norm = _norm(path)
    if contains_protected_part(norm):
        return False, "受保护路径"
    if is_under_prefix(norm, get_protected_prefixes(user_protected_paths)):
        return False, "受保护路径"
    if is_inside_project(path):
        return False, "项目目录，只统计不清理"
    return True, ""


def is_path_protected(
    path: str | Path,
    user_protected_paths: Iterable[str] | None = None,
) -> bool:
    allowed, _reason = deletion_allowed(path, user_protected_paths)
    return not allowed


def is_safe_delete_target(
    path: str | Path,
    user_protected_paths: Iterable[str] | None = None,
) -> bool:
    """True only for cache-whitelist files that also pass the hard deny."""
    if not deletion_allowed(path, user_protected_paths)[0]:
        return False
    if not is_under_prefix(_norm(path), get_safe_target_prefixes()):
        return False
    return True


def can_kill_process(
    name: str,
    pid: int,
    user_whitelist: Iterable[str] | None = None,
    current_pid: int | None = None,
) -> tuple[bool, str]:
    """Check if a process may be terminated. Returns (allowed, reason)."""
    if current_pid is not None and pid == current_pid:
        return False, "当前程序自身"
    lower = (name or "").lower()
    if lower in CRITICAL_PROCESS_NAMES:
        return False, "系统关键进程"
    if user_whitelist:
        for item in user_whitelist:
            if lower == item.lower() or lower.endswith("\\" + item.lower()):
                return False, "用户白名单"
    return True, ""
