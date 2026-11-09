# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：Windows 优先的桌面打包配置。

构建：
    pyinstaller --noconfirm --clean packaging/agent-team.spec
输出：
    dist/MultiAgentCodegenTeam/MultiAgentCodegenTeam.exe
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

project = Path(SPEC).resolve().parent.parent  # noqa: F821

datas = [
    (str(project / "web" / "static"), "web/static"),
]

hiddenimports = []
for package in (
    "langgraph",
    "langchain_core",
    "langchain_openai",
    "pydantic",
    "webview",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

# pywebview 在 Windows 上使用 EdgeChromium / WinForms；显式加入，避免被裁剪。
hiddenimports += [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "winotify",
]

a = Analysis(  # noqa: F821
    [str(project / "desktop" / "main.py")],
    pathex=[str(project)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PySide6", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MultiAgentCodegenTeam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project / "desktop" / "resources" / "icon.ico"),
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MultiAgentCodegenTeam",
)
