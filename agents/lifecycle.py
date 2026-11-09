"""Ticket 生命周期：状态牌、结构化报告与待办（Attention）。

借用 DeepSeek super-agent 的 TicketStage / Report / Attention 语义，
但按本项目的一次性流水线裁剪：

    plan_review -> queued -> working
                              ├─ awaiting_answer  （有 attention）
                              ├─ awaiting_review  （产出 ready，等人工验收）
                              ├─ blocked / failed
                              └─ done / aborted
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

TicketStage = Literal[
    "plan_review",
    "queued",
    "working",
    "awaiting_answer",
    "awaiting_review",
    "blocked",
    "failed",
    "done",
    "aborted",
]

ReportState = Literal[
    "progress",
    "waiting",
    "ready",
    "blocked",
    "failed",
    "dropped",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_report(
    state: dict[str, Any],
    report_state: ReportState,
    summary: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成一条结构化报告。"""
    return {
        "state": report_state,
        "summary": str(summary or ""),
        "at": _now(),
        "ticket_id": state.get("ticket_id", ""),
        "assignment_id": (
            (state.get("assignments") or [{}])[-1].get("id", "")
            if state.get("assignments")
            else ""
        ),
        "evidence": evidence or {},
    }


def make_attention(
    question: str,
    *,
    options: list[dict[str, str]] | None = None,
    source: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成一个“一次只问一个具体问题”的待办。"""
    return {
        "id": uuid.uuid4().hex[:12],
        "question": str(question or "").strip(),
        "options": options or [
            {"id": "retry", "label": "重试"},
            {"id": "abort", "label": "终止任务"},
        ],
        "source": source,
        "detail": detail or {},
        "created_at": _now(),
        "answered_at": None,
        "answer": None,
    }


def is_attention_open(attention: dict[str, Any] | None) -> bool:
    return bool(attention) and not attention.get("answered_at")


def derive_stage(state: dict[str, Any]) -> TicketStage:
    """按“第一条命中”的规则计算当前阶段。"""
    from .plan import is_approved

    if is_attention_open(state.get("attention")):
        return "awaiting_answer"

    plan = state.get("plan") or {}
    if not is_approved(plan):
        return "plan_review"

    report = state.get("report") or {}
    report_state = report.get("state")
    if report_state == "ready":
        return "awaiting_review"
    if report_state in ("blocked", "failed"):
        return "blocked" if report_state == "blocked" else "failed"

    if state.get("success"):
        return "done"
    if state.get("phase") == "aborted" or state.get("abort_reason"):
        return "failed"
    if not state.get("assignments"):
        return "queued"
    return "working"


def answer_attention(
    state: dict[str, Any],
    attention_id: str,
    option: str,
    note: str = "",
) -> dict[str, Any]:
    """记录待办回答；返回新的 attention 字典（保留审计痕迹）。"""
    attention = dict(state.get("attention") or {})
    if not attention:
        raise ValueError("当前没有待处理事项")
    if attention.get("answered_at"):
        raise ValueError("该待办已经回答过了")
    if attention_id and attention.get("id") != attention_id:
        raise ValueError("待办 ID 不匹配")
    options = {str(o.get("id")) for o in (attention.get("options") or [])}
    if options and option not in options:
        raise ValueError(f"不支持的选项: {option}")
    attention.update({
        "answered_at": _now(),
        "answer": {"option": option, "note": str(note or "")},
    })
    return attention


def summarize_lifecycle(state: dict[str, Any]) -> dict[str, Any]:
    """给 UI / status.json 用的生命周期摘要。"""
    return {
        "ticket_stage": state.get("ticket_stage") or derive_stage(state),
        "report": state.get("report") or {},
        "attention": state.get("attention") or {},
        "assignments": state.get("assignments") or [],
    }
