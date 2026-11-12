"""探针：`-ss` 放在 `-i` 之前时，`subtitles` 滤镜按哪条时间轴找字幕？

这个问题必须实测，不能推。两种都说得通的机制各执一端：

* A 说：ffmpeg 输入定位后把解码帧的时间戳**保留成原片时间**，libass 拿到 12.0 秒这个
  值，于是照原样 SRT 就对齐（不需要平移）。
* B 说：ffmpeg 会把起点归零，libass 拿到的是 0～4 秒，于是照原样 SRT 烧出来**一个字都没有**
  （cue 在 12 秒，早过了片段末尾），必须把字幕时间轴整体 −start 才对得上。

判据不靠眼睛看字，靠两处机器可读的信号：

1. **画面亮度就是源时间**。样片是 `geq` 生成的亮度斜坡（luma = 255 × T/总时长），
   所以 GIF 每一帧的平均亮度反解回秒，就是这一帧在**原片**上的时刻。
2. **字幕墨迹就是那一帧有没有字**。cue 期间画面下部会多出白字，按"比该帧底色亮 60 以上
   的像素数"判，数出来的是像素，不是猜。

于是三种结果各自指向一个结论（三组的出字时刻都按**原片时间**比——亮度斜坡量到的本来就是
原片时刻，乙组平移的是字幕时间轴，不是画面内容）：
  * 对照组出字、甲组全程没字、乙组在原片 12~13.5 出字 → B 成立，必须平移。
  * 对照组出字、甲组在原片 12~13.5 出字、乙组早到 2~3.5 → A 成立，不许平移。
  * 对照组也不出字 → 是"烧字幕"或"出字检测"本身坏了，先修探针，别动生产代码。

用法：python tests/probe_gif_sub.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QCoreApplication          # noqa: E402
from PyQt5.QtGui import QImage, QImageReader       # noqa: E402

from app import config as cfgmod                  # noqa: E402
from app.engine import gif as G                   # noqa: E402

FF = cfgmod.FFMPEG_EXE
TOTAL = 20.0           # 样片总时长（秒）
CUE_A, CUE_B = 12.0, 13.5     # 字幕在原片上的出现区间
START, DUR = 10.0, 4.0        # 导出这一段


def run(cmd: list[str], tag: str) -> int:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=flags)
    if r.returncode != 0:
        print(f"[{tag}] ffmpeg rc={r.returncode}\n{(r.stderr or '').strip()[-800:]}")
    return r.returncode


def make_sample(dst: str) -> int:
    """亮度斜坡样片：luma=255*T/20，每帧都是关键帧（-g 1），小分辨率跑得快。"""
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "lavfi", "-i", f"color=black:s=320x180:r=10:d={TOTAL}",
           "-vf", f"geq=lum='255*min(T/{TOTAL},1)':cb=128:cr=128",
           "-g", "1", "-c:v", "mpeg4", "-q:v", "2", dst]
    return run(cmd, "样片")


def _hms(sec: float) -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


# cue 只写在原片的 CUE_A~CUE_B 这一段，别的时刻画面里一个字都没有
SRT = f"1\n{_hms(CUE_A)} --> {_hms(CUE_B)}\nMARK MARK MARK\n\n"


def frames_of(path: str) -> list[QImage]:
    """逐帧 read() 到解不动为止。

    别拿 jumpToNextImage 起手：本机 PyQt 5.15.2 上"先 jump 再 image()"一帧都拿不到
    ——探针第一版就是这么把三份好好的 GIF 读成空的（同一批文件 gif.read_gif 数出
    160/40/40 帧）。读到的若是单帧静止图，read() 循环跑一次就自然停。
    """
    r = QImageReader(path)
    out: list[QImage] = []
    while len(out) < 500:
        img = r.read()
        if img.isNull():
            break
        out.append(img.convertToFormat(QImage.Format_Grayscale8))
    return out


def measure(img: QImage) -> tuple[float, int]:
    """回 (这一帧在原片上的秒数, 下部字幕区里的"白字"像素数)。

    取中位数当底色（斜坡在这一段里几乎是平的，中位数=背景本身），白字按
    "比底色亮 60 以上"判。用 constBits().asstring() 取像素：Format_Grayscale8 上
    pixel(x,y) 回的不是灰度值（实测回 4283256141 这种调色板索引串），必须按字节读。
    """
    w, h = img.width(), img.height()
    if img.isNull() or not img.byteCount():
        return 0.0, 0
    buf = img.constBits().asstring(img.byteCount())
    stride = img.bytesPerLine()
    top = int(h * 0.72)
    vals, ink = [], 0
    for y in range(h):
        row = buf[y * stride: y * stride + w]
        if len(row) < w:
            break
        for x in range(0, w, 2):
            vals.append(row[x])
    if not vals:
        return 0.0, 0
    vals.sort()
    med = vals[len(vals) // 2]
    for y in range(top, h):
        row = buf[y * stride: y * stride + w]
        if len(row) < w:
            break
        for x in range(0, w, 2):
            if row[x] > med + 60:
                ink += 1
    return med / 255.0 * TOTAL, ink


def one_arm(ff: str, video: str, tmp: str, tag: str, srt_name: str, srt_text: str,
            start: float, dur: float) -> dict:
    """跑一趟导出并把 GIF 逐帧量回来。回 {ok, frames, times, ink_at, detail}。"""
    srt = os.path.join(tmp, srt_name)
    with open(srt, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(srt_text)
    out = os.path.join(tmp, f"{srt_name[:-4]}_{int(start * 100)}.gif")
    if os.path.exists(out):
        os.remove(out)
    cmd = G.build_cmd(ff, video, start, dur, 320, 10, out, srt_name)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # cwd=tmp + 相对字幕名：滤镜参数里就不出现盘符冒号与反斜杠，转义这一层整个绕开
    rr = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", creationflags=flags)
    if rr.returncode != 0:
        return {"ok": False, "detail": f"rc={rr.returncode} {(rr.stderr or '')[-300:]}"}
    rows = [measure(im) for im in frames_of(out)]
    if not rows:
        return {"ok": False, "detail": "GIF 一帧都没解出来"}
    ink_at = sorted(round(t, 2) for t, ink in rows if ink > 3)
    return {"ok": True, "frames": len(rows), "times": [round(t, 2) for t, _ in rows],
            "ink_at": ink_at, "detail": f"{len(rows)} 帧，出字 {len(ink_at)} 帧"}


def main() -> int:
    if not os.path.exists(FF):
        print(f"没找到 ffmpeg：{FF}")
        return 2
    QCoreApplication(sys.argv)                    # QImageReader 需要一份 QCoreApplication
    tmp = tempfile.mkdtemp(prefix="gelu-gifsub-")
    video = os.path.join(tmp, "ramp.mp4")
    if make_sample(video) != 0:
        return 2
    print(f"样片：{video}  {os.path.getsize(video)} 字节，亮度斜坡=原片时间")
    print(f"设定：cue {CUE_A}~{CUE_B} 秒出字；导出 {START}~{START + DUR} 秒")

    shifted = G.shift_sub_text(SRT, ".srt", START)
    if not shifted:
        print("shift_sub_text 回空——平移这一支根本没写成，探针不作数。")
        return 1
    arms = {
        # 对照组：不定位（start=0）、字幕原样。这一组必须出字，否则说明"烧字幕"或
        # "出字检测"本身坏了，另外两组的"没出字"就不能当成 A/B 的证据。
        "对照 不定位+原样": one_arm(FF, video, tmp, "ctl", "ctl.srt", SRT, 0.0, 16.0),
        "甲 定位+原样": one_arm(FF, video, tmp, "raw", "raw.srt", SRT, START, DUR),
        "乙 定位+平移": one_arm(FF, video, tmp, "sft", "sft.srt", shifted, START, DUR),
    }
    for k, v in arms.items():
        print(f"[{k}] {v.get('detail')} 出字时刻={v.get('ink_at')}")

    print("\n---- 判读 ----")
    ctl, a, b = arms["对照 不定位+原样"], arms["甲 定位+原样"], arms["乙 定位+平移"]
    for name, v in (("对照", ctl), ("甲", a), ("乙", b)):
        if not v.get("ok"):
            print(f"结论：{name} 组没跑成（{v.get('detail')}），这轮不作数。")
            return 1
    # 三组的出字时刻都按**原片时间**比。亮度斜坡量到的本来就是原片时刻（斜坡是烧进
    # 像素里的，跟定位不定位无关），所以甲乙两组期待的位置和对照组是同一个区间：
    # 乙平移过的是"字幕时间轴"，不是"画面内容"——画面还是原片 10~14 秒那一段。
    lo, hi = CUE_A - 0.8, CUE_B + 0.8          # 容 0.8 秒：亮度经量化，反解出的时刻有零点几秒偏
    ctl_hit = [x for x in ctl["ink_at"] if lo <= x <= hi]
    a_hit = [x for x in a["ink_at"] if lo <= x <= hi]
    b_hit = [x for x in b["ink_at"] if lo <= x <= hi]
    print(f"对照：{len(ctl_hit)} 帧落在原片 {CUE_A}~{CUE_B}（必须 >0，否则探针不灵）")
    print(f"甲（不平移）：出字 {len(a['ink_at'])} 帧，落在原片 {CUE_A}~{CUE_B} 的 {len(a_hit)} 帧")
    print(f"乙（平移 {START} 秒）：出字 {len(b['ink_at'])} 帧，落在原片 {CUE_A}~{CUE_B} 的 {len(b_hit)} 帧")
    b_wrong = [x for x in b["ink_at"] if not (lo <= x <= hi)]
    if not ctl_hit:
        print("结论：对照组都没出字——是烧字幕或出字检测坏了，先修探针，别改生产代码。")
        return 1
    if b_hit and not a_hit and not b_wrong:
        print(f"结论：B 成立——输入定位（-ss 在 -i 前）把字幕滤镜看到的时间戳归零了，"
              f"**必须**把字幕时间轴平移 start 秒。gif.py 现在的做法对。")
        return 0
    if a_hit and not b_hit:
        print("结论：A 成立——时间戳保留原片值，**不许**平移。gif.py 要把 shift 去掉。")
        return 0
    print("结论：既不是 A 也不是 B（见逐帧数据）。按猜的写进生产代码之前，先把这组数据解释清楚。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
