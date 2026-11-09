"""极简 Web 前端后端 —— 仅用 Python 标准库，零新增依赖。

为什么用 stdlib 而不是 FastAPI/Flask？
"简单"在这里是硬约束：前端是为了让计划审核更顺手，不该反过来引入一堆依赖
和部署复杂度。http.server + ThreadingHTTPServer + json 已经够用。

核心机制 —— 人类决策如何嵌入自动流程：
    计划审核本质是"流程等人"。后端用 threading.Event 让规划线程**阻塞挂起**，
    等前端 POST 来审核结果后再唤醒。这样既不用轮询，也不用把流程拆成状态机，
    人类决策就自然嵌进了 agent 流水线。

接口：
    POST /api/tasks                  创建任务（后台开始规划）
    GET  /api/tasks/<id>             查询任务快照（前端轮询这个）
    POST /api/tasks/<id>/review      提交审核决策（批准/打回/自拟）
    GET  /api/tasks/<id>/messages    读取黑板消息流
    POST /api/tasks/<id>/attention   回答“待你决定”的一个具体问题
    GET  /                           前端页面
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# 允许 `python web/server.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.config import SETTINGS  # noqa: E402
from agents.lifecycle import make_attention  # noqa: E402
from agents.mandate import build_mandate  # noqa: E402
from agents.review import ReviewDecision, run_planning_phase  # noqa: E402
from agents.runner import run_team  # noqa: E402

APP_VERSION = "1.1.0"

# PyInstaller one-dir/one-file 解压后，静态资源在 sys._MEIPASS 下。
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
STATIC = BUNDLE_ROOT / "web" / "static"
ROOT = BUNDLE_ROOT
DEFAULT_PROJECT_ROOT = Path.cwd()

TERMINAL_STATES = {"done", "error", "aborted", "plan_aborted"}


@dataclass
class ServerContext:
    """一次服务实例的运行上下文（桌面/浏览器共用）。"""
    token: str = ""
    workspace_root: str = ""
    default_project_root: str = ""
    desktop: bool = False
    version: str = APP_VERSION
    settings_saver: Callable[[dict], None] | None = None


SERVER_CONTEXT = ServerContext()


# --------------------------------------------------------------------------- #
# 会话（一次任务 = 一个会话）
# --------------------------------------------------------------------------- #
class Session:
    """一个任务的完整运行态。所有跨线程访问都过 lock。"""

    def __init__(
        self,
        task_id: str,
        requirement: str,
        project_root: str,
        task_name: str | None,
        workspace_root: str | None = None,
    ):
        self.id = task_id
        self.requirement = requirement
        self.project_root = project_root
        self.task_name = task_name or f"web-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.workspace_root = str(
            workspace_root or (Path(project_root) / SETTINGS.workspace_dirname)
        )

        self.status = "planning"          # planning|awaiting_review|executing|done|error|aborted|plan_aborted
        self.plan: dict[str, Any] = {}
        self.review_round = 0
        self.workspace = ""
        self.snapshot_totals: dict[str, Any] = {}
        self.artifacts: list[dict] = []
        self.alignment: dict = {}
        self.security: dict = {}
        self.bugfix: dict = {}
        self.final_summary = ""
        self.success = False
        self.abort_reason = ""
        self.error = ""
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # P1：面向人的生命周期字段
        self.ticket_stage = "plan_review"
        self.report: dict[str, Any] = {}
        self.attention: dict[str, Any] = {}
        self.mandate: dict[str, Any] = {}
        self.mandate_id = ""

        self.timeline: list[dict] = []    # 执行阶段的事件轨迹（供 UI 画时间线）
        self.log: list[str] = []          # 人类可读日志

        self._lock = threading.RLock()
        self._review_event = threading.Event()
        self._decision: ReviewDecision | None = None

    # ---- 线程安全访问 ----
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def add_log(self, text: str) -> None:
        with self._lock:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
            self.log = self.log[-200:]

    # ---- 审核握手 ----
    def wait_for_review(self, plan: dict, round_index: int) -> ReviewDecision:
        """规划线程在此挂起，直到前端提交审核结果。

        关键：**绝不能持有锁进入 wait()**。否则 HTTP 线程调用 submit_review()
        时拿不到锁，永远无法唤醒这里，双方互锁 —— 这正是开发中真实踩到的坑。
        正确做法：先在锁内更新状态，释放锁后再阻塞等待。
        """
        with self._lock:
            self.plan = plan
            self.review_round = round_index
            self.status = "awaiting_review"
            self.ticket_stage = "plan_review"
            self.report = {"state": "waiting", "summary": "等待你审核计划"}
            self._decision = None
            self._review_event.clear()
            self.add_log(f"第 {round_index} 版计划已生成，等待审核")
        # 锁已释放，此时阻塞是安全的
        self._review_event.wait()
        with self._lock:
            return self._decision or ReviewDecision(ReviewDecision.REVISE, note="未收到审核结果")

    def submit_review(self, decision: ReviewDecision) -> None:
        with self._lock:
            self._decision = decision
            self.status = "planning"              # 用户已决策，回到规划线程继续
            self.ticket_stage = "queued"
            self.report = {"state": "waiting", "summary": "已收到审核结果，等待继续"}
            self.add_log(f"收到审核结果: {decision.action}")
        self._review_event.set()                  # 在锁外唤醒，避免锁竞争

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "requirement": self.requirement,
                "project_root": self.project_root,
                "task_name": self.task_name,
                "status": self.status,
                "stage": self.ticket_stage or self.status,
                "ticket_stage": self.ticket_stage,
                "report": self.report,
                "attention": self.attention,
                "mandate_id": self.mandate_id,
                "workspace": self.workspace,
                "workspace_root": self.workspace_root,
                "snapshot_totals": self.snapshot_totals,
                "plan": self.plan,
                "review_round": self.review_round,
                "artifacts": self.artifacts,
                "alignment": self.alignment,
                "security": self.security,
                "bugfix": self.bugfix,
                "final_summary": self.final_summary,
                "success": self.success,
                "abort_reason": self.abort_reason,
                "error": self.error,
                "created_at": self.created_at,
                "timeline": self.timeline[-40:],
                "log": self.log[-60:],
                "terminal": self.status in TERMINAL_STATES,
            }


class WebReviewer:
    """把 Session 适配成 review.py 期望的 reviewer 协议。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def review(self, plan: dict, round_index: int) -> ReviewDecision:
        return self.session.wait_for_review(plan, round_index)


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
def _run_task(session: Session) -> None:
    """后台线程：先规划（等人审核），批准后再执行。"""
    try:
        project_root = Path(session.project_root).resolve()
        if not project_root.exists():
            raise FileNotFoundError(f"项目目录不存在: {project_root}")

        def on_event(ev: dict) -> None:
            t = ev.get("type")
            if t == "workspace":
                with session.lock:
                    session.workspace = ev.get("path", "")
                    session.snapshot_totals = ev.get("snapshot", {}) or {}
                session.add_log(
                    f"已扫描真实项目：{session.snapshot_totals.get('files_scanned', 0)} 个文件"
                )
            elif t == "plan_draft":
                session.add_log(f"计划草稿 v{ev.get('round')} 就绪")
            elif t == "plan_approved":
                session.add_log("计划已批准，进入执行阶段")

        ws, plan, state = run_planning_phase(
            requirement=session.requirement,
            project_root=project_root,
            workspace_root=session.workspace_root,
            task_name=session.task_name,
            reviewer=WebReviewer(session),
            on_event=on_event,
        )
        with session.lock:
            session.workspace = str(ws.dir)
            session.plan = plan
            session.mandate = (state or {}).get("mandate") or {}
            session.mandate_id = (session.mandate or {}).get("id", "")

        if not plan:
            with session.lock:
                session.status = "plan_aborted"
                session.ticket_stage = "failed"
                session.report = {
                    "state": "failed",
                    "summary": state.get("abort_reason", "计划未获批准"),
                }
                session.abort_reason = state.get("abort_reason", "计划未获批准")
            session.add_log("计划未获批准，流程结束")
            return

        _execute_plan(session, project_root, ws, plan, session.mandate)

    except Exception as exc:  # noqa: BLE001
        with session.lock:
            session.status = "error"
            session.ticket_stage = "awaiting_answer"
            session.error = f"{type(exc).__name__}: {exc}"
            session.report = {"state": "failed", "summary": session.error}
            session.attention = make_attention(
                f"运行出错：{session.error}。是否重试或终止？",
                options=[{"id": "retry", "label": "重试"}, {"id": "abort", "label": "终止任务"}],
                source="web_error",
                detail={"error": str(exc)},
            )
        session.add_log(f"运行出错: {type(exc).__name__}: {exc}")
        traceback.print_exc()


