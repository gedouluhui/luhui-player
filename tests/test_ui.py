"""第一轮外壳真机验收：真窗口、真内核、真截屏。

用法：
    python tests/test_ui.py            # 正常验收：全部断言必须 PASS
    python tests/test_ui.py --falsify  # 证伪：故意喂坏样本，检测器必须判 FAIL 才算可信

退出码：0 全绿；1 有断言或坏样本没过；2 缺样片；3 **本轮有判废**（环境没给证据），三桩：
①整屏抠不出像素（显示器休眠/锁屏）②抓屏区域被**别的进程**的窗口压住、raise_/topmost 都抬不动
③按键通道中途被外来窗口抢走前台。这些既不算通过也不算产品失败——它们量的是"眼睛/键盘此刻
归谁"，不是功能，逐条点名后收尾报 rc=3，别拿它当 bug 查；其余判据照常记账，一趟环境抖动
不带走整张台账。注意优先级：**同轮只要有一条真红，rc 仍是 1**，判废不许把失败洗成环境问题。

与 test_engine.py 的分工：那份证明"内核说得对"，这份证明"外壳接上以后屏幕上真的
出现了画面"。所以这份必须跑在有桌面会话的真机上，不接受 offscreen 平台，也不用
"应该能显示"当结论——画面有无、气泡位置、控件与内核是否一致，全部用像素和属性值判。
"""
from __future__ import annotations

import ast
import ctypes
import ctypes.wintypes as wt
import inspect
import io
import math
import os
import re
import shutil
import sys
import time
import tokenize

if hasattr(sys.stdout, "reconfigure"):     # Windows 控制台默认 GBK，中文断言名会糊
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.abspath(os.path.join(HERE, ".selftest-data")))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from PyQt5.QtCore import (QEvent, QPointF, QPoint, QRect, QSize, Qt, QUrl,
                          QMimeData)  # noqa: E402
from PyQt5.QtGui import (QColor, QCursor, QDropEvent, QFont, QFontMetrics, QIcon, QImage,
                         QKeySequence, QMouseEvent, QPainter, QPen,
                         QPixmap)  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel, QLineEdit,
                             QPushButton, QScrollArea, QSlider, QShortcut,
                             QStackedWidget, QToolButton, QVBoxLayout, QWidget)  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app import win32  # noqa: E402
from app.engine import perf, transform, tweaks  # noqa: E402
from app.engine.player import _OBSERVED  # noqa: E402  ← 判据要的就是"订阅表里有没有这项"这份事实
from app.engine.thumbs import ThumbStore  # noqa: E402
from app.main import _enable_high_dpi, crash_log_path, install_crash_log  # noqa: E402
from app.ui import chrome, icons, keymap, library_panel as LP, theme, tweak_panel as TP  # noqa: E402
from app.ui import form_rows, transform_panel as GP  # noqa: E402
from app.ui.main_window import (KERNEL_ONLY, KEYMAP, MORE_ACTIONS, VIEW_ACTIONS,
                                MainWindow, menu_action_names)  # noqa: E402
from app.ui.title_bar import TitleBar  # noqa: E402
from app.ui.settings_dialog import (EFFECT_NOW, INT_RANGE, ITEMS, SettingsDialog)  # noqa: E402
from app.ui.sleep_timer import ACTIONS, SleepDialog  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
OUT = os.path.join(HERE, "out")
FILES = [os.path.join(SAMPLES, n) for n in ("gelu-sample-1.mp4", "gelu-sample-2.mp4")]

APP: QApplication | None = None
results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool | None, str]] = []     # None = 这条本轮不作数（见 f_case）
qt_messages: list[str] = []
# 判据前提立不住的地方，两桩：① 抓屏被**别的过程**的窗口按住不动（capture() 自己记）；
# ② 按键通途中途被外来窗口抢走（env_check 记）。单独开一份台账的原因和 f_case 一样：
# 这类红不是产品坏，把它算进失败项就是拿环境状态冒充判据结论；而把它悄悄跳过又会让
# "真的没画出来/键真的是死的"混进去没人发现。记下来、逐条点名、收尾时整轮判 rc=3
# （环境不足）——三条都要，才既不冤枉产品也不放过真坏，也不让一趟环境抖动带走整张台账。
env_notes: list[str] = []
# capture() 在这一块上判过废（挡路的窗口全属别的过程）⇒ 记下这块的名字。
# 像素判据靠它把两件事分开：「眼睛被别人的窗口挡住了」记 ENV，「我们真的没画出来」照常 FAIL。
# 混成一类是最坏的一种形状——R7 实测（tests/out/accept-ui-r7.log:148）外来窗口按住视频区，
# 那条判据既进了判废台账又进了失败项，rc 从 3 变成 1，下一位读者会去改本来对的渲染代码。
CAPTURE_ENV: set[str] = set()

# 滑轨把手的直径，和 app/ui/theme.py 那条 `QSlider::handle:horizontal` 里写的 width 同一个数。
# 为什么在测试这边也留一份而不从 QSS 里读：判据要问的是"屏幕上这颗圆是不是这么宽"，
# 把期望值从被检的那张表里抄出来，就等于让样式表自己出题自己答（改了数字判据跟着变，永远不会红）。
# 两处不一致时 test_ui 会直接报"实测 12×14 / 期望 14"，把改动人逼回来对齐这一行。
HANDLE_DIAMETER = 14


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def env_check(name: str, detail: str = "") -> None:
    """判据**前提立不住**时记一条 ENV：既不写 PASS（那等于把没验过的报成验过），
    也不写 FAIL（那等于拿环境状态冒充产品结论）。

    和 f_case 里 `ok=None` 那一档同一个规矩，只是用在真窗口这一头。收尾时 report() 会把
    整轮判 rc=3 并逐条点名——台账一行都不许悄悄消失。
    """
    env_notes.append(f"{name} → {detail}")
    print(f"[ENV] {name}  —— {detail}", flush=True)


def pixel_check(cap: str, name: str, ok: bool, detail: str = "") -> None:
    """吃真抓屏的那几条判据怎么记账：capture() 刚在这块上判过废 ⇒ 记 ENV，不记 FAIL。

    为什么必须分家（R7 实测的形状，tests/out/accept-ui-r7.log:148）：外来窗口按住视频区时
    capture() 已经把这块登进判废台账、返回的是空图，而判据写作 `... and not e_area` 就必然
    为假 ⇒ 同一条既"不作数"又"真实失败"，rc 从 3 掉到 1。那一轮 153/154 里唯一的红就是这么
    来的，而窗口内容一个字没变。把环境状态判成产品失败，引着人去改本来对的代码。
    为什么不许悄悄跳过（记 PASS 或不记）：那等于给所有"真的没画出来"发免检金牌。判据是
    `cap in CAPTURE_ENV`，来自 capture() 那一趟的实际分流（GetWindowThreadProcessId 的 pid），
    不读错误文案；且 capture() 每次进先把名字摘出去，所以命中的永远是**这一趟**的结论，
    不会拿上一块的判废给这一块免检。由 U57 两条腿钉住（外来挡路记 ENV、自家挡路照常 FAIL）。
    """
    if cap in CAPTURE_ENV:
        print(f"[ENV] {name}  —— 这一块的抓屏本轮不作数（挡路的窗口全属别的过程），"
              f"既不算通过也不算产品失败：{detail}", flush=True)
        return
    check(name, ok, detail)


SCREEN_OK: bool | None = None      # 抓屏通路此刻出不出像素（capture_health 的结论）


def f_case(name: str, hit: bool, detail: str = "", needs_screen: bool = False) -> None:
    """记一条证伪样本；**依赖真抓屏**的那几条在屏幕点不亮时挂成 ENV（ok=None）。

    为什么必须单独认这一类，而不是让它算 CAUGHT 或 MISSED：屏幕休眠时整屏抠出来是全 0，
    于是"空容器必须被判为无画面"这种**负向**样本会"抓到"得毫无意义（任何图都判无画面），
    而"彩色帧必须判有画面"这种**正向**样本会 MISSED（看着像检测器坏了）。两种都是拿环境
    状态冒充检测器结论。挂成 ENV、退出码走 3，才是这份测试自己立的规矩。
    """
    falsified.append((name, None if (needs_screen and SCREEN_OK is False) else bool(hit), detail))


def pump(secs: float = 0.0) -> None:
    """把事件跑出去：Qt 的跨线程信号要有人跑循环才送达。

    单位是**秒**。这个参数以前叫 `ms`，是名骗人的旧名：函数体一直拿 `time.monotonic()`
    的秒数在比，于是 `pump(400)` 不是 400 毫秒而是 400 秒（本机实测：新写的验收脚本
    照名字传毫秒，整轮卡在那一行，30 秒周期的 faulthandler 栈把它钓了出来）。
    要跑毫秒就写 `pump(0.4)`。改名不动语义：仓里 120 多个调用点传的都是秒级小数
    （0.2/0.3/180/120.0），全按位置传，没有一处用 `ms=` 关键字。
    """
    if APP is None:
        return
    APP.processEvents()
    if secs:
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            APP.processEvents()
            time.sleep(0.005)


def wait_for(pred, timeout: float, step: float = 0.03) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    pump()
    return False


# ---------------------------------------------------------------- 像素判读
def sample_stats(img: QImage, nx: int = 48, ny: int = 27) -> dict:
    """把图压成三个数：平均亮度、亮度标准差、粗量化后的颜色种类。

    只采 nx×ny 个格子（1296 点）而不是逐像素：判"这里有没有画面"用不着全图，
    而 QImage.pixelColor 逐点调用在整屏分辨率上会把测试拖到几十秒。
    """
    if img.isNull() or img.width() < 4 or img.height() < 4:
        return {"mean": 0.0, "std": 0.0, "distinct": 0, "n": 0,
                "min": 0.0, "max": 0.0, "chroma": 0}
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    buckets: set[tuple[int, int, int]] = set()
    lumas: list[float] = []
    chroma = 0
    for j in range(ny):
        y = min(h - 1, int((j + 0.5) * h / ny))
        for i in range(nx):
            x = min(w - 1, int((i + 0.5) * w / nx))
            c = img.pixelColor(x, y)
            buckets.add((c.red() // 12, c.green() // 12, c.blue() // 12))
            lumas.append(0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue())
            # 彩度命中：max(r,g,b)-min(r,g,b) ≥ 40 才算"有颜色"。单靠亮度方差判
            # "有画面"会被文字骗过去（--falsify U18：空浮层上的时间气泡是白字压深底，
            # 标准差照样 18、颜色桶照样 ≥6，判据把"只是个空框"判成了"真有一帧"）。
            if max(c.red(), c.green(), c.blue()) - min(c.red(), c.green(), c.blue()) >= 40:
                chroma += 1
    n = len(lumas)
    mean = sum(lumas) / n
    var = sum((v - mean) ** 2 for v in lumas) / n
    return {"mean": mean, "std": math.sqrt(var), "distinct": len(buckets), "n": n,
            "min": min(lumas), "max": max(lumas), "chroma": chroma}


def has_picture(img: QImage) -> bool:
    """非单色、有亮度反差、且**真有颜色** → 判"画面真在里面"。

    三个条件各管一类冒牌货：纯色/全黑/全透明的标准差接近 0；深底白字（空浮层上的
    时间气泡、卡片占位块）有反差有颜色桶却没有彩度。门槛不是拍出来的——U18/U20 拿
    "彩色帧 vs 纯色空框"和"真封面 vs 占位块"两组已知答案把这条判据两头都夹住。

    彩度按**面积比**而不是绝对像素数：Windows 上文字走 ClearType 亚像素抗锯齿，
    占位块右下角那行灰色 "MP4" 会在笔画边缘留下几粒鲜艳到 max-min≥40 的彩边
    （真机实测 4/1296，离屏平台则是 0）。只数"有几粒"的话，灰字占位块会被判成
    有帧；要求彩色像素占到采样点的 1% 才能通过，真帧（同一样本 10%–100%）不受影响。
    """
    s = sample_stats(img)
    return s["distinct"] >= 6 and s["std"] >= 4.0 and s["chroma"] * 100 >= s["n"]


# 系统电源状态（仅取"视频是否已断电"这一位用来解释为什么整屏是黑的）
class _SYSPOWER(ctypes.Structure):
    """Win32 SYSTEM_POWER_STATUS：四个字节加两个 DWORD，**顺序与名字都要按 SDK 来**。
    旧写法只有三个字节，于是 `flag` 这个变量拿到的其实是第四位之前的"电量百分比"
    （本机实测 0x5b=91，正是电量而不是任何标志位），旁边还编了一句"0x8=显示器断电/休眠"
    ——那一位在 BatteryFlag 里是"交流在线"，而这个 API 压根不报显示器开没开。
    判据不吃它（屏在不在只看整屏采样），但这行字是给人读"这轮为什么不作数"的，编不得。"""
    _fields_ = [("ac_line", ctypes.c_ubyte),       # 0=电池 1=交流 255=未知
                ("battery_flag", ctypes.c_ubyte),  # 逐位见 power_words
                ("battery_pct", ctypes.c_ubyte),   # 255=未知／无电池
                ("system_flag", ctypes.c_ubyte),
                ("life", ctypes.c_long), ("full", ctypes.c_long)]


_BATTERY_BITS = ((0x01, "电量高"), (0x02, "电量临界"), (0x04, "电量低"),
                 (0x08, "交流在线"), (0x10, "充电中"), (0x20, "保留位"),
                 (0x40, "无电池"), (0x80, "电源状态未知"))


def power_words(flag: int) -> str:
    """把 BatteryFlag 那一个字节逐位翻成人话（纯函数，证伪轮直接喂已知答案）。"""
    hits = [w for bit, w in _BATTERY_BITS if flag & bit]
    return "／".join(hits) if hits else "无位"


def desktop_evidence() -> str:
    """环境侧的三样旁证：外壳窗口、任务栏、电源状态。只读，取不到就说取不到。"""
    try:
        u = ctypes.WinDLL("user32")     # 新开实例：不给共享的 windll.user32 挂 argtypes
        k = ctypes.WinDLL("kernel32")
        sh = int(u.GetShellWindow() or 0)
        tb = int(u.FindWindowW("Shell_TrayWnd", None) or 0)
        vis = bool(u.IsWindowVisible(ctypes.c_void_p(tb))) if tb else False
        r = wt.RECT()
        if tb:
            u.GetWindowRect(ctypes.c_void_p(tb), ctypes.byref(r))
        p = _SYSPOWER()
        k.GetSystemPowerStatus(ctypes.byref(p))
        pct = "未知" if p.battery_pct == 255 else f"{p.battery_pct}%"
        return (f"外壳窗口={hex(sh) if sh else '无'} 任务栏={hex(tb) if tb else '无'}"
                f"({'可见' if vis else '不可见'},{r.left},{r.top},{r.right},{r.bottom})"
                f" 电源={power_words(p.battery_flag)}(0x{p.battery_flag:02x}) 电量={pct}"
                f"（屏在不在不靠这里，靠整屏采样）")
    except BaseException as exc:
        return f"环境旁证取失败:{type(exc).__name__}"


def capture_health(img: QImage | None = None) -> tuple[bool, str]:
    """抓屏之前先证明"这块屏此刻真的在出像素"，否则整轮的像素判据都不作数。

    为什么需要这一条：实测踩过——人离开、显示器休眠以后，整块屏（连任务栏一起）抠出来
    全是 0，而 WindowFromPoint 照样说我们的窗口压在最上面。遮挡闸门全过，五条像素判据
    一起 FAIL，看着像产品同时坏了五件事，其实是环境不给屏幕。把环境问题写成功能失败，
    等于伪造故障；所以这里先单独判一次，判据用"整屏采样里有没有第二种亮度"，
    旁证（任务栏、供电位）只用来点名"是谁把屏幕拿走了"。

    `img` 是注入口：证伪那case要喂一张"已知全黑"的图和一张"已知有色"的图，
    两头都夹住才说明这个检测器本身可信，而不是跟着当时的桌面状态随机报。
    """
    if img is None:
        img = APP.primaryScreen().grabWindow(0).toImage()
    st = sample_stats(img)
    ok = img.width() >= 16 and img.height() >= 16 and st["distinct"] >= 2 and st["max"] > 8
    return ok, (f"整屏 {img.width()}×{img.height()} 亮度{st['mean']:.0f}"
                f"（最低{st['min']:.0f}/最高{st['max']:.0f}）颜色{st['distinct']}；"
                f"任务栏在不在、显示器有没有断电——{desktop_evidence()}")


def has_ink(img: QImage) -> tuple[bool, str]:
    """气泡这类"深底浅字"的小图：判"字有没有真的画上去"用明暗反差，不用颜色数。

    has_picture 那套（颜色种类 ≥6）在这里会误杀：气泡底是近实心的深色圆角块，
    24×12 采样点大多落在同几个色桶里，判据该问的是"有亮字压在暗底上吗"。
    """
    s = sample_stats(img, 24, 12)
    ok = img.width() >= 8 and img.height() >= 8 and s["max"] - s["min"] > 60 and s["std"] > 8.0
    return ok, (f"{img.width()}×{img.height()} 明暗跨度{s['max'] - s['min']:.0f} "
                f"标准差{s['std']:.0f} 颜色{s['distinct']}")


def icon_dark_ink(name: str, px: int = 32, color: str = "#FFFFFF") -> tuple[int, int]:
    """按请求色画一遍，返回 (明显偏离该色的墨迹像素数, 墨迹像素总数)。

    为什么单独测：图标工厂里 _fill() 会 setPen(Qt.NoPen)，之后 p.pen().color() 拿到的
    是黑色，紧接着 _stroke 就画成黑线——深色底上那半边图标直接隐身，尺寸再大也看不出来。
    像素是唯一会告密的地方。
    """
    img = icons.pixmap(name, px, color).toImage().convertToFormat(QImage.Format_ARGB32)
    want = QColor(color)
    dark = tot = 0
    for j in range(img.height()):
        for i in range(img.width()):
            c = img.pixelColor(i, j)
            if c.alpha() > 40:
                tot += 1
                if (abs(c.red() - want.red()) + abs(c.green() - want.green())
                        + abs(c.blue() - want.blue())) > 150:
                    dark += 1
    return dark, tot


def icon_out_of_frame(name: str, px: int = 32) -> int:
    """把 0..24 设计框放进 3×3 的中间格来画，数框外还剩多少墨。

    出框的几何在正常渲染里会被 pixmap 直接裁掉——"画了等于没画"，而且不报错。
    正常渲染看不到，所以这里故意给一张 3 倍大的画布，让出框部分显形。
    """
    pm = QImage(px * 3, px * 3, QImage.Format_ARGB32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QColor("#FFFFFF")
    p.setPen(QPen(c))
    p.setBrush(c)
    p.translate(px, px)
    p.scale(px / 24.0, px / 24.0)
    icons.D[name](p)
    p.end()
    out = 0
    for j in range(0, pm.height(), 2):
        for i in range(0, pm.width(), 2):
            if (i < px or i >= 2 * px or j < px or j >= 2 * px) and pm.pixelColor(i, j).alpha() > 8:
                out += 1
    return out


def grab_widget(w: QWidget) -> QImage:
    """从**屏幕**上抠这块控件，而不是 QWidget.grab()/PrintWindow。

    要证的是"眼睛看得见画面"：mpv 的画面是 D3D 子窗口，Qt 的合成截图能不能读到它
    我没有实测过，而抓屏幕读到的必然就是屏幕上真正显示的东西。

    代价是**别的窗口盖上来就会拍到别人的像素**（实测拍到过一次 Google 安全验证弹窗，
    整张图是白的，而"颜色数 ≥6"照样判 PASS）。所以这条只用在必须抓屏的 mpv 画面上，
    并且配合 capture() 的遮挡闸门；纯 Qt 画的面板改用 grab_qt()。
    """
    scr = w.screen() if hasattr(w, "screen") else APP.primaryScreen()
    shot = scr.grabWindow(0)                       # 该显示器的整屏（设备像素）
    dpr = w.devicePixelRatioF() or 1.0
    origin = scr.geometry().topLeft()
    tl = w.mapToGlobal(QPoint(0, 0)) - origin
    x = int(tl.x() * dpr) + 6
    y = int(tl.y() * dpr) + 6
    cw = int(w.width() * dpr) - 12
    ch = int(w.height() * dpr) - 12
    if cw < 8 or ch < 8:
        return QImage()
    return shot.copy(QRect(x, y, min(cw, shot.width() - x), min(ch, shot.height() - y))).toImage()


_U32 = ctypes.windll.user32
_U32.WindowFromPoint.restype = ctypes.c_void_p
_U32.GetAncestor.restype = ctypes.c_void_p
_U32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
# 注意：`ctypes.windll.user32` 是**整个进程共享**的缓存对象，在这里给 GetWindowRect /
# GetClassNameW 之类挂 argtypes，会把 app/win32.py 里同一批调用一起打死（实测报
# "expected LP_RECT instance instead of pointer to RECT"）。取名字只需要 byref，不挂。


def _hwnd_chain_at(dev_x: int, dev_y: int) -> list[int]:
    """屏幕物理坐标这个点上，最靠上的窗口是谁，再沿 GA_PARENT 一路往上（含它自己）。

    为什么要走整条链而不是只比顶层：这个应用在这个点上可能是三种窗口——
    ① Qt 的非原生子控件（侧栏、面板）：WindowFromPoint 只能给出宿主顶层窗口；
    ② mpv 的渲染子窗口：它是视频容器的**子窗口**，顶层才是我们的主窗口；
    ③ OSD / 悬停预览这种带 parent 的 Tool 浮层：Windows 把它的 owner 当成
    GA_ROOT 的答案（实测：只比 `winId()` 会把自家浮层判成"被别人盖住"）。
    把命中窗口和它的父链一起摊出来，三种都能对上，而别人的窗口不在这条链上。
    """
    h = _U32.WindowFromPoint(wt.POINT(int(dev_x), int(dev_y)))
    out: list[int] = []
    while h and len(out) < 12:
        hwnd = int(h)
        if hwnd in out:
            break
        out.append(hwnd)
        h = _U32.GetAncestor(h, 1)                   # GA_PARENT = 1
    return out


def _hwnd_pid(h: int) -> int:
    """这个句柄属于哪个进程；读不出来回 0（0 在 _exempt_blocker 里=不豁免，宁可不给金牌）。"""
    try:
        pid = wt.DWORD(0)
        _U32.GetWindowThreadProcessId(ctypes.c_void_p(int(h)), ctypes.byref(pid))
        return int(pid.value)
    except BaseException:                    # 判据自己不许崩
        return 0


def _hwnd_debug(h: int) -> str:
    """给一个句柄起个名：类名 + 进程 + 屏幕矩形。

    FAIL 行必须点名"挡着的是谁"——只报一串十六进制的话，分不清是用户自己的浏览器
    压在测试窗口上（环境问题）还是我们自己的浮层压根没画出来（功能问题），而后者的
    修法完全不同。真踩过：一条"被别的窗口盖住"里三个句柄，一个是 Qt 的覆盖窗、
    一个是主窗口的宿主、一个是视频子窗，光看数字谁也说不清挡路的是哪一层。
    """
    try:
        buf = ctypes.create_unicode_buffer(256)
        _U32.GetClassNameW(ctypes.c_void_p(int(h)), buf, 256)
        pid = wt.DWORD(0)
        _U32.GetWindowThreadProcessId(ctypes.c_void_p(int(h)), ctypes.byref(pid))
        r = wt.RECT()
        _U32.GetWindowRect(ctypes.c_void_p(int(h)), ctypes.byref(r))
        vis = bool(_U32.IsWindowVisible(ctypes.c_void_p(int(h))))
        own = "自家" if int(pid.value) == os.getpid() else "外来"
        return (f"{hex(h)}/{buf.value or '?'}/{own}pid{pid.value}/"
                f"{'可见' if vis else '隐藏'}/({r.left},{r.top},{r.right},{r.bottom})")
    except BaseException as exc:            # 判据自己不许崩（崩了会被当成"抓到 bug"）
        return f"{hex(h)}?({type(exc).__name__})"


def _own_hwnds(w: QWidget) -> set[int]:
    """这块控件在 Win32 侧可能以哪些句柄出现在命中链上：它自己 + 它的 Qt 祖先的顶层。"""
    chain: list[QWidget] = [w, w.window()]
    pw = w.parentWidget()
    while pw is not None:
        chain.append(pw)
        chain.append(pw.window())
        pw = pw.parentWidget()
    return {int(x.winId()) for x in dict.fromkeys(chain)}


def _want_hwnds(w: QWidget) -> set[int]:
    """这个点上算"我们自己"的句柄集合。

    浮层（悬停预览、OSD）自己就是一个窗口，只允许命中它本身。留着祖先句柄的后果是
    实测踩到的那样：浮层没显示时，命中链落在它爹（视频容器）上照样放行，于是拿到
    一张全黑图，彩色帧与纯色帧两个样本一起判"无画面"——证伪那头的"抓到"其实是"被骗"。
    """
    if w.window() is w:
        return {int(w.winId())}
    return _own_hwnds(w)


def _set_topmost(hwnd: int, on: bool) -> None:
    """HWND_TOPMOST / HWND_NOTOPMOST：越过 Windows 的前台锁。

    为什么 raise_() 不够：别的进程刚接过鼠标时，SetForegroundWindow 会被系统拒掉，
    Qt 只把窗口标成"需要用户注意"，Z 序根本没动。抓屏验收要证的是"这块像素是我们
    画的"，不能拿前台权赌运气——实测浏览器盖在上面时四条抓屏判据一起 FAIL，而窗口
    内容一点没变（那是环境问题，不是功能坏了）。压完立刻还原，不改应用自身的窗口样式。
    """
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    _U32.SetWindowPos(ctypes.c_void_p(int(hwnd)), ctypes.c_void_p(-1 if on else -2),
                      0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def capture(top: QWidget, w: QWidget, name: str = "",
            fracs: tuple[tuple[float, ...], tuple[float, ...]] = ((0.15, 0.5, 0.85),
                                                                  (0.15, 0.5, 0.85))
            ) -> tuple[QImage, str]:
    """抓屏 + 遮挡闸门：先证明"这块区域露在最上层的就是我们自己"，再抠图。

    返回 (图, 错误说明)。错误非空时这张图**不能用**，调用方必须把它一起报出来——
    没有这道闸，别的窗口盖在上面时"有画面/颜色够多"这类判据会拿别人的像素判 PASS
    （真踩过：侧栏那张留档整张是别人的验证码弹窗，判据全绿）。

    `fracs` 是采样点的位置（宽/高 fractions）。OSD 与悬停预览是**分层窗口**：
    圆角外沿是透明的，WindowFromPoint 会穿透到下面的窗口，所以那种浮层只量中心
    （中心一定落在浮层自己的不透明底上）。

    进门先把这个名字从判废集合里摘出去：CAPTURE_ENV 表示的是**这一趟**的结论。不摘就会留下
    "上一趟被外来窗口挡住、这一趟明明抓到了却仍被记 ENV"的金牌——同一名字在证伪那一趟里
    会被反复 capture（"视频区"就有两次），漏摘就是拿旧的环境状态免检新的判据。
    """
    CAPTURE_ENV.discard(name)
    top.raise_()
    # 浮层（OSD / 悬停预览）带 WindowDoesNotAcceptFocus：对它们调 activateWindow()
    # 只会让 Qt 吐一条 "requestActivate() called for ... has WindowDoesNotAcceptFocus"
    # 警告，把 Qt 消息计数污染成噪声。raise_() 足以把它们顶到最上层。
    if not (top.window().windowFlags() & Qt.WindowDoesNotAcceptFocus):
        top.activateWindow()
    pump(0.3)
    scr = w.screen() if hasattr(w, "screen") else APP.primaryScreen()
    origin = scr.geometry().topLeft()
    dpr = w.devicePixelRatioF() or 1.0
    want = _want_hwnds(w)

    def occluded() -> list[tuple[str, int]]:
        bad: list[tuple[str, int]] = []
        for fx in fracs[0]:
            for fy in fracs[1]:
                g = w.mapToGlobal(QPoint(int(w.width() * fx), int(w.height() * fy))) - origin
                hit = _hwnd_chain_at(g.x() * dpr, g.y() * dpr)
                if not (set(hit) & want):
                    bad.append((f"({fx},{fy})→[{_hwnd_debug(hit[0]) if hit else '空'}]",
                                int(hit[0]) if hit else 0))
        return bad

    bad = occluded()
    if bad:
        _set_topmost(int(top.winId()), True)
        pump(0.35)
        bad = occluded()
        _set_topmost(int(top.winId()), False)
        pump(0.1)
    if bad:
        ws = "、".join(_hwnd_debug(h) for h in sorted(want)[:3])
        tops = list(dict.fromkeys((b[1], _hwnd_pid(b[1])) for b in bad if b[1]))
        # 抬不动的时候再分一次流：**每个**挡路的窗口都属于别的过程 ⇒ 这是环境（外来窗口抢前台，
        # 我们无权动它），本轮像素判据整体不作数（rc=3）；只要有一个是我们自己的窗口，那就还是
        # 功能问题（浮层没画出来 / Z 序自己搞乱），照旧 FAIL。判据来自
        # GetWindowThreadProcessId 的返回值，不读文案——和 U23 那条豁免分流同一个口径。
        if tops and all(_exempt_blocker(h, p) for h, p in tops):
            who = "、".join(_hwnd_debug(h) for h, _p in tops[:2])
            env_notes.append(f"{name} → {who}")
            CAPTURE_ENV.add(name)         # 同一份结论交给 pixel_check：这一块上的像素判据记 ENV
            return QImage(), (f"{name}：这块区域被**别的进程**的窗口盖住，raise_/topmost 都抬不动"
                              f"（挡路的：{bad[0][0]}）——环境不足，本轮像素判据不作数")
        return QImage(), (f"{name}：这块区域没露在最上层（我们要的是 {ws}，"
                          f"已试过 raise_ 与 topmost；挡在最上面的是 {[b[0] for b in bad][:3]}）")
    return grab_widget(w), ""


def _exempt_blocker(hwnd: int, pid: int) -> bool:
    """这一挡该不该记"本轮不作数"：**只有别的进程的窗口**才豁免。

    单独拎成纯函数，是为了让 U23b 能拿四条已知答案钉死它（没挡路／自家／外来／读不出 pid）。
    "读不出 pid"也归到不豁免：豁免是免检金牌，金牌只能戴在证据完整的外来窗口上。
    """
    return hwnd != 0 and pid != 0 and pid != os.getpid()


def env_blocker(w: QWidget) -> tuple[int, int]:
    """提升之后仍压着这块控件的窗口是谁：(句柄, pid)；没被压住时返回 (0, 0)。

    只给 U23 的"必须放行"那一半分诊用，判据来自 WindowFromPoint 与
    GetWindowThreadProcessId 的返回值，**不读错误文案**——文案一改就把真故障分流成
    "环境问题"是这一类闸门最坏的失效法。
    分流规则：pid 不是我们自己 → 本轮不作数（用户开着浏览器很正常，测试无权动别人的窗口，
    更不该把它判成产品坏了）；pid 是我们自己的另一个句柄 → 那是自家浮层没抬起来，
    必须记 MISSED，不许拿"环境问题"当挡箭牌。
    """
    top = w.window()
    top.raise_()
    _set_topmost(int(top.winId()), True)
    pump(0.35)
    try:
        scr = w.screen() if hasattr(w, "screen") else APP.primaryScreen()
        origin = scr.geometry().topLeft()
        dpr = w.devicePixelRatioF() or 1.0
        g = w.mapToGlobal(QPoint(int(w.width() / 2), int(w.height() / 2))) - origin
        hit = _hwnd_chain_at(g.x() * dpr, g.y() * dpr)
        if not hit or (set(hit) & _want_hwnds(w)):
            return 0, 0
        pid = wt.DWORD(0)
        _U32.GetWindowThreadProcessId(ctypes.c_void_p(int(hit[0])), ctypes.byref(pid))
        return int(hit[0]), int(pid.value)
    finally:
        _set_topmost(int(top.winId()), False)
        pump(0.1)


def grab_qt(w: QWidget) -> QImage:
    """Qt 自己渲染这块控件（不经屏幕），纯 Qt 面板用它。

    为什么不用抓屏：抓屏会被别的窗口盖住（见 capture 的闸门），而这些面板本来就是
    Qt 画的，`grab()` 画的就是屏幕上那份内容（同一套 QSS、同一套字体度量），
    还顺带把"用户把窗口最小化了测不了"这种环境问题去掉。mpv 的 D3D 子窗口不在 Qt 的
    绘制体系里，那部分只能抓屏——所以两种抓法各管各的，不是随手挑的。
    """
    return w.grab().toImage()


def osd_in_picture(win: MainWindow) -> tuple[bool, str]:
    """OSD 是独立顶层窗口：位置算错（DPI 那份账）会飘到画面外，这条专门抓它。"""
    g = win.osd.geometry()
    tl = win.video.mapToGlobal(QPoint(0, 0))
    br = tl + win.video.rect().bottomRight()
    ok = (win.osd.isVisible() and g.width() <= win.video.width()
          and g.x() >= tl.x() and g.y() >= tl.y() and g.right() <= br.x() and g.bottom() <= br.y())
    return ok, f"osd=({g.x()},{g.y()},{g.width()}×{g.height()}) 画面={tl.x()},{tl.y()}..{br.x()},{br.y()}"


def floater_in_picture(w: QWidget) -> tuple[bool, str]:
    """浮层（OSD / 悬停预览）通用位置判据：可见、不超出画面、不越过窗口下沿。

    锚点是视频控件不是主窗口，所以用 `.window()` 回溯顶层——直接拿 anchor 当窗口用
    会在 VideoSurface 上找不到 .video（第一版就这么崩过）。
    """
    win = w.anchor.window()
    g = w.geometry()
    tl = win.video.mapToGlobal(QPoint(0, 0))
    br = tl + win.video.rect().bottomRight()
    bar_top = win.bar.mapToGlobal(QPoint(0, 0)).y()
    inside = (g.width() <= win.video.width() and g.x() >= tl.x() and g.y() >= tl.y()
              and g.right() <= br.x() and g.bottom() <= br.y())
    above_bar = g.bottom() <= bar_top
    return (w.isVisible() and inside and above_bar), (
        f"{type(w).__name__}=({g.x()},{g.y()},{g.width()}×{g.height()}) "
        f"画面={tl.x()},{tl.y()}..{br.x()},{br.y()} 控制条上沿={bar_top} "
        f"可见={w.isVisible()} 在内={inside} 在条上={above_bar}")


def preview_contention(x_ref: int, at_ref, cur, scrub_rect,
                       x_now: int, at_now, visible_now: bool) -> tuple[bool, str]:
    """合成悬停之后浮层又挪了位/藏起来了——是**真光标**挤的，还是产品自己动的手？

    为什么要这一把（2026-10-13，#72 的落点）：`hover_scrub` 发的是 `QApplication.sendEvent`
    的合成 MouseMove，它不搬操作系统那只真鼠标。而 Windows 还在往同一个控件投真实移动——
    r13 真机那一趟紧接着的截图像素判据报的是"浮层 0×0 + 原生窗说它隐藏"，同时浮层的 x
    从 433 跳到 736。736 正是 `_place` 的右端夹取上限（视频左 61、宽 890、浮层宽 212 ⇒
    `hi_x = 61+890-212-4 = 735`），也就是"有一次鼠标移动落在了轨道很靠右的那一格"。
    合成事件只发过一次（40% 那一格），能把浮层推到右端再藏起来的只有真光标：用户那只鼠标
    当时正压在时间轴上，`pump()` 一跑就把 Qt 排队的真实 move/leave 一起处理了。

    判决只看一件事，而且是**可证伪**的一件事：漂移发生的那一刻，真光标在不在时间轴的矩形里。
    在 ⇒ 争用（环境的证据拿走了，记 ENV）；不在 ⇒ 没有任何外部输入却让浮层跑了位，那才是产品缺陷。
    纯函数：所有数都从参数进来，证伪台账（U6g）能喂已知答案的四种形状，不必真造一只鼠标。
    """
    same_at = tuple(at_ref or ()) == tuple(at_now or ())
    drift = (x_now != x_ref) or (not same_at) or (not visible_now)
    if not drift:
        return False, "浮层没被动过（这一条不涉争用）"
    over = bool(scrub_rect is not None and scrub_rect.contains(cur))
    return over, (f"浮层被动过：事件里最后那格 {tuple(at_ref or ())} → {tuple(at_now or ())}，"
                  f"x {x_ref}→{x_now}，可见={visible_now}；真光标=({cur.x()},{cur.y()}) "
                  f"轨道矩形={scrub_rect.getRect() if scrub_rect is not None else '(没有)'} "
                  f"→ {'压在轨道上＝真实鼠标挤了合成事件' if over else '不在轨道上＝没人碰鼠标它也动了'}")


def play_tip_flip(pause_now, tip_before: str, tip_now: str) -> tuple[bool, str]:
    """按了一次播放/暂停之后：按钮上那句"说法"要**对**，而且要和按之前**不一样**。

    为什么要这一把纯函数（2026-10-13，#73 的落点）。原先验收里那条判据写的是
    `pause 变 True 且 toolTip().startswith("播放")`，两个洞：
      ① **前缀不算方向**。内核还没推 `paused` 时按钮说的是中性的「播放/暂停（Space/K）」，
         它照样以「播放」开头；而暂停时说「暂停」是状态口径（2026-10-13 之前的产品写法），
         它不以「播放」开头——所以 r14 那条红是真红。但"没重画所以还是旧话"这一类坏法
         会被前缀蒙过去。于是这里比的是**括号之前那半句的全文**，不是前缀。
      ② **只比"现在对不对"不比"变没变"**。`player._apply()` 跑在 `mpv-ipc-read` 那根读事件
         线程上，它**先写 `_props` 缓存再发 `sig_pause`**（`app/engine/player.py:422`／`:429`），
         而 `sig_pause` 到控件是跨线程排队连接。`wait_for` 的谓词是"泵事件循环＋读缓存＋读提示"，
         于是在"缓存已经 True、重画还没送达"的那个窗口里，状态那一半能过，说法那一半读的还是
         上一次那半句——r13/r8 的绿就是这么来的（偶然，不是产品对）。
         要求"和按之前不一样"把这个时间窗钉死：读到的必须是**这次翻转之后**的新话。
    纯函数：三个数都从参数进来，证伪台账（U6h）能喂六种已知答案的形状，不必真造一次点击。
    """
    want = "播放" if bool(pause_now) else "暂停"
    saying = str(tip_now or "").split("（", 1)[0].strip()
    if saying != want:
        return False, (f"pause={bool(pause_now)} 该说={want!r} 实说={saying!r}"
                       f"（整句={tip_now!r}）")
    if str(tip_now or "") == str(tip_before or ""):
        return False, (f"说法对了但和按之前那句**一字不差**（{tip_now!r}）——要么这颗按钮的说法"
                       "不跟状态变，要么判据读到的是跨线程送达之前的旧话；两种都不算同步")
    return True, f"pause={bool(pause_now)} {tip_before!r} → {tip_now!r}"


def hover_scrub(scrub, x: int) -> None:
    """把一次鼠标移动送进时间轴的**事件通道**，而不是直接调它的内部方法。

    要证的是"光标 → sig_hover_sec → 主窗口处理器 → 浮层"这条链：直调 _emit_preview
    会把中间的信号连接整个跳过，链子断了照样绿。
    """
    ev = QMouseEvent(QEvent.MouseMove, QPoint(x, scrub.height() // 2),
                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(scrub, ev)


def leave_scrub(scrub) -> None:
    QApplication.sendEvent(scrub, QEvent(QEvent.Leave))


# ---------------------------------------------------------------- 侧栏页序
def page_index(win, label: str) -> int:
    """某一页在侧栏栈里的下标：按**页签标签**从 `win.tab_defs` 反查，调用处不许抄数字。

    为什么要收口成一把（2026-10-13，从真机漏网里学到的）：证伪台账里有 `win2._set_sidebar(True, 4)`
    这种手抄下标，而 2026-10-12 加「声音均衡」那一格把「统计」从 4 顶到 5。r13 真机那一趟
    U36「丢帧行」因此切到了均衡页，统计页根本没显示 ⇒ 三条子判据全在"藏着的行"上量，
    记成 MISSED（读数就是那行 `侧栏可见=False`）。产品一行没坏，坏的是抄下来的页序。
    `tests/probe_surface_cover.py` 管逐屏体检那四处手抄名单，这一把管台账里的切页——
    两把都不许再按下标写死。
    """
    names = [str(t[0]) for t in win.tab_defs]
    if label not in names:
        return -1           # 不抛：崩在报告路上比红一条更坏；-1 被 `_sidebar_page` 夹成第 0 页，
                            # 于是依赖这一页的判据自己翻红，而 `页序查无…` 会印在 detail 里
    return names.index(label)


def page_of(win, label: str):
    """按标签取侧栏页的**节点**（`sidebar_stack.widget(i)`），同上，不抄下标。"""
    return win.sidebar_stack.widget(max(0, page_index(win, label)))


# ---------------------------------------------------------------- 媒体库判读
def card_image(icon: QIcon) -> QImage:
    """把卡片图标的位图取出来。

    为什么不能用"有没有图标"当判据：占位块和真封面都是**有色**的 QIcon，
    `isNull()` 两头都 False。能分开的只有像素分布——占位块是一整块底色加角落几个字，
    采样出来的颜色桶和标准差都趴在地上，真帧则两者都高。--falsify U20 就是拿
    真的 `_placeholder()` 产物来否掉这条判据的。
    """
    sizes = icon.availableSizes()
    sz = sizes[0] if sizes else QSize(LP.ICON_W, LP.ICON_H)
    return icon.pixmap(sz).toImage()


def row_of(grid, suffix: str) -> int:
    """按文件名找行号。视图的真相在模型里，视图自己不再持有 item。"""
    model = grid.model()
    if model is None:
        return -1
    for i in range(model.rowCount()):
        p = str(model.index(i, 0).data(Qt.UserRole) or "").replace("/", os.sep).lower()
        if p.endswith(suffix.lower()):
            return i
    return -1


def card_text(grid, suffix: str) -> str:
    """按文件名取卡片文字。行号取不到时返回空串——测试宁可判 FAIL 也不许自己崩。"""
    model = grid.model()
    r = row_of(grid, suffix)
    return "" if r < 0 or model is None else str(model.index(r, 0).data(Qt.DisplayRole) or "")


def dblclick_row(grid, row: int) -> bool:
    """把一次真的双击送进列表的事件通道，并回报 `doubleClicked` 到底响没响。

    直接调 `panel._play(item)` 会跳过 itemDoubleClicked 那段链——链子断了（比如
    信号没连、行号取错）照样绿，而用户按的恰恰是鼠标。

    必须发**完整四步**（press→release→dblclick→release）：Qt 的
    `QAbstractItemView::mouseDoubleClickEvent` 开头就要求内部状态是 Pressing
    （`d->state != Pressing` 直接 `event->ignore()`），裸发一个 DblClick 事件
    会被吞掉——真实鼠标双击天然带前面的按下，所以只有合成事件会踩这个坑。

    返回值取"信号有没有发出来"而不是"事件有没有送出去"：后者永远为真，
    等于没判。
    """
    model = grid.model()
    if model is None or not 0 <= row < model.rowCount():
        return False
    r = grid.visualRect(model.index(row, 0))
    if not r.isValid() or r.isEmpty():
        return False
    c = r.center()
    g = grid.viewport().mapToGlobal(c)
    fired: list[int] = []
    grid.doubleClicked.connect(lambda idx: fired.append(idx.row()))
    seq = ((QEvent.MouseButtonPress, Qt.LeftButton, Qt.LeftButton),
           (QEvent.MouseButtonRelease, Qt.LeftButton, Qt.NoButton),
           (QEvent.MouseButtonDblClick, Qt.LeftButton, Qt.LeftButton),
           (QEvent.MouseButtonRelease, Qt.LeftButton, Qt.NoButton))
    try:
        for etype, button, buttons in seq:
            QApplication.sendEvent(grid.viewport(), QMouseEvent(
                etype, QPointF(c), QPointF(g), button, buttons, Qt.NoModifier))
            QApplication.processEvents()
    finally:
        try:
            grid.doubleClicked.disconnect()
        except Exception:
            pass
    return bool(fired)


def cards_agree(rows: int, items: int) -> bool:
    """卡片数与扫描条目一致，且表非空。

    为什么要单拎出"非空"这半句：空表时 rows==items==0 也满足"一致"，内联写法的判据
    当场变成恒真——扫描整个没跑、跑崩返回空列表，都能拿绿。写成可调用对象是为了
    --falsify U21 能直接对它注入 (0, 0) 这组已知坏样本；内联表达式没法注入。
    """
    return rows == items and rows >= 3


def bare_dblclick(grid, row: int) -> bool:
    """只发一个 MouseButtonDblClick（不带前面的按下），回报 doubleClicked 响没响。

    这是 dblclick_row 的证伪对照：如果裸发也能触发，说明 `clicked` 那个返回值
    根本不需要四步序列，"送对了事件"就成了空话。Qt 的
    `QAbstractItemView::mouseDoubleClickEvent` 要求内部状态为 Pressing，
    所以裸发应当**不**触发——U22 判的就是这个方向。
    """
    model = grid.model()
    if model is None or not 0 <= row < model.rowCount():
        return False
    r = grid.visualRect(model.index(row, 0))
    if not r.isValid() or r.isEmpty():
        return False
    c, g = r.center(), grid.viewport().mapToGlobal(r.center())
    fired: list[int] = []
    grid.doubleClicked.connect(lambda idx: fired.append(idx.row()))
    try:
        QApplication.sendEvent(grid.viewport(), QMouseEvent(
            QEvent.MouseButtonDblClick, QPointF(c), QPointF(g),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        QApplication.processEvents()
    finally:
        try:
            grid.doubleClicked.disconnect()
        except Exception:
            pass
    return bool(fired)


# ---------------------------------------------------------------- 调节面板判读
def mpv_fills(video) -> tuple[bool, str]:
    """mpv 的渲染子窗口是否铺满 Qt 容器（差 ≤2 物理像素）。

    侧栏开合、全屏、缩放全靠 video.refit() 重铺，所以量的是**原生几何**而不是
    Qt 的 width()：真出问题时（DPI 账算错、在动画中间态取样）Qt 侧看着是对的，
    屏幕上却留一条没铺到的黑边——只有子窗口矩形能发现。
    """
    kids = win32.direct_children(video.hwnd)
    if not kids:
        return False, "没有子窗口可量"
    cw, ch = win32.client_size(video.hwnd)
    r = win32.window_rect(kids[0])
    sw, sh = r[2] - r[0], r[3] - r[1]
    return abs(sw - cw) <= 2 and abs(sh - ch) <= 2, f"容器 {cw}×{ch}，子窗口 {sw}×{sh}"


FS_OK_SHAPES = ("铺满", "上下留黑", "左右留黑")   # 「正常档」：窗口态／全屏都只许落在这三档


def fit_reading(video, top=None, tag: str = "") -> tuple[bool, str, str, str]:
    """量一拍「mpv 子窗口铺满容器了没有 ＋ 那块上的画面形状落在哪一档」。

    两层都得有：「窗口全屏了、里面那块却没铺满」是两件事（R20 用户实测的形状），
    而**只有原生矩形**能证明铺满（`--wid` 嵌进来的子窗口不随父窗口缩放），
    **只有像素**能证明画面真到了边上（矩形相等而内核把图画小是另一件事）。

    `top` 给了才量像素（那要抓屏），不给就只交矩形。验收与证伪共用**这一个函数**：
    两处各写一遍，迟早只有一处跟着改。
    形状尺直接借 R20 探针那把（`probe_fullscreen_fit.ink_bbox/shape_of`；它自带已知答案档与
    证伪注入，**条数由 `--selfcheck` / `--selfcheck-mutate` 自己打印**，这里 deliberately 不抄——
    抄进注释的每一处都会在下次加档时变成谎话。其中 ⑦/M7 那一对钉的是"没铺到的那圈是主题底色
    #17181C 而不是纯黑"时不许读成铺满），这里不另立口径。延迟 import 是因为探针 import 本文件，
    写在模块顶层会成环。
    """
    fills, fd = mpv_fills(video)
    if top is None:
        return fills, fd, "", ""
    img, err = capture(top, video, f"视频区·{tag}")
    if err or img.isNull():
        return fills, fd, "无图", err or "空图"
    import probe_fullscreen_fit as R20
    box = R20.ink_bbox(img)
    return (fills, fd, R20.shape_of(box, img.width(), img.height()),
            f"内缩={box} 抠图={img.width()}×{img.height()}")


def chrome_reading(win, fs: bool | None = None) -> tuple[dict, str]:
    """量一拍「全屏时那块舞台被谁压扁了」——几何尺借 R20 探针那把（`chrome_of`）。

    为什么验收要再挂一把**几何**尺、光靠 §10.1 的像素尺不够（R20 的教训）：用户那句
    "画面只在屏幕中间一块、四周都是空的"在像素上叫「居中偏小」，跟"内核把图画小了"（真·B）
    同分——mpv 自己的信箱黑边和容器外面那圈黑是同一种黑。分得开它们的只有布局：
    标题栏在不在场、舞台宽高还差整块屏多少。所以字段与 `probe_fullscreen_fit.qt_block()`
    交出来的那一把**同名同形状**，两处各写一遍迟早只有一处跟着改；
    这把尺自己带已知答案档与证伪注入，**条数由脚本自己打印**（跑
    `python tests/probe_fullscreen_fit.py --selfcheck` 与 `--selfcheck-mutate`）——
    把数字抄进注释的每一处都会在下一次加档时变成谎话，这里 deliberately 不写。
    `fs` 不给就按 windowState 自己判。延迟 import 是因为探针 import 本文件，写在顶层会成环。
    """
    scr = win.screen().geometry()
    on = bool(win.windowState() & Qt.WindowFullScreen) if fs is None else bool(fs)
    host = getattr(win, "host", None)
    hosted = bool(host.is_hosting()) if host is not None else False
    hg = host.geometry() if (host is not None and hosted) else None
    q = {"title_vis": win.title.isVisible(), "bar_vis": win.bar.isVisible(),
         "bar_h": win.bar.height(), "title_h": win.title.height(),
         "video": (win.video.width(), win.video.height()),
         "screen_qt": (scr.width(), scr.height()),
         "screen_phys": (scr.width(), scr.height()),
         "dpr": win.video.devicePixelRatioF(),
         # R21 的四个键与探针 qt_block 同名同形状：`bar_hosted` 决定"醒着的条该不该扣高度"，
         # `sidebar_*` 决定"该给的宽度是不是整块屏"，`float_wanted` 用来抓"开着浮层却还占行"。
         # `host_seen` 单独记一只眼看没看见宿主——看不见时判据必须失效而不是翻红（量具的瞎
         # 不许写成产品的坏），与探针那边是同一套规矩。
         "bar_hosted": hosted, "host_seen": host is not None,
         "float_wanted": bool(win.cfg["float_controls_in_fs"]),
         "sidebar_vis": win.sidebar.isVisible(), "sidebar_w": win.sidebar.width(),
         "host_geo": (None if hg is None else
                      (int(hg.x()), int(hg.y()), int(hg.width()), int(hg.height())))}
    import probe_fullscreen_fit as R20
    ch = R20.chrome_of(q, on)
    det = (f"舞台={q['video'][0]}×{q['video'][1]} 屏={scr.width()}×{scr.height()} "
           f"标题栏={ch['title_up']}(高{q['title_h']}) 条={ch['bar_up']}(高{q['bar_h']}"
           f"住{'浮层' if ch['bar_hosted'] else '布局'}) 浮层矩形={q['host_geo']} "
           f"让行={ch['rows_lost']}px 侧栏={q['sidebar_vis']}({q['sidebar_w']}px) "
           f"整块屏={ch['full_stage']} 浮层没生效={ch['overlay_off']} fs={on} dpr={q['dpr']:.2f}")
    return ch, det


def host_reading(win) -> tuple[dict, str]:
    """量一拍「控制条到底住在哪、宿主那块顶层窗摆在哪」——浮层那一改的全部物理证据都在这儿。

    为什么不能只看 `chrome_of` 的几何：`full_stage` 只证明"舞台等于整块屏"，它证明不了
    条**看得见、点得到**（宿主藏起来了、或跑到屏幕外面、或只盖住半屏，舞台一样是整块屏，
    用户那边却是"全屏之后控制条没了"）。所以这里量四件事，每件都能单独翻红：
      `hosting`      条是否真被宿主认领（`parentWidget() is host`，比 `is_hosting()` 更硬：
                     它认的是 Qt 实际认的那棵树，不是宿主自己记的那一笔）；
      `full_w/bottom`通栏＋贴下沿（`place()` 的几何契约，多屏/混合 DPI 下最容易错的一位）；
      `over_video`   宿主确实压在画面那块上面（airspace 之下只能靠顶层窗，不压上去就等于没浮层）；
      `visible`      现在亮不亮（收起时连宿主一起收，见 `_set_bar_shown`）。
    全是逻辑像素，与 `chrome_of` 同一口径：`mapToGlobal` 给逻辑、`geometry()` 给逻辑，
    这里一个 dpr 都不乘（乘了会在 150% 那档凭空多算半条屏）。
    """
    host, bar = win.host, win.bar
    g = host.geometry()
    scr = win.screen().geometry()
    vg = win.video.mapToGlobal(QPoint(0, 0))
    r = {"hosting": bar.parentWidget() is host,
         "is_hosting": host.is_hosting(),
         "visible": host.isVisible(),
         "geo": (g.x(), g.y(), g.width(), g.height()),
         "full_w": abs(g.width() - scr.width()) <= 2,
         "bottom": abs((g.y() + g.height()) - scr.height()) <= 2,
         "over_video": (vg.y() <= g.y() < vg.y() + win.video.height())}
    det = (f"宿主矩形=({g.x()},{g.y()},{g.width()}×{g.height()}) 屏={scr.width()}×{scr.height()} "
           f"画面顶={vg.y()} 画面高={win.video.height()} 条的父是宿主={r['hosting']} "
           f"宿主自称在带={r['is_hosting']} 可见={r['visible']}")
    return r, det


def panel_matches_table(tp) -> tuple[bool, str]:
    """面板上的控件必须和 engine/tweaks.py 那张表一一对应（数量与键集合都比）。

    单独拎成函数有两个原因：① _build_sub 里曾经写过 `SUB_FLAG[1:]`，表有三档、界面
    只画两根，不报错也不崩溃，只有把键集合摊开比才看得见；② 证伪（U23）要往里注入
    "拔掉一根滑轨"的变异，判据必须能被测试自己调用。
    """
    want = {
        "rows": {s.prop for s in tweaks.NUMS},
        "choices": {c.prop for c in tweaks.CHOICES},
        "flags": {f.prop for f in tweaks.FLAGS},
    }
    got = {"rows": set(tp._rows), "choices": set(tp._choices), "flags": set(tp._flags)}
    bad: list[str] = []
    for k in want:
        diff = want[k] ^ got[k]
        if diff:
            bad.append(f"{k} 差 {sorted(diff)}")
    for prop, row in tp._rows.items():
        spec = tweaks.spec(prop)
        if spec is None or row.spec is not spec:
            bad.append(f"{prop} 的滑轨没绑在表项上")
    n = (f"滑轨 {len(got['rows'])}/{len(want['rows'])} 下拉 {len(got['choices'])}/"
         f"{len(want['choices'])} 勾选 {len(got['flags'])}/{len(want['flags'])}")
    return (not bad), n + ("".join("；" + b for b in bad) if bad else "；逐项对得上")


def panel_agrees_with_kernel(tp) -> tuple[bool, str]:
    """每个控件画的值 == 内核此刻回报的值（面板不许照"我发出去的数"画）。

    比对用 tweaks.same_value——和引擎确认内核回报时同一个判据，这样"界面↔内核"
    与"命令↔回报"两套检查不会各自定一套宽容度（100 与 100.0、颜色的 AA 前缀都是
    这里第一次踩到的）。内核还没回报（None）的项跳过：那是"没起内核"的另一条路，
    由 _current 退回配置。
    """
    off: list[str] = []
    for prop, row in tp._rows.items():
        v = tp.p.prop(prop, None)
        if v is None:
            continue
        got = row.value_of(row.slider.value())
        if abs(got - row.spec.clamp(v)) > 1e-4:
            off.append(f"{prop} 界面{got} 内核{v}")
    for prop, cb in tp._choices.items():
        v = tp.p.prop(prop, None)
        if v is None or str(v) == "":
            continue
        if not tweaks.same_value(tweaks.spec(prop), cb.currentData(), v):
            off.append(f"{prop} 界面{cb.currentData()!r} 内核{v!r}")
    for prop, c in tp._flags.items():
        v = tp.p.prop(prop, None)
        if v is None:
            continue
        if c.isChecked() is not bool(v):
            off.append(f"{prop} 界面{c.isChecked()} 内核{v}")
    total = len(tp._rows) + len(tp._choices) + len(tp._flags)
    return (not off), f"{total} 项里不符 {len(off)}" + (f"：{off[:6]}" if off else "")


def eq_all_zero(win) -> bool:
    """画面五项是否都真的回到 0。

    这里刻意不写 `float(v or 0)`：0.0 是假值，会把"正好回到 0"当成"没回报"，
    反过来又把"内核根本没回"当成"回到 0"——两头都错（引擎自测那条就是这么假红过的）。
    """
    vs = [win.player.prop(s.prop, None) for s in tweaks.EQ]
    return all(v is not None and abs(float(v)) < 1e-6 for v in vs)


_QMAX = 16777215          # Qt 的 QWIDGETSIZE_MAX：用来解除 setFixedWidth/Height 的锁


def panel_layout_ok(tp) -> tuple[bool, str]:
    """面板的"看得见"判据：滑轨真有尺寸、整页确实比视口高（滚动区在承重）。

    拎成函数是为了 --falsify U27：把一根滑轨压成 1px、或把滚动区拉到比内容还高，
    这条都必须变红。否则"控件存在 ≠ 看得见"只是注释里的一句愿望——0 宽的滑轨
    不报错、不崩溃，只是用户找不到它在哪儿。空表时 min() 走 default=0，判不过。
    """
    min_w = min((r.slider.width() for r in tp._rows.values()), default=0)
    min_h = min((r.height() for r in tp._rows.values()), default=0)
    # 把手是一颗 14 的圆，而 Qt 把控件矩形外的像素裁掉：滑轨自己若矮于直径，
    # 圆的上下就被削平——这一步在 QSS 里读不出来（样式表只说"画多大"，不说"给不给画"）。
    min_sh = min((r.slider.height() for r in tp._rows.values()), default=0)
    body_h, vp_h = tp.scroll.widget().height(), tp.scroll.viewport().height()
    return (min_w >= 120 and min_h >= 30 and min_sh >= HANDLE_DIAMETER
            and body_h > vp_h > 0), (
        f"最窄滑轨={min_w} 最矮行={min_h} 最矮滑轨高={min_sh}"
        f"（把手要 {HANDLE_DIAMETER}） 内容高={body_h} 视口高={vp_h}")


# ---------------------------------------------------------------- 统计页判读
# 统计页每一行的数据来源只有两处：订阅表里的属性名（player._OBSERVED）与一秒一轮
# 现问的轮询表（player.STATS_PROPS）。剩下的行是界面拿这些值自己拼出来的（尺寸 =
# width×height、轨道数 = track-list 计数……），必须在这张表里点名。
# 没点名的行 = "永远填不上数的死行"。本机实测 container-format 这类属性在内核里根本
# 不存在（property not found），把它画上去要么留一个空槽，要么写 0 —— 后者把"没测到"
# 伪装成"测到 0"，丢帧两行尤其危险：用户看到"0 帧丢弃"会以为不卡，实际是没法知道。
SYNTH_ROWS = ("size", "aspect", "tracks", "playlist-count", "mpv-version", "ipc",
              "quality_preset",
              # 底部那四行不是内核推的，是 app/engine/usage.py 量出来的（CPU/内存按 pid，
              # GPU 走 nvidia-smi）。它们同样"有源头"——源头是用户点下去的那一次采样，
              # 所以数据源表里必须点名，否则这条判据会把新功能整块判成死行。
              "usage_mpv", "usage_shell", "usage_gpu", "usage_clock")


def rows_have_source(keys, observed, polled) -> tuple[bool, str]:
    dead = [k for k in keys if k not in SYNTH_ROWS and k not in observed and k not in polled]
    return (not dead), f"没有数据源的行={dead}"


def hwdec_row_agrees(raw, visible: bool, text: str) -> tuple[bool, str]:
    """硬解那一行必须写 perf.hwdec_state(raw) 的判读，一个字都不许界面自己猜。

    三个方向都得能翻红：① `'no'` 是真值字符串，老写法 `if raw:` 会念成"硬解 no"，
    而那恰好是"这一版根本没在硬件解码"的反面；② raw 是 None（订阅还没来）时必须整行
    藏起来，写"软件解码"等于把没测到说成测到了；③ 有回报时不许藏着。
    """
    hard, want = perf.hwdec_state(raw)
    expect = ("硬件 " + want) if hard else want
    if raw is None:
        if visible:
            return False, f"内核还没回报 hwdec-current，这行却画着 {text!r}"
        return True, ""
    if not visible:
        return False, f"内核回报 {raw!r}，这一行却藏着"
    if text != expect:
        return False, f"回报 {raw!r} 应写 {expect!r}，实际写 {text!r}"
    return True, ""


def tab_state_agrees(win, i: int) -> tuple[bool, str]:
    """"当前在哪一页"这件事在四个地方各写了一份，四份必须一致，而且**颗数**也要对得上。

    栈索引 / 页签勾选 / 控制条三个灯 / 各页可见性。少同步任何一处，用户看到的
    "灯亮在哪页"就和屏幕上当前的页面不一致（点了统计、灯还留在调节页）。画面、声音均衡与统计
    （第 3、4、5 页）没有专属按钮，所以开这三页时三个灯必须全灭——留着上一页的灯就是双份状态。
    颗数那一笔是 2026-10-12 加页之后补的：这一把尺原来写死 `range(5)`，
    于是"侧栏多了一页、而那页永远不进这份名单"会被读成通过——加页这件事必须由栈自己报数。
    """
    shown = win.sidebar.isVisible()
    idx = win.sidebar_stack.currentIndex()
    checked = [k for k, b in enumerate(win.tab_btns) if b.isChecked()]
    lights = [k for k, b in enumerate((win.bar.btn_list, win.bar.btn_library,
                                       win.bar.btn_tweak)) if b.isChecked()]
    pages = [w.isVisible() for w in (win.panel, win.lib_panel, win.tweak_panel,
                                     win.transform_panel, win.audio_eq_panel, win.stats_panel)]
    want_lights = [idx] if shown and idx <= 2 else []
    # 期望长度按下标数而不是写死：这一栏加过一页，写死数字的那把尺会把"多出来的一页
    # 从来没被显示过"读成通过。
    want_pages = [shown and k == i for k in range(len(pages))]
    bad = []
    counts = (int(win.sidebar_stack.count()), len(pages), len(win.tab_btns), len(win.tab_defs))
    if len(set(counts)) != 1:
        bad.append(f"页数对不上：栈={counts[0]} 名单={counts[1]} 页签={counts[2]} 定义={counts[3]}")
    if idx != i:
        bad.append(f"栈索引={idx}（应为 {i}）")
    if checked != [i]:
        bad.append(f"页签勾选={checked}（应为 [{i}]）")
    if lights != want_lights:
        bad.append(f"控制条灯={lights}（应为 {want_lights}）")
    if pages != want_pages:
        bad.append(f"各页可见={pages}（应为 {want_pages}）")
    return (not bad), "；".join(bad)


def _tab_rows_of(win) -> list[list]:
    """把所有页签按「第几排」分组。（"五颗"是加页之前的旧说法，名单按下标现取。）

    为什么不照 `b.y()` 分桶（上一版的做法）：`_place_tab_rows` 之后每一排是**自己的一个
    横向盒**，盒内按钮的 y 恒为 0，于是两排和三排在 y 上根本分不开——U51 报"排数=1"
    而 `probe_tab_rows.py` 量到 2~3 排，差的就是这一处。现在按父件分组，同时兼容
    网格（父件相同 ⇒ 再按 y 区间切），两种结构都量得出真实排数。
    """
    rows: list[list] = []
    cur: list = []
    for b in win.tab_btns:
        if cur:
            prev = cur[-1]
            same = (b.parentWidget() is prev.parentWidget()
                    and abs(b.y() - prev.y()) < min(b.height(), prev.height()))
            if not same:
                rows.append(cur)
                cur = []
        cur.append(b)
    if cur:
        rows.append(cur)
    return rows


def _in_strip(win, b) -> QRect:
    """按钮在**页签条**坐标里的矩形。

    必须 mapTo 到条：盒结构下 `b.y()` 是"在这一排盒里"的坐标，恒 0，拿它和条高比就永远
    不出界——那一排真的掉在条外也量不到（④b 就是这么被架空的红：判据看着绿，其实瞎）。
    """
    tl = b.mapTo(win.tab_strip, QPoint(0, 0))
    return QRect(tl.x(), tl.y(), b.width(), b.height())


def tabs_fit(win, widths, squeeze: int | None = None,
             no_grow: bool = False) -> tuple[bool, str]:
    """侧栏页签的六条形不变量，设置页全量程（240..520）每一档都得成立。

    六条一起判，因为它们是六种不同的坏（只量其中一条会放过另外五种）：
      ① 这一排**需要的**宽 ≤ 侧栏实际宽：排不开时最后一格被侧栏裁掉（点不到统计页）；
      ② 每格实得宽 ≥ 它自己的 `sizeHint` 宽：布局压破控件自己报的最小需求，这一档也许
         还没切字（QSS 的 padding 已是 0，整格给字用），但换台大字号就是真切字——侧栏 240
         那一档原来就是这么"靠运气不切字"的（tests/probe_tab_fit.py）；
      ③ 实得宽 ≥ 文字宽：切字的直接判据；
      ④ 同一排相邻两格不重叠：1.5 档字号实测到的坏形状——每格钉成固定宽（min==max）而空间不够时，
         盒布局不去破坏固定宽，而是**压缩 x**，于是「播放列表」占 10..95、「媒体库」从 74 开始，
         字压字。②③都量不到它（每格宽度是够的）。判"同一排"是必须的：3.0 档字号下页签条会
         **换行**（`_layout_tabs(..., wrap=True)`），第二排第一个 x≈10、第一排最后一个 x+w≈500，
         按相邻下标硬比会把正常的两排判成重叠。
      ④b 每格都在页签条内：换行长出来的那一排如果被裁掉（条高没跟着长），四把几何尺一条都不报
         ——没切字、没压扁、不跟谁重叠，但它确实点不到了。
      ⑤ 侧栏不超过窗口一半：加宽是有上限的，否则视频区被吃掉。
    `squeeze`：把每格钉成「文字＋squeeze」，模拟"产品退回旧写法"（测②③）。
    `no_grow`：跳过产品那条"排不开就把侧栏加宽"（`_fit_sidebar`），照用户宽度硬钉——
        用来证明①与④是**被那一步保住的**，不是量具白捡的绿。
    """
    orig = int(win.sidebar_width)
    cuts: list[str] = []      # ③真的切字——最严重，排最前，免得被 detail 的截断挤掉
    hints: list[str] = []     # ②压破样式自己报的最小需求
    rows: list[str] = []      # ①整排排不开
    overs: list[str] = []     # ④两格叠在一起
    caps: list[str] = []      # ⑤侧栏吃掉窗口
    try:
        for w in widths:
            win.sidebar_width = w
            if no_grow:
                need = win._layout_tabs(w)
                win.sidebar.setFixedWidth(w)
            else:
                win._fit_sidebar()
                need = int(win.tab_row_need)
            if squeeze is not None:
                for b, (label, _tip) in zip(win.tab_btns, win.tab_defs):
                    b.setFixedWidth(QFontMetrics(b.font()).horizontalAdvance(label) + squeeze)
            pump(0.12)
            if not win.sidebar.isVisible():
                return False, f"侧栏没显示，量出来的宽是摆设（w={w}）"
            for b, (label, _tip) in zip(win.tab_btns, win.tab_defs):
                t = QFontMetrics(b.font()).horizontalAdvance(label)
                if b.width() < b.sizeHint().width():
                    hints.append(f"{w}px 档「{label}」实得 {b.width()} < 样式要的 "
                                 f"{b.sizeHint().width()}")
                if b.width() < t:
                    cuts.append(f"{w}px 档「{label}」实得 {b.width()} < 文字 {t}（切字）")
            if need > win.sidebar.width():
                rows.append(f"{w}px 档这一排需要 {need}px，侧栏只有 {win.sidebar.width()}px（排不开）")
            # ④ 只对**同一排**的两格比 x：换行之后第二排第一个（盒内 x≈0）天然小于第一排最后一个
            # （x+w≈500），照旧写法"按 tab_btns 的相邻下标"去比会把正常的两排判成重叠——那是量具的错。
            # 排与排的身份由 `_tab_rows_of` 给（父件＝那一排的盒），不靠 y。
            for group in _tab_rows_of(win):
                ordered = sorted(group, key=lambda b: b.x())
                for a, b in zip(ordered, ordered[1:]):
                    if a.x() + a.width() > b.x() + 1:
                        overs.append(f"{w}px 档「{a.text()}」与「{b.text()}」重叠 "
                                     f"{a.x() + a.width() - b.x()}px（布局压的是 x，不是格子）")
            # ④b 换行之后新长出来的一排必须还在页签条里：整格掉出去的话四把几何尺一条都不报
            # （没切字、没压扁、没和谁重叠），但它确实点不到了。
            # 比的是**映射到页签条之后**的坐标：盒结构下 `b.y()` 恒 0，跟条高比永远不出界。
            for b, (label, _tip) in zip(win.tab_btns, win.tab_defs):
                r = _in_strip(win, b)
                if (r.right() > win.tab_strip.width()
                        or r.bottom() > win.tab_strip.height() or r.left() < 0):
                    overs.append(f"{w}px 档「{label}」整格掉在页签条外 "
                                 f"{r.x()},{r.y()} {r.width()}×{r.height()} vs 条 "
                                 f"{win.tab_strip.width()}×{win.tab_strip.height()}")
            if win.sidebar.width() * 2 > win.width():
                caps.append(f"{w}px 档侧栏 {win.sidebar.width()}px 超过窗口 {win.width()}px 的一半")
    finally:
        win.sidebar_width = orig
        win._fit_sidebar()
        pump(0.1)
    bad = cuts + hints + rows + overs + caps
    return (not bad), "；".join(bad[:5]) or f"{len(list(widths))} 档全过（含复原）"


def row_hidden_without_data(sp, key: str) -> tuple[bool, str]:
    """没数据源时这一行必须整行不画。判据只看当前状态，所以能两头用：
    真实代码摘掉数据源后必须判「藏着」（正向），手动把它摆回来必须判红（证伪）。
    """
    row = sp._rows[key]
    return (not row.isVisible()), f"可见={row.isVisible()} 值={row.value_text()!r}"


def keymap_covered(actions, dispatch_src: str) -> tuple[bool, str]:
    """快捷键表里的每个动作名，dispatch 里都得真有分支。

    症状是「按了没反应、也不报错」——表是给人看的（快捷键一览、帮助页读它），分支是真
    做事的，两份东西写在同一个文件的不同位置，加一行表忘一段分支没人拦得住。jump_N 是
    参数化分支（startswith("jump_")），单独认；拎成纯函数是为了 --falsify 能直接喂一条
    表外动作名进来，确认这条判据真的会翻红而不是恒过。
    """
    names = set(re.findall(r'action == "([a-z0-9_]+)"', dispatch_src))
    jump = 'action.startswith("jump_")' in dispatch_src
    missing = sorted(a for a in actions
                     if a not in names and not (jump and a.startswith("jump_")))
    return (not missing), f"没有分支的动作={missing}（dispatch 认 {len(names)} 个名字）"


def controls_cover_table(rows, choices, flags, all_keys) -> tuple[bool, str]:
    """画面事实表与那一页的控件必须互为子集，而且控件得绑对行。两边各差一类坏。

    表里有、界面上没有 ＝ 这一项调不了（快捷键能改、面板上找不到对应的滑轨，用户看不出
    哪个坏了）；界面上有、表里没有 ＝ 孤儿控件，改它的值没人接（`set_transform` 对表外键
    只留一行日志），正是本仓库一直禁的"设了没用"。
    键集合对得上还不够：`_add_num(box, 拿错的那一行)` 数量与键名全对，滑轨调的却是另一项
    ——所以滑轨要回查 `row.spec.cfg`，开关要回查它的 `accessibleName`（标签来自 `transform.label`，
    抄错就是"界面上写着锐化、动的是降噪"）。开关自己不留文字（标题在同级 QLabel 上，
    见 `form_rows.add_check`），但"这一项叫什么"仍然挂在控件身上，判据读的就是这一份。
    拎成纯函数是为了 `--falsify` 能直接喂缺项、绑错、孤儿三种坏样本，确认这条判据真会翻红而不是恒过。
    """
    have = set(rows) | set(choices) | set(flags)
    need = set(all_keys)
    missing = sorted(need - have)
    orphan = sorted(have - need)
    misbound = sorted(k for k, r in rows.items()
                      if getattr(r, "spec", None) is None or r.spec.cfg != k)
    wrong_label = sorted(k for k, c in flags.items() if c.accessibleName() != transform.label(k))
    # 名字对上了还不够：屏幕上那一行字现在是**另一块控件**（同级 QLabel）。
    # 只核 accessibleName 的话，"标题丢了/标题和名字不是一份"这两种坏都会绿着过去，
    # 而用户看到的就是"一颗不知道管什么的开关"。
    no_label = sorted(k for k, c in flags.items()
                      if getattr(c, "_row_label", None) is None
                      or c._row_label.text() != c.accessibleName())
    parts = [f"表里有界面没有={missing}" if missing else "",
             f"界面有表里没有={orphan}" if orphan else "",
             f"控件绑错表项={misbound}" if misbound else "",
             f"开关名字对不上表={wrong_label}" if wrong_label else "",
             f"开关行没有可见标题={no_label}" if no_label else ""]
    bad = "；".join(x for x in parts if x)
    return (not bad), (f"{bad}（控件 {len(have)} 项 / 表 {len(need)} 项）" if bad
                       else f"{len(have)} 项与表 {len(need)} 项互为等集、绑定与标签全对")


# ---------------------------------------------------------------- 真实按键
# QKeySequence 解析出来的一个键值 = 事件里的 key（去掉修饰位）+ modifiers。
# 0x01FFFFFF 是 Qt 的键码掩码，0x1E000000 是 Shift|Control|Alt|Meta 四个修饰位
# （小键盘位 0x20000000 不算修饰：主回车和小键盘回车要能各自绑，绑出去的 modifiers
# 里就不该带它）。拆法与 tests/probe_keyseq3.py 实测的事件一致。
KEY_CODE_MASK = 0x01FFFFFF
KEY_MOD_BITS = 0x1E000000

HITS: list[str] = []          # 被摘了调用的 dispatch 落点：只记"哪条动作被触发"


def key_event_parts(seq: str) -> tuple[int, int]:
    """存储串 → 真实按键事件的 (key, modifiers)；解析不出东西回 (0, 0)。"""
    try:
        qs = QKeySequence(str(seq))
        code = int(qs[0]) if qs.count() else 0
    except Exception:          # noqa: BLE001 - 判据自己不许崩
        return 0, 0
    return code & KEY_CODE_MASK, code & KEY_MOD_BITS


def press_key(win: MainWindow, key: int, mods: int = 0, wait: float = 0.05) -> list[str]:
    """往窗口递一次**真** QKeyEvent，回这段时间里被触发的动作（按触发顺序）。

    为什么非要走事件而不直接调 win.dispatch：直接调证明的是"分支存在"，从来没证明
    "这把键按下去走得到那个分支"。上一版默认表里 5 条永不响的写法（PgDn / Backslash /
    Minus / Equals 和只绑了小键盘的 Enter）就是这么躲过两轮验收的——
    `QKeySequence(不认识的键名)` 不报错，解析成 Key_UNKNOWN，快捷键装上、永不响。

    按之前先确认窗口是**活动窗口**：QShortcut(context=WindowShortcut) 只在它自己的窗口
    拿到系统前台时才响应，用户切去浏览器的那一瞬间按下去全是空。上一轮 70 行里有 7 行
    这么空掉（含对照键 Z），被报成了 7 条产品失败。这里补的是**测量条件**，不是判据：
    抬得回来就照常判，抬不回来交给 channel_split 记 ENV。
    """
    HITS.clear()
    if not win.isActiveWindow():
        win.activateWindow()
        win.setFocus(Qt.OtherFocusReason)
        pump(0.15)
    QTest.keyClick(win, Qt.Key(int(key)), Qt.KeyboardModifier(int(mods)))
    pump(wait)
    APP.processEvents()
    return list(HITS)


def press_seq(win: MainWindow, seq: str, wait: float = 0.05) -> list[str]:
    """按存储串递事件。死写法在这里**当场拒绝**：探针 4 实测 `QTest.keyClick(0x01ffffff)`
    直接把进程带走（rc=127、stderr 全空），一个测试助手不许有"按下去就整轮没了"的入口。
    要按死写法背后那把物理键，调用方自己用 press_key 递真键码。"""
    key, mods = key_event_parts(seq)
    if key in (0, keymap.UNKNOWN_KEY):
        raise ValueError(f"{seq!r} 解析成 Key_UNKNOWN，没有可递的事件键码")
    return press_key(win, key, mods, wait)


def install_raw_shortcut(win: MainWindow, seq: str, tag: str) -> QShortcut:
    """往窗口上临时挂一条 QShortcut（校准用：绕开生效表，只看 Qt 认不认这把键）。"""
    sc = QShortcut(QKeySequence(seq), win, context=Qt.WindowShortcut)
    sc.activated.connect(lambda checked=False, t=tag: HITS.append(t))
    return sc


def drop_shortcuts(*scs: QShortcut) -> None:
    """和 _install_shortcuts 同一套卸法：setEnabled(False) + 摘父 + deleteLater。

    只 del 引用没用——QShortcut 是窗口的子对象，摘了 Python 引用它照样挂在窗口上响应，
    校准探针就会污染后面每一条判据（一次按键多出一条命中）。另外别写 clearShortcut()：
    本机 PyQt5 没这个方法（探针 4 实测 AttributeError），而它写在测试里会把整轮带走。
    """
    for sc in scs:
        sc.setEnabled(False)
        sc.setParent(None)
        sc.deleteLater()
    APP.processEvents()


def _foreground_debug() -> str:
    """此刻系统前台归谁（ENV 行必须点名，不然"被抢前台"和"键是死的"在日志里长得一样）。

    用产品自己的 `win32.foreground_hwnd()`（那里 restype 已声明成 HWND），不直接碰
    `_U32.GetForegroundWindow`——裸调在这个进程里拿到的是被截成 32 位的句柄。
    """
    try:
        h = win32.foreground_hwnd()
        return _hwnd_debug(h) if h else "没有前台窗口（会话没桌面？）"
    except BaseException as exc:              # 判据自己不许崩
        return f"读不出({type(exc).__name__})"


def keyboard_focus_ok(win: MainWindow) -> tuple[bool, str]:
    """合成按键到底进不进得来：进不来就是焦点/会话问题，判据必须先让路（rc=3），
    不许把出厂那 61 行全判成产品坏了。挑一把已知活的键当对照，而不是猜。"""
    fw = APP.focusWidget()
    if fw is not None and fw is not win:
        fw.clearFocus()
    win.activateWindow()
    win.setFocus(Qt.OtherFocusReason)
    pump(0.2)
    got = press_seq(win, "Z")
    return bool(got), (f"按 Z 命中={got}（应为 ['mute']）"
                       f"；此刻前台窗口={_foreground_debug()} 我们活动={win.isActiveWindow()}")


# ---------------------------------------------------------------- 按键判读（纯函数）
# 这一段判的全是"按一次键之后收集到的东西对不对"，不需要屏幕、也不需要真窗口：
# 把命中记录喂给下面这几个纯谓词就能验。拎出来的理由和 keymap_covered 一样——
# --falsify 要能直接喂已知坏样本（命中 0 次 / 命中 2 次 / 命中别家 / 条数对不上表 /
# 死写法探针按响了），而这些形状在真窗口上**造不出来**：造一条孤儿 QShortcut 就得先把
# 生产代码写坏。判据留在 main() 里内联的话，这一整段就是"跑通了就算对"，恒真与否没人知道。
def combine(*verdicts: tuple[bool, str]) -> tuple[bool, str]:
    """几条判读并成一条：全过才算过，详情按顺序拼起来（红了能一眼看出挂在哪半条）。"""
    return all(bool(v[0]) for v in verdicts), " ｜ ".join(str(v[1]) for v in verdicts)


def counts_agree(*pairs: tuple[str, int]) -> tuple[bool, str]:
    """条数一致性：几个「名字=数值」必须全相等。

    孤儿 QShortcut 唯一的可见形状就是"装的条数 ≠ 表行数"：多出来那条挂在窗口上，看不见、
    按得响，用户那边只会说"我把这行删了它还在响应"。而"表行数 ≠ 出厂行数"是另一码事
    （折叠没生效 / 配置没落地），所以名字带进详情，红了能分清是哪一组不对。
    """
    vals = {int(v) for _n, v in pairs}
    txt = " ".join(f"{n}={int(v)}" for n, v in pairs)
    return (len(vals) <= 1), (txt if len(vals) <= 1 else f"条数不一致：{txt}")


def hit_once(got, want: str, installed: int | None = None, rows: int | None = None,
             default_rows: int | None = None) -> tuple[bool, str]:
    """按一次键的判读：恰好命中 want **一次**；给了条数就一并核对装的 = 表的（= 出厂的）。

    三种坏各拦一半：命中 0 次（死写法、或那把键根本没装上）、命中 2 次（重装时旧快捷键
    没摘干净，或同一把键在产品表里留了两行）、命中别家（撞键）、条数对不上表（孤儿
    QShortcut 还挂在窗口上，看得见摸不着）。
    """
    got = list(got)
    probs: list[str] = []
    if got != [want]:
        probs.append(f"命中={got}，应为 [{want!r}] 恰好一次")
    counts = [("装", int(installed)), ("表", int(rows))] if installed is not None and rows is not None else []
    if default_rows is not None and rows is not None:
        counts = counts + [("出厂", int(default_rows))]
    if counts:
        ok_c, d_c = counts_agree(*counts)
        if not ok_c:
            probs.append(d_c)
    if probs:
        return False, "；".join(probs)
    tail = ("，条数一致（" + "/".join(f"{n}={v}" for n, v in counts) + "）") if counts else ""
    return True, f"命中={got} 一次{tail}"


def no_hit(got, act: str) -> tuple[bool, str]:
    """这把键**不该**触发 act：删掉了那行、或者写的是按不出的死写法。"""
    return (act not in list(got)), f"命中={list(got)}（里面不该有 {act!r}）"


def table_has(rows, keep=None, drop=None) -> tuple[bool, str]:
    """装出去的表里该有哪条、不该有哪条（别名折叠、删行、改绑三处都靠它）。

    `rows` 既收 (键, 动作, 说明) 也收 (键, 动作)——真窗口上 `win.key_rows` 是三元组，
    证伪样本里懒得写说明，所以取前两格归一化。
    """
    pairs = {(r[0], r[1]) for r in rows}
    probs: list[str] = []
    if keep is not None and tuple(keep) not in pairs:
        probs.append(f"表里没有装出去的 {tuple(keep)}")
    if drop is not None and tuple(drop) in pairs:
        probs.append(f"表里还留着该被折掉/删掉的 {tuple(drop)}")
    said = " ".join(x for x in (f"含{tuple(keep)}" if keep is not None else "",
                                f"不含{tuple(drop)}" if drop is not None else "") if x)
    return (not probs), ("；".join(probs) or f"表 {len(pairs)} 条，{said}")


def rows_all_hit(rows, hit_of) -> tuple[bool, str]:
    """逐条真按的判读：每行 (键, 动作) 按下去**恰好**命中它自己那一条。

    `hit_of(键)` 回那次按键记录到的动作列表。坏法三种都进 misses：0 次（死写法）、
    2 次（旧的没摘）、命中的是别家（撞键）。
    """
    misses = key_misses(rows, hit_of)
    return (not misses), (f"不对的={misses}（共 {len(list(rows))} 行）" if misses
                          else f"{len(list(rows))} 行逐条各命中自己一次")


def key_misses(rows, hit_of) -> list[tuple[str, str, list[str]]]:
    """rows_all_hit 的取证半段：把"按这把键没恰好命中它自己那一条"的行原样列出来。

    单拆出来是因为半途判废要用它当输入（先重按这些行、再拿对照键分流），
    而"哪些行不合规"这个动作只该有一份实现——两处各写一遍迟早会漂成两套口径。
    """
    out: list[tuple[str, str, list[str]]] = []
    for r in rows:
        seq, act = r[0], r[1]
        got = list(hit_of(seq))
        if got != [act]:
            out.append((seq, act, got))
    return out


def red_route(lost: bool, ok: bool, re_ok: bool | None = None) -> str:
    """按键段"红先问通道"的控制流（纯函数，U48d 拿已知答案钉）：回 ENV / ASK / CHECK。

    · `lost` 已判废 ⇒ "ENV"：同一个环境问题重复记成产品红没有信息量。
    · 判据本身过 ⇒ "CHECK"：照常记账（不许顺手重按对照键——那会给绿光加一条无关前提）。
    · 红了、通道还没判废、且**还没问过** ⇒ "ASK"：调用方当场重按对照键再回来。
    · 问过且对照键整批不响（`re_ok is False`）⇒ "ENV"：这条红的证据本来就不成立。
    · 问过且对照键响（`re_ok is True`）⇒ "CHECK"：真坏，照常 FAIL。
    写成三家而不是两家，是为了让"忘了问"这一形状可被钉住：re_ok=None 时绝不许直接判 ENV
    或 FAIL（那样判据就又变成"跑通了就算对"）。
    """
    if lost:
        return "ENV"
    if ok:
        return "CHECK"
    return "CHECK" if re_ok else ("ASK" if re_ok is None else "ENV")


CONTROL_KEY = ("Z", "mute")        # 已知活的对照键：和 keyboard_focus_ok 用的是同一把


def channel_split(misses, control_got, total: int,
                  control_act: str = CONTROL_KEY[1]) -> tuple[str, str]:
    """按键段收尾分流：**不响的行**归"环境"还是归"产品"。

    三家分开写，是因为上一轮那八条红里七条是同一个成因，而它们长得一模一样：
      · 没有不响的行 → "PASS"。
      · 对照键按下**什么都没命中** → "ENV"：QShortcut(context=WindowShortcut) 只在它自己的
        窗口是**当前活动窗口**时才响应，前台半途被别的进程抢走时，一批行会一起变成按下去
        没反应。这形状和"死写法"完全同形，但写成 FAIL 会引着人去改本来对的键位表。
      · 对照键命中了**任何东西**（哪怕不是它自己那一条）→ "FAIL"：事件确实进了窗口，
        通道是通的，还不响的那几行就是真坏（死写法 / 旧键没摘 / 撞键）。
        这一档故意不给 ENV——"响的是别家"说明的是键位表配错了，不是系统没给键盘。
    对照键的证据取"当场重按那一次"，不取循环开始前的闸门——闸门证明的是起跑时通道是通的，
    判这些行不作数需要的是**此刻**通不通。
    """
    if not misses:
        return "PASS", (f"{int(total)} 行逐条各命中自己一次"
                        f"（收尾再按一次对照键 {CONTROL_KEY[0]}：命中={list(control_got)}）")
    got = list(control_got)
    if not got:
        return "ENV", (f"不响的行={misses}；但此刻连对照键 {CONTROL_KEY[0]} 按下去命中={got}"
                       f"（应为 [{control_act!r}]）⇒ 按键通道被外来窗口抢走，"
                       f"这批 misses 是'眼睛被挡住'不是'键是死的'，本轮不作数")
    note = ("" if control_act in got else
            f"（注意：对照键响的是 {got} 而不是 {control_act!r}，那是键位配置被改过，"
            f"不是通道不通）")
    return "FAIL", (f"不对的={misses}（共 {len(list(misses))} 行）"
                    f"——对照键 {CONTROL_KEY[0]} 此刻命中={got}，事件进得来，"
                    f"这些行是真坏：0 次=死写法，2 次=旧快捷键没摘干净，命中别家=撞键"
                    f"{note}")


_DEAD_TAG = "probe_from_dead_spelling"
_ALIVE_TAG = "probe_from_alive_spelling"


def calibration_verdict(freed, got_dead, got_alive, refused,
                        dead_tag: str = _DEAD_TAG, alive_tag: str = _ALIVE_TAG) -> tuple[bool, str]:
    """校准段判读：这条判据到底看不看得见"上一版那 5 条死键"那一类坏。

    四条各守一个坑，少任何一条这段就是摆设：
      · `freed` 必须空——这把键没腾空的话，量到的是"同键值两条互沉默"（探针 4 步骤 F）
        而不是"死写法不响"，上一轮就是这么把校准段判成 FAIL 的；
      · 死写法探针按下去不许响——按了才说明检测器真能看见这一类；
      · 活写法探针必须响——不响说明按键通路本身不通，前面两条的"不响"全无意义；
      · 递死串必须被当场拒绝——探针 4 实测 `QTest.keyClick(0x01ffffff)` 把进程带走。
    """
    probs: list[str] = []
    if list(freed):
        probs.append(f"腾空以后按下仍命中 {list(freed)}：这把键没腾空，量的不是死写法")
    if dead_tag in list(got_dead):
        probs.append("死写法探针按响了：检测器看不见这一类坏")
    if list(got_alive) != [alive_tag]:
        probs.append(f"活写法探针没响（按下={list(got_alive)}）：按键通路不通，校准不作数")
    if not refused:
        probs.append("死串没被拒递：真递下去整轮进程没了")
    return (not probs), ("；".join(probs)
                         or f"腾空={list(freed)} 死写法={list(got_dead)} 活写法={list(got_alive)} "
                            f"拒递={refused}")


# ---------------------------------------------------------------- QSS 判读
def qss_applies(sheet: str, fill: str) -> bool:
    """正向验证样式表真的解析生效：#Primary 按钮渲染出来的底色 ≈ fill。

    传的 fill 是 **theme.PRIMARY_FILL**（暖象牙白——主按钮靠"亮"而不是"绿"做主操作），
    不是标识色 ACCENT，也不是 ACCENT_FILL：主色只留给状态（选中、进度、开关），
    主按钮的底色是另一份令牌。拿主色来比会在每个主题色上都差过容差。

    不拿"有没有报警告"当判据——坏 QSS 到底吐不吐警告是 Qt 的事（--falsify U4 专门
    验证这条颜色判据能否掉坏表），用户能感知的只有"规则吃没吃进去"。
    """
    # styleSheet 只有实例版（QApplication.styleSheet() 是未绑定方法，直接调会 TypeError）
    old = APP.styleSheet()
    APP.setStyleSheet(sheet)
    try:
        b = QPushButton("x")
        b.setObjectName("Primary")
        b.resize(120, 34)
        b.ensurePolished()
        pm = b.grab()
        APP.setStyleSheet(old)
        img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
        want = QColor(fill)
        hit = tot = 0
        for j in range(0, img.height(), 2):
            for i in range(0, img.width(), 2):
                c = img.pixelColor(i, j)
                tot += 1
                if (abs(c.red() - want.red()) < 26 and abs(c.green() - want.green()) < 26
                        and abs(c.blue() - want.blue()) < 26):
                    hit += 1
        return tot > 0 and hit / tot > 0.5
    finally:
        APP.setStyleSheet(old)


def primary_ink(sheet: str) -> QColor | None:
    """#Primary 按钮画出来**占比最大的不透明色**——拿它算字与底的对比度。

    为什么不复用 qss_applies 的比值当这一条的判据：那条比的是"和代码里那份 fill 一致"，
    要是有人把 background 从象牙换了、期望值跟着代码走，它照样绿。这一条不认代码，
    只认"墨色字压在这块实际颜色上够不够 4.5:1"——底色被换成主色绿会当场红
    （#4E705B 对深墨只有 ~3.4:1，--falsify U4b 就是注入这一条坏改动来证明尺子会咬）。
    """
    old = APP.styleSheet()
    APP.setStyleSheet(sheet)
    try:
        b = QPushButton("x")
        b.setObjectName("Primary")
        b.resize(120, 34)
        b.ensurePolished()
        img = b.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        buckets: dict[tuple[int, int, int], int] = {}
        for j in range(2, img.height() - 2, 3):
            for i in range(2, img.width() - 2, 3):
                c = img.pixelColor(i, j)
                if c.alpha() < 200:
                    continue
                k = (c.red() >> 3, c.green() >> 3, c.blue() >> 3)
                buckets[k] = buckets.get(k, 0) + 1
        if not buckets:
            return None
        (r, g, bl), _n = max(buckets.items(), key=lambda kv: kv[1])
        return QColor(r << 3, g << 3, bl << 3)
    finally:
        APP.setStyleSheet(old)


def _render_widget(w: QWidget) -> QImage:
    w.ensurePolished()
    return w.grab().toImage().convertToFormat(QImage.Format_ARGB32)


SWITCH_TRACK_RATIO = 1.4      # 轨道必须是"横胶囊"：16×16 那种方框量出来是 1.00
KNOB_AREA_MIN = 100           # 把手的逻辑面积下限（18 直径的圆 ≈ 254，丢了/被压扁就掉到几十以下）
KNOB_MID_MIN = 60             # 禁用档要看见的"中灰墨"下限（四个 12px 汉字的一撇一捺远多于这个数）


def switch_px(sheet: str, checked: bool, enabled: bool = True,
              text: str = "") -> dict:
    """量一颗开关画出来的样子，返回把手/轨道的几何与均色（**一律逻辑像素**）。

    为什么量像素而不是读样式表里那句 `image: url(...)`：写了不等于吃进去了。url 里的
    路径带空格会被截断、文件不在会被跳过、两态误用同一张图、Qt 换版本改了子控件画法也会
    静默不画——这几种在字符串上长得一模一样，只有"把手在哪、多大、什么色"能分辨。

    为什么每条读数都除以 dpr：`grab()` 给的是**设备像素**，本机真窗口 dpr=2.0、离屏装配
    dpr=1.0，绝对像素数跨装配不可比（V1 那把滑轨尺就是这么被量具自己骗过一次：真窗口把
    14×14 读成 28×28 判红）。面积要除 dpr²。判据因此全部落在"比例／相对位置／面积下限"上。

    把手取近白（min(r,g,b)≥190）：轨道两档（BG_HOVER 与派生 fill）和描边都过不了这条线，
    所以近白只可能来自把手本身。中灰档（90≤min<190）单独计数，是给"禁用时字要变暗"那条用的
    ——未勾选档的轨道与描边都落在 <90 的暗档，所以中灰墨几乎只可能来自被压暗的文字。
    """
    old = APP.styleSheet()
    APP.setStyleSheet(sheet)
    try:
        c = QCheckBox(text)
        c.setChecked(checked)
        c.setEnabled(enabled)
        c.ensurePolished()
        c.adjustSize()
        img = _render_widget(c)
        dpr = img.devicePixelRatio() or 1.0
        knob: list[tuple[int, int]] = []
        track: list[tuple[int, int]] = []
        knob_c: list[tuple[int, int, int]] = []
        track_c: list[tuple[int, int, int]] = []
        mid = 0
        for j in range(img.height()):
            for i in range(img.width()):
                col = img.pixelColor(i, j)
                if col.alpha() < 200:
                    continue
                m = min(col.red(), col.green(), col.blue())
                if m >= 190:
                    knob.append((i, j))
                    knob_c.append((col.red(), col.green(), col.blue()))
                else:
                    track.append((i, j))
                    track_c.append((col.red(), col.green(), col.blue()))
                    if m >= 90:
                        mid += 1
        out = {"knob_area": len(knob) / (dpr * dpr), "track_area": len(track) / (dpr * dpr),
               "mid_area": mid / (dpr * dpr), "dpr": dpr}
        # 几何一律先在**设备像素**里算完（同一坐标系才可比），最后一步才除 dpr 换成逻辑值。
        # 混着算是最容易骗过自己的一种写法：把手中心用设备坐标、轨道左沿用逻辑坐标，
        # 相对位置在 dpr=2 的装配上会直接算出负数或 1.5，看着像"判据抓得很准"。
        box = {}
        for name, pts in (("knob", knob), ("track", track)):
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            box[name] = (min(xs), max(xs), min(ys), max(ys))
        if "knob" in box:
            x0, x1, y0, y1 = box["knob"]
            out["knob_w"] = (x1 - x0 + 1) / dpr
            out["knob_h"] = (y1 - y0 + 1) / dpr
            out["knob_cx"] = (x0 + x1 + 1) / 2.0
            out["knob_hex"] = _mean_hex(knob_c)
        if "track" in box:
            tx0, tx1, ty0, ty1 = box["track"]
            out["track_w"] = (tx1 - tx0 + 1) / dpr
            out["track_h"] = (ty1 - ty0 + 1) / dpr
            out["track_hex"] = _mean_hex(track_c)
            if "knob" in box:
                kx0, kx1, ky0, ky1 = box["knob"]
                span = tx1 + 1 - tx0
                out["knob_rel"] = ((kx0 + kx1 + 1) / 2.0 - tx0) / span if span else 0.0
                # 出框：把手有任何一粒落在轨道外接框之外（画布缩放/居中一旦不对就会戳出去）
                out["out"] = sum(1 for i, j in knob
                                 if not (tx0 + 1 <= i <= tx1 - 1 and ty0 + 1 <= j <= ty1 - 1))
        return out
    finally:
        APP.setStyleSheet(old)


def _mean_hex(pixels: list[tuple[int, int, int]]) -> str:
    n = len(pixels)
    c = QColor(round(sum(p[0] for p in pixels) / n), round(sum(p[1] for p in pixels) / n),
               round(sum(p[2] for p in pixels) / n))
    return c.name()


def switch_ok(m: dict, want_right: bool) -> tuple[bool, str]:
    """开关该长什么样：横胶囊轨道＋一颗近正圆的把手，关在左、开在右，把手压得住轨道。

    `want_right` 是"这一态把手该在哪一边"——判据不许从被检的那张样式表里抄期望值
    （从 `checked` 那条规则反推"该在右边"，等于让样式表给自己判卷）。
    """
    bad = []
    if m.get("knob_area", 0) < KNOB_AREA_MIN:
        bad.append(f"把手没了（面积={m.get('knob_area', 0):.0f}<{KNOB_AREA_MIN}）")
    if m.get("track_area", 0) < 2 * KNOB_AREA_MIN:
        bad.append(f"轨道没了（面积={m.get('track_area', 0):.0f}）")
    tw, th = m.get("track_w", 0), m.get("track_h", 1)
    if tw / th < SWITCH_TRACK_RATIO:
        bad.append(f"轨道不是横胶囊（{tw:.0f}x{th:.0f}={tw / th:.2f}<{SWITCH_TRACK_RATIO}）")
    kw, kh = m.get("knob_w", 0), m.get("knob_h", 1)
    if not kw or abs(kw / kh - 1.0) > 0.12:
        bad.append(f"把手不是近正圆（{kw:.0f}x{kh:.0f}={kw / kh:.2f}）")
    if m.get("out", 0):
        bad.append(f"把手戳到轨道外（{m['out']} 粒）")
    rel = m.get("knob_rel", -1)
    if want_right and rel < 0.60:
        bad.append(f"开着但把手不在右边（轨道内相对位置={rel:.2f}）")
    if not want_right and rel > 0.40:
        bad.append(f"关着但把手不在左边（轨道内相对位置={rel:.2f}）")
    ct = 0.0
    if m.get("knob_hex") and m.get("track_hex"):
        ct = theme.contrast(m["knob_hex"], m["track_hex"])
        if ct < 3.0:
            bad.append(f"把手对轨道只有 {ct:.2f}:1（WCAG 1.4.11 要 ≥3:1）")
    return (not bad), ("、".join(bad) or
                       f"轨道 {tw:.0f}x{th:.0f} 把手 {kw:.0f}x{kh:.0f} "
                       f"位置={rel:.2f} 对比={ct:.2f}:1")


def groove_px(sheet: str, obj: str, value: int = 50) -> tuple[int, int]:
    """一根横向滑轨画出来之后：(主色像素数, 中性槽像素数)。obj 传 "" 走通用规则、
    传 "BipSlider" 走双极那一档。

    这条尺子要判的是"值为 0 的双极项有没有被画成已经调了一半"。为什么不读 QSS：
    `QSlider#BipSlider::sub-page` 那条规则写没写、选择器名和 setObjectName 拼没拼上，
    都是纸面事；真的看一眼"滑块左边那半根是什么颜色"才算数。
    所以它天生成对：通用滑轨在 50% 处该有主色（>0），双极滑轨该一点主色都没有（=0）。
    """
    old = APP.styleSheet()
    APP.setStyleSheet(sheet)
    try:
        s = QSlider(Qt.Horizontal)
        if obj:
            s.setObjectName(obj)
        s.setRange(0, 100)
        s.setValue(value)
        s.setFixedSize(200, 22)
        img = _render_widget(s)
        want = QColor(theme.ACCENT)
        neutral = QColor(theme.BORDER)
        acc = neu = 0
        for j in range(img.height()):
            for i in range(img.width()):
                c = img.pixelColor(i, j)
                if c.alpha() < 200:
                    continue
                if (abs(c.red() - want.red()) < 34 and abs(c.green() - want.green()) < 34
                        and abs(c.blue() - want.blue()) < 34):
                    acc += 1
                elif (abs(c.red() - neutral.red()) < 12 and abs(c.green() - neutral.green()) < 12
                        and abs(c.blue() - neutral.blue()) < 12):
                    neu += 1
        return acc, neu
    finally:
        APP.setStyleSheet(old)


def handle_px(sheet: str) -> tuple[int, int]:
    """滑轨把手画出来之后的外接框（**逻辑像素**，已除掉 dpr）；一个都画不出返回 (0, 0)。

    为什么必须落到像素上（而不是读 QSS 里那三个数）：Qt 画横向把手时**不看 `height`**——
    实测 height 12 与 22 在同一份 margin 下画出同一个 12×4，真正决定高度的是
    "groove 高 + 2×|上下 margin|"。所以"写着 height:14 就是 14 高"是错的，
    上一版就是这样写出一个 12 宽 × 14 高的竖圆角方块（本意是正圆）。

    为什么要除 dpr：grab() 出来的是设备像素。真窗口 dpr=2.0 时同一颗圆量到 28×28，
    离屏 dpr=1.0 量到 14×14——不换算的话这条尺子会在真窗口上判"太大"（第一次真跑就红在这里，
    量的是量具而不是产品）。换算成逻辑像素之后两种装配读同一个数。

    近白阈值取 min(r,g,b)>=190：把手底色是 TEXT(#E8EAF0，最小通道 232)，而中性槽是 BORDER
    (#33363D)、已过的一段是主色（六档主色最小通道最大的也只有 0x8D=141），两边都够不着这条线。
    """
    old = APP.styleSheet()
    APP.setStyleSheet(sheet)
    try:
        s = QSlider(Qt.Horizontal)
        s.setRange(0, 100)
        s.setValue(50)
        s.setFixedSize(200, 22)
        img = _render_widget(s)
        pts = [(i, j) for j in range(img.height()) for i in range(img.width())
               if (lambda c: c.alpha() >= 200 and min(c.red(), c.green(), c.blue()) >= 190)(
                   img.pixelColor(i, j))]
        if not pts:
            return (0, 0)
        dpr = img.devicePixelRatio()
        if dpr <= 0:
            dpr = s.devicePixelRatioF() or 1.0
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (round((max(xs) - min(xs) + 1) / dpr), round((max(ys) - min(ys) + 1) / dpr))
    finally:
        APP.setStyleSheet(old)


def handle_round_ok(w: int, h: int, want: int = HANDLE_DIAMETER) -> tuple[bool, str]:
    """把手是不是一颗正圆：宽高相等、且等于样式表声明的那一直径（逻辑像素）。

    把 want 也判上是因为"两边都是 8"同样能凑出 ratio=1：只有先确认画出来的就是声明的那 14，
    这条尺子才不会放过"规则整条丢失、回到 Fusion 默认小把手"（实测摘掉规则后近白像素=0），
    也不会放过"滑轨控件比把手矮、上下被裁平"（实测控件高 10 时是 14×10）。
    """
    if w == 0 or h == 0:
        return (False, "把手一个近白像素都没画出来（规则丢了？）")
    ratio = w / h
    return (abs(ratio - 1.0) <= 0.08 and w == want and h == want,
            f"实测 {w}×{h}（逻辑像素），宽高比={ratio:.2f}"
            f"（需 1.00±0.08 且两边都等于 theme.SLIDER_HANDLE={want}）")


def sidebar_ink_ok(rows: list[tuple[bool, str, bool]]) -> tuple[bool, str]:
    """侧栏"当前项"的颜色尺子。rows = [(是不是当前行, 前景十六进制, 粗体)]。

    为什么判据不能只写 `前景 == theme.ACCENT_TEXT`：那等于让代码自己出题自己答——
    三种主色角色（原色/填充/文字）只要被顺手合并回一种，令牌跟着变，判据照样绿。
    所以这里加一条**不读代码**的腿：当前行的前景压在选中行实际底色（BG_ACTIVE，见
    QSS 的 `QListWidget::item:selected`）上必须 ≥4.5:1。主色原色压在这块底上实测只有
    3.7:1（紫档更差），这一腿正是为那种改法准备的。
    第三条腿防"整列都涂主色"：非当前行必须还是普通文字色，粗体只出现在当前行。
    """
    idx = [i for i, (cur, _c, _b) in enumerate(rows) if cur]
    if len(idx) != 1:
        return False, f"标成当前的行数={len(idx)}（应为 1）"
    cur_color = rows[idx[0]][1]
    c = theme.contrast(cur_color, theme.BG_ACTIVE)
    # 其余行按"是不是当前行"排除，不是按下标 0 排除——当前项不一定排在第一行（续播/切歌都在中间）
    others = {r[1] for i, r in enumerate(rows) if i != idx[0]}
    bolds = [i for i, (_c, _col, b) in enumerate(rows) if b]
    ok = (cur_color.upper() == theme.ACCENT_TEXT.upper() and c >= theme.TEXT_MIN
          and others == {theme.TEXT.upper()} and bolds == idx)
    return ok, (f"当前行={idx[0]} 前景={cur_color} 压在 BG_ACTIVE 上 {c:.2f}:1"
                f"（需≥{theme.TEXT_MIN}） 其余色={sorted(others)} 粗体行={bolds}")


def title_fits(bar, full: str) -> tuple[bool, str]:
    """窄窗口里的长片名：画出来那串必须**被裁过且留下线索**，整句必须在 tooltip 里。

    尺子读的是三个现量：标签上的字（省略版）、tooltip（整句）、以及标签自己的实际可用宽度
    （DragRegion 宽减左右 12 边距）。为什么不读 `bar._doc`：那是产品自己的存货，
    判"产品知不知道全文"要问的是**用户能不能拿到全文**，也就是 tooltip。
    两头各防一种坏：只判"含 …"会放过"裁错了把扩展名裁没还自洽"的情况；
    只判宽度会放过"根本没裁、断在半截"的旧写法（那正是 title_bar._elide 修掉的行为）。
    """
    txt = bar.lbl_doc.text()
    fm = bar.lbl_doc.fontMetrics()
    avail = max(0, bar.drag.width() - 24)
    need = fm.horizontalAdvance(full)
    fits = need <= avail
    ok = (txt == full if fits else
          ("…" in txt and bar.lbl_doc.toolTip() == full
           and fm.horizontalAdvance(txt) <= avail + 1))
    return ok, (f"整句需 {need}px、可用 {avail}px、画出来 {fm.horizontalAdvance(txt)}px"
                f"（{txt!r}） tooltip={'有整句' if bar.lbl_doc.toolTip() == full else '不是整句'}")


def crash_probe(marker: str) -> tuple[bool, bool]:
    """装崩溃钩子 → 故意抛一条只含 marker 的异常 → 看盘上有没有留下它。

    返回 (日志里新出现了 marker, marker 在抛之前就已经在日志里)。第二个值是否决
    "文件里本来就有这句话"这种空判据用的。
    """
    path = install_crash_log()
    try:
        before = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    except Exception:
        before = ""
    try:
        raise ValueError(marker)
    except ValueError:
        sys.excepthook(*sys.exc_info())      # 输出里那条故意的 traceback 就是它，不是失败
    try:
        after = open(path, encoding="utf-8").read()
    except Exception:
        after = before
    return (marker in after and len(after) > len(before)), (marker in before)


INK_SOLID_MIN = 30      # 少于这么多实心字像素＝量具瞎了（离屏平台下 grab 全是透明）
TIER_GAP_MIN = 0.12     # 主档与次档的字芯亮度差下限（实测分开 0.248/0.314，糊在一起是 0.000）


def label_ink(w: QWidget):
    """一个标签画出来的"字芯"色与亮度；读不到实心字就回 **None（量具瞎了，不是"字很暗"）**。

    只认 alpha==255 的像素、取其中最亮的一撮（前 5%）求均值。为什么不照搬"剔掉最暗的
    背景、看剩下"那把尺（第一版就是这么写的）：单独 `grab()` 一个 QLabel 拿到的图
    **没有背景**——底是全透明，实心像素全是字，于是"最暗的实心像素"是字自己的边缘，
    `floor+40` 那条相对阈值把整撮字全判掉了，日志里印出"没画上字"这种
    **结论错、归因也错**的话（明明有 782 个不透明像素）。2026-10-12 实测：
    windows 平台能读到 #e8eaf0/#a6abb8 两档实心字色，同一份代码
    `QT_QPA_PLATFORM=offscreen` 下读到的是一张全透明的图。
    """
    img = _render_widget(w)
    px = []
    for j in range(img.height()):
        for i in range(img.width()):
            c = img.pixelColor(i, j)
            if c.alpha() == 255:
                px.append((0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue(), c))
    if len(px) < INK_SOLID_MIN:
        return None
    px.sort(key=lambda p: -p[0])
    top = px[: max(1, int(len(px) * 0.05))]
    n = len(top)
    r = round(sum(p[1].red() for p in top) / n)
    g = round(sum(p[1].green() for p in top) / n)
    b = round(sum(p[1].blue() for p in top) / n)
    return (f"#{r:02x}{g:02x}{b:02x}", round((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0, 3), n)


def panel_bg_px(w: QWidget):
    """一块面板画出来的**底色**：渲染图上出现最多的那个实心色。

    判对比要用屏幕上那块底，不能用 `theme.BG_RAISE`——后者是"我以为的底"，
    样式表里被后面的规则盖掉、或者那块地方其实铺的是悬停底，令牌的数都对不上。
    """
    img = _render_widget(w)
    cnt: dict[str, int] = {}
    for j in range(0, img.height(), 3):
        for i in range(0, img.width(), 3):
            c = img.pixelColor(i, j)
            if c.alpha() == 255:
                cnt[c.name()] = cnt.get(c.name(), 0) + 1
    return max(cnt, key=cnt.get) if cnt else None


def tier_px(sheet: str) -> dict:
    """在给定样式表下把三档文字各画一遍，返回每档的字芯＋那块面板底（全像素）。

    三档都走产品自己的工厂（`mk_label` / `mk_hint`），不在判据里手写 objectName：
    "默认档是什么"这件事归工厂管，判据只管画出来分不分得开。
    """
    old = APP.styleSheet()
    APP.setStyleSheet(sheet)
    try:
        box = QWidget()
        box.setObjectName("Panel")
        lay = QVBoxLayout(box)
        ws = {"行标题": form_rows.mk_label("充满画面", box),
              "说明行": form_rows.mk_hint("R 旋转 · Shift+R 比例 · H 镜像", box),
              "状态读数": form_rows.mk_label("滤镜链：format、hflip", box, "Status")}
        for w in ws.values():
            lay.addWidget(w)
        box.ensurePolished()
        box.adjustSize()
        box.resize(300, box.sizeHint().height())
        APP.processEvents()
        out = {k: label_ink(w) for k, w in ws.items()}
        out["底"] = panel_bg_px(box)
        return out
    finally:
        APP.setStyleSheet(old)


def tier_ok(r: dict) -> tuple[bool, str]:
    """层级必须**在像素上**存在，而且每一档都得压得住那块底。

    判的是两条主张，不判"三档三个色"：说明行与状态读数按设计同色不同号
    （12px 带字距 vs 13px），要它们三色各异等于把设计判成 bug。
    """
    keys = ("行标题", "说明行", "状态读数")
    blind = [k for k in keys if r.get(k) is None]
    if blind or not r.get("底"):
        return (False, f"量具瞎了：{blind or '读不到面板底'}（离屏装配下 grab 不出字，"
                       f"这条读数不作数，别当产品红看）")
    bg = r["底"]
    t, d, s = (r[k][1] for k in keys)
    bad = []
    if t - d < TIER_GAP_MIN:
        bad.append(f"行标题没比说明行亮（{t:.3f}-{d:.3f}<{TIER_GAP_MIN}）")
    if t - s < TIER_GAP_MIN:
        bad.append(f"行标题没比状态读数亮（{t:.3f}-{s:.3f}<{TIER_GAP_MIN}）")
    for k in keys:
        ct = theme.contrast(r[k][0], bg)
        if ct < 4.5:
            bad.append(f"{k} {r[k][0]} 压在 {bg} 上只有 {ct:.2f}:1（正文小字需 ≥4.5）")
    return (not bad), (f"行标题 {r['行标题'][0]}/{t:.3f} 说明行 {r['说明行'][0]}/{d:.3f} "
                       f"状态读数 {r['状态读数'][0]}/{s:.3f} 底={bg} "
                       + ("；".join(bad) if bad else "三档都达标"))


def ink_box(w: QWidget):
    """实心字的**外接框**（设备像素宽高）——比 `width()` 诚实：包装不下的时候
    控件宽是布局给的，字有多宽只有像素知道。读不到字回 None（同 `label_ink` 的口径）。"""
    img = _render_widget(w)
    xs, ys = [], []
    for j in range(img.height()):
        for i in range(img.width()):
            if img.pixelColor(i, j).alpha() == 255:
                xs.append(i)
                ys.append(j)
    if len(xs) < INK_SOLID_MIN:
        return None
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def pills_ok(panel: QWidget) -> tuple[bool, str]:
    """一页里那些行尾读数：等宽、右缘齐、拖到量程两端不许变宽。

    为什么"等宽"要钉：这一列的用途是**扫读**——数值列左缘参差时眼睛得逐行找数；
    而 Qt 的布局默认按每行自己的 sizeHint 给宽，`setMinimumWidth(58)` 只是兜住窄的那头，
    将来表里加一项读数比 58 宽（比如 ±240.0s），那一行就会顶出来，而其它行一动不动。
    为什么不许"拖动途中变宽"：滑轨正被手指拖着，读数一变宽整行重排，标题和滑轨跟着跳。
    全部用逻辑像素（Qt 的控件几何本来就是逻辑单位；只有 grab() 是设备像素，别混）。
    """
    rows = [r for r in panel.findChildren(form_rows.NumRow) if r.isVisibleTo(panel)]
    if not rows:
        return (False, "这一页一个滑轨都没找到（判据看不到东西）")
    dpr = panel.window().devicePixelRatioF() or 1.0
    widths, edges, moved = {}, {}, []
    for r in rows:
        w0 = int(r.val.width())
        widths[w0] = widths.get(w0, 0) + 1
        x = r.val.mapTo(panel, QPoint(0, 0)).x()
        key = x + w0
        edges[key] = edges.get(key, 0) + 1
        saved = r.value_of(r.slider.value())
        for v in (r.spec.lo, r.spec.hi, r.spec.default):
            r.show(v)
            if int(r.val.width()) != w0:
                moved.append(f"{r.spec.label}@{v}→{int(r.val.width())}（基线 {w0}）")
        r.show(saved)
    bad = []
    if len(widths) > 1:
        bad.append(f"读数宽度不是一档：{sorted(widths)}")
    if len(edges) > 1:
        bad.append(f"右缘不齐（逻辑像素）：{sorted(edges)}")
    if moved:
        bad.append(f"拖动途中会变宽：{moved[:4]}")
    return (not bad), (f"{len(rows)} 行 宽度={sorted(widths)} 右缘={sorted(edges)} "
                       + ("；".join(bad) if bad else "等宽、齐右、全程不变形"))


def chips_agree(dlg) -> tuple[bool, str]:
    """设置页每行行尾的「生效时机」标注对不对——多标、少标、标错都算错。

    期望值在这里**独立重算**，不从产品的 `_row_effect` 借：借来的话规则一改判据跟着改，
    就没人守着"屏幕上到底说没说"。三条主张：
      ① 整节同一档 ⇒ 由节标题说一次，行尾不再重复（旧写法五枚实心胶囊同一句话，
         视觉重量和下拉、按钮一样，用户扫一眼以为那是五个不同状态）；
      ② 「立即生效」是默认档 ⇒ 不标（它是用户对设置的预期，写出来是噪音）；
      ③ 省略了就必须有**别处写着它**——节标题的字面里含这一档，否则"不标"就是"漏标"。

    数胶囊只能在**这一行**的容器（`dlg._rows[cfg]`）里数。往 `parentWidget()` 上方摸
    是第一版的写法，它预设"每行都有一枚"：一旦按上面两条省掉胶囊，往上就摸到邻行那枚，
    九行全被读成「下次载入生效」（2026-10-12 实测红成这样，而产品是对的）。
    """
    bad: list[str] = []
    n = 0
    for lab, items in dlg._sections:
        title = lab.text()
        uniform = len({i.effect for i in items}) == 1
        for it in items:
            node = dlg._rows.get(it.cfg)
            if node is None:
                # 判据的前提：这一行在界面上存在。缺了不许当"行尾没标"处理——
                # 量不到 ⇒ 读成空 ⇒ 空＝期望空 ⇒ 过，那正是本轮修掉的那类假绿。
                bad.append(f"{it.cfg}：_rows 里没有这一行的容器，判据看不到它")
                continue
            chips = [c.text() for c in node.findChildren(QLabel, "Chip")]
            n += len(chips)
            if uniform and it.effect in title:
                want: list[str] = []
            elif it.effect == EFFECT_NOW:
                want = []
            else:
                want = [it.effect]
            if chips != want:
                bad.append(f"{it.cfg}：框里写 {chips}，期望 {want}（表里是 {it.effect!r}，"
                           f"节标题「{title}」）")
            if not want and it.effect not in title:
                bad.append(f"{it.cfg}：行尾没标，而节标题「{title}」里也没有「{it.effect}」"
                           f"这句话（省略必须看得见理由）")
    return (not bad), f"共 {n} 枚；" + ("；".join(bad) if bad else "全对")


# ==================== R12-V6：按键提示 / 搜索框 ====================
# 判据自己带的"侧栏该说到哪几把键"清单（页面名 → (动作组, 说法)）。
# 为什么在判据里**另写一份**而不是读产品的 `lab.groups`：产品那份是"它决定说什么"，
# 判据若照抄，"画面页把比例那一段删了"这种事就没有任何地方能发现；这里这份是需求。
# 说法（"旋转""字幕延迟"）也照产品当前界面上写的那句抄一份进来当锚——它改了字，
# 这条会红，人来看一眼才知道是"措辞调整"还是"漏了动作"。
HINT_PAGES: tuple[tuple[str, tuple[tuple[tuple[str, ...], str], ...]], ...] = (
    ("画面页页脚", ((("rotate_cycle",), "旋转"),
                   (("aspect_cycle",), "比例"),
                   (("mirror_h_toggle",), "镜像"))),
    ("调节页页脚", ((("sub_delay_down", "sub_delay_up"), "字幕延迟"),
                   (("sub_size_down", "sub_size_up"), "大小"))),
    ("调节页 HDR 节", ((("tone_cycle",), "换色调映射档位"),)),
    ("调节页 A-B 节", ((("ab_a", "ab_b"), "设点"),
                      (("ab_clear",), "清除"))),
)


def hint_expect(rows, groups, sep: str = " · ") -> str:
    """**在这里重写一遍**拼接规则，不叫 `keymap.key_hint`。

    期望值必须独立算：借产品那把尺的话，"把动作查成了说明列""多键只报第一把"这类错
    会同时出现在被测和量具两侧，永远对得上。
    """
    parts = []
    for acts, phrase in groups:
        ks = []
        for seq, act, _desc in rows:
            if act in acts and seq not in ks:
                ks.append(seq)
        if ks:
            parts.append("/".join(ks) + " " + phrase)
    return sep.join(parts)


def _hint_on_screen(win, lab) -> tuple[bool, str]:
    """这一行按键提示**此刻在屏幕上露没露**——量之前先把它所在的那一页切成当前页。

    为什么必须切（2026-10-13，r13 真机一条 FAIL 教出来的）：`isVisibleTo(win)` 把祖先算进来，
    而侧栏是 QStackedWidget——非当前页整体藏着。判据跑到这里时当前页是调节页，于是
    「画面页页脚」那一行无论产品怎么写都恒判"藏着"：一条永远红的判据，红的是判据不是产品。
    U6d 那条证伪合成的是普通 QWidget（四行全直接可见），所以它从没暴露过这个前提——
    这正是"证伪用合成装配、验收走真页面"两半都必须有牙的原因。
    切完**当场还原**：这一行后面紧跟着按当前页构图的截图像素判据，不还原就是判据自己改了产品状态。
    """
    try:
        stack, p = None, lab.parentWidget()
        while p is not None:
            if isinstance(p, QStackedWidget):
                stack = p
                break
            p = p.parentWidget()
        if stack is None:
            return lab.isVisibleTo(win), ""
        page = lab
        while page.parentWidget() is not stack:
            page = page.parentWidget()
        prev = stack.currentWidget()
        if prev is not page:
            stack.setCurrentWidget(page)
            pump(0.05)
        vis = lab.isVisibleTo(win)
        if prev is not None and prev is not page:
            stack.setCurrentWidget(prev)
            pump(0.05)
        return vis, f"（切到 {type(page).__name__} 量，已还原）"
    except Exception as exc:                      # 断言自己不许崩：崩了会把整趟顶成 traceback
        return False, f"（量具异常：{type(exc).__name__}: {exc}）"


def hints_agree(win) -> tuple[bool, str]:
    """侧栏每一行按键提示：屏幕上那句必须**等于**生效表算出来的那句，且一页都不能漏。

    这一条防的是最静默的一种坏：提示行是**文案**，改键位之后它不会报错、不会崩，
    只是继续写着那把已经不响的键。用户照它按，没反应，然后以为播放器坏了。
    """
    labels = win.findChildren(form_rows.KeyHintLabel)
    if not labels:
        return (False, "一行按键提示都没找到（判据看不到东西；手抄的 mk_hint 不算，它跟着不了键位）")
    rows = keymap.effective(win.cfg.get("keymap"))
    bad: list[str] = []
    shown: list[str] = []
    for page, groups in HINT_PAGES:
        want_groups = [(tuple(a), p) for a, p in groups]
        hit = [l for l in labels if l.groups == want_groups]
        if not hit:
            bad.append(f"{page}：界面上没有说到这些动作的提示行（{want_groups}）")
            continue
        want = hint_expect(rows, want_groups)
        got = hit[0].text()
        if got != want:
            bad.append(f"{page}：屏幕上={got!r}，生效表算出来={want!r}")
        vis, note = _hint_on_screen(win, hit[0])
        if note:
            shown.append(f"{page}{note}")
        if vis != bool(want):
            bad.append(f"{page}：该有字却没有可见行（或反之）：期望可见={bool(want)}")
    extra = len(labels) - len(HINT_PAGES)
    if extra:
        bad.append(f"界面上 {len(labels)} 行提示，判据的清单只有 {len(HINT_PAGES)} 页"
                   f"（多出来的那几页没进清单＝没人核过它说的是什么）")
    detail = (f"{len(labels)} 行｜" + ("；".join(bad) if bad else "四页提示逐句等于生效表")
              + ("｜" + " ".join(shown) if shown else ""))
    return (not bad), detail


def key_tokens(rows) -> list[str]:
    """能当"手抄键名"证据的那批序列：数字排除（"1 秒一轮"里的 1 不是键名）。"""
    return sorted({s for s, _a, _d in rows if not s.isdigit()}, key=len, reverse=True)


# ---- V7：提示语出口清单（"这句会进屏幕"的那些函数）----
# 名字 → 那句文案坐在第几个参数上（`self` 不算）。
# 为什么是一张**名字表**而不是扫所有字面串：2026-10-12 实测过更宽的口径（扫全部调用参数），
# 18 条命中里 18 条全是误报——「A-B 段」「用 A-B 循环」里的 B 是 A-B 循环的点名，
# 「（Esc 取消）」是键位编辑框自己的取消防御，`strip(" ，,·")` 里的逗号是字符集。
# 那些都是**说给人听的词**，不是"替键位表转述一把键"，把它们一起管会把尺子磨到量不出东西。
# 这张表的残余盲区（换个名字的新工厂函数就看不见）由运行时那一把 `tips_agree` 封：
# 抄到界面上的那句最后一定要落到 `widget.toolTip()`，那里没有"换个函数名"可躲。
TIP_SINKS: dict[str, int] = {
    "mk_hint": 0,                # 侧栏提示行（V6）
    "setToolTip": 0,             # 裸悬停：本轮之后不该再说键名
    "bind_key_tip": 2,           # 登记的悬停：说法参数里也不许夹键名（会画成「…（空格）（Space）」）
    "bind_key_tip_parts": 2,     # 分段登记：整段结构都翻一遍
    "_tool": 2,                  # 控制条的按钮工厂
    "_head_btn": 2,              # 播放列表/媒体库的头部按钮工厂
    "_cap": 1,                   # 标题栏的按钮工厂
}
TIP_KWARGS = ("tip", "label", "text")     # 同一个参数写成关键字也得认（`tip="R 旋转"`）


def _call_name(call: ast.Call) -> str | None:
    """`mk_hint(...)`、`form_rows.mk_hint(...)`、`self._tool(...)` 三种写法都认。"""
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _name_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [c for c in ast.walk(tree) if isinstance(c, ast.Call) and _call_name(c) == name]


def _name_uses(text: str, name: str) -> tuple[int, int]:
    """tokenize 眼里这个名字被用了多少次，拆成「带着括号的那次调用」与「只是被转手的引用」。

    为什么要拆开数（V7 之前只有"次数"一个数）：`edit.textChanged.connect(edit.setToolTip)`
    是**引用**而不是调用，把它算进调用点，这把尺就会在产品最正常的一行上报假红。
    为什么要留着这个数 altogether：调用点如果比 AST 认出的多，说明中间隔了一层
    AST 看不懂的写法（动态取函数名、再包一层），那正是"量具悄悄瞎了"的形状。
    """
    lines: list[list[tokenize.TokenInfo]] = [[]]
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            continue
        if tok.type == tokenize.NEWLINE:
            lines.append([])
            continue
        lines[-1].append(tok)
    calls = refs = 0
    for ln in lines:
        words = [t.string for t in ln if t.type == tokenize.NAME]
        if "import" in words or (words and words[0] in ("def", "class")):
            continue          # 定义它的那一行、搬工具的导入：都不是"在这儿抄了一句"
        for i, t in enumerate(ln):
            if t.type != tokenize.NAME or t.string != name:
                continue
            nxt = next((x for x in ln[i + 1:] if x.type not in
                        (tokenize.NL, tokenize.COMMENT)), None)
            if nxt is not None and nxt.type == tokenize.OP and nxt.string == "(":
                calls += 1
            else:
                refs += 1
    return calls, refs


def _renamed_sinks(tree: ast.AST) -> list[str]:
    """`from .form_rows import bind_key_tip as tip` 这一类**改名导入**。

    AST 那半边只认表里的本名，一旦改了名，调用点就从天上掉下来——扫描器不会报错，
    它只是"什么都没看见"。这是量具最坏的失效方式（假绿），所以见到改名直接算一条红：
    要么用回本名，要么把这把尺一起改。
    """
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                base = a.name.split(".")[-1]
                if base in TIP_SINKS and a.asname not in (None, base):
                    out.append(f"{a.name} → {a.asname}")
    return out


def _str_of(node: ast.AST) -> str | None:
    """字面串就回它，f-string 把常量片段拼起来判（`f"R 旋转"` 也是抄），否则 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        s = "".join(v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str))
        return s or None
    return None


def _sink_literals(call: ast.Call, pos: int, deep: bool) -> list[str]:
    """这次出口调用里"写死在源码上的那句文案"；算出来的（变量、函数调用）回空列表——
    那正是本轮要的写法，不该被当成抄的。`deep` 给 `bind_key_tip_parts` 那种**结构**参数用。
    """
    out: list[str] = []
    node = call.args[pos] if len(call.args) > pos else None
    if node is None:
        for kw in call.keywords:
            if kw.arg in TIP_KWARGS:
                node = kw.value
                break
    if node is None:
        return out
    if deep:
        for n in ast.walk(node):
            s = _str_of(n) if isinstance(n, (ast.Constant, ast.JoinedStr)) else None
            if s:
                out.append(s)
        return out
    s = _str_of(node)
    return [s] if s else out


def paren_key_runs(text: str, seqs: set[str]) -> list[str]:
    """「（V）」「（Z / Shift+,）」这种**整括号都是键序列**的片段。

    为什么还要这第二条形状（第一条是"键序列紧跟汉字"）：f-string 的 `…（L）` 拼出来是
    `"循环：（L）"`，L 后面跟的是右括号而不是汉字，第一条够不着；而它正是本轮拆掉的那种抄法。
    括号里必须**每一段**都在生效表里，所以「（CPU、立刻见效）」「（nvidia-smi 的整机读数）」
    这类说明性括号不会被误抓（2026-10-12 在真窗口 53 条悬停上实测，零误报）。
    """
    out: list[str] = []
    for m in re.finditer(r"（([^（）]*)）", text):
        pieces = [p.strip() for p in re.split(r"\s*/\s*", m.group(1)) if p.strip()]
        if pieces and all(p in seqs for p in pieces):
            out.append(m.group(0))
    return out


def key_shaped(text: str, tokens: list[str]) -> str | None:
    """这句文案里有没有"像键名"的片段（两种形状之一撞上就算），回撞上的那个键名。"""
    for tok in tokens:
        # `A-B` / `A–B` 是区间名字，不是「把 B 绑给了某个动作」。注释里早就写着这类
        # 散文不该算抄，但旧正则的 `(?<![0-9A-Za-z])` 恰好放过了连字符，于是
        # 「A-B 循环区间」被读成抄了 B——这里把两种破折号都从键名前排除。
        if re.search(r"(?<![0-9A-Za-z])(?<!A-)(?<!A–)"
                     + re.escape(tok) + r"(?![0-9A-Za-z])\s*[^\x00-\x7f]", text):
            return tok
    return None


def hand_copied_hints(text: str, tokens: list[str],
                      seqs: set[str] | None = None) -> list[str]:
    """扫一份源码，找出**手抄了键名**的提示语（这些必须走 `KeyHintLabel` / `bind_key_tip`）。

    为什么不靠"看代码评审"记住这条：这类文案的坏法是静默的——抄死的那句在默认键位下
    完全正确，只有用户改了键位之后才变成谎话，而改过键位的人才会看见，验收永远看不见。
    所以把"不许抄"变成一条机器规矩。

    为什么走 AST 而不是拿正则扫源码字面（第一版就是正则）：正则会读到**注释和 docstring**，
    于是"把坏例子写进说明书"这件事看起来像在抄键名——`form_rows.py` 的 `KeyHintLabel`
    文档里举的正是那句旧写法，2026-10-12 这条尺子因此报了一个假红。AST 只认真在代码里
    发生的调用。代价是另一头的盲区：改名导入、再包一层，AST 就认不出那是同一个出口了，
    所以下面那三道闸门必须一起跑——它们各管一种"这把尺悄悄瞎了"：
      ① 读不进来（语法错）：必须点名，不许回"干净"；
      ② tokenize 的调用点数 > AST 认出的：中间隔了一层 AST 看不懂的写法；
      ③ 表里的名字被改名导入：直接算一条红。
    宁可红得难看，也不许静默漏过。
    """
    seqs = seqs or set(tokens)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return [f"<这份源码读不进来，扫描器什么都没看到：{type(exc).__name__}: {exc}>"]
    bad: list[str] = []
    for nm in _renamed_sinks(tree):
        bad.append(f"<{nm} 被改名导入：这把尺只认出口清单里的本名，改了名它就连着抄的那句一起看不见>")
    for name, pos in TIP_SINKS.items():
        tcalls, _refs = _name_uses(text, name)
        calls = _name_calls(tree, name)
        if tcalls > len(calls):
            bad.append(f"<源码里 {tcalls} 处调用 {name}，AST 只认到 {len(calls)} 处："
                       f"再包一层会让这把尺看不见抄的那句>")
        for c in calls:
            for lit in _sink_literals(c, pos, name == "bind_key_tip_parts"):
                tok = key_shaped(lit, [t for t in tokens if not t.isdigit()])
                if tok:
                    bad.append(f"{lit[:40]!r}（抄了 {tok}）")
                    continue
                run = paren_key_runs(lit, seqs)
                if run:
                    bad.append(f"{lit[:40]!r}（抄了 {run[0]}）")
    return bad


def ui_source_offenders() -> tuple[list[str], int]:
    """整个 `app/ui` 里手抄键名的提示语；返回（offenders, 扫过的文件数）。

    扫到 0 个文件＝量具瞎了（目录改名/路径错），调用方要把这记成红而不是"干净"。
    """
    ui_dir = os.path.join(os.path.dirname(HERE), "app", "ui")
    rows = keymap.effective(None)
    toks = key_tokens(rows)
    seqs = {s for s, _a, _d in rows}
    out, n = [], 0
    for fn in sorted(os.listdir(ui_dir)) if os.path.isdir(ui_dir) else []:
        if not fn.endswith(".py"):
            continue
        n += 1
        with open(os.path.join(ui_dir, fn), encoding="utf-8") as fh:
            src = fh.read()
        for hit in hand_copied_hints(src, toks, seqs):
            out.append(f"{fn}: {hit}")
    return out, n


# ---- V7 运行时那把尺：悬停提示逐颗对账 ----
# 中文里管某把键的叫法（表里写 "Space"，人写「空格」）。这把尺只用在**运行时**：
# 静态那一趟撞上过 4 条散文误报（「（见 mk_hint 那条换行理由）」「（正斜杠，QSS 认这个）」），
# 而在真窗口的 53 条悬停上实测零误报。别把它加回静态清单。
KEY_WORDS: dict[str, str] = {
    "空格": "Space", "回车": "Return/Enter", "换行": "Enter", "退格": "Backspace",
    "上箭头": "Up", "下箭头": "Down", "左箭头": "Left", "右箭头": "Right",
}
# 标准窗口实测 33 颗（控制条/页签/标题栏/列表头/两页复位各自登记）。
# 留 8 颗余量（删按钮、合并入口都是合法改动），但**不许一路掉到 0**：
# 0 颗的意思是"这条刷新已经空转"，而空转的判据比没有判据更坏——它会一直绿。
TIP_FLOOR = 20


def tip_expect(rows, parts) -> tuple[str, list[str]]:
    """判据**自己**按生效表算出这颗按钮该说的那句，回（那句, 没绑键的动作名）。

    为什么不叫 `form_rows.paint_key_tip` 也不叫 `keymap.tip_note`：期望值必须独立算，
    借产品那把尺的话，"把动作查成了说明列""多键只报第一把""没绑键也照样画个括号"
    这类错会同时出现在被测和量具两侧，永远对得上。这里的规则是照需求重写的一遍：
    说法＋（表里这些动作绑的键，按表内顺序、/ 连接），一把都没绑就**不画括号**。
    """
    unknown = sorted({a for _w, acts in parts for a in acts} - keymap.known_actions())
    out = []
    for word, acts in parts:
        ks: list[str] = []
        for seq, act, _d in rows:
            if act in acts and seq not in ks:
                ks.append(seq)
        out.append((word() if callable(word) else word)
                   + ("（" + "/".join(ks) + "）" if ks else ""))
    return "".join(out), unknown


def tips_agree(win) -> tuple[bool, str]:
    """整窗悬停提示：屏幕上的那句必须**等于**生效表算出来的那句，且一颗都不许漏。

    三条各管一种坏法：
      ① 逐颗对账：管"过期"。改了键位没重画、或者状态变了没重画，屏幕上留的是旧那句，
         而悬停提示不会报错、不会崩——它是这一族里最静默的坏。
      ② 动作名必须真在表里：管"登记写错字"。`("tooggle_play",)` 这种手错会让括号**静默变空**
         （一把键都没查到 ⇒ 回空串），按钮从此不说键，而没有任何一处会红。
      ③ 未登记却说了键名的控件：管"绕过登记本抄一句"。静态那把尺只认出口清单里的名字，
         新写一个工厂函数它就看不见；而抄到界面上的那句最后一定要落到 `toolTip()` 上，
         在这里没有改名、包一层可躲。
    """
    reg = form_rows.key_tips(win)
    if not reg:
        return (False, "一颗登记过的悬停提示都没找到（刷新已空转或属性名漂了；手抄的那句不算）")
    rows = keymap.effective(win.cfg.get("keymap"))
    toks = key_tokens(rows)
    seqs = {s for s, _a, _d in rows}
    bad: list[str] = []
    if len(reg) < TIP_FLOOR:
        bad.append(f"只登记了 {len(reg)} 颗悬停提示，低于 {TIP_FLOOR}（漏一颗＝那颗永远不跟着键位动）")
    for w, parts in reg:
        try:
            want, unknown = tip_expect(rows, parts)
        except Exception as exc:              # 量具在这颗上崩了也要留话，不许带崩整趟
            bad.append(f"{_tip_id(w)}：这颗算不出该说哪句（{type(exc).__name__}: {exc}）")
            continue
        if unknown:
            bad.append(f"{_tip_id(w)}：登记的动作名不在键位表里 {unknown}"
                       f"（括号会静默变空，从此这颗按钮不说键）")
            continue
        got = str(w.toolTip() or "")
        if got != want:
            bad.append(f"{_tip_id(w)}：屏幕上={got!r}，生效表算出来={want!r}")
        for word, _acts in parts:
            if not (word() if callable(word) else word):
                bad.append(f"{_tip_id(w)}：有一段说法是空的（只剩键名的那句人话不是人话）")
                break
    stray = []
    for w in win.findChildren(QWidget):
        if form_rows.TIP_ATTR in w.__dict__:
            continue
        t = str(w.toolTip() or "")
        if not t:
            continue
        tok = key_shaped(t, toks)
        run = paren_key_runs(t, seqs)
        word = next((k for k in KEY_WORDS if k in t), None)
        hit = tok or (run[0] if run else "") or word or ""
        if hit:
            stray.append(f"{_tip_id(w)}={t[:40]!r}（{hit}）")
    if stray:
        bad.append("这些悬停说了键名却没登记，改键位不会跟着动：" + "；".join(stray[:6]))
    detail = (f"{len(reg)} 颗登记｜悬停 {sum(1 for w in win.findChildren(QWidget) if w.toolTip())} 颗｜"
              + ("；".join(bad) if bad else "逐颗等于生效表、动作名都在表里、没有漏网的抄句"))
    return (not bad), detail


def _tip_id(w: QWidget) -> str:
    """判据里点名一颗控件：QSS 名（`ToolBtn` 这种一整排共用）＋那句说法的前半段。

    为什么光有 objectName 不够：控制条上 18 颗按钮的 objectName 都是 `ToolBtn`，
    红的時候会打出两条一模一样的"ToolBtn：屏幕上=…"，看的人分不出是哪两颗——
    而这条判据管的正是"哪一颗过期了"，点名点不准等于没点名。
    """
    parts = w.__dict__.get(form_rows.TIP_ATTR)
    first = ""
    if parts and parts[1]:
        try:
            word = parts[1][0][0]
            first = "=" + str(word() if callable(word) else word)[:14]
        except Exception:                     # 点名器自己不许把整趟判据带崩
            first = "=<说法读不出>"
    return f"{w.objectName() or type(w).__name__}{first}"


def search_field_ok(ed: QLineEdit, sheet: str | None = None) -> tuple[bool, str]:
    """媒体库搜索框：行首有放大镜、有清除件、形状是**胶囊**。

    参数是那个框本身（不是整个窗口）：证伪那一趟要在**合成**框上试这把尺有没有牙，
    拿真窗口当量具的对照组，等于每次注入都重开一次播放器。
    圆角为什么按"≥ 框高一半"判而不是读那个数字：999 和 40 在 30 高的框上画出来是同一个东西，
    而 8（card 档）画出来是方框倒角——判据要否的是后者，所以问"圆到头了没有"，
    不问"写的数够不够大"。框高取**排过版之后**的实际高，字号换档时这条自动跟着变严。
    """
    sheet = theme.qss() if sheet is None else sheet
    bad = []
    icons_ = [a for a in ed.actions() if not a.icon().isNull()]
    if not icons_:
        bad.append("行首没有放大镜（这一栏在筛这一页，可它长得和输入路径那栏一样）")
    if not ed.isClearButtonEnabled():
        bad.append("没有清除件")
    name = ed.objectName()
    h = int(ed.height())
    r: int | None = None
    # 图标**在左边**才算"行首的放大镜"（贴在右边就和一个普通按钮没区别了）。
    # 只有排过版、可见的时候几何才有意义：合成宿主里那些按钮 `isVisible()` 全假，
    # 这时不判位置（判了就是拿量具自己的装配问题去红产品），但也不写进"通过"的话术里。
    pos_note = "位置未判（没排版）"
    if ed.isVisible():
        btns = [w for w in ed.findChildren(QWidget)
                if w.metaObject().className() == "QLineEditIconButton" and w.isVisible()]
        left = [w for w in btns if w.x() + w.width() // 2 < int(ed.width()) // 2]
        pos_note = f"左侧图标件={len(left)}/{len(btns)}"
        if not left:
            bad.append(f"放大镜不在行首（可见图标件 {len(btns)} 个，没有一个落在左半边，"
                       f"框宽 {ed.width()}）")
    if not name:
        bad.append("搜索框没有 objectName，那条胶囊规则点不到它")
    else:
        m = re.search(r"#%s\s*\{[^}]*border-radius:\s*(\d+)px" % re.escape(name), sheet)
        if m is None:
            bad.append(f"样式表里没有 #{name} 的圆角规则")
        else:
            r = int(m.group(1))
            if r * 2 < h:
                bad.append(f"半径 {r} 圆不到头（框高 {h}，要 ≥{h // 2}）：那不是胶囊")
    return (not bad), (f"名=#{name or '—'} 图标={len(icons_)} 清空={ed.isClearButtonEnabled()} "
                       f"{pos_note} 框高={h} 半径={r if r is not None else '—'}｜"
                       + ("；".join(bad) if bad else "放大镜在行首、能清空、胶囊形"))


def _snap(win) -> dict:
    """「添加/打开」这一路在界面上留下的四件事（列表长度、侧栏行数、正播哪条、当前下标）。
    取样只走属性缓存，不发同步请求——判据要在 GUI 线程里连取两次，问一次内核就堵一次。"""
    pl = win.player.prop("playlist", []) or []
    return {"n": len(pl), "rows": win.panel.list.count(),
            "path": os.path.basename(str(win.player.prop("path", ""))),
            "pos": win.player.prop("playlist-pos")}


def add_verdict(mode: str, before: dict, after: dict, osd: str) -> tuple[bool, str]:
    """点「添加媒体文件」/把文件拖到窗口上，之后该看到什么（**纯函数**）。

    三档是三件不同的事，各自对应本机实测出来的一个坏形状
    （tests/out/probe-append-idle.txt，内核 v0.41.0-1101，2026-10-17）：
    • `playing`：列表里**已有**当前条目 ⇒ 只许加长，不许把正在播的那条换掉。当年
      `sig_add_paths` 直连 `add_paths`（`replace` 默认 True），点"添加"做的是整表替换：
      列表从 3 条变成 1 条、用户正在看的那条没了。
    • `empty`：列表**空着**（`playlist-pos` 是 −1）⇒ 加完必须真的播起来。裸发
      `loadfile … append` 在这一档只把条目**放进**列表（pos 仍 −1、path 仍空），
      症状是"我加了文件，它一动不动"；而改成 `append-play` 又会把 `playing` 那一档
      抢掉（实测 pos 0→1），所以引擎自己按有没有当前条目分流，两档就得分别判。
      这一档的气泡**说的是「载入」不是「追加」**，2026-10-19 订正，依据两处在盘上：
      `player.open_files` 里 `append_first = append and live >= 0`——列表空着时它为假，
      第一条发的是 `replace`；`main_window.open_receipt` 对 `plays=True` 只认内核的
      `file-loaded`，凭据到了就说「已载入 N 个文件」。改之前这里钉「追加」，那是回执层
      改版之前对界面句子的旧期待。**承重的是上面 pos/path 那两条**（"只入列表不动"
      只有它们抓得到），文字这两条只用来抓"两条分支的凭据读串了"。
    • `replace`：**拖到窗口上**（和标题栏"打开文件"同一条语义）⇒ 就是要整表换成这一支。
      这一档不给，"顺手把 dropEvent 也改成 append"就没人拦——两条入口分得开才是这次修复的内容。

    为什么写成纯函数、还在 --falsify 那头拿旧读数过一遍：这三条都是真机观察，而观察
    本身不会证明自己**有牙**（判据整条写成 `check(..., True, ...)` 照样跑绿）。
    """
    if mode == "playing":
        bad = []
        if after["n"] != before["n"] + 1:
            bad.append(f"列表没加长：{before['n']}→{after['n']}（整表替换就长这样）")
        if after["rows"] != after["n"]:
            bad.append(f"侧栏行数没跟着走：{after['rows']}≠列表 {after['n']}")
        if after["path"] != before["path"] or after["pos"] != before["pos"]:
            bad.append(f"正在播的那条被换掉了：{before['path']!r}/pos={before['pos']} → "
                       f"{after['path']!r}/pos={after['pos']}")
        if "追加 1 个文件" not in osd:
            bad.append(f"气泡没说「追加 1 个文件」：{osd!r}（说「载入」就是把替换当成了添加）")
        return (not bad), (f"列表 {before['n']}→{after['n']} 侧栏={after['rows']} "
                           f"条目 {before['path']!r}→{after['path']!r} pos {before['pos']}→"
                           f"{after['pos']} 气泡={osd!r}" + ("".join("；" + b for b in bad)))
    if mode == "empty":
        if before["n"] or before["pos"] not in (-1, None):
            return False, (f"前提立不住：这一档要量的是「没有当前条目」，清空后却是 "
                           f"n={before['n']} pos={before['pos']!r} path={before['path']!r}。"
                           "判据不放宽——它此刻量的不是 append 那一条分支")
        bad = []
        if after["n"] != 1:
            bad.append(f"没加进来：n={after['n']}")
        if after["pos"] != 0 or not after["path"]:
            bad.append(f"进了列表却没播：pos={after['pos']!r} path={after['path']!r}"
                       "（裸 append 在空列表上就是这个形状）")
        if "载入 1 个文件" not in osd:
            bad.append(f"气泡没说「载入 1 个文件」：{osd!r}")
        if "追加" in osd:
            bad.append(f"空列表这一档不该说「追加」：{osd!r}（「追加」那句的凭据是"
                       "「内核推的列表变长、当前条目没变」，这一档没有当前条目，凭据立不住＝"
                       "回执读了另一条分支的凭据）")
        return (not bad), (f"清空后 n={before['n']} pos={before['pos']!r}（−1＝没有当前条目） → "
                           f"加完 n={after['n']} pos={after['pos']!r} path={after['path']!r} "
                           f"气泡={osd!r}" + ("".join("；" + b for b in bad)))
    if mode == "replace":
        bad = []
        if after["n"] != 1 or after["rows"] != 1:
            bad.append(f"没换成一条：列表={after['n']} 侧栏={after['rows']}")
        if after["path"] == before["path"] or after["pos"] != 0:
            bad.append(f"换片没换成：{before['path']!r} → {after['path']!r} pos={after['pos']!r}")
        if "载入 1 个文件" not in osd:
            bad.append(f"气泡没说「载入 1 个文件」：{osd!r}")
        return (not bad), (f"列表 {before['n']}→{after['n']} 侧栏={after['rows']} 条目 "
                           f"{before['path']!r}→{after['path']!r} pos={after['pos']!r} "
                           f"气泡={osd!r}" + ("".join("；" + b for b in bad)))
    return False, f"不认识的模式 {mode!r}（这把尺只有 playing/empty/replace 三档，写错不许放行）"


def build_window(cfg) -> MainWindow:
    win = MainWindow(cfg, files=[])
    win.move(60, 60)
    win.resize(1180, 720)
    win.show()
    pump(0.3)
    return win


def make_cfg():
    cfg = cfgmod.Config()
    cfg["resume"] = False
    cfg["screenshot_dir"] = os.path.join(cfgmod.DATA_DIR, "shots")
    os.makedirs(cfg["screenshot_dir"], exist_ok=True)
    cfg.save()
    return cfg


def main() -> int:
    global APP
    import faulthandler
    faulthandler.enable()          # 硬崩（访问违例）时也要把 Python 栈留在日志里
    faulthandler.dump_traceback_later(120, repeat=True, exit=False)
    os.makedirs(OUT, exist_ok=True)
    for p in FILES:
        if not os.path.exists(p):
            print(f"缺少样片 {p}，请先运行 python tests/gen_sample.py")
            return 2

    def handler(_mode, _ctx, text):
        qt_messages.append(str(text))
    try:
        from PyQt5.QtCore import qInstallMessageHandler
        qInstallMessageHandler(handler)
    except ImportError:
        pass

    _enable_high_dpi()
    APP = QApplication.instance() or QApplication(sys.argv[:1])
    # 崩溃钩子必须在任何窗口/线程活儿之前装：C++ 回调里抛出的异常会让 PyQt5 直接
    # abort（rc=127、stderr 全空），只有落盘的那一份能事后查证。之前把它放到第 13 节，
    # 结果第 9.8 节的硬崩一行栈都没留下，定位靠的是把这段单独摘出来跑。
    install_crash_log()
    cfg = make_cfg()
    # 与 app/main.py 同一条装配：theme.install 里既改令牌重发 QSS，也给一份深色 QPalette
    # （漏掉调色板那条不报错，只会让 Qt 自绘的对勾/箭头带着浅色主题的颜色混进深色界面）
    theme.install(APP, str(cfg["theme_accent"]))

    # 1. 样式表
    check("全局样式表解析并生效（#Primary 渲染成暖象牙实底）",
          qss_applies(theme.qss(), theme.PRIMARY_FILL), f"fill={theme.PRIMARY_FILL}")
    _ink = primary_ink(theme.qss())
    check("主按钮底色压得住墨色字（量像素不读令牌：≥4.5:1）",
          _ink is not None and theme.contrast(theme.PRIMARY_INK, _ink.name()) >= 4.5,
          f"实测底色={None if _ink is None else _ink.name()} "
          f"对比={0.0 if _ink is None else theme.contrast(theme.PRIMARY_INK, _ink.name()):.2f}:1")
    check("样式表没有以 # 开头的注释行（QSS 只认 /* */）",
          not [ln for ln in theme.qss().splitlines()
               if ln.strip().startswith("#") and "{" not in ln],
          "以 # 起头又不是选择器的行会让整张表静默失效")

    # 1.5 开关、双极滑轨、滑轨把手：这三件都是"QSS 里写了、屏幕上没有"的高发区
    _sw_assets = [theme.switch_path(x) for x in (False, True)]
    check("开关两态的把手资产各在盘上、且真的被写进了样式表"
          "（QSS 的 url 只认文件，data URI 与现画 QPixmap 都不认）",
          all(_sw_assets) and all(os.path.isfile(p.replace("/", os.sep)) for p in _sw_assets)
          and all(f' image: url("{p}");' in theme.qss() for p in _sw_assets),
          " / ".join(_sw_assets) or "switch_path() 返回空串：资产缺失，开关会只剩一条没有把手的轨道")
    _sw_on = switch_px(theme.qss(), True)
    _sw_off = switch_px(theme.qss(), False)
    _sw_on_ok, _sw_on_msg = switch_ok(_sw_on, True)
    _sw_off_ok, _sw_off_msg = switch_ok(_sw_off, False)
    # 两条各管一边：两态误用同一张图时"开"那条翻红，两张互换时"关"那条翻红——
    # 合起来才排掉"只有一张图也能过"的那类假绿（旧判据只有"勾在/勾不在"一对，换到开关不够用）。
    check("开关：开着＝把手靠右、近正圆、压得住轨道（量像素不读样式表）",
          _sw_on_ok, _sw_on_msg)
    check("开关：关着＝把手靠左（与上一条互为对照，位置是状态的第一条线索）",
          _sw_off_ok, _sw_off_msg)
    _tr_on = _sw_on.get("track_hex", "")
    _tr_off = _sw_off.get("track_hex", "")
    check("开关：两态的轨道分得开（颜色是第二条线索，不要求它独自承担状态）",
          bool(_tr_on and _tr_off) and theme.contrast(_tr_on, _tr_off) >= 1.5,
          f"关={_tr_off} 开={_tr_on} 对比="
          f"{0.0 if not (_tr_on and _tr_off) else theme.contrast(_tr_on, _tr_off):.2f}:1")
    _dis = switch_px(theme.qss(), False, enabled=False, text="字幕粗体")
    _ena = switch_px(theme.qss(), False, text="字幕粗体")
    check("禁用的开关看着就不能动（文字从近白掉进中灰档；GIF 工坊没挂字幕时就是这一态）",
          _dis["mid_area"] >= KNOB_MID_MIN and _ena["mid_area"] < KNOB_MID_MIN / 4
          and _dis["knob_area"] >= KNOB_AREA_MIN,
          f"禁用中灰={_dis['mid_area']:.0f} 可用中灰={_ena['mid_area']:.0f} "
          f"禁用把手={_dis['knob_area']:.0f}")
    _plain_acc, _plain_neu = groove_px(theme.qss(), "")
    _bip_acc, _bip_neu = groove_px(theme.qss(), "BipSlider")
    check("双极滑轨（亮度/延迟…）零位不再铺半根主色",
          _bip_acc == 0 and _bip_neu > 200,
          f"双极：主色={_bip_acc} 中性={_bip_neu}")
    check("普通滑轨（音量/字幕大小…）仍按主色填充已过的部分",
          _plain_acc > 100, f"通用：主色={_plain_acc} 中性={_plain_neu}")
    _hw, _hh = handle_px(theme.qss())
    _hr_ok, _hr_msg = handle_round_ok(_hw, _hh)
    check("滑轨把手是一颗正圆（Qt 对横向把手不看 height，只按 groove+2×|margin| 画）",
          _hr_ok, _hr_msg)
    _tier = tier_px(theme.qss())
    _tier_ok, _tier_msg = tier_ok(_tier)
    check("文字层级画出来分得开：行标题比说明行/状态读数亮，且三档都压得住面板底"
          "（量字芯像素，不读 QSS 里那几个 color:）", _tier_ok, _tier_msg)

    # 2. 图标工厂：每个注册名都画得出东西（少一笔是静默失败，只能靠像素发现）
    empty_icons = [n for n in sorted(icons.D)
                   if sample_stats(icons.pixmap(n, 22, theme.TEXT).toImage(), 12, 12)["distinct"] < 2]
    check("每个图标都画得出来（无空图）", not empty_icons,
          f"共 {len(icons.D)} 个" + (f"；空的：{empty_icons}" if empty_icons else ""))

    # 2.5 图标的两类静默失败：颜色在半路换成黑色、几何画到设计框外被裁掉。
    #     两者都不会报错、都不会让图标消失，只会"少一笔/黑一块"，只有像素能发现。
    off_color = [n for n in sorted(icons.D) if icon_dark_ink(n)[0] > 0]
    check("每个图标都按请求色画出来（NoPen 之后取笔色会变黑）", not off_color,
          f"{len(icons.D)} 个里偏色的：{off_color[:8]}")
    outside = [n for n in sorted(icons.D) if icon_out_of_frame(n) > 0]
    check("每个图标的几何都在 0..24 设计框内（出框=被裁，画了等于没画）", not outside,
          f"出框的：{outside[:8]}")

    # 3. 窗口与外壳
    win = build_window(cfg)
    check("窗口显示且不小于最小尺寸", win.isVisible() and win.width() >= 880 and win.height() >= 560,
          f"{win.width()}×{win.height()} @dpr={win.devicePixelRatioF()}")
    hwnd = int(win.winId())
    check("原生 MSG 结构体按 64 位布局（wParam/lParam 不是 32 位）",
          ctypes.sizeof(chrome._MSG) == 48 and chrome._MSG.wParam.offset == 16
          and chrome._MSG.lParam.offset == 24,
          f"sizeof={ctypes.sizeof(chrome._MSG)} wParam@{chrome._MSG.wParam.offset} "
          f"lParam@{chrome._MSG.lParam.offset}")
    style = win32.get_style(hwnd)
    check("无边框但保留原生框架位（阴影与贴边快照靠它）",
          bool(style & win32.WS_THICKFRAME) and bool(style & win32.WS_CAPTION), hex(style))
    check("原生事件过滤器真的在收本窗口的消息", bool(win.chrome.seen),
          f"计数={ {hex(k): v for k, v in win.chrome.seen.items()} }")
    check("外壳兜住了异常（chrome.last_error 为空）", win.chrome.last_error is None,
          str(win.chrome.last_error))
    check("空状态：视频容器隐藏、占位页可见", win.video.isHidden() and win.empty.isVisible(), "")

    # 4. 引擎嵌入
    #    产品路径已是异步启动（Popen 立即返回，40ms 轮询握手），这里按真实用户时序
    #    走一遍：发起启动 → 让事件循环跑起来 → 等 running。不再有同步的 ensure_engine。
    ok = win._begin_engine()
    deadline = time.monotonic() + 12.0
    while ok and not win.player.running and time.monotonic() < deadline:
        pump(0.05)
    check("内核随窗口启动成功", ok and win.player.running,
          f"version={win.player.version}")
    kids = win32.direct_children(win.video.hwnd)
    check("mpv 的渲染窗口真的挂在视频容器下", len(kids) >= 1, f"子窗口={kids}")
    fits, d_fit = mpv_fills(win.video)
    check("mpv 子窗口铺满容器（差 ≤2 物理像素）", fits, d_fit)

    # 4.6 快捷键真按键段。**故意放在像素闸门之前**：这一段不看屏幕，显示器睡着也要能跑
    #     （上一轮就是被 rc=3 挡在外面，键位这条链一直没做过事件级验收）。
    #     dispatch 在这里换成录音笔：只记"哪条动作被触发"，不真去做任何事——否则按一遍
    #     表就等于把全屏、退出、菜单弹窗全过一遍，QMenu.exec 还会把这一轮卡死。
    #     边界说清楚：QTest 把事件**直接递给外壳窗口**，验的是 Qt 侧的键→动作匹配，
    #     绕过了操作系统焦点与 mpv 子窗口。"真键盘按下去谁吃到"这一层归人眼（真机目检）。
    #     通道判废只作废**按键这一段**：上一版一断就 return 3，把后面十几节（像素、面板、
    #     内核）的台账一起带走，等于一次焦点抖动白跑一趟。现在记 ENV、跳过按键、照常往下。
    real_dispatch = win.dispatch
    win.dispatch = lambda action: HITS.append(str(action))
    chan = {"lost": False, "why": ""}

    def kcheck(name: str, ok: bool, detail: str = "") -> bool:
        """按键段的 check。两件事：① 通道判废之后一律记 ENV；② **红先问通道**。

        第 ② 条是上一轮那八条红教出来的：一条判据红了，先当场重按对照键（Z）——
        它也不响就说明按键通道在这一刻不通，这条红的证据本来就不成立 ⇒ 记 ENV、
        整段判废、继续往下跑；它响而这条不响，那才是真坏（死写法 / 旧键没摘 / 撞键），
        照常 FAIL。判据本身一个字没动，动的只是"有没有资格判它"。
        分流走 red_route 那份纯函数，控制流形状由 U48d 钉（这段闭包本身在 --falsify 里跑不到）。
        """
        route = red_route(chan["lost"], ok)
        if route == "ASK":
            re_ok, d_re = keyboard_focus_ok(win)
            route = red_route(chan["lost"], ok, re_ok)
            if route == "ENV":
                chan["lost"], chan["why"] = True, d_re
                env_check(name, f"{detail}；且此刻对照键也不响（{d_re}）⇒ 通道半途不通，"
                               f"这条红不作数（是'键盘此刻归别人'，不是'键是死的'）")
                return False
        if route == "ENV":
            env_check(name, f"按键通道已判废（{chan['why']}），这条量不到东西：{detail}")
            return False
        return check(name, ok, detail)

    focus_ok, d_focus = keyboard_focus_ok(win)
    if not focus_ok:
        chan["lost"], chan["why"] = True, d_focus
        env_check("合成按键进得来（拿已知活键 Z 当对照；进不来是焦点/会话问题）", d_focus)
    else:
        print(f"[PASS] 合成按键进得来（拿已知活键 Z 当对照）  —— {d_focus}")
    try:
        recorded: dict[str, list[str]] = {}
        if chan["lost"]:
            env_check(f"生效表 {len(win.key_rows)} 行逐条真按",
                      f"起跑闸门就没过（{d_focus}），整表一次都不该按：跳过，不重复报成因")
        else:
            for seq, _act, _d in win.key_rows:
                recorded[seq] = press_seq(win, seq)
            misses = key_misses(win.key_rows, lambda s: recorded.get(s, []))
            if misses:
                # 不响的行先重按一遍**这些行**（不是整表）：press_key 现在会在窗口不是活动
                # 窗口时抬一次前台，用户切走浏览器那一瞬间的空按到这里大多自己就好了。
                # 为什么重按不会把真坏洗白：死写法在通道活着的时候再按**还是**不响，
                # 撞键再按还是命中别家——重按能消掉的只有"整批一起沉默"这一种形状，
                # 而那正是前台被抢的现场。U48 拿四条已知答案钉住这两家分得开。
                for seq, _act, _got in list(misses):
                    recorded[seq] = press_seq(win, seq)
                misses = key_misses(win.key_rows, lambda s: recorded.get(s, []))
            verdict, d_rows = channel_split(misses, press_seq(win, CONTROL_KEY[0]),
                                            len(win.key_rows))
            if verdict == "ENV":
                chan["lost"], chan["why"] = True, d_rows
                env_check(f"生效表 {len(win.key_rows)} 行逐条真按", d_rows)
            else:
                kcheck(f"生效表 {len(win.key_rows)} 行逐条真按：每把键**恰好**命中它自己那一条"
                       f"（0 次=死写法，2 次=旧快捷键没摘干净，命中别家=撞键）",
                       verdict == "PASS", d_rows)

        # 校准：这条判据到底看不看得见"上一版那 5 条死键"那一类坏——只测活键不算校准。
        # 做法是往窗口上临时挂一条探针快捷键，看按下去回不回话。
        # **必须先把这把键腾空**再挂：探针 4 步骤 E/F 实测，同窗口同 context 下两条解析
        # 成同一键值的 QShortcut 会让**两条一起沉默**。生效表里本来就有 ("PgDown","next")，
        # 不腾空就挂探针，量到的是"撞键全哑"而不是"死写法不响"，上一轮就是这么把校准段
        # 判成 FAIL 的（活写法按下=[]）。
        saved_km = dict(win.cfg.get("keymap") or {})
        try:
            win.cfg["keymap"] = {"off": ["PgDown"], "on": []}
            win._install_shortcuts()
            freed = press_key(win, Qt.Key_PageDown)
            probe = install_raw_shortcut(win, "PgDn", "probe_from_dead_spelling")
            got_dead = press_key(win, Qt.Key_PageDown)
            drop_shortcuts(probe)
            probe2 = install_raw_shortcut(win, "PgDown", "probe_from_alive_spelling")
            got_alive = press_key(win, Qt.Key_PageDown)
            drop_shortcuts(probe2)
            refused = ""
            try:
                press_seq(win, "PgDn")                 # 死写法：助手必须拒递，不许把进程带走
            except ValueError as exc:
                refused = str(exc)
            ok_cal, d_cal = calibration_verdict(freed, got_dead, got_alive, refused)
            kcheck("检测器校准：同样指 PageDown 这把键，「PgDn」写法装上按下去不响、「PgDown」响；"
                  "且按死写法的串被当场拒绝（探针 4：真递 0x01ffffff 会把进程带走）", ok_cal, d_cal)

            # 撞键在产品路径上的样子：用户往配置里加一条别名写法（Page Down），默认表里
            # 那行 PgDown 是同一把键。effective() 折叠成一条，所以这把键**照样响、且只响
            # 一次**；没有折叠的话探针实测是两条全哑——那是"加一条键位把另一条弄坏"。
            win.cfg["keymap"] = {"off": [], "on": [["Page Down", "mute"]]}
            win._install_shortcuts()
            got = press_key(win, Qt.Key_PageDown)
            rows = [(s, a) for s, a, _d in win.key_rows]
            kcheck("配置里有别名撞键（用户写 Page Down、默认表写 PgDown）：装的是用户那条，"
                  "这把键按一次只响一次（没有按键值折叠就会两条全哑）",
                  got == ["mute"] and ("Page Down", "mute") in rows
                  and ("PgDown", "next") not in rows
                  and len(win._shortcuts) == len(win.key_rows) == len(keymap.DEFAULT),
                  f"命中={got} 表 {len(win.key_rows)} 行 / 装了 {len(win._shortcuts)} 条"
                  f"（出厂表 {len(keymap.DEFAULT)} 行）")
        finally:
            win.cfg["keymap"] = saved_km
            win._install_shortcuts()
        try:
            win.cfg["keymap"] = {"off": ["Z", "PgDown"], "on": [["PgDn", "mute"]]}
            real_dead = keymap.dead
            keymap.dead = lambda s: False          # 绕过存储校验，模拟没有 keymap.py 的世界
            try:
                win._install_shortcuts()
            finally:
                keymap.dead = real_dead
            got = press_key(win, Qt.Key_PageDown)
            ok_dead, d_dead = no_hit(got, "mute")
            kcheck("走真装机的死写法：按 PageDown 不响应 mute（校验闸门真在装快捷键前面兜住）",
                  ok_dead, d_dead)
            win.cfg["keymap"] = {"off": ["E"], "on": []}
            win._install_shortcuts()
            after = press_seq(win, "E")
            # 删行以后那把键**本来就不该在表里**，所以不能拿 hit_once(after, "screenshot")
            # 去判——那等于要求它还响一次。条数一致性单独用 counts_agree 比。
            ok_removed, d_removed = combine(
                no_hit(after, "screenshot"),
                counts_agree(("装的 QShortcut", len(win._shortcuts)),
                             ("生效表行数", len(win.key_rows))),
                table_has(win.key_rows, drop=("E", "screenshot")))
            kcheck("删一行再装：那把键当场不再响应，且窗口上挂的 QShortcut 条数跟着掉",
                  ok_removed, d_removed)
            win.cfg["keymap"] = {"off": ["K"], "on": [["K", "next"]]}
            win._install_shortcuts()
            got = press_seq(win, "K")
            ok_rebind, d_rebind = hit_once(got, "next")
            kcheck("改绑 K→下一个：按 K 只命中新动作一次（off/on 不成对折算就会同键留两行，"
                  "响哪条看 Qt 心情）", ok_rebind, d_rebind)
            win.cfg["keymap"] = {"off": ["PgUp"], "on": [["Page Up", "vol_up"]]}
            win._install_shortcuts()
            got = press_seq(win, "Page Up")
            ok_alias_alive, d_alias_alive = combine(
                hit_once(got, "vol_up"),
                table_has(win.key_rows, keep=("Page Up", "vol_up"), drop=("PgUp", "prev")))
            kcheck("配置里写别名（Page Up）照样按得响，而且只响一次", ok_alias_alive, d_alias_alive)
            win._install_shortcuts()
            got = press_seq(win, "Z")
            ok_reinstall, d_reinstall = hit_once(got, "mute", installed=len(win._shortcuts),
                                                  rows=len(win.key_rows))
            kcheck("连装两遍（用户保存两次键位）：按一次只触发一次，窗口上不留孤儿 QShortcut",
                  ok_reinstall, d_reinstall)
        finally:
            win.cfg["keymap"] = saved_km
            win._install_shortcuts()
    finally:
        win.dispatch = real_dispatch
        HITS.clear()

    # 4.5 环境闸门：显示器休眠/锁屏时整屏抠出来是全黑，而命中链照样说我们的窗口在最上面。
    #     这时候后面每一条像素判据都只能报"环境问题"，不许写成产品坏了（rc=3）。
    health_ok, d_health = capture_health()
    print(f"[{'PASS' if health_ok else 'ENV'}] 抓屏通路可用（整屏不是死黑；挡路的环境自己点名）  —— {d_health}")
    if not health_ok:
        print(f"\n---- 中止：屏幕此刻抠不出像素，像素类判据全部不作数。样片、内核、窗口那几条已跑完。----")
        win.close()
        return 3

    # 5. 加载样片
    times: list[float] = []
    logs: list[str] = []
    win.player.sig_time.connect(times.append)
    win.player.sig_log.connect(logs.append)
    win.add_paths(FILES)
    # 等到"路径 + 两层切换"都落定再判：path 和 playlist 是两条独立的属性推送，
    # 谁先到不保证。之前在 path 刚到、playlist 事件还没投递时取样，就会误报
    # "容器没顶替占位页"（同一个断言两次跑出不同结果，就是这种竞态）。
    loaded = wait_for(lambda: "sample-1" in str(win.player.prop("path", ""))
                      and not win.video.isHidden() and win.empty.isHidden(), 8.0)
    check("加载后画面容器顶替占位页", loaded,
          f"path={os.path.basename(str(win.player.prop('path', '')))} "
          f"video.hidden={win.video.isHidden()} empty.hidden={win.empty.isHidden()}")
    # 6 秒预算在冷启动那天不够用（第一次跑就红过一次：内核刚起、盘上首读，duration
    # 推上来要 7 秒）。判据不变，只把等待放宽到 10 秒——这条测的是"时长会进时间轴"，
    # 不是启动耗时；启动耗时另有专门的一条在量。
    # 取样窗口也要等**界面那一份**：内核缓存里的 duration 和滑轨的 _dur 之间隔着一次
    # Qt 事件投递，"等到内核有数、立刻量界面"会拿到 0（实测同一份代码两轮里一轮绿一轮
    # 红，红的正是这条——和上面 1526 行那条注释说的是同一个坑）。判据本身没动：
    # 滑轨量程落不进 9.5~10.5 就还是 FAIL，只是现在等的是它自己的值。
    dur_ok = wait_for(lambda: 9.5 <= float(win.player.prop("duration", 0) or 0) <= 10.5
                      and 9.5 <= win.bar.scrub._dur <= 10.5, 10.0)
    check("时长进入时间轴", dur_ok,
          f"duration={win.player.prop('duration')} scrub={win.bar.scrub._dur:.2f} "
          f"path={os.path.basename(str(win.player.prop('path', '')))}")
    t0 = len(times)
    adv = wait_for(lambda: len(times) > t0 + 3, 3.0)
    check("时间在推进且界面跟着走", adv and win.bar.lbl_time.text() != "00:00 / 00:00",
          f"3s 内新增 {len(times) - t0} 次 time-pos，界面={win.bar.lbl_time.text()}")
    pos = float(win.player.prop("time-pos", 0) or 0)
    check("界面时间 == 内核时间（±1s，证明不是各自猜）",
          abs(win.bar.scrub._pos - pos) <= 1.0, f"ui={win.bar.scrub._pos:.2f} mpv={pos:.2f}")

    # 6. 播放列表面板
    win.toggle_playlist()
    rows = win.panel.list.count()
    cur = win.player.prop("playlist-pos")
    check("侧栏可打开且行数与内核列表一致", win.panel.isVisible() and rows == len(FILES),
          f"rows={rows} playlist={len(win.player.prop('playlist', []) or [])}")
    _rows_ink = [(i == win.panel.list.currentRow(),
                  win.panel.list.item(i).foreground().color().name().upper(),
                  win.panel.list.item(i).font().bold())
                 for i in range(win.panel.list.count())]
    _ok_ink, _d_ink = sidebar_ink_ok(_rows_ink)
    check("当前项在侧栏用主色高亮（且这一档主色压得住选中行的底，非当前行不跟着涂色）",
          cur == 0 and win.panel.list.currentRow() == 0 and _ok_ink, _d_ink)

    # 7. 标题栏
    check("标题栏与窗口标题带上片名",
          "sample-1" in win.title.lbl_doc.text() and "sample-1" in win.windowTitle(),
          f"doc={win.title.lbl_doc.text()!r}")

    # 7.5 窗口收到最小宽度 + 长片名：QLabel 不会自己裁字，给它多窄它就画多少个像素然后
    #     **直接断掉**（"gelu-sample-1-这是…"这种半截串，用户既读不全也不知道后面还有字）。
    #     产品侧 title_bar._elide 走的是"中间省略 + 整句进 tooltip"，这条判据量的是结果。
    #     片名长度是算出来的：880 宽的窗口里 DragRegion 559px，减左右内边距后可用 535px，
    #     这一串实测 691px。旧 fixture 在当前字体下只有 539px，只多 4px，换个字重就可能
    #     掉进"放得下"那一支，让证伪那条恒真，所以这里把它加长了。
    #     （第一版随手写的名字只有 444px，"放得下"那条分支直接放行，整条判据恒真——
    #     是 --falsify 那头注入"根本不裁"的旧写法时才发现红不起来。）
    _doc_before, _sz_before = win.title._doc, win.size()
    _long = "gelu-sample-1-这是一串长得放不下的片名-4K-HDR-第03集-导演剪辑-最终版-重制版-补充镜头-国语中字-60帧-1920x1080.mkv"
    win.resize(880, _sz_before.height())
    pump(0.4)
    win.title.set_document(_long)
    pump(0.2)
    _ok_t, _d_t = title_fits(win.title, _long)
    check("最小宽度下的长片名裁成中间省略号、整句留在 tooltip（不是断在半截）", _ok_t, _d_t)
    win.title.set_document(_doc_before)
    win.resize(_sz_before)
    pump(0.4)

    # 8. 控件 ↔ 内核双向一致
    win.bar.vol.setValue(64)
    check("拖动音量条 → 内核音量跟上",
          wait_for(lambda: int(win.player.prop("volume", 0) or 0) == 64, 2.0),
          f"volume={win.player.prop('volume')}")
    win.player.set_volume(112)
    check("内核音量变化 → 音量条同步（>100 也要显示得出）",
          wait_for(lambda: win.bar.vol.value() == 112, 2.0), f"slider={win.bar.vol.value()}")

    # 空格暂停这一对（#73，2026-10-13）。原先第一句写的是 `startswith("播放")`、第二句只读缓存，
    # 两句都不比"这句是不是刚翻过来的"，于是它能靠**跨线程送达之前的旧话**变绿——旧映射
    # （暂停时说「暂停」）在 r5/r7/r8/r9/r11/r12/r13 里读了六趟以上"pause=True tip=播放（空格）"，
    # 那半句其实是**播放态**留下的旧话，恰好长得像正确答案；r14 只是送达早了一步，就照出了产品侧的反义。
    # 现在两半都要：方向对（比括号前那半句全文，不比前缀）**且**和按之前那句不一样。
    def _judge73(rec, want_pause, before):
        def one():
            if bool(win.player.prop("pause", not want_pause)) is not bool(want_pause):
                return False
            ok, why = play_tip_flip(want_pause, before, win.bar.btn_play.toolTip())
            rec[0], rec[1] = ok, f"{why}（按之前={before!r}）"
            return ok
        return one

    _v73a, _v73b = [False, ""], [False, ""]
    _tip73_0 = win.bar.btn_play.toolTip()
    win.dispatch("toggle_play")
    check("空格暂停：内核 pause 与按钮提示同步（说法要跟着翻，且必须是翻过之后的那句）",
          wait_for(_judge73(_v73a, True, _tip73_0), 2.0), _v73a[1])
    _tip73_1 = win.bar.btn_play.toolTip()
    win.dispatch("toggle_play")
    check("再按一次恢复播放：同一把尺倒着量一次（播放态的说法该是「暂停」）",
          wait_for(_judge73(_v73b, False, _tip73_1), 2.0), _v73b[1])
    win.dispatch("vol_up")
    check("↑ 音量 +5", wait_for(lambda: int(win.player.prop("volume", 0) or 0) == 117, 2.0),
          f"volume={win.player.prop('volume')}")
    win.dispatch("speed_up")
    check("] 加速：内核 speed 与倍速标签同步",
          wait_for(lambda: abs(float(win.player.prop("speed", 1) or 1) - 1.1) < 0.01
                   and win.bar.lbl_speed.text() == "1.1x", 2.0),
          f"speed={win.player.prop('speed')} chip={win.bar.lbl_speed.text()}")
    win.dispatch("speed_reset")
    win.dispatch("loop")
    check("L 循环切到列表循环（图标与勾选态与内核一起变）",
          wait_for(lambda: str(win.player.prop("loop-playlist")) == "inf"
                   and win.bar.btn_loop.isChecked() and win.bar._loop_idx == 1, 2.0),
          f"loop-playlist={win.player.prop('loop-playlist')} "
          f"checked={win.bar.btn_loop.isChecked()} idx={win.bar._loop_idx}")
    # 绕过按钮直接改内核（等价于右键菜单那条入口）：界面必须自己跟着内核回到"不循环"，
    # 而不是靠测试手动把 UI 拨回去——手动拨会把"不同步"这个真 bug 遮掉。
    win.player.set_loop("no")
    check("内核改回不循环后按钮自动同步（状态以内核回报为准）",
          wait_for(lambda: str(win.player.prop("loop-playlist", "inf")) != "inf"
                   and not win.bar.btn_loop.isChecked() and win.bar._loop_idx == 0, 2.5),
          f"loop-playlist={win.player.prop('loop-playlist')} "
          f"checked={win.bar.btn_loop.isChecked()} idx={win.bar._loop_idx}")

    win.dispatch("jump_50")
    check("数字键 5 → 跳到 50%（±0.6s）",
          wait_for(lambda: abs(float(win.player.prop("time-pos", 0) or 0) - 5.0) < 0.6, 3.0),
          f"time-pos={win.player.prop('time-pos')}")
    win.bar._on_seek(2.0, True)
    check("时间轴松手精确定位", wait_for(lambda: abs(float(win.player.prop("time-pos", 0) or 0) - 2.0) < 0.6, 3.0),
          f"time-pos={win.player.prop('time-pos')}")
    win.player.goto_index(1)
    check("侧栏跳轨：内核换文件且高亮跟着走",
          wait_for(lambda: "sample-2" in str(win.player.prop("path", "")), 6.0)
          and wait_for(lambda: win.panel.list.currentRow() == 1, 2.0),
          f"path={os.path.basename(str(win.player.prop('path', '')))}")

    # 6.6 播放列表那一页的「添加媒体文件」＝追加（#88，2026-10-17）。
    #     用户实测的形状：在一支好几条的列表上点"添加"，按钮说的是添加，做的却是
    #     "整表换成刚挑的那一个"——列表被清空、正在播的那条也没了。
    #     tests/test_append_semantics.py 钉的是发出去的命令与槽位落点（假内核，pos 按
    #     probe-append-idle.txt 的实测语义**推演**）；这一段钉的是**真窗口＋真内核**里
    #     用户看得见的结果：列表长没长、正在播那条换没换、侧栏行数跟没跟、气泡说的是哪句。
    #     从信号进而不是从按钮进：按钮后面是模态 QFileDialog，测试里驱动不了；
    #     "按钮→信号"那一段的落点由 test_append_semantics 的 AST 尺子钉（W1/W2 两条）。
    G3 = os.path.join(SAMPLES, "gelu-sample-3.mkv")
    b6 = _snap(win)
    win.osd._text = ""
    _pl_pre = list((win.player.prop("playlist") or [])[:b6["n"]])
    win.panel.sig_add_paths.emit([G3])
    # 发完命令**立刻**喂一条「没变长」的 playlist 推送。为什么要手动喂：内核在追加这一趟里
    # 本来就会推好几条、且不都变长（本机实测：一次初始载入里 `playlist` 连推 4 次、长度全是 2，
    # `tests/out/probe-append-receipt-hidden2.log` 的推送计数那一行），而 R20 真机那句
    # 「气泡=''」就是这么踩中的——旧 `_settle_open_receipt` 进门第一句就 `self._open_claim = None`，
    # 这种推送把回执的账销了却什么也没说，随后真变长那条没人认，20 秒后的超时也一起没了。
    # 靠运气等内核自己推一条短的＝这一条判据时而量得到时而量不到，所以在这里当场把它做出来：
    # 同一线程同一调用链里发，保证排在内核那条真推送之前（真推送要等事件循环送达）。
    win._on_playlist(_pl_pre)
    _kept = win._open_claim is not None and win._pl_len == len(_pl_pre)
    check("追加的回执不许被一条「没变长」的 playlist 推送吃掉：账必须还挂着"
          "（吃掉＝那句话永远不说，R20 真机「气泡=''」的根因；"
          "`_pl_len` 也跟着这条推送走了才证明注入真的进了产品的门，不是空操作）",
          _kept, f"喂入长度={len(_pl_pre)}（追加前列表={b6['n']}）之后 账还在="
                 f"{win._open_claim is not None} _pl_len={win._pl_len} "
                 f"claim={None if win._open_claim is None else dict(win._open_claim)}")
    got6 = wait_for(lambda: _snap(win)["n"] == b6["n"] + 1, 6.0)
    pump(0.6)                       # 侧栏按窗合并（R10-c）：内核先落，行数是下一次 _rebuild_now 才画
    a6 = _snap(win)
    ok6, d6 = add_verdict("playing", b6, a6, win.osd._text)
    check("播放列表点「添加」＝加长：列表多一条、侧栏跟着多一行、正在播那条与下标都不动",
          got6 and ok6, d6)

    # 6.7 对照：同一支文件**拖到窗口上**仍是"打开这些"＝整表替换（那是"打开"的语义）。
    #     不钉这一条，"以后把 dropEvent 也顺手改成 append"就没人拦得住——两条入口分得开
    #     才是这次修复的全部内容，只钉一条等于只钉了一半。
    #     事件自己造（拖放的真通路要外壳与资源管理器配合，这里进不去），入口仍然是产品的
    #     dropEvent：它读 mimeData、决定 replace 还是 append，那正是要量的那一步。
    b7 = _snap(win)
    _md = QMimeData()
    _md.setUrls([QUrl.fromLocalFile(G3)])
    _ev = QDropEvent(QPointF(24.0, 24.0), Qt.CopyAction, _md, Qt.LeftButton, Qt.NoModifier)
    drop_err = ""
    try:
        win.dropEvent(_ev)
    except BaseException as exc:                    # noqa: BLE001 - 真坏了要红在这条上，不许带崩整轮
        drop_err = f"{type(exc).__name__}: {exc}"
    got7 = wait_for(lambda: _snap(win)["n"] == 1, 6.0)
    pump(0.6)
    a7 = _snap(win)
    ok7, d7 = add_verdict("replace", b7, a7, win.osd._text)
    check("对照：拖到窗口＝整表替换（换成拖进来的那一支、pos 归 0、气泡说「载入」）",
          got7 and ok7 and not drop_err, f"{drop_err and drop_err + '；'}{d7}")

    # 6.8 空列表上点「添加」：必须**顺手播起来**，不许只把条目放进列表就不动。
    #     前面 6.6 量的是"已有当前条目"那一档，这一档量的才是 open_files 里
    #     `append_first` 那条分支本身（缓存里 playlist-pos 是 −1 ⇒ 第一条发成 replace）。
    #     清空立不立得住也算进去：立不住就是"前提没了"，红要红在点名前提上。
    win.player.clear_playlist()
    wait_for(lambda: _snap(win)["n"] == 0, 4.0)
    pump(0.4)
    b8 = _snap(win)
    win.osd._text = ""
    win.panel.sig_add_paths.emit([G3])
    got8 = wait_for(lambda: _snap(win)["pos"] == 0 and _snap(win)["path"], 8.0)
    pump(0.6)
    a8 = _snap(win)
    ok8, d8 = add_verdict("empty", b8, a8, win.osd._text)
    check("空列表上点「添加」也真的播起来（pos 到 0、path 就是刚加的那支；只入列表不动＝缺陷形状）",
          got8 and ok8, d8)

    # 6.9 把状态还原成 6.6 之前那样（2 条、正播 sample-2）。
    #     这一条不是给产品记账的，是给**后面的判据**保前提：悬停预览那段拿
    #     `k_prev = win._thumb_key` 再 `goto_index(0)`，要求"key 跟着文件变"——
    #     留成单支 sample-3 的话那条量的就不再是"换片重排队"了。
    win.add_paths(FILES)
    back1 = wait_for(lambda: len(win.player.prop("playlist", []) or []) == len(FILES)
                     and "sample-1" in str(win.player.prop("path", "")), 10.0)
    win.player.goto_index(1)
    back2 = wait_for(lambda: "sample-2" in str(win.player.prop("path", ""))
                     and win.player.prop("playlist-pos") == 1, 10.0)
    check("还原到本段开始时的状态（2 条、正播 sample-2）——给后面的判据保前提",
          back1 and back2,
          f"载入={back1} 跳回={back2} 列表={len(win.player.prop('playlist', []) or [])} "
          f"path={os.path.basename(str(win.player.prop('path', '')))!r} "
          f"pos={win.player.prop('playlist-pos')!r}")

    # 6.10 换过好几支片子之后，媒体信息那句"输出 gpu-next"还在不在（#95 整表普查量出来的第四格）。
    #      机制：`_on_start_file` 抹 `_FILE_SCOPED`，指望内核在新文件上再推一发把缓存填回来；
    #      而 `tests/probe_file_scoped_table.py` 两条腿都量到 `current-vo` 是
    #      'gpu-next'→'gpu-next'（值不变）且换文件那一下**没推**（留档
    #      tests/out/probe-file-scoped-table-v4-092438.txt 的逐格表与 [FINAL] 行）——
    #      抹了就永远空着，于是统计页「输出驱动」那行变空、按 I 那半句消失。
    #      三段（现状／摘掉／交回）："摘掉"是把这一格从缓存里 pop 掉＝那一格被抹之后的状态，
    #      "交回"走产品自己的入缓存口 `_apply`（就是内核那一发推回来时经过的那一步）。
    #      牙的另一半在离线那趟：`tests/test_append_semantics.py` 的 E20 与它的变异
    #      （把 current-vo 注回 `_FILE_SCOPED` 必须顶红 E20）——那条钉的是"表里不许再有它"，
    #      这一条钉的是"用户看得见的那半句"，中间隔着 `_media_info()` 一层，两边不互相代替。
    _vo_key = "current-vo"
    vo_before = win.player.prop(_vo_key)
    info_before = win._media_info()
    _saved_vo = win.player._props.get(_vo_key)
    win.player._props.pop(_vo_key, None)            # 注入：这一格被抹且内核没补
    info_inj = win._media_info()
    vo_inj = win.player.prop(_vo_key)
    win.player._apply(_vo_key, _saved_vo)           # 交回：内核那一发推回来了
    pump(0.2)
    info_back = win._media_info()
    vo_back = win.player.prop(_vo_key)
    check("换过文件之后媒体信息仍有「输出 …」（current-vo 不在换文件抹掉的那张表里）",
          bool(vo_before) and ("输出 " + str(vo_before)) in info_before
          and not vo_inj and "输出 " not in info_inj
          and vo_back == _saved_vo and ("输出 " + str(vo_back)) in info_back,
          f"现状 vo={vo_before!r} 信息句含输出={'输出 ' in info_before}；"
          f"注入后 vo={vo_inj!r} 信息句={info_inj!r}；交回后 vo={vo_back!r} 含输出={'输出 ' in info_back}")

    # 9. OSD 浮层位置（150% 缩放下最容易被 DPI 账算错的一项）
    #    ms 给到 8000，不是因为产品侧该停 8 秒，而是这条判据后面还要 raise_、可能再
    #    topmost、两轮 pump，累计 0.7~1.4s——正好压在默认 1000ms 的淡出窗口上。实测
    #    显示器刚醒的那一轮，抠回来 230×80 全是 0（明暗跨度 0）：那不是气泡没画字，
    #    是量到了正在变透明的泡。停留时长本身另有专门一条在下面判（默认值 1000ms）。
    t_osd = time.monotonic()
    win.osd.show_text("倍速 1.1x", 8000)
    pump(0.1)
    inside, d = osd_in_picture(win)
    check("OSD 浮层可见且落在画面内", inside, d)
    # 抓屏前给分层窗口一次完整合成：show() 返回时窗口已经"存在"，但那一帧的像素还没
    # 端上屏，立刻抠区域会拍到底下的视频帧（跨度 39 而不是 219），看起来像"气泡没画字"。
    pump(0.3)
    osd_img, e_osd = capture(win.osd, win.osd, "OSD 气泡", ((0.5,), (0.5,)))
    p_osd = os.path.join(OUT, "screen-osd.png")
    osd_img.save(p_osd)
    ink, d_ink = has_ink(osd_img)
    pixel_check("OSD 气泡", "OSD 气泡真的把字画到了屏幕上（深底亮字，不是透明空气）",
                ink and win.osd.last_error is None and not e_osd,
          f"{d_ink} 量之前已过 {time.monotonic() - t_osd:.2f}s "
          f"不透明度={win.osd.windowOpacity():.2f} last_error={win.osd.last_error} "
          f"{e_osd} → {p_osd}")

    # 9.1 默认停留时长：不传 ms 时，1 秒后必须自己淡掉。上面把 ms 撑到 8 秒是为了量像素，
    #     这条把产品默认值单独量回来——两个数各管各的，别拿一个测试用的值当产品行为。
    win.osd.hide()
    pump(0.2)
    win.osd.show_text("默认停留")
    stays = win.osd.isVisible() and win.osd._hide.isActive() and win.osd._hide.interval() == 1000
    gone = wait_for(lambda: not win.osd.isVisible() or win.osd.windowOpacity() < 0.2, 3.0)
    check("不传 ms 时默认停留 1000ms，到点自己淡出（不是钉在屏幕上）", stays and gone,
          f"起表={stays} 表长={win.osd._hide.interval()}ms 3s 内收掉={gone} "
          f"可见={win.osd.isVisible()} 不透明度={win.osd.windowOpacity():.2f}")
    win.osd._hide.stop()
    win.osd._anim.stop()
    win.osd.setWindowOpacity(1.0)
    win.osd.hide()

    # 9.5 时间轴缩略图：时长落定自动排队抽帧 → 悬停贴出那一格
    key = win._thumb_key
    cur_path = str(win.player.prop("path", "") or "")
    cur_dur = float(win.player.prop("duration", 0) or 0)
    check("时长落定后自动排队抽帧，缓存 key 就绪",
          bool(key) and win.thumbs.ready(key),
          f"key={key or '(空)'} ready={win.thumbs.ready(key) if key else False} "
          f"info={win.thumbs.last_info or '(本轮命中上一轮留下的缓存，没重跑 ffmpeg)'}")

    # 冷启动单独量一次：上面那条 ready 完全可能是命中上一轮的盘上缓存（那时整条
    # pipeline 一行都没跑）。换一个空目录逼它真起 ffmpeg，顺带拿到真实耗时。
    cold_dir = os.path.join(cfgmod.CACHE_DIR, "thumbs-cold")
    shutil.rmtree(cold_dir, ignore_errors=True)
    cold = ThumbStore(cfgmod.FFMPEG_EXE, cold_dir, parent=win)
    cold_done: list[tuple[str, bool, str]] = []
    cold.sig_done.connect(lambda k, ok, d: cold_done.append((k, ok, d)))
    t_cold = time.monotonic()
    ck = cold.request(cur_path, cur_dur)
    # 谓词里必须带上"回报也收到了"：worker 是**先写 meta.json 再 emit**，只等 ready 的话
    # 完全可能在 meta 刚落盘、那条跨线程信号还在队列里时就返回（实测 0.14s 拿到空回报）。
    cold_ok = wait_for(lambda: cold.ready(ck) and bool(cold_done), 30.0)
    dt_cold = time.monotonic() - t_cold
    check("冷启动（空缓存目录）确实起 ffmpeg 抽出帧，并回报耗时",
          cold_ok and bool(cold_done) and cold_done[0][1] and cold.running() == 0,
          f"{os.path.basename(cur_path)} {cur_dur:.1f}s，整轮用时 {dt_cold:.2f}s，"
          f"回报={cold_done[:1]}")
    shutil.rmtree(cold_dir, ignore_errors=True)

    n_active = len(win.thumbs._active)
    win.thumbs.request(cur_path, cur_dur)
    check("同一份片重复请求不再起进程（命中盘上缓存 / 在途去重）",
          len(win.thumbs._active) == n_active,
          f"请求前 {n_active} 个在途，请求后 {len(win.thumbs._active)} 个")

    x_a = int(win.bar.scrub.width() * 0.4)
    # 开量之前先把**真光标**请出时间轴（#72）。落点选视频区左上角而不是别处：`scrubber.py`
    # 是全仓唯一接 `mouseMoveEvent`/`leaveEvent` 的控件（实测 grep），而 mpv 那边
    # `--input-default-bindings=no`（app/engine/player.py:202），鼠标停在那儿什么都不触发。
    # 不做这一步，操作系统那只鼠标要是正压在轨道上，`pump()` 就会把它的真实 move/leave 和
    # 我们的合成事件一起喂进同一个槽——浮层被拖到右端夹取上限再藏掉，r13 两条红就是这个形状。
    _pv_park = win.video.mapToGlobal(QPoint(6, 6))
    QCursor.setPos(_pv_park)
    pump(0.15)                                   # 等真实 leave 处理完，状态归位再发合成事件
    hover_scrub(win.bar.scrub, x_a)
    pump(0.15)
    inside, d_prev = floater_in_picture(win.preview)
    check("悬停时间轴 → 预览浮层出现，且落在画面内、控制条上方",
          win.preview.isVisible() and inside, d_prev)
    # 底照在这里拍下来（不是量完之后回想）：`_preview_at` 是主窗记着"鼠标此刻指在第几秒、
    # 轨道内哪个像素"的那个槽，合成事件发的是 x_a，所以它就是这一趟的应有值。
    _pv_x_ref = int(win.preview.x())
    _pv_at_ref = tuple(win._preview_at) if win._preview_at else ()
    _scrub_rect = QRect(win.bar.scrub.mapToGlobal(QPoint(0, 0)), win.bar.scrub.size())
    pump(0.3)                      # 同上：等分层窗口把这一帧真的端上屏再抠图
    _cur_now = QCursor.pos()       # 争用与否按"此刻真光标在不在轨道里"判，见 preview_contention
    _pv_env, _pv_msg = preview_contention(_pv_x_ref, _pv_at_ref, _cur_now, _scrub_rect,
                                          int(win.preview.x()),
                                          tuple(win._preview_at) if win._preview_at else (),
                                          win.preview.isVisible())
    pv, e_pv = capture(win.preview, win.preview, "悬停预览", ((0.5,), (0.5,)))
    p_pv = os.path.join(OUT, "screen-scrub-preview.png")
    pv.save(p_pv)
    st_pv = sample_stats(pv)
    _pv_ok = has_picture(pv) and win.preview.last_error is None and not e_pv
    _pv_detail = (f"{e_pv}{pv.width()}×{pv.height()} 亮度{st_pv['mean']:.0f} "
                  f"标准差{st_pv['std']:.0f} 颜色{st_pv['distinct']} "
                  f"last_error={win.preview.last_error} → {p_pv}")
    if _pv_ok:
        pixel_check("悬停预览", "浮层里真的是抽出来的一帧（有画面，不是空框或透明空气）",
                    True, f"{_pv_detail}｜{d_prev}")
    elif _pv_env:
        # 环境把这块的证据拿走了（真光标正压在轨道上）：记 ENV 不记 FAIL，读数值行——
        # 拿"眼睛/鼠标此刻归谁"当产品结论，会引着人去改本来对的代码（同 pixel_check 那套规矩）。
        env_check("浮层里真的是抽出来的一帧（有画面，不是空框或透明空气）",
                  f"{_pv_msg}｜{_pv_detail}｜已先把真光标请到 {_pv_park.x()},{_pv_park.y()}，"
                  f"仍被真实鼠标挤掉＝这只鼠标在被人动")
    else:
        pixel_check("悬停预览", "浮层里真的是抽出来的一帧（有画面，不是空框或透明空气）",
                    False, f"{_pv_detail}｜{_pv_msg}｜（真光标不在轨道上，这一条不作环境分流）")

    x_b = int(win.bar.scrub.width() * 0.75)
    gx_a = win.preview.x()
    hover_scrub(win.bar.scrub, x_b)
    pump(0.15)
    check("光标换一格 → 浮层跟着走（不是钉死在中间）",
          win.preview.isVisible() and win.preview.x() != gx_a, f"{gx_a} → {win.preview.x()}")

    good_key, win._thumb_key = win._thumb_key, "0" * 16      # 模拟"还在抽/抽失败"
    hover_scrub(win.bar.scrub, x_a)
    pump(0.15)
    check("缩略图没就绪时不画空框（降级成时间轴自带的时间气泡）",
          not win.preview.isVisible(),
          f"visible={win.preview.isVisible()} key={win._thumb_key}")
    win._thumb_key = good_key
    hover_scrub(win.bar.scrub, x_a)
    pump(0.15)
    leave_scrub(win.bar.scrub)
    check("鼠标离开时间轴 → 浮层收起", wait_for(lambda: not win.preview.isVisible(), 0.6),
          f"visible={win.preview.isVisible()}")

    k_prev = win._thumb_key
    win.player.goto_index(0)
    swapped = wait_for(lambda: "sample-1" in str(win.player.prop("path", ""))
                       and win._thumb_key and win._thumb_key != k_prev, 8.0)
    check("换片后按新文件重新排队（key 跟着文件走，不串片）",
          swapped and win.thumbs.ready(win._thumb_key),
          f"{k_prev} → {win._thumb_key} ready={win.thumbs.ready(win._thumb_key) if win._thumb_key else False}")

    # 9.8 媒体库（侧栏第二页）：真扫描 → 卡片 → 真封面 → 双击换片
    win.library.roots = [SAMPLES]
    if win.sidebar.isVisible():          # 前面几段可能把侧栏开着：先收起，宽度账才有起点
        win._set_sidebar(False)
        pump(0.2)
    w_video_before = win.video.width()
    win.toggle_library()
    page_ok = wait_for(lambda: win.sidebar.isVisible()
                       and win.sidebar_stack.currentIndex() == 1
                       and win.lib_panel.isVisible() and not win.panel.isVisible(), 3.0)
    cw2, ch2 = win32.client_size(win.video.hwnd)
    kids2 = win32.direct_children(win.video.hwnd)
    refit_ok = False
    if kids2:
        rr = win32.window_rect(kids2[0])
        refit_ok = abs((rr[2] - rr[0]) - cw2) <= 2 and abs((rr[3] - rr[1]) - ch2) <= 2
    check("Ctrl+M 换到媒体库页：卡片页显示、列表页让位、按钮勾选态跟着走",
          page_ok and win.bar.btn_library.isChecked() and not win.bar.btn_list.isChecked(),
          f"page={win.sidebar_stack.currentIndex()} 可见={win.lib_panel.isVisible()} "
          f"列表页可见={win.panel.isVisible()}")
    check("开侧栏后视频让出宽度、mpv 子窗口跟着重铺（画面不被侧栏盖住）",
          win.video.width() < w_video_before and refit_ok,
          f"视频宽 {w_video_before}→{win.video.width()} 容器 {cw2}×{ch2} "
          f"子窗口={win32.window_rect(kids2[0]) if kids2 else None}")

    scanned = wait_for(lambda: len(win.library.items()) >= 3 and not win.library.running(), 90.0)
    # 期望集合按**目录现数**，不抄标题（2026-10-17 真机 leg4 就是红在手抄的那颗数上：
    # 目录里后来加了 uhd/ 三支 4K 样片，读数是"收到 7 个、缺=[]、多=[av1,h264,hevc]"——
    # 收进来的三支全在盘上、全确实是视频容器，所以那一条量的是**台账过时**，不是产品）。
    # 分界用本文件自己写的扩展名表：不 import 产品的 VIDEO_EXT，否则这条判据变成
    # "拿被测代码的答案验被测代码"（fixture 预写答案＝自证）。目录噪声的跳过规则
    # 是夹具约定（回收站／以 . 或 $ 开头的同步缓存），量具与产品各写一份、口径一致才对得上账。
    _vid_ext = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".mpg",
                ".mpeg", ".ts", ".m2ts", ".asf", ".3gp", ".divx", ".h264", ".hevc", ".vvc"}
    _sub_ext = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".pgs", ".sup"}
    _files = []
    for _dp, _dn, _fn in os.walk(SAMPLES):
        _dn[:] = [d for d in _dn if not d.startswith(("$", ".")) and not d.startswith("@eaDir")
                  and d.lower() != "system volume information"]
        _files += [os.path.normpath(os.path.join(_dp, f)) for f in _fn]

    def _ext(p: str) -> str:
        return os.path.splitext(p)[1].lower()

    want = {os.path.normcase(p) for p in _files if _ext(p) in _vid_ext}
    subs = {os.path.normcase(p) for p in _files if _ext(p) in _sub_ext}
    got = {os.path.normcase(str(e.get("path") or "")) for e in win.library.items()}
    premise = len(want) >= 3 and bool(subs)
    check("真扫描把样片目录里的视频容器全收进表、字幕与文本一律不进（期望按目录现数，不抄标题）",
          scanned and premise and got == want,
          ("" if premise else f"前提立不住：样片目录现数容器 {len(want)} 支、字幕 {len(subs)} 份"
                              "（要 ≥3 支且至少 1 份字幕，否则「字幕不进」这一半没牙）；")
          + f"目录现数=容器 {len(want)}／字幕 {len(subs)} 收到={sorted(os.path.basename(x) for x in got)} "
          f"缺={sorted(os.path.basename(x) for x in want - got)} "
          f"多={sorted(os.path.basename(x) for x in got - want)} 状态={win.library.last_status}")
    lib_model = win.lib_panel.grid.model()
    rows = 0 if lib_model is None else lib_model.rowCount()
    check("卡片数与扫描结果一致且非空（空表时 0==0 那种「一致」不算通过）",
          cards_agree(rows, len(win.library.items())),
          f"卡片{rows} 条目{len(win.library.items())}")

    def entry_of(path: str):
        return next((x for x in win.library.items() if str(x.get("path")) == path), None)

    def posters_in():
        for i in range(lib_model.rowCount()):
            idx = lib_model.index(i, 0)
            e = entry_of(str(idx.data(Qt.UserRole) or ""))
            if e and float(e.get("dur") or 0) > 0 and not has_picture(
                    card_image(idx.data(Qt.DecorationRole))):
                return False
        return True

    got_posters = wait_for(posters_in, 90.0)
    lit, flat = [], []
    for i in range(rows):
        idx = lib_model.index(i, 0)
        e = entry_of(str(idx.data(Qt.UserRole) or "")) or {}
        nm = os.path.basename(str(idx.data(Qt.UserRole) or ""))
        icon = idx.data(Qt.DecorationRole)
        s = sample_stats(card_image(icon), 24, 12)
        (lit if has_picture(card_image(icon)) else flat).append(
            "%s(dur=%.1f 色%s 标准差%.0f)" % (nm, float(e.get("dur") or 0), s["distinct"], s["std"]))
    check("探到时长的片子，卡片上都是真抽出来的一帧（占位块不许冒充封面）",
          got_posters and len(flat) == 1 and flat[0].startswith("broken.mp4"),
          f"有帧={lit} 无帧={flat}")
    check("探不到时长的坏容器走降级：显示 --:-- 和占位块，不假装成功",
          "--:--" in card_text(win.lib_panel.grid, "broken.mp4"),
          f"卡片文字={card_text(win.lib_panel.grid, 'broken.mp4')!r} 图标={flat}")
    txt1 = card_text(win.lib_panel.grid, "gelu-sample-1.mp4")
    check("卡片里的时长来自探测（sample-1 → 00:10，且带片名与大小）",
          "gelu-sample-1" in txt1 and "00:10" in txt1 and "KB" in txt1, f"{txt1!r}")

    p2 = os.path.join(SAMPLES, "gelu-sample-2.mp4")
    # 数字是合成的：样片只有 6 秒，而"片尾 15 秒内算看完"这条规则会把 6 秒的片判成
    # 已看完，测不到进度徽标。这里要证的是"卡片会去查观看记录并换算成百分比"，
    # 换算用的是记录里的 pos/dur，所以给一对能算出 66% 的值（自测档案，不影响真机）。
    win.cfg.remember_position(p2, 240.0, 360.0)
    win.lib_panel.set_items(win.library.items())
    txt2 = card_text(win.lib_panel.grid, "gelu-sample-2.mp4")
    check("续播进度进卡片：记住「看到 66%」后文字立刻带上",
          "看到 66%" in txt2, f"{txt2!r}")

    lib_shot, e_lib = capture(win, win.lib_panel, "媒体库侧栏")
    p_lib = os.path.join(OUT, "screen-library-panel.png")
    lib_shot.save(p_lib)
    pixel_check("媒体库侧栏", "媒体库侧栏屏幕留档（卡片、封面、文字、选中态一起目检，且拍到的是我们自己）",
                not lib_shot.isNull() and not e_lib and win.lib_panel.last_error is None,
          f"{e_lib}{lib_shot.width()}×{lib_shot.height()} last_error={win.lib_panel.last_error} → {p_lib}")

    tgt = "gelu-sample-3.mkv"
    tgt_row = row_of(win.lib_panel.grid, tgt)
    clicked = dblclick_row(win.lib_panel.grid, tgt_row)
    played = wait_for(lambda: tgt in str(win.player.prop("path", "")), 12.0)
    check("双击卡片 → 内核真的换到那部片（点的就是放的，不是各自演）",
          clicked and played,
          f"双击命中行={tgt_row} 信号发出={clicked} path="
          f"{os.path.basename(str(win.player.prop('path', '')))}")

    win.add_paths(FILES)
    restored = wait_for(lambda: "sample-1" in str(win.player.prop("path", "")), 12.0)
    win.toggle_library()
    check("再按 Ctrl+M 收起媒体库、原片单恢复（后面几段验收还要用 sample-1）",
          restored and not win.sidebar.isVisible() and not win.bar.btn_library.isChecked(),
          f"恢复={restored} 侧栏可见={win.sidebar.isVisible()} "
          f"按钮={win.bar.btn_library.isChecked()}")

    # 9.9 调节面板（侧栏第三页）：双向一致、开页让位、快捷键与控件走同一段代码
    #     这一页的验收只回答一个问题：界面说的，内核认不认；内核改的，界面跟不跟。
    #     所有"跟上"都由 wait_for 等推送，测试自己不动控件——手动把界面拨正会把
    #     "不同步"这个真 bug 遮掉（第 8 节的循环按钮就是这么处理的）。
    tp = win.tweak_panel
    ok_tbl, d_tbl = panel_matches_table(tp)
    check("面板控件与可调项表一一对应（表加一档、界面少一根=那档功能根本没做出来）",
          ok_tbl, d_tbl)

    # 侧栏的隐藏是"标志立刻变、布局下一轮才重算"，所以量宽度前必须先跑一轮事件循环，
    # 否则 vw0 拿到的是上一段媒体库还开着时的宽度（第一版就是这么量出 891→891 的）。
    pump(0.4)
    vw0 = win.video.width()
    win.toggle_tweak()
    opened = wait_for(lambda: win.sidebar.isVisible()
                      and win.sidebar_stack.currentIndex() == 2
                      and win.bar.btn_tweak.isChecked()
                      and win.tab_btns[2].isChecked(), 2.5)
    pump(0.4)                     # 布局与 refit 落定后再量几何，不在动画中间态取样
    fit_open, d_fit_open = mpv_fills(win.video)
    check("Ctrl+T 开到调节页：侧栏可见、第三页选中、控制条与页签两个按钮一起勾上",
          opened, f"可见={win.sidebar.isVisible()} 页={win.sidebar_stack.currentIndex()} "
                  f"工具栏={win.bar.btn_tweak.isChecked()} 页签={win.tab_btns[2].isChecked()}")
    check("开页后视频容器让出宽度、mpv 子窗口跟着铺满（不能留一条没铺到的黑边）",
          fit_open and 0 < win.video.width() < vw0,
          f"容器宽 {vw0}→{win.video.width()} {d_fit_open}")

    agree0, d_agree0 = panel_agrees_with_kernel(tp)
    check("面板默认态：每个控件画的就是内核回报值", agree0, d_agree0)

    # —— 内核 → 面板：从面板以外的入口改一遍全表，界面必须自己跟上
    probe_num = {"sub-scale": 1.4, "sub-delay": 0.5, "sub-border-size": 5.0,
                 "sub-shadow-offset": 2.0, "audio-delay": -0.3, "brightness": 30,
                 "contrast": -15, "gamma": 10, "saturation": 20, "hue": -25}
    probe_choice = (("sub-align-x", "left"), ("sub-align-y", "top"),
                    ("sub-color", "#FFFFF700"), ("sub-back-color", "#FF000000"),
                    ("tone-mapping", "mobius"), ("gamut-mapping-mode", "perceptual"),
                    ("target-peak", "1000"))
    for _p, _v in probe_num.items():
        win.player.set_tweak(_p, _v)
    for _p, _v in probe_choice:
        win.player.set_tweak(_p, _v)
    for _p in ("sub-bold", "sub-visibility", "sub-use-margins"):
        win.player.set_tweak(_p, False)
    synced = wait_for(lambda: panel_agrees_with_kernel(tp)[0], 3.5)
    agree1, d_agree1 = panel_agrees_with_kernel(tp)
    check("引擎被别的入口改了 20 项 → 面板不靠手动重画、自己跟着改完",
          synced and agree1, d_agree1)

    # 另一条腿：真·内核推送。绕过 set_tweak 直接 set_prop（等价于"另一个入口改了内核"），
    # 面板要靠 property-change 事件跟上——上一条证的是"写入即广播"，这条证的才是订阅链。
    row_s = tp._rows["sub-scale"]
    win.player.set_prop("sub-scale", 1.8)
    pushed = wait_for(lambda: abs(row_s.value_of(row_s.slider.value()) - 1.8) < 1e-3, 3.0)
    check("只等内核推事件（不经外壳写入），面板也跟上 —— 订阅链是通的",
          pushed, f"缓存={win.player.prop('sub-scale')} 界面="
                  f"{row_s.value_of(row_s.slider.value())}")

    # 实测记录：本机内核对 sub-border-size **不推** property-change——值真的改了
    # （get 回得来 6.5），事件却永远不来（缓存停在旧值），播着片也一样。
    # 这正是引擎那句 sig_info 自广播存在的理由；哪天内核开始推了，这条会转红提醒重估。
    win.player.set_prop("sub-border-size", 6.5)
    got_push = wait_for(lambda: abs(float(win.player.prop("sub-border-size", 0) or 0) - 6.5) < 1e-6, 1.5)
    read_back = float(win.player.get("sub-border-size", 0) or 0)
    check("实测：sub-border-size 内核收了值却不推事件（引擎自广播补的正是这一发）",
          (not got_push) and abs(read_back - 6.5) < 1e-6,
          f"缓存={win.player.prop('sub-border-size')}（没推就还是旧值） get 回来={read_back}")
    win.player.set_tweak("sub-border-size", 5.0)   # 缓存与内核重新对齐，别把脏态留给后面

    # —— 面板 → 内核：拖滑轨（这一路等价于松手那一下）
    row_scale = tp._rows["sub-scale"]
    row_scale.slider.setValue(row_scale.index_of(2.2))
    got_scale = wait_for(lambda: abs(float(win.player.prop("sub-scale", 0) or 0) - 2.2) < 1e-3, 2.5)
    check("拖滑轨到 2.2 → 内核字幕大小真的是 2.2（不是界面自己画的数）",
          got_scale, f"内核={win.player.prop('sub-scale')} "
                     f"界面={row_scale.value_of(row_scale.slider.value())}")

    # —— 拖动途中与松手的分工：途中只写值、不等应答，否则每动一格都要跑一趟 IPC，
    #    手感变成一格一顿。这条在屏幕上看不出来，只能数同步请求。
    gets: list[str] = []
    real_get = win.player.get
    win.player.get = lambda name, *a, **kw: (gets.append(str(name)), real_get(name, *a, **kw))[1]
    tp._on_num_change("sub-delay", 1.5, False)
    during = [g for g in gets if g == "sub-delay"]
    gets.clear()
    tp._on_num_change("sub-delay", 1.5, True)
    on_commit = [g for g in gets if g == "sub-delay"]
    win.player.get = real_get
    check("拖动途中不发同步请求、松手那次才发（一次往返换一次真值）",
          not during and len(on_commit) == 1, f"途中 get={during} 松手 get={on_commit}")

    # —— 下拉：程序改索引不该发命令（内核→界面→内核的回环就是这么来的），
    #    只有 activated（用户操作才发）该发。两条腿一起判才不是"恰好没连上"。
    cb_tm = tp._choices["tone-mapping"]
    idx_h = cb_tm.findData("hable")
    tm_before = str(win.player.prop("tone-mapping", ""))
    cb_tm.setCurrentIndex(idx_h)
    pump(0.35)
    tm_after_programmatic = str(win.player.prop("tone-mapping", ""))
    cb_tm.activated.emit(idx_h)
    moved_cb = wait_for(lambda: str(win.player.prop("tone-mapping", "")) == "hable", 2.5)
    check("下拉：setCurrentIndex 不动内核、activated 才动内核",
          tm_after_programmatic == tm_before and moved_cb,
          f"程序改后内核={tm_after_programmatic!r}（原本 {tm_before!r}） 用户改后={tm_after_programmatic}→"
          f"{win.player.prop('tone-mapping')}")

    # —— 勾选框：同一对防线（clicked 而不是 toggled）
    cf = tp._flags["sub-use-margins"]
    m_before = bool(win.player.prop("sub-use-margins", True))
    cf.setChecked(not m_before)
    pump(0.35)
    m_kernel = bool(win.player.prop("sub-use-margins", True))
    tp.repaint_from_kernel()          # 面板自己的对齐入口，不是测试手动拨
    m_resynced = cf.isChecked() is m_before
    cb_bold = tp._flags["sub-bold"]
    b_before = bool(win.player.prop("sub-bold", True))
    cb_bold.click()
    b_flipped = wait_for(lambda: bool(win.player.prop("sub-bold", True)) is (not b_before), 2.5)
    check("勾选框：setChecked 不动内核、click 才动内核，且错位能被重画纠正",
          m_kernel is m_before and m_resynced and b_flipped,
          f"程序改后内核={m_kernel}（原本 {m_before}） 重画后勾={cf.isChecked()} "
          f"用户改后 sub-bold={win.player.prop('sub-bold')}")

    # —— 快捷键：全部转发到面板的同一批处理器，所以内核值必须按表里的步长走
    d_base = float(win.player.prop("sub-delay", 0) or 0)
    win.dispatch("sub_delay_down")
    d_moved = wait_for(lambda: abs(float(win.player.prop("sub-delay", 0) or 0)
                                   - (d_base - 0.1)) < 1e-3, 2.5)
    s_base = float(win.player.prop("sub-scale", 1) or 1)
    win.dispatch("sub_size_down")
    s_moved = wait_for(lambda: abs(float(win.player.prop("sub-scale", 1) or 1)
                                   - (s_base - 0.05)) < 1e-3, 2.5)
    ui_moved = wait_for(lambda: panel_agrees_with_kernel(tp)[0], 2.5)
    check("J/- 快捷键的步长来自表：延迟走 0.1、大小走 0.05（不是两处各写死的数）",
          d_moved and s_moved and ui_moved,
          f"delay {d_base}→{win.player.prop('sub-delay')}（表步长 "
          f"{tweaks.spec('sub-delay').step}） scale {s_base}→{win.player.prop('sub-scale')}"
          f"（{tweaks.spec('sub-scale').step}）")

    tm_cur = str(win.player.prop("tone-mapping", ""))
    win.dispatch("tone_cycle")
    want_tm = tweaks.cycle_option(tweaks.TONE_MAPPING, tm_cur)
    tm_cycled = wait_for(lambda: str(win.player.prop("tone-mapping", "")) == want_tm
                         and panel_agrees_with_kernel(tp)[0], 2.5)
    check("Shift+T 循环一档：内核走到表里的下一格、下拉自己选中那格", tm_cycled,
          f"{tm_cur} → {win.player.prop('tone-mapping')}（期望 {want_tm}） "
          f"下拉={tp._choices['tone-mapping'].currentData()}")

    win.dispatch("eq_reset")
    eq_ok = wait_for(lambda: eq_all_zero(win) and panel_agrees_with_kernel(tp)[0], 3.0)
    check("Shift+G 画面复位：内核五项归零、五根滑轨自己弹回中间", eq_ok,
          f"回报={[win.player.prop(s.prop) for s in tweaks.EQ]} "
          f"滑轨={[tp._rows[s.prop].slider.value() for s in tweaks.EQ]}")

    # —— A-B 循环：面板上那两个按钮和快捷键是同一段代码
    win.player.seek_abs(2.0)
    pump(0.6)
    win.dispatch("ab_a")
    win.player.seek_abs(5.0)
    pump(0.6)
    win.dispatch("ab_b")
    rng_in = wait_for(lambda: win.player.ab_range() is not None, 2.5)
    rng = win.player.ab_range()
    text_ok = wait_for(lambda: "→" in tp.lbl_ab.text(), 2.0)
    check("Shift+A/B 标的区间进内核也进面板文字", rng_in and text_ok and rng and rng[0] < rng[1],
          f"区间={rng} 面板={tp.lbl_ab.text()!r}")
    win.dispatch("ab_clear")
    ab_cleared = wait_for(lambda: win.player.ab_range() is None and "未设置" in tp.lbl_ab.text(), 2.5)
    check("Shift+C 清除：内核 ab-loop 回 no、面板文字回到未设置", ab_cleared,
          f"区间={win.player.ab_range()} 面板={tp.lbl_ab.text()!r}")

    # —— 输出设备：清单是问内核要的（它不是订阅属性，没有事件推上来）。
    #    这里只验"列出来且选中的是当前设备"，不做真切换：换设备会打断这台机器上的声音。
    tp.refresh_devices()
    datas = [tp.cb_device.itemData(i) for i in range(tp.cb_device.count())]
    cur_dev = str(win.player.prop("audio-device", "") or "")
    check("输出设备下拉列出内核给的真设备，且当前设备被选中",
          tp.cb_device.isEnabled() and len(datas) >= 2 and datas[0] is not None
          and cur_dev and str(tp.cb_device.currentData()) == cur_dev,
          f"项数={len(datas)} 当前={cur_dev!r} 选中={tp.cb_device.currentData()!r}")

    # —— 布局：控件存在 ≠ 看得见。0 宽/0 高的滑轨不报错，只是用户找不到。
    lay_ok, d_lay = panel_layout_ok(tp)
    check("滑轨真的有尺寸、整页内容比视口高（滚动区在承重，不是把下面挤出可视范围）",
          lay_ok, d_lay)

    # —— V3/V5 落在**真页面**上：合成装配过了不等于产品里真用了那一档。
    #    切页再量（stacked page 当过当前页才排过版；没排过版时两页右缘读成 123/107，
    #    那是量具的旧几何，不是产品没对齐——探针第一版就把它当成了结论）。
    win.sidebar_stack.setCurrentWidget(tp)
    pump(0.05)
    _p1_ok, _p1_msg = pills_ok(tp)
    check("调节页的行尾数值：同页等宽、右缘齐、拖到量程两端不许变宽（扫读列不许逐行找数）",
          _p1_ok, _p1_msg)
    win.sidebar_stack.setCurrentWidget(win.transform_panel)
    pump(0.05)
    _p2_ok, _p2_msg = pills_ok(win.transform_panel)
    check("画面页同理（两页共用同一种控件，坏一处就该两处点名）", _p2_ok, _p2_msg)
    win.sidebar_stack.setCurrentWidget(tp)
    pump(0.05)

    # 状态读数同一档：滤镜链与 A-B 是同一个角色（"内核现在怎样"），以前分在两档
    # （11px 内联 #9A9A9A vs 12px #A6ABB8）。判法是把**同一句话**塞给两个标签再比像素——
    # 直接比它们此刻的文字会量到两件事：字不同、字号也不同，红了我分不清是哪个。
    _sc, _sa = win.transform_panel.lbl_chain, tp.lbl_ab
    _txt = "滤镜链：（空）"
    _old_c, _old_a = _sc.text(), _sa.text()
    _sc.setText(_txt)
    _sa.setText(_txt)
    pump(0.05)
    _ic, _ia = label_ink(_sc), label_ink(_sa)
    _bc, _ba = ink_box(_sc), ink_box(_sa)
    _st_blind = [n for n, v in (("滤镜链字芯", _ic), ("A-B 字芯", _ia),
                               ("滤镜链外接框", _bc), ("A-B 外接框", _ba)) if v is None]
    _st_ok = (not _st_blind and _ic[0] == _ia[0]
              and abs(_bc[0] - _ba[0]) <= 2 and abs(_bc[1] - _ba[1]) <= 2)
    check("两页的状态读数同一档：同一句话在两个标签上画成同色同尺寸（±2 设备像素）",
          _st_ok, (f"量具瞎了：{_st_blind}" if _st_blind else
                   f"链={_ic[0]}/{_bc} A-B={_ia[0]}/{_ba}"
                   + ("" if _st_ok else " ← 同角色两种长相"))
          )
    _sc.setText(_old_c)
    _sa.setText(_old_a)
    win.transform_panel._paint_chain()      # 还原成缓存里那份真状态，别把探针塞进去的字留给后面
    pump(0.05)

    # —— V6：按键提示必须跟着键位表走，搜索框必须看得出是搜索框。
    #    提示行是**文案**，改错它不报错、不崩，只是继续写着那把已经不响的键——
    #    这种坏只有"改过键位再去按提示"的用户会撞见，所以必须机器守着。
    _hint_ok, _hint_msg = hints_agree(win)
    check("侧栏四页的按键提示逐句等于生效键位表（含：漏页／多页／空行没藏都算错）",
          _hint_ok, _hint_msg)
    _hc, _n_src = ui_source_offenders()
    check(f"app/ui 的源码里不许再有手抄键名的提示行（扫了 {_n_src} 个文件）",
          _n_src > 6 and not _hc,
          f"文件数={_n_src}" + ("" if _n_src > 6 else "（少到这个数＝量具没扫到东西，不作数）")
          + (f"｜抄了的：{_hc}" if _hc else "｜全都走 KeyHintLabel"))
    # 悬停提示是**这一族里最静默的坏**：写了旧键名不报错、不崩、不丢样式，只是用户照提示按
    # 下去没反应。静态那把尺只认得出出口清单里的名字（换个工厂函数就看不见），所以这里再按
    # "屏幕上那句"逐颗对账——抄的那句最后一定要落到 toolTip() 上，那里没有名字可换。
    _tip_ok, _tip_msg = tips_agree(win)
    check("每一颗按钮的悬停提示逐颗等于生效键位表（控制条/页签/标题栏/列表头/复位键一起对账）",
          _tip_ok, _tip_msg)
    # 搜索框那一条要先切到媒体库页再量：没当过当前页的 stacked page **没排过版**，
    # `height()` 读到的是 Qt 默认 30，于是"半径 ≥ 框高一半"这条会拿假高度去判真样式。
    win.sidebar_stack.setCurrentWidget(win.lib_panel)
    pump(0.05)
    _sf_ok, _sf_msg = search_field_ok(win.lib_panel.search)
    check("媒体库搜索框：放大镜在行首、带一键清空、形状是胶囊（圆到头）", _sf_ok, _sf_msg)
    win.sidebar_stack.setCurrentWidget(tp)
    pump(0.05)

    shot_tp, e_tp = capture(win, win.sidebar, "调节页侧栏")
    p_tp = os.path.join(OUT, "screen-tweak-panel.png")
    shot_tp.save(p_tp)
    st_tp = sample_stats(shot_tp, 24, 40)
    pixel_check("调节页侧栏", "调节页屏幕留档（滑轨/下拉/勾选/按钮一起目检，且拍到的是我们自己）",
                not shot_tp.isNull() and not e_tp and shot_tp.width() >= win.sidebar.width() - 2
                and st_tp["distinct"] >= 6,
          f"{e_tp}{shot_tp.width()}×{shot_tp.height()} 颜色{st_tp['distinct']} "
          f"亮度{st_tp['mean']:.0f} → {p_tp}")

    # —— 收摊：这一页把全表都动过了，必须回默认值。
    #    亮度留 30 会让后面"视频区有画面"的阈值判据变松，那是自己给自己放水。
    win.player.reset_video_eq()
    for _p in probe_num:
        win.player.set_tweak(_p, tweaks.spec(_p).default)
    for _p, _v in probe_choice:
        win.player.set_tweak(_p, tweaks.spec(_p).default)
    for _p in ("sub-bold", "sub-visibility", "sub-use-margins"):
        win.player.set_tweak(_p, True)
    back_ok = wait_for(lambda: panel_agrees_with_kernel(tp)[0] and eq_all_zero(win), 3.0)
    agree2, d_agree2 = panel_agrees_with_kernel(tp)
    check("全表回到默认值后界面与内核再次一致（自测档案不留脏值）",
          back_ok and eq_all_zero(win) and agree2, d_agree2)

    win.toggle_tweak()
    closed = wait_for(lambda: not win.sidebar.isVisible() and not win.bar.btn_tweak.isChecked(), 2.5)
    pump(0.4)
    fit_back, d_fit_back = mpv_fills(win.video)
    check("再按 Ctrl+T 收起：侧栏隐藏、按钮弹起、mpv 子窗口又铺回整宽",
          closed and fit_back and win.video.width() >= vw0 - 2,
          f"可见={win.sidebar.isVisible()} 按钮={win.bar.btn_tweak.isChecked()} "
          f"容器宽 {vw0}→{win.video.width()} {d_fit_back}")
    # 面板的每次动作都会发一条状态文字到 OSD；留着会让第 12 节的视频区截屏拍到气泡
    win.osd._hide.stop()
    win.osd.hide()

    # 9.10 统计页 / 快捷键与菜单对账 / 睡眠定时 / 设置页
    #     这一段判的还是同一件事：界面说出来的话，内核与事实表认不认。
    #     统计页危险在「没测到」能被画成「测到 0」；设置页危险在「要重启才生效」
    #     能被写成「已生效」；睡眠定时危险在「取消之后照样到点」。三条都得双向判。
    sp = win.stats_panel
    src_ok, d_src = rows_have_source(list(sp._rows), _OBSERVED, list(win.player.STATS_PROPS))
    check("统计页每一行都有数据源（订阅表、轮询表，或点名过的合成行）——没点名的行就是永远填不上数的死行",
          src_ok, d_src)

    win.dispatch("toggle_stats")
    st_open = wait_for(lambda: win.sidebar.isVisible() and win.sidebar_stack.currentIndex() == 5, 2.5)
    pump(0.35)
    tab4, d_tab4 = tab_state_agrees(win, 5)
    check("I 键开到统计页：栈索引、页签勾选、控制条三个灯、各页可见性四处一致（统计页没专属按钮，三个灯该全灭）",
          st_open and tab4, d_tab4)
    check("统计页可见时轮询表在跑（内核一秒一轮；页面藏着还问就是白烧一趟 IPC）",
          sp._timer.isActive() and win.player.running,
          f"isActive={sp._timer.isActive()} running={win.player.running}")

    sp._polled.clear()
    win.player.poll_stats()
    polled = wait_for(lambda: len(sp._polled) >= 3 and not win.player._stats_busy, 4.0)
    check("poll_stats 的一轮结果经 sig_stats 回到面板，且在途标记落回 False（同时在途只允许一轮）",
          polled, f"轮询到的项={sorted(sp._polled)[:5]} busy={win.player._stats_busy}")

    hw_row = sp._rows["hwdec-current"]
    agree_hw, d_hw = hwdec_row_agrees(win.player.prop("hwdec-current"), hw_row.isVisible(),
                                      hw_row.value_text())
    check("硬解那一行写的就是 hwdec-current 的判读：'no' 不许念成硬解 no，内核没回报时不许猜软件解码",
          agree_hw, d_hw)
    preset_row = sp._rows["quality_preset"]
    want_preset = perf.label("quality_preset", perf.resolve(cfg)["quality_preset"])
    check("缩放预设那行念的是校验过后的档位标签（配置里留着的非法值不会被原样念出来）",
          preset_row.value_text() == want_preset,
          f"界面={preset_row.value_text()!r} 期望={want_preset!r}")

    # 「整行不画」这条要两头判：摘掉数据源必须藏起来，把数还回去必须画回来。
    # 只判一头的话，一个「永远藏着这行」的实现也能通过——那等于把丢帧统计删了。
    fd_key = "frame-drop-count"
    sp._timer.stop()
    saved_fd = sp._polled.pop(fd_key, None)
    saved_prop = win.player._props.pop(fd_key, None)
    synth_fd = saved_fd is None and saved_prop is None
    sp._paint()
    hid_ok, d_hid = row_hidden_without_data(sp, fd_key)
    # 把数还回去：优先还真实的来源；内核这一版恰好没给这个数时（实测它会给，真走到
    # 这里说明内核换了版本），用一轮「给了 0」的假数据只验机制，不冒充真读数。
    if saved_fd is not None:
        sp._polled[fd_key] = saved_fd
    elif saved_prop is not None:
        win.player._props[fd_key] = saved_prop
    else:
        sp._polled[fd_key] = 0
    sp._paint()
    vis_ok, d_vis = row_hidden_without_data(sp, fd_key)
    if synth_fd:
        sp._polled.pop(fd_key, None)
    sp.refresh()
    check("丢帧行两个方向都对：内核没回报时整行不画，有数时又必须画回来（判据才不是恒真或恒假）",
          hid_ok and not vis_ok,
          f"无数据时{d_hid}；有数据时{d_vis}（假数据兜底={synth_fd}）")

    # 逐个点过去的**名单按按钮数取**，不写死下标：2026-10-12 侧栏多了"声音均衡"一页（统计让到
    # 索引 5），而这一趟原来写的是 `(0,1,2,3,4,0)`——它正好把新的一页**点不到**，于是"每一页
    # 都一致"这句话里少了一页，而尺子照样绿。回到 0 那一步留着：切走再切回也要一致。
    _order = list(range(len(win.tab_btns))) + [0]
    per_tab = []
    for k in _order:
        win.tab_btns[k].click()
        pump(0.25)
        ok_k, d_k = tab_state_agrees(win, k)
        per_tab.append((k, ok_k, d_k))
    check(f"{len(win.tab_btns)} 个页签逐个点过去每次都一致（名单按按钮数取，加一页不会被漏）："
          "点了统计页灯就该在统计，不能两处各亮各的",
          len(_order) == len(win.tab_btns) + 1 and all(t[1] for t in per_tab),
          "；".join(f"第{k}页 {d}" for k, d, _ in per_tab if not d))

    # 页签的**几何**：每个标签一字不切，是这一排的全部意义（切了就没人知道第 3 页叫什么；
    # 标签颗数按 `tab_btns` 现取，加一页不会从这句话里溜出去）。
    # 只测默认 300 等于承认"其余档位没测过"——设置页那根滑条允许 240..520，而 240 恰好是
    # 原来那版写法压破 sizeHint 的那一档（tests/probe_tab_fit.py 留了逐档数字）。
    fit_ok, fit_d = tabs_fit(win, tuple(range(240, 521, 20)))
    check("侧栏页签在设置页全量程（240..520，每 20px 一档）五条形不变量全立：排得开／不低于样式要的宽／一字不切／两格不叠／侧栏不吃掉窗口一半",
          fit_ok, fit_d)

    # 量程之外：`config.json` 是用户能手改的（README 与 `python -m app.doctor` 都教人改它），
    # 而设置页那根 spinbox 只夹得住"人按出来的值"。一个 100 会让最后两个页签变成点不到的死键，
    # 所以收口发生在界面这一侧，判据也得在这一侧——量程只有一份出处（settings_dialog.INT_RANGE）。
    clamp_cases = ((100, 240), (239, 240), (240, 240), (300, 300), ("280", 280),
                   (520, 520), (521, 520), (99999, 520), (None, 300), ("abc", 300))
    clamp_bad = [f"{raw!r}→{win._clamped_sidebar_width(raw)}（应为 {want}）"
                 for raw, want in clamp_cases if win._clamped_sidebar_width(raw) != want]
    check("手改 config 的侧栏宽度先按设置页量程收口（越界夹到端点、非数字回默认 300，边界 239/240/520/521 各自归位）",
          not clamp_bad, "；".join(clamp_bad) or f"{len(clamp_cases)} 个输入全按量程/默认收口")

    # ---- 五处「形状靠程序算」的闸：宽／高／字号 ----
    # 这五处的共同点：产品不钉死像素，而是在运行时问 Qt「这些东西要多大」，再照着那个数
    # 摆放窗口。任何一处算少，症状都不是报错而是「屏幕右端少半截字」，而布局仍然"排开了"
    # ——五把机械尺里只有拿 sizeHint／视口做对照的那两把看得见。下面的 U52–U56 一一对应证伪。
    # 量具放本地而不是文件头：capture_sweep 自己 `import test_ui`，文件头引会拿到半成品。
    import capture_sweep as SW

    # ① 侧栏宽度＝五页里最宽那一页的需求。逐页翻过去量（QStackedWidget 的隐藏页没有真视口，
    #   只在该页 current 之后量到的数才算数——2026-10-11 实测：构造期这条读 378、开过调节页
    #   之后同一页读 402，差的正是隐藏滚动区量不到滚动条那一根）。
    cap_side = INT_RANGE["sidebar_width"][1]
    per_page_need: list[str] = []
    page_reds: list[str] = []
    lying: list[str] = []
    _cur0 = win.sidebar_stack.currentIndex()
    for i in range(win.sidebar_stack.count()):
        win.sidebar_stack.setCurrentIndex(i)
        pump(0.12)
        pg = win.sidebar_stack.widget(i)
        cw = form_rows.content_width(pg)
        naive = int(pg.minimumSizeHint().width())
        has_sc = bool(pg.findChildren(QScrollArea))
        if has_sc and cw <= naive:
            lying.append(f"第{i}页 content_width={cw} 不大于滚动区自报的 {naive}（那把尺会撒谎）")
        bad_c = SW.clipped_without_hint(pg) + SW.beyond_viewport(pg)
        if bad_c:
            page_reds.append(f"第{i}页 {bad_c[0][:44]}")
        per_page_need.append(f"{i}:{cw}" + ("(有滚动区)" if has_sc else ""))
    win.sidebar_stack.setCurrentIndex(_cur0)
    pump(0.12)
    need_now = win._pages_need()
    side_w = win.sidebar.width()
    check("侧栏宽 ≥ 逐页翻完后重算的需求（或已顶到量程上限），且每一页都没有裁字／整块掉出视口；"
          "带滚动区那几页的需求必须大于滚动区自报的最小宽（否则等于把撒谎的那把尺当依据）",
          (side_w >= need_now or side_w >= cap_side) and not page_reds and not lying,
          f"侧栏={side_w} 需求={need_now} 上限={cap_side} 逐页需求=[{','.join(per_page_need)}] "
          f"滚动区撒谎没被绕开={lying[:2]} 页面红={page_reds[:2]}")

    # ①′ 侧栏覆盖对账（#70，2026-10-13 接进常跑链）：`capture_sweep.py` 里那三处**手抄名单**
    #   （`SURFACES` 一行、`s_sidebar_*` 一个捕法、捕法里 `_sidebar(N)` 的下标，外加 `widget_of`
    #   的一条根）与 `tab_defs` 的页序逐一对账。纯 AST、不建窗口、不抓屏、不写文件，
    #   所以屏幕睡着也跑得动，也不与看门狗那条"会改文件的扫不许并跑"冲突。
    #   为什么 `probe_surface_cover.py` 自己能跑还要在这里再挂一遍：独立那一腿不在看门狗的六趟里，
    #   加一页时没人记得跑它——而"少抄一处不报错、那一页永远没有档、整趟体检照旧全绿"正是它要治的病
    #   （§6 第 34 条）。量具自己出问题（读不到文件／解析不出）一律记 ENV，不拿环境冒充产品红；
    #   三条出口各自兜住，绝不让这一条把整趟顶成 NameError。
    try:
        import probe_surface_cover as PSC
        try:
            _psc_pages, _psc_tables = PSC.load()
            _psc_rows = PSC.judges(_psc_pages, _psc_tables)
            _psc_bad = [f"{n}（{d[:60]}）" for n, ok, d in _psc_rows if not ok]
            check("逐屏体检的侧栏覆盖：`capture_sweep` 的 SURFACES／捕法／widget_of 三处名单"
                  "必须与 `tab_defs` 的页序一一对上（加一页时少抄任何一处都不许静默过去）",
                  len(_psc_rows) >= 5 and not _psc_bad,
                  f"页序={'/'.join(_psc_pages)}｜判据 {len(_psc_rows)} 条，红={_psc_bad or '无'}")
        except PSC.CannotMeasure as _psc_e:
            env_check("逐屏体检的侧栏覆盖对账（纯 AST，不建窗）", f"连测量对象都没有：{_psc_e}")
        except (OSError, SyntaxError, ValueError, KeyError, TypeError, AttributeError) as _psc_e:
            env_check("逐屏体检的侧栏覆盖对账（纯 AST，不建窗）",
                      f"量具自己没跑成：{type(_psc_e).__name__}: {_psc_e}")
    except ImportError as _psc_e:
        env_check("逐屏体检的侧栏覆盖对账（纯 AST，不建窗）", f"量具 import 不上：{_psc_e}")

    # ② mk_hint：说明行靠换行放下，而不是把整列顶宽、也不是悄悄切掉右端。
    #   前提要在 fixture 上立住（单行需求 > 实得宽），否则这条判据量的是"本来就不会切"。
    _host = QWidget(None)
    _lay = QVBoxLayout(_host)
    _lay.setContentsMargins(8, 8, 8, 8)
    _txt = "把文件或文件夹拖进来即可播放，也可以 Ctrl+O 从菜单里挑"
    _lab = form_rows.mk_hint(_txt, _host)
    _lay.addWidget(_lab)
    _host.show()
    pump(0.2)
    _fm = _lab.fontMetrics()
    _one = _fm.horizontalAdvance(_txt)
    _host.setFixedWidth(max(40, int(_one * 0.45)))
    pump(0.2)
    _held = _lab.width()
    _prem = _one > _held + 1
    _wrapped = (_lab.wordWrap() and _lab.hasHeightForWidth()
                and _lab.heightForWidth(_held) > _fm.height())
    _no_push = int(_host.minimumSizeHint().width()) * 2 < _one
    _hint_clean = (not SW.clipped_labels(_host) and not SW.clipped_without_hint(_host)
                   and not SW.squeezed_widgets(_host))
    # 收尾之前把数取走：`deleteLater()` 之后再去问 `_lab` 会撞上"wrapped C/C++ object deleted"，
    # 而那发生在拼 detail 的字符串里＝判据自己崩在报告路上（红还是次要，栈会把这一条顶成 traceback）。
    _hosts_need = int(_host.minimumSizeHint().width())
    _hfw = _lab.heightForWidth(_held) if _held > 0 else 0
    _host.deleteLater()
    # 名单从**栈自己**取（页名用 `tab_defs[i][0]`、节点用 `sidebar_stack.widget(i)`），不在这里
    # 抄第二份面板名单：这一栏 2026-10-12 刚多了"声音均衡"一页，而**抄名单的那把尺**会把
    # "多出来的一页从来没进过这项检查"读成通过（同第 890 行那条理由）。颗数由 `count` 与
    # `len(tab_defs)` 现场对齐，缺哪一头都当场红——不是靠这里写死一个数字。
    _pages = min(win.sidebar_stack.count(), len(win.tab_defs))
    _panel_wrapped = {str(win.tab_defs[i][0]):
                      sum(1 for t in win.sidebar_stack.widget(i).findChildren(QLabel)
                          if t.wordWrap()) for i in range(_pages)}
    check("mk_hint 的契约在「单行放不下」的格子里成立：换行（hasHeightForWidth 且按实得宽要两行）、"
          "不把整列顶宽（宿主最小宽 < 单行需求的一半）、三把裁字尺一条不报；"
          f"{_pages} 页各自都还有换行说明行（页名与节点都按栈取，加一页不必回来改这份名单）",
          _prem and _wrapped and _no_push and _hint_clean
          and win.sidebar_stack.count() == len(win.tab_defs)
          and all(v >= 1 for v in _panel_wrapped.values()),
          f"单行要 {_one}px／实得 {_held}px（前提立住={_prem}） 换行后高={_hfw} vs 行高 {_fm.height()} "
          f"宿主最小宽={_hosts_need} 干净={_hint_clean} 各页换行说明行数={_panel_wrapped} "
          f"栈={win.sidebar_stack.count()} 定义={len(win.tab_defs)}")

    # ③ 标题条高度：标准字号下这条规则算出来就是 M.title_h，和"钉死 44"的旧写法一模一样——
    #   所以判据必须在**同一次运行里把字放大**才有鉴别力（放大靠子件 setFont，QSS 的 font-size
    #   不覆盖显式 setFont 的控件，tests/probe_fit_teeth.py 量到 20→36px）。
    _tb = win.title
    _kids = _tb.findChildren(QLabel) + _tb.findChildren(QToolButton)
    _saved_font = {k: k.font() for k in _kids}

    def _tb_need() -> int:
        return max((int(k.sizeHint().height()) for k in _kids), default=0)

    _h_base = _tb.height()
    _need_base = _tb_need()
    for k in _kids:
        _f = QFont(k.font())
        _px = _f.pixelSize() if _f.pixelSize() > 0 else max(1, int(_f.pointSize() * 96.0 / 72.0))
        _f.setPixelSize(_px * 3)
        k.setFont(_f)
    pump(0.2)
    _need_big = _tb_need()
    _tb._fit_height()
    pump(0.2)
    _h_big = _tb.height()
    _sqz_big = SW.squeezed_widgets(_tb)
    for k, f in _saved_font.items():
        k.setFont(f)
    _tb._fit_height()
    pump(0.2)
    _h_back = _tb.height()
    check("标题条高度跟着字走：子件字号×3 之后需求从 "
          f"{_need_base} 涨到 {_need_big}，条高必须≥需求+12（上下留白），交回产品能回到标准档；"
          "标准字号下等于 M.title_h 不算通过——那条要的是「放大之后不再停在 44」",
          _need_big > _need_base and _h_big >= _need_big + 12 and _h_back == _h_base
          and not _sqz_big,
          f"标准 高{_h_base}/需{_need_base} 放大 高{_h_big}/需{_need_big}（下限 "
          f"{theme.M.title_h}） 复原 高{_h_back} 压扁={_sqz_big[:1]}")

    # ④ 下拉的 12 字档：像素档会随字号失效，字数档不会。两处工厂（form_rows.add_choice 与
    #   设备下拉）都得在场，且每个下拉合起来时的 sizeHint 真的容得下 12 个「0」。
    _cbs = [cb for node in (win.tweak_panel, win.transform_panel)
            for cb in node.findChildren(QComboBox)]
    _dev = getattr(win.tweak_panel, "cb_device", None)
    if _dev is not None:
        _cbs = [cb for cb in _cbs if cb is not _dev] + [_dev]
    _cb_bad: list[str] = []
    for cb in _cbs:
        _need12 = cb.fontMetrics().horizontalAdvance("0" * 12)
        if cb.minimumContentsLength() < 12:
            _cb_bad.append(f"{type(cb.parent()).__name__} 字数档={cb.minimumContentsLength()}")
        if int(cb.sizeHint().width()) < _need12:
            _cb_bad.append(f"{type(cb.parent()).__name__} sizeHint={int(cb.sizeHint().width())}"
                           f"<12字{_need12}")
    check("每一个下拉合起来也容得下 12 个字：字数档在（minimumContentsLength≥12）、"
          "sizeHint 真的不小于 12 个「0」的宽（钉像素会在大字号档失效，钉字数不会）",
          len(_cbs) >= 8 and _dev is not None and not _cb_bad,
          f"下拉数={len(_cbs)} 设备下拉在={_dev is not None} "
          f"读数={[(int(c.sizeHint().width()), c.minimumContentsLength()) for c in _cbs[:3]]} "
          f"坏={_cb_bad[:3]}")
    win.sidebar_stack.setCurrentIndex(_cur0)
    pump(0.12)

    shot_st, e_st = capture(win, win.sidebar, "统计页侧栏")
    p_st = os.path.join(OUT, "screen-stats-panel.png")
    shot_st.save(p_st)
    st_st = sample_stats(shot_st, 24, 40)
    pixel_check("统计页侧栏", "统计页屏幕留档（行名与实时数值一起目检，且那块像素是我们的窗口）",
                not shot_st.isNull() and not e_st and st_st["distinct"] >= 6,
          f"{e_st}{shot_st.width()}×{shot_st.height()} 颜色{st_st['distinct']} "
          f"亮度{st_st['mean']:.0f} → {p_st}")

    # 侧栏开着但停在别的页时按 I，语义与 Ctrl+L/M/T 同一条：切过去，而不是收起。
    # 这条单独判，是因为收起与切换走的是 _set_sidebar 与 _sidebar_page 两个不同出口。
    win.dispatch("toggle_stats")
    switched = wait_for(lambda: win.sidebar.isVisible()
                        and win.sidebar_stack.currentIndex() == 5, 2.5)
    pump(0.3)
    tab_sw, d_tab_sw = tab_state_agrees(win, 5)
    check("侧栏开着但在列表页时按 I：切到统计页而不是收起（与 Ctrl+L/M/T 同一条规则）",
          switched and tab_sw, d_tab_sw)

    win.dispatch("toggle_stats")
    st_closed = wait_for(lambda: not win.sidebar.isVisible(), 2.5)
    pump(0.3)
    check("已在统计页时再按 I 收起：侧栏隐藏且轮询表停下（QStackedWidget 翻页会 hide，停表挂在 hideEvent 上）",
          st_closed and not sp._timer.isActive(),
          f"可见={win.sidebar.isVisible()} isActive={sp._timer.isActive()}")

    # 9.11 画面页（侧栏第四页）：控件覆盖事实表 / 几何真落内核 / 增强走整条链 / 文案按事实分形
    #      这一页的两种下发形状**不同**（几何一条 set_property、增强一次 vf set），
    #      所以每条判据都点名它是哪一半：把两半混成"改了就行"，就查不出"链里列着名字但
    #      内核把滤镜扔了"那一类（实测硬解帧上会静默禁用，而 vf 列表照样好看）。
    g = win.transform_panel
    win.dispatch("toggle_transform")
    tr_open = wait_for(lambda: win.sidebar.isVisible()
                       and win.sidebar_stack.currentIndex() == 3, 2.5)
    pump(0.35)
    tab_tr, d_tab_tr = tab_state_agrees(win, 3)
    check("Ctrl+P 开到画面页：栈索引、页签勾选、三个灯、各页可见性一致（画面页没专属按钮，灯该全灭）",
          tr_open and tab_tr, d_tab_tr)

    cover, d_cover = controls_cover_table(g._rows, g._choices, g._flags, transform.ALL_KEYS)
    check("画面表每一项在这一页都有控件、且页面上的控件全能回查到表里（表加了项界面漏了＝调不了；界面留孤儿＝改了没人接）",
          cover, d_cover)
    hint_orphan = sorted(set(GP.HINTS) - set(transform.ALL_KEYS))
    check("每项说明（tooltip）写的键全在画面表里（写给一个表里没的键，那句说明就永远不落在任何控件上）",
          not hint_orphan, f"表外键={hint_orphan}")
    act_cover = sorted(k for k, cb in g._choices.items()
                       if [cb.itemData(i) for i in range(cb.count())] !=
                       [o[0] for o in transform.spec_for_cfg(k).options])
    check("两个下拉（比例、旋转）的选项和事实表逐项同序（下拉自己抄一份，表加一档就少一档）",
          not act_cover, f"对不上的下拉={act_cover}")

    # 几何：快捷键 → 配置 → 控件 → 内核回报，四处同一个角。旋转有订阅，所以内核那份是真值。
    win.player.set_transform("rotate", "0", confirm=True)
    pump(0.4)
    statuses: list[str] = []
    g.sig_status.connect(statuses.append)
    win.dispatch("rotate_cycle")
    pump(0.7)
    rot_kernel = win.player.prop("video-rotate", None)
    rot_ui = g._choices["rotate"].currentData()
    check("R 键转一格：配置、下拉、内核回报三处同一个角（三处各写一份就是「界面转了内核没转」）",
          cfg.get("rotate") == "90" and rot_ui == "90"
          and transform.same_value("rotate", "90", rot_kernel),
          f"cfg={cfg.get('rotate')!r} 下拉={rot_ui!r} 内核={rot_kernel!r}")
    check("快捷键走的正是面板那一段代码：状态行念的就是表里的中文档名（键盘与鼠标两个入口两套话就是双份逻辑）",
          bool(statuses) and "旋转" in statuses[-1] and "90°" in statuses[-1],
          f"状态={statuses[-1:]!r}")

    # 增强：一次动作动的是**整条链**，所以判据是链上的名字＋内核日志，而不只是属性回读。
    win.dispatch("mirror_h_toggle")
    pump(0.9)
    chain_txt = g.lbl_chain.text()
    check("H 键开左右镜像：配置为真、内核链上列出 hflip、那一次下发没听到禁用行（链里列着名字≠在跑）",
          bool(cfg.get("mirror_h")) and "hflip" in win.player.vf_names()
          and not win.player.filter_failures()
          and chain_txt.startswith("滤镜链") and "hflip" in chain_txt,
          f"cfg={cfg.get('mirror_h')} 链={win.player.vf_names()} 坏行={win.player.filter_failures()} "
          f"状态行={chain_txt!r}")
    win.dispatch("mirror_h_toggle")
    pump(0.9)
    check("再按一次 H：链清空且状态行换成「（空）」（只测开不测关的话，「关不掉」就混得过去）",
          (not cfg.get("mirror_h")) and not win.player.vf_names()
          and "（空）" in g.lbl_chain.text(),
          f"cfg={cfg.get('mirror_h')} 链={win.player.vf_names()} 状态行={g.lbl_chain.text()!r}")

    # 内核日志那句"滤镜被内核禁用"必须进状态行，而旁白式的日志不许覆盖它。
    g._on_engine_log("滤镜被内核禁用：Disabling filter hflip.")
    bad_line = g.lbl_chain.text()
    g.repaint_from_state()
    check("状态行两头都夹得住：内核说「禁用」时必须是那句原话，重画后必须换回链的实际内容（恒红与恒绿都是坏判据）",
          "禁用" in bad_line and "hflip" in bad_line and "禁用" not in g.lbl_chain.text(),
          f"注入后={bad_line!r} 重画后={g.lbl_chain.text()!r}")

    # 整族复位：先动两项（一几何一增强），复位后表里每一项都得回默认、控件跟着重画。
    win.dispatch("rotate_cycle")
    win.dispatch("crop_cycle")
    pump(0.9)
    moved = [k for k in transform.ALL_KEYS
             if cfg.get(k) != transform.defaults()[k]]
    btn = g.btn_reset
    btn.click()
    pump(1.0)
    left = [k for k in transform.ALL_KEYS if cfg.get(k) != transform.defaults()[k]]
    repaint_ok = (g._choices["rotate"].currentData() == transform.defaults()["rotate"]
                  and g._choices["crop_ratio"].currentData() == transform.defaults()["crop_ratio"]
                  and not g._flags["mirror_h"].isChecked())
    check(f"画面复位一次管全（复位前改动了 {len(moved)} 项）：配置每项回默认、下拉与勾选跟着回位、链清空",
          bool(moved) and not left and repaint_ok and not win.player.vf_names()
          and transform.same_value("rotate", "0", win.player.prop("video-rotate", None)),
          f"复位前={moved} 复位后仍非默认={left} 控件回位={repaint_ok} "
          f"内核旋转={win.player.prop('video-rotate')} 链={win.player.vf_names()}")

    win.player.seek_abs(0)      # 这一段泵掉约 6 秒：把样片倒回开头，后面的全屏/截屏段才有整片可用
    tr_shot, e_tr = capture(win, win.sidebar, "画面页侧栏")
    p_tr = os.path.join(OUT, "screen-transform-panel.png")
    tr_shot.save(p_tr)
    st_tr = sample_stats(tr_shot, 24, 40)
    pixel_check("画面页侧栏", "画面页屏幕留档（滑轨/下拉/勾选与状态行一起目检，且那块像素是我们的窗口）",
                not tr_shot.isNull() and not e_tr and st_tr["distinct"] >= 6,
          f"{e_tr}{tr_shot.width()}×{tr_shot.height()} 颜色{st_tr['distinct']} "
          f"亮度{st_tr['mean']:.0f} → {p_tr}")
    win.dispatch("toggle_transform")
    tr_closed = wait_for(lambda: not win.sidebar.isVisible(), 2.5)
    pump(0.25)
    check("再按 Ctrl+P 收起（这条尾巴不能污染后面的全屏与截屏段）", tr_closed,
          f"可见={win.sidebar.isVisible()}")

    # —— 入口对账：快捷键表、dispatch 分支、菜单三处必须说同一套动作名。
    #    表里写一个 dispatch 不认的名字，症状是「按了没反应、也不报错」——
    #    而 QMenu.exec 是阻塞的，测试里真弹菜单会把整轮卡死，所以拿常量表对账。
    cov, d_cov = keymap_covered({a for _, a, _ in KEYMAP},
                                inspect.getsource(MainWindow.dispatch))
    check("快捷键表里每个动作名在 dispatch 都真有分支（表与分支写在同文件两处，改一处忘另一处没人拦）",
          cov, d_cov)
    orphan = sorted(menu_action_names() - {a for _, a, _ in KEYMAP})
    check("菜单条目用的动作名快捷键表全都认（菜单与快捷键走同一段 dispatch，不是各写一份）",
          not orphan, f"菜单里表外动作={orphan}")
    check("更多按钮与右键菜单共用同一张视图动作表（不会出现右键有统计、更多里没有）",
          set(MORE_ACTIONS) <= {a for a, _ in VIEW_ACTIONS}, f"更多={MORE_ACTIONS}")

    # —— 画面那一族的九个动作：不进 KERNEL_ONLY 是**设计决定**（那些值全进启动参数，
    #    内核没起来时按快捷键照样做得成事——记进设置、播放时一次带上），所以双向判：
    #    九个动作全在快捷键表里（有人改了名字这份清单就失效，那是"设了没用"的反面）
    #    ＋一个都没混进 KERNEL_ONLY（混进去的症状是离线按 R 回一句"播放内核还没启动"，
    #    而那句话是错的：值已经记下了）。
    TR_ACT = ("rotate_cycle", "aspect_cycle", "crop_cycle", "keepaspect_toggle",
              "mirror_h_toggle", "mirror_v_toggle", "panscan_toggle", "transform_reset",
              "toggle_transform")
    known = {a for _, a, _ in KEYMAP}
    stale = sorted(set(TR_ACT) - known)
    mixed = sorted(set(TR_ACT) & set(KERNEL_ONLY))
    check("画面一族九个动作：快捷键表里全有（这份清单没过期）、且一个都不在 KERNEL_ONLY 里（离线也做得成事）",
          not stale and not mixed, f"表里找不到={stale} 混进 KERNEL_ONLY={mixed}")

    # —— 睡眠定时：余量、拒收、取消、到点真落内核
    t = win.sleep
    states: list[str] = []
    t.sig_state.connect(states.append)
    expired: list[str] = []
    t.sig_expire.connect(expired.append)

    armed_ok = t.arm(30, "pause") and t.armed
    rem = t.remaining()
    tick_live = bool(t._fire_timer is not None and t._fire_timer.isActive())
    check("设 30 分钟后暂停：真布了一个在跑的 QTimer，余量按绝对截止现算（不是每秒减一的假倒计时）",
          armed_ok and 29.4 * 60 <= rem <= 30 * 60 and t.action() == "pause" and tick_live,
          f"armed={t.armed} 余量={rem:.1f}s 动作={t.action()} 在途定时器={tick_live}")

    states.clear()
    bad_act = t.arm(5, "reboot")
    kept = t.armed and t.action() == "pause" and t.remaining() > 29 * 60
    check("不认的到点动作当场拒收、且不动已经设好的定时（以为设的是退出、实际只暂停，机器整晚醒着没人知道）",
          bad_act is False and kept and any("不认的动作" in s for s in states),
          f"返回={bad_act} 还挂着={t.armed} 动作={t.action()} 余量={t.remaining():.1f} 提示={states[:2]}")

    out_of_range = (t.arm(0, "pause") is False) and (t.arm(1000, "quit") is False) and t.armed
    check("分钟数越界拒收（下限 1、上限 720），已设的定时不受影响", out_of_range,
          f"0 与 1000 分钟都拒={out_of_range} 还挂着={t.armed} 余量={t.remaining():.1f}")

    t.cancel()
    canceled = (not t.armed) and t.remaining() == 0.0 and t._fire_timer is None
    check("取消把在途的 QTimer 真的撤销了（singleShot 发出去收不回，那是最难查的一类 bug）",
          canceled, f"armed={t.armed} 余量={t.remaining()} fire_timer={t._fire_timer}")

    # 到点动作真落内核：先把片倒回开头，再验"暂停→恢复"。样片只有 10 秒，而前面几段
    # （含 9.11 画面页那 6 秒泵）随时可能已经跑到片尾——那时内核在 EOF 上写 pause=yes，
    # play() 恢复不了它，这条判据就变成拿墙钟运气当结论（实测连跑两轮一红一绿，
    # 红的正是"恢复播放"，而不是产品坏了）。倒带是把**测量条件**摆正，不动判据。
    win.player.seek_abs(0)
    win.player.play()
    playing = wait_for(lambda: win.player.running and not bool(win.player.prop("pause")), 3.0)
    expired.clear()
    t.arm(1, "pause")
    t._fire()                       # QTimer 到点走的就是这个方法；1 分钟那条路没人等得起
    paused_by_timer = wait_for(lambda: bool(win.player.prop("pause")), 3.0)
    check("到点动作真落内核：sig_expire → 主窗口执行 → 内核回报 pause，且窗口没被 quit 顺手关掉",
          playing and paused_by_timer and expired == ["pause"] and t._fire_timer is None
          and win.isVisible(),
          f"信号={expired} 内核 pause={win.player.prop('pause')} 定时器残留={t._fire_timer}")
    win.player.seek_abs(0)        # 同上：倒回开头再恢复，别让"片放完了"冒充"恢复坏了"
    win.player.play()
    resumed = wait_for(lambda: not bool(win.player.prop("pause")), 3.0)
    check("恢复播放（这条尾巴不能污染后面的全屏与截屏段）", resumed,
          f"pause={win.player.prop('pause')}")

    sld = SleepDialog(t, win)
    sld.show()
    pump(0.25)
    btn_preset = [b for b in sld.findChildren(QPushButton) if b.text() == "15"]
    btn_on = [b for b in sld.findChildren(QPushButton) if b.text() == "启用"]
    btn_off = [b for b in sld.findChildren(QPushButton) if b.text() == "取消定时"]
    if btn_preset:
        btn_preset[0].click()
        pump(0.1)
    spin_ok = bool(btn_preset) and sld.spin.value() == 15
    off_disabled = bool(btn_off) and not btn_off[0].isEnabled()
    if btn_on:
        btn_on[0].click()
        pump(0.2)
    dialog_armed = t.armed and t.action() == "pause" and int(round(t.remaining() / 60.0)) == 15
    label_ok = "还剩" in sld.lbl_state.text()
    if btn_off:
        btn_off[0].click()
        pump(0.2)
    dialog_cancel = not t.armed and t._fire_timer is None
    act_datas = [sld.cb_action.itemData(i) for i in range(sld.cb_action.count())]
    check("睡眠定时对话框：预设→启用→显示余量→取消，全程只有这一个定时在跑，动作文案取自 ACTIONS 表",
          spin_ok and off_disabled and dialog_armed and label_ok and dialog_cancel
          and act_datas == [k for k, _ in ACTIONS],
          f"预设={spin_ok} 未设时取消禁用={off_disabled} 启用后={dialog_armed} "
          f"文字={sld.lbl_state.text()!r} 取消后 armed={t.armed} 下拉={act_datas}")
    sld.close()
    pump(0.15)

    # —— 设置页：四档对应事实表、重启项不许假装已生效、立即项必须当场跟上
    win.dispatch("settings")
    sdlg = win.settings_dlg
    opened = wait_for(lambda: sdlg is not None and sdlg.isVisible(), 2.5)
    win.dispatch("settings")
    pump(0.2)
    check("Ctrl+, 打开设置页，第二次开复用同一个实例（反复 new 会留下一堆没人认领的对话框）",
          opened and isinstance(sdlg, SettingsDialog) and win.settings_dlg is sdlg,
          f"可见={None if sdlg is None else sdlg.isVisible()} 同实例={win.settings_dlg is sdlg}")

    mism: list[str] = []
    for key, dflt in perf.DEFAULTS.items():
        w = sdlg._widgets.get(key)
        opts = perf.options_for(key)
        if not isinstance(w, QComboBox):
            mism.append(f"{key}：设置页没有下拉（这一档等于没做界面）")
            continue
        if not opts:
            mism.append(f"{key}：事实表没给可选值")
            continue
        datas = [w.itemData(i) for i in range(w.count())]
        if datas != [v for v, _ in opts]:
            mism.append(f"{key}：下拉档位={datas} 与事实表 {[v for v, _ in opts]} 不一致")
        bad = [v for v in datas if not perf.valid(key, v)]
        if bad:
            mism.append(f"{key}：下拉里有本机内核不认的档位 {bad}")
        if str(w.currentData()) != str(cfg.get(key, dflt)):
            mism.append(f"{key}：界面选中 {w.currentData()!r}，配置里是 {cfg.get(key, dflt)!r}")
    check("性能四档的下拉与 engine/perf 事实表一一对应（在界面里另抄一份清单，正是设置页最典型的失效方式）",
          not mism, "；".join(mism))

    # 生效时机标注：V4 之后"整节同一档"由节标题说一次、"立即生效"是默认档不标，
    # 所以判据不能再写死"每行必须有一枚胶囊"（那是旧形状），也不能反过来放松成"有没有都行"。
    # 这里独立重算一遍期望（不叫产品那个函数，否则产品怎么写判据就怎么过），三条腿各钉一件事：
    #   ① 该标的行必须标对（写错＝告诉用户改完就能看到，而他等不到）；
    #   ② 不该标的行必须一枚都没有（把五枚"重启后生效"加回来就撞这一条）；
    #   ③ 省略的理由必须在屏幕上看得见（标题里得有那句话），否则"没标"会被读成"漏了"。
    _chip_ok, _chip_msg = chips_agree(sdlg)
    check("生效时机：整节同一档由标题说一次、默认档不标，其余每行必须标对（多标少标都红）",
          _chip_ok, _chip_msg)

    # ⑤ 设置窗的加宽闸：标准字号下 520（默认宽）本来就 > 需求 391，闸是**惰性的**——照自然态判
    #   它就是恒真。所以当场把它压到需求以下（Qt 只让它缩到自身最小宽 308），再问闸能不能把它
    #   修回"一个字都不被切"的宽；上限那一头（屏幕可用宽）另说，本机 3840 宽兜不住它就是产品坏。
    _s_need0 = int(sdlg.content_need)
    _s_w0 = int(sdlg.width())
    _s_screen = sdlg.screen() or QApplication.primaryScreen()
    _s_avail = int(_s_screen.availableGeometry().width()) if _s_screen is not None else 0
    sdlg.resize(300, sdlg.height())
    pump(0.3)
    _s_squeezed = int(sdlg.width())
    _s_reds = SW.clipped_without_hint(sdlg) + SW.beyond_viewport(sdlg)
    _s_back = int(sdlg._fit_width())
    pump(0.3)
    _s_clean = SW.clipped_without_hint(sdlg) + SW.beyond_viewport(sdlg)
    _s_need1 = int(sdlg.content_need)
    sdlg.resize(_s_w0, sdlg.height())
    pump(0.2)
    check("设置窗被压到需求以下时自己加宽回去：content_need 与当场重算一致、加宽后三把裁字尺全清，"
          "且没越过屏幕上限（越界宁可留一条被切的红，也不造一个关不掉的窗口）",
          _s_squeezed < _s_need0 and bool(_s_reds) and _s_back >= _s_need0
          and not _s_clean and _s_need1 == form_rows.content_width(sdlg)
          and _s_back <= max(0, _s_avail - 80),
          f"需求={_s_need0} 自然宽={_s_w0} 压到 300 实得={_s_squeezed}（该切：{len(_s_reds)} 条，"
          f"首条 {_s_reds[0][:40] if _s_reds else '无'}） 修回={_s_back} 修后红={_s_clean[:1]} "
          f"重算={_s_need1} 屏幕可用={_s_avail}")

    btn_save = [b for b in sdlg.findChildren(QPushButton) if b.text() == "保存"][0]
    applied: dict[str, object] = {}
    sdlg.sig_applied.connect(lambda ch, rs: applied.update({"changed": ch, "restart": rs}))
    hw_orig = str(cfg.get("hwdec"))
    accent_orig = str(cfg.get("theme_accent"))
    side_orig = int(cfg.get("sidebar_width"))
    cb_hw = sdlg._widgets["hwdec"]
    hw_alt = next(v for v, _ in perf.options_for("hwdec") if str(v) != hw_orig)
    cb_hw.setCurrentIndex(cb_hw.findData(hw_alt))
    cur_before = win.player.prop("hwdec-current")
    applied.clear()
    btn_save.click()
    pump(0.4)
    check("改硬解档位并保存：配置写进去、按重启后生效报出来，而内核眼下的真值一个字没变（界面不许说成已经生效）",
          str(cfg["hwdec"]) == str(hw_alt) and applied.get("restart") == ["hwdec"]
          and not applied.get("changed") and "重启" in sdlg.lbl_note.text()
          and win.player.prop("hwdec-current") == cur_before,
          f"cfg={cfg['hwdec']} changed={applied.get('changed')} restart={applied.get('restart')} "
          f"提示={sdlg.lbl_note.text()!r} hwdec-current={cur_before}→{win.player.prop('hwdec-current')}")

    accent_alt = next(v for v, _ in theme.ACCENTS if v.upper() != theme.ACCENT.upper())
    side_alt = side_orig + 20
    sdlg._widgets["theme_accent"].setCurrentIndex(
        sdlg._widgets["theme_accent"].findData(accent_alt))
    sdlg._widgets["sidebar_width"].setValue(side_alt)
    applied.clear()
    btn_save.click()
    pump(0.5)
    check("主题色与侧栏宽度是立即生效：保存后令牌、窗口样式表、侧栏宽度当场跟上，且不混进要重启那份名单",
          theme.ACCENT.upper() == accent_alt.upper()
          and accent_alt.upper() in win.styleSheet().upper()
          and win.sidebar.width() == side_alt
          and applied.get("changed") == {"theme_accent": accent_alt, "sidebar_width": side_alt}
          and not applied.get("restart"),
          f"令牌={theme.ACCENT} 表里含新色={accent_alt.upper() in win.styleSheet().upper()} "
          f"侧栏宽={win.sidebar.width()}（应为 {side_alt}）changed={applied.get('changed')} "
          f"restart={applied.get('restart')}")

    # 非法档位能不能被递给内核：--falsify U4 证过「内核收了非法值不报错、只是静默退回
    # 软解」，所以设置页这一头必须拦。这里手动往下拉里塞一条非法值来走那条防线。
    cb_hw.blockSignals(True)
    cb_hw.insertItem(0, "非法档探针", "bogus-hwdec")
    cb_hw.setCurrentIndex(0)
    cb_hw.blockSignals(False)
    cfg_before = str(cfg["hwdec"])
    applied.clear()
    btn_save.click()
    pump(0.3)
    blocked = (("不认的档位" in sdlg.lbl_note.text() or "不是本机内核认的" in sdlg.lbl_note.text())
               and str(cfg["hwdec"]) == cfg_before and not applied)
    cb_hw.removeItem(0)
    cb_hw.setCurrentIndex(cb_hw.findData(cfg_before))
    check("下拉里出现内核不认的档位时保存必须被拦下：配置不写、信号不发、并把话说清楚",
          blocked, f"提示={sdlg.lbl_note.text()!r} cfg={cfg['hwdec']} 发出去={applied}")

    odd = next(v for v in perf.HWDEC_ALL if not any(str(v) == str(o) for o, _ in
                                                    perf.options_for("hwdec")))
    cfg["hwdec"] = odd
    sdlg.reload_from_cfg()
    pump(0.2)
    n_before = cb_hw.count()
    sdlg.reload_from_cfg()
    pump(0.2)
    custom_ok = (cb_hw.currentText().startswith("当前") and str(cb_hw.currentData()) == str(odd)
                 and getattr(cb_hw, "_gelu_custom", False) and cb_hw.count() == n_before)
    check("配置里的档位界面没列出来时（手改过、或换过内核版本）显式列成当前 X 并选中，"
          "不悄悄跳到第 0 项；重开一次也不会越加越多",
          custom_ok, f"选中={cb_hw.currentText()!r}/{cb_hw.currentData()!r} "
                     f"项数={cb_hw.count()}（第一次 {n_before}）")

    cb_hw.setCurrentIndex(cb_hw.findData(hw_orig))
    sdlg._widgets["theme_accent"].setCurrentIndex(
        sdlg._widgets["theme_accent"].findData(accent_orig))
    sdlg._widgets["sidebar_width"].setValue(side_orig)
    applied.clear()
    btn_save.click()
    pump(0.5)
    restored = (str(cfg["hwdec"]) == hw_orig and theme.ACCENT.upper() == accent_orig.upper()
                and win.sidebar.width() == side_orig and int(cfg["sidebar_width"]) == side_orig)
    check("三项都还原回原值（自测档案不留脏配置，且还原走的也是同一条保存链）", restored,
          f"cfg hwdec={cfg['hwdec']} 令牌={theme.ACCENT} 侧栏宽={win.sidebar.width()} "
          f"cfg 宽={cfg['sidebar_width']}")
    sdlg.close()
    pump(0.2)
    win.player.seek_abs(3.0)
    pump(0.3)
    win.osd._hide.stop()
    win.osd.hide()

    # 10. 全屏
    win.toggle_fullscreen(True)
    scr_geo = win.screen().geometry()
    check("全屏：窗口覆盖整个显示器",
          wait_for(lambda: bool(win.windowState() & Qt.WindowFullScreen), 3.0)
          and win.width() >= scr_geo.width() - 2 and win.height() >= scr_geo.height() - 2,
          f"{win.width()}×{win.height()} vs {scr_geo.width()}×{scr_geo.height()}")

    # 10.1 全屏那一瞬**画面**铺没铺满（R20：用户实测"窗口全屏了，画面只在屏幕中间一块、四周留黑"）。
    # 上一条只量到"窗口覆盖整屏"就收工，而这一幕里窗口是对的——黑边来自它里面那两层，得分开量：
    #   ① 原生矩形：mpv 的渲染窗口是 `--wid` 嵌进我们容器的原生子窗口，**不随父窗口自动缩放**
    #      （`win32.fit_child_windows` 必须每次 resize 手动铺），所以量现成的 `mpv_fills`；
    #   ② 像素形状：子窗口铺满了也不等于画面铺满了，还要看抓到的那块上黑边留在**哪几边**——
    #      「居中偏小／贴左上／贴右下／不规则留黑」是缺陷，「铺满／上下留黑／左右留黑」里
    #      后两档是片子自己的遮幅（样片与原盘都有），属正常。
    # 形状尺与 R20 探针**共用同一把**（`probe_fullscreen_fit.ink_bbox/shape_of`，它的已知答案档与
    # 证伪注入**条数由 `--selfcheck` / `--selfcheck-mutate` 自己打印**，这里不抄；其中 ⑦/M7 那一对
    # 钉的就是"没铺到的那圈是主题底色 #17181C 而不是纯黑"时不许读成铺满）；这里不另立口径，
    # 免得两把尺各判各的。
    # 取两拍：+0.6s（全屏动画与 resizeEvent 里那次 refit 都在这拍）与 +3.4s（越过
    # HIDE_AFTER_MS=2600ms 的控制条自动收起——收起时 `_maybe_autohide` 还会再铺一次，
    # 而光标压在控制条上那一次**不发生**，正好是要盯的那一支；两支都量才盖得住）。
    # 判据前提：全屏态＋内核在放＋这块像素可用。被**别的过程**的窗口挡住时 `pixel_check` 记 ENV，
    # 既不算通过也不算产品坏；自家浮层挡住或真抓不到 → 照旧 FAIL（分流在 capture() 里，不在这另写）。
    for _tag, _dt in (("+0.6s", 0.6), ("+3.4s", 2.8)):
        pump(_dt)
        _fills, _fd, _shape, _pd = fit_reading(win.video, win, f"全屏{_tag}")
        check(f"全屏{_tag}：mpv 子窗口铺满容器客户区（--wid 不会跟着父窗口缩放）", _fills, _fd)
        pixel_check(f"视频区·全屏{_tag}", f"全屏{_tag}：画面形状落在正常档（实测 {_shape}）",
                    _shape in FS_OK_SHAPES, _pd)
        if _tag != "+0.6s":
            continue
        # 宿主**亮不亮**只能在这一拍问：+3.4s 那一拍已过 `HIDE_AFTER_MS=2600`，控制条按产品
        # 规矩就是收着的（同一拍上另有一条判据要求「条收起时宿主顶层窗要一起藏」）。R20 真机
        # 那条「宿主矩形=(0,1004,1920×76) … 可见=False」的红就是把可见性要求排在了收起之后——
        # 拿收起那一拍要求亮，判据物理上不可能绿，坏的是时序而不是产品（同一条读数在两分钟后的
        # 「一起藏」那条上以同一个 `可见=False` 跑绿，就是这件事的现场证据：
        # tests/out/accept-ui-r20-watch-0244.log 第 167 行与第 175 行）。
        _c_aw, _c_awd = chrome_reading(win)
        _h_aw, _h_awd = host_reading(win)
        check("前提：+0.6s 这一拍控制条必须真的醒着"
              "（不醒着的话下一条的「宿主可见」就是在量一个不存在的状态——"
              "前提塌了要点名塌，不许让判据悄悄换意思）",
              _c_aw["bar_up"], _c_awd)
        check("开着浮层进全屏：条真的搬进宿主，宿主通栏、贴下沿、压在画面那块上面，"
              "而且**醒着这一拍看得见**（`full_stage` 只证明舞台铺满，证明不了控制条"
              "看得见也点得到——搬过去却摆错位置的那一版，用户骂的是第二句「全屏之后控制条没了」）",
              (not win.cfg["float_controls_in_fs"]
               or (_h_aw["hosting"] and _h_aw["is_hosting"] and _h_aw["visible"]
                   and _h_aw["full_w"] and _h_aw["bottom"] and _h_aw["over_video"])), _h_awd)

    # 10.2 那块舞台被谁压扁了（R20 的根因就在这一条，2026-10-19 用户实测＋截图量数）。
    # 上一小节只证明"子窗口=容器"，而**容器本身不是整块屏**时画面照样只剩中间一块：
    # 标题栏与控制条都排在 QVBoxLayout 的行里（airspace：mpv 的子窗口压在 Qt 画的东西上面，
    # 条只能占行、不能覆盖），所以"谁在场"直接决定画面按宽还是按高适配。各自多高不在这里手抄，
    # 由判据详情当场印（`舞台=A×B 屏=C×D 标题栏=…(高…) 条=…(高…) 让行=Npx`）。
    # 判据走 R20 那把几何尺（`chrome_of`，字段与 qt_block 同形状；它的已知答案档与证伪注入的
    # **条数由 `--selfcheck` / `--selfcheck-mutate` 自己打印**，抄进注释的每一处都会在下次加档时变谎话）。
    # R21 换了口径：条住在浮层里时它**一分高度都不该扣**（`by_bar=0`），所以"舞台＝整块屏"第一次
    # 成了可以要求的判据（`full_stage`）；侧栏挤掉的宽由 `by_sidebar` 归因，`overlay_off` 专抓
    # "设置开着浮层、条却还占着布局行"（＝ChromeHost 没接手，不是用户选的退路）。
    _c_a, _c_ad = chrome_reading(win)
    check("全屏当下：标题栏必须藏着、浮层该接手就得接手、舞台不许比「该给的」还矮"
          "（R20 的 D 档＋R21 的 D2 档：chrome 占布局 ⇒ 16:9 画面只能按高适配 ⇒ 左右各一圈黑）",
          (not _c_a["d"] and not _c_a["h_short"] and not _c_a["overlay_off"]
           and (win.sidebar.isVisible() or not _c_a["w_short"])), _c_ad)
    check("开着 `float_controls_in_fs` 进全屏：控制条必须真的搬出布局，"
          "舞台当场等于整块屏（这条就是「退路模式」与「浮层生效」的分界——"
          "键关掉时它必须整条让位，所以判据按配置分流，不是一句无条件的美化）",
          (not win.cfg["float_controls_in_fs"]
           or (_c_a["bar_hosted"] and _c_a["full_stage"])), _c_ad)
    _h_a, _h_ad = host_reading(win)
    check("收起之后宿主的几何契约仍然成立：条的父还是它、还是通栏、还是贴下沿、还压在画面那块上面"
          "（**这里不问可见性**：这一拍按产品规矩就该是藏着的，可见性排在上面 §10.1 的"
          "「醒着那一拍」问，收起态由下面那条「一起藏」问——三拍各问各的，一条判据不许跨两态）",
          (not win.cfg["float_controls_in_fs"]
           or (_h_a["hosting"] and _h_a["is_hosting"]
               and _h_a["full_w"] and _h_a["bottom"] and _h_a["over_video"])), _h_ad)

    # 光标划过画面那一拍（用户按下「全屏」之后紧接着做的第一件事）。
    # 这里发的是**信号本身**（sig_hover → _wake_ui）：30ms 轮询 GetCursorPos 那一头由 R20 探针
    # 用真光标量（`hover_wake`，sendEvent 塞 MouseMove 它根本不看），两头合起来才盖住整条链。
    win.video.sig_hover.emit(QPoint(win.video.width() // 2, win.video.height() // 2))
    pump(0.35)
    _c_b, _c_bd = chrome_reading(win)
    check("悬停唤醒只许唤回控制条：标题栏必须仍藏着、舞台不许再矮一条标题栏"
          "（R20 的直接复现点：旧实现 `_wake_ui` 连着把标题栏 setVisible(True)，"
          "16:9 塞进被压扁的盒子就正是「画面只在屏幕中间一块」）",
          (not _c_b["d"] and not _c_b["h_short"] and _c_b["bar_up"] and not _c_b["overlay_off"]
           and (win.sidebar.isVisible() or not _c_b["w_short"])), _c_bd)
    # 唤回来的那条必须还住在浮层里：否则"唤醒"这个手势本身就把舞台压矮一条（左右各一圈黑），
    # 而这正是用户按下全屏后紧接着做第一个动作时看到的那一幕。
    check("悬停唤回来的控制条不许顺手搬回布局（`bar_hosted` 必须一路为真、"
          "舞台仍等于整块屏）",
          (not win.cfg["float_controls_in_fs"]
           or (_c_b["bar_hosted"] and _c_b["full_stage"])), _c_bd)

    # 唤醒之后自动收起必须真的把那一行还回来：条一藏，舞台就该＝整块屏。
    pump(3.0)
    _c_c, _c_cd = chrome_reading(win)
    if _c_c["bar_up"]:
        env_check("全屏悬停后自动收起要把舞台还给整块屏（rows_lost ≤ 2）",
                  f"3.0s 后控制条仍醒着（真光标大概正压在条上，`_maybe_autohide` 那一支不响）："
                  f"{_c_cd}")
    else:
        check("全屏悬停后自动收起要把舞台还给整块屏：条藏、标题栏藏、屏高一行都不让"
              "（rows_lost ≤ 2）",
              (not _c_c["d"] and not _c_c["h_short"] and _c_c["rows_lost"] <= 2
               and (win.sidebar.isVisible() or not _c_c["w_short"])), _c_cd)
        _h_c, _h_cd = host_reading(win)
        check("条收起时宿主顶层窗要一起藏：留一块没人认领、又挡鼠标的空底板，"
              "形态就是「画面底下凭空多一条按了没反应的黑边」（`_set_bar_shown` 只动 bar 时踩过）",
              not _h_c["visible"], _h_cd)
    # 自动收起那一支是 singleShot：被"光标还压在条上"挡回去时**必须重挂下一趟**，
    # 不重挂就是"一开始是好的，手一抖之后那条黑边长驻"（2026-10-20 全屏审查 §2 点名的三条错位之一）。
    # `underMouse()` 是 Qt 自己按真实鼠标算的，程序没法"造"一次悬停，所以这里临时换成一句谎话、
    # 量完立刻换回来——换回来这一步失败比判据本身更危险（后面每一拍都活在那句谎话里）。
    # **先把真光标请出条外**（同 §9 悬停预览那一手，落点取画面左上角）：用户离开机器时鼠标
    # 停在哪儿没有保证，若正好压在屏幕下沿那条上，复原之后 `_maybe_autohide` 每次都按产品规矩
    # 重挂——最后那条"该收起了"会拿**正确的产品行为**判成红。这不是放宽判据：`underMouse` 的
    # 真假仍由产品自己算，只是把"鼠标此刻在不在条上"这个环境前提做成已知值，量完再放回原处。
    _cur_save = QCursor.pos()
    QCursor.setPos(win.video.mapToGlobal(QPoint(6, 6)))
    pump(0.15)
    win._set_bar_shown(True)
    pump(0.2)
    _orig_under = win.bar.underMouse
    win.bar.underMouse = lambda: True
    win._arm_autohide()
    pump(3.0)                                   # 让它响一次：那一支应当被"还压着"挡回去
    _armed = win._hide_timer.isActive()
    win.bar.underMouse = _orig_under
    check("自动收起被「光标还压在条上」挡回去时必须重挂下一趟（`_hide_timer` 是 singleShot，"
          "不重挂＝这一行从此长驻，用户那边是「全屏里那条控制条再也收不掉、画面永远矮一截」）",
          _armed and win.bar.isVisible(),
          f"挡回去之后定时器还挂着={_armed} 条仍醒着={win.bar.isVisible()}")
    pump(3.2)                                   # 真光标已在画面左上角，下一趟必须真的收
    _c_d, _c_dd = chrome_reading(win)
    check("重挂的那一趟落定之后确实收起了（舞台还给整块屏）",
          (not _c_d["bar_up"] and _c_d["rows_lost"] <= 2), _c_dd)
    QCursor.setPos(_cur_save)                   # 探针不把手搬走：量完放回用户原来的位置
    pump(0.1)

    win.toggle_fullscreen(False)
    check("退出全屏回到窗口态", wait_for(lambda: not (win.windowState() & Qt.WindowFullScreen), 3.0),
          f"state={int(win.windowState())}")

    # 10.3 侧栏与全屏的往来（R21：控制条压的是**纵向**、侧栏挤的是**横向**，同一条债的两半。
    #      2560 宽屏开着 260 的侧栏 ⇒ 舞台 2300×… ⇒ 16:9 按宽适配 ⇒ 上下各一条黑，
    #      用户读到的还是那句「全屏之后周围显示不全」）。
    #      先把前提做出来（侧栏确实挤得动宽度），否则"全屏收起它"这条判据是空转。
    _w_side_off = win.video.width()
    win._set_sidebar(True, 0)
    pump(0.4)
    _w_side_on = win.video.width()
    _side_w = win.sidebar.width()
    check("侧栏开着确实把舞台挤窄一整栏（前提：挤不动就说明后面那几条在量一个不存在的东西）",
          _w_side_on <= _w_side_off - max(1, _side_w - 2),
          f"关={_w_side_off} 开={_w_side_on} 侧栏宽={_side_w}")
    win.toggle_fullscreen(True)
    pump(0.8)
    _c_s, _c_sd = chrome_reading(win)
    check("进全屏要把侧栏收起：`by_sidebar` 必须归零、`w_short` 不许响"
          "（R21 的横向半边 ⇒ 上下各一圈黑；尺子的已知答案是 ⑨/⑬ 那两档）",
          (not win.sidebar.isVisible() and _c_s["by_sidebar"] == 0 and not _c_s["w_short"]), _c_sd)
    win.toggle_fullscreen(False)
    pump(0.8)
    check("退出全屏要把侧栏原样还给用户：收起只是全屏期间的临时状态，"
          "不是把用户的设置改掉（页签还得停在他原来那一页）",
          (win.sidebar.isVisible() and win.sidebar_stack.currentIndex() == 0),
          f"开={win.sidebar.isVisible()} 页={win.sidebar_stack.currentIndex()}")
    # 反向半边：全屏里**用户自己**打开的东西，退出时不许被"还原"强行收掉。
    win._set_sidebar(False)
    pump(0.3)
    win.toggle_fullscreen(True)
    pump(0.6)
    win._set_sidebar(True, 1)                   # 全屏里显式开到媒体库页
    pump(0.4)
    win.toggle_fullscreen(False)
    pump(0.6)
    check("全屏里用户显式打开的侧栏，退出时不许被还原逻辑强行收掉"
          "（显式操作优先于进出全屏的默认值；`_set_sidebar` 里那一句销账就是为这一条）",
          win.sidebar.isVisible() and win.sidebar_stack.currentIndex() == 1,
          f"开={win.sidebar.isVisible()} 页={win.sidebar_stack.currentIndex()}")
    win._set_sidebar(False)
    pump(0.3)

    # 10.4 退路模式（`float_controls_in_fs=False`）：控制条搬回布局那一行。
    #      这一档存在的意义是"浮层在个别机器上不弹菜单时用户能退回旧行为"，所以它必须**真的**能退，
    #      而且退了之后 `overlay_off` 不许响（那会把用户自己选的退路判成 bug）。
    win.cfg["float_controls_in_fs"] = False
    try:
        win.toggle_fullscreen(True)
        pump(0.8)
        _c_r, _c_rd = chrome_reading(win)
        _h_r, _h_rd = host_reading(win)
        check("退路模式：条回到根布局那一行（宿主不再认领、`overlay_off` 必须不响）"
              "——开着浮层却不接手才是 bug，关掉浮层占行是**明码标价的代价**",
              (not _h_r["hosting"] and _c_r["bar_up"] and not _c_r["overlay_off"]
               and _c_r["by_bar"] > 0 and not _c_r["h_short"]), _c_rd + "｜" + _h_rd)
        win.toggle_fullscreen(False)
        pump(0.6)
        _h_r2, _h_r2d = host_reading(win)
        check("退路模式退出全屏后条仍在布局里（搬出搬进这一手必须可逆，来回都走得通）",
              not _h_r2["hosting"], _h_r2d)
    finally:
        # 配置必须换回来，而且换回来之后要立刻按真配置重摆一次：`_apply_mode` 不碰窗口态，
        # 只把条从布局搬回宿主——留一个"配置说浮层、条却住在布局"的现场给后面每一拍，
        # 比这条退路本身没验到更糟。
        win.cfg["float_controls_in_fs"] = True
        win._apply_mode()
        pump(0.4)

    # 10.5 外力改窗口态之后 chrome 不许错位（R21-a 三条错位的第三条：全屏里最小化、再从任务栏点回来）。
    #      早先**全仓没有任何地方监听窗口态**，于是 Alt+Tab／任务栏／"显示桌面"把窗口从全屏里
    #      拽出去之后，`_fs` 还停在 True：标题栏不来、条还住在浮层里、窗口明明只有 1180×720
    #      却在"等自动收起"——用户读到的是"窗口变小了画面还是没铺满、四周空的"。
    win.toggle_fullscreen(True)
    pump(0.8)
    win.showMinimized()
    _min = wait_for(lambda: bool(win.windowState() & Qt.WindowMinimized), 3.0)
    pump(0.4)
    _host_min, _host_mind = host_reading(win)
    win.showNormal()
    pump(0.8)
    _c_m, _c_md = chrome_reading(win, fs=False)
    _h_m, _h_md = host_reading(win)
    check("全屏里最小化时宿主不能孤零零留在桌面上（它是**顶层窗**，不跟着收就是"
          "「播放器都关了屏幕上还有一条控制条」）", (not _min) or (not _host_min["visible"]),
          f"最小化到位={_min}｜{_host_mind}")
    check("从全屏里被外力拽回窗口态后 chrome 必须跟着降级：标题栏回来、条搬回布局、"
          "「以为还在全屏」那一笔账必须销掉（`changeEvent` 监的就是这一手）",
          (not (win.windowState() & Qt.WindowFullScreen) and not win._fs
           and win.title.isVisible() and not _h_m["hosting"] and _c_m["bar_up"]),
          _c_md + "｜" + _h_md)
    win.toggle_fullscreen(False)
    pump(0.4)

    # 11. 截图链（E → 内核落盘 → sig_saved → OSD）
    saved: list[str] = []
    win.player.sig_saved.connect(saved.append)
    before = set(os.listdir(cfg["screenshot_dir"]))
    win.dispatch("screenshot")

    def shot():
        new = sorted(set(os.listdir(cfg["screenshot_dir"])) - before)
        if not new:
            return ""
        p = os.path.join(cfg["screenshot_dir"], new[0])
        return p if os.path.exists(p) and os.path.getsize(p) > 5000 else ""

    got = wait_for(lambda: bool(shot()), 8.0)
    check("截图存帧成功（>5KB，名字走自定义模板）",
          got and os.path.basename(shot()).startswith(f"{cfgmod.BRAND_ID}-"),
          f"新增={sorted(set(os.listdir(cfg['screenshot_dir'])) - before)}")
    check("截图路径经 sig_saved 回报", wait_for(lambda: bool(saved), 4.0), f"sig_saved={saved[:1]}")
    if saved:
        img = QImage(saved[0])
        check("截出来的帧有画面（非单色）", has_picture(img), f"stats={sample_stats(img, 20, 12)}")
    else:
        check("截出来的帧有画面（非单色）", False, "没有帧可判")

    # 12. 屏幕真截一张：证明画面在 Qt 窗口里，而不是"内核在放、屏幕上一片黑"
    win.player.goto_index(0)
    wait_for(lambda: "sample-1" in str(win.player.prop("path", "")), 6.0)
    win.player.seek_abs(3.0)
    pump(0.8)
    area, e_area = capture(win, win.video, "视频区")
    p_area = os.path.join(OUT, "screen-video-area.png")
    area.save(p_area)
    st = sample_stats(area)
    pixel_check("视频区", "视频区在屏幕上真的有画面（且那块像素是我们的窗口）",
                has_picture(area) and st["mean"] > 12 and not e_area,
          f"{e_area}{area.width()}×{area.height()} 亮度{st['mean']:.0f} 标准差{st['std']:.0f} "
          f"颜色{st['distinct']} → {p_area}")
    whole, e_whole = capture(win, win, "整窗")
    p_whole = os.path.join(OUT, "screen-whole-window.png")
    whole.save(p_whole)
    st_whole = sample_stats(whole)
    pixel_check("整窗", "整窗屏幕截图留档（标题栏/控制条/侧栏一起目检，且不是死黑一张）",
                not whole.isNull() and not e_whole and st_whole["distinct"] >= 6,
          f"{e_whole}{st_whole['distinct']} 色 亮度{st_whole['mean']:.0f} → {p_whole}")

    # 12.5 两层（占位页 / 画面容器）来回切换：清空列表要让位，重新载入要回来
    win.player.clear_playlist()
    gone = wait_for(lambda: win.video.isHidden() and not win.empty.isHidden(), 2.5)
    win.add_paths(FILES)
    back = wait_for(lambda: (not win.video.isHidden()) and win.empty.isHidden(), 5.0)
    check("清空列表回到占位页、再载入又回到画面（两层不能同时藏、也不能同时冒）",
          gone and back,
          f"清空后={gone} 重载后={back} video.hidden={win.video.isHidden()} "
          f"empty.hidden={win.empty.isHidden()}")

    # 13. 崩溃留痕：PyQt5 的回调异常是"无声 abort"（rc=127、stdout/stderr 全空），
    #     没有落盘这一份，用户那边就只剩"播放器突然没了"。故意抛一条验证真写了盘。
    marker = f"gelu-crash-probe-{os.getpid()}-{int(time.time() * 1000)}"
    wrote, stale = crash_probe(marker)
    check("未捕获异常会写进 crash.log（含异常文本与新长度）", wrote and not stale,
          f"写入={wrote} 抛之前就有={stale} 路径={crash_log_path()}")

    # 14. 内核日志与外壳兜底
    errs = [l for l in logs if "error" in l.lower()]
    check("外壳运行期内核无 error 上报", not errs, f"{errs[:3]}")
    check("外壳没有吞掉的异常（原生回调里出错会静默改行为）",
          win.chrome.last_error is None and win.video.last_error is None
          and win.osd.last_error is None,
          f"chrome={win.chrome.last_error} video={win.video.last_error} osd={win.osd.last_error}")

    # 15. 收尾
    proc = win.player._proc
    win.close()
    pump(0.4)
    check("关闭后内核进程退出（无残留 mpv）",
          (not win.player.running) and proc is not None and proc.poll() is not None,
          f"running={win.player.running} poll={None if proc is None else proc.poll()}")
    check("关闭后 IPC 已断开", win.player._ipc is None, "")
    check("关闭后原生事件过滤器已摘除（Qt 只存裸指针，留着就是悬空地址）",
          not win.chrome._installed and win.chrome._hwnd is None,
          f"installed={win.chrome._installed} hwnd={win.chrome._hwnd}")

    return report()


def final_exit_code(fails: list[str], env: list[str]) -> int:
    """真跑收尾的分流（纯函数）：**产品失败**优先于**环境判废**。

    和 `env_exit_code` 是同一族规矩，只是那一头比的是"量具不可信 vs 环境没给证据"，
    这一头比的是"真红 vs 判废"。写反了会出最坏的一种读法：本轮既有一条真的失败、又碰上
    外来窗口抢屏，rc=3 会把产品失败洗成"今天环境不给"，下一轮没人再去查那条红。
    · 有真红 ⇒ 1（判废照样逐条点名，但绝不掩盖失败）。
    · 只有判废 ⇒ 3：这一轮那几条没有证据，既不是通过也不是失败。
    · 都没有 ⇒ 0。
    """
    if fails:
        return 1
    if env:
        return 3
    return 0


def env_exit_code(env: list[str], missed: list[str], junk: list[str]) -> int | None:
    """收尾分流（纯函数）：**量具判废**与**环境没给证据**谁优先。

    · 证伪台账里有"坏样本没抓到"或"记回来的不是布尔" ⇒ 1。**量具自己不可信**比环境问题
      严重一级：把它降级成"只是环境"是这一份测试最容易骗过下一轮的谎（恒真的检测器配上
      rc=3，看起来就像"今天屏幕不给"）。
    · 没有量具问题、只有判废记录 ⇒ 3：本轮这些判据没有证据，既不是通过也不是产品失败。
    · 都没有 ⇒ None，交给正常"全绿回 0、有红回 1"。
    """
    if junk or missed:
        return 1
    if env:
        return 3
    return None


def falsify_bucket(ok: object) -> str:
    """一行证伪台账归哪家（纯函数）：判决行的标签与收尾自打**共用这一把**。

    `"ENV "` 末尾那个空格是打印口径（老日志里就是 `[ENV ]`），留着是为了不把这一轮的 grep
    面和上一轮劈成两种字节；它不参与任何判断。
    判据一律用 `is` 而不是 `in (True, False, None)`：后者把 0 当成 False、1 当成 True，
    于是"忘了取元组的 [0]、串回来一个整数 1"会混进 CAUGHT——那正是这本台账最怕的假绿。
    """
    if ok is True:
        return "CAUGHT"
    if ok is False:
        return "MISSED"
    if ok is None:
        return "ENV "
    return "JUNK"


def falsify_tally(rows: list[tuple[str, object, str]]) -> dict:
    """把证伪台账数成"每家的名字 + 条数"（纯函数）：自打那一行和收尾分流都吃同一份结果。"""
    names = {"CAUGHT": [], "MISSED": [], "ENV ": [], "JUNK": []}
    junk_types: list[tuple[str, str]] = []
    for name, ok, _detail in rows:
        bucket = falsify_bucket(ok)
        names[bucket].append(name)
        if bucket == "JUNK":
            junk_types.append((name, type(ok).__name__))
    return {"total": len(rows), "caught": len(names["CAUGHT"]),
            "missed": len(names["MISSED"]), "env": len(names["ENV "]),
            "junk": len(names["JUNK"]), "names": names, "junk_types": junk_types}


# 自打收口"接对没接对"的四条形状（纯函数，U59 用已知答案的源码样本钉它，也拿真的 report
# 源码过一遍）。写成纯函数是因为这四条都能在**下一轮被人改回去**，而改回去的那一趟照样能
# 印出一句看着合理的收口——只有把形状也钉住，自打的数才不会变成新的自说自话。
_TALLY_GOOD = '''
def report():
    t = falsify_tally(falsified)
    for name, ok, detail in falsified:
        print(f"[{falsify_bucket(ok)}] {name}")
    print("---- 证伪台账自打 ----")
    missed, env, junk = t["names"]["MISSED"], t["names"]["ENV "], t["junk_types"]
    rc = env_exit_code(env, missed, junk)
'''


def tally_wiring_ok(src: str) -> tuple[bool, str]:
    """report() 里那句自打收口接得对不对：回 (合格, 不合格点名)。

    ① 只许有一份数法：`falsify_tally(falsified)` 恰好出现一次，且不许留下就地重数的旧写法
       （`if ok is False]` / `if ok is None]` / `not in (True, False, None)]`）。同一件事抄两遍
       就一定会漂——上一轮那句"83 + 1 条对照 = 84，与静态对上"就是两套数法凑出来的。
    ② 判决行的标签必须来自同一把 `falsify_bucket(ok)`，否则标签与计数各说各话
       （非布尔行旧口径会被打成 `[ENV ]`，看着像"环境没给"，其实是量具记错了类型）。
    ③ 自打那行必须排在 `rc = env_exit_code(` **之前**：rc=1／rc=3 那两趟最需要这句话，
       排在后面就等于只在"全抓到"时出现，而那种时候没人怀疑数。
    ④ 分流必须吃这份数（`env_exit_code(env, missed, junk)`），不许自打之后另算一套。
    """
    bad = []
    if src.count("falsify_tally(falsified)") != 1:
        bad.append("①数法不唯一（要么没接上，要么接了两遍）")
    for stale in ("if ok is False]", "if ok is None]", "not in (True, False, None)]"):
        if stale in src:
            bad.append(f"①还留着就地重数：{stale}")
    if "falsify_bucket(ok)" not in src:
        bad.append("②判决行的标签没走共用分桶")
    i_tally = src.find("证伪台账自打")
    i_rc = src.find("rc = env_exit_code(")
    if i_tally < 0 or i_rc < 0:
        bad.append("③自打行或分流调用不在 report 里")
    elif i_tally > i_rc:
        bad.append("③自打行排在分流之后（rc=1／rc=3 那两趟印不到）")
    if "env_exit_code(env, missed, junk)" not in src:
        bad.append("④分流没吃这份数")
    return (not bad), "；".join(bad)


def report() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 汇总：{passed}/{len(results)} 通过"
          + (f"；失败项：{fails}" if fails else "；无失败") + " ----", flush=True)
    if falsified:
        print("==== 证伪（坏样本必须被抓出来）====")
        # 台账里只许出现 True / False / None 三种值。写成 `(判据函数) and (另一条)` 时忘了取
        # [0]，串回来的就是元组——元组恒真，于是这条"抓到"是真的，抓到的是个空气。
        # 本机就在 U39 上中过一次（i_good 是 has_ink 回的元组）。所以第四种值单独一家，
        # 既不记 CAUGHT 也不记 ENV（旧口径把它打成 `[ENV ]`，"记错了类型"就被读成"今天屏幕不给"）。
        tally = falsify_tally(falsified)
        for name, ok, detail in falsified:
            print(f"[{falsify_bucket(ok)}] {name}" + (f"  —— {detail}" if detail else ""))
        # 自打收口：这趟登记了几行、各家几条。以前这个数只能由外部 grep 得到（出处＝grep），
        # 现在由回执自己印——并且排在分流之前，rc=1／rc=3 那两趟也带着这句话。
        print(f"---- 证伪台账自打：共 {tally['total']} 行 —— CAUGHT {tally['caught']}／"
              f"MISSED {tally['missed']}／ENV {tally['env']}／非布尔 {tally['junk']} ----",
              flush=True)
        missed, env, junk = tally["names"]["MISSED"], tally["names"]["ENV "], tally["junk_types"]
        rc = env_exit_code(env, missed, junk)     # 谁优先写成纯函数，U48 拿已知答案钉它
        if rc == 1:
            if junk:
                print("!! 以下证伪样本记回来的不是布尔值（十成是忘了取元组的 [0]，恒真的元组会冒充"
                      "\"抓到了\"）：" + ", ".join(f"{n}→{ty}" for n, ty in junk))
            if missed:
                print("!! 以下坏样本没被抓到，自检不可信：" + ", ".join(missed))
            return 1
        if rc == 3:
            print(f"~~ 以下 {len(env)} 条要靠真实屏幕摆出来，而屏幕此刻给不了这份证据"
                  f"（整屏不出像素，或别人的窗口压在最上层、我们无权动它）——本轮不作数，"
                  f"既不算抓到也不算漏网：" + ", ".join(env))
            return 3
    if env_notes:
        # 外来窗口把我们的区域按住（抬不动）、或把前台抢走（按键不响）：这一轮这两类判据
        # 没有证据。上一轮的八条红全属这一类（窗口内容一点没变，挡路的是用户的浏览器）——
        # 把它们算成产品失败会引着人去改本来对的代码。
        # 只有这两类**本身**不作数，其余判据照常记账：一趟环境抖动不许带走整张台账。
        print(f"~~ 本轮不作数 {len(env_notes)} 处（判据的前提被同进程的早先副作用或外来窗口拿走了）：",
              flush=True)
        for n in env_notes:
            print("   ", n, flush=True)
    style_warns = [m for m in qt_messages if "stylesheet" in m.lower() or "qss" in m.lower()]
    print(f"---- Qt 消息 {len(qt_messages)} 条（样式相关 {len(style_warns)}）----")
    for m in qt_messages[:6]:
        print("   ", m[:180])
    final = final_exit_code(fails, env_notes)     # 真红优先于判废，U48e 拿已知答案钉它
    if env_notes:
        print("   上面这些既不算通过也不算产品失败：它们量的是'眼睛/键盘此刻归谁'，不是功能。"
              "换个没人抢前台的时间点重跑，或把抢前台的那个窗口关掉。"
              + (f"本轮另有 {len(fails)} 条真实失败 ⇒ rc=1（判废不许掩盖失败）。" if fails
                 else "⇒ rc=3。"), flush=True)
    return final


def falsify() -> int:
    """证明这份测试的检测器不是恒真：每个坏样本都必须被对应的检查判失败。"""
    global APP
    _enable_high_dpi()
    APP = QApplication.instance() or QApplication(sys.argv[:1])
    # 和 main() 一样先把全局样式表挂上再建窗口。少了这一条，证伪跑的是"裸 Qt 外观"，
    # 滑轨/行高这类几何判据量到的就不是真机那套尺寸（U27 第一版就是这么误报的）。
    APP.setStyleSheet(theme.qss())
    global SCREEN_OK
    SCREEN_OK, d_screen = capture_health()
    print(f"[{'PASS' if SCREEN_OK else 'ENV'}] 抓屏通路可用（下面依赖真抓屏的证伪样本才有意义）  —— {d_screen}",
          flush=True)

    # U1 纯单色图必须判"无画面"
    flat = QImage(640, 360, QImage.Format_ARGB32)
    flat.fill(QColor("#101418"))
    falsified.append(("单色图必须被判成无画面", not has_picture(flat), f"stats={sample_stats(flat)}"))

    # U2 全透明图也必须判"无画面"
    clear = QImage(640, 360, QImage.Format_ARGB32)
    clear.fill(QColor(0, 0, 0, 0))
    falsified.append(("全透明图必须被判成无画面", not has_picture(clear),
                      f"stats={sample_stats(clear)}"))

    # U3 画不出东西的图标必须被"空图"检查抓到
    icons.D["_probe_empty"] = lambda p: None
    caught = [n for n in sorted(icons.D)
              if sample_stats(icons.pixmap(n, 22, theme.TEXT).toImage(), 12, 12)["distinct"] < 2]
    falsified.append(("空图标必须被检出", "_probe_empty" in caught, f"检出={caught}"))
    icons.D.pop("_probe_empty", None)

    # U3.5 偏色判据必须能否决"setPen(NoPen) 之后取笔色"这个旧写法（audio/volume_mute 踩过）
    def probe_black(p):
        p.setPen(Qt.NoPen)                       # _fill() 干的事
        p.setBrush(QColor("#FFFFFF"))
        pen = QPen(p.pen().color(), 2.0)         # 旧写法：这时笔色已经是黑了
        p.setPen(pen)
        p.drawLine(QPointF(4, 4), QPointF(20, 20))
    icons.D["_probe_black"] = probe_black
    d, t = icon_dark_ink("_probe_black")
    falsified.append(("NoPen 之后取笔色的写法必须被判偏色", d > 0 and t > 0, f"偏色{d}/{t}"))
    icons.D.pop("_probe_black", None)

    # U3.6 出框判据必须能否决"几何画到 24 框外"（旧 logo 的射线就是这么整条消失的）
    icons.D["_probe_out"] = lambda p: p.drawLine(QPointF(12, 12), QPointF(40, 40))
    o = icon_out_of_frame("_probe_out")
    in_box = icon_out_of_frame("play")
    falsified.append(("画到设计框外的图标必须被检出，且正常图标不得误报",
                      o > 0 and in_box == 0, f"出框样本={o} 正常 play={in_box}"))
    icons.D.pop("_probe_out", None)

    # U4 坏样式表（把 /* */ 换成 # 注释）必须让正向颜色判据失败
    broken = theme.qss().replace("/* 根节点不写", "# 根节点不写")
    falsified.append(("含 # 注释的样式表必须被判为未生效", not qss_applies(broken, theme.PRIMARY_FILL),
                      "若这里为 False(没抓到)，说明 QSS 判据是假的"))

    # U4b 把主按钮底色从暖象牙改成主色绿——"主色铺满主按钮"是最常见的一次顺手合并，
    #     它不会报错、不会丢样式，只是深墨字压不住（#4E705B 只有 ~3.4:1）。
    #     qss_applies 对这种改动是瞎的（期望值跟着代码走），所以这条专门喂 primary_ink。
    raw = theme.qss().replace(f"background: {theme.PRIMARY_FILL}", f"background: {theme.ACCENT_FILL}")
    _bad = primary_ink(raw)
    _good = primary_ink(theme.qss())
    falsified.append(("主按钮改铺主色绿必须被对比度尺子抓到（好表≥4.5、坏表<4.5）",
                      _bad is not None and theme.contrast(theme.PRIMARY_INK, _bad.name()) < 4.5
                      and _good is not None and theme.contrast(theme.PRIMARY_INK, _good.name()) >= 4.5,
                      f"坏表实底={None if _bad is None else _bad.name()}"
                      f"({0.0 if _bad is None else theme.contrast(theme.PRIMARY_INK, _bad.name()):.2f}:1) "
                      f"好表实底={None if _good is None else _good.name()}"
                      f"({0.0 if _good is None else theme.contrast(theme.PRIMARY_INK, _good.name()):.2f}:1)"))

    def _qss_sub(pattern: str, repl: str) -> tuple[str, bool]:
        got = re.sub(pattern, repl, theme.qss(), count=1)
        return (got, got != theme.qss())

    # U4c 开关的五种坏写法，每种都得被上面那把尺抓到。为什么是这五种：它们各自对应一件
    #   **真实会犯的错**——① 摘掉 image（资产丢了、或 url 里的空格把路径截断，"写了不吃"）；
    #   ② 两态共用一张图（开关退化成"永远关着"）；③ 两张挂反（"开"显示成"关"，比没有更坏）；
    #   ④ 轨道尺寸被顺手改回旧的 16×16 方框；⑤ 摘掉禁用档的文字颜色（禁用件看着和能点的一样）。
    #   还有一条方向相反的：摘掉 url 之后样式表其余部分必须照常生效——证明"资产缺失"只是
    #   少一个把手，不会像 # 注释那样把整张表带走（theme._img_decl 的降级）。
    #   ②③⑤ 这三条的注入都先断言"文本真的变了"：换不动的注入是最坏的一种假绿。
    _p_on, _p_off = theme.switch_path(True), theme.switch_path(False)
    _sw_noimg = re.sub(r'\s*image: url\("[^"]*"\);', '', theme.qss())
    _sw_same = theme.qss().replace(_p_on, _p_off)
    _sw_swap = theme.qss().replace(_p_on, "\0").replace(_p_off, _p_on).replace("\0", _p_off)
    _c_same, _c_swap = _sw_same != theme.qss(), _sw_swap != theme.qss()
    _sq_noimg = switch_ok(switch_px(_sw_noimg, True), True)
    _sq_same = switch_ok(switch_px(_sw_same, True), True)
    _sq_swap_on = switch_ok(switch_px(_sw_swap, True), True)
    _sq_swap_off = switch_ok(switch_px(_sw_swap, False), False)
    _sw_box, _c_box = _qss_sub(rf"QCheckBox::indicator \{{ width: {theme.SWITCH_W}px;"
                               rf" height: {theme.SWITCH_H}px",
                               "QCheckBox::indicator { width: 16px; height: 16px")
    _sq_box = switch_ok(switch_px(_sw_box, True), True)
    _sw_dis, _c_dis = _qss_sub(r"\nQPushButton:disabled, QCheckBox:disabled[^}]*\}", "")
    _sq_dis = switch_px(_sw_dis, False, enabled=False, text="字幕粗体")
    falsified.append(("开关：摘 url／两态同图／两图互换／轨道回方框／禁用不变暗 五种坏写法必须全被抓到，"
                      "且摘掉 url 之后其余规则照常生效",
                      (not _sq_noimg[0]) and _c_same and (not _sq_same[0])
                      and _c_swap and (not _sq_swap_on[0]) and (not _sq_swap_off[0])
                      and _c_box and (not _sq_box[0])
                      and _c_dis and _sq_dis["mid_area"] < KNOB_MID_MIN
                      and qss_applies(_sw_noimg, theme.PRIMARY_FILL),
                      f"摘url={_sq_noimg[1]} 同图={_sq_same[1]}(改了={_c_same}) "
                      f"互换:开={_sq_swap_on[1]} 互换:关={_sq_swap_off[1]}(改了={_c_swap}) "
                      f"方框={_sq_box[1]}(改了={_c_box}) "
                      f"禁用中灰={_sq_dis['mid_area']:.0f}(改了={_c_dis}) "
                      f"摘url后表仍生效={qss_applies(_sw_noimg, theme.PRIMARY_FILL)}"))

    # U4d 把双极滑轨那一档的选择器名改错（setObjectName 与 QSS 拼不上＝这一档静默失效）：
    #     通用规则会重新接管，滑块左边又铺回半根主色，尺子必须看见。
    bipbad = theme.qss().replace("QSlider#BipSlider", "QSlider#Nope")
    _bad_bip = groove_px(bipbad, "BipSlider")[0]
    _good_bip = groove_px(theme.qss(), "BipSlider")[0]
    falsified.append(("双极滑轨的选择器一旦对不上必须被抓到（坏表铺回主色、好表为零）",
                      _bad_bip > 100 and _good_bip == 0,
                      f"改名后主色={_bad_bip} 现状={_good_bip}"))

    # U4e 把手形状：① 留旧值（width 12 / margin −5 ⇒ 12×14 竖方块）、② 整条规则摘掉、
    #     ③ margin 放宽一档（⇒ 14×20 横方块），三种坏写法都得被同一把尺抓到。
    #     为什么这三条都用正则改而不是 replace 整段字面量：theme.qss() 里那一行现在是
    #     **从 SLIDER_HANDLE/GROOVE_H 两个令牌算出来的**（跨了两行），拿"完整一行"去 replace
    #     会静默换不动，而"换不动的注入"是最坏的一种假绿——它看起来像"尺子抓到了"（因为
    #     坏样本读数和现状一样、判据同样为真），实际上什么都没测。所以每条注入都先断言
    #     文本确实变了（NOP 直接算没抓到），再拿像素说话。
    #     ②那条特别值得留着：Qt 对横向把手不看 height，只读 height 的人会以为改 height 就能圆。
    _h_now = handle_px(theme.qss())
    _s_old, _c_old = _qss_sub(r"(QSlider::handle:horizontal \{\s*width: )14px", r"\g<1>12px")
    _h_old = handle_px(_s_old)
    _s_fat, _c_fat = _qss_sub(r"(QSlider::handle:horizontal \{[^}]*?margin: )-5px", r"\g<1>-8px")
    _h_fat = handle_px(_s_fat)
    _s_gone = "\n".join(ln for ln in theme.qss().splitlines() if "QSlider::handle" not in ln)
    _c_gone = _s_gone != theme.qss()
    _h_gone = handle_px(_s_gone)
    falsified.append(("滑轨把手：旧宽度/margin 放宽/整条摘掉必须判非圆且注入真落盘，现状必须判正圆",
                      handle_round_ok(*_h_now)[0] and _c_old and _c_fat and _c_gone
                      and not handle_round_ok(*_h_old)[0]
                      and not handle_round_ok(*_h_fat)[0] and not handle_round_ok(*_h_gone)[0],
                      f"现状={_h_now} 旧宽12={_h_old}(改了={_c_old}) margin-8={_h_fat}(改了={_c_fat}) "
                      f"摘掉={_h_gone}(改了={_c_gone})"))

    # U6a 文字层级这把尺必须能翻红，而且是**三种各自会犯的错**各演一次：
    #     ① 把 #RowLabel 的 color 换回次要色（层级塌回一档，一整页没有主次——旧形状）；
    #     ② 整条 #Status 摘掉（状态读数落回通用那条 13px 正文色，和行标题一个长相）；
    #     ③ 把 #Status 的 color 换成弱化档 TEXT_FAINT（层级还在，但 12px 的字压面板底只有
    #        3.40:1——把"次要"做成"看不清"是最容易被"它就是次要信息"糊过去的一种坏）。
    #     每条注入都先断言样式表文本真的变了：换不动的注入看着像"尺子抓到了"，其实什么都没测。
    _t_good, _d_good = tier_ok(tier_px(theme.qss()))
    _s_flat, _c_flat = _qss_sub(r"(#RowLabel \{ color: )[^;]+;", r"\g<1>" + theme.TEXT_DIM + ";")
    _t_flat = tier_ok(tier_px(_s_flat))
    _s_nost, _c_nost = _qss_sub(r"#Status \{[^}]*\}\n?", "")
    _t_nost = tier_ok(tier_px(_s_nost))
    _s_faint, _c_faint = _qss_sub(r"(#Status \{ color: )[^;]+;", r"\g<1>" + theme.TEXT_FAINT + ";")
    _t_faint = tier_ok(tier_px(_s_faint))
    falsified.append(("文字层级尺：正文档换回次要色／状态档整条摘掉／状态档掉进弱化色 "
                      "三种坏写法必须各自被抓到，且现状必须过",
                      _t_good and _c_flat and not _t_flat[0] and _c_nost and not _t_nost[0]
                      and _c_faint and not _t_faint[0],
                      f"现状={_t_good} 塌档={_t_flat[0]}(改了={_c_flat}) 摘Status={_t_nost[0]}"
                      f"(改了={_c_nost}) 弱化={_t_faint[0]}(改了={_c_faint}) "
                      f"| 塌档详情={_t_flat[1]} 摘除详情={_t_nost[1]} 弱化详情={_t_faint[1]}"))

    # U6b 行尾读数那把尺：把 `setMinimumWidth(58)` 摘掉（列宽下限没了 ⇒ 每行按自己的
    #     字数要宽 ⇒ 左缘参差）。注入先自证——宽度集合必须真的从"一档"变成"多档"，
    #     否则这条只是"产品本来就红"或者"什么都没改"，两种都测不到尺子有没有牙。
    _ph = QWidget()
    _ph.setObjectName("Panel")
    _pl = QVBoxLayout(_ph)
    _prows = [form_rows.NumRow(sp, _ph) for sp in tweaks.NUMS]
    for _r in _prows:
        _pl.addWidget(_r)
    _ph.adjustSize()
    _ph.resize(300, _ph.sizeHint().height())
    _ph.show()
    pump(0.05)
    _pil_good, _d_pil_good = pills_ok(_ph)
    for _r in _prows:
        _r.val.setMinimumWidth(0)
    pump(0.05)
    _w_naked = sorted({int(_r.val.width()) for _r in _prows})
    _pil_naked = pills_ok(_ph)
    for _r in _prows:
        _r.val.setMinimumWidth(58)
    pump(0.05)
    _pil_back = pills_ok(_ph)
    _ph.close()
    falsified.append(("行尾读数尺：摘掉列宽下限必须红（且真的量出多档宽），装回去必须复绿",
                      _pil_good and len(_w_naked) > 1 and not _pil_naked[0] and _pil_back[0],
                      f"现状={_pil_good} 摘下限后宽档={_w_naked} 判={_pil_naked[0]}"
                      f"（{_pil_naked[1]}） 装回={_pil_back[0]}"))

    # U6c 「生效时机」那条判据的三种坏形状：删掉一枚该在的胶囊、给默认档多挂一枚、
    #     把节标题里那句话改掉（行尾省略的理由就没了）。三种都得被同一把尺抓到。
    _d6c = SettingsDialog(make_cfg(), None)
    _ch_good, _d_ch_good = chips_agree(_d6c)
    _resume_row = _d6c._rows["resume"]
    _gone = [c for c in _resume_row.findChildren(QLabel, "Chip")]
    for c in _gone:
        c.setParent(None)                       # 摘掉"下次载入生效"那枚：该说的没人说了
    _ch_lost = chips_agree(_d6c)
    _extra = QLabel(EFFECT_NOW, _d6c._rows["theme_accent"])
    _extra.setObjectName("Chip")
    _d6c._rows["theme_accent"].layout().addWidget(_extra)   # 默认档又多标一枚：噪音回来了
    _ch_extra = chips_agree(_d6c)
    _extra.setParent(None)
    for c in _gone:
        _resume_row.layout().addWidget(c)       # 装回去，再改标题那一档
    _orig_title = _d6c._sections[0][0].text()
    _d6c._sections[0][0].setText("性能（都是启动期参数）")
    _ch_title = chips_agree(_d6c)
    _d6c._sections[0][0].setText(_orig_title)
    _ch_back = chips_agree(_d6c)
    _d6c.deleteLater()
    falsified.append(("生效时机尺：删掉该标的胶囊／给默认档多挂一枚／节标题不再写着那一档 "
                      "三种坏写法必须各自被抓到，还原后必须复绿",
                      _ch_good and not _ch_lost[0] and not _ch_extra[0] and not _ch_title[0]
                      and _ch_back[0],
                      f"现状={_ch_good} 少标={_ch_lost[0]}（{_ch_lost[1][:110]}） "
                      f"多标={_ch_extra[0]}（{_ch_extra[1][:110]}） "
                      f"改标题={_ch_title[0]}（{_ch_title[1][:110]}） 还原={_ch_back[0]}"))

    # U6d V6 那三把尺的牙，各演一次：
    #     ① 提示行**不跟着键位表刷新**——这一族最静的坏：用户改了键位，屏幕上还写着旧键，
    #        不报错、不崩，只是按了没反应。先证"不 refresh 就对不上"，再证 refresh 之后对得上；
    #     ② detector 的方向性：手抄的那句必须点名，而「上半页 1 秒一轮」里那个 "1"**不许**
    #        被认成键名（一把什么都报红的尺子三天内就会被关掉，比没有更坏）；
    #     ③ 搜索框那把尺：圆角退回 card 档(8px)、整条规则摘掉、放大镜拿掉——三种都得红。
    _h6_host = QWidget()
    _h6_host.setObjectName("Panel")
    _h6_cfg = make_cfg()
    _h6_host.cfg = _h6_cfg
    _h6_lay = QVBoxLayout(_h6_host)
    for _pg, _gr in HINT_PAGES:
        _h6_lay.addWidget(form_rows.KeyHintLabel(_h6_cfg, _gr, _h6_host))
    _h6_host.adjustSize()
    _h6_host.show()
    pump(0.05)
    _h6_good, _d_h6_good = hints_agree(_h6_host)
    _h6_cfg["keymap"] = {"off": ["R", "Shift+R"], "on": [["X", "rotate_cycle"]]}
    _h6_stale, _d_h6_stale = hints_agree(_h6_host)      # 还没 refresh：界面上是旧键
    _h6_text_stale = _h6_host.findChildren(form_rows.KeyHintLabel)[0].text()
    _h6_n = form_rows.refresh_key_hints(_h6_host)
    _h6_back, _d_h6_back = hints_agree(_h6_host)
    _h6_want = hint_expect(keymap.effective(_h6_cfg.get("keymap")), HINT_PAGES[0][1])
    _h6_host.close()
    _toks6d = key_tokens(keymap.effective(None))
    _inj6d = 'a = mk_hint("把文件或文件夹拖进来即可播放", tip)\n' \
             'b = mk_hint("R 旋转 · Shift+R 比例 · H 镜像", self)\n'
    _hit6d = hand_copied_hints(_inj6d, _toks6d)
    _neg6d = hand_copied_hints('a = mk_hint("上半页 1 秒一轮（只在本页可见时问内核）", self)\n'
                               'b = mk_hint("双击卡片播放 · 右键更多操作", self)\n', _toks6d)
    _src6d, _nsrc6d = ui_source_offenders()
    _ed6d = QLineEdit()
    _ed6d.setObjectName("SearchField")
    _ed6d.setClearButtonEnabled(True)
    _mag6d = _ed6d.addAction(icons.icon("search", 14, theme.TEXT_DIM), QLineEdit.LeadingPosition)
    _ed6d.resize(220, 34)
    _ed6d.show()
    pump(0.05)
    _sf_good, _d_sf_good = search_field_ok(_ed6d)
    _sf_card_sheet, _c_sf_card = _qss_sub(r"(#SearchField \{ border-radius: )\d+px", r"\g<1>8px")
    # `search_field_ok` 回的是 (判据, 读数)，四处都得**拆开接**：上一版把后三个接成了整个元组，
    # 于是 `not _sf_card` 恒为 False（非空元组永远真）、而 `_sf_good[0]` 又当场崩——
    # 一条"永远红 + 打印时崩"的腿，比没有这条腿更坏（它会把整趟证伪带断，就像刚才那样）。
    _sf_card, _d_sf_card = search_field_ok(_ed6d, _sf_card_sheet)
    _sf_gone_sheet, _c_sf_gone = _qss_sub(r"#SearchField \{[^}]*\}\n?", "")
    _sf_norule, _d_sf_norule = search_field_ok(_ed6d, _sf_gone_sheet)
    # 放大镜拿掉：框还是那个框，可没人知道它是搜索。
    # 为什么不用 `clearActions()`：本机 PyQt 5.15.10 **没有**这个方法（实测 AttributeError，
    # 而上一版就是这么写的——那一行一崩，整趟证伪跑到这里就断了，"没放大镜"这条腿其实
    # 从没验过）。`removeAction` 在，而且点名摘掉的就是我们挂上去的那一颗。
    _ed6d.removeAction(_mag6d)
    _sf_noicon, _d_sf_noicon = search_field_ok(_ed6d)
    _ed6d.close()
    falsified.append(("按键提示尺：改了键位不刷新必须红、刷新后必须绿；手抄 detector 必须点名"
                      "且不把「1 秒一轮」里的 1 当键名；搜索框尺必须否掉 card 圆角/摘掉规则/没放大镜",
                      _h6_good and _c_sf_card and _c_sf_gone
                      and not _h6_stale and _h6_text_stale != _h6_want
                      and _h6_n == len(HINT_PAGES) and _h6_back
                      and len(_hit6d) == 1 and not _neg6d and _nsrc6d > 6 and not _src6d
                      and _sf_good and not _sf_card and not _sf_norule and not _sf_noicon,
                      f"现状={_h6_good} 不刷新={_h6_stale}(旧句={_h6_text_stale!r} 该是={_h6_want!r}) "
                      f"刷新几行={_h6_n} 复绿={_h6_back} 抄的={_hit6d} 误报={_neg6d} "
                      f"扫到文件={_nsrc6d} 现状抄的={_src6d} "
                      f"框:现状={_sf_good} card={_sf_card} 摘规则={_sf_norule}"
                      f"(改了={_c_sf_gone}) 无图标={_sf_noicon}"
                      f"｜{_d_h6_good[:80]}｜{_d_sf_good[:60]}｜{_d_sf_card[1][:40]}"
                      f"｜{_d_sf_norule[1][:40]}｜{_d_sf_noicon[1][:40]}"))

    # U6e 「提示行此刻露没露」这一半必须有真 QStackedWidget 形状的牙（2026-10-13，r13 教的）。
    #     U6d 合成的宿主是普通 QWidget，四行提示全直接可见 ⇒ 它从没暴露"非当前页整体藏着"这个
    #     前提，判据搬到真侧栏上就恒红。四条读数把前提本身钉住：
    #       ① 朴素 `isVisibleTo` 在"这行该可见、但坐在非当前页"时报 False —— 那正是 r13 那条假红
    #          的形状，留着它是为了证明**这一族坏法真的存在过**，而不是让新尺自证；
    #       ② `_hint_on_screen` 切页再量，必须把它救成 True（环境不再冒充产品）；
    #       ③ 同一行**真的**藏起来（setVisible(False)，模拟"该有字却没上屏"）时切页也救不回，
    #          必须仍报 False —— 这一半是牙：修判据不许顺手把产品侧的真坏一起修没；
    #       ④ 量完必须落回原来那一页（判据不许改产品状态：它后面紧跟着按当前页构图的像素判据）。
    _h6e_host = QWidget()
    _h6e_cfg = make_cfg()
    _h6e_host.cfg = _h6e_cfg
    _h6e_lay = QVBoxLayout(_h6e_host)
    _h6e_stack = QStackedWidget()
    _h6e_lay.addWidget(_h6e_stack)
    _h6e_pg0 = QWidget()                       # 当前页：一行提示都没有
    _h6e_pg1 = QWidget()
    _h6e_pg1_lay = QVBoxLayout(_h6e_pg1)
    _h6e_pg1_lay.addWidget(form_rows.KeyHintLabel(_h6e_cfg, HINT_PAGES[0][1], _h6e_pg1))
    _h6e_lab = _h6e_pg1.findChildren(form_rows.KeyHintLabel)[0]
    _h6e_stack.addWidget(_h6e_pg0)
    _h6e_stack.addWidget(_h6e_pg1)
    _h6e_host.resize(420, 260)
    _h6e_host.show()
    pump(0.05)
    _h6e_want = hint_expect(keymap.effective(_h6e_cfg.get("keymap")), HINT_PAGES[0][1])
    _h6e_plain = _h6e_lab.isVisibleTo(_h6e_host)                       # ① 旧写法：假红
    _h6e_after, _h6e_note = _hint_on_screen(_h6e_host, _h6e_lab)       # ② 切页再量
    _h6e_restored = _h6e_stack.currentWidget() is _h6e_pg0             # ④ 当场还原
    _h6e_lab.setVisible(False)
    _h6e_hidden, _d_h6e_hidden = _hint_on_screen(_h6e_host, _h6e_lab)  # ③ 真藏着：救不回来
    _h6e_lab.setVisible(True)
    _h6e_back, _d_h6e_back = _hint_on_screen(_h6e_host, _h6e_lab)
    _h6e_host.close()
    falsified.append(("提示可见尺：提示行坐在非当前页时朴素 isVisibleTo 必须报 False（r13 那条假红的形状）、"
                      "切页再量必须报 True、真的 setVisible(False) 时切页也必须仍报 False、"
                      "且量完必须落回原来那一页",
                      bool(_h6e_want) and not _h6e_plain and _h6e_after and _h6e_restored
                      and not _h6e_hidden and _h6e_back,
                      f"该可见={bool(_h6e_want)} 朴素={_h6e_plain} 切页={_h6e_after} "
                      f"还原={_h6e_restored} 真藏={_h6e_hidden} 复现={_h6e_back}"
                      f"｜{_h6e_note[:60]}｜{_d_h6e_hidden[:70]}"))

    # U6f #70 那条「侧栏覆盖对账」必须有牙，而且这一条腿自己不许是空操作：
    #     内存里摘掉 `SURFACES` 的最后一行侧栏档 ⇒ 判据必须点名「每一页都有一个侧栏表面档」。
    #     为什么还要判"注入是否真的改了表"（NOP 判别）：这条台账早先吃过教训——`str.replace`
    #     漏写次数时注入是空操作，却被人当成"抓到了"；这里三张表逐张比一遍，没改动就记失败。
    #     为什么基线红要顶掉"抓到"：基线本来就红的时候，注入后红不红都看不出这一条的鉴别力。
    try:
        import probe_surface_cover as PSC
        _p6f_pages, _p6f_tab = PSC.load()
        _p6f_base = [n for n, ok, _ in PSC.judges(_p6f_pages, _p6f_tab) if not ok]
        _p6f_p2, _p6f_t2, _p6f_kw = PSC._mutate(_p6f_pages, _p6f_tab, "drop_surface")
        _p6f_nop = (dict(_p6f_t2.surfaces) == dict(_p6f_tab.surfaces)
                    and dict(_p6f_t2.pick) == dict(_p6f_tab.pick)
                    and dict(_p6f_t2.roots) == dict(_p6f_tab.roots))
        _p6f_hit = [n for n, ok, _ in PSC.judges(_p6f_p2, _p6f_t2) if not ok]
        _p6f_named = [n for n in _p6f_hit if _p6f_kw in n]
        _p6f_back = [n for n, ok, _ in PSC.judges(_p6f_pages, _p6f_tab) if not ok]
        falsified.append(("侧栏覆盖对账：内存里摘掉 SURFACES 最后一行侧栏档必须点名「每一页都有一个"
                          "侧栏表面档」；注入不许是空操作；原表（未改的那份）必须一条不红",
                          not _p6f_base and not _p6f_nop and bool(_p6f_named) and not _p6f_back,
                          f"基线红={_p6f_base or '无'} 空操作={_p6f_nop} 点名={_p6f_named or '无'}"
                          f"（注入后共红 {len(_p6f_hit)} 条） 还原后红={_p6f_back or '无'}"))

    except BaseException as _p6f_e:                 # 断言自己不许崩在报告路上
        falsified.append(("侧栏覆盖对账的注入腿：量具读盘／解析这一步就没跑成（不作数，但也不许静默）",
                          False, f"{type(_p6f_e).__name__}: {_p6f_e}"))

    # U6g #72 那把"指针争用"分流必须有牙。它的坏法有两个方向，两个都得钉：
    #     ① 把真产品坏说成环境问题（恒 ENV 金牌）——那以后"浮层自己乱跑"永远报"不作数"；
    #     ② 把争用说成产品坏（恒 FAIL）——那以后用户手放在鼠标上就白挨一条红。
    #     纯函数喂已知答案的六种形状，不造真鼠标：没漂移、漂移+光标在轨道里、漂移+光标在轨道外、
    #     只有"藏起来"这一种漂移、矩形读不到（证据缺失不许判成争用）、位置与"最后那格"同时变了。
    _r6g = QRect(264, 700, 688, 18)
    _c6g_in = QPoint(900, 709)          # 轨道最右那一格（对应 r13 那个被夹到 hi_x 的读数）
    _c6g_out = QPoint(67, 111)          # 视频区左上角，即量具自己请光标去的那个落点
    _g_none = preview_contention(433, (4.0, 275), _c6g_out, _r6g, 433, (4.0, 275), True)
    _g_in = preview_contention(433, (4.0, 275), _c6g_in, _r6g, 735, (9.0, 578), True)
    _g_out = preview_contention(433, (4.0, 275), _c6g_out, _r6g, 735, (9.0, 578), True)
    _g_hide = preview_contention(433, (4.0, 275), _c6g_in, _r6g, 433, (4.0, 275), False)
    _g_norect = preview_contention(433, (4.0, 275), _c6g_in, None, 735, (9.0, 578), True)
    _g_onlyhide_env = preview_contention(433, (4.0, 275), _c6g_out, _r6g, 433, (4.0, 275), False)
    falsified.append(("指针争用分流：没漂移不许判争用；漂移且真光标压在轨道上判争用（ENV）；"
                      "漂移而真光标不在轨道上不许判争用（那是产品自己动的，FAIL）；"
                      "只有\"藏起来\"这一种漂移也算漂移；量具拿不到轨道矩形时不许蒙判争用",
                      _g_none[0] is False and _g_in[0] is True and _g_out[0] is False
                      and _g_hide[0] is True and _g_norect[0] is False
                      and _g_onlyhide_env[0] is False
                      and "没被动过" in _g_none[1] and "压在轨道上" in _g_in[1]
                      and "不在轨道上" in _g_out[1],
                      f"无漂移={_g_none[0]} 争用={_g_in[0]} 产品侧={_g_out[0]} "
                      f"只藏起来={_g_hide[0]} 无矩形={_g_norect[0]} 光标在轨外却藏了={_g_onlyhide_env[0]} "
                      f"｜{_g_in[1][:120]}"))

    # U6h #73 那把"播放键说法翻没翻"的尺必须有牙。它要钉的坏法也有两个方向：
    #     ① 只比方向、不比"变没变"——于是跨线程送达之前的旧话能冒充同步（r13 之前那六趟绿就是这么来的）；
    #     ② 只比"变没变"、方向用前缀——于是中性的「播放/暂停」以「播放」开头，永远绿。
    #     纯函数喂八种已知答案的形状，不真点按钮：正解两向、状态口径（修复前的产品写法）、
    #     方向对但一字不差、中性旧话蒙前缀、反方向说错、说法半句空、整句空。
    #     判绿之外还要求**理由点名对那一半**：只实现其中一条闸的尺，会在某两种形状上给出另一个理由。
    _h_ok_pause = play_tip_flip(True, "暂停（Space/K）", "播放（Space/K）")
    _h_ok_play = play_tip_flip(False, "播放（Space/K）", "暂停（Space/K）")
    _h_inverted = play_tip_flip(True, "播放/暂停（Space/K）", "暂停（Space/K）")
    _h_stale = play_tip_flip(True, "播放（Space/K）", "播放（Space/K）")
    _h_prefix = play_tip_flip(True, "暂停（Space/K）", "播放/暂停（Space/K）")
    _h_wrong_back = play_tip_flip(False, "暂停（Space/K）", "播放（Space/K）")
    _h_saying_empty = play_tip_flip(True, "暂停（Space/K）", "（Space/K）")
    _h_all_empty = play_tip_flip(True, "暂停（Space/K）", "")
    falsified.append(("播放键说法翻转尺：暂停后说「播放」、恢复后说「暂停」算对；状态口径（暂停后还说"
                      "「暂停」）、方向对但和按之前一字不差、拿中性的「播放/暂停」冒充前缀命中、"
                      "反向说错、说法半句为空都判红；且理由要点名是哪一半不成立",
                      _h_ok_pause[0] is True and _h_ok_play[0] is True
                      and _h_inverted[0] is False and _h_stale[0] is False
                      and _h_prefix[0] is False and _h_wrong_back[0] is False
                      and _h_saying_empty[0] is False and _h_all_empty[0] is False
                      and "该说" in _h_inverted[1] and "该说" in _h_prefix[1]
                      and "一字不差" in _h_stale[1] and "该说" in _h_wrong_back[1]
                      and "该说" in _h_saying_empty[1] and "该说" in _h_all_empty[1]
                      and "→" in _h_ok_pause[1],
                      f"正解暂停={_h_ok_pause[0]} 正解恢复={_h_ok_play[0]} 状态口径={_h_inverted[0]} "
                      f"旧话={_h_stale[0]} 前缀蒙={_h_prefix[0]} 反向错={_h_wrong_back[0]} "
                      f"半句空={_h_saying_empty[0]} 整句空={_h_all_empty[0]} "
                      f"｜{_h_prefix[1][:120]}"))

    # U58 V7 那把「悬停提示逐颗对账」的尺必须有牙。这一族的坏是**最静的一类**：屏幕上的那句
    #     写了旧键名，不报错、不崩、不丢样式，只是用户照提示按下去没反应；而静态扫描只认得出
    #     出口清单里的名字，换个没登记过的工厂函数就看不见——所以运行时这把按"屏幕上的那句"判。
    #     七条腿各钉一种坏法，每条都要求"注入后翻红、还原后复绿"（只要求翻红不要求复绿的尺
    #     是恒红摆设，看着严、其实量不出任何东西）：
    #       ① 改了键位没刷新（键名那半句过期）；② 登记里动作名写错字（括号静默变空，
    #          只有"动作名须在表里"那条能抓——对账本身照样对得上）；
    #       ③ 状态变了没重画（说法那半句过期）；④ 未登记却抄一句（PgDown 形状）；
    #       ⑤ 未登记却说中文键名（「空格」）；⑥ 一颗登记都没有（刷新在空转）；
    #       ⑦ 静态那三闸：读不进来要点名、改名导入要红、AST 少认一处调用要红（这条的注入
    #          方式是把 `_call_name` 摘成只认裸名，模拟"扫描器自己退化"，而不是改产品源码）。
    _self58 = sys.modules[__name__]
    _cfg58 = make_cfg()
    _w58 = build_window(_cfg58)
    _toks58 = key_tokens(keymap.effective(None))
    _tip_good, _d_tip_good = tips_agree(_w58)
    # ① 过期：改键位不刷新
    _km_old58 = _w58.cfg.get("keymap")
    _w58.cfg["keymap"] = {"off": ["S"], "on": [["J", "shuffle"]]}
    _tip_stale, _d_tip_stale = tips_agree(_w58)
    _n_ref58 = form_rows.refresh_key_hints(_w58)
    _tip_after, _d_tip_after = tips_agree(_w58)          # 新键位下应当自洽（证明不是恒红）
    _shuf_tip58 = next((str(w.toolTip()) for w, ps in form_rows.key_tips(_w58)
                        if any("shuffle" in acts for _wd, acts in ps)), "")
    _w58.cfg["keymap"] = _km_old58
    form_rows.refresh_key_hints(_w58)
    _tip_back1, _ = tips_agree(_w58)
    # ② 动作名写错字
    _b58, _ps58 = next((w, ps) for w, ps in form_rows.key_tips(_w58)
                       if any("shuffle" in acts for _wd, acts in ps))
    _saved58 = _b58.__dict__[form_rows.TIP_ATTR]
    _bcfg58, _bparts58 = _saved58
    _b58.__dict__[form_rows.TIP_ATTR] = (_bcfg58, tuple(
        (wd, tuple("tooggle_play" if a == "shuffle" else a for a in acts))
        for wd, acts in _bparts58))
    _tip_typo, _d_tip_typo = tips_agree(_w58)
    _b58.__dict__[form_rows.TIP_ATTR] = _saved58
    _tip_back2, _ = tips_agree(_w58)
    # ③ 状态变了没重画
    _st_old58 = _w58.bar._paused
    _w58.bar._paused = not bool(_st_old58)
    _tip_state, _d_tip_state = tips_agree(_w58)
    _w58.bar._paused = _st_old58
    form_rows.paint_key_tip(_w58.bar.btn_play)
    _tip_back3, _ = tips_agree(_w58)
    # ④⑤ 未登记却说了键名（两种形状各演一次）
    _q58 = QLabel("悬停", _w58)
    _q58.setToolTip("下一个（PgDown）")
    _tip_stray, _d_tip_stray = tips_agree(_w58)
    _q58.setToolTip("播放（空格）")
    _tip_alias, _d_tip_alias = tips_agree(_w58)
    _q58.setToolTip("")
    _tip_back4, _ = tips_agree(_w58)
    # ⑥ 一颗登记都没有＝刷新空转
    _stripped58 = [(w, w.__dict__.pop(form_rows.TIP_ATTR)) for w, _p in form_rows.key_tips(_w58)]
    _tip_none, _d_tip_none = tips_agree(_w58)
    for _sw, _sv in _stripped58:
        _sw.__dict__[form_rows.TIP_ATTR] = _sv
    _tip_back5, _ = tips_agree(_w58)
    _q58.destroy()
    _w58.close()
    # ⑦ 静态那三道防瞎的闸
    _syn58 = hand_copied_hints('a = mk_hint("把文件拖进来", tip)\nb = (\n', _toks58)
    _rn58 = hand_copied_hints('from .form_rows import bind_key_tip as tip\n'
                              'tip(b, cfg, "下一个（PgDown）", ("next",))\n', _toks58)
    _mod58 = 'form_rows.mk_hint("R 旋转 · Shift+R 比例", self)\n'
    _mod_norm = hand_copied_hints(_mod58, _toks58)
    _orig_cn58 = _self58._call_name
    try:
        _self58._call_name = lambda c: getattr(c.func, "id", None)   # 退化：只认裸名
        _mod_blind = hand_copied_hints(_mod58, _toks58)
    finally:
        _self58._call_name = _orig_cn58                              # 交回量具（不许留给后面几腿）
    _doc58 = hand_copied_hints('"""把 R 旋转写死是坏的（文档里的反面例子）。"""\n'
                               'x = mk_hint(computed, self)\n', _toks58)
    falsified.append(("悬停提示尺：改键不刷新／动作名写错字／状态没重画／未登记抄一句"
                      "（含中文键名）／一颗登记都没有 五种坏必须各自翻红且还原复绿；"
                      "静态三闸：语法读不进来要点名、改名导入要红、少认调用要红，"
                      "而文档里的反面例子与算出来的文案不许误报",
                      _tip_good and _tip_back1 and _tip_back2 and _tip_back3
                      and _tip_back4 and _tip_back5 and _n_ref58 > 0
                      and not _tip_stale and "shuffle" not in _d_tip_stale
                      and _tip_after and "乱序（J）" in _shuf_tip58
                      and not _tip_typo and "不在键位表里" in _d_tip_typo
                      and not _tip_state and "播放" in _d_tip_state
                      and not _tip_stray and "没登记" in _d_tip_stray
                      and not _tip_alias and "空格" in _d_tip_alias
                      and not _tip_none and "一颗登记" in _d_tip_none
                      and len(_syn58) == 1 and "读不进来" in _syn58[0]
                      and len(_rn58) == 1 and "改名导入" in _rn58[0]
                      and len(_mod_norm) == 1 and "抄了" in _mod_norm[0]
                      and len(_mod_blind) == 1 and "AST 只认到" in _mod_blind[0]
                      and not _doc58,
                      f"现状={_tip_good} 不刷新={_tip_stale}（{_d_tip_stale[:70]}）"
                      f" 刷新{_n_ref58}处后={_tip_after}（乱序那颗={_shuf_tip58!r}）"
                      f" 错字={_tip_typo}（{_d_tip_typo[:70]}） 状态={_tip_state}"
                      f"（{_d_tip_state[:70]}） 抄句={_tip_stray}（{_d_tip_stray[:70]}）"
                      f" 空格={_tip_alias}（{_d_tip_alias[:70]}） 摘光={_tip_none}"
                      f"（{_d_tip_none[:50]}） 还原={_tip_back1}{_tip_back2}{_tip_back3}"
                      f"{_tip_back4}{_tip_back5}｜语法={_syn58[:1]} 改名={_rn58[:1]} "
                      f"正常={_mod_norm[:1]} 退化={_mod_blind[:1]} 文档={_doc58}"
                      f"｜{_d_tip_good[:60]}"))

    # U4f 侧栏"当前项"这把尺子必须能否决**三种主色角色合并回一种**（本轮最容易复发的改法：
    #     只动一行、不报错、不丢样式，只是蓝压蓝只有 3.73:1）。
    #     为什么非要加一条不读代码的腿：`前景 == theme.ACCENT_TEXT` 这半条的期望值取自代码，
    #     角色一旦合并，令牌和被测值一起变，永远绿；"压在选中行实际底色 BG_ACTIVE 上够不够
    #     TEXT_MIN"量的是两个真颜色，改代码的人躲不过它。
    #     注入方式选**改令牌**而不是改样本颜色：样本按产品的取色写法（当前行读 ACCENT_TEXT、
    #     其余读 TEXT）现取现拼，这样"合并"这件事是被测代码那条路走的。
    #     档位必须锁：六档里有两档（青绿 4.68、灰 5.20）的原色本来就达标，跟着上一轮留下的
    #     主题跑会误判成"尺子没抓到"。
    _prev_accent = theme.ACCENT
    theme.set_accent(theme.ACCENT_DEFAULT)

    def _side_rows(cur: int = 1, n: int = 3) -> list[tuple[bool, str, bool]]:
        return [(i == cur, (theme.ACCENT_TEXT if i == cur else theme.TEXT).upper(), i == cur)
                for i in range(n)]

    try:
        _side_good, _ = sidebar_ink_ok(_side_rows())
        _merged_text = theme.ACCENT_TEXT
        theme.ACCENT_TEXT = theme.ACCENT          # 模拟"角色合并回标识色"
        _side_merge, d_side_merge = sidebar_ink_ok(_side_rows())
        theme.ACCENT_TEXT = _merged_text
        _side_all, _ = sidebar_ink_ok([(i == 1, theme.ACCENT_TEXT.upper(), i == 1)
                                       for i in range(3)])
        _side_none, _ = sidebar_ink_ok(_side_rows(cur=-1))
    finally:
        theme.set_accent(_prev_accent)
    falsified.append(("侧栏尺子：角色合并必须红（对比不足）、整列涂主色必须红、没有当前项也必须红",
                      _side_good and not _side_merge and not _side_all and not _side_none,
                      f"现状={_side_good} 合并={_side_merge}（{d_side_merge}）"
                      f" 全涂={_side_all} 无当前={_side_none}"))

    # U-t 标题省略号的尺子必须能否决"根本不裁"的旧写法（QLabel 画到宽度尽头就断掉，
    #     半截片名既不完整也不像被裁过）。三条腿各钉一件事：
    #       现状（中间省略 + 整句 tooltip）→ 放行；
    #       把 _elide 换成"整句直接塞标签"→ 必须红（画出来 691px > 可用 535px、且没有 …）；
    #       放得下的短名 → 必须放行**且不要求省略号**（少了这条，"永远裁"也能骗过判据）。
    #     宽度取 880（窗口最小宽）：更窄产品不允许，量到的就不是用户真能碰到的现场。
    _tb = TitleBar()
    _tb.resize(880, theme.M.title_h)
    _tb.show()
    pump(0.3)
    _t_long = "gelu-sample-1-这是一串长得放不下的片名-4K-HDR-第03集-导演剪辑-最终版-重制版-补充镜头-国语中字-60帧-1920x1080.mkv"
    _tb.set_document(_t_long)
    pump(0.2)
    _t_good, d_t_good = title_fits(_tb, _t_long)
    _t_real = _tb._elide
    _tb._elide = lambda: _tb.lbl_doc.setText(_tb._doc)      # 变异：回到"不裁"的旧写法
    _tb.set_document(_t_long)
    _t_naive, d_t_naive = title_fits(_tb, _t_long)
    _tb._elide = _t_real
    _tb.set_document("a.mp4")
    pump(0.2)
    _t_short, d_t_short = title_fits(_tb, "a.mp4")
    _t_no_hint = "…" in _tb.lbl_doc.text()
    _tb.close()
    _tb.destroy()
    falsified.append(("标题尺子：不裁的旧写法必须红，短名必须放行且不硬塞省略号",
                      _t_good and not _t_naive and _t_short and not _t_no_hint,
                      f"现状={_t_good}（{d_t_good}） 不裁={_t_naive}（{d_t_naive}）"
                      f" 短名={_t_short}（{d_t_short}） 短名带省略号={_t_no_hint}"))

    # U5 内核没启动时 dispatch 传输类动作必须原地拒绝，不能顺手把内核拉起来
    cfg = make_cfg()
    win = MainWindow(cfg, files=[])
    win.move(60, 60)
    win.show()
    pump(0.25)
    for act in ("toggle_play", "screenshot", "vol_up", "speed_up", "jump_50", "loop"):
        win.dispatch(act)
    pump(0.2)
    falsified.append(("空窗口 dispatch 不得拉起内核", not win.player.running,
                      f"running={win.player.running}"))
    win.close()

    # U6 等待器不能恒真：不可能谓词必须在预算内返回 False
    t0 = time.monotonic()
    falsified.append(("wait_for 对不可能条件必须返回 False",
                      wait_for(lambda: False, 0.4) is False and time.monotonic() - t0 < 1.5,
                      f"耗时 {time.monotonic() - t0:.2f}s"))

    # U7 气泡位置判据必须能否决"飘出画面"的情况（DPI 账算错时就是这个形状）
    win2 = build_window(cfg)
    win2.osd.show_text("气泡")
    pump(0.1)
    good, _ = osd_in_picture(win2)
    win2.osd.setGeometry(QRect(-4000, -4000, 300, 46))
    bad, d = osd_in_picture(win2)
    falsified.append(("画面外的 OSD 必须被判失败", good is True and bad is False,
                      f"正常={good} 挪走后={bad} {d}"))

    # U8 原生 MSG 布局判据必须能否决 32 位 wParam（那是本机踩过的坑）
    class _MSG32(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                    ("wParam", ctypes.c_uint32), ("lParam", ctypes.c_uint32),
                    ("time", ctypes.c_uint32)]

    falsified.append(("32 位版 MSG 必须被布局判据否决",
                      not (ctypes.sizeof(_MSG32) == 48 and _MSG32.wParam.offset == 16),
                      f"sizeof={ctypes.sizeof(_MSG32)} wParam@{_MSG32.wParam.offset}"))

    # U9 nativeEventFilter 里抛异常必须被兜住：异常穿过原生回调 = PyQt5 直接 abort 进程
    #    （第一次跑这份验收时就是这样无声退出的，日志里连栈都留不下）
    c = chrome.Chrome(win2)

    def boom(_t, _m):
        raise RuntimeError("故意炸")
    c._dispatch = boom
    try:
        handled = c.nativeEventFilter(b"windows_generic_MSG", 0)
        contained = handled == (False, 0) and bool(c.last_error)
    except Exception as exc:                    # 没兜住就会走到这里
        contained = False
        handled = f"抛出了 {exc!r}"
    falsified.append(("nativeEventFilter 的异常必须被兜住", contained,
                      f"{handled} last_error={c.last_error}"))

    # U9b 方法名写错（nativeEvent）等于没实现：基类每条消息都抛抽象方法错误
    check_ = hasattr(chrome.Chrome, "nativeEventFilter") and not hasattr(chrome.Chrome, "nativeEvent")
    falsified.append(("过滤器必须实现 nativeEventFilter（不是 nativeEvent）", check_,
                      f"有 nativeEvent={hasattr(chrome.Chrome, 'nativeEvent')}"))

    # U10 空容器（占位页阶段）时"画面在屏幕里"的判据必须判失败
    img_blankish, e_u10 = capture(win2, win2.video, "空状态视频区")
    f_case("空状态抠图必须被判为无画面", not e_u10 and not has_picture(img_blankish),
           f"{e_u10}{img_blankish.width()}×{img_blankish.height()} "
           f"stats={sample_stats(img_blankish, 16, 9)}", needs_screen=True)

    # U11 "按钮勾选态"判据必须能否决不可勾选的按钮：QToolButton 默认 checkable=False，
    #     这时 setChecked(True) 是**静默无效**的。本机就是靠这条发现循环按钮的
    #     :checked 样式从来没生效过（内核已循环、按钮看着没按下）。
    b_no = QToolButton()
    b_no.setChecked(True)
    b_yes = QToolButton()
    b_yes.setCheckable(True)
    b_yes.setChecked(True)
    falsified.append(("不可勾选按钮的 setChecked 必须被判为没生效",
                      (not b_no.isChecked()) and b_yes.isChecked(),
                      f"未设 checkable={b_no.isChecked()} 设了={b_yes.isChecked()}"))

    # U12 绘制品里抛异常必须被兜住并留痕：paintEvent 里逃出异常 = PyQt5 abort 整个进程
    #      （实测 rc=127、stdout/stderr 全空，表现为"一冒气泡播放器就无声消失"）
    orig_paint = win2.osd._paint

    def boom_paint():
        raise RuntimeError("故意炸")
    win2.osd._paint = boom_paint
    win2.osd.show_text("炸一次")
    pump(0.3)
    noted = win2.osd.last_error
    win2.osd._paint = orig_paint
    win2.osd.last_error = None
    win2.osd.hide()
    falsified.append(("OSD 绘制异常必须被兜住并记账（进程还活着）",
                      noted is not None and "RuntimeError" in str(noted), f"last_error={noted}"))

    # U13 "深底亮字"判据必须能否决只有底色没有字的气泡底
    flat_dark = QImage(240, 92, QImage.Format_ARGB32)
    flat_dark.fill(QColor(12, 13, 16))
    ok_flat, d_flat = has_ink(flat_dark)
    lit = QImage(240, 92, QImage.Format_ARGB32)
    lit.fill(QColor(12, 13, 16))
    pp = QPainter(lit)
    pp.setPen(QColor(theme.TEXT))
    pp.drawText(lit.rect(), Qt.AlignCenter, "倍速 1.1x")
    pp.end()
    ok_lit, d_lit = has_ink(lit)
    falsified.append(("没有字的深底图必须被判为无墨", (not ok_flat) and ok_lit,
                      f"空底={d_flat} 有字={d_lit}"))

    # U14 变异：把 _on_playlist 退回"只处理空列表"的旧写法，"容器回来了"的判据必须失效
    #      （旧写法实测会造成"内核在放、视频容器和占位页两层都藏着"）
    win2.video.set_empty(True)
    win2.empty.setVisible(True)

    def only_empty(pl):
        if not pl:
            win2.video.set_empty(True)
            win2.empty.setVisible(True)
    only_empty([{"id": 1, "filename": "x.mp4"}])
    old_pred = (not win2.video.isHidden()) and win2.empty.isHidden()
    win2._on_playlist([{"id": 1, "filename": "x.mp4"}])
    new_pred = (not win2.video.isHidden()) and win2.empty.isHidden()
    falsified.append(("非空列表不恢复容器的旧写法必须被判失败", (not old_pred) and new_pred,
                      f"旧写法={old_pred} 现实现={new_pred}"))

    # U15 "过滤器已摘除"判据必须能否决只关窗口不卸载的情况（悬空指针就是这么留下的）
    win3 = build_window(cfg)
    still_in = win3.chrome._installed
    win3.chrome.uninstall()
    after_un = win3.chrome._installed
    falsified.append(("卸载前必须仍处于已安装态（否则这条判据是空的）",
                      still_in and not after_un,
                      f"卸载前={still_in} 卸载后={after_un}"))
    win3.chrome.install()
    win3.close()

    # U16 循环档位判据必须能否决"UI 自己记的序号和内核不一致"：一切以内核回报为准
    win4 = build_window(cfg)
    win4.bar._loop_idx = 2                       # UI 自称"单曲循环"
    win4.player._props.pop("loop-file", None)    # 内核根本没设过单曲
    win4.player._props["loop-playlist"] = "no"
    mode = win4.bar._loop_from_kernel()
    win4.bar._on_info({"loop-playlist": "no"})   # 内核推来"不循环"，按钮必须跟
    falsified.append(("UI 档位与内核不一致时必须以内核为准并重画",
                      mode == "no" and win4.bar._loop_idx == 0 and not win4.bar.btn_loop.isChecked(),
                      f"内核判={mode} 重画后 idx={win4.bar._loop_idx}"))
    win4.close()

    # U17 崩溃日志判据要两头都成立：装了钩子必须留痕、没装钩子必须不留痕
    #     （只验"留痕"这一头，判据可能被文件里的陈旧内容满足而恒真）
    m_on = f"gelu-hook-on-{os.getpid()}-{int(time.time() * 1000)}"
    wrote_on, stale = crash_probe(m_on)
    sys.excepthook = sys.__excepthook__          # 退回默认钩子：它只往 stderr 印，不落盘
    m_off = f"gelu-hook-off-{os.getpid()}-{int(time.time() * 1000)}"
    try:
        raise ValueError(m_off)
    except ValueError:
        sys.__excepthook__(*sys.exc_info())      # 下面输出里那条故意的 traceback 就是它
    p_crash = crash_log_path()
    txt = open(p_crash, encoding="utf-8").read() if os.path.exists(p_crash) else ""
    falsified.append(("崩溃日志两头都要成立：装钩子留痕、不装钩子不得留痕",
                      wrote_on and (not stale) and (m_off not in txt),
                      f"装钩子留痕={wrote_on} 抛前已命中={stale} 未装钩子却留痕={m_off in txt}"))
    install_crash_log()

    # U18 悬停预览的"里面有帧"判据必须能否决纯色空框：否则"浮层里有画面"这条
    #      只是"浮层出现了"的换一种说法。两头都验：彩色帧必须判"有画面"（对照组），
    #      纯色帧必须判"无画面"——少了对照组，浮层压根没显示也会被算成"抓到了"。
    win2.video.setVisible(True)
    win2.empty.setVisible(False)
    pump(0.2)

    def put_preview(pm):
        tl = win2.video.mapToGlobal(QPoint(0, 0))
        win2.preview.show_at(pm, "00:05", tl.x() + 300, win2.bar.mapToGlobal(QPoint(0, 0)).y() - 8)
        pump(0.5)                       # 分层窗口要把这一帧端上屏，立刻抠图会拍到底下的空页

    lit_pm = QPixmap(200, 113)
    lit_pm.fill(QColor("#101418"))
    lp = QPainter(lit_pm)
    for i, col in enumerate(("#FF3B30", "#34C759", "#007AFF", "#FFCC00", "#AF52DE", "#FFFFFF")):
        lp.fillRect(QRect(8 + i * 32, 18, 26, 74), QColor(col))
    lp.end()
    flat_pm = QPixmap(200, 113)
    flat_pm.fill(QColor("#101418"))

    put_preview(lit_pm)
    shot_lit, e_lit = capture(win2.preview, win2.preview, "彩色帧预览", ((0.5,), (0.5,)))
    pos = has_picture(shot_lit) and not e_lit
    put_preview(flat_pm)
    shot_flat, e_flat = capture(win2.preview, win2.preview, "纯色帧预览", ((0.5,), (0.5,)))
    neg = has_picture(shot_flat) and not e_flat
    f_case("纯色空框必须被判为无画面，且彩色帧必须判为有画面",
           pos and not neg,
           f"彩色={pos}({e_lit}{shot_lit.width()}×{shot_lit.height()} {sample_stats(shot_lit, 24, 12)}) "
           f"纯色={neg}({e_flat}{sample_stats(shot_flat, 24, 12)})", needs_screen=True)

    # U19 预览浮层的位置判据必须能否决"飘出画面/盖住控制条"（DPI 账算错就是这个形状）
    put_preview(flat_pm)
    good_pv, _ = floater_in_picture(win2.preview)
    win2.preview.setGeometry(QRect(-4000, -4000, 212, 141))
    bad_pv, d_pv = floater_in_picture(win2.preview)
    falsified.append(("画面外的悬停预览必须被判失败，且正常摆放不得误报",
                      good_pv is True and bad_pv is False,
                      f"正常={good_pv} 挪走后={bad_pv} {d_pv}"))
    win2.preview.hide_preview()

    # U20 卡片"有帧/没帧"的判据必须能否决占位块，而且占位块自己得先能通过
    #     图标的类型关。真实踩过的一次是 _placeholder() 返回 QPixmap：模型换图标
    #     只收 QIcon，TypeError 从 C++ 投递的信号槽里抛出来没人接得住，进程直接
    #     abort（rc=127、stderr 全空）。所以这里两头都判：占位块判"无帧"（否掉
    #     "卡片有色就是有封面"）、真封面判"有帧"（否掉判据恒假）。
    ph_icon = LP._placeholder({"ext": "mp4"})
    ph_is_qicon = isinstance(ph_icon, QIcon)
    if ph_is_qicon:
        ph_img = card_image(ph_icon)
        ph_stats = sample_stats(ph_img, 24, 12)
        ph_pic = has_picture(ph_img)
    else:
        ph_img, ph_stats, ph_pic = None, {}, False
    v1 = os.path.join(SAMPLES, "gelu-sample-1.mp4")
    win2.posters.request(v1, 10.0)
    k1 = win2.posters.key_for(v1)
    got_real = wait_for(lambda: win2.posters.image(k1) is not None, 30.0)
    pm_real = win2.posters.image(k1)
    real_img = card_image(QIcon(pm_real) if pm_real is not None else QIcon())
    real_pic = has_picture(real_img)
    falsified.append(("卡片封面判据两头都要成立：占位块是 QIcon 且判无帧，真帧判有帧",
                      ph_is_qicon and (not ph_pic) and (got_real and real_pic),
                      f"占位块是QIcon={ph_is_qicon} 判有帧={ph_pic}({ph_stats}) "
                      f"真帧取到={got_real} 判有帧={real_pic}({sample_stats(real_img, 24, 12)})"))

    # U21 「卡片数与条目一致」在空表时是恒真的：0==0。把谓词拎成函数就是为了能注入
    #      这组已知坏样本，顺便确认真实那组 (4, 4) 不会被误杀。
    falsified.append(("空表的一致性必须判不过（0==0 不算通过）",
                      (not cards_agree(0, 0)) and cards_agree(4, 4),
                      f"空表={cards_agree(0, 0)} 四行={cards_agree(4, 4)} "
                      f"三行不齐={cards_agree(3, 4)}"))

    # U22 双击的两条腿都得是自己挣的：
    #     (a) 裸发 DblClick 不该触发 doubleClicked——触发了说明 clicked 那个返回值
    #         不需要四步序列，"事件送对了"就成了白写的判据；
    #     (b) 断掉 grid→panel 那段链后，信号照旧发得出（说明 (a)(b) 量的不是同一段），
    #         但 sig_play_path 必须发不出来（说明"内核换片"那半句真的依赖这条链）。
    #     侧栏用手摆而不走 _set_sidebar：那条路在库为空时会自己起一趟扫描，
    #     扫完把整表推回来——正中间的这份网格被换掉，(a)(b) 量的就是别人的卡片了。
    from app.engine.library import entry_for as _ef
    ents = [_ef(os.path.join(SAMPLES, n)) for n in ("gelu-sample-1.mp4", "gelu-sample-2.mp4")]
    win2.sidebar.setVisible(True)
    win2.sidebar_stack.setCurrentIndex(1)
    pump(0.3)
    win2.lib_panel.set_items(ents)
    pump(0.3)
    r_tgt = row_of(win2.lib_panel.grid, "gelu-sample-2.mp4")
    bare = bare_dblclick(win2.lib_panel.grid, r_tgt)
    emitted: list[str] = []
    win2.lib_panel.sig_play_path.connect(emitted.append)
    for sig in (win2.lib_panel.grid.doubleClicked, win2.lib_panel.grid.activated):
        try:
            sig.disconnect()
        except Exception:
            pass        # 本来就没连（不该发生，但断链工具自己不许把测试弄死）
    clicked_broken = dblclick_row(win2.lib_panel.grid, r_tgt)
    pump(0.4)
    falsified.append(("双击的两条腿都得自己挣：裸发不触发、断链后不得再发播放请求",
                      (not bare) and clicked_broken and not emitted,
                      f"裸发触发={bare} 断链后信号发出={clicked_broken} "
                      f"断链后仍请求播放={emitted} 行数={win2.lib_panel.grid.model().rowCount()} "
                      f"行号={r_tgt}"))
    win2.sidebar.setVisible(False)
    win2.video.refit()
    pump(0.2)

    # U23 抓屏闸门必须两头都能翻：别的窗口盖上来要拒绝，撤走后要放行。
    #     只验"会拒绝"是不够的——一个恒报"被盖住"的闸门同样能让每条留档判据变红，
    #     而那等于把判据全删了。另一半（撤走后 err 为空、图非空）才是"闸门不误伤"。
    #     这里把 _set_topmost 摘掉来造"抬不起来"的状态：那次 TOPMOST 提升正是为了让
    #     我们自己回到最前，摘掉它就等于回到"别的窗口压着我们"的现场——闸门必须改口
    #     拒绝，而不是拿着别人的像素判 PASS（真踩过：留档整张是别人的弹窗，判据全绿）。
    g_vid = win2.video.mapToGlobal(QPoint(0, 0))
    cover = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    cover.setAttribute(Qt.WA_ShowWithoutActivating, True)
    cover.setGeometry(int(g_vid.x()) + 10, int(g_vid.y()) + 10, 240, 160)
    cover.show()
    pump(0.3)
    real_topmost = globals()["_set_topmost"]
    globals()["_set_topmost"] = lambda hwnd, on: None      # 变异：拿掉越前台锁的那次提升
    img_blocked, e_blocked = capture(win2, win2.video, "被盖住的视频区")
    globals()["_set_topmost"] = real_topmost
    pump(0.3)
    img_free, e_free = capture(win2, win2.video, "视频区")
    _hb, _hp = (0, 0) if not e_free else env_blocker(win2.video)
    cover.close()
    cover.destroy()
    pump(0.3)
    # "必须放行"那一半的前提是"我们的窗口此刻抬得起来"。屏幕上别人的窗口（实测是用户的
    # 浏览器）压在最上层时，SetWindowPos(HWND_TOPMOST) 也夺不过来——那是环境，不是产品坏了，
    # 而测试无权最小化/关闭用户的应用。所以这一半按 pid 分流：外来进程→本轮不作数（None），
    # 自家句柄→仍然记 MISSED（那才是我们自己的浮层没抬起来）。
    # 另一头不许松：摘掉提升的那一半必须照旧被抓到，否则这条判据退化成"环境一挡就全绿"。
    #     屏幕点不亮时整条挂 ENV（f_case 的 needs_screen）：这一条的两半都吃真抓屏与真 Z 序，
    #     显示器休眠时 capture() 谁都抬不起来、抠出来又全是 0，两半的量都作废——
    #     拿它记 MISSED 就是把环境状态冒充检测器结论（本轮实测：整屏亮度 0 时它红过一次）。
    _free_ok = (not e_free) and not img_free.isNull()
    _exempt = _exempt_blocker(_hb, _hp)
    _verdict = None if _exempt else _free_ok
    f_case("遮挡闸门两头都要成立：抬不起来时必须拒绝、提升成功后必须放行",
           bool(e_blocked) and img_blocked.isNull() and _verdict,
           f"摘掉提升时 err={e_blocked!r} 图为空={img_blocked.isNull()}；"
           f"装回提升后 err={e_free!r} 图={img_free.width()}×{img_free.height()}"
           + ("" if _hb == 0 else
              f"；挡路的是{'外来' if _exempt else '自家'}窗口 {_hwnd_debug(_hb)}"
              f"（pid {_hp}，我们 {os.getpid()}）"
              + ("，这一半本轮不作数" if _exempt else "")),
           needs_screen=True)

    # U23c capture() 里那条"外来窗口 ⇒ 本轮不作数"的分流必须**接得上线**：U23b 只钉了
    #     纯函数 _exempt_blocker 的四条已知答案，没钉"闸门真的照它记台账"。写反了会是最坏
    #     的一种形状——所有遮挡（包括我们自己的浮层没抬起来）都能贴上"环境问题"的免检金牌。
    #     两条腿：自家句柄挡路 ⇒ 走普通拒绝、判废台账一个字不许涨；把分流摘了（变异）⇒
    #     同一种挡路必须被记成 ENV（证明这条腿真的在接线）。
    #     现场用**接线桩**（把 _hwnd_chain_at 换成"命中链上只有一个自家句柄"）而不是真摆一个
    #     遮挡窗：上一版真摆过，结果 capture() 自己的 raise_() 把探针抬到了遮挡窗上面，
    #     闸门量到"没挡路"⇒ 这条随机 MISSED。Z 序这一头的形状 U23/U23b 已经用真窗口钉过了，
    #     这一条只负责"分流结果有没有落进台账"，所以桩换成真实存在的**自家句柄**
    #     （pid 由 _hwnd_pid 现场读，不是硬写的），两家分流仍由真 API 返回值决定。
    _env_before = len(env_notes)
    blocker = QWidget(None, Qt.FramelessWindowHint)
    _blk_hwnd = int(blocker.winId())           # 真实存在、不属于 win2 的自家句柄（不必显示）
    globals()["_real_chain"] = globals()["_hwnd_chain_at"]
    globals()["_real_exempt"] = _exempt_blocker
    globals()["_hwnd_chain_at"] = lambda _x, _y: [_blk_hwnd]
    try:
        img_own, e_own = capture(win2, win2.video, "自家挡路（接线桩）")
        # 分类的证据只取**台账长度**与**判废集合**：闸门里"记 ENV"这一动作只有外来分支会走，
        # 所以"台账没涨、名字没进 CAPTURE_ENV"就是"没被当成环境问题"。不拿错误文案当判据
        # （文案一改就把真接线判成断线）。
        _own_ordinary = (bool(e_own) and img_own.isNull() and len(env_notes) == _env_before
                         and "自家挡路（接线桩）" not in CAPTURE_ENV)
        globals()["_exempt_blocker"] = lambda h, p: True      # 变异：谁都给免检金牌
        img_gold, e_gold = capture(win2, win2.video, "免检金牌下的视频区")
        _gold_hits = (len(env_notes) > _env_before and img_gold.isNull()
                      and "免检金牌下的视频区" in CAPTURE_ENV)
        _blk_pid = _hwnd_pid(_blk_hwnd)
    finally:
        globals()["_hwnd_chain_at"] = _real_chain
        globals()["_exempt_blocker"] = _real_exempt
        del globals()["_real_chain"], globals()["_real_exempt"]
        del env_notes[_env_before:]        # 证伪跑次不许把判废台账带给收尾码
        # 判废集合同样不许留给后面：这两块的名字留着，main() 那八处 pixel_check 里同名的一块
        # 就会拿"上一趟的环境结论"免检自己这一趟（capture() 进门也摘，这里再摘一次是双保险，
        # 因为证伪这一趟根本不会再用这两个名字去抓）。
        CAPTURE_ENV.discard("自家挡路（接线桩）")
        CAPTURE_ENV.discard("免检金牌下的视频区")
        blocker.close()
        blocker.destroy()
        pump(0.2)
    falsified.append(("自家句柄挡路必须走普通拒绝（判废台账与判废集合一个字不许涨）；把豁免分流"
                      "摘掉后同一种挡路必须被记成 ENV（证明这条接线真的在起作用）",
                      _own_ordinary and _gold_hits and _blk_pid == os.getpid(),
                      f"自家普通拒绝={_own_ordinary}（err 非空={bool(e_own)} 图为空="
                      f"{img_own.isNull()} 名字没进判废集合）金牌下记 ENV={_gold_hits} 挡路句柄 "
                      f"pid={_blk_pid}（我们 {os.getpid()}）｜台账已还原到 {len(env_notes)} 条"))

    # U23b 豁免分流必须有已知答案的对照：**自家**窗口挡路时不许豁免，外来的才豁免。
    #     没有这一条，上面那个 None 分支就是永久免检金牌——自家浮层没抬起来也能说成"环境问题"。
    #     两处设计上的坑（都是这一轮实测踩到的）：
    #       · 采样点不能选视频区：那一带此刻正被用户的浏览器（外来 topmost）压着，
    #         自家遮挡窗争不过中心点，第一条腿会量成"没挡路"，误报成分流器坏。
    #       · 不能直接对 probe 调 env_blocker 来造"自家挡路"：它自己就会 raise_ + TOPMOST
    #         把 probe 抬回最前，遮挡窗被它亲手撤走（上一版就是这么量出 0 的）。
    #         所以这一档把 _set_topmost 摘掉——等于"我们抬不动自己的窗口"，
    #         那才是"自家浮层没起来"的真实现场。
    _scr = APP.primaryScreen().geometry()
    probe = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    probe.setAttribute(Qt.WA_ShowWithoutActivating, True)
    probe.setGeometry(_scr.right() - 130, _scr.top() + 20, 110, 90)
    probe.show()
    pump(0.35)
    _h0, _p0 = env_blocker(probe)                  # 干净现场：应当报 0
    cover2 = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    cover2.setAttribute(Qt.WA_ShowWithoutActivating, True)
    cover2.setGeometry(probe.x() + 4, probe.y() + 4, probe.width() - 8, probe.height() - 8)
    cover2.show()
    pump(0.35)
    _real_topmost = globals()["_set_topmost"]
    globals()["_set_topmost"] = lambda hwnd, on: None
    try:
        _own_h, _own_pid = env_blocker(probe)      # 抬不动自己：自家遮挡窗必须被点名
        # 点名要在句柄还活着的时候取。上一版把 _hwnd_debug(_own_h) 留在 append 的详情里，
        # 而那时 cover2 已经 destroy()，打出来是"?/外来pid0/隐藏/(0,0,0,0)"——把自家的
        # 遮挡窗说成外来窗口，正好是这条判据要防的那种谎。
        _own_dbg = _hwnd_debug(_own_h) if _own_h else "没量到"
    finally:
        globals()["_set_topmost"] = _real_topmost
    cover2.close()
    cover2.destroy()
    pump(0.35)
    _none_h, _none_pid = env_blocker(probe)        # 撤走遮挡：必须重新报 0
    probe.close()
    probe.destroy()
    pump(0.2)
    # 分流本身是一条纯函数规则，四条已知答案：没挡路→不豁免（走正常判据）、自家挡路→不豁免、
    # 外来挡路→豁免、pid 读不出来→不豁免（读不出就豁免等于免检金牌）。
    # 写成扁平四元组：上一版多套了一层 list，比较式右边是 [F,F,T,F]、左边是 [(F,F,T,F)]，
    # 永远不相等，于是这条对照把自己判成 MISSED——对照自己失效时最该警惕的就是这种"恒红"。
    _ex = (_exempt_blocker(0, 0), _exempt_blocker(_own_h, os.getpid()),
           _exempt_blocker(_own_h, os.getpid() + 1), _exempt_blocker(_own_h, 0))
    falsified.append(("分流器：自家窗口挡路必须点名且 pid=自己，干净与撤走后必须报 0，"
                      "且只有外来 pid 能拿到豁免",
                      _own_h != 0 and _own_pid == os.getpid() and _h0 == 0 and _none_h == 0
                      and _ex == (False, False, True, False),
                      f"干净现场={_h0}/{_p0} 自家挡路={_own_dbg} pid={_own_pid}"
                      f" 撤走后={_none_h}/{_none_pid} 豁免分流(无/自家/外来/无pid)={_ex}"))

    # U24 面板↔可调项表的判据必须能否决两种"静默少一档"：拔掉一根滑轨、
    #     以及滑轨还在但绑错了表项（键集合对得上，只有绑定错——旧版 SUB_FLAG[1:]
    #     少画一根就是这一类：数量差能被抓住，绑错只能靠 `row.spec is spec`）。
    tp = win2.tweak_panel
    tbl0, _ = panel_matches_table(tp)
    row_b = tp._rows["brightness"]
    tp._rows.pop("brightness")
    tbl1, d_tbl1 = panel_matches_table(tp)
    tp._rows["brightness"] = row_b
    saved_spec = row_b.spec
    row_b.spec = tweaks.spec("gamma")
    tbl2, d_tbl2 = panel_matches_table(tp)
    row_b.spec = saved_spec
    tbl3, _ = panel_matches_table(tp)
    falsified.append(("表↔控件的判据必须能否决少一根滑轨与滑轨绑错表项，且复原后放行",
                      tbl0 and (not tbl1) and (not tbl2) and tbl3,
                      f"原样={tbl0} 拔掉={tbl1}({d_tbl1}) 绑错={tbl2}({d_tbl2}) 复原={tbl3}"))

    # U25 "界面画的==内核回报的"必须能否决三类控件各自跑偏。
    #     内核回报直接写进订阅缓存（prop() 读的就是这一份，不发 IPC），
    #     所以这条不需要真起内核：判据的输入是缓存，写缓存就等于模拟内核推值。
    def seed_cache():
        for _p, _r in tp._rows.items():
            tp.p._props[_p] = _r.value_of(_r.slider.value())
        for _p, _cb in tp._choices.items():
            tp.p._props[_p] = _cb.currentData()
        for _p, _c in tp._flags.items():
            tp.p._props[_p] = _c.isChecked()

    seed_cache()
    ag0, _ = panel_agrees_with_kernel(tp)
    row_b.slider.blockSignals(True)
    row_b.slider.setValue(3)
    row_b.slider.blockSignals(False)
    ag1, d_ag1 = panel_agrees_with_kernel(tp)
    fl = tp._flags["sub-bold"]
    fl.blockSignals(True)
    fl.setChecked(not fl.isChecked())
    fl.blockSignals(False)
    ag2, d_ag2 = panel_agrees_with_kernel(tp)
    ch = tp._choices[tweaks.TONE_MAPPING.prop]
    ch.blockSignals(True)
    ch.setCurrentIndex((ch.currentIndex() + 1) % ch.count())
    ch.blockSignals(False)
    ag3, d_ag3 = panel_agrees_with_kernel(tp)
    tp.repaint_from_kernel()
    ag4, d_ag4 = panel_agrees_with_kernel(tp)
    falsified.append(("一致判据必须能否决滑轨/勾选/下拉各自跑偏，且按缓存重画后放行",
                      ag0 and (not ag1) and (not ag2) and (not ag3) and ag4,
                      f"原样={ag0} 滑轨跑偏={ag1}({d_ag1}) 勾选={ag2}({d_ag2}) "
                      f"下拉={ag3}({d_ag3}) 重画后={ag4}({d_ag4})"))

    # U26 画面复位判据：全 0、留脏值、内核根本没回报——三种必须分开判。
    #     把"没回报"和"回到 0"混成通过，就是 `float(v or 0)` 那个坑的两种表现。
    for _s in tweaks.EQ:
        tp.p._props[_s.prop] = 0.0
    z0 = eq_all_zero(win2)
    tp.p._props[tweaks.EQ[0].prop] = 30.0
    z1 = eq_all_zero(win2)
    tp.p._props[tweaks.EQ[0].prop] = None
    z2 = eq_all_zero(win2)
    falsified.append(("复位判据必须能否决留了 30 的脏值，也不能把「没回报」当成 0",
                      z0 and (not z1) and (not z2), f"全 0={z0} 留 30={z1} 无回报={z2}"))

    # U27 布局判据必须能否决 1px 滑轨与"视口比内容还高"（滚动区没承重=下面被挤出可视范围）
    win2._set_sidebar(True, page_index(win2, "调节"))
    pump(0.4)
    lay0, d_lay0 = panel_layout_ok(tp)
    row_b.slider.setMinimumWidth(1)
    row_b.slider.setMaximumWidth(1)
    pump(0.2)
    lay1, d_lay1 = panel_layout_ok(tp)
    row_b.slider.setMinimumWidth(0)
    row_b.slider.setMaximumWidth(_QMAX)
    # 第三腿：把滑轨控件自己压回"没有 theme.SLIDER_HANDLE 那句话"的高度（实测 11）。
    # 这一腿管的是本轮真窗口量到的那个形状缺陷——把手是一颗 14 的圆，控件只有 11 高时
    # 上下各被裁 1.5px，圆变鼓形。样式表里一个字都没错，只有控件几何知道。
    _sh_saved = row_b.slider.minimumHeight()
    row_b.slider.setMinimumHeight(0)
    row_b.slider.setMaximumHeight(11)
    pump(0.2)
    lay1b, d_lay1b = panel_layout_ok(tp)
    row_b.slider.setMaximumHeight(_QMAX)
    row_b.slider.setMinimumHeight(_sh_saved)
    # 反向变异：整页压到比视口还矮，滚动区就没事可做了（body_h > vp_h 那半边必须能翻）
    tp._body.setMinimumHeight(200)
    tp._body.setMaximumHeight(200)
    pump(0.2)
    lay2, d_lay2 = panel_layout_ok(tp)
    tp._body.setMinimumHeight(0)
    tp._body.setMaximumHeight(_QMAX)
    pump(0.2)
    lay3, d_lay3 = panel_layout_ok(tp)
    falsified.append(("布局判据必须能否决 1px 滑轨、比把手矮的滑轨与滚不动的整页，且复原后放行",
                      lay0 and (not lay1) and (not lay1b) and (not lay2) and lay3,
                      f"原样={lay0}({d_lay0}) 压窄={lay1}({d_lay1}) 压矮={lay1b}({d_lay1b}) "
                      f"拉高={lay2}({d_lay2}) 复原={lay3}({d_lay3})"))

    # U28 内核没启动时这一页必须"原地拒绝"：A-B 禁用、设备下拉只给未启动提示且没有
    #     可选数据（itemData=None 时 _on_device 会直接 return，不会顺手发 setProperty）。
    tp.page_shown()
    ab_before = tp.p.ab_range()
    tp.mark_ab("a")
    pump(0.2)
    ab_after = tp.p.ab_range()
    hint_ok = (not tp.btn_ab_a.isEnabled() and not tp.btn_ab_b.isEnabled()
               and not tp.btn_ab_c.isEnabled()
               and tp.cb_device.count() >= 1 and tp.cb_device.itemData(0) is None
               and TP.DEVICE_HINT in tp.cb_device.itemText(0)
               and not tp.p.running
               and ab_before is None and ab_after is None)
    falsified.append(("无内核时：A-B 全禁用、设备下拉显示未启动且设点不生效",
                      hint_ok,
                      f"按钮可用={tp.btn_ab_a.isEnabled()}/{tp.btn_ab_b.isEnabled()}/"
                      f"{tp.btn_ab_c.isEnabled()} 下拉首项={tp.cb_device.itemText(0)!r}"
                      f" data={tp.cb_device.itemData(0)!r} running={tp.p.running} "
                      f"A-B {ab_before}→{ab_after}"))

    # U29 「内核给的值不在预设里」那条临时项必须在重画后消失：留在下拉里会越加越多，
    #     而且用户下次点开看到的是自己没选过的"当前 #123456"。
    cb_col = tp._choices[tweaks.SUB_COLOR.prop]
    n_col = cb_col.count()
    cb_col.blockSignals(True)
    cb_col.insertItem(0, "当前 #FF123456", "#FF123456")
    cb_col._gelu_custom = True
    cb_col.blockSignals(False)
    grew = cb_col.count() != n_col
    tp._paint_choice(tweaks.SUB_COLOR.prop, "#FFFFF700")
    falsified.append(("预设外的自定义项必须只在需要时出现、重画后被清掉",
                      grew and cb_col.count() == n_col and not getattr(cb_col, "_gelu_custom", False),
                      f"塞进去变长={grew} 重画后项数={cb_col.count()}（应为 {n_col}） "
                      f"残留标记={getattr(cb_col, '_gelu_custom', False)} 当前={cb_col.currentData()!r}"))
    tp.repaint_from_kernel()
    win2._set_sidebar(False)
    pump(0.2)

    # U30 硬解行的判读必须能否决三种写法：把 'no' 念成"硬解 no"、内核还没回报就猜
    #     "软件解码"、以及有回报却把整行藏着。对照组（真值与空值各一种正常写法）必须
    #     放行，否则这条判据可能只是恒红——那和第 9.10 节没写过是一样的。
    g1, _ = hwdec_row_agrees("d3d11va", True, "硬件 d3d11va")
    g2, _ = hwdec_row_agrees(None, False, "—")
    b1, d_b1 = hwdec_row_agrees("no", True, "硬件 no")
    b2, d_b2 = hwdec_row_agrees(None, True, "软件解码")
    b3, d_b3 = hwdec_row_agrees("nvdec", False, "硬件 nvdec")
    falsified.append(("硬解行判据必须能否决硬件 no、没回报却猜软件解码、有回报却藏着，且两种正常写法放行",
                      g1 and g2 and (not b1) and (not b2) and (not b3),
                      f"正常={g1}/{g2} 硬件no={b1}({d_b1}) 猜值={b2}({d_b2}) 藏着={b3}({d_b3})"))

    # U31 "当前在哪一页"在四个地方各写了一份，判据必须能否决其中任何一处漏同步
    #     下标一律按标签查（`page_index`）：这一带原先写死 0 与 1，加一页就会让它们各差一格。
    _p0 = page_index(win2, "播放列表")
    _p_lib = page_index(win2, "媒体库")
    win2._set_sidebar(True, _p0)
    pump(0.3)
    base_ok, _ = tab_state_agrees(win2, _p0)
    win2.bar.btn_tweak.setChecked(True)                  # 灯跟着上一页
    m1, d_m1 = tab_state_agrees(win2, _p0)
    win2.bar.btn_tweak.setChecked(False)
    win2.tab_btns[_p_lib].setChecked(True)               # 两个页签同时亮
    m2, d_m2 = tab_state_agrees(win2, _p0)
    win2.tab_btns[_p_lib].setChecked(False)
    win2.stats_panel.setVisible(True)                    # 两页同时可见
    m3, d_m3 = tab_state_agrees(win2, _p0)
    win2.stats_panel.setVisible(False)
    win2._sidebar_page(_p0)
    back_ok, d_back = tab_state_agrees(win2, _p0)
    falsified.append(("页签一致性判据必须能否决灯跟旧页、两页签同亮、两页同可见，且复原后放行",
                      base_ok and (not m1) and (not m2) and (not m3) and back_ok,
                      f"原样={base_ok} 灯错位={m1}({d_m1}) 双页签={m2} 双页={m3} 复原={back_ok}({d_back})"))

    # U32 "每一行都有数据源"必须能否决一行挂在内核根本没有的属性名上：这种行不报错、
    #     不崩溃，只是一辈子空着——而把它填成 0 就是"没测到"冒充"测到 0"。
    keys_real = list(win2.stats_panel._rows)
    polled_tbl = list(win2.player.STATS_PROPS)
    s0, d_s0 = rows_have_source(keys_real, _OBSERVED, polled_tbl)
    s1, d_s1 = rows_have_source(keys_real + ["video-bitrate"], _OBSERVED, polled_tbl)
    falsified.append(("死行必须被检出（数据源两张表都没有的属性名），且真实行表不得误报",
                      s0 and (not s1), f"真实={s0} 注入死行={s1}({d_s1}) {d_s0}"))

    # U33 睡眠定时的取消必须真的撤销在途 QTimer：把 _stop_timers 摘掉之后，cancel()
    #     表面上"没设了"（armed 翻假），底下那条 QTimer 还在跑——这就是"取消之后照样
    #     到点"那一类最难查的 bug。另外 `_fire()` 的 armed 守卫是第二层防线，单独判。
    from app.ui.sleep_timer import SleepTimer
    t3 = SleepTimer()
    fired: list[str] = []
    t3.sig_expire.connect(fired.append)
    t3.arm(30, "pause")
    live_before = bool(t3._fire_timer is not None and t3._fire_timer.isActive())
    t3.cancel()
    clean = t3._fire_timer is None and not t3.armed
    orig_stop = t3._stop_timers
    t3._stop_timers = lambda: None                       # 变异：取消只清状态、不停表
    t3.arm(30, "pause")
    t3.cancel()
    leaked = t3._fire_timer is not None and t3._fire_timer.isActive()
    t3._stop_timers = orig_stop
    if t3._fire_timer is not None:
        t3._fire_timer.stop()
    t3._fire_timer = t3._warn_timer = None
    t3._deadline = 0.0
    t3._fire()                                            # 已取消却到点：armed 守卫必须拦住
    falsified.append(("取消必须停掉在途 QTimer（不停表的写法必须被抓到），且到点方法对已取消的表不得发信号",
                      live_before and clean and leaked and not fired,
                      f"设定时表在跑={live_before} 正常取消={clean} 变异后表仍在跑={leaked} "
                      f"已取消还发信号={fired}"))

    # U34 立即生效那一项必须真的进 changed_now。把它的生效时机写成"重启后生效"，设置页
    #     照样保存、照样提示，但主窗口收不到值 → 界面纹丝不动：用户看到的就是"设置没
    #     存住"。这条抓的是时机标注与信号内容之间的那根线，两头都判（写错丢项、改回 came back）。
    from app.ui import settings_dialog as SD
    import dataclasses
    cfg6 = make_cfg()
    win6 = build_window(cfg6)
    d6 = SettingsDialog(cfg6, win6)
    applied6: list[tuple[dict, list]] = []
    d6.sig_applied.connect(lambda ch, rs: applied6.append((dict(ch), list(rs))))
    items_orig = SD.ITEMS
    sw = d6._widgets["screenshot_include_sub"]
    orig_flag = bool(cfg6.get("screenshot_include_sub"))
    sw.setChecked(not orig_flag)
    d6._save()                                   # 原表：应当作为"立即生效"发出去
    control = dict(applied6[-1][0]) if applied6 else {}
    sw.setChecked(orig_flag)
    SD.ITEMS = tuple(dataclasses.replace(i, effect=SD.EFFECT_RESTART)
                     if i.cfg == "screenshot_include_sub" else i for i in items_orig)
    d6._save()                                   # 坏表：同样的操作只该进"要重启"那份名单
    mutated_changed, mutated_restart = applied6[-1]
    SD.ITEMS = items_orig
    falsified.append(("时机标注决定这一项走哪条信号：写成重启后生效就必须从 changed_now 里消失、"
                      "回到立即生效又必须重新出现（值本身两次都存住了）",
                      control.get("screenshot_include_sub") is (not orig_flag)
                      and "screenshot_include_sub" not in mutated_changed
                      and mutated_restart == ["screenshot_include_sub"]
                      and bool(cfg6.get("screenshot_include_sub")) is orig_flag,
                      f"原表 changed={control} 坏表 changed={mutated_changed} "
                      f"restart={mutated_restart} 落盘={cfg6.get('screenshot_include_sub')}"))
    win6.close()

    # U35 快捷键表与 dispatch 分支的判据必须能否决"表加一行、分支忘写"
    src_disp = inspect.getsource(MainWindow.dispatch)
    c0, _ = keymap_covered({a for _, a, _ in KEYMAP}, src_disp)
    c1, d_c1 = keymap_covered({a for _, a, _ in KEYMAP} | {"toggle_super_stats"}, src_disp)
    falsified.append(("表里没有分支的动作名必须被检出，且真实快捷键表不得误报",
                      c0 and (not c1), f"真实={c0} 注入={c1}({d_c1})"))

    # U36 "内核没回报就整行不画"必须三个方向都对：没数时藏着（正向）、把没数的行摆出来
    #     要判红（变异）、把数还回去必须画回来（否掉"这行永远藏着"那种恒真写法）。
    #     前提是侧栏真的开着：isVisible() 算上祖先，侧栏藏着时怎么摆都量不到这一行自己
    #     ——第一版就在这里漏掉了变异（值都写出 2 帧了还判"藏着"）。
    #     切到第几页**按标签查**（见 `page_index`）：这一行原先抄的是 4，而 2026-10-12 多了
    #     「声音均衡」那一格之后 4 是均衡页、统计页挪到 5——r13 真机因此记了两条 MISSED。
    win2._set_sidebar(True, page_index(win2, "统计"))
    pump(0.35)
    sp2 = win2.stats_panel
    ancestor_shown = sp2.isVisible()
    h0, d_h0 = row_hidden_without_data(sp2, "frame-drop-count")
    sp2._rows["frame-drop-count"].setVisible(True)
    h1, d_h1 = row_hidden_without_data(sp2, "frame-drop-count")
    sp2._polled["frame-drop-count"] = 2
    sp2._paint()
    h2, d_h2 = row_hidden_without_data(sp2, "frame-drop-count")
    sp2._polled.pop("frame-drop-count", None)
    sp2._paint()
    falsified.append(("丢帧行：没数时必须藏着、硬摆出来必须判红、有数时必须画回来",
                      ancestor_shown and h0 and (not h1) and (not h2),
                      f"统计页下标={page_index(win2, '统计')} 侧栏可见={ancestor_shown} "
                      f"没数={h0}({d_h0}) 摆出来={h1}({d_h1}) "
                      f"有数={h2}({d_h2})"))
    win2._set_sidebar(False)
    pump(0.2)

    # U37 抓屏通路的健康判据必须两头都夹住：喂一张已知全黑的图必须判"屏幕不给像素"，
    #     喂一张已知有色差的一帧必须放行。少了这一条，显示器休眠那一轮的五条像素 FAIL
    #     会被读成"产品同时坏了五件事"（真踩过）；反过来一个恒绿的版本会把真瞎了的抓屏
    #     通路读成一切正常，留档那几张全黑 PNG 就冒充成验收证据。
    black = QImage(320, 200, QImage.Format_ARGB32)
    black.fill(QColor(0, 0, 0))
    lit = QImage(320, 200, QImage.Format_ARGB32)
    lit.fill(QColor(12, 16, 24))
    pq = QPainter(lit)
    pq.fillRect(30, 20, 220, 150, QColor(220, 60, 90))
    pq.end()
    hb0, d_b0 = capture_health(black)
    hb1, d_b1 = capture_health(lit)
    falsified.append(("整屏死黑必须判环境不可用，有色差的一帧必须放行",
                      (not hb0) and hb1, f"死黑判={hb0} 有色判={hb1}"))

    # U37b 旁证那行字的解码器必须有已知答案：本轮真机判废时它写的是"供电标志=0x5b
    #     （0x8=显示器断电/休眠）"，而 SYSTEM_POWER_STATUS 第三个字节是**电量百分比**
    #     （0x5b=91%），BatteryFlag 的 0x8 是"交流在线"——那句注解是把一个字节读错了位置
    #     再编了一层机制。判据本身不吃这行字（屏在不在只看整屏采样），但这行字负责向人解释
    #     "这轮为什么不作数"，错了就会把后来的人引到"显示器供电 API 能报休眠"这条死路上。
    pw_09 = power_words(0x09)
    pw_0 = power_words(0)
    pw_255 = power_words(0x40 | 0x80)
    _real_flag = _SYSPOWER().battery_flag      # 结构体本身也要能填：字段名写错就恒 0
    falsified.append(("电源旁证的逐位解码有已知答案：0x09=电量高＋交流在线、0=无位、"
                      "0xc0=无电池＋未知，且结构体第一个字节才是 AC 线状态（错位就点名）",
                      pw_09 == "电量高／交流在线" and pw_0 == "无位"
                      and pw_255 == "无电池／电源状态未知" and _real_flag == 0
                      and len(_SYSPOWER._fields_) == 6
                      and _SYSPOWER._fields_[1][0] == "battery_flag",
                      f"0x09={pw_09} 0x00={pw_0} 0xc0={pw_255}"))

    # U38 点名挡着者的那段文字：自己进程的窗口要标成自家、句柄取不到时不得把整轮带崩。
    #     判据崩掉会被上面那条误读成"抓到了 bug"，所以坏句柄也算一个坏样本。
    nm_self = _hwnd_debug(int(win2.winId()))
    nm_bad = _hwnd_debug(0)
    falsified.append(("句柄点名必须写出类名与自家/外来，且坏句柄不得抛（只能退回原样）",
                      ("自家" in nm_self and "pid" in nm_self and len(nm_bad) > 2),
                      f"自家={nm_self} 坏句柄={nm_bad}"))

    # U39 OSD 的墨迹判据必须两头都夹住：泡在、字也在 → 判有字；泡在、字被抹掉 → 判无字。
    #     上一轮 FAIL 的就是"整块 0"那种空泡（量到正在淡出的窗口），当时无法区分
    #     "气泡没画字"和"根本没气泡"，所以这条把两种已知答案各摆一次。
    win2.osd.show_text("倍速 1.1x", 8000)
    pump(0.35)
    i_good, d_good = has_ink(grab_qt(win2.osd))
    keep_text = win2.osd._text
    win2.osd._text = ""
    win2.osd.update()
    pump(0.35)
    i_blank, d_blank = has_ink(grab_qt(win2.osd))
    win2.osd._text = keep_text
    win2.osd.update()
    pump(0.2)
    falsified.append(("OSD 墨迹判据：空泡必须判无字，有字的气泡必须判有字",
                      bool(i_good) and (not i_blank), f"有字={i_good}/{d_good} 空泡={i_blank}/{d_blank}"))

    # U40–U44 按键段那几个纯谓词（main() 里按键那 8 条判据的真身）。这一段在真窗口上
    #     **没法证伪**：命中 0 次、命中 2 次、孤儿 QShortcut 这三种坏都得先把生产代码写坏
    #     才造得出来，而"跑一遍全 PASS"对恒真的判据同样成立。所以判读抽成纯函数，坏样本
    #     直接喂：每条都同时夹两头（坏的必须被否 + 已知好的不得误报）。
    h_bad_zero = hit_once([], "mute")                                  # 死写法／没装上
    h_bad_two = hit_once(["mute", "mute"], "mute")                     # 旧的没摘干净
    h_bad_other = hit_once(["next"], "mute")                           # 撞键，响的是别家
    h_bad_orphan = hit_once(["mute"], "mute", installed=60, rows=59)   # 多一条孤儿挂在窗口上
    h_bad_default = hit_once(["mute"], "mute", installed=59, rows=59, default_rows=60)
    h_good = hit_once(["mute"], "mute", installed=59, rows=59, default_rows=59)
    f_case("hit_once：0 次／2 次／命中别家／条数不一致必须全被否，已知好样本不得误报",
           all(not x[0] for x in (h_bad_zero, h_bad_two, h_bad_other,
                                  h_bad_orphan, h_bad_default)) and h_good[0],
           f"坏样本被否={[not x[0] for x in (h_bad_zero, h_bad_two, h_bad_other, h_bad_orphan, h_bad_default)]}"
           f" 好样本={h_good[1]}")

    c_ok = counts_agree(("装", 59), ("表", 59), ("出厂", 59))
    c_bad = counts_agree(("装", 60), ("表", 59), ("出厂", 59))
    c_one = counts_agree(("装", 59),)
    n_bad = no_hit(["mute"], "mute")
    n_ok = no_hit(["next"], "mute")
    f_case("counts_agree 与 no_hit：条数不一致／这把键仍响必须被否，一致与不响不得误报",
           c_ok[0] and (not c_bad[0]) and c_one[0] and (not n_bad[0]) and n_ok[0],
           f"一致={c_ok[1]} 不一致={c_bad[1]} 单值={c_one[1]} 仍响={n_bad[1]} 不响={n_ok[1]}")

    rows3 = [("Page Down", "mute", "静音切换"), ("Space", "toggle_play", "播放 / 暂停")]
    t_ok = table_has(rows3, keep=("Page Down", "mute"), drop=("PgDown", "next"))
    t_missing = table_has(rows3, keep=("K", "toggle_play"))
    t_kept = table_has([("E", "screenshot", "截图")], drop=("E", "screenshot"))
    t_pair_shape = table_has([("E", "screenshot")], drop=("E", "screenshot"))   # 二元组也得认
    f_case("table_has：该装的没装、该折的没折必须各自被否，二元组形状不得漏判",
           t_ok[0] and (not t_missing[0]) and (not t_kept[0]) and (not t_pair_shape[0]),
           f"好样本={t_ok[1]} 没装={t_missing[1]} 没折={t_kept[1]} 二元组={t_pair_shape[1]}")

    rows_all = [("Z", "mute", "静音切换"), ("E", "screenshot", "截图")]
    r_ok = rows_all_hit(rows_all, lambda s: {"Z": ["mute"], "E": ["screenshot"]}[s])
    r_dead = rows_all_hit(rows_all, lambda s: [] if s == "Z" else ["screenshot"])
    r_twice = rows_all_hit(rows_all, lambda s: ["mute", "mute"] if s == "Z" else ["screenshot"])
    r_wrong = rows_all_hit(rows_all, lambda s: ["next"] if s == "Z" else ["screenshot"])
    f_case("rows_all_hit：某行按不响／响两次／响成别家必须各自被否，全对的那份不得误报",
           r_ok[0] and (not r_dead[0]) and (not r_twice[0]) and (not r_wrong[0]),
           f"好样本={r_ok[1]} 不响={r_dead[1]} 两次={r_twice[1]} 别家={r_wrong[1]}")

    k_ok = calibration_verdict([], [], [_ALIVE_TAG], "QTest 拒递")
    k_not_freed = calibration_verdict(["next"], [], [_ALIVE_TAG], "QTest 拒递")
    k_dead_rang = calibration_verdict([], [_DEAD_TAG], [_ALIVE_TAG], "QTest 拒递")
    k_alive_silent = calibration_verdict([], [], [], "QTest 拒递")
    k_no_refuse = calibration_verdict([], [], [_ALIVE_TAG], "")
    falsified.append(("calibration_verdict：没腾空／死写法按响／活写法不响／没拒递，四种必须各自被否",
                      k_ok[0] and (not k_not_freed[0]) and (not k_dead_rang[0])
                      and (not k_alive_silent[0]) and (not k_no_refuse[0]),
                      f"好样本={k_ok[1]} 没腾空={k_not_freed[1]} 按响={k_dead_rang[1]} "
                      f"不响={k_alive_silent[1]} 没拒={k_no_refuse[1]}"))
    falsified.append(("combine 必须是否决权：任何一半红整体就红，详情里留着红的那半句",
                      (not combine((True, "甲"), (False, "乙"))[0])
                      and "乙" in combine((True, "甲"), (False, "乙"))[1]
                      and combine((True, "甲"), (True, "乙"))[0],
                      combine((True, "甲"), (False, "乙"))[1]))

    # U45 画面页的表↔控件判据必须能否决五种坏：少一根滑轨、多一个孤儿控件、
    #     滑轨绑错表项（键集合照样对得上——这是 U24 那一类的翻版）、开关名字抄错
    #     （"界面上写着左右镜像、动的是上下镜像"）、名字在但屏幕上没有标题（开关改成
    #     "标题贴左、开关贴右"之后，标题与名字是两份东西，只核一份会漏掉另一份）。
    #     原样与复原各放行一次，否则这条可能只是恒红。
    gp = win2.transform_panel
    cv0, _ = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    row_s = gp._rows["sharpen"]
    gp._rows.pop("sharpen")
    cv1, d_cv1 = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    gp._rows["sharpen"] = row_s
    gp._choices["ghost"] = gp._choices["rotate"]
    cv2, d_cv2 = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    gp._choices.pop("ghost")
    saved_spec = row_s.spec
    row_s.spec = transform.spec_for_cfg("denoise")
    cv3, d_cv3 = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    row_s.spec = saved_spec
    fl_m = gp._flags["mirror_h"]
    keep_name = fl_m.accessibleName()
    fl_m.setAccessibleName("上下镜像")
    cv4, d_cv4 = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    fl_m.setAccessibleName(keep_name)
    keep_lab = fl_m._row_label.text()
    fl_m._row_label.setText("")          # 名字还在、屏幕上没有：整行只剩一颗孤零零的开关
    cv6, d_cv6 = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    fl_m._row_label.setText(keep_lab)
    cv5, _ = controls_cover_table(gp._rows, gp._choices, gp._flags, transform.ALL_KEYS)
    falsified.append(("表↔控件判据必须能否决少滑轨／孤儿控件／绑错表项／名字抄错／标题丢失，"
                      "且原样与复原各放行",
                      cv0 and (not cv1) and (not cv2) and (not cv3) and (not cv4)
                      and (not cv6) and cv5,
                      f"原样={cv0} 少滑轨={cv1}({d_cv1}) 孤儿={cv2}({d_cv2}) "
                      f"绑错={cv3}({d_cv3}) 名字={cv4}({d_cv4}) 丢标题={cv6}({d_cv6}) 复原={cv5}"))

    # U46 状态行的措辞必须按事实分形。这一页最难查的坏是"内核把滤镜扔了、界面还说已开启"：
    #     链上列着名字、像素没动，只有那一次下发的日志知道（实测见 tests/out/probe-transform*.log）。
    #     所以判据两头夹：忽略 live（没起内核也当已生效）与忽略 ok（被扔了也当已生效）两种写法
    #     都必须使文案撞车，而真代码的四种形状必须两两不同、各自带关键短语。
    class _StubP:
        def __init__(self, fails):
            self._f = list(fails)

        def filter_failures(self):
            return list(self._f)

    class _StubPanel:
        def __init__(self, fails):
            self.p = _StubP(fails)

    def status(fails):
        return lambda spec, shown, ok, live: GP.TransformPanel._status(
            _StubPanel(fails), spec, shown, ok, live)

    s_live = status([])(transform.SHARPEN.spec, 1.0, True, True)
    s_off = status([])(transform.SHARPEN.spec, 1.0, True, False)
    s_bad = status(["Disabling filter unsharp."])(transform.SHARPEN.spec, 1.0, False, True)
    s_clamp = status([])(transform.SHARPEN.spec, 2.0, False, True)
    falsified.append(("状态文案分得出已生效／没起内核／滤镜被扔／只接受表里的档：四种两两不同且各带关键短语",
                      len({s_live, s_off, s_bad, s_clamp}) == 4
                      and "已记入设置" in s_off and "内核没跑起来" in s_bad
                      and "内核只接受" in s_clamp and "已记入" not in s_live,
                      f"生效={s_live!r} 离线={s_off!r} 被扔={s_bad!r} 夹档={s_clamp!r}"))

    # U47 链状态行的三档必须全部由**缓存**驱动（不是由测试塞进去的参数驱动）：
    #     ①名字有、坏行也有 → 必须红字点名（"链上列着名字"永远不许冒充"在跑"）；
    #     ②只有名字 → 绿字念名单；③两个都空 → 「（空）」。
    #     第一版在这里只塞了 fails 参数、名字表还是空的，于是②那一档量的是"没名字"，
    #     判据红绿都成立却什么都没测到——所以这次把 _vf_names 真摆出两项来量。
    gp.p._vf_names = ["format", "hflip"]
    gp.p._vf_failures = ["Disabling filter hflip."]
    gp._paint_chain()
    c_bad_line, c_bad_style = gp.lbl_chain.text(), gp.lbl_chain.styleSheet()
    gp.p._vf_failures = []
    gp._paint_chain()
    c_good_line, c_good_style = gp.lbl_chain.text(), gp.lbl_chain.styleSheet()
    gp.p._vf_names = []
    gp._paint_chain()
    c_empty_line, c_empty_style = gp.lbl_chain.text(), gp.lbl_chain.styleSheet()
    falsified.append(("链状态行三档全由缓存驱动：有坏行必须红字点名（哪怕链上列着名字）、"
                      "只有名字必须绿字念名单、两个都空必须写（空）",
                      "内核没跑起来" in c_bad_line and "hflip" in c_bad_line
                      and GP.WARN_TINT in c_bad_style
                      and c_good_line == "滤镜链：format、hflip" and GP.OK_TINT in c_good_style
                      and c_empty_line == "滤镜链：（空）" and GP.DIM_TINT in c_empty_style,
                      f"坏行={c_bad_line!r}/{c_bad_style} 名单={c_good_line!r}/{c_good_style} "
                      f"空={c_empty_line!r}/{c_empty_style}"))
    gp.repaint_from_state()

    # U48 按键段"环境 / 产品"分流（上一轮那八条红的真修法）。三家必须先分得开，
    #     否则这条修法只是把红换个写法：ENV 判宽了 ⇒ 以后任何死键都能说成"今天系统没给键盘"；
    #     ENV 判窄了 ⇒ 用户开着浏览器就永远跑不绿。四条已知答案钉 channel_split，
    #     其中"对照键响的是别家"那一档钉的是**不许**记 ENV（事件进得来＝通道通，只是键位表
    #     被改过），这一档写反了就是那个最坏的形状。
    _cs_pass, _d_cs_pass = channel_split([], ["mute"], 3)
    _cs_env, _d_cs_env = channel_split([("PgDn", "next", [])], [], 3)
    _cs_fail, _d_cs_fail = channel_split([("PgDn", "next", [])], ["mute"], 3)
    _cs_other, _d_cs_other = channel_split([("PgDn", "next", [])], ["vol_down"], 3)
    falsified.append(("通道分流四家分得开：没坏行=PASS、对照键整批不响=ENV、对照键响着还哑=FAIL、"
                      "对照键响的是**别家**也算 FAIL（事件进得来就不是环境问题）",
                      _cs_pass == "PASS" and _cs_env == "ENV" and _cs_fail == "FAIL"
                      and _cs_other == "FAIL" and "不作数" in _d_cs_env
                      and "真坏" in _d_cs_fail and "键位配置被改过" in _d_cs_other,
                      f"无坏行={_cs_pass} 对照哑={_cs_env} 对照响={_cs_fail} 响别家={_cs_other}"))

    # U48b key_misses / rows_all_hit 共用一份取证：四种形状（恰好一次 / 0 次 / 2 次 / 命中别家）
    #      必须只有第一种算合规，而且两个函数对同一批输入给的答案必须一致——
    #      main() 用 key_misses 挑重按对象、判读却走 rows_all_hit，两套口径就会漏行。
    _rows = [("Z", "mute", ""), ("E", "screenshot", ""), ("PgDn", "next", ""),
             ("K", "vol_up", "")]
    _hits = {"Z": ["mute"], "E": [], "PgDn": ["next", "next"], "K": ["mute"]}
    _m = [x[0] for x in key_misses(_rows, lambda s: _hits.get(s, []))]
    _a_ok, _a_d = rows_all_hit(_rows, lambda s: _hits.get(s, []))
    falsified.append(("取证只放过「恰好命中自己一次」：0 次 / 2 次 / 命中别家都要点名，"
                      "且 key_misses 与 rows_all_hit 必须同一套口径",
                      _m == ["E", "PgDn", "K"] and _a_ok is False
                      and all(s in _a_d for s in _m) and "'Z'" not in _a_d,
                      f"点名={_m} 判读={_a_ok}/{_a_d[:80]}"))

    # U48c 判废台账的三条规矩：①env_check **不许**写 results（写了就是把没验的记成一条，
    #     记 True 是假绿、记 False 是假红）；②必须点名到 env_notes；③收尾谁优先看
    #     env_exit_code——「量具没抓到坏样本」（1）压过「环境没给证据」（3），反过来的话
    #     一个恒真的检测器只要摊上一次遮挡就能永远装作"只是环境问题"。
    _keep_res, _keep_env = list(results), list(env_notes)
    try:
        results.clear()
        env_notes.clear()
        _n_res_before = len(results)
        env_check("探针：这条本轮不作数", "外来窗口抢前台")
        _ledger = (len(results) == _n_res_before and len(env_notes) == 1
                   and "探针：这条本轮不作数" in env_notes[0]
                   and "抢前台" in env_notes[0])
        _rc = [env_exit_code(["x"], [], []), env_exit_code(["x"], [], [("j", 1)]),
               env_exit_code(["x"], ["m"], []), env_exit_code([], [], []),
               env_exit_code([], ["m"], []), env_exit_code(["x"], ["m"], [])]
    finally:
        results[:] = _keep_res
        env_notes[:] = _keep_env
    falsified.append(("判废台账只写 env_notes 不写 results，且收尾分流把「量具不可信」排在"
                      "「环境没给证据」前面（1 压 3，两者皆无回 None）",
                      _ledger and _rc == [3, 1, 1, None, 1, 1],
                      f"台账={_ledger} 分流(仅ENV/ENV+junk/ENV+missed/全空/仅missed/ENV+missed)"
                      f"={_rc}"))

    # U48d 红先问通道这段闭包在 --falsify 里**跑不到**（它在 main() 内部），所以钉两层：
    #     ①分流谓词 red_route 的六种输入各回哪一家；②闭包真的调用这两个名字（控制流形状），
    #     光有谓词不接线＝上一轮那种"判据写好了没人用"。
    _rr = [red_route(True, False), red_route(False, True), red_route(False, False),
           red_route(False, False, True), red_route(False, False, False),
           red_route(True, True)]
    _src_main = inspect.getsource(main)
    _k_body = _src_main[_src_main.index("def kcheck"):
                        _src_main.index("focus_ok, d_focus = keyboard_focus_ok(win)")]
    falsified.append(("红先问通道的控制流：判废后一律 ENV、绿的不许顺手重按对照键、红的必须先问；"
                      "且 kcheck 闭包真的接上 red_route 与 keyboard_focus_ok",
                      _rr == ["ENV", "CHECK", "ASK", "CHECK", "ENV", "ENV"]
                      and 'red_route(chan["lost"], ok)' in _k_body
                      and 'if route == "ASK":' in _k_body
                      and "keyboard_focus_ok(win)" in _k_body
                      and "check(name, ok, detail)" in _k_body
                      and _k_body.count("env_check(") == 2,
                      f"分流(判废红/绿/红未问/红+对照响/红+对照哑/判废绿)={_rr} "
                      f"闭包 {len(_k_body)} 字"))

    # U48e 收尾的优先级必须钉住：**真红压过判废**。判废那段若写成"先 return 3"，
    #     本轮哪怕有一条产品失败也会被洗成"今天环境不给"，下一轮没人再去查那条红——
    #     这正是这一类台账最坏的失效法（把失败读成没跑）。四档已知答案＋两条形状：
    #     ①report 真的调用这个谓词（不是摆个纯函数没人用）；②env_notes 那一段之后
    #     不许再出现裸的 return 3（短路就是分流失效的形状）。
    _fx = [final_exit_code([], []), final_exit_code([], ["判废"]),
           final_exit_code(["真红"], []), final_exit_code(["真红"], ["判废"])]
    _rep = inspect.getsource(report)
    _tail = _rep[_rep.index("if env_notes:"):]
    falsified.append(("收尾分流：真红压过判废（都无⇒0／只有判废⇒3／有红⇒1／红+判废⇒1），"
                      "且 report 真的走这条分流、判废段之后不许再短路回 3",
                      _fx == [0, 3, 1, 1]
                      and "final_exit_code(fails, env_notes)" in _tail
                      and "return 3" not in _tail,
                      f"分流(都无/仅判废/仅红/红+判废)={_fx} 判废段 {len(_tail)} 字"))

    # U59 证伪台账的**自打收口**：这趟登记了几行、各家几条，由回执自己印出来（以前只能外部
    #     grep，出处＝grep；现在出处＝回执自报）。三档都得钉，缺一档这句自打就会变成新的假话：
    #     ①分桶与计数的已知答案——含四种"看起来像布尔其实不是"的值（元组／1／""／0）。
    #       旧写法用 `o not in (True, False, None)`，而 0 == False、1 == True，所以"忘了取 [0]
    #       却串回整数 1"会被当成抓到了；换成 `is` 之后它归 JUNK 且 rc 掉 1。
    #     ②四家条数之和必须等于行数（不重不漏；只有一处数法才谈得上"不重"）。
    #     ③接线形状：拿五份源码样本喂 tally_wiring_ok——一份好的必须绿，四份坏的（就地重数／
    #       标签没走共用分桶／自打排在分流之后／干脆没接）必须各自红并点名是哪一条；
    #       最后要求**真的 report 源码**过这一关。第三档弱于真注入（它证的是形状没被人改回去，
    #       不是某个坏行为现场发生过），所以②同时把"总数＝四家之和"当场算给这行看。
    _tb = [falsify_bucket(v) for v in (True, False, None, ("元组", "另一条"), 1, "", 0)]
    _ta = [("抓到", True, "d"), ("漏网", False, "d"), ("靠屏幕", None, "d"),
           ("忘了取[0]", ("元组", "另一条"), "d"), ("串回来是整数", 1, "d"),
           ("空串", "", "d"), ("零", 0, "d")]
    _tl = falsify_tally(_ta)
    _t_shape = (_tl["total"] == len(_ta)
                and _tl["caught"] + _tl["missed"] + _tl["env"] + _tl["junk"] == _tl["total"])
    _t_named = (_tl["names"]["MISSED"] == ["漏网"] and _tl["names"]["ENV "] == ["靠屏幕"]
                and [n for n, _ty in _tl["junk_types"]] ==
                ["忘了取[0]", "串回来是整数", "空串", "零"])
    _t_num = (_tl["caught"], _tl["missed"], _tl["env"], _tl["junk"]) == (1, 1, 1, 4)
    _tw_good = tally_wiring_ok(_TALLY_GOOD)
    _ts_inline = _TALLY_GOOD.replace(
        '    missed, env, junk = t["names"]["MISSED"], '
        't["names"]["ENV "], t["junk_types"]',
        "    missed = [n for n, ok, _ in falsified if ok is False]")
    _ts_tag = _TALLY_GOOD.replace("falsify_bucket(ok)",
                                  '"CAUGHT" if ok is True else "MISSED"')
    _ts_late = _TALLY_GOOD.replace(
        '    print("---- 证伪台账自打 ----")\n', ""
    ).replace("    rc = env_exit_code(env, missed, junk)",
              "    rc = env_exit_code(env, missed, junk)\n"
              '    print("---- 证伪台账自打 ----")')
    _ts_none = "def report():\n    return 0\n"
    # 注入不许是空操作：坏样本必须先真的和好的那份不一样，否则"它红了"是假的（台账里吃过
    #     str.replace 次数写错的亏——一处都没换，却被人记成"抓到了"）。
    _tw_mut = (_ts_inline != _TALLY_GOOD and _ts_tag != _TALLY_GOOD and _ts_late != _TALLY_GOOD)
    _tw_inline, _tw_tag, _tw_late = (tally_wiring_ok(_ts_inline), tally_wiring_ok(_ts_tag),
                                     tally_wiring_ok(_ts_late))
    _tw_none = tally_wiring_ok(_ts_none)
    _tw_live = tally_wiring_ok(inspect.getsource(report))
    falsified.append(("证伪台账自打收口：分桶与计数的已知答案（元组/1/\"\"/0 都归非布尔，"
                      "四家之和＝行数），且 report 的接线形状必须过五份源码样本"
                      "（好的绿；就地重数／标签另算／排在分流之后／干脆没接，四份各自红并点名、"
                      "且不许是空操作）",
                      _tb == ["CAUGHT", "MISSED", "ENV ", "JUNK", "JUNK", "JUNK", "JUNK"]
                      and _t_num and _t_shape and _t_named and _tw_mut
                      and _tw_good[0] and not _tw_inline[0] and not _tw_tag[0]
                      and not _tw_late[0] and not _tw_none[0]
                      and "①" in _tw_inline[1]
                      and "②" in _tw_tag[1] and "③" in _tw_late[1] and "④" in _tw_none[1]
                      and _tw_live[0],
                      f"分桶={_tb} 计数(总/抓/漏/环/非布尔)="
                      f"({_tl['total']},{_tl['caught']},{_tl['missed']},{_tl['env']},"
                      f"{_tl['junk']}) 形状不重不漏={_t_shape} 点名={_t_named} 注入非空操作={_tw_mut} "
                      f"接线[好/重数/另算标签/排后面/没接/真report]="
                      f"{[(_tw_good[0], _tw_inline[0], _tw_tag[0], _tw_late[0], _tw_none[0], _tw_live[0])]} "
                      f"坏样本点名={_tw_inline[1]}｜{_tw_tag[1]}｜{_tw_late[1]}｜{_tw_none[1]}"))

    # U49 页签几何判据必须能否决四种坏，而且**各自归到各自那条**（合起来红一团＝分不清谁抓到的）：
    #     ①把每格钉成「文字+2」（＝旧写法压破样式，但这一档还没切字）要报"样式要的宽"；
    #     ②钉成「文字−6」（比文字还窄）要报"切字"；
    #     ③钉成「文字+40」：每格自己够宽、`_layout_tabs` 报的 need 也没变（它算的是钉之前的宽），
    #       但实际摆出来 416px 挤进 300px → **只有"重叠"这条**该红。这条是专门给④配的独立证据，
    #       否则④永远和①同时红，就说不清它到底量到了什么；
    #     ④摘掉"排不开就把侧栏加宽"那一步（no_grow）：200 那一档要同时报"排不开"和"重叠"，
    #       证明那一步是承重件；
    #     ⑤原样与复原都必须放行——否则这条判据是恒红，第 9.x 节那条 PASS 不作数。
    win2._set_sidebar(True, page_index(win2, "播放列表"))
    pump(0.2)
    u49_base, d49_base = tabs_fit(win2, (300, 240))
    u49_hint, d49_hint = tabs_fit(win2, (300,), squeeze=2)
    u49_cut, d49_cut = tabs_fit(win2, (300,), squeeze=-6)
    u49_over, d49_over = tabs_fit(win2, (300,), squeeze=40)
    u49_tight, d49_tight = tabs_fit(win2, (200,), no_grow=True)
    u49_back, d49_back = tabs_fit(win2, (300, 240))
    falsified.append(("页签几何判据：压破样式／真的切字／只有重叠／排不开（摘掉加宽那步）四种坏各自点名，原样与复原放行",
                      u49_base and (not u49_hint) and "样式要的" in d49_hint and "切字" not in d49_hint
                      and (not u49_cut) and "切字" in d49_cut
                      and (not u49_over) and "重叠" in d49_over and "排不开" not in d49_over
                      and "切字" not in d49_over and "样式要的" not in d49_over
                      and (not u49_tight) and "排不开" in d49_tight and "重叠" in d49_tight
                      and u49_back,
                      f"原样={u49_base} 压破样式={u49_hint}({d49_hint[:34]}) 切字={u49_cut}"
                      f"({d49_cut[:34]}) 只有重叠={u49_over}({d49_over[:34]}) "
                      f"摘掉加宽={u49_tight}({d49_tight[:34]}) 复原={u49_back}"))

    # U50 侧栏宽度的两道闸要各自能否决：
    #     · 量程收口（`_clamped_sidebar_width`）：100/99999/非数字各归其位，这是**纯函数**那一半，
    #       不需要真窗口，当场对账；
    #     · 加宽上限（`_fit_sidebar` 里那个 min(…, 520)）：越界配置在闸内五条件全立，
    #       把闸摘掉（照配置原样 setFixedWidth）时⑤必须红——红在"侧栏比窗口还宽"这个量上。
    #       这里**不拿视频区宽度当证据**：本机实测 `win.video.width()` 在摘闸前后都是 160
    #       （没载入片子时那块本来就不跟布局走），一个不随状态变的量没有鉴别力。
    u50_vals = tuple(win2._clamped_sidebar_width(v) for v in (100, 99999, None))
    u50_in, d50_in = tabs_fit(win2, (99999, 100))
    _orig_w = int(win2.sidebar_width)
    win2.sidebar_width = 99999
    win2.sidebar.setFixedWidth(99999)          # 摘掉两道闸：配置说多少就钉多少
    pump(0.12)
    u50_out = win2.sidebar.width() * 2 > win2.width()
    _side_out = win2.sidebar.width()
    win2.sidebar_width = _orig_w
    u50_back, d50_back = tabs_fit(win2, (300,))
    falsified.append(("侧栏宽度两道闸：摘闸后侧栏真的比窗口还宽（⑤红），装闸后越界档五条件全立且能复原，越界/非数字各归其位",
                      u50_vals == (240, 520, 300) and u50_in and u50_out and u50_back,
                      f"收口值={u50_vals} 闸内={u50_in}({d50_in[:26]}) 摘闸后侧栏={_side_out}"
                      f"/窗口={win2.width()} 超限={u50_out} 复原={u50_back}({d50_back[:20]})"))

    # U51 页签条"换行"这一级（`_layout_tabs(..., wrap=True)`）的独立证据。这一条存在的理由
    # 是它和 U49④ 的关系：如果哪天有人把 wrap 设成默认开（"排不开自己换行不就永远不重叠了"），
    # U49 那条"摘掉加宽那步要报排不开"就永远绿——重叠被量具自己消化掉了，产品那一步加宽
    # 还在不在没人知道。所以两头夹：
    #     · 默认（wrap=False）在 200 这一档**仍然要报 need>200、而且只有一排**：证明换行是开关、
    #       不是默认行为，"先把容器加宽"仍然是第一责任；
    #     · 开关打开后必须真的排开：≥2 排、同排不重叠、每格都在页签条内、返回的 need ≤ 200
    #       （"换行之后这一排要多宽"是个排得开的数，不是原来那个 700）；
    #     · 摘掉落位（把 rows 强制压回单排）时同排重叠必须立刻出现——否则上面那三条是白捡的绿。
    # 第三注在上一版里是**假绿**（MISSED）：`_layout_tabs` 只钉按钮宽、不动容器宽，容器还是
    # 300，而这一排单排要 229——229 塞得进 300，压回单排自然谁也不叠。所以先把容器真钉到 200
    # （`sidebar.setFixedWidth`：这正是"加宽已到上限、容器就这么宽"那一档的真实形状），
    # 落位摘掉之后那一排的盒才会去压 x，重叠才会当场出现。收尾由 `_fit_sidebar` 交回产品。
    #
    # 排数与"在条内"为什么改成 `_tab_rows_of`/`_in_strip`：页签条从共享列的网格换成
    # "每排一个独立盒"之后（4.0 档字号实测重叠 23×72px 的根因，见 main_window.__init__），
    # 盒内按钮的 y 恒为 0，`{b.y()}` 这种按 y 数排数的写法只会数出 1 排——上一轮 U51 报
    # "排数=1"而 `probe_tab_rows.py` 量到 2~3 排，差的就是这一处，**是量具瞎不是产品坏**。
    # 现在排数取自按钮的父件（＝Qt 真正把它摆进的那个盒），另取 `len(_tab_rows)` 对账：
    # 按钮说"排开了"、布局说"没摆那么多盒"，也算不一致，得红。
    win2._layout_tabs(200)                          # 先按单排钉一次，容器还没窄，形状是"一排铺满"
    pump(0.15)
    win2.sidebar.setFixedWidth(200)                 # 容器真的窄到排不开（200 < 单排要 229）
    pump(0.15)
    _need_flat = win2._layout_tabs(200)
    pump(0.15)
    _flat_rows = _tab_rows_of(win2)
    _need_wrap = win2._layout_tabs(200, wrap=True)
    pump(0.18)
    _wrap_rows = _tab_rows_of(win2)

    def _same_row_over(win):
        """只在**同一排**内比 x：排的身份由 `_tab_rows_of` 给（父件＝那一排的盒）。"""
        out = []
        for group in _tab_rows_of(win):
            ordered = sorted(group, key=lambda b: b.x())
            out += [f"{a.text()}×{b.text()}" for a, b in zip(ordered, ordered[1:])
                    if a.x() + a.width() > b.x() + 1]
        return out

    _ov_wrap = _same_row_over(win2)
    _out_wrap = [b.text() for b in win2.tab_btns
                 if _in_strip(win2, b).right() > win2.tab_strip.width()
                 or _in_strip(win2, b).bottom() > win2.tab_strip.height()]
    # 页签条几何要在**量它的那一刻**记下来：detail 那串是在收尾（已复原到 300 单排）之后才拼的，
    # 当场不记就会印成"页签条=300×37"，把将来某条 MISSED 指向错的容器宽度。
    _host_geo = (win2.tab_strip.width(), win2.tab_strip.height(), win2.sidebar.width())
    _boxes = len(win2._tab_rows)                    # 布局层面的排数：按钮父件说"排开了"，盒要说"真摆了那么多"
    win2._place_tab_rows([list(range(len(win2.tab_btns)))])   # 注入：落位摘掉，五格全压回第 0 行
    pump(0.18)
    _flat_after = len(_tab_rows_of(win2))
    _ov_injected = _same_row_over(win2)
    win2.sidebar_width = int(cfg["sidebar_width"])
    win2._fit_sidebar()                                        # 交回产品自己排（宽度够 ⇒ 回到单排）
    pump(0.15)
    u51_back = len(_tab_rows_of(win2)) == 1 and not _same_row_over(win2)
    falsified.append(("页签换行是开关不是默认：200 档默认仍只按单排报需求；打开后≥2 排且同排不重叠/每格在条内/need≤200；"
                      "摘掉落位时同排重叠立刻出现，交回产品后复原",
                      _need_flat > 200 and len(_flat_rows) == 1
                      and len(_wrap_rows) >= 2 and _boxes == len(_wrap_rows)
                      and _need_wrap <= 200 and not _ov_wrap and not _out_wrap
                      and _flat_after == 1 and bool(_ov_injected) and u51_back,
                      f"单排need={_need_flat} 换行need={_need_wrap} 排数={len(_wrap_rows)}(盒{_boxes}) "
                      f"每排格数={[len(r) for r in _wrap_rows]} 页签条={_host_geo[0]}×{_host_geo[1]}(侧栏{_host_geo[2]}) "
                      f"重叠={_ov_wrap[:2]} 掉出={_out_wrap[:2]} "
                      f"摘落后排数={_flat_after} 摘落后重叠={_ov_injected[:2]} 复原={u51_back}"))

    # ---- U52～U56：五处「形状靠程序算」的闸，各自要能翻红 ----
    # 共同纪律：每条都是「原样 → 摘掉那一处必须红 → 交回产品必须复原」三段，摘的方式是把产品
    # 那一处换成"没有这一步会怎样"的真实旧写法（问滚动区自己／把 44 钉死／字数档调回 0／
    # 把加宽那步换成恒等），不是把判据调成恒红。量具放本地：capture_sweep 自己 import 本模块。
    import capture_sweep as SW

    # U52 `form_rows.content_width` 是 `_pages_need()` 的唯一算法出处。三条注：
    #     ①时序：隐藏页与显示页必须给同一个数。2026-10-11 之前给的是 378 与 402 两个数——
    #       构造期 `sc.width()` 不是 0 而是 Qt 默认的 100（视口 98），"问实际差值"那一条
    #       量到 2px、兜底轮不到，侧栏因此钉窄一整根滚动条，行尾数值胶囊「1.00x」被切；
    #     ②下限：需求必须 ≥ 内容块最小宽 + 样式给的滚动条厚度（把 2px 那一路调回来就违反它）；
    #       ②不依赖"这一页在本轮里还没被排过版"，所以①被环境削弱时它仍然有牙。
    #     ③承重：换成"问滚动区自己"（`minimumSizeHint`）那一版，侧栏立刻窄于真实需求——
    #       证明上面那两条不是恒真。③分两档量：
    #       (a) 标准字号 + 配置钉到量程下沿 240：只断**数值**。摘闸后报出来的需求（195）
    #           连内容块自己的最小宽都不到，侧栏宽 240 < 真实需求 270。
    #           这一档两把尺在本机证伪进程里是 0 条（`tests/probe_u52_teeth.py` 两套装配都量过：
    #           内容块 238/缺口 32px ⇒ 0 条；内容块 274/缺口 66px ⇒ 越界尺 11 条），
    #           原因是长行里的标签带省略号，缺口先被"字变短"吸收，实测约 40px 以内不点名。
    #           所以这一档不许写"必须红"——那是把量具的盲区当成产品的清白。
    #       (b) 把调节页字号 ×2（R5 那一族本来就是这件事）再钉 240：缺口越过省略号能吸收的
    #           那一段，摘闸必须被裁而未告/越界尺点名；同一档装着闸必须清场（对照组，
    #           排除"钉窄就一定红"），且侧栏没顶到量程上限（顶到了就不是这一处算法在决定宽度）。
    _tw_page = page_of(win2, "调节")      # 按标签取节点，不抄下标（见 `page_index`）
    _cw_hidden = form_rows.content_width(_tw_page)
    _body_tw, _sc_tw = None, None
    for _sc in _tw_page.findChildren(QScrollArea):
        if _sc.widget() is not None:
            _body_tw, _sc_tw = _sc.widget(), _sc
            break
    # 兜底那一把取的是滚动条自己的 sizeHint（跟着 QSS 走），不是样式的 PM_ScrollBarExtent
    # （出厂形状实测 10 vs 26：拿 26 就是凭空给侧栏多要 16px）。
    _bar_floor = int(_sc_tw.verticalScrollBar().sizeHint().width()) if _sc_tw is not None else 0
    _body_tw_min = int(_body_tw.minimumSizeHint().width()) if _body_tw is not None else 0
    _floor_ok = _cw_hidden >= _body_tw_min + _bar_floor
    win2._sidebar_page(page_index(win2, "调节"))
    pump(0.5)
    _cw_shown = form_rows.content_width(_tw_page)
    _cw_real = win2._pages_need()
    # ③第一档：把配置的侧栏宽钉到量程下沿 240。`_fit_sidebar` 是
    #   `min(max(配置宽, max(页签行需求, 页面内容需求)), 520)`，配置留在 300 时"需求 273"
    #   这种环境里 max 取的是配置那一头，闸咬不咬看不出差别（本机出厂形状就吃了一次这个哑巴亏：
    #   需求 306/322 随装配而变）。取 240 之后侧栏宽只能由需求决定。
    #   ⚠ 但"由需求决定"在 2026-10-13 之后只剩一半成立：加「声音均衡」那一格把页签行顶到
    #   288px，比内容需求（r13 真机量到 272）还宽，于是**总宽**这一档由页签行决定，摘掉内容闸
    #   总宽照样 288 ⇒ 原来那条 `_side_naive < _cw_real`（240<270 时在 r8/r11/falsify-r13 三趟
    #   都有牙）第一次真跑就 MISSED。所以这一档改问**内容通道自己**（`sidebar_page_need` 是
    #   `_fit_sidebar` 当场记下的那半个数，产品早就把两个数分开存着）：摘闸必须把这一通道从
    #   272 打到「内容块最小宽＋滚动条」以下。带像素牙的那一半留在 ③(b)（字号 ×2 之后内容需求
    #   327 > 页签行 288，摘闸立刻被裁字/越界尺点名——闸坏在内容那一侧时仍然会红）。
    _sw_back = int(win2.sidebar_width)
    win2.sidebar_width = INT_RANGE["sidebar_width"][0]
    _orig_cw = form_rows.content_width
    _naive_cw = lambda p: int(p.minimumSizeHint().width())     # noqa: E731 注入：问滚动区自己
    _side_real = int(win2._fit_sidebar())
    _pn_real = int(win2.sidebar_page_need)     # 装着闸时 fit 用的那半个数（内容通道）
    _tr_real = int(win2.tab_row_need)          # 页签行那一半，用来点名"总宽是谁决定的"
    pump(0.3)
    _clean_real = SW.clipped_without_hint(_tw_page) + SW.beyond_viewport(_tw_page)
    form_rows.content_width = _naive_cw
    _side_naive = int(win2._fit_sidebar())
    _pn_naive = int(win2.sidebar_page_need)
    pump(0.3)
    _naive_need = win2._pages_need()
    _red_at_floor = SW.clipped_without_hint(_tw_page) + SW.beyond_viewport(_tw_page)
    form_rows.content_width = _orig_cw                        # 交回产品
    _side_back = int(win2._fit_sidebar())
    pump(0.3)
    _clean_back = SW.clipped_without_hint(_tw_page) + SW.beyond_viewport(_tw_page)
    # ③(b)：字号 ×2 造一个"省略号吸收不完"的形状，再在同一档（量程下沿）问两把尺。
    _cap52 = INT_RANGE["sidebar_width"][1]
    _kids52 = (_tw_page.findChildren(QLabel) + _tw_page.findChildren(QComboBox)
               + _tw_page.findChildren(QToolButton))
    _font_back52 = {k: k.font() for k in _kids52}
    for k in _kids52:
        _f52 = QFont(k.font())
        _px52 = _f52.pixelSize() if _f52.pixelSize() > 0 else max(1, int(_f52.pointSize() * 96.0 / 72.0))
        _f52.setPixelSize(_px52 * 2)
        k.setFont(_f52)
    pump(0.8)                                    # 字号变更要等布局跑完才见真值（见 (a) 那 24px 教训）
    win2.sidebar_width = INT_RANGE["sidebar_width"][0]
    form_rows.content_width = _orig_cw
    for _ in range(4):        # 需求会跟着排版一轮一轮地涨，装闸一侧要等它稳住再当对照组
        win2._fit_sidebar()
        pump(0.35)
        if int(win2.sidebar.width()) >= win2._pages_need():
            break
    _cw52 = win2._pages_need()
    _side52_real = int(win2.sidebar.width())
    _clean52 = SW.clipped_without_hint(_tw_page) + SW.beyond_viewport(_tw_page)
    form_rows.content_width = _naive_cw
    _side52_naive = int(win2._fit_sidebar())
    pump(0.5)
    _red52 = SW.clipped_without_hint(_tw_page) + SW.beyond_viewport(_tw_page)
    form_rows.content_width = _orig_cw                        # 交回产品 + 还原字号
    for k, f in _font_back52.items():
        k.setFont(f)
    win2.sidebar_width = _sw_back
    win2._fit_sidebar()
    pump(0.4)
    falsified.append(("侧栏需求量：隐藏页与显示页同数、且不小于内容块+滚动条 sizeHint；把配置宽钉到量程"
                      "下沿后换成问滚动区自己，报出来的**内容通道需求**必须连内容块自身最小宽＋滚动条厚度"
                      "都不到（2026-10-13 起：这一档的总宽由页签行决定，所以摘闸看的是 `sidebar_page_need`，"
                      "不再是侧栏宽——理由见上面③那段注释；"
                      "把该页字号×2 再钉同一档，摘闸必须被裁字/越界尺点名、装着闸必须清场且没顶到量程上限，"
                      "交回产品后宽度与两把尺都复原",
                      _cw_hidden == _cw_shown and _floor_ok
                      and _side_real >= _cw_real and not _clean_real
                      and _pn_real >= _cw_real and _pn_naive < _cw_real
                      and _naive_need < _body_tw_min + _bar_floor
                      and _side_back >= _cw_real and not _clean_back
                      and _side52_real >= _cw52 and _side52_real < _cap52 and not _clean52
                      and _side52_naive < _cw52 and bool(_red52),
                      f"隐藏={_cw_hidden} 显示={_cw_shown} 真实需求={_cw_real} "
                      f"内容+滚动条={_body_tw_min}+{_bar_floor}={_body_tw_min + _bar_floor} 240 档装闸侧栏={_side_real}（红 {_clean_real[:1]}） "
                      f"摘闸侧栏={_side_naive}（摘闸需求读数={_naive_need}，尺红 {len(_red_at_floor)} 条——"
                      f"这一档缺口还在省略号能吸收的范围内，尺看不见，只作数值断言） "
                      f"内容通道 装闸={_pn_real}→摘闸={_pn_naive}／页签行={_tr_real}"
                      f"（{_tr_real} ≥ {_cw_real} 时总宽由页签行决定，摘内容闸看不出宽度差） "
                      f"×2 档：需求={_cw52} 装闸侧栏={_side52_real}（上限{_cap52}，红 {len(_clean52)} 条 {_clean52[:1]}） "
                      f"摘闸侧栏={_side52_naive} 红={len(_red52)}条 首条={_red52[:1]} "
                      f"复原侧栏={_side_back} 复原红={_clean_back[:1]}"))

    # U53 mk_hint：说明行的出路是「换行」，不是「把整列顶宽」也不是「切掉右端」。
    #     摘掉 wordWrap（退回 mk_label 单行写法）之后，同一格子里必须同时出现两件事：
    #     宿主最小宽被这一行顶到单行需求（挤掉视频区），以及截字尺点名（悄悄切右端）。
    _txt53 = "把文件或文件夹拖进来即可播放，也可以 Ctrl+O 从菜单里挑"
    _h53 = QWidget(None)
    _l53 = QVBoxLayout(_h53)
    _l53.setContentsMargins(8, 8, 8, 8)
    _mk53 = form_rows.mk_hint(_txt53, _h53)
    _l53.addWidget(_mk53)
    _h53.show()
    pump(0.2)
    _one53 = _mk53.fontMetrics().horizontalAdvance(_txt53)
    _h53.setFixedWidth(max(40, int(_one53 * 0.45)))
    pump(0.2)
    _held53 = _mk53.width()
    _ok53 = (not SW.clipped_labels(_h53) and not SW.clipped_without_hint(_h53)
             and not SW.squeezed_widgets(_h53))
    _min53 = int(_h53.minimumSizeHint().width())
    _orig_hint = form_rows.mk_hint
    form_rows.mk_hint = lambda text, parent: form_rows.mk_label(text, parent)   # 注入：单行写法
    _h53b = QWidget(None)
    _l53b = QVBoxLayout(_h53b)
    _l53b.setContentsMargins(8, 8, 8, 8)
    _mk53b = form_rows.mk_hint(_txt53, _h53b)
    _l53b.addWidget(_mk53b)
    _h53b.show()
    pump(0.2)
    _h53b.setFixedWidth(max(40, int(_one53 * 0.45)))
    pump(0.2)
    _bad53 = SW.clipped_labels(_h53b) + SW.clipped_without_hint(_h53b)
    _push53 = int(_h53b.minimumSizeHint().width())
    _wrap53b = _mk53b.wordWrap()
    form_rows.mk_hint = _orig_hint                                              # 交回产品
    _ok53_back = (not SW.clipped_labels(_h53) and not SW.squeezed_widgets(_h53))
    falsified.append(("说明行换行闸：单行需求放不下的格子里，mk_label 写法必须既顶宽宿主又被截字尺点名，"
                      "mk_hint 两样都不许有（前提：45% 宽确实 < 单行需求）",
                      _one53 > _held53 + 1 and _ok53 and _min53 * 2 < _one53
                      and (not _wrap53b) and bool(_bad53) and _push53 * 2 > _one53 and _ok53_back,
                      f"单行要 {_one53}px 实得 {_held53}px 宿主最小宽={_min53} 干净={_ok53} "
                      f"单行版顶宽={_push53} 单行版红={_bad53[:1]} 复原干净={_ok53_back}"))
    _h53.deleteLater()
    _h53b.deleteLater()

    # U54 标题条高度：标准字号下规则算出来就是 M.title_h，和旧的"钉死 44"看不出差别，
    #     所以这一注必须先把子件字号放大（探针量到 20→36px，QSS 的 font-size 盖不掉显式
    #     setFont），再要求"高 ≥ 需求+12"；把 `_fit_height` 换成 `setFixedHeight(M.title_h)`
    #     时同一条必须红（44 < 36+12）。
    _tb2 = win2.title
    _kids2 = _tb2.findChildren(QLabel) + _tb2.findChildren(QToolButton)
    _font_back = {k: k.font() for k in _kids2}
    for k in _kids2:
        _f = QFont(k.font())
        _px = _f.pixelSize() if _f.pixelSize() > 0 else max(1, int(_f.pointSize() * 96.0 / 72.0))
        _f.setPixelSize(_px * 3)
        k.setFont(_f)
    pump(0.2)
    _need54 = max(int(k.sizeHint().height()) for k in _kids2)
    _orig_fh = TitleBar._fit_height
    TitleBar._fit_height = lambda self: self.setFixedHeight(theme.M.title_h)   # 注入：钉死旧写法
    _tb2._fit_height()
    pump(0.2)
    _h54_pin = int(_tb2.height())
    TitleBar._fit_height = _orig_fh                                             # 交回产品
    _tb2._fit_height()
    pump(0.2)
    _h54_fit = int(_tb2.height())
    _sqz54 = SW.squeezed_widgets(_tb2)
    for k, f in _font_back.items():
        k.setFont(f)
    _tb2._fit_height()
    pump(0.2)
    _h54_back = int(_tb2.height())
    falsified.append(("标题条高度跟着字走：字号×3 之后钉死 M.title_h 的旧写法必须红（高<需求+12），"
                      "换回 `_fit_height` 立刻涨上去，还原字号后回到标准档",
                      _need54 > theme.M.title_h - 12 and _h54_pin < _need54 + 12
                      and _h54_fit >= _need54 + 12 and not _sqz54,
                      f"放大后需求={_need54} 钉死={_h54_pin} 规则={_h54_fit} "
                      f"（下限 {theme.M.title_h}） 压扁={_sqz54[:1]} 还原后高={_h54_back}"))

    # U55 下拉的 12 字档：这一档管的是"被布局压到时不许短成这样"，所以判据用两个用同一份
    #     工厂造出来的下拉对跑——一个保持 12、一个调回 0（＝没这一行），后者必须连
    #     "12 个 0"都放不下。产品侧那条验收量的是每个下拉都过了这一档。
    #     "交回"那一腿不能拿同一个控件改回 12：Qt 把 `sizeHint` 缓在控件里，
    #     polish 之后再 `setMinimumContentsLength(12)` 数字不回涨（实测停在 80，
    #     上一轮证伪就是把这条误记成 MISSED）。所以交回腿用**工厂重新造一只**——
    #     它走的正是产品那条路径，同时还是个对照组：排除"第一只天生宽、第二只天生窄"
    #     这种与那一行无关的解释。
    class _Item55:
        label = "输出设备"
        options = [("auto", "自动"), ("null", "无输出")]

    _h55 = QWidget(None)
    _l55 = QVBoxLayout(_h55)
    _cb12 = form_rows.add_choice(_l55, _Item55(), _h55, lambda _c: None)
    _cb0 = form_rows.add_choice(_l55, _Item55(), _h55, lambda _c: None)
    _cb0.setMinimumContentsLength(0)                       # 注入：把这一档摘掉
    _h55.show()
    pump(0.2)
    _n12 = _cb12.fontMetrics().horizontalAdvance("0" * 12)
    _w12 = int(_cb12.sizeHint().width())
    _w0 = int(_cb0.sizeHint().width())
    _cb_back = form_rows.add_choice(_l55, _Item55(), _h55, lambda _c: None)   # 交回产品那一档
    pump(0.2)
    _w0_back = int(_cb_back.sizeHint().width())
    falsified.append(("下拉最短宽按字数给：同一份工厂、同样两条短选项，摘掉 12 字档必须连 12 个「0」"
                      "都放不下，用工厂重新造一只立刻够（交回腿不拿同一只控件改回 12：sizeHint 已缓存）",
                      _w12 >= _n12 and _w0 < _n12 and _w0_back >= _n12,
                      f"12个0要={_n12} 12档={_w12} 摘掉={_w0} 复原={_w0_back}"))
    _h55.deleteLater()

    # U56 设置窗的加宽闸（`form_rows.fit_to_content`）：把窗口压到需求以下再放手，
    #     真闸必须加宽到 ≥ 需求并让两把裁字尺清场；换成恒等版（"只报当前宽、不动窗口"）
     #    之后宽度停在自身最小值、右侧标签整块掉出视口——这条红的正是"关不掉的窗口"
    #     与"被切的最后一格"之间选了后者，而前者才是要避免的。
    _d56 = SettingsDialog(cfg, win2)
    _d56.show()
    pump(0.4)
    _need56 = int(_d56.content_need)
    _orig_fit = form_rows.fit_to_content
    form_rows.fit_to_content = lambda w, cap=None: int(w.width())   # 注入：加宽那步摘掉
    _d56.resize(300, _d56.height())
    pump(0.3)
    _d56._fit_width()
    pump(0.3)
    _w56_bad = int(_d56.width())
    _red56 = (SW.clipped_without_hint(_d56) + SW.beyond_viewport(_d56))
    form_rows.fit_to_content = _orig_fit                             # 交回产品
    _w56 = int(_d56._fit_width())
    pump(0.3)
    _clean56 = (SW.clipped_without_hint(_d56) + SW.beyond_viewport(_d56))
    _need56_again = int(_d56.content_need)
    _d56.hide()
    _d56.deleteLater()
    falsified.append(("设置窗加宽闸：压到需求以下时恒等版必须停在窄处并被两把尺点名，"
                      "真闸必须加宽到≥需求且清场（content_need 当场重算一致）",
                      _w56_bad < _need56 and bool(_red56) and _w56 >= _need56
                      and not _clean56 and _need56_again == _need56,
                      f"需求={_need56} 摘闸后宽={_w56_bad}（红 {len(_red56)} 条：{_red56[0][:34]}） "
                      f"复原宽={_w56} 复原红={_clean56[:1]} 重算={_need56_again}"))

    # ---- U57 像素判据的记账分家（R7 实测的那条：tests/out/accept-ui-r7.log:148 外来窗口
    #      pid18248 压住视频区，capture() 已把这块登进判废台账，而判据写作 `... and not e_area`
    #      又必然为假 ⇒ 同一条既"不作数"又"真实失败"，rc 从 3 掉到 1、汇总报 153/154，
    #      而窗口内容一个字没变）。三条腿，证据全取台账长度，不读错误文案：
    #      ①接线：名字在 CAPTURE_ENV 里 ⇒ pixel_check 必须不往 results 记红（记 ENV）；
    #      ②对照：同一份参数、名字摘掉 ⇒ **必须**照常记红。少了②这条分流就是"什么都豁免"的
    #        恒真金牌，"真的没画出来"也能说成环境问题；
    #      ③金牌不跨趟：先让闸门判废一次（把 _exempt_blocker 调成恒真＝谁都算外来），同一个
    #        名字再抓一次此刻真没挡路的 ⇒ 必须抓到像素**且**名字已从判废集合里摘干净。
    #        少了③，一次早到的遮挡就会把这一整趟后面所有同名判据免检掉。
    _n57 = "U57 视频区（金牌不跨趟）"
    _r_before = len(results)
    pixel_check("U57 判废块", "U57-A 没判废时必须照常记红", False, "对照组：这一条该进台账")
    _r_mid = len(results)                     # A 那条应当已经进台账，下面的"不涨"以这个数为基准
    _bites = (_r_mid == _r_before + 1 and results[-1][0] == "U57-A 没判废时必须照常记红"
              and results[-1][1] is False)
    CAPTURE_ENV.add("U57 判废块")
    pixel_check("U57 判废块", "U57-B 判废块必须记 ENV（不许进台账）", False, "这一块的抓屏没证据")
    _env_side = len(results) == _r_mid
    CAPTURE_ENV.discard("U57 判废块")
    del results[_r_before:]                       # 证伪跑次不许把这条桩红留给汇总
    # ③ 先造一次"外来挡路"，再用同一个名字抓一次没挡路的
    _env_before57 = len(env_notes)
    blocker57 = QWidget(None, Qt.FramelessWindowHint)
    _blk57 = int(blocker57.winId())               # 真实存在的自家句柄（不必显示）
    _chain_back57 = globals()["_hwnd_chain_at"]
    _exempt_back57 = _exempt_blocker
    globals()["_hwnd_chain_at"] = lambda _x, _y: [_blk57]
    globals()["_exempt_blocker"] = lambda h, p: True          # 变异：谁都算外来窗口
    img57_env, e57_env = capture(win2, win2.video, _n57)
    _marked57 = (_n57 in CAPTURE_ENV and img57_env.isNull() and len(env_notes) > _env_before57)
    globals()["_exempt_blocker"] = _exempt_back57              # 交回产品
    globals()["_hwnd_chain_at"] = _chain_back57                # 交回真命中链：此刻没东西挡路
    img57_ok, e57_ok = capture(win2, win2.video, _n57)
    _cleared57 = (_n57 not in CAPTURE_ENV and not e57_ok and not img57_ok.isNull())
    del env_notes[_env_before57:]
    blocker57.close()
    blocker57.destroy()
    pump(0.2)
    f_case("像素判据的记账分家：打了判废标记的块必须记 ENV 不进台账；同一份参数没打标记时必须"
           "照常记红（少了这条分流就是恒真金牌）；同一个名字判废过一次、再抓到干净帧时必须从"
           "判废集合里摘干净（少了它一次早到的遮挡就会免检后面整趟）",
           _env_side and _bites and _marked57 and _cleared57 and bool(e57_env),
           f"没打标记时记红={_bites}（A 那条确实进了台账）打标记后不涨={_env_side} "
           f"判废时挂上标记={_marked57}（err={e57_env[:40]!r}）干净帧后摘净={_cleared57}"
           f"（图 {img57_ok.width()}×{img57_ok.height()} err={e57_ok!r}）｜台账与判废清单已还原",
           needs_screen=True)

    # ---- U58 「添加＝追加」那三条真机判据的牙（#88，2026-10-17）----
    #       三条判据量的是"观察结果"，观察本身不会证明自己能把坏形状否掉：判据整条写成
    #       `check(..., True, ...)` 也照样跑绿。谓词拎成 add_verdict 就是为了这里能喂
    #       **已知坏的读数**——四种坏写法各一份，外加"模式名写错"那份：
    #         playing 档的坏读数＝当年整表替换（3 条变 1 条、正在播那条没了、气泡说"载入"）；
    #         empty   档的坏读数＝裸 append 只入列表（n=1 但 pos 仍 −1、path 仍空）；
    #         empty   档第二条＝播起来了却说"追加…没打断当前播放"（这一档没有当前条目可打断，
    #                 那句是追加分支的凭据读串过来的；2026-10-19 回执层改版后补的）；
    #         replace 档的坏读数＝把 dropEvent 也顺手改成 append（列表变 2 条、没换片）；
    #         认错模式＝这把尺只剩一档也在"通过"（三档里有一档被人删掉时不许静默放行）。
    #       同时"好读数必须放行"和"前提立不住必须点名前提"两条也一起判：只测负样本会
    #       把尺子改成恒假也测不出来。
    _b_p = {"n": 2, "rows": 2, "path": "gelu-sample-2.mp4", "pos": 1}
    _g_p = {"n": 3, "rows": 3, "path": "gelu-sample-2.mp4", "pos": 1}
    _r_old = {"n": 1, "rows": 1, "path": "gelu-sample-3.mkv", "pos": 0}
    _e_b = {"n": 0, "rows": 0, "path": "", "pos": -1}
    _g_e = {"n": 1, "rows": 1, "path": "gelu-sample-3.mkv", "pos": 0}
    _r_noplay = {"n": 1, "rows": 1, "path": "", "pos": -1}
    _b_r = {"n": 3, "rows": 3, "path": "gelu-sample-2.mp4", "pos": 1}
    _r_app = {"n": 4, "rows": 4, "path": "gelu-sample-2.mp4", "pos": 1}
    #   empty 档的两条文字判据是同一句话的两半（"这一档只许说载入家族"），所以那份坏读数
    #   会同时咬两条——没有单故障样本，这里不自欺：承重的仍然是 pos/path 那两条。
    _v_good, _d_good = add_verdict("playing", _b_p, _g_p, "已追加 1 个文件")
    _v_old, _d_old = add_verdict("playing", _b_p, _r_old, "已载入 1 个文件")
    _v_egood, _d_egood = add_verdict("empty", _e_b, _g_e, "已载入 1 个文件")
    _v_eno, _d_eno = add_verdict("empty", _e_b, _r_noplay, "已载入 1 个文件")
    _v_eapp, _d_eapp = add_verdict("empty", _e_b, _g_e, "已追加 1 个文件（没打断当前播放）")
    _v_rgood, _d_rgood = add_verdict("replace", _b_r, _r_old, "已载入 1 个文件")
    _v_rno, _d_rno = add_verdict("replace", _b_r, _r_app, "已追加 1 个文件")
    _v_bogus, _d_bogus = add_verdict("playng", _b_p, _g_p, "已追加 1 个文件")
    _v_prem, _d_prem = add_verdict("empty", _b_p, _g_p, "已载入 1 个文件")
    f_case("「添加＝追加」结果尺有牙：整表替换／只入列表不播／空列表上说了追加那句／把拖放也"
           "改成追加／模式认错 五种读数必须全被否决，三份好读数必须放行，empty 档前提立不住时"
           "必须点名前提",
           _v_good and not _v_old and _v_egood and not _v_eno and not _v_eapp and _v_rgood
           and not _v_rno and not _v_bogus and not _v_prem and "前提立不住" in _d_prem,
           f"playing 好={_v_good} 旧替换={_v_old}｜empty 好={_v_egood} 不播={_v_eno} "
           f"说追加={_v_eapp}（{_d_eapp[-58:]}）｜"
           f"replace 好={_v_rgood} 误追加={_v_rno}｜认错模式={_v_bogus}（{_d_bogus[:24]}）｜"
           f"前提={_v_prem}（{_d_prem[:34]}）")

    # ---- U60 「全屏铺满」那两把尺有没有牙（#99，2026-10-19 用户实测的形状：窗口全屏了、
    #      画面只在屏幕中间一块）----
    #      验收 §10.1 新加的四条判据量的全是"观察"，而观察本身不证明它能否掉坏形状：判据整条
    #      写成 `check(..., True, ...)` 照样跑绿。所以这里**真造**一次坏形状——把 mpv 的
    #      原生子窗口 MoveWindow 成容器的一半（这正是 A 档的形状：WS_CHILD 坐标相对父客户区
    #      左上角，所以没铺到的那圈必然落在右、下两侧），要求两把尺**同时**翻红，且读数必须
    #      与基线不同（`mutated != base`：只比"现在红不红"会把"无图／没有子窗口"那种恒红
    #      冒充成抓到）。再用**产品自己的** `video.refit()` 复原、要求同时翻绿——复原若靠
    #      测试自己把窗口搬回去，产品的这扇门就一个字没量到。
    #      前提两条，缺任何一条整条记 ENV（None）而不是 MISSED：①容器里真有原生子窗口；
    #      ②那块像素抓得到（"无图"＝屏幕休眠或被外来窗口压住，两把尺都无从可读）。
    #      基线是红的**不算前提**：那是产品红，这条会照样红、并把基线读数印在详情里。
    win2.add_paths([FILES[0]])
    _u60_kids = wait_for(lambda: bool(win32.direct_children(win2.video.hwnd)), 20.0)
    pump(1.2)                                 # 让内核把帧画进新铺的矩形，别在中间态取样
    _b_fills, _b_fd, _b_shape, _b_pd = fit_reading(win2.video, win2, "U60 基线")
    if _u60_kids and _b_shape not in ("", "无图"):
        _kid = win32.direct_children(win2.video.hwnd)[0]
        _cw, _ch = win32.client_size(win2.video.hwnd)
        win32.move_window(_kid, 0, 0, max(1, _cw // 2), max(1, _ch // 2))
        pump(0.8)
        _m_fills, _m_fd, _m_shape, _m_pd = fit_reading(win2.video, win2, "U60 注入")
        win2.video.refit()                    # 复原走产品那一手，不是测试自己搬回去
        pump(0.8)
        _r_fills, _r_fd, _r_shape, _r_pd = fit_reading(win2.video, win2, "U60 复原")
        _u60 = (_m_fills is False and _m_shape not in FS_OK_SHAPES
                and (_m_fills, _m_shape) != (_b_fills, _b_shape)
                and _r_fills is True and _r_shape in FS_OK_SHAPES)
        _u60_d = (f"基线（该绿）={_b_fills}/{_b_shape}｜注入（两把尺都要红、且与基线不同）="
                  f"{_m_fills}/{_m_shape} 矩形「{_m_fd}」像素「{_m_pd}」｜"
                  f"复原（走产品 refit）={_r_fills}/{_r_shape} 矩形「{_r_fd}」像素「{_r_pd}」")
    else:
        _u60 = None
        _u60_d = (f"前提立不住：容器里有原生子窗口={_u60_kids}，基线像素读数={_b_shape!r}"
                  f"（{_b_pd}）——内核没起来或这块抓不到像素，两把尺都无从可读")
    falsified.append(("全屏铺满那两把尺有牙：把 mpv 子窗口真缩成容器一半必须让矩形尺与像素尺同时翻红"
                      "且读数与基线不同；再用产品自己的 refit() 必须同时翻绿（复原若靠测试自己搬回去，"
                      "就等于没量产品那扇门）", _u60, _u60_d))

    # ---- U61 「全屏悬停不许把标题栏放回来」这条判据有没有牙（R20 的根因本身，#99/#100）----
    #      注入的就是**旧实现**那一行：`_wake_ui()` 里连着 `title.setVisible(True)`。
    #      这一条踩的坑与"摘映射变异改类属性、对登记表里的绑定方法对象是空操作"同形：
    #      Qt 的连接存的是**建连那一刻解析出来的绑定方法**，所以只写 `win2._wake_ui = ...`
    #      或只改类属性都动不了已经连上的那条槽——必须 disconnect→换→connect 三步走，
    #      复原也走同一条路，并且**当场断言 mutated 的读数与基线不同**（不然又是空操作冒充抓到）。
    #      基线那拍先量一次"悬停回来的是条、不是标题栏"；注入那拍要求 `d` 成立（判据翻红）；
    #      复原那拍先手动抹掉注入留下的那一行、再发一次悬停，要求产品自己的处理器不再放它回来
    #      ——复原若只靠"我自己把标题栏藏回去"，产品这一手就一个字没量到。
    win2.toggle_fullscreen(True)
    _u61_fs = wait_for(lambda: bool(win2.windowState() & Qt.WindowFullScreen), 3.0)
    pump(0.6)
    if _u61_fs:
        _orig_wake = win2._wake_ui
        # 三拍都是**同一个手势**（发一次悬停再读），否则"基线绿、注入红"可能只是两拍状态不同。
        win2.video.sig_hover.emit(QPoint(60, 60))
        pump(0.4)
        _b_ch, _b_d = chrome_reading(win2)
        _b_ok = (not _b_ch["d"] and not _b_ch["h_short"]
                 and (win2.sidebar.isVisible() or not _b_ch["w_short"]))

        def _old_wake(_pt=None):                     # ← R20 之前：条与标题栏一起回来
            if not win2._fs:
                return
            win2.bar.setVisible(True)
            win2.title.setVisible(True)
            win2._arm_autohide()

        win2.video.sig_hover.disconnect(_orig_wake)
        win2._wake_ui = _old_wake
        win2.video.sig_hover.connect(win2._wake_ui)
        win2.video.sig_hover.emit(QPoint(60, 60))
        pump(0.4)
        _m_ch, _m_d = chrome_reading(win2)
        win2.video.sig_hover.disconnect(win2._wake_ui)
        del win2._wake_ui                            # 回到类上那个真实现
        win2.video.sig_hover.connect(_orig_wake)
        win2.title.setVisible(False)                 # 抹掉注入留下的那一行（下一步要量的是处理器）
        pump(0.4)
        win2.video.sig_hover.emit(QPoint(140, 140))
        pump(0.4)
        _r_ch, _r_d = chrome_reading(win2)
        _r_ok = (not _r_ch["d"] and not _r_ch["h_short"]
                 and (win2.sidebar.isVisible() or not _r_ch["w_short"]))
        _u61 = bool(_b_ok and _m_ch["d"] is True and (_m_ch["d"], _m_ch["rows_lost"])
                    != (_b_ch["d"], _b_ch["rows_lost"]) and _r_ok)
        _u61_d = (f"基线（该绿）={_b_ok}｜注入旧实现那拍（必须 d=True 且与基线不同）="
                  f"{_m_ch['d']}｜复原后再悬停（该绿）={_r_ok}｜"
                  f"基线「{_b_d}」→ 注入「{_m_d}」→ 复原「{_r_d}」")
    else:
        _u61 = None
        _u61_d = "前提立不住：全屏没进去（屏被别的窗口压住／显示服务没应答），几何尺三拍都无从可读"
    falsified.append(("全屏悬停那把几何尺有牙：把 _wake_ui 注回旧实现（连着放标题栏回来）必须让"
                      "「标题栏必须仍藏着」当场翻红、且读数与基线不同；换回真处理器后再悬停必须翻绿"
                      "（连接存的是建连那一刻的绑定方法，所以必须 disconnect→换→connect 三步走）",
                      _u61, _u61_d))
    win2.toggle_fullscreen(False)
    pump(0.6)

    # ---- U62 「开着浮层却不接手」这一档抓得住吗（R21 的 overlay_off／host_reading 那两条）----
    #      注入的是 `ChromeHost.take` 变空操作：条留在根布局那一行里。这一档**光看几何看不出来**——
    #      老尺子的 `want_h` 无条件减掉醒着的条（2026-10-20 审查点名的结构失明），所以只有
    #      overlay_off 与 host_reading 两条会翻红。注入之后**不自己动手搬条**：复原走产品自己的
    #      `_apply_mode()`，否则"搬进搬出"这扇门又是一个字没量到。
    if win2.cfg["float_controls_in_fs"]:
        win2.host.take = lambda bar: None                 # 宿主永远不接手
        win2.toggle_fullscreen(True)
        pump(0.8)
        _m62_ch, _m62_cd = chrome_reading(win2)
        _m62_h, _m62_hd = host_reading(win2)
        del win2.host.take                                # 换回类上那个真实现
        win2._apply_mode()                                # 复原＝让产品自己重摆一次
        pump(0.8)
        _r62_ch, _r62_cd = chrome_reading(win2)
        _r62_h, _r62_hd = host_reading(win2)
        _u62_t = (_m62_ch["overlay_off"], _m62_ch["bar_hosted"], _m62_h["hosting"])
        _u62_r = (_r62_ch["overlay_off"], _r62_ch["bar_hosted"], _r62_h["hosting"])
        _u62 = (bool(_m62_ch["overlay_off"]) and not _m62_ch["bar_hosted"] and not _m62_h["hosting"]
                and _u62_t != _u62_r
                and not _r62_ch["overlay_off"] and _r62_ch["bar_hosted"] and _r62_h["hosting"]
                and _r62_ch["full_stage"])
        _u62_d = (f"注入（该红：overlay_off 响、条的父不是宿主）={_u62_t}｜"
                  f"复原（该绿：走产品 _apply_mode 之后条回到宿主、舞台＝整块屏）={_u62_r}｜"
                  f"注入「{_m62_cd}」{_m62_hd}｜复原「{_r62_cd}」{_r62_hd}")
    else:
        _u62 = None
        _u62_d = ("前提立不住：`float_controls_in_fs` 是关着的——退路模式本来就不该接手，"
                  "这一档没有可注入的对象（不是判据没牙）")
    falsified.append(("浮层接手这一手有牙：把 ChromeHost.take 注成空操作必须让 overlay_off 当场响、"
                      "条的父必须不是宿主；换回真实现后由产品自己的 _apply_mode() 重摆必须翻绿"
                      "（overlay_off 灭、bar_hosted 真、full_stage 真）——"
                      "这一档老几何尺结构性看不见，所以复原也不许测试自己动手搬条",
                      _u62, _u62_d))

    # ---- U62b 「醒着那一拍宿主必须亮」这条要求有没有牙（R20 的时序教训）----
    #      R20 真机那条「宿主 可见=False」的红是判据把可见性排到了**收起之后**，不是产品的坏；
    #      可这一句不能光靠推理就改判据——得证明"宿主不亮"这一手**真的有哪条判据看得见**，
    #      否则把要求挪到醒着那一拍，等于把一条本来就量不到的判据换了个位置继续量不到。
    #      注入的是 `ChromeHost.reveal` 变空操作：条照样搬进宿主、几何全对，但顶层窗不上台——
    #      正是用户那句「全屏之后控制条没了」的形状。要求三件事：注入那拍 `visible` 必须假、
    #      复原那拍必须真、**两拍的几何一模一样**（几何看不见这一手，所以可见性必须单独问）。
    if win2.cfg["float_controls_in_fs"]:
        win2.toggle_fullscreen(True)
        pump(0.8)                                   # 醒着那一拍（还在 HIDE_AFTER_MS 之内）
        _b62, _b62d = host_reading(win2)
        win2.host.reveal = lambda: None             # 注入：宿主自己不上台
        win2._set_bar_shown(False)
        win2._set_bar_shown(True)                   # 让产品自己走一遍 reveal
        pump(0.2)
        _a62, _a62d = host_reading(win2)
        del win2.host.reveal                        # 换回类上那个真实现
        win2._set_bar_shown(False)
        win2._set_bar_shown(True)
        pump(0.2)
        _c62, _c62d = host_reading(win2)
        _u62b = (_b62["visible"] and not _a62["visible"] and _c62["visible"]
                 and _a62["geo"] == _b62["geo"] == _c62["geo"]
                 and _b62["full_w"] and _b62["bottom"] and _b62["over_video"]
                 and _a62["hosting"] and _a62["is_hosting"])
        _u62b_d = (f"现状（该亮）={_b62['visible']} 注入（该灭）={_a62['visible']} "
                   f"复原（该亮）={_c62['visible']}｜三拍几何={_b62['geo']}/"
                   f"{_a62['geo']}/{_c62['geo']}（一模一样＝几何看不见这一手）｜"
                   f"注入那拍条仍住在宿主里={_a62['hosting']}/{_a62['is_hosting']}"
                   f"「{_a62d}」")
    else:
        _u62b = None
        _u62b_d = ("前提立不住：`float_controls_in_fs` 关着⇒没有宿主可注入（不是判据没牙）")
    falsified.append(("宿主不亮这一手有牙：reveal 注成空操作必须让醒着那一拍的可见性翻假，"
                      "换回真实现后必须翻真，而两拍的几何逐位相同（几何看不见＝必须另有一把尺）",
                      _u62b, _u62b_d))

    # ---- U63 「全屏收起侧栏、退出还原」有没有牙（R21 的横向半边：挤掉的是**宽** ⇒ 上下留黑）----
    #      注入的是 `_side_for_fs` 变空操作。三拍分别要求：注入那拍侧栏还开着且 `by_sidebar` 记账；
    #      复原那拍（产品自己重摆）侧栏收起、`by_sidebar` 归零；**退出全屏**那一拍必须把用户开着的那
    #      一栏还回来——还回来的判据也在这条里，不然"收起"就变成"把用户的设置改掉了"。
    win2._set_sidebar(True, 0)
    pump(0.4)
    _u63_pre_w = win2.sidebar.width()
    win2._side_for_fs = lambda fs: None
    win2.toggle_fullscreen(True)
    pump(0.8)
    _m63, _m63d = chrome_reading(win2)
    _m63_vis = win2.sidebar.isVisible()
    del win2._side_for_fs
    win2._apply_mode()
    pump(0.8)
    _r63, _r63d = chrome_reading(win2)
    _r63_vis = win2.sidebar.isVisible()
    win2.toggle_fullscreen(False)
    pump(0.8)
    _x63_vis = win2.sidebar.isVisible()
    _x63_page = win2.sidebar_stack.currentIndex()
    win2._set_sidebar(False)
    pump(0.3)
    _u63 = (bool(_m63_vis) and _m63["by_sidebar"] >= max(1, _u63_pre_w - 2)
            and (not _r63_vis) and _r63["by_sidebar"] == 0 and not _r63["w_short"]
            and _x63_vis and _x63_page == 0
            and (_m63_vis, _m63["by_sidebar"]) != (_r63_vis, _r63["by_sidebar"]))
    _u63_d = (f"注入（该红：侧栏还开着、by_sidebar 记着 {_u63_pre_w}px）=({_m63_vis},"
              f"{_m63['by_sidebar']})「{_m63d}」｜复原（该绿：产品重摆之后收起、归零）=({_r63_vis},"
              f"{_r63['by_sidebar']})「{_r63d}」｜退出全屏还回来={_x63_vis} 页={_x63_page}")
    falsified.append(("全屏与侧栏的往来有牙：把 _side_for_fs 注成空操作必须让"
                      "「进全屏要收起侧栏」当场翻红（by_sidebar 记着一整栏）；换回真实现后由产品"
                      "重摆必须收起、且退出全屏必须把用户那一栏原页还回来——"
                      "只测「收起」不测「还原」的话，这条改动就是在偷偷改用户的设置",
                      _u63, _u63_d))

    # ---- U64 「自动收起被挡回去要重挂」有没有牙（#99 里最阴的一种：越放越黑）----
    #      注入旧实现那一支：光标还压在条上就 `return`，不重挂 singleShot。判据读的是
    #      "挡回去之后 `_hide_timer` 还挂不挂着"——所以得先把"光标压在条上"这个前提做出来
    #      （`underMouse()` 是 Qt 按真实鼠标算的，程序造不出悬停，只能临时换成一句谎话，
    #      量完立刻换回来；换回来这一步失败比判据本身更危险）。
    #      **注入必须换槽，不能只赋值**：`_hide_timer.timeout` 在建窗那一刻就绑到了当时的
    #      绑定方法上（main_window.py 的 `__init__`），而后 `win2._maybe_autohide = ...` 对
    #      已连上的槽是空操作——本机实测 QTimer 照旧调旧方法（与 U61 注释里那条同一个坑）。
    #      所以这里与 U61 一样走 disconnect→换→connect 三步，并用 `_hit64` 钉住"坏槽真的跑了"：
    #      没有这个计数，一条永久空操作也能靠读数巧合蒙过去（记忆里的「摘映射变异＝空操作」）。
    win2.toggle_fullscreen(True)
    pump(0.8)
    win2._hide_timer.stop()
    win2._set_bar_shown(True)
    pump(0.2)
    _under_orig = win2.bar.underMouse
    win2.bar.underMouse = lambda: True

    _hit64 = {"n": 0}

    def _no_rearm():                              # ← 旧实现：挡回去就算了，这一行从此长驻
        _hit64["n"] += 1
        if not win2._fs:
            return
        if win2.bar.underMouse() or win2.title.underMouse():
            return
        win2._set_bar_shown(False)
        win2.title.setVisible(False)
        win2.video.refit()

    win2._hide_timer.timeout.disconnect(win2._maybe_autohide)
    win2._maybe_autohide = _no_rearm
    win2._hide_timer.timeout.connect(win2._maybe_autohide)
    win2._arm_autohide()
    pump(3.0)
    _m64 = win2._hide_timer.isActive()
    _m64_hit = _hit64["n"]
    win2._hide_timer.timeout.disconnect(win2._maybe_autohide)
    del win2._maybe_autohide                     # 回到类上那个真实现
    win2._hide_timer.timeout.connect(win2._maybe_autohide)
    win2._arm_autohide()
    pump(3.0)
    _r64 = win2._hide_timer.isActive()
    win2.bar.underMouse = _under_orig             # 谎话收回去，下一趟让它真的收
    pump(3.2)
    _r64b = (not win2.bar.isVisible()) or win2.bar.underMouse()
    _u64 = (_m64_hit >= 1 and _m64 is False and _r64 is True and _r64b and _m64 != _r64)
    _u64_d = (f"注入那拍（挡回去之后定时器还挂着吗；该 False）={_m64}"
              f" 坏槽真跑过={_m64_hit} 次（0＝注入是空操作，不算抓到）｜"
              f"换回真实现同一手势（该 True）={_r64}｜"
              f"谎话收回后再等一趟（条该真的收起）={_r64b} 条醒着={win2.bar.isVisible()}")
    falsified.append(("自动收起的重挂那一支有牙：把 _maybe_autohide 注成「被光标挡回去就不重挂」"
                      "必须让「singleShot 还挂着」当场变 False，换回真实现同一手势必须是 True；"
                      "并且谎话收回去之后下一趟要真的收起（只比两次读数会把恒不响冒充成抓到）",
                      _u64, _u64_d))

    # ---- U65 「条收起时宿主一起收」有没有牙（空底板：一块没人认领、又挡鼠标的黑边）----
    #      注入只动 bar 的那一版 `_set_bar_shown`：宿主是**顶层窗**，不跟着收就留在桌面上。
    #      读的是 `host_reading()["visible"]`——bar 自己的 setVisible 翻面它看不见，
    #      只看宿主那块窗在不在，所以这条注错了立刻 MISSED 而不是假绿。
    win2._hide_timer.stop()
    win2._set_bar_shown(True)
    pump(0.3)

    def _only_bar(on: bool) -> None:              # ← 旧写法：宿主留在桌面上
        win2.bar.setVisible(bool(on))

    _u65_pre, _u65_pred = host_reading(win2)
    if _u65_pre["hosting"] and _u65_pre["visible"]:
        win2._set_bar_shown = _only_bar
        win2._set_bar_shown(False)
        pump(0.4)
        _m65, _m65d = host_reading(win2)
        del win2._set_bar_shown
        win2._set_bar_shown(False)
        pump(0.4)
        _r65, _r65d = host_reading(win2)
        win2._set_bar_shown(True)
        pump(0.3)
        # 两拍的**摆位**必须相同（都还住在宿主里）：这一注只该翻"宿主亮不亮"，
        # 要是顺手把条搬回布局，那红就是搬动带来的，跟"收起没连宿主一起收"无关。
        _u65 = (_m65["visible"] is True and _r65["visible"] is False
                and _m65["hosting"] and _r65["hosting"] and _r65["is_hosting"]
                and (_m65["visible"],) != (_r65["visible"],))
        _u65_d = (f"注入（该红：条藏了、宿主还亮着）={_m65['visible']}｜"
                  f"换回真实现（该绿：宿主跟着一起藏）={_r65['visible']}｜{_m65d}→{_r65d}")
    else:
        _u65 = None
        _u65_d = f"前提立不住：此刻条没住在宿主里／宿主没亮（{_u65_pred}）——退路模式下这一档不存在"
    falsified.append(("收起时要连宿主一起收有牙：把 _set_bar_shown 注成只动 bar 必须让"
                      "「宿主还亮着」当场翻红、换回真实现必须翻绿（读的是宿主那块顶层窗，"
                      "不是 bar 自己的可见性——那种写法翻的正是 bar）",
                      _u65, _u65_d))

    # ---- U66 「外力把窗口从全屏里拽走之后必须降级」有没有牙（R21-a 的窗口态监听）----
    #      注入空 `changeEvent`（＝改之前那版：全仓没人监听窗口态）。读的是"账与实际一致吗"：
    #      `_fs` 还停在 True 而 windowState 已不是全屏，那一版会一直等自动收起、标题栏不来。
    #      复原走产品自己的出口（`toggle_fullscreen(False)`），不是测试把 _fs 手动抹平。
    win2.toggle_fullscreen(True)
    pump(0.8)
    win2.changeEvent = lambda ev: None            # 实例属性覆盖虚函数（PyQt5 查的是实例 dict）
    win2.showMinimized()
    _min66 = wait_for(lambda: bool(win2.windowState() & Qt.WindowMinimized), 3.0)
    pump(0.5)
    win2.showNormal()
    _pulled = wait_for(lambda: not (int(win2.windowState()) & int(Qt.WindowFullScreen)), 2.0)
    pump(0.4)
    _m66_fs = bool(win2._fs)
    _m66_state = int(win2.windowState())
    _m66_title = win2.title.isVisible()
    del win2.changeEvent
    win2.toggle_fullscreen(False)                 # 产品自己的出口
    pump(0.8)
    _r66_fs = bool(win2._fs)
    _r66_title = win2.title.isVisible()
    if not (_min66 and _pulled):
        _u66 = None
        _u66_d = (f"前提立不住：外力没把窗口从全屏里拽走（最小化到位={_min66} "
                  f"还原后全屏位已清={_pulled}）⇒ 没有"
                  "「账是全屏、窗口不是」这笔错账可记，这一档不作数（不是判据没牙）")
    else:
        _u66 = (_m66_fs is True and _m66_title is False
                and _r66_fs is False and _r66_title is True
                and (_m66_fs, _m66_title) != (_r66_fs, _r66_title))
        _u66_d = (f"注入空 changeEvent 后「最小化再点回来」：_fs 还停在 True={_m66_fs} "
                  f"state={_m66_state}（不含全屏位）标题栏={_m66_title}｜走产品出口后："
                  f"_fs={_r66_fs} 标题栏={_r66_title}｜最小化到位={_min66} 全屏位已清={_pulled}")
    falsified.append(("窗口态监听有牙：把 changeEvent 注成空操作，全屏里最小化再点回来必须留下"
                      "「_fs=True 而窗口态已不是全屏、标题栏不来」这笔错账；换回真实现后走"
                      "产品自己的 toggle_fullscreen(False) 必须把账抹平"
                      "（复原若靠测试自己改 _fs，这扇门就一个字没量到）",
                      _u66, _u66_d))
    win2.toggle_fullscreen(False)
    pump(0.6)
    return report()


if __name__ == "__main__":
    sys.exit(falsify() if "--falsify" in sys.argv else main())
