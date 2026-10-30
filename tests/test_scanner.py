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


def test_regenerable_dir_keeps_its_path_and_skips_inner_records(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text("{}", encoding="utf-8")
    (app / "src.js").write_bytes(b"s" * 2000)
    modules = app / "node_modules" / "pkg"
    modules.mkdir(parents=True)
    (modules / "index.js").write_bytes(b"m" * 3000)
    (modules / "package.json").write_text("{}", encoding="utf-8")
    extra = app / "extra" / "node_modules"
    extra.mkdir(parents=True)
    (extra / "lib.js").write_bytes(b"e" * 1500)

    result = scan_path(tmp_path, large_min_bytes=1000, store_depth=6)

    project = result.projects[0]
    found = {item["path"]: item for item in project["regenerable_dirs"]}
    top = next(item for path, item in found.items() if path.endswith("node_modules") and "extra" not in path)
    assert top["kind"] == "node_modules"
    assert top["bytes"] >= 3000
    assert any(path.endswith("extra\\node_modules") or path.endswith("extra/node_modules") for path in found)
    assert project["regenerable"]["node_modules"] == sum(item["bytes"] for item in found.values())
    assert len(result.projects) == 1
    assert project["subprojects"] == []
    assert any(item.path.endswith("src.js") for item in result.large_files)
    assert not any("node_modules" in item.path for item in result.large_files)
    assert result.categories["其他"] == result.total_bytes


def test_system_tree_sums_without_children_but_keeps_advice_dirs(tmp_path: Path, monkeypatch):
    windows = tmp_path / "Windows"
    system32 = windows / "System32"
    system32.mkdir(parents=True)
    (system32 / "a.dll").write_bytes(b"w" * 5000)
    temp = windows / "Temp"
    temp.mkdir()
    (temp / "t.tmp").write_bytes(b"t" * 700)
    monkeypatch.setattr("core.scanner.is_system_tree", lambda path: str(path).endswith("Windows"))
    monkeypatch.setattr(
        "core.scanner.get_advice_only_locations",
        lambda: [(str(temp), "Windows 临时目录", "reason", "suggestion")],
    )

    result = scan_path(tmp_path, large_min_bytes=1000, store_depth=6)

    node = next(child for child in result.tree.children if child.name == "Windows")
    assert node.size == 5700
    assert node.protected == "system"
    assert node.children == []
    assert result.large_files == []
    assert [(item[0], item[1], item[2]) for item in result.advice_dirs] == [(str(temp), 700, "Windows 临时目录")]
