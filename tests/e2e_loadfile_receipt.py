"""真内核那一腿：产品自己发的 `loadfile`，这台 mpv 收不收、收了有没有从续播位置起。

离线那两趟（`tests/test_append_semantics.py` 的 E21、`tests/test_open_receipt.py`）能证明的
只有"命令串的形状按量具表逐位对得上"与"界面那句话改由回执撑着"。它们**都没问过内核**——
而 2026-10-19 那笔事故恰恰是"离线全绿、用户那边黑的"：E11 当时是绿的，因为它只查字符串里
有没有 `start=` 字样，内核却因为那一项落在 index 位上把整条命令退了回去。
所以这一腿只干离线做不到的三件事：
 ① 用**产品的** `MpvPlayer.open_files` 真起内核真播（不是裸 IPC、不是推演器）；
 ② 有续播历史时必须**从历史位置起播**——这是 `start=` 被收下之后唯一的外部可见后果；
 ③ 拿"没有历史"那一趟当**鉴别力对照**：两个读数必须拉开，否则这把尺只是饱和。
外加一条把话说清楚：界面那句失败提示写着"原话在 data\\logs\\mpv.log"，这句承诺本身也得量——
旧凶形状当阳性对照，日志里到底有没有内核的原话，测出来什么就写什么（看不见＝那句是空头承诺）。

这把尺自己的牙（怎么注入、怎么还原、两份 md5 与三句翻红的读数全在留档
`tests/out/e2e-loadfile-drift-1019-215401.txt`）：把 `player.open_files` 里那一项从
`[-1, "start=…"]` 注回**当年的凶形状** `[["start=…"]]`，三句同时翻红——K3 装载计数 1→1、
time-pos 仍停在基线那一秒，K3b 差 0.00s，K4 产品路上冒出
`Command loadfile: argument index has incompatible type.`；K2 照旧绿，正对上当年
"手上没进度时看着一切正常、一旦带续播位置整条被退"的现场。还原走第三方留底（/Temp 里
带时间戳的 `cp -p` 副本），还原后再跑一遍确认全绿。

静音是前置（K1）：所有读数从**被测实例自己**重读，不继承上一趟的"已经静音了"
（真设备的静音闸门是进程内的，换了内核实例就作废）。
数据目录一律 `GELU_DATA` 指到临时目录，K7 还要证明用户真那份 `data\\` 一个字节都没动。
样片用 `gelu-sample-long.mp4`（32s，`tests/gen_sample.py` 里的样片 D）：续播那一档要躲开
`Config.remember_position` 的"片尾 15 秒／92% 算看完"规则，短片上根本没有合法的续播点——
这一条不是推演，是 2026-10-19 第二趟真内核跑出来的红（写入=6.0 读回=0.0）。

跑法：
    python tests/e2e_loadfile_receipt.py
退出码：0 全绿；1 有真失败；2 环境不给（Qt／内核可执行文件／内核起不来／IPC 半路断／
样片太短使续播那一档没有合法输入）。环境红一律记 ENV，不冒充产品红。
"""
from __future__ import annotations

import ast
import ctypes
import datetime
import inspect
import io
import json
import os
import re
import shutil
import sys
import time

if hasattr(sys.stdout, "reconfigure"):     # 本机控制台默认 GBK，见 test_append_semantics 同条
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = os.path.join(os.environ.get("TEMP", "C:" + chr(92) + "temp"), f"gelu-loadfile-{os.getpid()}")
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)
os.environ["GELU_DATA"] = TMP                      # 必须在 import app.config 之前
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import player_harness as H          # noqa: E402  - 静音判据八档与进程把手共用同一份真源
from app import config as cfgmod    # noqa: E402
from app.engine.player import MpvPlayer    # noqa: E402

RESULTS: list[list[str]] = []
OUT_PATH = ""
SAMPLE = os.path.join(HERE, "samples", "gelu-sample-long.mp4")
MIN_DUR = 25.0
# 25 这个地板不是随手写的，是被**产品自己的规则**倒逼出来的：`Config.remember_position`
# 把"距片尾 15 秒以内"和"看过 92% 以上"都记成看完（下次从头播），于是短片上不存在合法的
# 续播位置——本机第一趟真内核就是这样撞红的（写入=6.0 读回=0.0，样片只有 10s）。
# 要 0.4D 与 D−15.5 都 > 1 秒且拉开，D 至少 25s；样片 D（32s）就是为这一条生成的。
TAIL = 15.0                       # 同那条"片尾 15 秒算看完"，K3 选点时要避开

