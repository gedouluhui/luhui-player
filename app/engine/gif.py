"""GIF 片段导出：一条 ffmpeg 命令出成品（palettegen + paletteuse 双通道）。

这五件事决定了模块的形状，全部是实测/踩坑来的，不是想当然：

1. **调色板必须为这一段单独生成**。GIF 只有 256 色，直接用 ffmpeg 默认的单遍编码，
   暗场片段会整片糊成灰块。所以滤镜图里先 `split`，一支走 `palettegen=stats_mode=diff`
   （diff 模式只统计"变化"的像素，运动片段比全帧统计更用得开那 256 个格子），另一支
   再喂给 `paletteuse`。这是同一条命令里的两遍，不是一个中间文件两步走——省一次落盘，
   也让取消只需要杀一个进程。
2. **时间轴只用 ffmpeg 自己报的数**。进度来自 `-progress pipe:1` 的 `out_time_us=`，
   不是"我以为编码到第几帧"。读的是 stdout 的逐行 key=value，stderr 单独进日志文件：
   两个管道都留在内存里读，ffmpeg 报错刷满管道就会把子进程卡死在写日志上。
3. **字幕只能烧外挂文件**。`subtitles` 滤镜（本机 ffmpeg 实测 `--enable-libass`，
   `-filters` 里 subtitles/ass/palettegen/paletteuse 四个都在）要的是磁盘上的字幕文件；
   内嵌轨没解出来就没有文件可指。所以 `sub_file_of()` 只认 track-list 里
   `selected+external+type=sub` 那一条，其它一律回空串，界面上把选项灰掉并说清为什么。
4. **产物要解码一遍才算成**。ffmpeg 退出码 0 + 文件存在 ≠ GIF 能动：滤镜写错时它会
   吐一个单帧文件然后高兴地回 0。`read_gif()` 用 QImageReader 数帧、量宽高，
   帧数由时长与帧率反推，容得下首尾各一帧的边界差。
5. **不覆盖用户的东西**。产物名从视频名派生，落盘前逐个试 `#1 #2 …`，
   拿到一个盘上不存在的名字才开工。

`-ss` 放在 `-i` 之前（快速定位）会把解码帧的时间戳**归零**，而 `subtitles` 滤镜正是按
帧时间戳找字幕的——两者一撞，照原样递进去的 SRT 就"整段都不出现"（cue 在原片 12 秒，
片段却只有 0~4 秒）。这条不是推出来的，是 tests/probe_gif_sub.py 实测的（本机 ffmpeg
git-2da55bf59，2026-10-08 跑，rc=0）：亮度斜坡样片、cue 12.0~13.5 秒、导出 10~14 秒，
对照组（不定位）15 帧出字、甲组（定位+原样字幕）**0 帧出字**、乙组（定位+字幕平移
10 秒）15 帧出字且出字时刻与对照组逐帧一致。处理办法就是乙组那样：把字幕时间轴整体
−start 写到临时文件再交给 ffmpeg（临时文件与产物同目录、命令用相对文件名，顺带绕开
滤镜参数里盘符冒号与反斜杠的转义地狱）。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal

from . import poolkit

# 界面上的档位。宽度不设"原始分辨率"这一档：1080p 的 GIF 动辄几十 MB，
# 而 GIF 的用途就是发出去给人看，太大的文件等于没有。
WIDTHS = (320, 480, 640, 800, 960)
FPS_LADDER = (8, 10, 12, 15, 20)
DEFAULT_WIDTH = 480
DEFAULT_FPS = 12
DEFAULT_DURATION = 4.0
MAX_DURATION = 30.0          # 超过这个时长 GIF 就大得没法用了，界面上直接夹住
MIN_DURATION = 0.5
DEFAULT_SUB_FONT = 22        # 缩放以后再烧字，libass 会按小画面自动排；给个下限保证看得清


# ================= 纯函数：界值和命令都在这里算好，测试不碰 ffmpeg =================
def clamp_range(start: float, dur: float, length: float) -> tuple[float, float]:
    """把 (起点, 时长) 收进 [0, length] 的合法区间。

    length<=0（时长还不知道 / 流式没有时长）时只夹下限：这时候硬要拿 0 当上界，
    等于把用户的导出请求悄悄改成"从头导出"，比报"时长未知"更坏。
    """
    s = max(0.0, float(start or 0.0))
    d = max(MIN_DURATION, float(dur or 0.0))
    if length and length > 0:
        d = min(d, max(MIN_DURATION, float(length) - s))
        s = min(s, max(0.0, float(length) - MIN_DURATION))
    return round(s, 3), round(d, 3)


def expect_frames(dur: float, fps: int) -> int:
    """这一段该有多少帧。ffmpeg 的 fps 滤镜是"到点补帧"，所以取整用 round 不是 floor：
    4 秒 @12 会给 48 帧，floor(47.9999) 会把正常产物判成缺帧。"""
    return max(1, int(round(float(dur) * int(fps))))


def _fmt_srt(ms: float) -> str:
    ms = max(0, int(round(ms)))
    h, r = divmod(ms, 3600_000)
    m, r = divmod(r, 60_000)
    s, x = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{x:03d}"


def _fmt_ass(cs: float) -> str:
    cs = max(0, int(round(cs)))
    h, r = divmod(cs, 360_000)
    m, r = divmod(r, 6_000)
    s, x = divmod(r, 100)
    return f"{h}:{m:02d}:{s:02d}.{x:02d}"


def _ms_to_hms(ms: float) -> tuple[int, int, int, int]:
    ms = max(0, int(round(ms)))
    h, r = divmod(ms, 3600_000)
    m, r = divmod(r, 60_000)
    s, x = divmod(r, 1000)
    return h, m, s, x


_SRT_LINE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
# 时间字段的小数点：ASS/SSA 规范写的是 `H:MM:SS.cs`（点分厘秒）。这里写成 [.:] 是因为
# 实测有人手改过的 .ass 里是冒号（ffmpeg 两种都吃得），但**输出只按规范给点**——
# 曾经只认 `:`，结果整份 Dialogue 一行都匹配不上，shift_ass 静默回"改了几处=0"，
# 上层把它当"平移不出来"，于是带字幕的导出默默出一版没字的（比报错坏得多）。
_ASS_DLG = re.compile(r"^(Dialogue:\s*(?:[^,]*,){1,8}?)(\d+):(\d{2}):(\d{2})[.:](\d{2})"
                      r"(.*?,)(\d+):(\d{2}):(\d{2})[.:](\d{2})(.*)$")


def shift_sub_text(text: str, ext: str, delta: float) -> str:
    """把整份字幕的时间轴平移 -delta 秒（外挂字幕烧进已定位过的片段时用）。

    只做时间字段，内容原样返回；认不出的行、认不出的格式一律**原样退回**，
    宁可不烧字幕也不能烧出一份错位的。返回空串表示"这份字幕我处理不了"。
    """
    if not text or not delta:
        return text or ""
    e = (ext or "").lower().lstrip(".")
    if e in ("srt", "vtt", "ass", "ssa"):
        shifted, touched = (shift_srt(text, -delta) if e in ("srt", "vtt")
                            else shift_ass(text, -delta))
        return shifted if touched else ""
    return ""


def shift_srt(text: str, delta: float) -> tuple[str, int]:
    """平移 SRT/WebVTT 时间轴（单位秒，可为负）。返回 (新文本, 改了几处时间戳)。

    毫秒写成 3 位（SRT 的逗号与 VTT 的点都吃，_SRT_LINE 两种分隔符都认）。
    只动形如 HH:MM:SS,mmm 的字段，正文里出现长得像时间戳的一行也会被改——
    实测 SRT 正文里写时间的场景比"这一行是时间轴"少得多，且改错了也只是字幕文本
    里一个数字变了，不影响同步；真要收紧就得按"上一行是纯数字行"判，代价是把
    无编号的 SRT（确实存在）排除掉。这里选宽松，理由记着。
    """
    n = 0

    def rep(m: re.Match) -> str:
        nonlocal n
        h, mi, s, ms = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int((m.group(4) + "000")[:3]))
        new = (h * 3600 + mi * 60 + s) * 1000 + ms + int(round(delta * 1000))
        n += 1
        return _fmt_srt(new)

    out = _SRT_LINE.sub(rep, text)
    return out, n


def shift_ass(text: str, delta: float) -> tuple[str, int]:
    """平移 ASS/SSA 的 Dialogue 行 Start/End（百分秒精度）。非 Dialogue 行原样留着。"""
    n = 0
    step = int(round(delta * 100))          # ASS 的时间字段是 厘秒
    lines = []
    for ln in text.splitlines():
        m = _ASS_DLG.match(ln)
        if not m:
            lines.append(ln)
            continue
        a = max(0, int(m.group(2)) * 360000 + int(m.group(3)) * 6000 +
                int(m.group(4)) * 100 + int(m.group(5))) + step
        b = max(0, int(m.group(7)) * 360000 + int(m.group(8)) * 6000 +
                int(m.group(9)) * 100 + int(m.group(10))) + step
        pre = m.group(1)
        lines.append(f"{pre}{_fmt_ass(a)}{m.group(6)}{_fmt_ass(b)}{m.group(11)}")
        n += 2
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), n


def sub_file_of(tracks: Any) -> str:
    """从 mpv 的 track-list 里挑"当前选中的外挂字幕文件"，挑不出就回空串。

    为什么只认 external：ffmpeg 的 subtitles 滤镜要的是磁盘上的文件，内嵌轨没解出来
    就没有文件可指（想烧内嵌轨得先 -map 0:s:n 解出来，那是另一条命令、另一份临时文件，
    第一版不做，界面上也得说清"这条轨烧不进去"而不是默默出一版没字的）。
    """
    if not isinstance(tracks, list):
        return ""
    for t in tracks:
        if not isinstance(t, dict):
            continue
        if str(t.get("type") or "") != "sub" or not t.get("selected"):
            continue
        if not t.get("external"):
            return ""
        f = str(t.get("external-filename") or "")
        return f if f and os.path.exists(f) else ""
    return ""


def out_path(out_dir: str, video: str, start: float, dur: float,
             width: int, fps: int) -> str:
    """产物名：视频名 + 区间 + 规格，再撞车就 #1 #2 …。绝不覆盖已有文件。"""
    stem = os.path.splitext(os.path.basename(video))[0] or "clip"
    stem = re.sub(r'[\\/:*?"<>|\s]+', "_", stem)[:48]
    base = f"{stem}_{start:g}s_{dur:g}s_{width}w{fps}f"
    os.makedirs(out_dir, exist_ok=True)
    # 第一个名字不带后缀，从第一次撞车起才加 #1（与顶部第 5 条承诺一致）：
    # 用户一天导三次是「名.gif」「名.gif#1」…不是「名」「名#2」——数不回来。
    for i in range(0, 999):
        p = os.path.join(out_dir, f"{base}{'' if i == 0 else '#' + str(i)}.gif")
        if not os.path.exists(p):
            return p
    return os.path.join(out_dir, f"{base}_{int(time.time())}.gif")


