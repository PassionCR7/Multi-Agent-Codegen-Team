# 框架设计详解

> 本文档描述 LangGraph 多智能体代码生成框架的**内部设计**：每个决策为什么这么定、
> 代价是什么、以及已知局限。所有数字与结构均对照代码核实。
>
> 想快速上手请看 [README.md](README.md) / [RUNNING.md](RUNNING.md)；本文面向"要改它"的人。

---

## 1. 设计哲学：三个不可让渡的原则

这套框架的所有细节，都能追溯到三条原则。理解它们，比记住模块名重要。

### 原则一：决策权归代码，不归模型

模型负责"生成内容"，代码负责"决定流程"。具体落实为：

| 环节 | 谁决定 | 为什么 |
|---|---|---|
| 下一步走哪个节点 | `supervisor.decide()` 纯规则函数 | 路由出错会死循环/提前收尾；规则化后必然终止、可复现 |
| 产出哪些文件 | 计划的 `scope.files` 白名单 + `check_scope()` 程序比对 | 提示词是"请求"，程序比对才是"执法" |
| 能否读某个文件 | `SafeReader` | 安全边界必须在代码里，不能交给模型自觉 |
| 数"接口对不对" | `interface.py` 正则提取 | 事实比对不需要智能，LLM 反而会漏 |

**代价**：灵活性降低。模型无法"临机应变"多写一个文件——它会被告知越界，必须回到计划阶段。
**换来**：可解释、可复现、必然终止，且副作用可控。

### 原则二：事实从磁盘读，不从内存副本读

这条原则来自一次真实事故：早期实现用内存里的 `written`（写入时的副本）给子 agent 提供已完成模块的签名。但 `apply_patch` 会改动文件 → 内存副本**立刻过期** → 父节点拿着**过期签名**去指挥后续模块（状态漂移）。

现在所有"事实"（已完成签名、产物内容、进度）一律经 `Workspace.read_artifact()` 从磁盘实读，并汇总成 `state.md`。回归测试 `test_state_doc_no_drift` 锁死这一点。

### 原则三：机器负责事实，LLM 负责判断

跨模块对账是最典型的例子。找"名字对不上"是确定性工作，让 LLM 干既贵又不可靠（它会漏、会编）；而"这算不算问题、该怎么改"才需要智能。所以：

```
interface.py（正则）  ── 找出悬空 import / 重复定义 / 调用未导入
        ↓ 低噪信号
RECONCILER（LLM）     ── 裁决真伪、补充语义不一致、给最小修补
        ↓ patch
apply_patch（代码）   ── old→new 精确替换，要求唯一匹配
```

---

## 2. 模块地图

```
agents/                     约 4545 行
├── config.py      352   配置 + LLM 工厂（真实 / Mock 自动降级）
├── state.py       147   LangGraph 共享状态（26 个字段）
├── plan.py        292   Plan 数据模型 + 范围强约束（check_scope）
├── scanner.py     252   真实项目扫描 → project_snapshot.json
├── reader.py      237   安全文件读取器（按需阅读的边界）
├── interface.py   399   跨模块接口提取与一致性分析
├── workspace.py   592   共享工作区 + 共享状态文档
├── prompts.py     293   8 个提示词（7 个在用）
├── nodes.py       802   7 个节点实现 + 分段生成 + 对账
├── supervisor.py  146   确定性路由
├── graph.py        92   LangGraph 图装配
├── review.py      207   计划审核循环（Phase 0）
├── runner.py      225   执行器（Phase 1）
├── main.py        339   CLI
└── utils.py       139   JSON 健壮解析 / 计时 / 阻断判定
web/  server.py 457 + static/index.html   零依赖前端
tests/  约 1300 行，166 项断言
```

---

## 3. 数据层

### 3.1 共享状态（`AgentState`，26 字段）

状态是 agent 间的"信封"。设计上刻意把**持久化事实**与**流转决策**分开：

```python
# 输入（不可变）
requirement, acceptance, project_root, workspace_root, workspace_dir

# 流转控制
iteration, bugfix_rounds, plan_rounds, code_attempts, phase, next_agent

# 增量累积（用 operator.add reducer，节点只返回"新增部分"）
artifacts[], route_history[], feedback[], errors[]

# 各 agent 最新结论（覆盖式）
plan, alignment, security, bugfix, reconcile, coder_notes,
pending_scope_violations[], read_audit[]

# 输出
final_summary, success, abort_reason
```

