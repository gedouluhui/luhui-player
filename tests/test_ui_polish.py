# -*- coding: utf-8 -*-
"""界面收口验收：窄窗控制条、关闭钮悬停换图、主按钮三态。**offscreen，不弹窗**。

用法：
    python tests/test_ui_polish.py            # 验收：全部判据必须 PASS
    python tests/test_ui_polish.py --falsify  # 证伪：坏写法必须被同一条判据抓住

为什么这三件事值得单开一份：
1. **窄窗控制条**：标准档按钮和 1033px，而外壳承诺能缩到 880px。只把
   `minimumSizeHint` 改小没用——QLayout 会把原生最小宽写回父窗口，窗口根本到不了 880
   （实测请求 880、实际停在 1033）。这里同时量"能到 880"和"到了 880 以后没有被裁"，
   后者靠每个按钮在控制条坐标系里的右边界，不靠 `isVisible()`（被裁掉的按钮仍然可见）。
2. **关闭钮悬停**：QSS 的 `color` 改不到 QIcon 的像素，红底上那枚灰 X 几乎看不见；
   `QIcon.Active` 在 QToolButton 只悬停时也不保证被取用。判据直接读图标像素的最亮值，
   不读 cacheKey 了事——同一个 QIcon 对象内部换 pixmap 时 cacheKey 也会变，但它证明不了
   "屏幕上确实亮了"。
3. **主按钮三态**：`#Primary` 是 id 选择器，特异度压过 `QPushButton:pressed/:disabled`。
   不点名这两档时，按下去和悬停一样、导出中的"开始"仍是可点的象牙实底（gif_dialog 真的会
   禁用它）。判据在 offscreen 上真渲染一颗按钮，读按钮底的像素，不 grep 样式表。

证伪纪律（与 tests/test_engine_async_real.py 同一套）：
- 基线必须绿，变异才允许记 CAUGHT；
- 变异必须真的落地（NOP 单独记失败，不许算"漏网"）；
- 判据不因变异而放宽——变异与基线走同一个 judge_*。
"""
from __future__ import annotations

import atexit
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
TMP = tempfile.mkdtemp(prefix="gelu-polish-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")
os.environ["QT_QPA_PLATFORM"] = "offscreen"       # 用户在用电脑时也不许弹窗
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QEvent, QObject, QPoint, Qt, pyqtSignal     # noqa: E402
from PyQt5.QtGui import QColor, QIcon                                # noqa: E402
from PyQt5.QtWidgets import (QApplication, QLayout, QPushButton,     # noqa: E402
                             QToolButton)

from app import config as cfgmod                                     # noqa: E402
from app.ui import icons, theme                                      # noqa: E402
from app.ui.controls_bar import ControlsBar                          # noqa: E402
from app.ui.main_window import MainWindow                            # noqa: E402
from app.ui.stats_panel import StatsPanel                             # noqa: E402
from app.ui.title_bar import TitleBar, _HoverSwapButton              # noqa: E402

M = theme.M
APP = QApplication.instance() or QApplication(sys.argv[:1])

results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def pump(ms: int = 40) -> None:
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


CFG = make_cfg()
theme.install(APP, theme.ACCENT_DEFAULT)          # 量的是产品那套餐具，不是 Fusion 默认


class DummyPlayer(QObject):
    """ControlsBar 只连这些信号，不碰内核。"""

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


# ===================== 窄窗控制条 =====================
def make_bar() -> ControlsBar:
    bar = ControlsBar(DummyPlayer(), CFG)
    bar.resize(1200, 80)
    bar.show()
    pump(80)
    return bar


def bar_snapshot(bar: ControlsBar, width: int) -> dict:
    bar.resize(width, 80)
    pump(60)
    bar.layout().activate()                        # 让固定尺寸的改动落进最小值计算
    pump(20)
    btns = list(bar._tool_buttons)
    secondary = [b for b in btns if b is not bar.btn_play]
    right = 0
    for b in btns:
        right = max(right, b.mapTo(bar, QPoint(0, 0)).x() + b.width())
    return {
        "want": int(width),
        "width": int(bar.width()),
        "compact": bool(bar._compact),
        "min_hint": int(bar.minimumSizeHint().width()),
        "layout_min": int(bar.layout().minimumSize().width()),
        "vol": int(bar.vol.width()),
        "spacing": int(bar._row.spacing()),
        "big": int(bar.btn_play.width()),
        "secondary": [int(b.width()) for b in secondary],
        "icons": [int(b.iconSize().width()) for b in secondary],
        "base_icons": [int(bar._icon_base.get(b, 20)) for b in secondary],
        "n_buttons": len(btns),
        "n_visible": sum(1 for b in btns if b.isVisible()),
        "right": int(right),
    }


