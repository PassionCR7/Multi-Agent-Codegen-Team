"""桌面后端自检：动态端口、健康检查、token、Cookie、关闭。

运行::

    ./.venv/Scripts/python.exe -m tests.test_desktop_backend
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.backend import DesktopBackend
from desktop.config import AppConfig
from desktop.native_api import NativeApi
from desktop.single_instance import SingleInstance
from web.server import APP_VERSION, create_server

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


def _get(url: str, opener=None, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    if opener is not None:
        return opener.open(req, timeout=3)
    return urllib.request.urlopen(req, timeout=3)


def test_server_handle() -> None:
    print("\n[1] ServerHandle：健康检查 / token / Cookie / 关闭")
    with tempfile.TemporaryDirectory() as t:
        handle = create_server(
            port=0,
            token="test-token",
            workspace_root=Path(t) / "ws",
            default_project_root=Path(t),
            desktop=True,
        )
        handle.start()
        check(handle.wait_ready(5), "服务在 5 秒内就绪")
        check(handle.base_url.startswith("http://127.0.0.1:"), "绑定 127.0.0.1 随机端口")
        check(not handle.has_running_tasks(), "初始没有运行中任务")

        with _get(handle.base_url + "/api/health") as r:
            health = json.loads(r.read())
        check(health.get("ok") is True, "/api/health 返回 ok")
        check(health.get("desktop") is True, "/api/health 标记 desktop")
        check(health.get("version") == APP_VERSION, "健康检查带版本号")

        try:
            _get(handle.base_url + "/api/config")
        except urllib.error.HTTPError as exc:
            check(exc.code == 401, "无 token 访问 /api/config 返回 401")
        else:
            check(False, "无 token 访问 /api/config 返回 401")

        # 先访问首页拿 Cookie，再访问 API
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(handle.base_url + "/", timeout=3) as r:
            check(r.status == 200, "首页可访问")
        cookies = {c.name: c.value for c in jar}
        check(cookies.get("agent_token") == "test-token", "首页下发 agent_token Cookie")
        with opener.open(handle.base_url + "/api/config", timeout=3) as r:
            cfg = json.loads(r.read())
        check(cfg.get("desktop") is True, "带 Cookie 可访问 /api/config")
        check(Path(cfg.get("workspace_root", "")) == Path(t) / "ws", "配置返回工作区路径")

        handle.shutdown()
        check(True, "关闭不抛异常")


def test_settings_endpoint() -> None:
    print("\n[2] 模型设置：GET/POST + 持久化回调")
    with tempfile.TemporaryDirectory() as t:
        saved: dict = {}
        handle = create_server(
            port=0,
            token="settings-token",
            workspace_root=Path(t) / "ws",
            default_project_root=Path(t),
            desktop=True,
            settings_saver=lambda data: saved.update(data),
        )
        handle.start()
        handle.wait_ready(5)
        headers = {"X-Desktop-Token": "settings-token"}

        with _get(handle.base_url + "/api/settings", headers=headers) as r:
            before = json.loads(r.read())
        check(before.get("has_api_key") is False, "初始无 API Key")

        req = urllib.request.Request(
            handle.base_url + "/api/settings",
            data=json.dumps({
                "api_key": "sk-test",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "mock": False,
            }).encode("utf-8"),
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            posted = json.loads(r.read())
        check(posted.get("model") == "deepseek-chat", "POST 设置更新模型")
        check(posted.get("has_api_key") is True, "POST 设置后 has_api_key=True")
        check(saved.get("api_key") == "sk-test", "设置通过回调持久化")

        with _get(handle.base_url + "/api/settings", headers=headers) as r:
            after = json.loads(r.read())
        check(after.get("model") == "deepseek-chat", "GET 设置返回新模型")
        handle.shutdown()


def test_desktop_backend() -> None:
    print("\n[3] DesktopBackend：启动 / 页面 URL / 关闭")
    with tempfile.TemporaryDirectory() as t:
        cfg = AppConfig(
            last_project=t,
            workspace_root=str(Path(t) / "workspaces"),
        )
        backend = DesktopBackend(cfg).start()
        check(backend.base_url.startswith("http://127.0.0.1:"), "DesktopBackend 动态端口")
        check("?desktop=1" in backend.page_url, "页面 URL 带 desktop=1")
        check(not backend.has_running_tasks(), "初始无运行中任务")
        backend.shutdown()
        check(True, "DesktopBackend 可关闭")


def test_single_instance() -> None:
    print("\n[4] SingleInstance：同一名称只允许一个实例")
    name = "MultiAgentCodegenTeamSelfTest"
    a = SingleInstance(name)
    b = SingleInstance(name)
    try:
        check(a.acquire() is True, "第一个实例获取锁")
        check(b.acquire() is False, "第二个实例被拒绝")
        a.release()
        check(b.acquire() is True, "释放后第二个实例可获取")
    finally:
        b.release()
        a.release()


def test_native_api_info() -> None:
    print("\n[5] NativeApi：基础信息")
    api = NativeApi(lambda: None)
    info = api.get_app_info()
    check(info.get("name") == "Multi-Agent Codegen Team", "应用名称正确")
    check(info.get("version") == "1.1.0", "应用版本正确")
    check(isinstance(info.get("platform"), str), "平台字段存在")


def main() -> int:
    print("=" * 62)
    print("  桌面后端自检")
    print("=" * 62)
    test_server_handle()
    test_settings_endpoint()
    test_desktop_backend()
    test_single_instance()
    test_native_api_info()
    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"  结果: {passed}/{total} 通过")
    if passed != total:
        for ok, label in _results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
