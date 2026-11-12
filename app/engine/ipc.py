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
ERROR_NO_DATA = 231
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
READ_SIZE = 65536
# 关闭/超时时仍可能在途的缓冲区：钉住不放，防止内核往已释放内存写
_LEAKED: list[tuple] = []


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


class WinPipe:
    """命名管道的重叠（异步）读写。

    实测过、值得写下来的一条：同一个**同步**句柄上系统会串行化所有 I/O——读线程
    阻塞在 ReadFile 期间，另一线程对同一句柄的 WriteFile 一个字节也发不出去（裸抓包：
    WriteFile 从 0.5s 一直阻塞到进程被杀）。换掉 os.open/os.read 没用，因为串行化
    发生在 kernel32 句柄层，不在 msvcrt 层。所以句柄必须 FILE_FLAG_OVERLAPPED。

    读侧不依赖取消：始终只有一个读挂起在途，超时只是"换个等待"，退出用独立的
    stop_ev 唤醒（少一处内核行为不确定性，也避免丢已到的数据）。
    """

    def __init__(self, path: str):
        self.path = path
        self.h: int | None = None
        # 关闭用的唤醒事件：读线程会长时间挂起等待，需要一个能被 SetEvent 叫醒的对象
        self.stop_ev = _kernel32.CreateEventW(None, True, False, None)
        self._pending: tuple | None = None      # (buf, ov, ev, got, 同步完成?)：唯一在途的读
        self.last_error = 0                     # 上一次 try_open 失败的 GetLastError（判据/日志用）

    # ---- 内部：一次性的事件对象 ----
    @staticmethod
    def _new_ov() -> tuple[_Overlapped, int]:
        ev = _kernel32.CreateEventW(None, True, False, None)
        if not ev or ev == INVALID:
            raise OSError(f"CreateEventW 失败 GetLastError={_kernel32.GetLastError()}")
        ov = _Overlapped()
        ov.hEvent = ev
        return ov, ev

    @staticmethod
    def _close_ov(ov: _Overlapped, ev: int) -> None:
        try:
            _kernel32.CloseHandle(ev)
        except Exception:
            pass

    def try_open(self, dead: Callable[[], bool] | None = None) -> bool:
        """非阻塞地试一次 CreateFile。

        已打开 → True；进程已退出 → 抛 IpcError（再等也不会有管道）；管道还没出现 → False，
        原因留在 `last_error`。调用方自己决定下一次什么时候试——GUI 版就是按 40ms 一拍，
        这样等待不再是"GUI 线程睡 10 秒"，而是一帧一帧地正常呼吸。
        """
        if self.h is not None:
            return True
        if dead is not None and dead():
            raise IpcError("mpv 进程已退出，管道不会再出现")
        h = _kernel32.CreateFileW(self.path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                  OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if h and h != INVALID:
            self.h = h
            self.last_error = 0
            return True
        self.last_error = _kernel32.GetLastError()
        return False

    def open(self, timeout: float, dead: Callable[[], bool] | None = None) -> None:
        deadline = time.monotonic() + timeout
        last_err = 0
        while time.monotonic() < deadline:
            if self.try_open(dead):
                return
            last_err = self.last_error
            time.sleep(0.02 if last_err == 233 else 0.05)
        raise IpcError(f"打开管道失败 {self.path}（GetLastError={last_err}）")

    def _finish(self, ov: _Overlapped, got: wt.DWORD) -> int:
        """取重叠操作的实际字节数；失败抛 OSError，取消/断开返回 -1。"""
        if _kernel32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False):
            return got.value
        err = _kernel32.GetLastError()
        if err in (ERROR_OPERATION_ABORTED, ERROR_BROKEN_PIPE, ERROR_NO_DATA):
            return -1
        raise OSError(f"管道 I/O 失败 GetLastError={err}")

    def _wait2(self, ev: int, timeout: float | None) -> int:
        """同时等"操作完成"和"被关闭唤醒"，返回 WAIT_OBJECT_0+index。"""
        handles = (ctypes.c_void_p * 2)(ev, self.stop_ev)
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        return _kernel32.WaitForMultipleObjects(2, handles, False, ms)

    def _issue_read(self) -> tuple:
        buf = ctypes.create_string_buffer(READ_SIZE)
        got = wt.DWORD(0)
        ov, ev = self._new_ov()
        if _kernel32.ReadFile(self.h, buf, READ_SIZE, ctypes.byref(got), ctypes.byref(ov)):
            return (buf, ov, ev, got, True)      # 罕见：同步就完成
        err = _kernel32.GetLastError()
        if err != ERROR_IO_PENDING:
            self._close_ov(ov, ev)
            raise OSError(f"ReadFile 失败 GetLastError={err}")
        return (buf, ov, ev, got, False)

    def read(self, timeout: float | None = None) -> bytes:
        """取下一块数据；timeout 秒内没等到就返回 b''，挂起的读原样保留下次接着等。

        为什么不像普通 socket 那样"超时即取消再重来"：取消一次读要等 IRP 真正结束
        才能回收缓冲区，而这段时间不可控；常驻一个在途读、超时只是换个等待，既不会
        丢掉已到达的字节，也不给关闭路径留悬空缓冲区。
        """
        if self.h is None:
            raise OSError("管道未打开")
        if self._pending is None:
            self._pending = self._issue_read()
        buf, ov, ev, got, sync_done = self._pending
        if not sync_done:
            sig = self._wait2(ev, timeout)
            if sig == WAIT_OBJECT_0 + 1:
                raise OSError("管道已停止")
            if sig == WAIT_FAILED:
                raise OSError(f"等待读完成失败 GetLastError={_kernel32.GetLastError()}")
            if sig == WAIT_TIMEOUT:
                return b""
        self._pending = None
        try:
            n = self._finish(ov, got)
        finally:
            self._close_ov(ov, ev)
        if n < 0:
            raise OSError("读未完成（对端已关闭管道）")
        return buf.raw[:n]

    def write(self, data: bytes) -> None:
        if self.h is None:
            raise OSError("管道未打开")
        buf = ctypes.create_string_buffer(data, max(1, len(data)))
        got = wt.DWORD(0)
        ov, ev = self._new_ov()
        try:
            if _kernel32.WriteFile(self.h, buf, wt.DWORD(len(data)),
                                   ctypes.byref(got), ctypes.byref(ov)):
                done = got.value
            else:
                err = _kernel32.GetLastError()
                if err != ERROR_IO_PENDING:
                    raise OSError(f"WriteFile 失败 GetLastError={err}")
                sig = self._wait2(ev, 10.0)
                if sig == WAIT_OBJECT_0 + 1:
                    _LEAKED.append((buf, ov))
                    raise OSError("管道已停止")
                if sig == WAIT_TIMEOUT:
                    _LEAKED.append((buf, ov))
                    raise OSError("WriteFile 超时（管道背压 10s 未消化）")
                if sig != WAIT_OBJECT_0:
                    raise OSError(f"等待写完成失败 GetLastError={_kernel32.GetLastError()}")
                done = self._finish(ov, got)
            if done < 0:
                raise OSError("WriteFile 未完成（对端已关闭管道）")
            if done != len(data):
                raise OSError(f"WriteFile 短写：{done}/{len(data)}")
        finally:
            self._close_ov(ov, ev)

    def close(self) -> None:
        if self.h:
            try:
                if self.stop_ev:
                    _kernel32.SetEvent(self.stop_ev)
            except Exception:
                pass
            # 挂起的读无法取消：把它引用的缓冲区钉住，防止内核写入已释放内存
            if self._pending is not None:
                _LEAKED.append((self._pending[0], self._pending[1]))
                self._pending = None
            try:
                _kernel32.CancelIoEx(self.h, None)   # 尽力而为，不作为退出依赖
            except Exception:
                pass
            try:
                _kernel32.CloseHandle(self.h)
            except Exception:
                pass
            self.h = None


