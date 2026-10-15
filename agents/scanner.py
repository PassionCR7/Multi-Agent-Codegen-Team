"""真实项目扫描器 —— 防跑偏的关键一环。

为什么需要它？多智能体最容易"跑偏"的方式，是凭想象写代码：幻觉出项目里
不存在的模块、引入没安装的依赖、违反既有目录约定。解决办法不是反复叮嘱
模型"要贴紧项目"，而是把项目的**客观事实**扫描成结构化快照，让每个 agent
在动手前都必须先读到这份事实。

快照内容：
    * 文件树（受忽略规则约束，控制体积）
    * 语言构成统计
    * 依赖清单（requirements.txt / pyproject.toml / package.json 等）
    * 既有测试文件
    * Git 状态（分支、最近提交、是否有未提交改动）
    * 探测到的项目约定（包管理、测试框架、源码根目录、命名风格）
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

# 扫描时忽略的目录/文件（同时用于体积控制与隐私保护）
IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "dist",
    "build", ".next", ".nuxt", "target", "coverage", ".tox", ".workbuddy",
    "_agent_workspace", ".egg-info",
}
IGNORE_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".lock", ".log",
                   ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".7z", ".mp4"}

LANG_BY_SUFFIX = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cs": "C#", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".sh": "Shell", ".sql": "SQL", ".vue": "Vue",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".md": "Markdown", ".json": "JSON",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".tf": "Terraform",
}

MANIFESTS = ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
             "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle"]
TEST_HINTS = ("test_", "_test.", ".test.", ".spec.", "tests", "spec")


class ProjectScanner:
    """扫描真实项目并产出可写入工作区的快照。"""

    def __init__(self, project_root: str | Path, max_files: int = 400) -> None:
        self.root = Path(project_root).resolve()
        self.max_files = max_files

    # ------------------------------------------------------------------ #
    def scan(self) -> dict:
        snapshot = {
            "project_root": str(self.root),
            "exists": self.root.exists(),
            "tree": self._tree(),
            "languages": self._languages(),
            "manifests": self._manifests(),
            "tests": self._tests(),
            "conventions": {},
            "git": self._git(),
            "totals": {},
        }
        snapshot["conventions"] = self._conventions(snapshot)
        snapshot["totals"] = {
            "files_scanned": len(snapshot["tree"]),
            "languages": len(snapshot["languages"]),
            "test_files": len(snapshot["tests"]),
        }
        return snapshot

    # ------------------------------------------------------------------ #
    def _iter_files(self):
        if not self.root.exists():
            return
        count = 0
        for path in sorted(self.root.rglob("*")):
            if count >= self.max_files:
                return
            if not path.is_file():
                continue
            parts = set(path.relative_to(self.root).parts)
            if parts & IGNORE_DIRS:
                continue
            if path.suffix.lower() in IGNORE_SUFFIXES:
                continue
            count += 1
            yield path

    def _tree(self) -> list[dict]:
        files = []
        for path in self._iter_files():
            rel = path.relative_to(self.root).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append({"path": rel, "size": size, "language": LANG_BY_SUFFIX.get(path.suffix.lower(), "Other")})
        return files

    def _languages(self) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for item in self._tree():
            counter[item["language"]] += 1
        return dict(counter.most_common())

    def _manifests(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for name in MANIFESTS:
            # 支持子目录中的清单（常见于 monorepo，取前 5 个）
            found = list(self.root.rglob(name))[:5]
            for path in found:
                if set(path.relative_to(self.root).parts) & IGNORE_DIRS:
                    continue
                rel = path.relative_to(self.root).as_posix()
                result[rel] = self._parse_manifest(path)
        return result

    @staticmethod
    def _parse_manifest(path: Path) -> object:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"error": "unreadable"}

        try:
            if path.name == "requirements.txt":
                deps = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
                return {"type": "requirements", "dependencies": deps[:80]}
            if path.name == "pyproject.toml":
                data = tomllib.loads(text)
                project = data.get("project", {})
                deps = list(project.get("dependencies", []))
                # poetry 风格
                deps += list(data.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys())
                return {
                    "type": "pyproject",
                    "name": project.get("name"),
                    "requires_python": project.get("requires-python"),
                    "dependencies": deps[:80],
                }
            if path.name == "package.json":
                data = json.loads(text)
                return {
                    "type": "package.json",
                    "name": data.get("name"),
                    "scripts": list(data.get("scripts", {}).keys()),
                    "dependencies": list(data.get("dependencies", {}).keys())[:80],
                    "devDependencies": list(data.get("devDependencies", {}).keys())[:80],
                }
            if path.name == "go.mod":
                modules = [ln.split()[0] for ln in text.splitlines() if "require" not in ln and "/" in ln]
                return {"type": "go.mod", "dependencies": modules[:80]}
            if path.name == "Cargo.toml":
                data = tomllib.loads(text)
                return {"type": "Cargo.toml", "name": data.get("package", {}).get("name"),
                        "dependencies": list(data.get("dependencies", {}).keys())[:80]}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"parse failed: {exc}"}
        return {"type": path.name, "raw_head": text[:400]}

    def _tests(self) -> list[str]:
        tests = []
        for item in self._tree():
            low = item["path"].lower()
            if any(h in low for h in TEST_HINTS) and item["language"] != "Markdown":
                tests.append(item["path"])
        return tests[:40]

    def _git(self) -> dict:
        def run(*args: str) -> str:
            try:
                out = subprocess.run(
                    ["git", *args], cwd=self.root, capture_output=True, text=True, timeout=10
                )
                return out.stdout.strip() if out.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                return ""

        if not (self.root / ".git").exists():
            return {"is_repo": False}
        return {
            "is_repo": True,
            "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "last_commit": run("log", "-1", "--pretty=%h %s"),
            "dirty": bool(run("status", "--porcelain")),
            "recent_commits": run("log", "-5", "--pretty=%h %ad %s", "--date=short").splitlines(),
        }

    def _conventions(self, snapshot: dict) -> dict:
        """从快照推断项目约定，供 coder 遵守。"""
        langs = snapshot.get("languages", {})
        manifests = snapshot.get("manifests", {})
        conv: dict[str, object] = {}

        if "Python" in langs:
            conv["primary_language"] = "Python"
            conv["package_manager"] = "pip/poetry"
            conv["test_framework"] = "pytest" if any("pytest" in str(m) for m in manifests.values()) else "unittest"
        elif "TypeScript" in langs or "JavaScript" in langs:
            conv["primary_language"] = "TypeScript/JavaScript"
            conv["package_manager"] = "npm/yarn/pnpm"
            conv["test_framework"] = "jest/vitest"
        elif "Go" in langs:
            conv["primary_language"] = "Go"
            conv["package_manager"] = "go modules"
            conv["test_framework"] = "go test"
        else:
            conv["primary_language"] = next(iter(langs), "未知")
            conv["package_manager"] = "未知"
            conv["test_framework"] = "未知"

        # 探测源码根目录
        roots = {"src", "lib", "app", "agents", "packages"}
        existing = {p.split("/")[0] for p in (i["path"] for i in snapshot.get("tree", [])) if p}
        conv["source_roots"] = sorted(roots & existing) or ["(项目根目录)"]
        conv["has_tests"] = bool(snapshot.get("tests"))
        return conv

    # ------------------------------------------------------------------ #
    @staticmethod
    def render_for_prompt(snapshot: dict, max_tree_lines: int = 60) -> str:
        """把快照压缩成给 LLM 阅读的文本（控制 token）。"""
        if not snapshot:
            return "（未扫描到项目信息）"
        lines = [
            f"项目根目录: {snapshot.get('project_root')}",
            f"语言构成: {json.dumps(snapshot.get('languages', {}), ensure_ascii=False)}",
            f"项目约定: {json.dumps(snapshot.get('conventions', {}), ensure_ascii=False)}",
        ]
        git = snapshot.get("git", {})
        if git.get("is_repo"):
            lines.append(
                f"Git: 分支={git.get('branch')} 最近提交={git.get('last_commit')} 有未提交改动={git.get('dirty')}"
            )
        manifests = snapshot.get("manifests", {})
        if manifests:
            lines.append("依赖清单:")
            for name, info in list(manifests.items())[:4]:
                lines.append(f"  - {name}: {json.dumps(info, ensure_ascii=False)[:300]}")
        tests = snapshot.get("tests", [])
        if tests:
            lines.append(f"既有测试({len(tests)}): {', '.join(tests[:8])}")
        tree = [i["path"] for i in snapshot.get("tree", [])]
        if tree:
            lines.append(f"文件树(前{max_tree_lines}项, 共{len(tree)}):")
            lines += [f"  {p}" for p in tree[:max_tree_lines]]
        return "\n".join(lines)