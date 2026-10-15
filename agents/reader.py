"""安全文件读取器 —— 给模型"按需阅读"真实项目文件的能力。

为什么需要这一层？coder 只拿到"文件树 + 依赖清单"摘要时，无法知道既有模块的
真实接口（函数签名、类结构、命名风格），于是容易臆造出项目里不存在的接口。

但"让模型读真实文件"本身是有风险的动作，必须有一道严格的安全边界：

    1. 路径围栏   —— 只能读项目根目录之内的文件（防目录穿越）
    2. 忽略规则   —— 复用扫描器的忽略清单（.git / node_modules / 二进制 等）
    3. 体积与行数 —— 单文件有上限，防止把整个仓库塞进上下文烧钱
    4. 密钥拦截   —— .env / *.pem / id_rsa 及疑似凭据内容**拒绝读取**
    5. 内容脱敏   —— 万一命中疑似密钥，做掩码而非原样外送

第 4、5 条尤其重要：这个架构的工作区里可能引用了密钥（用户曾从别的项目
`.env` 里取过 API Key），如果 coder 能把 `.env` 读进提示词，密钥就会流向
第三方模型端点。这是**数据外泄**，不是理论风险。
"""

from __future__ import annotations

import re
from pathlib import Path

from .scanner import IGNORE_DIRS, IGNORE_SUFFIXES

# 明确拒绝读取的敏感文件名 / 后缀（优先级高于一切）
DENY_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development", ".env.test",
    ".npmrc", ".pypirc", ".netrc", ".htpasswd", "credentials", "id_rsa",
    "id_ed25519", "id_ecdsa", ".git-credentials", "secrets.yaml", "secrets.yml",
    ".dockercfg", "config.json",
}
DENY_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk", ".asc"}

# 文件内容里疑似密钥的模式 —— 命中则对值做掩码
# 注意：内联标记 (?i) 只能出现在整个正则最开头，放在中间会报 PatternError；
# 需要局部忽略大小写时用作用域写法 (?i:...)。
SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),                                     # OpenAI/DeepSeek 风格
    re.compile(r"(AKIA[0-9A-Z]{12,})"),                                         # AWS AccessKey
    re.compile(r"((?i:api[_-]?key)\s*[=:]\s*)(\S{8,})"),                        # api_key = xxx
    re.compile(r"((?i:secret|token|passwd|password)\s*[=:]\s*)(\S{6,})"),       # secret/token/password
    re.compile(r"(LTAI[0-9A-Za-z]{8,})"),                                       # 阿里云 AccessKey
]

MAX_FILE_BYTES = 60_000     # 单文件体积上限
MAX_FILE_LINES = 900        # 单文件行数上限
MAX_TOTAL_BYTES = 200_000   # 单次读取总量上限


class ReadResult:
    """一次读取的结果。"""

    __slots__ = ("path", "ok", "content", "reason", "truncated", "redacted", "lines", "bytes")

    def __init__(self, path: str, ok: bool, content: str = "", reason: str = "",
                 truncated: bool = False, redacted: bool = False, lines: int = 0, bytes_: int = 0):
        self.path = path
        self.ok = ok
        self.content = content
        self.reason = reason
        self.truncated = truncated
        self.redacted = redacted
        self.lines = lines
        self.bytes = bytes_

    def to_dict(self) -> dict:
        return {
            "path": self.path, "ok": self.ok, "content": self.content, "reason": self.reason,
            "truncated": self.truncated, "redacted": self.redacted,
            "lines": self.lines, "bytes": self.bytes,
        }


def _is_denied(path: Path) -> str | None:
    """判断文件是否属于禁止读取的敏感文件。返回原因或 None。"""
    name = path.name.lower()
    if name in DENY_FILENAMES:
        return "敏感文件（可能含密钥），拒绝读取"
    if path.suffix.lower() in DENY_SUFFIXES:
        return "密钥/证书类文件，拒绝读取"
    # 任何 .env 变体
    if name.startswith(".env") or name.endswith(".env"):
        return "环境变量文件，拒绝读取"
    return None


def _redact(text: str) -> tuple[str, bool]:
    """对疑似密钥做掩码。返回 (处理后文本, 是否发生掩码)。"""
    hit = False

    def mask_value(m: re.Match) -> str:
        nonlocal hit
        hit = True
        # 有捕获组前缀的（如 "api_key = "）保留前缀，只掩码值
        if m.re.groups == 2:
            return m.group(1) + m.group(2)[:4] + "*" * 8
        return m.group(1)[:6] + "*" * 8

    for pat in SECRET_PATTERNS:
        text = pat.sub(mask_value, text)
    return text, hit


