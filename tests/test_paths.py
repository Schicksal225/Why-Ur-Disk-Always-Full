"""Tests for the data-directory override used by the GUI and the MCP server."""

from __future__ import annotations

from pathlib import Path

from core.paths import get_data_dir


def test_data_dir_env_override(monkeypatch):
    target = Path(__file__).resolve().parent / ".data-dir-test"
    monkeypatch.setenv("PCOPT_DATA_DIR", str(target))
    assert get_data_dir() == target.resolve()


def test_data_dir_defaults_to_repo_root(monkeypatch):
    monkeypatch.delenv("PCOPT_DATA_DIR", raising=False)
    assert get_data_dir() == Path(__file__).resolve().parent.parent
