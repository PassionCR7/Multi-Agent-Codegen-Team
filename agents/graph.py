"""LangGraph 图组装。

拓扑结构::

        START
          |
     [supervisor] <─────────────────┐
          |  (条件边)               │
    ┌─────┼──────┬─────────┐        │
    v     v      v         v        │
 [coder][aligner][security][bugfixer]│
    │     │      │         │        │
    └─────┴──────┴─────────┘        │
                |                   │
                └───(下一轮调度)─────┘
          |
        (done / abort)
          v
         END

要点：
    * supervisor 是唯一的"决策中枢"，其余节点只做事、不决定走向。
    * 每个业务节点执行完都回到 supervisor，由它决定下一跳 —— 星型拓扑，
      避免节点之间隐式耦合导致的行为不一致。
    * 循环上限由 supervisor 兜底熔断，保证图必然终止。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import aligner_node, bugfixer_node, coder_node, planner_node, repair_node, security_node
from .state import AgentState
from .supervisor import apply as supervisor_apply
from .supervisor import route_from_supervisor


def supervisor_node(state: AgentState) -> dict:
    """把 supervisor 的决策包装成 LangGraph 节点。"""
    return supervisor_apply(state)


def build_graph(*, checkpointer=None):
    """构建并编译多智能体图。

    Args:
        checkpointer: 可选，LangGraph 的持久化后端（如 SqliteSaver），
                      传入后可支持断点续跑与时间旅行。
    """
    graph = StateGraph(AgentState)

    # --- 注册节点 ---
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("planner", planner_node)
    graph.add_node("coder", coder_node)
    graph.add_node("aligner", aligner_node)
    graph.add_node("security", security_node)
    graph.add_node("bugfixer", bugfixer_node)
    graph.add_node("repair", repair_node)

    # --- 入口 ---
    graph.add_edge(START, "supervisor")

    # --- 条件边：supervisor 决定下一跳 ---
    graph.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "planner": "planner",
            "coder": "coder",
            "aligner": "aligner",
            "security": "security",
            "bugfixer": "bugfixer",
            "repair": "repair",
            "done": END,
            "abort": END,
        },
    )

    # --- 业务节点执行完统一回到 supervisor（星型回流）---
    for node in ("planner", "coder", "aligner", "security", "bugfixer", "repair"):
        graph.add_edge(node, "supervisor")

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def render_mermaid() -> str:
    """导出图结构（ASCII/mermaid），便于文档与调试。"""
    return build_graph().get_graph().draw_mermaid()


if __name__ == "__main__":  # pragma: no cover
    print(render_mermaid())