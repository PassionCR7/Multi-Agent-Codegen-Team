"""通用工具：健壮的 LLM JSON 解析、计时、文本截断。"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中尽最大努力抽取出一个 JSON 对象。

    依次尝试：直接解析 -> 剥代码围栏 -> 截取第一个平衡的花括号块。
    失败时返回 {"_raw": 原文, "_parse_error": ...}，保证调用方永不崩溃。
    """
    if not text or not text.strip():
        return {"_raw": text, "_parse_error": "empty response"}

    candidates: list[str] = [text.strip()]

    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())

    balanced = _first_balanced_object(text)
    if balanced:
        candidates.append(balanced)

    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
            return {"_value": data}
        except json.JSONDecodeError:
            continue

    return {"_raw": text.strip()[:4000], "_parse_error": "no valid JSON object found"}


def _first_balanced_object(text: str) -> str | None:
    """扫描出第一个括号平衡的 {...} 片段（忽略字符串内的括号）。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


@contextmanager
def timed():
    """上下文管理器：yield 一个可读的耗时容器。"""
    box = {"ms": 0}

    class _Timer:
        @property
        def ms(self) -> int:
            return box["ms"]

    start = time.perf_counter()
    timer = _Timer()
    try:
        yield timer
    finally:
        box["ms"] = int((time.perf_counter() - start) * 1000)


def truncate(text: str, limit: int = 3000, suffix: str = "\n... (已截断)") -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + suffix


def safe_get(data: dict, key: str, default: Any = None) -> Any:
    value = data.get(key, default)
    return default if value is None else value


def severity_rank(sev: str) -> int:
    """把严重程度映射为可比较的数值，便于排序。"""
    return {
        "critical": 4,
        "high": 3,
        "major": 3,
        "medium": 2,
        "minor": 1,
        "low": 1,
        "info": 0,
    }.get(str(sev).lower(), 0)


BLOCKING_ALIGN_SEVERITIES = {"critical", "major"}
BLOCKING_SECURITY_SEVERITIES = {"critical", "high"}


def has_blocking_issues(alignment: dict, security: dict) -> tuple[bool, list[str]]:
    """判断对齐/安全报告中是否存在阻断性问题。"""
    reasons: list[str] = []

    for item in alignment.get("missing", []) or []:
        if severity_rank(item.get("severity", "")) >= 3:
            reasons.append(f"[对齐] {item.get('severity')}: {item.get('requirement', item)}")
    for item in alignment.get("issues", []) or []:
        if severity_rank(item.get("severity", "")) >= 3:
            reasons.append(f"[对齐] {item.get('severity')}: {item.get('problem', item)}")

    for finding in security.get("findings", []) or []:
        if severity_rank(finding.get("severity", "")) >= 3:
            reasons.append(f"[安全] {finding.get('severity')}: {finding.get('description', finding)}")

    # 显式否定也会阻断
    if alignment and alignment.get("aligned") is False and not reasons:
        reasons.append("[对齐] 未通过对齐检查但未列出具体缺项，需人工复核")

    return bool(reasons), reasons