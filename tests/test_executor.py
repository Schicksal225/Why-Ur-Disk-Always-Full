"""Executor must re-check safety and honor dry-run."""

from __future__ import annotations

import os
from pathlib import Path

from core.classifier import Finding
from core.executor import execute_findings


def _finding(path: str, action: str, size: int = 4) -> Finding:
    return Finding(
        id=action + path,
        path=path,
        label=path,
        category="测试",
        action=action,
        risk="low",
        size=size,
        reason="测试",
        suggestion="测试",
        selectable=action in ("cache_clean", "recycle_suggest"),
        selected=True,
    )


def test_dry_run_does_not_delete_or_recycle(tmp_path: Path):
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"data")
    recycled: list[str] = []
    results = execute_findings(
        [_finding(str(target), "recycle_suggest", size=4)],
        protected_paths=[],
        dry_run=True,
        recycle_fn=lambda path: recycled.append(path) or (True, ""),
    )
    assert target.exists()
    assert recycled == []
    assert results[0].ok is True
    assert results[0].freed_bytes == 4


def test_system_path_never_reaches_recycle():
    recycled: list[str] = []
    results = execute_findings(
        [_finding(r"C:\Windows\System32\kernel32.dll", "recycle_suggest", size=100)],
        protected_paths=[],
        dry_run=False,
        recycle_fn=lambda path: recycled.append(path) or (True, ""),
    )
    assert recycled == []
    assert results[0].ok is False


def test_advice_only_is_not_executed(tmp_path: Path):
    target = tmp_path / "disk.vhdx"
    target.write_bytes(b"data")
    recycled: list[str] = []
    results = execute_findings(
        [_finding(str(target), "advice_only")],
        protected_paths=[],
        dry_run=False,
        recycle_fn=lambda path: recycled.append(path) or (True, ""),
    )
    assert target.exists()
    assert recycled == []
    assert results[0].ok is False


def test_recycle_uses_callback_for_allowed_file(tmp_path: Path):
    target = tmp_path / "old.zip"
    target.write_bytes(b"zip!")
    recycled: list[str] = []

    def _recycle(path: str) -> tuple[bool, str]:
        recycled.append(path)
        os.remove(path)
        return True, ""

    results = execute_findings(
        [_finding(str(target), "recycle_suggest", size=4)],
        protected_paths=[],
        dry_run=False,
        recycle_fn=_recycle,
    )
    assert recycled == [str(target)]
    assert results[0].ok is True
    assert not target.exists()


def test_recycle_refuses_when_file_exceeds_bin_capacity(tmp_path: Path):
    target = tmp_path / "big.bin"
    target.write_bytes(b"abcd")
    called: list[str] = []

    def policy(_path: str) -> dict:
        return {"nuke": False, "max_bytes": 1024}

    results = execute_findings(
        [_finding(str(target), "recycle_suggest", size=10_000_000)],
        protected_paths=[],
        dry_run=False,
        recycle_fn=lambda path: called.append(path) or (True, ""),
        recycle_policy=policy,
    )
    assert called == []
    assert target.exists()
    assert results[0].ok is False
    assert "容量" in results[0].message


def test_recycle_refuses_when_bin_is_disabled(tmp_path: Path):
    target = tmp_path / "note.txt"
    target.write_text("x", encoding="utf-8")
    called: list[str] = []
    results = execute_findings(
        [_finding(str(target), "recycle_suggest", size=1)],
        protected_paths=[],
        dry_run=False,
        recycle_fn=lambda path: called.append(path) or (True, ""),
        recycle_policy=lambda _path: {"nuke": True, "max_bytes": 10**12},
    )
    assert called == []
    assert results[0].ok is False
    assert "关闭回收站" in results[0].message


def test_cache_clean_rejects_path_outside_whitelist():
    root = Path(os.environ["USERPROFILE"]) / "PCOptimizerAuditTest"
    root.mkdir(exist_ok=True)
    target = root / "a.tmp"
    target.write_text("x", encoding="utf-8")
    try:
        results = execute_findings(
            [_finding(str(root), "cache_clean", size=1)],
            protected_paths=[],
            dry_run=False,
        )
        assert target.exists()
        assert results[0].ok is False
    finally:
        if target.exists():
            target.unlink()
        if root.exists():
            root.rmdir()
