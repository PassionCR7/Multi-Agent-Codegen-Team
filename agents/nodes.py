"""四个业务 agent 节点 + 调度节点。

每个节点遵循统一的执行契约：
    1. 读取共享工作区（项目快照 / 上游报告 / 黑板消息）—— 确保不脱离真实项目
    2. 调用 LLM 完成自己的单一职责
    3. 把结论写回工作区（产物 / 报告 / 消息 / 运行日志）
    4. 返回**状态增量**（LangGraph 用 reducer 合并）

这种"读工作区 → 做事 → 写工作区"的模式，让任何 agent 都被同一份客观事实约束，
也是防止多智能体跑偏的结构性保障。
"""

from __future__ import annotations

from typing import Any

from .config import SETTINGS, get_llm
from .plan import (
    check_scope,
    is_approved,
    normalize_plan,
    plan_feedback_items,
    render_scope_brief,
    render_plan,
)
from .interface import analyze_modules, render_signals, render_signatures
from .prompts import (
    ALIGNER, BUGFIXER, CODER, PLANNER, RECONCILER, REFLECT_TEMPLATE, SECURITY, SUBTASK_CODER,
)
from .reader import SafeReader, format_read_results
from .scanner import ProjectScanner
from .state import AgentState, artifacts_digest, latest_artifacts
from .utils import extract_json, has_blocking_issues, timed, truncate
from .workspace import Workspace

# LLM 采取惰性单例：首次调用时初始化，便于测试时替换
_LLM = None


def _llm():
    global _LLM
    if _LLM is None:
        _LLM = get_llm(SETTINGS)
    return _LLM


def set_llm(llm) -> None:
    """测试注入点。"""
    global _LLM
    _LLM = llm


def _snapshot_text(state: AgentState) -> str:
    """从工作区读取真实项目快照并渲染为提示词上下文。

    这是所有 agent 动手前必须经过的一道事实校准：优先读工作区里已落盘的快照，
    没有才现场扫描。保证同一轮内所有 agent 看到的是**同一份**项目事实。
    """
    ws = _ws(state)
    snapshot = ws.read_snapshot()
    if not snapshot:
        snapshot = ProjectScanner(state["project_root"]).scan()
        ws.write_snapshot(snapshot)
    return ProjectScanner.render_for_prompt(snapshot)


def _ws(state: AgentState) -> Workspace:
    """重新打开状态中记录的工作区（不新建目录）。"""
    return Workspace.from_dir(state["workspace_dir"])


# --------------------------------------------------------------------------- #
# 0. PLANNER —— 先规划，等待用户确认
# --------------------------------------------------------------------------- #
def planner_node(state: AgentState) -> dict[str, Any]:
    """生成 Plan-Act 行动路径，写入工作区供用户审核。

    这个节点**只规划、不写码**。它的产出必须经过用户确认（由 runner 的审核
    循环负责），确认后才注入 state["plan"] 并放行到执行阶段。
    """
    ws = _ws(state)
    plan_rounds = state.get("plan_rounds", 0)
    existing = state.get("plan", {}) or {}
    # 上一轮被打回时，把打回意见一并给规划官
    revision_hint = existing.get("review_note") or existing.get("feedback") or ""

    user = (
        f"【需求】\n{state['requirement']}\n\n"
        f"【真实项目快照】\n{_snapshot_text(state)}\n"
    )
    if revision_hint:
        user += f"\n【上一版计划被用户打回的理由】\n{revision_hint}\n请针对这些意见重新规划。\n"

    ws.post_message("planner", f"第 {plan_rounds + 1} 版计划开始规划。")
    with timed() as t:
        raw = _llm().complete(PLANNER, user)
    data = extract_json(raw)

    plan = normalize_plan(data, fallback_requirement=state["requirement"])
    plan["revision"] = plan_rounds + 1
    plan["status"] = "draft"
    plan["source"] = "llm"
    if revision_hint:
        plan["revision_note"] = revision_hint

    # 落盘：既存结构化 JSON，也存人类可读 Markdown
    ws.write_report(f"plan_v{plan['revision']}", plan)
    (ws.reports_dir / f"plan_v{plan['revision']}.md").write_text(
        render_plan(plan), encoding="utf-8"
    )
    # 最新的计划同时写到固定文件名，方便脚本与人类直接找
    ws.write_report("plan_current", plan)
    (ws.reports_dir / "plan_current.md").write_text(render_plan(plan), encoding="utf-8")

    summary = (
        f"计划 v{plan['revision']}：{len(plan['steps'])} 步，"
        f"{len(plan['scope']['files'])} 个文件，{len(plan['open_questions'])} 个待确认问题"
    )
    ws.post_message("planner", summary, kind="plan", plan=plan)
    ws.log_run("planner", ok=bool(plan["steps"]), duration_ms=t.ms, summary=summary)
    ws.record_turn("planner", "planning", state.get("iteration", 0), summary)

    return {
        "plan": plan,
        "plan_rounds": plan_rounds + 1,
        "phase": "planning",
        "route_history": [{"agent": "planner", "revision": plan["revision"], "steps": len(plan["steps"])}],
    }


