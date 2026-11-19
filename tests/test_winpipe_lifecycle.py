# -*- coding: utf-8 -*-
"""WinPipe 生命周期回归：假内核 / 假时钟 / 假调度器。

守的是设计 `video-handle-lifecycle-design.md` §3 的收口条件：
  · 请求停止 → 确认完成 → 回收；CancelIoEx 只发起取消，不是完成屏障；
  · 每个 Op 独占 buffer / OVERLAPPED / 完成 event / 原始句柄，先登记再发起；
  · 关闭冻结新 I/O；迟到 open/issue 必须由原 owner 收回，不能绕开取消；
  · stop_ev 即使从未 open 也要回收（旧实现 6 次构造→close 令句柄 +6）；
  · 996 继续等、995 查 GetOverlappedResult、1168 也必须查结果、WAIT_FAILED 钉住
    整个 owner 包、短写写完剩余、CloseHandle 检查 BOOL 且只在确定未关闭时有限重试；
  · 未确认完成一律进 STALLED，强持有 h / stop / Op / event，不清 `_LEAKED`。

全离线：假内核不创建任何真实句柄；只有 RealKernelHandleIntegration 会构造
WinPipe 的真实 event 对象（不 CreateFile、不发 I/O），用来量 stop_ev 回收。
不导入 GUI/config，不碰 %APPDATA%，不启动 mpv/ffmpeg。

运行：python -B tests/test_winpipe_lifecycle.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import inspect
import io
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from collections import deque
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engine import ipc as IP  # noqa: E402

# 假内核返回的“确定未关闭”错误码（只用于测试替身；真实 CloseHandle 不会给出证明）。
FAKE_RETRYABLE_CLOSE = 170
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF


class FakeClock:
    """可注入时钟：sleep 只推进虚拟时间，测试不真睡。"""

    def __init__(self, start: float = 1000.0):
        self._t = float(start)
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            if seconds > 0:
                self._t += seconds


class RecordingScheduler:
    """记录后台收割器线程，便于断言名字与存活；仍用真线程执行。"""

    def __init__(self):
        self.threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def spawn(self, fn, name: str):
        th = threading.Thread(target=fn, name=name, daemon=True)
        with self._lock:
            self.threads.append(th)
        th.start()
        return th

    def alive(self) -> list[str]:
        with self._lock:
            return [t.name for t in self.threads if t.is_alive()]


class FakeKernelApi:
    """与 `ipc.KernelApi` 同形的假内核。

    额外承担两件事：
      1) 句柄台账（open/kind/close_calls/close_threads），泄漏与迟到 open 可观测；
      2) 危险关闭判红：在 I/O pending 或 wait 正在引用时 CloseHandle(event)，
         记录 violation 并返回失败——测试 tearDown 会把 violation 当失败。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.next_handle = 0x1000
        self.kind: dict[int, str] = {}
        self.open: set[int] = set()
        self.signaled: set[int] = set()
        self.pending: dict[int, dict] = {}      # event -> op
        self.ops: dict[int, dict] = {}          # id(ov) -> op
        self.active_waits: dict[int, int] = {}
        self.violations: list[tuple] = []
        self.close_calls: dict[int, int] = {}
        self.close_threads: dict[int, list] = {}
        self.written: list[bytes] = []
        self.create_file_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self.cancel_calls = 0
        self.gor_calls = 0
        self.wait_calls = 0
        self.set_event_calls = 0
        self.read_script: deque = deque()
        self.write_script: deque = deque()
        self.gor_script: deque = deque()
        self.cancel_script: deque = deque()
        self.wait_script: deque = deque()
        self.create_file_script: deque = deque()
        self.create_event_script: deque = deque()
        self.close_script: dict[int, deque] = {}
        self.before_read = None
        self.before_write = None
        self.after_gor = None
        self.gor_stuck = False                  # 取消后 GOR 仍恒 996（不可确认）
        self.blocking = False                   # wait 真阻塞，用于并发/waiter 判红
        self.create_file_threads: list = []

    # ---- 台账 ----
    def _violation_locked(self, reason: str, h=None):
        self.violations.append((reason, h, threading.current_thread().name))

    def live_handles(self) -> int:
        with self.lock:
            return len(self.open)

    def alloc_file(self) -> int:
        with self.lock:
            return self._alloc_locked("file")

    def _alloc_locked(self, kind: str) -> int:
        h = self.next_handle
        self.next_handle += 1
        self.kind[h] = kind
        self.open.add(h)
        return h

    # ---- KernelApi ----
    def create_event(self):
        with self.lock:
            if self.create_event_script:
                item = self.create_event_script.popleft()
                if callable(item):
                    return item(self)
                return item
            return self._alloc_locked("event"), 0

    def create_file(self, path):
        with self.lock:
            self.create_file_calls += 1
            self.create_file_threads.append(threading.current_thread())
            script = self.create_file_script.popleft() if self.create_file_script else None
        if script is not None:
            if callable(script):
                return script(self)
            h, err = script
            if h:
                with self.lock:
                    self.kind[h] = "file"
                    self.open.add(h)
            return h, err
        return self.alloc_file(), 0

    def _issue(self, kind, h, buf, size, ov):
        hook = self.before_read if kind == "read" else self.before_write
        if hook is not None:
            hook()
        with self.lock:
            if h not in self.open:
                self._violation_locked(f"{kind} on closed handle", h)
                return False, IP.ERROR_INVALID_HANDLE, 0
            if kind == "read":
                self.read_calls += 1
            else:
                self.write_calls += 1
                self.written.append(bytes(buf.raw[:size]))
            script = self.read_script if kind == "read" else self.write_script
            if script:
                item = script.popleft()
                if callable(item):
                    item = item(self)
                ok, err, got = item
            else:
                ok, err, got = False, IP.ERROR_IO_PENDING, 0
            op = {"kind": kind, "h": h, "ev": ov.hEvent, "ov": ov, "buf": buf,
                  "status": ("done" if ok else
                             ("pending" if err == IP.ERROR_IO_PENDING else "error")),
                  "bytes": got, "error": err, "cancel_attempts": 0,
                  "cancelled": False}
            self.ops[id(ov)] = op
            if op["status"] == "pending":
                self.pending[ov.hEvent] = op
            if ok:
                self.signaled.add(ov.hEvent)
            self.cond.notify_all()
            return ok, err, got

    def read_file(self, h, buf, size, ov):
        return self._issue("read", h, buf, size, ov)

    def write_file(self, h, buf, size, ov):
        return self._issue("write", h, buf, size, ov)

    def get_overlapped_result(self, h, ov):
        with self.lock:
            self.gor_calls += 1
        hook = self.after_gor
        if hook is not None:
            hook(self)
        with self.lock:
            op = self.ops.get(id(ov))
            if op is None:
                return False, IP.ERROR_IO_PENDING, 0
            if self.gor_stuck:
                return False, IP.ERROR_IO_PENDING, 0
            if self.gor_script:
                item = self.gor_script.popleft()
                if callable(item):
                    item = item(self, op)
                ok, err, got = item
            elif op["status"] == "done":
                ok, err, got = True, 0, op["bytes"]
            elif op["cancelled"]:
                ok, err, got = False, IP.ERROR_OPERATION_ABORTED, 0
            else:
                ok, err, got = False, IP.ERROR_IO_PENDING, 0
            if ok:
                op["status"] = "done"
                op["bytes"] = got
                self.pending.pop(op["ev"], None)
            elif err == IP.ERROR_IO_PENDING:
                pass
            else:
                op["status"] = ("aborted" if err == IP.ERROR_OPERATION_ABORTED
                                else "error")
                op["error"] = err
                self.pending.pop(op["ev"], None)
            self.cond.notify_all()
            return ok, err, got

    def cancel_io_ex(self, h, ov):
        with self.lock:
            self.cancel_calls += 1
            op = self.ops.get(id(ov))
            if op is None:
                return False, IP.ERROR_NOT_FOUND
            if self.cancel_script:
                item = self.cancel_script.popleft()
                if callable(item):
                    item = item(self, op)
                ok, err = item
            else:
                ok, err = True, 0
            op["cancel_attempts"] += 1
            if ok:
                # 取消是异步的：只代表请求已受理；完成由 GetOverlappedResult 确认。
                op["cancelled"] = True
                self.signaled.add(op["ev"])
                self.cond.notify_all()
            return ok, err

    def set_event(self, h):
        with self.lock:
            self.set_event_calls += 1
            if h not in self.open:
                self._violation_locked("SetEvent on closed handle", h)
                return False, IP.ERROR_INVALID_HANDLE
            self.signaled.add(h)
            self.cond.notify_all()
            return True, 0

    def wait_for_multiple_objects(self, handles, timeout_ms):
        handles = list(handles)
        with self.lock:
            self.wait_calls += 1
            if self.wait_script:
                item = self.wait_script.popleft()
                if callable(item):
                    return item(self, handles)
                return item
            for h in handles:
                self.active_waits[h] = self.active_waits.get(h, 0) + 1
            try:
                if not self.blocking:
                    for index, h in enumerate(handles):
                        if h in self.signaled:
                            return index
                    return WAIT_TIMEOUT
                deadline = (None if timeout_ms >= IP.INFINITE
                            else time.monotonic() + timeout_ms / 1000.0)
                while True:
                    for index, h in enumerate(handles):
                        if h in self.signaled:
                            return index
                        if h not in self.open:
                            self._violation_locked("wait on closed handle", h)
                            return WAIT_FAILED
                    remaining = (None if deadline is None
                                 else deadline - time.monotonic())
                    if remaining is not None and remaining <= 0:
                        return WAIT_TIMEOUT
                    self.cond.wait(0.2 if remaining is None else min(0.2, remaining))
            finally:
                for h in handles:
                    self.active_waits[h] -= 1
                    if self.active_waits[h] <= 0:
                        del self.active_waits[h]

    def close_handle(self, h):
        with self.lock:
            self.close_calls[h] = self.close_calls.get(h, 0) + 1
            self.close_threads.setdefault(h, []).append(threading.current_thread())
            if h in self.pending:
                self._violation_locked("close event while I/O pending", h)
            if self.active_waits.get(h, 0) > 0:
                self._violation_locked("close handle while waiter active", h)
            if any(op["h"] == h and op["status"] == "pending"
                   for op in self.ops.values()):
                self._violation_locked("close pipe handle with I/O pending", h)
            script = self.close_script.get(h)
            if script:
                item = script.popleft()
                if callable(item):
                    item = item(self, h)
                ok, err, closed = item
            else:
                ok, err, closed = True, 0, True
            if ok:
                self.open.discard(h)
                self.kind.pop(h, None)
                self.signaled.discard(h)
                self.cond.notify_all()
            return ok, err, closed

    # ---- 测试辅助 ----
    def complete(self, op, data=b""):
        with self.lock:
            if op["kind"] == "read" and data:
                ctypes.memmove(op["buf"], data, min(len(data), len(op["buf"].raw)))
            op["status"] = "done"
            op["bytes"] = len(data)
            self.pending.pop(op["ev"], None)
            self.signaled.add(op["ev"])
            self.cond.notify_all()

    def pending_op(self):
        with self.lock:
            return next(iter(self.pending.values()), None)


