"""多模块分段生成与跨模块对账的自检。

运行::

    ./.venv/Scripts/python.exe -m tests.selftest_multimodule
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import nodes
from agents.interface import (
    analyze_modules,
    extract_calls,
    extract_defined,
    extract_imports,
    render_signals,
    render_signatures,
)
from agents.runner import run_team
from agents.workspace import Workspace

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


def _plan(files: list[str]) -> dict:
    return {"requirement": "t", "steps": [], "scope": {"files": files},
            "status": "approved", "revision": 1, "source": "test"}


# --------------------------------------------------------------------------- #
def test_extraction() -> None:
    print("\n[1] 机械提取：定义 / 导入 / 调用")
    py = (
        "from pkg.helper import thing, other\n"
        "import os, json\n\n"
        "CONFIG = {}\n\n"
        "def run(a, b=1):\n"
        "    return helper(x)\n\n"
        "class Widget(Base):\n"
        "    pass\n"
    )
    defs = extract_defined(py, "m.py", "python")
    names = {d["name"] for d in defs}
    check({"run", "Widget", "CONFIG"} <= names, f"提取定义（{sorted(names)}）")
    check(next(d for d in defs if d["name"] == "run")["params"] == "a, b=1", "归一化参数")

    imps = extract_imports(py, "m.py", "python")
    check(any(i["module"] == "pkg.helper" and "thing" in i["names"] for i in imps), "提取 from-import")
    check(not any(i["module"] in ("os", "json") for i in imps), "过滤标准库裸 import")

    calls = extract_calls(py, "m.py", "python")
    check("helper" in calls, "提取函数调用")
    check("print" not in calls and "len" not in calls, "过滤内置函数")

    js = "import {foo, bar} from './util';\nfunction go(a) { return foo(a); }\n"
    check({d["name"] for d in extract_defined(js, "m.js", "javascript")} == {"go"}, "JS 定义提取")
    check(any(i["module"] == "./util" for i in extract_imports(js, "m.js", "javascript")), "JS 导入提取")


def test_signals() -> None:
    print("\n[2] 跨模块信号：三类低噪问题")
    mods = [
        {"path": "a.py", "language": "python",
         "content": "def fetch(url):\n    return {}\n"},
        {"path": "b.py", "language": "python",
         "content": "from a import fetch, parse\n\ndef run(x):\n    return process(x)\n"},
        {"path": "c.py", "language": "python",
         "content": "def process(rows):\n    return rows\n"},
    ]
    sig = analyze_modules(mods)
    check([x["name"] for x in sig["dangling_imports"]] == ["parse"], "识别悬空 import")
    check(any(x["name"] == "process" for x in sig["call_without_import"]), "识别调用未导入")

    # 签名不同 -> 重复定义
    dup = analyze_modules([
        {"path": "x.py", "language": "python", "content": "def go(a):\n    return a\n"},
        {"path": "y.py", "language": "python", "content": "def go(a, b):\n    return a\n"},
    ])
    check([d["name"] for d in dup["duplicate_defs"]] == ["go"], "识别签名不一致的重复定义")

    # 签名相同 -> 不报（故意控噪）
    same = analyze_modules([
        {"path": "x.py", "language": "python", "content": "def go(a):\n    return a\n"},
        {"path": "y.py", "language": "python", "content": "def go(a):\n    return a\n"},
    ])
    check(not same["duplicate_defs"], "签名相同不误报（控噪）")

    # 相对导入解析
    rel = analyze_modules([
        {"path": "pkg/__init__.py", "language": "python", "content": ""},
        {"path": "pkg/helper.py", "language": "python", "content": "def h():\n    pass\n"},
        {"path": "pkg/main.py", "language": "python", "content": "from .helper import missing\n"},
    ])
    check(any(x["name"] == "missing" for x in rel["dangling_imports"]), "解析相对导入并识别悬空")

    # 渲染
    check("悬空" in render_signals(sig), "信号可渲染为提示词")
    check("fetch" in render_signatures(mods[:1]), "签名可渲染")


def test_subtask_flow() -> None:
    print("\n[3] 分段生成：顺序执行 + 接口递增")

    class Stub:
        def __init__(self):
            self.targets: list[str] = []
            self.injected_sigs: list[str] = []

        def complete(self, system, user):
            from agents.prompts import detect_role

            role = detect_role(system) or "CODER"
            if role == "CODER":
                import re

                m = re.search(r"【你当前负责的模块】\s*\n\s*(\S+)", user)
                target = m.group(1) if m else "?"
                self.targets.append(target)
                # 记录第二轮起是否注入了已完成模块的签名
                self.injected_sigs.append(user)
                name = target.replace(".py", "").replace("/", "_")
                return json.dumps({
                    "need_files": [],
                    "files": [{"path": target, "language": "python",
                               "content": f"def fn_{name}():\n    return 1\n",
                               "description": target}],
                    "used_interfaces": [],
                    "notes": "",
                })
            if role == "RECONCILER":
                return json.dumps({"aligned": True, "consistency": 0.95, "findings": [],
                                   "false_positives": [], "summary": "ok"})
            if role == "ALIGNER":
                return json.dumps({"aligned": True, "missing": [], "issues": [], "score": 1.0})
            if role == "SECURITY":
                return json.dumps({"safe": True, "findings": [], "summary": ""})
            return "{}"

    with tempfile.TemporaryDirectory() as t:
        project = Path(t) / "proj"
        project.mkdir()
        stub = Stub()
        nodes.set_llm(stub)
        try:
            result = run_team("三模块", project, task_name="sub-task",
                              plan=_plan(["a.py", "b.py", "c.py"]))
        finally:
            nodes.set_llm(None)

        check(stub.targets == ["a.py", "b.py", "c.py"], f"按模块顺序生成（{stub.targets}）")
        # 第二个模块起，提示词里应含有第一个模块的真实签名
        check("fn_a" in stub.injected_sigs[1], "后续模块注入了已完成模块的真实签名")
        check("fn_a" not in stub.injected_sigs[0], "首个模块无前序签名（合理）")

        rec = result.get("reconcile") or {}
        check("aligned" in rec, "对账结果写入状态")
        check(rec.get("aligned") is True, "无问题时对账通过")


def test_reconcile_and_patch() -> None:
    print("\n[4] 对账 + 最小修补闭环")

    class Stub:
        def complete(self, system, user):
            from agents.prompts import detect_role

            role = detect_role(system) or "CODER"
            if role == "CODER":
                import re

                m = re.search(r"【你当前负责的模块】\s*\n\s*(\S+)", user)
                target = m.group(1) if m else "?"
                body = {
                    "a.py": "def fetch(url):\n    return {}\n",
                    "b.py": "from a import fetch, parse\n\ndef run(x):\n    return process(x)\n",
                    "c.py": "def process(rows):\n    return rows\n",
                }[target]
                return json.dumps({"need_files": [],
                                   "files": [{"path": target, "language": "python",
                                              "content": body, "description": target}],
                                   "used_interfaces": [], "notes": ""})
            if role == "RECONCILER":
                has = "未发现机械可检" not in user
                findings = []
                if has:
                    findings.append({
                        "id": "R1", "severity": "major", "type": "dangling_import",
                        "is_false_positive": False, "path": "b.py",
                        "description": "b.py 悬空 import parse 且未导入 process",
                        "evidence": "b.py 第1、4行",
                        "fix": "删除 parse，补 from c import process",
                        "patch": [{"old": "from a import fetch, parse",
                                   "new": "from a import fetch\nfrom c import process"}],
                    })
                return json.dumps({"aligned": not has, "consistency": 0.7 if has else 0.95,
                                   "findings": findings, "false_positives": [], "summary": "x"},
                                  ensure_ascii=False)
            if role == "ALIGNER":
                return json.dumps({"aligned": True, "missing": [], "issues": [], "score": 1.0})
            if role == "SECURITY":
                return json.dumps({"safe": True, "findings": [], "summary": ""})
            return "{}"

    with tempfile.TemporaryDirectory() as t:
        project = Path(t) / "proj"
        project.mkdir()
        nodes.set_llm(Stub())
        try:
            result = run_team("三模块", project, task_name="recon-test",
                              plan=_plan(["a.py", "b.py", "c.py"]))
        finally:
            nodes.set_llm(None)

        ws = Workspace.from_dir(result["workspace_dir"])
        b = (ws.artifacts_dir / "b.py").read_text(encoding="utf-8")
        check("parse" not in b, "悬空 import 被移除")
        check("from c import process" in b, "缺失 import 被补上")
        check(b.count("def run") == 1, "其余代码未被破坏")

        rec = result.get("reconcile") or {}
        check(rec.get("patched_files", 0) >= 1, f"记录修补文件数（{rec.get('patched_files')}）")
        check(len(rec.get("signals", {}).get("dangling_imports", [])) >= 1, "对账信号留痕")
        check((ws.reports_dir / "reconcile_r1.json").exists(), "对账报告已落盘")


def test_patch_safety() -> None:
    print("\n[5] 修补安全性：不唯一 / 不存在 的替换必须被拒绝")
    with tempfile.TemporaryDirectory() as t:
        ws = Workspace(t, "p")
        ws.write_artifact("m.py", "x = 1\ny = 1\n")

        ok, msg = ws.apply_patch("m.py", [{"old": "x = 1", "new": "x = 2"}])
        check(ok and "x = 2" in (ws.artifacts_dir / "m.py").read_text(encoding="utf-8"), "唯一片段可替换")

        # 另起一个文件，让目标片段**真的**出现两次
        ws.write_artifact("dup.py", "val = alpha\nval = alpha\n")
        ok2, msg2 = ws.apply_patch("dup.py", [{"old": "val = alpha", "new": "val = beta"}])
        check(not ok2, f"多义片段（出现多次）被拒绝（{msg2}）")
        check((ws.artifacts_dir / "dup.py").read_text(encoding="utf-8").count("alpha") == 2,
              "被拒绝时不修改原文件")

        ok3, msg3 = ws.apply_patch("m.py", [{"old": "不存在的文本", "new": "z"}])
        check(not ok3, "未找到的原文被拒绝")

        ok4, _ = ws.apply_patch("nope.py", [{"old": "a", "new": "b"}])
        check(not ok4, "不存在的文件被拒绝")

        # 混合：一处可应用、一处不可 —— 应报告部分成功而不静默
        mixed_ok, mixed_msg = ws.apply_patch("m.py", [
            {"old": "y = 1", "new": "y = 3"},
            {"old": "不存在", "new": "x"},
        ])
        check(mixed_ok and "失败" in mixed_msg, f"部分成功时明确报告失败项（{mixed_msg}）")

        # 路径穿越防护
        blocked = False
        try:
            ws.apply_patch("../../evil.py", [{"old": "a", "new": "b"}])
        except ValueError:
            blocked = True
        check(blocked, "拒绝越界路径")


def test_state_doc() -> None:
    print("\n[6] 共享状态文档：子 agent 的共同事实来源")
    from agents.workspace import Workspace

    with tempfile.TemporaryDirectory() as t:
        ws = Workspace(t, "doc")
        ws.write_report("plan_approved", {
            "requirement": "三模块", "steps": [{"goal": "写 A"}, {"goal": "写 B"}],
            "scope": {"files": ["a.py", "b.py", "c.py"]},
            "out_of_scope": ["不引入依赖"], "status": "approved", "revision": 1,
        })
        ws.write_snapshot({"languages": {"Python": 3}, "conventions": {"测试": "pytest"}})
        ws.write_artifact("a.py", "def fetch(u):\n    return {}\n")

        doc = ws.build_state_doc(progress={"done": 1, "total": 3, "current": "b.py"})

        # 落盘
        check(ws.state_json_path.exists(), "写入 state.json")
        check(ws.state_md_path.exists(), "写入 state.md")
        md = ws.state_md_path.read_text(encoding="utf-8")

        # 内容完整性
        check("def fetch" in md, "state.md 含已完成模块的真实签名")
        check("a.py" in doc["artifacts"][0]["path"], "结构化文档含产物条目")
        check(set(doc["pending_modules"]) == {"b.py", "c.py"}, f"正确算出未完成模块（{doc['pending_modules']}）")
        check(doc["progress"]["done"] == 1, "记录分段进度")
        check("三模块" in md, "state.md 含需求")
        check("不引入依赖" in md, "state.md 含明确不做")

        # 计划外产物应被显式标出
        ws.write_artifact("rogue.py", "x = 1\n")
        doc2 = ws.build_state_doc()
        check("rogue.py" in doc2["extra_artifacts"], "标出计划外产物")


def test_state_doc_no_drift() -> None:
    print("\n[7] 回归：状态文档必须反映磁盘真相（修补后不得漂移）")
    from agents.workspace import Workspace

    with tempfile.TemporaryDirectory() as t:
        ws = Workspace(t, "drift")
        ws.write_report("plan_approved", {"requirement": "r", "steps": [],
                                          "scope": {"files": ["m.py"]}, "status": "approved", "revision": 1})
        ws.write_artifact("m.py", "def old_name(a):\n    return a\n")

        before = ws.build_state_doc()["artifacts"][0]["defines"][0]
        check(before["name"] == "old_name", "初始文档反映写入时的签名")

        # 施加补丁（对账修补的真实动作）
        ok, _ = ws.apply_patch("m.py", [{"old": "def old_name(a):", "new": "def new_name(a, b):"}])
        check(ok, "补丁应用成功")

        after = ws.build_state_doc()["artifacts"][0]["defines"][0]
        check(after["name"] == "new_name", "重建文档后签名已更新（无漂移）")
        check(after["signature"] == "a, b", f"参数同步更新（{after['signature']}）")

        # 反向验证：若沿用内存副本会出错 —— 磁盘才是权威
        check("new_name" in ws.read_artifact("m.py"), "磁盘内容与文档一致")


def test_subtask_sees_state_doc() -> None:
    print("\n[8] 子 agent 能拿到共享状态文档指引，并读到前序模块真实签名")

    class Stub:
        def __init__(self):
            self.users: dict[str, str] = {}

        def complete(self, system, user):
            from agents.prompts import detect_role

            role = detect_role(system) or "CODER"
            if role == "CODER":
                import re

                m = re.search(r"【你当前负责的模块】\s*\n\s*(\S+)", user)
                target = m.group(1) if m else "?"
                self.users[target] = user
                return json.dumps({"need_files": [],
                                   "files": [{"path": target, "language": "python",
                                              "content": f"def fn_{target.replace('.py','')}():\n    return 1\n",
                                              "description": target}],
                                   "used_interfaces": [], "notes": ""})
            if role == "RECONCILER":
                return json.dumps({"aligned": True, "consistency": 0.95, "findings": [],
                                   "false_positives": [], "summary": "ok"})
            if role == "ALIGNER":
                return json.dumps({"aligned": True, "missing": [], "issues": [], "score": 1.0})
            if role == "SECURITY":
                return json.dumps({"safe": True, "findings": [], "summary": ""})
            return "{}"

    with tempfile.TemporaryDirectory() as t:
        project = Path(t) / "proj"
        project.mkdir()
        stub = Stub()
        nodes.set_llm(stub)
        try:
            result = run_team("三模块", project, task_name="doc-visible",
                              plan=_plan(["a.py", "b.py", "c.py"]))
        finally:
            nodes.set_llm(None)

        check(all("共享状态文档" in u for u in stub.users.values()), "每个子任务都被告知共享状态文档")
        check("state.md" in stub.users["b.py"], "提示词给出 state.md 可读路径")
        check("def fn_a" in stub.users["b.py"], "后写模块看到前序模块真实签名")

        ws = Workspace.from_dir(result["workspace_dir"])
        check(ws.state_md_path.exists(), "任务结束后共享状态文档仍在工作区")
        check("fn_a" in ws.state_md_path.read_text(encoding="utf-8"), "文档含全部已完成模块签名")


def main() -> int:
    print("=" * 62)
    print("  多模块分段生成与对账 自检")
    print("=" * 62)
    test_extraction()
    test_signals()
    test_subtask_flow()
    test_reconcile_and_patch()
    test_patch_safety()
    test_state_doc()
    test_state_doc_no_drift()
    test_subtask_sees_state_doc()

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"  结果: {passed}/{total} 通过")
    if passed < total:
        for ok, label in _results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())