"""Logic tests for the read-only MCP tools (no filesystem, no subprocesses)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.mcp_tools as mcp_tools
from core.mcp_tools import FINDING_FIELDS, ToolError, call_tool
from core.paths import get_data_dir

GB = 1024**3
MB = 1024**2
NOW = datetime.now()


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _finding(id_: str, path: str, action: str, size: int, selectable: bool) -> dict:
    return {
        "id": id_,
        "path": path,
        "label": id_,
        "category": "test",
        "action": action,
        "risk": "low",
        "size": size,
        "reason": "reason",
        "suggestion": "suggestion",
        "selectable": selectable,
        "selected": selectable,
    }


def _tree() -> dict:
    return {
        "name": "C:",
        "path": "C:\\",
        "size": 100 * GB,
        "protected": "",
        "children": [
            {
                "name": "Users",
                "path": "C:\\Users",
                "size": 80 * GB,
                "protected": "",
                "children": [
                    {
                        "name": "A",
                        "path": "C:\\Users\\A",
                        "size": 70 * GB,
                        "protected": "",
                        "children": [
                            {"name": "AppData", "path": "C:\\Users\\A\\AppData", "size": 40 * GB, "protected": "", "children": []},
                            {"name": "Downloads", "path": "C:\\Users\\A\\Downloads", "size": 20 * GB, "protected": "", "children": []},
                        ],
                    }
                ],
            },
            {"name": "Windows", "path": "C:\\Windows", "size": 30 * GB, "protected": "system", "children": []},
        ],
    }


def _cache() -> dict:
    return {
        "results": [
            {"root": "C:\\", "scanned_at": _iso(1.0), "tree": _tree(), "categories": {}, "projects": []},
            {"root": "D:\\", "scanned_at": _iso(30.0), "tree": {"name": "D:", "path": "D:\\", "size": 0, "protected": "", "children": []}},
        ],
        "findings": [
            _finding("c1", "C:\\Users\\A\\AppData\\Local\\Temp", "cache_clean", 3600 * MB, True),
            _finding("c2", "C:\\Users\\A\\npm-cache", "cache_clean", 1800 * MB, True),
            _finding("r1", "C:\\Users\\A\\Downloads\\Setup.exe", "recycle_suggest", 96 * MB, True),
            _finding("a1", "D:\\VM\\ubuntu.vhdx", "advice_only", 45000 * MB, False),
            _finding("o1", "C:\\Windows", "readonly", 33000 * MB, False),
        ],
        "projects": [
            {
                "name": "studio",
                "path": "D:\\work\\studio",
                "project_type": "node",
                "size": 6 * GB,
                "source_mtime": (datetime.now() - timedelta(days=5)).timestamp(),
                "regenerable": {"node_modules": 3 * GB, "dist": 0.4 * GB},
                "regenerable_dirs": [
                    {"path": "D:\\work\\studio\\node_modules", "kind": "node_modules", "bytes": 3 * GB},
                    {"path": "D:\\work\\studio\\dist", "kind": "dist", "bytes": int(0.4 * GB)},
                ],
                "git": {"remote": "https://example.com/studio.git", "last_commit": "2026-09-27 21:04", "dirty": True},
                "risk": "high",
                "risk_note": "has uncommitted changes",
                "subprojects": [
                    {
                        "name": "packages/ui",
                        "path": "D:\\work\\studio\\packages\\ui",
                        "size": int(0.8 * GB),
                        "source_mtime": (datetime.now() - timedelta(days=3)).timestamp(),
                        "regenerable": {".venv": int(0.2 * GB)},
                        "regenerable_dirs": [
                            {"path": "D:\\work\\studio\\packages\\ui\\.venv", "kind": ".venv", "bytes": int(0.2 * GB)}
                        ],
                        "subprojects": [],
                    }
                ],
            },
            {
                "name": "rust-tools",
                "path": "E:\\code\\rust-tools",
                "project_type": "rust",
                "size": 5 * GB,
                "source_mtime": (datetime.now() - timedelta(days=200)).timestamp(),
                "regenerable": {"target": 4 * GB},
                "regenerable_dirs": [{"path": "E:\\code\\rust-tools\\target", "kind": "target", "bytes": 4 * GB}],
                "git": {"remote": "https://example.com/rust-tools.git", "last_commit": "2026-03-22 09:40", "dirty": False},
                "risk": "low",
                "risk_note": "idle",
                "subprojects": [],
            },
            {
                "name": "old-crawler",
                "path": "D:\\archive\\old-crawler",
                "project_type": "python",
                "size": 2 * GB,
                "source_mtime": 0.0,
                "regenerable": {},
                "regenerable_dirs": [],
                "git": {"remote": "", "last_commit": "", "dirty": None},
                "risk": "medium",
                "risk_note": "no remote",
                "subprojects": [],
            },
        ],
    }


@pytest.fixture()
def cache(monkeypatch) -> dict:
    data = _cache()
    monkeypatch.setattr(mcp_tools, "load_audit_cache", lambda: data)
    monkeypatch.setattr(mcp_tools, "deletion_allowed", lambda path, protected: (True, ""))
    monkeypatch.setattr(mcp_tools, "is_cache_clean_path", lambda path: True)
    # execute_findings is called with dry_run=True; its own gate must not reject
    # the synthetic fixture paths before the dry-run branch is reached.
    monkeypatch.setattr("core.executor.deletion_allowed", lambda path, protected: (True, ""))

    class _Store:
        protected_paths: list[str] = []

    monkeypatch.setattr(mcp_tools, "Store", _Store)
    return data


def _payload(tool: str, args: dict | None = None) -> dict:
    blocks = call_tool(tool, args or {})
    assert len(blocks) == 1 and blocks[0]["type"] == "text"
    return json.loads(blocks[0]["text"])


# ---------------------------------------------------------------- schemas
def test_tools_list_has_complete_schemas():
    names = {tool["name"] for tool in mcp_tools.TOOLS}
    assert names == {
        "drives_list",
        "findings_query",
        "dir_children",
        "projects_list",
        "cleanup_plan",
        "handoff_export",
    }
    for tool in mcp_tools.TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    findings = next(tool for tool in mcp_tools.TOOLS if tool["name"] == "findings_query")
    assert set(findings["inputSchema"]["properties"]) == {
        "action",
        "min_size_mb",
        "drive",
        "include_locked",
        "limit",
        "offset",
    }
    children = next(tool for tool in mcp_tools.TOOLS if tool["name"] == "dir_children")
    assert children["inputSchema"]["required"] == ["drive", "path"]
    plan = next(tool for tool in mcp_tools.TOOLS if tool["name"] == "cleanup_plan")
    assert plan["inputSchema"]["required"] == ["candidate_ids"]


# ---------------------------------------------------------------- findings_query
def test_findings_default_hides_locked_rows_and_sorts_by_size(cache):
    payload = _payload("findings_query")
    assert payload["total"] == 3
    assert [row["id"] for row in payload["findings"]] == ["c1", "c2", "r1"]
    assert set(payload["findings"][0]) == set(FINDING_FIELDS)
    assert payload["stale"] is False
    assert payload["scan_age_hours"] == pytest.approx(1.0, abs=0.2)


def test_findings_include_locked(cache):
    payload = _payload("findings_query", {"include_locked": True})
    assert payload["total"] == 5
    assert {row["id"] for row in payload["findings"]} == {"c1", "c2", "r1", "a1", "o1"}


def test_findings_action_size_drive_filters(cache):
    by_action = _payload("findings_query", {"action": "cache_clean"})
    assert [row["id"] for row in by_action["findings"]] == ["c1", "c2"]

    big = _payload("findings_query", {"min_size_mb": 100})
    assert [row["id"] for row in big["findings"]] == ["c1", "c2"]

    on_drive = _payload("findings_query", {"drive": "d", "include_locked": True})
    assert [row["id"] for row in on_drive["findings"]] == ["a1"]


def test_findings_pagination_limit_clamp_and_offset(cache):
    page = _payload("findings_query", {"limit": 2, "offset": 1})
    assert [row["id"] for row in page["findings"]] == ["c2", "r1"]
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 1)

    clamped = _payload("findings_query", {"limit": 500})
    assert clamped["limit"] == 200
    assert clamped["total"] == 3

    beyond = _payload("findings_query", {"offset": 99})
    assert beyond["findings"] == []
    assert beyond["total"] == 3


def test_findings_rejects_bad_arguments(cache):
    with pytest.raises(ToolError):
        call_tool("findings_query", {"action": "nuke"})
    with pytest.raises(ToolError):
        call_tool("findings_query", {"unknown": 1})
    with pytest.raises(ToolError):
        call_tool("findings_query", {"drive": "CD"})
    with pytest.raises(ToolError):
        call_tool("findings_query", {"limit": 0})


def test_findings_recomputes_selectable_from_safety(cache, monkeypatch):
    monkeypatch.setattr(
        mcp_tools,
        "deletion_allowed",
        lambda path, protected: (False, "blocked") if "npm-cache" in path else (True, ""),
    )
    rows = {row["id"]: row for row in _payload("findings_query")["findings"]}
    assert rows["c1"]["selectable"] is True
    assert rows["c2"]["selectable"] is False


def test_findings_cache_clean_gate_is_rechecked(cache, monkeypatch):
    monkeypatch.setattr(mcp_tools, "is_cache_clean_path", lambda path: False)
    rows = {row["id"]: row for row in _payload("findings_query")["findings"]}
    assert rows["c1"]["selectable"] is False


def test_findings_empty_cache(monkeypatch):
    monkeypatch.setattr(mcp_tools, "load_audit_cache", lambda: None)
    payload = _payload("findings_query")
    assert payload["findings"] == []
    assert payload["total"] == 0
    assert payload["stale"] is True
    assert payload["scan_age_hours"] is None


def test_findings_marks_old_scan_stale(cache):
    for item in cache["results"]:
        item["scanned_at"] = _iso(40.0)
    payload = _payload("findings_query")
    assert payload["stale"] is True
    assert payload["scan_age_hours"] == pytest.approx(40.0, abs=0.2)


# ---------------------------------------------------------------- dir_children
def test_dir_children_depth1_and_sorting(cache):
    payload = _payload("dir_children", {"drive": "C", "path": "C:\\Users\\A"})
    node = payload["node"]
    assert node["name"] == "A"
    assert node["protected"] == ""
    assert [child["name"] for child in node["children"]] == ["AppData", "Downloads"]
    assert node["children"][0]["children"] == []


def test_dir_children_depth2_and_case_insensitive_path(cache):
    payload = _payload("dir_children", {"drive": "c", "path": "c:/users", "depth": 2})
    node = payload["node"]
    assert node["name"] == "Users"
    assert [child["name"] for child in node["children"]] == ["A"]
    assert [grand["name"] for grand in node["children"][0]["children"]] == ["AppData", "Downloads"]


def test_dir_children_depth_is_clamped(cache):
    payload = _payload("dir_children", {"drive": "C", "path": "C:\\Users", "depth": 9})
    assert payload["node"]["children"][0]["children"][0]["children"] == []


def test_dir_children_protected_flag(cache):
    payload = _payload("dir_children", {"drive": "C", "path": "C:\\Windows"})
    assert payload["node"]["protected"] == "system"


def test_dir_children_rejects_outside_and_unscanned(cache):
    with pytest.raises(ToolError):
        call_tool("dir_children", {"drive": "C", "path": "C:\\Nope"})
    with pytest.raises(ToolError):
        call_tool("dir_children", {"drive": "E", "path": "E:\\code"})
    with pytest.raises(ToolError):
        call_tool("dir_children", {"drive": "C", "path": "D:\\work"})
    with pytest.raises(ToolError):
        call_tool("dir_children", {"drive": "C", "path": "C:\\Users", "extra": 1})


# ---------------------------------------------------------------- projects_list
def test_projects_default_sort_and_shape(cache):
    payload = _payload("projects_list")
    names = [row["name"] for row in payload["projects"]]
    assert names == ["rust-tools", "studio", "old-crawler"]
    assert payload["total"] == 3
    assert payload["stale"] is False
    studio = payload["projects"][1]
    assert set(studio) == {
        "name",
        "path",
        "project_type",
        "size",
        "regen_bytes",
        "regenerable_dirs",
        "git",
        "risk",
        "risk_note",
    }
    assert studio["regen_bytes"] == 3 * GB + int(0.4 * GB)
    assert studio["git"] == {
        "remote": "https://example.com/studio.git",
        "last_commit": "2026-09-27 21:04",
        "dirty": True,
    }


def test_projects_flattens_subproject_regen_dirs(cache):
    studio = _payload("projects_list")["projects"][1]
    paths = [entry["path"] for entry in studio["regenerable_dirs"]]
    assert "D:\\work\\studio\\node_modules" in paths
    assert "D:\\work\\studio\\packages\\ui\\.venv" in paths
    node_modules = next(entry for entry in studio["regenerable_dirs"] if entry["kind"] == "node_modules")
    assert "npm install" in node_modules["restore"]
    assert all(entry["restore"] for entry in studio["regenerable_dirs"])


def test_projects_size_and_idle_sorts(cache):
    by_size = _payload("projects_list", {"sort": "size"})["projects"]
    assert [row["name"] for row in by_size] == ["studio", "rust-tools", "old-crawler"]
    by_idle = _payload("projects_list", {"sort": "idle"})["projects"]
    assert [row["name"] for row in by_idle] == ["rust-tools", "studio", "old-crawler"]


def test_projects_min_regen_filter(cache):
    payload = _payload("projects_list", {"min_regen_mb": 4000})
    assert [row["name"] for row in payload["projects"]] == ["rust-tools"]
    assert payload["total"] == 1


def test_projects_rejects_bad_arguments(cache):
    with pytest.raises(ToolError):
        call_tool("projects_list", {"sort": "newest"})
    with pytest.raises(ToolError):
        call_tool("projects_list", {"min_regen_mb": -1})
    with pytest.raises(ToolError):
        call_tool("projects_list", {"nope": True})


def test_projects_empty_cache(monkeypatch):
    monkeypatch.setattr(mcp_tools, "load_audit_cache", lambda: None)
    payload = _payload("projects_list")
    assert payload["projects"] == []
    assert payload["total"] == 0
    assert payload["stale"] is True


# ---------------------------------------------------------------- cleanup_plan
def _plan(candidate_ids, **extra):
    args = {"candidate_ids": candidate_ids}
    args.update(extra)
    return _payload("cleanup_plan", args)


def test_cleanup_plan_reports_steps_without_deleting(cache, monkeypatch):
    real = mcp_tools.execute_findings
    calls = {}
    monkeypatch.setattr(mcp_tools, "recycle_block_reason", lambda path, size: None)

    def wrapped(findings, protected_paths, **kwargs):
        calls["findings"] = findings
        calls["kwargs"] = kwargs
        return real(findings, protected_paths, **kwargs)

    monkeypatch.setattr(mcp_tools, "execute_findings", wrapped)
    plan = _plan(["c1", "r1"], min_age_days=7)

    assert plan["plan_id"].startswith("plan-")
    assert plan["note"]
    assert [step["id"] for step in plan["steps"]] == ["c1", "r1"]
    assert [step["method"] for step in plan["steps"]] == ["cache_clean", "recycle_suggest"]
    assert plan["steps"][0]["paths"] == ["C:\\Users\\A\\AppData\\Local\\Temp"]
    assert plan["steps"][0]["would_delete"] == 0
    assert plan["steps"][0]["warning"]
    assert plan["steps"][1]["warning"] is None
    assert calls["kwargs"]["dry_run"] is True
    assert calls["kwargs"]["store"] is None
    assert calls["kwargs"]["min_age_days"] == 7


def test_cleanup_plan_skips_gate_rejected_paths(cache, monkeypatch):
    monkeypatch.setattr(mcp_tools, "recycle_block_reason", lambda path, size: None)
    monkeypatch.setattr(
        mcp_tools,
        "deletion_allowed",
        lambda path, protected: (False, "受保护路径") if "Temp" in path else (True, ""),
    )
    plan = _plan(["c1", "r1"])
    step_paths = [path for step in plan["steps"] for path in step["paths"]]
    assert "C:\\Users\\A\\AppData\\Local\\Temp" not in step_paths
    assert [step["id"] for step in plan["steps"]] == ["r1"]
    assert plan["skipped"][0]["id"] == "c1"
    assert "受保护" in plan["skipped"][0]["reason"]


def test_cleanup_plan_skips_advice_and_cache_gate(cache, monkeypatch):
    monkeypatch.setattr(mcp_tools, "recycle_block_reason", lambda path, size: None)
    monkeypatch.setattr(mcp_tools, "is_cache_clean_path", lambda path: False)
    plan = _plan(["o1", "c1"])
    assert plan["steps"] == []
    reasons = {item["id"]: item["reason"] for item in plan["skipped"]}
    assert "建议" in reasons["o1"]
    assert "缓存" in reasons["c1"]


def test_cleanup_plan_reports_recycle_warning(cache, monkeypatch):
    monkeypatch.setattr(mcp_tools, "recycle_block_reason", lambda path, size: "回收站已关闭")
    plan = _plan(["r1"])
    assert "回收站已关闭" in plan["steps"][0]["warning"]


def test_cleanup_plan_has_no_side_effects(cache, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("cleanup_plan attempted a filesystem change")

    monkeypatch.setattr("os.remove", boom)
    monkeypatch.setattr("shutil.rmtree", boom)
    monkeypatch.setattr("core.executor.send_to_recycle_bin", boom)
    monkeypatch.setattr("core.executor._clean_directory", boom)
    monkeypatch.setattr(mcp_tools, "recycle_block_reason", lambda path, size: None)
    history_writes = []

    class _Store:
        protected_paths: list[str] = []

        def add_storage_run(self, *args, **kwargs):
            history_writes.append(args)

    monkeypatch.setattr(mcp_tools, "Store", _Store)

    plan = _plan(["c1", "r1"])
    assert len(plan["steps"]) == 2
    assert history_writes == []


def test_cleanup_plan_rejects_bad_input(cache):
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {})
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {"candidate_ids": []})
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {"candidate_ids": [1]})
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {"candidate_ids": ["nope"]})
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {"candidate_ids": ["c1"], "min_age_days": -1})
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {"candidate_ids": ["c1"], "extra": 1})


def test_cleanup_plan_requires_cache(monkeypatch):
    monkeypatch.setattr(mcp_tools, "load_audit_cache", lambda: None)
    with pytest.raises(ToolError):
        call_tool("cleanup_plan", {"candidate_ids": ["c1"]})


# ---------------------------------------------------------------- handoff_export
def _fake_export(monkeypatch, *, items: int = 3, error: Exception | None = None, folder: Path | None = None):
    captured: dict = {"projects": None, "drives": None, "dest_root": None}
    target = folder or (get_data_dir() / "exports" / "handoff-20260101-000000")

    def fake(projects, drives, dest_root=None):
        captured["projects"] = projects
        captured["drives"] = drives
        captured["dest_root"] = dest_root
        if error is not None:
            raise error
        return target

    monkeypatch.setattr(mcp_tools, "export_handoff", fake)
    monkeypatch.setattr(mcp_tools, "_count_export_items", lambda _folder: items)
    monkeypatch.setattr(
        mcp_tools,
        "get_disk_usage",
        lambda: [{"drive": "D", "total_gb": 1, "used_gb": 0.5, "free_gb": 0.5, "percent": 50}],
    )
    return captured, target


def test_handoff_export_defaults_to_the_exports_folder(cache, monkeypatch):
    captured, folder = _fake_export(monkeypatch)
    payload = json.loads(call_tool("handoff_export", {})[0]["text"])

    assert payload["folder"] == str(folder)
    assert payload["report"] == str(folder / "REPORT.md")
    assert payload["inventory"] == str(folder / "inventory.json")
    assert payload["items"] == 3
    assert captured["dest_root"] is None
    assert captured["projects"] == cache["projects"]
    assert captured["drives"][0]["drive"] == "D"


def test_handoff_export_keeps_dest_under_exports(cache, monkeypatch):
    captured, folder = _fake_export(monkeypatch)
    payload = json.loads(call_tool("handoff_export", {"dest": "agent/run1"})[0]["text"])
    exports_root = (get_data_dir() / "exports").resolve()
    assert captured["dest_root"] == (exports_root / "agent" / "run1").resolve()
    assert (exports_root / "agent" / "run1").resolve().is_relative_to(exports_root)
    assert payload["items"] == 3


@pytest.mark.parametrize("dest", ["C:\\evil", "..\\escape", "D:foo", "\\\\server\\share", "   "])
def test_handoff_export_rejects_escapes(cache, monkeypatch, dest):
    calls = []
    monkeypatch.setattr(mcp_tools, "export_handoff", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ToolError):
        call_tool("handoff_export", {"dest": dest})
    assert calls == []


def test_handoff_export_wraps_oserror(cache, monkeypatch):
    _fake_export(monkeypatch, error=OSError("disk full"))
    with pytest.raises(ToolError):
        call_tool("handoff_export", {})


def test_handoff_export_requires_cache(monkeypatch):
    monkeypatch.setattr(mcp_tools, "load_audit_cache", lambda: None)
    with pytest.raises(ToolError):
        call_tool("handoff_export", {})


def test_handoff_export_rejects_unknown_argument(cache):
    with pytest.raises(ToolError):
        call_tool("handoff_export", {"target": "x"})
