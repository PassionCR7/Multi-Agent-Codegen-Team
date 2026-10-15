"""多智能体代码生成团队 —— 命令行入口。

支持"先规划、用户确认、再执行"的完整流程。

用法示例::

    # 交互式：先出计划，由你确认（可打回重规划、也可自己写方案）
    python -m agents.main "实现一个读取CSV并计算每列统计量的工具" --project .
    python -m agents.main "给用户模块增加密码强度校验" --project ./myapp

    # 跳过人工环节（CI / 自动化）：自动批准首版计划
    python -m agents.main "需求" --project . --auto-plan

    # 只出计划、不执行（把计划拿给别人评审）
    python -m agents.main "需求" --project . --plan-only

    # 复用已完成计划批准的任务，只跑执行阶段
    python -m agents.main "需求" --project . --workspace _agent_workspace/<task> --yes

    # 查看图结构 / 某次任务的工作区状态
    python -m agents.main --graph
    python -m agents.main --inspect _agent_workspace/<task>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import SETTINGS
from .plan import render_plan
from .review import ReviewDecision, run_planning_phase
from .runner import NODE_LABELS, run_team


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except ImportError:
        return False


def _print_banner(project_root: Path) -> None:
    print("=" * 72)
    print("  LangGraph 多智能体代码生成团队")
    print("=" * 72)
    print(f"  真实项目: {project_root}")
    print(f"  模型配置: {SETTINGS.describe()}")
    print(f"  循环上限: iteration<{SETTINGS.max_iterations}, bugfix<{SETTINGS.max_bugfix_rounds}, "
          f"plan<{SETTINGS.max_plan_rounds}")
    print("=" * 72)


# --------------------------------------------------------------------------- #
# reviewer 实现
# --------------------------------------------------------------------------- #
class InteractiveReviewer:
    """命令行交互式计划审核。

    三个选项直接对应需求里的三条路径：
        1 = 批准       -> 放行执行
        2 = 打回重规划  -> 带上理由让 planner 重做
        3 = 自拟方案    -> 用户写 plan，框架结合原始需求 + 补充说明转换为标准计划，再确认
    """

    def review(self, plan: dict, round_index: int) -> ReviewDecision:
        print()
        print(render_plan(plan))
        print()
        print("-" * 72)
        print("  请确认这份计划：")
        print("    1) 批准，开始写代码")
        print("    2) 不满意，打回让 PLANNER 重新规划")
        print("    3) 我自己写一份方案（给出补充说明，框架据此重组计划）")
        print("-" * 72)

        while True:
            try:
                choice = input("请输入 1 / 2 / 3: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n（输入中断，默认按‘打回’处理）")
                return ReviewDecision(ReviewDecision.REVISE, note="用户中断了审核")

            if choice == "1":
                return ReviewDecision(ReviewDecision.APPROVE)
            if choice == "2":
                note = input("请说明不满意的原因（会作为重规划依据）: ").strip()
                return ReviewDecision(ReviewDecision.REVISE, note=note or "未说明理由")
            if choice == "3":
                print("请描述你期望的方案（可分行；空行结束）：")
                lines = []
                while True:
                    try:
                        line = input()
                    except EOFError:
                        break
                    if not line.strip():
                        break
                    lines.append(line)
                text = "\n".join(lines).strip()
                if not text:
                    print("未输入内容，请重新选择。")
                    continue
                note = input("补充说明（可选，回车跳过）: ").strip()
                scope_raw = input("限定改动文件（可选，逗号分隔，回车跳过）: ").strip()
                scope_files = [s.strip() for s in scope_raw.split(",") if s.strip()] or None
                return ReviewDecision(
                    ReviewDecision.USER_PLAN, note=note, user_plan_text=text, scope_files=scope_files
                )
            print("无效输入，请输入 1、2 或 3。")


class AutoApproveReviewer:
    """自动批准首版计划（用于 CI / --auto-plan / --yes）。"""

    def __init__(self, print_plan: bool = True) -> None:
        self.print_plan = print_plan

    def review(self, plan: dict, round_index: int) -> ReviewDecision:
        if self.print_plan:
            print(render_plan(plan))
            print()
        if plan.get("open_questions"):
            print(f"  （自动批准：计划含 {len(plan['open_questions'])} 个待确认问题，已一并接受）")
        print("  → 自动批准该计划。")
        return ReviewDecision(ReviewDecision.APPROVE)


def _make_printer():
    """返回执行阶段的事件打印函数（rich 美化，无则回退纯文本）。"""
    if not _rich_available() or not sys.stdout.isatty():
        def plain(event: dict) -> None:
            if event["type"] == "node":
                st = event["state"]
                print(
                    f"  ▶ {event['label']:<22} 轮次={st['iteration']} 产物={st['artifacts']} "
                    f"对齐={st['aligned']} 安全={st['safe']} bugs={st['bugs']}"
                )
            elif event["type"] == "finish":
                print("\n" + "=" * 72)
                print(event["summary"])
                print("=" * 72)

        return plain

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    rows: list[tuple] = []

    def fancy(event: dict) -> None:
        if event["type"] == "node":
            st = event["state"]
            rows.append((
                event["label"], st["iteration"], st["artifacts"],
                "✅" if st["aligned"] else "—",
                "✅" if st["safe"] else "—",
                st["bugs"],
            ))
        elif event["type"] == "finish":
            table = Table(title="执行轨迹", show_lines=False)
            for col in ("Agent", "轮次", "产物", "对齐", "安全", "Bugs"):
                table.add_column(col, justify="center")
            for r in rows:
                table.add_row(*[str(x) for x in r])
            console.print(table)
            style = "green" if event["success"] else "yellow"
            console.print(Panel(event["summary"], title=f"[{style}]最终结果[/{style}]", border_style=style))

    return fancy


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    project_root = Path(args.project).resolve()
    if not project_root.exists():
        print(f"错误：项目目录不存在 -> {project_root}", file=sys.stderr)
        return 2

    _print_banner(project_root)
    ws_root = Path(args.workspace_root).resolve() if args.workspace_root else None

    # ---------------- Phase 0: 规划 + 用户确认 ----------------
    if args.workspace and args.yes:
        # 复用已批准计划，跳过规划
        from .workspace import Workspace

        ws = Workspace.from_dir(args.workspace)
        plan_file = ws.reports_dir / "plan_approved.json"
        if not plan_file.exists():
            plan_file = ws.reports_dir / "plan_current.json"
        if not plan_file.exists():
            print(f"错误：工作区中没有可用计划 -> {ws.reports_dir}", file=sys.stderr)
            return 2
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        print(f"复用已批准计划: {plan_file.name}")
        ws_dir = ws.dir
    else:
        reviewer = (
            AutoApproveReviewer(print_plan=not args.quiet)
            if (args.auto_plan or args.yes)
            else InteractiveReviewer()
        )

        print("\n【阶段一】生成行动计划，等待你确认\n")
        ws, plan, _state = run_planning_phase(
            requirement=args.requirement,
            project_root=project_root,
            workspace_root=ws_root or project_root / SETTINGS.workspace_dirname,
            task_name=args.task_name,
            reviewer=reviewer,
        )
        ws_dir = ws.dir

        if not plan:
            print("\n计划未获批准（重规划次数已达上限），流程结束。", file=sys.stderr)
            print(f"工作区: {ws_dir}")
            return 1

        print(f"\n✅ 计划已批准（v{plan.get('revision')}），保存在: {ws.reports_dir / 'plan_approved.md'}")

        if args.plan_only:
            print("\n--plan-only：已按要求停止，不进入执行阶段。")
            print(f"工作区: {ws_dir}")
            return 0

    # ---------------- Phase 1: 执行 ----------------
    print("\n【阶段二】按已批准计划执行（编码 → 对齐 → 安全 → 修复）\n")
    printer = _make_printer()
    result = run_team(
        requirement=args.requirement,
        project_root=project_root,
        task_name=args.task_name,
        plan=plan,
        workspace_dir=ws_dir,
        on_event=printer,
    )

    if args.json:
        print(json.dumps({
            "success": result.get("success"),
            "workspace": result.get("workspace_dir"),
            "plan_status": (result.get("plan") or {}).get("status"),
            "iteration": result.get("iteration"),
            "bugfix_rounds": result.get("bugfix_rounds"),
            "final_summary": result.get("final_summary"),
        }, ensure_ascii=False, indent=2))

    return 0 if result.get("success") else 1


def cmd_graph(_: argparse.Namespace) -> int:
    from .graph import render_mermaid

    print(render_mermaid())
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    ws_dir = Path(args.inspect).resolve()
    if not ws_dir.exists():
        print(f"错误：工作区不存在 -> {ws_dir}", file=sys.stderr)
        return 2

    print(f"# 工作区: {ws_dir}\n")
    for name, title in [
        ("task.md", "任务书"),
        ("status.json", "实时状态"),
        ("project_snapshot.json", "项目快照"),
    ]:
        path = ws_dir / name
        if path.exists():
            print(f"--- {title} ({name}) ---")
            text = path.read_text(encoding="utf-8")
            print(text[:2000] + ("\n... (截断)" if len(text) > 2000 else ""))
            print()

    approved = ws_dir / "reports" / "plan_approved.md"
    if approved.exists():
        print("--- 已批准计划 (reports/plan_approved.md) ---")
        print(approved.read_text(encoding="utf-8"))
        print()

    msg_path = ws_dir / "messages.jsonl"
    if msg_path.exists():
        print("--- 黑板消息（最近 15 条）---")
        lines = msg_path.read_text(encoding="utf-8").strip().splitlines()[-15:]
        for ln in lines:
            try:
                m = json.loads(ln)
                print(f"  [{m.get('ts')}] {m.get('agent'):<11} {m.get('content')}")
            except json.JSONDecodeError:
                continue
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents",
        description="LangGraph 多智能体代码生成团队（规划 → 用户确认 → 编码 / 对齐 / 安全 / 修复）",
    )
    parser.add_argument("requirement", nargs="?", help="用户需求描述")
    parser.add_argument("--project", default=".", help="真实项目根目录（默认当前目录）")
    parser.add_argument("--task-name", default=None, help="任务名（决定工作区子目录名）")
    parser.add_argument("--workspace-root", default=None, help="工作区上级目录")
    parser.add_argument("--workspace", default=None, help="复用指定工作区目录（配合 --yes 直接执行）")
    parser.add_argument("--yes", action="store_true", help="跳过人工审核，自动批准计划")
    parser.add_argument("--auto-plan", action="store_true", help="自动批准首版计划（适合 CI）")
    parser.add_argument("--plan-only", action="store_true", help="只生成并确认计划，不执行编码")
    parser.add_argument("--quiet", action="store_true", help="自动模式下不打印完整计划")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出最终结果摘要")
    parser.add_argument("--graph", action="store_true", help="打印图结构（mermaid）后退出")
    parser.add_argument("--inspect", metavar="WORKSPACE_DIR", help="查看指定工作区状态后退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.graph:
        return cmd_graph(args)
    if args.inspect:
        return cmd_inspect(args)
    if not args.requirement:
        parser.print_help()
        return 2
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())