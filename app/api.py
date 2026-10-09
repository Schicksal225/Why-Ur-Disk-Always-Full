"""Methods exposed to the web view. None of them touch the DOM directly."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from app.jobs import JobRunner
from app.shortcuts import create_shortcuts
from core.advisor import build_drive_advice
from core.cleaner import build_targets, execute_target, format_bytes, get_disk_usage, scan_all
from core.classifier import classify_scan, findings_from_duplicates
from core.duplicates import find_duplicates
from core.executor import execute_findings
from core.handoff import export_handoff
from core.perf import collect_system_snapshot, generate_memory_advice, generate_suggestions, kill_processes
from core.projects import finalize_projects
from core.scanner import load_audit_cache, result_from_dict, result_to_dict, save_audit_cache, scan_path
from core.store import Store


class Api:
    def __init__(self, store: Store | None = None) -> None:
        self._store = store or Store()
        self._jobs = JobRunner(self._emit)
        self._window = None
        self._results: list = []
        self._findings: list[dict] = []
        self._projects: list[dict] = []
        self._advice: list[dict] = []
        self._categories: dict[str, int] = {}
        self._cache_targets: list[dict] = []

    # pywebview exposes every public attribute and method to page script, and
    # walks public objects recursively. Keep state and helpers underscored.
    def _bind(self, window) -> None:
        self._window = window

    def _emit(self, payload: dict) -> None:
        if self._window is None:
            return
        self._window.evaluate_js("window.onProgress(" + json.dumps(payload, ensure_ascii=False) + ")")

    def get_version(self) -> str:
        from core.paths import get_bundle_dir

        try:
            return (get_bundle_dir() / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def get_overview(self) -> dict:
        disks = get_disk_usage()
        snap = collect_system_snapshot(
            top_n=5,
            cpu_interval=0.2,
            process_whitelist=self._store.process_whitelist,
            current_pid=os.getpid(),
        )
        tips = generate_suggestions(disks, snap)
        return {"disks": disks, "snapshot": _snapshot(snap), "tips": tips, "categories": self._categories}

    def load_cached(self) -> dict:
        payload = load_audit_cache()
        if not payload:
            return {"ok": False}
        self._apply_payload(payload)
        return {"ok": True, "summary": self._summary()}

    def start_scan(self, drives, duplicates: bool = False) -> dict:
        letters = [str(item).rstrip(":\\") for item in (drives or [])]
        if not letters:
            return {"ok": False, "error": "请至少选择一个盘"}
        roots = [f"{letter}:\\" for letter in letters]
        want_dup = bool(duplicates)

        def work(cancel, emit):
            return self._audit(roots, cancel, emit, want_dup)

        return self._jobs.start("scan", work)

    def cancel_scan(self) -> dict:
        return self._jobs.cancel()

    def get_results(self) -> dict:
        return {
            "findings": self._findings,
            "categories": self._categories,
            "advice": self._advice,
            "trees": [_tree(result.tree) for result in self._results if result.tree is not None],
            "scans": [{"root": result.root, "scanned_at": result.scanned_at} for result in self._results],
            "summary": self._summary(),
        }

    def get_projects(self) -> dict:
        return {"projects": self._projects}

    def start_cache_scan(self) -> dict:
        def work(_cancel, emit):
            emit({"type": "progress", "message": "正在扫描已知缓存"})
            targets = scan_all(build_targets(self._store.protected_paths), self._store.protected_paths)
            self._cache_targets = [
                {
                    "id": item.id,
                    "label": item.label,
                    "level": item.level,
                    "size": item.reclaimable_bytes,
                    "description": item.description,
                    "selected": item.level == "safe",
                }
                for item in targets
            ]
            return {"count": len(self._cache_targets)}

        return self._jobs.start("cache", work)

    def get_cache_targets(self) -> dict:
        return {"targets": self._cache_targets}

    def execute(self, finding_ids) -> dict:
        chosen_ids = set(finding_ids or [])
        chosen = [item for item in self._findings if item["id"] in chosen_ids and item.get("selectable")]
        if not chosen:
            return {"ok": False, "error": "没有可执行的项目"}

        def work(_cancel, emit):
            emit({"type": "progress", "message": "正在执行勾选项目"})
            from core.classifier import Finding

            objects = [Finding(**{key: item[key] for key in Finding.__dataclass_fields__}) for item in chosen]
            results = execute_findings(
                objects,
                self._store.protected_paths,
                store=self._store,
                min_age_days=int(self._store.get_preference("temp_min_age_days", 1)),
            )
            done_paths = {item.path for item in results if item.ok and item.action == "recycle_suggest"}
            self._findings = [item for item in self._findings if item["path"] not in done_paths]
            return {
                "results": [asdict(item) for item in results],
                "freed": format_bytes(sum(item.freed_bytes for item in results if item.ok)),
                "ok_count": sum(1 for item in results if item.ok),
                "fail_count": sum(1 for item in results if not item.ok),
            }

        return self._jobs.start("execute", work)

    def execute_caches(self, target_ids) -> dict:
        ids = set(target_ids or [])
        targets = [item for item in build_targets(self._store.protected_paths) if item.id in ids and item.level == "safe"]
        if not targets:
            return {"ok": False, "error": "没有可直接清理的缓存"}

        def work(_cancel, emit):
            emit({"type": "progress", "message": "正在清理缓存"})
            freed = 0
            lines = []
            min_age = int(self._store.get_preference("temp_min_age_days", 1))
            for target in targets:
                result = execute_target(target, self._store.protected_paths, min_age_days=min_age)
                freed += result.freed_bytes
                lines.append({"label": result.label, "freed": result.freed_bytes, "errors": result.errors[:3]})
            self._store.add_clean_run({"freed_bytes": freed, "summary": f"{len(lines)} 项，释放 {format_bytes(freed)}", "details": lines})
            return {"freed": format_bytes(freed), "lines": lines}

        return self._jobs.start("clean", work)

    def export_handoff(self) -> dict:
        if not self._projects and not self._findings:
            return {"ok": False, "error": "请先盘查，再导出"}
        folder = export_handoff(self._projects, self._findings, get_disk_usage())
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except OSError:
            pass
        return {"ok": True, "path": str(folder / "REPORT.md")}

    def get_processes(self) -> dict:
        snap = collect_system_snapshot(
            top_n=40,
            cpu_interval=0.4,
            process_whitelist=self._store.process_whitelist,
            current_pid=os.getpid(),
        )
        advice = [
            {"title": item.title, "detail": item.detail, "severity": item.severity}
            for item in generate_memory_advice(snap)
        ]
        return {"snapshot": _snapshot(snap), "advice": advice}

    def kill_processes(self, pids) -> dict:
        result = kill_processes([int(pid) for pid in pids], process_whitelist=self._store.process_whitelist, current_pid=os.getpid())
        self._store.add_kill_run(result)
        return result

    def get_settings(self) -> dict:
        return {
            "protected_paths": self._store.protected_paths,
            "process_whitelist": self._store.process_whitelist,
            "clean_history": self._store.get_clean_history()[:10],
            "storage_history": self._store.get_storage_history()[:10],
        }

    def save_settings(self, payload: dict) -> dict:
        payload = payload or {}
        self._store.config["protected_paths"] = list(payload.get("protected_paths") or [])
        self._store.config["process_whitelist"] = [str(item).lower() for item in payload.get("process_whitelist") or []]
        self._store.save_config()
        return {"ok": True}

    def create_shortcut(self) -> dict:
        icon = _icon_path()
        created = create_shortcuts(str(icon) if icon else None)
        return {"ok": True, "paths": created}

    def _audit(self, roots: list[str], cancel, emit, duplicates: bool) -> dict:
        results = []
        findings = []
        categories: dict[str, int] = {}
        projects = []
        for root in roots:
            if cancel.is_set():
                break
            emit({"type": "progress", "message": f"正在盘查 {root}"})
            result = scan_path(
                root,
                cancel=cancel,
                progress=lambda message, drive=root: emit({"type": "progress", "message": f"{drive} {message}"}),
                collect_duplicates=duplicates,
            )
            results.append(result)
            findings.extend(_finding_dict(item) for item in classify_scan(result))
            projects.extend(result.projects)
            for name, size in result.categories.items():
                categories[name] = categories.get(name, 0) + size
            if duplicates and result.dup_candidates and not cancel.is_set():
                emit({"type": "progress", "message": f"正在比对 {root} 的重复文件"})
                findings.extend(_finding_dict(item) for item in findings_from_duplicates(find_duplicates(result.dup_candidates)))
        if not cancel.is_set():
            emit({"type": "progress", "message": "正在读取项目的 git 信息"})
            projects = finalize_projects(projects)
        advice = [asdict(item) for item in build_drive_advice(get_disk_usage(), _finding_objects(findings), categories)]
        self._results = results
        self._findings = findings
        self._projects = projects
        self._advice = advice
        self._categories = categories
        if not cancel.is_set():
            save_audit_cache(
                {
                    "results": [result_to_dict(item) for item in results],
                    "findings": findings,
                    "projects": projects,
                    "advice": advice,
                    "categories": categories,
                }
            )
        return self._summary()

    def _apply_payload(self, payload: dict) -> None:
        self._results = [result_from_dict(item) for item in payload.get("results") or []]
        self._findings = list(payload.get("findings") or [])
        self._projects = list(payload.get("projects") or [])
        self._advice = list(payload.get("advice") or [])
        self._categories = dict(payload.get("categories") or {})

    def _summary(self) -> dict:
        return {
            "findings": len(self._findings),
            "projects": len(self._projects),
            "bytes": sum(int(item.get("size") or 0) for item in self._findings),
        }


def _snapshot(snap) -> dict:
    return {
        "cpu_percent": snap.cpu_percent,
        "memory_percent": snap.memory_percent,
        "memory_used_gb": snap.memory_used_gb,
        "memory_total_gb": snap.memory_total_gb,
        "processes": [
            {
                "pid": item.pid,
                "name": item.name,
                "cpu_percent": item.cpu_percent,
                "memory_mb": item.memory_mb,
                "status": item.status,
                "suspicious": item.suspicious,
                "can_kill": item.can_kill,
                "kill_block_reason": item.kill_block_reason,
            }
            for item in snap.processes
        ],
    }


def _finding_dict(item) -> dict:
    return asdict(item)


def _finding_objects(rows: list[dict]):
    from core.classifier import Finding

    objects = []
    for row in rows:
        data = {key: row[key] for key in Finding.__dataclass_fields__ if key in row}
        objects.append(Finding(**data))
    return objects


def _tree(node) -> dict:
    return {
        "name": node.name or node.path,
        "path": node.path,
        "size": node.size,
        "protected": node.protected,
        "children": [_tree(child) for child in node.children],
    }


def _icon_path() -> Path | None:
    from core.paths import get_bundle_dir

    candidate = get_bundle_dir() / "assets" / "icon.ico"
    return candidate if candidate.is_file() else None
