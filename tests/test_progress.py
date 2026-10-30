"""Progress events are throttled so the page is not redrawn on every file batch."""

from __future__ import annotations

from app.progress import throttle


def test_throttle_keeps_done_and_drops_burst_progress():
    sent = []
    clock = {"t": 0.0}
    wrapped = throttle(sent.append, interval=0.5, now=lambda: clock["t"])
    wrapped({"type": "progress", "message": "a"})
    wrapped({"type": "progress", "message": "b"})
    wrapped({"type": "done", "ok": True})
    clock["t"] = 0.5
    wrapped({"type": "progress", "message": "c"})
    assert [item["message"] if item["type"] == "progress" else "done" for item in sent] == ["a", "done", "c"]


def test_throttle_passes_other_event_types_untouched():
    sent = []
    wrapped = throttle(sent.append, interval=10.0, now=lambda: 0.0)
    wrapped({"type": "progress", "message": "a"})
    wrapped({"type": "caches", "targets": []})
    wrapped({"type": "caches", "targets": [1]})
    assert [item["type"] for item in sent] == ["progress", "caches", "caches"]