# Qt 用来看见"事件投递"这一路：`sig_file_loaded` 是从 IPC 读线程 emit 的，默认 AutoConnection
# 会按**接收方的线程亲和**排成 QueuedConnection——那需要主线程有个事件循环在转。
# 量具自己不起事件循环时计数器会永远停在 0（本机实发：内核日志里 10 秒的片子一路播到 EOF，
# 这边计数 0→0，看上去像"产品没收到 file-loaded"，其实是量具瞎）。所以这里分两只计数器：
#   direct＝挂 DirectConnection，只回答"内核到底发没发这一声"（等装载完成用它）；
#   queued＝按产品原样（Auto）挂，回答"主线程那条投递路也走得通吗"（要 processEvents 才涨）。
# 两只分开记才分得开"内核没发"与"量具没泵"——这跟 app/engine/ipc.py 里那条死锁守卫是同一族坑。
try:
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
except BaseException as _exc:                     # noqa: BLE001 - 起不来说明这趟不作数
    Qt = None
    QApplication = None
    QT_ERR = f"{type(_exc).__name__}: {_exc}"
else:
    QT_ERR = ""

QAPP = None


def ensure_app():
    """造一个**活得比这趟久**的 QApplication（模块级引用扣住）。

    为什么必须挂全局：`a = QApplication([])` 写在函数里，函数一返回 sip 就把这份
    QCoreApplication 回收，于是所有无父 QObject 的 C++ 半边一起没——本机实发的形状是
    读线程此后每一条事件都报 `[ipc] 回调异常: wrapped C/C++ object of type MpvPlayer
    has been deleted`，看上去像"产品的事件层坏了"，其实是量具把 app 释放了。
    产品在 app/main.py 里那份是全局的，所以这条路只有量具会踩。
    """
    global QAPP
    if QAPP is None:
        QAPP = QApplication.instance() or QApplication(["gelu-e2e-loadfile"])
    return QAPP


def say(tag: str, what: str, why: str = "") -> None:
    RESULTS.append([tag, what, why])
    line = f"[{tag}] {what}" + (f" —— {why}" if why else "")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with io.open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def count(tag: str) -> int:
    return sum(1 for r in RESULTS if r[0] == tag)


def ask_real(p: MpvPlayer, prop: str) -> tuple[object, str]:
    """问**被测实例的内核**要一个属性（同步往返，值不来自任何缓存）。

    为什么不读 `p.prop(...)`：那是订阅缓存。"缓存里有什么"与"内核现在是什么"是两件事
    ——E18/E19 那笔账就是这一族，而这一趟要判的恰恰是内核那头。
    """
    if p._ipc is None:
        return None, "IPC 还没接上"
    try:
        return p._ipc.request(["get_property", prop], timeout=5.0), ""
    except BaseException as exc:                   # noqa: BLE001 - 问不到要点名，不许当成 None
        return None, f"{type(exc).__name__}: {exc}"


