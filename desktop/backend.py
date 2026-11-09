"""桌面端后端生命周期：动态端口 + 健康检查 + 优雅关闭。"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from .config import AppConfig


class DesktopBackend:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.handle: Any = None

    def _apply_llm_settings(self) -> None:
        """把桌面配置写进运行期 SETTINGS（覆盖 .env，但空值不覆盖）。"""
        from agents.config import SETTINGS

        if self.config.api_key:
            SETTINGS.api_key = self.config.api_key
        if self.config.base_url:
            SETTINGS.base_url = self.config.base_url
        if self.config.model:
            SETTINGS.model = self.config.model
        if self.config.mock is not None:
            SETTINGS.force_mock = bool(self.config.mock)

    def _save_llm_settings(self, data: dict) -> None:
        if "api_key" in data:
            self.config.api_key = str(data.get("api_key") or "")
        if "base_url" in data:
            self.config.base_url = str(data.get("base_url") or "")
        if "model" in data:
            self.config.model = str(data.get("model") or "")
        if "mock" in data:
            self.config.mock = bool(data.get("mock"))
        self.config.save()

    def start(self) -> "DesktopBackend":
        from web.server import create_server

        self._apply_llm_settings()
        workspace_root = Path(self.config.workspace_root).expanduser().resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        default_project = Path(self.config.last_project).expanduser().resolve()

        self.handle = create_server(
            host="127.0.0.1",
            port=0,
            token=secrets.token_urlsafe(32),
            workspace_root=workspace_root,
            default_project_root=default_project,
            desktop=True,
            settings_saver=self._save_llm_settings,
        )
        self.handle.start()
        if not self.handle.wait_ready(timeout=10.0):
            self.shutdown()
            raise RuntimeError("本地后端启动超时：/api/health 未就绪")
        return self

    @property
    def base_url(self) -> str:
        if self.handle is None:
            raise RuntimeError("后端尚未启动")
        return self.handle.base_url

    @property
    def page_url(self) -> str:
        return self.base_url + "/?desktop=1"

    def has_running_tasks(self) -> bool:
        return bool(self.handle and self.handle.has_running_tasks())

    def shutdown(self) -> None:
        if self.handle is not None:
            self.handle.shutdown()
            self.handle = None
