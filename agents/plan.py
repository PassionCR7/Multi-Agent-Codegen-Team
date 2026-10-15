"""Plan（行动计划）数据模型与范围校验。

为什么把 Plan 做成独立的数据结构而不是一段文本？

因为"强约束"需要一个**机器可判定的边界**："coder 只能按 Plan 执行"这句话，
只有把 Plan 里的文件清单变成程序可比的集合，才谈得上"检测越界"。
一段自由文本是没法自动校验的。

核心字段：
    * understanding   —— 需求理解（用模型的复述暴露误解）
    * assumptions     —— 假设（后续代码的依据，也是事故重灾区）
    * open_questions  —— 待用户确认的问题（审核时最该看的部分）
    * steps           —— Plan-Act：逐步做什么、改哪个文件、如何验证
    * scope           —— 允许新增/修改的文件清单（强约束的判定依据）
    * risks           —— 风险
    * status          —— draft / approved / rejected / user_written
"""

from __future__ import annotations

import re
from typing import Any

# 状态常量
STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_USER_WRITTEN = "user_written"

_SEV_ORDER = {"high": 3, "medium": 2, "low": 1}


# --------------------------------------------------------------------------- #
# 构造与归一化
# --------------------------------------------------------------------------- #
def normalize_plan(raw: dict[str, Any], *, fallback_requirement: str = "") -> dict[str, Any]:
    """把 LLM 输出的 Plan 归一化成稳定结构。

    模型输出常常缺字段或类型不稳，这里做一次"防御性整形"，
    保证下游（渲染、校验、写盘）永远拿到形状一致的对象。
    """
    raw = raw or {}

    def _as_list(value: Any) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [value]

    steps: list[dict] = []
    for i, s in enumerate(_as_list(raw.get("steps")), 1):
        if isinstance(s, str):
            s = {"action": s}
        if not isinstance(s, dict):
            continue
        touched = s.get("files") or s.get("touched_files") or s.get("touches") or []
        steps.append({
            "id": s.get("id") or f"S{i}",
            "goal": s.get("goal") or s.get("title") or s.get("action") or f"步骤 {i}",
            "detail": s.get("detail") or s.get("description") or s.get("action") or "",
            "files": [str(f) for f in _as_list(touched)],
            "verify": s.get("verify") or s.get("validation") or "",
        })

    # scope：允许显式给出，否则从 steps 的 files 聚合推导
    scope = raw.get("scope") or {}
    if isinstance(scope, list):
        scope = {"files": scope}
    if not isinstance(scope, dict):
        scope = {}
    scope_files = [str(f) for f in _as_list(scope.get("files") or scope.get("allowed_files"))]
    if not scope_files:
        scope_files = sorted({f for st in steps for f in st["files"]})

    risks = []
    for r in _as_list(raw.get("risks")):
        if isinstance(r, str):
            r = {"description": r, "severity": "medium"}
        if isinstance(r, dict):
            risks.append({
                "description": r.get("description") or r.get("risk") or str(r),
                "severity": r.get("severity", "medium"),
                "mitigation": r.get("mitigation", ""),
            })

    return {
        "requirement": raw.get("requirement") or fallback_requirement,
        "understanding": raw.get("understanding") or raw.get("summary") or "",
        "assumptions": [a if isinstance(a, str) else str(a) for a in _as_list(raw.get("assumptions"))],
        "open_questions": [
            q if isinstance(q, str) else (q.get("question") if isinstance(q, dict) else str(q))
            for q in _as_list(raw.get("open_questions") or raw.get("questions"))
        ],
        "steps": steps,
        "scope": {"files": scope_files, "notes": scope.get("notes", "")},
        "risks": risks,
        "test_plan": raw.get("test_plan") or raw.get("testing") or "",
        "out_of_scope": [o if isinstance(o, str) else str(o) for o in _as_list(raw.get("out_of_scope"))],
        "status": raw.get("status", STATUS_DRAFT),
        "source": raw.get("source", "llm"),
        "revision": int(raw.get("revision", 1) or 1),
    }


