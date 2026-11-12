"""GIF 导出自检：纯函数用已知答案，产物用真 ffmpeg 跑一遍再解回来量。

用法：
    python tests/test_gif.py            # 正常自检：全部 PASS 才算过
    python tests/test_gif.py --falsify  # 证伪：把实现换成"常见错误写法"，对应判据必须翻红

为什么值得单开一份：这条链上"文件建出来了"和"GIF 能动、时间对得上、字幕没跑偏"是四件事。
ffmpeg 退出码 0 + 文件存在 这三种坏都能同时成立（滤镜写错时它吐一个单帧文件还高兴回 0）。
所以主判据不看退出码，看**解码回来的帧数**和**逐帧亮度反解出的原片时刻**——量法的实现
直接复用 tests/probe_gif_sub.py（探针已经把"亮度斜坡=原片时间、白字=字幕墨迹"这套对照
校准过一次，两份测试各写一遍量法就是两个可能互相矛盾的尺子）。

两条老规矩：
  · 判据不许恒真 —— 每条都由 --falsify 里对应的坏桩证伪；
  · 环境不够就报环境 —— 没装 ffmpeg 时这轮判 rc=3（环境），不伪装成"功能失败"。
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import gif as G  # noqa: E402
import probe_gif_sub as PGS  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
FF = cfgmod.FFMPEG_EXE
results: list[tuple[str, bool, str]] = []

# 样片：亮度斜坡（每一帧的灰度就是它在原片上的秒数），20 秒、每帧都是关键帧。
# 每帧都是关键帧这一条很重要：-ss 输入定位在这种情况下**必然是准的**，
# 于是"起点偏了"和"字幕时间轴偏了"这两种坏能分开看（样片本身由探针生成，量法同源）。
RAMP_SECONDS = PGS.TOTAL


def pump(seconds: float, pred) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds and not pred():
        APP.processEvents()
        time.sleep(0.02)
    APP.processEvents()
    return bool(pred())


def ramp(tmp: str) -> str:
    path = os.path.join(tmp, "ramp.mp4")
    if not os.path.exists(path):
        if PGS.make_sample(path) != 0:
            return ""
    return path


def loop_count(path: str) -> int | None:
    """读 GIF 里 NETSCAPE2.0 扩展块的循环次数（0=无限循环）。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    i = raw.find(b"NETSCAPE2.0")
    if i < 0:
        return None
    j = i + len(b"NETSCAPE2.0")
    if j + 4 > len(raw) or raw[j] != 3 or raw[j + 1] != 1:
        return None
    return struct.unpack_from("<H", raw, j + 2)[0]


# ============================================================ 判据
def g_clamp(tmp: str) -> tuple[bool, str]:
    a = G.clamp_range(100, 40, 120)         # 超片尾：时长夹到剩的 20 秒
    b = G.clamp_range(5, 4, 0)              # 时长未知：不硬夹成 0
    c = G.clamp_range(119.9, 4, 120)        # 起点贴尾：起点往回让，保住最短时长
    d = G.clamp_range(-3, 0.1, 100)         # 负起点／超短时长：各自夹回合法值
    ok = (a == (100.0, 20.0) and b == (5.0, 4.0) and c[0] <= 119.9 and c[1] >= G.MIN_DURATION
          and d == (0.0, G.MIN_DURATION))
    return ok, f"超尾={a} 时长未知={b} 贴尾={c} 非法输入={d}"


def g_expect_frames(tmp: str) -> tuple[bool, str]:
    # 8.2*15 在二进制浮点里是 122.99999999999999：floor 会得 122（全 UI 可达的
    # 时长×帧率格子里只有 8.2@15 与 16.4@15 两格会踩到，是实测数出来的，不是编的）。
    # 为什么在乎：这个数是"帧数不够＝导出失败"的下界，少算一帧就是把判据放宽一格，
    # 短了一秒的 GIF 也能报"已导出"。曾经写的 2.9s@10 其实等于 29.0，抓不到 floor。
    v = G.expect_frames(8.2, 15)
    z = G.expect_frames(0, 12)
    w = G.expect_frames(3.0, 10)
    return (v == 123 and z == 1 and w == 30), \
        f"8.2s@15={v}（该 123，floor 给 122） 0s@12={z}（该 1，不能是 0） 3s@10={w}（该 30）"


SRT_KNOWN = "1\n00:01:02,500 --> 00:01:05,250\n第一行\n\n2\n00:02:00,000 --> 00:02:03,000\n第二行\n\n"


