"""Project discovery and handoff export."""

from __future__ import annotations

import json
from pathlib import Path

from core.handoff import export_handoff
from core.projects import apply_risk, finalize_projects, read_git_info
from core.scanner import scan_path


def test_scan_splits_nested_projects_and_regenerable_dirs(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (app / "src").mkdir()
    (app / "src" / "main.js").write_text("console.log(1)\n", encoding="utf-8")
    modules = app / "node_modules" / "leftpad"
    modules.mkdir(parents=True)
    (modules / "index.js").write_bytes(b"x" * 3000)
    (modules / "package.json").write_text("{}", encoding="utf-8")
    nested = app / "packages" / "lib"
    nested.mkdir(parents=True)
    (nested / "pyproject.toml").write_text("[project]\nname='lib'\n", encoding="utf-8")
    (nested / "lib.py").write_text("x = 1\n", encoding="utf-8")

    result = scan_path(tmp_path, store_depth=6)
    assert len(result.projects) == 1
    project = result.projects[0]
    assert project["project_type"] == "node"
    assert project["regenerable"].get("node_modules", 0) >= 3000
    assert [item["project_type"] for item in project["subprojects"]] == ["python"]
    assert project["path"].endswith("app")


def test_git_metadata_and_risk(tmp_path: Path):
    repo = tmp_path / "repo"
    git = repo / ".git"
    (git / "logs").mkdir(parents=True)
    (git / "config").write_text('[remote "origin"]\n\turl = https://example.com/repo.git\n', encoding="utf-8")
    (git / "logs" / "HEAD").write_text(
        "a b Ada <ada@example.com> 1700000000 +0800\tcommit: init\n",
        encoding="utf-8",
    )
    info = read_git_info(str(repo), runner=lambda _cmd: " M src/main.py\n")
    assert info["remote"] == "https://example.com/repo.git"
    assert info["last_commit"].startswith("2023-")
    assert info["dirty"] is True

    project = {
        "path": str(repo),
        "markers": [".git", "package.json"],
        "kind": "project",
        "project_type": "node",
        "size": 10,
        "file_count": 1,
        "source_mtime": 1,
        "regenerable": {"node_modules": 4096},
        "subprojects": [],
        "git": info,
        "risk": "",
        "risk_note": "",
    }
    apply_risk(project, now=200 * 86400)
    assert project["risk"] == "high"
    assert "未提交" in project["risk_note"]


def test_handoff_writes_rules_and_metadata_only(tmp_path: Path):
    projects = finalize_projects(
        [
            {
                "path": str(tmp_path / "app"),
                "name": "app",
                "markers": ["package.json"],
                "kind": "project",
                "project_type": "node",
                "size": 100,
                "file_count": 2,
                "source_mtime": 0,
                "regenerable": {"node_modules": 50},
                "subprojects": [],
                "git": {"remote": "", "last_commit": "", "dirty": None},
                "risk": "low",
                "risk_note": "源码目录，本工具不会删除其中的源码",
            }
        ],
        runner=lambda _cmd: "",
    )
    folder = export_handoff(
        projects,
        [{"path": str(tmp_path / "cache"), "label": "缓存", "category": "缓存", "action": "cache_clean", "risk": "low", "size": 4, "reason": "可再生", "suggestion": "可清理", "secret": "nope"}],
        [{"drive": "D", "total_gb": 100, "used_gb": 40, "free_gb": 60}],
        dest_root=tmp_path / "out",
    )
    inventory = json.loads((folder / "inventory.json").read_text(encoding="utf-8"))
    report = (folder / "REPORT.md").read_text(encoding="utf-8")
    assert inventory["schema_version"] == 1
    assert inventory["safety_rules"]["never_delete_files"]
    assert inventory["projects"][0]["path"].endswith("app")
    assert "secret" not in json.dumps(inventory)
    assert "给接手 agent 的操作规则" in report
    assert "app (node)" in report
    assert "不得删除" in report
