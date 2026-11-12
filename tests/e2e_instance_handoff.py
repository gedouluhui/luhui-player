"""端到端：真的开**两个进程**，模拟「播放器已经开着，我又双击了第二个文件」。

这一趟证明的是什么（用户原话：双击视频确实打开了播放器，但视频根本没加载进来）：
上一轮的 tests/test_instance.py 是在**一个进程里**造一个假窗口来接转交，它能证明协议、
成帧、排队、去重这些规矩，但证明不了"两个真进程隔着命名管道也能把文件递到手"。
这条腿只有真跨进程才踩得到：

1. 起第一个实例（带样本 A），确认它**真的在播 A**——问内核自己的 `path`，不看界面标题；
2. 起第二个实例（带样本 B）：它**不许开第二扇窗口**，而要把 B 转交给第一个然后自己退掉；
3. 回到第一个实例：内核的 `path` 换成 B、`playlist-count` 还是 1（产品语义是"双击哪个就播
   哪个"，`add_paths(replace=True)` → `open_files(append=False)` → 内核收到 `loadfile … replace`，
   **替换**列表而不是往后追加），位置还在往前走、窗口标题带上 B —— 这几条互相咬合，
   任何一条单独骗过去都会被另一条抓住；
   反向对照那一趟把 B 换成盘上没有的路径：`path 换成 B` 那一条**必须翻红**（这就是这本账的牙，
   恒绿的量具抓不到"转交把文件吞了"），同时 A 必须**原样还在播**、列表不许被清空；
4. 第一个实例的 `launch.log` 里留下一条 `kind=forward argv=1 kept=1`：
   下次用户报"双击没加载"，这本账就是现场证据，不必再靠猜。
5. 收尾只按显式 PID 收拾自己起的进程（含内核），别人家一个不碰。

**静音**：这一趟会在真声卡上起两次播放，所以先把 `GELU_DATA` 指到临时目录、
在里头预置 `mute=true`＋`volume=0`，起进程后**从被测实例自己**读回 `mute`/`volume`
（进程内垫片一重启就作废，读回来的才算数）。读回来不是静音就立刻收场并记 ENV——
宁可这趟不作数，也不许在用户睡着的时候放出声。

跑法：
    python tests/e2e_instance_handoff.py
    python tests/e2e_instance_handoff.py --expect-red     # 反向对照：第二个文件换成不存在的，这本账必须红

退出码：0 全绿；1 至少一条真失败；2 环境不给（管道已被占／没有内核／静音前提不成立）。
"""
from __future__ import annotations

import ctypes
import datetime
import io
import os
import sys
import tempfile
import time

BS = chr(92)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

# 起进程的形状、按命令行认自家、隔着命名管道问属性、只按显式 PID 收拾、静音八档判据——
# 这些把手一律**借 tests/player_harness.py 那一份**，不在这里抄第二份。为什么值得较这个真：
# 抄第二份时坑会跟着抄，判据却会各改各的，最后分叉——静音那条真分叉过一次
# （`volume or 1` 把"音量确实是 0.0"读成"没读到"，于是**已经静音好**的一趟被判成 ENV，
# 整趟没踩到转交，留档 010121）。借来之后本文件只剩"判据与取证"，操作只有一个真源。
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import player_harness as ph
    PH_ERR = ""
except BaseException as _exc:                    # noqa: BLE001 - 把手起不来＝这趟不作数，不是产品坏
    ph = None
    PH_ERR = f"{type(_exc).__name__}: {_exc}"

SAMPLE_A = os.path.join(ROOT, "tests", "samples", "gelu-sample-3.mkv")
SAMPLE_B = os.path.join(ROOT, "tests", "samples", "gelu-sample-1.mp4")
BAD_B = os.path.join(ROOT, "tests", "samples", "这文件盘上根本没有.mkv")
BOOT_WAIT = 150.0                              # 第一次起窗口含冷启动与内核握手
HANDOFF_WAIT = 40.0                            # 转交那一击：等第二个进程退掉

RESULTS: list[list[str]] = []
OUT_PATH = ""
EXPECT_RED = "--expect-red" in sys.argv


def say(tag: str, what: str, why: str = "") -> None:
    RESULTS.append([tag, what, why])
    line = f"[{tag}] {what}" + (f" —— {why}" if why else "")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def count(tag: str) -> int:
    return sum(1 for r in RESULTS if r[0] == tag)


