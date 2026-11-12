"""合成测试样片（不依赖网络素材）：H.264 + AAC + 外挂中文字幕。

用途：驱动播放器的真机验收。画面里有随时间变化的元素（滚动色条、移动方块、秒表数字、
中文标题），因此“截图有没有变化”能证明时间推进与解码正确。
"""
from __future__ import annotations

import math
import os
import sys

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("GELU_SAMPLES") or os.path.join(HERE, "samples")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
]


def load_font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def palette(n: int):
    return [(int(120 + 120 * math.sin(i * 0.7)), int(120 + 120 * math.sin(i * 0.7 + 2.0)),
             int(120 + 120 * math.sin(i * 0.7 + 4.0))) for i in range(n)]


def make_frame(t: float, duration: float, label: str, w: int = 1280, h: int = 720) -> np.ndarray:
    img = Image.new("RGB", (w, h), (16, 18, 24))
    d = ImageDraw.Draw(img)
    cols = palette(10)
    # 顶部滚动色条
    bw = w / 10
    for i in range(12):
        idx = i % 10
        x = ((t * 160 + i * bw) % (w + bw * 2)) - bw
        d.rectangle([x, 20, x + bw * 0.86, 90], fill=cols[idx])
    # 中部秒表与中文标题
    f_big = load_font(120)
    f_mid = load_font(46)
    d.text((60, 170), f"{t:5.2f}s", font=f_big, fill=(245, 245, 248))
    d.text((60, 320), label, font=f_mid, fill=(255, 200, 87))
    # 移动方块（不同时间位置唯一，便于比对截图差异）
    k = t / max(duration, 1e-6)
    bx = int(40 + k * (w - 200))
    d.rounded_rectangle([bx, 430, bx + 120, 550], 18, fill=(76, 141, 255))
    # 底部进度刻度
    for i in range(0, 61):
        hh = 10 + int(60 * abs(math.sin(i * 0.35 + t * 2)))
        d.line([(40 + i * (w - 80) / 60, h - 40), (40 + i * (w - 80) / 60, h - 40 - hh)],
               fill=(150, 160, 180), width=3)
    return np.asarray(img)


def encode_video(path: str, seconds: float, fps: int, label: str, w: int = 1280, h: int = 720) -> None:
    container = av.open(path, mode="w")
    vstream = container.add_stream("libx264", rate=fps)
    vstream.width, vstream.height, vstream.pix_fmt = w, h, "yuv420p"
    vstream.options = {"preset": "veryfast", "crf": "20"}

    astream = container.add_stream("aac", rate=48000)
    astream.bit_rate = 128_000
    sr, ch = 48000, 2
    samples_per_frame = int(sr / fps)
    nframes = int(seconds * fps)

    for i in range(nframes):
        t = i / fps
        arr = make_frame(t, seconds, label, w, h)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        for pkt in vstream.encode(frame):
            container.mux(pkt)
        # 音频：随时间升高音高的正弦 + 节拍脉冲
        idx = np.arange(samples_per_frame) + i * samples_per_frame
        freq = 220 + 180 * (i / nframes)
        beat = np.where(idx % (sr // 2) < sr // 8, 1.0, 0.35)
        wave = (np.sin(2 * np.pi * freq * idx / sr) * beat * 0.35).astype(np.float32)
        data = np.stack([wave, np.roll(wave, 120)])
        aframe = av.AudioFrame.from_ndarray(data, format="fltp", layout="stereo")
        aframe.sample_rate = sr
        aframe.pts = i * samples_per_frame
        for pkt in astream.encode(aframe):
            container.mux(pkt)
    for pkt in vstream.encode():
        container.mux(pkt)
    for pkt in astream.encode():
        container.mux(pkt)
    container.close()


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def write_srt(path: str, seconds: float) -> None:
    texts = ["LuHui播放器 字幕测试：中文显示", "第二行：字号与描边是否清晰", "第三行：字幕位置是否正确",
             "第四行：SRT / ASS 都支持", "第五行：双语字幕稍后测试", "第六行：字幕延迟可调"]
    step = 2.0
    blocks = []
    n = 0
    t = 0.0
    while t < seconds:
        end = min(t + step - 0.05, seconds)
        n += 1
        blocks.append(f"{n}\n{_ts(t)} --> {_ts(end)}\n{texts[(n - 1) % len(texts)]}\n")
        t += step
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(blocks))


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    jobs = [("gelu-sample-1.mp4", 10.0, 30, "样片 A · 1280x720 · 30fps"),
            ("gelu-sample-2.mp4", 6.0, 60, "样片 B · 60fps 高帧率"),
            ("gelu-sample-3.mkv", 8.0, 25, "样片 C · Matroska 容器"),
            # 样片 D 只为「续播」这一条判据存在：`Config.remember_position` 把"距片尾 15 秒
            # 以内"和"看过 92% 以上"都记成看完（下次从头播），10 秒的样片上任何位置都落在
            # 这片"看完"区里，续播那一档根本写不进可恢复的进度——2026-10-19 真内核那趟就是
            # 这么撞出来的（写入=6.0 读回=0.0）。32 秒能让 0.4D=12.8s 落在合法区，
            # 且离两头都有余量。出处：tests/e2e_loadfile_receipt.py 的 K3。
            ("gelu-sample-long.mp4", 32.0, 24, "样片 D · 32s 续播用")]
    for name, secs, fps, label in jobs:
        path = os.path.join(OUT_DIR, name)
        if os.path.exists(path) and os.environ.get("GELU_SAMPLES_FORCE") != "1":
            print(f"skip  {path} 已存在")
            continue
        encode_video(path, secs, fps, label)
        write_srt(os.path.join(OUT_DIR, os.path.splitext(name)[0] + ".srt"), secs)
        sz = os.path.getsize(path) / 1e6
        print(f"ok    {name}  {secs}s@{fps}fps  {sz:.1f} MB")
    print("目录：" + OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