class LifecycleTestBase(unittest.TestCase):
    """公共前置：断言新 API 已就位。

    红跑时这里必须因 AssertionError 失败——不允许 ImportError/AttributeError
    冒充“测试红了”。
    """

    REQUIRED_KERNEL = ("create_file", "create_event", "read_file", "write_file",
                       "get_overlapped_result", "cancel_io_ex", "set_event",
                       "wait_for_multiple_objects", "close_handle")
    REQUIRED_PIPE_API = ("request_stop", "wait_closed", "snapshot", "close",
                         "read", "write", "try_open", "open")
    REQUIRED_PARAMS = ("kernel", "clock", "scheduler", "leaks")

    def setUp(self):
        missing = [name for name in ("KernelApi", "LeakRegistry")
                   if not hasattr(IP, name)]
        self.assertEqual(missing, [], f"生命周期 API 缺失：{missing}")
        if hasattr(IP, "KernelApi"):
            absent = [n for n in self.REQUIRED_KERNEL
                      if not callable(getattr(IP.KernelApi, n, None))]
            self.assertEqual(absent, [], f"KernelApi 缺方法：{absent}")
        absent = [n for n in self.REQUIRED_PIPE_API
                  if not callable(getattr(IP.WinPipe, n, None))]
        self.assertEqual(absent, [], f"WinPipe 缺生命周期方法：{absent}")
        params = inspect.signature(IP.WinPipe.__init__).parameters
        absent = [n for n in self.REQUIRED_PARAMS if n not in params]
        self.assertEqual(absent, [], f"WinPipe.__init__ 缺注入参数：{absent}")
        self.assertEqual(hasattr(IP.MpvIpc, "close_async"), True,
                         "MpvIpc 缺 close_async()")
        self.data = tempfile.mkdtemp(prefix="winpipe-life-")
        self.addCleanup(self._rm_data)
        self.kernels: list[FakeKernelApi] = []
        self.leaks = IP.LeakRegistry(alert=1000)

    def _rm_data(self):
        import shutil
        shutil.rmtree(self.data, ignore_errors=True)

    def make_pipe(self, leaks=None, clock=None, scheduler=None):
        kernel = FakeKernelApi()
        clock = clock or FakeClock()
        scheduler = scheduler or RecordingScheduler()
        leaks = leaks if leaks is not None else self.leaks
        path = os.path.join(self.data, "pipe-" + uuid.uuid4().hex)
        pipe = IP.WinPipe(path, kernel=kernel, clock=clock,
                          scheduler=scheduler, leaks=leaks)
        self.kernels.append(kernel)
        self.addCleanup(self._safe_stop, pipe)
        return pipe, kernel, clock, scheduler

    @staticmethod
    def _safe_stop(pipe):
        try:
            pipe.request_stop()
            pipe.wait_closed(0.2)
        except Exception:
            pass

    def tearDown(self):
        for kernel in self.kernels:
            if kernel.violations:
                self.fail("假内核判红（危险关闭/关闭后 I/O）：" + repr(kernel.violations))

    def open_pipe(self, **kwargs):
        pipe, kernel, clock, scheduler = self.make_pipe(**kwargs)
        self.assertTrue(pipe.try_open())
        self.assertIsNotNone(pipe.h)
        return pipe, kernel, clock, scheduler

    def close_now(self, pipe):
        pipe.request_stop()
        self.assertEqual(pipe.wait_closed(2.0), True,
                         f"关闭未在预算内完成：{pipe.snapshot()!r}")


