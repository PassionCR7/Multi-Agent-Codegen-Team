"""计划授权（Mandate）与写前范围闸门。

借用 DeepSeek super-agent 的核心思想：
    * 用户批准的计划是不可变授权（Mandate），执行只受它约束；
    * 写文件之前先过代码闸门，而不是写完之后再检查；
    * 越界不是“提示词请求”，而是程序拒绝。

本模块只依赖标准库，避免和 LangGraph / Workspace 形成循环依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence


class MandateError(ValueError):
    """计划授权缺失、失效或被篡改。"""


# 参与指纹计算的字段：只包含“计划内容”，不包含 review_note / mandate_id 等
# 生命周期字段。这样计划从 draft 变成 approved 不会改变指纹。
DIGEST_FIELDS = (
    "requirement",
    "understanding",
    "assumptions",
    "open_questions",
    "steps",
    "scope",
    "out_of_scope",
    "risks",
    "test_plan",
    "revision",
    "source",
)

_STATUS_APPROVED = {"approved", "user_written"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def canonical_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """取计划中参与授权指纹的稳定子集。"""
    if not isinstance(plan, dict):
        return {}
    return {key: plan.get(key) for key in DIGEST_FIELDS if key in plan}


def plan_digest(plan: dict[str, Any]) -> str:
    """计算计划内容指纹。"""
    payload = json.dumps(
        canonical_plan(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normcase(value: str) -> str:
    """Windows 下大小写不敏感，其他平台保持原样；始终统一为正斜杠。"""
    return os.path.normcase(value).replace("\\", "/")


def normalize_rel_path(path: str) -> str:
    """归一化相对路径；拒绝绝对路径、空路径和 ``..``。

    这里刻意不使用 ``lstrip("./")``：那会把 ``../evil`` 变成 ``evil``，
    从而绕过范围检查。正确做法是逐段解析。
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise MandateError("路径为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise MandateError(f"不允许绝对路径: {path!r}")
    if "\x00" in raw:
        raise MandateError(f"路径包含非法字符: {path!r}")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise MandateError(f"路径不允许包含 '..': {path!r}")
        parts.append(part)
    if not parts:
        raise MandateError(f"路径为空: {path!r}")
    return "/".join(parts)


def scope_allows(scope: Sequence[str] | None, rel_path: str) -> bool:
    """判断产物路径是否落在计划白名单内。

    规则：
        * ``src/app.py``   —— 只允许这一个文件；
        * ``src/``         —— 允许 ``src/`` 下所有文件；
        * 空 scope         —— 不做限制（兼容旧计划）。
    """
    items = [str(item) for item in (scope or []) if str(item).strip()]
    if not items:
        return True

    try:
        rel_norm = normalize_rel_path(rel_path)
    except MandateError:
        return False
    rel_cmp = _normcase(rel_norm)

    for item in items:
        raw = item.strip().replace("\\", "/")
        is_dir = raw.endswith("/")
        try:
            item_norm = normalize_rel_path(raw)
        except MandateError:
            continue
        item_cmp = _normcase(item_norm)
        if is_dir:
            if rel_cmp == item_cmp or rel_cmp.startswith(item_cmp + "/"):
                return True
        elif rel_cmp == item_cmp:
            return True
    return False


def _normalize_scope(scope: Sequence[str] | None) -> tuple[str, ...]:
    """保存时保留目录的尾斜杠，便于区分“文件”和“目录”。"""
    out: list[str] = []
    for item in scope or []:
        raw = str(item).strip().replace("\\", "/")
        if not raw:
            continue
        is_dir = raw.endswith("/")
        norm = normalize_rel_path(raw)
        out.append(norm + ("/" if is_dir else ""))
    # 去重但保持顺序
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True)
class Mandate:
    """一份已批准计划的不可变授权。"""

    id: str
    plan_revision: int
    plan_digest: str
    scope_files: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    approved_at: str
    approved_by: str
    policy_version: str = "local-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "scope_files": list(self.scope_files),
            "out_of_scope": list(self.out_of_scope),
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Mandate":
        if not isinstance(data, dict) or not data:
            raise MandateError("缺少计划授权（mandate）")
        try:
            return cls(
                id=str(data.get("id") or ""),
                plan_revision=int(data.get("plan_revision") or 1),
                plan_digest=str(data.get("plan_digest") or ""),
                scope_files=tuple(str(x) for x in (data.get("scope_files") or [])),
                out_of_scope=tuple(str(x) for x in (data.get("out_of_scope") or [])),
                approved_at=str(data.get("approved_at") or ""),
                approved_by=str(data.get("approved_by") or ""),
                policy_version=str(data.get("policy_version") or "local-v1"),
            )
        except (TypeError, ValueError) as exc:
            raise MandateError(f"计划授权格式错误: {exc}") from exc


def build_mandate(plan: dict[str, Any], *, approved_by: str = "user") -> Mandate:
    """从一份已批准计划生成 Mandate。"""
    if not isinstance(plan, dict) or plan.get("status") not in _STATUS_APPROVED:
        raise MandateError("只有已批准的计划才能生成授权")

    scope = _normalize_scope(((plan.get("scope") or {}).get("files") or []))
    return Mandate(
        id=uuid.uuid4().hex[:12],
        plan_revision=int(plan.get("revision") or 1),
        plan_digest=plan_digest(plan),
        scope_files=scope,
        out_of_scope=tuple(str(x) for x in (plan.get("out_of_scope") or [])),
        approved_at=_now(),
        approved_by=approved_by,
        policy_version=os.getenv("CODER_POLICY_VERSION", "local-v1"),
    )


def verify_mandate(plan: dict[str, Any], mandate: dict[str, Any] | Mandate | None) -> Mandate:
    """校验计划仍与授权一致；不一致时抛出 ``MandateError``。"""
    m = mandate if isinstance(mandate, Mandate) else Mandate.from_dict(mandate)
    if not isinstance(plan, dict) or plan.get("status") not in _STATUS_APPROVED:
        raise MandateError("计划未处于已批准状态")

    current_revision = int(plan.get("revision") or 1)
    if current_revision != m.plan_revision:
        raise MandateError(
            f"计划版本已变化（授权 v{m.plan_revision}，当前 v{current_revision}）"
        )
    if not m.plan_digest or plan_digest(plan) != m.plan_digest:
        raise MandateError("已批准计划内容被修改，授权指纹不匹配")
    return m


@dataclass(frozen=True)
class ScopeGuard:
    """写前范围闸门。"""

    mandate: dict[str, Any]

    def check(self, rel_path: str) -> str:
        m = Mandate.from_dict(self.mandate)
        norm = normalize_rel_path(rel_path)
        if not scope_allows(m.scope_files, norm):
            raise MandateError(
                f"文件 {rel_path!r} 不在已批准计划范围内；"
                "请先回到计划阶段扩大范围并重新批准"
            )
        return norm