def g_shift_srt(tmp: str) -> tuple[bool, str]:
    out, n = G.shift_srt(SRT_KNOWN, -62.5)
    # 已知答案：01:02.500 − 62.5 = 0 秒；01:05.250 − 62.5 = 2.75 秒；
    #           02:00 − 62.5 = 57.5 秒；02:03 − 62.5 = 60.5 秒（跨分钟/跨小时都借到位）
    want = ["00:00:00,000", "00:00:02,750", "00:00:57,500", "00:01:00,500"]
    # 最后一格是 00:01:00,500 不是 00:00:60,500：60.5 秒必须进位成 1 分 0 秒，
    # "写出 60 秒"这种时间戳 SRT 解析器不认，看着像对、烧进去整条字幕就断了。
    got = [ln.split(" --> ")[0].split("\n")[-1] for ln in out.splitlines() if " --> " in ln]
    stamps = [x.strip() for x in out.replace(" --> ", "\n").splitlines()
              if ":" in x and "," in x]
    body_ok = "第一行" in out and "第二行" in out and out.count("\n\n") == 2
    neg = G.shift_srt("1\n00:00:01,000 --> 00:00:02,000\nx\n", -10.0)
    return (n == 4 and stamps == want and body_ok and neg[0].startswith("1\n00:00:00,000")), \
        f"改了几处={n} 时间戳={stamps} 正文留着={body_ok} 负数夹到 0={neg[0][:18]!r} got={got}"


def g_shift_ass(tmp: str) -> tuple[bool, str]:
    src = ("[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           "Dialogue: 0,0:00:12.00,0:00:14.50,Default,,0000,0000,0000,,你好\n"
           "Comment: 0,0:00:12.00,0:00:14.50,Default,,0000,0000,0000,,不动我\n")
    out, n = G.shift_ass(src, -10.0)
    d = [ln for ln in out.splitlines() if ln.startswith("Dialogue")][0]
    c = [ln for ln in out.splitlines() if ln.startswith("Comment")][0]
    # 厘秒：12.00s → 2.00s，14.50s → 4.50s；Format/Comment 行一个字都不该变
    return (n == 2 and "0:00:02.00" in d and "0:00:04.50" in d and "你好" in d
            and c == src.splitlines()[3]), f"Dialogue={d} 改了几处={n} Comment 原样={c == src.splitlines()[3]}"


def g_shift_unknown_format(tmp: str) -> tuple[bool, str]:
    a = G.shift_sub_text(SRT_KNOWN, ".sub", 5.0)     # 认不出的格式：回空串=不烧，别原样递
    b = G.shift_sub_text("", ".srt", 5.0)
    c = G.shift_sub_text("没有时间轴的文件", ".srt", 5.0)   # 有内容但没有一个时间戳：也判"处理不了"
    return (a == "" and b == "" and c == ""), f"未知格式={a!r} 空文本={b!r} 无时间戳={c!r}"


def g_sub_file_of(tmp: str) -> tuple[bool, str]:
    real = os.path.join(tmp, "zh.ass")
    with open(real, "w", encoding="utf-8") as fh:
        fh.write("x")
    ext_sel = [{"type": "sub", "selected": True, "external": True, "external-filename": real}]
    embedded = [{"type": "sub", "selected": True, "sid": 0}]
    # 旗子说不是外挂、却带了一个存在的文件名：这种半新半旧的 track-list 条目是真的会出现
    # （换过字幕轨以后属性没刷全）。这里必须信旗子——信了文件名就会把**上一部片的字幕**
    # 烧进这一条 GIF，画面配错字，比不烧字幕恶劣得多。
    stale = [{"type": "sub", "selected": True, "external": False, "external-filename": real}]
    not_sel = [{"type": "sub", "selected": False, "external": True, "external-filename": real}]
    gone = [{"type": "sub", "selected": True, "external": True, "external-filename": os.path.join(tmp, "nada.srt")}]
    audio = [{"type": "audio", "selected": True, "external": True, "external-filename": real}]
    junk = [{"type": "sub"}, None, "字符串", 7]
    return (G.sub_file_of(ext_sel) == real and G.sub_file_of(embedded) == ""
            and G.sub_file_of(stale) == ""
            and G.sub_file_of(not_sel) == "" and G.sub_file_of(gone) == ""
            and G.sub_file_of(audio) == "" and G.sub_file_of(junk) == ""
            and G.sub_file_of(None) == "" and G.sub_file_of({"a": 1}) == ""), \
        f"外挂={G.sub_file_of(ext_sel) == real} 内嵌={G.sub_file_of(embedded)!r} " \
        f"旗子不对但带文件名={G.sub_file_of(stale)!r} " \
        f"未选中={G.sub_file_of(not_sel)!r} 文件没了={G.sub_file_of(gone)!r} 非字幕轨={G.sub_file_of(audio)!r}"


