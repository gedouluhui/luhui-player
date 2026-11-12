# 一次性测量（结论进 test_transform.py）：display-side 的三项（panscan / keepaspect / video-pan-x）
# 到底有没有像素证据可拿。probe8 的答案是"**video 模式截图完全不动**"——这三项发生在显示那一侧，
# 不进滤镜链输出。剩下的问号只有 window 模式：`--wid 0` 的跑法里 vo=gpu 自己建窗口，
# window 截图会不会把 panscan 的放大、keepaspect=no 的拉伸照进去。
# 跑法：python tests/probe_transform9.py   （> tests/out/probe-transform9.log）
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                         # noqa: E402
from PIL import Image                      # noqa: E402
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app import config as cfgmod           # noqa: E402
from app.engine.player import MpvPlayer    # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
OUT = os.path.join(HERE, "out", "probe-frames")
os.makedirs(OUT, exist_ok=True)
APP = QCoreApplication([])


def pump(s=0.1):
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def shot(p, tag, mode):
    path = os.path.join(OUT, f"p9-{tag}-{mode}.png")
    if os.path.exists(path):
        os.remove(path)
    p._req(["screenshot-to-file", path.replace("\\", "/"), mode], timeout=8)
    end = time.monotonic() + 8
    while time.monotonic() < end:
        pump(0.08)
        if os.path.exists(path) and os.path.getsize(path) > 5000:
            time.sleep(0.25)
            return path
    return ""


def load(path):
    if not path:
        return None
    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)


def main():
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    if not p.start(hwnd=0):
        print("内核起不来")
        return 1
    print(f"# {p.version} vo={p.get('vo')}")
    p.open_files([SAMPLE], remember=False)
    pump(2.0)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(1.0)
    print("窗口尺寸/几何：", p.get("window-id"), p.get("windows"), p.get("geometry"))
    for mode in ("video", "window", "subtitles"):
        s = shot(p, "probe-mode", mode)
        a = load(s)
        print(f"  {mode:9s} -> {None if a is None else (a.shape[1], a.shape[0])}")

    def trio(tag):
        outs = {}
        for mode in ("video", "window"):
            a = load(shot(p, tag, mode))
            outs[mode] = None if a is None else (a.shape[1], a.shape[0],
                                                 round(float(np.abs(a).mean()), 2))
        return outs

    print("\n基线（三项都回默认）：")
    for k, v in (("panscan", 0.0), ("keepaspect", True), ("video-pan-x", 0.0),
                ("video-pan-y", 0.0), ("video-aspect-override", "no")):
        p.set_prop(k, v)
    pump(0.8)
    b = trio("base")
    print("  ", b)
    print("\n逐项动，看 window 模式跟不跟：")
    for prop, val in (("panscan", 0.6), ("panscan", 1.0), ("video-pan-x", 0.8),
                      ("video-pan-y", -0.8), ("video-aspect-override", "4:3"),
                      ("keepaspect", False)):
        p.set_prop(prop, val)
        pump(0.9)
        a = trio("x")
        dwin = ""
        if b["window"] and a["window"]:
            w0, h0, m0 = b["window"]
            w1, h1, m1 = a["window"]
            dwin = f" 窗口尺寸 {w0}×{h0}->{w1}×{h1} 均值 {m0}->{m1}"
        print(f"  {prop}={val}: 回报={p.get(prop)} video={a['video']} window={a['window']}{dwin}")
    p.set_prop("panscan", 0.0)
    p.set_prop("video-pan-x", 0.0)
    p.set_prop("video-pan-y", 0.0)
    p.set_prop("video-aspect-override", "no")
    p.set_prop("keepaspect", True)
    pump(0.6)
    print("\n复位之后与基线是否一致：", trio("reset"), "基线=", b)
    p.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
