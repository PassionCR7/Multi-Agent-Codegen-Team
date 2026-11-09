"""单实例锁：Windows 使用命名 Mutex，其他平台使用文件锁。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class SingleInstance:
    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None
        self._lock_file = None

    def acquire(self) -> bool:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            self._handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            if not self._handle:
                return False
            already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
            if already:
                kernel32.CloseHandle(self._handle)
                self._handle = None
                return False
            return True

        try:
            import fcntl

            lock_path = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp")) / f"{self.name}.lock"
            self._lock_file = open(lock_path, "w")  # noqa: SIM115
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ImportError):
            return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            finally:
                self._handle = None
        if self._lock_file is not None:
            try:
                import fcntl

                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            finally:
                try:
                    self._lock_file.close()
                finally:
                    self._lock_file = None