def g_out_path(tmp: str) -> tuple[bool, str]:
    d = os.path.join(tmp, "shots1")
    p1 = G.out_path(d, os.path.join(tmp, "a b:c/d*e?.mp4"), 12.5, 4, 480, 12)
    open(p1, "wb").write(b"x")
    p2 = G.out_path(d, "a b:c/d*e?.mp4", 12.5, 4, 480, 12)
    p3 = G.out_path(d, "x.mp4", 1, 2, 320, 8)
    b1, b2 = os.path.basename(p1), os.path.basename(p2)
    stem1 = os.path.splitext(b1)[0]
    illegal = set('\\/:*?"<>|') & set(b1 + b2 + os.path.basename(p3))
    return (p1 != p2 and not os.path.exists(p2) and b1 == stem1 + ".gif"
            and b2 == stem1 + "#1.gif" and not illegal
            and os.path.dirname(p1) == d and p3.endswith("x_1s_2s_320w8f.gif")
            and os.path.isdir(d)), \
        f"第一个={b1} 第二个={b2} 非法字符={sorted(illegal)} 另一个={os.path.basename(p3)}"


def g_build_cmd(tmp: str) -> tuple[bool, str]:
    c = G.build_cmd("ffmpeg.exe", "v.mp4", 12.5, 4.0, 480, 12, "o.gif", "clip.srt")
    j = " ".join(c)
    i_ss, i_in = c.index("-ss"), c.index("-i")
    i_t = c.index("-t")
    ok = (i_ss < i_in < i_t                       # 定位在输入前，时长在输入后
          and c[i_ss + 1] == "12.500" and c[i_t + 1] == "4.000"
          and "-progress" in c and "pipe:1" in c
          and c[c.index("-loop") + 1] == "0"
          and "palettegen" in j and "paletteuse" in j and "split[s0][s1]" in j
          and "subtitles=clip.srt" in j
          and "-an" in c and "-sn" in c and "-dn" in c
          and "-loglevel" in c and "error" in c[c.index("-loglevel") + 1]
          and not any("mpv" in x for x in c))
    no_sub = G.build_cmd("ffmpeg.exe", "v.mp4", 0, 2, 320, 8, "o.gif", "")
    clean = ("subtitles" not in " ".join(no_sub) and no_sub[no_sub.index("-t") + 1] == "2.000")
    return ok and clean, f"顺序 -ss<{i_ss} -i<{i_in} -t<{i_t} 无字幕那趟不含 subtitles={clean}"


def g_progress(tmp: str) -> tuple[bool, str]:
    a = G.parse_progress("out_time_us=2400000")
    b = G.parse_progress("out_time_ms=2400000")
    c = G.parse_progress("frame=  120 fps= 44 q=0.0")
    d = G.parse_progress("out_time=00:00:02.400000")
    e = G.parse_progress("  out_time_us=1000000  ")
    return (a == 2.4 and b is None and c is None and d is None and e == 1.0), \
        f"us={a} ms={b} frame={c} out_time={d} 带空白={e}"


def g_real_export(tmp: str) -> tuple[bool, str]:
    """真跑一趟：3 秒片段 → 解回来数帧、量宽、验循环、验临时文件都清了。"""
    if not os.path.exists(FF):
        return False, f"没找到 ffmpeg：{FF}"
    video = ramp(tmp)
    if not video:
        return False, "样片生成失败（geq 跑不起来）"
    out_dir = os.path.join(tmp, "shots2")
    store = G.GifStore(FF)
    done: list[tuple[bool, str, str]] = []
    store.sig_done.connect(lambda _i, ok, d, p: done.append((ok, d, p)))
    jid, err = store.start(video, 2.0, 3.0, out_dir, width=320, fps=10)
    if err:
        return False, f"开不成：{err}"
    if not pump(180, lambda: bool(done)):
        return False, f"180 秒没回话（job {jid}）"
    ok, detail, path = done[0]
    if not ok:
        return False, detail
    info = G.read_gif(path)
    left = sorted(os.listdir(out_dir))
    return (info["ok"] and info["frames"] >= G.expect_frames(3.0, 10) - 1
            and info["w"] == 320 and info["h"] == 180
            and loop_count(path) == 0
            and os.path.dirname(path) == out_dir
            and all(not f.endswith((".log", ".srt")) for f in left)), \
        f"{detail} 循环={loop_count(path)} 目录残留={left}"


