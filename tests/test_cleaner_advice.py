"""Windows locations are advice-only, and cache measurement stays cheap."""

from __future__ import annotations

import os
import time
from pathlib import Path

from core.cleaner import CleanTarget, build_targets, execute_target, scan_all, scan_target


def test_windows_locations_are_not_deleted():
    targets = build_targets([])
    by_id = {item.id: item for item in targets}
    assert by_id["windows_temp"].level == "advice"
    assert by_id["windows_update_cache"].level == "advice"
    for key in ("windows_temp", "windows_update_cache"):
        result = execute_target(by_id[key], [])
        assert result.files_removed == 0
        assert result.freed_bytes == 0
        assert result.errors


def test_measure_does_not_use_delete_check(tmp_path: Path, monkeypatch):
    cache = tmp_path / "Cache"
    cache.mkdir()
    (cache / "part.bin").write_bytes(b"x" * 1200)

    def boom(*_args, **_kwargs):
        raise AssertionError("measuring called the delete check")

    monkeypatch.setattr("core.cleaner.is_safe_delete_target", boom)
    monkeypatch.setattr("core.cleaner.is_path_protected", boom)
    target = CleanTarget(id="chrome_cache", label="Chrome", level="safe", paths=[str(cache)])
    assert scan_target(target, []) == 1200


def test_safe_targets_are_reported_before_advice(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "core.cleaner.scan_target",
        lambda target, protected, min_age_days=0: seen.append(target.id) or 0,
    )
    batches: list[list[str]] = []
    scan_all(build_targets([]), [], on_batch=lambda done: batches.append([item.id for item in done]))
    assert len(batches) == 1
    assert "user_temp" in batches[0]
    assert "windows_temp" not in batches[0]
    assert seen.index("user_temp") < seen.index("windows_temp")


def test_user_temp_respects_age_and_skips_named_dirs(tmp_path: Path):
    temp = tmp_path / "Temp"
    temp.mkdir()
    old = temp / "old.tmp"
    old.write_bytes(b"o" * 100)
    (temp / "new.tmp").write_bytes(b"n" * 40)
    nest = temp / "node_modules"
    nest.mkdir()
    (nest / "pkg.bin").write_bytes(b"p" * 999)
    old_time = time.time() - 3 * 86400
    os.utime(old, (old_time, old_time))
    target = CleanTarget(id="user_temp", label="temp", level="safe", paths=[str(temp)])
    assert scan_target(target, [], min_age_days=1) == 100


def test_downloads_report_counts_only_large_files(tmp_path: Path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "small.bin").write_bytes(b"x" * 1024)
    with open(downloads / "big.bin", "wb") as handle:
        handle.truncate(50 * 1024 * 1024)
    target = CleanTarget(id="downloads_report", label="Downloads", level="risk", paths=[str(downloads)])
    assert scan_target(target, []) == 50 * 1024 * 1024


def test_thumbcache_counts_only_matching_files(tmp_path: Path):
    explorer = tmp_path / "Explorer"
    explorer.mkdir()
    (explorer / "thumbcache_256.db").write_bytes(b"t" * 300)
    (explorer / "iconcache_16.db").write_bytes(b"i" * 500)
    target = CleanTarget(id="thumbcache", label="thumb", level="safe", paths=[str(explorer)])
    assert scan_target(target, []) == 300