class IpcError(RuntimeError):
    pass


class MpvIpc:
    def __init__(self, pipe_path: str):
        self.pipe_path = pipe_path
        self._pipe = WinPipe(pipe_path)
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._outq: queue.SimpleQueue = queue.SimpleQueue()
        self._alive = False
        self._closing = False
        self._observers: dict[str, int] = {}        # 属性名 -> mpv observe id
        self._obs_callbacks: dict[int, Callable] = {}
        self._event_callbacks: dict[str, list[Callable]] = {}
        self._any_event_callbacks: list[Callable] = []
        self.stats = {"sent": 0, "received": 0, "events": 0, "timeouts": 0, "errors": 0,
                      "reconnects": 0, "rtt_ms_max": 0.0, "rtt_ms_sum": 0.0, "rtt_n": 0}

    # ---------------- 连接 ----------------
    def try_connect(self, proc=None) -> bool:
        """非阻塞握手：管道已经出现就起读写线程，否则回 False。

        与 `connect()` 共用同一段 `start_io`，所以"怎么连上"只有一份实现；区别只在
        谁负责等待。GUI 那条路（MainWindow._poll_engine_start）用这个一帧试一次。
        """
        if self._reader is not None:
            return True
        dead = (lambda: proc.poll() is not None) if proc is not None else None
        if not self._pipe.try_open(dead=dead):
            return False
        self.start_io()
        return True

    def start_io(self) -> None:
        """管道句柄已就绪后，把读写线程拉起来。重复调用是空操作。"""
        if self._reader is not None:
            return
        self._alive = True
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc-read", daemon=True)
        self._reader.start()
        self._writer = threading.Thread(target=self._write_loop, name="mpv-ipc-write", daemon=True)
        self._writer.start()

    def connect(self, timeout: float = 8.0, proc=None) -> bool:
        dead = (lambda: proc.poll() is not None) if proc is not None else None
        self._pipe.open(timeout, dead=dead)
        self.start_io()
        return True

    def close(self) -> None:
        self._closing = True
        self._alive = False
        self._outq.put(None)
        self._fail_pending("IPC 已关闭")
        self._pipe.close()
        for th in (self._reader, self._writer):
            if th is not None:
                th.join(timeout=1.5)
        self._reader = self._writer = None

    def _fail_pending(self, reason: str) -> None:
        """把还在等应答的请求一次性唤醒。

        读线程一死，所有在途 request 就只可能等到超时（默认 3 秒）——界面上表现为
        "点了没反应，几秒后突然报错"，而内核其实早没了。这里给它们一个立刻返回的
        错误，调用方按正常失败路径收尾。写线程死亡同理（命令根本没发出去）。
        """
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
        # 交给写线程串行发出：管道背压时 UI 线程不会被 WriteFile 卡住
        self._outq.put(data)

    def _write_loop(self) -> None:
        while self._alive:
            try:
                data = self._outq.get(timeout=0.2)
            except queue.Empty:
                continue
            if data is None:
                break
            try:
                with self._lock:
                    self._pipe.write(data)
                self.stats["sent"] += 1
            except OSError as exc:
                self.stats["errors"] += 1
                self._alive = False
                self._fail_pending("IPC 写线程已退出")
                print(f"[ipc] 写出错：{exc}", file=sys.stderr)
                break

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
        self._pending[rid] = slot
        t0 = time.perf_counter()
        # 读线程里发同步请求 = 自我死锁（应答只有本线程能投递），必须立刻报错而不是
        # 静默等到超时——曾经就是回调里 get() 把首包之后的请求全拖成 1.5s 超时。
        if self._reader is not None and threading.get_ident() == self._reader.ident:
            self._pending.pop(rid, None)
            self.stats["errors"] += 1
            raise IpcError("禁止在 IPC 读线程内发同步请求（会自我死锁），改用属性缓存或投递到主线程")
        try:
            self._write({"command": command, "request_id": rid, "async": True})
        except IpcError:
            self._pending.pop(rid, None)
            raise
        if not slot["done"].wait(timeout):
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
        if name in self._observers:
            self._obs_callbacks[self._observers[name]] = callback
            return
        mid = next(self._ids)
        self._observers[name] = mid
        self._obs_callbacks[mid] = callback
        if self._alive:
            self.request(["observe_property", mid, name], timeout=2.0)

    def reobserve_all(self) -> None:
        for name, mid in list(self._observers.items()):
            try:
                self.request(["observe_property", mid, name], timeout=1.0)
            except IpcError:
                pass

    def on(self, event: str, callback: Callable[[dict], None]) -> None:
        self._event_callbacks.setdefault(event, []).append(callback)

    def on_any_event(self, callback: Callable[[dict], None]) -> None:
        self._any_event_callbacks.append(callback)

    # ---------------- 接收 ----------------
    def _read_loop(self) -> None:
        buf = bytearray()
        while self._alive:
            try:
                chunk = self._pipe.read(timeout=0.5)
            except OSError:
                break
            except Exception:
                break
            if not chunk:
                continue        # 超时轮询：连接仍然正常
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl]).strip()
                del buf[: nl + 1]
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    self.stats["errors"] += 1
                    continue
                self._dispatch(msg)
        self._alive = False
        if not self._closing:
            self._fail_pending("IPC 读线程已退出")

    def _dispatch(self, msg: dict) -> None:
        self.stats["received"] += 1
        rid = msg.get("request_id")
        if rid and rid in self._pending:
            slot = self._pending[rid]
            if msg.get("error") == ASYNC_ACCEPTED and not msg.get("endreply"):
                return  # 先到的 accepted，等最终应答
            slot["reply"] = msg
            slot["done"].set()
            self._pending.pop(rid, None)
            return

        event = msg.get("event")
        if event in ("properties-changed", "property-change"):
            cb = self._obs_callbacks.get(msg.get("id"))
            if cb:
                self.stats["events"] += 1
                _safe(cb, msg.get("data"))
        if event:
            self.stats["events"] += 1
            for cb in self._event_callbacks.get(event, []):
                _safe(cb, msg)
            for cb in self._any_event_callbacks:
                _safe(cb, msg)


def _safe(fn: Callable, arg: Any) -> None:
    try:
        fn(arg)
    except Exception as exc:  # 回调异常不能杀死读线程
        print(f"[ipc] 回调异常: {exc}", file=sys.stderr)
