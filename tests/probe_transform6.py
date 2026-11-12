# 一次性测量（不留进验收）：把表落地前最后四个问号钉死。
#   1) 表达式版 crop（iw/ih）能不能直接写进启动参数——能的话裁剪就不必等片子尺寸；
#   2) 旋转 90° 与裁剪链同时开，几何按谁算；
#   3) 降噪强度滑块：在**有噪声**的片段上，hqdn3d 的三档强度是否单调地把高频压下去
#      （合成样片本身没噪声，在它上面"降噪前后一样"是应该的，不能拿它当判据）；
#   4) 内核自带的 GPU 侧去带（--deband 这一族）能不能运行时改（能改就不必再挂一条滤镜）。
# 跑法：python tests/probe_transform6.py   （> tests/out/probe-transform6.log）
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
NOISY = os.path.join(HERE, "out", "tmp-noisy.mp4")
OUT = os.path.join(HERE, "out", "probe-frames")
os.makedirs(OUT, exist_ok=True)
APP = QCoreApplication([])
BAD = re.compile(r"Disabling filter|failed to configure|Failed to configure|not found|"
                 r"Impossible to convert|Could not set AVOption|Creating filter .* failed", re.I)


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


def bad_in(p, off):
    try:
        with open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(off)
            t = fh.read()
    except OSError:
        return []
    return sorted({m.group(0)[:36] for m in BAD.finditer(t)})


def shot(p, tag):
    path = os.path.join(OUT, f"p6-{tag}.png")
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


def mk_noisy():
    if os.path.exists(NOISY) and os.path.getsize(NOISY) > 10000:
        return True
    ff = cfgmod.FFMPEG_EXE
    if not os.path.exists(ff):
        return False
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", SAMPLE,
                        "-vf", "noise=alls=40:allf=t+u", "-c:v", "libx264", "-preset",
                        "ultrafast", "-crf", "20", "-an", NOISY],
                       capture_output=True, timeout=300)
    if r.returncode:
        print("噪声片段生成失败:", r.stderr.decode("utf-8", "replace")[-240:])
    return os.path.exists(NOISY)


