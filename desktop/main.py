"""桌面端主入口：单实例 → 本地后端 → pywebview 窗口。"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import traceback
from pathlib import Path

from desktop.backend import DesktopBackend
from desktop.config import APP_NAME, APP_TITLE, AppConfig, logs_dir
from desktop.native_api import NativeApi
from desktop.single_instance import SingleInstance


def _message(title: str, text: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"{title}: {text}", file=sys.stderr)


def _build_window_kwargs(config: AppConfig) -> dict:
    kwargs = {
        "width": config.width,
        "height": config.height,
        "min_size": (1024, 700),
        "text_select": True,
        "js_api": None,  # 由调用处覆盖
    }
    if config.x is not None and config.y is not None:
        kwargs["x"] = config.x
        kwargs["y"] = config.y
    return kwargs


def _setup_logging() -> None:
    logging.basicConfig(
        filename=str(logs_dir() / "desktop.log"),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    logging.info("desktop starting")
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--debug", action="store_true", help="打开 WebView 调试工具")
    args = parser.parse_args(argv)

    config = AppConfig.load()
    if args.debug:
        config.debug = True

    lock = SingleInstance(APP_NAME)
    if not lock.acquire():
        _message(APP_TITLE, "应用已经在运行，请不要重复打开。")
        return 1

    backend: DesktopBackend | None = None
    try:
        backend = DesktopBackend(config).start()
    except Exception as exc:  # noqa: BLE001
        logging.exception("backend start failed")
        lock.release()
        _message(APP_TITLE, f"本地后端启动失败：\n{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1

    try:
        import webview
    except ImportError:
        backend.shutdown()
        lock.release()
        _message(APP_TITLE, "缺少 pywebview。请先安装：\n\npip install -r requirements-desktop.txt")
        return 1

    holder: dict[str, object] = {}
    api = NativeApi(lambda: holder.get("window"))
    kwargs = _build_window_kwargs(config)
    kwargs["js_api"] = api
    logging.info("backend ready: %s", backend.page_url)
    window = webview.create_window(APP_TITLE, backend.page_url, **kwargs)
    holder["window"] = window
    logging.info("window created")

    def on_closing() -> bool:
        if backend and backend.has_running_tasks():
            try:
                confirmed = window.create_confirmation_dialog(
                    APP_TITLE,
                    "还有任务正在规划/执行。\n\n确定退出吗？退出后当前任务会中断。",
                )
            except Exception:  # noqa: BLE001
                confirmed = True
            if not confirmed:
                return False
        config.remember_window(window)
        config.save()
        if backend:
            backend.shutdown()
        return True

    def on_closed() -> None:
        try:
            config.remember_window(window)
            config.save()
        finally:
            if backend:
                backend.shutdown()
            lock.release()

    try:
        window.events.closing += on_closing
        window.events.closed += on_closed
        webview.start(debug=config.debug)
        logging.info("desktop closed")
        return 0
    finally:
        if backend:
            backend.shutdown()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
