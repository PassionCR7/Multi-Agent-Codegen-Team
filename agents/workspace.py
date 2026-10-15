"""共享工作区（黑board）。

这是整个架构的"防跑偏"核心：所有 agent 在行动前都读取这里的真实项目快照，
行动后把进展写回这里。任何人（包括人类）打开这个文件夹就能看到当前任务
处在什么阶段、每个 agent 说了什么、产出了什么文件。

目录结构::

    <workspace>/
        task.md              # 人类可读的任务书 + 验收标准
        status.json          # 机器可读的实时状态（阶段/轮次/路由历史）
        messages.jsonl       # 黑板消息流（append-only，各 agent 的发言）
        runs.jsonl           # 运行日志（append-only，每步耗时与结果摘要）
        project_snapshot.json# 真实项目结构快照（防跑偏的关键）
        artifacts/           # 各 agent 产出的代码文件
        reports/             # 对齐报告 / 安全报告 / 修复报告
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(name: str) -> str:
    """把任意字符串转为安全的文件名片段。"""
    name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", name).strip("._-")
    return name[:60] or "unnamed"


class Workspace:
    """本地共享工作区，所有 agent 的公共黑板。"""

    def __init__(self, root: str | os.PathLike[str], task_name: str = "task") -> None:
        self.root = Path(root).resolve()
        self.task_name = _safe_name(task_name)
        # 每个任务一个独立子目录，避免多次运行互相污染
        self.dir = self.root / self.task_name
        self.artifacts_dir = self.dir / "artifacts"
        self.reports_dir = self.dir / "reports"
        self.state_dir = self.dir / "state"
        self._ensure()

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dir(cls, directory: str | os.PathLike[str]) -> "Workspace":
        """从已存在的工作区目录重建对象（不做任务名二次加工）。

        LangGraph 节点之间只通过状态传递路径字符串，此方法用于在节点内部
        安全地"重新打开"同一个工作区，而不会因任务名拼接产生新目录。
        """
        directory = Path(directory).resolve()
        obj = cls.__new__(cls)
        obj.root = directory.parent
        obj.task_name = directory.name
        obj.dir = directory
        obj.artifacts_dir = directory / "artifacts"
        obj.reports_dir = directory / "reports"
        obj.state_dir = directory / "state"
        obj._ensure()
        return obj

    def _ensure(self) -> None:
        for d in (self.dir, self.artifacts_dir, self.reports_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        # 追加型文件：不存在则创建
        for f in ("messages.jsonl", "runs.jsonl"):
            (self.dir / f).touch(exist_ok=True)
        if not self.status_path.exists():
            self._write_json(
                self.status_path,
                {"task": self.task_name, "phase": "created", "iteration": 0, "history": [], "created_at": _now()},
            )

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def task_path(self) -> Path:
        return self.dir / "task.md"

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    @property
    def messages_path(self) -> Path:
        return self.dir / "messages.jsonl"

    @property
    def runs_path(self) -> Path:
        return self.dir / "runs.jsonl"

    @property
    def snapshot_path(self) -> Path:
        return self.dir / "project_snapshot.json"

    @property
    def state_json_path(self) -> Path:
        """共享状态文档（机器可读）。"""
        return self.dir / "state.json"

    @property
    def state_md_path(self) -> Path:
        """共享状态文档（人类/模型可读）。子 agent 可请求读取它。"""
        return self.dir / "state.md"

    def artifact_path(self, rel: str) -> Path:
        """定位产物文件，并强制其落在 artifacts/ 之内（防路径穿越）。"""
        rel = (rel or "").replace("\\", "/").lstrip("/")
        target = (self.artifacts_dir / rel).resolve()
        if not str(target).startswith(str(self.artifacts_dir)):
            raise ValueError(f"非法产物路径（越界）: {rel}")
        return target

    def report_path(self, name: str) -> Path:
        return self.reports_dir / (name if name.endswith(".json") else f"{name}.json")

    # ------------------------------------------------------------------ #
    # 读写原语
    # ------------------------------------------------------------------ #
    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _append_jsonl(path: Path, record: dict) -> None:
        # 用 open(...,'a') 是操作系统级追加，写入不会被并发覆盖
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ #
    # 对外 API
    # ------------------------------------------------------------------ #
    def init_task(self, requirement: str, acceptance: list[str] | None = None) -> None:
        """写入人类可读的任务书。"""
        acceptance = acceptance or [
            "代码可运行且无语法错误",
            "实现覆盖需求中的全部功能点",
            "无高危安全问题",
            "与真实项目既有约定/结构一致",
        ]
        lines = [
            f"# 任务书：{self.task_name}",
            "",
            f"- 创建时间：{_now()}",
            f"- 工作区：`{self.dir}`",
            "",
            "## 需求原文",
            "",
            "```",
            requirement.strip(),
            "```",
            "",
            "## 验收标准",
            "",
        ]
        lines += [f"{i}. {a}" for i, a in enumerate(acceptance, 1)]
        lines += [
            "",
            "## 协作说明",
            "",
            "本目录是多智能体共享黑板。阶段流转见 `status.json`，",
            "各 agent 发言见 `messages.jsonl`，运行日志见 `runs.jsonl`。",
            "任何 agent 在动手前必须先读取 `project_snapshot.json`，以确保不偏离真实项目。",
            "**`state.md` / `state.json` 是共享状态文档**：汇总当前进度、已批准计划、",
            "已完成模块的真实签名与历次校验结论。子 agent 应以此作为对齐依据。",
            "",
        ]
        self.task_path.write_text("\n".join(lines), encoding="utf-8")
        # 把需求也存进状态，供共享状态文档与各 agent 直接读取（不必再解析 task.md）
        self.update_status(requirement=requirement.strip())
        self.post_message("system", "任务已初始化，等待各 agent 认领。", kind="lifecycle")

    def read_status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"task": self.task_name, "phase": "unknown", "iteration": 0, "history": []}

    def update_status(self, **fields: Any) -> dict:
        status = self.read_status()
        status.update(fields)
        status["updated_at"] = _now()
        self._write_json(self.status_path, status)
        return status

    def record_turn(self, agent: str, phase: str, iteration: int, note: str = "") -> None:
        """记录一次路由流转，形成可回放的执行轨迹。"""
        status = self.read_status()
        history = status.get("history", [])
        history.append({"agent": agent, "phase": phase, "iteration": iteration, "note": note, "at": _now()})
        status.update({"history": history[-100:], "phase": phase, "iteration": iteration, "current_agent": agent})
        status["updated_at"] = _now()
        self._write_json(self.status_path, status)

    def post_message(self, agent: str, content: str, kind: str = "note", **extra: Any) -> None:
        """向黑板写入一条消息。"""
        self._append_jsonl(
            self.messages_path,
            {"ts": _now(), "agent": agent, "kind": kind, "content": content, **extra},
        )

    def log_run(self, agent: str, ok: bool, duration_ms: int, summary: str, **extra: Any) -> None:
        """写入运行日志。"""
        self._append_jsonl(
            self.runs_path,
            {
                "ts": _now(),
                "agent": agent,
                "ok": ok,
                "duration_ms": duration_ms,
                "summary": summary[:2000],
                **extra,
            },
        )

    def write_artifact(self, rel_path: str, content: str) -> Path:
        """把 agent 产出的代码写入 artifacts/。"""
        path = self.artifact_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.post_message(
            "coder",
            f"写入产物 `{path.relative_to(self.dir).as_posix()}`（{len(content)} 字符）",
            kind="artifact",
            path=str(path),
        )
        return path

    def write_report(self, name: str, data: dict) -> Path:
        """把结构化报告写入 reports/。"""
        path = self.report_path(name)
        self._write_json(path, data)
        return path

    def apply_patch(self, rel_path: str, patch: list[dict]) -> tuple[bool, str]:
        """对一个已存在的产物文件施加"最小替换"补丁。

        为什么需要它？跨模块对账往往只需改一行 import 或一个调用名。
        若强迫 LLM 重写整个文件，既费 token，又有"顺手改坏别处"的风险。
        这里用 old→new 的精确文本替换，改动面最小、可审计。

        Args:
            rel_path: 产物相对路径（必须已存在）。
            patch:    [{"old": "原文本（需唯一出现）", "new": "替换文本"}]

        Returns:
            (是否成功, 说明)
        """
        from .reader import SafeReader

        path = self.artifact_path(rel_path)
        if not path.exists():
            return False, f"文件不存在: {rel_path}"

        # 断言文件在项目内的安全性由 artifact_path 的围栏保证
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"读取失败: {exc}"

        text = original
        applied, failed = 0, []
        for i, hunk in enumerate(patch, 1):
            old = hunk.get("old", "")
            new = hunk.get("new", "")
            if not old:
                failed.append(f"#{i} 缺少 old 字段")
                continue
            count = text.count(old)
            if count == 0:
                failed.append(f"#{i} 未找到原文: {old[:40]!r}")
                continue
            if count > 1:
                failed.append(f"#{i} 原文出现 {count} 次（需唯一）: {old[:40]!r}")
                continue
            text = text.replace(old, new, 1)
            applied += 1

        if applied and text != original:
            path.write_text(text, encoding="utf-8")
            self.post_message("reconciler", f"对 {rel_path} 施加 {applied} 处修补",
                              kind="patch", path=rel_path, applied=applied)

        # 顺手做一次密钥扫描，防止补丁把密钥写进去
        from .reader import _redact
        _, leaked = _redact(text)
        if leaked:
            return applied > 0, f"应用 {applied} 处；⚠ 检测到疑似密钥（已记录，建议人工复核）"

        if failed:
            return applied > 0, f"应用 {applied} 处，失败 {len(failed)} 处: {'; '.join(failed)}"
        return applied > 0, f"应用 {applied} 处修补"

    def write_snapshot(self, snapshot: dict) -> Path:
        self._write_json(self.snapshot_path, snapshot)
        return self.snapshot_path

    def read_snapshot(self) -> dict:
        """读取真实项目快照（防跑偏的输入）。"""
        try:
            return json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------ #
    # 产物读取（共享状态文档的数据来源）
    # ------------------------------------------------------------------ #
    def read_artifact(self, rel_path: str) -> str:
        """读取**磁盘上**的产物内容（不是内存副本）。

        为什么强调"磁盘"？因为补丁会改动文件。若上层沿用写入时的内存副本，
        子 agent 拿到的就是**过期签名**——父节点会拿着旧事实去指挥后续模块。
        一切"已完成模块的接口"都必须从这里取，保证与磁盘一致。
        """
        p = self.artifact_path(rel_path)
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    def list_artifacts(self) -> list[str]:
        """列出 artifacts/ 下所有文件的相对路径。"""
        if not self.artifacts_dir.exists():
            return []
        out: list[str] = []
        for p in sorted(self.artifacts_dir.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                try:
                    out.append(p.relative_to(self.artifacts_dir).as_posix())
                except ValueError:
                    continue
        return out

    # ------------------------------------------------------------------ #
    # 共享状态文档（子 agent 的对齐依据）
    # ------------------------------------------------------------------ #
    def build_state_doc(self, *, progress: dict | None = None) -> dict:
        """构建"共享状态文档"：把散落的事实归拢成一份可供所有 agent 阅读的文档。

        为什么需要它？子 agent 若只看父节点临时拼的提示词，就相当于"只听转述"。
        这份文档把全部客观事实（需求 / 计划 / 真实项目约定 / 已完成模块的**真实签名** /
        未完成模块 / 待处理问题 / 历次对账结果）落到**磁盘上的单一文件**，任何
        子 agent 都可以自己去读，从而保证不同模块之间的代码对齐有共同依据。

        内容全部取自磁盘（产物、报告、状态），而非内存副本 —— 这样补丁改了文件后，
        文档会随之刷新，不会出现"父节点拿旧事实指挥子 agent"的漂移。

        Returns:
            结构化文档（同时写入 state.json / state.md）。
        """
        from .interface import extract_defined  # 延迟导入，避免模块级循环依赖

        status = self.read_status()
        plan: dict = {}
        plan_path = self.report_path("plan_approved")
        if plan_path.exists():
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                plan = {}

        # 已完成模块：从磁盘读取真实内容并提取真实签名
        signatures: list[dict] = []
        for rel in self.list_artifacts():
            content = self.read_artifact(rel)
            language = "python" if rel.endswith(".py") else ""
            defs = extract_defined(content, rel, language)
            if not defs:
                continue
            signatures.append({
                "path": rel,
                "defines": [
                    {"name": d["name"], "kind": d["kind"],
                     "signature": (d.get("display") or d.get("params") or "")}
                    for d in defs
                ],
            })

        reconcile: dict = {}
        recon_files = sorted(self.reports_dir.glob("reconcile_r*.json"))
        if recon_files:
            try:
                reconcile = json.loads(recon_files[-1].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reconcile = {}

        alignment: dict = {}
        align_files = sorted(self.reports_dir.glob("alignment_iter*.json"))
        if align_files:
            try:
                alignment = json.loads(align_files[-1].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                alignment = {}

        snapshot = self.read_snapshot()
        scope = (plan.get("scope") or {}).get("files") or []

        # 需求：优先状态里存的；否则回落到已批准计划里的 requirement
        requirement = status.get("requirement") or plan.get("requirement") or ""
        if not requirement:
            # 最后回落到任务书（首个代码块即为需求原文）
            try:
                md = self.task_path.read_text(encoding="utf-8")
                m = re.search(r"##\s*需求原文\s*```(.*?)```", md, re.S)
                if m:
                    requirement = m.group(1).strip()
            except (OSError, AttributeError):
                requirement = ""

        # 计划外产物：用**全部**产物路径判断，而不是"有可解析定义的产物"
        # （否则像 rogue.py 这种只有 `x=1` 的文件会被漏掉）
        all_artifacts = self.list_artifacts()
        done_paths = {s["path"] for s in signatures}
        pending = [f for f in scope if f not in done_paths] if scope else []
        extra = [p for p in all_artifacts if scope and p not in scope]

        doc = {
            "task": status.get("task_name") or self.dir.name,
            "requirement": requirement,
            "progress": progress or {},
            "status": {
                "phase": status.get("phase", ""),
                "iteration": status.get("iteration", 0),
                "plan_status": status.get("plan_status", ""),
            },
            "plan": {
                "revision": plan.get("revision"),
                "status": plan.get("status"),
                "steps": [s.get("goal") for s in (plan.get("steps") or [])],
                "scope_files": scope,
                "out_of_scope": plan.get("out_of_scope") or [],
            },
            "project_conventions": {
                "languages": snapshot.get("languages", {}),
                "conventions": snapshot.get("conventions", {}),
            },
            "artifacts": signatures,
            "pending_modules": pending,
            "extra_artifacts": extra,
            "reconcile": {
                "aligned": reconcile.get("aligned"),
                "consistency": reconcile.get("consistency"),
                "findings": [
                    {"type": f.get("type"), "severity": f.get("severity"),
                     "description": f.get("description")}
                    for f in (reconcile.get("findings") or [])
                ],
            } if reconcile else {},
            "alignment": {
                "aligned": alignment.get("aligned"),
                "missing": [m.get("requirement") for m in (alignment.get("missing") or [])],
                "issues": [
                    {"severity": i.get("severity"), "problem": i.get("problem")}
                    for i in (alignment.get("issues") or [])
                ],
            } if alignment else {},
        }

        self._write_json(self.state_json_path, doc)
        self.state_md_path.write_text(self._render_state_md(doc), encoding="utf-8")
        return doc

    @staticmethod
    def _render_state_md(doc: dict) -> str:
        """把状态文档渲染成人类与模型都易读的 Markdown。"""
        L: list[str] = ["# 任务状态文档（共享）", ""]
        L.append("> 本文件是**所有 agent 的共同事实来源**。子 agent 在动手前应以此为准；")
        L.append("> 如需查看全文，可把 `state.md` 放进 need_files 请求读取。")
        L.append("")

        if doc.get("requirement"):
            L += ["## 需求", "", doc["requirement"], ""]

        st = doc.get("status") or {}
        prog = doc.get("progress") or {}
        L += ["## 当前进度", ""]
        L.append(f"- 阶段: {st.get('phase', '?')}  轮次: {st.get('iteration', 0)}")
        if prog:
            L.append(f"- 分段进度: 已完成 {prog.get('done', 0)}/{prog.get('total', '?')} 个模块")
            if prog.get("current"):
                L.append(f"- 当前负责: {prog['current']}")
        L.append("")

        plan = doc.get("plan") or {}
        if plan.get("scope_files"):
            L += ["## 已批准计划", ""]
            if plan.get("revision"):
                L.append(f"- 版本: v{plan['revision']}（{plan.get('status', '')}）")
            if plan.get("steps"):
                L.append("- 步骤:")
                for s in plan["steps"]:
                    L.append(f"    - {s}")
            L.append(f"- 改动白名单: {', '.join(plan['scope_files'])}")
            if plan.get("out_of_scope"):
                L.append(f"- 明确不做: {', '.join(plan['out_of_scope'])}")
            L.append("")

        conv = doc.get("project_conventions") or {}
        if conv.get("languages"):
            L += ["## 真实项目约定", ""]
            L.append(f"- 语言构成: {conv['languages']}")
            for k, v in (conv.get("conventions") or {}).items():
                L.append(f"- {k}: {v}")
            L.append("")

        arts = doc.get("artifacts") or []
        if arts:
            L += ["## 已完成模块（真实签名，取自磁盘）", ""]
            for a in arts:
                L.append(f"### {a['path']}")
                for d in a["defines"]:
                    if d["kind"] == "func":
                        L.append(f"  def {d['name']}({d['signature']})")
                    elif d["kind"] == "class":
                        L.append(f"  class {d['name']}({d['signature']})")
                    else:
                        L.append(f"  {d['name']} = <常量>")
                L.append("")

        if doc.get("pending_modules"):
            L += ["## 尚未完成的模块", ""]
            for p in doc["pending_modules"]:
                L.append(f"- {p}")
            L.append("")

        if doc.get("extra_artifacts"):
            L += ["## ⚠ 计划外产物（不在白名单内）", ""]
            for p in doc["extra_artifacts"]:
                L.append(f"- {p}")
            L.append("")

        rec = doc.get("reconcile") or {}
        if rec:
            L += ["## 最近一次跨模块对账", ""]
            L.append(f"- 结论: aligned={rec.get('aligned')}  一致性={rec.get('consistency')}")
            for f in (rec.get("findings") or [])[:8]:
                L.append(f"    - [{f.get('severity')}] {f.get('type')}: {f.get('description')}")
            L.append("")

        al = doc.get("alignment") or {}
        if al:
            L += ["## 最近一次需求对齐", ""]
            L.append(f"- 结论: aligned={al.get('aligned')}")
            for m in (al.get("missing") or [])[:8]:
                L.append(f"    - 缺失: {m}")
            for i in (al.get("issues") or [])[:8]:
                L.append(f"    - [{i.get('severity')}] {i.get('problem')}")
            L.append("")

        return "\n".join(L).rstrip() + "\n"

    def tree(self) -> str:
        """给终端展示的工作区概览。"""
        rows = []
        for path in sorted(self.dir.rglob("*")):
            rel = path.relative_to(self.dir)
            depth = len(rel.parts) - 1
            size = f"  {path.stat().st_size:>7,}B" if path.is_file() else "/"
            rows.append(f"{'  ' * depth}{rel.name}{size}")
        return "\n".join(rows)

    def finalize(self, success: bool, summary: str) -> None:
        self.update_status(
            phase="completed" if success else "aborted",
            finished_at=_now(),
            success=success,
            summary=summary,
        )
        self.post_message("system", summary, kind="lifecycle", success=success)


def new_workspace(root: str | os.PathLike[str], requirement: str, task_name: str | None = None) -> Workspace:
    """便捷工厂：根据需求自动生成任务名并初始化工作区。"""
    if task_name is None:
        task_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{_safe_name(requirement[:24])}"
    ws = Workspace(root, task_name)
    ws.init_task(requirement)
    return ws