def build_cmd(exe: str, video: str, start: float, dur: float, width: int, fps: int,
              out: str, sub_name: str = "") -> list[str]:
    """拼出那条 ffmpeg 命令（纯函数：测试就看这个形状，不真跑）。

    `-ss` 在 `-i` **之前**：先按关键帧跳，再由 accurate_seek（转码时默认开）把开头
    那段解出来丢掉，起点是准的，而代价只是 GOP 级的一小段解码——放到 `-i` 之后就从
    影片第一帧开始解，一部两小时的片子等不起。
    字幕烧在 scale **之后**：先缩到目标宽度再排字，字号按小画面来（配合 force_style
    的下限），不然 480 宽的 GIF 上字幕会占掉半屏。
    """
    chain = f"fps={int(fps)},scale={int(width)}:-1:flags=lanczos"
    if sub_name:
        chain += f",subtitles={sub_name}:force_style='FontSize={DEFAULT_SUB_FONT}'"
    chain += (",split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
              "[s1][p]paletteuse=dither=bayer:bayer_scale=4")
    return [
        exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-progress", "pipe:1",
        "-ss", f"{float(start):.3f}", "-i", video,
        "-t", f"{float(dur):.3f}",
        "-an", "-sn", "-dn",
        "-filter_complex", chain,
        "-loop", "0",
        out,
    ]


