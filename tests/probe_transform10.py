# 一次性测量（结论进 test_transform.py）：把 mpv **嵌进一块固定尺寸的窗口**之后，
# `screenshot-to-file window` 能不能照出 letterbox（黑边），从而给 keepaspect / panscan
# 这两项在 --wid 0 的跑法下"结构性看不见"的项拿到像素证据。
# probe9 的教训：内核自建窗口会跟着片子比例自动改尺寸（16:9 的片子塞不进 4:3 的框），
# 于是"保持比例"永远没有可比的边框——panscan 也就永远没有该被裁的东西。
# 所以这一轮只回答三件事：①嵌进去之后 window 模式出不出帧（不显示会不会黑）；
# ②黑边占比随 keepaspect / panscan 怎么变；③需不需要真显示、需不需要 topmost。
# 跑法：python tests/probe_transform10.py  （> tests/out/probe-transform10.log）
import ctypes
import os
import sys
import time
import ctypes.wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                          # noqa: E402
from PIL import Image                       # noqa: E402
from PyQt5.QtCore import Qt                 # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from app import config as cfgmod            # noqa: E402
from app.engine.player import MpvPlayer     # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
OUT = os.path.join(HERE, "out", "probe-frames")
os.makedirs(OUT, exist_ok=True)
APP = QApplication.instance() or QApplication(sys.argv[:1])
user32 = ctypes.windll.user32


def pump(s=0.1):
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def shot(p, tag, mode="window"):
    path = os.path.join(OUT, f"p10-{tag}.png")
    if os.path.exists(path):
        os.remove(path)
    p._req(["screenshot-to-file", path.replace("\\", "/"), mode], timeout=8)
    end = time.monotonic() + 8
    while time.monotonic() < end:
        pump(0.08)
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            time.sleep(0.25)
            return path
    return ""


def black_rows(path):
    """逐行/逐列的近黑占比剖面：黑边就是"整条都是 0 附近"的那一段。"""
    if not path:
        return None
    a = np.asarray(Image.open(path).convert("L"), dtype=np.int16)
    dark = (a < 12)
    return {"size": (a.shape[1], a.shape[0]),
            "all_dark": float(dark.mean()),
            "row_dark": [round(float(x), 3) for x in dark.mean(axis=1)][::40],
            "col_dark": [round(float(x), 3) for x in dark.mean(axis=0)][::40]}


def main():
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    host = QWidget(None)
    host.setWindowFlags(Qt.Window)
    host.resize(1280, 720)              # 16:9 的框，片也是 16:9 —— 想照出黑边就得改比例
    host.show()
    pump(0.6)
    for st in ("显示", "最小化"):
        if st == "最小化":
            host.showMinimized()
            pump(0.5)
        p = MpvPlayer(cfg)
        hwnd = int(host.winId())
        ok = p.start(hwnd=hwnd)
        time.sleep(0.3)
        p.open_files([SAMPLE], remember=False)
        pump(2.2)
        p.set_prop("time-pos", 3.0)
        p.set_prop("pause", True)
        pump(0.8)
        v = shot(p, f"{st}-video", "video")
        w = shot(p, f"{st}-window", "window")
        print(f"[{st}] start={ok} hwnd={hwnd} "
              f"mpv子窗={user32.IsWindowVisible(int(p.get('window-id') or 0))} "
              f"video={black_rows(v)} window={black_rows(w)}")
        if st == "显示":
            for tag, kv in (("keep=yes ar=no", {"keepaspect": True, "video-aspect-override": "no"}),
                           ("keep=yes ar=4:3", {"keepaspect": True, "video-aspect-override": "4:3"}),
                           ("keep=no  ar=4:3", {"keepaspect": False, "video-aspect-override": "4:3"}),
                           ("keep=yes ar=4:3 ps=1", {"keepaspect": True,
                                                    "video-aspect-override": "4:3",
                                                    "panscan": 1.0}),
                           ("keep=yes ar=1:1 ps=0", {"keepaspect": True,
                                                     "video-aspect-override": "1:1",
                                                     "panscan": 0.0}),
                           ("keep=yes ar=1:1 ps=1", {"keepaspect": True,
                                                     "video-aspect-override": "1:1",
                                                     "panscan": 1.0})):
                for k, val in kv.items():
                    p.set_prop(k, val)
                pump(1.0)
                print(f"   {tag:22s} window剖面={black_rows(shot(p, 'sweep-' + tag.replace(' ', '_')))}")
        p.quit()
        pump(0.6)
        if st == "最小化":
            host.showNormal()
            pump(0.4)
    host.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
