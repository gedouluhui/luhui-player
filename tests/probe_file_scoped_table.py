"""小探测（一次性，不进常跑链）：`_FILE_SCOPED` 这张表**逐格**量——换文件时内核改不改值、推不推。

为什么要逐格量：playlist-pos 那一笔的机制是"抹掉＋内核不推第二发 ⇒ 缓存永远空着 ⇒
决策读默认值"（tests/out/probe-playlist-pos-wipe.txt），而当时我只量了四格
（playlist-pos／loop-file／ab-loop-a／ab-loop-b，见 tests/out/probe-globals-across-file.txt）
就说"其余无害"——那是猜。判"该不该留在表里"的标准是两件事合起来：

    留在表里（换文件时抹掉）安全 ⇔ 那一格换文件时【真值会变】或【内核会推第二发】
    该从表里摘掉            ⇔ 真值不变 且 不推 且【换文件之后有人在读它】
    判不了                  ⇔ 夹具造不出这一格（两趟都读不到）——**不许算进"无害"**

"有人读"这一列也不许抄：脚本现在扫 `app/` 源码里的 `prop("<格名>"` 字面读点，外加一组
**当场回源码核对**的间接锚点（`menus.TRACK_PROP` 那类把格名装进表的写法）；锚点找不到就
点名"锚点失效"并按最坏判（fail-closed），绝不静默降成"没人读"。

两条腿（同一颗内核、同一套判读，一次一条命令序列）：
  A 无声腿：`gelu-sample-1.mp4`(1280×720/10s/30fps) → `uhd/h264-4k.mp4`(3840×2160/20s/60fps)。
    故意选两支不同规格的：拿两支同规格的片子量 width/height，"没变"是夹具造的。
  B 有声腿（`--mute=yes --volume=0`，且**从这一条实例自己**回读 mute/volume 确认不会出声）：
    `chapters-2sub.mkv` → `chapters-2sub-b.mkv`（两支都带 3 章节＋2 条字幕轨，尺寸/时长/章节
    标题不同，音轨数一支 1 条、一支 2 条——`aid` 因此有两种形状可看；由本脚本按需拿
    runtime 里的 ffmpeg 现造）。
    这一腿专治 A 腿的夹具盲区：A 腿 `--no-audio`，`aid`/`audio-codec` 两趟都读不到，
    在 A 腿上判"该摘"是伪结果；章节同理（样片目录里没一支带章节）。
    第二支**也必须带章节**——上一趟第二支用的是没章节的样片，换过去那一下 `chapter`
    直接变成 property unavailable，两条腿都问不到 ⇒ 只能记"判不了"（留档
    probe-file-scoped-table-v2-091702.txt 那一行的来历）。

留档：tests/out/probe-file-scoped-table-*.txt。静音判据与收拾进程都走 `player_harness`
（只按显式 PID、命令行里认出自家才动）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from app.engine.ipc import MpvIpc                      # noqa: E402
from app.engine.player import MpvPlayer                # noqa: E402
from player_harness import ask_soft, kill_pids, silent_now, tell  # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
FFMPEG = os.path.join(ROOT, "runtime", "ffmpeg", "ffmpeg.exe")
S1 = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
S2 = os.path.join(HERE, "samples", "gelu-sample-2.mp4")
UHD = os.path.join(HERE, "samples", "uhd", "h264-4k.mp4")
FIXDIR = os.path.join(HERE, "out", "probe-fixture")
FIX = os.path.join(FIXDIR, "chapters-2sub.mkv")
FIX_B = os.path.join(FIXDIR, "chapters-2sub-b.mkv")

# 表的真源在产品那边（不手抄一份）；playlist-pos 刚被摘出去，列进来只为看它是不是
# "值会变"那一族；loop-file/ab-loop 三格同表外，作为"值不变且不推"的第二观测点。
KEYS = sorted(set(MpvPlayer._FILE_SCOPED) | {"playlist-pos"})
EXTRA = ["loop-file", "ab-loop-a", "ab-loop-b"]
ALL = sorted(set(KEYS) | set(EXTRA))
WIPED = sorted(set(MpvPlayer._FILE_SCOPED))            # 现在还在表里的（判"该摘"才有动作含义）

# 间接读点锚点：格名不在 `prop("…")` 字面里、而是装在表里传出去的写法。
# 每条都**当场回源码找这个字面**，找不到就点名并按"有人读"判（宁可多摘不许漏）。
INDIRECT = {
    "aid": ('app/ui/menus.py', '"audio": "aid"'),
    "vid": ('app/ui/menus.py', '"video": "vid"'),
    "sid": ('app/ui/menus.py', '"sub": "sid"'),
}


def readers(cell: str) -> tuple[list[str], list[str]]:
    """现扫 app/ 源码：谁在读这一格的缓存。回 (读点列表, 锚点问题列表)。"""
    hits: list[str] = []
    bad: list[str] = []
    pat = re.compile(r'prop\(\s*["\']' + re.escape(cell) + r'["\']')
    for base, _dirs, files in os.walk(os.path.join(ROOT, "app")):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(base, fn)
            try:
                with open(fp, encoding="utf-8") as fh:
                    src = fh.read()
            except BaseException:
                continue
            for i, line in enumerate(src.splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{os.path.relpath(fp, ROOT)}:{i}")
    rel, lit = INDIRECT.get(cell, (None, None))
    if rel:
        try:
            with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as fh:
                ok = lit in fh.read()
        except BaseException:
            ok = False
        if ok:
            hits.append(f"{rel}（间接：表驱动，格名不在 prop() 字面里）")
        else:
            bad.append(f"{rel} 里找不到 {lit!r} ⇒ 锚点失效，按最坏判'有人读'")
    return hits, bad


def build_fixtures() -> tuple[bool, str]:
    """按需造两条夹具：都是"3 章节＋2 字幕轨＋音轨"，但尺寸/时长/章节标题不一样。

    两支都带章节是**判据要求的**：`chapter` 这一格只在"有章节"的文件上问得到，
    第二支要是没章节，换过去就 property unavailable，那一格只能记"判不了"。
    已在的两支都复用（读数只认盘上这两支），不重新编码——避免同一份留档隔天对不上。
    """
    specs = {FIX: dict(size="640x480", secs=12, titles=("第一章 起点", "第二章 中段", "第三章 收尾")),
             FIX_B: dict(size="800x600", secs=15, titles=("甲片头", "乙片中", "丙片尾"))}
    todo = {k: v for k, v in specs.items() if not (os.path.exists(k) and os.path.getsize(k) > 10_000)}
    if not todo:
        return True, "、".join(f"{os.path.basename(k)} {os.path.getsize(k)}B（复用）" for k in specs)
    if not os.path.exists(FFMPEG):
        return False, f"没有 {FFMPEG}"
    os.makedirs(FIXDIR, exist_ok=True)
    made: list[str] = []
    for out, sp in todo.items():
        secs = int(sp["secs"])
        chap = out + ".chapters.txt"
        a_srt = out + ".a.srt"
        b_srt = out + ".b.srt"
        step = secs // 3
        with open(chap, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(";FFMETADATA1\n")
            for i, title in enumerate(sp["titles"]):
                s, e = i * step * 1000, (i + 1) * step * 1000 if i < 2 else secs * 1000
                fh.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={s}\nEND={e}\ntitle={title}\n")
        for path, text in ((a_srt, f"1\n00:00:00,000 --> 00:00:0{min(3, secs // 4)},000\n甲轨第一句\n\n"),
                           (b_srt, f"1\n00:00:01,000 --> 00:00:0{min(5, secs // 3)},000\n乙轨第一句\n\n")):
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        cmd = [FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-i",
               f"testsrc=size={sp['size']}:rate=25:duration={secs}", "-f", "lavfi", "-i",
               f"sine=frequency=440:duration={secs}", "-f", "lavfi", "-i",
               f"sine=frequency=880:duration={secs}", "-i", a_srt, "-i", b_srt, "-i", chap,
               "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s",
               "-metadata:s:a:0", "language=chi", "-metadata:s:a:1", "language=eng",
               "-metadata:s:s:0", "language=chi", "-metadata:s:s:1", "language=eng",
               "-map_chapters", "5", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "aac", "-c:s", "subrip", out]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if not os.path.exists(out):
            return False, f"{os.path.basename(out)} 造失败：{(r.stderr or r.stdout)[-300:]}"
        made.append(f"{os.path.basename(out)} {os.path.getsize(out)}B（现造）")
    return True, "、".join(made) + f"（另一支复用：{', '.join(os.path.basename(k) for k in specs if k not in todo) or '无'}）"


def run_leg(name: str, first: str, second: str, audio: bool,
            presets: list[list]) -> dict | None:
    """一条腿：起内核 → 订阅全部格 → 放上第一支并预设 → 换第二支 → 逐格回"变没变/推没推"。"""
    pipe = rf"\\.\pipe\gelu-probe-scoped-{name}-{int(time.time() * 1000) % 10_000_000}"
    cmd = [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
           "--idle=yes", "--force-window=yes", "--keep-open=yes", "--really-quiet"]
    cmd += ([] if audio else ["--no-audio"])
    cmd += (["--mute=yes", "--volume=0"] if audio else [])
    p = subprocess.Popen(cmd, cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"\n[probe] ==== 腿 {name}：{os.path.basename(first)} → {os.path.basename(second)}"
          f"（{'有声但静音' if audio else '无声'}）pid={p.pid} pipe={pipe}", flush=True)
    ipc = MpvIpc(pipe)
    pushes: list[str] = []
    mark = threading.Event()
    try:
        if not ipc.connect(timeout=10.0, proc=p):
            print(f"[probe] 腿 {name} 连不上 IPC（记本腿作废，不替产品下结论）", flush=True)
            return None
        if audio:
            mute, miss = ask_soft(pipe, ["mute", "volume"], wait=6.0)
            ok = bool(mute) and silent_now(mute)
            print(f"[probe] 腿 {name} 静音回读（从这条实例自己问）：{mute} 读不到={miss or '（没有）'}"
                  f" ⇒ {'确实不会出声' if ok else '不敢放行'}", flush=True)
            if not ok:
                return None
        for k in ALL:
            ipc.observe(k, (lambda nm: (lambda v: pushes.append(
                f"{nm}={v!r} {'换文件后' if mark.is_set() else '换文件前'}")))(k))
        time.sleep(1.0)
        tell(pipe, ["loadfile", first, "replace"])
        time.sleep(3.0)
        tell(pipe, ["loadfile", second, "append"])       # 只进列表，不动当前播放
        time.sleep(1.0)
        for c in presets:
            tell(pipe, c)
            time.sleep(0.6)
        time.sleep(1.5)
        on1, miss1 = ask_soft(pipe, ALL + ["path"], wait=8.0)
        print(f"[probe] 腿 {name} ①第一支读数：{ {k: on1.get(k, '（读不到）') for k in ALL} }",
              flush=True)
        print(f"[probe] 腿 {name} ①读不到的格：{sorted(miss1) or '（没有）'}", flush=True)
        if not on1 or on1.get("path") != first:
            print(f"[probe] 腿 {name} 前提立不住：path 读回来不是第一支"
                  f"（{on1.get('path')!r}）⇒ 本腿不作数", flush=True)
            return None

        pushes.clear()
        mark.set()
        tell(pipe, ["set_property", "playlist-pos", "1"])
        time.sleep(4.0)
        on2, miss2 = ask_soft(pipe, ALL + ["path"], wait=8.0)
        print(f"[probe] 腿 {name} ②第二支读数：{ {k: on2.get(k, '（读不到）') for k in ALL} }",
              flush=True)
        print(f"[probe] 腿 {name} ③换文件那一下内核推过：{sorted({s.split('=', 1)[0] for s in pushes})}",
              flush=True)
        if on2.get("path") != second:
            print(f"[probe] 腿 {name} 前提立不住：换完文件 path 不是第二支"
                  f"（{on2.get('path')!r}）⇒ 本腿不作数", flush=True)
            return None
        return {"leg": name, "on1": on1, "on2": on2, "miss": sorted(set(miss1) | set(miss2)),
                "notfound": {m.split("（", 1)[0] for m in (miss1 + miss2)
                             if "property not found" in m},
                "pushed": {s.split("=", 1)[0] for s in pushes if " 换文件后" in s}}
    finally:
        try:
            ipc.close()
        except BaseException:
            pass
        kill_pids([p.pid])
        print(f"[probe] 腿 {name} 内核已收", flush=True)


def main() -> int:
    print(f"[probe] 量的格数={len(ALL)}（表的真源在 app/engine/player.py，这里现取；"
          f"表内 {len(WIPED)} 格＋表外对照 {len(ALL) - len(WIPED)} 格）", flush=True)
    fixture_ok, fixture_msg = build_fixtures()
    print(f"[probe] 夹具：{fixture_msg}", flush=True)

    legs: list[dict] = []
    legs.append(run_leg("A", S1, UHD, audio=False,
                        presets=[["set_property", "loop-file", "inf"],
                                 ["set_property", "ab-loop-a", "1.0"],
                                 ["set_property", "ab-loop-b", "2.0"]]))
    if fixture_ok:
        legs.append(run_leg("B", FIX, FIX_B, audio=True,
                            presets=[["set_property", "sid", "1"],
                                     ["set_property", "secondary-sid", "2"],
                                     ["set_property", "aid", "1"],
                                     ["set_property", "loop-file", "inf"],
                                     ["set_property", "ab-loop-a", "1.0"],
                                     ["set_property", "ab-loop-b", "2.0"]]))
    legs = [x for x in legs if x]
    if not legs:
        print("[probe] 两条腿都没跑成（IPC/静音/前提任一）⇒ 不作数，退出 2", flush=True)
        return 2

    print("\n[probe] 逐格（任一条腿看到'值变了'或'被推过'即算安全；所有能测的腿都"
          "『不变且不推』才算该摘；一条腿都测不到分两家：内核查无此格／夹具造不出＝判不了）",
          flush=True)
    print("格名 | 在表里 | 各腿读数（换文件前→后） | 值变了 | 被推过 | 谁读它 | 判读",
          flush=True)
    wipe: list[str] = []
    wipe_noread: list[str] = []
    blind: list[str] = []
    dead: list[str] = []
    for k in ALL:
        rows = [lg for lg in legs if k in lg["on1"] and k in lg["on2"]]
        changed = any(str(lg["on1"][k]) != str(lg["on2"][k]) for lg in rows)
        pushed = any(k in lg["pushed"] for lg in rows)
        hits, anchor_bad = readers(k)
        read_by = f"{len(hits)} 处：" + ", ".join(hits[:3]) if hits else "没扫到直接读点"
        if anchor_bad:
            read_by += "；⚠" + "；".join(anchor_bad)
        shown = [f"{lg['leg']}:{str(lg['on1'][k])[:18]!r}→{str(lg['on2'][k])[:18]!r}"
                 for lg in rows]
        meas = ", ".join(shown) if shown else "（没有一条腿读得到这一格）"
        push_legs = "/".join(lg["leg"] for lg in rows if k in lg["pushed"]) or "无"
        in_table = "是" if k in WIPED else "否（已摘）"
        if not rows:
            # 分两家：property not found ＝这颗内核根本没这一格（与夹具无关，订阅它就是死条目）；
            # property unavailable ＝格在、此刻没值＝夹具造不出这一格（换夹具能测，记"判不了"）。
            nf = all(k in lg["notfound"] for lg in legs)
            if nf:
                verdict = ("内核里查无此格（每条腿都 property not found）⇒ 订阅它永远填不上，"
                           "抹不抹都是空操作；这一格不归本趟判，归「属性名存不存在」那本账")
                if k in WIPED:
                    dead.append(k)
            else:
                verdict = "判不了：夹具造不出这一格 ⇒ 不许写进'无害'"
                if k in WIPED:
                    blind.append(k)
        elif changed:
            verdict = "留在表里安全（值会变；抹掉正是防陈旧）"
        elif pushed:
            verdict = "留在表里安全（值没变，但内核会补一发把缓存填回来）"
        elif hits or anchor_bad:
            verdict = "★该摘：值不变又不推，而换文件后有人读 ⇒ 抹了就永远空着"
            if k in WIPED:
                wipe.append(k)
        else:
            verdict = "★该摘（但当前没人读，摘不摘不改变行为）"
            if k in WIPED:
                wipe_noread.append(k)
        print(f"{k} | {in_table} | {meas} | {changed} | {push_legs} | {read_by} | {verdict}",
              flush=True)

    print("", flush=True)
    print(f"[probe] 每条腿读不到的格：" +
          "；".join(f"{lg['leg']}腿 {lg['miss'] or '（没有）'}" for lg in legs), flush=True)
    safe = len(WIPED) - len(wipe) - len(wipe_noread) - len(blind) - len(dead)
    total = len(wipe) + len(wipe_noread) + len(blind) + len(dead) + safe
    print(f"[probe] [FINAL] 逐格 {len(ALL)} 格（表内 {len(WIPED)}＋表外对照 {len(ALL) - len(WIPED)}）："
          f"该摘且有人读 {len(wipe)} {wipe or '（无）'}；该摘但没人读 {len(wipe_noread)} "
          f"{wipe_noread or '（无）'}；查无此格 {len(dead)} {dead or '（无）'}；"
          f"判不了（夹具）{len(blind)} {blind or '（无）'}；留在表里有实测依据 {safe}", flush=True)
    print(f"[probe] 分桶对账：{len(wipe)}+{len(wipe_noread)}+{len(dead)}+{len(blind)}+{safe}"
          f"={total}，表内格数={len(WIPED)} ⇒ "
          f"{'逐格都归了桶' if total == len(WIPED) else '★有格没归桶或多归，本趟不作数'}", flush=True)
    return 0 if total == len(WIPED) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.exit(2)
