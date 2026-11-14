# -*- coding: utf-8 -*-
"""IPC 边界与诊断升级自检（审计 3.4）：单行上限、回调快照、泄漏阈值、日志轮转、线程/原生崩溃捕获。

守的五件事：
  H1 IPC 读缓冲有 4 MiB 单行上限：无换行的巨行立刻断开，不再无限吃内存；
  H2 上限之内的正常行照旧解析（上限不是"读一点就断"的误伤）；
  H3 事件回调遍历用快照：读线程派发期间别的线程注册回调不许把读线程弄崩；
  H4 `_LEAKED` 钉住的在途缓冲超过 64 条要留下告警（只在越线那一刻报一次）；
  H5 `crash.log` 2 MiB × 3 份轮转；`faulthandler(all_threads=True)` 与
     `threading.excepthook` 都真挂上，且两者写下的异常都落进日志。

全离线：不启动 mpv、不开窗口。
"""
from __future__ import annotations

import contextlib
import faulthandler
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = tempfile.mkdtemp(prefix="hardening-", dir=HERE)
os.environ["LUHUI_DATA"] = DATA
sys.path.insert(0, ROOT)

from app import config as cfgmod     # noqa: E402
from app import main as MAIN         # noqa: E402
from app.engine import ipc as IP     # noqa: E402

results: list[tuple[str, bool, str]] = []
MB = 1024 * 1024


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ｜ {detail}" if detail else ""))
    return bool(ok)


class _FakePipe:
    """按脚本吐块的假管道：块用完后抛 OSError（模拟对端消失）。"""

    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.reads = 0
        self.closed = False
        self.writes: list[bytes] = []

    def read(self, timeout=None) -> bytes:
        self.reads += 1
        if self.chunks:
            return self.chunks.pop(0)
        raise OSError("fake pipe exhausted")

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


def _ipc_with(pipe: _FakePipe) -> IP.MpvIpc:
    ipc = IP.MpvIpc(r"\\.\pipe\luhui-hardening-probe")
    ipc._pipe = pipe
    ipc._alive = True
    return ipc


class _FakeProc:
    def __init__(self):
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def h1_line_cap() -> tuple[bool, str]:
    if getattr(IP, "MAX_LINE_BYTES", None) != 4 * MB:
        return False, f"MAX_LINE_BYTES={getattr(IP, 'MAX_LINE_BYTES', '缺失')}（要 {4 * MB}）"
    pipe = _FakePipe([b"A" * (IP.MAX_LINE_BYTES + 1), b"B" * 10])
    ipc = _ipc_with(pipe)
    proc = _FakeProc()
    ipc._proc = proc
    ipc._read_loop()
    ok = (ipc.alive is False and ipc.stats["errors"] >= 1 and pipe.closed
          and pipe.reads == 1            # 越线立刻断：不许再读第二块
          and proc.killed)               # 断开重连：内核被杀，看门狗按既有路径重启续播
    return ok, (f"alive={ipc.alive} errors={ipc.stats['errors']} "
                f"closed={pipe.closed} reads={pipe.reads} 内核已杀={proc.killed}")


def h2_normal_line() -> tuple[bool, str]:
    payload = b'{"event":"probe-ok"}\n'
    pipe = _FakePipe([payload])     # 第二块读不到 → 线程自然收口
    ipc = _ipc_with(pipe)
    got: list[str] = []
    ipc.on("probe-ok", lambda m: got.append("hit"))
    ipc._read_loop()
    ok = ipc.stats["received"] == 1 and got == ["hit"]
    return ok, f"received={ipc.stats['received']} 回调命中={got}"


def h3_callback_snapshot() -> tuple[bool, str]:
    """快照语义：派发这一帧时注册的回调，从**下一帧**才生效。

    为什么这条能判出旧写法：旧代码直接 `for cb in self._event_callbacks[...]`，
    list 迭代器按当前长度走，处理中途 append 进来的回调会**当场**被这一帧调用；
    快照写法看不到它。这个差别在"回调里再注册回调"（比如观察者重连）时决定会不会
    同一帧重复触发。
    """
    ipc = IP.MpvIpc(r"\\.\pipe\luhui-hardening-cb")
    attached = [0]
    late = [0]

    def cb(_msg):
        attached[0] += 1
        ipc.on("probe-event", lambda m: late.__setitem__(0, late[0] + 1))

    ipc.on("probe-event", cb)
    ipc._dispatch({"event": "probe-event"})
    first = late[0]                     # 快照生效时仍为 0
    ipc._dispatch({"event": "probe-event"})
    second = late[0]                    # 第二帧才轮到它
    ok = attached[0] == 2 and first == 0 and second == 1
    return ok, f"首帧命中={attached[0]} 中途注册的回调首帧被调={first} 次帧={second}"