def g_real_export_sub(tmp: str) -> tuple[bool, str]:
    """带字幕那一趟的**时间对齐**：导出原片 10~14 秒，cue 在 12~13.5 秒。

    两臂喂的是**同一份原样 SRT**——平移由引擎负责，测试不能自己先平移一次再交给它
    （第一版就是这么写的：手动平移 + 引擎再平移 = 错两格，等于把 bug 修进测试、把坏
    留在产品里，且这一条判据会指着正确的实现报"失败"）。两条腿缺一不可：
      · 引擎平移（sub_delta=True）必须**出字**，否则字幕根本没烧进去；
      · 引擎不平移（sub_delta=False，正是探针量过的那条坏写法）必须**不出字**，
        否则这条判据在两种写法下都成立——那是摆设。
    这正是探针量出来的结论（-ss 在 -i 前会把字幕滤镜看到的时间戳归零），这里钉成回归测试。
    """
    if not os.path.exists(FF):
        return False, f"没找到 ffmpeg：{FF}"
    video = ramp(tmp)
    if not video:
        return False, "样片生成失败"
    out_dir = os.path.join(tmp, "shots3")
    srt = os.path.join(tmp, "cue.srt")
    with open(srt, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PGS.SRT)

    def ink(store: G.GifStore, delta: bool, tag: str) -> list[float]:
        done: list[tuple[bool, str, str]] = []
        store.sig_done.connect(lambda _i, ok, d, pp: done.append((ok, d, pp)))
        jid, err = store.start(video, PGS.START, PGS.DUR, out_dir, width=320, fps=10,
                               sub_file=srt, sub_delta=delta)
        if err:
            raise RuntimeError(f"{tag} 开不成：{err}")
        if not pump(180, lambda: bool(done)):
            raise RuntimeError(f"{tag} 180 秒没回话")
        ok, detail, path = done[0]
        if not ok:
            raise RuntimeError(f"{tag} 跑挂：{detail}")
        # 只保留"这一帧有白字"的时刻；亮度反解出来的永远是**原片**秒数
        return [round(t, 2) for t, i in (PGS.measure(im) for im in PGS.frames_of(path))
                if i > 3]

    try:
        with_shift = ink(G.GifStore(FF), True, "平移")
        without = ink(G.GifStore(FF), False, "不平移")
    except RuntimeError as exc:
        return False, f"跑挂了：{exc}"
    lo, hi = PGS.CUE_A - 0.8, PGS.CUE_B + 0.8
    hit = [x for x in with_shift if lo <= x <= hi]
    return (bool(hit) and not without), \
        (f"平移后出字 {len(hit)}/{len(with_shift)} 帧（该落在原片 {PGS.CUE_A}~{PGS.CUE_B}），"
         f"不平移出字 {len(without)} 帧（必须 0，否则这条判据是摆设）")


def g_store_rejects(tmp: str) -> tuple[bool, str]:
    """四条"开不成"必须各报各的，不能混成一句"导出失败"。"""
    d = os.path.join(tmp, "shots4")
    a = G.GifStore(os.path.join(tmp, "no-such-ffmpeg.exe")).start("v.mp4", 0, 3, d)
    b = G.GifStore(FF).start(os.path.join(tmp, "no-such-video.mp4"), 0, 3, d)
    video = ramp(tmp)
    c = G.GifStore(FF).start(video, 0, -5, d)          # 时长被夹到下限以下？clamp 会抬到 0.5
    return (a[0] == 0 and "ffmpeg" in a[1] and b[0] == 0 and "不存在" in b[1]), \
        f"没装 ffmpeg={a} 视频不在={b} 负时长={c}"


def g_busy_gate(tmp: str) -> tuple[bool, str]:
    """一趟没跑完时第二趟必须被拒：并发两个 ffmpeg 会把解码线程占满，正放的片子先卡。"""
    video = ramp(tmp)
    if not video:
        return False, "样片生成失败"
    d = os.path.join(tmp, "shots5")
    store = G.GifStore(FF)
    jid, err = store.start(video, 0, 6.0, d, width=800, fps=20)
    if err:
        return False, f"第一趟就没开成：{err}"
    jid2, err2 = store.start(video, 0, 2.0, d, width=320, fps=8)
    got: list[bool] = []
    store.sig_done.connect(lambda _i, ok, _dl, _p: got.append(ok))
    pump(180, lambda: bool(got))
    return (jid2 == 0 and "在跑" in err2), f"第一趟 id={jid} 第二趟={jid2} 原因={err2!r}"


def g_cancel(tmp: str) -> tuple[bool, str]:
    """取消以后：报"已取消"，而且**不留下半截 GIF**。"""
    video = ramp(tmp)
    if not video:
        return False, "样片生成失败"
    d = os.path.join(tmp, "shots6")
    store = G.GifStore(FF)
    got: list[tuple[bool, str, str]] = []
    store.sig_done.connect(lambda _i, ok, dl, p: got.append((ok, dl, p)))
    # 挑一个跑得久的规格（宽 960、20fps、15 秒），好让 cancel 落在编码途中
    jid, err = store.start(video, 0, 15.0, d, width=960, fps=20)
    if err:
        return False, f"开不成：{err}"
    time.sleep(0.6)
    store.cancel()
    if not pump(120, lambda: bool(got)):
        return False, "取消以后 120 秒没回话"
    ok, detail, path = got[0]
    left = [f for f in os.listdir(d)] if os.path.isdir(d) else []
    return (not ok and "取消" in detail and not path and not left), \
        f"ok={ok} 详情={detail!r} 路径={path!r} 目录残留={left}"


