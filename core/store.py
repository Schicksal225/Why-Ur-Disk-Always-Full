"""Persistent configuration and history storage."""

from __future__ import annotations

import json
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
    "preferences": {
        "temp_min_age_days": 1,
    },
}


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
    path.parent.mkdir(parents=True, exist_ok=True)
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

    def get_preference(self, key: str, default: Any = None) -> Any:
        return self.config.get("preferences", {}).get(key, default)

    def _add_run(self, key: str, result: dict[str, Any]) -> None:
        runs = self.history.setdefault(key, [])
        runs.insert(0, {"time": datetime.now().isoformat(timespec="seconds"), **result})
        self.history[key] = runs[:50]
        self.save_history()

    def add_clean_run(self, result: dict[str, Any]) -> None:
        self._add_run("clean_runs", result)

    def add_kill_run(self, result: dict[str, Any]) -> None:
        self._add_run("kill_runs", result)

    def add_storage_run(self, result: dict[str, Any]) -> None:
        self._add_run("storage_runs", result)

    def get_clean_history(self) -> list[dict[str, Any]]:
        return list(self.history.get("clean_runs", []))

    def get_storage_history(self) -> list[dict[str, Any]]:
        return list(self.history.get("storage_runs", []))
