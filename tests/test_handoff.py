"""The agent hand-off: a short list of rebuildable directories on D and E."""

from __future__ import annotations

import json
from pathlib import Path

from core.handoff import (
    MAX_ENTRIES,
    MIN_ENTRY_BYTES,
    export_handoff,
    export_scope_projects,
    restore_command,
)

MIB = 1024 * 1024


def _project(path: str, dirs: list[tuple[str, str, int]], dirty=False, subprojects=None) -> dict:
    return {
        "path": path,
        "name": path.rsplit("\\", 1)[-1],
        "markers": ["package.json"],
        "kind": "project",
        "project_type": "node",
        "size": sum(item[2] for item in dirs) + 1000,
        "file_count": 10,
        "source_mtime": 0,
        "regenerable": {},
        "regenerable_dirs": [{"path": p, "kind": kind, "bytes": size} for p, kind, size in dirs],
        "subprojects": subprojects or [],
        "git": {"remote": "", "last_commit": "", "dirty": dirty},
        "risk": "low",
        "risk_note": "",
        "secret": "must-not-leak",
    }


def _export(tmp_path: Path, projects: list[dict], drives: list[dict] | None = None) -> tuple[dict, str]:
    folder = export_handoff(
        projects,
        drives if drives is not None else [{"drive": "C", "total_gb": 1, "used_gb": 1, "free_gb": 0, "x": 1}, {"drive": "D", "total_gb": 100, "used_gb": 40, "free_gb": 60, "x": 1}],
        dest_root=tmp_path / "out",
    )
    inventory = json.loads((folder / "inventory.json").read_text(encoding="utf-8"))
    return inventory, (folder / "REPORT.md").read_text(encoding="utf-8")


def test_handoff_lists_rebuildable_dirs_on_d_and_e_only(tmp_path: Path):
    inner = _project("D:\\work\\b\\packages\\lib", [("D:\\work\\b\\packages\\lib\\node_modules", "node_modules", 150 * MIB)])
    projects = [
        _project("C:\\work\\a", [("C:\\work\\a\\node_modules", "node_modules", 900 * MIB)]),
        _project("D:\\work\\b", [("D:\\work\\b\\node_modules", "node_modules", 300 * MIB)], subprojects=[inner]),
        _project("e:\\code\\c", [("e:\\code\\c\\target", "target", 200 * MIB)]),
    ]
    inventory, report = _export(tmp_path, projects)
    assert [item["path"] for item in inventory["items"]] == [
        "D:\\work\\b\\node_modules",
        "e:\\code\\c\\target",
        "D:\\work\\b\\packages\\lib\\node_modules",
    ]
    assert inventory["items"][2]["project_path"] == "D:\\work\\b\\packages\\lib"
    assert [drive["drive"] for drive in inventory["drives"]] == ["D"]
    assert "C:\\work" not in json.dumps(inventory) and "C:\\work" not in report


def test_handoff_orders_by_size_and_rolls_up_small_dirs(tmp_path: Path):
    dirs = [
        ("D:\\w\\a\\node_modules", "node_modules", 150 * MIB),
        ("D:\\w\\a\\dist", "dist", 120 * MIB),
        ("D:\\w\\a\\build", "build", 99 * MIB),
        ("D:\\w\\a\\.venv", ".venv", MIN_ENTRY_BYTES),
    ]
    inventory, _report = _export(tmp_path, [_project("D:\\w\\a", dirs)])
    assert [item["bytes"] for item in inventory["items"]] == [150 * MIB, 120 * MIB, MIN_ENTRY_BYTES]
    assert inventory["rolled_up"] == {"count": 1, "bytes": 99 * MIB}


def test_handoff_caps_entries(tmp_path: Path):
    dirs = [(f"D:\\w\\p{i}\\node_modules", "node_modules", (200 + i) * MIB) for i in range(MAX_ENTRIES + 1)]
    projects = [_project(f"D:\\w\\p{i}", [dirs[i]]) for i in range(MAX_ENTRIES + 1)]
    inventory, _report = _export(tmp_path, projects)
    assert len(inventory["items"]) == MAX_ENTRIES
    assert inventory["rolled_up"] == {"count": 1, "bytes": 200 * MIB}


