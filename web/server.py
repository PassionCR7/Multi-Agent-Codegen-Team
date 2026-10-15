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
    GET  /                           前端页面
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 允许 `python web/server.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.config import SETTINGS  # noqa: E402
from agents.plan import is_approved  # noqa: E402
from agents.review import ReviewDecision, run_planning_phase  # noqa: E402
from agents.runner import run_team  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"

TERMINAL_STATES = {"done", "error", "aborted", "plan_aborted"}


# --------------------------------------------------------------------------- #
# 会话（一次任务 = 一个会话）
# --------------------------------------------------------------------------- #
class Session:
    """一个任务的完整运行态。所有跨线程访问都过 lock。"""

    def __init__(self, task_id: str, requirement: str, project_root: str, task_name: str | None):
        self.id = task_id
        self.requirement = requirement
        self.project_root = project_root
        self.task_name = task_name or f"web-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

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
                "workspace": self.workspace,
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

        ws, plan, info = run_planning_phase(
            requirement=session.requirement,
            project_root=project_root,
            workspace_root=project_root / SETTINGS.workspace_dirname,
            task_name=session.task_name,
            reviewer=WebReviewer(session),
            on_event=on_event,
        )
        with session.lock:
            session.workspace = str(ws.dir)

        if not plan:
            with session.lock:
                session.status = "plan_aborted"
                session.abort_reason = info.get("abort_reason", "计划未获批准")
            session.add_log("计划未获批准，流程结束")
            return

        # ---------------- 执行阶段 ----------------
        with session.lock:
            session.status = "executing"
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
                        "at": datetime.now().strftime("%H:%M:%S"),
                    })
            elif ev.get("type") == "finish":
                pass

        result = run_team(
            requirement=session.requirement,
            project_root=project_root,
            task_name=session.task_name,
            plan=plan,
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
            session.status = "done" if session.success else "aborted"
        session.add_log(f"执行结束：{'成功' if session.success else '未完全达成'}")

    except Exception as exc:  # noqa: BLE001
        with session.lock:
            session.status = "error"
            session.error = f"{type(exc).__name__}: {exc}"
        session.add_log(f"运行出错: {type(exc).__name__}: {exc}")
        traceback.print_exc()


# --------------------------------------------------------------------------- #
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
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            return self._send_file(STATIC / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            if not str(target).startswith(str(STATIC)):   # 防路径穿越
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
                     "created_at": s.created_at}
                    for s in SESSIONS.values()
                ]
            return self._send_json({"tasks": sorted(items, key=lambda x: x["created_at"], reverse=True)})

        if path == "/api/config":
            return self._send_json({
                "project_root": str(ROOT),
                "model": SETTINGS.model,
                "use_mock": SETTINGS.use_mock,
                "base_url": SETTINGS.base_url or "",
                "max_iter": SETTINGS.max_iterations,
                "max_plan": SETTINGS.max_plan_rounds,
            })

        return self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_json()

        if path == "/api/tasks":
            return self._create_task(body)

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

        project_root = (body.get("project_root") or str(ROOT)).strip()
        if not Path(project_root).exists():
            return self._send_json({"error": f"项目目录不存在: {project_root}"}, 400)

        task_id = uuid.uuid4().hex[:12]
        sess = Session(task_id, requirement, project_root, body.get("task_name"))
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
def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    mode = "MockLLM（离线）" if SETTINGS.use_mock else f"{SETTINGS.model} @ {SETTINGS.base_url}"
    print("=" * 66)
    print("  LangGraph 多智能体代码生成团队 —— Web 前端")
    print("=" * 66)
    print(f"  访问地址: http://{host}:{port}")
    print(f"  默认项目: {ROOT}")
    print(f"  模型配置: {mode}")
    print("=" * 66)
    print("  按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="多智能体团队 Web 前端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    serve(args.host, args.port)