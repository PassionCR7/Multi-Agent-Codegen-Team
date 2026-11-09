"""P0/P1 自检：计划授权、写前范围闸门、生命周期与待办。

运行::

    ./.venv/Scripts/python.exe -m tests.selftest_mandate
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import nodes
from agents.lifecycle import (
    answer_attention,
    derive_stage,
    make_attention,
    make_report,
)
from agents.mandate import (
    MandateError,
    build_mandate,
    normalize_rel_path,
    plan_digest,
    scope_allows,
    verify_mandate,
)
from agents.plan import check_scope, normalize_plan
from agents.runner import run_team
from agents.state import new_state
from agents.supervisor import decide
from agents.workspace import Workspace

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


def _plan(files=("ok.py",), status="approved") -> dict:
    plan = normalize_plan({"steps": [], "scope": {"files": list(files)}, "revision": 1})
    plan["status"] = status
    return plan


def _expect_error(fn, error_type, label: str) -> None:
    try:
        fn()
    except error_type:
        check(True, label)
    except Exception as exc:  # noqa: BLE001
        check(False, f"{label}（实际抛出 {type(exc).__name__}）")
    else:
        check(False, label)


def test_mandate_core() -> None:
    print("\n[1] Mandate：计划指纹与不可变授权")
    plan = _plan(["src/app.py"])
    digest1 = plan_digest(plan)
    digest2 = plan_digest(dict(plan))
    check(digest1 == digest2, "相同计划指纹稳定")

    changed = dict(plan)
    changed["scope"] = {"files": ["src/app.py", "src/extra.py"]}
    check(plan_digest(changed) != digest1, "计划内容变化后指纹变化")

    mandate = build_mandate(plan, approved_by="test")
    check(mandate.plan_digest == digest1, "Mandate 记录计划指纹")
    check(mandate.scope_files == ("src/app.py",), "Mandate 记录白名单")
    check(mandate.approved_by == "test", "Mandate 记录批准人")

    verify_mandate(plan, mandate.to_dict())
    check(True, "正常计划通过授权校验")

    tampered = dict(plan)
    tampered["scope"] = {"files": ["src/app.py", "src/evil.py"]}
    _expect_error(lambda: verify_mandate(tampered, mandate.to_dict()), MandateError, "计划被篡改后授权失效")

    _expect_error(lambda: build_mandate(_plan(status="draft")), MandateError, "草稿计划不能生成授权")


def test_path_and_scope() -> None:
    print("\n[2] 路径归一化与范围判断")
    check(normalize_rel_path("./src\\a.py") == "src/a.py", "反斜杠与 ./ 被归一化")
    _expect_error(lambda: normalize_rel_path("../evil"), MandateError, "拒绝 .. 路径")
    _expect_error(lambda: normalize_rel_path("/etc/passwd"), MandateError, "拒绝绝对路径")
    _expect_error(lambda: normalize_rel_path(""), MandateError, "拒绝空路径")

    check(scope_allows(["src/app.py"], "src/app.py"), "精确文件名通过")
    check(not scope_allows(["src/app.py"], "src/app.py/x"), "精确文件名不允许子路径")
    check(scope_allows(["src/"], "src/new.py"), "目录范围允许其下文件")
    check(not scope_allows(["src/"], "src2/new.py"), "目录范围不误伤同前缀目录")
    check(not scope_allows(["src/"], "../evil.py"), "目录范围拒绝路径穿越")

    ok, v = check_scope(_plan(["src/app.py"]), ["src/app.py/x"])
    check(not ok and v == ["src/app.py/x"], "check_scope 使用新语义")


def test_workspace_guard() -> None:
    print("\n[3] Workspace：写前范围闸门")
    with tempfile.TemporaryDirectory() as t:
        ws = Workspace(Path(t), "guard-test")
        plan = _plan(["ok.py"])
        mandate = build_mandate(plan).to_dict()

        _expect_error(lambda: ws.artifact_path("../artifacts_evil/x.py"), ValueError, "拒绝 artifacts_evil 前缀逃逸")
        _expect_error(lambda: ws.artifact_path("/etc/passwd"), ValueError, "拒绝绝对路径")

        path = ws.write_artifact_checked("ok.py", "x = 1\n", mandate)
        check(path.exists(), "白名单内文件可写")

        _expect_error(
            lambda: ws.write_artifact_checked("evil.py", "x = 2\n", mandate),
            MandateError,
            "白名单外文件被拒绝写入",
        )
        check(not (ws.artifacts_dir / "evil.py").exists(), "越界文件未落盘")

        rejection = ws.reject_artifact("evil.py", "x = 2\n", "测试拒绝", mandate)
        check(rejection.exists() and rejection.parent.name == "scope_rejected", "越界内容留档到 scope_rejected")

        (ws.artifacts_dir / "ok.py").write_text("old = 1\n", encoding="utf-8")
        ok, _msg = ws.apply_patch("ok.py", [{"old": "old", "new": "new"}], mandate=mandate)
        check(ok, "白名单内补丁可应用")
        ok2, _msg2 = ws.apply_patch("evil.py", [{"old": "x", "new": "y"}], mandate=mandate)
        check(not ok2, "白名单外补丁被拒绝")


def test_lifecycle() -> None:
    print("\n[4] Ticket 生命周期与 Attention")
    draft = new_state("test", ".", ".", "ws", plan=_plan(status="draft"))
    check(derive_stage(draft) == "plan_review", "草稿计划 -> plan_review")

    approved = new_state("test", ".", ".", "ws", plan=_plan())
    check(derive_stage(approved) == "queued", "已批准但未开始 -> queued")

    working = dict(approved)
    working["assignments"] = [{"id": "a1", "state": "running"}]
    check(derive_stage(working) == "working", "有运行中 assignment -> working")

    ready = dict(working)
    ready["report"] = make_report(ready, "ready", "完成")
    check(derive_stage(ready) == "awaiting_review", "ready 报告 -> awaiting_review")

    blocked = dict(working)
    blocked["report"] = make_report(blocked, "blocked", "卡住")
    check(derive_stage(blocked) == "blocked", "blocked 报告 -> blocked")

    attention = make_attention("是否纳入范围？", options=[{"id": "expand_scope", "label": "纳入"}])
    blocked["attention"] = attention
    check(derive_stage(blocked) == "awaiting_answer", "有 attention -> awaiting_answer")

    answered = answer_attention(blocked, attention["id"], "expand_scope", "同意")
    check(answered["answered_at"] is not None, "回答 attention 后记录时间")
    blocked["attention"] = answered
    check(derive_stage(blocked) == "blocked", "回答后回到原阶段")


def test_supervisor_mandate() -> None:
    print("\n[5] Supervisor：计划被篡改时中止")
    plan = _plan(["ok.py"])
    mandate = build_mandate(plan).to_dict()
    state = new_state("test", ".", ".", "ws", plan=plan, mandate=mandate)
    state["artifacts"] = []
    check(decide(state) == "coder", "正常授权 -> 路由到 coder")

    tampered = dict(state)
    tampered["plan"] = dict(plan)
    tampered["plan"]["scope"] = {"files": ["ok.py", "evil.py"]}
    check(decide(tampered) == "abort", "计划被篡改 -> abort")


def test_run_team_scope_rejection() -> None:
    print("\n[6] run_team：越界文件被拦截并生成待办")
    class Stub:
        def complete(self, system: str, user: str) -> str:
            if "[ROLE=CODER]" in system:
                return json.dumps({"files": [{"path": "evil.py", "content": "x = 1\n"}], "notes": "try"})
            return json.dumps({"aligned": True, "safe": True, "findings": [], "patches": [], "resolved": True})

    old_llm = nodes._LLM
    nodes.set_llm(Stub())
    try:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "proj"
            root.mkdir()
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            plan = _plan(["ok.py"])
            mandate = build_mandate(plan).to_dict()
            result = run_team(
                "test", root,
                workspace_root=Path(t) / "ws",
                task_name="scope-test",
                plan=plan,
                mandate=mandate,
            )
            ws = Path(result["workspace_dir"])
            check(result.get("success") is False, "任务判定失败")
            check(result.get("ticket_stage") == "awaiting_answer", "进入 awaiting_answer")
            check(bool(result.get("attention")), "生成待你决定事项")
            check(not (ws / "artifacts" / "evil.py").exists(), "越界文件未写入 artifacts")
            check(bool(list((ws / "reports" / "scope_rejected").glob("*.json"))), "越界内容留有审计记录")
    finally:
        nodes.set_llm(old_llm)


def main() -> int:
    print("=" * 62)
    print("  P0/P1 自检：Mandate + 生命周期 + 范围闸门")
    print("=" * 62)
    test_mandate_core()
    test_path_and_scope()
    test_workspace_guard()
    test_lifecycle()
    test_supervisor_mandate()
    test_run_team_scope_rejection()

    total = len(_results)
    passed = sum(1 for ok, _ in _results if ok)
    print("\n" + "=" * 62)
    print(f"  结果: {passed}/{total} 通过")
    if passed != total:
        print("  失败项:")
        for ok, label in _results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
