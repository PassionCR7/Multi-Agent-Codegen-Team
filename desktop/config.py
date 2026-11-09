"""桌面端配置与数据目录。

打包后安装目录通常只读，因此配置、日志、工作区默认放在用户数据目录：
    Windows: %APPDATA%\MultiAgentCodegenTeam\
    macOS:   ~/Library/Application Support/Multi-Agent-Codegen-Team/
    Linux:   ~/.local/share/multi-agent-codegen-team/
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_NAME = "MultiAgentCodegenTeam"
APP_TITLE = "Multi-Agent Codegen Team"


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / APP_NAME


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_project_dir() -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.exists() else Path.home()


@dataclass
class AppConfig:
    recent_projects: list[str] = field(default_factory=list)
    last_project: str = ""
    workspace_root: str = ""
    width: int = 1280
    height: int = 860
    x: int | None = None
    y: int | None = None
    debug: bool = False
    # LLM 设置（API Key 存在用户目录，注意本机安全）
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    mock: bool | None = None

    @classmethod
    def load(cls) -> "AppConfig":
        data_dir = app_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "config.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
        else:
            raw = {}

        cfg = cls(
            recent_projects=[str(x) for x in (raw.get("recent_projects") or []) if str(x).strip()],
            last_project=str(raw.get("last_project") or ""),
            workspace_root=str(raw.get("workspace_root") or ""),
            width=int(raw.get("width") or 1280),
            height=int(raw.get("height") or 860),
            x=raw.get("x") if isinstance(raw.get("x"), int) else None,
            y=raw.get("y") if isinstance(raw.get("y"), int) else None,
            debug=bool(raw.get("debug", False)),
            api_key=str(raw.get("api_key") or ""),
            base_url=str(raw.get("base_url") or ""),
            model=str(raw.get("model") or ""),
            mock=raw.get("mock") if isinstance(raw.get("mock"), bool) else None,
        )
        if not cfg.last_project or not Path(cfg.last_project).exists():
            cfg.last_project = str(default_project_dir())
        if not cfg.workspace_root:
            cfg.workspace_root = str(data_dir / "workspaces")
        Path(cfg.workspace_root).mkdir(parents=True, exist_ok=True)
        cfg.width = max(1024, cfg.width)
        cfg.height = max(700, cfg.height)
        return cfg

    def save(self) -> None:
        data_dir = app_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "config.json"
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # 某些 Windows 环境下 AppData 被重定向，跨卷 replace 会失败；
            # 直接写目标文件，保证配置仍能保存。
            path.write_text(payload, encoding="utf-8")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def remember_project(self, project: str) -> None:
        project = str(Path(project).expanduser().resolve())
        self.last_project = project
        self.recent_projects = [p for p in self.recent_projects if p != project]
        self.recent_projects.insert(0, project)
        self.recent_projects = self.recent_projects[:10]

    def remember_window(self, window: Any) -> None:
        try:
            self.width = max(1024, int(window.width))
            self.height = max(700, int(window.height))
            self.x = int(window.x)
            self.y = int(window.y)
        except (AttributeError, TypeError, ValueError):
            pass
