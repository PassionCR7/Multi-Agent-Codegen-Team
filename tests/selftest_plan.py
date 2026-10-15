"""计划审核循环的自检 —— 覆盖需求里的三条路径。

运行::

    ./.venv/Scripts/python.exe -m tests.selftest_plan
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import nodes
from agents.plan import check_scope, from_user_text, is_approved, normalize_plan, render_plan
from agents.review import ReviewDecision, run_planning_phase
from agents.workspace import Workspace

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


class ScriptedReviewer:
    """按脚本依次给出审核决策，用于自动验证三条路径。"""

    def __init__(self, decisions: list[ReviewDecision]):
        self.decisions = list(decisions)
        self.seen_plans: list[dict] = []

    def review(self, plan: dict, round_index: int) -> ReviewDecision:
        self.seen_plans.append(json.loads(json.dumps(plan, ensure_ascii=False)))
        if self.decisions:
            return self.decisions.pop(0)
        return ReviewDecision(ReviewDecision.APPROVE)


def _project(tmp: Path) -> Path:
    p = tmp / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    (p / "tests").mkdir()
    (p / "tests" / "test_app.py").write_text("def test(): pass\n", encoding="utf-8")
    (p / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
def test_plan_model() -> None:
    print("\n[1] Plan 数据模型")
    raw = {
        "understanding": "理解",
        "steps": [
            {"goal": "步骤A", "files": ["a.py"], "verify": "可运行"},
            {"goal": "步骤B", "files": ["b.py"]},
        ],
        "risks": [{"severity": "high", "description": "风险X", "mitigation": "缓解Y"}],
    }
    plan = normalize_plan(raw, fallback_requirement="需求Z")
    check(plan["requirement"] == "需求Z", "补齐需求字段")
    check(len(plan["steps"]) == 2, "解析 steps")
    check(plan["steps"][0]["id"] == "S1", "自动编号步骤")
    check(set(plan["scope"]["files"]) == {"a.py", "b.py"}, "scope 从 steps 聚合推导")
    check(len(plan["risks"]) == 1 and plan["risks"][0]["severity"] == "high", "解析 risks")

    # 步骤用字符串简写
    p2 = normalize_plan({"steps": ["只做一件事"]})
    check(p2["steps"][0]["goal"] == "只做一件事", "兼容字符串步骤")


def test_scope_enforcement() -> None:
    print("\n[2] 强约束：范围校验")
    plan = normalize_plan({"scope": {"files": ["src/a.py", "src/b.py"]}})

    ok, v = check_scope(plan, ["src/a.py"])
    check(ok and not v, "白名单内文件通过")

    ok, v = check_scope(plan, ["src/a.py", "src/evil.py"])
    check(not ok and v == ["src/evil.py"], "越界文件被识别")

    # 反斜杠 / ./ 前缀应被归一化
    ok, v = check_scope(plan, ["./src\\a.py"])
    check(ok, "路径归一化（./ 与反斜杠）")

    # 计划限定目录时，其下新增文件视为在范围内
    dirplan = normalize_plan({"scope": {"files": ["src/"]}})
    ok, v = check_scope(dirplan, ["src/new_module.py"])
    check(ok, "计划指定目录时允许其下新增文件")

    # 未限定范围时不误伤
    empty = normalize_plan({})
    ok, v = check_scope(empty, ["anything.py"])
    check(ok and not v, "无范围时不做越界判定")


def test_user_written_plan() -> None:
    print("\n[3] 用户自拟方案转结构化 Plan")
    text = """
    1. 先写核心函数 src/validator.py
    2. 再补测试 tests/test_validator.py
    - 最后更新 README.md
    """
    plan = from_user_text(text, requirement="校验手机号")
    check(plan["source"] == "user", "标记来源为 user")
    check(plan["status"] == "user_written", "状态为 user_written")
    check(len(plan["steps"]) == 3, f"解析出 3 个步骤（实际 {len(plan['steps'])}）")
    check("src/validator.py" in plan["scope"]["files"], "自动识别文件路径")


def test_revision_loop() -> None:
    print("\n[4] 路径一：打回 → 重规划 → 批准")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = _project(tmp)
        calls = {"n": 0}

        class CountingReviewer:
            def review(self, plan, round_index):
                calls["n"] += 1
                if calls["n"] == 1:
                    return ReviewDecision(ReviewDecision.REVISE, note="范围太大，请缩小到单个文件")
                return ReviewDecision(ReviewDecision.APPROVE)

        ws, plan, _ = run_planning_phase(
            requirement="实现校验", project_root=project,
            workspace_root=tmp / "_ws", task_name="revise-test",
            reviewer=CountingReviewer(),
        )
        check(bool(plan) and is_approved(plan), "最终获得已批准计划")
        check(calls["n"] == 2, f"恰好经历 2 轮审核（实际 {calls['n']}）")
        check(plan["revision"] == 2, f"修订版本号递增到 2（实际 {plan.get('revision')}）")
        check((ws.reports_dir / "plan_approved.json").exists(), "落盘 plan_approved.json")
        check((ws.reports_dir / "plan_v1_rejected.json").exists(), "记录被打回的版本")

        # 打回理由应被传给 planner
        msgs = ws.messages_path.read_text(encoding="utf-8")
        check("范围太大" in msgs, "打回理由写入黑板")


def test_user_plan_path() -> None:
    print("\n[5] 路径二：用户自拟方案 → 确认")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = _project(tmp)
        reviewer = ScriptedReviewer([
            ReviewDecision(ReviewDecision.USER_PLAN,
                           note="优先保证可读性",
                           user_plan_text="1. 新建 src/handler.py 实现处理\n2. 新建 tests/test_handler.py 测试",
                           scope_files=["src/handler.py", "tests/test_handler.py"]),
            ReviewDecision(ReviewDecision.APPROVE),
        ])
        ws, plan, _ = run_planning_phase(
            requirement="实现处理器", project_root=project,
            workspace_root=tmp / "_ws", task_name="userplan-test",
            reviewer=reviewer,
        )
        check(bool(plan) and is_approved(plan), "用户自拟方案最终获批")
        check(plan.get("source") == "user", "计划来源标记为 user")
        check("src/handler.py" in plan["scope"]["files"], "用户指定的范围被采纳")
        check((ws.reports_dir / "plan_approved.json").exists(), "落盘已批准计划")
        # 第一次审核看到的是 LLM 草稿（证明"先规划再让用户改"的顺序正确）
        check(reviewer.seen_plans[0].get("status") in ("draft", None), "首轮先给出 LLM 草稿供用户参考")


def test_plan_cap_abort() -> None:
    print("\n[6] 熔断：始终打回 → 达到上限中止")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = _project(tmp)
        import agents.config as cfg

        old = cfg.SETTINGS.max_plan_rounds
        cfg.SETTINGS.max_plan_rounds = 3

        class AlwaysReject:
            def review(self, plan, round_index):
                return ReviewDecision(ReviewDecision.REVISE, note="永不满意")

        nodes.set_llm(None)
        try:
            ws, plan, info = run_planning_phase(
                requirement="永不通过", project_root=project,
                workspace_root=tmp / "_ws", task_name="plan-abort",
                reviewer=AlwaysReject(),
            )
            check(not plan, "未产出计划")
            check(info.get("success") is False, "标记为失败")
            check(info.get("abort_reason"), "记录中止原因")
            st = ws.read_status()
            check(st.get("phase") == "aborted", f"工作区状态为 aborted（实际 {st.get('phase')}）")
        finally:
            cfg.SETTINGS.max_plan_rounds = old


def test_graph_blocks_coding_without_plan() -> None:
    print("\n[7] 强约束：无已批准计划时不得进入编码")
    from agents.supervisor import decide
    from agents.state import new_state

    st = new_state("需求", "/example/project", "/example/project/_ws", "/example/project/_ws/t", plan=None)
    check(decide(st) == "planner", "无计划 -> 路由到 planner")

    st2 = new_state("需求", "/example/project", "/example/project/_ws", "/example/project/_ws/t",
                    plan={"status": "approved", "revision": 1, "scope": {"files": ["a.py"]}, "steps": []})
    check(decide(st2) == "coder", "有计划 -> 放行到 coder")

    st3 = new_state("需求", "/example/project", "/example/project/_ws", "/example/project/_ws/t",
                    plan={"status": "draft", "revision": 1})
    check(decide(st3) == "planner", "草稿状态计划不算已批准")


def main() -> int:
    print("=" * 62)
    print("  计划审核循环自检")
    print("=" * 62)
    test_plan_model()
    test_scope_enforcement()
    test_user_written_plan()
    test_revision_loop()
    test_user_plan_path()
    test_plan_cap_abort()
    test_graph_blocks_coding_without_plan()

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"  结果: {passed}/{total} 通过")
    if passed < total:
        print("  失败项:")
        for ok, label in _results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())