# --------------------------------------------------------------------------- #
# 1. CODER —— 写代码
# --------------------------------------------------------------------------- #
def _reads_loop(
    *,
    system_prompt: str,
    base_user: str,
    ws: Workspace,
    reader: SafeReader,
    allow_reads: bool,
    tag: str,
    on_trace: list[str],
) -> tuple[dict[str, Any], str, dict[str, int]]:
    """执行"按需阅读循环"，返回 (最终 JSON, 最后一次原始输出, 统计)。

    抽出成独立函数的原因：无论是整包生成还是按模块分段生成，都需要同一套
    "模型请求文件 → 安全读取 → 再问一次"的逻辑。避免两份实现逐渐跑偏。

    关键设计：模型只能**表达**需要哪些文件（need_files），读不读、怎么读
    由这里和 SafeReader 决定 —— 决策权始终在代码手里。
    """
    reader_stats = {"requested": 0, "served": 0, "rounds": 0}
    user = base_user
    raw = ""
    data: dict[str, Any] = {}

    for read_round in range(SETTINGS.max_read_rounds + 1):
        raw = _llm().complete(system_prompt, user)
        data = extract_json(raw)

        needs = data.get("need_files") or []
        if isinstance(needs, str):
            needs = [needs]
        needs = [str(n).strip() for n in needs if str(n).strip()]
        if not needs:
            break

        if not allow_reads:
            on_trace.append(f"[{tag}] 当前为修订模式，忽略读取请求: {', '.join(needs[:4])}")
            break

        reader_stats["requested"] += len(needs)
        if read_round >= SETTINGS.max_read_rounds:
            on_trace.append(f"[{tag}] 已达按需阅读轮次上限（{SETTINGS.max_read_rounds} 轮）")
            ws.post_message("coder", f"[{tag}] 按需阅读达上限，请求被忽略: {', '.join(needs[:6])}", kind="warning")
            reader_stats["rounds"] = read_round
            break

        results = reader.read_many(needs, max_files=SETTINGS.max_files_per_read)
        served = [r for r in results if r.ok]
        denied = [r for r in results if not r.ok]
        reader_stats["served"] += len(served)
        reader_stats["rounds"] = read_round + 1

        summary = f"按需读取 {len(served)}/{len(results)} 个文件: " + ", ".join(r.path for r in results)
        ws.post_message("coder", f"[{tag} 按需阅读 {read_round + 1}] {summary}",
                        kind="read", audit=reader.audit[-len(results):])
        on_trace.append(f"[{tag}] 第 {read_round + 1} 轮读取：成功 {len(served)}，拒绝 {len(denied)}")
        if denied:
            on_trace.append("  拒绝明细: " + "; ".join(f"{r.path}({r.reason})" for r in denied))

        user = (
            base_user
            + "\n\n【你请求查看的既有文件内容 —— 请基于真实接口实现，不要臆造】\n"
            + format_read_results(results)
            + "\n\n现在请基于以上真实接口，产出最终实现（need_files 留空）。"
        )

    return data, raw, reader_stats


def _write_generated(ws: Workspace, files: list[dict], revision: int, errors: list[str]) -> list[dict]:
    """把模型产出的文件写入工作区，返回用于状态累积的记录。"""
    written: list[dict] = []
    for f in files:
        rel = f.get("path") or f.get("filename") or "output.py"
        content = f.get("content", "")
        try:
            ws.write_artifact(rel, content)
            written.append({
                "path": rel,
                "language": f.get("language", "python"),
                "content": content,
                "description": f.get("description", ""),
                "revision": revision,
            })
        except ValueError as exc:
            errors.append(str(exc))
    return written