def from_user_text(text: str, *, requirement: str, scope_files: list[str] | None = None) -> dict[str, Any]:
    """把"用户自己写的方案"转成标准 Plan 结构。

    用户写的通常是自由文本，这里尽力解析出步骤与涉及文件；
    解析不到的字段留空并标注来源为 user，绝不臆造用户没说过的内容。
    """
    text = (text or "").strip()
    steps: list[dict] = []
    # 兼容 "1. xxx" / "- xxx" / "步骤1: xxx" 等常见写法
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(?:\d+[.、)]|[-*•]|步骤\s*\d+[:：]?)\s*(.+)$", line)
        if m and m.group(1).strip():
            steps.append({
                "id": f"S{len(steps) + 1}",
                "goal": m.group(1).strip(),
                "detail": "",
                "files": [],
                "verify": "",
            })
    if not steps and text:
        steps = [{"id": "S1", "goal": text.splitlines()[0][:200], "detail": text, "files": [], "verify": ""}]

    # 从全文里抓取看起来像文件路径的片段
    detected = re.findall(r"[\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cs|rb|php|sql|vue|css|html|md|json|yaml|yml|toml)", text)
    files = scope_files if scope_files is not None else sorted(set(detected))

    return normalize_plan({
        "requirement": requirement,
        "understanding": f"（用户自拟方案）\n{text}",
        "steps": steps,
        "scope": {"files": files, "notes": "范围来自用户自拟方案解析"},
        "status": STATUS_USER_WRITTEN,
        "source": "user",
    }, fallback_requirement=requirement)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_plan(plan: dict[str, Any]) -> str:
    """把 Plan 渲染为人类可读的审核材料（CLI / 文档 / 日志共用）。"""
    if not plan:
        return "（无计划）"

    L: list[str] = []
    status_label = {
        STATUS_DRAFT: "草稿（待审核）",
        STATUS_APPROVED: "已批准",
        STATUS_REJECTED: "已打回",
        STATUS_USER_WRITTEN: "用户自拟",
    }.get(plan.get("status", ""), plan.get("status", ""))

    L.append(f"行动计划（第 {plan.get('revision', 1)} 版 · {status_label}）")
    L.append("=" * 66)

    if plan.get("understanding"):
        L.append("【需求理解】")
        L.append(f"  {plan['understanding']}")
        L.append("")

    if plan.get("assumptions"):
        L.append("【关键假设】（若以下假设有误，请打回）")
        for a in plan["assumptions"]:
            L.append(f"  ⚠ {a}")
        L.append("")

    if plan.get("open_questions"):
        L.append("【待你确认的问题】")
        for i, q in enumerate(plan["open_questions"], 1):
            L.append(f"  {i}. {q}")
        L.append("")

    if plan.get("steps"):
        L.append("【Plan-Act 执行步骤】")
        for st in plan["steps"]:
            files = f"  涉及: {', '.join(st['files'])}" if st.get("files") else ""
            L.append(f"  {st.get('id', '')} {st.get('goal', '')}")
            if st.get("detail"):
                L.append(f"      {st['detail']}")
            if files:
                L.append(f"    {files}")
            if st.get("verify"):
                L.append(f"      验证: {st['verify']}")
        L.append("")

    scope_files = (plan.get("scope") or {}).get("files") or []
    if scope_files:
        L.append("【改动范围】（强约束：coder 只能动这些文件）")
        for f in scope_files:
            L.append(f"  · {f}")
        L.append("")

    if plan.get("out_of_scope"):
        L.append("【明确不做】")
        for o in plan["out_of_scope"]:
            L.append(f"  · {o}")
        L.append("")

    if plan.get("risks"):
        L.append("【风险】")
        for r in plan["risks"]:
            sev = str(r.get("severity", "medium")).upper()
            line = f"  [{sev}] {r.get('description', '')}"
            if r.get("mitigation"):
                line += f"\n      缓解: {r['mitigation']}"
            L.append(line)
        L.append("")

    if plan.get("test_plan"):
        L.append("【验证方式】")
        L.append(f"  {plan['test_plan']}")
        L.append("")

    return "\n".join(L).rstrip()


def render_scope_brief(plan: dict[str, Any]) -> str:
    """给 coder 的简短范围清单（放进提示词）。"""
    files = (plan.get("scope") or {}).get("files") or []
    if not files:
        return "（计划未限定文件，请在实现说明中列明新增文件）"
    return "\n".join(f"  - {f}" for f in files)


# --------------------------------------------------------------------------- #
# 强约束：范围校验
# --------------------------------------------------------------------------- #
def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip().lstrip("./").lower()


def check_scope(plan: dict[str, Any], produced_files: list[str]) -> tuple[bool, list[str]]:
    """校验产物是否越出 Plan 范围（强约束的执法点）。

    Returns:
        (是否全部在范围内, 越界文件列表)
    """
    scope = {_norm_path(f) for f in ((plan.get("scope") or {}).get("files") or [])}
    if not scope:
        # 计划未限定范围时不做越界判定，避免误伤
        return True, []

    violations: list[str] = []
    for f in produced_files:
        nf = _norm_path(f)
        if nf in scope:
            continue
        # 允许"计划指定目录下的新增文件"（计划写 dir/ 时，dir/xxx 视为在范围内）
        if any(nf.startswith(s.rstrip("/") + "/") for s in scope if s.endswith("/") or "/" in s):
            continue
        violations.append(f)
    return (not violations), violations


def plan_feedback_items(plan: dict[str, Any]) -> list[str]:
    """把已批准计划转换成给 coder 的硬性要求条目。"""
    items: list[str] = []
    files = (plan.get("scope") or {}).get("files") or []
    if files:
        items.append(f"[计划-范围] 只允许新增/修改以下文件：{', '.join(files)}")
    for st in plan.get("steps", []):
        line = f"[计划-{st.get('id', '')}] {st.get('goal', '')}"
        if st.get("verify"):
            line += f"（完成标准：{st['verify']}）"
        items.append(line)
    if plan.get("out_of_scope"):
        items.append(f"[计划-不做] {', '.join(plan['out_of_scope'])}")
    return items


def is_approved(plan: dict[str, Any] | None) -> bool:
    return bool(plan) and plan.get("status") in {STATUS_APPROVED, STATUS_USER_WRITTEN}


def summarize(plan: dict[str, Any] | None) -> dict[str, Any]:
    """给状态摘要用的精简信息。"""
    if not plan:
        return {}
    return {
        "revision": plan.get("revision", 1),
        "status": plan.get("status"),
        "steps": len(plan.get("steps", [])),
        "files": len((plan.get("scope") or {}).get("files", [])),
        "questions": len(plan.get("open_questions", [])),
    }