def h4_leak_alert() -> tuple[bool, str]:
    if not hasattr(IP, "_pin_leaked"):
        return False, "缺少 _pin_leaked（_LEAKED 无阈值告警）"
    if getattr(IP, "LEAK_ALERT", None) != 64:
        return False, f"LEAK_ALERT={getattr(IP, 'LEAK_ALERT', '缺失')}（要 64）"
    IP._LEAKED.clear()
    IP._LEAK_ALERTED = False
    quiet = io.StringIO()
    with contextlib.redirect_stderr(quiet):
        for _ in range(64):
            IP._pin_leaked(object(), object())
    no_early = "钉住" not in quiet.getvalue()
    loud = io.StringIO()
    with contextlib.redirect_stderr(loud):
        for _ in range(6):
            IP._pin_leaked(object(), object())
    text = loud.getvalue()
    n = len(IP._LEAKED)
    IP._LEAKED.clear()
    IP._LEAK_ALERTED = False
    return (no_early and text.count("钉住") == 1 and n == 70,
            f"64 条时静默={no_early} 越线告警行数={text.count('钉住')} 钉住={n}")


def h5_crash_log() -> tuple[bool, str]:
    if getattr(MAIN, "CRASH_LOG_MAX", None) != 2 * MB:
        return False, f"CRASH_LOG_MAX={getattr(MAIN, 'CRASH_LOG_MAX', '缺失')}（要 {2 * MB}）"
    if getattr(MAIN, "CRASH_LOG_KEEP", None) != 3:
        return False, f"CRASH_LOG_KEEP={getattr(MAIN, 'CRASH_LOG_KEEP', '缺失')}（要 3）"
    import inspect
    src = inspect.getsource(MAIN.install_crash_log)
    has_all = "all_threads=True" in src
    has_fault = "faulthandler.enable(" in src

    logs = os.path.join(DATA, "logs-probe")
    cfgmod.LOG_DIR = logs
    if getattr(MAIN, "_CRASH_FH", None) is not None:
        faulthandler.disable()
        MAIN._CRASH_FH.close()
        MAIN._CRASH_FH = None
    MAIN.install_crash_log()
    fault_on = faulthandler.is_enabled()
    thread_hooked = threading.excepthook is not threading.__excepthook__
    sys_hooked = sys.excepthook is not sys.__excepthook__

    old_max, old_keep = MAIN.CRASH_LOG_MAX, MAIN.CRASH_LOG_KEEP
    MAIN.CRASH_LOG_MAX, MAIN.CRASH_LOG_KEEP = 4096, 3
    try:
        for i in range(6):
            MAIN._append_crash(f"chunk-{i}-" + "x" * 5000)
        with contextlib.redirect_stderr(io.StringIO()):
            sys.excepthook(ValueError, ValueError("hardening-probe"), None)
            threading.excepthook(types.SimpleNamespace(
                exc_type=RuntimeError, exc_value=RuntimeError("thread-probe"),
                exc_traceback=None, thread=threading.current_thread()))
    finally:
        MAIN.CRASH_LOG_MAX, MAIN.CRASH_LOG_KEEP = old_max, old_keep

    path = MAIN.crash_log_path()
    names = sorted(os.listdir(logs))
    rotated = (os.path.exists(path) and all(os.path.exists(path + f".{i}") for i in (1, 2, 3))
               and not os.path.exists(path + ".4"))
    body = ""
    for name in names:
        try:
            with open(os.path.join(logs, name), encoding="utf-8", errors="replace") as fh:
                body += fh.read()
        except OSError:
            pass
    hooked = "hardening-probe" in body and "thread-probe" in body

    faulthandler.disable()
    if MAIN._CRASH_FH is not None:
        MAIN._CRASH_FH.close()
        MAIN._CRASH_FH = None
    ok = (has_all and has_fault and fault_on and thread_hooked and sys_hooked
          and rotated and hooked)
    return ok, (f"all_threads={has_all} faulthandler={fault_on} 线程钩子={thread_hooked} "
                f"sys钩子={sys_hooked} 轮转={rotated} 两份异常都落盘={hooked} 文件={names}")


def h6_empty_crash_not_alarm() -> tuple[bool, str]:
    """faulthandler 的句柄一启动就把 crash.log 建出来（0 字节）。那是"常开的安全带"，
    不是一次崩溃；doctor 和打包冒烟都不许把它报成"突然没了"。"""
    from app import doctor
    level, text, _advice = doctor.j_crash({
        "crash_exists": True, "crash_bytes": 0,
        "crash_mtime": time.time(), "newest_src_mtime": 0,
    })
    return level == doctor.OK, f"级别={level} 说法={text}"


def main() -> int:
    rows = [("H1 单行 4 MiB 上限：无换行巨行立刻断开", h1_line_cap),
            ("H2 上限之内的正常行照旧解析", h2_normal_line),
            ("H3 事件回调快照：派发期间注册不崩读线程", h3_callback_snapshot),
            ("H4 _LEAKED 超过 64 条告警（越线只报一次）", h4_leak_alert),
            ("H5 crash.log 2 MiB×3 轮转 + faulthandler/threading 钩子", h5_crash_log),
            ("H6 空的 crash.log 是常开句柄，不许报成崩溃", h6_empty_crash_not_alarm)]
    red = 0
    for name, fn in rows:
        try:
            ok, detail = fn()
        except Exception as exc:            # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ｜ {detail}" if detail else ""))
        red += 0 if ok else 1
    shutil.rmtree(DATA, ignore_errors=True)
    print(f"[FINAL] IPC/诊断加固：{len(rows) - red}/{len(rows)} 通过；红 {red} 条")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