def _execute_plan(session: Session, project_root: Path, ws, plan: dict, mandate: dict) -> None:
    """执行已批准计划，并把生命周期字段同步给 Web Session。"""
    with session.lock:
        session.status = "executing"
        session.ticket_stage = "working"
        session.report = {"state": "progress", "summary": "开始执行"}
        session.attention = {}
    session.add_log("开始执行：编码 → 对齐 → 安全 → 修复")

    def on_exec_event(ev: dict) -> None:
        if ev.get("type") == "node":
            st = ev.get("state", {})
            node = ev.get("node", "")
            with session.lock:
                session.timeline.append({
                    "node": node,
                    "label": ev.get("label", node),
                    "iteration": st.get("iteration", 0),
                    "artifacts": st.get("artifacts", 0),
                    "aligned": st.get("aligned", False),
                    "safe": st.get("safe"),
                    "bugs": st.get("bugs", 0),
                    "stage": st.get("ticket_stage", ""),
                    "at": datetime.now().strftime("%H:%M:%S"),
                })
                if st.get("ticket_stage"):
                    session.ticket_stage = st["ticket_stage"]
                if st.get("report"):
                    session.report = st["report"]
                if st.get("attention"):
                    session.attention = st["attention"]
        elif ev.get("type") == "finish":
            pass

    result = run_team(
        requirement=session.requirement,
        project_root=project_root,
        task_name=session.task_name,
        plan=plan,
        mandate=mandate or None,
        workspace_dir=str(ws.dir),
        on_event=on_exec_event,
    )

    # 汇总产物
    from agents.state import latest_artifacts

    arts = [
        {"path": f["path"], "language": f.get("language", ""), "revision": f.get("revision", 1),
         "content": f.get("content", ""), "description": f.get("description", "")}
        for f in latest_artifacts(result)
    ]
    with session.lock:
        session.artifacts = arts
        session.alignment = result.get("alignment", {}) or {}
        session.security = result.get("security", {}) or {}
        session.bugfix = result.get("bugfix", {}) or {}
        session.final_summary = result.get("final_summary", "")
        session.success = bool(result.get("success"))
        session.abort_reason = result.get("abort_reason", "")
        session.attention = result.get("attention") or {}
        session.report = result.get("report") or {}
        session.ticket_stage = result.get("ticket_stage") or ("awaiting_review" if session.success else "failed")
        session.mandate = result.get("mandate") or session.mandate
        session.mandate_id = (session.mandate or {}).get("id", "")
        session.status = "done" if session.success else "aborted"
    session.add_log(f"执行结束：{'成功' if session.success else '未完全达成'}")


