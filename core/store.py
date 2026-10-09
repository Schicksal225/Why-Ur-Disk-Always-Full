"""Persistent configuration and history storage."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from core.paths import get_data_dir

APP_DIR = get_data_dir()
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "protected_paths": [],
    "process_whitelist": [],
    "baseline": None,
    "preferences": {
        "temp_min_age_days": 1,
        "auto_safe_clean": True,
        "last_clean_targets": [],
    },
}


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(default)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return deepcopy(default)
        merged = deepcopy(default)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return deepcopy(default)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Store:
    def __init__(self) -> None:
        self.config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
        self.history: dict[str, Any] = _load_json(
            HISTORY_PATH, {"clean_runs": [], "kill_runs": [], "storage_runs": []}
        )

    def save_config(self) -> None:
        _save_json(CONFIG_PATH, self.config)

    def save_history(self) -> None:
        _save_json(HISTORY_PATH, self.history)

    @property
    def protected_paths(self) -> list[str]:
        return list(self.config.get("protected_paths", []))

    @property
    def process_whitelist(self) -> list[str]:
        return list(self.config.get("process_whitelist", []))

    def add_protected_path(self, path: str) -> None:
        norm = os.path.normpath(path)
        paths = self.config.setdefault("protected_paths", [])
        if norm not in paths:
            paths.append(norm)
            self.save_config()

    def remove_protected_path(self, path: str) -> None:
        paths = self.config.setdefault("protected_paths", [])
        norm = os.path.normpath(path)
        self.config["protected_paths"] = [p for p in paths if p != norm]
        self.save_config()

    def add_process_whitelist(self, name: str) -> None:
        names = self.config.setdefault("process_whitelist", [])
        lower = name.lower()
        if lower not in [n.lower() for n in names]:
            names.append(lower)
            self.save_config()

    def remove_process_whitelist(self, name: str) -> None:
        names = self.config.setdefault("process_whitelist", [])
        lower = name.lower()
        self.config["process_whitelist"] = [n for n in names if n.lower() != lower]
        self.save_config()

    def set_baseline(self, snapshot: dict[str, Any]) -> None:
        self.config["baseline"] = {
            **snapshot,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save_config()

    def get_baseline(self) -> dict[str, Any] | None:
        baseline = self.config.get("baseline")
        return baseline if isinstance(baseline, dict) else None

    def get_preference(self, key: str, default: Any = None) -> Any:
        return self.config.get("preferences", {}).get(key, default)

    def set_preference(self, key: str, value: Any) -> None:
        prefs = self.config.setdefault("preferences", {})
        prefs[key] = value
        self.save_config()

    def add_clean_run(self, result: dict[str, Any]) -> None:
        runs = self.history.setdefault("clean_runs", [])
        runs.insert(
            0,
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                **result,
            },
        )
        self.history["clean_runs"] = runs[:50]
        self.save_history()

    def add_kill_run(self, result: dict[str, Any]) -> None:
        runs = self.history.setdefault("kill_runs", [])
        runs.insert(
            0,
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                **result,
            },
        )
        self.history["kill_runs"] = runs[:50]
        self.save_history()

    def add_storage_run(self, result: dict[str, Any]) -> None:
        runs = self.history.setdefault("storage_runs", [])
        runs.insert(
            0,
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                **result,
            },
        )
        self.history["storage_runs"] = runs[:50]
        self.save_history()

    def get_storage_history(self) -> list[dict[str, Any]]:
        return list(self.history.get("storage_runs", []))

    def get_clean_history(self) -> list[dict[str, Any]]:
        return list(self.history.get("clean_runs", []))

    def get_kill_history(self) -> list[dict[str, Any]]:
        return list(self.history.get("kill_runs", []))
