"""One background job at a time, with progress events for the web view."""

from __future__ import annotations

import threading
from typing import Callable

Emit = Callable[[dict], None]
Worker = Callable[[threading.Event, Emit], dict]


class JobRunner:
    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, name: str, worker: Worker) -> dict:
        with self._lock:
            if self.busy():
                return {"ok": False, "error": "请等待当前任务完成"}
            self._cancel = threading.Event()
            cancel = self._cancel

            def run() -> None:
                try:
                    result = worker(cancel, self._emit)
                    self._emit({"type": "done", "job": name, "ok": True, "result": result})
                except Exception as exc:
                    self._emit({"type": "done", "job": name, "ok": False, "error": str(exc)})

            self._thread = threading.Thread(target=run, name=f"pcopt-{name}", daemon=True)
            self._thread.start()
            return {"ok": True}

    def cancel(self) -> dict:
        self._cancel.set()
        return {"ok": True}