**四个独立计数器**是防死循环的关键——它们各自熔断一类循环：

| 计数器 | 上限（默认） | 防的是什么 |
|---|---|---|
| `iteration` | 6 | 对齐/修复来回拉扯的总轮次 |
| `bugfix_rounds` | 4 | "永远修不好" |
| `plan_rounds` | 5 | 用户与 planner 反复拉扯 |
| `code_attempts` | 3 | coder 一直**交白卷**（iteration 不增长，只能靠它熔断） |

> ⚠️ **易踩的坑**：LangGraph 对**未在 `AgentState` 里声明的 key 不报错、直接丢弃**。
> 开发中 `reconcile` 字段就这样静默丢失过——函数正确返回了报告，却进不了状态。

### 3.2 共享工作区（`_agent_workspace/<任务名>/`）

```
task.md                 人类可读任务书 + 验收标准
status.json             机器可读实时状态（阶段/轮次/含 requirement）
messages.jsonl          黑板消息流（append-only）
runs.jsonl              运行日志（每步耗时与结果）
project_snapshot.json   真实项目快照（防跑偏输入）
state.md / state.json   ★ 共享状态文档（子 agent 的对齐依据）
artifacts/              产出的代码文件（真实项目零污染）
reports/                各类报告：plan_vN / alignment_iterN / security_iterN
                        / reconcile_rN / final_state
state/                  预留目录（当前未使用）
```

### 3.3 Plan（`plan.py`）—— 强约束的载体

Plan 是**数据结构**而非一段文本。理由：只有把文件清单变成程序可比的集合，"coder 只能按计划执行"才谈得上**执法**。

```python
{
  "understanding":  "需求复述，2-3 句",
  "assumptions":    ["需求未明说但必须做的假设"],
  "open_questions": ["需要用户明确的关键问题"],
  "steps": [{"id","goal","detail","files","verify"}],
  "scope": {"files": [...白名单...], "notes": ""},
  "out_of_scope":   ["明确不做的事"],
  "risks": [{"severity","description","mitigation"}],
  "test_plan":      "整体验证方式",
  "status":  draft|approved|rejected|user_written,
  "source":  llm|user,
  "revision": 1
}
```

`scope.files` 是**可被程序判定的白名单**，由 `check_scope()` 执法：

```
产物路径归一化（去 ./、反斜杠、大小写）
      ↓
在白名单内？ → 通过
计划写了目录（dir/）？ → 其下新增文件视为在范围内
否则 → 记入 violations → 注入对齐报告(major) → aligned=False → 最终判定失败
```

`assumptions` 与 `open_questions` 是 Plan 的**核心价值**：如果 Plan 只是复述需求，审核就是走过场。真正值得人看的是模型**没把握的地方**。实测中规划官对"手机号校验"主动提出了 5 个待确认问题（+86 前缀、号段严格度、放置位置、命名、返回值形式）。

---

## 4. 执行层

### 4.1 图结构：星型拓扑

```
          START
            │
      [supervisor] ◄──────────────────┐
            │  (条件边)               │
  ┌─────┬───┴───┬─────────┬────────┐  │
  ▼     ▼       ▼         ▼        ▼  │
[planner][coder][aligner][security][bugfixer]
  │     │       │         │        │  │
  └─────┴───────┴─────────┴────────┘  │
            │                         │
            └────(执行完统一回流)──────┘
            │
       (done / abort)
            ▼
           END
```

**为什么是星型？** supervisor 是唯一决策中枢，其余节点只做事、不决定走向。这避免了节点间隐式耦合导致的行为不一致。图是**静态**的——没有动态生成子图，`recursion_limit=100` 是最后一道物理防线。

### 4.2 路由规则（`decide()` 完整决策表）

按顺序短路判断，第一条命中即返回：

| # | 条件 | 去向 | 设计意图 |
|---|---|---|---|
| 0 | `iteration >= 6` | abort | 总轮次熔断 |
| 0.5 | 无已批准计划 | planner / abort | **计划闸门**；`plan_rounds>=5` 则 abort |
| 1 | 无代码产物 | coder / abort | **交白卷闸门**；`code_attempts>=3` 则 abort |
| 2 | `phase == "bugfixing"` | repair | **复检闸门**（见下） |
| 3 | 无对齐报告 | aligner | |
| 4 | 无安全报告 | security | |
| 5 | 存在阻断问题 | bugfixer / abort | `bugfix_rounds>=4` 则 abort |
| 6 | 全部通过 | done | |

