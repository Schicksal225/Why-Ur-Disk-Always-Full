"""Process and system performance diagnostics."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import psutil

from core.safety import can_kill_process


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    status: str
    username: str
    suspicious: bool = False
    suspicious_reason: str = ""
    can_kill: bool = False
    kill_block_reason: str = ""


@dataclass
class SystemSnapshot:
    cpu_percent: float
    memory_percent: float
    memory_used_gb: float
    memory_total_gb: float
    processes: list[ProcessInfo] = field(default_factory=list)


@dataclass
class MemoryAdviceItem:
    category: str
    title: str
    detail: str
    severity: str  # info | warning | critical | success


@dataclass
class ProcessGroup:
    key: str
    display_name: str
    process_names: list[str]
    instance_count: int
    total_memory_mb: float
    advice_hint: str


# Known memory-heavy app profiles
_APP_PROFILES: dict[str, dict[str, str]] = {
    "chrome.exe": {
        "display": "Google Chrome",
        "category": "browser",
        "hint": "关闭不用的标签页，或安装标签页休眠扩展（如 The Great Suspender 类工具）。",
    },
    "msedge.exe": {
        "display": "Microsoft Edge",
        "category": "browser",
        "hint": "关闭多余标签页；Edge 内置「休眠标签页」可在设置中开启。",
    },
    "firefox.exe": {
        "display": "Firefox",
        "category": "browser",
        "hint": "关闭不常用标签页；可在 about:preferences 开启标签页休眠。",
    },
    "opera.exe": {
        "display": "Opera",
        "category": "browser",
        "hint": "减少同时打开的标签页数量。",
    },
    "brave.exe": {
        "display": "Brave",
        "category": "browser",
        "hint": "关闭闲置标签页以降低内存占用。",
    },
    "cursor.exe": {
        "display": "Cursor 编辑器",
        "category": "ide",
        "hint": "关闭不用的工作区窗口；大型项目可禁用不必要的扩展。",
    },
    "code.exe": {
        "display": "VS Code",
        "category": "ide",
        "hint": "关闭多余窗口；检查扩展是否占用过多内存。",
    },
    "node.exe": {
        "display": "Node.js 开发服务",
        "category": "dev",
        "hint": "停止未使用的 dev server（npm run dev / vite 等）。",
    },
    "docker desktop.exe": {
        "display": "Docker Desktop",
        "category": "dev",
        "hint": "不开发时可退出 Docker Desktop 释放大量内存。",
    },
    "com.docker.backend.exe": {
        "display": "Docker 后台",
        "category": "dev",
        "hint": "停止未使用的容器后退出 Docker。",
    },
    "msedgewebview2.exe": {
        "display": "WebView2 组件",
        "category": "embedded",
        "hint": "通常由其他应用嵌入网页引起，可关闭对应宿主应用（如 Teams、微信）。",
    },
    "wechat.exe": {
        "display": "微信",
        "category": "app",
        "hint": "可尝试最小化到托盘或退出后重新登录。",
    },
    "dingtalk.exe": {
        "display": "钉钉",
        "category": "app",
        "hint": "关闭不用的视频会议或聊天窗口。",
    },
    "msmpeng.exe": {
        "display": "Windows Defender",
        "category": "system",
        "hint": "杀毒扫描时会短暂占用大量资源，等待扫描完成即可。",
    },
    "svchost.exe": {
        "display": "Windows 系统服务",
        "category": "system",
        "hint": "系统服务宿主进程，属正常组件，不建议结束。",
    },
    "memcompression": {
        "display": "Windows 内存压缩",
        "category": "system",
        "hint": "系统用于压缩内存页以腾出空间，属正常机制，无需处理。",
    },
    "searchindexer.exe": {
        "display": "Windows 搜索索引",
        "category": "system",
        "hint": "索引重建时会占用 CPU/内存，完成后会恢复。",
    },
    "python.exe": {
        "display": "Python 进程",
        "category": "dev",
        "hint": "检查是否有未退出的脚本、Jupyter 或训练任务。",
    },
    "java.exe": {
        "display": "Java 应用",
        "category": "dev",
        "hint": "可能是 IDE 或后端服务，确认后关闭不需要的实例。",
    },
    "vmmemwsl": {
        "display": "WSL 子系统",
        "category": "dev",
        "hint": "Linux 子系统内存占用。不用时可在 PowerShell 执行 wsl --shutdown 释放。",
    },
}


def _proc_username(proc: psutil.Process) -> str:
    try:
        return proc.username()
    except (psutil.Error, OSError):
        return ""


def collect_system_snapshot(
    top_n: int = 30,
    cpu_interval: float = 0.5,
    baseline: dict[str, Any] | None = None,
    process_whitelist: list[str] | None = None,
    current_pid: int | None = None,
) -> SystemSnapshot:
    mem = psutil.virtual_memory()

    baseline_cpu = (baseline or {}).get("cpu_percent", 0) or 0
    baseline_procs: dict[str, float] = (baseline or {}).get("top_processes", {}) or {}

    # Prime CPU counters (first call always returns 0.0)
    psutil.cpu_percent(interval=None)
    raw_procs: list[tuple[psutil.Process, dict[str, Any]]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "status"]):
        try:
            proc.cpu_percent(interval=None)
            raw_procs.append((proc, proc.info))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if cpu_interval > 0:
        time.sleep(cpu_interval)

    cpu_total = psutil.cpu_percent(interval=None)

    processes: list[ProcessInfo] = []
    for proc, info in raw_procs:
        try:
            pid = info["pid"]
            name = info.get("name") or "unknown"
            mem_info = info.get("memory_info")
            rss_mb = (mem_info.rss / 1024 / 1024) if mem_info else 0.0
            try:
                cpu = proc.cpu_percent(interval=None)
            except (psutil.Error, OSError):
                cpu = 0.0

            allowed, reason = can_kill_process(
                name, pid, process_whitelist, current_pid=current_pid
            )

            suspicious = False
            susp_reason = ""
            base_mem = baseline_procs.get(name.lower(), 0)
            if baseline:
                if cpu > max(15, baseline_cpu * 2) and cpu > 5:
                    suspicious = True
                    susp_reason = f"CPU 偏高 ({cpu:.1f}%)"
                elif rss_mb > max(200, base_mem * 1.8) and rss_mb > 100:
                    suspicious = True
                    susp_reason = f"内存偏高 ({rss_mb:.0f} MB)"
                elif name.lower() in baseline_procs and rss_mb > base_mem * 2.5 and rss_mb > 50:
                    suspicious = True
                    susp_reason = f"内存较基线翻倍 ({rss_mb:.0f} vs {base_mem:.0f} MB)"

            processes.append(
                ProcessInfo(
                    pid=pid,
                    name=name,
                    cpu_percent=cpu,
                    memory_mb=rss_mb,
                    status=info.get("status") or "",
                    username=_proc_username(proc),
                    suspicious=suspicious,
                    suspicious_reason=susp_reason,
                    can_kill=allowed,
                    kill_block_reason=reason,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    processes.sort(key=lambda p: (p.cpu_percent, p.memory_mb), reverse=True)
    top = processes[:top_n]

    return SystemSnapshot(
        cpu_percent=cpu_total,
        memory_percent=mem.percent,
        memory_used_gb=round(mem.used / 1024 ** 3, 2),
        memory_total_gb=round(mem.total / 1024 ** 3, 2),
        processes=top,
    )


def build_baseline_snapshot(current_pid: int | None = None) -> dict[str, Any]:
    snap = collect_system_snapshot(
        top_n=50, cpu_interval=1.0, current_pid=current_pid
    )
    top_map: dict[str, float] = {}
    for p in snap.processes:
        key = p.name.lower()
        top_map[key] = max(top_map.get(key, 0), round(p.memory_mb, 1))
    return {
        "cpu_percent": round(snap.cpu_percent, 1),
        "memory_percent": round(snap.memory_percent, 1),
        "memory_used_gb": snap.memory_used_gb,
        "top_processes": top_map,
    }


def kill_processes(
    pids: list[int],
    process_whitelist: list[str] | None = None,
    current_pid: int | None = None,
) -> dict[str, Any]:
    killed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for pid in pids:
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            failed.append({"pid": pid, "error": str(exc)})
            continue

        allowed, reason = can_kill_process(
            name, pid, process_whitelist, current_pid=current_pid
        )
        if not allowed:
            skipped.append({"pid": pid, "name": name, "reason": reason})
            continue

        try:
            proc.terminate()
            proc.wait(timeout=3)
            killed.append({"pid": pid, "name": name})
        except psutil.TimeoutExpired:
            try:
                proc.kill()
                killed.append({"pid": pid, "name": name, "forced": True})
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                failed.append({"pid": pid, "name": name, "error": str(exc)})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            failed.append({"pid": pid, "name": name, "error": str(exc)})

    invalidate_memory_cache()
    return {"killed": killed, "failed": failed, "skipped": skipped}


_memory_rows_cache: tuple[float, list[tuple[str, float]]] | None = None
_MEMORY_CACHE_TTL = 2.0


def _iter_all_process_memory(force_refresh: bool = False) -> list[tuple[str, float]]:
    """Scan all running processes for memory grouping (short TTL cache)."""
    global _memory_rows_cache
    now = time.time()
    if (
        not force_refresh
        and _memory_rows_cache is not None
        and now - _memory_rows_cache[0] < _MEMORY_CACHE_TTL
    ):
        return _memory_rows_cache[1]

    rows: list[tuple[str, float]] = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or "unknown"
            mem_info = info.get("memory_info")
            rss_mb = (mem_info.rss / 1024 / 1024) if mem_info else 0.0
            if rss_mb > 1:
                rows.append((name, rss_mb))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    _memory_rows_cache = (now, rows)
    return rows


def invalidate_memory_cache() -> None:
    global _memory_rows_cache
    _memory_rows_cache = None


def group_memory_hogs(min_mb: float = 80, force_refresh: bool = False) -> list[ProcessGroup]:
    """Aggregate processes by app name for memory diagnosis."""
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "mem": 0.0, "names": set()}
    )
    source = _iter_all_process_memory(force_refresh=force_refresh)
    for name, rss_mb in source:
        key = name.lower()
        profile = _APP_PROFILES.get(key)
        if profile:
            group_key = profile["category"] + ":" + profile["display"]
            display = profile["display"]
            hint = profile["hint"]
            if profile["category"] == "system" and rss_mb < 300:
                continue
        else:
            if rss_mb < min_mb:
                continue
            group_key = "other:" + name
            display = name
            hint = "确认是否为需要保留的后台程序，不需要时可结束。"
        buckets[group_key]["count"] += 1
        buckets[group_key]["mem"] += rss_mb
        buckets[group_key]["names"].add(name)
        buckets[group_key]["display"] = display
        buckets[group_key]["hint"] = hint

    groups: list[ProcessGroup] = []
    for key, data in buckets.items():
        if data["mem"] < min_mb:
            continue
        groups.append(
            ProcessGroup(
                key=key,
                display_name=data["display"],
                process_names=sorted(data["names"]),
                instance_count=data["count"],
                total_memory_mb=round(data["mem"], 1),
                advice_hint=data["hint"],
            )
        )
    groups.sort(key=lambda g: g.total_memory_mb, reverse=True)
    return groups


def generate_memory_advice(
    snapshot: SystemSnapshot,
    killed: list[dict[str, Any]] | None = None,
    mem_before_percent: float | None = None,
    mem_after_percent: float | None = None,
    groups: list[ProcessGroup] | None = None,
) -> list[MemoryAdviceItem]:
    """Produce actionable memory advice, especially after killing processes."""
    items: list[MemoryAdviceItem] = []

    if mem_before_percent is not None and mem_after_percent is not None:
        delta = mem_before_percent - mem_after_percent
        if delta > 0.5:
            items.append(
                MemoryAdviceItem(
                    category="result",
                    title=f"内存已下降约 {delta:.1f}%",
                    detail=(
                        f"结束进程前内存 {mem_before_percent:.1f}%，"
                        f"现在 {mem_after_percent:.1f}%。"
                        f"若仍偏高，请继续查看下方建议。"
                    ),
                    severity="success",
                )
            )
        elif killed:
            items.append(
                MemoryAdviceItem(
                    category="result",
                    title="进程已结束，但内存下降不明显",
                    detail=(
                        "部分应用（尤其浏览器）结束后内存不会立刻归还系统，"
                        "或被杀进程并非主要占用源。请查看下方「主要内存占用」分析。"
                    ),
                    severity="warning",
                )
            )

    mem_pct = snapshot.memory_percent
    if mem_pct >= 90:
        items.append(
            MemoryAdviceItem(
                category="pressure",
                title="内存严重不足",
                detail=f"当前使用率 {mem_pct:.1f}%，系统可能明显卡顿。建议优先处理浏览器、IDE 和开发服务。",
                severity="critical",
            )
        )
    elif mem_pct >= 75:
        items.append(
            MemoryAdviceItem(
                category="pressure",
                title="内存偏高",
                detail=f"当前使用率 {mem_pct:.1f}%，建议关闭不必要的应用或结束可疑进程。",
                severity="warning",
            )
        )

    if groups is None:
        groups = group_memory_hogs(min_mb=60, force_refresh=mem_after_percent is not None)
    for g in groups[:8]:
        # Skip pure system groups in actionable advice unless very large
        if g.key.startswith("system:") and g.total_memory_mb < 1500:
            continue
        severity = "warning" if g.total_memory_mb >= 500 else "info"
        if g.total_memory_mb >= 1200:
            severity = "critical"

        title = f"{g.display_name} 占用约 {g.total_memory_mb:.0f} MB"
        if g.instance_count > 1:
            title += f"（{g.instance_count} 个进程）"

        detail = g.advice_hint
        lower_names = [n.lower() for n in g.process_names]
        if any(n in lower_names for n in ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe")):
            if g.instance_count >= 4 or g.total_memory_mb >= 800:
                detail = (
                    f"检测到 {g.instance_count} 个浏览器相关进程，合计约 {g.total_memory_mb:.0f} MB。"
                    f"这通常意味着打开了过多标签页或扩展占用过高。{g.advice_hint}"
                )
            elif g.instance_count >= 2:
                detail = (
                    f"浏览器有多个进程在运行（正常为多进程架构），"
                    f"但合计 {g.total_memory_mb:.0f} MB 偏高。{g.advice_hint}"
                )
        elif "cursor.exe" in lower_names or "code.exe" in lower_names:
            if g.instance_count >= 3:
                detail = (
                    f"编辑器启动了 {g.instance_count} 个进程，可能打开了多个项目窗口。{g.advice_hint}"
                )
        elif "node.exe" in lower_names and g.instance_count >= 2:
            detail = (
                f"有 {g.instance_count} 个 Node 进程在运行，可能多个 dev server 未关闭。{g.advice_hint}"
            )

        items.append(
            MemoryAdviceItem(
                category="hog",
                title=title,
                detail=detail,
                severity=severity,
            )
        )

    if killed:
        killed_names = [k.get("name", "") for k in killed]
        for name in killed_names:
            key = name.lower()
            profile = _APP_PROFILES.get(key)
            if profile and profile["category"] == "browser":
                items.append(
                    MemoryAdviceItem(
                        category="post_kill",
                        title="浏览器进程已结束",
                        detail=(
                            "下次请养成习惯：定期关闭不用的标签页；"
                            "单窗口标签建议控制在 15 个以内；"
                            "可开启浏览器的「休眠标签页」功能。"
                        ),
                        severity="info",
                    )
                )
                break

    if not items:
        items.append(
            MemoryAdviceItem(
                category="ok",
                title="内存状态良好",
                detail=f"当前内存使用率 {mem_pct:.1f}%，未发现明显异常占用。",
                severity="success",
            )
        )
    return items


def generate_suggestions(
    disk_usage: list[dict[str, float | str]],
    snapshot: SystemSnapshot,
    baseline: dict[str, Any] | None,
) -> list[str]:
    tips: list[str] = []
    c_drive = next((d for d in disk_usage if d.get("drive") == "C"), None)
    if c_drive and float(c_drive.get("free_gb", 0)) < 30:
        tips.append(
            f"C 盘剩余仅 {c_drive['free_gb']} GB，建议优先执行「安全清理」并考虑将 Downloads 迁到 E 盘。"
        )
    if snapshot.memory_percent > 85:
        tips.append(
            f"内存使用率 {snapshot.memory_percent:.1f}% 较高，请查看「进程性能」标签中的内存建议面板。"
        )
        mem_groups = group_memory_hogs(min_mb=60)
        for item in generate_memory_advice(snapshot, groups=mem_groups)[:3]:
            if item.category in ("hog", "pressure"):
                tips.append(f"{item.title}：{item.detail}")
    if snapshot.cpu_percent > 80:
        tips.append(
            f"CPU 使用率 {snapshot.cpu_percent:.1f}% 较高，检查是否有编译/索引/杀毒扫描在后台运行。"
        )
    suspicious = [p for p in snapshot.processes if p.suspicious]
    if suspicious:
        names = ", ".join(f"{p.name}({p.suspicious_reason})" for p in suspicious[:5])
        tips.append(f"相对基线异常进程: {names}")
    if baseline is None:
        tips.append("尚未记录性能基线，建议在系统较空闲时到「设置/记忆」中记录基线。")
    tips.append("E 盘项目依赖的 node_modules、.pnpm-store、Docker 数据已自动保护，不会被清理。")
    tips.append("可在 Windows 设置 → 系统 → 存储 中开启「存储感知」自动清理临时文件。")
    tips.append("定期将 C 盘大文件（安装包、ISO、视频）移至 D/E 盘可显著缓解空间压力。")
    return tips
