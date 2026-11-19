"""mpv JSON IPC 客户端（Windows 命名管道）。

职责边界：只管“连上管道、发命令、收应答/事件”，不含任何播放语义与 UI 概念。
线程模型：一个读线程解析行 JSON；request() 在调用线程阻塞等待 request_id 匹配的应答；
observe/on 注册的回调在读线程中被调用，上层需自行跨线程（Qt 信号队列投递即可）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import itertools
import json
import queue
import sys
import threading
import time
from typing import Any, Callable

ASYNC_ACCEPTED = "async"
INVALID = ctypes.c_void_p(-1).value

_kernel32 = ctypes.windll.kernel32
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                  wt.DWORD, wt.DWORD, ctypes.c_void_p]
_kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_kernel32.ReadFile.restype = wt.BOOL
_kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_kernel32.WriteFile.restype = wt.BOOL
_kernel32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.POINTER(wt.DWORD), wt.BOOL]
_kernel32.GetOverlappedResult.restype = wt.BOOL
_kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_kernel32.CancelIoEx.restype = wt.BOOL
_kernel32.CreateEventW.restype = ctypes.c_void_p
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_kernel32.SetEvent.argtypes = [ctypes.c_void_p]
_kernel32.SetEvent.restype = wt.BOOL
_kernel32.WaitForSingleObject.restype = wt.DWORD
_kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
_kernel32.WaitForMultipleObjects.restype = wt.DWORD
_kernel32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                             wt.BOOL, wt.DWORD]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.GetLastError.restype = wt.DWORD

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_FLAG_OVERLAPPED = 0x40000000
OPEN_EXISTING = 3
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
ERROR_BROKEN_PIPE = 109
# 系统文本（FormatError）证据：232 = ERROR_NO_DATA，231 = ERROR_PIPE_BUSY。
# 旧代码把 232 写成 231，等于把"对端已关闭"认成"管道忙"，断连会被误当成可重试。
ERROR_NO_DATA = 232
ERROR_PIPE_BUSY = 231
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_NOT_FOUND = 1168
ERROR_INVALID_HANDLE = 6
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
READ_SIZE = 65536
# 单行上限：mpv 的 JSON 事件都在几十 KB 以内（播放列表上千条也远小于 1 MiB）。
# 对端一旦输出无换行的巨行（损坏/被替换成别的进程/恶意写管道），没有这条闸门
# `buf += chunk` 就是无界增长——断连比吃满内存诚实。
MAX_LINE_BYTES = 4 * 1024 * 1024
# 关闭/超时时仍可能在途的缓冲区：钉住不放，防止内核往已释放内存写。
# 越线要留告警：正常播放一年也不该出现一条，出现即说明关闭路径在反复超时。
LEAK_ALERT = 64
_LEAKED: list[tuple] = []
_LEAK_ALERTED = False

# 生命周期预算（秒）。取消只是"请求已受理"；确认完成只认 GetOverlappedResult。
CANCEL_OFFSETS = (0.0, 0.1, 0.5)   # 最多三次 CancelIoEx
CANCEL_BUDGET = 2.0                # 从第一次取消起，查结果的总预算
CANCEL_POLL = 0.05
PIPE_CLOSE_WAIT = 2.5              # close() 等回收完成的上限
PIPE_ISSUE_GRACE = 2.0             # 收割器等"已登记未下发"op 交回的宽限（真实时间）
PIPE_LEASE_WAIT = 2.5              # 等借用者退出后再释放句柄
WRITE_TIMEOUT = 10.0
CLOSE_RETRIES = 2                  # 仅"确定仍未关闭"时允许的有限重试


def _pin_leaked(buf: Any, ov: Any) -> None:
    """钉住一个可能仍被内核写入的缓冲区；数量越过阈值只报一次（日志要有信号不许刷屏）。"""
    global _LEAK_ALERTED
    _LEAKED.append((buf, ov))
    if len(_LEAKED) > LEAK_ALERT and not _LEAK_ALERTED:
        _LEAK_ALERTED = True
        print(f"[ipc] 在途管道缓冲区已钉住 {len(_LEAKED)} 个（阈值 {LEAK_ALERT}）："
              f"关闭/超时路径可能在反复泄漏，检查管道对端", file=sys.stderr)


class _Overlapped(ctypes.Structure):
    """OVERLAPPED：Offset/OffsetHigh 与 Pointer 是**同一个 union 槽**，不能摊成三个字段。

    摊开会让 hEvent 偏到第 32 字节（真实结构是 24），内核读到的 hEvent 恒为 0，
    于是 I/O 正常完成（GetOverlappedResult 拿得到字节数）但事件永远不置信号，
    所有基于事件的等待都白等——这套代码"首请求之后全超时"就是它，排查时务必
    先验证 hEvent 而不是怀疑对端。
    """
    _fields_ = [
        ("Internal", ctypes.c_void_p),        # ULONG_PTR
        ("InternalHigh", ctypes.c_void_p),    # ULONG_PTR
        ("_union", ctypes.c_void_p),          # {Offset,OffsetHigh} 与 Pointer 共用
        ("hEvent", ctypes.c_void_p),
    ]


class KernelApi:
    """kernel32 的语义化封装：每个调用都返回 `(值, GetLastError)`。

    真实实现里 CloseHandle 失败只能给出"结果不明"（None），绝不回报 False——
    BOOL 为 0 不等于"句柄确定还开着"，只有假内核才可能有这种证明能力。
    """

    def create_file(self, path: str) -> tuple[int, int]:
        h = _kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                  OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if h and h != INVALID:
            return h, 0
        return 0, _kernel32.GetLastError()

    def create_event(self) -> tuple[int, int]:
        ev = _kernel32.CreateEventW(None, True, False, None)
        if ev and ev != INVALID:
            return ev, 0
        return 0, _kernel32.GetLastError()

    def read_file(self, h, buf, size, ov) -> tuple[bool, int, int]:
        got = wt.DWORD(0)
        ok = _kernel32.ReadFile(h, buf, wt.DWORD(size), ctypes.byref(got), ctypes.byref(ov))
        if ok:
            return True, 0, got.value
        return False, _kernel32.GetLastError(), 0

    def write_file(self, h, buf, size, ov) -> tuple[bool, int, int]:
        got = wt.DWORD(0)
        ok = _kernel32.WriteFile(h, buf, wt.DWORD(size), ctypes.byref(got), ctypes.byref(ov))
        if ok:
            return True, 0, got.value
        return False, _kernel32.GetLastError(), 0

    def get_overlapped_result(self, h, ov) -> tuple[bool, int, int]:
        got = wt.DWORD(0)
        ok = _kernel32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(got), False)
        if ok:
            return True, 0, got.value
        return False, _kernel32.GetLastError(), 0

    def cancel_io_ex(self, h, ov) -> tuple[bool, int]:
        ok = _kernel32.CancelIoEx(h, ctypes.byref(ov))
        return bool(ok), 0 if ok else _kernel32.GetLastError()

    def set_event(self, h) -> tuple[bool, int]:
        ok = _kernel32.SetEvent(h)
        return bool(ok), 0 if ok else _kernel32.GetLastError()

    def wait_for_multiple_objects(self, handles, timeout_ms: int) -> int:
        arr = (ctypes.c_void_p * len(handles))(*handles)
        return _kernel32.WaitForMultipleObjects(len(handles), arr, False, timeout_ms)

    def close_handle(self, h) -> tuple[bool, int, bool | None]:
        if not h:
            return True, 0, True
        ok = _kernel32.CloseHandle(h)
        if ok:
            return True, 0, True
        err = _kernel32.GetLastError()
        if err == ERROR_INVALID_HANDLE:
            return False, err, None       # 已关闭/不归我们：不可重试
        return False, err, None           # 真实内核无法证明"仍然开着"：不重试


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Scheduler:
    def spawn(self, fn, name: str):
        th = threading.Thread(target=fn, name=name, daemon=True)
        th.start()
        return th


class LeakRegistry:
    """私有泄漏名册：默认只影响本实例；全局名册仍由 `_pin_leaked` 维系。

    越线只在越线那一刻告警一次（日志要有信号，不许刷屏）。
    """

    def __init__(self, alert: int = LEAK_ALERT, sink: Callable | None = None, label: str = ""):
        self.alert = int(alert)
        self.items: list[tuple] = []
        self.alerted = False
        self._sink = sink
        self.label = label
        self._lock = threading.Lock()

    def pin(self, buf, ov=None, detail: str = "") -> None:
        with self._lock:
            self.items.append((buf, ov, detail))
            n = len(self.items)
            fire = n > self.alert and not self.alerted
            if fire:
                self.alerted = True
        if self._sink is not None:
            self._sink(buf, ov)
        if fire:
            print(f"[ipc] {self.label or '在途 I/O'}已钉住 {n} 个"
                  f"（阈值 {self.alert}）：关闭/超时路径可能在反复泄漏，检查管道对端",
                  file=sys.stderr)


DEFAULT_LEAKS = LeakRegistry(alert=0x7FFFFFFF, sink=_pin_leaked, label="管道在途缓冲区")
_KERNEL = KernelApi()
_CLOCK = Clock()
_SCHEDULER = Scheduler()


class _Op:
    """一次重叠 I/O 的唯一 owner 包：buffer / OVERLAPPED / 完成 event / 原始句柄。"""

    _seq = itertools.count(1)

    def __init__(self, kind: str, buf, size: int):
        self.id = next(_Op._seq)
        self.kind = kind
        self.buf = buf
        self.size = size
        self.h: int | None = None
        self.ov: _Overlapped | None = None
        self.ev: int | None = None
        self.issued = False
        self.issued_ev = threading.Event()
        self.status = "new"          # new|pending|done|aborted|error
        self.bytes = 0
        self.error = 0
        self.cancel_attempts = 0
        self.lock = threading.RLock()


class WinPipe:
    """命名管道的重叠（异步）读写与句柄生命周期。

    生命周期只有一条主线：请求停止 → 确认完成 → 回收。CancelIoEx 只发起取消，
    不是完成屏障；只有 GetOverlappedResult 能证明 I/O 已终止。未确认完成时就进入
    STALLED，强持有 h / stop_ev / Op（buffer、OVERLAPPED、event）直到进程退出，
    绝不关闭可能仍被内核引用的对象。

    同一个**同步**句柄上系统会串行化所有 I/O，所以句柄必须 FILE_FLAG_OVERLAPPED。
    读侧始终只有一个读挂起在途；超时只是换个等待，退出由独立 stop_ev 唤醒。
    """

    def __init__(self, path: str, *, kernel: KernelApi | None = None,
                 clock: Clock | None = None, scheduler: Scheduler | None = None,
                 leaks: LeakRegistry | None = None):
        self.path = path
        self._kernel = kernel if kernel is not None else _KERNEL
        self._clock = clock if clock is not None else _CLOCK
        self._scheduler = scheduler if scheduler is not None else _SCHEDULER
        self._leaks = leaks if leaks is not None else DEFAULT_LEAKS
        self.h: int | None = None
        self.stop_ev: int | None = None
        self.last_error = 0
        self.last_close_error = 0
        self._lock = threading.RLock()
        self._read_gate = threading.RLock()
        self._tls = threading.local()
        self._state = "new"                 # new|opening|open|stopping|closing|closed|stalled
        self._gen = 0
        self._frozen = False
        self._ops: dict[int, _Op] = {}
        self._quarantine: list[_Op] = []
        self._read_op_id: int | None = None
        self._lease_count = 0
        self._leases_zero = threading.Event()
        self._leases_zero.set()
        self._done = threading.Event()
        self._reaper: threading.Thread | None = None
        self._reaper_started = False
        self._closing_handles: set[int] = set()
        self._dead_handles: set[int] = set()
        self._unclosed: list[tuple] = []
        self.stats = {"reads": 0, "writes": 0, "short_writes": 0,
                      "timeouts": 0, "stalled": 0}
        ev, err = self._kernel.create_event()
        if not ev:
            raise OSError(f"CreateEventW 失败 GetLastError={err}")
        self.stop_ev = ev

    # ---------------- 借用与状态 ----------------
    def _acquire_lease(self, *, check_open: bool) -> None:
        with self._lock:
            if self._frozen:
                raise OSError("管道已停止（不再接受新的 I/O）")
            if check_open and (self._state != "open" or self.h is None):
                raise OSError("管道未打开或已停止")
            self._lease_count += 1
            self._tls.depth = getattr(self._tls, "depth", 0) + 1
            self._leases_zero.clear()

    def _release_lease(self) -> None:
        with self._lock:
            self._lease_count = max(0, self._lease_count - 1)
            depth = max(0, getattr(self._tls, "depth", 0) - 1)
            self._tls.depth = depth
            if self._lease_count == 0:
                self._leases_zero.set()

    def _lease_depth(self) -> int:
        return getattr(self._tls, "depth", 0)

    @property
    def _pending(self):
        """兼容只读视图（旧探针断言未 open 时 `_pending is None`）。"""
        with self._lock:
            op = self._ops.get(self._read_op_id) if self._read_op_id is not None else None
            if op is None or op.status != "pending":
                return None
            return (op.buf, op.ov, op.ev, wt.DWORD(0), False)

    def snapshot(self) -> dict:
        with self._lock:
            handles = ((1 if self.h else 0) + (1 if self.stop_ev else 0)
                       + len(self._ops) + len(self._quarantine))
            return {"state": self._state, "ops": len(self._ops),
                    "leases": self._lease_count, "handles": handles,
                    "quarantine": len(self._quarantine), "gen": self._gen,
                    "frozen": self._frozen, "last_error": self.last_error,
                    "last_close_error": self.last_close_error,
                    "unclosed": list(self._unclosed)}

    # ---------------- 打开 ----------------
    def try_open(self, dead: Callable[[], bool] | None = None) -> bool:
        """非阻塞地试一次 CreateFile；close 已赢时迟到句柄交回本线程关闭。"""
        with self._lock:
            if self._state == "open" and self.h is not None:
                return True
            if self._frozen or self._state != "new":
                return False
            self._state = "opening"
        self._acquire_lease(check_open=False)
        try:
            if dead is not None and dead():
                raise IpcError("mpv 进程已退出，管道不会再出现")
            h, err = self._kernel.create_file(self.path)
            late = None
            with self._lock:
                if not h:
                    self.last_error = err
                    if not self._frozen and self._state == "opening":
                        self._state = "new"
                    return False
                if self._frozen or self._state != "opening":
                    late = h
                else:
                    self.h = h
                    self.last_error = 0
                    self._state = "open"
            if late is not None:
                self._close_late_handle(late)
                return False
            return True
        finally:
            self._release_lease()

    def open(self, timeout: float, dead: Callable[[], bool] | None = None) -> None:
        deadline = self._clock.monotonic() + timeout
        last_err = 0
        while self._clock.monotonic() < deadline:
            if self.try_open(dead):
                return
            if self._frozen:
                raise IpcError("IPC 已关闭，连接已取消")
            last_err = self.last_error
            self._clock.sleep(0.02 if last_err == ERROR_PIPE_NOT_CONNECTED else 0.05)
        raise IpcError(f"打开管道失败 {self.path}（GetLastError={last_err}）")

    def _close_late_handle(self, h: int) -> None:
        ok, err, closed = self._kernel.close_handle(h)
        if ok or err == ERROR_INVALID_HANDLE:
            return
        with self._lock:
            self.last_close_error = err
            self._dead_handles.add(h)
            self._unclosed.append(("late_file", h))
            self._frozen = True
            self._state = "stalled"
        self._done.set()

    # ---------------- Op 准备 / 下发 ----------------
    def _prepare_op(self, kind: str, data: bytes | None = None) -> _Op | None:
        if kind == "read":
            buf = ctypes.create_string_buffer(READ_SIZE)
            size = READ_SIZE
        else:
            size = len(data or b"")
            buf = ctypes.create_string_buffer(data or b"", max(1, size))
        ev, err = self._kernel.create_event()
        if not ev:
            raise OSError(f"CreateEventW 失败 GetLastError={err}")
        ov = _Overlapped()
        ov.hEvent = ev
        op = _Op(kind, buf, size)
        op.ov, op.ev = ov, ev
        with self._lock:
            late = self._frozen or self._state != "open" or self.h is None
            if not late:
                op.h = self.h
                self._ops[op.id] = op
        if late:
            self._close_bare_event(ev)
            return None
        return op

    def _close_bare_event(self, ev: int) -> None:
        ok, err, _closed = self._kernel.close_handle(ev)
        if not ok and err != ERROR_INVALID_HANDLE:
            with self._lock:
                self.last_close_error = err
                self._unclosed.append(("bare_event", ev))
                self._dead_handles.add(ev)

    def _issue(self, op: _Op) -> bool:
        fn = self._kernel.read_file if op.kind == "read" else self._kernel.write_file
        ok, err, got = fn(op.h, op.buf, op.size, op.ov)
        with self._lock:
            if ok:
                op.status = "done"
                op.bytes = got
            elif err == ERROR_IO_PENDING:
                op.status = "pending"
            else:
                op.status = "error"
                op.error = err
            if op.kind == "read":
                self.stats["reads"] += 1
            else:
                self.stats["writes"] += 1
            frozen = self._frozen
        op.issued = True
        op.issued_ev.set()
        return frozen

    # ---------------- 结算 ----------------
    def _settle(self, op: _Op, *, cancel: bool) -> _Op:
        with op.lock:
            if op.status != "pending":
                return op
            if cancel:
                self._kernel.cancel_io_ex(op.h, op.ov)
                op.cancel_attempts += 1
            ok, err, got = self._kernel.get_overlapped_result(op.h, op.ov)
            if ok:
                op.status, op.bytes = "done", got
            elif err == ERROR_IO_PENDING:
                op.status = "pending"
            elif err in (ERROR_OPERATION_ABORTED, ERROR_BROKEN_PIPE,
                         ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED):
                op.status, op.error = "aborted", err
            else:
                op.status, op.error = "error", err
            return op

    def _settle_with_budget(self, op: _Op) -> _Op:
        """先取消、再确认：取消成功不算完成；2 秒预算内查不出结果就交回隔离。"""
        start = self._clock.monotonic()
        deadline = start + CANCEL_BUDGET
        attempt = 0
        while True:
            with op.lock:
                if op.status != "pending":
                    return op
            now = self._clock.monotonic()
            if attempt < len(CANCEL_OFFSETS) and now >= start + CANCEL_OFFSETS[attempt]:
                self._settle(op, cancel=True)
                attempt += 1
            else:
                self._settle(op, cancel=False)
            with op.lock:
                if op.status != "pending":
                    return op
            if self._clock.monotonic() >= deadline:
                return op
            self._clock.sleep(CANCEL_POLL)

    def _retire(self, op: _Op) -> bool:
        """结算已确认后，才关闭这个 op 自己的完成 event。"""
        with self._lock:
            self._ops.pop(op.id, None)
            if self._read_op_id == op.id:
                self._read_op_id = None
            ev = op.ev
        if ev is None:
            return True
        ok, err, _closed = self._kernel.close_handle(ev)
        if ok or err == ERROR_INVALID_HANDLE:
            op.ev = None
            return True
        with self._lock:
            if all(q.id != op.id for q in self._quarantine):
                self._quarantine.append(op)
            self.last_close_error = err
            self._frozen = True
            self._state = "stalled"
        self._done.set()
        return False

    def _quarantine_op(self, op: _Op, reason: str) -> None:
        """未确认完成：钉住整个 owner 包，不关 h/stop/event，不伪造回收。"""
        with self._lock:
            self._ops.pop(op.id, None)
            if self._read_op_id == op.id:
                self._read_op_id = None
            if all(q.id != op.id for q in self._quarantine):
                self._quarantine.append(op)
            self._frozen = True
            self._state = "stalled"
            self.stats["stalled"] += 1
        self._leaks.pin(op.buf, op.ov, f"{op.kind} op#{op.id}: {reason}")
        self._done.set()

    # ---------------- 读 ----------------
    def _wait2(self, ev: int, timeout: float | None) -> int:
        with self._lock:
            stop = self.stop_ev
        handles = [ev] + ([stop] if stop else [])
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        return self._kernel.wait_for_multiple_objects(handles, ms)

    def _current_read_op(self) -> _Op | None:
        with self._lock:
            if self._read_op_id is None:
                return None
            op = self._ops.get(self._read_op_id)
            if op is None or not op.issued:
                return None
            return op

    def read(self, timeout: float | None = None) -> bytes:
        """取下一块数据；超时返回 b''，挂起的读原样保留给下一次。

        轮询复用同一个在途读：不重复 ReadFile、不丢已到字节。关闭后的迟到下发
        不做任何完成等待，直接交回收割器（它只认 GetOverlappedResult）。
        """
        self._acquire_lease(check_open=True)
        try:
            with self._read_gate:
                op = self._current_read_op()
                if op is None:
                    op = self._prepare_op("read")
                    if op is None:
                        raise OSError("管道已停止（不再接受新的 I/O）")
                    self._issue(op)
                    with self._lock:
                        if op.status == "pending" and not self._frozen:
                            self._read_op_id = op.id
                    if self._frozen:
                        raise OSError("管道已停止")
                    if op.status != "pending":
                        return self._read_result(op)
                sig = self._wait2(op.ev, timeout)
                if sig == WAIT_OBJECT_0 + 1:
                    raise OSError("管道已停止")
                if sig == WAIT_FAILED:
                    self._quarantine_op(op, "等待读完成失败（WAIT_FAILED）")
                    raise OSError("等待读完成失败（WAIT_FAILED），管道进入 STALLED")
                if sig == WAIT_TIMEOUT:
                    return b""
                self._settle(op, cancel=False)
                return self._read_result(op)
        finally:
            self._release_lease()

    def _read_result(self, op: _Op) -> bytes:
        with op.lock:
            status, n, err = op.status, op.bytes, op.error
        if status == "done":
            self._retire(op)
            return op.buf.raw[:n]
        if status in ("aborted", "error"):
            self._retire(op)
            if status == "aborted":
                raise OSError("读未完成（对端已关闭管道）")
            raise OSError(f"管道读失败 GetLastError={err}")
        if self._frozen:
            raise OSError("管道已停止")
        return b""

    # ---------------- 写 ----------------
    def write(self, data: bytes) -> None:
        """同步写；短写续写剩余字节，超时/停止都先确认取消结果再决定回收还是隔离。"""
        if not data:
            return
        self._acquire_lease(check_open=True)
        try:
            offset = 0
            while offset < len(data):
                chunk = data[offset:]
                op = self._prepare_op("write", chunk)
                if op is None:
                    raise OSError("管道已停止（不再接受新的 I/O）")
                self._issue(op)
                if op.status == "pending":
                    sig = self._wait2(op.ev, WRITE_TIMEOUT)
                    if sig == WAIT_OBJECT_0 + 1:
                        self._settle_with_budget(op)
                        with op.lock:
                            st = op.status
                        if st == "pending":
                            self._quarantine_op(op, "停止后写未确认")
                            raise OSError("管道已停止（写未确认，已进入 STALLED）")
                        self._retire(op)
                        raise OSError("管道已停止")
                    if sig == WAIT_FAILED:
                        self._quarantine_op(op, "等待写完成失败（WAIT_FAILED）")
                        raise OSError("等待写完成失败（WAIT_FAILED），管道进入 STALLED")
                    if sig == WAIT_TIMEOUT:
                        self.stats["timeouts"] += 1
                        self._settle_with_budget(op)
                        with op.lock:
                            st = op.status
                        if st == "pending":
                            self._quarantine_op(op, "写超时未确认")
                            raise OSError("WriteFile 超时（管道背压 10s 未消化，完成未确认）")
                        self._retire(op)
                        if st == "aborted":
                            raise OSError("WriteFile 超时后确认取消")
                        raise OSError("WriteFile 超时（管道背压 10s 未消化）")
                    self._settle(op, cancel=False)
                with op.lock:
                    st, n, err = op.status, op.bytes, op.error
                if st == "pending":
                    self._quarantine_op(op, "写完成后仍未确认")
                    raise OSError("WriteFile 完成未确认，管道进入 STALLED")
                self._retire(op)
                if st == "done":
                    if n <= 0:
                        raise OSError(f"WriteFile 短写：0/{len(chunk)}（无进展）")
                    offset += n
                    if n < len(chunk):
                        self.stats["short_writes"] += 1
                    continue
                if st == "aborted":
                    raise OSError("写未完成（已取消）")
                raise OSError(f"WriteFile 失败 GetLastError={err}")
        finally:
            self._release_lease()

    # ---------------- 停止 / 回收 ----------------
    def request_stop(self) -> None:
        """冻结新 I/O、唤醒等待者；无在途/无借用者时同步回收，否则交给单个收割器。"""
        with self._lock:
            if self._state in ("closed", "stalled"):
                return
            self._frozen = True
            if self._state not in ("stopping", "closing"):
                self._state = "stopping"
            stop = self.stop_ev
            trivial = (not self._ops and not self._quarantine
                       and self._lease_count == 0 and not self._reaper_started)
            spawn = False
            if not trivial and not self._reaper_started:
                self._reaper_started = True
                self._reaper = None
                spawn = True
            gen = self._gen
        if stop is not None:
            ok, err = self._kernel.set_event(stop)
            if not ok and err != ERROR_INVALID_HANDLE:
                with self._lock:
                    self.last_error = err
        if trivial:
            self._finalize(gen)
        elif spawn:
            self._reaper = self._scheduler.spawn(
                lambda: self._reap(gen), f"winpipe-reaper-{gen}")

    def wait_closed(self, timeout: float | None = None) -> bool:
        with self._lock:
            if self._state in ("closed", "stalled"):
                return self._state == "closed"
        self._done.wait(timeout)
        with self._lock:
            return self._state == "closed"

    def close(self) -> None:
        """幂等；回调/借用线程内重入不等待，避免 join 自己。"""
        self.request_stop()
        with self._lock:
            if self._state in ("closed", "stalled"):
                return
            if threading.current_thread() is self._reaper or self._lease_depth() > 0:
                return
        self._done.wait(PIPE_CLOSE_WAIT)

    def _reap(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            ops = list(self._ops.values())
        for op in ops:
            with self._lock:
                if op.id not in self._ops:
                    continue
            if not op.issued:
                op.issued_ev.wait(PIPE_ISSUE_GRACE)
            if not op.issued:
                self._quarantine_op(op, "停止时仍未下发（owner 未回报）")
                return
            self._settle_with_budget(op)
            with op.lock:
                status = op.status
            if status == "pending":
                self._quarantine_op(op, "取消预算内未确认完成")
                return
            self._retire(op)
            with self._lock:
                if self._state == "stalled":
                    return
        if not self._leases_zero.wait(PIPE_LEASE_WAIT):
            with self._lock:
                if gen == self._gen and self._state not in ("closed", "stalled"):
                    self._state = "stalled"
                    self._frozen = True
            self._done.set()
            return
        self._finalize(gen)

    def _finalize(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen or self._state in ("closed", "stalled"):
                return
            self._frozen = True
            self._state = "closing"
            leftover = list(self._ops)
        if leftover:
            for op in leftover:
                self._quarantine_op(op, "回收时仍有在途 op")
            return
        if self.h is not None and not self._close_field("h"):
            return
        if self.stop_ev is not None and not self._close_field("stop_ev"):
            return
        with self._lock:
            self._state = "closed"
        self._done.set()

    def _close_field(self, field: str) -> bool:
        with self._lock:
            h = getattr(self, field)
            if h is None:
                return True
            self._closing_handles.add(h)
        retries = 0
        while True:
            ok, err, closed = self._kernel.close_handle(h)
            if ok:
                with self._lock:
                    setattr(self, field, None)
                    self._closing_handles.discard(h)
                return True
            if err == ERROR_INVALID_HANDLE:
                # 已经关了/不归我们：不重试旧值，也不计入未关闭。
                with self._lock:
                    setattr(self, field, None)
                    self._closing_handles.discard(h)
                return True
            if closed is None:
                break           # 结果不明：绝不重试旧句柄值
            retries += 1
            if retries > CLOSE_RETRIES:
                break
        with self._lock:
            self._closing_handles.discard(h)
            self.last_close_error = err
            if not self.last_error:
                self.last_error = err
            self._dead_handles.add(h)
            self._unclosed.append((field, h))
            self._frozen = True
            self._state = "stalled"
        self._done.set()
        return False


class IpcError(RuntimeError):
    pass


class CloseTicket:
    """异步关闭票据：wait/done 都不持状态锁，调用方可以在任意线程等。"""

    def __init__(self):
        self._done = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        return bool(self._done.wait(timeout))

    def done(self) -> bool:
        return self._done.is_set()

    def _finish(self) -> None:
        self._done.set()


class MpvIpc:
    def __init__(self, pipe_path: str):
        self.pipe_path = pipe_path
        self._pipe = WinPipe(pipe_path)
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._proc = None                           # mpv 进程（协议损坏时要能请它重启）
        self._outq: queue.SimpleQueue = queue.SimpleQueue()
        self._alive = False
        self._closing = False
        self._disconnect_lock = threading.RLock()
        self._disconnected = False
        self._opening = False                     # open 返回前由启动者独占句柄清理责任
        self._close_ticket: CloseTicket | None = None
        self._pipe_close_done = False
        self._observers: dict[str, int] = {}        # 属性名 -> mpv observe id
        self._obs_callbacks: dict[int, Callable] = {}
        self._event_callbacks: dict[str, list[Callable]] = {}
        self._any_event_callbacks: list[Callable] = []
        # 回调名册的锁：注册发生在 UI 线程，遍历发生在读线程。快照在锁内取、调用在锁外做——
        # 回调里再注册/发请求都是常态，握着锁回调就是把两条线程焊死。
        self._cb_lock = threading.RLock()
        self.stats = {"sent": 0, "received": 0, "events": 0, "timeouts": 0, "errors": 0,
                      "reconnects": 0, "rtt_ms_max": 0.0, "rtt_ms_sum": 0.0, "rtt_n": 0}

    # ---------------- 连接 ----------------
    def try_connect(self, proc=None) -> bool:
        """非阻塞握手：管道已经出现就起读写线程，否则回 False。

        与 `connect()` 共用同一段 `start_io`，所以"怎么连上"只有一份实现；区别只在
        谁负责等待。GUI 那条路（MainWindow._poll_engine_start）用这个一帧试一次。
        """
        return self._connect(timeout=None, proc=proc)

    def connect(self, timeout: float = 8.0, proc=None) -> bool:
        return self._connect(timeout=timeout, proc=proc)

    def _connect(self, timeout: float | None, proc) -> bool:
        with self._disconnect_lock:
            if self._disconnected or self._closing:
                raise IpcError("IPC 已关闭，不能重新连接")
            if self._reader is not None:
                return True
            if self._opening:
                raise IpcError("IPC 正在连接")
            self._opening = True
            if proc is not None:
                self._proc = proc
        dead = (lambda: proc.poll() is not None) if proc is not None else None
        try:
            if timeout is None:
                opened = self._pipe.try_open(dead=dead)
            else:
                self._pipe.open(timeout, dead=dead)
                opened = True
        finally:
            with self._disconnect_lock:
                self._opening = False
                abandoned = self._disconnected
            # close 不与 CreateFile/句柄发布并行：若 close 已赢，启动者收拢迟到句柄。
            # 否则清理责任已转交给 _disconnect，后续 start_io 仍须检查终态。
            if abandoned:
                self._close_pipe()
        if abandoned:
            raise IpcError("IPC 已关闭，连接已取消")
        if not opened:
            return False
        self.start_io()
        return True

    def start_io(self) -> None:
        """启动完整线程对；活连接重复调用是空操作，关闭是本实例的终态。"""
        def run_io(loop):
            # 等构造/start 阶段释放锁。半启动回滚不能通过假读写故障去恢复进程。
            with self._disconnect_lock:
                if not self._alive or self._closing:
                    return
            loop()

        failure = None
        with self._disconnect_lock:
            if self._disconnected or self._closing:
                raise IpcError("IPC 已关闭，不能启动读写线程")
            if self._opening:
                raise IpcError("IPC 管道尚在打开")
            if self._reader is not None or self._writer is not None:
                return
            try:
                self._reader = threading.Thread(target=run_io, args=(self._read_loop,),
                                                name="mpv-ipc-read", daemon=True)
                self._writer = threading.Thread(target=run_io, args=(self._write_loop,),
                                                name="mpv-ipc-write", daemon=True)
                self._alive = True
                self._reader.start()
                self._writer.start()
            except Exception as exc:
                self._alive = False
                self._closing = True
                failure = exc
        if failure is not None:
            reason = f"IPC 线程启动失败：{type(failure).__name__}: {failure}"
            self._disconnect(reason, recover=False)
            self.close()  # 断连、pending 唤醒、管道清理和 join 均不嵌套在状态锁内。
            raise IpcError(reason) from failure

    def close(self) -> None:
        self._disconnect("IPC 已关闭", recover=False)
        self._join_io_threads()

    def close_async(self) -> CloseTicket:
        """返回关闭票据；join 在锁外、后台线程里做，不阻塞 UI/调用线程。

        重复调用返回同一张票据；管道关闭责任要么已由 _disconnect（live 路径）/
        启动者（open 在途）承担，要么由本票据的后台收尾线程承担——恰好一次。
        """
        with self._disconnect_lock:
            if self._close_ticket is not None:
                return self._close_ticket
            ticket = CloseTicket()
            self._close_ticket = ticket
        _converted, deferred = self._disconnect("IPC 已关闭", recover=False,
                                                defer_pipe=True)

        def finish():
            try:
                if deferred:
                    self._close_pipe()
                self._join_io_threads()
            finally:
                ticket._finish()

        threading.Thread(target=finish, name="mpv-ipc-close", daemon=True).start()
        return ticket

    def _join_io_threads(self) -> None:
        """join 在锁外；跳过未启动/当前线程，超时未退的引用继续保留。"""
        with self._disconnect_lock:
            threads = (self._reader, self._writer)
        current = threading.current_thread()
        for th in threads:
            # ident 在成功启动后保留到退出；未启动对象不能 join。
            if th is not None and th is not current and th.ident is not None:
                th.join(timeout=1.5)
        with self._disconnect_lock:
            if self._reader is threads[0] and self._reader is not None and not self._reader.is_alive():
                self._reader = None
            if self._writer is threads[1] and self._writer is not None and not self._writer.is_alive():
                self._writer = None

    def _disconnect(self, reason: str, recover: bool,
                    *, defer_pipe: bool = False) -> tuple[bool, bool]:
        """主动关闭、读故障、写故障共用一次转换；先到者决定是否请求恢复。

        返回 (是否完成本次转换, 是否把管道关闭责任留给调用方的后台收尾)。
        """
        with self._disconnect_lock:
            if self._disconnected:
                return False, False
            self._disconnected = True
            self._alive = False
            self._closing = True
            # 固定本次故障的进程，迟到的 close 不得撤销恢复或改杀别的进程。
            proc = self._proc if recover else None
            close_pipe = not self._opening and not defer_pipe
            deferred = not self._opening and defer_pipe
            self._outq.put(None)
            self._fail_pending(reason)
            if recover:
                self.stats["errors"] += 1
        # 不持状态锁做慢清理；先唤醒请求，读写线程也不互相 join。
        try:
            if recover:
                print(f"[ipc] {reason}", file=sys.stderr)
        finally:
            try:
                if close_pipe:
                    self._close_pipe()
            finally:
                try:
                    if proc is not None and proc.poll() is None:
                        # 上层看门狗按既有流程重启并恢复播放位置。
                        proc.kill()
                except Exception as exc:
                    print(f"[ipc] 断线恢复失败：{exc}", file=sys.stderr)
        return True, deferred

    def _close_pipe(self) -> None:
        """管道关闭恰好一次：close 与 close_async 可以并发但不能重复 CloseHandle。"""
        with self._disconnect_lock:
            if self._pipe_close_done:
                return
            self._pipe_close_done = True
        try:
            self._pipe.close()
        except Exception as exc:
            print(f"[ipc] 关闭管道失败：{exc}", file=sys.stderr)

    def _fail_pending(self, reason: str) -> None:
        """把还在等应答的请求一次性唤醒。

        读线程一死，所有在途 request 就只可能等到超时（默认 3 秒）——界面上表现为
        "点了没反应，几秒后突然报错"，而内核其实早没了。这里给它们一个立刻返回的
        错误，调用方按正常失败路径收尾。写线程死亡同理（命令根本没发出去）。
        """
        with self._disconnect_lock:
            pend, self._pending = self._pending, {}
            for slot in pend.values():
                try:
                    slot["reply"] = {"error": reason or "ipc-closed"}
                    slot["done"].set()
                except Exception:
                    pass

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def last_open_error(self) -> int:
        """最近一次管道未出现的 GetLastError（超时提示里要带上，否则只剩一句"没连上"）。"""
        return self._pipe.last_error

    # ---------------- 发送 ----------------
    def _write(self, payload: dict) -> None:
        if not self._alive or self._pipe.h is None:
            raise IpcError("IPC 未连接")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        # 入队与断连互斥；序列化期间连接也可能已经关闭。
        with self._disconnect_lock:
            if not self._alive or self._closing:
                raise IpcError("IPC 未连接")
            # 交给写线程串行发出：管道背压时 UI 线程不会被 WriteFile 卡住
            self._outq.put(data)

    def _write_loop(self) -> None:
        reason = "IPC 写线程已退出"
        try:
            while self._alive:
                try:
                    data = self._outq.get(timeout=0.2)
                except queue.Empty:
                    continue
                if data is None or not self._alive:
                    break
                with self._lock:
                    if not self._alive:
                        break
                    self._pipe.write(data)
                self.stats["sent"] += 1
        except Exception as exc:
            reason = f"IPC 写线程已退出：{type(exc).__name__}: {exc}"
        finally:
            self._disconnect(reason, recover=True)

    def fire(self, command: list) -> None:
        """发完即走，不等结果（用于高频 set/add/cycle）。"""
        self._write({"command": command, "async": True})

    def request(self, command: list, timeout: float = 3.0) -> Any:
        """发命令并等待最终应答，返回 data；出错抛 IpcError。

        注意：mpv 0.41 起 request_id 必须是整数（实测字符串会得到
        {"error":"invalid parameter"}），这里用自增整数。
        """
        rid = next(self._ids)
        slot: dict[str, Any] = {"done": threading.Event(), "reply": None}
        t0 = time.perf_counter()
        with self._disconnect_lock:
            # 读线程里发同步请求 = 自我死锁（应答只有本线程能投递），必须立刻报错而不是
            # 静默等到超时——曾经就是回调里 get() 把首包之后的请求全拖成 1.5s 超时。
            if self._reader is not None and threading.get_ident() == self._reader.ident:
                self.stats["errors"] += 1
                raise IpcError("禁止在 IPC 读线程内发同步请求（会自我死锁），改用属性缓存或投递到主线程")
            if self._disconnected:
                raise IpcError("IPC 未连接")
            self._pending[rid] = slot
        try:
            self._write({"command": command, "request_id": rid, "async": True})
        except IpcError:
            with self._disconnect_lock:
                self._pending.pop(rid, None)
            raise
        if not slot["done"].wait(timeout):
            with self._disconnect_lock:
                self._pending.pop(rid, None)
            self.stats["timeouts"] += 1
            raise IpcError(f"命令超时({timeout}s): {command}")
        reply = slot["reply"] or {}
        ms = (time.perf_counter() - t0) * 1000.0
        self.stats["rtt_ms_sum"] += ms
        self.stats["rtt_n"] += 1
        self.stats["rtt_ms_max"] = max(self.stats["rtt_ms_max"], ms)
        err = reply.get("error", "unknown")
        if err != "success":
            self.stats["errors"] += 1
            raise IpcError(f"命令失败({err}): {command}")
        return reply.get("data")

    def request_async(self, command: list, callback, timeout: float = 8.0) -> None:
        """后台线程等应答，调用线程立刻返回。

        用在"命令本身慢、又有返回值、又不该卡住 UI"的场合——截图就是：实测 mpv 会把
        产物路径放进命令应答的 ``data.filename``，而 **不发 file-saved 事件**
        （0.41.0-1101 全量事件列表里根本没有它），所以只能靠应答取路径。
        callback(data, err)：err 为 None 表示成功。
        """
        def work():
            try:
                callback(self.request(command, timeout=timeout), None)
            except IpcError as exc:
                callback(None, str(exc))
            except Exception as exc:  # 回调里炸了也不能让线程静默消失
                callback(None, f"回调异常：{exc}")

        threading.Thread(target=work, name="mpv-async-cmd", daemon=True).start()

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self.request(["get_property", name], timeout=2.0)
        except IpcError:
            return default

    def set(self, name: str, value: Any) -> None:
        self.fire(["set_property", name, value])

    def add(self, name: str, amount: Any) -> None:
        self.fire(["add", name, amount])

    def cycle(self, name: str, direction: str | None = None) -> None:
        cmd = ["cycle", name]
        if direction:
            cmd.append(direction)
        self.fire(cmd)

    # ---------------- 观察 ----------------
    def observe(self, name: str, callback: Callable[[Any], None]) -> None:
        with self._cb_lock:
            if name in self._observers:
                self._obs_callbacks[self._observers[name]] = callback
                return
            mid = next(self._ids)
            self._observers[name] = mid
            self._obs_callbacks[mid] = callback
            alive = self._alive
        # 请求放在锁外：request() 会等应答，而应答要靠读线程投递，读线程派发时又要这把锁。
        if alive:
            self.request(["observe_property", mid, name], timeout=2.0)

    def reobserve_all(self) -> None:
        with self._cb_lock:
            pairs = list(self._observers.items())
        for name, mid in pairs:
            try:
                self.request(["observe_property", mid, name], timeout=1.0)
            except IpcError:
                pass

    def on(self, event: str, callback: Callable[[dict], None]) -> None:
        with self._cb_lock:
            self._event_callbacks.setdefault(event, []).append(callback)

    def on_any_event(self, callback: Callable[[dict], None]) -> None:
        with self._cb_lock:
            self._any_event_callbacks.append(callback)

    # ---------------- 接收 ----------------
    def _read_loop(self) -> None:
        buf = bytearray()
        reason = "IPC 读线程已退出"
        try:
            while self._alive:
                chunk = self._pipe.read(timeout=0.5)
                # read 可能在主动关闭之后才返回旧字节；不得再解释/派发。
                if not self._alive or self._closing:
                    break
                if not chunk:
                    continue        # b'' 是 WinPipe 轮询超时，不是 EOF
                buf += chunk
                while self._alive and not self._closing:
                    nl = buf.find(b"\n")
                    # 先检查原始行长度，再切片/strip；完整行也不能绕过闸门。
                    size = len(buf) if nl < 0 else nl
                    if size > MAX_LINE_BYTES:
                        reason = f"IPC 单行超限（上限 {MAX_LINE_BYTES} 字节），连接已断开"
                        return
                    if nl < 0:
                        break
                    line = bytes(buf[:nl]).strip()
                    del buf[: nl + 1]
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        self.stats["errors"] += 1
                        continue
                    if not isinstance(msg, dict):
                        self.stats["errors"] += 1
                        continue
                    self._dispatch(msg)
        except Exception as exc:
            reason = f"IPC 读线程已退出：{type(exc).__name__}: {exc}"
        finally:
            self._disconnect(reason, recover=True)

    def _dispatch(self, msg: dict) -> None:
        with self._disconnect_lock:
            if self._disconnected:
                return
            self.stats["received"] += 1
            rid = msg.get("request_id")
            # mpv 的标识是有符号 64 位整数；bool/float 在 Python 中可能别名整数键。
            if "request_id" in msg and (type(rid) is not int or not -(2 ** 63) <= rid < 2 ** 63):
                self.stats["errors"] += 1
                return
            event = msg.get("event")
            if "event" in msg and not isinstance(event, str):
                self.stats["errors"] += 1
                return
            if event in ("properties-changed", "property-change") and type(msg.get("id")) is not int:
                self.stats["errors"] += 1
                return
            if rid in self._pending:
                if msg.get("error") == ASYNC_ACCEPTED and not msg.get("endreply"):
                    return  # 先到的 accepted，等最终应答
                slot = self._pending.pop(rid)
                slot["reply"] = msg
                slot["done"].set()
                return
        # 已接纳帧的回调在状态锁外完成；回调可以 close，不拖住其他线程退出。

        if event in ("properties-changed", "property-change"):
            with self._cb_lock:
                cb = self._obs_callbacks.get(msg.get("id"))
            if cb:
                self.stats["events"] += 1
                _safe(cb, msg.get("data"))
        if event:
            self.stats["events"] += 1
            # 快照迭代：这一帧开始之后才注册的回调从下一帧生效。旧写法直接遍历活列表，
            # 回调里再注册回调会**当场**被这一帧调用（同一事件重复触发），而且和 UI 线程
            # 的注册共享同一容器，行为要靠 GIL 的巧合。锁只圈住拷贝，调用在锁外。
            with self._cb_lock:
                cbs = list(self._event_callbacks.get(event, ()))
                any_cbs = list(self._any_event_callbacks)
            for cb in cbs:
                _safe(cb, msg)
            for cb in any_cbs:
                _safe(cb, msg)


def _safe(fn: Callable, arg: Any) -> None:
    try:
        fn(arg)
    except Exception as exc:  # 回调异常不能杀死读线程
        print(f"[ipc] 回调异常: {exc}", file=sys.stderr)
