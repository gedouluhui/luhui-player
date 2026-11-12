# -*- coding: utf-8 -*-
"""内核实测探针 F1 第二趟（机制确认）：stream-record 为什么"设了没文件"，以及哪条路真能出文件。

第一趟（`tests/probe_record.py` → `tests/out/record-probe-r13b.log`）量到的形状：
  · 播放中 `set_property stream-record <路径>` 回 ok、`get_property` 把路径原样回报，
    但**盘上一个字节都没有**（②③④⑤⑦⑪ 六条腿全是 -1）；
  · 只有"文件是在选项已经设上的情况下打开的"那两条腿出了文件：
    ⑥ 设完之后换片 → 570632 字节、时长 6.04、两条流都在；
    ⑫ 启动参数 `--stream-record=` → 626598 字节、我们内核能放（duration=10.042，时间在走）；
  · ⑩ `dump-cache 2.00 4.50 c-dump.mkv` 出了 425361 字节、ffmpeg rc=0 无报错。
手册 p162 那句是关键线索："this will write **only** data that is appended at the end of the
cache, and the already cached data cannot be written"。本机出厂参数是
`--cache=yes --demuxer-max-bytes=200MiB`（见 `tests/.selftest-data/logs/mpv.log` 的命令行），
样片才 632KB——开片瞬间就读完了，之后没有任何"新读到的数据"，所以运行中开录=录了个空。
这一趟就是要把这个解释**量出来**，而不是停在"讲得通"：

  A. 默认参数下运行中开录：内核 log 里 recorder/cplayer 那两级说话没有、说了什么；
     同一台内核把选项留着再 loadfile 一次，文件该出（对照组：证明"开片时才挂录制器"）；
  B. 把缓存压小（`--demuxer-max-bytes=64KiB`）让内核边播边读：运行中开录能不能出文件、
     出得来是不是持续涨、录着 seek 会不会断——这条把 A 的"没文件"归到"没新数据"而不是"没这个功能"；
  C. 启动参数档放完整个片子：产物与源片对比（大小/时长/ffmpeg 校验），回答"转存一份"能不能做；
  D. `dump-cache` 的区间档与连续档：能不能精确导出 [start,end]、连续档怎么停、产物可不可用；
  E. 对照组（不走内核）：`ffmpeg -ss/-to -c copy` 直接切一段——如果这条又准又稳，
     那"录制"这一族在**本地播放器**语境里该走哪扇门就有答案了。

**本探针不做断言**：产出是抄进事实表的数字。跑法同第一趟（不需要屏幕）：
    PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_record2.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)          # 要 import 同目录的第一趟探针复用它的读数函数
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtWidgets import QApplication                      # noqa: E402

from app import config as cfgmod                              # noqa: E402
from app.engine.player import MpvPlayer                       # noqa: E402

# 复用第一趟的读数函数：两趟数字要能对照，量法不能换一套。
from probe_record import (SAMPLE, SAMPLE2, OUT_DIR, _pump, _ask, _set, _size,   # noqa: E402
                          _curve, _fmt, _ff_valid, _ff_info, _playable, _back_to_sample)

# 出厂参数把 msg-level 钉在 all=warn，recorder/demux 的话全被吃掉。这几条腿要听内核说，
# 所以每台探针内核自己追加一级明细——注意**追加**在原参数之后（后写的同名参数生效）。
DETAIL = ["--msg-level=recorder=v,cplayer=v,demux=v,demux_cache=v"]


def _new(extra: list[str], logs: list[str]) -> MpvPlayer | None:
    p = MpvPlayer(cfgmod.Config())
    p.sig_log.connect(logs.append)
    orig = p.build_args
    p.build_args = lambda hwnd: [*orig(hwnd), *DETAIL, *extra]
    if not p.start(hwnd=0):
        print(f"  内核起不来：" + " | ".join(logs[-3:])[:200])
        return None
    print(f"  内核 {p.version} 追加参数={extra}")
    return p


def _log_since(p: MpvPlayer, mark: int, keys=("record", "recorder", "dump", "Set property")) -> str:
    """从内核 log 文件里取新写的那几行。sig_log 只转发了 warn 以上，这里要读原文件。"""
    try:
        with open(p.log_path, encoding="utf-8", errors="replace") as fh:
            fh.seek(mark)
            new = fh.read()
    except OSError as exc:
        return f"（log 读不出：{exc}）"
    lines = [ln.strip() for ln in new.splitlines() if any(k in ln.lower() for k in keys)]
    return " ⏎ ".join(lines[-8:])[:900] if lines else "（log 里没有相关行）"


def _mark(p: MpvPlayer) -> int:
    try:
        return os.path.getsize(p.log_path)
    except OSError:
        return 0


def main() -> int:
    if not os.path.isfile(SAMPLE):
        print("没有样片，先跑 python tests/gen_sample.py")
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    print(f"# 样片：{_ff_info(SAMPLE)}")

    # ================= A. 默认参数：运行中开录 vs 留着选项再开片 =================
    print("\n## A. 默认缓存档（200MiB）：运行中开录到底发生了什么")
    logs: list[str] = []
    p = _new([], logs)
    if p is None:
        return 3
    try:
        p.open_files([SAMPLE])
        _pump(app, 2.0)
        a1 = os.path.join(OUT_DIR, "a-mid.mkv")
        if os.path.exists(a1):
            os.remove(a1)
        mark = _mark(p)
        ack, echo = _set(p, a1)
        pts = _curve(p, app, a1, 4.0)
        print(f"  A1 运行中 set → {ack} 读回={echo}")
        print(f"     曲线={_fmt(pts)}  盘上存在={os.path.exists(a1)}")
        print(f"     内核 log：{_log_since(p, mark)}")
        # A2 是 A1 的对照组：选项不动，只再开一次片。同一台内核、同一个路径形状，
        # 唯一的差别是"文件是在选项已设的情况下打开的"——这一下就把"没这个功能"排除了。
        mark = _mark(p)
        p.open_files([SAMPLE])
        pts2 = _curve(p, app, a1, 4.0)
        print(f"  A2 选项留着不动、再 loadfile → 曲线={_fmt(pts2)} 存在={os.path.exists(a1)}")
        print(f"     内核 log：{_log_since(p, mark)}")
        print(f"     停录前产物：{_ff_info(a1)}  ffmpeg：{_ff_valid(a1)}")
        # 先停录再回放：stream-record 指哪儿就**覆盖**哪儿（手册原话 "always be overwritten
        # without asking"），还挂着它去 loadfile 那个文件＝拿录制器覆写自己要量的产物。
        _set(p, "")
        _pump(app, 0.8)
        print(f"     停录后产物：{_ff_info(a1)}  ffmpeg：{_ff_valid(a1)}")
        print(f"     回放：{_playable(p, a1, app)}")
    finally:
        p.quit()

    # ================= B. 小缓存：让内核边播边读，运行中开录能不能出东西 =================
    print("\n## B. --demuxer-max-bytes=64KiB（读得慢，缓存装不下整片）：运行中开录")
    logs = []
    p = _new(["--demuxer-max-bytes=65536", "--demuxer-max-back-bytes=0"], logs)
    if p is None:
        return 3
    try:
        p.open_files([SAMPLE])
        _pump(app, 2.0)
        _cs, _ce = _ask(p, "demuxer-cache-state")
        _fw = _cs.get("fw-pct") if isinstance(_cs, dict) else "?"
        print(f"  开局缓存读数：demuxer-cache-state {'在，前端缓冲 ' + str(_fw) + '%' if not _ce else '✗' + _ce}"
              f" ｜ demuxer-cache-duration={_ask(p, 'demuxer-cache-duration')[0]!r}")
        b1 = os.path.join(OUT_DIR, "b-small-cache.mkv")
        if os.path.exists(b1):
            os.remove(b1)
        mark = _mark(p)
        ack, echo = _set(p, b1)
        pts = _curve(p, app, b1, 5.0)
        print(f"  B1 运行中 set → {ack} 读回={echo}")
        print(f"     曲线={_fmt(pts)} 存在={os.path.exists(b1)}")
        print(f"     内核 log：{_log_since(p, mark)}")
        if os.path.exists(b1):
            mark = _mark(p)
            try:
                p._ipc.request(["seek", 3, "absolute"], timeout=2.0)
            except Exception as exc:
                print(f"     seek 命令：✗{type(exc).__name__}: {str(exc)[:70]}")
            pts = _curve(p, app, b1, 4.0)
            print(f"  B2 录着 seek 到 3s：曲线={_fmt(pts)} 读回={repr(_ask(p, 'stream-record')[0])}")
            print(f"     内核 log：{_log_since(p, mark)}")
        _set(p, "")
        _pump(app, 0.8)
        print(f"     产物：{_ff_info(b1)}  ffmpeg：{_ff_valid(b1)}")
        print(f"     回放：{_playable(p, b1, app)}")
    finally:
        p.quit()

    # ================= C. 启动参数档放完整个片：转存一份的质量 =================
    print("\n## C. 开片前就设 --stream-record，并把整片放完（'转存'这条路的产物质量）")
    logs = []
    c1 = os.path.join(OUT_DIR, "c-full.mkv")
    if os.path.exists(c1):
        os.remove(c1)
    p = _new([f"--stream-record={c1}"], logs)
    if p is None:
        return 3
    try:
        p.open_files([SAMPLE])
        pts = _curve(p, app, c1, 12.0, 3.0)      # 片长 10s，放到头再留 2 秒
        print(f"  曲线={_fmt(pts)}")
        print(f"  停录前：{_ff_info(c1)}")
        mark = _mark(p)
        _set(p, "")
        _pump(app, 0.8)
        print(f"  停录后：{_ff_info(c1)}")
        print(f"  ffmpeg：{_ff_valid(c1)}")
        print(f"  与源片对比：源 {_size(SAMPLE)} 字节 / 录 {_size(c1)} 字节")
        print(f"  内核 log：{_log_since(p, mark)}")
        print(f"  回放：{_playable(p, c1, app)}")
    finally:
        p.quit()

    # ================= D. dump-cache：区间档与连续档 =================
    print("\n## D. dump-cache（缓存里那段直接写出去）")
    logs = []
    p = _new([], logs)
    if p is None:
        return 3
    try:
        _back_to_sample(p, app)
        try:
            p._ipc.request(["seek", 5.0, "absolute"], timeout=2.0)
        except Exception as exc:
            print(f"  seek 失败：{type(exc).__name__}: {str(exc)[:70]}")
        _pump(app, 1.0)
        d1 = os.path.join(OUT_DIR, "d-window.mkv")
        if os.path.exists(d1):
            os.remove(d1)
        mark = _mark(p)
        t = time.monotonic()
        try:
            r = p._ipc.request(["dump-cache", "2.00", "7.00", d1], timeout=8.0)
            done = f"应答={str(r)[:40]!r} 等了 {time.monotonic() - t:.2f}s"
        except Exception as exc:
            done = f"✗{type(exc).__name__}: {str(exc)[:70]} 等了 {time.monotonic() - t:.2f}s"
        _pump(app, 1.0)
        print(f"  D1 区间档 dump-cache 2→7：{done}")
        print(f"     产物：{_ff_info(d1)}  ffmpeg：{_ff_valid(d1)}")
        print(f"     内核 log：{_log_since(p, mark)}")
        print(f"     回放：{_playable(p, d1, app)}")
        # D2 连续档：命令不会自己回（要到停才回），所以只能 fire 不等应答
        d2 = os.path.join(OUT_DIR, "d-cont.mkv")
        if os.path.exists(d2):
            os.remove(d2)
        _back_to_sample(p, app)
        mark = _mark(p)
        p._ipc.fire(["dump-cache", "0", "no", d2])
        pts = _curve(p, app, d2, 3.0)
        print(f"  D2 连续档 fire（不等应答）：曲线={_fmt(pts)}")
        try:
            p._ipc.request(["dump-cache", "0", "0", ""], timeout=3.0)
            stop_ack = "空 filename 停：ok"
        except Exception as exc:
            stop_ack = f"空 filename 停：✗{type(exc).__name__}: {str(exc)[:60]}"
        _pump(app, 1.0)
        pts2 = _curve(p, app, d2, 1.5)
        print(f"     {stop_ack} 停后曲线={_fmt(pts2)} 产物={_ff_info(d2)}")
        print(f"     内核 log：{_log_since(p, mark)}")
    finally:
        p.quit()

    # ================= E. 对照组：ffmpeg 无损切一段（不走内核） =================
    print("\n## E. 对照组：ffmpeg -c copy 直接从盘上的片切一段（三种参数形状各量一遍）")
    if not os.path.exists(cfgmod.FFMPEG_EXE):
        print("  没有 ffmpeg.exe，这腿没跑")
        variants = []
    else:
        variants = [
            ("e1.mkv", ["-ss", "2", "-to", "6", "-i", SAMPLE, "-c", "copy"]),
            ("e2.mp4", ["-ss", "2", "-i", SAMPLE, "-t", "4", "-c", "copy"]),
            ("e3.mp4", ["-ss", "2", "-i", SAMPLE, "-t", "4", "-c", "copy",
                        "-movflags", "+faststart"]),
        ]
    e_outputs = []
    for name, args in variants:
        out = os.path.join(OUT_DIR, name)
        if os.path.exists(out):
            os.remove(out)
        try:
            r = subprocess.run([cfgmod.FFMPEG_EXE, "-y", "-v", "error", *args, out],
                               capture_output=True, timeout=120, text=True,
                               encoding="utf-8", errors="replace")
            err = (r.stderr or "").strip().replace("\n", " | ")[:180]
            e_outputs.append(out)
            print(f"  {name}（{' '.join(args[2:])[:56]}）: rc={r.returncode}"
                  + (f" 报错={err}" if err else " 无报错") + f" ｜ {_ff_info(out)}")
            print(f"        ffmpeg 解一遍：{_ff_valid(out)}")
        except Exception as exc:
            print(f"  {name}: 跑崩 {type(exc).__name__}: {str(exc)[:120]}")
    # 音频-only 的"录一段"也顺手量一下：竞品那种"只要声音"的导出，ffmpeg 一路就能做
    aout = os.path.join(OUT_DIR, "e-audio.m4a")
    if os.path.exists(aout):
        os.remove(aout)
    try:
        r = subprocess.run([cfgmod.FFMPEG_EXE, "-y", "-v", "error", "-ss", "2", "-to", "6",
                            "-i", SAMPLE, "-vn", "-c", "copy", aout],
                           capture_output=True, timeout=120, text=True,
                           encoding="utf-8", errors="replace")
        print(f"  E-音频档(-vn -c copy): rc={r.returncode} "
              f"{(r.stderr or '').strip()[:120]} ｜ {_ff_info(aout)}")
    except Exception as exc:
        print(f"  E-音频档: 跑崩 {type(exc).__name__}: {str(exc)[:120]}")
    # 我们自己的内核能不能把 ffmpeg 切的东西放起来（能放=可以当产品导出档）
    logs = []
    p = _new([], logs)
    if p is not None:
        try:
            for f in e_outputs + [aout]:
                if os.path.exists(f):
                    print(f"  内核放 {os.path.basename(f)}：{_playable(p, f, app)}")
        finally:
            p.quit()
    # ================= F/G. "停录之后多久能用"——收尾时机 =================
    # 上面几条腿暴露了一件事：文件字节数在录制中按 256KiB 一跳，`set stream-record ""` 之后
    # 0.8 秒未必收尾（C 腿量到 262144 + "File ended prematurely"，同一份文件到整趟结束才 626598）。
    # 产品要的是"停录后能不能立刻把路径给用户"，所以这条要单独量：F 是出厂缓存档（录制器压根
    # 没挂上的那种形状），G 是明确挂上了录制器的小缓存档——两种形状下的收尾时机不同，不能合并成一句。
    print("\n## F/G. 停录之后文件什么时候才'能用'（不等 quit，每 2 秒量一次）")
    for tag, extra, secs_rec in (("F 出厂缓存档", [], 3.0),
                                 ("G 小缓存档（录制器挂着）",
                                  ["--demuxer-max-bytes=65536", "--demuxer-max-back-bytes=0"], 5.0)):
        logs = []
        fpath = os.path.join(OUT_DIR, f"flush-{'f' if tag.startswith('F') else 'g'}.mkv")
        if os.path.exists(fpath):
            os.remove(fpath)
        p = _new(extra, logs)
        if p is None:
            continue
        try:
            p.open_files([SAMPLE])
            _pump(app, 2.0)
            print(f"  [{tag}] 开录 → {_set(p, fpath)}")
            pts = _curve(p, app, fpath, secs_rec)
            print(f"        录制中曲线={_fmt(pts)} 存在={os.path.exists(fpath)}")
            mark = _mark(p)
            print(f"        停录 → {_set(p, '')}")
            for i in range(6):
                _pump(app, 2.0)
                print(f"        停录后 {2 * (i + 1):2d}s 大小={_size(fpath):7d} "
                      f"ffmpeg：{_ff_valid(fpath)}")
            print(f"        内核 log（停录之后）：{_log_since(p, mark)}")
            print(f"        此刻还在放样片，直接换片再量一次：{_playable(p, fpath, app)}")
            _pump(app, 2.0)
            print(f"        换片后大小={_size(fpath)}  ffmpeg：{_ff_valid(fpath)}")
        finally:
            p.quit()
            _pump(app, 1.0)
        print(f"        quit 后：{_ff_info(fpath)}  ffmpeg：{_ff_valid(fpath)}")
        logs2: list[str] = []
        q = _new([], logs2)
        if q is not None:
            try:
                print(f"        新内核回放：{_playable(q, fpath, app)}")
            finally:
                q.quit()

    print("\n## 产物一览")
    for f in sorted(os.listdir(OUT_DIR)):
        print(f"    {f}:{_size(os.path.join(OUT_DIR, f))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