def kill_tree(pids: list[int], why: str) -> list[int]:
    """收拾自己起的那几个进程：**操作借 `ph.kill_pids`**（在命令行里认自家、terminate、
    补 kill 都在那一份里），这里只把结果记进本趟这本账——记账是各趟自己的事，不共用。
    """
    gone, still, skipped = ph.kill_pids(pids)
    for pid, cl in skipped:
        say("NOTE", f"跳过 pid={pid}：命令行里认不出自家，不动它", cl)
    say("PASS" if not still else "FAIL", f"收尾只按显式 PID 收拾自己起的进程（{why}）",
        f"terminate={gone} 仍在={still}" + (f" 没动的={len(skipped)}" if skipped else ""))
    return still


def launch_rows(data_dir: str) -> list[str]:
    path = os.path.join(data_dir, "logs", "launch.log")
    try:
        with io.open(path, encoding="utf-8", errors="ignore") as fh:
            return [l.strip() for l in fh if l.strip()]
    except OSError:
        return []


def run() -> int:
    global OUT_PATH
    out_dir = os.path.join(ROOT, "tests", "out")
    OUT_PATH = os.path.join(out_dir, "e2e-handoff-"
                            + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)
    t_all = time.time()

    # ---------------- 量具先自查：静音闸门八档已知好坏，错一档就是它自己瞎了 ----------------
    # 这一条不是装饰：第一版的静音判据用 `volume or 1`，把"音量真的是 0.0"读成"没读到"，
    # 于是**已经静音好**的一趟被记成 ENV，整趟没踩到转交那一步（留档 010121）。
    if ph is None:
        say("ENV", "共用把手 tests/player_harness.py 起不来（这一趟没它跑不了）", PH_ERR)
        return finish(2)
    bad = ph.silent_gate_selftest()
    say("PASS" if not bad else "FAIL",
        f"静音闸门自带 {len(ph.SILENT_CASES)} 档已知好坏对照（判据不许恒真/恒假）",
        "八档全对" if not bad else f"漏网 {len(bad)} 档：" + " ‖ ".join(bad))
    if bad:
        return finish(1)

    # ---------------- 环境闸门：缺哪样记 ENV 直接退，不许拿"没测"冒充"测过" ----------------
    if sys.platform != "win32":
        say("ENV", "只在 Windows 上跑（命名管道＋进程树是这一路的主角）", sys.platform)
        return finish(2)
    if ph.psutil is None:
        say("ENV", "进程核对能力（psutil）", ph.PSUTIL_ERR)
        return finish(2)
    if ph.cfgmod is None or ph.MpvIpc is None:
        say("ENV", "app.config / app.engine.ipc 起不来", ph.APP_ERR)
        return finish(2)
    if not os.path.exists(ph.cfgmod.MPV_EXE):
        say("ENV", "播放内核在不在（这一趟要真起两次内核）", ph.cfgmod.MPV_EXE)
        return finish(2)
    sample_b = BAD_B if EXPECT_RED else SAMPLE_B
    # 反向对照那一趟的 B **本来就该在盘上没有**——那正是它要喂进去的坏东西。
    # 这里要是一律 isfile() 拦下来，环境闸门就把自己的哨兵挡在门外（e2e_openwith.py 同款坑）。
    for p in ((SAMPLE_A,) if EXPECT_RED else (SAMPLE_A, sample_b)):
        if not os.path.isfile(p):
            say("ENV", "样本在不在", p)
            return finish(2)
    name = ph.pipe_name()
    if ph.pipe_listening(name):
        say("ENV", "前置条件：本机没有别的实例在听这根管道",
            f"{name} 已经有人在听——那一击会交给**别人**，这一轮不作数")
        return finish(2)
    say("PASS", "前置条件：管道没人听（这两个实例是这台机器上仅有的播放器）", name)

    # 临时数据目录：既隔离用户真正的配置（测试不许改用户设置），又是**静音**的落点。
    data = os.path.join(tempfile.gettempdir(), f"gelu-handoff-{os.getpid()}")
    env = ph.make_env(data, {"mute": True, "volume": 0})
    say("NOTE", "样本与数据目录", f"A={SAMPLE_A} B={sample_b} data={data}"
        f"（{'反向对照：B 故意用盘上没有的路径' if EXPECT_RED else '正常两文件'}）")

    alive: list[int] = []
    try:
        # ---------------- ① 第一个实例：真的把 A 播起来 ----------------
        base = {h[0] for h in ph.player_pids()}
        sp_a = ph.spawn(SAMPLE_A, env)
        say("NOTE", "第一个实例已丢出去（它就是「双击第一个文件」）", f"launcher pid={sp_a.pid}")
        pid_a, cmd_a = ph.wait_pid(SAMPLE_A, base, "带着样本 A", BOOT_WAIT)
        if not pid_a:
            say("FAIL", "第一个实例真的起来了（进程层）", cmd_a)
            return finish(1)
        alive.append(pid_a)
        say("PASS", "第一个实例真的起来了（进程层）", f"pid={pid_a}：{cmd_a[:150]}")

        mpid, mname, mcmd = ph.wait_child(pid_a, BOOT_WAIT)
        if not mpid:
            say("FAIL", "第一个实例把 mpv 内核实起来了（子进程层）", f"pid={pid_a} 名下没有 mpv")
            return finish(1)
        alive.append(mpid)
        pipe = ph.pipe_of(mcmd)
        say("PASS", "第一个实例把 mpv 内核实起来了（子进程层）",
            f"mpv pid={mpid}（{mname}）管道={pipe or '读不出'}")

        # 静音闸门：从**被测实例自己**读回来。写进 config 不等于内核真静音了，
        # 而这里一旦没静音就会在用户睡着的机器上放出声——宁可记 ENV 收场。
        # 判定只在 `ph.silent_now` 一处（上面那八档对照就是它的牙）。
        props, why = ph.ask(pipe, ["path", "mute", "volume", "pause", "idle-active",
                                   "playlist-count"])
        if not props:
            say("ENV", "问内核要属性这条腿跑不动（连不上管道／内核不接第二个客户端）",
                f"{why}；pipe={pipe!r}")
            return finish(2)
        silent = ph.silent_now(props)
        say("PASS" if silent else "ENV", "静音前提成立：从被测实例自己读回 mute/volume",
            f"mute={props.get('mute')!r} volume={props.get('volume')!r}"
            + ("" if silent else " —— 会出声，这一趟不作数，立刻收场"))
        if not silent:
            return finish(2)
        same_a = bool(props.get("path")) and \
            os.path.normcase(str(props["path"])) == os.path.normcase(SAMPLE_A)
        say("PASS" if same_a else "FAIL", "第一个实例的内核报的 path 就是样本 A",
            f"path={props.get('path')!r} 要找={SAMPLE_A!r} idle-active={props.get('idle-active')!r}")

        # ---------------- ② 第二个实例：不许开新窗，要转交后自己退 ----------------
        t1 = time.time()
        sp_b = ph.spawn(sample_b, env)
        alive.append(sp_b.pid)
        rc_b = None
        while time.time() < t1 + HANDOFF_WAIT:
            if sp_b.poll() is not None:
                rc_b = sp_b.returncode
                break
            time.sleep(0.2)
        dt_b = time.time() - t1
        if rc_b is None:
            say("FAIL", "第二个实例转交完就自己退掉（没退＝它开出了第二扇窗口）",
                f"pid={sp_b.pid} 等了 {dt_b:.1f}s 还活着")
        else:
            say("PASS" if rc_b == 0 else "FAIL",
                "第二个实例转交完就自己退掉（没退＝它开出了第二扇窗口）",
                f"pid={sp_b.pid} 退出码={rc_b} 耗时={dt_b:.2f}s")
        extra = [h for h in ph.player_pids() if h[0] not in (pid_a, sp_b.pid)]
        say("PASS" if not extra else "FAIL", "第二个实例没有另起一个播放器进程",
            f"除 A({pid_a}) 与 B({sp_b.pid}) 之外的实例进程={extra}")
        others = [h for h in ph.player_pids(sample_b)]
        say("PASS" if not others else "FAIL",
            "第二个文件没有开出「带自己文件名的第二个实例」（残留检查）",
            f"带着 B 的实例={[(p, c[:60]) for p, c in others]}")

        # ---------------- ③ 回到第一个实例：第二个文件到底进来了没有 ----------------
        # 产品的真实语义（读码确认，不是判据一相情愿）：转交走 `MainWindow.add_paths(kept)`，
        # 它的 `replace` 默认 True → `open_files(append=False)` → 内核收到 `loadfile B replace`。
        # 也就是"双击哪个就播哪个"：列表**换成**那一个，而不是往 A 后面追加。
        # 所以正向的账是 `path` 变成 B 且 `playlist-count` 仍是 1；反向（B 盘上没有）的账是
        # `kept=0` → add_paths 提前返回 → A **原样还在播**，列表不许被清空。
        pc_before = props.get("playlist-count")
        # 两条账分得很清楚：`want`（转交要落地）在反向那一趟**故意红**——那才是这本账的牙；
        # 而"坏文件不许把 A 挤掉"那条正反两趟都该绿，它守的是另一件事。
        # 反向时 `path` 永远等不到 B，不必把正向那 30 轮等待空烧一遍：8 轮足够判否。
        want = os.path.normcase(os.path.abspath(sample_b))
        want_a = os.path.normcase(os.path.abspath(SAMPLE_A))
        why_ask = "（还没问过）"
        got = {}
        for _i in range(8 if EXPECT_RED else 30):
            got, why_ask = ph.ask(pipe, ["path", "playlist-count", "playback-time", "pause"])
            if got and os.path.normcase(str(got.get("path") or "")) == want:
                break
            time.sleep(0.6)
        is_b = bool(got.get("path")) and os.path.normcase(str(got["path"])) == want
        say("PASS" if is_b else "FAIL",
            "第一个实例的内核把 path 换成了第二个文件（绕过界面，问内核自己）"
            + ("—— 反向对照里这条**必须红**" if EXPECT_RED else ""),
            f"path={got.get('path')!r} 要找={want!r} 问不动={why_ask or '（没）'}")
        pc = got.get("playlist-count")
        pc_ok = isinstance(pc, int) and pc == 1 and isinstance(pc_before, int) and pc_before >= 1
        say("PASS" if pc_ok else "FAIL",
            "播放列表按「双击哪个播哪个」换成第二条那一条（1 条，不是往 A 后面追加）",
            f"playlist-count {pc_before!r} → {pc!r}（语义=add_paths(replace=True)→loadfile replace）")
        if EXPECT_RED:
            kept_a = bool(got.get("path")) and os.path.normcase(str(got["path"])) == want_a
            say("PASS" if kept_a else "FAIL",
                "反向对照：盘上没有的文件不许把 A 挤掉（列表还是 A 那一条）",
                f"path={got.get('path')!r} 要找={want_a!r} playlist-count={pc!r}")
        # 反向那一趟的样本 A 只有 ~8s：等上面那几轮问完，它已经播到片尾，内核 pause=True、
        # 位置钉在 duration 上。那不是"停在封面"，是片长到了——这条判据的意思（位置在往前走）
        # 会因此红得没有指向。所以**只在反向趟**、量之前由量具把 A 倒回 0 并取消暂停
        # （动的是量具不是产品：这一趟要的是"A 没被挤掉"，而移动性仍然要真量）。
        # 证据串里把 eof-reached/duration 一起印出来，正反向都能一眼看出红在哪个形状上。
        rewind = ""
        if EXPECT_RED:
            rw = ph.tell(pipe, ["set_property", "time-pos", 0])
            rp = ph.tell(pipe, ["set_property", "pause", False])
            rewind = f"（量具把 A 倒回 0：time-pos→{rw}，pause→{rp}）"
            time.sleep(0.8)
            got, _wr = ph.ask(pipe, ["playback-time", "pause", "path"])   # 旧起点是片尾那个数，作废
        p1 = got.get("playback-time")
        time.sleep(2.5)
        got2, _w2 = ph.ask(pipe, ["playback-time", "pause", "path", "eof-reached", "duration"])
        p2 = got2.get("playback-time")
        moving = (isinstance(p1, (int, float)) and isinstance(p2, (int, float)) and p2 > p1
                  and got2.get("pause") is False)
        say("PASS" if moving else "FAIL",
            f"位置还在往前走、没停在封面（真的在播{'' if EXPECT_RED else '第二个'}文件）",
            f"playback-time {p1!r} → {p2!r} pause={got2.get('pause')!r} "
            f"eof-reached={got2.get('eof-reached')!r} duration={got2.get('duration')!r} "
            f"path={got2.get('path')!r}{rewind}")

        # ---------------- ④ 界面上也看得见（标题带文件名） ----------------
        from PyQt5.QtWidgets import QApplication  # 只为 EnumWindows 的量具声明 ctypes 已够，
        app = QApplication.instance() or QApplication(sys.argv[:1])   # noqa: F841
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        u.SetProcessDPIAware()
        u.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        cb_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.LPARAM, wt.LPARAM)
        titles: list[str] = []

        def cb(h, _l):
            box = wt.DWORD(0)
            u.GetWindowThreadProcessId(h, ctypes.byref(box))
            if int(box.value) == pid_a:
                n = u.GetWindowTextLengthW(h)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 2)
                    u.GetWindowTextW(h, buf, n + 1)
                    titles.append(buf.value)
            return True

        u.EnumWindows(cb_t(cb), 0)
        base_b = os.path.basename(sample_b)
        hit = [t for t in titles if base_b in t]
        say("PASS" if hit else ("FAIL" if not EXPECT_RED else "NOTE"),
            "第一个实例的窗口标题带上第二个文件（用户看得见的证据）",
            f"标题={titles} 要找={base_b!r}")

        # ---------------- ⑤ 留痕：下一次不必猜 ----------------
        rows = launch_rows(data)
        fwd = [l for l in rows if "kind=forward" in l]
        keep1 = [l for l in fwd if " kept=1 " in l]
        say("NOTE", "launch.log 全文", f"{len(rows)} 行：" + " ‖ ".join(l[:120] for l in rows))
        say("PASS" if rows else "FAIL", "两个实例各在 launch.log 里留下一行（这本账是自愈式的取证）",
            f"{data + BS + 'logs' + BS + 'launch.log'} 共 {len(rows)} 行")
        if EXPECT_RED:
            say("PASS" if (not keep1) else "FAIL",
                "反向对照：坏样本不该被记成「收到一条」（kept 必须是 0、dropped 必须是 1）",
                f"kind=forward 且 kept=1 的行={keep1}")
        else:
            say("PASS" if keep1 else "FAIL",
                "转交那一次留下 kind=forward、kept=1 的一行（下次报障就有现场）",
                f"forward 行={fwd}")
            say("PASS" if any("kind=cold" in l for l in rows) else "FAIL",
                "冷启动那一次留下 kind=cold 的一行（两种结局分得开）",
                f"共 {len(rows)} 行")

        rc = 1 if count("FAIL") else (2 if count("ENV") else 0)
        return finish(rc, t_all)
    finally:
        kill_tree(sorted(set(alive)), "本趟自己起的两个实例与内核")
        try:
            import shutil
            shutil.rmtree(data, ignore_errors=True)
            say("NOTE", "临时数据目录已清", data)
        except BaseException:
            pass


