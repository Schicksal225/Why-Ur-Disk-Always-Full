"""Project trees discovered during a scan, plus git metadata and risk notes."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from core.cleaner import format_bytes

REGENERABLE_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".next",
        "__pycache__",
        ".gradle",
    }
)
DOTNET_REGENERABLE = frozenset({"bin", "obj"})


def project_type_from_markers(markers: list[str]) -> str:
    names = {name.lower() for name in markers}
    if any(name.endswith(".sln") or name.endswith(".csproj") for name in names):
        return "dotnet"
    if "package.json" in names:
        return "node"
    if "pyproject.toml" in names or "requirements.txt" in names:
        return "python"
    if "cargo.toml" in names:
        return "rust"
    if "go.mod" in names:
        return "go"
    if "pom.xml" in names:
        return "java"
    if ".git" in names:
        return "git"
    return "other"


def regenerable_name(dirname: str, project_type: str) -> str | None:
    lowered = dirname.lower()
    if lowered in REGENERABLE_DIRS:
        return lowered
    if project_type == "dotnet" and lowered in DOTNET_REGENERABLE:
        return lowered
    return None


def empty_project(path: str, markers: list[str], kind: str) -> dict:
    return {
        "path": path,
        "name": Path(path).name or path,
        "markers": markers,
        "kind": kind,
        "project_type": project_type_from_markers(markers),
        "size": 0,
        "file_count": 0,
        "source_mtime": 0.0,
        "regenerable": {},
        "subprojects": [],
        "git": {"remote": "", "last_commit": "", "dirty": None},
        "risk": "low",
        "risk_note": "",
    }


def read_git_info(path: str, runner=None) -> dict:
    """Read remote, last commit time, and dirty state. Never raises."""
    info = {"remote": "", "last_commit": "", "dirty": None}
    git_dir = Path(path) / ".git"
    config = git_dir / "config"
    try:
        if config.is_file():
            text = config.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^\s*url\s*=\s*(\S+)", text, re.MULTILINE)
            if match:
                info["remote"] = match.group(1)
        head_log = git_dir / "logs" / "HEAD"
        if head_log.is_file():
            lines = [line for line in head_log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
            if lines:
                info["last_commit"] = _commit_time(lines[-1])
    except OSError:
        pass
    info["dirty"] = _git_dirty(path, runner)
    return info


def _commit_time(line: str) -> str:
    # "<old> <new> Name <email> <unix> <tz>\tmessage"
    head = line.split("\t", 1)[0]
    parts = head.split()
    for part in reversed(parts):
        if part.isdigit() and len(part) >= 9:
            try:
                return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(part)))
            except (OverflowError, OSError, ValueError):
                return ""
    return ""


def _git_dirty(path: str, runner) -> bool | None:
    if not (Path(path) / ".git").exists():
        return None
    command = ["git", "-C", path, "status", "--porcelain"]
    try:
        if runner is not None:
            result = runner(command)
        else:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                return None
            result = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not isinstance(result, str):
        return None
    return bool(result.strip())


def apply_risk(project: dict, now: float | None = None) -> dict:
    moment = time.time() if now is None else now
    notes: list[str] = []
    level = "low"
    git = project.get("git") or {}
    if git.get("dirty"):
        level = "high"
        notes.append("有未提交改动，不建议删除")
    if ".git" in {name.lower() for name in project.get("markers", [])} and not git.get("remote"):
        notes.append("无远程仓库，删除后无法恢复")
        if level == "low":
            level = "medium"
    source_mtime = float(project.get("source_mtime") or 0)
    regenerable = int(sum((project.get("regenerable") or {}).values()))
    if source_mtime and moment - source_mtime >= 180 * 86400 and regenerable > 0:
        days = int((moment - source_mtime) / 86400)
        notes.append(f"{days} 天未修改，可再生目录占 {format_bytes(regenerable)}")
    if not notes:
        notes.append("源码目录，本工具不会删除其中的源码")
    project["risk"] = level
    project["risk_note"] = "；".join(notes)
    for child in project.get("subprojects") or []:
        apply_risk(child, moment)
    return project


MAX_GIT_STATUS = 200


def _flatten(projects: list[dict]) -> list[dict]:
    flat: list[dict] = []
    stack = list(projects)
    while stack:
        item = stack.pop()
        flat.append(item)
        stack.extend(item.get("subprojects") or [])
    return flat


def finalize_projects(projects: list[dict], runner=None, now: float | None = None) -> list[dict]:
    """Attach git info in parallel. Past MAX_GIT_STATUS repos, dirty stays None (unknown)."""
    from concurrent.futures import ThreadPoolExecutor

    flat = _flatten(projects)
    ordered = sorted(flat, key=lambda item: int(item.get("size") or 0), reverse=True)
    checked = {id(item) for item in ordered[:MAX_GIT_STATUS]}

    def load(item: dict) -> None:
        if id(item) in checked:
            item["git"] = read_git_info(item["path"], runner=runner)
        else:
            item["git"] = read_git_info(item["path"], runner=lambda _cmd: None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(load, flat))
    for project in projects:
        apply_risk(project, now)
    return projects