class SafeReader:
    """受约束的文件读取器。所有读取都必须经过它。"""

    def __init__(self, project_root: str | Path,
                 max_file_bytes: int = MAX_FILE_BYTES,
                 max_file_lines: int = MAX_FILE_LINES,
                 max_total_bytes: int = MAX_TOTAL_BYTES) -> None:
        self.root = Path(project_root).resolve()
        self.max_file_bytes = max_file_bytes
        self.max_file_lines = max_file_lines
        self.max_total_bytes = max_total_bytes
        self._used = 0            # 本次会话已读取的字节数
        self.audit: list[dict] = []   # 读取审计日志

    # ------------------------------------------------------------------ #
    def _resolve(self, rel: str) -> tuple[Path | None, str]:
        """把相对路径解析为项目内的绝对路径，并做围栏检查。"""
        rel = (rel or "").strip().replace("\\", "/").lstrip("/")
        if not rel:
            return None, "空路径"
        if ".." in Path(rel).parts:
            return None, "路径包含 .. （拒绝目录穿越）"

        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            return None, "路径越出项目根目录（拒绝）"

        if not target.exists():
            return None, "文件不存在"
        if target.is_dir():
            return None, "是目录，不是文件"
        if not target.is_file():
            return None, "不是普通文件"

        # 忽略清单（复用扫描器规则，保持口径一致）
        parts = set(target.relative_to(self.root).parts)
        if parts & IGNORE_DIRS:
            return None, "位于被忽略的目录（如 .git / node_modules）"
        if target.suffix.lower() in IGNORE_SUFFIXES:
            return None, "二进制或非文本文件"
        return target, ""

    # ------------------------------------------------------------------ #
    def read(self, rel_path: str) -> ReadResult:
        """读取一个文件（受全部安全约束）。"""
        target, err = self._resolve(rel_path)
        if target is None:
            res = ReadResult(rel_path, False, reason=err)
            self.audit.append({"path": rel_path, "ok": False, "reason": err})
            return res

        denied = _is_denied(target)
        if denied:
            res = ReadResult(rel_path, False, reason=denied)
            self.audit.append({"path": rel_path, "ok": False, "reason": denied})
            return res

        try:
            raw = target.read_bytes()
        except OSError as exc:
            res = ReadResult(rel_path, False, reason=f"读取失败: {exc}")
            self.audit.append({"path": rel_path, "ok": False, "reason": res.reason})
            return res

        # 二进制嗅探（含 NUL 字节）
        if b"\x00" in raw[:2048]:
            res = ReadResult(rel_path, False, reason="疑似二进制文件")
            self.audit.append({"path": rel_path, "ok": False, "reason": res.reason})
            return res

        # 总量限额
        if self._used + len(raw) > self.max_total_bytes:
            res = ReadResult(rel_path, False, reason="本次会话读取总量已达上限")
            self.audit.append({"path": rel_path, "ok": False, "reason": res.reason})
            return res

        text = raw.decode("utf-8", errors="replace")
        truncated = False
        lines = text.splitlines()

        # 行数限额
        if len(lines) > self.max_file_lines:
            lines = lines[: self.max_file_lines]
            text = "\n".join(lines)
            truncated = True

        # 体积限额
        if len(text) > self.max_file_bytes:
            text = text[: self.max_file_bytes]
            truncated = True

        if truncated:
            text += "\n... （内容已按安全限额截断）"

        text, redacted = _redact(text)
        self._used += len(raw)

        res = ReadResult(
            rel_path, True, content=text, truncated=truncated, redacted=redacted,
            lines=len(lines), bytes_=len(raw),
        )
        self.audit.append({
            "path": rel_path, "ok": True, "bytes": len(raw),
            "truncated": truncated, "redacted": redacted,
        })
        return res

    def read_many(self, paths: list[str], max_files: int = 8) -> list[ReadResult]:
        """按顺序读取多个文件，受单次数量上限约束。"""
        out: list[ReadResult] = []
        for p in paths[:max_files]:
            out.append(self.read(p))
        return out


def format_read_results(results: list[ReadResult]) -> str:
    """把读取结果渲染成给模型阅读的提示词片段。"""
    if not results:
        return "（未请求读取任何文件）"

    blocks: list[str] = []
    for r in results:
        if not r.ok:
            blocks.append(f"### {r.path}\n[无法读取] {r.reason}")
            continue
        flags = []
        if r.truncated:
            flags.append("已截断")
        if r.redacted:
            flags.append("含疑似密钥已脱敏")
        note = f" ({', '.join(flags)})" if flags else ""
        lang = Path(r.path).suffix.lstrip(".") or "text"
        blocks.append(f"### {r.path}{note}\n```{lang}\n{r.content}\n```")
    return "\n\n".join(blocks)