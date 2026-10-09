"""Drive-level advice."""

from __future__ import annotations

from core.advisor import build_drive_advice
from core.classifier import Finding


def test_low_system_drive_suggests_move_and_hibernation():
    findings = [
        Finding(
            id="cache",
            path=r"C:\Users\A\AppData\Local\Temp",
            label="用户临时文件",
            category="缓存",
            action="cache_clean",
            risk="low",
            size=1024**3,
            reason="可再生缓存",
            suggestion="可直接清理",
            selectable=True,
            selected=True,
        )
    ]
    disks = [{"drive": "C", "total_gb": 200, "used_gb": 180, "free_gb": 20, "percent": 90}]
    advice = build_drive_advice(disks, findings, categories={"视频": 50 * 1024**3, "缓存": 1024**3})
    text = "\n".join(item.detail for item in advice)
    assert any("15%" in item.detail or "剩余" in item.detail for item in advice)
    assert "powercfg -h off" in text
    assert "迁移" in text or "用户文件夹" in text


def test_advice_explains_reclaimable_split():
    findings = [
        Finding(
            id="1",
            path=r"D:\a",
            label="缓存",
            category="缓存",
            action="cache_clean",
            risk="low",
            size=100,
            reason="缓存",
            suggestion="直接清理",
            selectable=True,
        ),
        Finding(
            id="2",
            path=r"D:\b",
            label="安装包",
            category="安装包",
            action="recycle_suggest",
            risk="medium",
            size=50,
            reason="旧安装包",
            suggestion="移入回收站",
            selectable=True,
        ),
    ]
    advice = build_drive_advice(
        [{"drive": "D", "total_gb": 100, "used_gb": 40, "free_gb": 60, "percent": 40}],
        findings,
        categories={"程序": 50},
    )
    blob = " ".join(f"{item.title} {item.detail}" for item in advice)
    assert "直接清理" in blob or "缓存" in blob
    assert "回收站" in blob