_PROGRESS = re.compile(r"^out_time_us=(\d+)\s*$")


def parse_progress(line: str) -> float | None:
    """`-progress pipe:1` 的一行 → 已编码秒数；不是那行就回 None（不猜）。

    只认 out_time_us：这一版 ffmpeg 同时会给 out_time_ms，但 out_time_ms 历史上
    是微秒值的错名（实测本机给的是 同一数字，为稳妥只取 us 这一支）。
    """
    m = _PROGRESS.match(line.strip())
    return int(m.group(1)) / 1_000_000.0 if m else None


# ================= 读回产物：帧数与尺寸，解码一遍才算成 =================
def read_gif(path: str) -> dict[str, Any]:
    """返回 {ok, frames, w, h, bytes, err}。用 Qt 自己的 GIF 解码器数帧，
    这样"验收说它能动"和"用户在屏幕上看到它动"是同一个解码器说的。

    两个本机实测的坑：①`QImageReader` **没有** `image()` 这个方法（PyQt 5.15.2，
    静态审计 tests/audit_qt_attrs.py 会点名它），只有 `read()`；②动图第一帧不能靠
    `jumpToNextImage()` 起手——先 jump 再读会一帧都拿不到（探针第一版就是这么把三份
    好好的 GIF 读成空的）。所以逐帧计数一律走 `read()` 循环。
    """
    from PyQt5.QtGui import QImageReader
    out: dict[str, Any] = {"ok": False, "frames": 0, "w": 0, "h": 0, "bytes": 0, "err": ""}
    if not os.path.exists(path):
        out["err"] = "产物不存在"
        return out
    out["bytes"] = os.path.getsize(path)
    r = QImageReader(path)
    if not r.canRead():
        out["err"] = f"解不开：{r.errorString()}"
        return out
    sz = r.size()
    out["w"], out["h"] = sz.width(), sz.height()
    n = r.imageCount()
    if n > 1:
        out["frames"] = n
    else:
        r2 = QImageReader(path)
        cnt = 0
        while cnt < 4000:
            img = r2.read()
            if img.isNull():
                break
            cnt += 1
        out["frames"] = cnt
    if out["frames"] <= 0:
        out["err"] = "一帧都解不出来"
        return out
    out["ok"] = True
    return out