def g_bad_output_not_kept(tmp: str) -> tuple[bool, str]:
    """起点越过片尾：ffmpeg 会正常退出但不产出（或产出空），这种"坏产物"必须被删掉。

    留着一个 0 字节／单帧文件，用户看到的就是一条"已导出"的假成功——比报错坏得多。
    """
    video = ramp(tmp)
    if not video:
        return False, "样片生成失败"
    d = os.path.join(tmp, "shots7")
    store = G.GifStore(FF)
    got: list[tuple[bool, str, str]] = []
    store.sig_done.connect(lambda _i, ok, dl, p: got.append((ok, dl, p)))
    jid, err = store.start(video, RAMP_SECONDS + 30, 2.0, d, width=320, fps=10)
    if err:
        return False, f"开不成：{err}"
    if not pump(120, lambda: bool(got)):
        return False, "120 秒没回话"
    ok, detail, path = got[0]
    left = os.listdir(d) if os.path.isdir(d) else []
    return (not ok and not path and not left), f"ok={ok} 详情={detail!r} 目录残留={left}"


def _py_sources() -> list[str]:
    d = os.path.join(ROOT, "app")
    return [os.path.join(r, f) for r, _s, fs in os.walk(d) if not os.path.basename(r).startswith("__")
            for f in fs if f.endswith(".py")]


_QT6_CALLS = (".waitForFinished(",)      # 本机 PyQt 5.15.2 上根本不存在（实测 dir() 只有 waitForDone）


def g_no_qt6_names(tmp: str) -> tuple[bool, str]:
    """Qt5/Qt6 改名留下的坑必须一次性扫干净，而且扫完要**真调一次**。

    为什么两条都要：四个 engine 的 wait() 都写成 `waitForFinished`（Qt6 的名字），本机是
    PyQt 5.15.2 → 每次调都是 AttributeError。它躲过了全部测试，因为别的套件明确不碰 wait()
    （见 tests/test_library.py 里那句"这里必须自己 pump"）——只扫源码会漏掉"扫到了但没人跑得动"，
    只调 GifStore 会漏掉另外三个文件。
    """
    hits = []
    for p in _py_sources():
        try:
            text = open(p, encoding="utf-8").read()
        except OSError:
            continue
        for bad in _QT6_CALLS:
            i = text.find(bad)
            if i >= 0:
                hits.append(f"{os.path.relpath(p, ROOT)} 第 {text[:i].count(chr(10)) + 1} 行")
    s = G.GifStore(FF)
    try:
        ret = s.wait(0)                  # 空闲时立刻返回；名字写错就在这里抛
    except AttributeError as exc:
        return False, f"源码扫描={hits}，但 GifStore.wait() 实测抛 AttributeError：{exc}"
    return (not hits and ret is True), f"扫描 {len(_py_sources())} 个源文件，命中={hits} 真调 wait() 回={ret}"


CHECKS = [
    ("clamp_range 两头都夹：超片尾收时长、时长未知不收、非法输入回合法值", g_clamp),
    ("expect_frames 用 round 不用 floor（8.2s@15 该 123 帧，少算一帧＝把下界放宽一格）",
     g_expect_frames),
    ("SRT 时间轴平移：跨分跨小时借位对、正文与空行不动、负数夹到 0", g_shift_srt),
    ("ASS 只动 Dialogue 的 Start/End（厘秒），Comment 与 Format 行一个字不改", g_shift_ass),
    ("认不出的字幕格式／空文本／一个时间戳都没有的文件一律回空串（宁可不烧）", g_shift_unknown_format),
    ("sub_file_of 只认「选中的外挂字幕且文件还在」，内嵌/旗子不对/未选中/文件没了/非字幕轨全回空",
     g_sub_file_of),
    ("产物名不带非法字符、撞名自动 #1，绝不覆盖已有文件", g_out_path),
    ("命令形状：-ss 在 -i 前、-t 在 -i 后、-progress pipe:1、-loop 0、调色板双通道、无字幕时不带 subtitles",
     g_build_cmd),
    ("进度只认 out_time_us（out_time_ms、frame=、out_time= 都不认，不猜）", g_progress),
    ("真跑一趟：帧数=时长×帧率、宽=请求宽、循环 0 次、目录里不留临时文件", g_real_export),
    ("真跑带字幕一趟：平移后的在 cue 的原片时刻出字，不平移的一个字都不出", g_real_export_sub),
    ("开不成的四种原因各报各的（没装 ffmpeg／视频不在／区间无效）", g_store_rejects),
    ("上一趟没跑完时第二趟被拒（并发两个 ffmpeg 会把解码线程占满）", g_busy_gate),
    ("取消：报「已取消」，不返回路径，也不留半截 GIF", g_cancel),
    ("坏产物一律删掉（0 字节或空输出不能算「已导出」）", g_bad_output_not_kept),
    ("engine 里不许留 Qt6 才有的线程池方法名，且 wait() 在这台机器上真调得动", g_no_qt6_names),
]