def _retry_worker(session: Session, plan: dict, mandate: dict) -> None:
    """后台线程：按用户对 attention 的回答重新执行。"""
    try:
        from agents.workspace import Workspace

        ws = Workspace.from_dir(session.workspace)
        _execute_plan(session, Path(session.project_root).resolve(), ws, plan, mandate)
    except Exception as exc:  # noqa: BLE001
        with session.lock:
            session.status = "error"
            session.ticket_stage = "awaiting_answer"
            session.report = {"state": "failed", "summary": f"{type(exc).__name__}: {exc}"}
            session.attention = make_attention(
                f"重试失败：{type(exc).__name__}: {exc}。是否再次重试或终止？",
                options=[{"id": "retry", "label": "重试"}, {"id": "abort", "label": "终止任务"}],
                source="retry_error",
                detail={"error": str(exc)},
            )
        session.add_log(f"重试失败: {type(exc).__name__}: {exc}")


def _handle_attention(session: Session, option: str, note: str, scope_files: list[str]) -> None:
    """处理 Web 端“待你决定”的回答。"""
    with session.lock:
        attention = dict(session.attention or {})
        if not attention:
            raise ValueError("当前没有待处理事项")
        if attention.get("answered_at"):
            raise ValueError("该待办已经回答过了")
        allowed = {str(o.get("id")) for o in (attention.get("options") or [])}
        if option not in {"abort", "retry", "expand_scope"}:
            raise ValueError(f"不支持的选项: {option}")
        if allowed and option not in allowed:
            raise ValueError(f"该待办没有这个选项: {option}")

    if option == "abort":
        with session.lock:
            attention["answered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            attention["answer"] = {"option": option, "note": note}
            session.attention = attention
            session.status = "aborted"
            session.ticket_stage = "aborted"
            session.report = {"state": "dropped", "summary": "用户终止任务"}
            session.add_log(f"收到待办回答: {option}" + (f"（{note}）" if note else ""))
        return

    if not session.workspace:
        raise ValueError("工作区尚未准备好，无法重试")

    plan = dict(session.plan or {})
    mandate = dict(session.mandate or {})

    if option == "expand_scope":
        files = [str(f).strip() for f in (scope_files or []) if str(f).strip()]
        if not files:
            files = [str(f).strip() for f in ((attention.get("detail") or {}).get("files") or []) if str(f).strip()]
        if not files:
            raise ValueError("请提供要纳入范围的文件")
        scope = dict(plan.get("scope") or {})
        scope["files"] = sorted(set([str(f) for f in (scope.get("files") or [])] + files))
        plan["scope"] = scope
        plan["revision"] = int(plan.get("revision") or 1) + 1
        # 先构建 Mandate；路径非法时在这里报错，不把待办标记成已回答
        mandate = build_mandate(plan, approved_by="user_attention").to_dict()
        from agents.workspace import Workspace

        ws = Workspace.from_dir(session.workspace)
        ws.write_report("plan_scope_expanded", plan)
        ws.write_report("mandate_approved", mandate)

    with session.lock:
        attention["answered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        attention["answer"] = {"option": option, "note": note}
        session.attention = attention
        session.plan = plan
        session.mandate = mandate
        session.mandate_id = (mandate or {}).get("id", "")
        session.status = "executing"
        session.ticket_stage = "working"
        session.report = {"state": "progress", "summary": "按你的决定重新执行"}
        session.add_log(f"收到待办回答: {option}" + (f"（{note}）" if note else ""))

    threading.Thread(target=_retry_worker, args=(session, plan, mandate), daemon=True).start()


# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "AgentTeamWeb/1.0"

    # ---- 工具 ----
    def _send_json(self, data: Any, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == "agent_token":
                return value
        return ""

    def _authorized(self) -> bool:
        """桌面模式校验本地 token；浏览器模式（token 为空）不校验。"""
        if not SERVER_CONTEXT.token:
            return True
        path = self.path.split("?")[0]
        if path == "/api/health" or not path.startswith("/api/"):
            return True
        provided = self.headers.get("X-Desktop-Token") or self._cookie_token()
        return bool(provided) and secrets.compare_digest(provided, SERVER_CONTEXT.token)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix in (".html", ".css", ".js"):
            ctype += "; charset=utf-8"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if SERVER_CONTEXT.token and path.name == "index.html":
            # 同源 Cookie：页面里的 fetch 会自动带上，不需要改前端请求代码。
            self.send_header(
                "Set-Cookie",
                f"agent_token={SERVER_CONTEXT.token}; Path=/; HttpOnly; SameSite=Strict",
            )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/api/health":
            return self._send_json({
                "ok": True,
                "version": SERVER_CONTEXT.version,
                "desktop": SERVER_CONTEXT.desktop,
            })
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)

        if path in ("/", "/index.html"):
            return self._send_file(STATIC / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            if target == STATIC or not target.is_relative_to(STATIC):  # 防路径穿越
                return self._send_json({"error": "forbidden"}, 403)
            return self._send_file(target)

        m = re.fullmatch(r"/api/tasks/([0-9a-f]+)", path)
        if m:
            sess = SESSIONS.get(m.group(1))
            if not sess:
                return self._send_json({"error": "task not found"}, 404)
            return self._send_json(sess.snapshot())

        m = re.fullmatch(r"/api/tasks/([0-9a-f]+)/messages", path)
        if m:
            sess = SESSIONS.get(m.group(1))
            if not sess:
                return self._send_json({"error": "task not found"}, 404)
            return self._send_json({"messages": _tail_messages(sess, 40)})

        if path == "/api/tasks":
            with SESSIONS_LOCK:
                items = [
                    {"id": s.id, "requirement": s.requirement[:60], "status": s.status,
                     "stage": s.ticket_stage or s.status,
                     "needs_attention": bool(s.attention and not s.attention.get("answered_at")),
                     "created_at": s.created_at}
                    for s in SESSIONS.values()
                ]
            return self._send_json({"tasks": sorted(items, key=lambda x: x["created_at"], reverse=True)})

        if path == "/api/settings":
            return self._send_json({
                "desktop": SERVER_CONTEXT.desktop,
                "model": SETTINGS.model,
                "base_url": SETTINGS.base_url or "",
                "has_api_key": bool(SETTINGS.api_key),
                "mock": SETTINGS.force_mock,
            })

        if path == "/api/config":
            return self._send_json({
                "project_root": SERVER_CONTEXT.default_project_root or str(DEFAULT_PROJECT_ROOT),
                "workspace_root": SERVER_CONTEXT.workspace_root,
                "desktop": SERVER_CONTEXT.desktop,
                "version": SERVER_CONTEXT.version,
                "model": SETTINGS.model,
                "base_url": SETTINGS.base_url or "",
                "has_api_key": bool(SETTINGS.api_key),
                "mock": SETTINGS.force_mock,
                "use_mock": SETTINGS.use_mock,
                "base_url": SETTINGS.base_url or "",
                "max_iter": SETTINGS.max_iterations,
                "max_plan": SETTINGS.max_plan_rounds,
            })

        return self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        body = self._read_json()

        if path == "/api/settings":
            if "api_key" in body:
                SETTINGS.api_key = str(body.get("api_key") or "")
            if "base_url" in body:
                SETTINGS.base_url = str(body.get("base_url") or "") or None
            if "model" in body:
                SETTINGS.model = str(body.get("model") or "gpt-4o-mini")
            if "mock" in body:
                SETTINGS.force_mock = bool(body.get("mock"))
            try:
                from agents import nodes
                nodes.set_llm(None)  # 下次调用按新配置重建 LLM
            except Exception:  # noqa: BLE001
                pass
            if SERVER_CONTEXT.settings_saver:
                SERVER_CONTEXT.settings_saver({
                    "api_key": SETTINGS.api_key,
                    "base_url": SETTINGS.base_url or "",
                    "model": SETTINGS.model,
                    "mock": SETTINGS.force_mock,
                })
            return self._send_json({
                "ok": True,
                "model": SETTINGS.model,
                "base_url": SETTINGS.base_url or "",
                "has_api_key": bool(SETTINGS.api_key),
                "mock": SETTINGS.force_mock,
            })

        if path == "/api/tasks":
            return self._create_task(body)

        m = re.fullmatch(r"/api/tasks/([0-9a-f]+)/attention", path)
        if m:
            sess = SESSIONS.get(m.group(1))
            if not sess:
                return self._send_json({"error": "task not found"}, 404)
            if not sess.attention or sess.attention.get("answered_at"):
                return self._send_json({"error": "当前没有待处理事项"}, 409)
            attention_id = str(body.get("attention_id") or "")
            if attention_id and attention_id != sess.attention.get("id"):
                return self._send_json({"error": "待办 ID 不匹配"}, 409)
            option = str(body.get("option") or "")
            scope = body.get("scope_files") or []
            if isinstance(scope, str):
                scope = [s.strip() for s in scope.split(",") if s.strip()]
            try:
                _handle_attention(sess, option, str(body.get("note") or ""), scope)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": True, "status": sess.status, "stage": sess.ticket_stage})

        m = re.fullmatch(r"/api/tasks/([0-9a-f]+)/review", path)
        if m:
            sess = SESSIONS.get(m.group(1))
            if not sess:
                return self._send_json({"error": "task not found"}, 404)
            if sess.status != "awaiting_review":
                return self._send_json({"error": f"当前状态 {sess.status} 不接受审核"}, 409)
            action = body.get("action")
            if action == "approve":
                decision = ReviewDecision(ReviewDecision.APPROVE)
            elif action == "reject":
                decision = ReviewDecision(ReviewDecision.REVISE, note=body.get("note") or "未说明理由")
            elif action == "user_plan":
                text = (body.get("user_plan_text") or "").strip()
                if not text:
                    return self._send_json({"error": "自拟方案内容不能为空"}, 400)
                scope = body.get("scope_files") or []
                if isinstance(scope, str):
                    scope = [s.strip() for s in scope.split(",") if s.strip()]
                decision = ReviewDecision(
                    ReviewDecision.USER_PLAN,
                    note=body.get("note") or "",
                    user_plan_text=text,
                    scope_files=scope or None,
                )
            else:
                return self._send_json({"error": f"未知 action: {action}"}, 400)

            sess.submit_review(decision)
            return self._send_json({"ok": True, "status": sess.status})

        return self._send_json({"error": "not found"}, 404)

    # ---- 创建任务 ----
    def _create_task(self, body: dict) -> None:
        requirement = (body.get("requirement") or "").strip()
        if not requirement:
            return self._send_json({"error": "需求不能为空"}, 400)

        default_project = SERVER_CONTEXT.default_project_root or str(DEFAULT_PROJECT_ROOT)
        project_root = (body.get("project_root") or default_project).strip()
        if not Path(project_root).exists():
            return self._send_json({"error": f"项目目录不存在: {project_root}"}, 400)

        workspace_root = body.get("workspace_root") or SERVER_CONTEXT.workspace_root
        if workspace_root:
            workspace_root = str(Path(str(workspace_root)).expanduser().resolve())
        else:
            workspace_root = str(Path(project_root) / SETTINGS.workspace_dirname)
        try:
            Path(workspace_root).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._send_json({"error": f"工作区目录不可写: {exc}"}, 400)

        task_id = uuid.uuid4().hex[:12]
        sess = Session(
            task_id, requirement, project_root, body.get("task_name"),
            workspace_root=workspace_root,
        )
        with SESSIONS_LOCK:
            SESSIONS[task_id] = sess

        threading.Thread(target=_run_task, args=(sess,), daemon=True).start()
        return self._send_json({"task_id": task_id}, 201)


def _tail_messages(sess: Session, n: int) -> list[dict]:
    """读取工作区的黑板消息（最近 n 条）。"""
    if not sess.workspace:
        return []
    path = Path(sess.workspace) / "messages.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


# --------------------------------------------------------------------------- #
class ServerHandle:
    """可嵌入的 HTTP 服务句柄，供桌面端启动/关闭。"""

    def __init__(
        self,
        httpd: ThreadingHTTPServer,
        *,
        token: str,
        workspace_root: str,
        default_project_root: str,
        desktop: bool,
    ) -> None:
        self.httpd = httpd
        self.token = token
        self.workspace_root = workspace_root
        self.default_project_root = default_project_root
        self.desktop = desktop
        host, port = httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self.thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> "ServerHandle":
        if self.thread is None:
            self.thread = threading.Thread(
                target=self.httpd.serve_forever,
                name="agent-web-server",
                daemon=True,
            )
            self.thread.start()
        return self

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """轮询 /api/health，直到服务就绪或超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base_url + "/api/health", timeout=1.0) as resp:
                    if resp.status == 200:
                        return True
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        return False

    def has_running_tasks(self) -> bool:
        with SESSIONS_LOCK:
            return any(
                s.status in {"planning", "awaiting_review", "executing"}
                for s in SESSIONS.values()
            )

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.thread is not None and self.thread.is_alive():
            try:
                self.httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        if self.thread is not None:
            self.thread.join(timeout=5.0)


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    token: str = "",
    workspace_root: str | Path | None = None,
    default_project_root: str | Path | None = None,
    desktop: bool = False,
    settings_saver: Callable[[dict], None] | None = None,
) -> ServerHandle:
    """创建一个可嵌入的 HTTP 服务（不自动阻塞）。"""
    global SERVER_CONTEXT
    SERVER_CONTEXT = ServerContext(
        token=token,
        workspace_root=str(workspace_root or ""),
        default_project_root=str(default_project_root or ""),
        desktop=desktop,
        version=APP_VERSION,
        settings_saver=settings_saver,
    )
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return ServerHandle(
        httpd,
        token=token,
        workspace_root=SERVER_CONTEXT.workspace_root,
        default_project_root=SERVER_CONTEXT.default_project_root,
        desktop=desktop,
    )


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    token: str = "",
    workspace_root: str | Path | None = None,
    default_project_root: str | Path | None = None,
) -> None:
    """浏览器模式：阻塞运行，保留给开发/调试使用。"""
    handle = create_server(
        host,
        port,
        token=token,
        workspace_root=workspace_root or (ROOT / SETTINGS.workspace_dirname),
        default_project_root=default_project_root or ROOT,
        desktop=False,
    )
    mode = "MockLLM（离线）" if SETTINGS.use_mock else f"{SETTINGS.model} @ {SETTINGS.base_url}"
    print("=" * 66)
    print("  LangGraph 多智能体代码生成团队 —— Web 前端")
    print("=" * 66)
    print(f"  访问地址: {handle.base_url}")
    print(f"  默认项目: {handle.default_project_root}")
    print(f"  工作区:   {handle.workspace_root}")
    print(f"  模型配置: {mode}")
    print("=" * 66)
    print("  按 Ctrl+C 停止")
    try:
        handle.httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        handle.shutdown()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="多智能体团队 Web 前端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace-root", default=None)
    args = ap.parse_args()
    serve(args.host, args.port, workspace_root=args.workspace_root)
