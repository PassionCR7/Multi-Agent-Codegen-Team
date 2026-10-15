"""LangGraph 共享状态定义。

状态是 agent 之间传递信息的"信封"。这里刻意把**持久化的事实**（工作区、
项目快照）与**流转中的决策**（阶段、产物、报告）分开：

    * 持久化事实放磁盘（Workspace），状态里只存路径 —— 避免状态膨胀。
    * 流转决策放内存状态，用 reducer 做增量合并 —— 便于回溯每一步。

列表字段使用 ``operator.add`` 作为 reducer：节点只需返回"新增的部分"，
LangGraph 会自动追加，天然支持多轮迭代时累积产物与历史。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Phase = Literal[
    "start", "planning", "planned", "coding", "aligning", "security",
    "bugfixing", "repairing", "replanning", "done", "aborted",
]


class AgentState(TypedDict, total=False):
    """多智能体共享状态。"""

    # --- 输入（不可变）---
    requirement: str                      # 用户需求原文
    acceptance: list[str]                 # 验收标准
    project_root: str                     # 真实项目根目录
    workspace_root: str                   # 工作区上级目录
    workspace_dir: str                    # 本任务的工作区目录

    # --- 流转控制 ---
    iteration: int                        # 当前大循环轮次
    bugfix_rounds: int                    # 修复轮次（独立计数，防止死循环）
    plan_rounds: int                      # 计划重规划轮次（独立计数）
    code_attempts: int                    # coder 尝试产出代码的次数（用于兜底交白卷）
    phase: Phase                          # 当前阶段 / 下一步路由目标
    next_agent: str                       # supervisor 决定的下一跳

    # --- 增量累积（reducer）---
    artifacts: Annotated[list[dict], operator.add]      # 历次代码产物（含修订）
    route_history: Annotated[list[dict], operator.add]  # 路由轨迹
    feedback: Annotated[list[str], operator.add]        # 传给 coder/bugfixer 的反馈
    errors: Annotated[list[str], operator.add]          # 运行期错误

    # --- 各 agent 的最新结论（覆盖式）---
    plan: dict                            # 已批准的行动计划（强约束依据）
    alignment: dict                       # 需求对齐报告
    security: dict                        # 安全审计报告
    bugfix: dict                          # bug 修复报告
    coder_notes: str                      # coder 的说明
    pending_scope_violations: list[str]   # 最近一轮产物越出计划范围的文件（程序化检测）
    read_audit: list[dict]                # coder 按需读取真实项目文件的审计日志
    reconcile: dict                       # 跨模块对账报告（机器信号 + LLM 裁决）

    # --- 输出 ---
    final_summary: str
    success: bool
    abort_reason: str


def new_state(
    requirement: str,
    project_root: str,
    workspace_root: str,
    workspace_dir: str,
    *,
    plan: dict | None = None,
) -> AgentState:
    """构造初始状态。

    Args:
        plan: 若用户在进入执行阶段前已批准行动计划，从这里注入；
              不传则为 None，由 supervisor 先路由到 planner。
    """
    return AgentState(
        requirement=requirement,
        acceptance=[
            "代码可运行且无语法错误",
            "实现覆盖需求中的全部功能点",
            "无高危安全问题",
            "与真实项目既有约定/结构一致",
            "（若已批准计划）不超出计划白名单范围",
        ],
        project_root=project_root,
        workspace_root=workspace_root,
        workspace_dir=workspace_dir,
        iteration=0,
        bugfix_rounds=0,
        plan_rounds=0,
        code_attempts=0,
        phase="start",
        next_agent="planner" if plan is None else "coder",
        plan=plan or {},
        artifacts=[],
        route_history=[],
        feedback=[],
        errors=[],
        alignment={},
        security={},
        bugfix={},
        reconcile={},
        coder_notes="",
        final_summary="",
        success=False,
        abort_reason="",
    )


def latest_artifacts(state: AgentState) -> list[dict]:
    """取最新一版产物：同名文件只保留最后一次修订。"""
    seen: dict[str, dict] = {}
    for art in state.get("artifacts", []):
        for f in art.get("files", []):
            seen[f["path"]] = f
    return list(seen.values())


def artifacts_digest(state: AgentState, limit_per_file: int = 4000) -> str:
    """把最新产物渲染成文本，供对齐/安全/修复 agent 阅读。"""
    files = latest_artifacts(state)
    if not files:
        return "（当前没有代码产物）"
    parts = []
    for f in files:
        content = f.get("content", "")
        if len(content) > limit_per_file:
            content = content[:limit_per_file] + "\n... (已截断)"
        parts.append(f"### 文件: {f['path']}  [语言: {f.get('language', '?')}]\n```{f.get('language', '')}\n{content}\n```")
    return "\n\n".join(parts)


def summarize_state(state: AgentState) -> dict[str, Any]:
    """给 UI 用的状态摘要。"""
    from .plan import summarize as summarize_plan

    return {
        "iteration": state.get("iteration", 0),
        "phase": state.get("phase", "?"),
        "artifacts": len(latest_artifacts(state)),
        "plan": summarize_plan(state.get("plan")),
        "aligned": bool(state.get("alignment")),
        "safe": state.get("security", {}).get("safe"),
        "bugs": len(state.get("bugfix", {}).get("bugs", [])),
        "success": state.get("success", False),
    }