def main():
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    if not p.start(hwnd=0):
        return 1
    print(f"# {p.version}")
    p.open_files([SAMPLE], remember=False)
    pump(1.6)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(0.9)

    def clear():
        p._req(["change-list", "vf", "set", ""], timeout=3)
        p.set_prop("video-rotate", 0)
        pump(0.4)

    def apply(expr, extra_prop=None):
        clear()
        off = logsize(p)
        if expr:
            p._req(["change-list", "vf", "set", expr], timeout=3)
        if extra_prop:
            for k, v in extra_prop:
                p.set_prop(k, v)
        pump(0.9)
        return off

    print("\n1) 表达式 crop（写进启动参数就够用，不需要先知道片子尺寸）")
    base = gray(shot(p, "base"))
    for tag, expr in (("数字 crop=1000:600:140:60", "crop=1000:600:140:60"),
                      ("表达式 iw:ih*9/16", "crop=iw:ih*9/16:0:(ih-ih*9/16)/2"),
                      ("表达式 min() 版", "crop=iw:min(ih,iw*16/9):0:(ih-min(ih,iw*16/9))/2"),
                      ("表达式+sar", "crop='trunc(iw/2)*2':'trunc(ih*9/16/2)*2':0:'(ih-trunc(ih*9/16/2)*2)/2'")):
        off = apply(expr)
        s = shot(p, tag.replace(" ", "_").replace("/", "-")[:22])
        a = gray(s) if s else None
        v = p.get("video-out-params", {}) or {}
        b = bad_in(p, off)
        print(f"  {tag:24s} 截图={None if a is None else a.shape} 回报d_w×d_h={v.get('w')}×{v.get('h')} "
              f"报错={b or 'OK'}")

    print("\n2) 旋转 90 与裁剪同时开（几何按谁算）")
    for tag, expr, props in (("只裁剪", "crop=iw:ih*9/16:0:(ih-ih*9/16)/2", None),
                             ("只旋转", "", [("video-rotate", 90)]),
                             ("裁剪+旋转", "crop=iw:ih*9/16:0:(ih-ih*9/16)/2", [("video-rotate", 90)])):
        off = apply(expr, props)
        s = shot(p, "rot_" + tag)
        a = gray(s) if s else None
        print(f"  {tag:12s} 截图尺寸={None if a is None else a.shape} "
              f"d_w×d_h={(p.get('video-out-params') or {}).get('w')}×{(p.get('video-out-params') or {}).get('h')} "
              f"rotate回报={p.get('video-rotate')} 报错={bad_in(p, off) or 'OK'}")
    clear()

    print("\n3) 降噪强度：在带噪声的片段上量（合成样片没噪声，量不出来）")
    if mk_noisy():
        p.open_files([NOISY], append=False, remember=False)
        pump(1.8)
        p.set_prop("time-pos", 3.0)
        p.set_prop("pause", True)
        pump(0.9)
        clear()
        nb = gray(shot(p, "noisy_base"))
        print(f"  噪声片段基线：边缘能量 {edge(nb):.3f}（干净样片是 1.400）")
        for s1 in (0, 2, 4, 7, 10):
            q = max(0.0, (s1 / 10.0) * 4.0)
            expr = f"format=yuv420p,hqdn3d={q:.2f}:{q * 0.75:.2f}:{q * 1.5:.2f}:{q * 1.125:.2f}"
            off = apply(expr if s1 else "")
            a = gray(shot(p, f"hqdn_{s1}"))
            print(f"  强度档 {s1:2d} -> {expr[:44]:44s} 边缘能量={edge(a):.3f} "
                  f"与基线逐像素差={float(np.abs(a - nb).mean()):.2f} 报错={bad_in(p, off) or 'OK'}")
        for s1 in (0, 4, 10):
            q = (s1 / 10.0) * 2.0
            off = apply(f"format=yuv420p,deband=1thr={0.004 * (s1 or 1):.4f}:2thr=0.02:3thr=0.02:range=16"
                        if s1 else "")
            a = gray(shot(p, f"deb_{s1}"))
            print(f"  去带档 {s1:2d} 边缘能量={edge(a):.3f} 逐像素差="
                  f"{float(np.abs(a - nb).mean()):.2f} 报错={bad_in(p, off) or 'OK'}")
        print("  锐化强度（同一噪声片段上，锐化会把噪声一起抬起来——界面措辞要诚实）")
        for amt in (0.0, 0.5, 1.0, 2.0):
            off = apply(f"format=yuv420p,unsharp=5:5:{amt}:5:5:0.0")
            a = gray(shot(p, f"uns_{amt}"))
            print(f"  luma_amount={amt:4.1f} 边缘能量={edge(a):.3f} 报错={bad_in(p, off) or 'OK'}")
    else:
        print("  没有 ffmpeg，第 3 段整段跳过")

    print("\n4) 内核自带的 GPU 去带能不能运行时改（--deband 那一族，启动参数里已经在用）")
    p.open_files([SAMPLE], append=False, remember=False)
    pump(1.6)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(0.9)
    clear()
    ob = gray(shot(p, "opt_base"))
    print(f"  改之前 options/deband = {p.get('options/deband')}")
    off = logsize(p)
    p.set_option("deband", "yes")
    pump(1.2)
    a1 = gray(shot(p, "opt_deband_yes"))
    print(f"  set_option deband=yes 之后 options/deband = {p.get('options/deband')} "
          f"逐像素差={float(np.abs(a1 - ob).mean()):.3f} 报错={bad_in(p, off) or 'OK'}")
    p.set_option("deband-iterations", "16")
    p.set_option("deband-threshold", "80")
    pump(1.2)
    a2 = gray(shot(p, "opt_deband_hi"))
    print(f"  再调 iterations/threshold：逐像素差={float(np.abs(a2 - ob).mean()):.3f}")
    # 另一种写法：直接 set_property options/deband（实测能不能绕过 file-local-options 的时机问题）
    p.set_prop("options/deband", "no")
    pump(1.0)
    print(f"  set_property options/deband=no 之后回报={p.get('options/deband')}")
    p.set_option("deband", "no")
    pump(1.0)
    a3 = gray(shot(p, "opt_deband_no"))
    print(f"  关回去：与基线差={float(np.abs(a3 - ob).mean()):.3f}（应≈0 才算真的可热切）")
    print(f"  换片之后选项还在吗：options/deband={p.get('options/deband')}")

    p.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
