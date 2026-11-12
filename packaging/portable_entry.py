"""PyInstaller 入口：只负责把 windowed 模式下缺失的标准流补上，然后交给 app.main。

console=False 时 PyInstaller 会把 sys.stdout / sys.stderr 置成 None，而代码里有若干
`print(..., file=sys.stderr)` 的降级提示。`print(file=None)` 会接着取 sys.stdout，
也是 None，于是本该"只记一句"的分支会抛 AttributeError——在 Qt 槽里就是整个进程消失。
所以要在 import app 之前把这两根管子接到 data/logs/stderr.log（建不了就接空设备）。
"""
from __future__ import annotations

import os
import sys


def _ensure_streams() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        base = (os.environ.get("LUHUI_DATA") or os.environ.get("GELU_DATA")
                or os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data"))
        logs = os.path.join(os.path.abspath(base), "logs")
        os.makedirs(logs, exist_ok=True)
        stream = open(os.path.join(logs, "stderr.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_ensure_streams()

from app.main import main  # noqa: E402  —— 必须在标准流补齐之后

if __name__ == "__main__":
    sys.exit(main())
