"""真内核量具 v2：这个内核（v0.41.0-1101）到底认哪一种 `loadfile` 形状。

事故（用户 2026-10-19 双击《美国队长3》）：窗口开出来了、界面印了"已载入 1 个文件"，画面一直是黑的。
内核日志里只有一条错误：`Command loadfile: argument index has incompatible type.`
`app/engine/player.py` 在有断点续播位置时发的是
`["loadfile", <片>, "replace", ["start=1234.500"]]`，而这个内核的第 4 个位置参数是**整数 index**，
`["start=…"]` 落在那一位上 → 整条命令被拒 → 片一条都没进列表。

v1 的教训（留档 probe-loadfile-index-1019-204521.txt）：它用 `playlist-clear` 清场，而
**mpv 的 playlist-clear 保留当前那一条**，于是"被拒"的那几步读到的仍是上一支在播的位置，
`落列表` 那一栏全是假的。v2 改用 `stop`（真正把列表清空并回 idle），每步先自证前提：
`playlist-count==0` 才发命令，被拒的步就如实报 count==0。

每一量两件事：①内核应答的 error（`tell` 那句人话）＋②`--log-file` 里这一句新添的 debug 原文
（"哪个参数、为什么不合型"只有内核自己说得出）。最后再量**能用的那条路**：
`loadfile` 不带选项 → 立刻 `seek`／`set_property playback-time`，位置到底落不落地。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from player_harness import ask_soft, kill_pids, tell  # noqa: E402

# 量具自己先兜住输出编码：GBK 控制台里一个"⏎"就能把整趟劈死（本机实发），
# 而崩在 print 上会被误读成"内核不给跑"。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
S1 = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
LOG = os.path.join(HERE, "out", "probe-loadfile-index-kernel.log")
START = 3.0
PROPS = ["playlist-count", "playlist-pos", "path", "playback-time", "duration", "idle-active"]


def read_log(since: int) -> str:
    """这一步之后内核新写的日志（只要带 loadfile/参数/弃用字样的那几行）。"""
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            fh.seek(since)
            txt = fh.read()
    except OSError:
        return "（读不到日志）"
    keep = [ln.strip() for ln in txt.splitlines()
            if ("loadfile" in ln or "ncompatible" in ln or "eprecat" in ln or "nvalid" in ln)]
    return " ⏎ ".join(keep[-3:]) or "（内核没写相关行）"


def log_size() -> int:
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def snap(pipe: str) -> dict:
    got, _miss = ask_soft(pipe, PROPS, wait=4.0)
    return got


def step(pipe: str, n: str, desc: str, cmd, settle: float = 1.4) -> dict:
    tell(pipe, ["stop"])
    time.sleep(0.5)
    pre = snap(pipe)
    clean = (pre.get("playlist-count") == 0)
    mark = log_size()
    rep = tell(pipe, cmd)
    time.sleep(settle)
    got = snap(pipe)
    pt, dur = got.get("playback-time"), got.get("duration")
    cnt = got.get("playlist-count")
    near = landed(pt, settle=settle)
    print(f"[{n}] {desc}\n    命令={str(cmd)[:150]}\n    清场自证：count={pre.get('playlist-count')} "
          f"{'（是空的，前提立住）' if clean else '（没清干净，这一步的位置不可信）'}\n"
          f"    应答={rep}\n    内核原话={read_log(mark)}\n"
          f"    之后：count={cnt} pos={got.get('playlist-pos')} 位置={pt} 片长={dur} "
          f"→ 落了={'是' if cnt else '否'}，位置≈{START}={'是' if near else '否'}", flush=True)
    return {"n": n, "desc": desc, "reply": rep, "count": cnt, "pt": pt,
            "at_start": near, "前提立住": clean}


def wait_loaded(pipe: str, want: float = 8.0) -> tuple[float, float]:
    """等到"这一支真的载入了"（playback-time 有数）为止，回 (花掉几秒, 当时的位置)。

    为什么要单独这么一步：v1/v2 第一版都在 `loadfile` 之后固定睡 1.2s 就发定位命令，
    三条路全部回 `property unavailable`／`error running command`——**文件还没载入，定位当然发不动**。
    而那一版的容差写成 ±1.6，把"根本没定位成"的 1.4 秒读成"≈3.0 是"，判决行跟着说了假话。
    这一条把"等到载入"和"容差"都收口：先等出数，再发命令，判据只认"位置确实被抬到 start 之后"。
    """
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < want:
        got = snap(pipe)
        pt = got.get("playback-time")
        if isinstance(pt, (int, float)):
            return time.perf_counter() - t0, pt
        time.sleep(0.2)
    return time.perf_counter() - t0, None


def landed(pt, start: float = START, settle: float = 1.4) -> bool:
    """定位成没成：只认"落在 [start, start+settle+0.8] 这段"。

    不写 ±1.6 那种"两头都能算对"的容差——没定位成时位置是 1.3～1.5，落在 ±1.6 里就被判成"是"。
    """
    return isinstance(pt, (int, float)) and start <= pt <= start + settle + 0.8


def timed(pipe: str, n: str, desc: str, load_cmd, then_cmd, settle: float = 1.4) -> dict:
    """载入 → 等到真载入 → 再发定位命令 → 看位置。这条路才是"能不能续播"的裁判。"""
    tell(pipe, ["stop"])
    time.sleep(0.5)
    pre = snap(pipe)
    mark = log_size()
    rep1 = tell(pipe, load_cmd)
    spent, pt0 = wait_loaded(pipe)
    rep2 = tell(pipe, then_cmd) if then_cmd else ""
    time.sleep(settle)
    got = snap(pipe)
    pt = got.get("playback-time")
    ok = landed(pt, settle=settle)
    print(f"[{n}] {desc}\n    命令=({str(load_cmd)[:110]}, {str(then_cmd)[:90]})\n"
          f"    清场自证：count={pre.get('playlist-count')} 等载入花了 {spent:.1f}s（到时位置 {pt0}）\n"
          f"    应答=({rep1} / {rep2 or '对照：不发定位命令'})\n    内核原话={read_log(mark)}\n"
          f"    之后：count={got.get('playlist-count')} 位置={pt} → 落到 {START}±={ok}", flush=True)
    return {"n": n, "desc": desc, "reply": f"{rep1} / {rep2}" if rep2 else rep1,
            "count": got.get("playlist-count"),
            "pt": pt, "at_start": ok, "前提立住": pre.get("playlist-count") == 0}


def main() -> int:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "w", encoding="utf-8").close()
    ver = subprocess.run([MPV, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    pipe = rf"\\.\pipe\gelu-probe-loadfile-{int(time.time())}"
    cmd = [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
           "--idle=yes", "--force-window=yes", "--keep-open=yes", "--no-audio",
           "--mute=yes", "--volume=0", "--log-file=" + LOG,
           # 提级用 `--msg-level=all=v`，不用 `--log-level=debug`：本机实测这个构建里
           # `--log-level=debug`／`=v` 会让内核启动 2 秒就 rc=1 静默退出（版本头写完就没），
           # 而产品自己走的是 `--msg-level=`（见 player.build_args 那句注释），照产品这条来。
           "--msg-level=all=v", "--really-quiet"]
    p = subprocess.Popen(cmd, cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[probe] {ver}\n[probe] pid={p.pid} 片长基准：start={START}", flush=True)
    rows: list[dict] = []
    try:
        base = snap(pipe)
        if not base:
            print("[probe] 内核没答话：这趟不作数（env）", flush=True)
            return 2
        print(f"[probe] 起步 {base}", flush=True)

        rows.append(step(pipe, "A0", "对照：只给 片+模式（必须成）", ["loadfile", S1, "replace"]))
        rows.append(step(pipe, "A1", "片+模式+整数 index（选项不给）", ["loadfile", S1, "replace", -1]))
        rows.append(step(pipe, "P0", "**产品现在发的形状**：选项列表塞第 4 位",
                         ["loadfile", S1, "replace", [f"start={START:.3f}"]]))
        rows.append(step(pipe, "P1", "补 index 后再给选项列表（v1 猜的解法）",
                         ["loadfile", S1, "replace", -1, [f"start={START:.3f}"]]))
        rows.append(step(pipe, "P2", "选项给 JSON 对象（不给列表）",
                         ["loadfile", S1, "replace", {"start": START}]))
        rows.append(step(pipe, "P3", "index + 对象选项",
                         ["loadfile", S1, "replace", -1, {"start": START}]))
        rows.append(step(pipe, "N1", "命名参数形状：args 给对象",
                         {"name": "loadfile", "args": {"url": S1, "mode": "replace",
                                                       "options": {"start": START}}}))
        rows.append(step(pipe, "N2", "命名参数形状：url/mode 只给这两个",
                         {"name": "loadfile", "args": {"url": S1, "mode": "replace"}}))
        rows.append(step(pipe, "Q1", "options 给**裸字符串**（A1 那行内核原话把 options 印成 \"\"，像字符串）",
                         ["loadfile", S1, "replace", -1, f"start={START:.3f}"]))
        rows.append(step(pipe, "Q2", "命名形状写对了：command 回对象里有 name/args",
                         {"command": {"name": "loadfile",
                                      "args": {"url": S1, "mode": "replace",
                                               "options": {"start": START}}}}))
        rows.append(step(pipe, "Q3", "命名形状 + index + 裸字符串 options",
                         {"command": {"name": "loadfile",
                                      "args": {"url": S1, "mode": "replace", "index": -1,
                                               "options": f"start={START:.3f}"}}}))
        rows.append(step(pipe, "Q4", "只给 片（不给模式）", ["loadfile", S1]))
        # 能用的那条路：先不带选项落进来，**等到真载入**再定位（产品里就是 open_files → file-loaded → seek）
        rows.append(timed(pipe, "S1", "载入后 set_property playback-time",
                          ["loadfile", S1, "replace"], ["set_property", "playback-time", START]))
        rows.append(timed(pipe, "S2", "载入后 seek absolute exact",
                          ["loadfile", S1, "replace"], ["seek", START, "absolute", "exact"]))
        rows.append(timed(pipe, "S3", "载入后 seek absolute（不给 exact）",
                          ["loadfile", S1, "replace"], ["seek", START, "absolute"]))
        rows.append(timed(pipe, "S4", "载入后 set_property time-pos",
                          ["loadfile", S1, "replace"], ["set_property", "time-pos", START]))
        # 对照：载入后什么定位命令都不发——这一条必须"没落到 3"，否则上面几条的"落到了"没有鉴别力
        rows.append(timed(pipe, "S0", "载入后不发第二条（对照：位置该停在 0 附近）",
                          ["loadfile", S1, "replace"], None))
        # 载入**之前**发 set_property start 是 S3 之外的另一条候选（能不能把 start 当属性预置）
        tell(pipe, ["stop"])
        time.sleep(0.5)
        mark = log_size()
        rep0 = tell(pipe, ["set_property", "start", START])
        rep1 = tell(pipe, ["loadfile", S1, "replace"])
        spent, _p = wait_loaded(pipe)
        time.sleep(1.4)
        got = snap(pipe)
        pt = got.get("playback-time")
        print(f"[S5] 载入**前** set_property start（预置那一档）\n    应答=({rep0} / {rep1})\n"
              f"    内核原话={read_log(mark)}\n    位置={pt}（等载入 {spent:.1f}s）→ 落到 {START}="
              f"{landed(pt)}", flush=True)
        rows.append({"n": "S5", "desc": "载入前 set_property start", "reply": f"{rep0} / {rep1}",
                     "count": got.get("playlist-count"), "pt": pt,
                     "at_start": landed(pt), "前提立住": True})
    finally:
        kill_pids([p.pid])
        try:
            p.wait(timeout=6)
        except BaseException:
            p.kill()

    print("\n==== 判决（「收下」只看第一条命令=loadfile 的应答；定位那条单列）====", flush=True)
    for r in rows:
        parts = r["reply"].split(" / ")
        got = parts[0].startswith("已发出")
        second = ("｜定位=" + ("成" if parts[1].startswith("已发出") else "不成")) if len(parts) > 1 else ""
        print(f"  {r['n']:<3} 收下={'是' if got else '否'} 落了={'是' if r['count'] else '否'} "
              f"落到{START}s={'是' if r['at_start'] else '否'} 前提={'立' if r['前提立住'] else '不立'}"
              f"{second} | {parts[0][:60]}", flush=True)
    ctrl = next((r for r in rows if r["n"] == "S0"), None)
    if ctrl is not None and ctrl["at_start"]:
        print("!! 对照那步（载入后什么都不发）也「落到 3s」——这把尺没有鉴别力，上面所有"
              "「落到=是」都不许当结论用（先修尺，再谈内核）", flush=True)
    print("\n[probe] 留档 " + os.path.relpath(LOG, ROOT), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
