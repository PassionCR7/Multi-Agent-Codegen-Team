"""JS ↔ Python 原生能力桥。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .config import APP_TITLE


class NativeApi:
    """pywebview 会把这个对象暴露为 window.pywebview.api。"""

    def __init__(self, window_getter: Callable[[], Any], version: str = "1.1.0") -> None:
        self._window_getter = window_getter
        self._version = version
        self._last_dir = str(Path.home())

    def choose_folder(self) -> str:
        window = self._window_getter()
        if window is None:
            return ""
        try:
            import webview

            result = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=self._last_dir,
            )
            if not result:
                return ""
            path = result[0] if isinstance(result, (list, tuple)) else result
            if path:
                self._last_dir = str(Path(path))
                return str(path)
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def open_path(self, path: str) -> bool:
        target = Path(str(path or "")).expanduser()
        if not target.exists():
            return False
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return True
        except Exception:  # noqa: BLE001
            return False

    def notify(self, title: str, message: str) -> bool:
        """Windows 用 winotify 发系统通知；未安装时静默降级。"""
        try:
            if os.name == "nt":
                from winotify import Notification

                toast = Notification(app_id=APP_TITLE, title=str(title), msg=str(message))
                toast.show()
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def get_app_info(self) -> dict[str, Any]:
        return {
            "name": APP_TITLE,
            "version": self._version,
            "platform": sys.platform,
            "frozen": bool(getattr(sys, "frozen", False)),
        }