def judge_standard(s: dict) -> list[str]:
    bad: list[str] = []
    if s["compact"]:
        bad.append("1200 宽仍是紧凑档")
    if s["width"] != s["want"]:
        bad.append(f"实际宽 {s['width']} ≠ 请求 {s['want']}")
    if s["layout_min"] <= 1000:
        bad.append(f"标准档布局最小宽只有 {s['layout_min']}（控件没恢复原尺寸？）")
    if any(w != M.btn for w in s["secondary"]):
        bad.append(f"次级按钮宽 {sorted(set(s['secondary']))} ≠ {M.btn}")
    if s["icons"] != s["base_icons"]:
        bad.append(f"图标 {s['icons'][:4]}… ≠ 标准 {s['base_icons'][:4]}…")
    if s["vol"] != 96:
        bad.append(f"音量条 {s['vol']} ≠ 96")
    if s["spacing"] != 2:
        bad.append(f"行距 {s['spacing']} ≠ 2")
    if s["n_visible"] != s["n_buttons"]:
        bad.append(f"可见 {s['n_visible']}/{s['n_buttons']} 颗（标准档不许藏按钮）")
    return bad


def judge_compact(s: dict) -> list[str]:
    bad: list[str] = []
    if s["width"] != s["want"]:
        bad.append(f"实际宽 {s['width']} ≠ 请求 {s['want']}：窗口被布局硬下限顶住了")
    if not s["compact"]:
        bad.append("到了 880 宽却没进紧凑档")
    if s["min_hint"] > 880:
        bad.append(f"minimumSizeHint={s['min_hint']} > 880")
    if s["layout_min"] > 880:
        bad.append(f"紧凑档布局最小宽 {s['layout_min']} > 880（放不下）")
    if any(w != M.btn_s for w in s["secondary"]):
        bad.append(f"次级按钮宽 {sorted(set(s['secondary']))} ≠ {M.btn_s}")
    want_icons = [max(16, b - 2) for b in s["base_icons"]]
    if s["icons"] != want_icons:
        bad.append(f"图标 {s['icons'][:4]}… ≠ 紧凑 {want_icons[:4]}…")
    if s["vol"] != 72:
        bad.append(f"音量条 {s['vol']} ≠ 72")
    if s["spacing"] != 0:
        bad.append(f"行距 {s['spacing']} ≠ 0")
    if s["big"] != M.big_play:
        bad.append(f"BigPlay {s['big']} ≠ {M.big_play}（主操作不许跟着缩）")
    if s["n_visible"] != s["n_buttons"]:
        bad.append(f"可见 {s['n_visible']}/{s['n_buttons']} 颗（紧凑档不许藏入口）")
    if s["right"] > s["width"]:
        bad.append(f"最右按钮到 {s['right']}px，超出 {s['width']}px（右边被裁）")
    return bad


def compact_measure() -> dict:
    """一次完整的 1200 → 880 → 回差 → 1200，读四组快照。"""
    bar = make_bar()
    try:
        std = bar_snapshot(bar, 1200)
        compact = bar_snapshot(bar, 880)
        h1 = bar_snapshot(bar, 1100)              # 仍 <1140：回差内必须保持紧凑
        h2 = bar_snapshot(bar, 1135)
        h3 = bar_snapshot(bar, 1185)              # >1180：退标准
        back = bar_snapshot(bar, 1200)
        return {"std": std, "compact": compact, "h1": h1, "h2": h2, "h3": h3,
                "back": back}
    finally:
        bar.close()
        bar.deleteLater()
        pump(30)


def judge_hysteresis(m: dict) -> list[str]:
    bad: list[str] = []
    if not m["h1"]["compact"] or not m["h2"]["compact"]:
        bad.append("1100/1135 宽提前退出了紧凑档（回差没兜住，拖动时会来回跳）")
    if m["h3"]["compact"]:
        bad.append("1185 宽仍停在紧凑档（回差上沿失效）")
    return bad