# ============================================================ 正常自检
# 这几条要真起 ffmpeg。没装时它们既不算 PASS 也不算 FAIL —— 报 ENV 并整轮 rc=3，
# 因为"功能坏了"和"这台机器没这个二进制"是两种完全不同的处置。
REAL = (g_real_export, g_real_export_sub, g_store_rejects, g_busy_gate, g_cancel,
        g_bad_output_not_kept)


def run(tmp: str) -> int:
    no_ff = not os.path.exists(FF)
    if no_ff:
        print(f"[ENV] 没装 ffmpeg（{FF}）：纯函数那几条照跑，真跑类记 ENV 并整轮判环境。")
    bad, env = [], 0
    for name, fn in CHECKS:
        if no_ff and fn in REAL:
            env += 1
            print(f"[ENV ] {name}  —— 需要 ffmpeg", flush=True)
            continue
        try:
            ok, detail = fn(tmp)
        except Exception as exc:  # noqa: BLE001 - 判据自己崩了也要留下是哪条
            ok, detail = False, f"判据自身抛 {type(exc).__name__}: {exc}"
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""),
              flush=True)
        if not ok:
            bad.append(name)
    print(f"\n合计 {len(CHECKS)} 条，失败 {len(bad)} 条" + (f"：{bad}" if bad else "")
          + (f"，环境挡掉 {env} 条" if env else ""))
    if no_ff:
        return 3
    return 1 if bad else 0