class ConstantsAndStopEvent(LifecycleTestBase):
    def test_error_no_data_is_232_and_pipe_busy_is_231(self):
        self.assertEqual(IP.ERROR_NO_DATA, 232,
                         "[constants] ERROR_NO_DATA 应为 232（231 是 ERROR_PIPE_BUSY）")
        self.assertEqual(IP.ERROR_PIPE_BUSY, 231, "[constants] ERROR_PIPE_BUSY 应为 231")
        self.assertEqual(IP.ERROR_NOT_FOUND, 1168, "[constants] ERROR_NOT_FOUND 应为 1168")

    def test_never_opened_close_reclaims_stop_event(self):
        pipe, kernel, _, _ = self.make_pipe()
        snap = pipe.snapshot()
        self.assertEqual(snap["state"], "new", snap)
        self.assertEqual(snap["ops"], 0, snap)
        self.assertEqual(snap["leases"], 0, snap)
        self.assertEqual(snap["handles"], 1,
                         f"[stop-event] 未 open 时 stop_ev 也应被计数/回收：{snap}")
        self.assertEqual(kernel.live_handles(), 1)
        pipe.close()
        self.assertEqual(pipe.snapshot()["state"], "closed", pipe.snapshot())
        self.assertEqual(pipe.snapshot()["handles"], 0,
                         f"[stop-event] close 后不得残留 stop_ev：{pipe.snapshot()}")
        self.assertIsNone(pipe.stop_ev, "[stop-event] 成功关闭后 stop_ev 字段应清空")
        self.assertEqual(kernel.live_handles(), 0)

    def test_six_never_opened_cycles_do_not_grow_handles(self):
        closed_events = []
        for _ in range(6):
            pipe, kernel, _, _ = self.make_pipe()
            ev = pipe.stop_ev
            pipe.close()
            self.assertEqual(pipe.snapshot()["handles"], 0, pipe.snapshot())
            self.assertEqual(kernel.close_calls.get(ev), 1,
                             f"[stop-event] 每个 stop_ev 必须恰好 CloseHandle 一次：{kernel.close_calls}")
            closed_events.append(ev)
            self.assertEqual(kernel.live_handles(), 0)
        self.assertEqual(len(set(closed_events)), 6)

    def test_create_event_failure_raises_without_partial_handles(self):
        kernel = FakeKernelApi()
        kernel.create_event_script.append((0, 5))
        with self.assertRaises(OSError) as ctx:
            IP.WinPipe(os.path.join(self.data, "never"), kernel=kernel,
                       clock=FakeClock(), scheduler=RecordingScheduler(),
                       leaks=self.leaks)
        self.assertIn("5", str(ctx.exception), "[create-event] 错误码要进异常")
        self.assertEqual(kernel.live_handles(), 0, "[create-event] 失败不得留下半个句柄")

    def test_repeated_close_is_idempotent_and_closes_each_handle_once(self):
        pipe, kernel, _, _ = self.open_pipe()
        h, stop = pipe.h, pipe.stop_ev
        for _ in range(3):
            pipe.close()
        self.assertEqual(pipe.snapshot()["state"], "closed", pipe.snapshot())
        self.assertEqual(kernel.close_calls.get(h), 1, kernel.close_calls)
        self.assertEqual(kernel.close_calls.get(stop), 1, kernel.close_calls)
        self.assertEqual(kernel.live_handles(), 0)


