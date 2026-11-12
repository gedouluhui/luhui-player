# 一次性测量（结论进 test_transform.py 的判据选择）：`video-aspect-override` 与 `keepaspect`
# 在**没有真窗口**（hwnd=0）的跑法下到底能不能用像素判。
# 问号只有一个：改比例之后，screenshot-to-file 的 video 模式出图尺寸会不会变？video-out-params
# 的 d_w/d_h 会不会变？两者都不变就意味着"这一族没有像素证据可拿"，判据只能落在内核回报值上，
# 那时必须诚实写明盲区，而不是摆一条恒真的像素对比。
# 跑法：python tests/probe_transform8.py   （> tests/out/probe-transform8.log）
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


def shot(p, tag, mode="video"):
    path = os.path.join(OUT, f"p8-{tag}-{mode}.png")
    if os.path.exists(path):
        os.remove(path)
    p._req(["screenshot-to-file", path.replace("\\", "/"), mode], timeout=6)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        pump(0.08)
        if os.path.exists(path) and os.path.getsize(path) > 5000:
            time.sleep(0.2)
            return path
    return ""


def main():
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    if not p.start(hwnd=0):
        print("内核起不来")
        return 1
    print(f"# {p.version} vo={p.get('vo')} hwdec-current={p.get('hwdec-current')}")
    p.open_files([SAMPLE], remember=False)
    pump(1.8)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(0.9)

    def readback():
        v = p.get("video-out-params", {}) or {}
        s = shot(p, f"{p.get('video-aspect-override')}_{p.get('keepaspect')}", "video")
        a = np.asarray(Image.open(s).convert("L")) if s else None
        return (f"aspect回报={p.get('video-aspect-override')} keep={p.get('keepaspect')} "
                f"d_w×d_h={v.get('w')}×{v.get('h')} sar={v.get('sar')} "
                f"video截图={None if a is None else (a.shape[1], a.shape[0])}")

    print("\n基线：", readback())
    for val in ("4:3", "1:1", "21:9", "no"):
        p.set_prop("video-aspect-override", val)
        pump(0.7)
        print(f"aspect={val:6s} ->", readback())
    p.set_prop("video-aspect-override", "4:3")
    pump(0.5)
    for keep in ("no", "yes"):
        p.set_prop("keepaspect", keep)
        pump(0.7)
        print(f"keepaspect={keep:4s} (aspect=4:3) ->", readback())
    p.set_prop("keepaspect", "yes")
    p.set_prop("video-aspect-override", "no")
    pump(0.5)
    print("\n panscan 有没有像素：")
    for ps in (0.0, 0.5, 1.0, 0.0):
        p.set_prop("panscan", ps)
        pump(0.7)
        print(f"panscan={ps} ->", readback())
    p.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
