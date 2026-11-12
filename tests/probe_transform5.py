# 一次性测量（不留进验收）：确定"能真的动像素的滤镜链长什么样"，并量代价。
# probe4 的结论：本机 hwdec=auto-safe（d3d11va）下，`hflip / unsharp / cas / hqdn3d /
# rotate` 单独发下去**内核会把滤镜整条禁用**（"Disabling filter …"＋"Impossible to
# convert…"），但 `vf` 列表里名字照旧在——所以列表不能当证据。加上 `format=yuv420p`
# 打头之后 unsharp 才真的生效（边缘能量 1.400→2.184），`crop` 则不需要前缀。
# 这一轮把每条要上架的链都按同样标准过一遍：无报错 + 像素真的动 + 代价数字。
# 跑法：python tests/probe_transform5.py   （> tests/out/probe-transform5.log）
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                        # noqa: E402
from PIL import Image                     # noqa: E402
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app import config as cfgmod          # noqa: E402
from app.engine.player import MpvPlayer   # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
CLIP4K = os.path.join(HERE, "out", "tmp-4k.mp4")
OUT = os.path.join(HERE, "out", "probe-frames")
os.makedirs(OUT, exist_ok=True)
APP = QCoreApplication([])
BAD = re.compile(r"Disabling filter|failed to configure|Failed to configure|not found|"
                 r"Impossible to convert|Could not set AVOption|Creating filter .* failed|"
                 r"No such filter", re.I)


def pump(s=0.1):
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def gray(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)


def edge(a):
    return float((np.abs(np.diff(a, axis=1)).mean() + np.abs(np.diff(a, axis=0)).mean()) / 2)


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
    path = os.path.join(OUT, f"p5-{tag}.png")
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


def mk4k():
    if os.path.exists(CLIP4K):
        return True
    ff = cfgmod.FFMPEG_EXE
    if not os.path.exists(ff):
        return False
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=25:duration=6",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                        "-pix_fmt", "yuv420p", "-an", CLIP4K],
                       capture_output=True, timeout=300)
    if r.returncode:
        print("4K 样片生成失败:", r.stderr.decode("utf-8", "replace")[-300:])
    return os.path.exists(CLIP4K)


