"""Windows locations under SystemRoot are advice-only."""

from __future__ import annotations

from core.cleaner import build_targets, execute_target


def test_windows_locations_are_not_deleted():
    targets = build_targets([])
    by_id = {item.id: item for item in targets}
    assert by_id["windows_temp"].level == "advice"
    assert by_id["windows_update_cache"].level == "advice"
    for key in ("windows_temp", "windows_update_cache"):
        result = execute_target(by_id[key], [])
        assert result.files_removed == 0
        assert result.freed_bytes == 0
        assert result.errors