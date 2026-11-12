# -*- coding: utf-8 -*-
"""全屏 chrome 的**离线状态机**验收：不弹任何窗口（`QT_QPA_PLATFORM=offscreen`）。

为什么要有这一条（2026-10-21）：全屏那三处让位（控制条搬进浮层、悬停只唤条、退出/最小化/
侧栏三条状态收口）的真机验收要占用前台，用户在用电脑时跑不了。但其中**大部分是纯状态机**：
根布局里还剩几行、宿主托管没托管、标题栏/侧栏该谁可见、外力拽出全屏后 `_fs` 有没有归位。
这些在离屏平台上能用同一份产品代码走一遍，且完全不碰屏幕。

它测不到什么（不许拿它冒充真机验收）：
  - mpv 原生子窗口有没有铺满、像素上有没有黑边——那要真窗口 + 截屏（tests/probe_fullscreen_fit.py）；
  - 浮层的实际 z-order、点击穿透、菜单能不能弹——离屏平台没有真窗口管理器；
  - 150% 缩放下宿主与主窗的 DPI 对齐——离屏只有一份逻辑坐标。
所以本文件只签"状态机"，`[FINAL]` 行里也这么写。

证伪（`--falsify`）：把上面每条状态收口各换成一个**坏实现**（只改运行时方法，不碰盘上文件），
逐条要求同一套判据翻红，并断言注入后确实是另一个函数对象（NOP 闸门）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

os.environ["QT_QPA_PLATFORM"] = "offscreen"      # 必须先于 PyQt 导入；强设，防继承真窗口平台
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# 每个进程一份临时数据目录：两份同时跑不会互相清掉对方的配置；退出时自己收干净。
# （用固定目录再在 import 期 rmtree 的写法，遇到并跑就是"把对方正在用的配置删了"。）
DATA = tempfile.mkdtemp(prefix="fs-offline-", dir=HERE)
os.environ["GELU_DATA"] = DATA

import atexit                                             # noqa: E402
atexit.register(lambda: shutil.rmtree(DATA, ignore_errors=True))

sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtCore import Qt                              # noqa: E402
from PyQt5.QtWidgets import QApplication, QMainWindow, QMenu    # noqa: E402

from app import config as cfgmod                         # noqa: E402
from app.ui import theme                                 # noqa: E402
from app.ui.floaters import ChromeHost                   # noqa: E402
from app.ui.main_window import MainWindow                # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])


def make_cfg(float_bar: bool = True):
    cfg = cfgmod.Config()
    cfg["resume"] = False
    cfg["float_controls_in_fs"] = float_bar
    cfg.save()
    return cfg


def build(cfg) -> MainWindow:
    w = MainWindow(cfg, files=[])
    w.resize(1280, 720)
    w.show()                    # 离屏平台：不会出现在屏幕上
    APP.processEvents()
    return w


def drop(w: MainWindow) -> None:
    try:
        w.hide()
        w.chrome.uninstall()    # 原生事件过滤器是挂在 qApp 上的，不摘会一条条攒着
    finally:
        w.deleteLater()
        APP.processEvents()


def run_legs(cfg, float_bar: bool = True) -> list[tuple[str, bool, str]]:
    """一套状态机判据。返回（名字, 是否绿, 现场）；异常由调用方记红，不许带崩进程。"""
    rows: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, bool(ok), detail))

    w = build(cfg)
    try:
        ck("L1 窗口态：条在根布局、宿主未托管且隐藏、标题可见",
           w.root_layout.count() == 3 and not w.host.is_hosting()
           and not w.host.isVisible() and w.title.isVisible() and w.bar.isVisible(),
           f"root={w.root_layout.count()} host={w.host.is_hosting()}/{w.host.isVisible()} "
           f"title={w.title.isVisible()} bar={w.bar.isVisible()}")

        w.sidebar.setVisible(True)
        APP.processEvents()
        w.toggle_fullscreen(True)
        APP.processEvents()
        ck("L2 进全屏：条搬出根布局（少一行）、宿主浮出、标题与侧栏让位",
           w._fs and w.root_layout.count() == 2 and w.host.is_hosting()
           and w.host.isVisible() and w.bar.isVisible()
           and not w.title.isVisible() and not w.sidebar.isVisible(),
           f"root={w.root_layout.count()} host={w.host.is_hosting()}/{w.host.isVisible()} "
           f"title={w.title.isVisible()} side={w.sidebar.isVisible()}")
        ck("L3 宿主几何：通栏=主窗宽、贴住主窗下沿、高=条自己的 sizeHint",
           w.host.width() == w.width()
           and w.host.y() + w.host.height() == w.y() + w.height()
           and w.host.height() == max(1, w.bar.sizeHint().height()),
           f"host={w.host.geometry().getRect()} win={w.geometry().getRect()} "
           f"barhint={w.bar.sizeHint().height()}")

        w._maybe_autohide()
        APP.processEvents()
        ck("L4 到点收起：条和宿主一起藏（不许剩一块空底板），标题也不回来",
           not w.bar.isVisible() and not w.host.isVisible() and not w.title.isVisible(),
           f"bar={w.bar.isVisible()} host={w.host.isVisible()} title={w.title.isVisible()}")

        w._wake_ui()
        APP.processEvents()
        ck("L5 晃鼠标唤醒：只唤条（宿主随条出现），标题仍藏（R21-a 的回归位）",
           w.bar.isVisible() and w.host.isVisible() and not w.title.isVisible(),
           f"bar={w.bar.isVisible()} host={w.host.isVisible()} title={w.title.isVisible()}")

        # 正按着轨道拖、光标又在条外停住：到点不许收（收走带鼠标抓取的控件，release 不会补发）
        w._drag_scrub = True
        w._maybe_autohide()
        APP.processEvents()
        ck("L16 拖轨道按住不动到点：条与宿主都不收，定时器重挂（不许把手底下的条抽走）",
           w.bar.isVisible() and w.host.isVisible() and w._hide_timer.isActive(),
           f"bar={w.bar.isVisible()} host={w.host.isVisible()} "
           f"timer={w._hide_timer.isActive()}")
        w._drag_scrub = False
        w._maybe_autohide()
        APP.processEvents()
        ck("L17 松手之后到点照收：这条守卫不会把条永久钉在屏幕上",
           not w.bar.isVisible() and not w.host.isVisible(),
           f"bar={w.bar.isVisible()} host={w.host.isVisible()}")

        # 菜单开着（条的孩子窗）：到点不许收——条一藏，菜单跟着消失
        w._wake_ui()
        menu = QMenu(w.bar)
        menu.addAction("x")
        menu.popup(w.bar.mapToGlobal(w.bar.rect().topLeft()))
        APP.processEvents()
        ck("L18 菜单开着的前提：popup 已生效（否则这一条测的是空气）",
           QApplication.activePopupWidget() is menu and menu.isVisible(),
           f"popup={type(QApplication.activePopupWidget()).__name__} menu={menu.isVisible()}")
        w._maybe_autohide()
        APP.processEvents()
        ck("L19 菜单开着到点不收：条与宿主都在，定时器重挂（不许把菜单连同条一起收掉）",
           w.bar.isVisible() and w.host.isVisible() and w._hide_timer.isActive(),
           f"bar={w.bar.isVisible()} host={w.host.isVisible()} "
           f"timer={w._hide_timer.isActive()}")
        menu.close()
        APP.processEvents()
        w._maybe_autohide()
        APP.processEvents()
        ck("L20 菜单关掉之后到点照收：这条守卫不会把条永久钉在屏幕上",
           not w.bar.isVisible() and not w.host.isVisible(),
           f"bar={w.bar.isVisible()} host={w.host.isVisible()}")

        w._apply_mode()
        APP.processEvents()
        ck("L6 重复应用：不搬出第二条、宿主布局里只有这一条",
           w.root_layout.count() == 2 and w.host.is_hosting()
           and w.host.layout().count() == 1
           and len([b for b in w.host.findChildren(type(w.bar)) if b is w.bar]) == 1,
           f"root={w.root_layout.count()} hostlay={w.host.layout().count()} "
           f"bars={len(w.host.findChildren(type(w.bar)))}")

        w.toggle_fullscreen(False)
        APP.processEvents()
        ck("L7 退全屏：条回根布局、宿主收且隐、标题与侧栏还原",
           not w._fs and w.root_layout.count() == 3 and not w.host.is_hosting()
           and not w.host.isVisible() and w.title.isVisible() and w.bar.isVisible()
           and w.sidebar.isVisible(),
           f"root={w.root_layout.count()} host={w.host.is_hosting()}/{w.host.isVisible()} "
           f"title={w.title.isVisible()} side={w.sidebar.isVisible()}")

        w._set_sidebar(False)
        APP.processEvents()
        w.toggle_fullscreen(True)
        APP.processEvents()
        w._set_sidebar(True)          # 全屏里用户显式开：退出时不许被强行收掉
        APP.processEvents()
        w.toggle_fullscreen(False)
        APP.processEvents()
        ck("L8 全屏里显式开侧栏：退出全屏后仍然开着（显式操作优先于默认还原）",
           w.sidebar.isVisible() and w.root_layout.count() == 3,
           f"side={w.sidebar.isVisible()} root={w.root_layout.count()}")

        w._set_sidebar(False)
        w.toggle_fullscreen(True)
        APP.processEvents()
        w.setWindowState(w.windowState() | Qt.WindowMinimized)
        APP.processEvents()
        ck("L9 全屏里最小化：宿主跟着收（不许孤零零留在桌面），_fs 不丢",
           not w.host.isVisible() and w._fs,
           f"host={w.host.isVisible()} fs={w._fs}")
        w.setWindowState(w.windowState() & ~Qt.WindowMinimized)
        APP.processEvents()
        ck("L10 从最小化回来：仍是全屏，标题仍藏、宿主仍托管",
           w.isFullScreen() and w._fs and not w.title.isVisible() and w.host.is_hosting(),
           f"state={int(w.windowState())} fs={w._fs} title={w.title.isVisible()} "
           f"host={w.host.is_hosting()}")

        w.showNormal()                # 外力把窗口从全屏里拽出来
        APP.processEvents()
        ck("L11 外力拽出全屏：_fs 归 False，chrome 回窗口态（不然只剩一扇无标题窗）",
           not w._fs and not w.isFullScreen() and w.root_layout.count() == 3
           and not w.host.is_hosting() and w.title.isVisible(),
           f"fs={w._fs} full={w.isFullScreen()} root={w.root_layout.count()} "
           f"host={w.host.is_hosting()} title={w.title.isVisible()}")
    finally:
        drop(w)

    # ---- 退路档：配置键关掉浮层，条留在布局里（旧行为），但标题仍让位、收起/唤醒仍成立 ----
    cfg2 = make_cfg(float_bar=False)
    w2 = build(cfg2)
    try:
        w2.toggle_fullscreen(True)
        APP.processEvents()
        ck("L12 退路档（float_controls_in_fs=False）：条留布局、宿主不托管、标题让位",
           w2._fs and w2.root_layout.count() == 3 and not w2.host.is_hosting()
           and not w2.title.isVisible() and w2.bar.isVisible(),
           f"root={w2.root_layout.count()} host={w2.host.is_hosting()} "
           f"title={w2.title.isVisible()} bar={w2.bar.isVisible()}")
        w2._maybe_autohide()
        APP.processEvents()
        ck("L13 退路档到点收起：条藏、标题藏、宿主始终没托管",
           not w2.bar.isVisible() and not w2.title.isVisible() and not w2.host.is_hosting(),
           f"bar={w2.bar.isVisible()} title={w2.title.isVisible()}")
        w2._wake_ui()
        APP.processEvents()
        ck("L14 退路档唤醒：条回来、标题不回来",
           w2.bar.isVisible() and not w2.title.isVisible() and not w2.host.is_hosting(),
           f"bar={w2.bar.isVisible()} title={w2.title.isVisible()}")
        w2.toggle_fullscreen(False)
        APP.processEvents()
        ck("L15 退路档退全屏：根布局三行、标题可见、条可见",
           not w2._fs and w2.root_layout.count() == 3 and w2.title.isVisible()
           and w2.bar.isVisible(),
           f"root={w2.root_layout.count()} title={w2.title.isVisible()} "
           f"bar={w2.bar.isVisible()}")
    finally:
        drop(w2)
    return rows


def print_rows(rows) -> None:
    for name, ok, detail in rows:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}"
              + (f"：{detail}" if detail else ""))


def accept() -> int:
    rows = run_legs(make_cfg())
    print_rows(rows)
    red = [n for n, ok, _ in rows if not ok]
    print(f"[FINAL] 全屏 chrome 离线状态机：共 {len(rows)} 条判据，红 {len(red)} 条"
          + (f"（{red}）" if red else "") + f" ⇒ rc={1 if red else 0}")
    return 1 if red else 0


# ------------------------------------------------------------------ 证伪
@contextmanager
def patched(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    try:
        yield
    finally:
        setattr(cls, name, orig)


def _bad_wake(self, _pt=None):
    """R21-a 回归：唤醒把标题栏也带回来（画面又被压矮一条）。"""
    if not self._fs:
        return
    self._set_bar_shown(True)
    self.title.setVisible(True)
    self._arm_autohide()


def _bad_hide_keeps_host(self, on):
    """收起时只藏条、不藏宿主：屏幕上剩一块按了没反应的黑底板。"""
    self.bar.setVisible(on)
    if on and self.host.is_hosting():
        self.host.reveal()


def _bad_release_keeps_host(self):
    """退全屏时把条交回去，却忘了收宿主：空浮层贴在窗口下沿。"""
    b = self._bar
    if b is None:
        return None
    self._lay.removeWidget(b)
    self._bar = None
    return b


def _bad_place_half_width(self):
    """宿主只铺半宽：真机上的形态是"画面铺满了，控制条只占左半"（另一类黑边）。"""
    if self._bar is None:
        return
    o = self.owner_widget
    h = max(1, self._bar.sizeHint().height())
    g = o.mapToGlobal(o.rect().topLeft())
    self.setGeometry(g.x(), g.y() + max(0, o.height() - h), max(1, o.width() // 2), h)


def _bad_side_exit(self, fs):
    """退出全屏不还原侧栏：进全屏时被收起来的那一栏从此回不来。"""
    if fs:
        if not self._side_saved:
            self._side_saved = True
            self._side_before = self.sidebar.isVisible()
        if self.sidebar.isVisible():
            self.sidebar.setVisible(False)
            self._sync_tab_lights()
    # 退出这一支什么都不做


def _bad_change_ignores_state(self, ev):
    """不监听窗口态：外力拽出全屏后 `_fs` 还留在 True，标题栏与控制条都不回来。"""
    super(MainWindow, self).changeEvent(ev)


def _bad_wake_no_host(self, _pt=None):
    """唤醒只让条"可见"，却不唤出宿主：条住在隐藏的顶层窗里，屏幕上什么都没有。"""
    if not self._fs:
        return
    self.bar.setVisible(True)
    self._arm_autohide()


def _bad_autohide_mid_drag(self):
    """到点不避让拖动：按住的条被从手底下收走，Qt 不会补发 mouseRelease ⇒ 拖动状态永远卡住。"""
    if not self._fs:
        return
    if QApplication.activePopupWidget() is not None:
        self._arm_autohide()
        return
    if self.bar.underMouse() or self.title.underMouse():
        self._arm_autohide()
        return
    self._set_bar_shown(False)
    self.title.setVisible(False)
    self.video.refit()


def _bad_autohide_over_menu(self):
    """菜单开着照收：菜单挂在条上，条一藏菜单跟着消失（全屏里挑字幕挑到一半）。"""
    if not self._fs:
        return
    if self._drag_scrub:
        self._arm_autohide()
        return
    if self.bar.underMouse() or self.title.underMouse():
        self._arm_autohide()
        return
    self._set_bar_shown(False)
    self.title.setVisible(False)
    self.video.refit()


MUTANTS = (
    ("X1 唤醒把标题栏也带回来（R21-a 的回归）", MainWindow, "_wake_ui", _bad_wake),
    ("X2 收起只藏条不藏宿主（空底板）", MainWindow, "_set_bar_shown",
     lambda self, on: _bad_hide_keeps_host(self, on)),
    ("X3 退全屏忘了收宿主（空浮层贴下沿）", ChromeHost, "release", _bad_release_keeps_host),
    ("X4 宿主只铺半宽（控制条只占左半）", ChromeHost, "place", _bad_place_half_width),
    ("X5 退出全屏不还原侧栏", MainWindow, "_side_for_fs",
     lambda self, fs: _bad_side_exit(self, fs)),
    ("X6 不监听窗口态（外力拽出后 _fs 不归位）", MainWindow, "changeEvent",
     _bad_change_ignores_state),
    ("X7 唤醒不唤宿主（条住在隐藏窗里）", MainWindow, "_wake_ui", _bad_wake_no_host),
    ("X8 拖动途中到点照收（按住的条被抽走，release 不再来）", MainWindow, "_maybe_autohide",
     _bad_autohide_mid_drag),
    ("X9 菜单开着到点照收（条一藏菜单跟着消失）", MainWindow, "_maybe_autohide",
     _bad_autohide_over_menu),
)


def falsify() -> int:
    base = run_legs(make_cfg())
    base_red = [n for n, ok, _ in base if not ok]
    ok_base = not base_red
    print(f"[{'PASS' if ok_base else 'FAIL'}] 基线：共 {len(base)} 条判据，红 {len(base_red)} 条"
          + (f"（{base_red}）" if base_red else ""))
    caught = missed = nop = 0
    for name, cls, attr, fn in MUTANTS:
        before = getattr(cls, attr)
        with patched(cls, attr, fn):
            if getattr(cls, attr) is before:
                nop += 1
                print(f"[NOP] {name}：注入后还是同一个函数对象（变异失效，不是漏网）")
                continue
            rows = run_legs(make_cfg())
        red = [n for n, ok, _ in rows if not ok and n not in base_red]
        if red:
            caught += 1
            print(f"[CAUGHT] {name} → {red[0]}")
        else:
            missed += 1
            print(f"[MISSED] {name}：同一套判据没翻红（恒真）")
    bad = missed + nop + (0 if ok_base else 1)
    print(f"[FINAL] 状态机证伪：共 {len(MUTANTS)} 条坏实现，判不到的（MISSED/NOP）{missed + nop} 条"
          f"，基线{'绿' if ok_base else '红'} ⇒ rc={1 if bad else 0}")
    return 1 if bad else 0


def main() -> int:
    return falsify() if "--falsify" in sys.argv else accept()


if __name__ == "__main__":
    raise SystemExit(main())
