# -*- coding: utf-8 -*-
"""窗口几何记忆的离线验收：纯规划器 + 真窗口接线 + 证伪（`QT_QPA_PLATFORM=offscreen`）。

这一条守的是四件用户能直接碰到的事：
  ① 关窗再开，位置/大小/最大化回来；首次启动按主屏居中，不蹲 Windows 默认位；
  ② 上次那块显示器被拔掉/分辨率降档，窗口必须**整窗**收回屏幕内（不是只露标题栏）；
  ③ 多屏负坐标（左屏）是合法值，不许当成"坏数据"扔回主屏；
  ④ 拖动停下才写盘，拖动途中不刷配置；保存失败不许从 closeEvent 里穿出去（PyQt5 abort）。

测不到什么：真窗口管理器的最大化动画、不同 DPI 屏之间的坐标换算、任务栏避让的
真实像素。这些要真机（用户空闲时另行验），本文件只签"规划 + 接线"。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = tempfile.mkdtemp(prefix="win-geom-", dir=HERE)
os.environ["GELU_DATA"] = DATA

import atexit                                             # noqa: E402
atexit.register(lambda: shutil.rmtree(DATA, ignore_errors=True))

sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtWidgets import QApplication                  # noqa: E402

from app import config as cfgmod                         # noqa: E402
from app.ui import win_geom                              # noqa: E402
from app.ui.main_window import MainWindow                # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])
_SCREENS = [("主屏", 0, 0, 1920, 1040),
            ("左屏", -2560, 0, 2560, 1400),
            ("右屏", 1920, 0, 1920, 1040)]
_SEQ = [0]


def make_cfg(rec=None):
    _SEQ[0] += 1
    cfg = cfgmod.Config(path=os.path.join(DATA, f"cfg-{_SEQ[0]}.json"))
    cfg["resume"] = False
    if rec is not None:
        cfg["win_geom"] = dict(rec)
    return cfg


def drop(w) -> None:
    try:
        w.hide()
        w.chrome.uninstall()
    finally:
        w.deleteLater()
        APP.processEvents()


def _qt_screens():
    return win_geom.from_qt(APP)


def run_legs() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, bool(ok), detail))

    # ---------- 纯规划器 ----------
    ck("P1 没有存档 → 交给调用方用出厂尺寸", win_geom.plan({}, _SCREENS) is None)
    ck("P2 正常窗口原样保留（最大化位也保留）",
       win_geom.plan({"x": 100, "y": 80, "w": 1180, "h": 720, "max": True}, _SCREENS)
       == (100, 80, 1180, 720, True))
    ck("P3 右屏之外整块跑飞 → 主屏居中（不是留在 5000）",
       win_geom.plan({"x": 5000, "y": 200, "w": 1180, "h": 720}, _SCREENS)
       == ((1920 - 1180) // 2, (1040 - 720) // 2, 1180, 720, False),
       f"got={win_geom.plan({'x': 5000, 'y': 200, 'w': 1180, 'h': 720}, _SCREENS)}")
    ck("P4 标题栏被推到屏幕上方 → 落回上沿",
       win_geom.plan({"x": 100, "y": -500, "w": 1180, "h": 720}, _SCREENS)
       == (100, 0, 1180, 720, False))
    big = win_geom.plan({"x": 100, "y": 80, "w": 5000, "h": 3000}, _SCREENS)
    # 5000 宽从 x=100 铺到 5100：与右屏交集最大（1920×960），整窗收到右屏内。
    ck("P5 比屏幕还大 → 整窗收进交集最大的那块屏（x/y 一起夹，不留半截在外）",
       big == (1920, 0, 1920, 1040, False), f"got={big}")
    ck("P6 左屏负坐标是合法的，原样留在左屏",
       win_geom.plan({"x": -2000, "y": 100, "w": 1200, "h": 700}, _SCREENS)
       == (-2000, 100, 1200, 700, False))
    ck("P7 只剩边缘一条 → 整窗拉回左屏内（不是只露 160px）",
       win_geom.plan({"x": -3700, "y": 100, "w": 1180, "h": 700}, _SCREENS)
       == (-2560, 100, 1180, 700, False),
       f"got={win_geom.plan({'x': -3700, 'y': 100, 'w': 1180, 'h': 700}, _SCREENS)}")
    ck("P8 存档比最小尺寸还小 → 抬到产品下限",
       win_geom.plan({"x": 100, "y": 80, "w": 200, "h": 100}, _SCREENS)
       == (100, 80, 880, 560, False))
    ck("P9 坏存档（0 宽 / 布尔 / 非数字串）一律当没有",
       all(win_geom.plan(v, _SCREENS) is None
           for v in ({"x": 1, "y": 2, "w": 0, "h": 400},
                     {"x": True, "y": 2, "w": 800, "h": 600},
                     {"x": "abc", "y": 2, "w": "800", "h": 600},
                     "nonsense", None)))
    ck("P9b 手改成数字串（config.json 常见）能读，但按数值规划",
       win_geom.plan({"x": "100", "y": "80", "w": "1180", "h": "720"}, _SCREENS)
       == (100, 80, 1180, 720, False))
    ck("P10 一块屏都问不到 → 不猜（None）",
       win_geom.plan({"x": 1, "y": 2, "w": 800, "h": 600}, []) is None)
    ck("P11 首次启动没有存档 → 按主屏可用区居中（不是 Windows 默认左上角）",
       win_geom.default_rect(_SCREENS) == (370, 160, 1180, 720),
       f"got={win_geom.default_rect(_SCREENS)}")
    ck("P12 屏比产品下限还小 → 窗口跟屏走，不许有一截在屏幕外",
       win_geom.plan({"x": 0, "y": 0, "w": 1180, "h": 720},
                     [("小屏", 0, 0, 800, 500)]) == (0, 0, 800, 500, False),
       f"got={win_geom.plan({'x': 0, 'y': 0, 'w': 1180, 'h': 720}, [('小屏', 0, 0, 800, 500)])}")
    ck("P13 首次启动遇上小屏 → 出厂尺寸也收进屏幕",
       win_geom.default_rect([("小屏", 0, 0, 800, 500)]) == (0, 0, 800, 500),
       f"got={win_geom.default_rect([('小屏', 0, 0, 800, 500)])}")

    # ---------- 真窗口接线 ----------
    rec = {"x": 5000, "y": 200, "w": 1180, "h": 720}
    exp = win_geom.plan(rec, _qt_screens(), (1180, 720), (880, 560))
    cfg = make_cfg(rec)
    w = MainWindow(cfg, files=[])
    try:
        w.show()
        APP.processEvents()
        ck("W1 构造期按规划器摆窗口（不是只在纯函数里对）",
           exp is not None and w.geometry().getRect() == tuple(exp[:4]),
           f"win={w.geometry().getRect()} plan={exp}")

        w.setGeometry(123, 111, 1000, 640)
        APP.processEvents()
        w._save_geometry()
        saved = cfg["win_geom"]
        ck("W2 保存的是普通态矩形，能落到盘上",
           (saved.get("x"), saved.get("y"), saved.get("w"), saved.get("h"))
           == (123, 111, 1000, 640) and os.path.exists(cfg.path),
           f"cfg={saved}")
        on_disk = json.load(open(cfg.path, encoding="utf-8"))["win_geom"]
        ck("W3 盘上那份和内存一致（不是只改内存没写盘）",
           (on_disk.get("x"), on_disk.get("y"), on_disk.get("w"), on_disk.get("h"))
           == (123, 111, 1000, 640), f"disk={on_disk}")

        # 新窗口读同一份配置：位置应该按存档回来
        w2 = MainWindow(cfg, files=[])
        try:
            w2.show()
            APP.processEvents()
            exp2 = win_geom.plan(cfg["win_geom"], _qt_screens(), (1180, 720), (880, 560))
            ck("W4 重开一个窗口读回同一份存档",
               exp2 is not None and w2.geometry().getRect() == tuple(exp2[:4]),
               f"win={w2.geometry().getRect()} plan={exp2}")
        finally:
            drop(w2)
    finally:
        drop(w)

    # 最大化存档：_start_max 只消费一次，show 之后不许再挂着
    cfg2 = make_cfg({"x": 80, "y": 60, "w": 1200, "h": 700, "max": True})
    w3 = MainWindow(cfg2, files=[])
    try:
        ck("W5 最大化存档在构造期只记成待办（未显示时不动窗口）",
           w3._start_max is True)
        w3.show()
        APP.processEvents()
        ck("W6 show 之后待办被消费掉（不会每次 show 又拽回最大化）",
           w3._start_max is False)
    finally:
        drop(w3)

    # 保存失败：只返回 False，不许抛（closeEvent 里抛 OSError = PyQt5 abort）
    bad = cfgmod.Config(path=os.path.join(DATA, "path-is-a-dir"))
    os.makedirs(bad.path, exist_ok=True)
    try:
        bad.save()
        ok_fail = False
    except BaseException as exc:                       # noqa: BLE001
        ok_fail = False
        print(f"[DEBUG] save 抛了 {type(exc).__name__}: {exc}")
    else:
        ok_fail = True
    ck("W7 保存失败（目标是目录）只记一行、不抛异常", ok_fail)

    # 关窗无条件 quit：running=False 的那一档也必须收内核（看门狗重启间隙）
    cfg3 = make_cfg({"x": 60, "y": 50, "w": 1100, "h": 680})
    w4 = MainWindow(cfg3, files=[])
    w4.show()
    APP.processEvents()
    calls: list[str] = []
    w4.player.quit = lambda: calls.append("quit")      # 只替这一步，不碰产品代码
    ck("W8 关窗前内核不在跑（running=False）作为前提", not w4.player.running)
    w4.close()
    APP.processEvents()
    ck("W9 关窗无条件 quit（重启间隙不留下孤儿内核）", calls == ["quit"], f"calls={calls}")
    drop(w4)
    return rows


# ---------------------------------------------------------------- 证伪
_BASE_PLAN = win_geom.plan
_BASE_RESTORE = MainWindow._restore_geometry


def _raw_x(saved, screens, *a, **kw):
    out = _BASE_PLAN(saved, screens, *a, **kw)
    if out and isinstance(saved, dict) and isinstance(saved.get("x"), int):
        return (saved["x"],) + tuple(out[1:])
    return out


def _raw_size(saved, screens, *a, **kw):
    out = _BASE_PLAN(saved, screens, *a, **kw)
    if out and isinstance(saved, dict):
        return (out[0], out[1], int(saved.get("w", out[2])), int(saved.get("h", out[3])), out[4])
    return out


def _always_primary(saved, screens, *a, **kw):
    if not screens:
        return _BASE_PLAN(saved, screens, *a, **kw)
    only = [screens[0]]
    return _BASE_PLAN(saved, only, *a, **kw)


def _no_center(saved, screens, *a, **kw):
    out = _BASE_PLAN(saved, screens, *a, **kw)
    if out and isinstance(saved, dict):
        return (int(saved.get("x", out[0])), int(saved.get("y", out[1]))) + tuple(out[2:])
    return out


def _drop_max(saved, screens, *a, **kw):
    out = _BASE_PLAN(saved, screens, *a, **kw)
    return out[:4] + (False,) if out else out


def _no_restore(self):
    return None


MUTANTS = [
    ("X1 不夹横向（窗口留在屏幕外）", win_geom, "plan", _raw_x),
    ("X2 不夹尺寸（5000×3000 原样交给 Qt）", win_geom, "plan", _raw_size),
    ("X3 永远挑主屏（左屏用户被拽回来）", win_geom, "plan", _always_primary),
    ("X4 屏外也不居中（留 5000,200）", win_geom, "plan", _no_center),
    ("X5 丢掉最大化位（重开变小窗）", win_geom, "plan", _drop_max),
    ("X6 构造期不恢复（存档形同虚设）", MainWindow, "_restore_geometry", _no_restore),
]


@contextmanager
def patched(cls, attr, fn):
    before = getattr(cls, attr)
    setattr(cls, attr, fn)
    try:
        yield
    finally:
        setattr(cls, attr, before)


def falsify() -> int:
    base = run_legs()
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
                print(f"[NOP] {name}：注入后还是同一个对象（变异失效）")
                continue
            rows = run_legs()
        red = [n for n, ok, _ in rows if not ok and n not in base_red]
        if red:
            caught += 1
            print(f"[CAUGHT] {name} → {red[0]}")
        else:
            missed += 1
            print(f"[MISSED] {name}：同一套判据没翻红")
    bad = missed + nop + (0 if ok_base else 1)
    print(f"[FINAL] 窗口几何证伪：共 {len(MUTANTS)} 条坏实现，判不到的（MISSED/NOP）{missed + nop} 条"
          f"，基线{'绿' if ok_base else '红'} ⇒ rc={1 if bad else 0}")
    return 1 if bad else 0


def main() -> int:
    if "--falsify" in sys.argv:
        return falsify()
    rows = run_legs()
    for name, ok, detail in rows:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ｜ {detail}" if detail and not ok else ""))
    red = [n for n, ok, _ in rows if not ok]
    print(f"[FINAL] 窗口几何：{len(rows) - len(red)}/{len(rows)} 通过；红 {len(red)} 条")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