def shell_measure() -> dict:
    """真主窗：880 是不是真能到，到了以后控制条最右一颗有没有出界。"""
    win = MainWindow(make_cfg("shell.json"), files=[])
    try:
        win.move(30, 30)
        win.show()
        pump(250)
        win.resize(880, 620)
        pump(250)
        bar = win.bar
        bar.layout().activate()
        pump(40)
        right = 0
        for b in bar._tool_buttons:
            right = max(right, b.mapTo(bar, QPoint(0, 0)).x() + b.width())
        tight = {
            "win_w": int(win.width()),
            "bar_w": int(bar.width()),
            "compact": bool(bar._compact),
            "right": int(right),
            "min_w": int(win.minimumWidth()),
            "screen_w": int(APP.primaryScreen().availableGeometry().width()),
        }
        win.resize(1200, 720)
        pump(250)
        wide = {"bar_w": int(bar.width()), "compact": bool(bar._compact)}
        return {"tight": tight, "wide": wide}
    finally:
        win.close()
        win.deleteLater()
        pump(80)


def judge_shell(m: dict) -> list[str]:
    bad: list[str] = []
    t = m["tight"]
    # 产品下限 880，但屏本身比 880 还窄时最小宽必须收到屏幕宽——否则窗口一开就整块
    # 出屏。本测试的 offscreen 屏就是 800 宽，正好覆盖"小屏/远程桌面"这一档。
    want_min = min(880, t["screen_w"])
    if t["min_w"] != want_min:
        bad.append(f"主窗最小宽 {t['min_w']} ≠ {want_min}（产品下限 880，屏宽 {t['screen_w']}）")
    if t["win_w"] != 880:
        bad.append(f"请求 880、主窗实际 {t['win_w']}")
    if not t["compact"]:
        bad.append("主窗缩到 880 时控制条没进紧凑档")
    if t["bar_w"] > 880:
        bad.append(f"控制条自己宽 {t['bar_w']} > 880")
    if t["right"] > t["bar_w"]:
        bad.append(f"最右按钮到 {t['right']}px，超出控制条 {t['bar_w']}px")
    if m["wide"]["bar_w"] != 1200 or m["wide"]["compact"]:
        bad.append(f"回到 1200 没恢复：width={m['wide']['bar_w']} compact={m['wide']['compact']}")
    return bad


# ===================== 关闭钮悬停 =====================
def icon_brightness(ic: QIcon) -> int:
    """图标所有不透明像素里最亮的"最小通道"。灰 X≈166，白 X≈255。"""
    pm = ic.pixmap(14, 14)
    if pm.isNull():
        return -1
    img = pm.toImage()
    best = -1
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() >= 64:
                best = max(best, min(c.red(), c.green(), c.blue()))
    return int(best)


def make_titlebar() -> tuple[TitleBar, QToolButton, list[QToolButton]]:
    tb = TitleBar(cfg=CFG)
    tb.resize(880, M.title_h)
    tb.show()
    pump(80)
    caps = [b for b in tb.findChildren(QToolButton) if "CaptionBtn" in b.objectName()]
    close = next(b for b in caps if "CloseBtn" in b.objectName())
    others = [b for b in caps if b is not close]
    return tb, close, others


def hover_measure() -> dict:
    tb, close, others = make_titlebar()
    try:
        normal = close.icon()
        d = {
            "is_swap": isinstance(close, _HoverSwapButton),
            "normal_bright": icon_brightness(normal),
            "normal_key": int(normal.cacheKey()),
            "others_swap": [isinstance(b, _HoverSwapButton) for b in others],
            "other_icon": icon_brightness(others[0].icon()),
            "qss_line": next((l for l in theme.qss().splitlines()
                              if "#CloseBtn:hover" in l), ""),
        }
        QApplication.sendEvent(close, QEvent(QEvent.Enter))
        pump(30)
        hot = close.icon()
        d["hot_bright"] = icon_brightness(hot)
        d["hot_key"] = int(hot.cacheKey())
        QApplication.sendEvent(others[0], QEvent(QEvent.Enter))
        pump(20)
        d["other_after"] = icon_brightness(others[0].icon())
        QApplication.sendEvent(close, QEvent(QEvent.Leave))
        pump(30)
        back = close.icon()
        d["back_key"] = int(back.cacheKey())
        d["back_bright"] = icon_brightness(back)
        d["white_icon_key"] = int(icons.icon("close", 14, theme.WHITE).cacheKey())
        d["dim_icon_key"] = int(icons.icon("close", 14, theme.TEXT_DIM).cacheKey())
        return d
    finally:
        tb.close()
        tb.deleteLater()
        pump(30)