def main():
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    if not p.start(hwnd=0):
        return 1
    print(f"# {p.version} hwdec={p.get('hwdec')}")
    p.open_files([SAMPLE], remember=False)
    pump(1.6)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(1.0)

    def clear():
        p._req(["change-list", "vf", "set", ""], timeout=3)
        pump(0.4)

    def listed():
        v = p.get("vf", [])
        return ",".join(str(x.get("name")) for x in v) if isinstance(v, list) else str(v)

    clear()
    bp = shot(p, "base")
    base = gray(bp)
    print(f"基线 {base.shape} 边缘能量 {edge(base):.3f}\n")

    CHAINS = [
        ("hflip_fmt", "format=yuv420p,hflip"),
        ("vflip_fmt", "format=yuv420p,vflip"),
        ("unsharp_lo", "format=yuv420p,unsharp=5:5:0.5:5:5:0.0"),
        ("unsharp_hi", "format=yuv420p,unsharp=5:5:2.0:5:5:0.0"),
        ("cas_fmt", "format=yuv420p,cas=strength=0.5"),
        ("hqdn3d_fmt", "format=yuv420p,hqdn3d=4:3:6:4.5"),
        ("nlmeans_fmt", "format=yuv420p,nlmeans=s=3"),
        ("deband_fmt", "format=yuv420p,deband=1thr=0.02:2thr=0.02:3thr=0.02:range=16"),
        ("crop_only", "crop=1000:600:140:60"),
        ("crop_expr", "crop=out_w=1000:out_h=600:x=140:y=60"),
        ("two_filters", "format=yuv420p,hflip,unsharp=5:5:1.0:5:5:0.0"),
        ("bare_format", "format=yuv420p"),
    ]
    print("链                       内核报错            逐像素差   边缘能量  列表")
    for tag, expr in CHAINS:
        clear()
        off = logsize(p)
        p._req(["change-list", "vf", "set", expr], timeout=3)
        pump(0.9)
        path = shot(p, tag)
        bad = sorted({m.group(0)[:34] for m in BAD.finditer(new_log(p, off))})
        if not path:
            print(f"  {tag:24s} 截图失败 {bad}")
            continue
        a = gray(path)
        d = float(np.abs(a - base).mean()) if a.shape == base.shape else float("nan")
        # 镜像专门加一条不可伪造的证据：翻转后与基线的差应远小于不翻转的差
        extra = ""
        if tag.startswith("hflip"):
            extra = f" 镜像吻合度={float(np.abs(a - np.fliplr(base)).mean()):.3f} vs 原样={d:.3f}"
        if tag.startswith("vflip"):
            extra = f" 镜像吻合度={float(np.abs(a - np.flipud(base)).mean()):.3f} vs 原样={d:.3f}"
        print(f"  {tag:24s} {('OK' if not bad else str(bad))[:20]:20s} {d:9.2f} {edge(a):9.3f} "
              f"[{listed()}]{extra}")
    clear()

    # 与变换属性的叠加：vf 里有 format 时，旋转/缩放/宽高比还管不管用
    print("\n叠加检查（vf=format=yuv420p,hflip 之上再动属性）：")
    p._req(["change-list", "vf", "set", "format=yuv420p,hflip"], timeout=3)
    pump(0.6)
    for prop, val in (("video-rotate", 90), ("panscan", 0.4), ("video-aspect-override", "4:3"),
                      ("video-aspect-override", "-1"), ("panscan", 0.0), ("video-rotate", 0)):
        p.set_prop(prop, val)
        pump(0.5)
        print(f"  {prop}={val} -> 回报 {p.get(prop)}")
    p.set_prop("video-rotate", 0)
    p.set_prop("panscan", 0.0)
    clear()

    # 换片之后滤镜链还在不在（决定"重开一片要不要补发"）
    print("\n换文件后：")
    p.open_files([os.path.join(HERE, "samples", "gelu-sample-2.mp4")], remember=False)
    pump(1.2)
    p._req(["change-list", "vf", "set", "format=yuv420p,unsharp=5:5:1.0:5:5:0.0"], timeout=3)
    pump(0.6)
    p.open_files([SAMPLE], append=False, remember=False)
    pump(1.6)
    print(f"  重新 loadfile 之后 vf = [{listed()}]")

    # 代价：720p 与 4K
    print("\n代价（estimated-vf-fps；720p 样片 25fps、4K 片段 25fps 为满）：")
    for clip, label in ((SAMPLE, "720p"), (CLIP4K if mk4k() else None, "4K")):
        if not clip:
            print("  4K 片段没生成，跳过")
            continue
        p.open_files([clip], append=False, remember=False)
        pump(2.0)
        p.set_prop("pause", False)
        for tag, expr in (("（无滤镜）", ""), ("format+hflip", "format=yuv420p,hflip"),
                          ("format+unsharp", "format=yuv420p,unsharp=5:5:1.0:5:5:0.0"),
                          ("format+hqdn3d", "format=yuv420p,hqdn3d=4:3:6:4.5"),
                          ("format+nlmeans", "format=yuv420p,nlmeans=s=3"),
                          ("format+deband", "format=yuv420p,deband=1thr=0.02:range=16"),
                          ("crop", "crop=1000:600:140:60")):
            p._req(["change-list", "vf", "set", expr], timeout=3)
            pump(3.2)
            s = p.stats_snapshot()
            fps = s.get("estimated-vf-fps")
            drop = s.get("decoder-frame-drop-count")
            print(f"  {label:5s} {tag:16s} fps={fps} 丢帧={drop} 硬解={p.get('hwdec-current')}")
        p._req(["change-list", "vf", "set", ""], timeout=3)
        p.set_prop("pause", True)
    p.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