def _generate_by_module(
    *,
    ws: Workspace,
    state: AgentState,
    plan: dict[str, Any],
    base_user: str,
    reader: SafeReader,
    scope_files: list[str],
    iteration: int,
    trace: list[str],
    errors: list[str],
) -> tuple[list[dict], list[dict]]:
    """按模块分段生成：逐个子任务、顺序生成、接口递增。

    为什么是"顺序"而不是"并行"？
    因为这些模块是**相互调用**的（用户确认的场景）。后写的模块必须看到前一个
    已经落地的**真实签名**，否则只能靠猜——那正是接口对不上的根源。

    所以每个子任务会被注入一段【已完成模块的真实签名】（由 interface.py 从已
    落地的代码里机械提取，不是让模型转述）。这使得：
      * 后写模块自然按前面已定义的接口来调用；
      * "对账"只需处理残余问题（反向依赖、误判），而不是从零发现所有不一致。

    Returns:
        (全部写入的文件记录, 各子任务上下文)
    """
    written: list[dict] = []
    contexts: list[dict] = []

    for idx, target in enumerate(scope_files, 1):
        # ---- 从**磁盘**读取已完成模块：重建共享状态文档 ----
        #
        # 关键修正：以前这里用内存里的 w["content"]，会与磁盘漂移（补丁改过文件后
        # 内存副本已过期）。现在改为 build_state_doc()——它从 artifacts/ 实读文件，
        # 并把真实签名、待办模块、历次对账结论汇总成一份**落盘的**共享状态文档。
        # 子 agent 因此有一份可自行查阅的共同事实来源，而不是只听父节点转述。
        progress = {"done": idx - 1, "total": len(scope_files), "current": target}
        state_doc = ws.build_state_doc(progress=progress)
        done_modules = [
            {"path": a["path"],
             "content": ws.read_artifact(a["path"]),
             "language": "python" if a["path"].endswith(".py") else ""}
            for a in state_doc.get("artifacts", [])
        ]
        sig_block = render_signatures(done_modules) if done_modules else "（你是第一个模块，暂无已完成模块）"

        # 待办模块：以**共享状态文档**为准（它从磁盘实读产物算出），
        # 而不是随手排除当前目标——否则会把"已完成"也说成"尚未实现"，误导模型。
        pending = [p for p in (state_doc.get("pending_modules") or []) if p != target]
        done_count = len(state_doc.get("artifacts") or [])

        sub_user = (
            base_user
            + "\n\n【本次任务的全部模块（你只负责其中一个）】\n"
            + "\n".join(f"  {i}. {f}" for i, f in enumerate(scope_files, 1))
            + f"\n\n【你当前负责的模块】\n  {target}\n"
            + "  你**只**输出这一个文件。其他模块由别的子任务负责，不要写出它们的代码。\n"
            + f"\n\n【已完成模块的真实签名 —— 必须复用，不可改签名、不可重复定义】\n{sig_block}\n"
            + (f"\n【其余尚未实现的模块（这些还没有人写，可先按合理签名调用）】\n"
               + "\n".join(f"  - {o}" for o in pending) if pending else
               "\n【其余模块均已完成，可直接按上面的真实签名调用】")
            + "\n\n【导入路径规范 —— 很重要】\n"
              "这些模块将来会作为同一个包的成员一起被导入。因此：\n"
              "  * 跨模块导入请使用**包内相对导入**或你所在包的**完整路径**，\n"
              "    不要在顶层直接 `from models import ...` 这种裸导入；\n"
              "  * 请先看【真实项目约定】里的 source_roots / 包结构，按其约定书写；\n"
              "  * 若不确定本模块将来处在什么包路径下，把疑问写进 notes。\n"
            + "\n\n【共享状态文档】\n"
              "本任务维护着一份共享状态文档，记录了当前进度、已批准计划、真实项目约定、\n"
              "已完成模块的真实签名、未完成模块、以及历次对账/对齐结论。\n"
              "路径（相对项目根）：\n"
              f"  {ws.state_md_path}    ← 人类/模型可读\n"
              f"  {ws.state_json_path}  ← 机器可读\n"
              "如果你需要更完整的上下文（例如想确认某个已生成模块的完整实现风格），\n"
              "可以把上述路径放进 need_files 请求读取，而不要凭猜测。\n"
            + "\n\n注意：如果本模块需要调用尚未实现的模块，就按你设计的合理签名调用，"
              "并在 used_interfaces 中写清你期望对方提供的函数名与参数——对账阶段会核对。"
        )

        ws.post_message("coder", f"[子任务 {idx}/{len(scope_files)}] 生成模块 {target}")
        ws.post_message("coder",
                        f"共享状态文档已刷新：已完成 {done_count} 个模块，待办 {len(pending)} 个",
                        kind="state_doc", path=str(ws.state_md_path))
        data, _raw, rstats = _reads_loop(
            system_prompt=SUBTASK_CODER, base_user=sub_user, ws=ws, reader=reader,
            allow_reads=True, tag=f"sub{idx}:{target}", on_trace=trace,
        )

        files = data.get("files", []) or []
        if not files:
            # 兜底：模型可能只回了一个文件但没放 files 里
            trace.append(f"[子任务 {idx}] 未产出文件，跳过 {target}")
            ws.post_message("coder", f"[子任务 {idx}] 模块 {target} 未产出内容", kind="warning")
            continue

        got = _write_generated(ws, files, iteration + 1, errors)
        written.extend(got)

        used = data.get("used_interfaces") or []
        contexts.append({
            "target": target, "generated": [g["path"] for g in got],
            "used_interfaces": [str(u) for u in used],
            "reads": rstats,
        })
        if used:
            trace.append(f"[子任务 {idx}] {target} 声明依赖: " + "; ".join(str(u) for u in used[:5]))

    ws.post_message("coder", f"分段生成完成：{len(written)} 个文件。")
    return written, contexts


