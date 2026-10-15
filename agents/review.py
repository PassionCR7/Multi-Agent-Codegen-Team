"""计划审核循环（Phase 0）—— 用户确认 Plan 的交互层。

这是"Plan-Act 先确认再执行"的落地实现。流程：

    1. planner 生成 Plan（草稿）
    2. 交给 reviewer（用户 / 自动化策略）审核
    3. reviewer 三种决策：
         approve  —— 通过，注入计划，放行到执行阶段
         revise   —— 打回，给理由，planner 重新规划
         user     —— 用户自拟方案，结合原始需求 + 补充说明生成新 Plan，再审核
    4. 循环直到通过，或达到重规划上限

设计要点：reviewer 是一个**可插拔回调**，因此同一套逻辑既能驱动 CLI 交互，
也能被自动化测试/CI 复用（传一个始终 approve 的 reviewer 即可）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol

from .config import SETTINGS
from .graph import build_graph
from .plan import (
    STATUS_APPROVED,
    STATUS_USER_WRITTEN,
    from_user_text,
    normalize_plan,
    render_plan,
)
from .state import new_state
from .workspace import Workspace, new_workspace

# --------------------------------------------------------------------------- #
# reviewer 协议
# --------------------------------------------------------------------------- #
class ReviewDecision:
    """审核结果。"""

    APPROVE = "approve"
    REVISE = "revise"
    USER_PLAN = "user_plan"

    def __init__(self, action: str, note: str = "", user_plan_text: str = "", scope_files: list[str] | None = None):
        self.action = action
        self.note = note                 # 打回理由，或用户方案的补充说明
        self.user_plan_text = user_plan_text
        self.scope_files = scope_files


class Reviewer(Protocol):
    def review(self, plan: dict[str, Any], round_index: int) -> ReviewDecision: ...


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
def run_planning_phase(
    *,
    requirement: str,
    project_root: Path,
    workspace_root: Path,
    task_name: str | None,
    reviewer: Reviewer,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Workspace, dict[str, Any], dict[str, Any]]:
    """Phase 0：反复规划/审核，直到计划被批准。

    Returns:
        (工作区, 已批准的计划, 用于启动执行阶段的初始状态)
    """
    # ---- 建工作区 + 扫描真实项目（与执行阶段共用同一份快照）----
    from .scanner import ProjectScanner

    snapshot = ProjectScanner(project_root).scan()
    ws = new_workspace(workspace_root, requirement, task_name)
    ws.write_snapshot(snapshot)
    ws.post_message(
        "system",
        f"已扫描真实项目：{snapshot.get('totals', {}).get('files_scanned', 0)} 个文件，"
        f"语言构成 {snapshot.get('languages', {})}",
        kind="lifecycle",
    )
    if on_event:
        on_event({"type": "workspace", "path": str(ws.dir), "snapshot": snapshot.get("totals", {})})

    state = new_state(
        requirement=requirement,
        project_root=str(project_root),
        workspace_root=str(workspace_root),
        workspace_dir=str(ws.dir),
        plan=None,  # 强制先走 planner
    )
    ws.update_status(phase="planning")

    # 只跑到"计划产出"为止：让 supervisor -> planner 走一轮
    plan_app = build_graph()
    plan: dict[str, Any] = {}
    round_index = 0
    user_note = ""

    while True:
        round_index += 1

        # ---- 1) 生成或重生成计划 ----
        # 构造本次规划的状态：把上一轮打回意见作为 review_note 传给 planner
        plan_state = dict(state)
        plan_state["plan"] = {
            "review_note": user_note,
            "status": "draft" if round_index == 1 else "rejected",
        }
        # 关键：让 planner 的 revision 与本轮序号一致
        plan_state["plan_rounds"] = round_index - 1

        # 关键：只消费到 planner 产出计划即停止，**不能**让图继续跑
        # （计划尚未获批，supervisor 会不断把它路由回 planner，直到熔断）
        for chunk in plan_app.stream(plan_state, config={"recursion_limit": 50}, stream_mode="updates"):
            stop = False
            for node_name, update in chunk.items():
                if not update:
                    continue
                if node_name == "planner" and "plan" in update:
                    plan = update["plan"]
                    state.update({k: v for k, v in update.items() if k != "plan"})
                    stop = True          # 拿到草稿，立刻停止消费
                elif node_name in ("coder", "aligner", "security", "bugfixer", "repair"):
                    # 计划未被批准时不应进入执行节点；若发生说明流程异常
                    raise RuntimeError(f"计划尚未批准却进入了执行节点 {node_name}，流程异常")
                else:
                    state.update(update)
            if stop:
                break

        if on_event:
            on_event({"type": "plan_draft", "round": round_index, "plan": plan})

        # ---- 2) 交给 reviewer 审核 ----
        decision = reviewer.review(plan, round_index)

        if decision.action == ReviewDecision.APPROVE:
            plan = dict(plan)
            plan["status"] = STATUS_APPROVED
            ws.write_report(f"plan_v{round_index}_approved", plan)
            ws.write_report("plan_approved", plan)  # 固定文件名，供 --workspace --yes 复用
            (ws.reports_dir / "plan_approved.md").write_text(render_plan(plan), encoding="utf-8")
            ws.post_message("user", "计划已批准，进入执行阶段。", kind="plan_decision", status="approved")
            ws.record_turn("user", "plan:approved", 0, f"第 {round_index} 版计划获批")
            if on_event:
                on_event({"type": "plan_approved", "plan": plan})
            break

        if decision.action == ReviewDecision.USER_PLAN:
            # 用户自拟方案 —— 结合首次 prompt 与用户补充，转成标准 Plan 再走审核
            user_note = decision.note or ""
            plan = from_user_text(
                decision.user_plan_text,
                requirement=requirement,
                scope_files=decision.scope_files,
            )
            plan["revision"] = round_index
            ws.write_report(f"plan_v{round_index}_user", plan)
            (ws.reports_dir / f"plan_v{round_index}_user.md").write_text(render_plan(plan), encoding="utf-8")
            ws.post_message(
                "user", "用户提交了自拟方案，已转为结构化计划待确认。"
                + (f" 补充说明: {user_note}" if user_note else ""),
                kind="plan_decision", status="user_written",
            )
            if on_event:
                on_event({"type": "plan_user_written", "round": round_index, "plan": plan})

            # 自拟方案同样需要用户确认一次
            decision2 = reviewer.review(plan, round_index)
            if decision2.action == ReviewDecision.APPROVE:
                plan["status"] = STATUS_APPROVED
                ws.write_report(f"plan_v{round_index}_user_approved", plan)
                ws.write_report("plan_approved", plan)
                (ws.reports_dir / "plan_approved.md").write_text(render_plan(plan), encoding="utf-8")
                ws.post_message("user", "用户自拟计划已确认，进入执行阶段。", kind="plan_decision", status="approved")
                ws.record_turn("user", "plan:approved", 0, f"用户自拟计划（第 {round_index} 版）确认")
                if on_event:
                    on_event({"type": "plan_approved", "plan": plan})
                break
            # 否则继续循环（把用户的意见作为下一轮输入）
            user_note = decision2.note or user_note
            ws.post_message("user", f"自拟方案被打回：{user_note}", kind="plan_decision", status="revise")
            continue

        # REVISE
        user_note = decision.note or "（未说明理由）"
        ws.write_report(f"plan_v{round_index}_rejected", {**plan, "review_note": user_note})
        ws.post_message("user", f"计划被打回，理由：{user_note}", kind="plan_decision", status="rejected")
        ws.record_turn("user", "plan:rejected", 0, user_note)

        if round_index >= SETTINGS.max_plan_rounds:
            ws.post_message(
                "system",
                f"计划重规划已达上限（{SETTINGS.max_plan_rounds} 轮），流程中止。",
                kind="lifecycle",
            )
            ws.update_status(phase="aborted", plan_aborted=True)
            return ws, {}, {
                "abort_reason": "计划审核未在限定轮次内达成一致",
                "success": False,
            }
        # 继续下一轮：planner 会读到 review_note 并针对性重规划

    ws.update_status(phase="planned", plan_status="approved", plan_revision=plan.get("revision"))
    return ws, plan, state