"""SUPERVISOR（调度官）。

设计取舍：默认使用**确定性规则**做路由，而不是让 LLM 自由决策。
为什么？路由是最不能出错的一环——LLM 的随机性可能导致死循环（A 让 B 改、
B 又让 A 改）或提前收尾。规则化后每次流转都可解释、可回溯、可复现。

只有在需要"评估质量决定是否收尾"的模糊场景，才可选地征询 LLM。
"""

from __future__ import annotations

from typing import Any, Literal

from .config import SETTINGS
from .plan import is_approved
from .state import AgentState, latest_artifacts
from .utils import has_blocking_issues
from .workspace import Workspace

Route = Literal["planner", "coder", "aligner", "security", "bugfixer", "repair", "done", "abort"]


def _ws(state: AgentState) -> Workspace:
    """重新打开状态中记录的工作区（不新建目录）。"""
    return Workspace.from_dir(state["workspace_dir"])


def decide(state: AgentState) -> Route:
    """核心路由决策：给定当前状态，返回下一跳。"""
    iteration = state.get("iteration", 0)
    bugfix_rounds = state.get("bugfix_rounds", 0)
    phase = state.get("phase", "start")
    plan = state.get("plan", {}) or {}

    has_code = bool(latest_artifacts(state))
    alignment = state.get("alignment", {}) or {}
    security = state.get("security", {}) or {}

    # 0) 达到总循环上限 —— 兜底熔断
    if iteration >= SETTINGS.max_iterations:
        return "abort"

    # 0.5) 尚无「已批准的计划」 -> 必须先出计划（等待用户在 runner 的审核循环里确认）
    #
    # 把"计划"作为执行前的硬性前置条件：没有经用户批准的计划，不允许写代码。
    # 这一步确保了"人工确认 Plan"是流程的结构性环节，而不是可选步骤。
    #
    # 注意：正常路径下，runner 会在进入图之前完成计划审核并注入已批准计划，
    # 所以这个分支平常不会触发。它是一道安全网——若被触发且反复规划仍无法
    # 通过审批，则按上限熔断，避免 planner ↔ supervisor 死循环。
    if not is_approved(plan):
        if state.get("plan_rounds", 0) >= SETTINGS.max_plan_rounds:
            return "abort"
        return "planner"

    # 1) 没有代码 -> 必须先写代码（但有次数上限，防止 coder 一直交白卷导致死循环）
    if not has_code:
        if state.get("code_attempts", 0) >= SETTINGS.max_code_attempts:
            return "abort"
        return "coder"

    # 2) 刚修复完 -> 先清空过期的检查结论，再让对齐/安全复检
    #
    # 这一步是闭环的关键闸门。若无此分流，bugfixer 修完代码后，旧的
    # alignment/security 报告仍留在状态里（其中的问题条目并未被清除），
    # supervisor 会再次判定"存在阻断问题"→ 又跳回 bugfixer，形成死循环，
    # 直到命中上限被杀。repair 节点负责把过期结论清空，使流水线重新走一遍
    # aligner → security，从而真正验证补丁是否生效。
    if phase == "bugfixing":
        return "repair"

    # 3) 有代码但还没对齐 -> 对齐
    if not alignment:
        return "aligner"

    # 4) 已对齐但还没安全审计 -> 安全
    if not security:
        return "security"

    # 5) 对齐 + 安全都做过 -> 判断是否有阻断性问题
    blocking, reasons = has_blocking_issues(alignment, security)
    if blocking:
        if bugfix_rounds >= SETTINGS.max_bugfix_rounds:
            return "abort"  # 修不动了，交还人类
        return "bugfixer"

    # 6) 全部通过
    return "done"


def apply(state: AgentState) -> dict[str, Any]:
    """执行决策并把结果落盘到黑板。"""
    route = decide(state)
    ws = _ws(state)

    reason_map = {
        "planner": "尚无已批准的计划，需先规划（等待用户确认）",
        "coder": "缺少代码产物，需要编码",
        "aligner": "代码就绪，待需求对齐",
        "security": "对齐完成，待安全审计",
        "bugfixer": "存在阻断性问题，需修复",
        "repair": "修复完成，待复检",
        "done": "全部检查通过，任务达成",
        "abort": "达到循环上限，中止并交还人类",
    }
    note = reason_map.get(route, "")

    ws.post_message("supervisor", f"决策: {route} —— {note}", kind="decision", route=route)
    ws.record_turn("supervisor", f"route:{route}", state.get("iteration", 0), note)

    delta: dict[str, Any] = {
        "next_agent": route,
        "phase": "aborted" if route == "abort" else ("done" if route == "done" else state.get("phase", "start")),
        "route_history": [{"agent": "supervisor", "decision": route, "iteration": state.get("iteration", 0), "note": note}],
    }
    if route == "abort":
        blocking, reasons = has_blocking_issues(state.get("alignment", {}), state.get("security", {}))
        if state.get("code_attempts", 0) >= SETTINGS.max_code_attempts and not latest_artifacts(state):
            delta["abort_reason"] = (
                f"coder 连续 {state.get('code_attempts')} 次未产出任何文件"
                f"（上限 {SETTINGS.max_code_attempts}），交还人类检查提示词或模型输出格式"
            )
        else:
            delta["abort_reason"] = (
                f"循环上限触发（iteration={state.get('iteration')}, bugfix_rounds={state.get('bugfix_rounds')}）"
                + (f"；仍存在未解决问题: {'; '.join(reasons[:3])}" if reasons else "")
            )
        delta["success"] = False
    return delta


def route_from_supervisor(state: AgentState) -> str:
    """条件边函数：LangGraph 用它选择下一个节点。"""
    return state.get("next_agent", "abort")


# 存储路由描述，供文档/日志使用
ROUTE_DESCRIPTIONS = {
    "planner": "制定行动计划",
    "coder": "编码",
    "aligner": "需求对齐",
    "security": "安全审计",
    "bugfixer": "缺陷修复",
    "repair": "修复后复检",
    "done": "任务完成",
    "abort": "中止",
}