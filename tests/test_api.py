"""API and job runner without opening a window."""

from __future__ import annotations

import threading
from pathlib import Path

from app.api import Api
from app.jobs import JobRunner
from app.shortcuts import _powershell


def test_second_job_is_rejected_while_busy():
    started = threading.Event()
    release = threading.Event()

    def worker(_cancel, _emit):
        started.set()
        release.wait(2)
        return {"ok": True}

    runner = JobRunner(lambda _payload: None)
    assert runner.start("scan", worker)["ok"] is True
    assert started.wait(2)
    assert runner.start("scan", worker)["error"] == "请等待当前任务完成"
    release.set()
    runner._thread.join(2)


def test_audit_collects_project_without_writing_cache(tmp_path: Path, monkeypatch):
    saved = []
    monkeypatch.setattr("app.api.save_audit_cache", lambda payload: saved.append(payload))
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (app / "main.js").write_text("console.log(1)\n", encoding="utf-8")
    api = Api.__new__(Api)
    api._store = None
    api._jobs = None
    api._window = None
    api._results = []
    api._findings = []
    api._projects = []
    api._advice = []
    api._categories = {}
    api._cache_targets = []
    summary = api._audit([str(tmp_path)], threading.Event(), lambda _payload: None, False)
    assert summary["projects"] == 1
    assert api._projects[0]["project_type"] == "node"
    assert saved and saved[0]["projects"][0]["name"] == "app"


def test_bridge_exposes_no_public_state():
    public = [name for name in vars(Api(store=_FakeStore())) if not name.startswith("_")]
    assert public == []


class _FakeStore:
    protected_paths: list = []
    process_whitelist: list = []


def test_shortcut_script_uses_wscript():
    script = _powershell(r"C:\Users\A\Desktop\PC Optimizer.lnk", r"C:\App\PCOptimizer.exe", "", r"C:\App", r"C:\App\icon.ico")
    assert "WScript.Shell" in script
    assert "PCOptimizer.exe" in script
    assert "IconLocation" in script
