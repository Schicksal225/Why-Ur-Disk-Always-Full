"""Desktop and Start Menu shortcuts. Created only when the user asks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def shortcut_targets() -> tuple[str, str, str]:
    """Return (executable, arguments, working directory)."""
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return exe, "", str(Path(exe).parent)
    python = str(Path(sys.executable).resolve())
    script = str(Path(__file__).resolve().parent.parent / "main.py")
    return python, script, str(Path(script).parent)


def shortcut_destinations() -> list[Path]:
    desktop = Path.home() / "Desktop"
    start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return [desktop / "PC Optimizer.lnk", start / "PC Optimizer.lnk"]


def create_shortcuts(icon: str | None = None, runner=None) -> list[str]:
    target, arguments, workdir = shortcut_targets()
    created: list[str] = []
    for dest in shortcut_destinations():
        dest.parent.mkdir(parents=True, exist_ok=True)
        script = _powershell(str(dest), target, arguments, workdir, icon or "")
        if runner is None:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        else:
            runner(script)
        created.append(str(dest))
    return created


def _powershell(dest: str, target: str, arguments: str, workdir: str, icon: str) -> str:
    def ps(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    icon_line = f"$s.IconLocation = {ps(icon)}" if icon else ""
    return (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut({ps(dest)}); "
        f"$s.TargetPath = {ps(target)}; "
        f"$s.Arguments = {ps(arguments)}; "
        f"$s.WorkingDirectory = {ps(workdir)}; "
        f"{icon_line}; "
        "$s.Save()"
    )
