"""自检脚本：验证架构关键行为，尤其是"发现阻断问题 → 修复 → 复检"的闭环。

运行::

    ./.venv/Scripts/python.exe -m tests.selftest
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import nodes
from agents.runner import run_team
from agents.scanner import ProjectScanner
from agents.utils import extract_json, has_blocking_issues
from agents.workspace import Workspace

PASS, FAIL = "✅", "❌"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  {PASS if cond else FAIL} {label}")


def _approved_plan(*files: str) -> dict:
    """构造一份已批准计划，供执行阶段测试直接注入。

    执行阶段测试关注的是"修复闭环/熔断"，而非计划审核本身，
    所以这里直接给一份最小可用的已批准计划，把计划环节旁路掉。
    """
    return {
        "requirement": "测试",
        "understanding": "测试用计划",
        "steps": [],
        "scope": {"files": list(files), "notes": "测试范围"},
        "status": "approved",
        "revision": 1,
        "source": "test",
    }


# --------------------------------------------------------------------------- #
class ScriptedLLM:
    """可编排的假模型：按调用顺序返回预设结果，用于精确驱动状态流转。"""

    def __init__(self, coder_payloads, aligner_payloads, security_payloads, bugfixer_payloads=None):
        self.queues = {
            "coder": list(coder_payloads),
            "aligner": list(aligner_payloads),
            "security": list(security_payloads),
            "bugfixer": list(bugfixer_payloads or [{}]),
        }
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        from agents.prompts import detect_role

        role = detect_role(system)
        if role is None:
            role = "CODER"
            for r in ("CODER", "ALIGNER", "SECURITY", "BUGFIXER"):
                if r in system.upper():
                    role = r
                    break
        role = role.lower()
        self.calls.append(role)
        queue = self.queues.get(role) or [{}]
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return json.dumps(payload, ensure_ascii=False)


def test_utils() -> None:
    print("\n[1] 工具层：JSON 抽取")
    check(extract_json('{"a": 1}') == {"a": 1}, "直接解析")
    check(extract_json('```json\n{"a": 2}\n```') == {"a": 2}, "剥离代码围栏")
    check(extract_json('前缀文字 {"a": {"b": 3}} 后缀')["a"]["b"] == 3, "截取平衡花括号")
    check("_parse_error" in extract_json("完全不是JSON"), "非法输入不崩溃")

    print("\n[2] 工具层：阻断性判定")
    blk, reasons = has_blocking_issues(
        {"missing": [{"severity": "critical", "requirement": "登录功能"}]}, {}
    )
    check(blk and len(reasons) == 1, "对齐 critical 缺失 → 阻断")
    blk, _ = has_blocking_issues({}, {"findings": [{"severity": "high", "description": "命令注入"}]})
    check(blk, "安全 high 发现 → 阻断")
    blk, _ = has_blocking_issues({}, {"findings": [{"severity": "info", "description": "风格建议"}]})
    check(not blk, "info 级不阻断")


def test_scanner() -> None:
    print("\n[3] 扫描器：真实项目事实提取")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_app.py").write_text("def test_x(): pass", encoding="utf-8")
        (root / "requirements.txt").write_text("requests\npandas>=2.0\n", encoding="utf-8")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")

        snap = ProjectScanner(root).scan()
        paths = [i["path"] for i in snap["tree"]]
        check("src/app.py" in paths, "识别源文件")
        check(not any("__pycache__" in p for p in paths), "忽略 __pycache__")
        check(snap["languages"].get("Python", 0) >= 2, "统计语言构成")
        check("requirements.txt" in snap["manifests"], "解析依赖清单")
        check(len(snap["tests"]) == 1, "识别测试文件")
        check(snap["conventions"]["primary_language"] == "Python", "推断项目约定")
        rendered = ProjectScanner.render_for_prompt(snap)
        check("requirements" in rendered or "Python" in rendered, "渲染为可读提示词")


def test_workspace() -> None:
    print("\n[4] 工作区：黑板读写与越界防护")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Workspace(tmp, "t1")
        ws.init_task("测试需求")
        ws.update_status(phase="coding")
        ws.post_message("coder", "你好")
        ws.log_run("coder", True, 12, "ok")
        p = ws.write_artifact("pkg/mod.py", "x = 1")

        check(ws.read_status()["phase"] == "coding", "状态可读写")
        check(p.exists() and p.parent.name == "pkg", "产物可写入子目录")
        # init_task / post_message / write_artifact 各写一条，共 3 条
        check(len(ws.messages_path.read_text(encoding="utf-8").strip().splitlines()) == 3, "消息 append-only")
        check(ws.tree().count("mod.py") == 1, "目录树可渲染")

        # 路径穿越必须被拦截
        blocked = False
        try:
            ws.write_artifact("../../evil.py", "bad")
        except ValueError:
            blocked = True
        check(blocked, "阻止路径穿越写入")

        ws2 = Workspace.from_dir(ws.dir)
        check(ws2.dir == ws.dir, "from_dir 复用同一目录")


def test_loop_with_blocking_issues() -> None:
    print("\n[5] 闭环：发现阻断问题 → 修复 → 复检通过")
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        (project / "main.py").write_text("def f(): pass", encoding="utf-8")

        code_v1 = {"files": [{"path": "sol.py", "language": "python", "content": "def f(): return 1", "description": "初版"}], "notes": "初版"}
        code_v2 = {"files": [{"path": "sol.py", "language": "python", "content": "def f():\n    return 2", "description": "修复版"}], "notes": "已修复"}

        llm = ScriptedLLM(
            coder_payloads=[code_v1, code_v2],
            aligner_payloads=[
                # 第一轮：发现 critical 缺失 → 触发修复
                {"aligned": False, "missing": [{"requirement": "边界处理", "severity": "critical", "why": "未处理空输入"}],
                 "issues": [], "score": 0.3, "coverage": [], "extra": [], "rationale": "缺边界"},
                # 复检：通过
                {"aligned": True, "missing": [], "issues": [], "score": 1.0, "coverage": [], "extra": [], "rationale": "已修复"},
            ],
            security_payloads=[
                {"safe": True, "score": 0.9, "findings": [], "summary": "无风险"},
                {"safe": True, "score": 0.95, "findings": [], "summary": "复检通过"},
            ],
            bugfixer_payloads=[{
                "bugs": [{"id": "BUG-001", "source": "aligner", "problem": "未处理空输入", "severity": "critical"}],
                "patches": [{"path": "sol.py", "language": "python", "content": "def f():\n    return 2", "fixes": "BUG-001", "explanation": "增加边界处理"}],
                "notes": "已修复空输入问题",
                "resolved": True,
            }],
        )

        nodes.set_llm(llm)
        try:
            result = run_team("实现函数 f", project, task_name="loop-test", plan=_approved_plan("sol.py"))
        finally:
            nodes.set_llm(None)  # 还原

        check(result["success"], "最终判定为成功")
        check(result.get("bugfix_rounds", 0) == 1, f"触发过 1 轮修复（实际 {result.get('bugfix_rounds')}）")
        check(result.get("iteration", 0) >= 1, f"迭代轮次递增（实际 {result.get('iteration')}）")
        check("bugfixer" in llm.calls, "bugfixer 被真正调用")
        check(llm.calls.count("aligner") >= 2, "修复后走了复检（aligner 再次执行）")

        ws = Workspace.from_dir(result["workspace_dir"])
        msgs = [json.loads(l) for l in ws.messages_path.read_text(encoding="utf-8").strip().splitlines()]
        agents_seen = {m["agent"] for m in msgs}
        check({"coder", "aligner", "security", "bugfixer", "supervisor"} <= agents_seen, "五方均在黑板留痕")
        check(ws.report_path("final_state").exists(), "产出最终状态报告")

        # 回归：reducer 列表不能被后续节点的空增量清空
        # （历史上 run_team 用 updates 累加，bugfixer 返回 artifacts:[] 时
        #   会把 coder 已产出的文件整体抹掉，导致最终产物丢失）
        from agents.state import latest_artifacts

        arts = latest_artifacts(result)
        check(len(arts) >= 1, f"修复后产物未丢失（实际 {len(arts)} 个）")
        check(any(a["path"] == "sol.py" for a in arts), "最终产物包含 coder 产出的 sol.py")


def test_reducer_not_clobbered() -> None:
    print("\n[6] 回归：带 reducer 的列表字段不被空增量清空")
    from agents.state import latest_artifacts
    from agents.config import SETTINGS
    from agents.graph import build_graph

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        (project / "m.py").write_text("x=1", encoding="utf-8")

        # coder 正常产出 -> 后续某节点返回空 artifacts，最终仍应保留 coder 的产物
        llm = ScriptedLLM(
            coder_payloads=[{"files": [{"path": "keep.py", "language": "python",
                                        "content": "def f(): return 1"}], "notes": ""}],
            aligner_payloads=[{"aligned": True, "missing": [], "issues": [], "score": 1.0}],
            security_payloads=[{"safe": True, "findings": [], "summary": ""}],
            bugfixer_payloads=[{"bugs": [], "patches": [], "notes": "无补丁", "resolved": True}],
        )
        nodes.set_llm(llm)
        try:
            result = run_team("实现 keep", project, task_name="reducer-test",
                              plan=_approved_plan("keep.py"))
        finally:
            nodes.set_llm(None)

        arts = [a["path"] for a in latest_artifacts(result)]
        check("keep.py" in arts, f"产物在空增量后依然保留（实际 {arts}）")


def test_safe_reader() -> None:
    print("\n[7] 安全读取器：围栏 / 拦截 / 脱敏")
    from agents.reader import SafeReader, _redact

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (root / ".env").write_text("OPENAI_API_KEY=sk-ABCDEFGH12345678\n", encoding="utf-8")
        (root / "server.pem").write_text("-----BEGIN KEY-----", encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        (root / "big.py").write_text("\n".join(f"x{i}=1" for i in range(2000)), encoding="utf-8")

        r = SafeReader(root, max_file_lines=100)
        ok = r.read("pkg/mod.py")
        check(ok.ok and "def f()" in ok.content, "正常文件可读")

        check(not r.read(".env").ok, "拒绝读取 .env")
        check(not r.read("server.pem").ok, "拒绝读取证书文件")
        check(not r.read("blob.bin").ok, "拒绝读取二进制")
        check(not r.read("../outside.txt").ok, "拒绝目录穿越（..）")
        check(not r.read("pkg/nope.py").ok, "不存在的文件被拒")
        check(not r.read("pkg").ok, "目录被拒")

        big = r.read("big.py")
        check(big.ok and big.truncated, "超长文件被截断")

        # 路径归一化（大小写不敏感、反斜杠）
        check(r.read("PKG\\MOD.PY").ok, "路径归一化后仍可读")

        # 密钥脱敏
        out, hit = _redact("OPENAI_API_KEY=sk-ABCDEFGH12345678")
        check(hit and "sk-ABCDEFGH12345678" not in out, "疑似密钥被脱敏")
        out2, _ = _redact("password=SuperSecret123")
        check("SuperSecret123" not in out2, "password 值被脱敏")


def test_ondemand_reading() -> None:
    print("\n[8] 按需阅读：coder 请求 → 安全注入 → 再产出")
    from agents import nodes as _nodes
    from agents.runner import run_team

    class ReadStub:
        def __init__(self):
            self.calls: list[str] = []
            self.second_user = ""

        def complete(self, system, user):
            from agents.prompts import detect_role

            role = (detect_role(system) or "CODER").lower()
            self.calls.append(role)
            if role == "coder":
                if "你请求查看的既有文件内容" not in user:
                    return json.dumps({"need_files": ["pkg/mod.py", ".env"], "files": [], "notes": "先读现有模块"})
                self.second_user = user
                return json.dumps({
                    "need_files": [],
                    "files": [{"path": "impl.py", "language": "python",
                               "content": "def f(): return 1", "description": "实现"}],
                    "notes": "基于真实接口实现",
                })
            if role == "aligner":
                return json.dumps({"aligned": True, "missing": [], "issues": [], "score": 1.0})
            if role == "security":
                return json.dumps({"safe": True, "findings": [], "summary": ""})
            return "{}"

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        (project / "pkg").mkdir(parents=True)
        (project / "pkg" / "mod.py").write_text("def existing_api(x):\n    return x\n", encoding="utf-8")
        (project / ".env").write_text("OPENAI_API_KEY=sk-SHOULDNOTLEAK123\n", encoding="utf-8")

        stub = ReadStub()
        _nodes.set_llm(stub)
        try:
            result = run_team("实现 impl", project, task_name="ondemand-test",
                              plan=_approved_plan("impl.py"))
        finally:
            _nodes.set_llm(None)

        check(stub.calls.count("coder") >= 2, f"coder 被调用多轮（实际 {stub.calls.count('coder')}）")
        check("def existing_api" in stub.second_user, "真实文件内容被注入第二轮提示词")
        check("SHOULDNOTLEAK" not in stub.second_user, "敏感文件内容未泄漏进提示词")
        check(result.get("success"), "最终成功产出")

        audit = result.get("read_audit", [])
        read_ok = [a for a in audit if a.get("ok")]
        read_no = [a for a in audit if not a.get("ok")]
        check(any(a["path"] == "pkg/mod.py" for a in read_ok), "审计记录成功读取")
        check(any(a["path"] == ".env" for a in read_no), "审计记录被拒读取")


def test_read_round_limit() -> None:
    print("\n[9] 按需阅读：轮次上限兜底（不无限索取）")
    from agents import nodes as _nodes
    from agents.runner import run_team
    import agents.config as cfg

    class GreedyStub:
        """永远在索要文件，从不产出 —— 必须被轮次上限打断。"""

        def __init__(self):
            self.coder_calls = 0

        def complete(self, system, user):
            from agents.prompts import detect_role

            role = (detect_role(system) or "CODER").lower()
            if role == "coder":
                self.coder_calls += 1
                return json.dumps({"need_files": ["pkg/mod.py"], "files": [], "notes": "还要看"})
            if role == "aligner":
                return json.dumps({"aligned": True, "missing": [], "issues": [], "score": 1.0})
            if role == "security":
                return json.dumps({"safe": True, "findings": [], "summary": ""})
            return "{}"

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        (project / "pkg").mkdir(parents=True)
        (project / "pkg" / "mod.py").write_text("x=1\n", encoding="utf-8")

        old_read = cfg.SETTINGS.max_read_rounds
        old_tries = cfg.SETTINGS.max_code_attempts
        cfg.SETTINGS.max_read_rounds = 2
        cfg.SETTINGS.max_code_attempts = 3
        stub = GreedyStub()
        _nodes.set_llm(stub)
        try:
            run_team("贪心索取", project, task_name="readlimit-test", plan=_approved_plan("pkg/x.py"))
        finally:
            cfg.SETTINGS.max_read_rounds = old_read
            cfg.SETTINGS.max_code_attempts = old_tries
            _nodes.set_llm(None)

        # 界应为：外层产出尝试次数 × (1 首轮 + max_read_rounds 次追问)
        bound = old_tries * (old_read + 1)
        check(stub.coder_calls <= bound,
              f"coder 调用次数受双重上限约束（实际 {stub.coder_calls}，上界 {bound}）")
        check(stub.coder_calls < 50, "未发生无限循环（远低于 recursion_limit）")


def test_abort_when_coder_never_produces() -> None:
    print("\n[11] 熔断：coder 一直不产出代码（交白卷兜底）")
    from agents import nodes as _nodes
    from agents.runner import run_team
    import agents.config as cfg

    class BlankStub:
        """从不产出任何文件，也不请求读取 —— 必须被 code_attempts 兜底截断。"""

        def __init__(self):
            self.coder_calls = 0

        def complete(self, system, user):
            from agents.prompts import detect_role

            role = (detect_role(system) or "CODER").lower()
            if role == "coder":
                self.coder_calls += 1
                return json.dumps({"need_files": [], "files": [], "notes": "我想不出来"})
            return json.dumps({"aligned": True, "safe": True, "missing": [], "issues": [],
                               "findings": [], "score": 1.0, "summary": ""})

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        (project / "m.py").write_text("x=1", encoding="utf-8")

        old = cfg.SETTINGS.max_code_attempts
        cfg.SETTINGS.max_code_attempts = 3
        stub = BlankStub()
        _nodes.set_llm(stub)
        try:
            result = run_team("交白卷", project, task_name="blank-test",
                              plan=_approved_plan("out.py"))
        finally:
            cfg.SETTINGS.max_code_attempts = old
            _nodes.set_llm(None)

        check(not result.get("success"), "判定为失败")
        check(result.get("phase") == "aborted", f"阶段为 aborted（实际 {result.get('phase')}）")
        check(stub.coder_calls <= old, f"coder 调用受上限约束（实际 {stub.coder_calls}，上限 {old}）")
        check("未产出" in (result.get("abort_reason") or ""), "中止原因指明 coder 未产出")
        check(stub.coder_calls < 50, "未退化为 recursion_limit 级别的空转")


def test_abort_on_limit() -> None:
    print("\n[12] 熔断：无法修复时到达上限中止")
    import agents.config as cfg

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "proj"
        project.mkdir()
        code = {"files": [{"path": "s.py", "language": "python", "content": "x=1"}], "notes": ""}
        # 永远无法通过对齐 → 必须被熔断
        llm = ScriptedLLM(
            coder_payloads=[code],
            aligner_payloads=[{"aligned": False, "missing": [{"requirement": "R", "severity": "critical", "why": "永不满足"}],
                               "issues": [], "score": 0.1, "coverage": [], "extra": [], "rationale": "x"}],
            security_payloads=[{"safe": True, "score": 1.0, "findings": [], "summary": ""}],
            bugfixer_payloads=[{"bugs": [{"id": "B", "source": "aligner", "problem": "x", "severity": "critical"}],
                                "patches": [{"path": "s.py", "language": "python", "content": "x=2", "fixes": "B", "explanation": "y"}],
                                "notes": "", "resolved": False}],
        )
        old = cfg.SETTINGS.max_bugfix_rounds
        cfg.SETTINGS.max_bugfix_rounds = 2
        nodes.set_llm(llm)
        try:
            result = run_team("永远修不好", project, task_name="abort-test", plan=_approved_plan("s.py"))
            check(not result["success"], "最终判定为失败")
            check(result.get("phase") == "aborted", f"阶段为 aborted（实际 {result.get('phase')}）")
            check(result.get("abort_reason"), "记录了中止原因")
            check(result.get("bugfix_rounds") <= 3, "修复轮次受上限约束，未死循环")
        finally:
            cfg.SETTINGS.max_bugfix_rounds = old
            nodes.set_llm(None)


def main() -> int:
    print("=" * 60)
    print("  多智能体架构自检")
    print("=" * 60)
    test_utils()
    test_scanner()
    test_workspace()
    test_loop_with_blocking_issues()
    test_reducer_not_clobbered()
    test_safe_reader()
    test_ondemand_reading()
    test_read_round_limit()
    test_abort_when_coder_never_produces()
    test_abort_on_limit()

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 60)
    print(f"  结果: {passed}/{total} 通过")
    if passed < total:
        print("  失败项:")
        for ok, label in _results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())