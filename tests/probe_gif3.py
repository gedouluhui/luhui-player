"""一次性诊断探针（不看像素判定产品，只量三件事到底怎么发生的）。

跑法：`python tests/probe_gif3.py`（屏幕醒着才跑得动第 4 步的按键，其它步都能跑）

要回答的三个问题——真机那三条红各自属于"产品坏了"还是"判据前提不成立"：
  A 建完窗口那一刻 `player.running`/`version` 是什么状态？如果几秒后才亮，那就是
    **懒启动设计**（`ensure_engine` 文档原话：窗口先出现，真的要放东西时才起 mpv），
    那条判据写的"随窗口启动成功"就是问错了对象。
  B 截图工坊开着的时候，主窗的快捷键还能不能命中？分四种情形量：
    ①工坊没开 ②工坊开着且是活动窗口 ③工坊开着但把主窗 activateWindow 回来
    ④直接把事件递给工坊本身。QShortcut 是 WindowShortcut 上下文，命中与否取决于
    **焦点在哪个窗口**，不在我把事件 send 给了谁——这一步就是来看这个的。
  C "长一趟"到底长不长：6 s/640w/15f 与 10 s/960w/20f 各跑一遍，每 0.1 s 记一次
    盘上活着 ffmpeg 的 pid，看它在 0.7 s 的取样窗口里是不是一闪而过。

产物只打印到 stdout（看门狗式落盘交给调用方重定向），不改任何产品代码。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ["GELU_DATA"] = os.path.abspath(os.path.join(HERE, ".selftest-data-probe3"))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtCore import Qt                                          # noqa: E402
from PyQt5.QtTest import QTest                                     # noqa: E402
from PyQt5.QtWidgets import QApplication                           # noqa: E402

from app import config as cfgmod                                   # noqa: E402
import test_gif_real as GR                                         # noqa: E402
import test_ui as TU                                               # noqa: E402


def line(s: str) -> None:
    print(s, flush=True)


def main() -> int:
    if not os.path.exists(GR.SAMPLE):
        line(f"[ENV] 缺样片 {GR.SAMPLE}")
        return 2
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    TU.APP = app
    ok_health, d_health = TU.capture_health()
    line(f"[STEP] 抓屏通路：{'可用' if ok_health else '不可用'} —— {d_health}")
    line("[STEP] 这份探针量的三件事没有一件看像素（按键命中走的是 Qt 的窗口激活与焦点，"
         "不是屏幕上有没有光），所以显示器睡着也照跑；只在回执里把这一行标出来，"
         "免得下次复跑的人以为「屏幕醒了才有效」。")

    cfg = TU.make_cfg()
    cfg["screenshot_dir"] = os.path.join(cfgmod.DATA_DIR, "shots")
    cfg["burst_count"], cfg["burst_gap_ms"] = (2, 150)
    cfg["resume"] = False
    cfg.save()
    TU.theme.install(app, str(cfg["theme_accent"]))

    # ---------------- A：建窗那一刻内核状态 ----------------
    win = TU.build_window(cfg)
    running0, ver0 = win.player.running, win.player.version
    t0 = time.monotonic()
    dt_run = dt_ver = None
    while time.monotonic() - t0 < 8.0:
        if dt_run is None and win.player.running:
            dt_run = time.monotonic() - t0
        if dt_ver is None and str(win.player.version or ""):
            dt_ver = time.monotonic() - t0
        if dt_run is not None and dt_ver is not None:
            break
        TU.pump(0.05)
    line(f"[A] 建窗返回那一刻：running={running0} version={ver0!r}")
    line(f"[A] 轮询 8 秒：running 变真用时={dt_run and round(dt_run, 2)}s "
         f"version 非空用时={dt_ver and round(dt_ver, 2)}s 此刻 version={win.player.version!r}")
    line(f"[A] 此刻 player.running={win.player.running}（懒启动的话这里就该是 False）")

    # ---------------- 播起来，顺便看 duration 什么时候有 ----------------
    GR.wire_hits(win)
    win.add_paths([GR.SAMPLE])
    loaded = TU.wait_for(lambda: "gelu-sample-1" in str(win.player.prop("path", "")), 12.0)
    dur_when_path = win.player.prop("duration")
    dt_dur = None
    t1 = time.monotonic()
    while time.monotonic() - t1 < 8.0:
        if float(win.player.prop("duration", 0) or 0) > 0:
            dt_dur = time.monotonic() - t1
            break
        TU.pump(0.05)
    line(f"[A2] path 命中={loaded} 那一刻 duration={dur_when_path!r}；"
         f"再过 {dt_dur and round(dt_dur, 2)}s duration 才={win.player.prop('duration')!r}")

    # ---------------- B：工坊开合与按键命中 ----------------
    def press(w, seq: str) -> list[str]:
        key, mods = TU.key_event_parts(seq)
        GR.HITS.clear()
        QTest.keyClick(w, Qt.Key(int(key)), Qt.KeyboardModifier(int(mods)))
        TU.pump(0.12)
        return list(GR.HITS)

    hits_nodlg = press(win, "B")
    TU.pump(1.2)                              # 让这一轮连拍跑完，别挂着进下一步
    line(f"[B1] 工坊没开、事件递给主窗：命中的动作={hits_nodlg} "
         f"活动窗口={type(app.activeWindow()).__name__}")

    hits_g = press(win, "G")
    dlg = win.gif_dlg
    line(f"[B2] 事件递给主窗按 G：命中的动作={hits_g} 工坊可见={dlg is not None and dlg.isVisible()} "
         f"活动窗口={type(app.activeWindow()).__name__}")
    hits_dlg_open = press(win, "B")
    fw = app.focusWidget()
    line(f"[B3] 工坊开着、事件仍递给主窗按 B：命中的动作={hits_dlg_open} "
         f"活动窗口={type(app.activeWindow()).__name__} 焦点控件={type(fw).__name__ if fw else None}")
    win.activateWindow()
    TU.pump(0.2)
    hits_back = press(win, "B")
    line(f"[B4] 把主窗 activateWindow 回来再按 B：命中的动作={hits_back} "
         f"活动窗口={type(app.activeWindow()).__name__}")
    hits_on_dlg = press(dlg, "B")
    line(f"[B5] 焦点在工坊、事件直接递给工坊本身按 B：命中的动作={hits_on_dlg} "
         f"活动窗口={type(app.activeWindow()).__name__}")

    # ---------------- B6/B7：焦点落在输入件上时，键还能不能冒到对话框 ----------------
    # 这条决定修法：如果 QSpinBox 把 'B' 吃掉（可打印字符它会先收下再让验证器拒绝），
    # 那么"在对话框的 keyPressEvent 里兜底派发给主窗"这条路对**数字/标点**根本走不通，
    # 只能装镜像快捷键；如果它能冒到对话框，兜底派发就够了。
    seen: list[str] = []
    real_kp = type(dlg).keyPressEvent

    def kp(ev):
        seen.append(f"{ev.text()!r}/{ev.key()}")
        return real_kp(dlg, ev)

    dlg.keyPressEvent = kp
    dlg.activateWindow()
    TU.pump(0.15)
    line(f"[B6/B7 前置] 把工坊重新变回活动窗口：activeWindow={type(app.activeWindow()).__name__}")
    for who, w in (("数字框 spin_dur", dlg.spin_dur), ("按钮 btn_go", dlg.btn_go),
                   ("下拉 combo_w", dlg.combo_w)):
        seen.clear()
        w.setFocus()
        TU.pump(0.05)
        h_letter = press(dlg, "B")
        line(f"[B6] 焦点={who}（focusWidget={type(app.focusWidget()).__name__}）"
             f"递 'B'：对话框收到={seen} 命中的动作={h_letter}")
        seen.clear()
        h_digit = press(dlg, "1")
        line(f"[B7] 焦点={who} 递 '1'：对话框收到={seen} 命中的动作={h_digit}")
    line(f"[B8] 对话框自己的键：Esc 会不会关掉它={dlg.isVisible()}（此刻还开着）")

    # ---------------- C：两档"长一趟"的活着时长 ----------------
    def one_export(start: float, dur: float, w: int, fps: int) -> str:
        dlg.spin_start.setValue(start)
        dlg.spin_dur.setValue(dur)
        GR.pick_combo(dlg.combo_w, w)
        GR.pick_combo(dlg.combo_f, fps)
        dlg.chk_sub.setChecked(False)
        GR.results.clear()
        t = time.monotonic()
        tl: list[str] = []
        dlg.btn_go.click()
        while time.monotonic() - t < 6.0:
            pids = GR.ffmpeg_here()
            tl.append(f"{round(time.monotonic() - t, 2)}s:{len(pids)}")
            if pids and time.monotonic() - t > 1.2:
                break
            TU.pump(0.1)
        alive = [s for s in tl if not s.endswith(":0")]
        dlg.btn_stop.click() if alive else None
        TU.pump(0.4)
        return f"取样 {' '.join(tl[:14])}"

    line(f"[C1] 6s/640w/15f —— {one_export(0.0, 6.0, 640, 15)}")
    line(f"[C2] 10s/960w/20f —— {one_export(0.0, 10.0, 960, 20)}")

    win.gif.cancel()
    TU.pump(0.3)
    win.close()
    TU.pump(0.3)
    left = GR.ffmpeg_here()
    line(f"[END] 关窗后残留 ffmpeg={left}（非零说明探针自己没收拾干净）")
    line("[NOTE] 探针没读过任何判据结论，上面每一行都是当场量的数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
