"""C-drive conservative cleanup scanner and executor."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from core.paths import get_user_local_appdata, get_user_profile, get_user_roaming_appdata, is_frozen
from core.safety import is_path_protected, is_reparse_point, is_safe_delete_target

ProgressCallback = Callable[[str], None]


@dataclass
class CleanTarget:
    id: str
    label: str
    level: str  # "safe" | "risk"
    paths: list[str] = field(default_factory=list)
    command: str | None = None
    description: str = ""
    reclaimable_bytes: int = 0
    selected: bool = False


@dataclass
class CleanResult:
    target_id: str
    label: str
    freed_bytes: int
    files_removed: int
    skipped: int
    errors: list[str] = field(default_factory=list)


def _dir_size(path: Path, protected_paths: Iterable[str], min_age_days: int = 0) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        if path.is_file():
            if not is_safe_delete_target(path, protected_paths):
                return 0
            if min_age_days and not _file_age_ok(path, min_age_days):
                return 0
            return path.stat().st_size
        for root, dirs, files in os.walk(path, topdown=True):
            root_path = Path(root)
            dirs[:] = [
                d
                for d in dirs
                if not is_path_protected(root_path / d, protected_paths)
            ]
            for name in files:
                fp = root_path / name
                if not is_safe_delete_target(fp, protected_paths):
                    continue
                if min_age_days and not _file_age_ok(fp, min_age_days):
                    continue
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _glob_dir_size(pattern_root: Path, glob_pattern: str, protected_paths: Iterable[str], min_age_days: int = 0) -> int:
    total = 0
    if not pattern_root.exists():
        return 0
    for fp in pattern_root.glob(glob_pattern):
        total += _dir_size(fp, protected_paths, min_age_days=min_age_days)
    return total


def _firefox_cache_dirs() -> list[str]:
    profiles = Path(os.path.expanduser(r"~\AppData\Local\Mozilla\Firefox\Profiles"))
    paths: list[str] = []
    if profiles.exists():
        for profile in profiles.iterdir():
            cache = profile / "cache2"
            if cache.exists():
                paths.append(str(cache))
    return paths


def build_targets(protected_paths: Iterable[str] | None = None) -> list[CleanTarget]:
    user = get_user_profile()
    local = get_user_local_appdata()
    roaming = get_user_roaming_appdata()

    targets: list[CleanTarget] = [
        CleanTarget(
            id="user_temp",
            label="用户临时文件 (%TEMP%)",
            level="safe",
            paths=[os.environ.get("TEMP", os.path.join(local, "Temp"))],
            description="超过设定天数的用户临时文件",
        ),
        CleanTarget(
            id="windows_temp",
            label="Windows 临时目录",
            level="advice",
            paths=[os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")],
            description="位于系统目录，只统计并建议使用「存储感知」，本工具不删除",
        ),
        CleanTarget(
            id="recycle_bin",
            label="回收站",
            level="safe",
            command="recycle_bin",
            description="清空回收站（仅用户可恢复内容）",
        ),
        CleanTarget(
            id="thumbcache",
            label="缩略图缓存",
            level="safe",
            paths=[os.path.join(local, r"Microsoft\Windows\Explorer")],
            description="thumbcache_*.db 缩略图缓存",
        ),
        CleanTarget(
            id="crash_dumps",
            label="崩溃转储",
            level="safe",
            paths=[
                os.path.join(local, "CrashDumps"),
                os.path.join(local, r"Microsoft\Windows\WER"),
            ],
            description="应用崩溃报告与转储文件",
        ),
        CleanTarget(
            id="delivery_opt",
            label="传递优化缓存",
            level="safe",
            paths=[os.path.join(local, r"Microsoft\Windows\DeliveryOptimization")],
            description="Windows 传递优化日志与缓存",
        ),
        CleanTarget(
            id="pip_cache",
            label="pip 下载缓存",
            level="safe",
            command="pip_cache",
            paths=[os.path.join(local, "pip", "cache")],
            description="pip 包下载缓存，不影响已安装包",
        ),
        CleanTarget(
            id="npm_cache",
            label="npm 缓存",
            level="safe",
            command="npm_cache",
            paths=[
                os.path.join(local, "npm-cache"),
                os.path.join(roaming, "npm-cache"),
            ],
            description="npm 下载缓存",
        ),
        CleanTarget(
            id="conda_cache",
            label="conda 下载缓存",
            level="safe",
            command="conda_cache",
            paths=[os.path.join(local, r"conda\conda\pkgs")],
            description="仅清理 tarballs/index 缓存，不触碰 envs",
        ),
        CleanTarget(
            id="chrome_cache",
            label="Chrome 浏览器缓存",
            level="safe",
            paths=[
                os.path.join(local, r"Google\Chrome\User Data\Default\Cache"),
                os.path.join(local, r"Google\Chrome\User Data\Default\Code Cache"),
            ],
            description="仅 Cache，不清理 Cookie/书签",
        ),
        CleanTarget(
            id="edge_cache",
            label="Edge 浏览器缓存",
            level="safe",
            paths=[
                os.path.join(local, r"Microsoft\Edge\User Data\Default\Cache"),
                os.path.join(local, r"Microsoft\Edge\User Data\Default\Code Cache"),
            ],
            description="仅 Cache，不清理 Cookie/书签",
        ),
        CleanTarget(
            id="firefox_cache",
            label="Firefox 浏览器缓存",
            level="safe",
            paths=_firefox_cache_dirs(),
            description="Firefox cache2 目录",
        ),
        CleanTarget(
            id="windows_update_cache",
            label="Windows 更新下载缓存",
            level="advice",
            paths=[os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), r"SoftwareDistribution\Download")],
            description="位于系统目录，只建议使用系统「磁盘清理」，本工具不删除",
        ),
        CleanTarget(
            id="downloads_report",
            label="Downloads 大文件报告",
            level="risk",
            paths=[os.path.join(user, "Downloads")],
            description="仅扫描报告，不自动删除",
        ),
    ]
    return targets


def measure_tree_bytes(path: Path) -> int:
    """Read-only size. Skips reparse points and does not apply the delete whitelist."""
    total = 0
    if not path.exists() or is_reparse_point(path):
        return 0
    try:
        if path.is_file():
            return path.stat().st_size
        for root, dirs, files in os.walk(path, topdown=True):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if not is_reparse_point(root_path / name)]
            for name in files:
                fp = root_path / name
                if is_reparse_point(fp):
                    continue
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def scan_target(target: CleanTarget, protected_paths: Iterable[str], min_age_days: int = 0) -> int:
    if target.level == "advice":
        total = 0
        for item in target.paths:
            total += measure_tree_bytes(Path(item))
        return total
    use_age = target.id == "user_temp"
    age = min_age_days if use_age else 0

    if target.command == "recycle_bin":
        return _estimate_recycle_bin()
    if target.command in ("pip_cache", "npm_cache", "conda_cache"):
        size = 0
        for p in target.paths:
            size += _dir_size(Path(p), protected_paths, min_age_days=0)
        return size
    if target.id == "thumbcache":
        root = Path(target.paths[0]) if target.paths else None
        if root and root.exists():
            return _glob_dir_size(root, "thumbcache_*.db", protected_paths)
        return 0
    if target.id == "downloads_report":
        return _scan_large_files_report(target.paths[0] if target.paths else "", protected_paths)
    total = 0
    for p in target.paths:
        total += _dir_size(Path(p), protected_paths, min_age_days=age)
    return total


def scan_all(
    targets: list[CleanTarget],
    protected_paths: Iterable[str],
    progress: ProgressCallback | None = None,
    min_age_days: int = 1,
) -> list[CleanTarget]:
    for t in targets:
        if progress:
            progress(f"扫描: {t.label}")
        t.reclaimable_bytes = scan_target(t, protected_paths, min_age_days=min_age_days)
        if t.level == "safe":
            t.selected = True
    return targets


def _estimate_recycle_bin() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong),
            ]

        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        shell32 = ctypes.windll.shell32
        if shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return max(0, int(info.i64Size))
    except Exception:
        pass
    return 0


def _scan_large_files_report(downloads: str, protected_paths: Iterable[str], min_mb: int = 50) -> int:
    """Return total size of files > min_mb for reporting only."""
    root = Path(downloads)
    if not root.exists():
        return 0
    total = 0
    threshold = min_mb * 1024 * 1024
    try:
        for fp in root.rglob("*"):
            if fp.is_file() and not is_path_protected(fp, protected_paths):
                try:
                    sz = fp.stat().st_size
                    if sz >= threshold:
                        total += sz
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _file_age_ok(fp: Path, min_age_days: int) -> bool:
    try:
        mtime = datetime.fromtimestamp(fp.stat().st_mtime)
        return mtime < datetime.now() - timedelta(days=min_age_days)
    except OSError:
        return False


def _delete_file(fp: Path, protected_paths: Iterable[str]) -> tuple[bool, str | None]:
    if not is_safe_delete_target(fp, protected_paths):
        return False, "受保护路径"
    try:
        if fp.is_dir():
            shutil.rmtree(fp, ignore_errors=False)
        else:
            fp.unlink(missing_ok=True)
        return True, None
    except OSError as exc:
        return False, str(exc)


def _clean_directory(
    path: Path,
    protected_paths: Iterable[str],
    min_age_days: int = 0,
    pattern: str | None = None,
) -> tuple[int, int, int, list[str]]:
    freed = files = skipped = 0
    errors: list[str] = []
    if not path.exists():
        return freed, files, skipped, errors

    if path.is_file():
        if pattern and not path.match(pattern):
            return freed, files, skipped, errors
        if min_age_days and not _file_age_ok(path, min_age_days):
            skipped += 1
            return freed, files, skipped, errors
        try:
            sz = path.stat().st_size
        except OSError:
            sz = 0
        ok, err = _delete_file(path, protected_paths)
        if ok:
            freed += sz
            files += 1
        else:
            skipped += 1
            if err:
                errors.append(f"{path}: {err}")
        return freed, files, skipped, errors

    try:
        for root, dirs, filenames in os.walk(path, topdown=False):
            root_path = Path(root)
            for name in filenames:
                fp = root_path / name
                if pattern and not fp.match(pattern):
                    continue
                if min_age_days and not _file_age_ok(fp, min_age_days):
                    skipped += 1
                    continue
                try:
                    sz = fp.stat().st_size
                except OSError:
                    sz = 0
                ok, err = _delete_file(fp, protected_paths)
                if ok:
                    freed += sz
                    files += 1
                else:
                    skipped += 1
                    if err and len(errors) < 20:
                        errors.append(f"{fp}: {err}")
            for name in dirs:
                dp = root_path / name
                if is_path_protected(dp, protected_paths):
                    continue
                try:
                    if not any(dp.iterdir()):
                        dp.rmdir()
                except OSError:
                    pass
    except OSError as exc:
        errors.append(str(exc))
    return freed, files, skipped, errors


def _empty_recycle_bin() -> tuple[int, int, list[str]]:
    try:
        import ctypes

        size_before = _estimate_recycle_bin()
        shell32 = ctypes.windll.shell32
        # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        result = shell32.SHEmptyRecycleBinW(None, None, 0x00000001 | 0x00000002 | 0x00000004)
        if result == 0:
            return size_before, 1, []
        return 0, 0, [f"回收站清理失败，错误码 {result}"]
    except Exception as exc:
        return 0, 0, [str(exc)]


def _run_command_cache(cmd: list[str], timeout: int = 120) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


def _find_conda_executable() -> str | None:
    found = shutil.which("conda")
    if found:
        return found
    profile = get_user_profile()
    for name in ("miniconda3", "anaconda3", "Miniconda3", "Anaconda3"):
        candidate = os.path.join(profile, name, "Scripts", "conda.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def execute_target(
    target: CleanTarget,
    protected_paths: Iterable[str],
    min_age_days: int = 1,
    dry_run: bool = False,
) -> CleanResult:
    if target.level == "advice":
        return CleanResult(
            target_id=target.id,
            label=target.label,
            freed_bytes=0,
            files_removed=0,
            skipped=0,
            errors=["仅建议，不执行删除。请使用系统「存储感知」或「磁盘清理」。"],
        )

    if dry_run or target.id == "downloads_report":
        return CleanResult(
            target_id=target.id,
            label=target.label,
            freed_bytes=target.reclaimable_bytes if dry_run else 0,
            files_removed=0,
            skipped=0,
            errors=[] if dry_run else ["仅报告，不执行删除"],
        )

    freed = files = skipped = 0
    errors: list[str] = []

    if target.command == "recycle_bin":
        f, n, errs = _empty_recycle_bin()
        return CleanResult(target.id, target.label, f, n, 0, errs)

    if target.command == "pip_cache":
        size_before = scan_target(target, protected_paths)
        ok = False
        msg = ""
        if not is_frozen():
            ok, msg = _run_command_cache([sys.executable, "-m", "pip", "cache", "purge"])
            if ok:
                return CleanResult(target.id, target.label, size_before, 1, 0, [])
        # frozen exe or pip failed: manual directory cleanup
        for p in target.paths:
            f, n, s, e = _clean_directory(Path(p), protected_paths)
            freed += f
            files += n
            skipped += s
            errors.extend(e)
        if not ok and msg:
            errors.append(msg)
        return CleanResult(target.id, target.label, freed or size_before, files, skipped, errors)

    if target.command == "npm_cache":
        size_before = scan_target(target, protected_paths)
        ok, msg = _run_command_cache(["npm", "cache", "clean", "--force"])
        if ok:
            return CleanResult(target.id, target.label, size_before, 1, 0, [])
        for p in target.paths:
            f, n, s, e = _clean_directory(Path(p), protected_paths)
            freed += f
            files += n
            skipped += s
            errors.extend(e)
        if not ok and msg:
            errors.append(msg)
        return CleanResult(target.id, target.label, freed or size_before, files, skipped, errors)

    if target.command == "conda_cache":
        size_before = scan_target(target, protected_paths)
        conda = _find_conda_executable()
        if conda:
            ok, msg = _run_command_cache([conda, "clean", "--tarballs", "--index-cache", "-y"])
            if ok:
                return CleanResult(target.id, target.label, size_before, 1, 0, [])
            if msg:
                errors.append(msg)
        else:
            errors.append("未找到 conda，已跳过 conda 缓存清理")
        return CleanResult(target.id, target.label, 0, 0, 0, errors)

    pattern = "thumbcache_*.db" if target.id == "thumbcache" else None
    use_age = target.id in ("user_temp", "windows_temp")

    for p in target.paths:
        path = Path(p)
        if target.id == "thumbcache" and path.is_dir():
            for fp in path.glob("thumbcache_*.db"):
                try:
                    sz = fp.stat().st_size
                except OSError:
                    sz = 0
                ok, err = _delete_file(fp, protected_paths)
                if ok:
                    freed += sz
                    files += 1
                else:
                    skipped += 1
                    if err:
                        errors.append(f"{fp}: {err}")
        else:
            f, n, s, e = _clean_directory(
                path,
                protected_paths,
                min_age_days=min_age_days if use_age else 0,
                pattern=pattern,
            )
            freed += f
            files += n
            skipped += s
            errors.extend(e)

    return CleanResult(target.id, target.label, freed, files, skipped, errors)


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def list_scannable_drives() -> list[str]:
    """Fixed and removable drives. Skips CD-ROM and unavailable volumes."""
    found: list[str] = []
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        bitmask = kernel.GetLogicalDrives()
        for index in range(26):
            if not bitmask & (1 << index):
                continue
            root = f"{chr(65 + index)}:\\"
            # 2 = removable, 3 = fixed. Skip CD-ROM (5) and remote/ram disks.
            if kernel.GetDriveTypeW(root) not in (2, 3):
                continue
            if os.path.exists(root):
                found.append(root)
        return found
    for letter in "CDEF":
        root = f"{letter}:\\"
        if os.path.exists(root):
            found.append(root)
    return found


def get_disk_usage() -> list[dict[str, float | str]]:
    drives = []
    for path in list_scannable_drives():
        try:
            usage = shutil.disk_usage(path)
            letter = path[0]
            drives.append(
                {
                    "drive": letter,
                    "total_gb": round(usage.total / 1024 ** 3, 1),
                    "used_gb": round(usage.used / 1024 ** 3, 1),
                    "free_gb": round(usage.free / 1024 ** 3, 1),
                    "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
                }
            )
        except OSError:
            pass
    return drives
