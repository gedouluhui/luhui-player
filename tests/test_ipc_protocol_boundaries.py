# -*- coding: utf-8 -*-
"""MpvIpc 协议离线回归：真实读取/分发，只有管道和进程使用替身。

运行：python -B tests/test_ipc_protocol_boundaries.py
不导入 GUI/config、不启动子进程、不创建 WinPipe 句柄、不改动 _LEAKED。
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engine import ipc as IP  # noqa: E402


def frame(message):
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


class FakePipe:
    """b'' 是 WinPipe 轮询超时；脚本耗尽抛破管，不复制协议解析。"""

    def __init__(self, chunks=(), *, close_error=False):
        self.chunks = iter(chunks)
        self.reads = 0
        self.close_calls = 0
        self.h = 1
        self.close_error = close_error

    def read(self, timeout=None):
        self.reads += 1
        item = next(self.chunks, OSError("fake broken pipe"))
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.close_calls += 1
        self.h = None
        if self.close_error:
            raise OSError("fake close failure")


class FakeProc:
    """仅记录本实例的恢复调用，不启动或查找任何系统进程。"""

    def __init__(self, returncode=None, *, kill_error=False):
        self.returncode = returncode
        self.kill_calls = 0
        self.kill_error = kill_error

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        if self.kill_error:
            raise OSError("fake kill failure")
        self.returncode = -9


class BlockingPipe(FakePipe):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.written = threading.Event()

    def read(self, timeout=None):
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError("test did not release read")
        raise OSError("fake broken pipe")

    def write(self, data):
        self.written.set()

    def close(self):
        super().close()
        self.release.set()


class OpeningPipe(BlockingPipe):
    """在句柄发布前/后暂停 open；close 只收已有句柄，不凭空制造读故障。"""

    def __init__(self, *, pause=False, late_handle=False, available=True):
        super().__init__()
        self.h = None
        self.pause = pause
        self.late_handle = late_handle
        self.available = available
        self.open_entered = threading.Event()
        self.open_release = threading.Event()
        self.open_calls = 0
        self.close_threads = []
        self.last_error = 2

    def try_open(self, dead=None):
        self.open_calls += 1
        if not self.available:
            return False
        if not self.late_handle:
            self.h = 1
        self.open_entered.set()
        if self.pause and not self.open_release.wait(5):
            raise RuntimeError("test did not release open")
        if self.late_handle:
            self.h = 1
        return True

    def open(self, timeout, dead=None):
        if not self.try_open(dead=dead):
            raise IP.IpcError("fake open timeout")

    def read(self, timeout=None):
        self.reads += 1
        if self.h is None:
            raise OSError("fake pipe not open")
        return super().read(timeout)

    def close(self):
        self.close_threads.append(threading.current_thread())
        super().close()


class IpcTestCase(unittest.TestCase):
    def setUp(self):
        self.thread_errors = []
        self.enterContext(patch.object(threading, "excepthook", self.capture_thread_error))
        self.addCleanup(self.assertEqual, self.thread_errors, [], "uncaught worker exceptions")

    def capture_thread_error(self, args):
        # 不持有 traceback / Thread，避免 excepthook 自己延长资源寿命。
        self.thread_errors.append((args.thread.name, args.exc_type.__name__, str(args.exc_value)))

    def make_fresh_ipc(self, pipe):
        with patch.object(IP, "WinPipe", return_value=pipe):
            return IP.MpvIpc(r"\\.\pipe\protocol-start-test-only")

    def make_ipc(self, chunks=(), *, pipe=None):
        pipe = pipe if pipe is not None else FakePipe(chunks)
        # 替换构造器而非先建真 WinPipe 再覆盖，避免遗留真实 stop_ev。
        with patch.object(IP, "WinPipe", return_value=pipe):
            ipc = IP.MpvIpc(r"\\.\pipe\protocol-test-only")
        ipc._alive = True
        return ipc, pipe

    def pending(self, ipc, rid=1):
        slot = {"done": threading.Event(), "reply": None}
        ipc._pending[rid] = slot
        return slot

    def run_loop(self, ipc):
        escaped = None
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            try:
                ipc._read_loop()
            except Exception as exc:
                escaped = exc
        self.assertIsNone(escaped, f"read loop leaked {escaped!r}")
        return output.getvalue()

    def assert_disconnected(self, ipc, pipe, slots):
        self.assertFalse(ipc.alive)
        self.assertEqual(pipe.close_calls, 1)
        self.assertEqual(ipc._pending, {})
        for slot in slots:
            self.assertTrue(slot["done"].is_set())
            self.assertNotEqual(slot["reply"].get("error"), "success")


class ProtocolBoundaries(IpcTestCase):
    def test_partial_utf8_timeouts_and_coalesced_frames(self):
        ipc, pipe = self.make_ipc([
            b'{"event":"', b'', b'\xe4', b'\xb8\xad"}\n{"event":"idle"}\n',
            b' \r\n{"event":"end"}', b'\n',
        ])
        events = []
        ipc.on_any_event(lambda msg: events.append(msg["event"]))
        slot = self.pending(ipc)
        self.run_loop(ipc)
        self.assertEqual(events, ["中", "idle", "end"])
        self.assertEqual(ipc.stats["received"], 3)
        self.assert_disconnected(ipc, pipe, [slot])

    def test_exact_byte_limit_accepts_complete_and_fragmented_lines(self):
        # 内容 16 字节，填充至 64：闸门按原始字节、排除 LF、先于 strip。
        line = b'{"event":"idle"}' + b' ' * 48
        self.assertEqual(len(line), 64)
        for chunks in ([line + b'\n'], [line, b'\n']):
            with self.subTest(chunks=len(chunks)), patch.object(IP, "MAX_LINE_BYTES", 64):
                ipc, _ = self.make_ipc(chunks)
                events = []
                ipc.on_any_event(lambda msg: events.append(msg["event"]))
                self.run_loop(ipc)
                self.assertEqual(events, ["idle"])

    def test_terminated_over_limit_frame_is_not_dispatched(self):
        for suffix in (b'\n', b'\r\n'):
            with self.subTest(suffix=suffix), patch.object(IP, "MAX_LINE_BYTES", 64):
                ipc, pipe = self.make_ipc([
                    b'{"event":"bad"}' + b' ' * 51 + suffix,
                    b'{"event":"unread"}\n',
                ])
                events = []
                ipc.on_any_event(lambda msg: events.append(msg["event"]))
                slot = self.pending(ipc)
                text = self.run_loop(ipc)
                self.assertEqual(events, [])
                self.assertEqual(pipe.reads, 1)
                self.assertIn("64", text)
                self.assert_disconnected(ipc, pipe, [slot])

    def test_fragmented_over_limit_frame_is_rejected_on_newline(self):
        with patch.object(IP, "MAX_LINE_BYTES", 64):
            ipc, pipe = self.make_ipc([
                b'{"event":"bad"}' + b' ' * 49, b' \n', b'not read',
            ])
            events = []
            ipc.on_any_event(lambda msg: events.append(msg["event"]))
            self.run_loop(ipc)
            self.assertEqual(events, [])
            self.assertEqual(pipe.reads, 2)

    def test_unterminated_over_limit_frame_disconnects_immediately(self):
        with patch.object(IP, "MAX_LINE_BYTES", 64):
            ipc, pipe = self.make_ipc([b'x' * 64, b'x', b'not read'])
            slot = self.pending(ipc)
            self.run_loop(ipc)
            self.assertEqual(pipe.reads, 2)
            self.assert_disconnected(ipc, pipe, [slot])

    def test_limit_counts_utf8_bytes_not_characters(self):
        payload = frame({"event": "中" * 20})
        self.assertLess(len(payload.decode("utf-8")), 64)
        self.assertGreater(len(payload) - 1, 64)
        with patch.object(IP, "MAX_LINE_BYTES", 64):
            ipc, _ = self.make_ipc([payload])
            events = []
            ipc.on_any_event(lambda msg: events.append(msg["event"]))
            self.run_loop(ipc)
            self.assertEqual(events, [])

    def test_limit_applies_per_line_not_to_whole_chunk(self):
        with patch.object(IP, "MAX_LINE_BYTES", 32):
            ipc, _ = self.make_ipc([b'{"event":"idle"}\n' * 10])
            events = []
            ipc.on_any_event(lambda msg: events.append(msg["event"]))
            self.run_loop(ipc)
            self.assertEqual(events, ["idle"] * 10)

    def test_valid_prefix_survives_but_frames_after_overflow_do_not(self):
        with patch.object(IP, "MAX_LINE_BYTES", 32):
            ipc, _ = self.make_ipc([
                b'{"event":"first"}\n' + b' ' * 33 + b'\n{"event":"last"}\n',
            ])
            events = []
            ipc.on_any_event(lambda msg: events.append(msg["event"]))
            self.run_loop(ipc)
            self.assertEqual(events, ["first"])

    def test_non_object_json_is_counted_and_following_event_survives(self):
        for value in ([], [1], None, 7, 1.5, True, "text"):
            with self.subTest(value=value):
                ipc, pipe = self.make_ipc([frame(value), b'{"event":"idle"}\n'])
                events = []
                ipc.on_any_event(lambda msg: events.append(msg["event"]))
                slot = self.pending(ipc)
                self.run_loop(ipc)
                self.assertEqual(events, ["idle"])
                self.assertGreaterEqual(ipc.stats["errors"], 2)  # 坏帧 + 最后破管
                self.assert_disconnected(ipc, pipe, [slot])

    def test_bad_json_utf8_and_deep_json_do_not_poison_next_line(self):
        for bad in (b'{bad}\n', b'{"event":"\xff"}\n', b'\xc3\n',
                    b'[' * 2000 + b']' * 2000 + b'\n'):
            with self.subTest(bad=bad[:25]):
                ipc, pipe = self.make_ipc([bad + b'{"event":"idle"}\n'])
                events = []
                ipc.on_any_event(lambda msg: events.append(msg["event"]))
                self.run_loop(ipc)
                self.assertEqual(events, ["idle"])
                self.assertGreaterEqual(ipc.stats["errors"], 2)  # 坏帧 + 最后破管
                self.assert_disconnected(ipc, pipe, [])

    def test_invalid_request_ids_cannot_resolve_an_integer_request(self):
        for rid in (True, False, 1.0, "1", None, [], [1], {}, {"id": 1},
                    -(2 ** 63) - 1, 2 ** 63):
            with self.subTest(rid=rid):
                ipc, _ = self.make_ipc([
                    frame({"request_id": rid, "error": "success", "data": "bad"}),
                    b'{"event":"checkpoint"}\n',
                    frame({"request_id": 1, "error": "success", "data": "good"}),
                ])
                slot = self.pending(ipc)
                premature = []
                ipc.on("checkpoint", lambda msg: premature.append(slot["done"].is_set()))
                self.run_loop(ipc)
                self.assertEqual(premature, [False])
                self.assertEqual(slot["reply"]["data"], "good")
                self.assertGreaterEqual(ipc.stats["errors"], 2)  # 坏帧 + 最后破管

    def test_async_accepted_late_duplicate_and_integer_boundaries(self):
        ipc, _ = self.make_ipc([
            frame({"request_id": 1, "error": "async"}) + b'{"event":"checkpoint"}\n',
            frame({"request_id": 1, "error": "success", "data": "final", "endreply": True}),
            frame({"request_id": 1, "error": "success", "data": "duplicate"}),
            frame({"request_id": 0, "error": "success"}),
            frame({"request_id": -(2 ** 63), "error": "success"}),
            frame({"request_id": 2 ** 63 - 1, "error": "success"}),
            b'{"event":"idle"}\n',
        ])
        slot = self.pending(ipc)
        premature = []
        events = []
        ipc.on("checkpoint", lambda msg: premature.append(slot["done"].is_set()))
        ipc.on_any_event(lambda msg: events.append(msg["event"]))
        self.run_loop(ipc)
        self.assertEqual(premature, [False])
        self.assertEqual(slot["reply"]["data"], "final")
        self.assertEqual(events, ["checkpoint", "idle"])

    def test_invalid_event_and_observer_ids_do_not_escape_dispatch(self):
        invalid = [{"event": value} for value in ([], ["idle"], {}, 1, True, None)]
        invalid += [{"event": "property-change", "id": value, "data": "bad"}
                    for value in ([], {}, True, 1.0, "1", None)]
        for message in invalid:
            with self.subTest(message=message):
                ipc, _ = self.make_ipc([
                    frame(message),
                    frame({"event": "property-change", "id": 1, "data": "good"}),
                ])
                values = []
                ipc._obs_callbacks[1] = values.append
                self.run_loop(ipc)
                self.assertEqual(values, ["good"])
                self.assertGreaterEqual(ipc.stats["errors"], 2)  # 坏帧 + 最后破管

    def test_unexpected_dispatch_failure_still_closes_and_wakes_pending(self):
        ipc, pipe = self.make_ipc([b'{"event":"idle"}\n'])
        slots = [self.pending(ipc, 1), self.pending(ipc, 2)]
        with patch.object(ipc, "_dispatch", side_effect=RuntimeError("dispatch failed")):
            self.run_loop(ipc)
        self.assert_disconnected(ipc, pipe, slots)

    def test_close_failure_cannot_skip_pending_cleanup(self):
        ipc, pipe = self.make_ipc(pipe=FakePipe(close_error=True))
        slots = [self.pending(ipc, 1), self.pending(ipc, 2)]
        self.run_loop(ipc)
        self.assert_disconnected(ipc, pipe, slots)


class Disconnection(IpcTestCase):
    def test_broken_pipe_and_overflow_recover_only_owned_process_once(self):
        cases = [([], "fake broken pipe"), ([b'x' * 65], "64"),
                 ([b'x' * 65 + b'\n'], "64")]
        for chunks, detail in cases:
            with self.subTest(detail=detail, chunks=len(chunks)), patch.object(IP, "MAX_LINE_BYTES", 64):
                ipc, pipe = self.make_ipc(chunks)
                ipc._proc = owned = FakeProc()
                other_ipc, _ = self.make_ipc()
                other_ipc._proc = unrelated = FakeProc()
                slots = [self.pending(ipc, 1), self.pending(ipc, 2)]
                text = self.run_loop(ipc)
                self.assertEqual(owned.kill_calls, 1)
                self.assertEqual(unrelated.kill_calls, 0)
                self.assertIn(detail, text)
                self.assertIn(detail, slots[0]["reply"]["error"])
                self.assertEqual(ipc.stats["errors"], 1)
                self.assert_disconnected(ipc, pipe, slots)
                # 上层看门狗再 close 或读线程再次收口，不重复关闭/恢复。
                ipc.close()
                ipc.close()
                self.run_loop(ipc)
                self.assertEqual(pipe.close_calls, 1)
                self.assertEqual(owned.kill_calls, 1)
                self.assertEqual(ipc.stats["errors"], 1)

    def test_exited_process_is_not_killed_again(self):
        ipc, pipe = self.make_ipc()
        ipc._proc = proc = FakeProc(returncode=0)
        self.run_loop(ipc)
        self.assertEqual(proc.kill_calls, 0)
        self.assert_disconnected(ipc, pipe, [])

    def test_recovery_failure_is_diagnostic_and_cannot_strand_pending(self):
        ipc, pipe = self.make_ipc()
        ipc._proc = proc = FakeProc(kill_error=True)
        slot = self.pending(ipc)
        text = self.run_loop(ipc)
        self.assertEqual(proc.kill_calls, 1)
        self.assertIn("fake kill failure", text)
        self.assert_disconnected(ipc, pipe, [slot])

    def test_unexpected_read_failure_uses_same_recovery(self):
        ipc, pipe = self.make_ipc([RuntimeError("unexpected read failure")])
        ipc._proc = proc = FakeProc()
        slot = self.pending(ipc)
        text = self.run_loop(ipc)
        self.assertEqual(proc.kill_calls, 1)
        self.assertIn("unexpected read failure", text)
        self.assert_disconnected(ipc, pipe, [slot])

    def test_active_close_is_idempotent_and_never_recovers(self):
        ipc, pipe = self.make_ipc([b'x' * (IP.MAX_LINE_BYTES + 1)])
        ipc._proc = proc = FakeProc()
        slot = self.pending(ipc)
        ipc.close()
        ipc.close()
        text = self.run_loop(ipc)
        self.assertEqual(text, "")
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(pipe.reads, 0)
        self.assertEqual(ipc.stats["errors"], 0)
        self.assert_disconnected(ipc, pipe, [slot])

    def test_close_wins_while_read_is_blocked(self):
        ipc, pipe = self.make_ipc(pipe=BlockingPipe())
        ipc._proc = proc = FakeProc()
        slot = self.pending(ipc)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            ipc.start_io()
            reader, writer = ipc._reader, ipc._writer
            try:
                self.assertTrue(pipe.entered.wait(1))
            finally:
                ipc.close()  # close 释放阻塞 read；它随即抛破管。
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(ipc.stats["errors"], 0)
        self.assertEqual(output.getvalue(), "")
        self.assert_disconnected(ipc, pipe, [slot])

    def test_fault_wins_while_close_arrives_during_pipe_cleanup(self):
        entered, release, close_converted = (threading.Event() for _ in range(3))

        class PausingClosePipe(FakePipe):
            def close(self):
                super().close()
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test did not release close")

        ipc, pipe = self.make_ipc(pipe=PausingClosePipe())
        ipc._proc = proc = FakeProc()
        slot = self.pending(ipc)
        failures = []

        disconnect = ipc._disconnect

        def observed_disconnect(reason, recover):
            disconnect(reason, recover)
            if not recover:
                # 实际断连转换（包括已断开守卫）已执行，而不是调用前亮灯。
                close_converted.set()

        def close():
            try:
                ipc.close()
            except Exception as exc:
                failures.append(exc)

        closer = threading.Thread(target=close, daemon=True)
        with contextlib.redirect_stderr(io.StringIO()), patch.object(
                ipc, "_disconnect", side_effect=observed_disconnect):
            ipc.start_io()
            reader, writer = ipc._reader, ipc._writer
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(slot["done"].is_set(), "pending must wake before pipe cleanup")
                closer.start()
                self.assertTrue(close_converted.wait(1))
                self.assertTrue(ipc._disconnected)
                self.assertEqual(proc.kill_calls, 0, "fault cleanup is still paused")
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(2)
                ipc.close()
        self.assertEqual(failures, [])
        self.assertFalse(closer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(proc.kill_calls, 1)
        self.assertEqual(ipc.stats["errors"], 1)
        self.assert_disconnected(ipc, pipe, [slot])

    def test_real_request_waiter_wakes_without_timeout_on_disconnect(self):
        ipc, pipe = self.make_ipc(pipe=BlockingPipe())
        ipc._proc = proc = FakeProc()
        slot = self.pending(ipc, 99)
        outcome = []
        finished = threading.Event()

        def request():
            try:
                ipc.request(["get_property", "pause"], timeout=5)
            except Exception as exc:
                outcome.append(exc)
            finally:
                finished.set()

        waiter = threading.Thread(target=request, daemon=True)
        with contextlib.redirect_stderr(io.StringIO()):
            ipc.start_io()
            reader, writer = ipc._reader, ipc._writer
            try:
                waiter.start()
                self.assertTrue(pipe.written.wait(1))
                pipe.release.set()
                self.assertTrue(finished.wait(1), "request must not wait for its 5s timeout")
                reader.join(1)
                writer.join(1)
                self.assertFalse(reader.is_alive())
                self.assertFalse(writer.is_alive())
                self.assertEqual(len(outcome), 1)
                self.assertIsInstance(outcome[0], IP.IpcError)
                self.assertIn("fake broken pipe", str(outcome[0]))
                self.assertEqual(ipc.stats["timeouts"], 0)
                self.assertEqual(proc.kill_calls, 1)
                self.assert_disconnected(ipc, pipe, [slot])
            finally:
                pipe.release.set()
                ipc.close()
                waiter.join(1)

    def test_write_failure_and_read_finally_share_one_disconnect(self):
        ipc, pipe = self.make_ipc()
        ipc._proc = proc = FakeProc()
        slot = self.pending(ipc)
        ipc._outq.put(b'{"command":["stop"]}\n')
        with patch.object(pipe, "write", side_effect=OSError("fake write failure"), create=True):
            with contextlib.redirect_stderr(io.StringIO()) as output:
                ipc._write_loop()
                ipc._read_loop()
        self.assertEqual(proc.kill_calls, 1)
        self.assertEqual(ipc.stats["errors"], 1)
        self.assertIn("fake write failure", output.getvalue())
        self.assertIn("fake write failure", slot["reply"]["error"])
        self.assert_disconnected(ipc, pipe, [slot])

    def test_close_during_read_discards_returned_bytes(self):
        ipc, pipe = self.make_ipc()
        events = []
        ipc.on_any_event(lambda msg: events.append(msg["event"]))

        def closing_read(timeout=None):
            ipc.close()
            return b'{"event":"late"}\n'

        with patch.object(pipe, "read", side_effect=closing_read):
            self.run_loop(ipc)
        self.assertEqual(events, [])
        self.assertEqual(ipc.stats["errors"], 0)
        self.assert_disconnected(ipc, pipe, [])

    def test_callback_can_close_reader_without_self_join_or_late_events(self):
        ipc, pipe = self.make_ipc([b'{"event":"first"}\n{"event":"late"}\n'])
        ipc._proc = proc = FakeProc()
        closed = threading.Event()
        events = []

        def close_on_first(msg):
            ipc.close()
            closed.set()

        ipc.on("first", close_on_first)
        ipc.on_any_event(lambda msg: events.append(msg["event"]))
        with contextlib.redirect_stderr(io.StringIO()) as output:
            ipc.start_io()
            try:
                self.assertTrue(closed.wait(1), "close must not join the current reader")
            finally:
                ipc.close()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(events, ["first"])
        self.assertEqual(proc.kill_calls, 0)
        self.assert_disconnected(ipc, pipe, [])


class StartupLifecycle(IpcTestCase):
    def check_open_close(self, *, late_handle):
        for entry in ("try_connect", "connect"):
            with self.subTest(entry=entry, late_handle=late_handle):
                pipe = OpeningPipe(pause=True, late_handle=late_handle)
                ipc = self.make_fresh_ipc(pipe)
                proc = FakeProc()
                slot = self.pending(ipc)
                outcomes, created = [], []
                real_thread = threading.Thread

                def create_thread(*args, **kwargs):
                    th = real_thread(*args, **kwargs)
                    created.append(th)
                    return th

                def connect():
                    try:
                        outcomes.append(getattr(ipc, entry)(proc=proc))
                    except Exception as exc:
                        outcomes.append(exc)

                starter = real_thread(target=connect, name="test-open-owner", daemon=True)
                with contextlib.redirect_stderr(io.StringIO()), patch.object(
                        IP.threading, "Thread", side_effect=create_thread):
                    starter.start()
                    try:
                        self.assertTrue(pipe.open_entered.wait(1))
                        self.assertEqual(pipe.h, None if late_handle else 1)
                        ipc.close()
                        self.assertTrue(ipc._disconnected)
                        self.assertTrue(slot["done"].is_set())
                    finally:
                        pipe.open_release.set()
                        starter.join(2)
                        ipc.close()
                self.assertFalse(starter.is_alive())
                self.assertTrue(all(not th.is_alive() for th in created))
                self.assertEqual(proc.kill_calls, 0, "late startup must not recover an active close")
                self.assertEqual(ipc.stats["errors"], 0)
                self.assertEqual(created, [], "a terminal IPC must not create I/O threads")
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], IP.IpcError)
                self.assertIsNone(pipe.h)
                self.assert_disconnected(ipc, pipe, [slot])
                if late_handle:
                    self.assertEqual(pipe.close_threads, [starter], "opener owns the late handle")

    def test_close_wins_after_handle_published_before_open_returns(self):
        self.check_open_close(late_handle=False)

    def test_close_wins_before_open_acquires_handle(self):
        self.check_open_close(late_handle=True)

    def test_terminal_instance_rejects_all_connection_entries(self):
        for entry in ("start_io", "try_connect", "connect"):
            with self.subTest(entry=entry):
                pipe = OpeningPipe()
                ipc = self.make_fresh_ipc(pipe)
                ipc.close()
                outcome = None
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        getattr(ipc, entry)()
                    except Exception as exc:
                        outcome = exc
                    finally:
                        ipc.close()
                self.assertIsInstance(outcome, IP.IpcError)
                self.assertEqual(pipe.open_calls, 0)
                self.assertEqual(pipe.reads, 0)
                self.assertEqual(ipc.stats["errors"], 0)
                self.assert_disconnected(ipc, pipe, [])

    def test_open_pending_can_retry_but_connected_call_does_not_reopen(self):
        pipe = OpeningPipe(available=False)
        ipc = self.make_fresh_ipc(pipe)
        proc = FakeProc()
        self.assertFalse(ipc.try_connect(proc=proc))
        self.assertFalse(ipc.alive)
        self.assertFalse(ipc._disconnected)
        pipe.available = True
        try:
            self.assertTrue(ipc.try_connect(proc=proc))
            self.assertTrue(pipe.entered.wait(1))
            reader, writer = ipc._reader, ipc._writer
            self.assertTrue(ipc.try_connect(proc=proc))
            self.assertEqual(pipe.open_calls, 2)
            self.assertIs(ipc._reader, reader)
            self.assertIs(ipc._writer, writer)
        finally:
            ipc.close()
        self.assertEqual(proc.kill_calls, 0)
        self.assert_disconnected(ipc, pipe, [])

    def check_thread_failure(self, stage):
        for entry in ("start_io", "try_connect", "connect"):
            with self.subTest(stage=stage, entry=entry):
                pipe = OpeningPipe()
                ipc = self.make_fresh_ipc(pipe)
                ipc._proc = proc = FakeProc()
                if entry == "start_io":
                    pipe.h = 1
                slot = self.pending(ipc)
                created, close_errors, cleanup_under_lock = [], [], []
                real_thread = threading.Thread
                real_close = pipe.close
                injected = (MemoryError("injected " + stage) if "construct" in stage
                            else RuntimeError("injected " + stage))
                calls = 0

                def create_thread(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if stage == "first_construct" and calls == 1:
                        raise injected
                    if stage == "second_construct" and calls == 2:
                        raise injected
                    th = real_thread(*args, **kwargs)
                    created.append(th)
                    real_join = th.join

                    def join(timeout=None):
                        cleanup_under_lock.append(ipc._disconnect_lock._is_owned())
                        return real_join(timeout)

                    th.join = join
                    if ((stage == "reader_start" and calls == 1)
                            or (stage == "writer_start" and calls == 2)):
                        def fail_start():
                            raise injected
                        th.start = fail_start
                    return th

                def close_pipe():
                    cleanup_under_lock.append(ipc._disconnect_lock._is_owned())
                    real_close()

                outcome = None
                with contextlib.redirect_stderr(io.StringIO()), patch.object(
                        IP.threading, "Thread", side_effect=create_thread), patch.object(
                        pipe, "close", side_effect=close_pipe):
                    try:
                        getattr(ipc, entry)()
                    except Exception as exc:
                        outcome = exc
                    # 在调用方兜底 close 之前观察自动回滚，而不是让测试代替产品清理。
                    rolled_back = (not ipc.alive and ipc._disconnected and pipe.h is None
                                   and slot["done"].is_set() and not ipc._pending
                                   and ipc._reader is None and ipc._writer is None
                                   and all(not th.is_alive() for th in created))
                    for _ in range(2):
                        try:
                            ipc.close()
                        except Exception as exc:
                            close_errors.append(str(exc))
                    pipe.release.set()
                    for th in created:
                        if th.ident is not None:
                            th.join(2)
                self.assertTrue(all(not th.is_alive() for th in created))
                self.assertEqual(close_errors, [], "repeat close must skip unstarted threads")
                self.assertTrue(rolled_back, "startup failure must roll back before returning")
                self.assertIsInstance(outcome, IP.IpcError)
                self.assertIs(outcome.__cause__, injected)
                self.assertIsNone(ipc._reader)
                self.assertIsNone(ipc._writer)
                self.assertTrue(cleanup_under_lock)
                self.assertFalse(any(cleanup_under_lock), "close/join must run outside state lock")
                self.assertEqual(proc.kill_calls, 0)
                self.assertEqual(pipe.reads, 0, "partial startup must not enter an I/O loop")
                self.assertEqual(ipc.stats["errors"], 0)
                self.assertIn("injected " + stage, slot["reply"]["error"])
                self.assert_disconnected(ipc, pipe, [slot])

    def test_first_thread_construction_failure_rolls_back(self):
        self.check_thread_failure("first_construct")

    def test_second_thread_construction_failure_rolls_back(self):
        self.check_thread_failure("second_construct")

    def test_reader_start_failure_rolls_back(self):
        self.check_thread_failure("reader_start")

    def test_writer_start_failure_rolls_back(self):
        self.check_thread_failure("writer_start")

    def test_close_retains_live_thread_but_clears_unstarted_and_exited(self):
        ipc = self.make_fresh_ipc(OpeningPipe())
        release = threading.Event()
        reader = threading.Thread(target=release.wait, name="test-slow-exit", daemon=True)
        writer = threading.Thread(target=lambda: None, name="test-unstarted", daemon=True)
        ipc._reader, ipc._writer = reader, writer
        reader.start()
        errors = []
        try:
            try:
                ipc.close()  # 使用真实的 1.5s join 上限；活线程不可被丢弃。
            except Exception as exc:
                errors.append(str(exc))
            self.assertIs(ipc._reader, reader)
            self.assertTrue(reader.is_alive())
            self.assertIsNone(ipc._writer)
            with self.assertRaises(IP.IpcError):
                ipc.try_connect()
        finally:
            release.set()
            reader.join(2)
            try:
                ipc.close()
            except Exception as exc:
                errors.append(str(exc))
        self.assertEqual(errors, [])
        self.assertFalse(reader.is_alive())
        self.assertIsNone(ipc._reader)
        self.assertIsNone(ipc._writer)


class FixedIoRaces(IpcTestCase):
    def test_read_and_write_faults_compete_in_both_orders(self):
        for winner in ("read", "write"):
            with self.subTest(winner=winner):
                entered = {name: threading.Event() for name in ("read", "write")}
                release = {name: threading.Event() for name in ("read", "write")}
                converted = {name: threading.Event() for name in ("read", "write")}
                cleanup_entered, cleanup_release = threading.Event(), threading.Event()

                class RacingPipe(FakePipe):
                    def fail(self, name):
                        entered[name].set()
                        if not release[name].wait(5):
                            raise RuntimeError("test did not release " + name)
                        raise OSError("fake " + name + " failure")

                    def read(self, timeout=None):
                        self.fail("read")

                    def write(self, data):
                        self.fail("write")

                    def close(self):
                        super().close()
                        cleanup_entered.set()
                        if not cleanup_release.wait(5):
                            raise RuntimeError("test did not release cleanup")

                ipc, pipe = self.make_ipc(pipe=RacingPipe())
                ipc._proc = proc = FakeProc()
                slot = self.pending(ipc)
                disconnect = ipc._disconnect

                def observed_disconnect(reason, recover):
                    disconnect(reason, recover)
                    name = threading.current_thread().name.removeprefix("mpv-ipc-")
                    if name in converted:
                        converted[name].set()  # 真转换/终态守卫返回之后。

                with contextlib.redirect_stderr(io.StringIO()) as output, patch.object(
                        ipc, "_disconnect", side_effect=observed_disconnect):
                    ipc.start_io()
                    reader, writer = ipc._reader, ipc._writer
                    try:
                        ipc.fire(["stop"])
                        self.assertTrue(entered["read"].wait(1))
                        self.assertTrue(entered["write"].wait(1))
                        release[winner].set()
                        self.assertTrue(cleanup_entered.wait(1))
                        self.assertTrue(ipc._disconnected)
                        self.assertTrue(slot["done"].is_set())
                        loser = "write" if winner == "read" else "read"
                        release[loser].set()
                        self.assertTrue(converted[loser].wait(1))
                        self.assertEqual(proc.kill_calls, 0)
                    finally:
                        release["read"].set()
                        release["write"].set()
                        cleanup_release.set()
                        ipc.close()
                self.assertFalse(reader.is_alive())
                self.assertFalse(writer.is_alive())
                self.assertEqual(proc.kill_calls, 1)
                self.assertEqual(ipc.stats["errors"], 1)
                self.assertIn("fake " + winner + " failure", output.getvalue())
                self.assertIn("fake " + winner + " failure", slot["reply"]["error"])
                self.assert_disconnected(ipc, pipe, [slot])


if __name__ == "__main__":
    unittest.main(verbosity=2)
