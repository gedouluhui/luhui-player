# -*- coding: utf-8 -*-
"""R25 四轴复检的离线判据：引擎健壮性 + 界面一致性。

这份量具只测**这一轮改动的行为**，全部离屏（`QT_QPA_PLATFORM=offscreen`），
不碰用户桌面、不启动 mpv、不弹真窗口。每条判据都先于实现写成（红），
再由产品代码转绿；跑法：

    python tests/test_round25.py

它盯的几类事：
· 只读目录 / 磁盘满时记进度不能把整个进程带走（Qt 槽里抛异常 = abort）；
· IPC 读/写线程死了，等在途请求必须**立刻**失败，不许干等 3 秒超时；
· 内核重启看门狗：久跑之后额度重置、旧管道先关再接新内核（不泄漏句柄/线程）；
· 启动日志只读尾巴（整份读在 1MB 日志上就是白占一份内存 + 一次长读盘）；
· 拖一个盘根进来，目录遍历也要有上限（文件数上限管不住"目录多、媒体少"）；
· 缩略图/封面回收遇到坏 meta、文件中途消失不许把回收炸掉；
· GIF 导出在**进程起来之前**被取消，就不该再起 ffmpeg；
· 空列表时任务栏标题要跟着回正；外部路径打不开时播放中也要看得见；
· 全屏开关不许把已最大化的窗口打回普通态；重复点当前页签不重放淡入；
· 续播：第一支之外的条目也要从各自上次的位置开始；
· 无参数启动第二份时，把已有窗口叫到前面，而不是再开一扇；
· 弱化文字达到 AA 对比度；长 OSD 中间省略而不是两头被裁；
· 换主色后 logo / 倍速胶囊 / 选中图标当场换色（不用重启）；
· 滑轨/窗钮/幽灵按钮有按下档；图标按真实屏幕 DPR 渲染。
"""
from __future__ import annotations

import atexit
import builtins
import json
import os
import shutil
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="gelu-r25-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QEvent, QObject, Qt, pyqtSignal                # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget                       # noqa: E402

from app import config as cfgmod                                        # noqa: E402
from app import main as M                                               # noqa: E402
from app.engine import player as player_mod                             # noqa: E402
from app.engine.gif import GifJob                                       # noqa: E402
from app.engine.ipc import IpcError, MpvIpc                             # noqa: E402
from app.engine.player import MpvPlayer                                 # noqa: E402
from app.engine.posters import PosterStore                              # noqa: E402
from app.engine.thumbs import ThumbStore                                # noqa: E402
from app.ui import icons, theme                                         # noqa: E402
from app.ui.chrome import Chrome                                        # noqa: E402
from app.ui.controls_bar import ControlsBar                             # noqa: E402
from app.ui.floaters import Osd                                         # noqa: E402
from app.ui.main_window import MainWindow                               # noqa: E402
from app.ui.title_bar import TitleBar                                   # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])
results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except BaseException as exc:          # 判据自己崩了也要单记 FAIL，不许伪造通过
        ok, detail = False, f"判据异常 {type(exc).__name__}: {exc}"
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def pump(ms: int = 60) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()


def make_cfg(name: str = "cfg.json") -> cfgmod.Config:
    cfg = cfgmod.Config(path=os.path.join(TMP, name))
    cfg["resume"] = False
    cfg["mute"] = True
    cfg["screenshot_dir"] = os.path.join(TMP, "shots")
    os.makedirs(cfg["screenshot_dir"], exist_ok=True)
    cfg.save()
    return cfg


