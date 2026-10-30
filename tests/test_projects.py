"""Project discovery and handoff export."""

from __future__ import annotations

from pathlib import Path

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


def test_git_status_only_runs_for_projects_with_rebuildable_dirs(tmp_path: Path):
    calls: list[str] = []

    def runner(command):
        calls.append(command[2])
        return ""

    def project(name: str, regenerable: dict) -> dict:
        folder = tmp_path / name
        (folder / ".git").mkdir(parents=True)
        return {
            "path": str(folder),
            "name": name,
            "markers": [".git"],
            "kind": "project",
            "project_type": "git",
            "size": 10,
            "file_count": 1,
            "source_mtime": 0,
            "regenerable": regenerable,
            "regenerable_dirs": [{"path": str(folder / "node_modules"), "kind": "node_modules", "bytes": 9}] if regenerable else [],
            "subprojects": [],
            "git": {"remote": "", "last_commit": "", "dirty": None},
            "risk": "low",
            "risk_note": "",
        }

    finalize_projects([project("with", {"node_modules": 9}), project("without", {})], runner=runner)
    assert calls == [str(tmp_path / "with")]