def finish(rc: int, t0: float | None = None) -> int:
    if EXPECT_RED:          # 反向对照：这本账要求"必须红"，全绿才说明量具是瞎的
        ok = count("FAIL") >= 1
        say("FINAL", f"反向对照：{'成立（判据真的会红）' if ok else '失败（喂了坏样本还全绿＝量具是瞎的）'}",
            f"FAIL {count('FAIL')} 条／ENV {count('ENV')} 条 rc={0 if ok else 1}")
        print("DONE", flush=True)
        _tail("DONE")
        return 0 if ok else 1
    tag = "全绿" if rc == 0 else ("有真失败" if rc == 1 else "环境不给")
    dur = f"耗时 {time.time() - t0:.1f}s" if t0 else "没起进程"
    say("FINAL", f"跨进程转交端到端：{tag} —— PASS {count('PASS')}／FAIL {count('FAIL')}／"
        f"ENV {count('ENV')}／NOTE {count('NOTE')}", f"{dur} rc={rc}")
    print("DONE", flush=True)
    _tail("DONE")
    return rc


def _tail(line: str) -> None:
    if not OUT_PATH:
        return
    try:
        with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        print("TRACEBACK（这一趟不作数）:", flush=True)
        traceback.print_exc()
        say("FINAL", "量具自己崩了 —— 端到端不作数", "rc=2")
        print("DONE", flush=True)
        _tail("DONE")
        sys.exit(2)