def write(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ============================================================ 引擎
def t_config_save_failure() -> tuple[bool, str]:
    """只读/不可建的数据目录：remember_position 不许抛，内存里的进度仍然可见。"""
    cfg = cfgmod.Config(path=os.path.join(TMP, "cfg-save.json"))
    old = cfgmod.DATA_DIR
    blocker = os.path.join(TMP, "not-a-dir")
    write(blocker, b"file blocks makedirs")
    cfgmod.DATA_DIR = blocker
    try:
        cfg.remember_position("C:/media/blocked.mkv", 12.0, 100.0)
        entry = cfg.watch_state("C:/media/blocked.mkv")
        ok = isinstance(entry, dict) and abs(float(entry.get("pos", -1)) - 12.0) < 1e-6
        return ok, f"内存进度={entry!r}"
    finally:
        cfgmod.DATA_DIR = old


def t_corrupt_watch_state() -> tuple[bool, str]:
    """手改坏 watch-state（非字典条目 / 非数字时间戳）：记进度与读进度都不许炸。"""
    d = os.path.join(TMP, "ws-corrupt")
    os.makedirs(d, exist_ok=True)
    old = cfgmod.DATA_DIR
    cfgmod.DATA_DIR = d
    bogus = {"bad": 5, "worse": {"pos": "x", "dur": "y", "ts": "z"},
             "good": {"pos": 12.0, "dur": 100.0, "ts": 1}}
    with open(os.path.join(d, "watch-state.json"), "w", encoding="utf-8") as fh:
        json.dump(bogus, fh)
    cfg = cfgmod.Config(path=os.path.join(TMP, "cfg-ws.json"))
    try:
        cfg.remember_position("C:/media/new.mkv", 30.0, 100.0)
        snap = cfg.watch_snapshot()
        all_dict = all(isinstance(v, dict) for v in snap.values())
        new_ok = abs(float(snap.get("C:/media/new.mkv", {}).get("pos", -1)) - 30.0) < 1e-6
        bad_ok = cfg.recall_position("worse") == 0.0
        good_ok = cfg.recall_position("good") == 12.0
        return all_dict and new_ok and bad_ok and good_ok, (
            f"全字典={all_dict} 新进度={new_ok} 坏值={bad_ok} 好值={good_ok}")
    finally:
        cfgmod.DATA_DIR = old


def t_ipc_fail_pending() -> tuple[bool, str]:
    """读/写线程一死，在途 request 必须立刻报错，而不是干等到超时。"""
    ipc = MpvIpc(r"\\.\pipe\gelu-r25-never")
    slot = {"done": threading.Event(), "reply": None}
    ipc._pending[1] = slot
    ipc._fail_pending("IPC 读写线程已退出")
    direct = slot["done"].is_set() and "退出" in str(slot["reply"])

    ipc2 = MpvIpc(r"\\.\pipe\gelu-r25-never-2")
    ipc2._write = lambda payload: None            # 不碰真管道
    got: list[str] = []

    def worker():
        try:
            ipc2.request(["get_property", "path"], timeout=5.0)
        except BaseException as exc:
            got.append(type(exc).__name__)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    time.sleep(0.08)
    t0 = time.monotonic()
    ipc2._fail_pending("IPC 读写线程已退出")
    th.join(1.5)
    fast = time.monotonic() - t0 < 1.0 and not th.is_alive()
    return direct and got == ["IpcError"] and fast, (
        f"直投={direct} 异常={got} 快速失败={fast}")


class _FakeProc:
    def __init__(self, code: int = 1):
        self.code = code

    def wait(self, *a, **k):
        return self.code

    def poll(self):
        return self.code


class _FakeIpc:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def t_watchdog_restart() -> tuple[bool, str]:
    """久跑>=60s 后重启额度要重置；重启前旧 IPC 必须先关（否则句柄/线程泄漏）。"""
    cfg = make_cfg("wd.json")
    p = MpvPlayer(cfg)
    p._quitting = False
    p._hwnd = 0
    p._proc = _FakeProc(1)
    p._ipc = _FakeIpc()
    p._restart_count = 2
    p._proc_started = time.monotonic() - 120.0
    started: list[int] = []
    p.start = lambda hwnd, wait=10.0: (started.append(hwnd), False)[1]
    died: list[int] = []
    p.sig_died.connect(lambda c: died.append(c))
    p._start_watchdog()
    p._watchdog.join(3.0)
    pump(30)     # sig_died 从看门狗线程走队列投递；不转一圈事件循环就看不见
    long_ok = started == [0] and p._ipc.closed == 1 and p._restart_count == 1 and died == [1]

    p2 = MpvPlayer(cfg)
    p2._quitting = False
    p2._hwnd = 0
    p2._proc = _FakeProc(1)
    p2._ipc = _FakeIpc()
    p2._restart_count = 2
    p2._proc_started = time.monotonic() - 5.0
    started2: list[int] = []
    p2.start = lambda hwnd, wait=10.0: (started2.append(hwnd), False)[1]
    p2._start_watchdog()
    p2._watchdog.join(3.0)
    short_ok = started2 == [] and p2._ipc.closed == 0 and p2._restart_count == 2
    return long_ok and short_ok, (
        f"久跑后重启={long_ok}（started={started} close={p._ipc.closed} n={p._restart_count}）"
        f" 短跑不重置={short_ok}")


def t_log_tail() -> tuple[bool, str]:
    """启动日志只读尾巴：1MB 日志不许整份 read() 进来。"""
    p = MpvPlayer(make_cfg("log.json"))
    p.log_path = os.path.join(TMP, "big-mpv.log")
    with open(p.log_path, "wb") as fh:
        fh.write(b"x" * (1024 * 1024))
        fh.write(b"\nFatal error: option not found\n")
    msgs: list[str] = []
    p.sig_log.connect(lambda t: msgs.append(t))
    reads: list[int] = []
    real_open = builtins.open

    class Spy:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

        def read(self, *a):
            data = self._fh.read(*a)
            reads.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

    player_mod.open = lambda *a, **k: Spy(real_open(*a, **k))
    try:
        p._scan_log_for_fatal()
    finally:
        del player_mod.open
    biggest = max(reads or [0])
    return bool(msgs) and biggest <= 16384, f"读入上限={biggest} 报警={msgs[:1]}"


def t_expand_dirs_cap() -> tuple[bool, str]:
    """目录遍历也要封顶：6 个空目录、上限 3，必须停下并记读数。"""
    root = os.path.join(TMP, "walk-root")
    for i in range(6):
        os.makedirs(os.path.join(root, f"d{i}", "sub"), exist_ok=True)
        write(os.path.join(root, f"d{i}", "sub", "note.txt"), b"not media")
    p = MpvPlayer(make_cfg("expand.json"))
    p.MAX_WALK_DIRS = 3
    out = p.expand(root)
    capped = p.last_expand_truncated and p.last_expand_dirs <= 3 and out == []

    small = os.path.join(TMP, "walk-small", "a")
    os.makedirs(small, exist_ok=True)
    write(os.path.join(small, "film.mkv"), b"x")
    out2 = p.expand(os.path.join(TMP, "walk-small"))
    small_ok = (not p.last_expand_truncated) and p.last_expand_dirs >= 2 and len(out2) == 1
    return capped and small_ok, (
        f"封顶={capped} dirs={p.last_expand_dirs} / 小树={small_ok} out={out2}")


def t_prune_guards() -> tuple[bool, str]:
    """坏 meta（JSON 是列表）与中途消失的文件：回收只该少回收，不该抛。"""
    tdir = os.path.join(TMP, "thumb-cache")
    os.makedirs(os.path.join(tdir, "a"), exist_ok=True)
    os.makedirs(os.path.join(tdir, "b"), exist_ok=True)
    with open(os.path.join(tdir, "a", "meta.json"), "w", encoding="utf-8") as fh:
        json.dump([], fh)                       # 合法 JSON，但不是字典
    with open(os.path.join(tdir, "b", "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"ts": 1.0}, fh)
    store = ThumbStore("ffmpeg", tdir, max_jobs=1)
    try:
        gone = store.prune(keep=1)
        thumb_ok = gone >= 1
    finally:
        store._pool.waitForDone(200)
        store.deleteLater()
        pump(20)

    pdir = os.path.join(TMP, "poster-cache")
    os.makedirs(pdir, exist_ok=True)
    write(os.path.join(pdir, "a.jpg"), b"x")
    write(os.path.join(pdir, "b.jpg"), b"x")
    pstore = PosterStore("ffmpeg", pdir, max_jobs=1)
    real_getmtime = os.path.getmtime
    os.path.getmtime = lambda path: (_ for _ in ()).throw(OSError("vanished"))
    try:
        pgone = pstore.prune(keep=0)
        poster_ok = pgone == 2
    finally:
        os.path.getmtime = real_getmtime
        pstore._pool.waitForDone(200)
        pstore.deleteLater()
        pump(20)
    return thumb_ok and poster_ok, f"缩略图删={gone} 封面删={pgone}"


def t_gif_cancel_before_run() -> tuple[bool, str]:
    """取消发生在进程起来之前：不许再起 ffmpeg，回报必须是"已取消"。"""
    out = os.path.join(TMP, "cancel.gif")
    params = {"exe": os.path.join(TMP, "no-ffmpeg.exe"), "video": "in.mp4",
              "start": 0.0, "dur": 1.0, "width": 320, "fps": 10, "out": out}
    job = GifJob(1, params)
    job.cancelled = True
    done: list[tuple] = []
    job.sig.done.connect(lambda jid, ok, detail, path: done.append((ok, detail, path)))
    job.run()
    ok = bool(done) and done[0][0] is False and done[0][1] == "已取消" and not os.path.exists(out)
    return ok, f"回报={done[:1]} 产物存在={os.path.exists(out)}"


# ============================================================ 界面
def t_title_clears() -> tuple[bool, str]:
    """列表清空后任务栏/标题栏不许留着上一支片名。"""
    win = MainWindow(make_cfg("title.json"), files=[])
    try:
        win.setWindowTitle("上一支片 - " + cfgmod.APP_TITLE)
        win._on_playlist([])
        return win.windowTitle() == cfgmod.APP_TITLE, f"标题={win.windowTitle()!r}"
    finally:
        win.close()
        win.deleteLater()
        pump(30)


def t_drop_osd_while_playing() -> tuple[bool, str]:
    """正在播（占位页隐藏）时，外部打不开的路径也得在 OSD 上点名。"""
    win = MainWindow(make_cfg("drop.json"), files=[])
    seen: list[str] = []
    try:
        win.show()
        pump(80)
        win.empty.hide()
        pump(20)
        win._osd = lambda text: seen.append(text)
        win.notify_external_drop([(r"C:\media\坏文件.mkv", "盘上没有这个文件")])
        notice = win.empty.notice.text()
        ok = bool(seen) and "坏文件.mkv" in seen[0] and "坏文件.mkv" in notice
        return ok, f"OSD={seen[:1]} 占位页={notice!r}"
    finally:
        win.close()
        win.deleteLater()
        pump(30)


def t_fullscreen_guard() -> tuple[bool, str]:
    """已最大化时调 set_fullscreen(False) 不许把它打回普通态。"""
    w = QWidget()
    w.resize(640, 400)
    w.showMaximized()
    pump(50)
    c = Chrome(w)
    c.set_fullscreen(False)
    pump(50)
    kept = w.isMaximized()
    w.showNormal()
    pump(30)
    c.set_fullscreen(True)
    pump(50)
    entered = w.isFullScreen()
    c.set_fullscreen(True)          # 重复进入也要留在全屏
    pump(30)
    again = w.isFullScreen()
    w.close()
    w.deleteLater()
    pump(20)
    return kept and entered and again, f"最大化保住={kept} 全屏={entered} 重复={again}"


def t_page_fade_once() -> tuple[bool, str]:
    """点当前页签不该重放淡入（内容根本没换）。"""
    win = MainWindow(make_cfg("fade.json"), files=[])
    try:
        calls: list[int] = []
        win.page_fade.run = lambda: calls.append(1)
        idx = win.sidebar_stack.currentIndex()
        win._sidebar_page(idx)
        pump(20)
        return calls == [], f"重放次数={len(calls)} idx={idx}"
    finally:
        win.close()
        win.deleteLater()
        pump(30)


def t_idle_disables_media_only() -> tuple[bool, str]:
    """空闲时倍速/音轨/字幕入口该灰掉；音量与视图开关仍可用。"""
    class P(QObject):
        sig_time = pyqtSignal(float)
        sig_duration = pyqtSignal(float)
        sig_buffered = pyqtSignal(float)
        sig_chapters = pyqtSignal(list)
        sig_pause = pyqtSignal(bool)
        sig_volume = pyqtSignal(int)
        sig_mute = pyqtSignal(bool)
        sig_speed = pyqtSignal(float)
        sig_info = pyqtSignal(dict)
        sig_idle = pyqtSignal(bool)

        def prop(self, name, default=None):
            return default

        def volume_max(self):
            return 200

        def ab_marks(self):
            return None, None

    bar = ControlsBar(P(), make_cfg("bar.json"))
    try:
        bar._on_idle(True)
        off = (not bar.btn_speed.isEnabled() and not bar.btn_sub.isEnabled()
               and not bar.btn_audio.isEnabled() and not bar.lbl_speed.isEnabled())
        stay = (bar.btn_volume.isEnabled() and bar.btn_loop.isEnabled()
                and bar.btn_shuffle.isEnabled() and bar.btn_more.isEnabled()
                and bar.btn_fs.isEnabled())
        bar._on_idle(False)
        back = bar.btn_speed.isEnabled() and bar.lbl_speed.isEnabled()
        return off and stay and back, f"灰掉={off} 保留={stay} 恢复={back}"
    finally:
        bar.close()
        bar.deleteLater()
        pump(20)


def t_noargs_focus_existing() -> tuple[bool, str]:
    """无参数启动：已有实例就转交并退出，不再建第二扇窗。"""
    calls: list[tuple] = []
    old_fwd, old_win = M.forward_to_running, M.MainWindow

    def fwd(paths, dropped=None, **kw):
        calls.append((list(paths), list(dropped or [])))
        return True

    class Boom(QWidget):
        def __init__(self, *a, **k):
            raise RuntimeError("无参数启动不该再建窗")

    M.forward_to_running, M.MainWindow = fwd, Boom
    try:
        rc = M.main([])
        return rc == 0 and calls == [([], [])], f"rc={rc} calls={calls}"
    finally:
        M.forward_to_running, M.MainWindow = old_fwd, old_win


def t_resume_each_track() -> tuple[bool, str]:
    """第一支以外的条目也要从各自上次的位置开始（start= 只挂在第一条命令上）。"""
    cfg = make_cfg("resume.json")
    cfg["resume"] = True
    p1 = write(os.path.join(TMP, "res", "one.mkv"), b"x")
    p2 = write(os.path.join(TMP, "res", "two.mkv"), b"x")
    cfg.remember_position(p1, 30.0, 100.0)
    cfg.remember_position(p2, 70.0, 200.0)
    cfg.save()
    p = MpvPlayer(cfg)
    fired: list[list] = []
    p._fire = lambda cmd: fired.append(cmd)
    p.open_files([p1, p2])
    skip_set = bool(getattr(p, "_resume_skip", ""))
    fired.clear()
    p._props["path"] = event_path = p2
    seeks: list[float] = []
    p.seek_abs = lambda pos: seeks.append(float(pos))
    p._on_file_loaded()
    second_ok = seeks == [70.0]
    # 第一条已经由 loadfile 的 start= 带过去了，file-loaded 回来不许再 seek 一次
    fired.clear()
    seeks.clear()
    p._props["path"] = p1
    p._on_file_loaded()
    first_ok = seeks == []
    return skip_set and second_ok and first_ok, (
        f"跳过记账={skip_set} 第二条seek={second_ok} 第一条不重复={first_ok} path={event_path}")


def t_faint_contrast() -> tuple[bool, str]:
    """弱化文字在窗口底/侧栏/悬停底/选中底上都要过 AA 4.5。"""
    def lum(h):
        h = h.lstrip("#")
        out = []
        for i in (0, 2, 4):
            c = int(h[i:i + 2], 16) / 255.0
            out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

    def ratio(fg, bg):
        a, b = lum(fg), lum(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    rows = [(theme.BG, 4.5), (theme.SIDEBAR_BG, 4.5), (theme.BG_HOVER, 4.5),
            (theme.BG_ACTIVE, 4.5), (theme.BG_RAISE, 4.5)]
    vals = [(ratio(theme.TEXT_FAINT, bg), need) for bg, need in rows]
    ok = all(v >= need for v, need in vals)
    return ok, " / ".join(f"{v:.2f}" for v, _ in vals) + f"（{theme.TEXT_FAINT}）"


def t_osd_elide() -> tuple[bool, str]:
    """长 OSD 中间省略，宽度不超上限；不许两头被裁。"""
    host = QWidget()
    host.resize(800, 500)
    host.show()
    pump(40)
    osd = Osd(host)
    text = "这是一条很长的提示：" + "很长的内容" * 20
    try:
        osd.show_text(text)
        pump(30)
        shown = osd._text
        fits = osd.fontMetrics().horizontalAdvance(shown) <= 560 - 48 + 2
        ok = shown != text and "…" in shown and fits
        return ok, f"显示={shown[-16:]!r} 宽={osd.fontMetrics().horizontalAdvance(shown)}"
    finally:
        osd.hide()
        osd.deleteLater()
        host.close()
        host.deleteLater()
        pump(20)


def t_accent_refresh() -> tuple[bool, str]:
    """换主色后：钩子被叫到、标题 logo / 倍速胶囊 / 选中图标当场换色。"""
    old_accent = theme.ACCENT
    hits: list[int] = []
    theme.on_accent(lambda: hits.append(1))
    tb = TitleBar()
    class P(QObject):
        sig_time = pyqtSignal(float)
        sig_duration = pyqtSignal(float)
        sig_buffered = pyqtSignal(float)
        sig_chapters = pyqtSignal(list)
        sig_pause = pyqtSignal(bool)
        sig_volume = pyqtSignal(int)
        sig_mute = pyqtSignal(bool)
        sig_speed = pyqtSignal(float)
        sig_info = pyqtSignal(dict)
        sig_idle = pyqtSignal(bool)

        def prop(self, name, default=None):
            return default

        def volume_max(self):
            return 200

        def ab_marks(self):
            return None, None

    bar = ControlsBar(P(), make_cfg("accent.json"))
    try:
        bar._on_speed(1.5)
        bar.btn_loop.setChecked(True)
        pump(20)
        before_logo = tb.logo.pixmap().cacheKey()
        before_icon = bar.btn_loop.icon().cacheKey()
        before_chip = bar.lbl_speed.styleSheet()
        theme.set_accent("#5B8FF9")
        theme.notify_accent()
        pump(20)
        after_logo = tb.logo.pixmap().cacheKey()
        after_icon = bar.btn_loop.icon().cacheKey()
        after_chip = bar.lbl_speed.styleSheet()
        ok = (hits and before_logo != after_logo and before_icon != after_icon
              and theme.ACCENT_TEXT in after_chip and before_chip != after_chip)
        return ok, (f"钩子={len(hits)} logo={before_logo != after_logo} "
                    f"图标={before_icon != after_icon} 胶囊={theme.ACCENT_TEXT in after_chip}")
    finally:
        theme.set_accent(old_accent)
        theme.notify_accent()
        tb.deleteLater()
        bar.close()
        bar.deleteLater()
        pump(20)


def t_pressed_states() -> tuple[bool, str]:
    """按下档必须存在且和悬停档不同（滑轨/窗钮/幽灵按钮）。"""
    sheet = theme.set_accent(theme.ACCENT_DEFAULT)
    need = {
        "滑轨按下": f"QSlider::handle:horizontal:pressed {{ background: {theme.PRIMARY_FILL_DOWN}; }}",
        "窗钮按下": "#CaptionBtn:pressed",
        "关闭钮按下": "#CloseBtn:pressed",
        "幽灵按钮按下": "QPushButton#Ghost:pressed",
    }
    missing = [k for k, s in need.items() if s not in sheet]
    return not missing, "缺：" + "、".join(missing) if missing else "四档都在"


def t_icon_dpr() -> tuple[bool, str]:
    """图标按真实屏幕 DPR 渲染，并进缓存键。"""
    original = icons._screen_dpr
    try:
        icons._screen_dpr = lambda: 1.25
        pm = icons.pixmap("play", 20, "#FFFFFF")
        pm_ok = abs(pm.devicePixelRatio() - 1.25) < 1e-6
        icons.icon("play", 20, "#FFFFFF")
        keys = [k for k in icons._ICON_CACHE if k and k[0] == "play"]
        key_ok = any(len(k) == 5 and abs(float(k[4]) - 1.25) < 1e-6 for k in keys)
        icons._screen_dpr = lambda: 2.0
        icons.icon("play", 20, "#FFFFFF")
        keys2 = [k for k in icons._ICON_CACHE if k and k[0] == "play"]
        split_ok = any(len(k) == 5 and abs(float(k[4]) - 2.0) < 1e-6 for k in keys2)
        return pm_ok and key_ok and split_ok, (
            f"位图dpr={pm.devicePixelRatio()} 键含dpr={key_ok} 两档分开={split_ok}")
    finally:
        icons._screen_dpr = original


def main() -> int:
    theme.install(APP, theme.ACCENT_DEFAULT)
    print("== 引擎健壮性 ==", flush=True)
    check("config：写盘失败不抛且内存进度可见", t_config_save_failure)
    check("config：坏 watch-state 条目自愈", t_corrupt_watch_state)
    check("ipc：线程死亡唤醒全部在途请求", t_ipc_fail_pending)
    check("watchdog：久跑重置额度 + 重启前关旧 IPC", t_watchdog_restart)
    check("日志：只读尾部 8KB", t_log_tail)
    check("展开：目录遍历封顶 + 读数", t_expand_dirs_cap)
    check("回收：坏 meta / 文件消失不炸", t_prune_guards)
    check("GIF：未起进程前取消不导出", t_gif_cancel_before_run)

    print("== 界面 / 日常 ==", flush=True)
    check("标题：列表清空后回正", t_title_clears)
    check("外部拖入：播放中也有 OSD 点名", t_drop_osd_while_playing)
    check("全屏：状态相同不动窗口", t_fullscreen_guard)
    check("页签：重复点当前页不重放淡入", t_page_fade_once)
    check("空闲：只灰掉媒体相关入口", t_idle_disables_media_only)
    check("单实例：无参数启动叫回已有窗口", t_noargs_focus_existing)
    check("续播：每条目各回各的位置", t_resume_each_track)
    check("对比度：弱化文字过 AA", t_faint_contrast)
    check("OSD：长文本中间省略", t_osd_elide)
    check("主色：logo/图标/胶囊热刷新", t_accent_refresh)
    check("QSS：滑轨/窗钮/幽灵按钮有按下档", t_pressed_states)
    check("图标：DPR 跟随屏幕并进缓存键", t_icon_dpr)

    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} PASS", flush=True)
    if bad:
        print("FAIL：", "；".join(r[0] for r in bad), flush=True)
        return 1
    print("[FINAL] R25 离线判据全绿（不冒充真机：未启动 mpv、未占前台）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