def _reconcile_modules(
    *,
    ws: Workspace,
    state: AgentState,
    plan: dict[str, Any],
    written: list[dict],
    base_user: str,
    revision: int,
    trace: list[str],
) -> dict[str, Any]:
    """跨模块对账：机器找信号 → LLM 裁决 → 最小修补。

    这是用户要求的"主 agent 保证各子模块输入输出对齐"的落地。分层职责：
      * interface.analyze_modules —— 机械提取事实信号（悬空 import / 重复定义 /
        调用未导入）。可靠、零成本、可复现。
      * RECONCILER（LLM）—— 裁决信号真伪，并发现机器查不出的**语义**不一致
        （类型约定、参数含义）。
      * apply_patch —— 只做 old→new 最小替换，避免"重写整个文件"带来的连带风险。
    """
    if len(written) < 2:
        return {}

    modules = [{"path": w["path"], "content": w["content"], "language": w.get("language", "python")}
               for w in written]

    # ---- 1) 机器找信号 ----
    signals = analyze_modules(modules)
    ws.write_report(f"reconcile_signals_r{revision}", signals)
    ws.post_message(
        "reconciler",
        f"机械对账完成：悬空 import {len(signals['dangling_imports'])}、"
        f"重复定义 {len(signals['duplicate_defs'])}、"
        f"调用未导入 {len(signals['call_without_import'])}",
        kind="reconcile", signals_count=signals["issue_count"],
    )
    trace.append(f"[对账] 机械信号 {signals['issue_count']} 条")

    if signals["issue_count"] == 0:
        # 无信号也交给 LLM 复核一次语义层面（类型/参数含义），但提示可快速通过
        pass

    # ---- 2) LLM 裁决 ----
    code_block = "\n\n".join(
        f"### {m['path']}\n```{m.get('language','')}\n{truncate(m['content'], 12000)}\n```"
        for m in modules
    )
    recon_user = (
        f"【需求】\n{state['requirement']}\n\n"
        f"【机器提取的机械信号】\n{render_signals(signals)}\n\n"
        f"【各模块完整代码】\n{code_block}\n\n"
        f"【共享状态文档】\n各模块的真实签名与白名单已汇总在：{ws.state_md_path}\n"
        "请裁决：哪些是真问题、哪些是误报；并补充机器查不出的语义不一致。"
    )

    with timed() as rt:
        raw = _llm().complete(RECONCILER, recon_user)
    report = extract_json(raw)

    findings = report.get("findings", []) or []
    real = [f for f in findings if not f.get("is_false_positive")]
    aligned = bool(report.get("aligned", not real))

    summary = (f"对账={'通过' if aligned else '需修补'} 真实问题={len(real)} "
               f"误报={len(report.get('false_positives', []) or [])} "
               f"一致性={report.get('consistency', '?')}")
    ws.write_report(f"reconcile_r{revision}", report)
    ws.post_message("reconciler", summary, kind="reconcile", aligned=aligned, findings=len(real))
    ws.log_run("reconciler", ok=True, duration_ms=rt.ms, summary=summary)
    trace.append(f"[对账] LLM 裁决: {summary}")

    # ---- 3) 最小修补 ----
    patched_total = 0
    skipped: list[str] = []
    for f in real:
        sev = str(f.get("severity", "")).lower()
        if sev not in ("critical", "major"):
            skipped.append(f"{f.get('id', '?')}(severity={sev}，留给 ALIGNER 复核)")
            continue
        path = f.get("path") or f.get("file")
        patch = f.get("patch") or f.get("patches")
        if not isinstance(patch, list) or not patch:
            skipped.append(f"{f.get('id', '?')}(无结构化 patch，需人工/上层处理)")
            continue
        if not path:
            # 有 patch 却没说改哪个文件 —— 显式记录，避免"静默不修补"
            skipped.append(f"{f.get('id', '?')}(有 patch 但缺少 path 字段)")
            ws.post_message("reconciler",
                            f"⚠ 对账项 {f.get('id', '?')} 提供了 patch 但未指定 path，无法自动修补",
                            kind="warning")
            continue
        ok, msg = ws.apply_patch(path, patch)
        trace.append(f"[对账-修补] {path}: {msg}")
        if ok:
            patched_total += 1
        else:
            skipped.append(f"{f.get('id', '?')}({msg})")

    if real:
        ws.post_message(
            "reconciler",
            f"对账结论：{len(real)} 处需修补，已对 {patched_total} 个文件施加最小补丁。"
            + (f" 未自动修补: {'; '.join(skipped[:3])}" if skipped else "")
            + ("（若仍有残留，ALIGNER 会在下一轮复核时标记）" if not aligned else ""),
            kind="reconcile",
        )

    return {
        "aligned": aligned,
        "consistency": report.get("consistency"),
        "signals": {k: signals[k] for k in ("dangling_imports", "duplicate_defs", "call_without_import")},
        "findings": findings,
        "false_positives": report.get("false_positives", []) or [],
        "patched_files": patched_total,
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
# 1. CODER —— 写代码（多模块任务会按模块分段生成并对账）
# --------------------------------------------------------------------------- #
def coder_node(state: AgentState) -> dict[str, Any]:
    ws = _ws(state)
    iteration = state.get("iteration", 0)
    feedback = state.get("feedback", [])
    plan = state.get("plan", {}) or {}
    is_revision = bool(state.get("artifacts"))   # 已有产物 -> 本轮是修订，不是初版

    feedback_block = "\n".join(f"- {f}" for f in feedback) if feedback else "（首轮，无反馈）"

    plan_block = ""
    if is_approved(plan):
        plan_block = (
            "\n\n【已批准的行动计划 —— 强约束，必须遵守】\n"
            f"{truncate(render_plan(plan), 4000)}\n\n"
            f"【允许改动的文件白名单】\n{render_scope_brief(plan)}\n"
            "注意：产出文件的路径必须落在上述白名单内，否则视为越界。"
        )

    base_user = REFLECT_TEMPLATE.format(
        requirement=state["requirement"],
        snapshot=_snapshot_text(state),
        feedback=feedback_block,
    ) + plan_block
    if is_revision:
        base_user += f"\n\n【当前已有代码（需在其基础上修订）】\n{truncate(artifacts_digest(state), 6000)}"

    reader = SafeReader(
        state["project_root"],
        max_file_bytes=SETTINGS.max_file_bytes,
        max_file_lines=SETTINGS.max_file_lines,
    )
    trace: list[str] = []
    all_written: list[dict] = []
    errors: list[str] = list(state.get("errors", []))
    requested_total = served_total = 0

    # 决定是否按模块分段：修订模式不拆（只改指定问题）；初版且文件数达到阈值才拆
    scope_files = ((plan.get("scope") or {}).get("files") or []) if is_approved(plan) else []
    use_subtasks = (
        not is_revision
        and len(scope_files) >= SETTINGS.subtask_threshold
        and len(scope_files) <= SETTINGS.subtask_max_modules
    )

    # 单模块任务也维持一份共享状态文档，便于人类随时查看当前进度与已改代码
    if not use_subtasks:
        state_doc = ws.build_state_doc(progress={"done": 0, "total": len(scope_files) or 1,
                                                 "current": (scope_files[0] if scope_files else "")})
        base_user += (
            "\n\n【共享状态文档】\n"
            "本任务在维护一份共享状态文档，记录当前进度、已批准计划、真实项目约定、"
            "已完成模块的真实签名与历次校验结论。路径（相对项目根）：\n"
            f"  {ws.state_md_path}    ← 人类/模型可读\n"
            f"  {ws.state_json_path}  ← 机器可读\n"
            "如需更完整的上下文，可把 state.md 放进 need_files 请求读取。\n"
            + (f"\n【已完成模块的真实签名】\n{render_signatures([{'path': p, 'content': ws.read_artifact(p), 'language': 'python'} for p in ws.list_artifacts()])}"
               if ws.list_artifacts() else "")
        )

    with timed() as t:
        if use_subtasks:
            ws.post_message("coder", f"第 {iteration + 1} 轮：多模块任务，按 {len(scope_files)} 个模块分段生成。")
            all_written, subtask_context = _generate_by_module(
                ws=ws, state=state, plan=plan, base_user=base_user, reader=reader,
                scope_files=scope_files, iteration=iteration, trace=trace, errors=errors,
            )
            requested_total = sum(c.get("reads", {}).get("requested", 0) for c in subtask_context)
            served_total = sum(c.get("reads", {}).get("served", 0) for c in subtask_context)

            # ---- 跨模块对账（机器找信号 → LLM 裁决 → 最小修补）----
            recon_report = _reconcile_modules(
                ws=ws, state=state, plan=plan, written=all_written, base_user=base_user,
                revision=iteration + 1, trace=trace,
            )
            if recon_report:
                # 修补会改动文件内容，需刷新 written 与**共享状态文档**
                # 注意：这里从磁盘重读（而不是沿用内存副本），保证状态文档与磁盘一致
                for w in all_written:
                    fresh = ws.read_artifact(w["path"])
                    if fresh:
                        w["content"] = fresh
                ws.build_state_doc(progress={"done": len(all_written), "total": len(scope_files),
                                             "current": "", "post_reconcile": True})
                ws.post_message("coder", "对账后已刷新共享状态文档（含修补结果）", kind="state_doc")
        else:
            ws.post_message("coder", f"第 {iteration + 1} 轮开始编码。" + (f" 依据 {len(feedback)} 条反馈。" if feedback else ""))
            data, raw, rstats = _reads_loop(
                system_prompt=CODER, base_user=base_user, ws=ws, reader=reader,
                allow_reads=not is_revision, tag="coder", on_trace=trace,
            )
            requested_total += rstats["requested"]
            served_total += rstats["served"]
            all_written = _write_generated(ws, data.get("files", []) or [], iteration + 1, errors)
            recon_report = {}
            _last_notes = data.get("notes", "") or data.get("_raw", "")
            # 为下面的统一处理准备 notes
            data_notes = _last_notes
        if not use_subtasks:
            notes = data_notes
        else:
            notes = f"按模块分段生成 {len(all_written)} 个文件，并完成跨模块对账。"

    # --- 强约束执法：检测产物是否越出计划范围 ---
    _, violations = check_scope(plan, [w["path"] for w in all_written])
    if violations:
        msg = f"[计划-越界] 以下文件不在已批准计划范围内: {', '.join(violations)}"
        ws.post_message("coder", msg, kind="warning")
        trace.append(msg)

    if not all_written:
        reason = "模型输出格式异常"
        ws.post_message("coder", f"未能解析出文件（{reason}），原始输出摘要: {truncate(raw if not use_subtasks else '', 300)}", kind="warning")
        msgs = [f"coder 未产出可解析的文件（{reason}）"]
    else:
        msgs = [f"已生成/修订 {len(all_written)} 个文件: {', '.join(w['path'] for w in all_written)}"]
        if violations:
            msgs.append(f"⚠ 越界文件 {len(violations)} 个")
    if requested_total:
        msgs.append(f"按需阅读: 请求 {requested_total}，成功 {served_total}")
    if use_subtasks:
        msgs.append(f"模块拆分: {len(scope_files)} 个模块分段生成 + 对账")

    ws.log_run("coder", ok=bool(all_written), duration_ms=t.ms, summary="; ".join(msgs))
    ws.record_turn("coder", "coding", iteration)

    delta: dict[str, Any] = {
        "artifacts": [{
            "iteration": iteration + 1, "files": all_written, "notes": notes,
            "scope_violations": violations,
            "reads": reader.audit,
            "subtasks": use_subtasks,
        }],
        "coder_notes": notes,
        "phase": "coding",
        "code_attempts": state.get("code_attempts", 0) + 1,
        "pending_scope_violations": violations,
        "read_audit": reader.audit,
        "reconcile": recon_report if use_subtasks else {},
        "errors": errors + ([] if all_written else ["coder 未产出文件"]),
    }
    if not all_written:
        delta["feedback"] = ["[coder] 本轮未产出任何文件，请直接输出 files（不要再请求读取）。"]
    if trace:
        delta["feedback"] = trace + delta.get("feedback", [])
    if violations:
        delta["feedback"] = delta.get("feedback", []) + [
            f"[计划-越界] 请移除或回退这些不在计划内的文件: {', '.join(violations)}"
        ]
    return delta


# --------------------------------------------------------------------------- #
# 2. ALIGNER —— 对齐需求
# --------------------------------------------------------------------------- #
def aligner_node(state: AgentState) -> dict[str, Any]:
    ws = _ws(state)
    iteration = state.get("iteration", 0)
    plan = state.get("plan", {}) or {}

    user = (
        f"【需求】\n{state['requirement']}\n\n"
        f"【验收标准】\n" + "\n".join(f"- {a}" for a in state.get("acceptance", [])) + "\n\n"
        f"【真实项目快照】\n{_snapshot_text(state)}\n\n"
        f"【待审查的代码实现】\n{truncate(artifacts_digest(state), 8000)}"
    )
    # 双重校验：把已批准计划也作为基准交给对齐官
    if is_approved(plan):
        user += (
            "\n\n【已批准的行动计划（第二基准，必须一并核对）】\n"
            f"{truncate(render_plan(plan), 3500)}\n"
            "\n请额外核对：实现是否遵守了计划的白名单范围为与步骤划分；"
            "任何超出计划范围的改动都要记为 issue（severity 至少 major）。"
        )

    ws.post_message("aligner", "开始逐条比对需求与实现" + ("（含已批准计划的双重校验）" if is_approved(plan) else "") + "。")
    with timed() as t:
        raw = _llm().complete(ALIGNER, user)
    report = extract_json(raw)

    aligned = bool(report.get("aligned"))
    missing = report.get("missing", []) or []

    # 程序化补充越界检测：不完全依赖 LLM 判断
    scope_violations = state.get("pending_scope_violations") or []
    if scope_violations:
        aligned = False
        report.setdefault("issues", [])
        for v in scope_violations:
            report["issues"].append({
                "severity": "major",
                "requirement": "遵守已批准计划的文件范围",
                "problem": f"产物 {v} 不在计划白名单内",
                "suggestion": "移除该文件，或回到计划阶段将其实纳入范围后重新批准",
            })
        report["aligned"] = False

    summary = f"对齐={'通过' if aligned else '未通过'} 缺失={len(missing)} 评分={report.get('score', '?')}"

    ws.write_report(f"alignment_iter{iteration + 1}", report)
    ws.post_message("aligner", summary, kind="report", aligned=aligned, missing=len(missing))
    ws.log_run("aligner", ok=True, duration_ms=t.ms, summary=summary)
    ws.record_turn("aligner", "aligning", iteration, summary)

    feedback_lines = [
        f"[对齐-{i.get('severity', 'minor')}] {i.get('problem')} → 建议: {i.get('suggestion')}"
        for i in (report.get("issues", []) or [])
    ]
    feedback_lines += [
        f"[对齐-缺失-{m.get('severity', 'major')}] {m.get('requirement')}：{m.get('why', '')}"
        for m in missing
    ]

    return {
        "alignment": report,
        "feedback": feedback_lines,
        "route_history": [{"agent": "aligner", "aligned": aligned, "iteration": iteration}],
        "phase": "aligning",
    }


# --------------------------------------------------------------------------- #
# 3. SECURITY —— 安全审计
# --------------------------------------------------------------------------- #
def security_node(state: AgentState) -> dict[str, Any]:
    ws = _ws(state)
    iteration = state.get("iteration", 0)

    user = (
        f"【需求背景】\n{state['requirement']}\n\n"
        f"【真实项目快照】\n{_snapshot_text(state)}\n\n"
        f"【待审计代码】\n{truncate(artifacts_digest(state), 8000)}"
    )

    ws.post_message("security", "开始安全审计（注入/反序列化/凭据/数据处理/依赖/错误处理）。")
    with timed() as t:
        raw = _llm().complete(SECURITY, user)
    report = extract_json(raw)

    findings = report.get("findings", []) or []
    safe = bool(report.get("safe"))
    summary = f"安全={'通过' if safe else '发现问题'} 发现项={len(findings)}"

    ws.write_report(f"security_iter{iteration + 1}", report)
    ws.post_message("security", summary, kind="report", safe=safe, findings=len(findings))
    ws.log_run("security", ok=True, duration_ms=t.ms, summary=summary)
    ws.record_turn("security", "security", iteration, summary)

    feedback_lines = [
        f"[安全-{f.get('severity', 'medium')}] {f.get('description')} @ {f.get('location', '?')} → {f.get('recommendation', '')}"
        for f in findings
        if str(f.get("severity", "")).lower() in {"critical", "high", "medium"}
    ]

    return {
        "security": report,
        "feedback": feedback_lines,
        "route_history": [{"agent": "security", "safe": safe, "iteration": iteration}],
        "phase": "security",
    }


# --------------------------------------------------------------------------- #
# 4. BUGFIXER —— 修 bug
# --------------------------------------------------------------------------- #
def bugfixer_node(state: AgentState) -> dict[str, Any]:
    ws = _ws(state)
    iteration = state.get("iteration", 0)
    bugfix_rounds = state.get("bugfix_rounds", 0)

    blocking, reasons = has_blocking_issues(state.get("alignment", {}), state.get("security", {}))

    user = (
        f"【需求】\n{state['requirement']}\n\n"
        f"【真实项目快照】\n{_snapshot_text(state)}\n\n"
        f"【当前代码】\n{truncate(artifacts_digest(state), 8000)}\n\n"
        f"【对齐报告】\n{truncate(str(state.get('alignment', {})), 3000)}\n\n"
        f"【安全报告】\n{truncate(str(state.get('security', {})), 3000)}\n\n"
        f"【需要修复的阻断性问题】\n" + ("\n".join(f"- {r}" for r in reasons) or "（无）")
    )

    ws.post_message("bugfixer", f"开始第 {bugfix_rounds + 1} 轮修复，待修问题 {len(reasons)} 项。")
    with timed() as t:
        raw = _llm().complete(BUGFIXER, user)
    report = extract_json(raw)

    patches = report.get("patches", []) or []
    written = []
    for p in patches:
        rel = p.get("path", "")
        if not rel:
            continue
        try:
            ws.write_artifact(rel, p.get("content", ""))
            written.append({
                "path": rel,
                "language": p.get("language", "python"),
                "content": p.get("content", ""),
                "description": f"修复 {p.get('fixes', '')}: {p.get('explanation', '')}",
                "revision": iteration + 1,
                "is_patch": True,
            })
        except ValueError as exc:
            state.setdefault("errors", []).append(str(exc))

    resolved = bool(report.get("resolved")) or bool(written)
    summary = f"修复轮次={bugfix_rounds + 1} 补丁={len(written)}"
    ws.write_report(f"bugfix_iter{iteration + 1}", report)
    ws.post_message("bugfixer", summary, kind="report", patches=len(written), resolved=resolved)
    ws.log_run("bugfixer", ok=True, duration_ms=t.ms, summary=summary)
    ws.record_turn("bugfixer", "bugfixing", iteration, summary)

    delta: dict[str, Any] = {
        "bugfix": report,
        "artifacts": [{"iteration": iteration + 1, "files": written, "notes": report.get("notes", "")}],
        "bugfix_rounds": bugfix_rounds + 1,
        "route_history": [{"agent": "bugfixer", "patches": len(written), "iteration": iteration}],
        "phase": "bugfixing",
    }
    if report.get("notes"):
        delta["feedback"] = [f"[修复说明] {report['notes']}"]
    return delta


# --------------------------------------------------------------------------- #
# 5. REPAIR —— 修复后复检（回到对齐/安全）
# --------------------------------------------------------------------------- #
def repair_node(state: AgentState) -> dict[str, Any]:
    """修复后清空对齐/安全结论，使流水线重新走一遍检查。"""
    ws = _ws(state)
    iteration = state.get("iteration", 0) + 1
    ws.post_message("system", f"修复完成，进入第 {iteration + 1} 轮复检。", kind="lifecycle")
    ws.record_turn("system", "repairing", iteration, "修复后复检")
    ws.update_status(iteration=iteration, phase="repairing")
    return {
        "iteration": iteration,
        "alignment": {},
        "security": {},
        "bugfix": {},
        "phase": "repairing",
        "route_history": [{"agent": "system", "action": "recheck", "iteration": iteration}],
    }


ALL_NODES = {
    "planner": planner_node,
    "coder": coder_node,
    "aligner": aligner_node,
    "security": security_node,
    "bugfixer": bugfixer_node,
    "repair": repair_node,
}