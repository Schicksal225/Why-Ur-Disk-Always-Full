"""Scanner tests against a temporary directory."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.scanner import scan_path


def test_scan_sums_categories_and_skips_reparse(tmp_path: Path):
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "clip.mp4").write_bytes(b"v" * 2048)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_bytes(b"hello")
    empty = tmp_path / "empty"
    empty.mkdir()

    target = tmp_path / "real"
    target.mkdir()
    (target / "secret.bin").write_bytes(b"z" * 100)
    link = tmp_path / "link"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"mklink failed: {created.stderr}")

    project = tmp_path / "proj"
    project.mkdir()
    marker = project / "pyproject.toml"
    marker.write_text("[project]\nname='x'\n", encoding="utf-8")
    (project / "big.bin").write_bytes(b"p" * 4096)

    result = scan_path(
        tmp_path,
        large_min_bytes=1000,
        stale_min_bytes=10**12,
        dup_min_bytes=1000,
        store_depth=3,
        max_children=10,
    )
    assert result.total_bytes == 2048 + 5 + 100 + 4096 + marker.stat().st_size
    assert result.categories.get("视频", 0) == 2048
    assert result.categories.get("文档", 0) == 5
    assert result.skipped_reparse >= 1
    assert result.file_count == 5
    names = {node.name for node in result.tree.children}
    assert "link" not in names or all(child.path != str(link) for child in result.tree.children)
    assert any(record.path.endswith("big.bin") and record.inside_project for record in result.large_files)
    assert any(record.path.endswith("clip.mp4") and not record.inside_project for record in result.large_files)
    assert str(empty) in result.empty_dirs or any(p.endswith(f"{os.sep}empty") for p in result.empty_dirs)


def test_scan_can_cancel(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")

    class Cancel:
        def is_set(self) -> bool:
            return True

    result = scan_path(tmp_path, cancel=Cancel())
    assert result.cancelled is True
