"""SystemGuard and project-protection tests."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from core.safety import (
    deletion_allowed,
    directory_is_project_root,
    is_drive_root,
    is_inside_project,
    is_path_protected,
    is_reparse_point,
    is_safe_delete_target,
    marker_kind,
    reset_safety_caches,
    system_block_reason,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    reset_safety_caches()
    yield
    reset_safety_caches()


def test_windows_and_program_files_are_blocked():
    for path in (
        r"C:\Windows\System32\kernel32.dll",
        r"c:\windows\system32\kernel32.dll",
        r"C:\Windows\System32\..\..\Windows\System32\kernel32.dll",
        r"C:\Windows\Temp\..\System32\foo.dll",
        r"\\?\C:\Windows\System32\kernel32.dll",
        r"C:\Program Files\App\app.exe",
        r"C:\Program Files (x86)\App\app.exe",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\desktop.ini",
    ):
        reason = system_block_reason(path)
        assert reason, path
        allowed, why = deletion_allowed(path)
        assert allowed is False
        assert why


def test_drive_roots_and_root_system_dirs_are_blocked():
    assert is_drive_root("C:\\")
    assert is_drive_root("D:/")
    assert system_block_reason("D:\\")
    assert system_block_reason(r"D:\System Volume Information\tracking.log")
    assert system_block_reason(r"E:\$Recycle.Bin\S-1-5\foo.txt")
    assert system_block_reason(r"C:\Recovery\WindowsRE\winre.wim")
    assert system_block_reason(r"C:\Boot\BCD")
    assert system_block_reason(r"C:\pagefile.sys")
    assert system_block_reason(r"C:\hiberfil.sys")
    assert system_block_reason(r"D:\swapfile.sys")


def test_data_drive_file_is_not_blanket_protected(tmp_path: Path):
    drive = tmp_path.drive or "D:"
    sample = f"{drive}\\not-a-project\\notes.txt"
    if os.path.normcase(sample).startswith(os.path.normcase(r"C:\Windows")):
        pytest.skip("temp path landed inside Windows")
    assert system_block_reason(sample) is None
    assert is_path_protected(sample) is False
    allowed, _ = deletion_allowed(sample)
    assert allowed is True


def test_user_temp_stays_cleanable_and_windows_temp_does_not():
    temp = os.environ.get("TEMP")
    assert temp
    user_file = os.path.join(temp, "pcoptimizer-safety-test.tmp")
    assert is_safe_delete_target(user_file) is True
    assert is_safe_delete_target(r"C:\Windows\Temp\foo.tmp") is False
    assert is_safe_delete_target(r"C:\Windows\SoftwareDistribution\Download\cab") is False
    allowed, _ = deletion_allowed(r"C:\Windows\Temp\foo.tmp")
    assert allowed is False


def test_project_tree_is_protected(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    nested = root / "src" / "main.py"
    nested.parent.mkdir()
    nested.write_text("print(1)", encoding="utf-8")
    assert directory_is_project_root(root) is True
    assert is_inside_project(nested) is True
    assert is_path_protected(nested) is True
    allowed, reason = deletion_allowed(nested)
    assert allowed is False
    assert "项目" in reason


def test_sln_marker_protects_directory(tmp_path: Path):
    root = tmp_path / "vs"
    root.mkdir()
    (root / "Demo.sln").write_text("", encoding="utf-8")
    target = root / "bin" / "out.dll"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    assert is_path_protected(target) is True


def test_lone_git_on_profile_or_drive_root_is_not_a_project():
    from core.paths import get_user_profile

    assert marker_kind(get_user_profile(), [".git", "Desktop"]) == "git"
    assert marker_kind(r"D:\loose-repo", [".git"]) == "git"
    assert marker_kind(r"D:\work\app", [".git", "src"]) == "project"
    assert marker_kind(r"D:\loose-repo", [".git", "package.json"]) == "project"


def test_git_dir_part_and_node_modules_stay_protected():
    assert is_path_protected(r"E:\work\repo\.git\objects\pack") is True
    assert is_path_protected(r"D:\code\app\node_modules\leftpad\index.js") is True


def test_system_attribute_and_reparse_point_block_deletion(tmp_path: Path):
    flagged = tmp_path / "system-flagged.txt"
    flagged.write_text("keep", encoding="utf-8")
    subprocess.run(["attrib", "+S", str(flagged)], check=True, capture_output=True)
    try:
        allowed, reason = deletion_allowed(flagged)
        assert allowed is False
        assert "系统属性" in reason
    finally:
        subprocess.run(["attrib", "-S", str(flagged)], check=False, capture_output=True)

    target = tmp_path / "target"
    target.mkdir()
    (target / "secret.txt").write_text("nope", encoding="utf-8")
    link = tmp_path / "link"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"mklink failed: {created.stderr}")
    assert is_reparse_point(link) is True
    allowed, reason = deletion_allowed(link)
    assert allowed is False
    assert "重解析" in reason
    assert stat.S_ISDIR(link.stat().st_mode)


def test_user_list_cannot_unprotect_windows(tmp_path: Path):
    allowed, _ = deletion_allowed(r"C:\Windows\System32\kernel32.dll", user_protected_paths=[])
    assert allowed is False
    user_file = tmp_path / "keep.txt"
    user_file.write_text("x", encoding="utf-8")
    assert is_path_protected(user_file, [str(tmp_path)]) is True
