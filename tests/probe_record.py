# -*- coding: utf-8 -*-
"""内核实测探针 F1（第二版）：录制这一族在本机 mpv 上到底认什么、录出来是什么东西。

第一版（`tests/out/record-probe-r13.log`）整趟没写出一个文件，回头看是探针自己的顺序错：
② 全程问的是 `record-file` 这个**本机内核不认**的名字；③ 把"开录"和"停录"塞在同一串命令里、
中间没泵事件，于是内核根本没机会读数据；`_playable()` 又去 load 了一个不存在的路径，
那行"duration=10.0 时间 7.50→8.67（在走）"说的是还在播的样片、不是录出来的文件。
所以那一版对 F1 一个结论都没给——本版本把顺序摆正重跑。

为什么先探再写：本仓库的规矩是"界面不动、内核先说话"（同 `tests/probe_transform.py`）。
录制这一族的坏法全是**静默**的：不认识的属性名照样回 error，写不进去的路径只在 log 里留一行，
封装不匹配能开出一个 0 字节的壳。所以清单要回答的不是"有没有这个属性"，而是：

  ① 本机内核认哪个名字（`stream-record` 是本名；`record-file` / `recording` /
     `recording-progress` 在第一版里回 "property not found"，这里同时拿**出厂手册**
     `runtime/mpv/doc/manual.pdf` 对一边，两个独立来源都说没有才算数）；
  ② 开录／停录各要发什么才真生效，停的时候 `""` 与 `"no"` 各是什么下场
     （`"no"` 要是被当成字面文件名，就会在 cwd 开出一个叫 no 的文件——那是产品陷阱）；
  ③ 录出来的东西能不能用：多大了、时长对不对、ffmpeg 整片解一遍报不报错、
     能不能被我们自己的内核放起来；MP4 的索引表在文件尾，强杀留下的半成品放不开——
     这条决定默认封装与退出前要不要收尾；
  ④ 它到底在录什么：手册原话是"把解封装器读到的数据 remux 写出去"，
     不是转码、不是抓屏，而且"通常只有输出格式与输入格式相同才工作"。
     这条决定这颗按钮在**本地文件**上是不是个真功能（还是只能对网络流开）。
  ⑤ 边角：录着 seek、录着换片、放到头还在录、路径不合法、启动前就设、dump-cache 另存一段。

**本探针不做断言**：产出是给人抄进事实表的清单，断言在实现之后的验收里落。

跑法（内核 hwnd=0 无窗口跑，不需要屏幕；不要设 QT_QPA_PLATFORM=offscreen）：
    PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_record.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtWidgets import QApplication                      # noqa: E402

from app import config as cfgmod                              # noqa: E402
from app.engine.player import MpvPlayer                       # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
SAMPLE2 = os.path.join(HERE, "samples", "gelu-sample-2.mp4")
OUT_DIR = os.path.join(HERE, "out", "record-probe")
MPV_DIR = os.path.dirname(cfgmod.MPV_EXE)          # 内核的 cwd：字面 "no" 会落在这里
FFMPEG = cfgmod.FFMPEG_EXE

# 想问的属性名。"问不到"本身就是结论（产品不能显示进度、不能靠内核实值画红点）。
PROPS = ["stream-record", "record-file", "recording", "recording-progress",
         "recording-tag-segment", "recording-initial-mode"]


def _pump(app, secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def _ask(p, name):
    """直接走 IPC 问属性：`player.get()` 把 IpcError 吞成 default，而这里要的**正是**
    "这项不存在"与"值为空"的区别（产品要据此决定画不画那一行），所以绕开它自己发。"""
    if p._ipc is None:
        return None, "IPC 已关（内核没了）"
    try:
        return p._ipc.request(["get_property", name], timeout=1.5), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _set(p, value):
    """发 set_property stream-record <value>，把应答与读回一起给出去。"""
    if p._ipc is None:
        return "IPC 已关", "—"
    try:
        p._ipc.request(["set_property", "stream-record", value], timeout=3.0)
        ack = "ok"
    except Exception as exc:
        ack = f"✗{type(exc).__name__}: {str(exc)[:90]}"
    return ack, repr(_ask(p, "stream-record")[0])


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _curve(p, app, path: str, secs: float, step: float = 1.0) -> list[tuple]:
    """边播边采样文件大小：这是"真在写"的唯一硬证据（属性回报会骗人，字节数不会）。"""
    pts = []
    n = max(1, int(round(secs / step)))
    for i in range(n):
        _pump(app, step)
        pts.append((round((i + 1) * step, 1), _size(path), round(float(_ask(p, "time-pos")[0] or 0), 2)))
    return pts


def _fmt(pts: list[tuple]) -> str:
    return " → ".join(f"{t:g}s:{sz}B@{tp:g}s" for t, sz, tp in pts)


def _ff_valid(path: str) -> str:
    """ffmpeg 整片解一遍：能解干净= remux 出来的东西真是能放的。"""
    if not os.path.exists(path):
        return "（文件不存在）"
    if not os.path.exists(FFMPEG):
        return "（没有 ffmpeg.exe，这趟没验封装有效性）"
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                           capture_output=True, timeout=120, text=True,
                           encoding="utf-8", errors="replace")
        err = (r.stderr or "").strip().replace("\n", " | ")
        return f"rc={r.returncode}" + (f" 报错={err[:170]}" if err else " 无报错")
    except Exception as exc:
        return f"ffmpeg 跑崩：{type(exc).__name__}: {exc}"


def _ff_info(path: str) -> str:
    """产物是什么：时长、几条流、什么编码。ffmpeg -i 对输入文件 rc≠0，只看 stderr。"""
    if not os.path.exists(path):
        return "（文件不存在）"
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                           capture_output=True, timeout=60, text=True,
                           encoding="utf-8", errors="replace")
        err = (r.stderr or "")
    except Exception as exc:
        return f"ffmpeg 跑崩：{type(exc).__name__}: {exc}"
    dur = re.search(r"Duration: ([\d:.]+)", err)
    bitrate = re.search(r"bitrate:\s*(\d+)\s*kb/s", err)
    streams = re.findall(r"Stream #\d+:\d+[^\n]*", err)
    brief = " / ".join(" ".join(s.split(":")[:3]).strip() +
                       ((":" + s.split("(", 1)[1].split(",", 1)[0]) if "(" in s else "")
                       for s in streams)
    return (f"存在={os.path.exists(path)} 大小={_size(path)} "
            f"Duration={dur.group(1) if dur else '?'} "
            f"码率={bitrate.group(1) if bitrate else '?'}kb/s 流={brief[:200] or '?'}")


def _playable(p, path: str, app) -> str:
    """用我们自己的内核把录出来的文件放一遍：duration>0 且时间真的在走才算能用。
    注意 load 的必须是**这个路径**，并且读完之后再回样片——不然读到的还是样片的时间。"""
    if not os.path.exists(path):
        return "（文件不存在，回放没跑）"
    p.open_files([path])
    _pump(app, 1.5)
    dur = _ask(p, "duration")[0]
    t0 = float(_ask(p, "time-pos")[0] or 0)
    _pump(app, 1.2)
    t1 = float(_ask(p, "time-pos")[0] or 0)
    cur = _ask(p, "path")[0]
    return (f"内核放的 path={os.path.basename(str(cur))} duration={dur} "
            f"时间 {t0:.2f}→{t1:.2f}" + ("（在走）" if t1 > t0 else "（**没走**）"))


def _back_to_sample(p, app) -> None:
    p.open_files([SAMPLE])
    _pump(app, 1.5)


def _stray_files() -> list[str]:
    """内核 cwd 与产物目录里"不该存在的录制残骸"：字面 no、空名、以及全部产物一览。"""
    out = []
    for d in (MPV_DIR, OUT_DIR):
        try:
            for f in os.listdir(d):
                if f == "no" or f.endswith(".mkv") or f.endswith(".mp4") or f == "rec":
                    out.append(f"{os.path.basename(d)}/{f}:{_size(os.path.join(d, f))}")
        except OSError as exc:
            out.append(f"{d} 读不出：{exc}")
    return out


def main() -> int:
    for need in (SAMPLE, SAMPLE2):
        if not os.path.isfile(need):
            print("没有样片，先跑 python tests/gen_sample.py")
            return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    logs: list[str] = []
    p2_logs: list[str] = []            # 提前绑：中途抛异常时 finally 里要读它，不许 NameError
    p2: MpvPlayer | None = None
    src_info = _ff_info(SAMPLE)
    p = MpvPlayer(cfgmod.Config())
    p.sig_log.connect(logs.append)
    if not p.start(hwnd=0):
        print("内核起不来，最后十条 log：")
        for line in logs[-10:]:
            print("   ", line)
        return 3
    print(f"# 内核 {p.version} ｜ 样片 {os.path.basename(SAMPLE)}：{src_info}")
    try:
        # ---- ① 名字普查：播放中问一遍 ----
        print("\n## ① 属性名普查（播放中）")
        p.open_files([SAMPLE])
        _pump(app, 2.0)
        for n in PROPS:
            v, e = _ask(p, n)
            print(f"  {n:24s} = {v!r}" + (f"  ✗{e}" if e else ""))

        # ---- ②③④ 三种封装：开录 → 曲线 → 停 → 体检 → 回放 ----
        cases = [("mkv·停用法用空串", os.path.join(OUT_DIR, "c-mkv.mkv"), ""),
                 ("mp4·停用法用 no", os.path.join(OUT_DIR, "c-mp4.mp4"), "no"),
                 ("ts·停用法用空串", os.path.join(OUT_DIR, "c-ts.ts"), "")]
        for tag, path, stop_shape in cases:
            for junk in (path, os.path.join(MPV_DIR, "no")):
                if os.path.exists(junk):
                    os.remove(junk)
            _back_to_sample(p, app)
            tp0 = float(_ask(p, "time-pos")[0] or 0)
            print(f"\n## ②/③/④ [{tag}] 在 time-pos={tp0:.2f} 开录 → {os.path.basename(path)}")
            ack, echo = _set(p, path)
            print(f"        set_property stream-record <路径> → {ack}  读回={echo}")
            pts = _curve(p, app, path, 4.0)
            print(f"        大小曲线（含当时的 time-pos）：{_fmt(pts)}")
            ack2, echo2 = _set(p, stop_shape)
            _pump(app, 0.8)
            s_after = _size(path)
            print(f"        停录 set_property stream-record {stop_shape!r} → {ack2} 读回={echo2}"
                  f" 停后大小={s_after}")
            print(f"        产物：{_ff_info(path)}")
            print(f"        ffmpeg 解一遍：{_ff_valid(path)}")
            print(f"        我们内核回放：{_playable(p, path, app)}")
            if os.path.exists(os.path.join(MPV_DIR, "no")):
                print(f"        ⚠ 内核 cwd 里冒出字面文件 no：{_size(os.path.join(MPV_DIR, 'no'))} 字节")

        # ---- ⑤ 录着 seek（手册：seek 出缓存外会在产物里留"跳"，甚至直接停录）----
        print("\n## ⑤ 录着 seek")
        seek_path = os.path.join(OUT_DIR, "c-seek.mkv")
        _back_to_sample(p, app)
        _set(p, seek_path)
        pre = _curve(p, app, seek_path, 2.0)
        try:
            p._ipc.request(["seek", 3, "absolute"], timeout=2.0)
        except Exception as exc:
            print(f"        seek 命令本身：✗{type(exc).__name__}: {str(exc)[:80]}")
        _pump(app, 0.4)
        post_seek_val = repr(_ask(p, "stream-record")[0])
        post = _curve(p, app, seek_path, 2.0)
        print(f"        seek 前 {_fmt(pre)} ｜ seek 后 stream-record 读回={post_seek_val} ｜ {_fmt(post)}")
        _set(p, "")
        _pump(app, 0.5)
        print(f"        产物：{_ff_info(seek_path)}")

        # ---- ⑥ 录着换片 ----
        print("\n## ⑥ 录着换片（loadfile 之后 stream-record 还挂着吗）")
        sw = os.path.join(OUT_DIR, "c-switch.mkv")
        _back_to_sample(p, app)
        _set(p, sw)
        _curve(p, app, sw, 1.5)
        p.open_files([SAMPLE2])
        after_load = repr(_ask(p, "stream-record")[0])
        pts = _curve(p, app, sw, 2.0)
        print(f"        换片后 stream-record 读回={after_load} 曲线={_fmt(pts)}")
        _set(p, "")
        _pump(app, 0.5)
        print(f"        产物：{_ff_info(sw)}")
        _back_to_sample(p, app)

        # ---- ⑦ 放到头还在录（keep-open=yes）----
        print("\n## ⑦ 播到片尾还在录")
        endp = os.path.join(OUT_DIR, "c-end.mkv")
        _back_to_sample(p, app)
        dur = float(_ask(p, "duration")[0] or 0)
        try:
            p._ipc.request(["seek", max(0.0, dur - 4.0), "absolute"], timeout=2.0)
        except Exception as exc:
            print(f"        seek 到片尾前 4 秒失败：{type(exc).__name__}: {str(exc)[:70]}")
        _pump(app, 0.6)
        _set(p, endp)
        pts = _curve(p, app, endp, 6.0, 1.5)
        print(f"        duration={dur} 曲线={_fmt(pts)} 片尾后 stream-record 读回="
              f"{repr(_ask(p, 'stream-record')[0])} eof-reason={_ask(p, 'eof-reason')[0]!r}")
        _set(p, "")
        _pump(app, 0.5)
        print(f"        产物：{_ff_info(endp)}  ffmpeg：{_ff_valid(endp)}")

        # ---- ⑧ 路径不合法 / 怪扩展名 ----
        print("\n## ⑧ 路径不合法与怪扩展名")
        bad = os.path.join(OUT_DIR, "no-such-dir", "x.mkv")
        ack, echo = _set(p, bad)
        _pump(app, 1.5)
        print(f"        录到不存在的目录：set→{ack} 读回={echo} "
              f"recording={_ask(p, 'recording')[0]!r}（这行的'不存在'是结论）")
        _set(p, "")
        badext = os.path.join(OUT_DIR, "c-weird.txt")  # 手册：输出格式由扩展名决定——怪扩展名会怎样
        ack, echo = _set(p, badext)
        pts = _curve(p, app, badext, 2.0)
        _set(p, "")
        _pump(app, 0.4)
        print(f"        写 c-weird.txt：set→{ack} 读回={echo} 曲线={_fmt(pts)} 产物={_ff_info(badext)}")

        # ---- ⑨ 事件：属性变化会不会推给我们（决定 UI 要不要自己记账）----
        print("\n## ⑨ observe_property stream-record 推不推")
        seen: list = []
        p._ipc.observe("stream-record", lambda v: seen.append(v))
        ev = os.path.join(OUT_DIR, "c-event.mkv")
        _set(p, ev)
        _pump(app, 1.0)
        _set(p, "")
        _pump(app, 0.6)
        print(f"        开录→停录期间收到的事件值：{seen}")

        # ---- ⑩ dump-cache 另存一段（录制失败时的备选路）----
        print("\n## ⑩ dump-cache <start> <end> <file>")
        _back_to_sample(p, app)
        dfile = os.path.join(OUT_DIR, "c-dump.mkv")
        t0 = float(_ask(p, "time-pos")[0] or 0)
        durs = float(_ask(p, "duration")[0] or 0)
        cmd = ["dump-cache", f"{max(0.0, t0 - 2.0):.2f}", f"{min(durs, t0 + 3.0):.2f}", dfile]
        try:
            r = p._ipc.request(cmd, timeout=3.0)
            print(f"        同步等 3 秒：应答={str(r)[:80]!r}（这条命令要到 dump 结束才回，"
                  f"超时≠失败）")
        except Exception as exc:
            print(f"        同步等：✗{type(exc).__name__}: {str(exc)[:90]}（预期：异步命令会卡应答）")
        _pump(app, 2.0)
        print(f"        盘上：{_ff_info(dfile)}  ffmpeg：{_ff_valid(dfile)}")
        try:
            p._ipc.request(["dump-cache", "0", "0", ""], timeout=2.0)
        except Exception as exc:
            print(f"        停 dump（filename 给空串=停）：✗{type(exc).__name__}: {str(exc)[:70]}")
        _pump(app, 0.5)
    finally:
        # ---- ⑪ 强停：录着直接 quit，不发停录命令（MP4 尾索引会不会留下放不开的壳）----
        print("\n## ⑪ 录着直接 quit（不发停录）")
        hard_mp4 = os.path.join(OUT_DIR, "c-hard.mp4")
        hard_mkv = os.path.join(OUT_DIR, "c-hard.mkv")
        for f in (hard_mp4, hard_mkv):
            if os.path.exists(f):
                os.remove(f)
        if p._ipc is not None:
            try:
                _back_to_sample(p, app)
                _set(p, hard_mkv)
                _curve(p, app, hard_mkv, 1.5)
                _set(p, "")
                _pump(app, 0.4)
            except Exception as exc:
                print(f"  （mkv 收尾腿没跑成：{type(exc).__name__}: {str(exc)[:90]}）")
        if not p._started:
            print("  （前段把内核跑崩了，这一腿没跑）")
        else:
            try:
                _back_to_sample(p, app)
                _set(p, hard_mp4)
                _curve(p, app, hard_mp4, 2.5)
                print(f"  quit 前：mkv={_size(hard_mkv)} mp4={_size(hard_mp4)}")
                p.quit()
            except Exception as exc:
                print(f"  （mp4 强停腿没跑成：{type(exc).__name__}: {str(exc)[:90]}）")
            _pump(app, 0.6)
            print(f"  quit 后 mp4：{_ff_info(hard_mp4)}")
            print(f"           ffmpeg：{_ff_valid(hard_mp4)}")
        # ---- ⑫ 新开一个实例：load 之前就把 stream-record 设上（= --stream-record=）----
        print("\n## ⑫ load 之前就设上（等价启动参数 --stream-record=）")
        pre = os.path.join(OUT_DIR, "c-pre.mkv")
        if os.path.exists(pre):
            os.remove(pre)
        p2 = MpvPlayer(cfgmod.Config())
        p2.sig_log.connect(p2_logs.append)
        orig = p2.build_args
        p2.build_args = lambda hwnd: [*orig(hwnd), f"--stream-record={pre}"]
        if not p2.start(hwnd=0):
            print("  第二台内核起不来：" + " | ".join(p2_logs[-3:])[:200])
        else:
            p2.open_files([SAMPLE])
            _pump(app, 4.0)
            pts = _curve(p2, app, pre, 2.0)
            print(f"  启动参数档 曲线={_fmt(pts)} 读回={repr(_ask(p2, 'stream-record')[0])}")
            _set(p2, "")
            _pump(app, 0.6)
            print(f"  产物：{_ff_info(pre)}  ffmpeg：{_ff_valid(pre)}")
            print(f"  回放：{_playable(p2, pre, app)}")
            p2.quit()
        print(f"\n## 收尾：录制残骸一览（cwd 里有没有叫 no 的文件也在这里）")
        for line in _stray_files():
            print("   ", line)
        print("\n## 内核这段时间说过的话（record 相关只会出现在这里）")
        rel = [l for l in logs + p2_logs
               if "record" in l.lower() or "option not found" in l or "no such" in l.lower()]
        for line in (rel[-25:] or ["（一条 record 相关的 log 都没有）"]):
            print("   ", line[:200])
        print("\n## 手册核对（出厂 manual.pdf 里 record 族的原文位置）")
        print("    --stream-record=<file> 在 p162；p45 是倒放章节里那句"
              "『Stream recording is broken』；手册里没有 record-file / recording /"
              " recording-progress 这几个名字（与上面 IPC 的 'property not found' 两路对齐）")
        if p._started:
            p.quit()
        if p2 is not None and p2._started:
            p2.quit()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
