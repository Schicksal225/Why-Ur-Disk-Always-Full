"""PC Optimizer desktop window."""

from __future__ import annotations

import ctypes
import os
import sys

from core.paths import get_bundle_dir

APP_DIR = str(get_bundle_dir())
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from app.api import Api  # noqa: E402
from app.runtime import WEBVIEW2_URL, webview2_available  # noqa: E402


def main() -> None:
    if os.name == "nt" and not webview2_available():
        ctypes.windll.user32.MessageBoxW(
            None,
            "需要 Microsoft Edge WebView2 运行时才能打开界面。\n" + WEBVIEW2_URL,
            "PC Optimizer",
            0x00000010,
        )
        return
    import webview

    api = Api()
    index = get_bundle_dir() / "web" / "index.html"
    window = webview.create_window(
        "PC Optimizer",
        index.as_uri(),
        js_api=api,
        width=1180,
        height=820,
        min_size=(960, 680),
    )
    api._bind(window)
    webview.start()


if __name__ == "__main__":
    main()