**"复检闸门"是一处关键修复**：没有它，bugfixer 修完后旧的 alignment/security 报告**仍留在状态里**（问题条目没被清除），supervisor 会再次判定"存在阻断问题"→ 又跳回 bugfixer → **死循环直到被上限杀掉**。`repair` 节点的职责就是**清空过期结论**，让流水线重新走 `aligner → security`，从而真正验证补丁是否生效。

### 4.3 流式状态合并（`runner.py`）

```python
for mode, chunk in app.stream(state, stream_mode=["updates", "values"]):
    if mode == "updates":   # 本步是哪些节点、各自返回什么增量（用于事件）
    else:                   # values —— reducer 合并后的权威快照
```

**为什么必须两个都订阅？** 带 `operator.add` reducer 的列表字段，在 `updates` 里是节点的**原始返回值**。若某节点返回空列表（如 bugfixer 无补丁时返回 `artifacts: []`），直接用 `updates` 覆盖会把之前累积的产物**整体清空**。开发中真实踩到：修复阶段一来，产物数就归零，最终产物丢失。

---

## 5. Agent 详解

| 角色 | 提示词 | 输入 | 输出（状态增量） |
|---|---|---|---|
| PLANNER | `PLANNER` | 需求 + 快照 | `plan`（草稿）, `plan_rounds+1` |
| CODER | `CODER` / `SUBTASK_CODER` | 需求 + 快照 + 白名单 + 反馈 | `artifacts[]`, `read_audit`, `reconcile` |
| ALIGNER | `ALIGNER` | 需求 + **已批准计划** + 代码 | `alignment`, `feedback` |
| SECURITY | `SECURITY` | 需求 + 代码 | `security`, `feedback` |
| BUGFIXER | `BUGFIXER` | 阻断问题清单 | `bugfix`, `artifacts`（补丁） |
| REPAIR | — | — | 清空 `alignment`/`security`，轮次+1 |
| RECONCILER | `RECONCILER` | 机械信号 + 各模块代码 | `reconcile`, 修补 |

> `prompts.py` 里还有一个 `SUPERVISOR` 提示词，但**当前未启用**——路由是纯规则，不需要 LLM。它作为留档存在。

### 5.1 CODER 的三条分支

`coder_node` 是唯一有明显分支逻辑的节点：

```
获批计划里文件数 >= 2 且 <= 8，且非修订轮？
├─ 是 → ① 按模块分段生成（_generate_by_module）
│        └─ ② 跨模块对账（_reconcile_modules）
└─ 否 → 单次生成（整包产出，或修订模式只改指定问题）
```

**修订模式（`is_revision`）刻意不拆**：只针对反馈改指定问题，拆分反而引入不必要的往返。

### 5.2 按需阅读（`_reads_loop` + `reader.py`）

coder 若不确定既有模块的真实接口，可在 JSON 里返回 `need_files` 表达"我需要看这些文件"。

**关键：这不是 function calling。** 模型只能*表达需求*，读不读、怎么读由代码决定——决策权仍在代码手里。

```json
{ "need_files": ["agents/utils.py"], "files": [], "notes": "先确认既有接口" }
```

`reader.py` 的安全边界：

| 约束 | 实现 |
|---|---|
| 路径围栏 | 拒绝 `..` 穿越；解析后必须在项目根内 |
| 忽略规则 | 复用 `scanner.py` 的 `IGNORE_DIRS` / `IGNORE_SUFFIXES` |
| 体积限额 | 单文件 60KB / 900 行截断；单次会话总量 200KB 封顶 |
| 二进制嗅探 | 前 2048 字节含 NUL 即拒绝 |
| **密钥拦截** | `.env*` / `*.pem` / `*.key` / `id_rsa` 等**拒绝读取** |
| **内容脱敏** | 命中 `sk-` / `AKIA` / `LTAI` / `password=` 则掩码 |
| 审计 | 每次读取记入 `audit[]`，随产物落盘 |

> **为什么密钥拦截是硬需求**：工作区里可能引用了密钥（用户曾从别的项目 `.env` 取过 API Key）。若 coder 能把 `.env` 读进提示词，密钥就会**流向第三方模型端点**——这是数据外泄，不是理论风险。

