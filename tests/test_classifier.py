"""Classification rules."""

from __future__ import annotations

import os
import time
from pathlib import Path

from core.classifier import classify_file, classify_scan
from core.scanner import DirNode, FileRecord, ScanResult


def _result(**kwargs) -> ScanResult:
    base = dict(
        root=r"C:\\",
        total_bytes=0,
        file_count=0,
        dir_count=0,
        skipped_errors=0,
        skipped_reparse=0,
        cancelled=False,
        tree=DirNode(path=r"C:\\", name="C:", size=0, file_count=0, protected="", children=[]),
        categories={},
        large_files=[],
        stale_files=[],
        cache_dirs=[],
        advice_dirs=[],
        empty_dirs=[],
        dup_candidates=[],
        projects=[],
        scanned_at="2026-10-09T00:00:00",
    )
    base.update(kwargs)
    return ScanResult(**base)


def test_old_installer_in_downloads_is_recycle():
    downloads = os.path.join(os.path.expanduser("~"), "Downloads", "setup.exe")
    old = time.time() - 120 * 86400
    finding = classify_file(downloads, size=30 * 1024 * 1024, mtime=old, inside_project=False)
    assert finding is not None
    assert finding.action == "recycle_suggest"
    assert finding.selectable is True
    assert "安装包" in finding.reason or "压缩" in finding.reason or "下载" in finding.reason


def test_system_file_is_readonly():
    finding = classify_file(r"C:\Windows\System32\ntoskrnl.exe", size=10**7, mtime=time.time(), inside_project=False)
    assert finding is not None
    assert finding.action == "readonly"
    assert finding.selectable is False


def test_vhdx_and_video_are_advice_only():
    image = classify_file(r"D:\vm\disk.vhdx", size=5 * 10**9, mtime=time.time(), inside_project=False)
    assert image is not None and image.action == "advice_only" and image.selectable is False
    video = classify_file(r"D:\movies\film.mkv", size=600 * 1024 * 1024, mtime=time.time(), inside_project=False)
    assert video is not None and video.action == "advice_only"


def test_wechat_file_is_advice_only():
    path = r"D:\WeChat Files\wxid\FileStorage\Video\1.mp4"
    finding = classify_file(path, size=800 * 1024 * 1024, mtime=time.time(), inside_project=False)
    assert finding is not None
    assert finding.action == "advice_only"
    assert "微信" in finding.reason or "聊天" in finding.reason


def test_project_file_is_not_deletable():
    finding = classify_file(r"D:\work\app\dist\movie.mp4", size=800 * 1024 * 1024, mtime=time.time(), inside_project=True)
    assert finding is not None
    assert finding.action == "readonly"
    assert finding.selectable is False


def test_scan_cache_and_windows_temp_actions(tmp_path: Path):
    temp = os.environ["TEMP"]
    cache = os.path.join(temp, "pcoptimizer-cache-sample")
    result = _result(
        cache_dirs=[(cache, 4096)],
        advice_dirs=[(r"C:\Windows\Temp", 8192, "Windows 临时目录")],
        empty_dirs=[str(tmp_path / "gone")],
        tree=DirNode(
            path=r"C:\\",
            name="C:",
            size=100,
            file_count=1,
            protected="",
            children=[
                DirNode(path=r"C:\Windows", name="Windows", size=80, file_count=1, protected="system", children=[]),
                DirNode(path=r"C:\Program Files", name="Program Files", size=20, file_count=1, protected="system", children=[]),
            ],
        ),
    )
    findings = classify_scan(result)
    by_path = {item.path: item for item in findings}
    assert by_path[cache].action == "cache_clean"
    assert by_path[r"C:\Windows\Temp"].action == "advice_only"
    assert by_path[r"C:\Windows"].action == "readonly"
    assert by_path[r"C:\Program Files"].action == "readonly"
    assert by_path[r"C:\Program Files"].suggestion
    empty = [item for item in findings if item.category == "空文件夹"]
    assert empty and empty[0].action == "recycle_suggest" and empty[0].selected is False
