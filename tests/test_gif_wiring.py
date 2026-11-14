"""主窗口那一层的 GIF/连拍接线验收：不起内核、不起 ffmpeg、不弹真对话框、不抓屏。

用法：
    python tests/test_gif_wiring.py            # 正常验收：全部判据必须 PASS
    python tests/test_gif_wiring.py --falsify  # 证伪：把实现换成「常见错误写法」，对应判据必须翻红

三份 GIF 测试各管一段，互不重复：
  · tests/test_gif.py —— 引擎算出来的 ffmpeg 命令与产物（真跑 ffmpeg，量帧数与逐帧亮度）；
  · tests/test_gif_dialog.py —— 对话框怎么把界面上的数收口（假 state_of 字典）；
  · 这一份 —— 中间那段**线**：内核订阅缓存 → gif_state() → 对话框 → spec → GifStore.start，
    加上回流的两条（引擎进度/结果 → 对话框；「打开目录」→ shell）和连拍那只计时器。

为什么这一层值得单开一份：它上面全是「两边各自都对、接错线还是坏」的东西。
参数对调（width↔fps）、产物目录拿错（DATA_DIR 顶了 screenshot_dir）、开不成的原因只写进
引擎日志不回灌界面、连拍再按一次变成「再起一轮」、`_open_gif()` 第二次调用把信号叠成两份
——这些既不在引擎的命令里，也不在对话框的控件里，只在**连接**里。连接看不见，只能把两侧的
调用记录抓下来比。

三处纪律在这里落地：
1. **窗口不 show，且用 offscreen 平台**。不在用户桌面上长出窗口，屏幕休眠时也照跑
   （真机像素级验收归 tests/test_ui.py 与 smoke_launch.py，只有那两份才有 ENV 的问题）。
2. **替身只替「线的那一头」**。GifStore 与 GifDialog 换成记参数的假对象，但 MainWindow 是真的、
   信号是真的、QTimer 是真的、`_osd` 走的也是 `Osd.show_text` 那一条（只在入口记一笔，不弹气泡）。
   替身测试最大的骗法是「假对象和真对象接口不一样，于是测的根本不是产品」，所以有
   c_dialog_surface 专门核对真 GifDialog 那张接口表——任何一边改名都会红。
3. **槽里不许抛**。这条不是洁癖：`GifDialog._persist` 里一个 KeyError 就曾经让整个播放器
   静默没了（rc=127、stderr 全空）。凡被信号直调的入口（gif_state / _gif_export /
   start_burst / _reveal_path）都各有一条「喂垃圾输入」的判据。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="gelu-gifwire-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")     # 必须在 import config 之前
os.environ["QT_QPA_PLATFORM"] = "offscreen"             # 不在用户桌面长窗口，也不依赖屏幕醒着
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QObject, pyqtSignal            # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget       # noqa: E402

from app import config as cfgmod                         # noqa: E402
from app.engine import gif as G                          # noqa: E402
from app.engine.player import MpvPlayer                  # noqa: E402
from app.ui import keymap as KM                          # noqa: E402
from app.ui import main_window as MW                     # noqa: E402
from app.ui.gif_dialog import GifDialog as REAL_DIALOG   # noqa: E402  接口对账用：产品类本体
from app.ui.main_window import MainWindow                # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
SUB_FILE = os.path.join(TMP, "zh.srt")
with open(SUB_FILE, "w", encoding="utf-8") as _fh:
    _fh.write("1\n00:00:12,000 --> 00:00:13,500\n喂\n\n")
VIDEO = os.path.join(TMP, "大侦探 01.mp4")
with open(VIDEO, "wb") as _fh:
    _fh.write(b"\0" * 16)

# _open_gif() 用到的那张接口表（替身与真对话框必须同名）
SURFACE = ("sig_export", "sig_cancel", "sig_burst", "sig_reveal", "refresh",
           "on_progress", "on_done", "install_owner_keys")


def pump(seconds: float, pred=None) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds and (pred is None or not pred()):
        APP.processEvents()
        time.sleep(0.01)
    APP.processEvents()
    return bool(pred is None or pred())


# ============================================================ 替身
class FakePlayer(MpvPlayer):
    """真 QObject、真信号、真 prop() 语义；就是不建进程、不发 IPC。

    继承而不是另写一个类：这样各面板构造时对 player 提的那一堆要求（十几个信号、一堆方法）
    自动满足，替身只负责三件事——缓存里放什么由测试说了算、snapshot 计数、以及把**任何一次
    同步读写**记下来（本仓纪律：界面刷自己只用订阅缓存，理由见 app/ui/main_window.py 顶部）。
    """

    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)
        self.running_flag = True
        self.shots: list[bool] = []          # 每次 snapshot 的 include_sub 旗子，逐次记
        self.sync_calls: list[str] = []      # 任何一次同步读（get / _req / stats_snapshot）
        self.fire_calls: list[str] = []      # 任何一次写命令
        self.quits = 0

    @property
    def running(self) -> bool:
        return self.running_flag

    def start(self, hwnd: int, wait: float = 10.0) -> bool:
        return True

    def quit(self) -> None:
        self.quits += 1

    def snapshot(self, include_sub: bool = True) -> None:
        self.shots.append(bool(include_sub))

    def get(self, name: str, default=None):
        self.sync_calls.append("get:" + str(name))
        return default

    def _req(self, command, timeout: float = 2.5):
        self.sync_calls.append("req:" + str(command[:2]))
        return None

    def stats_snapshot(self) -> dict:
        self.sync_calls.append("stats")
        return {}

    def _fire(self, command) -> None:
        self.fire_calls.append(str(command[:2]))


class FakeGif(QObject):
    """GifStore 的替身：把 start() 收到的每个参数原样记下，回话由测试摆。"""

    sig_progress = pyqtSignal(int, float, float)
    sig_done = pyqtSignal(int, bool, str, str)

    def __init__(self, exe: str, parent=None):
        super().__init__(parent)
        self.exe = exe
        self.calls: list[dict] = []
        self.ret: tuple[int, str] = (7, "")      # (job id, 失败原因)：签名与真 store 一致
        self.cancels = 0

    def busy(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancels += 1

    def start(self, video, start, dur, out_dir, width=G.DEFAULT_WIDTH,
              fps=G.DEFAULT_FPS, sub_file="", sub_delta=True):
        self.calls.append({"video": video, "start": start, "dur": dur, "out_dir": out_dir,
                           "width": width, "fps": fps, "sub_file": sub_file,
                           "sub_delta": sub_delta})
        return self.ret


class RecorderDialog(QWidget):
    """GifDialog 的替身：只证明「被接的那一头」通了几次；界面本身由它自己的测试管。"""

    sig_export = pyqtSignal(dict)
    sig_cancel = pyqtSignal()
    sig_burst = pyqtSignal(int, int)
    sig_reveal = pyqtSignal(str)

    def __init__(self, cfg, state_of, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.state_of = state_of
        self.refreshes = 0
        self.progress: list[tuple] = []
        self.dones: list[tuple] = []
        self.mirrored: list[tuple] = []        # 每次 install_owner_keys 收到的 (rows, sink)

    def refresh(self) -> None:
        self.refreshes += 1

    def install_owner_keys(self, rows, sink) -> int:
        """真对话框靠这个拿到主窗键位的镜像；替身只记下"递进来的是什么"。"""
        self.mirrored.append((rows, sink))
        return len(KM.mirrorable(rows))

    def on_progress(self, jid, sec, total) -> None:
        self.progress.append((jid, sec, total))

    def on_done(self, jid, ok, detail, path) -> None:
        self.dones.append((jid, ok, detail, path))


MW.MpvPlayer = FakePlayer
MW.GifStore = FakeGif
MW.GifDialog = RecorderDialog


def fresh_cfg(raw: dict | None = None) -> cfgmod.Config:
    p = os.path.join(TMP, "cfg", "config.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.isfile(p):
        os.remove(p)
    c = cfgmod.Config(p)
    c["screenshot_dir"] = os.path.join(TMP, "shots")
    for k, v in (raw or {}).items():
        c[k] = v
    c.save()
    cfgmod.ensure_dirs(c)
    return c


def build(raw: dict | None = None, state: dict | None = None,
          running: bool = True) -> tuple[MainWindow, FakePlayer, FakeGif]:
    """造一个「没 show 的主窗口」+ 填满订阅缓存的假内核 + 记参数的假导出门面。"""
    cfg = fresh_cfg(raw)
    win = MainWindow(cfg, files=[])
    win.player.running_flag = running
    win.player._props.update(state or {})
    osd: list[str] = []
    win.osd.show_text = lambda text, ms=1000: osd.append(str(text))
    win.osd_text = osd                                  # 判据从这里读「用户看到的那句话」
    return win, win.player, win.gif


def filled(win: MainWindow) -> FakePlayer:
    """一份「像在放片子」的缓存：路径、位置、时长、A-B、一条选中的外挂字幕。"""
    p = win.player
    p._props.update({"path": VIDEO, "media-title": "不该被用到的名字", "time-pos": 42.5,
                     "duration": 300.0, "ab-loop-a": 10.0, "ab-loop-b": 20.0,
                     "track-list": [{"type": "sub", "selected": True, "external": True,
                                     "external-filename": SUB_FILE}]})
    return p


SPEC = {"video": VIDEO, "start": 12.5, "dur": 4.0, "width": 640, "fps": 15, "sub": SUB_FILE}


# ============================================================ 判据
def c_state_from_cache() -> tuple[bool, str]:
    """gif_state() 的五个数全来自订阅缓存，且一个同步请求都不发。

    video 必须是 path 不是 media-title：ffmpeg 要的是磁盘上那个文件，而 media-title 对流、
    对某些容器是个盘上根本不存在的名字。注释里写明的理由，这条判据把它钉住。
    """
    win, p, _g = build()
    filled(win)
    st = win.gif_state()
    ok = (st["video"] == VIDEO and st["pos"] == 42.5 and st["duration"] == 300.0
          and tuple(st["ab"] or ()) == (10.0, 20.0) and st["sub"] == SUB_FILE
          and p.sync_calls == [] and p.fire_calls == [])
    return ok, (f"video={st['video']!r} pos={st['pos']} dur={st['duration']} ab={st['ab']} "
               f"sub={st['sub']!r} 同步请求={p.sync_calls} 写命令={p.fire_calls}")


def c_state_empty_cache() -> tuple[bool, str]:
    """内核没起来（缓存空着）时：空串/0/None/空字幕，一个都不许抛、也不许猜出一个值。"""
    win, _p, _g = build()
    try:
        st = win.gif_state()
    except BaseException as exc:                  # noqa: BLE001 - 这里要的就是「不抛」
        return False, f"缓存空着就抛 {type(exc).__name__}: {exc}"
    ok = (st["video"] == "" and st["pos"] == 0.0 and st["duration"] == 0.0
          and st["ab"] is None and st["sub"] == "")
    return ok, (f"video={st['video']!r} pos={st['pos']} dur={st['duration']} "
                f"ab={st['ab']} sub={st['sub']!r}")


def c_state_junk_cache() -> tuple[bool, str]:
    """缓存里躺着字符串/None/垃圾 track-list 时也必须回一份能用的数。

    这不是假想的脏：内核在没有媒体时把 time-pos 推成字符串 "no"（engine/player.py 里为它
    专门写过入缓存归一化），归一化管不到的那一类一旦漏到这里，抛出发生在 Qt 槽里——表现不是
    traceback，是进程当场没了（rc=127、stderr 全空）。
    """
    win, _p, _g = build()
    win.player._props.update({"path": None, "time-pos": "no", "duration": "abc",
                              "ab-loop-a": None, "ab-loop-b": "x", "track-list": "垃圾"})
    try:
        st = win.gif_state()
    except BaseException as exc:                  # noqa: BLE001
        return False, f"垃圾缓存抛 {type(exc).__name__}: {exc}"
    ok = (st["video"] == "" and st["pos"] == 0.0 and st["duration"] == 0.0
          and st["ab"] is None and st["sub"] == ""
          and isinstance(st["pos"], float) and isinstance(st["duration"], float))
    return ok, f"兜住以后={st}"


def c_open_gif_wires() -> tuple[bool, str]:
    """打开工坊：四条去、两条回各通一次，实例复用，第二次打开不把连接叠成两份。

    「叠成两份」用计数抓：第二次 _open_gif() 之后再发一次进度/结果信号，两边的记录各只该
    长一条。叠了的话真对话框上就是「进度条按两倍的量走、上一条结果被同一条消息写两遍」。
    """
    win, p, g = build()
    filled(win)
    win._open_gif()
    d = win.gif_dlg
    g.sig_progress.emit(1, 2.0, 4.0)
    g.sig_done.emit(1, True, "48 帧 / 480x270", os.path.join(TMP, "shots", "a.gif"))
    d.sig_export.emit(dict(SPEC))
    d.sig_cancel.emit()
    d.sig_reveal.emit(os.path.join(TMP, "shots", "a.gif"))
    d.sig_burst.emit(5, 120)
    snap = (d.refreshes, len(d.progress), len(d.dones), len(g.calls), g.cancels,
            len(d.state_of()), len(p.shots), win._burst_left, win._burst_timer.interval())
    first = (isinstance(d, RecorderDialog)
             and snap == (1, 1, 1, 1, 1, 5, 1, 4, 120))
    win.start_burst()                                    # 收掉刚起的那轮连拍
    win._open_gif()
    g.sig_progress.emit(2, 1.0, 4.0)
    g.sig_done.emit(2, True, "12 帧", os.path.join(TMP, "shots", "b.gif"))
    again = (d.refreshes, len(d.progress), len(d.dones))
    no_dup = (win.gif_dlg is d and again == (2, 2, 2))
    return bool(first and no_dup), (
        f"首轮 (刷新,进度,结果,导出,取消,状态键,截帧,剩余,间隔)={snap}（该 (1,1,1,1,1,5,1,4,120)）"
        f"；二次 复用={win.gif_dlg is d} (刷新,进度,结果)={again}（该 (2,2,2)，多出来的就是叠上去的连接）")


def c_open_gif_mirrors() -> tuple[bool, str]:
    """工坊一开就把**生效表**递给它的镜像，并把 `self.dispatch` 当作落点。

    真机实测的形状（tests/probe_gif3.py B1–B4）：工坊成为活动窗以后，主窗那 60 多条
    `QShortcut(context=WindowShortcut)` 整体收不到键——按 B 命中的动作是 `[]`，把主窗抢回
    活动才又响。修法要求"两份挂载、一份事实"：镜像用的行由主窗递（递进去的是
    `keymap.mirrorable()` 筛过的那一份），命中后交回主窗的 `dispatch`。

    所以这条只判**递的是什么**，四种常见的坏写法都能在这里翻红：
      · 不递 → 工坊上根本没有镜像；
      · 递出厂默认表而不是 `self.key_rows` → 用户改过的键位在工坊里又变回出厂那套（两份事实）；
      · 只在第一次打开时递 → 工坊开着时改完键位再打开，用的还是旧表；
      · 落点接成一个空壳/别的对象 → 键响了但没人办事。
    "工坊是活动窗时物理按键真的走这里"由 tests/test_gif_real.py 的 B 段在真窗口上验，
    这份测试替身没有真 QShortcut，测不到 Qt 的窗口激活匹配。
    """
    win, p, _g = build({"keymap": {"off": ["Space"], "on": [["Ctrl+J", "mute"]]}})
    filled(win)
    win._open_gif()
    d = win.gif_dlg
    if not d.mirrored:
        return False, "一次都没递过镜像：工坊上没有任何键位挂载（症状＝根本没修）"
    rows, sink = d.mirrored[-1]
    seqs = [str(r[0]) for r in rows]
    one_call = len(d.mirrored) == 1
    same_table = rows is win.key_rows and list(rows) == list(win.key_rows)
    user_rows = "Ctrl+J" in seqs and "Space" not in seqs
    is_dispatch = (getattr(sink, "__self__", None) is win
                   and getattr(sink, "__func__", None) is MainWindow.dispatch)
    fired_before = len(p.fire_calls)
    sink("back5")                                   # 工坊那边命中以后走的这条路
    routed = len(p.fire_calls) > fired_before and any("-5 秒" in t for t in win.osd_text)
    win._open_gif()                                 # 第二次打开：再递一遍
    twice = len(d.mirrored) == 2
    win.cfg["keymap"] = {"off": ["Space"], "on": [["Ctrl+J", "mute"], ["F7", "loop"]]}
    win.cfg.save()
    win._install_shortcuts()                        # 工坊开着时改了键位：重装的顺带就该重递
    seqs3 = [str(r[0]) for r in d.mirrored[-1][0]]
    on_reinstall = (len(d.mirrored) == 3 and d.mirrored[-1][0] is win.key_rows
                    and "F7" in seqs3 and "Space" not in seqs3)
    win._open_gif()
    seqs4 = [str(r[0]) for r in d.mirrored[-1][0]]
    late = len(d.mirrored) == 4 and d.mirrored[-1][0] is win.key_rows and "F7" in seqs4
    noise = ""
    try:
        win.gif_dlg = None
        win._install_shortcuts()                    # 工坊没开也不能炸（主窗自己那张表还得装上）
        win._install_gif_shortcuts()
    except BaseException as exc:                    # noqa: BLE001
        noise = f"{type(exc).__name__}: {exc}"
    bad = [n for n, ok in (("打开一次只递一次", one_call), ("递的就是生效表那一个对象", same_table),
                           ("用户改过的键位在里面", user_rows), ("落点是主窗的 dispatch", is_dispatch),
                           ("交回 dispatch 真发了命令", routed), ("二次打开重递一遍", twice),
                           ("改完键位重装时立刻换新表", on_reinstall),
                           ("改完键位再打开也用新表", late), ("工坊没开时不抛", not noise)) if not ok]
    return not bad, (f"没做到的={bad} 递过 {len(d.mirrored)} 次 首次 {len(rows)} 条 "
                     f"含 Ctrl+J={user_rows} 落点={getattr(sink, '__qualname__', sink)} "
                     f"写命令={p.fire_calls} OSD={win.osd_text[-1:]} 抛出的={noise or '无'}")


def c_dialog_surface() -> tuple[bool, str]:
    """真 GifDialog 必须有 _open_gif() 用到的那七个名字。

    上面那条判据跑在替身上——替身测试最容易骗人的地方就是「假对象接口和真对象不一样」：
    真对话框改名以后接线早就断了，这份测试还能全绿。于是把两边同名的接口摊开来比。
    """
    missing = [n for n in SURFACE if not hasattr(REAL_DIALOG, n)]
    mine = [n for n in SURFACE if not hasattr(RecorderDialog, n)]
    return (not missing and not mine), f"真类缺={missing} 替身缺={mine}"


def c_export_passes_args() -> tuple[bool, str]:
    """spec 的六个字段按原位交给 GifStore.start，产物目录用的是配置里那个 screenshot_dir。

    width/fps 对调、或目录拿成 DATA_DIR，都是「看起来完全正常」的坏——只有把调用记下来才看得见。
    """
    win, _p, g = build({"screenshot_dir": os.path.join(TMP, "shotsA")})
    win._gif_export(dict(SPEC))
    c = g.calls[0] if g.calls else {}
    want_dir = str(win.cfg["screenshot_dir"])
    ok = (len(g.calls) == 1 and c.get("video") == SPEC["video"]
          and c.get("start") == SPEC["start"] and c.get("dur") == SPEC["dur"]
          and c.get("out_dir") == want_dir and c.get("width") == SPEC["width"]
          and c.get("fps") == SPEC["fps"] and c.get("sub_file") == SPEC["sub"])
    return ok, f"收到={c} 期望目录={want_dir}"


def c_export_junk_spec() -> tuple[bool, str]:
    """spec 缺键/带脏值时不许抛（槽里抛＝进程静默没了），脏值折成合法类型再开。

    缺 video 的正确处置是「照样开一趟、让引擎报视频文件不存在」，而不是拿 None 去拼命令。
    """
    win, _p, g = build()
    before = len(g.calls)
    try:
        win._gif_export({})
        win._gif_export({"video": None, "start": "abc", "dur": None, "width": "x",
                         "fps": None, "sub": 5})
    except BaseException as exc:                  # noqa: BLE001
        return False, f"垃圾 spec 抛 {type(exc).__name__}: {exc}"
    called = g.calls[before:]
    ok = (len(called) == 2 and called[0]["video"] == ""
          and called[0]["out_dir"] == str(win.cfg["screenshot_dir"])
          and isinstance(called[1]["start"], float) and isinstance(called[1]["dur"], float)
          and isinstance(called[1]["width"], int) and isinstance(called[1]["fps"], int)
          and isinstance(called[1]["sub_file"], str))
    return ok, f"两次调用={called}"


def c_export_error_reported() -> tuple[bool, str]:
    """引擎回「开不成」时，那句原因必须回灌成一条失败结果，而不是只留在引擎日志里。

    判据看的是对话框收到 (id, False, 那句原因, "")：没这条的话用户按下导出、按钮灰掉、
    进度条停在 0，然后什么都没有——比报错坏得多。
    """
    win, _p, g = build()
    win._open_gif()
    g.ret = (0, "没找到 ffmpeg：" + os.path.join(TMP, "nada.exe"))
    win._gif_export(dict(SPEC))
    got = win.gif_dlg.dones[-1] if win.gif_dlg.dones else None
    ok = (got is not None and got[1] is False and "ffmpeg" in str(got[2]) and got[3] == "")
    return ok, f"对话框收到={got}"


def c_export_no_fake_failure() -> tuple[bool, str]:
    """开成了（err 为空）就不要发一条假失败：对话框会把「导出失败：」写在结果行上。"""
    win, _p, g = build()
    win._open_gif()
    g.ret = (9, "")
    win._gif_export(dict(SPEC))
    return (win.gif_dlg.dones == []), f"意外收到的结果={win.gif_dlg.dones}"


def c_kernel_only_says() -> tuple[bool, str]:
    """内核没起来时按这两条动作：必须回一句话，而且既不建对话框也不截帧。

    这条是 KERNEL_ONLY 那张表的存在理由：按空格没反应是正常预期，但这两条按下去看不出
    「键坏了」还是「没起内核」，所以它们和纯传送类分开列。
    """
    win, p, g = build(running=False)
    win.dispatch("export_gif")
    win.dispatch("burst_shot")
    ok = (win.gif_dlg is None and p.shots == [] and g.calls == []
          and any("内核" in t for t in win.osd_text))
    return ok, f"对话框={win.gif_dlg} 截帧={p.shots} 导出={g.calls} OSD={win.osd_text}"


def c_kernel_running_works() -> tuple[bool, str]:
    """内核在跑时同一组动作必须真做得成（dispatch 里少了分支就是按了没反应）。"""
    win, p, _g = build(running=True)
    win.dispatch("export_gif")
    win.dispatch("burst_shot")
    ok = (win.gif_dlg is not None and len(p.shots) == 1 and win._burst_left >= 1)
    return ok, (f"对话框={type(win.gif_dlg).__name__} 截帧={p.shots} "
                f"剩余={win._burst_left} OSD={win.osd_text}")


def c_burst_clamps() -> tuple[bool, str]:
    """张数夹 2–60、间隔夹 50–3000：上限保磁盘，下限保「这不叫连拍」。

    间隔不夹下限的话，用户手滑填 1 毫秒就是每 tick 一发 screenshot，内核和磁盘一起遭殃；
    不夹上限则是 200 张连拍跑到一半没人能停。
    """
    win, p, _g = build()
    win.start_burst(200, 99999)
    hi_vals = (win._burst_left, win._burst_timer.interval(), len(p.shots))
    hi = hi_vals == (59, 3000, 1)
    win.start_burst()                                    # 取消
    win.start_burst(1, 1)
    lo_vals = (win._burst_left, win._burst_timer.interval())
    lo = lo_vals == (1, 50)
    win.start_burst()
    ok = hi and lo and len(p.shots) == 2
    return ok, (f"上限那轮（要 200 张/99999 毫秒）→ 剩余={hi_vals[0]}（该 59：夹到 60 后已截 1 张）"
                f" 间隔={hi_vals[1]} 截帧={hi_vals[2]}；下限那轮（要 1 张/1 毫秒）→ "
                f"剩余={lo_vals[0]}（该 1：夹到 2 后已截 1 张） 间隔={lo_vals[1]}；"
                f"两轮合计截帧={len(p.shots)}")


def c_burst_repress_cancels() -> tuple[bool, str]:
    """再按一次 B 是「取消」，不是「再起一轮」——两轮叠在同一条时间轴上会截出双份乱序图。"""
    win, p, _g = build()
    win.start_burst(5, 60)
    n = len(p.shots)
    win.start_burst()
    after = len(p.shots)
    pump(1.2)
    stopped = (win._burst_left == 0 and not win._burst_timer.isActive()
               and len(p.shots) == after and any("取消" in t for t in win.osd_text))
    return bool(stopped and n >= 1), (f"取消前 {n} 张 → 取消那一下 {after} 张 → 再等 1.2 秒 "
                                      f"{len(p.shots)} 张 计时器在跑={win._burst_timer.isActive()} "
                                      f"OSD={win.osd_text[-2:]}")


def c_burst_completes() -> tuple[bool, str]:
    """一轮跑完：张数不多不少、末张报完成、表停下来（还挂着单发以外模式就是等着多截一张）。"""
    win, p, _g = build({"burst_count": 4, "burst_gap_ms": 60})
    win.start_burst()                                    # 不传参：全走配置
    done = pump(6.0, lambda: win._burst_left == 0)
    during = len(p.shots)
    pump(0.6)
    ok = (done and during == 4 and len(p.shots) == 4 and not win._burst_timer.isActive()
          and win._burst_timer.isSingleShot() and any("连拍完成" in t for t in win.osd_text))
    return ok, (f"跑完={done} 当时 {during} 张 / 又等 0.6 秒后 {len(p.shots)} 张 "
                f"计时器在跑={win._burst_timer.isActive()} 单发={win._burst_timer.isSingleShot()} "
                f"OSD={win.osd_text}")


def c_burst_sub_flag() -> tuple[bool, str]:
    """每张截图带不带字幕，跟着配置里那一个旗子走（硬编码 True 就不给关的机会）。"""
    a, pa, _g = build({"screenshot_include_sub": True})
    a.start_burst(3, 60)
    pump(6.0, lambda: a._burst_left == 0)
    ta = list(pa.shots)
    b, pb, _gb = build({"screenshot_include_sub": False})
    b.start_burst(3, 60)
    pump(6.0, lambda: b._burst_left == 0)
    tb = list(pb.shots)
    ok = (ta == [True, True, True] and tb == [False, False, False])
    return ok, f"旗子 True={ta} False={tb}"


def c_burst_junk_cfg() -> tuple[bool, str]:
    """连拍的张数/间隔有三条来源：对话框传参、配置里的键、两边都烂。三条都不许抛。

    config.json 是用户可以拿记事本改的，`"burst_count": "很多"` 是一份合法的 JSON；
    而 start_burst 坐在快捷键的 Qt 槽里 —— int() 在那里抛出的表现是「按一下 B 播放器闪退」，
    不是 traceback。浮点入参（滑块/未来别的调用方）也要折成整数张数，别把 3.7 传进 range()。
    """
    win, p, _g = build({"burst_count": 6, "burst_gap_ms": None})
    try:
        win.start_burst(3.7, "快点")              # 入参：浮点 + 字符串（配置里的 6 不该被用到）
    except BaseException as exc:                  # noqa: BLE001
        return False, f"垃圾入参抛 {type(exc).__name__}: {exc}"
    arg_leg = (win._burst_left, win._burst_timer.interval())
    hi_leg = arg_leg == (3, 250)
    win.start_burst()

    win2, _p2, _g2 = build({"burst_count": "很多", "burst_gap_ms": "慢点"})
    try:
        win2.start_burst()                        # 入参缺省 + 配置两个键都是字符串 → 回出厂值
    except BaseException as exc:                  # noqa: BLE001
        return False, f"垃圾配置抛 {type(exc).__name__}: {exc}"
    cfg_leg = (win2._burst_left, win2._burst_timer.interval())
    ok_cfg = cfg_leg == (7, 250)
    win2.start_burst()

    win3, _p3, _g3 = build({"burst_count": 12, "burst_gap_ms": 100})
    try:
        win3.start_burst()                        # 正常配置照走：12 张 / 100 毫秒
    except BaseException as exc:                  # noqa: BLE001
        return False, f"正常配置抛 {type(exc).__name__}: {exc}"
    plain_leg = (win3._burst_left, win3._burst_timer.interval())
    return bool(hi_leg and ok_cfg and plain_leg == (11, 100)), (
        f"入参 3.7/快点 → (剩余,间隔)={arg_leg}（该 (3,250)：折成 4 张已截 1 张，间隔折回出厂 250）；"
        f"配置 很多/慢点 → {cfg_leg}（该 (7,250)：出厂 8 张已截 1 张）；"
        f"配置 12/100 → {plain_leg}（该 (11,100)）")


def c_reveal_uses_dir() -> tuple[bool, str]:
    """「打开目录」递给 shell 的是产物所在的**目录**，不是那个 .gif 文件本身。

    顺带把 None 和空串喂一遍：这一条也坐在槽里（对话框的 sig_reveal 直连它）。
    """
    d = os.path.join(TMP, "shotsB")
    os.makedirs(d, exist_ok=True)
    win, _p, _g = build({"screenshot_dir": d})
    seen: list[str] = []
    real = os.startfile
    os.startfile = lambda x: seen.append(str(x))
    try:
        win._reveal_path(os.path.join(d, "clip.gif"))
        win._reveal_path(None)
        win._reveal_path("")
        bad_osd = list(win.osd_text)
    finally:
        os.startfile = real
    ok = (seen == [d] and len(bad_osd) == 2 and all("已经不在了" in t for t in bad_osd))
    return ok, f"递给 shell 的={seen} OSD={bad_osd}"


def c_reveal_failure_branches() -> tuple[bool, str]:
    """目录已经不在了／shell 拒开，两条各有自己的话，而且都不许把异常穿出槽外。"""
    d = os.path.join(TMP, "shotsC")
    os.makedirs(d, exist_ok=True)
    win, _p, _g = build()
    win._reveal_path(os.path.join(TMP, "gone", "clip.gif"))
    miss = any("已经不在了" in t for t in win.osd_text)

    def boom(_x):
        raise OSError("拒绝访问")

    real = os.startfile
    os.startfile = boom
    try:
        try:
            win._reveal_path(os.path.join(d, "clip.gif"))
        except BaseException as exc:              # noqa: BLE001 - shell 抛也不该穿出去
            return False, f"startfile 抛出没被兜住：{type(exc).__name__}: {exc}"
    finally:
        os.startfile = real
    hit = win.osd_text[-1] if win.osd_text else ""
    return bool(miss and hit.startswith("打不开目录：") and "拒绝访问" in hit), \
        f"目录不在={miss} 拒开时说的话={hit!r} 全部 OSD={win.osd_text}"


CHECKS = [
    ("gif_state 五数全取订阅缓存，一个同步请求都不发，video 用 path 不用 media-title",
     c_state_from_cache),
    ("内核没起来（缓存空着）：回空串/0/None，不抛也不猜一个值出来", c_state_empty_cache),
    ("缓存里躺着字符串/None/垃圾 track-list 时照样回一份能用的数（防槽里抛＝进程静默没了）",
     c_state_junk_cache),
    ("打开工坊：四条去两条回各通一次，实例复用，二次打开不把连接叠成两份", c_open_gif_wires),
    ("打开工坊时把生效表递给它的键位镜像，落点是主窗 dispatch，改完键位再打开要用新表",
     c_open_gif_mirrors),
    ("替身与真 GifDialog 同一张接口表（否则这份接线测试测的不是产品）", c_dialog_surface),
    ("spec 六字段按原位交给 GifStore.start，产物目录用配置里的 screenshot_dir",
     c_export_passes_args),
    ("spec 缺键/带脏值：不抛，且把脏值折成合法类型再开", c_export_junk_spec),
    ("引擎说「开不成」时那句原因必须回灌成一条失败结果", c_export_error_reported),
    ("开成了就不发假失败（对话框会显示「导出失败：」）", c_export_no_fake_failure),
    ("内核没起来时按这两条动作：回一句话，且不建对话框、不截帧", c_kernel_only_says),
    ("内核在跑时同一组动作真做得成（dispatch 少分支就是按了没反应）", c_kernel_running_works),
    ("连拍张数夹 2 到 60、间隔夹 50 到 3000", c_burst_clamps),
    ("再按一次 B 是取消：剩余归零、表停、之后不再多截一张", c_burst_repress_cancels),
    ("一轮跑完：张数不多不少、报「连拍完成」、表停下来且是单发模式", c_burst_completes),
    ("每张带不带字幕跟着 screenshot_include_sub 走（True/False 各一整轮）", c_burst_sub_flag),
    ("连拍的张数与间隔来自人手改过的 JSON：字符串、None、浮点都得折成能用的数",
     c_burst_junk_cfg),
    ("「打开目录」递的是目录不是文件，None 与空串都兜住", c_reveal_uses_dir),
    ("目录没了与 shell 拒开各有自己的话，且都不许把异常穿出槽外", c_reveal_failure_branches),
]


# ============================================================ 正常自检
def run() -> int:
    bad = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except BaseException as exc:                 # noqa: BLE001 - 崩了也要留下是哪条
            ok, detail = False, f"判据自身抛 {type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  —— {detail}", flush=True)
        if not ok:
            bad.append(name)
    print(f"\n合计 {len(CHECKS)} 条，失败 {len(bad)} 条" + (f"：{bad}" if bad else ""))
    return 1 if bad else 0


# ============================================================ 证伪
def falsify() -> int:
    """把主窗口某个方法换成「看起来对」的写法，对应那条判据必须翻红。翻不红＝那条是摆设。"""
    print("---- 证伪：削弱主窗口接线以后，对应判据必须变红 ----")
    names = ("gif_state", "_open_gif", "_gif_export", "_gif_cancel", "dispatch",
             "start_burst", "_burst_tick", "_reveal_path", "_install_gif_shortcuts",
             "_install_shortcuts")
    saved = {n: getattr(MainWindow, n) for n in names}
    saved_table = tuple(MW.KERNEL_ONLY)
    saved_dialog = {n: REAL_DIALOG.__dict__.get(n) for n in SURFACE}
    hits: list[tuple[str, bool, str]] = []
    covered: set = set()

    def restore():
        for n in names:
            setattr(MainWindow, n, saved[n])
        MW.KERNEL_ONLY = saved_table
        for n, v in saved_dialog.items():
            if v is None:
                if hasattr(REAL_DIALOG, n):
                    try:
                        delattr(REAL_DIALOG, n)
                    except (AttributeError, TypeError):
                        pass
            else:
                setattr(REAL_DIALOG, n, v)

    def target(fn):
        for _n, f in CHECKS:
            if f is fn:
                try:
                    return fn()
                except BaseException as exc:         # noqa: BLE001
                    return False, f"判据崩了 {type(exc).__name__}: {exc}"
        raise AssertionError(f"证伪表指向的函数不在 CHECKS 里：{fn.__name__}")

    def add(label, fn, stub):
        covered.add(fn)
        restore()
        stub()
        try:
            ok, detail = target(fn)
        except BaseException as exc:                 # noqa: BLE001
            ok, detail = False, f"判据崩了 {type(exc).__name__}: {exc}"
        hits.append((label, not ok, f"削弱后该条判据 ok={ok} —— {detail}"))
        restore()

    def st(key):
        """常见的「少读一个缓存项」：把 gif_state 里某一个键改成拍脑袋的值。"""
        def patched(self):
            p = self.player
            d = dict(saved["gif_state"](self))
            if key == "video":
                d["video"] = str(p.prop("media-title") or "")
            elif key == "pos":
                d["pos"] = 0.0
            elif key == "dur":
                d["duration"] = 0.0
            elif key == "ab":
                d["ab"] = None
            elif key == "sub":
                d["sub"] = ""
            elif key == "sync":
                d["pos"] = float(p.get("time-pos", 0.0) or 0.0)      # 同步请求：本仓纪律禁止
            return d
        return patched

    def naive_state(self):
        """缓存里的东西一个都不兜类型：这是「按一下就闪退（rc=127）」那一类的现场。"""
        p = self.player
        return {"video": str(p.prop("path")), "pos": float(p.prop("time-pos")),
                "duration": float(p.prop("duration")), "ab": p.ab_range(), "sub": ""}

    add("W1 video 拿 media-title → 缓存那条必须红", c_state_from_cache,
        lambda: setattr(MainWindow, "gif_state", st("video")))
    add("W2 pos 恒 0（忘了搬播放位置）", c_state_from_cache,
        lambda: setattr(MainWindow, "gif_state", st("pos")))
    add("W3 duration 恒 0（区间就夹不住）", c_state_from_cache,
        lambda: setattr(MainWindow, "gif_state", st("dur")))
    add("W4 A-B 不报（对话框那个按钮永远灰）", c_state_from_cache,
        lambda: setattr(MainWindow, "gif_state", st("ab")))
    add("W5 外挂字幕不报（烧字幕功能默默没了）", c_state_from_cache,
        lambda: setattr(MainWindow, "gif_state", st("sub")))
    add("W6 用同步 get 读位置（缓存纪律破了）", c_state_from_cache,
        lambda: setattr(MainWindow, "gif_state", st("sync")))
    add("W7 缓存空着不兜 None → 空缓存那条必须红", c_state_empty_cache,
        lambda: setattr(MainWindow, "gif_state", naive_state))
    add("W8 字符串缓存直接 float() → 垃圾缓存那条必须红", c_state_junk_cache,
        lambda: setattr(MainWindow, "gif_state", naive_state))

    def open_lazy(self):                     # 少三条接线，且只在创建那一次刷新界面
        if self.gif_dlg is None:
            self.gif_dlg = MW.GifDialog(self.cfg, self.gif_state, self)
            self.gif_dlg.sig_export.connect(self._gif_export)
            self.gif_dlg.sig_burst.connect(self.start_burst)
            self.gif_dlg.sig_reveal.connect(self._reveal_path)
            self.gif.sig_done.connect(self.gif_dlg.on_done)
        self.gif_dlg.show()
        self.gif_dlg.raise_()

    def open_every_time():                   # 每次都重连一遍：信号叠成两份
        def patched(self):
            if self.gif_dlg is None:
                self.gif_dlg = MW.GifDialog(self.cfg, self.gif_state, self)
            d = self.gif_dlg
            d.sig_export.connect(self._gif_export)
            d.sig_cancel.connect(self._gif_cancel)
            d.sig_burst.connect(self.start_burst)
            d.sig_reveal.connect(self._reveal_path)
            self.gif.sig_progress.connect(d.on_progress)
            self.gif.sig_done.connect(d.on_done)
            d.refresh()
            d.show()
        return patched

    add("W9 接线少三条 + 只在创建时刷新", c_open_gif_wires,
        lambda: setattr(MainWindow, "_open_gif", open_lazy))
    add("W10 每次打开都重连 → 叠连接那条必须红", c_open_gif_wires,
        lambda: setattr(MainWindow, "_open_gif", open_every_time()))

    def drop_on_done():
        try:
            delattr(REAL_DIALOG, "on_done")
        except (AttributeError, TypeError):
            pass

    add("W11 真对话框改名而替身没跟着改 → 接口对账必须红", c_dialog_surface, drop_on_done)

    def exp_swap(self, spec):                # width/fps 对调：最「看起来正常」的坏
        jid, err = self.gif.start(spec["video"], spec["start"], spec["dur"],
                                  str(self.cfg["screenshot_dir"]),
                                  width=spec["fps"], fps=spec["width"],
                                  sub_file=spec["sub"])
        if err and self.gif_dlg is not None:
            self.gif_dlg.on_done(0, False, err, "")

    def exp_dir(self, spec):                 # 产物目录拿成 DATA_DIR
        jid, err = self.gif.start(spec["video"], spec["start"], spec["dur"],
                                  cfgmod.DATA_DIR, width=spec["width"], fps=spec["fps"],
                                  sub_file=spec["sub"])
        if err and self.gif_dlg is not None:
            self.gif_dlg.on_done(0, False, err, "")

    def exp_naive(self, spec):               # 不夹键不兜类型：KeyError 坐在槽里
        jid, err = self.gif.start(spec["video"], spec["start"], spec["dur"],
                                  str(self.cfg["screenshot_dir"]), width=spec["width"],
                                  fps=spec["fps"], sub_file=spec["sub"])
        if err and self.gif_dlg is not None:
            self.gif_dlg.on_done(0, False, err, "")

    def exp_swallow(self, spec):             # 开不成的原因不回灌（按钮灰着等一个不来的回话）
        self.gif.start(spec["video"], spec["start"], spec["dur"],
                       str(self.cfg["screenshot_dir"]), width=spec["width"],
                       fps=spec["fps"], sub_file=spec["sub"])

    def exp_always_fail(self, spec):         # 不管有没有 err 都发一条失败
        jid, err = self.gif.start(spec["video"], spec["start"], spec["dur"],
                                  str(self.cfg["screenshot_dir"]), width=spec["width"],
                                  fps=spec["fps"], sub_file=spec["sub"])
        if self.gif_dlg is not None:
            self.gif_dlg.on_done(jid, False, err or "", "")

    add("W12 width/fps 对调 → 原位那条必须红", c_export_passes_args,
        lambda: setattr(MainWindow, "_gif_export", exp_swap))
    add("W13 产物目录拿成 DATA_DIR", c_export_passes_args,
        lambda: setattr(MainWindow, "_gif_export", exp_dir))
    add("W14 直接按键名取值 → 缺键那条必须红（原 KeyError 现场）", c_export_junk_spec,
        lambda: setattr(MainWindow, "_gif_export", exp_naive))
    add("W15 开不成的原因吞掉 → 回灌那条必须红", c_export_error_reported,
        lambda: setattr(MainWindow, "_gif_export", exp_swallow))
    add("W16 不管成败都报失败 → 「不发假失败」那条必须红", c_export_no_fake_failure,
        lambda: setattr(MainWindow, "_gif_export", exp_always_fail))

    def gate_one(self, action):              # 新动作没进 KERNEL_ONLY：没内核时静默无反应
        MW.KERNEL_ONLY = tuple(x for x in saved_table if x != action)
        saved["dispatch"](self, action)

    def no_branch(self, action):             # dispatch 里根本没有这两条分支
        if action in ("export_gif", "burst_shot"):
            return
        saved["dispatch"](self, action)

    add("W17 export_gif 不在 KERNEL_ONLY 里", c_kernel_only_says,
        lambda: setattr(MainWindow, "dispatch",
                        lambda self, a: gate_one(self, a) if a == "export_gif"
                        else saved["dispatch"](self, a)))
    add("W18 两条都从表里删掉", c_kernel_only_says,
        lambda: setattr(MW, "KERNEL_ONLY",
                        tuple(x for x in saved_table if x not in ("export_gif", "burst_shot"))))
    add("W19 dispatch 里没有这两条分支 → 内核在跑那半边必须红", c_kernel_running_works,
        lambda: setattr(MainWindow, "dispatch", no_branch))
    add("W20 start_burst 成了空壳 → 内核在跑那半边也必须红", c_kernel_running_works,
        lambda: setattr(MainWindow, "start_burst",
                        lambda self, count=None, gap_ms=None: None))

    def burst_noclamp(self, count=None, gap_ms=None):
        if self._burst_left:
            self._burst_left = 0
            self._burst_timer.stop()
            self._osd("连拍已取消")
            return
        n = int(count if count is not None else self.cfg.get("burst_count", 8))
        gap = int(gap_ms if gap_ms is not None else self.cfg.get("burst_gap_ms", 250))
        self._burst_left = n
        self._burst_timer.setInterval(gap)
        self._osd(f"连拍 {n} 张")
        self._burst_tick()

    def burst_repress(self, count=None, gap_ms=None):
        n = _si(count if count is not None else self.cfg.get("burst_count", 8), 8)
        gap = _si(gap_ms if gap_ms is not None else self.cfg.get("burst_gap_ms", 250), 250)
        self._burst_left = max(2, min(60, n))          # 没有「再按一次＝取消」那一段
        self._burst_timer.setInterval(max(50, min(3000, gap)))
        self._osd(f"连拍 {self._burst_left} 张")
        self._burst_tick()

    def _si(v, fallback):
        try:
            return int(v)
        except (TypeError, ValueError):
            return fallback

    add("W21 不夹边界 → 夹边界那条必须红", c_burst_clamps,
        lambda: setattr(MainWindow, "start_burst", burst_noclamp))
    add("W22 配置里的字符串直接 int() → 垃圾配置那条必须红", c_burst_junk_cfg,
        lambda: setattr(MainWindow, "start_burst", burst_noclamp))
    add("W23 再按一次当「再起一轮」 → 取消那条必须红", c_burst_repress_cancels,
        lambda: setattr(MainWindow, "start_burst", burst_repress))

    def tick_off_by_one(self):
        if not self._burst_left:
            self._burst_timer.stop()
            return
        self._burst_left -= 1                          # 先减后截：少一张
        if self._burst_left <= 0:
            self._osd("连拍完成")
            self._burst_timer.stop()
            return
        self.player.snapshot(bool(self.cfg["screenshot_include_sub"]))
        self._burst_timer.start()

    def tick_keep_running(self):
        if not self._burst_left:
            return                                     # 末张既不停表也不报完成
        self.player.snapshot(bool(self.cfg["screenshot_include_sub"]))
        self._burst_left -= 1
        if self._burst_left:
            self._burst_timer.start()

    def tick_always_sub(self):
        if not self._burst_left:
            self._burst_timer.stop()
            return
        self.player.snapshot(True)                     # 字幕旗子硬编码
        self._burst_left -= 1
        if self._burst_left:
            self._burst_timer.start()
        else:
            self._osd("连拍完成")

    add("W24 先减后截（少一张）→ 跑完那条必须红", c_burst_completes,
        lambda: setattr(MainWindow, "_burst_tick", tick_off_by_one))
    add("W25 末张不停表也不报完成", c_burst_completes,
        lambda: setattr(MainWindow, "_burst_tick", tick_keep_running))
    add("W26 字幕旗子硬编码 → True/False 各一轮那条必须红", c_burst_sub_flag,
        lambda: setattr(MainWindow, "_burst_tick", tick_always_sub))

    def reveal_file(self, path):                       # 递的是文件不是目录
        try:
            os.startfile(str(path or ""))
        except OSError as exc:
            self._osd(f"打不开目录：{exc}")

    def reveal_no_guard(self, path):                   # 不判目录还在不在：让 shell 去报错
        try:
            os.startfile(os.path.dirname(str(path or "")))
        except OSError as exc:
            self._osd(f"打不开目录：{exc}")

    def reveal_throws(self, path):                     # 连 OSError 都不兜：槽里抛＝进程没了
        d = os.path.dirname(str(path or ""))
        if d and os.path.isdir(d):
            os.startfile(d)

    add("W27 打开的是那个 .gif 文件 → 递目录那条必须红", c_reveal_uses_dir,
        lambda: setattr(MainWindow, "_reveal_path", reveal_file))
    add("W28 不判目录是否存在 → 「已经不在了」那支必须红", c_reveal_failure_branches,
        lambda: setattr(MainWindow, "_reveal_path", reveal_no_guard))
    add("W29 shell 抛出不兜 → 穿出槽外那条必须红", c_reveal_failure_branches,
        lambda: setattr(MainWindow, "_reveal_path", reveal_throws))

    def mirror_none(self):                          # 工坊拿到的是零条镜像：症状＝根本没修
        pass

    def mirror_default(self):                       # 递出厂表而不是 self.key_rows：两份事实
        if self.gif_dlg is None:                    # 构造期主窗自己装表时工坊还没建
            return
        self.gif_dlg.install_owner_keys(KM.effective(None), self.dispatch)

    def mirror_only_first():                        # 只在第一次打开时递：改完键位再开还是旧表
        def patched(self):
            if self.gif_dlg is None:
                self.gif_dlg = MW.GifDialog(self.cfg, self.gif_state, self)
                self.gif_dlg.sig_export.connect(self._gif_export)
                self.gif_dlg.sig_cancel.connect(self._gif_cancel)
                self.gif_dlg.sig_burst.connect(self.start_burst)
                self.gif_dlg.sig_reveal.connect(self._reveal_path)
                self.gif.sig_progress.connect(self.gif_dlg.on_progress)
                self.gif.sig_done.connect(self.gif_dlg.on_done)
                saved["_install_gif_shortcuts"](self)
            self.gif_dlg.refresh()
            self.gif_dlg.show()
            self.gif_dlg.raise_()
            self.gif_dlg.activateWindow()
        return patched

    def mirror_wrong_sink():                        # 表对了但落点是别人的方法：键响没人办事
        def patched(self):
            if self.gif_dlg is None:
                return
            sink = lambda a: None                   # noqa: E731 - 就是要一个什么都不做的落点
            self.gif_dlg.install_owner_keys(self.key_rows, sink)
        return patched

    add("W30 工坊不递镜像 → 打开工坊那条必须红", c_open_gif_mirrors,
        lambda: setattr(MainWindow, "_install_gif_shortcuts", mirror_none))
    add("W31 递的是出厂表而不是 self.key_rows → 两份事实那条必须红", c_open_gif_mirrors,
        lambda: setattr(MainWindow, "_install_gif_shortcuts", mirror_default))
    add("W32 只在第一次打开时递 → 改完键位再打开那条必须红", c_open_gif_mirrors,
        lambda: setattr(MainWindow, "_open_gif", mirror_only_first()))
    add("W33 落点接成空壳 → 交回 dispatch 那条必须红", c_open_gif_mirrors,
        lambda: setattr(MainWindow, "_install_gif_shortcuts", mirror_wrong_sink()))

    covered_names = {id(f) for f in covered}
    uncovered = [n for n, f in CHECKS if id(f) not in covered_names]
    missed = [l for l, caught, _d in hits if not caught]
    for label, caught, detail in hits:
        print(f"[{'CAUGHT' if caught else 'MISSED'}] {label}  —— {detail}", flush=True)
    print(f"\n证伪 {len(hits)} 组，漏网 {len(missed)} 组" + (f"：{missed}" if missed else "")
          + f"，没人证的判据 {len(uncovered)} 条"
          + (f"：{uncovered}" if uncovered else ""))
    return 1 if (missed or uncovered) else 0


def drain(seconds: float = 60.0) -> int:
    """退出前排空线程池：带着在跑的 Qt 任务结束解释器会 abort（rc=127），那会被误读成
    「功能坏了」。排不空就改判 rc=3（收尾/环境问题，不是功能结论）。"""
    from PyQt5.QtCore import QThreadPool
    pool = QThreadPool.globalInstance()
    t0 = time.time()
    while pool.activeThreadCount() and time.time() - t0 < seconds:
        APP.processEvents()
        time.sleep(0.05)
    pool.waitForDone(2000)                             # PyQt5 没有 Qt6 的 waitForFinished
    for _ in range(20):
        APP.processEvents()
        time.sleep(0.02)
    return pool.activeThreadCount()


if __name__ == "__main__":
    rc = falsify() if "--falsify" in sys.argv else run()
    left = drain()
    print(f"[exit] rc={rc} 线程池残留={left}", flush=True)
    if left:
        rc = 3                     # 收尾没排空：这轮的数不算功能结论
    shutil.rmtree(TMP, ignore_errors=True)
    # 判据与线程池都收完了还让解释器走正常析构，Qt 对象与 Python GC 的先后顺序
    # 在 Windows 上会随机踩空（0xC0000005）。进程走到这里，结论已经在屏幕上，
    # 直接按 rc 交账，不让收尾段的段错误改写结论（收尾前的大小写检查仍照跑）。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
