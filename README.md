# Multi-Agent for Planning · Coding · Test · Security

> 一个基于 **LangGraph** 的多智能体代码生成团队：先规划、人工确认，再分工编码，自动做需求对齐与安全审计，循环修复直到通过或熔断。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-green)](https://github.com/langchain-ai/langgraph)
[![License](https://img.shields.io/badge/License-MIT-yellow)](./LICENSE)

---

## 目录

- [一、这是什么](#一这是什么)
- [二、它解决什么问题](#二它解决什么问题)
- [三、架构设计](#三架构设计)
- [四、快速开始](#四快速开始)
- [五、三种运行方式](#五三种运行方式)
- [六、配置说明](#六配置说明)
- [七、代码结构](#七代码结构)
- [八、可调参数](#八可调参数)
- [九、运行自检](#九运行自检)
- [十、已知限制](#十已知限制)
- [十一、开源声明](#十一开源声明)

---

## 一、这是什么

给一句话需求，它替你走完「想清楚 → 写代码 → 自己检查」的全过程。但和常见的"AI 一把梭"不同，它有两个刻意的设计：

**1. 不直接开写，先出计划给你确认**

```
用户提出需求
     │
     ▼
┌──────────────────────────────────────────┐
│ 阶段一：计划审核（可反复迭代）              │
│                                          │
│  PLANNER 产出 Plan-Act 草稿：             │
│    · 需求理解   · 隐含假设                 │
│    · 待确认问题  · 执行步骤                │
│    · 改动范围白名单 · 风险 · 验证方式        │
│         │                                │
│         ▼                                │
│  你来审核 —— 三个选择：                     │
│    ① 批准            → 进入阶段二           │
│    ② 打回并说明理由    → 重新规划            │
│    ③ 自己写方案        → 框架据此重组计划     │
│  （循环直到批准，或达到重规划上限）           │
└──────────────────────────────────────────┘
     │ 已批准的计划（作为强约束）
     ▼
┌──────────────────────────────────────────┐
│ 阶段二：自动执行                           │
│  CODER → ALIGNER → SECURITY → BUGFIXER   │
└──────────────────────────────────────────┘
```

**2. 每个环节都有"事实"约束，不靠提示词反复叮嘱**

开工前扫描真实项目，产出一份客观快照（文件树、依赖、既有测试、目录约定），所有智能体动手前都必须读同一份快照。把"要贴紧项目"从一句叮嘱，变成必然读到的输入。

### 界面预览

**① 输入需求**（指定要操作的真实项目目录）

![起始页](web/preview/start.png)

**② 审核行动计划**（这是最值得停下来看的一步）

每个步骤都标明：涉及哪些文件、怎么验证。左侧"改动范围"是硬约束，右边的"待确认问题"列出了模型替你做的假设。

![计划审核](web/preview/plan-review.png)

**③ 执行过程与结果**

调度官的每一次路由、对齐与安全审计的结论、产出文件、校验报告，全部留痕。

![执行结果](web/preview/execution-result.png)

---

## 二、它解决什么问题

多智能体最危险的不是能力不足，而是**幻觉式漂移**——凭想象写出项目里不存在的模块、引入没装的依赖、违反既有目录结构。而一旦各智能体之间信息不同步，还会出现"你以为我改了、我以为你改了"的错位。

本项目用五个结构性机制来压制它：

| 机制 | 做法 |
| --- | --- |
| **防跑偏快照** | 扫描真实项目生成 `project_snapshot.json`，所有智能体动手前必读 |
| **按需阅读** | CODER 可主动请求读取真实文件，而不是全量塞进上下文（含围栏、拦截、脱敏边界） |
| **计划强约束** | 计划里的"改动范围白名单"是硬约束，越界写入会被拦截 |
| **模块拆分 + 跨模块对账** | 文件较多时按模块分段生成，再统一对账，避免模块间接口不一致 |
| **共享工作区（黑板）** | 所有消息、报告、产物落盘，全程可回看、可复现 |

---

## 三、架构设计

### 3.1 图结构

基于 LangGraph 的中心化编排：所有节点执行完都回到 `supervisor`，由它决定下一步去哪。

```
                    ┌──────────────┐
        START ─────►│  supervisor  │◄──────────────┐
                    │  （调度官）    │               │
                    └──────┬───────┘               │
                           │ 依据状态决定路由         │
        ┌──────────┬───────┼───────┬──────────┐    │
        ▼          ▼       ▼       ▼          ▼    │
   ┌────────┐ ┌────────┐ ┌───────┐ ┌────────┐ ┌─────────┐
   │planner │ │ coder  │ │aligner│ │security│ │bugfixer │
   │ 规划   │ │ 编码   │ │需求对齐│ │ 安全审计│ │ 缺陷修复 │
   └───┬────┘ └───┬────┘ └───┬───┘ └───┬────┘ └────┬────┘
       │          │          │         │           │
       └──────────┴──────────┴─────────┴───────────┘
                           │
                           ▼
                      ┌────────┐
                      │ repair │  修复后复检
                      └───┬────┘
                          └──────────────► 回到 supervisor
```

调度官可路由到的目标及其含义：

| 路由 | 含义 |
| --- | --- |
| `planner` | 制定/重规划行动计划 |
| `coder` | 编码 |
| `aligner` | 需求对齐检查 |
| `security` | 安全审计 |
| `bugfixer` | 缺陷修复 |
| `repair` | 修复后复检 |
| `done` | 任务完成 |
| `abort` | 中止（触发熔断，交还人类） |

路由由**确定性规则**决定（读取共享状态判断），而不是让模型自由发挥——这样行为可预测、可测试。

### 3.2 智能体分工

| 智能体 | 职责 |
| --- | --- |
| **Supervisor** | 中心调度，读取共享状态决定下一个执行者；负责熔断判断 |
| **Planner** | 生成 Plan-Act：需求理解、假设、待确认问题、步骤、改动白名单、风险 |
| **Coder** | 按已批准计划编码，可主动请求读取真实项目文件 |
| **Aligner** | 检查产出是否真正满足需求，给出对齐评分 |
| **Security** | 安全审计，识别注入、危险调用、敏感信息泄漏等问题 |
| **Bugfixer** | 依据对齐/安全报告修复缺陷 |
| **Repair** | 修复后的复检，确认问题真的解决了 |

### 3.3 为什么要"待确认问题"

如果计划只是把需求复述一遍，审核就成了走过场。真正值得人看的是模型**没把握的地方**——它会怎么理解、替你做了哪些决定、有哪些歧义。

实测中，规划官对一个"手机号校验"需求主动提出了 5 个待确认问题（是否支持 `+86` 前缀、号段校验严格程度、函数放在哪、命名、返回值形式）。这些恰恰是返工的高发点。

### 3.4 熔断保护

多智能体互推容易陷入死循环，因此有多重上限：

- `CODER_MAX_ITER` —— 整体迭代轮次上限
- `CODER_MAX_BUGFIX` —— 缺陷修复轮次上限
- `CODER_MAX_PLAN` —— 计划重规划轮次上限（防止用户与 planner 反复拉扯）
- `CODER_MAX_CODE_TRIES` —— 编码连续无产出上限（兜底"一直交白卷"）

任一路径触顶即路由到 `abort`，把控制权交还给人，而不是无限重试烧 token。

### 3.5 计划授权（Mandate）与写前范围闸门

计划批准不是"记一个 status"，而是生成一份**不可变授权**：

- 批准时计算计划内容指纹，生成 `Mandate`（计划版本、指纹、白名单、批准人、时间）；
- 执行前 `supervisor` 校验指纹；计划被改动、版本不一致或缺少授权，直接 `abort`；
- coder / bugfixer / 对账补丁在**写盘前**检查白名单：
  - `src/app.py` → 只允许这一个文件；
  - `src/` → 允许目录下所有文件；
  - 绝对路径、`..`、空路径一律拒绝；
- 越界文件不会写入 `artifacts/`，而是留档到 `reports/scope_rejected/`，并生成一个"待你决定"事项。

这比"先写出来再检查"更硬：提示词可以请求，代码闸门才负责拒绝。

### 3.6 Ticket 生命周期与"待你决定"

每个任务现在有一块面向人的状态牌：

```
plan_review → queued → working
                         ├─ awaiting_answer   （有具体问题等你回答）
                         ├─ awaiting_review   （产出完成，等你验收）
                         ├─ blocked / failed
                         └─ done / aborted
```

失败、卡住、越界时，系统只问**一个具体问题**，并给 2～3 个按钮（例如"纳入范围并继续 / 终止任务"）。
Web 页面顶部会优先显示"待你决定"，而不是让你从日志里猜现在该干什么。

---

## 四、快速开始

### 4.1 环境要求

- **Python 3.10+**
- 一个 OpenAI 兼容的大模型端点（OpenAI / DeepSeek / 通义千问 / 本地 vLLM 均可）
- 无需 API Key 也能试跑（见下方 Mock 模式）

### 4.2 安装

```bash
# 1) 克隆项目
git clone https://github.com/PassionCR7/multi-agent-codegen-team.git
cd multi-agent-codegen-team

# 2) 创建虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3) 安装依赖
pip install -r requirements.txt

# 4) 准备配置
cp .env.example .env
# 然后用编辑器打开 .env，填入你的密钥与端点
```

### 4.3 配置密钥

打开 `.env`，填入你的信息：

```ini
# 此处填写你的大模型 API 账户密钥
OPENAI_API_KEY=sk-your-api-key-here

# 此处填写你的大模型服务地址
# DeepSeek:  https://api.deepseek.com/v1
# OpenAI:    https://api.openai.com/v1
# 本地 vLLM: http://127.0.0.1:8000/v1
OPENAI_BASE_URL=https://api.deepseek.com/v1

# 使用的模型名
CODER_MODEL=deepseek-chat
```

> ⚠️ **请勿把填好真实密钥的 `.env` 提交到 Git**。`.gitignore` 已默认排除它。

---

## 五、四种运行方式

### 方式一：Windows 桌面版（推荐）

独立窗口启动，自带文件夹选择、打开工作区、模型设置和系统通知。

```powershell
# 开发模式直接启动
pip install -r requirements-desktop.txt
python -m desktop.main

# 打开 WebView 调试工具
python -m desktop.main --debug

# 打包成 exe
.\packaging\build_windows.ps1
```

打包输出：

```
dist\MultiAgentCodegenTeam\MultiAgentCodegenTeam.exe
```

桌面版默认把配置、日志和任务工作区放在：

```
%APPDATA%\MultiAgentCodegenTeam\
├── config.json        # 窗口、最近项目、模型设置
├── logs\
│   └── desktop.log    # 桌面端日志
└── workspaces\        # 任务工作区
```

关闭窗口时如果还有任务在规划/执行，会先弹确认框。

> API Key 会保存在 `%APPDATA%\MultiAgentCodegenTeam\config.json`，仅限本机使用；如果你更习惯环境变量，也可以继续用 `.env` / 系统环境变量。

### 方式二：Web 界面（浏览器调试）

图形化审核计划，最直观。

```bash
python web/server.py                # 默认 http://127.0.0.1:8765
python web/server.py --port 9000    # 换端口
```

浏览器打开后：输入需求 → 查看计划 → 批准 / 打回 / 自拟 → 观看执行过程。

**支持的接口**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建任务（后台开始规划） |
| `GET` | `/api/tasks/<id>` | 查询任务快照（前端轮询） |
| `GET` | `/api/health` | 健康检查（桌面启动器就绪探测） |
| `POST` | `/api/tasks/<id>/review` | 提交审核决策 |
| `POST` | `/api/tasks/<id>/attention` | 回答"待你决定"的一个具体问题 |
| `GET` | `/api/tasks/<id>/messages` | 读取黑板消息流 |
| `GET` | `/api/settings` | 读取模型设置（不返回 API Key 明文） |
| `POST` | `/api/settings` | 更新 API Key / Base URL / 模型 / Mock |

刷新页面不会丢进度：`http://127.0.0.1:8765/?task=<id>` 可直接打开指定任务。

### 方式三：命令行交互式

同样支持计划审核，会在终端问你。

```bash
python -m agents.main "你的需求" --project .
```

会弹出选择：

```
1) 批准    2) 打回重规划    3) 我自己写方案
```

### 方式四：全自动（CI / 脚本）

```bash
# 自动批准首版计划，一条命令跑完
python -m agents.main "你的需求" --project . --auto-plan

# 只出计划不执行（把计划交给别人评审）
python -m agents.main "重构日志模块" --project . --plan-only
```

### 常用参数

| 参数 | 作用 |
| --- | --- |
| `--project <路径>` | 真实项目根目录（智能体会扫描它以免跑偏），默认当前目录 |
| `--task-name <名字>` | 指定工作区子目录名，便于回看 |
| `--workspace <目录>` | 复用已有工作区（配合 `--yes` 直接执行） |
| `--workspace-root <目录>` | 工作区的上级目录 |
| `--yes` | 跳过人工审核，自动批准计划 |
| `--auto-plan` | 自动批准首版计划（适合 CI） |
| `--plan-only` | 只生成并确认计划，不执行编码 |
| `--quiet` | 自动模式下不打印完整计划 |
| `--json` | 以 JSON 输出最终结果摘要（便于脚本解析） |
| `--graph` | 打印图结构（mermaid）后退出 |
| `--inspect <工作区>` | 查看某次任务的状态与已批准计划 |

### 离线试跑（不需要任何密钥）

想先看看它长什么样？用 Mock 模式即可跑通全流程：

```bash
CODER_MOCK=1 python -m agents.main "实现加法和减法" --project . --auto-plan
```

---

## 六、配置说明

全部配置通过 `.env` 或环境变量提供。完整字段见 [`.env.example`](./.env.example)。

### LLM 相关

> 桌面版可以直接在窗口右上角“设置”里修改 API Key / Base URL / 模型，保存到 AppData；`.env` 主要用于浏览器模式和 CLI。

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 你的大模型 API 密钥 | `sk-...` |
| `OPENAI_BASE_URL` | 服务端点地址 | `https://api.deepseek.com/v1` |
| `CODER_MODEL` | 模型名 | `deepseek-chat` |
| `CODER_TEMPERATURE` | 采样温度 | `0.2` |
| `CODER_TIMEOUT` | 单次请求超时（秒） | `120` |
| `CODER_MOCK` | 置 `1` 强制离线 Mock 模式 | 空 / `1` |

### 工作区

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `CODER_WORKSPACE` | 任务产物存放目录 | `_agent_workspace` |

---

## 七、代码结构

```
Multi-Agent_for_Planning-Coding-Test-Security/
├── run.py                    便捷入口（等价于 python -m agents.main）
├── requirements.txt
├── .env.example              配置模板（复制为 .env 后填写）
├── .gitignore
├── LICENSE
├── README.md                 本文件：概览、架构、使用
├── DESIGN.md                 内部设计详解（数据层 / 路由决策表 / 已知局限）
├── RUNNING.md                运行方式与踩坑排查
│
├── desktop/                  Windows 桌面外壳（pywebview）
│   ├── main.py               单实例 → 后端 → 原生窗口
│   ├── backend.py            动态端口 / 健康检查 / 优雅关闭
│   ├── native_api.py         文件夹选择 / 打开目录 / 系统通知
│   ├── config.py             AppData 配置 / 工作区 / 窗口几何
│   ├── single_instance.py    单实例锁
│   └── resources/            应用图标
│
├── packaging/                 PyInstaller / Inno Setup 打包
│   ├── agent-team.spec
│   ├── build_windows.ps1
│   └── installer/windows.iss
│
├── requirements-desktop.txt  桌面运行依赖
├── requirements-build.txt    打包依赖
│
├── agents/                   核心实现
│   ├── main.py               CLI 入口与参数解析（含交互式计划审核）
│   ├── graph.py              LangGraph 图定义与组装
│   ├── supervisor.py         调度官：确定性路由规则与计划闸门
│   ├── nodes.py              各智能体节点实现（planner + 业务 agent + repair）
│   ├── plan.py               Plan 数据模型与范围校验（强约束的执法点）
│   ├── mandate.py            计划授权指纹 + 写前范围闸门
│   ├── lifecycle.py          Ticket 阶段 / 报告 / 待办（Attention）
│   ├── review.py             计划审核循环（可插拔 reviewer）
│   ├── prompts.py            各角色提示词（含机器可读角色标记）
│   ├── scanner.py            真实项目扫描 → project_snapshot.json
│   ├── reader.py             安全文件读取器（按需阅读的边界：围栏/拦截/脱敏）
│   ├── runner.py             执行阶段运行器（流式执行）
│   ├── workspace.py          共享工作区（黑板）读写
│   ├── state.py              共享状态与 reducer
│   ├── interface.py          跨模块接口提取与一致性分析
│   ├── config.py             配置与 LLM 工厂（真实 / Mock 自动降级）
│   └── utils.py              通用工具
│
├── web/                      Web 界面
│   ├── server.py             零依赖 HTTP 服务 + REST API（stdlib http.server）
│   ├── static/index.html     单文件前端（无构建步骤）
│   └── preview/              界面截图
│
└── tests/                    自检脚本
    ├── selftest.py                 执行阶段自检（58 项：含按需阅读与熔断）
    ├── selftest_plan.py            计划阶段自检（33 项）
    ├── selftest_multimodule.py     模块拆分、对账与共享状态文档自检（52 项）
    ├── selftest_mandate.py         Mandate / 写前闸门 / 生命周期自检（41 项）
    ├── test_desktop_backend.py     桌面后端自检（27 项）
    └── test_web_api.py             Web API 端到端测试（38 项，需先启动服务）
```

### 任务工作区结构

每次任务会在 `_agent_workspace/<任务名>/` 下落盘全过程：

```
_agent_workspace/<任务名>/
├── task.md                   任务说明
├── project_snapshot.json     开工前的项目快照
├── messages.jsonl            黑板消息流（全部智能体往来）
├── runs.jsonl                各节点执行记录
├── artifacts/                产出的代码文件
├── reports/                  报告（见下）
└── status.json               当前状态快照（前端轮询用）
```

`reports/` 下保留完整痕迹，便于回溯"为什么最终是这个计划"：

```
reports/
├── plan_v1.json / plan_v1.md             每版计划（草稿）
├── plan_v1_rejected.json                 被打回的版本（含用户理由）
├── plan_v2.json / plan_v2.md
├── plan_v2_approved.json                 获批的版本
├── plan_approved.json / plan_approved.md 固定文件名，供 --workspace --yes 复用
├── mandate_approved.json                 计划授权指纹（不可变）
├── scope_rejected/                       被范围闸门拒绝的越界产物留档
├── alignment_iter1.json                  需求对齐报告
├── security_iter1.json                   安全审计报告
└── final_state.json                      最终状态汇总
```

---

## 八、可调参数

除 LLM 配置外，以下参数用于控制行为边界：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `CODER_MAX_ITER` | 整体迭代轮次上限，超出即熔断 | `6` |
| `CODER_MAX_BUGFIX` | 缺陷修复轮次上限，防止修不动的死循环 | `4` |
| `CODER_MAX_PLAN` | 计划重规划轮次上限，防止用户与 planner 反复拉扯 | `5` |
| `CODER_MAX_CODE_TRIES` | 编码连续无产出的上限（交白卷兜底） | `3` |
| `CODER_MAX_READ` | 按需阅读追问轮次上限 | `3` |
| `CODER_READ_FILES` | 每轮最多读取文件数 | `6` |
| `CODER_READ_BYTES` | 单文件体积上限（超出截断） | `60000` |
| `CODER_READ_LINES` | 单文件行数上限 | `900` |
| `CODER_SUBTASK_THRESHOLD` | 计划文件数达到该值才启用"分段生成 + 对账" | `2` |
| `CODER_SUBTASK_MAX` | 单任务最多处理的模块数 | `8` |
| `CODER_RECONCILE_ROUNDS` | 对账后最多允许的修补轮次 | `1` |

---

## 九、运行自检

项目自带多组自检脚本（合计 249 项断言），改代码后建议全跑一遍：

```bash
python -m tests.selftest              # 执行阶段（58 项）
python -m tests.selftest_plan         # 计划解析与路由约束（33 项）
python -m tests.selftest_multimodule  # 模块拆分与对账（52 项）
python -m tests.selftest_mandate      # Mandate / 写前闸门 / 生命周期（41 项）
python -m tests.test_desktop_backend  # 桌面后端：端口/健康检查/token/设置（27 项）

# Web API 测试需先启动服务
python web/server.py
python tests/test_web_api.py --base http://127.0.0.1:8765
```

---

## 十、已知限制

- **产出需要人复核**：它是个加速器的角色，不是"一键代替工程师"。生成的代码请在合并前 review。
- **安全审计不是万能的**：`security` 节点做的是启发式检查，不能替代专业的安全测试与渗透测试。
- **依赖真实项目的结构**：项目越规范（有清晰的目录、依赖清单、测试），防跑偏效果越好。
- **大项目扫描有截断**：单文件读取有字节/行数上限，超大文件会被截断，可能影响理解完整性。
- **Mock 模式仅用于跑通流程**：它产出的是桩代码，不要用于真实业务。
- **模型质量决定上限**：本框架压制的是"跑偏"，不能弥补模型本身的能力边界。
- **Mandate 只约束路径，不约束内容正确性**：白名单能阻止写错文件，但不能保证写对内容；内容质量仍由对齐/安全/人工 review 兜底。
- **Attention 目前是"记录 + 重试/终止"**：更完整的"带答案继续执行"要等断点续跑（P2）落地。
- **桌面版关闭会中断当前任务**：确认退出后正在规划/执行的任务会停止；工作区文件保留，但 P2 断点续跑尚未实现。
- **API Key 以明文保存在本机**：桌面版默认写入 `%APPDATA%\MultiAgentCodegenTeam\config.json`，请勿把该文件或 `可使用的API-Key.txt` 提交/分享。
- **Windows 安装包需另行编译**：`packaging/installer/windows.iss` 已准备好，但需要安装 Inno Setup 才能生成 Setup.exe。

---

## 十一、开源声明

### 许可证

本项目基于 [MIT License](./LICENSE) 开源。

你可以自由地使用、修改、分发，甚至用于商业用途，只需保留版权声明即可。

### 贡献

欢迎提交 Issue 与 Pull Request。提交前建议：

1. 跑通 `tests/` 下全部自检
2. 不要提交 `.env` 或任何真实密钥
3. 遵循既有的代码风格与目录约定

### 安全提醒

- 本项目会读取你指定的项目目录，并可能在其中写入文件。**请先在测试目录上试用**，确认行为符合预期再用于重要项目。
- API Key 请通过 `.env` 提供，切勿硬编码进源码或提交到仓库。
- 若使用云端模型，请注意你的代码内容会发送到对应服务商。涉及敏感代码时，建议改用本地部署的模型端点。

### 致谢

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建。

---

<p align="center">如果这个项目对你有帮助，欢迎点个 Star ⭐</p>