def judge_hover(d: dict) -> list[str]:
    bad: list[str] = []
    if not d["is_swap"]:
        bad.append("关闭钮还是普通 QToolButton（悬停换不了图）")
    if not (140 <= d["normal_bright"] <= 200):
        bad.append(f"常态图标最亮值 {d['normal_bright']}（应为 TEXT_DIM 档 140~200）")
    if d["hot_bright"] < 230:
        bad.append(f"悬停图标最亮值 {d['hot_bright']}（红底上不够亮，要 ≥230）")
    if d["hot_key"] == d["normal_key"]:
        bad.append("悬停前后是同一个 QIcon（没换图）")
    if d["hot_key"] != d["white_icon_key"] or d["normal_key"] != d["dim_icon_key"]:
        bad.append("换的不是白/灰那两枚现成图标")
    if d["back_key"] != d["normal_key"]:
        bad.append("移出后没换回灰图标")
    if any(d["others_swap"]):
        bad.append("最小化/最大化也挂了换图（它们由 set_maximized 单点改图标，会留下热点图）")
    if d["other_after"] != d["other_icon"]:
        bad.append("最小化钮悬停时图标也变了（不该动）")
    if not d["qss_line"] or theme.DANGER.lower() not in d["qss_line"].lower():
        bad.append(f"悬停底色不再是 DANGER 红：{d['qss_line']!r}")
    return bad


# ===================== 主按钮三态 =====================
def _same(a: str, b: str, tol: int = 3) -> bool:
    ca, cb = QColor(a), QColor(b)
    return (ca.isValid() and cb.isValid()
            and abs(ca.red() - cb.red()) <= tol
            and abs(ca.green() - cb.green()) <= tol
            and abs(ca.blue() - cb.blue()) <= tol)


def sample_fill(btn: QPushButton) -> str:
    """读按钮底色：取左侧 padding 内一点，避开文字和圆角。"""
    img = btn.grab().toImage()
    dpr = max(1.0, float(img.devicePixelRatio()))
    x = int(10 * dpr)
    y = int(btn.height() * 0.5 * dpr)
    return img.pixelColor(min(x, img.width() - 1), min(y, img.height() - 1)).name().upper()


def theme_measure() -> dict:
    btn = QPushButton("导出", None)
    btn.setObjectName("Primary")
    btn.resize(140, 36)
    btn.show()
    pump(60)
    try:
        normal = sample_fill(btn)
        btn.setDown(True)
        pump(40)
        pressed = sample_fill(btn)
        btn.setDown(False)
        btn.setEnabled(False)
        pump(40)
        disabled = sample_fill(btn)
    finally:
        btn.close()
        btn.deleteLater()
        pump(20)
    return {"normal": normal, "pressed": pressed, "disabled": disabled,
            "want_normal": theme.PRIMARY_FILL.upper(),
            "want_pressed": theme.PRIMARY_FILL_DOWN.upper(),
            "want_disabled": theme.BG_HOVER.upper()}


def judge_theme(d: dict) -> list[str]:
    bad: list[str] = []
    if not _same(d["normal"], d["want_normal"]):
        bad.append(f"常态底 {d['normal']} ≠ {d['want_normal']}")
    if _same(d["want_pressed"], d["want_normal"]):
        bad.append("按下色与常态同值（令牌没派生出来）")
    if not _same(d["pressed"], d["want_pressed"]):
        bad.append(f"按下底 {d['pressed']} ≠ {d['want_pressed']}（:pressed 没生效）")
    if _same(d["pressed"], d["normal"]):
        bad.append("按下与常态一模一样")
    if not _same(d["disabled"], d["want_disabled"]):
        bad.append(f"禁用底 {d['disabled']} ≠ {d['want_disabled']}（仍是亮主色）")
    if _same(d["disabled"], d["normal"]):
        bad.append("禁用与常态一模一样")
    return bad


# ===================== 统计页空态 =====================
class StatsPlayer(QObject):
    """只满足 StatsPanel 的读接口：不建内核、不发 IPC。"""

    sig_info = pyqtSignal(dict)
    sig_stats = pyqtSignal(dict)
    sig_file_loaded = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._props: dict = {}
        self.version = ""
        self.running = False

    def prop(self, key, default=None):
        return self._props.get(key, default)

    def poll_stats(self) -> None:
        pass

    def ipc_stats(self) -> dict:
        return {}

    def volume_max(self) -> int:
        return 200