# ================= 后台任务 =================
class _JobSignals(QObject):
    """导出作业的回报壳。不继承 `poolkit.JobSignals`：这里 `done` 的签名是
    (int, bool, str, str)，与那边 (str, bool, str) 不同名同挂会出"读到哪一个看运气"的事。
    壳的寄管（`pin`）与发射（`emit`）照样走 poolkit——crash.log 那一桩（信号壳被 sip 提前
    拆掉 ⇒ 兜底分支再抛 ⇒ 进程 failfast）在这四家里是同一个形状，一处都不能只修一半。
    """
    prog = pyqtSignal(int, float)        # job id, 已编码秒
    done = pyqtSignal(int, bool, str, str)   # job id, 成功?, 详情, 产物路径


class GifJob(QRunnable):
    """一趟导出。参数打包成一个 dict，命令在 run() 里现拼：

    字幕平移只在**读到文件内容之后**才知道成不成立，而那时命令必须能重拼成"不带字幕"
    的版本。要是命令由调用方拼好传进来，这里就得反过来从 argv 里解析出宽度帧率再拼一遍
    ——解析器多一个，出错的地方就多一个。所以存参数，不存 argv。
    """

    def __init__(self, jid: int, params: dict[str, Any]):
        super().__init__()
        self.autoDelete = True
        self.jid = jid
        self.p = params
        self.sig = _JobSignals()
        self.out = params["out"]
        self.cmd = build_cmd(params["exe"], params["video"], params["start"],
                             params["dur"], params["width"], params["fps"], self.out,
                             params.get("sub_name") or "")
        self.expect = expect_frames(params["dur"], params["fps"])
        self.cancelled = False
        self.proc = None
        self.started_at = time.time()

    def cancel(self) -> None:
        """取消＝杀进程。留着进程继续往用户的目录里写一个没人要的 GIF 更坏。"""
        self.cancelled = True
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass

    def run(self) -> None:                    # 线程池线程里跑，绝不碰 Qt 对象
        log = self.out + ".log"
        tmp_sub = ""
        sig = self.sig                        # 入口取一次本地引用，见 app/engine/poolkit.py
        poolkit.pin(self)
        try:
            # 取消发生在进程起来之前（用户点"开始"后立刻反悔、或窗口已在收口）：
            # 旧实现照样拼字幕、起 ffmpeg、解完整条片子，最后才在 `_finish` 里看 cancelled
            # ——用户看到的"取消"其实白跑了几十秒，还占着一个核。
            if self.cancelled:
                self._finish(False, "已取消", keep=False)
                return
            sub_src, sub_name = self.p.get("sub_src") or "", self.p.get("sub_name") or ""
            if sub_src and sub_name:
                tmp_sub = os.path.join(os.path.dirname(self.out), sub_name)
                with open(sub_src, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                shifted = shift_sub_text(text, os.path.splitext(sub_src)[1],
                                         float(self.p.get("delta") or 0.0))
                if not shifted:
                    # 平移不出来就不烧一份错位的：退回无字幕的命令，界面详情里会写明。
                    # 临时字幕文件这时**还没写过**，所以只需把名字清空，不能去删它。
                    self.cmd = build_cmd(self.p["exe"], self.p["video"], self.p["start"],
                                         self.p["dur"], self.p["width"], self.p["fps"],
                                         self.out, "")
                    tmp_sub = ""
                    self.p["no_sub"] = "字幕时间轴平移失败，已改为不烧字幕"
                else:
                    with open(tmp_sub, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(shifted)
            with open(log, "wb") as lfh:
                # poolkit.spawn 而非裸 Popen：这个孩子进登记册，退出收口时杀得到它。
                # 实测过（tests/test_gif_real.py --residue）关掉窗口之后 pid 还在、
                # 产物从 0 字节一路写到 5 MB——那一版只是"窗口没了"，用户以为导出取消了。
                proc = poolkit.spawn(self, self.cmd, cwd=os.path.dirname(self.out),
                                     stdout=subprocess.PIPE, stderr=lfh)
                self.proc = proc
                assert proc.stdout is not None
                for raw in proc.stdout:
                    sec = parse_progress(raw.decode("ascii", "replace"))
                    if sec is not None:
                        poolkit.emit(sig, "prog", self.jid, sec)
                rc = proc.wait()
            if self.cancelled:
                self._finish(False, "已取消", keep=False)
                return
            if rc != 0:
                if poolkit.is_closing():
                    # 收口时杀掉的进程也走这条，但**别把它说成 ffmpeg 失败**：那一趟没坏，
                    # 是用户在关窗口。混成"ffmpeg 退出码 1"就是在给产品编事故原因。
                    self._finish(False, "窗口关闭，导出被收口终止", keep=False)
                    return
                self._finish(False, _tail(log) or f"ffmpeg 退出码 {rc}", keep=False)
                return
            info = read_gif(self.out)
            if not info["ok"]:
                self._finish(False, f"产物读不回：{info['err']}", keep=False)
                return
            if info["frames"] < self.expect - 1:
                self._finish(False, f"帧数不够：解出 {info['frames']}，按时长×帧率该有 {self.expect}",
                             keep=False)
                return
            self._finish(True, f"{info['frames']} 帧 / {info['w']}×{info['h']} / "
                               f"{info['bytes'] / 1048576:.1f} MB / "
                               f"{time.time() - self.started_at:.1f}s", keep=True)
        except BaseException as exc:           # 导出失败只配报一句，不配把播放器带走
            # BaseException：crash.log 那一桩就是"兜底分支自己再抛"把进程带走的，
            # 兜底只挡一半异常类型等于没挡（见 poolkit 开头第 1 条实测分布）。
            self._finish(False, f"{type(exc).__name__}: {exc}", keep=False)
        finally:
            poolkit.drop_child(self)
            poolkit.unpin(self)
            if tmp_sub:
                try:
                    os.remove(tmp_sub)
                except OSError:
                    pass
            try:
                os.remove(log)
            except OSError:
                pass

    def _finish(self, ok: bool, detail: str, keep: bool) -> None:
        if not keep and os.path.exists(self.out):
            try:
                os.remove(self.out)          # 坏产物不留：半截 GIF 会被当成成功
            except OSError:
                pass
        if not ok and self.p.get("no_sub"):
            detail = f"{self.p['no_sub']}；{detail}"
        poolkit.emit(self.sig, "done", self.jid, ok, detail, self.out if ok else "")


def _tail(path: str, n: int = 2) -> str:
    try:
        with open(path, "rb") as fh:
            lines = fh.read().decode("utf-8", "replace").strip().splitlines()
        return " / ".join(lines[-n:])
    except OSError:
        return ""


class GifStore(QObject):
    """GIF 导出任务的门面：一次只让一个在跑（导出是解码+编码双份力气，
    并发两个会把软解线程占满，正放着的片子先卡给你看）。"""

    sig_progress = pyqtSignal(int, float, float)   # id, 已编码秒, 目标秒
    sig_done = pyqtSignal(int, bool, str, str)     # id, 成功?, 详情, 路径

    def __init__(self, ffmpeg_exe: str, parent=None):
        super().__init__(parent)
        self.exe = ffmpeg_exe
        # 导出走全局池（一趟只允许一个，见类注释）。这里把它**登记**进 poolkit：
        # 不登记的话退出收口看不见这张池子，"正在导 GIF"恰好是关窗后还在写盘的那一条。
        self._pool = poolkit.adopt_global_pool("gif")
        self._next_id = 1
        self._cur: GifJob | None = None
        self._total = 0.0
        self.last_detail = ""

    def busy(self) -> bool:
        return self._cur is not None

    def cancel(self) -> None:
        if self._cur:
            self._cur.cancel()

    def start(self, video: str, start: float, dur: float, out_dir: str,
              width: int = DEFAULT_WIDTH, fps: int = DEFAULT_FPS,
              sub_file: str = "", sub_delta: bool = True) -> tuple[int, str]:
        """开一趟导出，回 (job id, 失败原因)。原因非空表示没开成（id 为 0）。

        四条"开不成"的账要单独报得出来，不能混成一句"导出失败"：没装 ffmpeg、视频不在、
        参数非法（时长被夹到 0 以下）、已经有一趟在跑。前两条是环境，第三条是界面没夹住，
        第四条是并发闸门——四种坏的用户处置方式完全不同。
        """
        if not os.path.exists(self.exe):
            return 0, f"没找到 ffmpeg：{self.exe}"
        if not os.path.exists(video):
            return 0, "视频文件不存在"
        if self.busy():
            return 0, "已经有一个导出在跑了"
        if poolkit.is_closing():
            # 收口之后不再起新趟：那时候起来的这一趟只会被下一条 `is_closing` 分支判成
            # "窗口关闭，导出被收口终止"，用户看到的是一条他从没点过的失败。
            return 0, "窗口正在关闭，导出不启动"
        start, dur = clamp_range(start, dur, 0.0)
        if dur < MIN_DURATION:
            return 0, "区间太短，导不出东西"
        out = out_path(out_dir, video, start, dur, width, fps)
        params = {"exe": self.exe, "video": video, "start": start, "dur": dur,
                  "width": int(width), "fps": int(fps), "out": out,
                  "sub_src": sub_file if sub_file and os.path.exists(sub_file) else "",
                  "sub_name": ("clip" + (os.path.splitext(sub_file)[1] or ".srt"))
                              if sub_file else "",
                  "delta": float(start) if (sub_file and sub_delta) else 0.0}
        job = GifJob(self._next_id, params)
        self._next_id += 1
        job.sig.prog.connect(self._on_prog)
        job.sig.done.connect(self._on_done)
        self._cur = job
        self._total = float(dur)
        self._pool.start(job)          # 就是 globalInstance()，但走 __init__ 登记过的那只手
        return job.jid, ""

    def _on_prog(self, jid: int, sec: float) -> None:
        self.sig_progress.emit(jid, min(sec, self._total), self._total)

    def _on_done(self, jid: int, ok: bool, detail: str, path: str) -> None:
        if self._cur and self._cur.jid == jid:
            self._cur = None
        if ok:
            self.last_detail = detail
        self.sig_done.emit(jid, ok, detail, path)

    def wait(self, ms: int = 0) -> bool:
        """等线程池排空（验收脚本用；UI 里永远不该调它）。"""
        t0 = time.time()
        while self._cur and (ms <= 0 or (time.time() - t0) * 1000 < ms):
            time.sleep(0.05)
        # PyQt 5 的名字是 waitForDone；waitForFinished 是 Qt6 的写法，在这里会得到一句
        # AttributeError（本机实测）。200ms 的尾巴只兜"刚刚投递完"的那一瞬——长等要用
        # poolkit.shutdown，它按 Python 侧轮询走，不攥着 GIL 等一个需要 GIL 的线程。
        self._pool.waitForDone(200)
        return self._cur is None
