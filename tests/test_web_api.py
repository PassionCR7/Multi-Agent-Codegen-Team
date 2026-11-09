"""Web API 端到端测试（驱动真实 HTTP 接口，带轮询）。

用法（需先启动 web/server.py）::

    ./.venv/Scripts/python.exe tests/test_web_api.py --base http://127.0.0.1:8767
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


def _req(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def wait_status(base: str, tid: str, want: set[str], timeout: float = 60.0) -> dict:
    """轮询直到任务进入目标状态（这才是真实前端的做法）。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _, last = _req(base, "GET", f"/api/tasks/{tid}")
        if last.get("status") in want:
            return last
        time.sleep(0.4)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 62)
    print(f"  Web API 端到端测试  {base}")
    print("=" * 62)

    # [1] 配置接口
    code, cfg = _req(base, "GET", "/api/config")
    check(code == 200 and "project_root" in cfg, "GET /api/config 可用")
    project_root = cfg.get("project_root") or ""

    # [2] 静态页面
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            html = r.read().decode("utf-8")
        check("<title>" in html and "多智能体" in html, "GET / 返回前端页面")
    except Exception as e:  # noqa: BLE001
        check(False, f"GET / 失败: {e}")
        html = ""

    # [3] 创建任务
    code, created = _req(base, "POST", "/api/tasks", {
        "requirement": "实现一个计算斐波那契数列的函数",
        "project_root": project_root,
        "task_name": "api-test",
    })
    check(code == 201 and created.get("task_id"), f"POST /api/tasks 创建任务（{code}）")
    tid = created.get("task_id", "")

    # [4] 空需求应被拒绝
    code, _ = _req(base, "POST", "/api/tasks", {"requirement": "   ", "project_root": project_root})
    check(code == 400, "空需求被拒绝（400）")

    # [5] 等待计划生成
    st = wait_status(base, tid, {"awaiting_review"})
    check(st.get("status") == "awaiting_review", "任务进入 awaiting_review（规划线程已挂起等待）")
    check(st.get("ticket_stage") == "plan_review", f"阶段牌为 plan_review（实际 {st.get('ticket_stage')}）")
    plan = st.get("plan") or {}
    check(bool(plan.get("steps")), f"计划含步骤（{len(plan.get('steps', []))} 步）")
    check(bool(plan.get("scope", {}).get("files")), f"计划含范围白名单 {plan.get('scope', {}).get('files')}")

    # [6] 非等待状态时重复提交应被拒绝（并发安全）
    #     此处状态正是 awaiting_review，先测非法 action
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/review", {"action": "乱来"})
    check(code == 400, "未知 action 被拒绝（400）")

    # [7] 打回 -> 重规划
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/review",
                   {"action": "reject", "note": "只保留一个文件，不要测试文件"})
    check(code == 200, "提交打回成功")
    st = wait_status(base, tid, {"awaiting_review"})
    check(st.get("plan", {}).get("revision") == 2, f"重规划后版本递增到 2（实际 {st.get('plan',{}).get('revision')}）")

    # [8] 用户自拟方案
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/review", {
        "action": "user_plan",
        "user_plan_text": "1. 新建 solution.py 实现 fibonacci\n2. 新建 test_solution.py 补测试",
        "note": "优先可读性",
        "scope_files": "solution.py, test_solution.py",
    })
    check(code == 200, "提交用户自拟方案成功")
    st = wait_status(base, tid, {"awaiting_review"})
    p = st.get("plan", {})
    check(p.get("source") == "user", "计划来源标记为 user")
    check("solution.py" in (p.get("scope", {}).get("files") or []), "用户指定的范围被采纳")

    # [9] 空自拟方案应被拒绝
    #     先打回一次回到等待态，再测空内容
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/review", {"action": "user_plan", "user_plan_text": "  "})
    check(code == 400, "空自拟方案被拒绝（400）")

    # [10] 批准并执行
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/review", {"action": "approve"})
    check(code == 200, "提交批准成功")
    st = wait_status(base, tid, {"done", "aborted", "error"}, timeout=90)
    check(st.get("status") in {"done", "aborted"}, f"任务进入终态（{st.get('status')}）")
    check(st.get("ticket_stage") in {"awaiting_review", "awaiting_answer", "failed", "blocked"},
          f"终态带生命周期阶段（实际 {st.get('ticket_stage')}）")
    check(bool(st.get("report")), "终态带结构化报告")
    check(bool(st.get("timeline")), f"产生执行时间线（{len(st.get('timeline', []))} 条）")

    arts = [a["path"] for a in st.get("artifacts", [])]
    check(bool(arts), f"产出文件（{arts}）")
    check("solution.py" in arts, "产物落在计划白名单内")

    # [11] 终态后不可再审
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/review", {"action": "approve"})
    check(code == 409, "终态后拒绝再审（409）")

    # [11b] 没有待办时，attention 接口返回 409
    code, _ = _req(base, "POST", f"/api/tasks/{tid}/attention", {"option": "retry"})
    check(code == 409, "无待办时 attention 返回 409")

    # [11c] 任务列表带阶段牌与待办标记
    code, listing = _req(base, "GET", "/api/tasks")
    items = listing.get("tasks", [])
    check(code == 200 and any(t.get("stage") for t in items), "任务列表带 stage 字段")
    check(any("needs_attention" in t for t in items), "任务列表带 needs_attention 字段")

    # [12] 黑板消息
    code, msgs = _req(base, "GET", f"/api/tasks/{tid}/messages")
    check(code == 200 and len(msgs.get("messages", [])) > 0, f"GET messages 返回黑板消息（{len(msgs.get('messages', []))} 条）")

    # [13] 不存在的任务
    code, _ = _req(base, "GET", "/api/tasks/ffffffffffff")
    check(code == 404, "不存在的任务返回 404")

    # [14] 路径穿越防护
    code, _ = _req(base, "GET", "/static/../server.py")
    check(code in (403, 404), f"静态目录阻止路径穿越（{code}）")

    # [15] 越界 → 待办 → 纳入范围 → 重跑成功
    code, created2 = _req(base, "POST", "/api/tasks", {
        "requirement": "实现一个加法函数",
        "project_root": project_root,
    })
    tid2 = created2.get("task_id", "")
    check(code == 201 and bool(tid2), "创建第二个任务用于范围闸门测试")
    st2 = wait_status(base, tid2, {"awaiting_review"})
    _req(base, "POST", f"/api/tasks/{tid2}/review", {
        "action": "user_plan",
        "user_plan_text": "1. 新建 allowed.py 实现加法",
        "scope_files": "allowed.py",
    })
    st2 = wait_status(base, tid2, {"awaiting_review"})
    check(st2.get("plan", {}).get("scope", {}).get("files") == ["allowed.py"], "用户计划范围只含 allowed.py")

    _req(base, "POST", f"/api/tasks/{tid2}/review", {"action": "approve"})
    st2 = wait_status(base, tid2, {"done", "aborted", "error"})
    att2 = st2.get("attention") or {}
    check(st2.get("ticket_stage") == "awaiting_answer", "越界后进入 awaiting_answer")
    check(att2.get("source") == "scope_violation", "待办来源为 scope_violation")
    opts2 = {o.get("id") for o in (att2.get("options") or [])}
    check("expand_scope" in opts2, "待办提供 expand_scope 选项")

    code, _ = _req(base, "POST", f"/api/tasks/{tid2}/attention", {
        "attention_id": att2.get("id"),
        "option": "expand_scope",
        "scope_files": "solution.py",
    })
    check(code == 200, "提交“纳入范围并继续”成功")
    st2 = wait_status(base, tid2, {"done", "aborted", "error"}, timeout=90)
    check(st2.get("status") == "done", f"重跑后成功（{st2.get('status')}）")
    check(st2.get("ticket_stage") == "awaiting_review", "重跑后进入 awaiting_review")
    arts2 = [a["path"] for a in st2.get("artifacts", [])]
    check("solution.py" in arts2, f"重跑产物包含 solution.py（{arts2}）")

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"  结果: {passed}/{total} 通过")
    if passed < total:
        for ok, label in _results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())