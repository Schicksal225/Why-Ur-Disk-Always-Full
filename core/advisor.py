"""Disk-level conclusions: what is there, what can be freed, and what to do by hand."""

from __future__ import annotations

from dataclasses import dataclass

from core.classifier import Finding
from core.cleaner import format_bytes


@dataclass
class DriveAdvice:
    title: str
    detail: str
    severity: str


def build_drive_advice(
    disks: list[dict],
    findings: list[Finding],
    categories: dict[str, int] | None = None,
) -> list[DriveAdvice]:
    items: list[DriveAdvice] = []
    if categories:
        ranked = sorted(categories.items(), key=lambda item: item[1], reverse=True)[:6]
        ranked = [(name, size) for name, size in ranked if size > 0]
        if ranked:
            detail = "、".join(f"{name} {format_bytes(size)}" for name, size in ranked)
            items.append(DriveAdvice("磁盘里主要是这些", detail, "info"))

    cache_bytes = sum(item.size for item in findings if item.action == "cache_clean")
    recycle_bytes = sum(item.size for item in findings if item.action == "recycle_suggest")
    advice_bytes = sum(item.size for item in findings if item.action == "advice_only")
    items.append(
        DriveAdvice(
            "可处理空间",
            (
                f"缓存可直接清理约 {format_bytes(cache_bytes)}。"
                f"建议移入回收站约 {format_bytes(recycle_bytes)}（可撤销）。"
                f"另有约 {format_bytes(advice_bytes)} 只作提醒，不会自动删除。"
            ),
            "info",
        )
    )

    for disk in disks:
        total = float(disk.get("total_gb") or 0)
        free = float(disk.get("free_gb") or 0)
        if total <= 0:
            continue
        if str(disk.get("drive", "")).upper() == "C" and free / total < 0.15:
            items.append(
                DriveAdvice(
                    "系统盘空间不足",
                    (
                        f"C 盘剩余 {disk.get('free_gb')} GB（低于 15%）。"
                        "可以把桌面、文档、下载等用户文件夹迁移到数据盘："
                        "设置 → 系统 → 存储 → 高级存储设置 → 保存新内容的地方。"
                        "若仍不够，可在管理员终端执行 powercfg -h off 关闭休眠以删除 hiberfil.sys。"
                        "本工具不会代为执行这两项。"
                    ),
                    "warning",
                )
            )
    return items
