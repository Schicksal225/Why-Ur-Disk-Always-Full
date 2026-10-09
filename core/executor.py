"""Apply findings. Every path is checked again before anything is removed."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from core.classifier import Finding
from core.cleaner import _clean_directory, format_bytes
from core.safety import deletion_allowed, is_cache_clean_path

FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_WANTNUKEWARNING = 0x4000


@dataclass
class ActionResult:
    path: str
    action: str
    ok: bool
    freed_bytes: int
    message: str


def query_recycle_policy(path: str) -> dict | None:
    """Read whether this volume recycles deletes and its max capacity in bytes.

    Returns {"nuke": bool, "max_bytes": int | None}, or None when the setting
    cannot be read. None means "do not delete" — failing open would permanently
    remove files that do not fit in the bin.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    absolute = os.path.abspath(path)
    drive, _tail = os.path.splitdrive(absolute)
    if not drive:
        return None
    root = drive + "\\"
    guid = _volume_guid(root)
    if not guid:
        return None
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            nuke = bool(_reg_dword(key, "NukeOnDelete", 0))
            max_mb = _reg_dword(key, "MaxCapacity", None)
    except OSError:
        return None
    max_bytes = None if max_mb is None else int(max_mb) * 1024 * 1024
    return {"nuke": nuke, "max_bytes": max_bytes}


def _reg_dword(key, name: str, default):
    import winreg

    try:
        value, kind = winreg.QueryValueEx(key, name)
    except OSError:
        return default
    if kind != winreg.REG_DWORD:
        return default
    return int(value)


def _volume_guid(root: str) -> str | None:
    buffer = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, len(buffer))
    if not ok:
        return None
    text = buffer.value  # \\?\Volume{guid}\
    start = text.find("{")
    end = text.find("}")
    if start < 0 or end < 0:
        return None
    return text[start : end + 1]


def recycle_block_reason(path: str, size: int, query=None) -> str | None:
    """Refuse a recycle when the shell would permanently delete instead."""
    reader = query or query_recycle_policy
    policy = reader(path)
    if not policy:
        return "无法确认该盘回收站是否可用，已拒绝以免文件被永久删除"
    if policy.get("nuke"):
        return "该盘已关闭回收站，删除会永久消失"
    max_bytes = policy.get("max_bytes")
    if max_bytes is not None and size > max_bytes:
        return "文件超过回收站容量，系统会直接永久删除"
    return None


def send_to_recycle_bin(path: str, size: int | None = None, policy_query=None) -> tuple[bool, str]:
    """Move a file or directory to the recycle bin. Returns (ok, message)."""
    if os.name != "nt":
        return False, "仅支持 Windows 回收站"
    if not os.path.exists(path):
        return False, "路径不存在"
    if size is None:
        try:
            size = os.path.getsize(path) if os.path.isfile(path) else 0
        except OSError:
            size = 0
    blocked = recycle_block_reason(path, int(size), query=policy_query)
    if blocked:
        return False, blocked

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    absolute = os.path.abspath(path)
    buffer = ctypes.create_unicode_buffer(absolute + "\0\0")
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
    operation.pTo = None
    # No SILENT / NOCONFIRMATION / NOERRORUI: those hide the "too big, will be
    # permanently deleted" warning. WANTNUKEWARNING asks the shell to show it.
    operation.fFlags = FOF_ALLOWUNDO | FOF_WANTNUKEWARNING
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if code == 0 and not operation.fAnyOperationsAborted:
        return True, ""
    return False, f"移入回收站失败，错误码 {code}"


def _is_temp_root(path: str) -> bool:
    temp = os.environ.get("TEMP", "")
    if not temp:
        return False
    return os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(temp))


def execute_findings(
    findings: list[Finding],
    protected_paths,
    dry_run: bool = False,
    recycle_fn=None,
    store=None,
    min_age_days: int = 1,
    recycle_policy=None,
) -> list[ActionResult]:
    recycle = recycle_fn or send_to_recycle_bin
    results: list[ActionResult] = []
    for finding in findings:
        results.append(_execute_one(finding, protected_paths, dry_run, recycle, min_age_days, recycle_policy, recycle_fn is None))
    if store is not None and not dry_run:
        freed = sum(item.freed_bytes for item in results if item.ok)
        ok_count = sum(1 for item in results if item.ok)
        store.add_storage_run(
            {
                "freed_bytes": freed,
                "summary": f"{ok_count} 项，释放 {format_bytes(freed)}",
                "details": [
                    {"path": item.path, "action": item.action, "ok": item.ok, "freed_bytes": item.freed_bytes, "message": item.message}
                    for item in results[:100]
                ],
            }
        )
    return results


def _execute_one(
    finding: Finding,
    protected_paths,
    dry_run: bool,
    recycle,
    min_age_days: int,
    recycle_policy,
    check_policy: bool,
) -> ActionResult:
    if finding.action in ("readonly", "advice_only"):
        return ActionResult(finding.path, finding.action, False, 0, "该项只提供建议，不执行")

    allowed, reason = deletion_allowed(finding.path, protected_paths)
    if not allowed:
        return ActionResult(finding.path, finding.action, False, 0, reason)

    if finding.action == "cache_clean":
        if not is_cache_clean_path(finding.path):
            return ActionResult(finding.path, finding.action, False, 0, "不在可直接清理的缓存白名单内")
        if dry_run:
            return ActionResult(finding.path, finding.action, True, finding.size, "演练：将直接清理")
        age = min_age_days if _is_temp_root(finding.path) else 0
        freed, files, skipped, errors = _clean_directory(Path(finding.path), protected_paths, min_age_days=age)
        if errors and freed == 0 and files == 0:
            return ActionResult(finding.path, finding.action, False, 0, "; ".join(errors[:3]))
        message = f"删除 {files} 项，跳过 {skipped}"
        if errors:
            message += "；" + "; ".join(errors[:3])
        return ActionResult(finding.path, finding.action, True, freed, message)

    if finding.action == "recycle_suggest":
        if dry_run:
            return ActionResult(finding.path, finding.action, True, finding.size, "演练：将移入回收站")
        if check_policy or recycle_policy is not None:
            blocked = recycle_block_reason(finding.path, finding.size, query=recycle_policy)
            if blocked:
                return ActionResult(finding.path, finding.action, False, 0, blocked)
        ok, message = recycle(finding.path)
        return ActionResult(finding.path, finding.action, ok, finding.size if ok else 0, message or ("已移入回收站" if ok else "失败"))

    return ActionResult(finding.path, finding.action, False, 0, "未知处理方式")
