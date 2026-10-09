"""Staged duplicate detection."""

from __future__ import annotations

from pathlib import Path

from core.duplicates import find_duplicates


def test_duplicates_keep_shortest_path(tmp_path: Path):
    keep = tmp_path / "a.bin"
    other_dir = tmp_path / "nested"
    other_dir.mkdir()
    other = other_dir / "copy.bin"
    payload = b"same-bytes" * 1000
    keep.write_bytes(payload)
    other.write_bytes(payload)
    unique = tmp_path / "unique.bin"
    unique.write_bytes(b"different" * 1000)

    groups = find_duplicates(
        [
            (str(keep), keep.stat().st_size, keep.stat().st_mtime),
            (str(other), other.stat().st_size, other.stat().st_mtime),
            (str(unique), unique.stat().st_size, unique.stat().st_mtime),
        ],
        min_size=100,
    )
    assert len(groups) == 1
    assert groups[0].keep == str(keep)
    assert groups[0].remove == [str(other)]


def test_same_size_different_content_is_not_duplicate(tmp_path: Path):
    left = tmp_path / "left.bin"
    right = tmp_path / "right.bin"
    left.write_bytes(b"a" * 2000)
    right.write_bytes(b"b" * 2000)
    groups = find_duplicates(
        [
            (str(left), 2000, 1),
            (str(right), 2000, 2),
        ],
        min_size=100,
    )
    assert groups == []
