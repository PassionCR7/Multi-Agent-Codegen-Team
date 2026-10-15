"""让 `python -m agents` 直接可用。"""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())