"""无头探针：窗口置顶（配置 / 快捷键 / 菜单 / Win32 Z 序）。

全程不往用户眼前的屏幕上放任何窗口：
  · Win32 那一段用 CreateWindowExW 建一个 4×4 的 WS_EX_TOOLWINDOW 工具窗，坐标
    −20000（屏幕外）、SW_SHOWNA（不抢焦点），只为拿到一个**看得见但不在屏上**的真 HWND
    来量 `WS_EX_TOPMOST` 位——对不可见窗口 SetWindowPos 不落这个位（实测）；
  · 界面那一段用 offscreen QPA 装配主窗口但**不 show**，把 `isVisible` 临时改成 True
    以让 `_apply_topmost` 走到调用点，同时把 `win32.set_topmost` 换成记录器——
    验的是"谁在什么时候喊了它、喊的是什么"，真 Z 序由前一段负责。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GELU_DATA", os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".selftest-data-topmost")))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QObject, QPoint, pyqtSignal                # noqa: E402
from PyQt5.QtWidgets import QApplication                           # noqa: E402

from app import config as cfgmod                                   # noqa: E402
from app import win32                                               # noqa: E402
from app.ui import keymap                                          # noqa: E402
from app.ui import main_window as MW                               # noqa: E402
from app.ui import menus                                           # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def win32_probe() -> None:
    """一个 4×4、坐标 −20000 的**屏幕外**工具窗：真 HWND、不上任何显示器。

    为什么要 show 一次（实测 2026-10-31，tests 里的这条探针）：对**不可见**窗口
    调 SetWindowPos(HWND_TOPMOST) 虽然回 TRUE，`WS_EX_TOPMOST` 位却不会落上
    （ex 一直是 0x100）。置顶是给"已经看得见的窗口"排 Z 序的，这一点只影响探针的
    搭法、不影响产品：产品里 `_apply_topmost` 只在 isVisible() 时动手。
    WS_EX_TOOLWINDOW 让它不进任务栏/Alt+Tab，SW_SHOWNA 不抢焦点，坐标在屏幕外
    ⇒ 用户看不到任何东西（"不要用前台屏幕"那条约束）。
    """
    u = ctypes.windll.user32
    u.CreateWindowExW.restype = wt.HWND
    u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
    u.DestroyWindow.argtypes = [wt.HWND]
    u.DestroyWindow.restype = wt.BOOL
    u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u.ShowWindow.restype = wt.BOOL
    WS_POPUP = 0x80000000
    WS_EX_TOOLWINDOW = 0x00000080
    SW_SHOWNA = 8
    h = u.CreateWindowExW(WS_EX_TOOLWINDOW, "STATIC", "gelu-topmost-probe", WS_POPUP,
                          -20000, -20000, 4, 4, None, None, None, None)
    if not h:
        check("Win32：建一个屏幕外工具窗（CreateWindowExW）", False,
              f"GetLastError={ctypes.get_last_error()}")
        return
    try:
        u.ShowWindow(wt.HWND(h), SW_SHOWNA)
        first = win32.is_topmost(int(h))
        ok_on = win32.set_topmost(int(h), True)
        after_on = win32.is_topmost(int(h))
        ok_off = win32.set_topmost(int(h), False)
        after_off = win32.is_topmost(int(h))
        check("Win32：置顶开关真的翻 WS_EX_TOPMOST 位，关掉也真回得来",
              first is False and ok_on and after_on is True and ok_off and after_off is False,
              f"初值={first} set(True)={ok_on}→{after_on} set(False)={ok_off}→{after_off}")
        check("Win32：空句柄不崩、返回 False（窗口还没建好时的调用路径）",
              win32.set_topmost(0, True) is False and win32.is_topmost(0) is False,
              "hwnd=0")
    finally:
        u.DestroyWindow(h)


class _Act:
    def __init__(self, text):
        self.text = text
        self.data = None
        self.checkable = False
        self.checked = False

    def setData(self, data):
        self.data = data

    def setCheckable(self, on):
        self.checkable = bool(on)

    def setChecked(self, on):
        self.checked = bool(on)


class _Menu:
    """QMenu.exec 是阻塞的；右键菜单这一段只核对"画出来的勾选态"。"""

    def __init__(self, _parent=None):
        self.acts = []

    def addAction(self, text):
        a = _Act(text)
        self.acts.append(a)
        return a

    def addSeparator(self):
        return None

    def exec(self, *_a):
        return None


class _SilentMenu(QObject):
    triggered = pyqtSignal(object)

    def exec(self, *_a):
        return None


def ui_probe(cfg) -> None:
    win = MW.MainWindow(cfg, files=[])
    seen: list[str] = []
    win._osd = lambda text: seen.append(text)       # 不贴到屏幕上，只记账

    calls: list[tuple[int, bool]] = []
    real_set = win32.set_topmost
    win32.set_topmost = lambda hwnd, on: calls.append((int(hwnd), bool(on))) or True
    MW.MainWindow.isVisible = lambda self: True       # 只看"喊没喊"，窗口不 show
    real_qmenu = MW.QMenu
    try:
        check("构造期不喊内核也不碰 Z 序（配置 False 时不置顶）",
              win._topmost is False and calls == [], f"_topmost={win._topmost} calls={calls}")

        win._apply_topmost()
        check("_apply_topmost 按当前状态喊 set_topmost(False)",
              calls == [(int(win.winId()), False)], f"{calls}")

        # ① 开关本身：状态、配置、落盘、OSD 四样一起动
        win.set_topmost(True)
        disk = json.load(open(cfg.path, "r", encoding="utf-8"))
        check("置顶：_topmost=True、cfg 与盘上都是 True、气泡「窗口已置顶」",
              win._topmost is True and cfg["always_on_top"] is True
              and disk.get("always_on_top") is True and seen[-1:] == ["窗口已置顶"]
              and calls[-1] == (int(win.winId()), True),
              f"_topmost={win._topmost} cfg={cfg['always_on_top']} disk={disk.get('always_on_top')} "
              f"osd={seen[-1:]!r} calls={calls[-1:]}")

        # ② 全屏切换会重排 Z 序：_apply_mode 必须把置顶态再喊一次
        n = len(calls)
        win._apply_mode()
        # 不 show 的探针里 chrome.showNormal() 会补一次 showEvent，所以这里不钉"恰好一次"，
        # 只钉"重摆结束之后，最后喊出去的是当前状态、而且中途没有把它喊反"。
        check("进出全屏（_apply_mode）之后重喊当前置顶态，不会进一趟全屏就丢",
              len(calls) > n and all(on is True for _h, on in calls[n:])
              and calls[-1] == (int(win.winId()), True),
              f"新增={calls[n:]}")

        # ③ 再切一次：取消置顶也要落盘、要说
        win.toggle_topmost()
        disk = json.load(open(cfg.path, "r", encoding="utf-8"))
        check("取消置顶：状态/盘上都回 False、气泡「已取消置顶」",
              win._topmost is False and disk.get("always_on_top") is False
              and seen[-1] == "已取消置顶" and calls[-1] == (int(win.winId()), False),
              f"disk={disk.get('always_on_top')} osd={seen[-1:]!r}")

        # ④ 快捷键那条路：dispatch("always_on_top") 与菜单同源
        win.dispatch("always_on_top")
        check("dispatch(always_on_top) 走同一支 toggle（Shift+W 与菜单同源）",
              win._topmost is True and seen[-1] == "窗口已置顶", f"osd={seen[-1:]!r}")

        # ⑤ 右键菜单：置顶项可勾选，勾选态读 _topmost
        def grab_context() -> _Act:
            box: list[_Menu] = []

            class _CaptureMenu(_Menu):
                def __init__(self, parent=None):
                    super().__init__(parent)
                    box.append(self)

            MW.QMenu = _CaptureMenu
            win._popup_context(QPoint(10, 10))
            acts = box[0].acts if box else []
            return next((a for a in acts if a.data == "always_on_top"), None)

        top_on = grab_context()
        win.set_topmost(False)
        top_off = grab_context()
        check("右键菜单里的「窗口置顶」可勾选，勾选态跟着当前状态走",
              top_on is not None and top_on.checkable and top_on.checked
              and top_off is not None and top_off.checkable and top_off.checked is False,
              f"置顶时={top_on is not None and top_on.checkable}/{top_on and top_on.checked}；"
              f"取消后={top_off is not None and top_off.checkable}/{top_off and top_off.checked}")

        # ⑥ "更多"菜单：extra 第三位只给置顶项，且真画成勾选项
        win.set_topmost(True)
        grabbed: dict = {}
        real_more = menus.more_menu

        def spy(cfg_, parent, extra=None, submenus=None):
            grabbed["extra"] = extra
            grabbed["menu"] = real_more(cfg_, parent, extra=extra, submenus=submenus)
            return _SilentMenu()

        menus.more_menu = spy
        try:
            win._show_menu("more", win.bar)
        finally:
            menus.more_menu = real_more
        extra = grabbed.get("extra") or []
        by_act = {e[1][1]: e for e in extra if len(e) > 2 and isinstance(e[1], tuple)}
        only_top = (by_act.get("always_on_top", (None, None, None))[2] is True
                    and all(e[2] is None for a, e in by_act.items() if a != "always_on_top"))
        real_actions = grabbed["menu"].actions()
        real_top = next((a for a in real_actions
                         if a.data() == ("do", "always_on_top")), None)
        check("「更多」菜单：置顶项勾选态=当前值，且它是唯一被画成勾选项的一条",
              only_top and real_top is not None and real_top.isCheckable()
              and real_top.isChecked(),
              f"extra={[(e[0], e[1], e[2]) for e in extra]} "
              f"real={real_top.isCheckable() if real_top else None}/{real_top.isChecked() if real_top else None}")

        # ⑦ 快捷键表：Shift+W 落在 always_on_top，且全表没有新的撞键/死键
        rows = keymap.effective(cfg["keymap"])
        hits = [s for s, a, _d in rows if a == "always_on_top"]
        check("快捷键表：Shift+W → always_on_top，且这张表装得上（无死串/撞键）",
              hits == ["Shift+W"] and keymap.valid("Shift+W")
              and keymap.problems(rows) == [],
              f"hits={hits} problems={keymap.problems(rows)}")
    finally:
        MW.QMenu = real_qmenu
        del MW.MainWindow.isVisible        # 类上没有自己的同名属性，删掉就回到继承的实现
        win32.set_topmost = real_set
        win.deleteLater()
        QApplication.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    os.makedirs(cfgmod.DATA_DIR, exist_ok=True)
    cfg = cfgmod.Config()
    cfg["always_on_top"] = False
    cfg["resume"] = False
    cfg.save()

    win32_probe()
    ui_probe(cfg)

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n---- 汇总：{len(results) - n_fail}/{len(results)} 通过"
          + (f"；失败：{[n for n, ok, _ in results if not ok]}" if n_fail else "") + " ----",
          flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
