"""接口一致性与跨模块对账。

## 为什么需要这个模块？

当一次任务要写多个**相互调用**的文件时，最容易出问题的不是单个文件写得好不好，
而是**它们之间的接口对不上**：A 定义了 `fetch_data()`，B 却调用 `get_data()`；
A 写 `from b import parse`，而 b 里根本没有 `parse`。

传统做法是让 LLM 通读所有代码去"找不一致"——它容易漏，而且每轮都要烧 token。
本模块的做法是：**先由机器提取机械信号，再把信号交给 LLM 裁决**。

    机器负责"事实比对"（正则可靠、零成本、可复现）
      ↓ 产出低噪信号
    LLM 负责"判断与裁决"（区分真不一致 vs 误报，决定怎么修）

分工的理由：找"名字对不上"是确定性工作，不需要智能；判断"这算不算问题、该怎么改"
才需要智能。让 LLM 干前者既贵又不可靠。

## 信号类型（刻意只保留低噪的三类）

1. dangling_import    —— import 了目标模块里并不存在的名字（强信号）
2. duplicate_def      —— 同名函数在多个模块被定义且签名不同（强信号）
3. call_without_import—— 调用了兄弟模块里定义的名字，却没在本模块 import

刻意**不**做"悬空函数调用"的全量检测：那会把语言内置函数、标准库、lambda 名
全都误报出来，噪音极大。只查"与本次生成的其他模块相关"的部分，噪音可控。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 语言关键字的定义 / import 模式
# --------------------------------------------------------------------------- #
PY_DEF = [
    re.compile(r"^[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", re.M),
    re.compile(r"^[ \t]*class\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?", re.M),
]
PY_TOP_CONST = re.compile(r"^([A-Za-z_]\w*)\s*=", re.M)
PY_IMPORT_FROM = re.compile(r"^[ \t]*from\s+([.\w]+)\s+import\s+([^\n#]+)", re.M)
PY_IMPORT_PLAIN = re.compile(r"^[ \t]*import\s+([.\w]+)", re.M)
PY_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

JS_DEF = [
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>"),
    re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)\s*(?:extends\s+[A-Za-z_$][\w$]*)?\s*\{"),
]
JS_IMPORT = re.compile(r"\bimport\s+(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))\s+from\s+['\"]([^'\"]+)['\"]")
JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

GO_DEF = [
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)\s*\(([^)]*)\)", re.M),
    re.compile(r"^type\s+([A-Z]\w*)\s+struct", re.M),
]
GO_CALL = re.compile(r"\b([A-Z]\w*)\s*\(")

# 常见语言内置 / 关键字 —— 用于降低噪音
PY_BUILTINS = {
    "print", "len", "range", "str", "int", "float", "list", "dict", "set", "tuple",
    "bool", "sum", "min", "max", "abs", "sorted", "enumerate", "zip", "isinstance",
    "getattr", "setattr", "hasattr", "open", "super", "type", "repr", "format",
    "map", "filter", "any", "all", "next", "iter", "id", "hash", "vars", "dir",
    "callable", "locals", "globals", "round", "divmod", "bytes", "bytearray",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError",
    "AttributeError", "OSError", "IOError", "NotImplementedError", "StopIteration",
    "self", "cls", "if", "for", "while", "return", "with", "assert", "lambda",
    "super", "property", "staticmethod", "classmethod", "dataclass", "field",
}
JS_BUILTINS = {
    "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    "require", "parseInt", "parseFloat", "String", "Number", "Boolean", "Array",
    "Object", "JSON", "Promise", "Math", "Date", "Error", "console", "setTimeout",
    "setInterval", "Symbol", "Map", "Set", "WeakMap", "RegExp",
}
GO_BUILTINS = {
    "make", "len", "cap", "append", "copy", "delete", "panic", "recover", "new",
    "print", "println", "error", "string", "int", "int64", "float64", "bool", "byte",
    "rune", "if", "for", "func", "return", "switch", "select", "go", "defer",
}


def _lang_family(language: str, path: str) -> str:
    lang = (language or "").lower()
    suffix = Path(path).suffix.lower()
    if lang in ("python",) or suffix == ".py":
        return "python"
    if lang in ("javascript", "typescript", "js", "ts") or suffix in (".js", ".jsx", ".ts", ".tsx", ".vue"):
        return "js"
    if lang in ("go",) or suffix == ".go":
        return "go"
    return "other"


# --------------------------------------------------------------------------- #
# 提取
# --------------------------------------------------------------------------- #
def extract_defined(content: str, path: str, language: str = "") -> list[dict[str, Any]]:
    """提取文件里定义的名字（函数 / 类 / 顶层常量）及其签名。

    注意 params 保留**原始形态**（含类型注解与默认值）——因为类型注解本身就是
    契约的一部分。比较签名时另用归一化版本，展示时保留原样。
    """
    family = _lang_family(language, path)
    out: list[dict[str, Any]] = []

    if family == "python":
        for pat, kind in ((PY_DEF[0], "func"), (PY_DEF[1], "class")):
            for m in pat.finditer(content or ""):
                params = (m.group(2) or "").strip()
                out.append({
                    "name": m.group(1), "kind": kind,
                    "params": _norm_params(params),          # 用于比较
                    "display": re.sub(r"\s+", " ", params),  # 用于展示（保留类型）
                    "line": content[: m.start()].count("\n") + 1, "path": path,
                })
        for m in PY_TOP_CONST.finditer(content or ""):
            name = m.group(1)
            if name.isupper() or name.startswith("_"):
                out.append({"name": name, "kind": "const", "params": "", "display": "",
                            "line": content[: m.start()].count("\n") + 1, "path": path})
    elif family == "js":
        for pat, kind in ((JS_DEF[0], "func"), (JS_DEF[1], "func"), (JS_DEF[2], "class")):
            for m in pat.finditer(content or ""):
                params = m.group(2) or "" if m.lastindex and m.lastindex >= 2 else ""
                out.append({
                    "name": m.group(1), "kind": kind,
                    "params": _norm_params(params), "display": re.sub(r"\s+", " ", params),
                    "line": content[: m.start()].count("\n") + 1, "path": path,
                })
    elif family == "go":
        for pat, kind in ((GO_DEF[0], "func"), (GO_DEF[1], "class")):
            for m in pat.finditer(content or ""):
                params = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                out.append({
                    "name": m.group(1), "kind": kind,
                    "params": _norm_params(params or ""), "display": re.sub(r"\s+", " ", params or ""),
                    "line": content[: m.start()].count("\n") + 1, "path": path,
                })

    # 去重（同名只留第一条）
    seen: set[str] = set()
    uniq = []
    for d in out:
        if d["name"] in seen:
            continue
        seen.add(d["name"])
        uniq.append(d)
    return uniq


def _norm_params(params: str) -> str:
    """归一化参数列表，便于比较（去空白、去类型注解差异）。"""
    params = re.sub(r"\s+", " ", params or "").strip()
    params = re.sub(r":\s*[^,=]+", "", params)      # Python 类型注解
    return params


def extract_imports(content: str, path: str, language: str = "") -> list[dict[str, Any]]:
    """提取 import 语句（模块 + 引入的名字）。"""
    family = _lang_family(language, path)
    out: list[dict[str, Any]] = []

    if family == "python":
        for m in PY_IMPORT_FROM.finditer(content or ""):
            module, names_raw = m.group(1), m.group(2)
            names = [n.strip().split(" as ")[0].strip() for n in names_raw.split(",") if n.strip()]
            names = [n.strip("()") for n in names if n.strip("()") != "*"]
            out.append({"module": module, "names": names, "raw": m.group(0).strip(),
                        "line": content[: m.start()].count("\n") + 1})
        for m in PY_IMPORT_PLAIN.finditer(content or ""):
            mod = m.group(1)
            if mod in ("os", "sys", "re", "json", "time", "math", "typing", "pathlib",
                       "collections", "datetime", "subprocess", "logging", "functools",
                       "itertools", "contextlib", "dataclasses", "abc", "enum", "random",
                       "hashlib", "base64", "urllib", "io", "csv", "asyncio", "threading"):
                continue
            out.append({"module": mod, "names": [], "raw": m.group(0).strip(),
                        "line": content[: m.start()].count("\n") + 1})
    elif family == "js":
        for m in JS_IMPORT.finditer(content or ""):
            names = []
            if m.group(1):
                names = [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",") if n.strip()]
            default = m.group(2)
            spec = m.group(3)
            out.append({"module": spec, "names": names, "default": default or "",
                        "raw": m.group(0).strip(), "line": content[: m.start()].count("\n") + 1})
    return out


def extract_calls(content: str, path: str, language: str = "") -> set[str]:
    """启发式提取被调用的名字。"""
    family = _lang_family(language, path)
    pat = {"python": PY_CALL, "js": JS_CALL, "go": GO_CALL}.get(family)
    if not pat:
        return set()
    builtins = {"python": PY_BUILTINS, "js": JS_BUILTINS, "go": GO_BUILTINS}.get(family, set())
    names = set()
    for m in pat.finditer(content or ""):
        n = m.group(1)
        if n in builtins or len(n) < 2:
            continue
        names.add(n)
    return names


# --------------------------------------------------------------------------- #
# 跨模块对账
# --------------------------------------------------------------------------- #
def _resolve_local_module(module_spec: str, importer_path: str, all_paths: list[str]) -> str | None:
    """把 import 的模块名解析成本次生成的某个文件路径。"""
    spec = (module_spec or "").strip()
    if not spec:
        return None
    # 只关心本地（相对或以本次生成模块为前缀的）导入
    candidates: list[str] = []

    base = spec.split("/")[-1]
    if spec.startswith("."):
        # 相对导入：from .b import x  ->  与导入者同目录的 b.py
        depth = len(spec) - len(spec.lstrip("."))
        name = spec[depth:]
        importer_dir = Path(importer_path).parent
        up = importer_dir
        for _ in range(max(depth - 1, 0)):
            up = up.parent
        if name:
            candidates.append((up / name).as_posix())
            candidates.append(f"{up.as_posix()}/{name}.py")
            candidates.append(f"{up.as_posix()}/{name}")
        else:
            # from . import x —— 报同目录的 __init__ 或同名文件，交给调用方处理
            candidates.append(importer_dir.as_posix())
    else:
        # 绝对导入：from pkg.b import x / from b import x
        if "/" in spec:
            candidates.append(spec)
            candidates.append(f"{spec}.py")
        else:
            candidates.append(f"{base}.py")
            candidates.append(base)

    for c in candidates:
        for p in all_paths:
            pn = p.replace("\\", "/")
            if pn == c or pn.endswith("/" + c) or Path(pn).stem == Path(c).stem:
                return p
    return None


def analyze_modules(modules: list[dict[str, Any]]) -> dict[str, Any]:
    """对本次生成的多个模块做跨模块一致性分析。

    Args:
        modules: [{"path":..., "content":..., "language":...}]

    Returns:
        信号集合（低噪），以及每个模块的定义清单。
    """
    all_paths = [m["path"] for m in modules]
    defined_by_module: dict[str, dict[str, dict]] = {}
    defined_index: dict[str, list[dict]] = {}   # name -> [定义项]

    for m in modules:
        defs = extract_defined(m.get("content", ""), m["path"], m.get("language", ""))
        defined_by_module[m["path"]] = {d["name"]: d for d in defs}
        for d in defs:
            defined_index.setdefault(d["name"], []).append(d)

    signals: dict[str, Any] = {
        "dangling_imports": [],
        "duplicate_defs": [],
        "call_without_import": [],
        "defined_by_module": {
            p: sorted(d.keys()) for p, d in defined_by_module.items()
        },
    }

    # --- 1) 悬空 import：import 了本地模块里不存在的名字 ---
    for m in modules:
        path = m["path"]
        for imp in extract_imports(m.get("content", ""), path, m.get("language", "")):
            target = _resolve_local_module(imp["module"], path, all_paths)
            if not target:
                continue
            target_defs = defined_by_module.get(target, {})
            for name in imp.get("names", []):
                if name and name not in target_defs:
                    signals["dangling_imports"].append({
                        "importer": path, "target": target, "name": name,
                        "line": imp.get("line"), "raw": imp.get("raw", ""),
                        "hint": f"{target} 中未定义 {name}（已定义: {sorted(target_defs)[:8]}）",
                    })

    # --- 2) 重复定义且签名不同 ---
    for name, defs in defined_index.items():
        if len(defs) < 2:
            continue
        sigs = {(d["kind"], d["params"]) for d in defs}
        if len(sigs) > 1:
            signals["duplicate_defs"].append({
                "name": name,
                "occurrences": [{"path": d["path"], "kind": d["kind"], "params": d["params"],
                                 "line": d["line"]} for d in defs],
                "hint": f"同名 {name} 在多个模块定义但签名不一致",
            })

    # --- 3) 调用兄弟模块的函数却没 import ---
    for m in modules:
        path = m["path"]
        own = set(defined_by_module.get(path, {}).keys())
        imported: set[str] = set()
        for imp in extract_imports(m.get("content", ""), path, m.get("language", "")):
            for n in imp.get("names", []):
                imported.add(n)
            if imp.get("default"):
                imported.add(imp["default"])

        for called in extract_calls(m.get("content", ""), path, m.get("language", "")):
            if called in own or called in imported:
                continue
            owners = [d["path"] for d in defined_index.get(called, []) if d["path"] != path]
            if owners:
                signals["call_without_import"].append({
                    "caller": path, "name": called, "defined_in": owners,
                    "hint": f"{path} 调用了 {called}（定义在 {owners}）但未 import",
                })

    signals["issue_count"] = (
        len(signals["dangling_imports"])
        + len(signals["duplicate_defs"])
        + len(signals["call_without_import"])
    )
    return signals


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_signatures(modules: list[dict[str, Any]], max_names: int = 20) -> str:
    """把已生成模块的接口签名渲染成给后续子任务阅读的文本。

    这是"顺序生成"能对齐的关键：后写的模块必须先看到已写好的真实签名，
    而不是让模型猜。
    """
    if not modules:
        return "（暂无已生成模块）"
    blocks = []
    for m in modules:
        defs = extract_defined(m.get("content", ""), m["path"], m.get("language", ""))
        if not defs:
            continue
        lines = [f"### {m['path']}"]
        for d in defs[:max_names]:
            sig = d.get("display") or d.get("params", "")
            if d["kind"] == "func":
                lines.append(f"  def {d['name']}({sig})")
            elif d["kind"] == "class":
                lines.append(f"  class {d['name']}({sig})")
            else:
                lines.append(f"  {d['name']} = <常量>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "（已生成模块未解析出可导出接口）"


def render_signals(signals: dict[str, Any]) -> str:
    """把对账信号渲染成给 LLM 裁决的文本。"""
    if not signals or signals.get("issue_count", 0) == 0:
        return "（未发现机械可检的接口不一致）"

    L: list[str] = []
    di = signals.get("dangling_imports") or []
    if di:
        L.append("【悬空 import】下列 import 指向的名字在目标模块中并不存在：")
        for x in di[:10]:
            L.append(f"  - {x['importer']} -> {x['target']} 的 `{x['name']}`（第 {x.get('line')} 行）")
            L.append(f"      原文: {x.get('raw')}")
    dd = signals.get("duplicate_defs") or []
    if dd:
        L.append("【重复定义且签名不一致】")
        for x in dd[:10]:
            L.append(f"  - `{x['name']}`:")
            for o in x["occurrences"]:
                L.append(f"      {o['path']}:{o['line']}  {o['kind']}({o['params']})")
    cw = signals.get("call_without_import") or []
    if cw:
        L.append("【调用未导入】")
        for x in cw[:10]:
            L.append(f"  - {x['caller']} 调用了 `{x['name']}`（定义在 {x['defined_in']}）但未 import")

    idx = signals.get("defined_by_module") or {}
    if idx:
        L.append("【各模块已定义的名字】")
        for p, names in idx.items():
            L.append(f"  - {p}: {names}")
    return "\n".join(L)