# ============================================================ 证伪
def falsify(tmp: str) -> int:
    """把某个实现换成"看起来对"的写法，对应那条自检必须翻红。翻不红＝那条判据是摆设。"""
    if not os.path.exists(FF):
        print(f"[ENV] 没装 ffmpeg（{FF}），真跑类的坏桩摆不出来，这轮不作数。")
        return 3
    saved = {k: getattr(G, k) for k in ("clamp_range", "expect_frames", "shift_srt", "shift_ass",
                                        "shift_sub_text", "sub_file_of", "out_path", "build_cmd",
                                        "parse_progress", "read_gif", "GifStore", "GifJob",
                                        "MIN_DURATION", "MAX_DURATION", "_ASS_DLG")}
    hits: list[tuple[str, bool, str]] = []
    covered: set = set()
    saved_sources = _py_sources

    def target(fn):
        for _n, f in CHECKS:
            if f is fn:
                try:
                    return fn(tmp)
                except Exception as exc:  # noqa: BLE001
                    return False, f"判据崩了 {type(exc).__name__}: {exc}"
        raise AssertionError(f"证伪表指向的函数不在 CHECKS 里：{fn.__name__}")

    def add(label, fn, stub):
        covered.add(fn)
        restore()
        stub()
        ok, detail = target(fn)
        hits.append((label, not ok, f"削弱后该条判据 ok={ok} —— {detail}"))
        restore()

    def restore():
        for k, v in saved.items():
            setattr(G, k, v)
        globals()["_py_sources"] = saved_sources      # 坏桩会换掉扫描范围，逐轮还原

    # D1 clamp_range 只夹下限不夹上限
    def d1():
        G.clamp_range = lambda s, d, L: (max(0.0, float(s or 0)), max(G.MIN_DURATION, float(d or 0)))
    add("D1 不夹片尾上限 → 超片尾必须被否", g_clamp, d1)

    # D2 expect_frames 用 floor（浮点尾差把下界放宽一格，短一截的 GIF 也算成功）
    def d2():
        G.expect_frames = lambda d, f: max(1, int(float(d) * int(f)))
    add("D2 floor 取帧数 → 8.2s@15 该 123 帧那条必须被否", g_expect_frames, d2)

    # D3 平移方向写反
    def d3():
        G.shift_srt = lambda t, delta: _raw_shift(t, -delta)
    def _raw_shift(t, delta):
        return saved["shift_srt"](t, delta)
    add("D3 平移方向反了 → 已知答案那条必须被否", g_shift_srt, d3)

    # D4 ASS 用毫秒当厘秒（差 10 倍）
    def d4():
        def wrong(text, delta):
            out, n = saved["shift_ass"](text, delta * 10.0)
            return out, n
        G.shift_ass = wrong
    add("D4 ASS 厘秒当毫秒 → Comment 不动/借位对那条必须被否", g_shift_ass, d4)

    # D5 未知格式原样退回（于是字幕会整体错位烧进 GIF）
    def d5():
        G.shift_sub_text = lambda t, ext, delta: t or ""
    add("D5 未知格式原样放行 → 宁可不烧那条必须被否", g_shift_unknown_format, d5)

    # D6 不看 external 旗子（内嵌轨也当外挂递进去）
    def d6():
        def naive(tracks):
            if not isinstance(tracks, list):
                return ""
            for t in tracks:
                if isinstance(t, dict) and t.get("type") == "sub" and t.get("selected"):
                    f = str(t.get("external-filename") or "")
                    if f and os.path.exists(f):
                        return f
            return ""
        G.sub_file_of = naive
    add("D6 认内嵌轨 → 只认外挂那条必须被否", g_sub_file_of, d6)

    # D7 不检查文件还在不在
    def d7():
        def naive(tracks):
            if not isinstance(tracks, list):
                return ""
            for t in tracks:
                if (isinstance(t, dict) and t.get("type") == "sub" and t.get("selected")
                        and t.get("external")):
                    return str(t.get("external-filename") or "")
            return ""
        G.sub_file_of = naive
    add("D7 文件没了照样回路径 → 存在性检查那半边必须红", g_sub_file_of, d7)

    # D8 产物名不带序号（直接覆盖用户的东西）
    def d8():
        G.out_path = lambda d, v, s, du, w, f: os.path.join(
            d, f"{os.path.splitext(os.path.basename(v))[0]}_{s}s.gif")
    add("D8 固定文件名会覆盖 → 撞名自动 #1 那条必须被否", g_out_path, d8)

    # D9 -ss 放到 -i 之后（精确但慢到不可用）
    def d9():
        def wrong(exe, video, start, dur, width, fps, out, sub_name=""):
            chain = (f"fps={fps},scale={width}:-1:flags=lanczos"
                     + (f",subtitles={sub_name}" if sub_name else "")
                     + ";split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse")
            return [exe, "-hide_banner", "-i", video, "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                    "-filter_complex", chain, out]
        G.build_cmd = wrong
    add("D9 -ss 在 -i 后 + 没有 -loop/-progress → 命令形状那条必须被否", g_build_cmd, d9)

    # D10 单遍编码（不出调色板，暗场糊成灰块）
    def d10():
        def wrong(exe, video, start, dur, width, fps, out, sub_name=""):
            chain = f"fps={fps},scale={width}:-1:flags=lanczos"
            if sub_name:
                chain += f",subtitles={sub_name}"
            return [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-progress", "pipe:1", "-ss", f"{start:.3f}", "-i", video,
                    "-t", f"{dur:.3f}", "-an", "-sn", "-dn", "-vf", chain, "-loop", "0", out]
        G.build_cmd = wrong
    add("D10 没有 palettegen/paletteuse → 双通道那条必须被否", g_build_cmd, d10)

    # D11 进度什么行都认（frame= 那行也当成秒数，进度条会跳到别处去）
    def d11():
        import re as _re

        def wrong(line):
            m = _re.search(r"(\d+(?:\.\d+)?)", str(line))
            return float(m.group(1)) if m else None
        G.parse_progress = wrong
    add("D11 连 frame= 行都当秒数 → 只认 out_time_us 那条必须被否", g_progress, d11)

    # D12 read_gif 只看文件存在（半截 GIF 也算成功）
    def d12():
        G.read_gif = lambda p: {"ok": os.path.exists(p), "frames": 1, "w": 0, "h": 0,
                                "bytes": os.path.getsize(p) if os.path.exists(p) else 0, "err": ""}
    add("D12 存在即成功 → 真跑那趟的帧数/尺寸判据必须被否", g_real_export, d12)

    # D13 循环次数写成 1（GIF 只动一遍就停，用户以为坏了）
    def d13():
        def wrong(*a, **kw):
            c = saved["build_cmd"](*a, **kw)
            c[c.index("-loop") + 1] = "1"
            return c
        G.build_cmd = wrong
    add("D13 -loop 1 → 命令形状那条必须被否", g_build_cmd, d13)

    # D14 带字幕时不平移时间轴（探针证过的那条坏）
    def d14():
        def naive(self, video, start, dur, out_dir, width=G.DEFAULT_WIDTH, fps=G.DEFAULT_FPS,
                  sub_file="", sub_delta=True):
            return saved["GifStore"].start(self, video, start, dur, out_dir, width, fps,
                                           sub_file, False)
        G.GifStore.start = naive
    add("D14 烧字幕不平移 → 出字那条必须被否（探针同一条结论）", g_real_export_sub, d14)

    # D15 没有并发闸门
    def d15():
        def naive(self):
            return False
        G.GifStore.busy = naive
    add("D15 busy 恒假 → 第二趟必须被拒那条翻红", g_busy_gate, d15)

    # D16 取消只置旗子不杀进程／回成功
    def d16():
        def naive(self, ok, detail, keep):
            self.sig.done.emit(self.jid, True, "已取消", self.out)
        G.GifJob._finish = naive
    add("D16 取消当成功并留产物 → 取消那条必须被否", g_cancel, d16)

    # D17 失败也留着产物（半截 GIF 会被当成"已导出"）
    def d17():
        def naive(self, ok, detail, keep):
            self.sig.done.emit(self.jid, ok, detail, self.out if ok else "")
        G.GifJob._finish = naive
    add("D17 不删坏产物 → 坏产物一律删掉那条必须被否", g_bad_output_not_kept, d17)

    # D18 start() 不检查 ffmpeg/视频存在
    def d18():
        def naive(self, video, start, dur, out_dir, width=G.DEFAULT_WIDTH, fps=G.DEFAULT_FPS,
                  sub_file="", sub_delta=True):
            params = {"exe": self.exe, "video": video, "start": float(start), "dur": float(dur),
                      "width": int(width), "fps": int(fps),
                      "out": G.out_path(out_dir, video, start, dur, width, fps),
                      "sub_src": "", "sub_name": "", "delta": 0.0}
            job = G.GifJob(1, params)
            from PyQt5.QtCore import QThreadPool
            QThreadPool.globalInstance().start(job)
            return 1, ""
        G.GifStore.start = naive
    add("D18 不校验环境就开跑 → 四条原因各报各的那条必须被否", g_store_rejects, d18)

    # D19 ASS 时间字段只认冒号（本次实测踩到的真 bug：规范是点分厘秒，
    # 于是整份 Dialogue 一行都匹配不上、shift_ass 静默回 0 处，字幕默默不烧）
    def d19():
        import re as _re
        G._ASS_DLG = _re.compile(r"^(Dialogue:\s*(?:[^,]*,){1,8}?)(\d+):(\d{2}):(\d{2}):?(\d{2})"
                                 r"(.*?,)(\d+):(\d{2}):(\d{2}):?(\d{2})(.*)$")
    add("D19 ASS 只认冒号分隔符 → 厘秒/借位/Comment 不动那条必须被否", g_shift_ass, d19)

    # D20 第一次撞名就出 #2（跟顶部承诺的 "#1 #2 …" 不一致，用户按名字找不出第几次导出）
    def d20():
        def wrong(d, v, s, du, w, f):
            p = saved["out_path"](d, v, s, du, w, f)
            return p if "#" in os.path.basename(p) else p.replace(".gif", "#2.gif")
        G.out_path = wrong
    add("D20 撞名从 #2 起 → 撞名自动 #1 那条必须被否", g_out_path, d20)

    # D21 扫描范围被换成"干净"的文件 → Qt6 名字那条必须翻红（不然这条判据是摆设：
    #     真实历史里四个 engine 文件全都写着 waitForFinished，而它一直没被抓出来，
    #     就是因为没有测试调用过 wait()。这里用一个已知含坏名字的样本来证扫描真的在扫。）
    def d21():
        bad = os.path.join(tmp, "bad_qt6.py")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("class X:\n    def wait(self):\n        self._pool.waitForFinished(200)\n")
        _py_sources_probe = lambda: [bad]          # noqa: E731 - 只是换掉扫描范围
        globals()["_py_sources"] = _py_sources_probe
    add("D21 已知含 waitForFinished 的样本 → Qt6 名字那条必须点名它", g_no_qt6_names, d21)

    bad = 0
    for label, hit, detail in hits:
        if not hit:
            bad += 1
        print(f"[{'CAUGHT' if hit else 'MISSED'}] {label}  —— {detail}", flush=True)
    holes = [n for n, fn in CHECKS if fn not in covered]
    for n in holes:
        print(f"[HOLE ] 这条判据没有任何坏桩指向它：{n}")
    print(f"\n证伪 {len(hits)} 组，漏网 {bad} 组，没人证的判据 {len(holes)} 条")
    return 1 if (bad or holes) else 0


def drain(seconds: float = 180.0) -> tuple[int, float]:
    """退出前把线程池排空，回 (还在跑的个数, 等了多久)。

    为什么必须有这一步：坏桩里 D15/D18 会真开出一趟 ffmpeg，主流程跑完时它还在后台。
    Python 解释器带着"线程池里还有活"退出时，Qt 在析构路径上 abort，回给调用方的 rc 是
    127——既不是"过了"也不是"哪条挂了"，只是进程自己没了。这种 rc 会把一次真成功读成失败、
    也会把一次真失败读成"崩了所以不知道"，所以判据的 rc 必须等所有后台任务落地再发。
    """
    from PyQt5.QtCore import QThreadPool
    pool = QThreadPool.globalInstance()
    t0 = time.time()
    while pool.activeThreadCount() and time.time() - t0 < seconds:
        APP.processEvents()
        time.sleep(0.05)
    pool.waitForDone(2000)
    for _ in range(20):                    # 让队列里的 done 信号有个落脚的地方
        APP.processEvents()
        time.sleep(0.02)
    return pool.activeThreadCount(), time.time() - t0


if __name__ == "__main__":
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="gelu-gif-")
    try:
        code = falsify(tmpdir) if "--falsify" in sys.argv else run(tmpdir)
        left, waited = drain()
        print(f"[exit] 后台线程池剩余 {left} 个，等了 {waited:.1f} 秒", flush=True)
        if left:
            print("[ENV] 还有导出任务没落地就退出：这一轮的 rc 会被进程析构盖掉，判环境不判功能。",
                  flush=True)
            code = 3
        sys.exit(code)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
