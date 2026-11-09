# 桌面端打包

## Windows 构建

```powershell
.\packaging\build_windows.ps1
```

输出：

```
dist\MultiAgentCodegenTeam\MultiAgentCodegenTeam.exe
```

## 依赖

- Python 3.10+
- pywebview
- PyInstaller
- Windows 10/11 自带的 Edge WebView2 Runtime

如果目标机器没有 WebView2 Runtime，需要安装：

https://developer.microsoft.com/microsoft-edge/webview2/

## 安装包（可选）

安装 Inno Setup 后，编译：

```
packaging\installer\windows.iss
```

输出在：

```
dist\installer\MultiAgentCodegenTeam-Setup.exe
```

## 开发模式直接运行

```powershell
.\.venv\Scripts\python.exe -m desktop.main --debug
```

浏览器调试模式仍然保留：

```powershell
.\.venv\Scripts\python.exe web/server.py
```
