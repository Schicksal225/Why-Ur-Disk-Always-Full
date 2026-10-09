"""Application path helpers for dev and PyInstaller builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_bundle_dir() -> Path:
    """Directory containing packaged code (source root or PyInstaller _MEIPASS)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def get_data_dir() -> Path:
    """Writable directory for config/history (beside exe when frozen)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_user_profile() -> str:
    return os.environ.get("USERPROFILE", os.path.expanduser("~"))


def get_user_local_appdata() -> str:
    return os.environ.get("LOCALAPPDATA", os.path.join(get_user_profile(), "AppData", "Local"))


def get_user_roaming_appdata() -> str:
    return os.environ.get("APPDATA", os.path.join(get_user_profile(), "AppData", "Roaming"))
