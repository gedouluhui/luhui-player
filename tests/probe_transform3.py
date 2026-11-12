# 一次性测量（不留进验收）：锐化走哪条路才真的有效、每条滤镜改动能不能在像素上看出来、代价多大。
# 为什么还要第三轮探针：前两轮只证明"这个名字能不能出现在 vf 列表里"，而实测
# lavfi=[nope] 照样列表（图是假的），所以**列表出现 ≠ 滤镜真的初始化**。
# 结论只能来自像素：同一帧、同一位置、加滤镜前后各截一张，比边缘能量和平均变化。
#
# 跑法：python tests/probe_transform3.py
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                      # noqa: E402
from PIL import Image                    # noqa: E402
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app import config as cfgmod         # noqa: E402
from app.engine.player import MpvPlayer  # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
OUT = os.path.join(HERE, "out", "probe-frames")
os.makedirs(OUT, exist_ok=True)

APP = QCoreApplication([])


def pump(s=0.1):
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def gray(path):
    a = np.asarray(Image.open(path).convert("L"), dtype=np.int16)
    return a


def metrics(a):
    """(平均亮度, 标准差, 边缘能量, 形状)——边缘能量＝相邻像素差的均值，锐化会抬它，去带/降噪会压它。"""
    dx = np.abs(np.diff(a, axis=1)).mean()
    dy = np.abs(np.diff(a, axis=0)).mean()
    return float(a.mean()), float(a.std()), float((dx + dy) / 2.0), a.shape


def shot(p, tag):
    path = os.path.join(OUT, f"{tag}.png")
    if os.path.exists(path):
        os.remove(path)
    p._req(["screenshot-to-file", path.replace("\\", "/"), "video"], timeout=6)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        pump(0.1)
        if os.path.exists(path) and os.path.getsize(path) > 5000:
            time.sleep(0.25)          # 落盘写完再读，半截 PNG 会解出花
            return path
    return ""


def log_tail(p, n=6):
    try:
        with open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip() for l in fh if l.strip()]
        return lines[-n:]
    except OSError:
        return ["<读不到日志>"]


def main():
    if not os.path.exists(SAMPLE):
        print("缺样片", SAMPLE)
        return 2
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    logs = []
    p.sig_log.connect(lambda s: logs.append(s))
    if not p.start(hwnd=0):
        print("内核没起来", logs[-5:])
        return 1
    print(f"# {p.version}  样片={os.path.basename(SAMPLE)}  日志={p.log_path}\n")

    p.open_files([SAMPLE], remember=False)
    pump(1.5)
    p.set_prop("pause", True)
    p.set_prop("time-pos", 3.0)
    pump(1.2)

    def clear_vf():
        p._req(["change-list", "vf", "set", ""], timeout=3)
        pump(0.5)

    def vf(expr):
        p._req(["change-list", "vf", "set", expr], timeout=3)
        pump(1.0)

    def listed():
        v = p.get("vf", [])
        return [x.get("name") for x in v] if isinstance(v, list) else v

    # 基线
    clear_vf()
    base_path = shot(p, "base")
    if not base_path:
        print("截图通路不通，后面全部作废")
        p.quit()
        return 1
    base = gray(base_path)
    b = metrics(base)
    print(f"基线：亮{b[0]:.1f} 标准差{b[1]:.1f} 边缘能量{b[2]:.3f} 尺寸{b[3]}\n")

    CANDS = [
        ("hflip", "hflip"),
        ("vflip", "vflip"),
        ("crop640", "crop=640:360:0:0"),
        ("rot90", "rotate=90"),
        ("deband", "deband=iterations=4"),
        ("deband1", "deband=iterations=1"),
        ("nlmeans", "nlmeans"),
        ("hqdn3d", "hqdn3d=4:3:6:4.5"),
        ("lavfi_unsharp", "lavfi=[unsharp=5:5:1.5:5:5:0.0]"),
        ("lavfi_unsharp_def", "lavfi=[unsharp]"),
        ("lavfi_nope", "lavfi=[nope]"),
        ("conv_sharpen", "convolution=0w1=0:-1:0:-1:5:-1:0:-1:0"),
        ("d3d11cg_unsharp", "d3d11cg=shaders=unsharp"),
    ]
    print("候选滤镜：列表在不在 / 像素动没动 / 边缘能量怎么变")
    for tag, expr in CANDS:
        clear_vf()
        vf(expr)
        path = shot(p, tag)
        if not path:
            print(f"  {tag:20s} 截图失败")
            continue
        m = metrics(gray(path))
        d = float(np.abs(gray(path).astype(np.int32) - base.astype(np.int32)).mean()) \
            if gray(path).shape == base.shape else -1.0
        print(f"  {tag:20s} 列表={listed()} 尺寸={m[3]} 亮{m[0]:.1f} 边缘{m[2]:.3f} "
              f"(基线{b[2]:.3f}) 逐像素平均差={d:.2f}"
              + ("" if d >= 0 else "  [尺寸变了，没法逐像素比]"))
    clear_vf()

    # 代价：暂停状态下读不到 vf fps，放开 2 秒再问
    print("\n代价（estimated-vf-fps，样片 25fps 为满）：")
    p.set_prop("pause", False)
    pump(2.5)
    print(f"  {'（无滤镜）':20s} {p.stats_snapshot().get('estimated-vf-fps')}")
    for tag, expr in (("deband", "deband=iterations=4"), ("nlmeans", "nlmeans"),
                      ("hqdn3d", "hqdn3d=4:3:6:4.5"), ("lavfi_unsharp", "lavfi=[unsharp=5:5:1.5:5:5:0.0]"),
                      ("conv_sharpen", "convolution=0w1=0:-1:0:-1:5:-1:0:-1:0")):
        clear_vf()
        vf(expr)
        pump(2.5)
        print(f"  {tag:20s} {p.stats_snapshot().get('estimated-vf-fps')}")
    clear_vf()
    p.set_prop("pause", True)

    # 几何：这些属性/滤镜各自把 d_w×d_h 改成什么（UI 要按它算裁剪框）
    print("\n几何回报（video-out-params）：")

    def geo(label):
        v = p.get("video-out-params", {}) or {}
        print(f"  {label:26s} d_w={v.get('w')} d_h={v.get('h')} "
              f"aspect={v.get('aspect-override', v.get('par'))} sar={v.get('sar')}")

    p.set_prop("pause", False)
    geo("复位")
    for prop, val in (("video-aspect-override", "16:9"), ("video-aspect-override", "-1"),
                      ("keepaspect", "no"), ("panscan", 0.5), ("video-scale-x", 2.0),
                      ("video-rotate", 90), ("video-rotate", 0), ("keepaspect", "yes"),
                      ("panscan", 0.0), ("video-scale-x", 1.0)):
        p.set_prop(prop, val)
        pump(0.6)
        geo(f"{prop}={val}")
    for prop, val in (("video-aspect-override", "-1"), ("keepaspect", "yes"), ("panscan", 0.0),
                      ("video-scale-x", 1.0), ("video-scale-y", 1.0), ("video-rotate", 0)):
        p.set_prop(prop, val)
    clear_vf()

    print("\n内核日志尾部：")
    for l in log_tail(p, 14):
        print("   ", l)
    p.quit()
    pump(0.5)
    print("\n外部日志里的 filter 相关行：")
    try:
        with open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
            for l in fh:
                if any(k in l.lower() for k in ("filter", "lavfi", "unsharp", "convolution", "not found")):
                    print("   ", l.rstrip())
    except OSError as exc:
        print("   读不到:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
