# 一次性测量（不留进验收）：滤镜链在**硬解开着**的机器上到底能不能真跑起来。
# probe3 的日志给出了决定性的坏消息：`lavfi=[unsharp=…]` 在本机（hwdec=d3d11va）下
# 初始化失败——内核原话 "Impossible to convert between the formats supported by the
# filter 'mpv_src_in0' and the filter 'auto_scale_0'" ＋ "Disabling filter lavfi.00"。
# 而 `vf` 列表里它照样出现，所以**只看列表会把一个根本没工作的滤镜当成开了**。
# 这一轮要回答三件事：
#   1) 哪条链能在 d3d11 硬解帧上真的初始化（日志里没有 Disabling/failed）；
#   2) 初始化之后像素有没有动（同一帧、同一位置的截图逐像素比）；
#   3) 代价（estimated-vf-fps，样片 25fps 为满）。
# 跑法：python tests/probe_transform4.py   （输出同时写 tests/out/probe-transform4.log）
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                       # noqa: E402
from PIL import Image                    # noqa: E402
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app import config as cfgmod         # noqa: E402
from app.engine.player import MpvPlayer  # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
OUT = os.path.join(HERE, "out", "probe-frames")
os.makedirs(OUT, exist_ok=True)
APP = QCoreApplication([])
BAD = re.compile(r"Disabling filter|failed to configure|not found|Impossible to convert|"
                 r"Could not set AVOption|Creating filter .* failed|No such filter", re.I)


def pump(s=0.1):
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def arr(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)


def sharp(a):
    dx = np.abs(np.diff(a, axis=1)).mean()
    dy = np.abs(np.diff(a, axis=0)).mean()
    return float((dx + dy) / 2.0)


def logsize(p):
    try:
        return os.path.getsize(p.log_path)
    except OSError:
        return 0


def new_log(p, off):
    try:
        with open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(off)
            return fh.read()
    except OSError:
        return ""


def shot(p, tag):
    path = os.path.join(OUT, f"{tag}.png")
    if os.path.exists(path):
        os.remove(path)
    p._req(["screenshot-to-file", path.replace("\\", "/"), "video"], timeout=6)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        pump(0.08)
        if os.path.exists(path) and os.path.getsize(path) > 5000:
            time.sleep(0.2)
            return path
    return ""


def main():
    if not os.path.exists(SAMPLE):
        print("缺样片", SAMPLE)
        return 2
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    msgs = []
    p.sig_log.connect(lambda s: msgs.append(s))
    if not p.start(hwnd=0):
        print("内核没起来", msgs[-5:])
        return 1
    print(f"# {p.version} hwdec={p.get('hwdec')} hwdec-current={p.get('hwdec-current')}")
    p.open_files([SAMPLE], remember=False)
    pump(1.5)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(1.0)

    def clear():
        p._req(["change-list", "vf", "set", ""], timeout=3)
        pump(0.4)

    def listed():
        v = p.get("vf", [])
        return [f"{x.get('name')}{'(off)' if not x.get('enabled') else ''}" for x in v] \
            if isinstance(v, list) else v

    clear()
    bpath = shot(p, "b4")
    if not bpath:
        print("截图通路不通")
        p.quit()
        return 1
    base = arr(bpath)
    print(f"基线：尺寸{base.shape} 边缘能量{sharp(base):.3f}\n")

    CHAINS = [
        ("unsharp_lavfi", "lavfi=[unsharp=5:5:1.0:5:5:0.0]"),
        ("unsharp_plain", "unsharp=5:5:1.0:5:5:0.0"),
        ("unsharp_hwd", "hwdownload,format=nv12,unsharp=5:5:1.0:5:5:0.0"),
        ("unsharp_hwd_yuv", "hwdownload,format=nv12,format=yuv420p,unsharp=5:5:1.0:5:5:0.0"),
        ("unsharp_fmt_first", "format=yuv420p,unsharp=5:5:1.0:5:5:0.0"),
        ("cas_hwd", "hwdownload,format=nv12,cas=strength=0.5"),
        ("cas_plain", "cas=strength=0.5"),
        ("deband", "deband=iterations=4"),
        ("deband_hwd", "hwdownload,format=nv12,deband=iterations=4"),
        ("nlmeans_hwd", "hwdownload,format=nv12,nlmeans"),
        ("hqdn3d", "hqdn3d=4:3:6:4.5"),
        ("hflip", "hflip"),
        ("crop", "crop=1000:600:140:60"),
        ("rotate90", "rotate=90"),
    ]
    print("链名                 列表                        像素       边缘能量   内核报错")
    for tag, expr in CHAINS:
        clear()
        off = logsize(p)
        p._req(["change-list", "vf", "set", expr], timeout=3)
        pump(0.9)
        path = shot(p, tag)
        bad = sorted({m.group(0)[:44] for m in BAD.finditer(new_log(p, off))})
        if not path:
            print(f"  {tag:20s} 截图失败  {bad}")
            continue
        a = arr(path)
        d = float(np.abs(a - base).mean()) if a.shape == base.shape else -1.0
        print(f"  {tag:20s} {str(listed())[:26]:26s} {d:9.2f} {sharp(a):10.3f} "
              f"{'OK' if not bad else bad}")
    clear()

    # 代价：跑起来才读得到 vf fps（暂停时内核不推）
    print("\n代价（estimated-vf-fps，样片 25fps＝满）：")
    p.set_prop("pause", False)
    pump(2.5)
    print(f"  {'（无滤镜）':20s} {p.stats_snapshot().get('estimated-vf-fps')}")
    for tag, expr in (("unsharp_hwd_yuv", "hwdownload,format=nv12,format=yuv420p,unsharp=5:5:1.0:5:5:0.0"),
                      ("cas_hwd", "hwdownload,format=nv12,cas=strength=0.5"),
                      ("deband", "deband=iterations=4"),
                      ("nlmeans_hwd", "hwdownload,format=nv12,nlmeans"),
                      ("hqdn3d", "hqdn3d=4:3:6:4.5")):
        clear()
        p._req(["change-list", "vf", "set", expr], timeout=3)
        pump(3.0)
        print(f"  {tag:20s} {p.stats_snapshot().get('estimated-vf-fps')}")
    clear()

    # 几何：滤镜把 d_w×d_h 改成什么（裁剪/旋转的回报口径要写进表）
    print("\n几何回报：")
    for tag, expr in (("crop=1000:600:140:60", "crop=1000:600:140:60"),
                      ("rotate=90", "rotate=90"),
                      ("hflip", "hflip")):
        clear()
        p._req(["change-list", "vf", "set", expr], timeout=3)
        pump(0.9)
        v = p.get("video-out-params", {}) or {}
        s = shot(p, f"geo_{tag}")
        shp = arr(s).shape if s else None
        print(f"  {tag:22s} d_w={v.get('w')} d_h={v.get('h')} 截图尺寸={shp}")
    clear()
    p.set_prop("time-pos", 3.0)
    p.quit()
    pump(0.5)
    print("\n外壳侧收到的内核消息：", msgs[:6])
    return 0


if __name__ == "__main__":
    sys.exit(main())
