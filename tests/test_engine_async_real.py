# -*- coding: utf-8 -*-
"""异步内核启动的**真内核**腿：begin 立即返回，握手由逐拍 poll 完成（全程无窗口）。

离线那条（tests/test_engine_async.py）用假内核摆时序，能证明界面接线；它证明不了
`MpvIpc.try_connect` 在真命名管道上第一次尝试会不会连上、超时会不会真收尾、内核早退
会不会被看见。这一条把 mpv 以 `--vo=null --ao=null --force-window=no` 拉起来（不占
桌面、不出声、窗口一个都不建），只测量启动/播放/退出的那几件事。

跑法：
    python tests/test_engine_async_real.py            # 验收
    python tests/test_engine_async_real.py --falsify  # 把 begin_start 换回"当场等完"，判据必须红

不测什么：窗口嵌入、首帧上屏、GPU 选择——那些要真 HWND/真显卡，归 test_engine.py 与
test_ui.py（会在桌面弹窗，用户在用电脑时不跑）。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = tempfile.mkdtemp(prefix="engine-async-real-", dir=HERE)
os.environ["GELU_DATA"] = DATA
atexit.register(lambda: shutil.rmtree(DATA, ignore_errors=True))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ROOT)

from PyQt5.QtCore import QCoreApplication                    # noqa: E402

from app import config as cfgmod                             # noqa: E402
from app.engine.player import MpvPlayer                      # noqa: E402

VID = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-1.mp4"))
APP = None
results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


class HeadlessPlayer(MpvPlayer):
    """产品的启动/IPC 全保留，只把输出端换成 null：不建窗口、不占声卡。"""

    drop_ipc = False

    def build_args(self, hwnd: int) -> list[str]:
        out: list[str] = []
        for a in super().build_args(hwnd):
            if a.startswith("--wid=") or a == "--force-window=yes" or a.startswith("--vo="):
                continue
            if self.drop_ipc and a.startswith("--input-ipc-server="):
                continue
            out.append(a)
        # 后写的覆盖先写的：即使 perf 表拼了 --hwdec=auto，这里也落回软解（null vo 不测硬解）
        out += ["--vo=null", "--ao=null", "--force-window=no", "--hwdec=no"]
        return out


class SyncStartPlayer(HeadlessPlayer):
    """变异：把 begin_start 换回旧行为——在调用方线程里等到握手完成才返回。"""

    def begin_start(self, hwnd: int, wait: float = 10.0) -> bool:
        if not MpvPlayer.begin_start(self, hwnd, wait):
            return False
        while True:
            state = MpvPlayer.poll_start(self)
            if state == "ok":
                return True
            if state == "fail":
                return False
            time.sleep(0.02)


class FakeOkPlayer(HeadlessPlayer):
    """变异：管道还没连上就宣布 ok（界面会以为命令能发，实际一条都过不去）。"""

    def poll_start(self) -> str:
        return "ok"


class BlindDeathPlayer(HeadlessPlayer):
    """变异：内核已经死了仍一直回 pending——界面会永远转圈。"""

    def poll_start(self) -> str:
        proc = self._proc
        if not self._started and proc is not None and proc.poll() is not None:
            return "pending"
        return super().poll_start()


class NeverTimeoutPlayer(HeadlessPlayer):
    """变异：管道一直不出现也不按 deadline 收尾（无限 pending）。"""

    def poll_start(self) -> str:
        if self._ipc is not None and not self._started:
            try:
                if not self._ipc.try_connect(self._proc):
                    return "pending"
            except Exception:
                return "pending"
        return super().poll_start()


def make_cfg() -> cfgmod.Config:
    cfg = cfgmod.Config(path=os.path.join(DATA, "cfg.json"))
    cfg["resume"] = False
    cfg["mute"] = True
    cfg["screenshot_dir"] = os.path.join(DATA, "shots")
    os.makedirs(cfg["screenshot_dir"], exist_ok=True)
    cfg.save()
    return cfg


def new_player(cls=HeadlessPlayer, drop_ipc: bool = False):
    p = cls(make_cfg())
    p.drop_ipc = drop_ipc
    logs: list[str] = []
    p.sig_log.connect(lambda s: logs.append(str(s)))
    return p, logs


def poll_until_done(p, timeout: float, t0: float | None = None):
    """真实 GUI 是 40ms 定时器；这里 20ms 一拍并让 Qt 投递跨线程日志。"""
    states: list[str] = []
    t0 = time.monotonic() if t0 is None else t0
    while time.monotonic() - t0 < timeout:
        state = p.poll_start()
        states.append(state)
        APP.processEvents()
        if state != "pending":
            return state, states, time.monotonic() - t0
        time.sleep(0.02)
    return "pending", states, time.monotonic() - t0


def start_leg(cls=HeadlessPlayer, wait: float = 10.0) -> dict:
    """一次完整的 start → ok/fail 测量；调用方负责 quit。"""
    p, logs = new_player(cls)
    t0 = time.monotonic()
    ok = p.begin_start(0, wait=wait)
    begin_s = time.monotonic() - t0
    snap = {
        "player": p,
        "logs": logs,
        "begin_ok": bool(ok),
        "begin_s": begin_s,
        "running_at_return": bool(p.running),
        "state": "",
        "states": [],
        "elapsed": 0.0,
        "poll_calls": 0,
        "version": "",
    }
    if ok:
        state, states, elapsed = poll_until_done(p, max(3.0, wait + 2.0), t0)
        snap.update({"state": state, "states": states, "elapsed": elapsed,
                     "poll_calls": len(states), "version": str(getattr(p, "version", "") or "")})
    return snap


def judge_begin(s: dict) -> list[str]:
    bad: list[str] = []
    if not s["begin_ok"]:
        bad.append("begin_start 返回 False（进程都没拉起来）")
    if s["running_at_return"]:
        bad.append("begin_start 返回时 running 已为 True：它把握手也等完了")
    if s["begin_s"] >= 0.30:
        bad.append(f"begin_start 花了 {s['begin_s'] * 1000:.0f}ms（要 <300ms，只 Popen）")
    return bad


def judge_handshake(s: dict) -> list[str]:
    bad: list[str] = []
    if s["state"] != "ok":
        bad.append(f"握手结局={s['state']!r}（states={s['states'][:8]}）")
    if not s["player"].running:
        bad.append("握手 ok 之后 running 仍为 False")
    if s["elapsed"] >= 12.0:
        bad.append(f"握手耗时 {s['elapsed']:.2f}s")
    if not s["version"] or s["version"] == "?":
        bad.append(f"没读到内核版本：{s['version']!r}")
    if any("内核参数不兼容" in l for l in s["logs"]):
        bad.append("内核日志报参数不兼容（启动参数表与这一版 mpv 对不上）")
    return bad


def wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    APP.processEvents()
    return bool(pred())


def report() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 汇总：{passed}/{len(results)} 通过"
          + (f"；失败项：{fails}" if fails else "；无失败") + " ----", flush=True)
    if falsified:
        print("==== 证伪（坏样本必须被抓出来）====")
        for name, ok, detail in falsified:
            print(f"[{'CAUGHT' if ok else 'MISSED'}] {name}"
                  + (f"  —— {detail}" if detail else ""), flush=True)
        missed = [n for n, ok, _ in falsified if not ok]
        print(f"---- 证伪收口：抓到 {len(falsified) - len(missed)}/{len(falsified)}"
              f"（漏网 {len(missed)}） ----", flush=True)
        if missed:
            return 1
    return 0 if passed == len(results) else 1


def early_death_leg(cls=HeadlessPlayer, wait: float = 5.0) -> dict:
    """拉起内核后立刻杀掉：poll 必须看见"死了"并收尾。"""
    p, logs = new_player(cls)
    ok = p.begin_start(0, wait=wait)
    proc = p._proc
    if proc is not None:
        proc.kill()
    state, states, spent = poll_until_done(p, 5.0)
    APP.processEvents()
    return {"player": p, "logs": logs, "begin_ok": bool(ok), "proc": proc,
            "state": state, "states": states, "spent": spent}


def judge_early_death(s: dict) -> list[str]:
    bad: list[str] = []
    if not s["begin_ok"]:
        bad.append("begin_start 返回 False（进程都没拉起来）")
    if s["state"] != "fail":
        bad.append(f"内核已死，poll 却给了 {s['state']!r}（states={s['states'][:5]}）")
    if not any("刚启动就退出" in l for l in s["logs"]):
        bad.append(f"没留下点名 code 的原话（logs={s['logs'][-2:]}）")
    return bad


def timeout_leg(cls=HeadlessPlayer, wait: float = 0.7) -> dict:
    """把 IPC 名摘掉：管道永远不出现，必须按 deadline 收尾。"""
    p, logs = new_player(cls, drop_ipc=True)
    ok = p.begin_start(0, wait=wait)
    state, states, spent = poll_until_done(p, 4.0)
    APP.processEvents()
    return {"player": p, "logs": logs, "begin_ok": bool(ok), "proc": p._proc,
            "state": state, "states": states, "spent": spent, "wait": wait}


def judge_timeout(s: dict) -> list[str]:
    bad: list[str] = []
    if not s["begin_ok"]:
        bad.append("begin_start 返回 False")
    if s["state"] != "fail":
        bad.append(f"管道始终没出现，poll 却给了 {s['state']!r}")
    if not (0.55 <= s["spent"] <= 3.0):
        bad.append(f"收尾用时 {s['spent']:.2f}s（wait={s['wait']}，不许无限 pending）")
    if s["proc"] is not None and s["proc"].poll() is None:
        bad.append("判 fail 之后进程还活着（没收掉）")
    if not any("启动超时" in l or "等 IPC 管道" in l for l in s["logs"]):
        bad.append(f"没留下带 deadline 的原话（logs={s['logs'][-2:]}）")
    return bad


def _finish(p) -> None:
    try:
        p.quit()
    except Exception as exc:      # noqa: BLE001 - 清理失败也要留话，不许静默
        print(f"[WARN] 清理时 quit 抛出 {type(exc).__name__}: {exc}", flush=True)
    APP.processEvents()


def falsify() -> int:
    def caught(label: str, fn, judge, mutant_cls) -> None:
        """基线必须绿，换上变异类必须红；两边无论成败都收掉进程。"""
        base = fn(HeadlessPlayer)
        try:
            base_bad = judge(base)
        finally:
            _finish(base["player"])
        if base_bad:
            falsified.append((label, False, f"基线自己就红：{base_bad[:2]}"))
            return
        mut = fn(mutant_cls)
        try:
            mut_bad = judge(mut)
        finally:
            _finish(mut["player"])
        falsified.append((label, bool(mut_bad),
                          ("基线绿、变异红：" + "；".join(mut_bad[:3])) if mut_bad
                          else "基线绿、变异也绿（判据没牙）"))

    caught("M1 begin_start 换回'等到 ok 才返回'必须被'立即返回'判据抓住",
           start_leg, judge_begin, SyncStartPlayer)
    caught("M2 管道没连上就回 ok 必须被'版本+在内核真活着'判据抓住",
           start_leg, judge_handshake, FakeOkPlayer)
    caught("M3 内核已死仍回 pending 必须被早退判据抓住",
           early_death_leg, judge_early_death, BlindDeathPlayer)
    caught("M4 管道不出现也不收尾必须被 deadline 判据抓住",
           timeout_leg, judge_timeout, NeverTimeoutPlayer)

    # 记账自检：红/绿两个已知答案
    n0 = len(falsified)
    falsified.append(("_自检：恒红必须记 CAUGHT", True, "假红"))
    self_red = len(falsified) == n0 + 1 and falsified[-1][1] is True
    falsified.pop()
    falsified.append(("_自检：全绿必须记 MISSED", False, "假绿"))
    self_green = len(falsified) == n0 + 1 and falsified[-1][1] is False
    falsified.pop()
    falsified.append(("记账自检", self_red and self_green, ""))
    return report()


def main() -> int:
    global APP
    APP = QCoreApplication.instance() or QCoreApplication([])
    if not os.path.exists(cfgmod.MPV_EXE):
        print(f"[ENV] 找不到内核 {cfgmod.MPV_EXE}，这一趟不作数", flush=True)
        return 2
    if not os.path.exists(VID):
        print(f"[ENV] 缺样片 {VID}，这一趟不作数", flush=True)
        return 2
    if "--falsify" in sys.argv:
        return falsify()

    # 1) 启动：只 Popen，不等管道
    s = start_leg(HeadlessPlayer)
    p = s["player"]
    try:
        check("begin_start 只拉起进程：返回时 running 仍为 False，耗时 <300ms",
              not judge_begin(s), "；".join(judge_begin(s)[:3]))
        check("真命名管道在逐拍 poll 里连上，版本读得到",
              not judge_handshake(s), "；".join(judge_handshake(s)[:3]) or
              f"{s['elapsed']:.2f}s / {s['poll_calls']} 拍")

        # 2) 连上以后命令真的能过：开一支样片，等内核自己报 path/duration
        res = p.open_files([VID], remember=False) or {}
        check("open_files 被接受（count=1、会换当前条目）",
              int(res.get("count", 0)) == 1 and bool(res.get("plays")),
              f"res={res}")
        # path 与 duration 是**两次独立推送**：path 先到、duration 晚一拍。只等 path
        # 会在同一条 tick 里读到 duration 的默认值 0，把"还没到货"误判成"没载入"。
        got = wait_for(lambda: os.path.basename(VID) in str(p.prop("path", ""))
                       and float(p.prop("duration", 0) or 0) > 0.0, 8.0)
        dur = float(p.prop("duration", 0) or 0)
        check("内核真的载入了这支片子（path 与时长都对）",
              got and 9.2 <= dur <= 10.8, f"path={p.prop('path')!r} duration={dur}")

        # 3) 退出：进程必须真的没了（不是只把 running 标成 False）
        proc = p._proc
        pid = p.mpv_pid
        p.quit()
        APP.processEvents()
        check("quit 之后进程真的结束、running=False",
              (not p.running) and proc is not None and proc.poll() is not None,
              f"pid={pid} rc={None if proc is None else proc.poll()}")
    finally:
        if p.running:
            p.quit()

    # 4) 内核早退：poll 必须看见并报出来，而不是一直 pending
    d = early_death_leg()
    try:
        bad = judge_early_death(d)
        check("内核刚启动就被杀：poll 判 fail 并点名 code（不是挂到超时）",
              not bad, "；".join(bad[:3]) or f"spent={d['spent']:.2f}s")
    finally:
        _finish(d["player"])

    # 5) 管道永远不出现：按 wait 到点失败、杀掉进程，不许无限 pending
    t = timeout_leg()
    try:
        bad = judge_timeout(t)
        check("管道没出现：按 deadline 判 fail 并收掉进程",
              not bad, "；".join(bad[:3]) or f"state={t['state']} spent={t['spent']:.2f}s")
    finally:
        _finish(t["player"])

    return report()


if __name__ == "__main__":
    sys.exit(main())
