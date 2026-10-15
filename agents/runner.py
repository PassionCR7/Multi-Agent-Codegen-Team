"""运行器：把扫描、计划审核、工作区、图执行串成一条可观测的流水线。

对外提供两个入口：
    * ``run_planning`` —— Phase 0：只做"规划 + 用户确认计划"，产出已批准的 Plan
    * ``run_team``     —— Phase 1：拿着已批准的计划执行编码与校验

拆成两段是刻意的：计划确认需要人的介入（等待时间不可控），而执行阶段是纯自动的。
分开后，执行阶段可以随时从已批准的 Plan 续跑，也便于在 CI 里跳过人工环节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import SETTINGS
from .graph import build_graph
from .plan import is_approved, render_plan
from .scanner import ProjectScanner
from .state import new_state, summarize_state
from .workspace import new_workspace

# 节点名 -> 中文显示名
NODE_LABELS = {
    "supervisor": "调度官 SUPERVISOR",
    "planner": "规划 PLANNER",
    "coder": "编码 CODER",
    "aligner": "对齐 ALIGNER",
    "security": "安全 SECURITY",
    "bugfixer": "修复 BUGFIXER",
    "repair": "复检 REPAIR",
}


def run_team(
    requirement: str,
    project_root: str | Path,
    *,
    workspace_root: str | Path | None = None,
    task_name: str | None = None,
    plan: dict[str, Any] | None = None,
    workspace_dir: str | Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    checkpointer=None,
) -> dict[str, Any]:
    """运行多智能体团队完成一次编码需求（执行阶段）。

    Args:
        requirement:   用户需求原文。
        project_root:  真实项目根目录（防跑偏的扫描对象）。
        workspace_root:工作区上级目录，默认 project_root/_agent_workspace。
        task_name:     任务名，默认按时间戳生成。
        plan:          已批准的行动计划。传入后直接进入执行阶段（强约束生效）。
        workspace_dir: 复用已存在的工作区目录（通常是 Phase 0 建好的那个）。
        on_event:      事件回调，用于实时展示（CLI / Web / 日志）。
        checkpointer:  LangGraph 持久化后端（可选）。

    Returns:
        最终状态字典（含工作区路径与摘要）。
    """
    project_root = Path(project_root).resolve()
    workspace_root = Path(workspace_root).resolve() if workspace_root else project_root / SETTINGS.workspace_dirname

    # 1) 工作区：复用 Phase 0 的，或新建
    if workspace_dir:
        from .workspace import Workspace

        ws = Workspace.from_dir(workspace_dir)
    else:
        ws = new_workspace(workspace_root, requirement, task_name)
        # 1b) 新建工作区时才需要现场扫描
        snapshot = ProjectScanner(project_root).scan()
        ws.write_snapshot(snapshot)
        ws.post_message(
            "system",
            f"已扫描真实项目：{snapshot.get('totals', {}).get('files_scanned', 0)} 个文件，"
            f"语言构成 {snapshot.get('languages', {})}",
            kind="lifecycle",
        )
        if on_event:
            on_event({"type": "workspace", "path": str(ws.dir), "snapshot": snapshot.get("totals", {})})

    # 2) 计划：必须有已批准的计划，否则交给 supervisor 先规划（安全网）
    if not is_approved(plan):
        ws.post_message("system", "警告：未提供已批准的计划，将先由 PLANNER 规划。", kind="warning")

    # 3) 构造初始状态
    state = new_state(
        requirement=requirement,
        project_root=str(project_root),
        workspace_root=str(workspace_root),
        workspace_dir=str(ws.dir),
        plan=plan,
    )

    # 4) 执行图，流式推送每个节点的更新
    #
    # 关键：同时订阅 "updates" 与 "values" 两种流。
    #   * updates：本节点返回的**原始增量**，用于告知"这一步做了什么"
    #   * values ：LangGraph 用 reducer 合并后的**权威快照**，用于累积最终状态
    #
    # 为什么不能只用 updates？因为带 reducer 的列表字段（artifacts/feedback 等）
    # 在 updates 里是节点的原始返回值。若某节点返回空列表（如 bugfixer 无补丁时
    # 返回 artifacts: []），直接 update 会把之前累积的产物**整体清空**。
    # 开发中真实踩到这个坑：修复阶段一来，产物数就归零，最终产物丢失。
    app = build_graph(checkpointer=checkpointer)
    final_state: dict[str, Any] = dict(state)
    pending_nodes: list[tuple[str, dict]] = []

    for mode, chunk in app.stream(state, config={"recursion_limit": 100},
                                  stream_mode=["updates", "values"]):
        if mode == "updates":
            # 先记录本步是哪些节点、各自返回了什么增量
            pending_nodes = [(n, u or {}) for n, u in chunk.items()]
        else:  # values —— reducer 合并后的权威状态
            final_state = dict(chunk)
            # 此刻再派发事件，state 才是准确的（含已合并的产物/反馈）
            if on_event:
                summary = summarize_state(final_state)
                for node_name, update in pending_nodes:
                    on_event({
                        "type": "node",
                        "node": node_name,
                        "label": NODE_LABELS.get(node_name, node_name),
                        "state": summary,
                        "update": update,
                    })
            pending_nodes = []

    # 5) 收尾：判定成功与否并写总结
    success = _judge_success(final_state)
    summary = _build_summary(final_state, success, ws)
    ws.write_report("final_state", {
        "success": success,
        "summary": summary,
        "plan": final_state.get("plan", {}),
        "iteration": final_state.get("iteration"),
        "bugfix_rounds": final_state.get("bugfix_rounds"),
        "route_history": final_state.get("route_history", []),
        "artifacts": [
            {"path": f["path"], "language": f.get("language"), "revision": f.get("revision")}
            for art in final_state.get("artifacts", [])
            for f in art.get("files", [])
        ],
        "scope_violations": [
            v for art in final_state.get("artifacts", []) for v in (art.get("scope_violations") or [])
        ],
        "read_audit": final_state.get("read_audit", []),
        "reconcile": final_state.get("reconcile", {}),
        "alignment": final_state.get("alignment", {}),
        "security": final_state.get("security", {}),
        "bugfix": final_state.get("bugfix", {}),
    })
    ws.finalize(success, summary)

    result = dict(final_state)
    result.update({"workspace_dir": str(ws.dir), "success": success, "final_summary": summary})
    if on_event:
        on_event({"type": "finish", "success": success, "summary": summary, "workspace": str(ws.dir)})
    return result


def _judge_success(state: dict) -> bool:
    """综合判定任务是否达成。"""
    if state.get("phase") == "aborted":
        return False
    from .state import latest_artifacts
    from .utils import has_blocking_issues

    if not latest_artifacts(state):
        return False
    # 存在越界产物 -> 不算成功（强约束）
    if any(art.get("scope_violations") for art in state.get("artifacts", [])):
        return False
    blocking, _ = has_blocking_issues(state.get("alignment", {}), state.get("security", {}))
    if blocking:
        return False
    alignment = state.get("alignment", {})
    security = state.get("security", {})
    if not alignment or not security:
        return False
    return bool(alignment.get("aligned")) and bool(security.get("safe"))


def _build_summary(state: dict, success: bool, ws) -> str:
    from .state import latest_artifacts

    files = latest_artifacts(state)
    lines = [
        f"{'✅ 任务成功完成' if success else '⚠️ 任务未完全达成'}",
        f"产出文件 {len(files)} 个：{', '.join(f['path'] for f in files) or '无'}",
        f"迭代轮次 {state.get('iteration', 0)}，修复轮次 {state.get('bugfix_rounds', 0)}",
    ]
    plan = state.get("plan", {})
    if plan:
        lines.append(
            f"已批准计划：v{plan.get('revision', 1)}，{len(plan.get('steps', []))} 步，"
            f"{len((plan.get('scope') or {}).get('files', []))} 个白名单文件"
        )
    violations = [v for art in state.get("artifacts", []) for v in (art.get("scope_violations") or [])]
    if violations:
        lines.append(f"⚠ 越界文件（超出计划范围）：{', '.join(sorted(set(violations)))}")
    alignment = state.get("alignment", {})
    security = state.get("security", {})
    if alignment:
        lines.append(f"需求对齐：{'通过' if alignment.get('aligned') else '未通过'}（评分 {alignment.get('score', '?')}）")
    if security:
        lines.append(f"安全审计：{'通过' if security.get('safe') else '发现问题'}（发现 {len(security.get('findings', []))} 项）")
    recon = state.get("reconcile") or {}
    if recon:
        lines.append(
            f"跨模块对账：{recon.get('summary', '已执行')}"
            + (f"（修补 {recon.get('patched_files', 0)} 个文件）" if recon.get("patched_files") else "")
        )
    if state.get("abort_reason"):
        lines.append(f"中止原因：{state['abort_reason']}")
    lines.append(f"工作区：{ws.dir}")
    return "\n".join(lines)


# 便捷再导出：计划阶段入口
def run_planning(*args, **kwargs):
    from .review import run_planning_phase

    return run_planning_phase(*args, **kwargs)