### 5.3 分段生成：为什么是"顺序"而非"并行"

当模块**相互调用**时，后写模块必须先看到前一个已经落地的**真实签名**，否则只能靠猜——那正是接口对不上的根源。

```
子任务 1 (a.py)  →  写入 artifacts/
      ↓ build_state_doc() 从磁盘实读 a.py，提取真实签名
子任务 2 (b.py)  ←  提示词注入 a.py 的真实签名 + 完整模块清单 + 导入路径规范
      ↓
子任务 3 (c.py)  ←  注入 a.py、b.py 的真实签名
```

提示词包含四块约束：① 本次全部模块（你只负责一个）② 已完成模块的真实签名 ③ 待办模块（来自共享状态文档）④ **导入路径规范**（要求包内相对导入，防止裸 `from models import ...` 无法作为包成员导入）。

每轮开始前刷新 `state.md`，并明确告知子 agent 其路径——文档是"共同事实来源"，不是只听父节点转述。

### 5.4 跨模块对账（`interface.py` + `RECONCILER`）

**机器找信号**（刻意只保留三类低噪信号）：

| 信号 | 含义 |
|---|---|
| `dangling_imports` | import 了目标模块里并不存在的名字 |
| `duplicate_defs` | 同名函数在多个模块定义且**签名不同** |
| `call_without_import` | 调用了兄弟模块定义的名字，却没 import |

支持 Python / JS / Go；内置函数与标准库被过滤以控噪。**刻意不做全量"悬空调用"检测**——那会把语言内置、lambda 名全误报出来。

> 设计细节：签名相同则**不报**重复定义（避免噪音），只有签名不一致才报。

**LLM 裁决**：`RECONCILER` 逐条裁决真伪（被误报必须给理由），并补充机器查不出的**语义**不一致（类型约定、参数含义）。

**最小修补**：`apply_patch()` 要求 `old` 在文件中**唯一出现**，否则拒绝：

```python
{"path": "b.py", "patch": [{"old": "from a import fetch, parse",
                            "new": "from a import fetch\nfrom c import process"}]}
```

对账往往只需改一行 import。若强迫重写整个文件，既费 token，又有"顺手改坏别处"的风险。部分失败时明确报告，不静默——避免"看起来对账了其实没修"。

---

## 6. 人机协同：两段式流程

```
用户 prompt
    │
    ▼
┌────────────── 阶段一：计划审核（可反复迭代）──────────────┐
│  PLANNER 生成 Plan-Act 草稿（理解/假设/待确认问题/步骤/    │
│                          白名单/风险/验证方式）           │
│      ↓                                                 │
│  用户审核 —— 三个分支：                                   │
│    ① 批准        → 进入阶段二                            │
│    ② 打回        → 附理由，PLANNER 针对性重规划            │
│    ③ 用户自拟方案 → 结合"首次 prompt + 补充说明"重组，再确认 │
│  （循环，直到批准或达到 max_plan_rounds=5 熔断）           │
└───────────────────────────────────────────────────────┘
    │ 已批准计划（强约束依据）
    ▼
┌────────────── 阶段二：自动执行 ────────────────────────┐
│  CODER → ALIGNER → SECURITY → (BUGFIXER → REPAIR →) 循环 │
└───────────────────────────────────────────────────────┘
```

**为什么拆两段？** 计划确认需要人的介入（等待时间不可控），执行阶段是纯自动的。分开后，执行阶段可随时从已批准计划续跑（`--workspace <dir> --yes`）。

**reviewer 是可插拔回调**——同一套逻辑驱动 CLI（`InteractiveReviewer`）、CI（`AutoApproveReviewer`）与 Web（`WebReviewer`，用 `threading.Event` 让规划线程阻塞挂起等用户提交）。

**熔断**：`max_plan_rounds` 用尽仍未达成一致 → 工作区标记 `aborted`，如实交还人类。

---

## 7. 配置全表（22 项）