def _stats_snapshot(sp: StatsPanel) -> dict:
    return {
        "notice": sp._empty.isVisible(),
        "sections": [l.isVisible() for l in sp._media_sections],
        "quality": sp._rows["quality_preset"].isVisible(),
        "usage_title": bool(sp._usage_section is not None and sp._usage_section.isVisible()),
    }


def stats_measure(panel: StatsPanel | None = None) -> dict:
    """空态 → 有片 → 停掉三段真转换。传 panel 进来时由调用方负责收尾（变异腿用）。"""
    sp = panel or StatsPanel(StatsPlayer(), CFG)
    sp.resize(300, 700)
    sp.show()
    pump(80)
    try:
        empty = _stats_snapshot(sp)
        sp.p._props["path"] = "C:/x/a.mp4"
        sp._paint()
        pump(40)
        media = _stats_snapshot(sp)
        sp.p._props["path"] = ""
        sp._paint()
        pump(40)
        back = _stats_snapshot(sp)
        return {"empty": empty, "media": media, "back": back}
    finally:
        if panel is None:
            sp.close()
            sp.deleteLater()
            pump(30)


def judge_stats(d: dict) -> list[str]:
    bad: list[str] = []
    e = d["empty"]
    if not e["notice"]:
        bad.append("没有片子时没画空态说明（只剩一排无数据的小标题）")
    if any(e["sections"]):
        bad.append("没有片子时媒体段标题还在")
    if e["quality"]:
        bad.append("没有片子时仍画了「缩放预设」这一行")
    if not e["usage_title"]:
        bad.append("没有片子时把「资源占用」也收起来了（它和有没有片子无关）")
    m = d["media"]
    if m["notice"]:
        bad.append("有片子时空态说明没消失")
    if not all(m["sections"]):
        bad.append("有片子时媒体段标题没回来")
    if not m["quality"]:
        bad.append("有片子时「缩放预设」那行没回来")
    if not d["back"]["notice"] or any(d["back"]["sections"]):
        bad.append("片子停掉后没回到空态")
    return bad


# ===================== 汇总 =====================
def report() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 验收汇总：{passed}/{len(results)} 通过"
          + (f"；失败项：{fails}" if fails else "；无失败") + " ----", flush=True)
    if falsified:
        print("==== 证伪（坏样本必须被抓出来）====", flush=True)
        for name, ok, detail in falsified:
            print(f"[{'CAUGHT' if ok else 'MISSED'}] {name}"
                  + (f"  —— {detail}" if detail else ""), flush=True)
        missed = [n for n, ok, _ in falsified if not ok]
        print(f"---- 证伪收口：抓到 {len(falsified) - len(missed)}/{len(falsified)}"
              f"（漏网/未落地 {len(missed)}） ----", flush=True)
        if missed:
            return 1
    return 0 if passed == len(results) else 1


def record_baselines() -> dict:
    m = compact_measure()
    hov = hover_measure()
    th = theme_measure()
    sh = shell_measure()
    st = stats_measure()
    return {
        "standard": not judge_standard(m["std"]),
        "compact": not judge_compact(m["compact"]),
        "hysteresis": not judge_hysteresis(m),
        "restore": not judge_standard(m["back"]),
        "shell": not judge_shell(sh),
        "hover": not judge_hover(hov),
        "theme": not judge_theme(th),
        "stats": not judge_stats(st),
    }


def caught(label: str, baseline_ok: bool, bad: list[str], note: str = "",
           landed: bool = True) -> None:
    # "变异没落地"与"判据漏网"是两种坏：前者说明这一趟白跑，不许记 CAUGHT。
    ok = bool(baseline_ok) and bool(bad) and bool(landed)
    detail = "基线绿；" + ("变异红：" + "；".join(bad[:3]) if bad else "变异仍绿")
    if not baseline_ok:
        detail = "基线不是绿的，这一条不作数"
    if not landed:
        detail = "变异未落地（NOP），这一条不作数"
    if note:
        detail = note + "；" + detail
    falsified.append((label, ok, detail))


