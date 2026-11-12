# -*- coding: utf-8 -*-
"""内核实测探针 F2（第 2 版）：均衡器在 mpv v0.41.0-1101 上要用哪条滤镜、哪种形状、热切真不真、代价多大。

为什么不能只看 `change-list af` 的应答：F1（`tests/probe_record.py`）刚踩过同款坑——
`set_property stream-record` 回 ok、`get_property` 把路径原样回报，盘上却一个字节都没有。
音频这一族更静默：`--af=lavfi=<写错的参数>` 时内核多半只丢一行 warn，`af` 属性照样回报那条链；
就算链装上了，参数名写错也可能被吞掉，滑轨拉到顶听起来却一动不动。
所以这一族只认**听得见的证据**：`--ao=pcm` 把内核真正送出去的那一路 PCM 落成 WAV，
再用 FFT 逐频带算能量。回报会骗人，频谱不会。

r13（`tests/out/eq-probe-r13.log`、留底 `eq-probe-r13.log.bak-*`）那一版有四处量具缺陷，这版逐条修：
  1. **ffmpeg 侧那条腿跑的是 mpv 的串**：`-af lavfi=bass=g=12:f=110` 里的 `lavfi` 不是 ffmpeg
     的音频滤镜名，于是 9 个候选**全部**回 `No such filter: 'lavfi'`——那一列自始至终没有
     鉴别力，却在报告里长得像"每个候选都试过"。现在 ffmpeg 侧吃**剥掉桥前缀后的图串**
     （`bass=g=12:f=110`），它才真的在验"本机 FFmpeg 有没有这个滤镜、参数名对不对"。
  2. **只有括号形状活下来、其它形状整趟没数据**：非括号形状让 mpv 在启动期就退出，而
     `_new()` 只说"内核起不来"，把内核原话丢了。现在失败也读 `--log-file`，把
     `Option af-set: …` 这类 `[e]/[w]` 原话记下来——"外壳拼链该照哪种写法"的答案就在那几行里。
  3. **热切腿的媒体比管线吞吐短**：ao=pcm 不以实时速度跑，8 秒的片在 0.4 秒就 EOF
     （r13 基线 log 原文 `filter output EOF @0.406`），所以"2 秒时挂上滤镜"挂在一个已经放完的
     进程上，那一腿量不到任何东西。现在用 90 秒长媒体，先标定吞吐，切点用发命令当下读回的
     time-pos，前后各取同样长的窗口比，并且**明写切点是否落在产物末尾之前**——落不进去就说"没证据"。
  4. **一把尺没有对照**：r13 里"bass+12 → 低频 +11.7 dB"是唯一有数据的一格，但没人证过那条链
     真的经过了滤镜（也可能是 ao=pcm 直接落解码后的原始数据）。现在加两枚对照：`anull`
     （该处处 ≈0）与 `volume=0`（该整条塌到本底）。对照翻红＝量具可信；对照不动＝这一族
     所有频谱读数作废，报告里会直接这么写。

要回答的六件事：
  ① 形状：`lavfi=[…]` / `lavfi=graph="…"` / 裸 `bass=…`（手册 p189"不是内置滤镜就自动试 lavfi 桥"）/
     `lavfi-bass=…`（手册 p189 的强制前缀）/ `lavfi=bass=…`（r13 产品当时用的写法）/ 引号包整串，
     各自的下场，失败的带内核原话；
  ② 滤镜：bass/treble/equalizer/anequalizer/superequalizer 在括号图里能不能装、装上去逐带 dB
     的变化和标称增益对不对得上（参数名与范围照抄 `ffmpeg -h filter=<name>`，本轮已量）；
     坏参数名、越界增益、不存在的滤镜名分别是"报错"还是"静默不动"；
  ③ 链长：10 段 equalizer 用逗号串在**同一个括号里**能不能活（`,` 同时是 mpv 切 af 列表的分隔符，
     vf 那族已经栽过一次——见 transform.py 第 7 条 `min(a,b)` 之死）；
  ④ 热切：播放中 `change-list af set` 之后，产物 WAV 切点前后频带是否真的不同；
  ⑤ 换片残留 / 复位形状（面板那颗"一键复位"要发什么）；
  ⑥ 代价：内核侧吞吐（产物里的音频秒数 ÷ 播放段墙钟秒，同一台 ao=pcm 内核跑同一条媒体）
     ——这是**内核内**的相对代价，不是 r13 那种 ffmpeg 离线代理。

**本探针不做断言**：产出是抄进事实表的表。跑法（会开一个 mpv 自己的窗口，热切那两腿要靠它把
管线拖到实时；无窗口时那两腿会自己报告"没证据"，不会假绿）：
    PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_eq.py > tests/out/eq-probe-r15.log 2>&1
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def say(*a) -> None:
    """每行当场落盘：长趟跑到第几条必须是看盘上文件就知道，而不是看终端重放。"""
    print(*a, flush=True)


from PyQt5.QtWidgets import QApplication                      # noqa: E402

from app import config as cfgmod                              # noqa: E402
from app.engine.player import MpvPlayer                       # noqa: E402

OUT = os.path.join(HERE, "out", "eq-probe")
NOISE = os.path.join(OUT, "pink-8s.wav")        # 短媒体：启动期各档用它（1.5 MB/趟）
LONG = os.path.join(OUT, "pink-90s.wav")        # 长媒体：热切腿的"快抽干对照"（证明这条腿量不到）
# 热切的**主证据腿**：带视频轨的 60 s 片 + `--force-window=yes`。
# 为什么非要一份"有画面的片子"（下面那三个"不管用"的读数是 2026-10-12 一把一次性量具量的，
# 脚本用完就删了、没留复现入口——所以这里只把结论抄在注释里，**不作数**；
# 留底的复现入口是本脚本自己的 ④a/④b 两条腿，见 tests/out/eq-probe-r15b.log）：
#   ao=pcm 落盘不以实时跑——90 s 粉噪在 +0.50 s 就已经 EOF（吞吐 ≥180×），于是"1.2 s 时发
#   change-list"发在一个早就放完的进程上，切点落在产物之外，那一腿和 r13 一样量不到东西。
#   `--speed=0.05` 不管用（实测 +1.09 s 就把 90 s 全写完，time-pos 直接 90.0）；
#   `--force-window=yes --no-video` 也不管用（+0.50 s time-pos=10.0，照样秒抽干）；
#   只有 `--force-window=yes` **保留视频轨**时才被画面节奏拖到 ≈1× 实时
#   （实测 time-pos 0.3→2.833 对应墙钟 0.52→3.03 s，WAV 从 524288 涨到 1097728）。
#   这一条也顺带说明：热切这件事的真相只有在"按真实节奏放"的管线上才量得出来。
AV = os.path.join(OUT, "av-60s.mp4")            # 视频 testsrc2 + 粉噪音频，60 s
AV2 = os.path.join(OUT, "av2-30s.mp4")          # 另一份（视频图案不同、音频同为粉噪），给"换片残留"腿
FFMPEG = cfgmod.FFMPEG_EXE
SR = 48000
BANDS = [(20, 60), (60, 150), (150, 350), (350, 800), (800, 1600),
         (1600, 3200), (3200, 6000), (6000, 12000), (12000, 20000)]
WIN_SEC = 5.0        # 每档只量固定长度的一段：各趟 WAV 长短不齐也不影响逐带可比

# ① 形状普查：同一条 bass+12@110，六种写法。graph 列是喂给 ffmpeg 那条腿的对照串。
SHAPES = [
    ("括号形状 lavfi=[…]",         'lavfi=[bass=g=12:f=110]',          "bass=g=12:f=110"),
    ("命名参数 lavfi=graph=\"…\"",  'lavfi=graph="bass=g=12:f=110"',    "bass=g=12:f=110"),
    ("不带前缀（内核自动试桥）",    'bass=g=12:f=110',                  "bass=g=12:f=110"),
    ("强制桥前缀 lavfi-bass=…",     'lavfi-bass=g=12:f=110',            "bass=g=12:f=110"),
    ("裸 lavfi=（r13 产品写法）",   'lavfi=bass=g=12:f=110',            "bass=g=12:f=110"),
    ("引号包整串 lavfi=\"…\"",      'lavfi="bass=g=12:f=110"',          "bass=g=12:f=110"),
]

# ②③ 滤镜普查：统一走括号形状。第三列=判读参考，不做断言。
EQ10 = ",".join(f"equalizer=f={f}:t=h:w={max(int(f * 0.45), 8)}:g=12"
                for f in (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000))
FILTERS = [
    ("对照 anull（该处处 ≈0）",      "anull",                              "全带 ~0"),
    ("对照 volume=0（该塌到本底）",  "volume=0",                           "全带 -几十"),
    ("bass +12 @110",               "bass=g=12:f=110",                    "抬 20-350"),
    ("bass -12 @110",               "bass=g=-12:f=110",                   "压 20-350"),
    ("treble -12 @3k",              "treble=g=-12:f=3000",                "压 3k 以上"),
    ("equalizer 1k +12 Q=1",        "equalizer=f=1000:t=q:w=1:g=12",      "抬 600-1.6k"),
    ("equalizer 31Hz +12 带宽20Hz", "equalizer=f=31:t=h:w=20:g=12",       "抬 20-60"),
    ("superequalizer 5b=×12",       "superequalizer=5b=12",               "262Hz 在 150-350 带，抬"),
    ("anequalizer 三段",            "anequalizer=f=100:t=h:w=100:g=12"
                                    "|f=1000:t=h:w=200:g=-12"
                                    "|f=6000:t=h:w=1000:g=6",             "已知会挂：本机 FFmpeg N-127213 的 anequalizer 只暴露 params 单串，"
                                                                           "手册那种 band|band 写法与引号/反斜杠共六种形状在 ffmpeg 自己那关就全报"
                                                                           " Option not found（2026-10-12 一次性量具试过）⇒ 不选它"),
    ("10 段 equalizer（逗号＝内核的 af 列表分隔符，见 ③b 两种拼法）", EQ10,
     "十带都该抬；但**拼法决定成败**：一个括号里放带逗号的图会被内核按 ',' 劈成多条 af 条目，"
     "实测报 `Option af-set: bass isn't supported.` + `Error parsing options`（本机 lavfi 桥不认图串"
     "内部的引号转义），所以产品侧要拼成 `lavfi=[eq1],lavfi=[eq2],…`——逗号让内核自己切，"
     "切完每条都是合法的单滤镜括号图。10 段能不能真的都生效，看这一档的频谱"),
    ("10 段 + 前置 −9 dB（削顶的解药）", EQ10 + ",volume=-9dB",
     "十带都抬但 clip 该回 0、RMS 该比上一档低 ~9 dB：这一条是给面板的 preamp 定 policy 的"),
    ("坏参数名 gain=",               "bass=gain=12:f=110",                  "?"),
    ("越界增益 g=9999",              "bass=g=9999:f=110",                   "?（范围 -900..900）"),
    ("不存在的滤镜名",               "definitely_not_a_filter=g=1",         "?"),
    ("空图 lavfi=[]",                "",                                    "?"),
]


def _spec_of(graph: str) -> str:
    """图串 → 发往内核的 `af` 值。

    带逗号的一律拆成**逗号分隔的多条 `lavfi=[单滤镜]` 条目**：本机实测（④ 之前的 ③b 那档留了
    原始 log）内核在解析 `af` 这个列表选项时**先按 ',' 切条目**，所以 `lavfi=[a,b]` 里的 `b`
    会作为第二条 af 条目被当成 mpv 内置滤镜名去找，直接 `isn't supported`。
    反过来，`lavfi=[a],lavfi=[b]` 切出来正好是两条各自合法的括号图，且 mpv 会把 af 列表按顺序串成链。
    这一条和 `transform.py` 第 7 条（`min(a,b)` 在 `--vf` 里必死）是同一个机制的两种死法。
    """
    if "," in graph:
        return ",".join(f"lavfi=[{g}]" for g in graph.split(",") if g)
    return f"lavfi=[{graph}]" if graph else "lavfi=[]"


def _pump(app, secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def _req(p, command: list, timeout: float = 2.5):
    """裸 IPC：返回 (应答, 错误串)。产品侧的 `MpvPlayer._req()` 把 IpcError 咽进 sig_log，
    探针要的正是那句原话（"命令被拒"和"命令没送到"在内核侧是两种下场），所以直接走 `_ipc`。"""
    try:
        return p._ipc.request(command, timeout=timeout), ""
    except Exception as exc:
        return None, f"✗{type(exc).__name__}: {str(exc)[:100]}"


def _rms_db(a: np.ndarray) -> float:
    if a.size == 0:
        return float("nan")
    return float(20 * np.log10(max(float(np.sqrt(np.mean(a.astype(np.float64) ** 2))), 1e-12)))


def _read_wav(path: str) -> tuple[np.ndarray, int] | None:
    """自己解析 RIFF，不用 `wave` 模块。

    为什么不用（r14 第一趟就是栽在这儿）：内核给滤镜链挑了 float 输出格式之后
    （log 原文 `48000Hz s16:stereo → flt:stereo`），ao=pcm 落的是 **WAVE_FORMAT_EXTENSIBLE**，
    Python 的 `wave.open` 直接回 "unknown extended format: 00000003-…"，于是
    `volume=0` 与 `superequalizer` 两格的频谱凭空变成"读不出"——那是量具瞎，不是内核没数据。
    这一族的编码本来就不由探针决定（滤镜会改采样格式），所以按文件头认，别预设"我传了 s16"。
    认不出来的编码要显式说"读不出"，不许静默读出一片假频谱。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        say(f"        WAV 读不出：{type(exc).__name__}: {exc}")
        return None
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        say(f"        WAV 读不出：不是 RIFF/WAVE（前 12 字节={data[:12]!r}）")
        return None

    def u16(b: bytes) -> int:
        return int.from_bytes(b, "little")

    def u32(b: bytes) -> int:
        return int.from_bytes(b, "little")

    fmt: dict | None = None
    doff = dsize = -1
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        sz = u32(data[pos + 4:pos + 8])
        body = data[pos + 8:pos + 8 + sz]
        if cid == b"fmt " and len(body) >= 16:
            tag = u16(body[0:2])
            sub = tag
            bits = u16(body[14:16])
            if tag == 0xFFFE and len(body) >= 26:          # extensible：真正的编码在 SubFormat GUID 头两个字节
                sub = u16(body[24:26])
                bits = u16(body[18:20]) or bits            # wValidBitsPerSample 优先
            fmt = {"ch": u16(body[2:4]), "sr": u32(body[4:8]), "bits": bits, "tag": tag, "sub": sub}
        elif cid == b"data":
            doff, dsize = pos + 8, sz
        pos += 8 + sz + (sz & 1)
    if fmt is None or doff < 0:
        say(f"        WAV 读不出：没找到 fmt/data 块（fmt={fmt} data_off={doff}）")
        return None
    raw = data[doff:doff + dsize]
    sw = max(fmt["bits"], 1) // 8
    is_float = fmt["sub"] == 3
    if is_float and sw == 4:
        a = np.frombuffer(raw[:len(raw) // 4 * 4], dtype="<f4").astype(np.float64)
    elif is_float and sw == 8:
        a = np.frombuffer(raw[:len(raw) // 8 * 8], dtype="<f8")
    elif sw == 1:
        a = (np.frombuffer(raw, dtype="u1").astype(np.float64) - 128.0) / 128.0
    elif sw == 2:
        a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 3:
        n = len(raw) // 3 * 3
        b = np.frombuffer(raw[:n], dtype="u1").astype(np.int64).reshape(-1, 3)
        v = (b[:, 0].astype(np.int64) | (b[:, 1].astype(np.int64) << 8)
             | (b[:, 2].astype(np.int64) << 16))
        v = np.where(v & 0x800000, v - (1 << 24), v)
        a = v.astype(np.float64) / float(1 << 23)
        return _finish(a, fmt["sr"], fmt)
    elif sw == 4 and not is_float:
        a = np.frombuffer(raw[:len(raw) // 4 * 4], dtype="<i4").astype(np.float64) / float(1 << 31)
        return _finish(a, fmt["sr"], fmt)
    else:
        say(f"        不支持的编码 tag={fmt['tag']} sub={fmt['sub']} {fmt['bits']} bit")
        return None
    return _finish(a, fmt["sr"], fmt)


def _finish(a: np.ndarray, sr: int, fmt: dict) -> tuple[np.ndarray, int]:
    """归一到 [-1,1] 的混合单声道（多声道取平均：带间比较不受影响，频谱一把尺量所有档）。"""
    ch = max(int(fmt.get("ch", 1) or 1), 1)
    if ch > 1:
        n = a.size // ch * ch
        if n:
            a = a[:n].reshape(-1, ch).mean(axis=1)
    return a, sr



def _bands_of(a: np.ndarray, sr: int) -> dict | None:
    """同一把尺（Hann 窗 rFFT、带内平均幅度）作用在给定那一段信号上。"""
    if a.size < sr // 20:
        return None
    sp = np.abs(np.fft.rfft(a * np.hanning(a.size)))
    fr = np.fft.rfftfreq(a.size, d=1.0 / sr)
    out = {"_rms": _rms_db(a), "_samples": int(a.size), "_sr": sr,
           "_peak": float(np.max(np.abs(a))) if a.size else 0.0,
           "_clip": int(np.count_nonzero(np.abs(a) >= 0.999))}
    for lo, hi in BANDS:
        m = (fr >= lo) & (fr < hi)
        out[(lo, hi)] = float(20 * np.log10(max(float(np.mean(sp[m])) if m.any() else 0.0, 1e-12)))
    return out


def _bands(path: str, upto: float = WIN_SEC) -> dict | None:
    got = _read_wav(path)
    if got is None:
        return None
    a, sr = got
    return _bands_of(a[:int(upto * sr)], sr)


def _sz(path: str) -> int:
    """产物当前字节数（没有就 -1）：热切/换片那两腿要按**写盘进度**定位窗口，不是按墙钟。"""
    return os.path.getsize(path) if os.path.exists(path) else -1


def _win(a: np.ndarray, sr: int, start_sec: float, dur: float = WIN_SEC) -> dict | None:
    """已读进来的音频里，从 `start_sec` 起取 `dur` 秒量频谱（窗口落在片外就返回 None）。"""
    st = int(max(float(start_sec), 0.0) * sr)
    n = int(dur * sr)
    if st + n > a.size or st >= a.size:
        return None
    return _bands_of(a[st:st + n], sr)


def _fmt(b: dict | None, base: dict | None = None) -> str:
    if b is None:
        return "（读不出）"
    cells = []
    for lo, hi in BANDS:
        v = b[(lo, hi)]
        d = f"({v - base[(lo, hi)]:+.1f})" if base else ""
        nm = f"{lo // 1000}k" if lo >= 1000 else str(lo)
        nh = f"{hi // 1000}k" if hi >= 1000 else str(hi)
        cells.append(f"{nm}-{nh}:{v:.1f}{d}")
    return (f"RMS={b['_rms']:.1f}dBFS peak={b['_peak']:.3f} clip={b['_clip']} " + " ".join(cells))


def _ffmpeg_offline(graph: str | None, dur: float = 20.0) -> tuple[str, float]:
    """② 的第二路证据 + ③ 的离线墙钟：同一条图在 ffmpeg 侧跑一遍，报 rc 与耗时。
    r13 在这一列传的是 mpv 的串（`lavfi=…`），所以那一列 9 行全回 No such filter: 'lavfi'，等于没测。"""
    af = ["-af", graph] if graph else []
    cmd = [FFMPEG, "-v", "error", "-f", "lavfi", "-i",
           f"anoisesrc=color=pink:sample_rate={SR}:amplitude=0.5", *af,
           "-t", f"{dur}", "-f", "null", "-"]
    t = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=240, text=True,
                           encoding="utf-8", errors="replace")
        err = (r.stderr or "").strip()
        return (f"rc={r.returncode}" + (f" 报错={err[:160]}" if err else ""), time.monotonic() - t)
    except Exception as exc:
        return f"跑崩 {type(exc).__name__}: {str(exc)[:120]}", 0.0


_ERR_KEYS = ("lavfi", "af-set", "af-add", "af-pre", "option af", "filter", "disabl")


def _pick_err_lines(txt: str, limit: int = 8) -> str:
    """从内核 log 里挑"af 这一族的下场"那几行。

    两件事必须一起做，r14 头一趟两样都踩过：
      ① `[v]` 级的逐帧噪音不留——它会把有用的那 3 行挤出去；
      ② `--msg-level=af=v` 时 lavfi 会把**滤镜图 ASCII 画**打出来（`mpv_src_in0:default--[48000Hz
         s16:stereo]--default| auto_aresample_0 |…`），一次十几行，把 `[e]` 那两行顶出预算，
         于是"内核 log"这一列全是画、没有原话。这里先剔画（含 `--[` / `]--` / `-----` 的行），
         `[d][lavfi]` 只在带 parsing/failed/no such/error 时才留。
    """
    out = []
    for ln in txt.splitlines():
        t = ln.strip()
        if not t:
            continue
        low = t.lower()
        if ("-----" in t) or ("--[" in t) or ("]--" in t) or t.startswith("|") or "abuffer" in low:
            continue
        if "[d][lavfi]" in t and not any(k in low for k in ("parsing", "failed", "no such", "error")):
            continue
        if ("[e]" in t or "[w]" in t or "[f]" in t
                or any(k in low for k in _ERR_KEYS)):
            out.append(t)
    return " ⏎ ".join(out[-limit:])[:900] or "（log 里没有错误/警告级、也没有 af 相关行）"


def _new(app, extra: list[str], logs: list[str]) -> MpvPlayer | None:
    p = MpvPlayer(cfgmod.Config())
    p.sig_log.connect(logs.append)
    orig = p.build_args
    p.build_args = lambda hwnd: [*orig(hwnd), "--msg-level=af=v,af_lavfi=v,ao=v,cplayer=v", *extra]
    if p.start(hwnd=0):
        return p
    say("  内核起不来，原话：" + _err_text(p))
    return None


def _err_text(p: MpvPlayer) -> str:
    """失败启动时内核留在 --log-file 里的原话（mpv 每次启动会重写这份文件，所以它就是这一趟的）。"""
    try:
        with open(p.log_path, encoding="utf-8", errors="replace") as fh:
            return _pick_err_lines(fh.read(), 5)
    except OSError as exc:
        return f"（log 读不出：{exc}）"


def _render(app, media: str, out_wav: str, af: str | None, media_dur: float,
            hold: float = 12.0, mid: tuple | None = None,
            extra: list[str] | None = None, pace: bool = False) -> dict:
    """跑一趟内核：ao=pcm 落成 WAV，**抽干就停**（产物大小连续四次不涨）。

    为什么自己盯抽干而不是固定 pump 若干秒（r14 头一趟的"吞吐 3.2×"就是这么来的假数）：
    ao=pcm 不是实时的，8 秒片在 ~0.4 秒就 EOF，固定 2.5 秒的窗口里绝大部分时间在等一个
    已经结束的播放，于是"墙钟"量的其实是 `hold` 这个常数，各档一模一样、毫无鉴别力。
    现在 `drain` 量的是"从 open_files 到产物不再长"，`drained` 说明这趟到底抽干了没有
    ——只有抽干了，`media_dur/drain` 才是吞吐；没抽干就得写"这档更贵所以更慢"而不是比值。

    `mid=(几秒后, af 链)` 用于热切腿：发命令前后的播放位置一起记下来，产物才切得开前后半。
    `pace=True` 是"按真实节奏放"的那条腿（配 `--force-window=yes` + 带视频轨的媒体）：
    它**不该**抽干，所以不盯文件大小提前退出，并且另外记 `end_pos`——`time-pos` 随墙钟的
    走法就是这一腿的节拍表，节拍不成立（内核还是全速抽干）时热切的切点就不作数。
    """
    if os.path.exists(out_wav):
        os.remove(out_wav)
    args = ["--ao=pcm", f"--ao-pcm-file={out_wav}"] + ([f"--af={af}"] if af else []) + list(extra or [])
    logs: list[str] = []
    p = _new(app, args, logs)
    if p is None:
        return {"ok": False, "why": "内核起不来", "log": " | ".join(logs[-2:])[:200]}
    res = {"ok": True, "log": "", "af": None, "switched_at": None, "cmd": None,
           "media_dur": media_dur, "pace": pace}
    try:
        mark = os.path.getsize(p.log_path) if os.path.exists(p.log_path) else 0
        t0 = time.monotonic()
        p.open_files([media])
        at, chain = mid if mid else (None, None)
        res["_at"] = at
        switched = mid is None
        last, stable, drain, drained = -1, 0, None, False
        while time.monotonic() - t0 < hold:
            _pump(app, 0.1)
            el = time.monotonic() - t0
            if not switched and el >= at:
                switched = True
                res["before_pos"] = p.get("time-pos")
                # 切点的**真位置**看产物字节，不看 time-pos：`--force-window` 那趟实测
                # 音频管线会跑在 time-pos 前面（按 time-pos 定位的前半窗口 peak=1.126、低频已经抬了，
                # 说明发命令那一刻产物里早就写进去了好几秒"已挂滤镜"的音频）。字节刻度是落盘顺序本身。
                res["cut_bytes"] = _sz(out_wav)
                _r, err = _req(p, ["change-list", "af", "set", chain], timeout=3.0)
                res["cmd"] = err or "ok"
                _pump(app, 0.3)
                res["switched_at"] = p.get("time-pos")
                res["af"], _e = _req(p, ["get_property", "af"])
            sz = os.path.getsize(out_wav) if os.path.exists(out_wav) else -1
            if sz > 0 and sz == last:
                stable += 1
            else:
                stable = 0
                last = sz
            if not pace and sz > 0 and stable >= 4:
                drain, drained = el, True
                break
        res["sent"] = switched
        res["end_pos"] = p.get("time-pos")
        res["wall"] = time.monotonic() - t0
        res["drain"] = drain if drain is not None else time.monotonic() - t0
        res["drained"] = drained
        try:
            with open(p.log_path, encoding="utf-8", errors="replace") as fh:
                fh.seek(mark)
                res["log"] = _pick_err_lines(fh.read())
        except OSError as exc:
            res["log"] = f"（log 读不出：{exc}）"
    finally:
        p.quit()
        _pump(app, 0.4)
    res["size"] = os.path.getsize(out_wav) if os.path.exists(out_wav) else -1
    return res


def _tp(run: dict) -> str:
    """吞吐／节拍那一列的措辞：只有真抽干了才配写"×实时"，节拍腿要报"time-pos 走了多远"。"""
    if run.get("pace"):
        w = run.get("wall", 0.0)
        e = run.get("end_pos")
        try:
            ep = float(e)
        except (TypeError, ValueError):
            return f"墙钟 {w:.2f}s，但 time-pos 读不回来（{e!r}）→ 节拍不明，切点不可信"
        return (f"墙钟 {w:.2f}s 里 time-pos 走到 {ep:.2f}s → 节奏≈{ep / max(w, 1e-6):.2f}× 实时"
                f"{'（≈1 才说明这腿真是按播放速度放的）' if 0.5 < ep / max(w, 1e-6) < 1.6 else '（离 1 很远：内核没被画面拖住，切点位置不可信）'}")
    if not run.get("drained"):
        return f"墙钟 {run.get('drain', 0):.2f}s 内**没抽干**（产物还在长）→ 只能定性：比上一档贵"
    return (f"抽干 {run['media_dur']:.0f}s 音频用了 {run['drain']:.2f}s → 吞吐≈"
            f"{run['media_dur'] / max(run['drain'], 1e-6):.1f}× 实时")


def _hot_report(wav: str, run: dict, guard: float = 1.5, win: float = WIN_SEC) -> None:
    """把热切产物按切点切成"挂之前"和"挂之后"两段，各取同样长的一截比频谱。

    切点位置**按产物字节算**，不按 `time-pos`：r15 第一趟实测到 `--force-window` 那腿的
    "前半（未挂）"窗口 peak=1.126、20-60Hz 已经 +11.5 dB——那就是"未挂"里混进了挂了之后的音频，
    因为音频管线跑在 `time-pos` 报出的位置前面（ao=pcm 一直接帧，视频那头才按节奏拖）。
    字节刻度没有这个歧义：`cut_bytes` 是发命令那一刻产物的大小，`size` 是收工后的大小，
    两者之比 × 产物总秒数就是切点在**文件里**的位置。前提是全程 bytes/sec 不变（同一格式），
    这一条当场核：产物秒数（按文件头算）与该档媒体的已播秒数对得上才用。

    三条诚实性闸门，缺一条就直接说"这腿没有证据"而不是硬给一个数：
      1. 命令真发出去了（`sent`）；
      2. 切点落在产物中段，且两侧各放得下一个完整窗口（还要留 `guard` 秒余量给在飞的帧）；
      3. 两窗口都取得出来（`_win` 越界返回 None）。
    """
    if not run.get("sent", True):
        say(f"     命令根本没发出去（这趟在 {_fmt_sec(run.get('drain'))}s 就抽干退出了，"
            f"而计划在第 {run.get('_at')}s 才发）⇒ 这腿没有证据")
        return
    got = _read_wav(wav)
    if got is None:
        say("     产物读不出 ⇒ 这腿没有证据")
        return
    a, sr = got
    total = a.size / sr
    cb, end = run.get("cut_bytes"), run.get("size")
    try:
        sw = float(cb) / float(end) * total
    except (TypeError, ValueError, ZeroDivisionError):
        say(f"     没记到字节刻度（cut_bytes={cb!r} size={end!r}）⇒ 退到 time-pos 定位，"
            f"切点位置有歧义、这腿只作参考")
        sw = float(run.get("switched_at") or 0.0)
    pre_start, post_start = sw - guard - win, sw + guard
    ok = sw > guard + win and post_start + win <= total
    say(f"     产物 {end} 字节 / 切点在第 {cb} 字节 → 文件里的切点 {sw:.1f} s（time-pos 报的是 "
        f"{run.get('switched_at')}）｜ 前半取 [{pre_start:.1f},{pre_start + win:.1f}] "
        f"后半取 [{post_start:.1f},{post_start + win:.1f}] ｜ "
        f"{'两侧都放得下' if ok else '放不下 ⇒ 这腿没有证据（媒体太短、切点太靠边，或内核没按节奏走）'}")
    if not ok:
        return
    pre = _win(a, sr, pre_start, win)
    post = _win(a, sr, post_start, win)
    if pre is None or post is None:
        say(f"     窗口取不出来（pre={pre is not None} post={post is not None}）⇒ 这腿没有证据")
        return
    say(f"     前半（未挂）：{_fmt(pre)}")
    say(f"     后半（已挂）：{_fmt(post, pre)}")


def _fmt_sec(v) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "?"


def _pace_cal(app, media: str, dur: float) -> dict | None:
    """这份带视频轨的媒体在这儿到底按不按实时走：**先单独量一趟不挂滤镜的节拍**。
    没有这一趟，后面两档的"切点"就没有可信度来源——节拍是量具的前提，不是被测项。"""
    wav = os.path.join(OUT, "pace-base.wav")
    run = _render(app, media, wav, None, dur, hold=dur + 8.0,
                  extra=["--force-window=yes"], pace=True)
    if not run.get("ok"):
        say("   节拍标定腿：内核起不来")
        return None
    say(f"   节拍标定腿（不挂 af，{os.path.basename(media)}）：{_tp(run)}  产物={run.get('size')} 字节")
    say(f"   内核 log：{run.get('log', '')[:200]}")
    return run


def main() -> int:
    if not os.path.exists(FFMPEG):
        say("没有 ffmpeg.exe，这趟的量频谱腿跑不了")
        return 2
    os.makedirs(OUT, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])

    # ---- 0. 造被测媒体 ----
    for path, dur, chans in ((NOISE, 8, 2), (LONG, 90, 1)):
        if not os.path.exists(path):
            r = subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                                f"anoisesrc=color=pink:duration={dur}:sample_rate={SR}:amplitude=0.5",
                                "-ac", str(chans), "-c:a", "pcm_s16le", path],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            say(f"# 造 {os.path.basename(path)}：rc={r.returncode} {(r.stderr or '').strip()[:120]}")
        try:
            say(f"# 媒体 {os.path.basename(path)}：{os.path.getsize(path)} 字节")
        except OSError as exc:
            say(f"# 媒体 {os.path.basename(path)}：读不到（{exc}）")
    # 热切主证据腿的那份 A/V：音频用同一份粉噪（和上面各档同一把尺），视频轨只为把节奏拖到实时。
    if not os.path.exists(AV):
        r = subprocess.run([FFMPEG, "-v", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=60",
                            "-f", "lavfi", "-i",
                            f"anoisesrc=color=pink:sample_rate={SR}:amplitude=0.5:duration=60",
                            "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                            "-shortest", AV],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        say(f"# 造 {os.path.basename(AV)}（热切腿的实时媒体）：rc={r.returncode} "
            f"{(r.stderr or '').strip()[:160]}")
    try:
        say(f"# 媒体 {os.path.basename(AV)}：{os.path.getsize(AV)} 字节")
    except OSError as exc:
        say(f"# 媒体 {os.path.basename(AV)}：读不到（{exc}）——热切腿只能退到'没证据'那一档")
    # 换片残留腿的第二份：视频图案换成 smptebars、音频还是同一把尺能量的粉噪。
    if not os.path.exists(AV2):
        r = subprocess.run([FFMPEG, "-v", "error", "-y",
                            "-f", "lavfi", "-i", "smptebars=size=640x360:rate=25:duration=30",
                            "-f", "lavfi", "-i",
                            f"anoisesrc=color=pink:sample_rate={SR}:amplitude=0.5:duration=30",
                            "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                            "-shortest", AV2],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        say(f"# 造 {os.path.basename(AV2)}（换片腿的第二份）：rc={r.returncode} "
            f"{(r.stderr or '').strip()[:160]}")
    try:
        say(f"# 媒体 {os.path.basename(AV2)}：{os.path.getsize(AV2)} 字节")
    except OSError as exc:
        say(f"# 媒体 {os.path.basename(AV2)}：读不到（{exc}）——换片腿只能退成'只读 af 不回听'")

    # ---- ⓪ 基线 + 吞吐标定 ----
    say("\n## ⓪ 基线（不挂 af）与吞吐标定：ao=pcm 不是实时的，这一行决定热切腿的切点可不可信")
    b_wav = os.path.join(OUT, "base8.wav")
    base = _render(app, NOISE, b_wav, None, 8.0, hold=8.0)
    b_short = _bands(b_wav) if base.get("ok") else None
    if base.get("ok"):
        got = _read_wav(b_wav)
        wrote = (got[0].size / got[1]) if got else float("nan")
        say(f"   8 秒短媒体：产物={base.get('size')} 字节（≈{wrote:.2f} s 音频） {_tp(base)}")
        say(f"   基线频谱（前 {WIN_SEC:.0f}s）：{_fmt(b_short)}")
        say(f"   内核 log：{base.get('log', '')[:300]}")
    else:
        say("   基线没跑起来")
    if b_short is None:
        say("\n!! 基线 WAV 读不出来，后面的频带对比全都不作数（先修 --ao=pcm 这条通路）")
        return 1

    # ---- ① 形状 ----
    say("\n## ① 同一条 bass+12@110 的六种 af 写法（启动期 --af=；手册 p189/p194 的两种、r13 产品那种）")
    for name, spec, graph in SHAPES:
        say(f"\n  [{name}] --af={spec}")
        say(f"     ffmpeg 侧对照（-af {graph}）：{_ffmpeg_offline(graph, 8)[0]}")
        wav = os.path.join(OUT, "shape.wav")
        run = _render(app, NOISE, wav, spec, 8.0, hold=8.0)
        if not run.get("ok"):
            say("     内核侧：没起来（上面的'原话'就是这一档的证据）")
            continue
        say(f"     内核侧：产物={run.get('size')} 字节 {_tp(run)}")
        say(f"     频谱：{_fmt(_bands(wav), b_short)}")
        say(f"     内核 log：{run.get('log', '')[:280]}")

    # ---- ②③ 滤镜 ----
    say("\n## ②③ 滤镜普查（单滤镜走括号形状 lavfi=[…]；带逗号的图串按 _spec_of 拆成多条 af 条目。"
        "括号是手册 p194 给的、由内核自证的那种写法）")
    say("    期望列只是判读参考，不做断言：真正的判据是'对照 anull 该全 0、volume=0 该塌掉'。")
    for name, graph, expect in FILTERS:
        spec = _spec_of(graph)
        say(f"\n  [{name}] {spec[:170]}")
        ffc, fft = _ffmpeg_offline(graph if graph else None, 20)
        say(f"     ffmpeg 侧：{ffc}  墙钟 {fft:.2f}s")
        wav = os.path.join(OUT, "flt.wav")
        run = _render(app, NOISE, wav, spec, 8.0, hold=8.0)
        if not run.get("ok"):
            say("     内核侧：没起来（'原话'在上面那行）")
            continue
        say(f"     内核侧：产物={run.get('size')} 字节 {_tp(run)}")
        say(f"     频谱：{_fmt(_bands(wav), b_short)}")
        say(f"     期望：{expect}")
        say(f"     内核 log：{run.get('log', '')[:280]}")

    # ---- ③b 同一条 10 段图的两种拼法 ----
    say("\n## ③b 十段 equalizer：'一个括号里塞逗号' vs '逗号切多条 lavfi 条目'（产品要选哪种，看内核原话）")
    for tag, spec in (("单括号塞逗号（手册那种图串）", f"lavfi=[{EQ10}]"),
                      ("多条条目 lavfi=[…],lavfi=[…]", _spec_of(EQ10))):
        wav = os.path.join(OUT, "chain.wav")
        say(f"\n  [{tag}] --af={spec[:150]}…")
        run = _render(app, NOISE, wav, spec, 8.0, hold=8.0)
        if not run.get("ok"):
            say("     内核侧：没起来（'原话'就是这一档的证据）")
            continue
        say(f"     内核侧：产物={run.get('size')} 字节 {_tp(run)}")
        say(f"     频谱：{_fmt(_bands(wav), b_short)}")
        say(f"     内核 log：{run.get('log', '')[:300]}")

    # ---- ④a 热切（主证据腿：按真实节奏放的管线） ----
    say("\n## ④a 播放中热切（带视频轨 + --force-window=yes 把管线拖到≈实时；"
        "对照腿 anull 必须处处 ≈0，否则这两档的差是量具造的）")
    have_av = os.path.exists(AV)
    pace = _pace_cal(app, AV, 60.0) if have_av else None
    if not have_av:
        say(f"   没有 {os.path.basename(AV)}，④a 整段跳过 ⇒ 热切就没有主证据腿")
    elif pace is None:
        say("   节拍标定腿没起来，④a 两档跳过（没有节拍前提就不量切点）")
    else:
        for tag, chain in (("anull对照", "lavfi=[anull]"),
                           ("bass+12真滤镜", "lavfi=[bass=g=12:f=110]"),
                           ("10段链路（产品形状）", _spec_of(EQ10))):
            wav = os.path.join(OUT, f"hot-{tag}.wav")
            say(f"\n  [{tag}] 第 15 s 发 change-list af set {chain[:120]}…")
            run = _render(app, AV, wav, None, 60.0, hold=40.0, mid=(15.0, chain),
                          extra=["--force-window=yes"], pace=True)
            if not run.get("ok"):
                say("     没起来")
                continue
            say(f"     命令应答={run.get('cmd')}  发命令时 time-pos={run.get('before_pos')} "
                f"挂上后 time-pos={run.get('switched_at')}")
            say(f"     读回 af={str(run.get('af'))[:260]}")
            say(f"     {_tp(run)}")
            _hot_report(wav, run)
            say(f"     内核 log：{run.get('log', '')[:300]}")

    # ---- ④b 快抽干：能不能不用窗口就把切点落在产物里 ----
    say("\n## ④b '纯 ao=pcm + 90 s 粉噪'上做同一件事（全速抽干 ≈150×，所以切点要么在产物外、"
        "要么落在开头几十秒；这一档决定 CI 上能不能不开窗口就量热切）")
    for at in (0.30, 1.2):
        for tag, chain in (("anull对照", "lavfi=[anull]"),
                           ("bass+12真滤镜", "lavfi=[bass=g=12:f=110]")):
            wav = os.path.join(OUT, f"hotfast-{at}-{tag}.wav")
            say(f"\n  [{tag}] 第 {at} s 发 change-list af set {chain}")
            run = _render(app, LONG, wav, None, 90.0, hold=12.0, mid=(at, chain))
            if not run.get("ok"):
                say("     没起来")
                continue
            say(f"     命令应答={run.get('cmd')}  发命令时 time-pos={run.get('before_pos')} "
                f"挂上后 time-pos={run.get('switched_at')}")
            say(f"     读回 af={str(run.get('af'))[:200]}")
            say(f"     {_tp(run)}")
            _hot_report(wav, run)
            say(f"     内核 log：{run.get('log', '')[:300]}")

    # ---- ⑤ 换片残留 ----
    say("\n## ⑤ 换片残留：挂上滤镜 → 换另一份媒体 → af 还在不在、后一份也被处理了吗")
    say("    （这一腿必须按真实节奏放：8 s 粉噪在 +0.5 s 就 EOF 了，'换片'会发生在一个已经"
        "结束的播放上，量到的只是空闲态的选项值。所以用带视频轨的 60 s + 30 s 两份片子。）")
    if not have_av:
        say("  没有 av-60s.mp4 ⇒ 这腿跳过（宁可不量，也不给 EOF 之后的读回当'换片残留'）")
    else:
        wav = os.path.join(OUT, "residue.wav")
        if os.path.exists(wav):
            os.remove(wav)
        p = _new(app, ["--ao=pcm", f"--ao-pcm-file={wav}", "--force-window=yes"], [])
        if p is None:
            say("  内核起不来")
        else:
            try:
                p.open_files([AV])
                _pump(app, 9.0)
                m_sz, m_pos = _sz(wav), p.get("time-pos")
                _, err = _req(p, ["change-list", "af", "set", "lavfi=[bass=g=12:f=110]"], timeout=3.0)
                _pump(app, 8.0)
                before, _ = _req(p, ["get_property", "af"])
                s_sz, s_pos = _sz(wav), p.get("time-pos")
                mark = os.path.getsize(p.log_path) if os.path.exists(p.log_path) else 0
                p.open_files([AV2])            # 换片：af 会不会跟着清掉
                _pump(app, 10.0)
                after, _ = _req(p, ["get_property", "af"])
                e_sz, e_pos = _sz(wav), p.get("time-pos")
                say(f"  挂上={err or 'ok'}  挂前 time-pos={m_pos} 换片时 time-pos={s_pos} "
                    f"换片后（新片内）time-pos={e_pos}")
                say(f"  换片前 af={str(before)[:200]}")
                say(f"  换片后 af={str(after)[:200]}")
                say(f"  产物字节刻度：挂上时 {m_sz} / 换片时 {s_sz} / 收工前 {e_sz}")
                try:
                    with open(p.log_path, encoding="utf-8", errors="replace") as fh:
                        fh.seek(mark)
                        say("  换片时内核 log：" + _pick_err_lines(fh.read(), 6)[:420])
                except OSError as exc:
                    say(f"  （log 读不出：{exc}）")
                # 听得到才算数：三段窗口——挂之前 / 挂了之后同一片 / 换了另一片。
                # 位置按**产物字节占比**换算成秒（前提是全程 bytes/sec 不变）；
                # 这个前提当场核：产物秒数该 ≈ 60（第一片放到的位置）+ 第二片那一段。
                got = _read_wav(wav)
                if not got:
                    say("  产物读不出 ⇒ 残留的可听证据没有")
                else:
                    a, sr = got
                    total = a.size / sr
                    say(f"  产物里共 {total:.1f} s 音频（第一片 60 s 内挂滤镜，换片后又接了 "
                        f"~{e_pos if isinstance(e_pos, (int, float)) else 0:.0f} s 第二片）")

                    def sec_of(sz):
                        return sz / max(e_sz, 1) * total

                    for tag, st in (("挂之前", sec_of(m_sz) - 6.0),
                                    ("挂了·同一片", sec_of(m_sz) + 1.0),
                                    ("挂了·换了片", sec_of(s_sz) + 1.0)):
                        say(f"    [{tag}] {st:.1f}~{st + WIN_SEC:.1f} s：{_fmt(_win(a, sr, st))}")
                    say("    判读：'挂了·同一片'和'挂了·换了片'都该相对'挂之前'抬低频；"
                        "第二格抬而第三格不抬＝换片把滤镜甩掉了（面板得在 file-loaded 补发）。")
            except Exception as exc:
                say(f"  这一腿跑崩（不影响后面）：{type(exc).__name__}: {str(exc)[:120]}")
            finally:
                p.quit()
                _pump(app, 0.5)

    # ---- ⑥ 复位形状 ----
    say("\n## ⑥ 复位与改档：面板那颗'一键复位'和每根滑轨要发什么（每条都读回 af 看真相，"
        "同样在按节奏放的管线上发）")
    hot_media, hot_dur, hot_extra = (AV, 60.0, ["--force-window=yes"]) if have_av else (NOISE, 8.0, [])
    if not have_av:
        say("  没有带视频轨的媒体 ⇒ 退回 8 s 粉噪（注意：那时命令都发在 EOF 之后，只证'形状收不收'）")
    wav = os.path.join(OUT, "reset.wav")
    p = _new(app, ["--ao=pcm", f"--ao-pcm-file={wav}", *hot_extra], [])
    if p is not None:
        try:
            p.open_files([hot_media])
            _pump(app, 6.0)
            _, err = _req(p, ["change-list", "af", "set", "lavfi=[bass=g=12:f=110]"], timeout=3.0)
            if err:
                say(f"  挂不上：{err}（复位腿作废）")
            else:
                _pump(app, 0.6)
                got, _ = _req(p, ["get_property", "af"])
                say(f"  挂上后 af={str(got)[:200]}")
                for cmd in (["change-list", "af", "set", _spec_of(EQ10)],
                            ["set_property", "af", _spec_of(EQ10)],
                            ["change-list", "af", "set", f"lavfi=[{EQ10}]"],
                            ["change-list", "af", "set", "lavfi=[bass=g=12:f=110]"],
                            ["change-list", "af", "set", "lavfi=[treble=g=-6:f=3000]"],
                            ["change-list", "af", "remove", "lavfi=[treble=g=-6:f=3000]"],
                            ["change-list", "af", "remove", "lavfi=[bass=g=12:f=110]"],
                            ["change-list", "af", "remove", "bass"],
                            ["change-list", "af", "set", ""],
                            ["set_property", "af", "[]"],
                            ["set_property", "af", ""],
                            ["change-list", "af", "append", "lavfi=[bass=g=12:f=110]"],
                            ["change-list", "af", "append", _spec_of(EQ10)],
                            ["change-list", "af", "toggle", "lavfi=[bass=g=12:f=110]"],
                            ["set_property", "af", "lavfi=[bass=g=12:f=110]"],
                            ["change-list", "af", "set", "lavfi=[volume=0]"]):
                    txt = " ".join(str(x) for x in cmd)
                    _, e = _req(p, cmd, timeout=3.0)
                    got, _ = _req(p, ["get_property", "af"])
                    say(f"  {txt[:58]:60s} → {(e or 'ok'):10s} 读回={str(got)[:150]}")
                _pump(app, 1.0)
                got, _ = _req(p, ["get_property", "af"])
                say(f"  收尾 af={str(got)[:200]}")
                say("  内核 log（这一腿的后半）：" + _err_text(p)[:400])
            _pump(app, 1.0)
        except Exception as exc:
            say(f"  这一腿跑崩（不影响后面）：{type(exc).__name__}: {str(exc)[:120]}")
        finally:
            p.quit()
            _pump(app, 0.5)

    say("\n## 产物一览")
    for f in sorted(os.listdir(OUT)):
        try:
            say(f"    {f}:{os.path.getsize(os.path.join(OUT, f))}")
        except OSError as exc:
            say(f"    {f}:（读不到 {exc}）")
    say("\n## 判读口径（抄进事实表时带上）")
    say(f"    逐带 dB 是同一把尺（Hann 窗 rFFT 带内平均幅度）的绝对值，括号里才是相对基线的差；"
        f"只量前 {WIN_SEC:.0f} s，且所有档都用同一份粉噪、同一台 ao=pcm 内核。")
    say("    粉噪本身每倍频程递减，所以 bass+12 只该在 20-350Hz 抬起来，中高频的差要接近 0；"
        "对照 anull 必须处处 ≈0、volume=0 必须整条塌——这两条不成立时本 log 里所有频谱列都不作数。")
    say("    吞吐 = 产物里的音频秒数 ÷ 播放段墙钟秒，是 **ao=pcm 全速渲染**下的相对代价"
        "（不是实时播放的 CPU 余量），只能用来排'哪条链更贵'，不当承诺。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
