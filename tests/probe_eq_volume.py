# volume 那几串写法在本机 libavfilter 里到底等于多少增益（ffprobe 之外的另一把尺：volumedetect）。
# 原是一次性量具，2026-10-12 转正留档：app/engine/equalizer.py 模块 docstring 第 6 条
# 与 docs/design.md §3 第 11 条都引这里的三个数（-9dB、0.3548、-12 无后缀），
# 引了就要在盘上找得回命令本身，故不再"用完删"。
# 跑法：python tests/probe_eq_volume.py > tests/out/eq-vol-r12.log 2>&1（只起 ffmpeg，不碰屏幕、不碰内核）。
# 为什么要单独问：`volume=-9dB` 是 r15b 才加进探针的行，而 FFmpeg 的 eval 支持隐式乘法＋
# 未知标识符自动建 0 —— 也就是 "-9dB" 有可能不是"降 9 dB"而是"乘 0＝静音"。
# 这两者在频谱上是天壤之别（-9 dB vs -inf），必须先分清，否则面板的 preamp 一开就把声音抹掉。
# 口径：volumedetect 打的是 ffmpeg 自己的均值（相对满刻度），只比"与 anull 基线的差"。
import subprocess
import sys
import time

sys.path.insert(0, ".")
from app import config as cfgmod  # noqa: E402

FF = cfgmod.FFMPEG_EXE
SR = 48000
DUR = "20"
CAND = [
    ("anull（基线）", "anull,volumedetect"),
    ("volume=-9dB", "volume=-9dB,volumedetect"),
    ("volume=dB(-9)", "volume=dB(-9),volumedetect"),
    ("volume=0.3548", "volume=0.3548,volumedetect"),
    ("volume=9dB", "volume=9dB,volumedetect"),
    ("volume=+9dB", "volume=+9dB,volumedetect"),
    ("volume=dB(9)", "volume=dB(9),volumedetect"),
    ("volume=-12", "volume=-12,volumedetect"),
    ("volume=0", "volume=0,volumedetect"),
]
base = None
for name, graph in CAND:
    cmd = [FF, "-v", "info", "-f", "lavfi", "-i",
           f"anoisesrc=color=pink:sample_rate={SR}:amplitude=0.5",
           "-af", graph, "-t", DUR, "-f", "null", "-"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=240, text=True,
                           encoding="utf-8", errors="replace")
        txt = r.stderr or ""
        mean = peak = ""
        for ln in txt.splitlines():
            if "mean_volume" in ln:
                mean = ln.split(":")[-1].strip()
            if "max_volume" in ln:
                peak = ln.split(":")[-1].strip()
        err = " | ".join(ln.strip() for ln in txt.splitlines()
                         if "Error" in ln or "error" in ln or "out of range" in ln)[:200]
        if base is None and mean:
            base = float(mean.split()[0])
        d = ""
        if mean:
            try:
                d = f"Δ={float(mean.split()[0]) - base:+.1f} dB"
            except ValueError:
                pass
        print(f"[{name:16s}] rc={r.returncode}  mean={mean or '(无)'}  max={peak or '(无)'}  {d}"
              f"  墙钟={time.monotonic() - t0:.2f}s" + (f"  报错={err}" if err else ""), flush=True)
    except Exception as exc:
        print(f"[{name:16s}] 跑崩 {type(exc).__name__}: {str(exc)[:120]}", flush=True)
