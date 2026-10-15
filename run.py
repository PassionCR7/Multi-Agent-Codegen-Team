"""便捷入口：等价于 `python -m agents.main`。

    python run.py "你的需求" --project ./目标项目
"""

from agents.main import main

if __name__ == "__main__":
    raise SystemExit(main())