class RealKernelHandleIntegration(unittest.TestCase):
    """真实内核（只建 event，不 CreateFile/不发 I/O）：未 open 六次构造→close 零增长。

    旧实现正是在这里 144→150；本用例不依赖新 API 前置，红跑时给出行为级失败。
    """

    @staticmethod
    def _handle_count() -> int:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        k32.GetProcessHandleCount.restype = wt.BOOL
        n = wt.DWORD()
        if not k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n)):
            raise OSError("GetProcessHandleCount failed")
        return n.value

    def test_six_construct_close_cycles_leave_no_handle_growth(self):
        # 预热一次，把首次懒初始化造成的进程级句柄变化排除在测量之外。
        warm = IP.WinPipe("\\\\.\\pipe\\never-opened-warm-" + uuid.uuid4().hex)
        warm.close()
        before = self._handle_count()
        for _ in range(6):
            pipe = IP.WinPipe("\\\\.\\pipe\\never-opened-" + uuid.uuid4().hex)
            pipe.close()
        after = self._handle_count()
        self.assertEqual(after, before,
                         f"[stop-event-real] 未 open 构造→close 泄漏句柄：{before}→{after}")


class OpenLifecycle(LifecycleTestBase):
    def test_late_open_is_returned_to_opener_and_never_published(self):
        pipe, kernel, _, _ = self.make_pipe()
        entered, release = threading.Event(), threading.Event()

        def paused_create(_kernel):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release create_file")
            return _kernel.alloc_file(), 0

        kernel.create_file_script.append(paused_create)
        outcome = []
        opener = threading.Thread(target=lambda: outcome.append(pipe.try_open()),
                                  name="late-open-owner", daemon=True)
        opener.start()
        try:
            self.assertTrue(entered.wait(1))
            pipe.request_stop()
        finally:
            release.set()
            opener.join(2)
        self.assertFalse(opener.is_alive())
        self.assertEqual(outcome, [False],
                         f"[late-open] 冻结后 try_open 必须失败而不是发布句柄：{outcome}")
        self.assertIsNone(pipe.h, "[late-open] 迟到句柄不得发布到 pipe.h")
        self.assertEqual(pipe.wait_closed(2.0), True, pipe.snapshot())
        self.assertEqual(kernel.live_handles(), 0,
                         f"[late-open] 迟到句柄必须由 open 方关闭：{kernel.live_handles()}")
        closed_threads = [ths for h, ths in kernel.close_threads.items()
                          if h != pipe.stop_ev]
        self.assertTrue(any(ths and ths[0] is opener for ths in closed_threads),
                        f"[late-open] 迟到句柄必须在 opener 线程关闭：{closed_threads}")

    def test_frozen_pipe_rejects_new_io_without_touching_kernel(self):
        pipe, kernel, _, _ = self.open_pipe()
        pipe.close()
        reads, writes, opens = kernel.read_calls, kernel.write_calls, kernel.create_file_calls
        with self.assertRaises(OSError) as ctx:
            pipe.read(timeout=0.01)
        self.assertIn("停止", str(ctx.exception), "[freeze-gate] 关闭后 read 必须报停止")
        with self.assertRaises(OSError) as ctx:
            pipe.write(b"x")
        self.assertIn("停止", str(ctx.exception), "[freeze-gate] 关闭后 write 必须报停止")
        self.assertFalse(pipe.try_open())
        self.assertEqual((kernel.read_calls, kernel.write_calls,
                          kernel.create_file_calls), (reads, writes, opens),
                         "[freeze-gate] 冻结后不得再发起任何系统调用")

    def test_open_failure_records_last_error(self):
        pipe, kernel, _, _ = self.make_pipe()
        kernel.create_file_script.append((0, 2))
        self.assertFalse(pipe.try_open())
        self.assertEqual(pipe.last_error, 2, "[open] 失败原因要留给调用方")
        self.assertEqual(kernel.live_handles(), 1, "仅 stop_ev 一个句柄")