def wait_load(p: MpvPlayer, nb: dict, timeout: float) -> tuple[bool, str, str, float]:
    """等**这一趟**的 file-loaded：内核亲口说"这支文件我装载完了"，再谈时长与位置。

    为什么不是等 `path`：`path` 在 `start-file` 那一刻就已经是这支片子了，而 `duration`／
    `time-pos` 要到 `file-loaded` 之后才有值——第一版就是栽在这里，把内核的
    "property unavailable" 读成了"产品没播起来"（一条假红）。这一趟判的恰恰是界面那句
    "已载入"所等的那一声，所以证据必须用同一个：内核的 file-loaded 事件。
    用 `nb["direct"]`（DirectConnection 那只）而不是 `nb["queued"]`：等的是"内核发没发"，
    而 queued 那只还额外要求主线程有个事件循环在转，拿它当装载判据会把量具没泵误读成
    内核没发（本机第二趟实发：内核日志里片子一路播到 EOF，这边计数 0→0）。
    判"计数变长"而不是"值大于几"，因为反复开同一支片子时计数是唯一可靠的分隔。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if nb["direct"] > nb["base"]:
            v, err = ask_real(p, "path")
            if v and os.path.abspath(str(v)) == SAMPLE:
                return True, str(v), "", time.time() - t0
        time.sleep(0.2)
    return False, "", (f"{timeout:.0f}s 没等到 file-loaded（direct 计数"
                       f" {nb['base']}→{nb['direct']}，queued {nb['queued']}）"), time.time() - t0


def wait_num(p: MpvPlayer, prop: str, cond, timeout: float) -> tuple[float, str, float]:
    """把某个数值属性问到满足条件为止。回 (值, 原因, 耗时)；问不到回 (-1.0, 原因, 耗时)。"""
    t0 = time.time()
    err = ""
    while time.time() - t0 < timeout:
        v, err = ask_real(p, prop)
        if isinstance(v, (int, float)) and cond(float(v)):
            return float(v), "", time.time() - t0
        time.sleep(0.2)
    return -1.0, err or f"{prop} 在 {timeout:.0f}s 内没到条件", time.time() - t0


def num(p: MpvPlayer, prop: str) -> tuple[float, str]:
    v, err = ask_real(p, prop)
    return (float(v), "") if isinstance(v, (int, float)) else (-1.0, err or f"{prop} 读回来不是数：{v!r}")


def mpv_log_text(p: MpvPlayer) -> str:
    try:
        with io.open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        return f"（读不到日志：{exc}）"


def reject_lines(text: str) -> list[str]:
    return [ln.strip()[:180] for ln in (text or "").splitlines()
            if "incompatible" in ln.lower() or "error running command" in ln.lower()]


def snapshot_user_data() -> dict[str, tuple[int, int]]:
    real = os.path.join(ROOT, "data")
    out: dict[str, tuple[int, int]] = {}
    for dirpath, _dirs, files in os.walk(real):
        for f in files:
            fp = os.path.join(dirpath, f)
            try:
                st = os.stat(fp)
                out[os.path.relpath(fp, real)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                out[os.path.relpath(fp, real)] = (-1, -1)
    return out


def pid_alive(pid: int) -> bool:
    if sys.platform != "win32" or not pid:
        return False
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x1000, 0, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259          # STILL_ACTIVE


# ============================================================ 主流程
def main() -> int:
    global OUT_PATH
    OUT_PATH = os.path.join(ROOT, "tests", "out", "e2e-loadfile-receipt-"
                            + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)
    say("NOTE", "量具形状",
        "真起内核＝产品自己的 MpvPlayer.start(hwnd=0)＋open_files；读数一律同步问内核本体"
        f"（不读订阅缓存）；GELU_DATA={TMP}；样片={os.path.basename(SAMPLE)}")
    if QT_ERR:
        say("ENV", "K0 Qt 起不来（这一趟有三条判据要靠它：装载事件、投递路、事件循环）", QT_ERR)
        return finish(2)
    leaks = H.silent_gate_selftest()
    say("PASS" if not leaks else "FAIL", "K0a 静音判据八档自测（判据自己的牙）",
        "八档全对" if not leaks else "；".join(leaks))
    if not os.path.isfile(SAMPLE):
        say("ENV", "K0 样片不在", SAMPLE)
        return finish(2)
    if not os.path.exists(cfgmod.MPV_EXE):
        say("ENV", "K0 内核可执行文件不在", cfgmod.MPV_EXE)
        return finish(2)

    user_before = snapshot_user_data()
    with io.open(os.path.join(TMP, "config.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"resume": True, "mute": True, "volume": 0}, ensure_ascii=False))
    ensure_app()                              # 先于 MpvPlayer：Qt 对象要活在 app 之后
    cfg = cfgmod.Config()
    p = MpvPlayer(cfg)
    st = {"pid": 0, "t0": time.time()}
    rc = 2
    try:
        rc = run(p, cfg, st)
    finally:
        # 收拾与算账分开：`finally` 里**不许 return**——那里一 return 就把异常吞成"正常结束"，
        # 于是量具自己崩了也能顶着一条 [FINAL] 全绿出场（本项目实发过的假绿光形状）。
        cleanup(p, st, user_before)
    return finish(rc)


def run(p: MpvPlayer, cfg: cfgmod.Config, st: dict) -> int:
    t0 = st["t0"]
    ok_start = p.start(0, wait=25.0)
    st["pid"] = p._proc.pid if p._proc else 0
    if not ok_start or not st["pid"]:
        say("ENV", "K0 内核起不来（这一趟不作数，不记产品红）",
            f"start 回 {ok_start}，耗时 {time.time() - t0:.1f}s，日志尾部："
            f"{mpv_log_text(p)[-260:]!r}")
        return 2
    say("PASS", "K0 真内核起来了（产品自己的 start）",
        f"pid={st['pid']} 耗时 {time.time() - t0:.1f}s")
    return legs(p, cfg, st)


def cleanup(p: MpvPlayer, st: dict, user_before: dict) -> None:
    pid = st.get("pid") or 0
    try:
        p.quit()
    except BaseException as exc:                   # noqa: BLE001 - 收拾不掉要点名，不许静默
        say("FAIL", "K6 quit 报异常", f"{type(exc).__name__}: {exc}")
    time.sleep(1.5)
    if pid and pid_alive(pid):
        H.kill_pids([pid])
    alive = bool(pid) and pid_alive(pid)
    say("PASS" if not alive else "FAIL", "K6 内核进程收干净了（只按自家 PID）",
        f"pid={pid} 仍在={alive}")
    say("NOTE", "内核日志尾部", mpv_log_text(p)[-200:].replace("\n", " / "))
    after = snapshot_user_data()
    changed = [k for k in set(after) | set(user_before) if after.get(k) != user_before.get(k)]
    say("PASS" if not changed else "FAIL", "K7 用户真那份 data\\ 一个字节都没动",
        f"变化文件={changed or '（没有）'}（写全进临时目录 {TMP}）")
    shutil.rmtree(TMP, ignore_errors=True)


def legs(p: MpvPlayer, cfg: cfgmod.Config, st: dict) -> int:
    """K1–K5：静音前置 → 基线 → 续播 → 鉴别力对照 → 产品路上无退回 → 拒绝原话可见。"""
    # ---- K1 静音：从这台实例自己重读，绝不继承"上一趟已经静音了"
    mute, e1 = ask_real(p, "mute")
    vol, e2 = ask_real(p, "volume")
    if not H.silent_now({"mute": mute, "volume": vol}):
        p.set_volume(0)
        p.set_prop("mute", True)
        mute, e1 = ask_real(p, "mute")
        vol, e2 = ask_real(p, "volume")
    silent = H.silent_now({"mute": mute, "volume": vol})
    say("PASS" if silent else "FAIL", "K1 这一趟是静音的（读数来自被测实例）",
        f"mute={mute!r} volume={vol!r}（{(e1 or '')[:60]} {(e2 or '')[:60]}）")

    # ---- K2 基线：盘上没历史 ⇒ 命令里不该有 start，内核真收了、真装载完了、时间轴贴着 0
    if cfg.watch_state(SAMPLE) or cfg.recall_position(SAMPLE):
        say("FAIL", "K2 前提立不住：临时目录里已经有这支片子的进度",
            f"watch_state={cfg.watch_state(SAMPLE)} recall={cfg.recall_position(SAMPLE)}"
            "（判据不放宽——它此刻量的不是「没有历史」那一档）")
        return 1
    nb = {"direct": 0, "queued": 0, "base": 0}
    p.sig_file_loaded.connect(lambda: nb.__setitem__("direct", nb["direct"] + 1),
                              Qt.DirectConnection)
    p.sig_file_loaded.connect(lambda: nb.__setitem__("queued", nb["queued"] + 1))

    def pumped() -> None:
        """替主线程走几步事件循环：产品里这一声就是靠它送到 MainWindow 手上的。"""
        app = ensure_app()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.05)
        app.processEvents()

    res = p.open_files([SAMPLE], append=False) or {}
    got, path_now, why, w1 = wait_load(p, nb, 25.0)
    if not got:
        verdict = "ENV" if not pid_alive(p._proc.pid if p._proc else 0) else "FAIL"
        say(verdict, "K2 内核没把这支片子装载完"
            if verdict == "FAIL" else "K2 内核中途没了（这一趟不作数）",
            f"{why}；返回值={res}；日志尾部：{mpv_log_text(p)[-300:]!r}")
        return 2 if verdict == "ENV" else 1
    pumped()
    say("PASS" if nb["queued"] >= nb["direct"] > nb["base"] else "FAIL",
        "K2b 这一声在主线程那条投递路上也到了（界面等的就是它）",
        f"direct={nb['direct']} queued={nb['queued']}（direct>queued＝内核发了、队列没走；"
        "两个都不涨＝产品的事件接线断了，那是真红不是量具瞎）")
    p.pause()
    D, edur, _ = wait_num(p, "duration", lambda x: x > 0, 8.0)
    tp_base, etp, _ = wait_num(p, "time-pos", lambda x: x >= 0, 8.0)
    near0 = D >= 0 and tp_base >= 0 and tp_base <= max(1.5, D * 0.2)
    say("PASS" if (D >= MIN_DUR and near0 and not edur and not etp) else "FAIL",
        "K2 没历史：内核真收了、真装载完了，且时间轴贴着 0",
        f"path={os.path.basename(path_now)} file-loaded 用了 {w1:.1f}s duration={D:.2f}s "
        f"time-pos={tp_base:.2f}s（要 ≤{max(1.5, D * 0.2):.2f}s）{(edur or etp) or ''} 返回值={res}")
    if 0 <= D < MIN_DUR:
        say("ENV", "K3 的对照失去鉴别力：样片太短",
            f"duration={D:.2f}s < {MIN_DUR}s（不放宽阈值——基线与续播两个读数会重叠，"
            f"且短片上躲不开那条片尾 {TAIL:.0f}s 算看完的规则）")
        return 2

    # ---- K3 有历史：必须从历史位置起播（start= 被收下唯一的外部可见后果）
    # 选点先躲开产品自己那条规则：`remember_position` 把"距片尾 15 秒内"与"看过 92% 以上"
    # 都记成看完＝存进去的 pos 直接是 0.0。所以取 0.4D 与 D−TAIL−0.5 里更小的那个。
    target = min(round(D * 0.4, 3), round(D - TAIL - 0.5, 3))
    if target <= 1.0:
        say("ENV", "K3 挑不出合法的续播点（这一趟不作数）",
            f"duration={D:.2f}s→候选={target:.2f}s，而产品只认 >1s 的进度"
            "（player.open_files 里 `if pos > 1`，remember_position 里 `pos <= 1` 直接不写）")
        return 2
    cfg.remember_position(SAMPLE, target, D)
    back = cfg.recall_position(SAMPLE)
    if abs(back - target) > 0.01:
        say("FAIL", "K3 前提立不住：合法的续播位置写进去了却读不回来",
            f"写入={target:.2f}s 读回={back:.2f}s 台账={cfg.watch_state(SAMPLE)}"
            "（这一步还没碰内核，红的是记账那一层）")
        return 1
    nb["base"] = nb["direct"]
    t1 = time.time()
    res2 = p.open_files([SAMPLE], append=False) or {}
    got2, _, why2, w2 = wait_load(p, nb, 20.0)
    pumped()
    p.pause()
    tp3, etp3 = num(p, "time-pos")
    lo = target - max(1.0, D * 0.1)
    ok3 = not etp3 and tp3 >= lo and got2 and nb["queued"] >= nb["direct"] > nb["base"]
    say("PASS" if ok3 else "FAIL", "K3 有历史：从历史位置起播（内核确实收下了 start=）",
        f"写入={target:.2f}s 读到 time-pos={tp3:.2f}s（要 ≥{lo:.2f}s）"
        f"file-loaded 用了 {w2:.1f}s（direct {nb['base']}→{nb['direct']}，queued {nb['queued']}）"
        f"{'' if got2 else '但 ' + why2}"
        f"{('；' + etp3) if etp3 else ''} 返回值={res2} 全程 {time.time() - t1:.1f}s")
    gap = tp3 - tp_base
    say("PASS" if gap >= target / 3.0 else "FAIL",
        "K3b 鉴别力对照：续播那趟比基线那趟真的拉开了",
        f"基线={tp_base:.2f}s 续播={tp3:.2f}s 差={gap:.2f}s（要 ≥{target / 3.0:.2f}s，"
        "否则这把尺是饱和读数，K3 那句绿不算数）")

    # ---- K4 产品这条路上没有一条命令被内核退回（**趁**还没故意发坏形状之前读）
    bad = reject_lines(mpv_log_text(p))
    say("PASS" if not bad else "FAIL", "K4 产品那条路上内核一条命令都没退",
        f"退回行={bad or '（0 条）'}（日志={os.path.basename(p.log_path)}）")

    # ---- K5 阳性对照：把当年的凶形状**故意**发一遍，看内核的原话到底看得见看不见
    if p._ipc is None:
        say("ENV", "K5 IPC 断了，阳性对照发不出去（这一趟不作数）", f"pipe={p.pipe_path}")
        return 2
    err_repr = ""
    try:
        r = p._ipc.request(["loadfile", SAMPLE, "replace", [f"start={target:.3f}"]], timeout=5.0)
        err_repr = f"应答={r!r}"
    except BaseException as exc:                   # noqa: BLE001 - 被拒正是这趟要的读数
        err_repr = f"抛出={type(exc).__name__}: {exc}"
    time.sleep(1.0)
    fresh = reject_lines(mpv_log_text(p))
    seen = bool(fresh) or "incompatible" in err_repr.lower()
    say("PASS" if seen else "FAIL",
        "K5 内核的拒绝原话看得见（界面那句「原话在 data\\logs\\mpv.log」靠这条撑着）",
        f"旧凶形状→{err_repr}；日志里退回行={fresh or '（没有）'}"
        + ("" if seen else "＝看不见，界面那句提示是空头承诺，得提级或改话术"))
    return 1 if count("FAIL") else 0


def _site_codes() -> list[str]:
    """现数"这趟准备了几条判定"，编号取每条 say 标签开头的 K 号。

    为什么不敢手抄：`[FINAL]` 那句"判据 7 条＋1 条自测"以前是手写的，改版时必滞后
    （本项目实发过）。站点数也**不等于**实跑行数——K3 之前 return 掉的那些站点不会响，
    所以这里把差额逐个点名（`没响的编号`），绝不拿两个总数硬凑。
    """
    codes: list[str] = []
    for fn in (main, run, legs, cleanup):
        try:
            tree = ast.parse(inspect.getsource(fn))
        except (OSError, TypeError, SyntaxError):   # noqa: BLE001 - 读不到源码就退回"数不清"
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "say"
                    and len(node.args) >= 2):
                for c in ast.walk(node.args[1]):
                    if (isinstance(c, ast.Constant) and isinstance(c.value, str)
                            and re.match(r"^K\d+[a-z]?\b", c.value.strip())):
                        codes.append(re.match(r"^(K\d+[a-z]?)", c.value.strip()).group(1))
                        break
    seen: list[str] = []
    for c in codes:
        if c not in seen:
            seen.append(c)
    return seen


def finish(rc: int) -> int:
    print("DONE", flush=True)
    if OUT_PATH:
        try:
            with io.open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("DONE\n")
        except OSError:
            pass
    got = {m.group(1) for r in RESULTS if (m := re.match(r"^(K\d+[a-z]?)", r[1].strip()))}
    sites = _site_codes()
    silent = [c for c in sites if c not in got]
    shutil.rmtree(TMP, ignore_errors=True)     # 三条 ENV 早退路也走到这里：临时目录不许留残骸
    say("FINAL", f"真内核 loadfile 回执：{'全绿' if rc == 0 else ('有真失败' if rc == 1 else '环境不给')}"
        f" —— PASS {count('PASS')}／FAIL {count('FAIL')}／ENV {count('ENV')}／NOTE {count('NOTE')}",
        f"判定站点 {len(sites)} 个编号（AST 现数）／本趟响过 {len(got)} 个"
        f"／没响的={silent or '（没有）'}（前面有 return，不是漏判）rc={rc}")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        print("TRACEBACK（这一趟不作数）:", flush=True)
        traceback.print_exc()
        say("FINAL", "量具自己崩了 —— 真内核这一趟不作数", "rc=2")
        print("DONE", flush=True)
        sys.exit(2)