def falsify(base: dict) -> int:
    # M1：把紧凑档整个摘掉（只改 minHint、不换尺寸的常见写法）
    bar = make_bar()
    try:
        original = bar._apply_compact
        bar._apply_compact = lambda on: None
        nop = bar._apply_compact is original
        s = bar_snapshot(bar, 880)
        caught("M1 紧凑档只改 minHint、控件不收小 → 必须红",
               base["compact"], judge_compact(s), landed=not nop)
    finally:
        bar.close()
        bar.deleteLater()
        pump(20)

    # M2：能进不能出（回差上沿失效）
    bar = make_bar()
    try:
        bar_snapshot(bar, 880)
        real = bar._apply_compact
        bar._apply_compact = lambda on: real(True)
        nop = bar._apply_compact is real
        s = bar_snapshot(bar, 1200)
        bad = judge_standard(s) or (["仍停在紧凑档"] if s["compact"] else [])
        caught("M2 进了紧凑档就退不回来 → 必须红", base["restore"], bad,
               landed=not nop)
    finally:
        bar.close()
        bar.deleteLater()
        pump(20)

    # M3：用"藏按钮"冒充紧凑（看着放得下，入口没了）
    bar = make_bar()
    try:
        real = bar._apply_compact

        def hide(on):
            for b in bar._tool_buttons:
                b.setVisible(not on)

        bar._apply_compact = hide
        nop = bar._apply_compact is real
        s = bar_snapshot(bar, 880)
        caught("M3 窄窗时藏掉次级按钮 → 必须红（功能入口不许消失）",
               base["compact"], judge_compact(s), landed=not nop)
    finally:
        bar.close()
        bar.deleteLater()
        pump(20)

    # M4：不让布局松掉 1033px 的硬下限（窗口根本到不了 880）
    bar = make_bar()
    try:
        before = bar.layout().sizeConstraint()
        bar.layout().setSizeConstraint(QLayout.SetDefaultConstraint)
        bar.layout().invalidate()
        bar.layout().activate()
        pump(60)
        nop = bar.layout().sizeConstraint() == before
        s = bar_snapshot(bar, 880)
        caught("M4 布局仍把 1033px 当父窗硬下限 → 必须红",
               base["compact"], judge_compact(s), landed=not nop)
    finally:
        bar.close()
        bar.deleteLater()
        pump(20)

    # M5：关闭钮不换图（把热点图标指回灰的那枚）
    tb, close, _ = make_titlebar()
    try:
        normal = close.icon()
        close.set_hover_icons(normal, normal)     # 变异：热点也指回灰图
        QApplication.sendEvent(close, QEvent(QEvent.Enter))
        pump(30)
        hot = close.icon()
        bad = [] if icon_brightness(hot) >= 230 else [f"最亮值仍只有 {icon_brightness(hot)}"]
        caught("M5 悬停图标指回同一枚灰图 → 必须红", base["hover"], bad)
    finally:
        tb.close()
        tb.deleteLater()
        pump(20)

    # M6：enter/leave 根本没接（只挂了一个没人调的 set_hover_icons）
    real_enter = _HoverSwapButton.enterEvent
    real_leave = _HoverSwapButton.leaveEvent
    tb = None
    try:
        _HoverSwapButton.enterEvent = lambda self, ev: QToolButton.enterEvent(self, ev)
        _HoverSwapButton.leaveEvent = lambda self, ev: QToolButton.leaveEvent(self, ev)
        nop = (_HoverSwapButton.enterEvent is real_enter
               or _HoverSwapButton.leaveEvent is real_leave)
        tb, close, _ = make_titlebar()
        QApplication.sendEvent(close, QEvent(QEvent.Enter))
        pump(30)
        bright = icon_brightness(close.icon())
        bad = [] if bright >= 230 else [f"悬停后最亮值 {bright}（没换图）"]
        caught("M6 悬停事件没接线 → 必须红", base["hover"], bad, landed=not nop)
    finally:
        _HoverSwapButton.enterEvent = real_enter
        _HoverSwapButton.leaveEvent = real_leave
        if tb is not None:
            tb.close()
            tb.deleteLater()
            pump(20)

    # M7/M8：从样式表里删掉 :pressed / :disabled。删行数必须为 1，否则是 NOP。
    # 只认**规则行**（以 QPushButton 开头）：注释里也提过同样的选择器名，
    # 按子串删会把注释一起算进去，变成"删了 2 行"而规则只少 1 行的假账。
    def strip_rule(sheet: str, needle: str) -> tuple[str, int]:
        lines = sheet.splitlines()
        kept = [l for l in lines if not l.strip().startswith("QPushButton" + needle)]
        return "\n".join(kept), len(lines) - len(kept)

    for label, needle, judge_key, want in (
        ("M7 删掉 #Primary:pressed → 必须红", "#Primary:pressed", "pressed",
         "按下与常态分不出来"),
        ("M8 删掉 #Primary:disabled → 必须红（导出中仍是可点的象牙底）",
         "#Primary:disabled", "disabled", "禁用仍铺常态象牙底"),
    ):
        sheet = theme.qss()
        mutated, removed = strip_rule(sheet, needle)
        APP.setStyleSheet(mutated)
        pump(40)
        try:
            d = theme_measure()
            want_color = d["want_pressed"] if judge_key == "pressed" else d["want_disabled"]
            same_as_normal = _same(d[judge_key], d["normal"])
            changed = _same(d[judge_key], want_color)
            bad = [] if (changed and not same_as_normal) else [
                f"{want}：实际 {d[judge_key]} / 期望 {want_color}"]
            caught(label, base["theme"], bad, landed=(removed == 1),
                   note=f"删了 {removed} 行")
        finally:
            theme.install(APP, theme.ACCENT_DEFAULT)
            pump(30)

    # S1：空态整段摘掉（永远画那排媒体标题，最常见的那种"没写空态"）
    sp = StatsPanel(StatsPlayer(), CFG)
    sp.show()
    pump(60)
    try:
        def no_empty(on):
            sp._empty.setVisible(False)
            for lab in sp._media_sections:
                lab.setVisible(True)

        sp._set_media_visible = no_empty
        caught("S1 统计页空态整段摘掉 → 必须红", base["stats"],
               judge_stats(stats_measure(sp)))
    finally:
        sp.close()
        sp.deleteLater()
        pump(20)

    # S2：空态常驻（有片子也跟着挂一句"还没有在播的片子"）
    sp = StatsPanel(StatsPlayer(), CFG)
    sp.show()
    pump(60)
    try:
        real = sp._set_media_visible

        def always_notice(on):
            real(True)
            sp._empty.setVisible(True)

        sp._set_media_visible = always_notice
        caught("S2 有片子时还挂着空态说明 → 必须红", base["stats"],
               judge_stats(stats_measure(sp)))
    finally:
        sp.close()
        sp.deleteLater()
        pump(20)

    # 记账自检：恒红/恒绿两个已知答案
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
    if "--falsify" in sys.argv:
        base = record_baselines()
        print("[基线] " + " ".join(f"{k}={'绿' if v else '红'}" for k, v in base.items()),
              flush=True)
        return falsify(base)

    m = compact_measure()
    check("控制条标准档：次级 36px / 音量 96 / 行距 2，布局原生最小宽 >1000px",
          not judge_standard(m["std"]), "；".join(judge_standard(m["std"])[:3]))
    check("880 宽进紧凑档：18 颗按钮全在、30px / 音量 72 / 行距 0、布局最小 ≤880、无出界",
          not judge_compact(m["compact"]), "；".join(judge_compact(m["compact"])[:3]))
    check("回差：1100/1135 仍紧凑，1185 才回标准（拖动时不来回跳）",
          not judge_hysteresis(m), "；".join(judge_hysteresis(m)[:3]))
    check("回到 1200 恢复标准档（36 / 96 / 2，图标回原尺寸）",
          not judge_standard(m["back"]), "；".join(judge_standard(m["back"])[:3]))

    sh = shell_measure()
    check("真主窗能缩到 880，控制条进紧凑档且最右按钮没有出界",
          not judge_shell(sh), "；".join(judge_shell(sh)[:3]))

    hov = hover_measure()
    check("关闭钮悬停：灰 X 换成白 X（读像素），移出换回；最小化钮不跟着变",
          not judge_hover(hov), "；".join(judge_hover(hov)[:3]))

    th = theme_measure()
    bad_theme = judge_theme(th)
    check("主按钮三态：常态象牙、按下更深、禁用退中性底（不再亮成'能点'）",
          not bad_theme, "；".join(bad_theme[:4]))

    st = stats_measure()
    check("统计页空态：没片子时收起四段与预设行、只留空态说明，资源占用段仍在",
          not judge_stats(st), "；".join(judge_stats(st)[:3]))

    return report()


if __name__ == "__main__":
    sys.exit(main())
