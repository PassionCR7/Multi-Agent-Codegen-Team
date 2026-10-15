"""全局配置：从环境变量 / .env 读取，提供统一的 LLM 客户端工厂。

设计要点：
    * 支持任意 OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / vLLM 等），
      只需改 BASE_URL 与 API_KEY。
    * 未配置 API_KEY 时自动降级为 MockLLM，保证架构可离线跑通、可测试。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

try:  # dotenv 可选
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """运行期配置。"""

    # --- LLM ---
    api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL") or None)
    model: str = field(default_factory=lambda: os.getenv("CODER_MODEL", "gpt-4o-mini"))
    temperature: float = field(default_factory=lambda: _env_float("CODER_TEMPERATURE", 0.2))
    request_timeout: int = field(default_factory=lambda: _env_int("CODER_TIMEOUT", 120))
    force_mock: bool = field(default_factory=lambda: os.getenv("CODER_MOCK", "").lower() in {"1", "true", "yes"})

    # --- 循环控制（防止 agent 之间无限拉扯）---
    max_iterations: int = field(default_factory=lambda: _env_int("CODER_MAX_ITER", 6))
    max_bugfix_rounds: int = field(default_factory=lambda: _env_int("CODER_MAX_BUGFIX", 4))
    max_plan_rounds: int = field(default_factory=lambda: _env_int("CODER_MAX_PLAN", 5))
    # coder 连续未产出代码的上限。用于兜底"coder 一直交白卷"的死循环：
    # 没有代码且没有代码时，supervisor 会一直路由回 coder，而 iteration 不增长，
    # 只能等 recursion_limit（100）才被动终止。改为主动计数熔断。
    max_code_attempts: int = field(default_factory=lambda: _env_int("CODER_MAX_CODE_TRIES", 3))

    # --- 模块拆分与跨模块对账 ---
    # 计划里文件数 >= 该阈值时，启用"按模块分段生成 + 对账"
    subtask_threshold: int = field(default_factory=lambda: _env_int("CODER_SUBTASK_THRESHOLD", 2))
    # 单次任务最多处理的模块数（防止一次铺太开）
    subtask_max_modules: int = field(default_factory=lambda: _env_int("CODER_SUBTASK_MAX", 8))
    # 对账后最多允许的修补轮次
    reconcile_max_rounds: int = field(default_factory=lambda: _env_int("CODER_RECONCILE_ROUNDS", 1))

    # --- 按需阅读（coder 读取真实项目文件）---
    max_read_rounds: int = field(default_factory=lambda: _env_int("CODER_MAX_READ", 3))
    max_files_per_read: int = field(default_factory=lambda: _env_int("CODER_READ_FILES", 6))
    max_file_bytes: int = field(default_factory=lambda: _env_int("CODER_READ_BYTES", 60_000))
    max_file_lines: int = field(default_factory=lambda: _env_int("CODER_READ_LINES", 900))

    # --- 路径 ---
    workspace_dirname: str = field(default_factory=lambda: os.getenv("CODER_WORKSPACE", "_agent_workspace"))

    @property
    def use_mock(self) -> bool:
        return self.force_mock or not self.api_key

    def describe(self) -> str:
        if self.use_mock:
            return f"MockLLM (离线) | max_iter={self.max_iterations}"
        return f"{self.model} @ {self.base_url or 'api.openai.com'} | max_iter={self.max_iterations}"


SETTINGS = Settings()


# --------------------------------------------------------------------------- #
# LLM 抽象
# --------------------------------------------------------------------------- #
class LLM(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class RealLLM:
    """基于 langchain-openai 的真实模型客户端。"""

    def __init__(self, settings: Settings) -> None:
        from langchain_openai import ChatOpenAI  # 延迟导入

        kwargs: dict[str, Any] = {
            "model": settings.model,
            "temperature": settings.temperature,
            "timeout": settings.request_timeout,
            "api_key": settings.api_key,
        }
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        self._client = ChatOpenAI(**kwargs)

    def complete(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = self._client.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        content = getattr(resp, "content", resp)
        if isinstance(content, list):  # 多模态返回的情形
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        return str(content)


class MockLLM:
    """离线桩模型：不联网，按角色名返回结构化且确定性的结果。

    它并非"假数据"——它按 agent 角色产出符合真实 schema 的响应，
    使整条流水线（解析、工作区落盘、路由、循环收敛）都能被真实验证。
    """

    _ROLE_HINTS = ("RECONCILER", "PLANNER", "CODER", "ALIGNER", "SECURITY", "BUGFIXER")

    def complete(self, system: str, user: str) -> str:
        # 优先用机器可读的角色标记判定，避免关键词误判
        from .prompts import detect_role

        role = detect_role(system)
        if role is None:
            role = next((r for r in self._ROLE_HINTS if r in system.upper()), "CODER")

        requirement = self._extract_requirement(user)
        if role == "PLANNER":
            return self._mock_planner(requirement, user)
        if role == "RECONCILER":
            return self._mock_reconciler(requirement, user)
        if role == "CODER":
            return self._mock_coder(requirement, user)
        if role == "ALIGNER":
            return self._mock_aligner(requirement, user)
        if role == "SECURITY":
            return self._mock_security(requirement, user)
        if role == "BUGFIXER":
            return self._mock_bugfixer(requirement, user)
        return "{}"

    # -- helpers -- #
    @staticmethod
    def _mock_reconciler(requirement: str, user: str) -> str:
        """桩对账官：从提示词里数出机械信号条数，据此给结论。

        桩模型不做真裁决，但会**忠实反映输入信号的规模**，使上层流程
        （写报告、是否修补、是否 aligned）能被真实验证。
        """
        n_dangling = user.count("悬空 import")
        n_dup = user.count("重复定义")
        n_call = user.count("调用未导入")
        # 粗略估计信号条数（提示词里每条信号以 "  - " 开头）
        signal_lines = sum(1 for ln in user.splitlines() if ln.strip().startswith("- "))
        has_signals = "未发现机械可检的接口不一致" not in user

        findings = []
        if has_signals and signal_lines:
            findings.append({
                "id": "R1",
                "severity": "major",
                "type": "call_without_import",
                "is_false_positive": False,
                "description": "存在跨模块引用不一致，需补齐 import 或对齐签名",
                "evidence": "见机械信号列表",
                "fix": "在调用方补齐 import（最小改动）",
                "patch": [],
            })

        return json.dumps({
            "aligned": not findings,
            "consistency": 0.75 if findings else 0.95,
            "findings": findings,
            "false_positives": [],
            "summary": ("存在需处理的接口不一致" if findings else "各模块接口一致"),
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def _mock_planner(requirement: str, user: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", requirement.lower())[:24].strip("_") or "solution"
        module = f"{slug}.py"
        return json.dumps(
            {
                "understanding": f"把需求「{requirement}」拆解为一个可独立运行的实现模块，并配套单元测试。",
                "assumptions": [
                    "默认输入为 Python 原生数据结构，不依赖外部服务",
                    "模块放置于项目既有源码根目录下，遵循现有命名风格",
                ],
                "open_questions": [
                    "是否需要处理超大输入（流式）？当前按一次性载入实现",
                ],
                "steps": [
                    {"id": "S1", "goal": "实现核心处理函数", "detail": "在模块中实现入口函数与参数校验",
                     "files": [module], "verify": "函数可被 import 且基本调用不报错"},
                    {"id": "S2", "goal": "补充单元测试", "detail": "覆盖正常输入与边界输入",
                     "files": [f"test_{module}"], "verify": "测试全部通过"},
                ],
                "scope": {"files": [module, f"test_{module}"], "notes": "仅新增两个文件，不改动既有代码"},
                "out_of_scope": ["不引入第三方依赖", "不做性能优化"],
                "risks": [
                    {"severity": "low", "description": "输入类型未限定，可能需要调用方做前置校验",
                     "mitigation": "在函数内做类型与边界处理"},
                ],
                "test_plan": "运行新增的单元测试文件，确认用例全部通过",
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _extract_requirement(text: str) -> str:
        m = re.search(r"【需求】\s*(.+?)(?:\n【|\Z)", text, re.S)
        if m:
            return m.group(1).strip()
        return text.strip()[:200]

    @staticmethod
    def _mock_coder(requirement: str, user: str) -> str:
        # 已带真实文件内容 -> 说明"按需阅读"这一轮已完成，直接产出实现
        already_read = "你请求查看的既有文件内容" in user

        # 首次请求：若快照里存在可复用的模块，先要求读取它（模拟真实模型的按需行为）
        if not already_read:
            m = re.search(r"^\s*(\S+\.py)\s*$", user, re.M)
            if m and "文件树" in user:
                # 挑一个项目里真实存在的 .py，且不是自己将要产出的文件
                candidates = [
                    ln.strip() for ln in user.splitlines()
                    if ln.strip().endswith(".py") and "/" in ln.strip()
                ]
                if candidates:
                    return json.dumps({"need_files": candidates[:2], "files": [],
                                       "notes": "先阅读既有模块以确认接口，避免臆造"}, ensure_ascii=False)

        # 若已有反馈则产出修复版
        is_fix = "修复" in user or "BUG" in user.upper() or "SECURITY" in user.upper()
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", requirement.lower())[:24].strip("_") or "solution"
        module = f"{slug}.py"
        revision = "r2" if is_fix else "r1"
        code = (
            '"""自动生成模块：{req}"""\n'
            "from __future__ import annotations\n\n"
            "import math\n\n\n"
            "def solve(data):\n"
            '    """核心入口。\\n\\n'
            "    参数:\\n        data: 输入数据（dict 或可迭代对象）\\n"
            "    返回:\\n        处理结果\\n"
            '    """\n'
            "    if data is None:\n"
            "        raise ValueError('data 不能为空')\n"
            "    if isinstance(data, dict):\n"
            "        items = list(data.values())\n"
            "    else:\n"
            "        items = list(data)\n"
            "    if not items:\n"
            "        return 0\n"
            "    total = 0\n"
            "    for v in items:\n"
            "        total += float(v)\n"
            "    return total\n"
        )
        payload = {
            "need_files": [],
            "files": [
                {
                    "path": module,
                    "language": "python",
                    "content": code.format(req=requirement),
                    "description": f"实现「{requirement}」的核心逻辑",
                }
            ],
            "notes": f"已生成 {module}（{revision}），包含入参校验与空集合处理。",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _mock_aligner(requirement: str, user: str) -> str:
        return json.dumps(
            {
                "aligned": True,
                "coverage": [
                    {"requirement": requirement, "implemented": True, "evidence": "solve() 中实现核心求和逻辑"}
                ],
                "missing": [],
                "extra": [],
                "score": 0.82,
                "rationale": "代码实现了需求的核心路径，边界（None / 空集合）均有处理。",
                "issues": [
                    {
                        "severity": "minor",
                        "requirement": requirement,
                        "problem": "返回类型在混合输入时可能为 float，需求未明确是否允许",
                        "suggestion": "补充类型约定或在文档中说明",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _mock_security(requirement: str, user: str) -> str:
        return json.dumps(
            {
                "safe": True,
                "score": 0.9,
                "findings": [
                    {
                        "id": "SEC-EXC-001",
                        "severity": "info",
                        "category": "error-handling",
                        "description": "float() 转换在非数值输入时会抛 ValueError，属预期行为但建议上层捕获",
                        "location": "solve()",
                        "recommendation": "在调用侧增加 try/except 或改用数据校验库",
                    }
                ],
                "summary": "未发现命令注入、反序列化、路径穿越等高风险问题；数值转换异常需调用方处理。",
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _mock_bugfixer(requirement: str, user: str) -> str:
        payload = {
            "bugs": [],
            "patches": [],
            "notes": "对齐结果与安全扫描均未给出阻断性问题，本轮无需修补。",
            "resolved": True,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


def get_llm(settings: Settings | None = None) -> LLM:
    """返回可用的 LLM 客户端（真实或 Mock）。"""
    settings = settings or SETTINGS
    if settings.use_mock:
        return MockLLM()
    return RealLLM(settings)