class ReadLifecycle(LifecycleTestBase):
    def test_polling_reuses_the_single_pending_read(self):
        pipe, kernel, _, _ = self.open_pipe()
        for _ in range(3):
            self.assertEqual(pipe.read(timeout=0.01), b"")
        self.assertEqual(kernel.read_calls, 1,
                         f"[read-poll] 轮询不得重复发起 ReadFile：{kernel.read_calls}")
        snap = pipe.snapshot()
        self.assertEqual((snap["ops"], snap["leases"], snap["handles"]), (1, 0, 3), snap)
        op = kernel.pending_op()
        self.assertIsNotNone(op)
        kernel.complete(op, b"hello")
        self.assertEqual(pipe.read(timeout=1.0), b"hello")
        snap = pipe.snapshot()
        self.assertEqual((snap["ops"], snap["handles"]), (0, 2), snap)
        self.close_now(pipe)
        self.assertEqual(kernel.live_handles(), 0)

    def test_late_issue_after_stop_is_handed_back(self):
        pipe, kernel, _, _ = self.open_pipe()
        entered, release = threading.Event(), threading.Event()
        kernel.before_read = lambda: (entered.set(), release.wait(5))
        # 读完即停：不允许“冻结后的读”再进入任何完成等待。
        kernel.wait_script.append(WAIT_TIMEOUT)
        outcome = []

        def reader():
            try:
                outcome.append(pipe.read(timeout=0.5))
            except BaseException as exc:  # noqa: BLE001 - 测试要观察异常类型
                outcome.append(exc)

        th = threading.Thread(target=reader, name="late-issue-reader", daemon=True)
        th.start()
        try:
            self.assertTrue(entered.wait(1))
            pipe.request_stop()
        finally:
            release.set()
            th.join(3)
        self.assertFalse(th.is_alive(), "late issue 后读线程不得悬挂")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], OSError,
                              f"[late-issue] 冻结后的下发必须交回 owner 并报停止：{outcome!r}")
        self.assertEqual(kernel.wait_calls, 0,
                         "[late-issue] 交回 owner 后不得再等旧 op 的完成事件")
        self.assertEqual(pipe.wait_closed(2.0), True, pipe.snapshot())
        self.assertGreaterEqual(kernel.cancel_calls, 1,
                                "[late-issue] 已登记的下发必须被取消")
        self.assertEqual(kernel.live_handles(), 0)

    def test_cancel_success_is_not_completion(self):
        pipe, kernel, _, _ = self.open_pipe()
        self.assertEqual(pipe.read(timeout=0.01), b"")   # 留下一个 pending 读
        kernel.cancel_script.append((True, 0))
        kernel.gor_script.extend([(False, IP.ERROR_IO_PENDING, 0),
                                  (False, IP.ERROR_IO_PENDING, 0),
                                  (False, IP.ERROR_OPERATION_ABORTED, 0)])
        pipe.request_stop()
        self.assertEqual(pipe.wait_closed(2.0), True, pipe.snapshot())
        self.assertGreaterEqual(kernel.gor_calls, 3,
                                "[cancel-not-completion] CancelIoEx 成功不等于完成，必须查结果")
        self.assertEqual(pipe.snapshot()["state"], "closed", pipe.snapshot())
        self.assertEqual(kernel.live_handles(), 0)

    def test_cancel_995_retires_after_confirmed_abort(self):
        pipe, kernel, _, _ = self.open_pipe()
        self.assertEqual(pipe.read(timeout=0.01), b"")
        kernel.cancel_script.append((True, 0))
        kernel.gor_script.append((False, IP.ERROR_OPERATION_ABORTED, 0))
        pipe.request_stop()
        self.assertEqual(pipe.wait_closed(2.0), True, pipe.snapshot())
        self.assertEqual(kernel.gor_calls, 1, kernel.gor_calls)
        self.assertEqual(pipe.snapshot()["quarantine"], 0, pipe.snapshot())
        self.assertEqual(kernel.live_handles(), 0)

    def test_cancel_1168_queries_result_and_must_retire(self):
        pipe, kernel, _, _ = self.open_pipe()
        self.assertEqual(pipe.read(timeout=0.01), b"")
        kernel.cancel_script.append((False, IP.ERROR_NOT_FOUND))
        kernel.gor_script.append((True, 0, 2))
        pipe.request_stop()
        self.assertEqual(pipe.wait_closed(2.0), True, pipe.snapshot())
        self.assertGreaterEqual(kernel.gor_calls, 1,
                                "[cancel-1168-query] 1168（取消但已完成）必须继续查结果")
        self.assertEqual(pipe.snapshot()["quarantine"], 0, pipe.snapshot())
        self.assertEqual(kernel.live_handles(), 0)

    def test_wait_failed_pins_whole_owner_and_stalls(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.read_script.append((False, IP.ERROR_IO_PENDING, 0))
        kernel.wait_script.append(WAIT_FAILED)
        with self.assertRaises(OSError) as ctx:
            pipe.read(timeout=0.5)
        self.assertIn("等待", str(ctx.exception), "[wait-failed-pin] 必须报等待失败")
        snap = pipe.snapshot()
        self.assertEqual(snap["state"], "stalled", f"[wait-failed-pin] {snap}")
        self.assertEqual(snap["quarantine"], 1, f"[wait-failed-pin] {snap}")
        self.assertEqual(snap["ops"], 0, snap)
        self.assertEqual(len(self.leaks.items), 1,
                         f"[wait-failed-pin] 必须钉住 buffer/OVERLAPPED：{self.leaks.items}")
        self.assertFalse(pipe.wait_closed(0.2), "[wait-failed-pin] STALLED 不算关闭")
        before = dict(kernel.close_calls)
        pipe.close()
        pipe.close()
        self.assertEqual(kernel.close_calls, before,
                         "[wait-failed-pin] 未确认完成时不得关闭 h/stop/op event")
        self.assertIsNotNone(pipe.h)
        self.assertIsNotNone(pipe.stop_ev)
        self.assertGreaterEqual(kernel.live_handles(), 3)

    def test_close_while_read_blocked_reclaims_after_reader_exits(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.blocking = True
        kernel.read_script.append((False, IP.ERROR_IO_PENDING, 0))
        outcome = []

        def reader():
            try:
                outcome.append(pipe.read())
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        th = threading.Thread(target=reader, name="blocked-reader", daemon=True)
        th.start()
        self.assertTrue(self._wait_until(lambda: kernel.pending_op() is not None))
        pipe.close()
        th.join(3)
        self.assertFalse(th.is_alive(), "close 后读线程必须退出")
        self.assertIsInstance(outcome[0], OSError, outcome)
        self.assertEqual(pipe.snapshot()["state"], "closed", pipe.snapshot())
        self.assertEqual(kernel.live_handles(), 0)

    def test_close_from_io_completion_does_not_deadlock(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.read_script.append((False, IP.ERROR_IO_PENDING, 0))
        kernel.wait_script.append(WAIT_OBJECT_0)
        closed_in_hook = []

        def close_in_gor(_kernel):
            if not closed_in_hook:
                closed_in_hook.append(time.monotonic())
                pipe.close()

        kernel.after_gor = close_in_gor
        started = time.monotonic()
        with self.assertRaises(OSError) as ctx:
            pipe.read(timeout=1.0)
        elapsed = time.monotonic() - started
        self.assertIn("停止", str(ctx.exception), "[callback-close] 回调 close 后读应报停止")
        self.assertEqual(len(closed_in_hook), 1)
        self.assertLess(elapsed, 1.5, "[callback-close] 回调 close 不得在 join/wait 上死等")
        self.assertEqual(pipe.wait_closed(2.0), True, pipe.snapshot())
        self.assertEqual(kernel.live_handles(), 0)

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False


class WriteLifecycle(LifecycleTestBase):
    def test_short_write_writes_the_remainder(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.write_script.extend([(True, 0, 3), (True, 0, 7)])
        pipe.write(b"0123456789")
        self.assertEqual(kernel.written, [b"0123456789", b"3456789"],
                         f"[short-write] 必须用剩余字节续写：{kernel.written}")
        self.assertEqual(pipe.stats.get("short_writes"), 1, pipe.stats)
        self.assertEqual(pipe.snapshot()["ops"], 0, pipe.snapshot())
        self.close_now(pipe)
        self.assertEqual(kernel.live_handles(), 0)

    def test_write_timeout_unconfirmed_cancel_quarantines(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.write_script.append((False, IP.ERROR_IO_PENDING, 0))
        kernel.wait_script.append(WAIT_TIMEOUT)
        kernel.cancel_script.append((True, 0))
        kernel.gor_stuck = True
        with self.assertRaises(OSError) as ctx:
            pipe.write(b"payload")
        self.assertIn("超时", str(ctx.exception), "[write-timeout] 必须报写超时")
        snap = pipe.snapshot()
        self.assertEqual(snap["state"], "stalled", f"[write-timeout] {snap}")
        self.assertEqual(snap["quarantine"], 1, f"[write-timeout] {snap}")
        self.assertEqual(len(self.leaks.items), 1, "[write-timeout] 未确认完成必须钉住 owner")
        self.assertIsNotNone(kernel.pending_op(), "pending op 不得被假装完成")
        before = dict(kernel.close_calls)
        pipe.close()
        self.assertEqual(kernel.close_calls, before,
                         "[write-timeout] STALLED 后不得关闭仍在途的 event/h")

    def test_write_timeout_completion_wins_1168(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.write_script.append((False, IP.ERROR_IO_PENDING, 0))
        kernel.wait_script.append(WAIT_TIMEOUT)
        kernel.cancel_script.append((False, IP.ERROR_NOT_FOUND))
        kernel.gor_script.append((True, 0, 7))
        pipe.write(b"payload")
        self.assertGreaterEqual(kernel.gor_calls, 1,
                                "[write-1168] 取消返回 1168 必须查结果")
        self.assertEqual(pipe.snapshot()["ops"], 0, pipe.snapshot())
        self.close_now(pipe)
        self.assertEqual(kernel.live_handles(), 0)

    def test_write_timeout_confirmed_cancel_raises_and_retires(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.write_script.append((False, IP.ERROR_IO_PENDING, 0))
        kernel.wait_script.append(WAIT_TIMEOUT)
        kernel.cancel_script.append((True, 0))
        kernel.gor_script.append((False, IP.ERROR_OPERATION_ABORTED, 0))
        with self.assertRaises(OSError) as ctx:
            pipe.write(b"payload")
        self.assertIn("取消", str(ctx.exception), "[write-abort] 必须报取消")
        self.assertEqual(pipe.snapshot()["ops"], 0, pipe.snapshot())
        self.assertEqual(pipe.snapshot()["quarantine"], 0, pipe.snapshot())
        self.close_now(pipe)
        self.assertEqual(kernel.live_handles(), 0)

    def test_zero_progress_short_write_is_an_error(self):
        pipe, kernel, _, _ = self.open_pipe()
        kernel.write_script.append((True, 0, 0))
        with self.assertRaises(OSError) as ctx:
            pipe.write(b"abc")
        self.assertIn("短写", str(ctx.exception), "[short-write-zero] 0 字节不得死循环")
        self.close_now(pipe)
        self.assertEqual(kernel.live_handles(), 0)


class CloseSemantics(LifecycleTestBase):
    def test_close_retry_is_bounded_when_definitely_not_closed(self):
        pipe, kernel, _, _ = self.open_pipe()
        stop = pipe.stop_ev
        kernel.close_script[stop] = deque(
            [(False, FAKE_RETRYABLE_CLOSE, False)] * 1000 + [(True, 0, True)])
        pipe.close()
        self.assertEqual(kernel.close_calls.get(stop), 1 + IP.CLOSE_RETRIES,
                         f"[close-retry-cap] 重试必须有上限：{kernel.close_calls.get(stop)}")
        self.assertEqual(pipe.snapshot()["state"], "stalled", pipe.snapshot())
        self.assertEqual(pipe.snapshot()["handles"], 1, pipe.snapshot())
        self.assertEqual(pipe.last_close_error, FAKE_RETRYABLE_CLOSE)
        calls = kernel.close_calls.get(stop)
        pipe.close()
        pipe.close()
        self.assertEqual(kernel.close_calls.get(stop), calls,
                         "[close-retry-cap] 放弃后不得再重试旧句柄值")

    def test_unknown_close_failure_is_not_retried(self):
        pipe, kernel, _, _ = self.open_pipe()
        stop = pipe.stop_ev
        kernel.close_script[stop] = deque([(False, IP.ERROR_ACCESS_DENIED, None)] * 10)
        pipe.close()
        self.assertEqual(kernel.close_calls.get(stop), 1,
                         f"[close-unknown] 结果不明不得重试：{kernel.close_calls.get(stop)}")
        self.assertEqual(pipe.snapshot()["state"], "stalled", pipe.snapshot())
        self.assertEqual(pipe.last_close_error, IP.ERROR_ACCESS_DENIED)
        self.assertEqual(pipe.snapshot()["handles"], 1, pipe.snapshot())

    def test_invalid_handle_is_not_retried_and_not_counted(self):
        pipe, kernel, _, _ = self.open_pipe()
        stop = pipe.stop_ev
        kernel.close_script[stop] = deque([(False, IP.ERROR_INVALID_HANDLE, None)] * 10)
        pipe.close()
        self.assertEqual(kernel.close_calls.get(stop), 1,
                         f"[close-invalid] INVALID_HANDLE 不得重试：{kernel.close_calls.get(stop)}")
        self.assertEqual(pipe.snapshot()["state"], "closed", pipe.snapshot())
        self.assertEqual(pipe.snapshot()["handles"], 0, pipe.snapshot())
        self.assertIsNone(pipe.stop_ev)


class PrivateLeakRegistry(LifecycleTestBase):
    def test_alert_fires_once_and_global_leaked_is_untouched(self):
        leaks = IP.LeakRegistry(alert=1)
        global_before = len(IP._LEAKED)
        stderr = io.StringIO()
        with patch.object(IP, "_pin_leaked", side_effect=AssertionError("global registry used")):
            with self._redirect_stderr(stderr):
                for _ in range(2):
                    pipe, kernel, _, _ = self.make_pipe(leaks=leaks)
                    self.assertTrue(pipe.try_open())
                    kernel.read_script.append((False, IP.ERROR_IO_PENDING, 0))
                    kernel.wait_script.append(WAIT_FAILED)
                    with self.assertRaises(OSError):
                        pipe.read(timeout=0.1)
                    pipe.close()
        self.assertEqual(len(leaks.items), 2, leaks.items)
        self.assertTrue(leaks.alerted, "[leak-alert] 越线应告警")
        self.assertEqual(stderr.getvalue().count("钉住"), 1,
                         f"[leak-alert] 只在越线报一次：{stderr.getvalue()!r}")
        self.assertEqual(len(IP._LEAKED), global_before,
                         "[leak-alert] 私有 registry 不得改全局 _LEAKED")

    @staticmethod
    def _redirect_stderr(stream):
        import contextlib
        return contextlib.redirect_stderr(stream)


class FakeKernelGuardControls(unittest.TestCase):
    """量具自检：假内核必须在“pending / waiter 存在时关 event”当场判红。"""

    def _make_op(self):
        kernel = FakeKernelApi()
        ev, _ = kernel.create_event()
        h = kernel.alloc_file()
        ov = IP._Overlapped()
        ov.hEvent = ev
        buf = ctypes.create_string_buffer(8)
        ok, err, _got = kernel.read_file(h, buf, 8, ov)
        self.assertFalse(ok)
        self.assertEqual(err, IP.ERROR_IO_PENDING)
        return kernel, ev, h, ov

    def test_pending_event_close_is_flagged(self):
        kernel, ev, h, _ov = self._make_op()
        ok, _err, _closed = kernel.close_handle(ev)
        self.assertFalse(ok)
        self.assertTrue(any("pending" in v[0] for v in kernel.violations),
                        f"假内核必须自曝：{kernel.violations}")

    def test_close_event_with_active_waiter_is_flagged(self):
        kernel = FakeKernelApi()
        ev, _ = kernel.create_event()
        kernel.blocking = True
        result = []
        th = threading.Thread(target=lambda: result.append(
            kernel.wait_for_multiple_objects([ev], IP.INFINITE)), daemon=True)
        th.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not kernel.active_waits.get(ev):
            time.sleep(0.01)
        self.assertTrue(kernel.active_waits.get(ev), "waiter 未登记")
        ok, _err, _closed = kernel.close_handle(ev)
        self.assertFalse(ok)
        th.join(2)
        self.assertTrue(any("waiter" in v[0] for v in kernel.violations),
                        f"假内核必须自曝：{kernel.violations}")


class FakePipe:
    """MpvIpc 的管道替身：只复制 read/write/close 契约。"""

    def __init__(self, chunks=(), *, block_read=False):
        self.h = 1
        self.chunks = list(chunks)
        self.reads = 0
        self.close_calls = 0
        self.writes: list[bytes] = []
        self.block_read = block_read
        self.read_entered = threading.Event()
        self.release = threading.Event()

    def read(self, timeout=None):
        self.reads += 1
        if self.block_read:
            self.read_entered.set()
            self.release.wait(3)
            raise OSError("fake broken pipe")
        if self.chunks:
            return self.chunks.pop(0)
        raise OSError("fake broken pipe")

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.close_calls += 1
        self.h = None
        self.release.set()


class MpvIpcCloseAsync(LifecycleTestBase):
    def _make_ipc(self, pipe):
        with patch.object(IP, "WinPipe", return_value=pipe):
            return IP.MpvIpc(os.path.join(self.data, "mpv-async"))

    def test_close_async_returns_same_ticket_and_joins_without_locking(self):
        pipe = FakePipe(block_read=True)
        ipc = self._make_ipc(pipe)
        with self._redirect_stderr():
            ipc.start_io()
            try:
                self.assertTrue(pipe.read_entered.wait(1))
                t0 = time.monotonic()
                ticket = ipc.close_async()
                first = time.monotonic() - t0
                again = ipc.close_async()
                self.assertIs(again, ticket, "[close-ticket] 重复 close_async 必须返回同一票据")
                self.assertFalse(ticket.done(), "[close-ticket] 票据此时不应提前完成")
                self.assertLess(first, 1.0, "[close-ticket] close_async 不得阻塞调用线程")
                self.assertTrue(ticket.wait(2.0), "[close-ticket] 票据应在 join 完成后兑现")
                self.assertEqual(pipe.close_calls, 1,
                                 f"[close-ticket] 管道只许关闭一次：{pipe.close_calls}")
            finally:
                ipc.close()
        self.assertFalse(ipc.alive)

    def test_close_async_on_unstarted_instance_completes(self):
        pipe = FakePipe()
        ipc = self._make_ipc(pipe)
        ticket = ipc.close_async()
        self.assertTrue(ticket.wait(2.0), "[close-ticket] 未启动实例的票据必须兑现")
        self.assertEqual(pipe.close_calls, 1)
        self.assertTrue(ipc._disconnected)
        self.assertIsNone(ipc._reader)
        self.assertIsNone(ipc._writer)
        ipc.close()
        self.assertEqual(pipe.close_calls, 1, "[close-ticket] 重复 close 不得再关一次")

    def test_sync_close_still_works_after_close_async(self):
        pipe = FakePipe()
        ipc = self._make_ipc(pipe)
        ticket = ipc.close_async()
        self.assertTrue(ticket.wait(2.0))
        ipc.close()
        self.assertEqual(pipe.close_calls, 1)
        self.assertTrue(ipc._disconnected)

    @staticmethod
    def _redirect_stderr():
        import contextlib
        return contextlib.redirect_stderr(io.StringIO())


if __name__ == "__main__":
    unittest.main(verbosity=2)
