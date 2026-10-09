"""Duplicate files: size, then the first 64KB, then the full hash."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class DuplicateGroup:
    size: int
    keep: str
    remove: list[str]
    reason: str


def _hash_file(path: str, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        if limit is None:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        else:
            digest.update(handle.read(limit))
    return digest.hexdigest()


def find_duplicates(
    entries: list[tuple[str, int, float]],
    min_size: int = 1024 * 1024,
) -> list[DuplicateGroup]:
    by_size: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for path, size, mtime in entries:
        if size >= min_size:
            by_size[size].append((path, mtime))

    groups: list[DuplicateGroup] = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        by_partial: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for path, mtime in items:
            try:
                partial = _hash_file(path, 64 * 1024)
            except OSError:
                continue
            by_partial[partial].append((path, mtime))
        for copies in by_partial.values():
            if len(copies) < 2:
                continue
            by_full: dict[str, list[tuple[str, float]]] = defaultdict(list)
            for path, mtime in copies:
                try:
                    full = _hash_file(path)
                except OSError:
                    continue
                by_full[full].append((path, mtime))
            for same in by_full.values():
                if len(same) < 2:
                    continue
                ordered = sorted(same, key=lambda item: (len(item[0]), item[1], item[0]))
                keep = ordered[0][0]
                remove = [path for path, _mtime in ordered[1:]]
                groups.append(
                    DuplicateGroup(
                        size=size,
                        keep=keep,
                        remove=remove,
                        reason="内容相同，保留路径更短的一份",
                    )
                )
    return groups