def test_handoff_carries_dirty_as_a_fact_without_filtering(tmp_path: Path):
    projects = [
        _project("D:\\w\\dirty", [("D:\\w\\dirty\\node_modules", "node_modules", 300 * MIB)], dirty=True),
        _project("D:\\w\\clean", [("D:\\w\\clean\\node_modules", "node_modules", 200 * MIB)], dirty=False),
        _project("D:\\w\\unknown", [("D:\\w\\unknown\\node_modules", "node_modules", 150 * MIB)], dirty=None),
    ]
    inventory, report = _export(tmp_path, projects)
    assert [item["dirty"] for item in inventory["items"]] == [True, False, None]
    assert "是" in report and "否" in report and "未知" in report


def test_restore_command_follows_lock_files(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    assert restore_command(str(root), "node_modules") == "在项目根目录执行 npm install"
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    assert restore_command(str(root), "node_modules") == "在项目根目录执行 npm ci"
    (root / "yarn.lock").write_text("", encoding="utf-8")
    assert restore_command(str(root), "node_modules") == "在项目根目录执行 yarn install"
    (root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert restore_command(str(root), "node_modules") == "在项目根目录执行 pnpm install"
    assert restore_command(str(root), "target") == "在项目根目录执行 cargo build"
    assert restore_command(str(root), "obj") == "在项目根目录执行 dotnet build"
    assert restore_command(str(root), "__pycache__") == "Python 会自动重新生成"
    assert restore_command(str(root), ".venv") == "按项目说明重建虚拟环境"
    assert restore_command(str(root), "dist") == "按该项目的构建命令重新生成"
    assert restore_command(str(root), ".gradle") == "在项目根目录执行 gradle build"


def test_handoff_shape_and_no_leaks(tmp_path: Path):
    inventory, report = _export(tmp_path, [_project("D:\\w\\a", [("D:\\w\\a\\node_modules", "node_modules", 300 * MIB)])])
    assert set(inventory) == {"schema_version", "generated_at", "drives", "rules", "items", "rolled_up"}
    assert inventory["schema_version"] == 2
    assert len(inventory["rules"]) == 3
    assert set(inventory["items"][0]) == {"path", "bytes", "kind", "project_path", "dirty", "restore"}
    assert set(inventory["drives"][0]) == {"drive", "total_gb", "used_gb", "free_gb"}
    assert "must-not-leak" not in json.dumps(inventory) and "must-not-leak" not in report
    assert "D:\\w\\a\\node_modules" in report


def test_handoff_without_candidates_says_so(tmp_path: Path):
    old_cache_project = _project("D:\\w\\old", [])
    del old_cache_project["regenerable_dirs"]
    inventory, report = _export(tmp_path, [old_cache_project])
    assert inventory["items"] == []
    assert inventory["rolled_up"] == {"count": 0, "bytes": 0}
    assert "没有识别到可重建目录" in report


def test_export_scope_ignores_other_drives():
    assert export_scope_projects([_project("C:\\w\\a", [])]) == []
    assert len(export_scope_projects([_project("c:\\w\\a", []), _project("d:\\w\\b", [])])) == 1


def test_needs_rescan_flags_scans_from_before_rebuildable_paths():
    from core.handoff import needs_rescan

    old = _project("D:\\w\\old", [])
    del old["regenerable_dirs"]
    old["regenerable"] = {"node_modules": 5}
    assert needs_rescan([old]) is True
    assert needs_rescan([_project("D:\\w\\new", [("D:\\w\\new\\node_modules", "node_modules", 5)])]) is False
    other_drive = _project("C:\\w\\old", [])
    del other_drive["regenerable_dirs"]
    other_drive["regenerable"] = {"node_modules": 5}
    assert needs_rescan([other_drive]) is False
    no_rebuildable = _project("D:\\w\\plain", [])
    del no_rebuildable["regenerable_dirs"]
    assert needs_rescan([no_rebuildable]) is False