| 变量 | 默认 | 作用 |
|---|---|---|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `CODER_MODEL` | — / — / gpt-4o-mini | 任意 OpenAI 兼容端点 |
| `CODER_TEMPERATURE` | 0.2 | 低温度利于结构化输出 |
| `CODER_TIMEOUT` | 120 | 请求超时（秒） |
| `CODER_MOCK` | — | 置 1 强制离线 |
| `CODER_MAX_ITER` | 6 | 总迭代上限 |
| `CODER_MAX_BUGFIX` | 4 | 修复轮次上限 |
| `CODER_MAX_PLAN` | 5 | 计划重规划上限 |
| `CODER_MAX_CODE_TRIES` | 3 | coder 交白卷上限 |
| `CODER_SUBTASK_THRESHOLD` | 2 | 文件数达此值启用分段生成 |
| `CODER_SUBTASK_MAX` | 8 | 单任务最多模块数 |
| `CODER_RECONCILE_ROUNDS` | 1 | 对账修补轮次（当前为预留） |
| `CODER_MAX_READ` | 3 | 按需阅读追问轮次 |
| `CODER_READ_FILES` | 6 | 每轮最多读几个文件 |
| `CODER_READ_BYTES` / `CODER_READ_LINES` | 60000 / 900 | 单文件截断阈值 |
| `CODER_WORKSPACE` | `_agent_workspace` | 工作区目录名 |

> **模型选型经验**：本框架依赖模型**直接输出结构化 JSON**。带思考链（reasoning）的模型会占用 token 预算，可能导致 JSON 被截断。建议首选非思考模型（如 `deepseek-chat`）。

---

## 8. 测试与验证

166 项断言，三个专项自检 + 一个端到端测试：

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `tests/selftest.py` | 58 | 执行阶段：修复闭环、四类熔断、范围约束、reducer 回归、按需阅读、安全读取 |
| `tests/selftest_plan.py` | 33 | 计划阶段：三条审核路径、用户自拟、计划熔断 |
| `tests/selftest_multimodule.py` | 52 | 模块拆分、对账闭环、**共享状态文档与漂移回归**、修补安全性 |
| `tests/test_web_api.py` | 23 | Web API 端到端（含并发安全、路径穿越防护） |

**测试方法论**：用可编程桩 LLM（`ScriptedLLM` / 自定义 stub）精确控制模型返回，从而验证**流程逻辑**而非模型智力。离线可跑，`CODER_MOCK=1` 即全绿。

真实模型（DeepSeek）实测结论：
- 三模块互调任务中机械信号 **0 条**——顺序生成 + 签名注入确实对齐了接口；
- 跨模块导入正确使用包内相对导入（`from .geo import distance`），生成代码可作包成员导入，25 项测试全通过；
- 单文件修改任务中，模型读了真实 `utils.py`，写回后**原有 7 个函数全部保留、零回归**。

---

## 9. 已知局限（诚实清单）

| 局限 | 说明 |
|---|---|
| 强约束不覆盖"内容正确性" | `check_scope` 只管**文件路径**合规，管不了文件内容是否合理 |
| `apply_patch` 依赖唯一匹配 | 目标片段出现多次即拒绝；复杂重构需退回重写文件 |
| 重复定义检测会漏"同签名"情况 | 刻意控噪的代价；同签名重复定义不报 |
| 机器信号不做全量调用分析 | 跨语言、动态特性场景可能漏判 |
| 产出密钥无法在写盘时拦截 | `reader` 拦的是**读取**；若 coder 在产出代码里硬编码密钥，需靠 SECURITY 事后审计 |
| 模块依赖方向未建模 | 当前按**文件数**决定是否分段；双向依赖场景顺序生成也救不了 |
| 段落式生成不并行 | 顺序是接口对齐的代价；大规模任务吞吐受限 |
| `SUPERVISOR` 提示词未启用 | 路由为纯规则，提示词仅留档 |
| `CODER_RECONCILE_ROUNDS` 未生效 | 预留，当前对账只做一轮 |
| `state/` 目录预留给后续用途 | 当前未使用 |
| Web 端无鉴权 | 仅绑 `127.0.0.1`；**勿**暴露公网 |

---

## 10. 如何扩展

- **加一个 agent**：`nodes.py` 写节点 → `graph.py` 注册 → `supervisor.py` 加路由分支 → `state.py` 声明输出字段（**务必声明，否则静默丢弃**）。
- **换审核方式**：实现 `reviewer` 协议即可（如对接外部审批系统 API），无需改流程。
- **换持久化**：`build_graph(checkpointer=SqliteSaver(...))` 支持断点续跑与时间旅行。
- **接入 CI**：`run_team()` 返回 `success` / `workspace_dir`，可直接作门禁；计划环节用 `--auto-plan` 跳过。