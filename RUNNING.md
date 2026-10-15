**五、三种运行入口**

### 1) Web 界面（推荐先试这个）

```bash
cd <项目根目录>                                      # 换成你克隆后的实际路径
./.venv/Scripts/python.exe web/server.py            # 默认 http://127.0.0.1:8765
./.venv/Scripts/python.exe web/server.py --port 9000
```

浏览器打开后：输入需求 → 看计划 → 批准/打回/自拟 → 看执行。

### 2) 命令行交互式（会先让你确认计划）

```bash
./.venv/Scripts/python.exe -m agents.main "你的需求" --project .
```

会自动询问：`1) 批准  2) 打回重规划  3) 我自己写方案`

### 3) 全自动（CI / 脚本用）

```bash
./.venv/Scripts/python.exe -m agents.main "你的需求" --project . --auto-plan
```

其他常用参数：

| 参数                 | 作用                           |
| ------------------ | ---------------------------- |
| `--project <路径>`   | 项目根目录（agent 会扫描它以免跑偏），默认当前目录 |
| `--task-name <名字>` | 指定工作区子目录名，便于回看               |
| `--plan-only`      | 只出计划、不写代码（把计划拿去评审）           |
| `--auto-plan`      | 自动批准计划，跳过人工确认                |
| `--json`           | 以 JSON 输出结果摘要（便于脚本解析）        |
| `--graph`          | 打印图结构                        |
| `--inspect <工作区>`  | 查看某次任务的状态与已批准计划              |

---

## 六、常用组合示例

```bash
cd <项目根目录>

# 离线试跑（不需要 API Key，用桩模型）
CODER_MOCK=1 ./.venv/Scripts/python.exe -m agents.main "实现加法和减法" --project . --auto-plan

# 真实模型 + 交互确认计划
./.venv/Scripts/python.exe -m agents.main "给 utils 增加 safe_div 函数" --project . --task-name my-task

# 只出计划，先评审
./.venv/Scripts/python.exe -m agents.main "重构日志模块" --project . --plan-only

# 回看某次任务的完整留痕
./.venv/Scripts/python.exe -m agents.main --inspect _agent_workspace/my-task

# 跑自检
./.venv/Scripts/python.exe -m tests.selftest
./.venv/Scripts/python.exe -m tests.selftest_plan
```

---
