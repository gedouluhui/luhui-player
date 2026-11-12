"""探针（一次性证据，不是验收）：`screenshot` 命令到底在哪一种组合下失败。

为什么开这一份：屏幕亮后的首轮 `tests/test_ui.py` 完整验收里，四条红全部来自同一句
——`win.dispatch("screenshot")` 之后内核回 `error running command: ['screenshot', 'subtitles']`，
shots 目录零新增（logs/test_ui-screen-10100630.log:140-151）。讲得通的成因有三种，
而它们的修法完全不同，不能靠"看着像"挑一种修：

  A **模式**：`subtitles` 这一档在嵌窗（--wid）+ 硬解下不走，而 `video`/`window` 走
     （`test_engine.py` 那条同样用 include_sub=True 的判据是**独立窗口**跑的，它绿）；
  B **时序**：这一段紧跟"全屏进→全屏出"（test_ui.py:2845-2855），换链之后内核一时取不到帧；
  C **环境**：那一刻本机有外来全屏窗口压着（同一时刻 `capture_sweep --screen` 报
     SystemSettings.exe/CoreWindow 盖住整屏），会话不可见时合成器不给帧。

所以这里把三个轴分开跑：同一台机器、同一个样片、同一份配置，
①只换模式（video / subtitles / window）→ 分辨 A；
②只换时序（常规态 vs 全屏往返之后）→ 分辨 B；
③顺手把遮挡状态量出来（自家窗口在不在最上层）→ 给 C 一个数。

判据不写在这里：这里只打印原始应答与内核 stderr 行，结论由读数决定。
用法：python tests/probe_shot_modes.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("GELU_DATA", os.path.join(ROOT, "tests", ".selftest-data"))

from PyQt5.QtWidgets import QApplication                        # noqa: E402
from app.engine.ipc import IpcError                              # noqa: E402
import tests.test_ui as TU                                       # noqa: E402

MODES = ("video", "subtitles", "window")
# 样片路径取自 test_ui 自己那份清单：探针要和它复现同一条链，另抄一份路径就等于
# 允许两边指到不同的文件上（那之后"这边绿那边红"根本没法归因）。
SAMPLES = list(TU.FILES)


def try_mode(win, mode: str) -> str:
    """直接打 `screenshot <mode>`，把内核原话带回来（不经过 snapshot 的文案加工）。"""
    before = set(os.listdir(win.cfg["screenshot_dir"]))
    t0 = time.perf_counter()
    try:
        data = win.player._ipc.request(["screenshot", mode], timeout=8.0)
        ok_after = sorted(set(os.listdir(win.cfg["screenshot_dir"])) - before)
        return (f"应答={data!r} 落盘={ok_after} "
                f"耗时={time.perf_counter() - t0:.2f}s")
    except IpcError as exc:
        return f"抛错={exc} 耗时={time.perf_counter() - t0:.2f}s"
    except BaseException as exc:                # 探针自己不许崩成"没结果"
        return f"探针异常={type(exc).__name__}:{exc}"


def main() -> int:
    for p in SAMPLES:
        if not os.path.exists(p):
            print(f"缺少样片 {p}，请先跑 python tests/gen_sample.py")
            return 2
    app = QApplication.instance() or QApplication([])
    TU.APP = app
    cfg = TU.make_cfg()
    logs: list[str] = []
    win = TU.build_window(cfg)
    win.player.sig_log.connect(lambda s: logs.append(s))
    win.add_paths(SAMPLES[:1])          # 与验收同一条入口（test_ui 用的是 add_paths）
    if not TU.wait_for(lambda: win.player.prop("path"), 10.0):
        print("样片没载入，探针跑不下去")
        return 2
    TU.pump(1.2)
    win.player.seek_abs(3.0)
    TU.pump(0.6)
    print(f"hwdec-current={win.player.prop('hwdec-current')} "
          f"vo={win.player.prop('vo')} 截图目录={cfg['screenshot_dir']}")

    print("\n==== ① 常规播放态：逐模式 ====")
    for m in MODES:
        print(f"  · {m:<9} → {try_mode(win, m)}")
        TU.pump(0.25)

    print("\n==== ② 全屏往返之后：逐模式 ====")
    win.toggle_fullscreen(True)
    TU.pump(0.8)
    win.toggle_fullscreen(False)
    TU.pump(1.0)
    for m in MODES:
        print(f"  · {m:<9} → {try_mode(win, m)}")
        TU.pump(0.25)

    print("\n==== ③ 那一刻屏幕给不给像素 / 谁压在上面 ====")
    ok_env, d_env = TU.capture_health()
    print(f"  capture_health={ok_env} {d_env}")
    img, occl = TU.capture(win, win, "整窗")
    print(f"  整窗抓屏：图={'None' if img.isNull() else f'{img.width()}×{img.height()}'} "
          f"遮挡说明={occl or '无'}")

    print("\n==== 内核原话里带 screenshot 的行 ====")
    hits = [l for l in logs if "screenshot" in l.lower()]
    for l in hits[:12]:
        print("  ↳ " + l)
    if not hits:
        print("  （一条都没有——说明内核没往 stderr 写失败原因，失败只在命令应答里）")
    win.player.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
