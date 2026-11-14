"""媒体库自检：真扫描、真探测、真封面，重点盯"数字对不对得上"。

用法：
    python tests/test_library.py            # 正常自检：全部 PASS 才算过
    python tests/test_library.py --falsify  # 证伪：故意喂坏条件，检测器必须判 FAIL/抓到

为什么值得单开一份：媒体库这条链上最难看的事故不是崩，而是**静默给错数**——
探测判据说反（拿返回码当判据，于是每个文件都探成 0 秒）、索引复用把旧时长贴给新片、
跳目录的谓词写宽（用"包含"而不是"前缀"）把整片库跳没、封面命令的流开关写错（-vn 关的
正是视频流）却"降级得很优雅"：卡片照样有，只是每张都是占位块。四种都长得像"能用"。
所以每条判据都配已知答案的对照：同一棵树里放一个必须被跳过的文件和一个必须被收下的
孪生文件；复用与重探用状态行里的计数判，不靠"看起来扫完了"。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, ROOT)

from PyQt5.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import library as LB  # noqa: E402
from app.engine import posters as PP  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
VID = os.path.join(SAMPLES, "gelu-sample-1.mp4")     # 10.00s 1280x720
VID2 = os.path.join(SAMPLES, "gelu-sample-2.mp4")    # 6.00s 1280x720 @60fps
VID3 = os.path.join(SAMPLES, "gelu-sample-3.mkv")    # 8.02s 1280x720
BROKEN = os.path.join(SAMPLES, "broken.mp4")         # 8KB 伪 mp4：容器头读不出来
SRT = os.path.join(SAMPLES, "gelu-sample-1.srt")     # 同目录里的字幕，不该入表
FFMPEG = cfgmod.FFMPEG_EXE

BASE = os.path.join(cfgmod.DATA_DIR, "library-test")
TREE = os.path.join(BASE, "tree")                    # 主片库（6 个媒体文件）
DEEP = os.path.join(BASE, "deep")                   # 只给 walk 用，避免多出 8 次探测
OTHER = os.path.join(BASE, "tree2")                 # 兄弟前缀目录：验 remove_root 不误伤
PCACHE = os.path.join(BASE, "posters")
INDEX = os.path.join(BASE, "library.json")
INDEX2 = os.path.join(BASE, "library-crash.json")
INDEX3 = os.path.join(BASE, "library-mut.json")
INDEX4 = os.path.join(BASE, "library-race.json")
INDEX5 = os.path.join(BASE, "library-queue.json")

# walk 在 TREE 里**应该**收到的 6 个文件名（其余全是诱饵）
WANT = {"sample-1.mp4", "sample-2.mp4", "sample-3.mkv", "twin.mp4", "nearskip.mp4", "tone.mp4"}

GRAY = QImage.Format_Grayscale8
APP = QApplication.instance() or QApplication(sys.argv)
results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def pump(seconds: float, pred) -> bool:
    """跑事件循环等条件成立（扫描/封面的完成信号是跨线程 queued，没人转圈就永远收不到）。

    这里绝不能用 Library.wait()/PosterStore.wait()：它们只 sleep 不 processEvents，
    信号要等主循环转起来才投递，界面拿到的"表"会永远空着。
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    APP.processEvents()
    return bool(pred())


def copy(src: str, *parts: str) -> str:
    dst = os.path.join(TREE, *parts)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def build_tree() -> None:
    for d in (TREE, DEEP, OTHER, PCACHE):
        shutil.rmtree(d, ignore_errors=True)
    for f in (INDEX, INDEX2, INDEX3, INDEX4, INDEX5):
        try:
            os.remove(f)
        except OSError:
            pass
    os.makedirs(OTHER, exist_ok=True)
    shutil.copy2(VID, os.path.join(OTHER, "x.mp4"))
    sub = ("movies", os.path.join("movies", ".hidden"), os.path.join("movies", "$RECYCLE.BIN"),
           os.path.join("movies", "_visible"), os.path.join("movies", "RECYCLE-not-skip"), "audio")
    for d in sub:
        os.makedirs(os.path.join(TREE, d), exist_ok=True)
    copy(VID, "movies", "sample-1.mp4")
    copy(VID2, "movies", "sample-2.mp4")
    copy(VID3, "movies", "sample-3.mkv")
    copy(VID, "movies", "_visible", "twin.mp4")
    copy(VID, "movies", "RECYCLE-not-skip", "nearskip.mp4")
    copy(VID, "movies", ".hidden", "ghost.mp4")
    copy(VID, "movies", "$RECYCLE.BIN", "trash.mp4")
    copy(VID, "audio", "tone.mp4")
    copy(SRT, "movies", "gelu-sample-1.srt")
    with open(os.path.join(TREE, "movies", "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("不是媒体文件\n")
    # 深度阶梯：第 k 层放 deep-k.mp4（k=1..8），用来量 MAX_DEPTH 的边界到底在哪一层断
    cur = DEEP
    for k in range(1, 9):
        cur = os.path.join(cur, "d%d" % k)
        os.makedirs(cur, exist_ok=True)
        shutil.copy2(VID, os.path.join(cur, "deep-%d.mp4" % k))


def names(paths: list[str]) -> set[str]:
    return {os.path.basename(p) for p in paths}


def _same_file(a: str, b: str) -> bool:
    """Windows 路径同一性：不分大小写、斜杠方向无关、不带尾分隔符。"""
    def n(p: str) -> str:
        return os.path.normcase(os.path.abspath(p or "")).rstrip("\\/")
    return n(a) == n(b)


def counts(detail: str) -> tuple[int, int]:
    """从状态行里取（探测数, 复用数）；取不到返回 (-1,-1) 让判据自己红掉。"""
    m = re.search(r"探测 (\d+) / 复用 (\d+)", detail or "")
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def scan(lib: LB.Library, timeout: float = 180.0) -> str:
    """跑一趟扫描并回报状态行。信号是唯一真相来源：_on_done 里的 detail 才带计数。"""
    box: list[str] = []
    lib.sig_status.connect(box.append)
    started = lib.refresh()
    got = pump(timeout, lambda: bool(box) and not lib.running())
    try:
        lib.sig_status.disconnect(box.append)
    except TypeError:
        pass
    return (box[-1] if box else "") if (started and got) else ""


def in_flight_scenario(index_path: str, cls=None) -> dict:
    """真扫一趟（7 条）→ 模拟"这一趟还在途时，用户移除一个文件和一个根"→ 回包。

    为什么直接把 `_busy` 置真再调 `_on_done`：要判的正是"旧快照回包时怎么收口"，
    真等一趟扫描在快照之后才发生的移除是不可复现的时序（扫描几秒就跑完了）。
    其余步骤（forget／remove_root／_on_done／落盘）全走产品代码。`cls` 让证伪把
    `_on_done` 换成旧的"整表照抄"写法，同一条判据必须在它上面翻红。
    """
    cls = cls or LB.Library
    lib = cls(FFMPEG, index_path, [TREE, OTHER])
    first = scan(lib)
    pre_items = lib.items()
    pre_index = dict(lib._index)
    victim = os.path.abspath(os.path.join(TREE, "audio", "tone.mp4"))
    other_file = os.path.join(OTHER, "x.mp4")
    lib._busy = True                         # 这一趟已经拿着旧索引在跑
    forgot = lib.forget(victim)
    removed = lib.remove_root(OTHER)
    lib._on_done(pre_items, pre_index, "模拟在途回包")
    paths = [str(e.get("path")) for e in lib.items()]
    disk = set(LB.Library(FFMPEG, index_path, [])._index)
    return {"env": "", "first": first, "n0": len(pre_items), "n1": len(paths),
            "forgot": forgot, "removed": removed,
            "victim_left": any(_same_file(p, victim) for p in paths),
            "victim_disk": any(_same_file(p, victim) for p in disk),
            "other_left": any(_same_file(p, other_file) for p in paths),
            "other_disk": any(_same_file(p, other_file) for p in disk)}


def judge_in_flight(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (bool(m["first"]) and m["forgot"] and m["removed"] and m["n0"] == 7
          and m["n1"] == 5 and not m["victim_left"] and not m["victim_disk"]
          and not m["other_left"] and not m["other_disk"])
    return ok, (f"{m['n0']} 条 → 回包后 {m['n1']} 条（要 5）；被 forget 的还在={m['victim_left']}"
                f"／盘上={m['victim_disk']}；被移根的还在={m['other_left']}"
                f"／盘上={m['other_disk']}；旧整表照抄时这四条都会是 True、条数回 7")


def queued_scenario(index_path: str, cls=None) -> dict:
    """扫 TREE 的途中加入 OTHER 并 refresh：那趟必须在回包后真起，新根要被扫进来。"""
    cls = cls or LB.Library
    lib = cls(FFMPEG, index_path, [TREE])
    first = scan(lib)
    n0 = len(lib.items())
    added = lib.add_root(OTHER)
    lib._busy = True
    queued = lib.refresh()                   # 在途：应当记下而不是丢掉
    lib._on_done(lib.items(), lib._index, "模拟在途回包")
    done = pump(60.0, lambda: not lib.running())    # 旧写法不排队，这里立刻收（判据随后红）
    names_now = names([str(e.get("path")) for e in lib.items()])
    return {"env": "", "first": first, "n0": n0, "added": added, "queued": queued,
            "followups": lib.followups, "done": done,
            "has_x": "x.mp4" in names_now,
            "n1": len(names_now)}


def judge_queued(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (bool(m["first"]) and m["added"] and m["queued"] is False and m["followups"] == 1
          and m["done"] and m["has_x"] and m["n0"] == 6 and m["n1"] == 7)
    return ok, (f"在途 refresh 返回 {m['queued']}（要 False＝去重）、回包后补扫 {m['followups']} 趟"
                f"（要 1）、完成={m['done']}；{m['n0']} 条 → {m['n1']} 条，"
                f"新根文件进来={m['has_x']}；旧写法不排队时补扫 0 趟、仍 6 条")


def gray_stats(img: QImage) -> dict:
    g = img.convertToFormat(GRAY)
    ptr = g.constBits()
    ptr.setsize(g.sizeInBytes())
    data = bytes(ptr.asstring(g.sizeInBytes()))
    bpl, w, h = g.bytesPerLine(), g.width(), g.height()
    vals = [data[y * bpl + x] for y in range(h) for x in range(w)]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"mean": mean, "std": var ** 0.5, "distinct": len(set(vals)), "size": (w, h)}


def fresh_posters(max_jobs: int = 2) -> PP.PosterStore:
    shutil.rmtree(PCACHE, ignore_errors=True)
    os.makedirs(PCACHE, exist_ok=True)
    return PP.PosterStore(FFMPEG, PCACHE, max_jobs=max_jobs)


def main() -> int:
    t0 = time.time()
    if not os.path.exists(FFMPEG):
        check("runtime 里有 ffmpeg", False, FFMPEG)
        return report(t0)
    check("runtime 里有 ffmpeg", True, os.path.basename(FFMPEG))
    build_tree()

    # ---------- 1. 时长解析：纯文本已知答案，零进程 ----------
    check("parse_duration 认标准行（01:02:03.45 → 3723.45）",
          abs(LB.parse_duration("Duration: 01:02:03.45, start: 0.000000, bitrate: 505 kb/s")
              - 3723.45) < 1e-6, str(LB.parse_duration("Duration: 01:02:03.45")))
    check("parse_duration 没有小数秒时也自洽（00:00:59 → 59.0）",
          LB.parse_duration("Duration: 00:00:59, start: N/A") == 59.0)
    check("三位小数不被截断（0:01:59.876 → 119.876）",
          abs(LB.parse_duration("Duration: 0:01:59.876") - 119.876) < 1e-9,
          str(LB.parse_duration("Duration: 0:01:59.876")))
    check("小写 duration: 也认（IGNORECASE 不是装饰）",
          LB.parse_duration("duration: 00:00:10.00") == 10.0)
    bad_inputs = ["Duration: N/A, bitrate: N/A", "", "Duration: 1:2:3", "Input #0, mov,mp4"]
    check("畸形行一律落到 0.0（宁可显示 --:-- 也不给假时长）",
          all(LB.parse_duration(s) == 0.0 for s in bad_inputs),
          " ".join("%r→%.3f" % (s[:18], LB.parse_duration(s)) for s in bad_inputs))

    # ---------- 2. 真探测：容器三种、降级三种 ----------
    d1 = LB.probe_duration(FFMPEG, VID)
    d3 = LB.probe_duration(FFMPEG, VID3)
    check("mp4 探到 10.00s", abs(d1 - 10.0) <= 0.3, f"{d1:.3f}s")
    check("mkv 走同一条命令也探得到（8.02s）", abs(d3 - 8.02) <= 0.3, f"{d3:.3f}s")
    # 这条是**机制证据**：-i 不带输出必然非 0 退出，所以"返回码==0 才算成功"会把
    # 每一个好文件都探成 0 秒。留着它是因为判据写错时没人会发现。
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "info", "-nostdin", "-i", VID],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    check("探测命令确实非 0 退出（所以判据只能看 stderr）",
          r.returncode != 0 and LB.parse_duration(r.stderr) > 0,
          f"rc={r.returncode} 而 stderr 里探到 {LB.parse_duration(r.stderr):.2f}s")
    check("伪 mp4 探不到 → 0.0 且不抛", LB.probe_duration(FFMPEG, BROKEN) == 0.0,
          f"{LB.probe_duration(FFMPEG, BROKEN)}")
    check("文本文件冒充媒体 → 0.0", LB.probe_duration(
        FFMPEG, os.path.join(TREE, "movies", "notes.txt")) == 0.0)
    check("缺 ffmpeg / 文件不存在都降级为 0.0",
          LB.probe_duration(os.path.join(BASE, "no-ffmpeg.exe"), VID) == 0.0
          and LB.probe_duration(FFMPEG, os.path.join(TREE, "没了.mp4")) == 0.0)

    # ---------- 3. 收集：收谁、跳谁、去重、上限 ----------
    check("is_media 认容器与音频扩展名且不分大小写",
          all(LB.is_media("a" + e) for e in (".mp4", ".mkv", ".MP4", ".flac", ".rmvb")))
    check("is_media 不认字幕/图片/文本",
          not any(LB.is_media("a" + e) for e in (".srt", ".ass", ".jpg", ".txt", ".nfo")))
    found = LB.walk([TREE])
    got_names = names(found)
    check("walk 收全 6 个媒体文件（含 .mkv）", got_names == WANT,
          f"收到{sorted(got_names)} 缺{sorted(WANT - got_names)} 多{sorted(got_names - WANT)}")
    check("隐藏目录与回收站跳过，但名字相近的目录必须收下（前缀匹配不是子串匹配）",
          "ghost.mp4" not in got_names and "trash.mp4" not in got_names
          and "nearskip.mp4" in got_names and "twin.mp4" in got_names,
          f"跳过={sorted(n for n in got_names if n in {'ghost.mp4', 'trash.mp4'})} "
          f"收下={sorted(n for n in got_names if n in {'twin.mp4', 'nearskip.mp4'})}")
    check("同目录里的 notes.txt / .srt 不入表",
          not (got_names & {"notes.txt", "gelu-sample-1.srt"}))
    check("walk 去重：同一根给两遍不产生双份",
          len(LB.walk([TREE, TREE])) == len(found), f"{len(LB.walk([TREE, TREE]))} vs {len(found)}")
    check("父子根同给也不产生双份",
          len(LB.walk([TREE, os.path.join(TREE, "movies")])) == len(found))
    capped = LB.walk([TREE], max_files=2)
    check("max_files 是硬闸", len(capped) == 2, f"上限 2 → 实收 {len(capped)}")
    deep = names(LB.walk([DEEP]))
    want_deep = {"deep-%d.mp4" % k for k in range(1, 7)}
    check("深度上限在第 6 层断（deep-1..6 全收、deep-7 起不收）",
          deep == want_deep, f"收到 {sorted(deep)}")
    check("根不存在 / 根为空 → 空表且不抛",
          LB.walk([os.path.join(BASE, "没有这个目录")]) == [] and LB.walk([]) == [])
    check("返回的每个路径都在盘上且已 normpath",
          all(os.path.isfile(p) and p == os.path.normpath(p) for p in found),
          f"{len(found)} 个")

    # ---------- 4. entry_for：条目的每个字段都得有出处 ----------
    e = LB.entry_for(VID, 12.5)
    st = os.stat(VID)
    check("entry_for 的 size/mtime 来自 stat 而不是猜",
          e["size"] == st.st_size and abs(e["mtime"] - st.st_mtime) < 1.0
          and e["name"] == os.path.basename(VID), f"size={e['size']} mtime={e['mtime']:.1f}")
    check("entry_for 保留传入时长、ext 归一小写",
          e["dur"] == 12.5 and e["ext"] == ".mp4", f"dur={e['dur']} ext={e['ext']}")
    gone = LB.entry_for(os.path.join(BASE, "没了.mp4"))
    check("文件消失时 entry_for 降级为 0 而不是抛",
          gone["size"] == 0 and gone["mtime"] == 0.0, f"{gone}")

    # ---------- 5. Library 端到端：扫描、索引复用、集合维护 ----------
    lib1 = LB.Library(FFMPEG, INDEX, [TREE])
    detail = scan(lib1)
    items = lib1.items()
    check("首扫把 6 个条目全交给界面", len(items) == 6, f"{len(items)} 项 状态={detail}")
    probed, reused = counts(detail)
    check("首扫计数：探测 6 / 复用 0", (probed, reused) == (6, 0), f"探测{probed} 复用{reused}")
    check("首扫每条都探到时长（0 秒只允许出现在真探不到的文件上）",
          all(float(x["dur"]) > 0 for x in items),
          " ".join("%s=%.2f" % (x["name"], x["dur"]) for x in items))
    try:
        with open(INDEX, encoding="utf-8") as fh:
            saved = json.load(fh)
    except Exception as exc:
        saved = {"_err": str(exc)}
    check("索引落盘且 JSON 可读（键是绝对路径）",
          isinstance(saved, dict) and len(saved) == 6
          and all(isinstance(v, dict) and "dur" in v for v in saved.values()),
          f"{len(saved) if isinstance(saved, dict) else saved} 键")

    lib2 = LB.Library(FFMPEG, INDEX, [TREE])          # 模拟重启：索引从盘上读回来
    detail2 = scan(lib2)
    check("二扫全复用（探测 0 / 复用 6）：没变过的文件不再重探",
          counts(detail2) == (0, 6), f"状态={detail2}")

    tone = os.path.join(TREE, "audio", "tone.mp4")
    with open(tone, "ab") as fh:
        fh.write(b"\0" * 2048)
    check("文件被改动后只重探那一个（探测 1 / 复用 5）",
          counts(scan(lib2)) == (1, 5), f"状态={lib2.last_status}")

    os.utime(tone, (time.time() + 5, time.time() + 5))
    check("只改 mtime（大小没变）也必须重探——复用条件是两者都不变",
          counts(scan(lib2)) == (1, 5), f"状态={lib2.last_status}")

    lib2.roots = [TREE]
    box: list[str] = []
    lib2.sig_status.connect(box.append)
    first = lib2.refresh()
    dup = lib2.refresh()
    settled = pump(180.0, lambda: len(box) >= 2 and not lib2.running())
    try:
        lib2.sig_status.disconnect(box.append)
    except TypeError:
        pass
    check("扫描进行中重复 refresh 不再排队（忙标记在 start 前置真，没有窗口期）",
          first is True and dup is False and settled,
          f"first={first} dup={dup} 收尾={settled} 状态行={len(box)} 条")
    try:
        readable = lib2.running()
        crash = ""
    except Exception as exc:
        readable, crash = None, f"{type(exc).__name__}: {exc}"
    check("扫描结束后 running() 仍可读（QRunnable 的 Python 引用没断）",
          readable is False, f"running={readable} {crash}")

    # 异常降级：扫描中途炸（这里用 monkeypatch 造一个必然的异常）只能让库空着，
    # 不能把异常抛到主线程。对照组在同一个实例上紧跟着跑一次正常扫描：
    # 若"空表"是测试自身把 lib 弄坏了，那条正常扫描也会空，判据就红。
    orig_walk = LB.walk
    lib3 = LB.Library(FFMPEG, INDEX2, [TREE])
    boom: list[str] = []
    lib3.sig_status.connect(boom.append)

    def raise_walk(*_a, **_k):
        raise RuntimeError("模拟扫描炸了")

    LB.walk = raise_walk
    try:
        lib3.refresh()
        got_crash = pump(30.0, lambda: bool(boom))
    except RuntimeError as exc:         # 抛到宿主就是 FAIL
        got_crash, boom = False, [f"异常逃到调用方：{exc}"]
    finally:
        LB.walk = orig_walk
    after = list(boom)
    empty_after_crash = list(lib3.items())
    LB.walk = orig_walk
    ok_again = scan(lib3)
    check("扫描抛异常 → 空表 + 状态行带异常名，宿主不跟着崩（且随后仍能正常扫）",
          got_crash and any("RuntimeError" in s for s in after) and not empty_after_crash
          and lib3.running() is False and len(lib3.items()) == 6,
          f"异常行={after[-1] if after else '无'} 恢复后={len(lib3.items())} 项")

    rel = os.path.relpath(OTHER, ROOT)
    c1 = lib2.add_root(TREE) is False                     # 已在库里
    c2 = lib2.add_root(OTHER) is True                     # 新根
    c3 = lib2.roots[-1] == os.path.abspath(OTHER)         # 存的是绝对路径
    c4 = lib2.add_root(rel) is False                      # 相对写法撞同一根
    c5 = lib2.add_root("") is False
    check("add_root：重复/空串拒绝，相对与绝对算同一个根，库里只存绝对路径",
          c1 and c2 and c3 and c4 and c5, f"roots={lib2.roots} 相对写法={rel}")

    victim = os.path.abspath(os.path.join(TREE, "audio", "tone.mp4"))
    n_before = len(lib2.items())
    ok_forget = lib2.forget(victim)
    left_names = names([x["path"] for x in lib2.items()])
    check("forget 同时摘掉条目和索引（下次扫描不会带着旧时长复活），未知路径返回 False",
          ok_forget is True and len(lib2.items()) == n_before - 1
          and LB._norm_path(victim) not in lib2._index and "tone.mp4" not in left_names
          and lib2.forget(os.path.join(BASE, "根本没这个.mp4")) is False,
          f"{n_before}→{len(lib2.items())} 索引还在={LB._norm_path(victim) in lib2._index}")

    # 「移除」只改内存的话，本次会话里看着删了、重启又整条复活——而界面上根本没有
    # "正在保存"这一档。所以判据不看内存，看**从盘上重新读回来的那一份**。
    lib2_again = LB.Library(FFMPEG, INDEX, [TREE])
    left_on_disk = [k for k in lib2_again._index if _same_file(k, victim)]
    check("forget 落盘：换一个 Library 从盘上读回来，被移除的那条不复活",
          not left_on_disk, f"读回 {len(lib2_again._index)} 条，仍含 {left_on_disk}")

    fake_sibling = os.path.join(OTHER, "keepme.mp4")
    lib2._index[fake_sibling] = {"size": 1, "mtime": 1.0, "dur": 3.0}
    # 直属文件：它的 dirname 恰好等于根本身（没有尾分隔符）。旧写法按
    # `dirname(k).startswith(root + sep)` 比，第一批被漏掉的正是这种条目。
    direct = os.path.join(TREE, "direct.mp4")
    lib2._index[direct] = {"size": 1, "mtime": 1.0, "dur": 2.0}
    removed = lib2.remove_root(TREE)
    # 判据用产品自己的前缀判据（带分隔符 + 规范形）：裸 startswith(TREE) 会把 tree2
    # 也算成"tree 剩下的人"，而对规范形键做裸 startswith 又会因盘符大小写漏判。
    purged = [k for k in lib2._index if LB._is_under(k, TREE)]
    check("remove_root 连带清掉该根下的索引（含直属文件），兄弟前缀目录不误伤",
          removed is True and not purged and direct not in lib2._index
          and fake_sibling in lib2._index,
          f"残留={len(purged)} 直属={direct in lib2._index} 兄弟保留={fake_sibling in lib2._index}")
    lib2_again = LB.Library(FFMPEG, INDEX, [TREE])
    # 索引键从盘上读回来时统一成规范形（normcase+normpath，见审计 3.3），比较也要按规范形。
    check("remove_root 也真落盘：盘上只剩兄弟目录那一条，直属文件不残留",
          set(lib2_again._index) == {LB._norm_path(fake_sibling)},
          f"盘上 {len(lib2_again._index)} 条：{sorted(os.path.basename(k) for k in lib2_again._index)}")
    check("remove_root 对不在库里的路径返回 False",
          lib2.remove_root(os.path.join(BASE, "没加过")) is False)

    # 扫描在工作线程里拿着旧索引跑；回包时用户可能已经"移除文件/移除根/加入根"。
    # 这三件事都必须在回包那一步收口，否则界面出现"删了又回来"、新目录要手动再刷。
    m_race = in_flight_scenario(INDEX4)
    ok_race, d_race = judge_in_flight(m_race)
    check("扫描途中 forget / 移除根：旧回包不许把它们带回来（内存与盘上都算）", ok_race, d_race)

    m_queue = queued_scenario(INDEX5)
    ok_queue, d_queue = judge_queued(m_queue)
    check("扫描途中加入目录：那次 refresh 排队到回包后再扫，新根真被扫进来", ok_queue, d_queue)

    # ---------- 6. PosterStore：封面真的从视频里来 ----------
    store = fresh_posters()
    key = store.request(VID, 10.0)
    got = pump(120.0, lambda: store.ready(key))
    path_out = store.file_of(key)
    check("请求后封面真的落盘（且不是半张：>512B）",
          got and os.path.getsize(path_out) > 512, f"key={key} 详情={store.last_info}")
    if not got:
        return report(t0)
    pm = store.image(key)
    if pm is None:
        check("封面取回来了（image 不许对已就绪的 key 返回 None）", False, f"key={key}")
        return report(t0)
    check("封面几何＝320×180（宽按 POSTER_W，高按源 16:9 取偶）",
          pm.width() == PP.POSTER_W and pm.height() == 180, f"{pm.width()}×{pm.height()}")
    sp = gray_stats(pm.toImage())
    flat = QPixmap(PP.POSTER_W, 180)
    flat.fill(QColor(128, 128, 128))
    sf = gray_stats(flat.toImage())
    check("封面是真画面（有细节）", sp["distinct"] >= 6,
          f"颜色{sp['distinct']} 标准差{sp['std']:.1f} 均值{sp['mean']:.1f}")
    check("对照组：纯色块必须被同一判据否掉", sf["distinct"] == 1,
          f"纯色 颜色{sf['distinct']} 标准差{sf['std']:.1f}")

    store2 = PP.PosterStore(FFMPEG, PCACHE, max_jobs=2)
    m1 = os.path.getmtime(path_out)
    k2 = store2.request(VID, 10.0)
    check("命中缓存不再起 ffmpeg（同一 key、盘上文件没被重写）",
          k2 == key and not store2._active and os.path.getmtime(path_out) == m1,
          f"active={list(store2._active)}")
    check("不存在的文件也能算出稳定 key（界面不会因 stat 失败而崩）",
          store2.key_for(tone + ".nope") == store2.key_for(tone + ".nope")
          and store2.key_for(tone + ".nope") != store2.key_for(tone),
          f"缺文件 key={store2.key_for(tone + '.nope')}")
    tmpc = os.path.join(PCACHE, "copy.mp4")
    shutil.copy2(VID, tmpc)
    old_k = store2.key_for(tmpc)
    with open(tmpc, "ab") as fh:
        fh.write(b"\0" * 4096)
    check("同一份内容改尺寸后必须换 key", store2.key_for(tmpc) != old_k,
          f"旧={old_k} 新={store2.key_for(tmpc)}")
    check("key 随宽度变（160 的封面不许塞进 320 的格子）",
          store2.key_for(VID, 160) != store2.key_for(VID, PP.POSTER_W))

    empty_dir = os.path.join(BASE, "posters-empty")
    shutil.rmtree(empty_dir, ignore_errors=True)
    os.makedirs(empty_dir, exist_ok=True)
    st4 = PP.PosterStore(os.path.join(BASE, "no-ffmpeg.exe"), empty_dir, max_jobs=1)
    k4 = st4.request(VID, 10.0)
    check("内核缺失：ready 为假、image 为 None、不抛、也不说自己在跑",
          st4.ready(k4) is False and st4.image(k4) is None and st4.running() == 0,
          f"key={k4}")
    st4.request(VID, 10.0)
    st4.request(VID, 10.0)
    check("失败的 key 被拉黑，重复请求不再起进程（不许每个卡片都撞一次墙）",
          k4 in st4._failed and not st4._active, f"failed={len(st4._failed)} active={len(st4._active)}")
    k5 = st4.request(os.path.join(TREE, "没了.mp4"), 10.0)
    check("源文件不存在同样降级（返回 key 但不 ready）",
          isinstance(k5, str) and st4.ready(k5) is False)

    st5 = fresh_posters()
    lie = st5.request(VID, 600.0)          # 撒谎的时长：10 秒的片说成 10 分钟 → 取 60s 那帧
    got_lie = pump(60.0, lambda: lie in st5._failed)
    check("时长撒谎时干净失败（拿不到帧就拉黑，不许冒充成功、不许卡住）",
          got_lie and st5.ready(lie) is False and st5.image(lie) is None,
          f"failed={lie in st5._failed} ready={st5.ready(lie)}")

    ghost_key = st5.key_for(os.path.join(TREE, "半张片.mp4"), 320)
    with open(st5.file_of(ghost_key) + ".part.jpg", "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0" + b"half-written" * 60)
    check("半成品 .part.jpg 不算就绪（只有 os.replace 之后的成品才算）",
          st5.ready(ghost_key) is False, f"file={os.path.basename(st5.file_of(ghost_key))}")

    newest = st5.request(VID2, 6.0)
    if not pump(120.0, lambda: st5.ready(newest)):
        check("第二张封面也落盘（prune 的对照组需要它）", False, "超时")
        return report(t0)
    os.utime(st5.file_of(newest), (time.time() + 30, time.time() + 30))
    for i in range(3):
        f = os.path.join(PCACHE, "old-%03d.jpg" % i)
        with open(f, "wb") as fh:
            fh.write(b"old")
        t = time.time() - 500 + i
        os.utime(f, (t, t))
    before = [f for f in os.listdir(PCACHE) if f.endswith(".jpg")]
    gone = st5.prune(keep=1)
    left = sorted(f for f in os.listdir(PCACHE) if f.endswith(".jpg"))
    check("prune 只留最新 1 张，且留下的确实是最新那张",
          gone == len(before) - 1 and left == [os.path.basename(st5.file_of(newest))],
          f"删{gone} 剩{left} 原有={sorted(before)}")
    for i in range(60):
        st5._img["fake%02d" % i] = pm
        st5._order.append("fake%02d" % i)
    st5.image(newest)
    check("image() 的内存缓存封顶 60 张（滚动大库不吃内存）",
          len(st5._order) <= 60 and "fake00" not in st5._img,
          f"缓存{len(st5._order)} 张")

    # ---- R17：「最近播放」的去重键——同一部片的两种写法只许占一格
    #      为什么在这里验而不是只在真机上看：用户真配置里就躺着一条 `F:/…` 与一条 `F:\\…`
    #      并排的重复（命令行形状探针喂进去的正斜杠形状留下的），可那种文件本机只有一份，
    #      判据不能依赖用户的片库；这里用自家样本的三种写法把同一件事量干净。
    BS_ = chr(92)
    rcfg = cfgmod.Config(os.path.join(BASE, "recent-dedup.json"))
    rcfg.data["recent"] = []
    back, fwd, mixed = VID, VID.replace(BS_, "/"), VID.replace("gelu-sample-1", "GELU-SAMPLE-1")
    rcfg.push_recent(back)
    rcfg.push_recent(fwd)
    check("同一部片换一种写法（\\ 与 /）再打开，「最近播放」不许并排两条",
          len(rcfg.data["recent"]) == 1 and rcfg.data["recent"][0] == fwd,
          f"{len(rcfg.data['recent'])} 条：{rcfg.data['recent']}")
    rcfg.push_recent(mixed)
    check("只有大小写不同也一样塌成一条（Windows 不分大小写）",
          len(rcfg.data["recent"]) == 1, f"{len(rcfg.data['recent'])} 条")
    rcfg.save()
    rcfg2 = cfgmod.Config(os.path.join(BASE, "recent-dedup.json"))
    check("落盘再读回来还是 1 条（不许只在内存里好看）",
          isinstance(rcfg2.data.get("recent"), list) and len(rcfg2.data["recent"]) == 1,
          f"读回 {rcfg2.data.get('recent')}")
    rcfg2.push_recent(VID2)
    check("去重不许过头：另一部片仍各占一条", len(rcfg2.data["recent"]) == 2,
          f"{len(rcfg2.data['recent'])} 条："
          f"{[os.path.basename(p) for p in rcfg2.data['recent']]}")
    rcfg2.push_recent("")
    check("空串这种怪写法不许把整表清掉（去重键只认同一部片）",
          len(rcfg2.data["recent"]) == 3 and VID2 in rcfg2.data["recent"],
          f"{len(rcfg2.data['recent'])} 条")

    return report(t0)


def report(t0: float) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 媒体库自检：{passed}/{len(results)} 通过"
          + (f"；失败：{fails}" if fails else "；无失败")
          + f"，用时 {time.time() - t0:.1f}s ----", flush=True)
    return 0 if passed == len(results) else 1


def probe_cmd(src: str, out: str, extra: str) -> tuple[bool, int]:
    """直接跑一条取帧命令，返回 (是否有产物, 返回码)。用来对照流开关写错的下场。"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-ss", "1.000", "-i", src, extra, "-sn", "-dn", "-frames:v", "1",
           "-vf", "scale=320:-2", out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (os.path.exists(out), r.returncode)


def falsify() -> int:
    """坏条件必须被抓到：返回码判据、只比大小、子串匹配、-vn、坏图、prune 乱序。"""
    print("---- 证伪模式：以下每一条都必须被抓到，否则媒体库自检不可信 ----")
    if not os.path.exists(FFMPEG):
        print("[SKIP] 没有 ffmpeg，证伪做不了")
        return 1
    build_tree()

    # L1：拿返回码当判据（看起来最合理的一条）会把每个好文件都探成 0 秒
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "info", "-nostdin", "-i", VID],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    falsified.append(("返回码判据必须被证伪：探测必然非 0 退出，而 stderr 里有真时长",
                      r.returncode != 0 and LB.parse_duration(r.stderr) > 0,
                      f"rc={r.returncode} 探到 {LB.parse_duration(r.stderr):.2f}s"))

    # L2：复用条件弱化成"只看大小"→ 只改 mtime 的文件会带着旧时长复活
    #     这里真跑一趟：索引里故意留一份"大小一样、mtime 旧、时长是假的 1.0s"
    idx = {VID: {"size": os.stat(VID).st_size, "mtime": os.stat(VID).st_mtime - 30.0,
                 "dur": 1.0}}
    with open(INDEX, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False)
    lib = LB.Library(FFMPEG, INDEX, [os.path.dirname(VID)])
    detail = scan(lib)
    probed, reused = counts(detail)
    dur = [e["dur"] for e in lib.items() if e["path"].lower() == VID.lower()]
    falsified.append(("只改过 mtime 的文件必须重探（不许把索引里的假 1.0s 当真时长）",
                      probed >= 1 and dur and abs(dur[0] - 1.0) > 0.5,
                      f"探测{probed} 复用{reused} 结果时长={dur}"))

    # L3：跳目录用"子串匹配"会把 RECYCLE-not-skip 这种正常目录一起跳没
    got = names(LB.walk([TREE]))
    falsified.append(("相近名字的目录必须仍能收进来（否则整片库可能被静默跳没）",
                      "nearskip.mp4" in got and "twin.mp4" in got,
                      f"收到={sorted(got)}"))

    # L4：流开关写成 -vn（关的正是视频流）——必须没有产物，好让上层判失败；
    #     对照组 -an 同一条命令必须有产物，否则"没产物"只是环境出了问题。
    ok_vn, rc_vn = probe_cmd(VID, os.path.join(BASE, "l4-vn.jpg"), "-vn")
    ok_an, rc_an = probe_cmd(VID, os.path.join(BASE, "l4-an.jpg"), "-an")
    falsified.append(("-vn 必须产不出封面而 -an 能（上层据此判失败，不能静默留空）",
                      (not ok_vn) and ok_an,
                      f"-vn rc={rc_vn} 产物={ok_vn} ／ -an rc={rc_an} 产物={ok_an}"))

    # L5：坏图（截断的 jpg）——ready 只查文件存在会放行，image 必须拒绝
    store = fresh_posters()
    k = store.request(VID, 10.0)
    if not pump(120.0, lambda: store.ready(k)):
        print("[SKIP] 主链没跑通，证伪做不了")
        return 1
    junk = os.path.join(PCACHE, "junkkey.jpg")
    with open(junk, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0\x00\x10junkjunkjunk")
    pm = store.image("junkkey")
    falsified.append(("盘上的坏图不许变成 QPixmap（界面必须落回占位块）",
                      pm is None, f"image 返回 {pm!r}"))

    # L6：prune 若没按 mtime 排序，"留最新一张"就是句空话——先删的必须是最旧的
    os.utime(store.file_of(k), (time.time() + 30, time.time() + 30))
    for i in range(3):
        f = os.path.join(PCACHE, "l6-%d.jpg" % i)
        with open(f, "wb") as fh:
            fh.write(b"x")
        t = time.time() - (1000 - i * 10)
        os.utime(f, (t, t))
    newest_file = os.path.basename(store.file_of(k))
    before = [f for f in os.listdir(PCACHE) if f.endswith(".jpg")]
    gone = store.prune(keep=1)
    left = [f for f in os.listdir(PCACHE) if f.endswith(".jpg")]
    falsified.append(("prune 留下的必须真是最新那张（删掉的必须含最旧的）",
                      left == [newest_file] and gone == len(before) - 1,
                      f"保留={newest_file} 剩={left} 删={gone} 原有={sorted(before)}"))

    # L7：remove_root 若少加了分隔符，移除 D:\\片库 会连带清掉 D:\\片库2
    lib2 = LB.Library(FFMPEG, INDEX2, [TREE, OTHER])
    fake = os.path.join(OTHER, "keepme.mp4")
    lib2._index[fake] = {"size": 1, "mtime": 1.0, "dur": 2.0}
    lib2.remove_root(TREE)
    falsified.append(("移除 tree 不许误伤兄弟目录 tree2 的索引",
                      fake in lib2._index
                      and not [k for k in lib2._index if k.startswith(TREE + os.sep)],
                      f"残留={len([k for k in lib2._index if k.startswith(TREE + os.sep)])}"))

    # L7b：落盘与直属文件。两条坏样本都是**上一版的真实写法**，而且都能在内存里
    #      表现得完全正确：只改内存的 forget 重启才复活；按 dirname 前缀比的
    #      remove_root 永远漏掉根目录的直属文件。所以判据必须从盘上读回来、
    #      而且直属文件必须真的进索引。
    def forget_would_survive(lib: LB.Library, path: str) -> bool:
        """判据：移除后换一个 Library 从盘上读回，那条**必须已经不在**。"""
        lib.forget(path)
        back = LB.Library(FFMPEG, lib.index_path, [TREE])
        return not any(_same_file(k, path) for k in back._index)

    class OldForgetLib(LB.Library):
        def forget(self, path: str) -> bool:      # 旧写法：只改内存，不落盘
            key = os.path.abspath(path)
            hit = self._index.pop(key, None) is not None
            self._items = [e for e in self._items if os.path.abspath(e["path"]) != key]
            return hit

    victim_m = os.path.join(TREE, "movies", "sample-1.mp4")
    old_lib = OldForgetLib(FFMPEG, INDEX3, [TREE])
    old_lib._index = {victim_m: {"size": 1, "mtime": 1.0, "dur": 9.0}}
    old_lib._items = [LB.entry_for(victim_m, 9.0)]
    old_lib.save_index()
    survived_old = not forget_would_survive(old_lib, victim_m)
    base_lib = LB.Library(FFMPEG, INDEX3, [TREE])
    base_lib._index = {victim_m: {"size": 1, "mtime": 1.0, "dur": 9.0}}
    base_lib._items = [LB.entry_for(victim_m, 9.0)]
    base_lib.save_index()
    saved_new = forget_would_survive(base_lib, victim_m)
    mutated_forget = OldForgetLib.forget is not LB.Library.forget
    falsified.append(("forget 不落盘时必须被盘上判据抓到（旧写法残留／现写法真删）",
                      mutated_forget and survived_old and saved_new,
                      f"变异生效={mutated_forget} 旧写法残留={survived_old} 现写法真删={saved_new}"))

    def root_would_purge(lib: LB.Library, root: str, direct: str, sibling: str) -> bool:
        """判据：移除根之后从盘上读回——直属文件与子目录条目都没了，兄弟还在。"""
        lib.remove_root(root)
        back = LB.Library(FFMPEG, lib.index_path, [root])
        keys = set(back._index)
        return direct not in keys and not [k for k in keys if k.startswith(root + os.sep)] \
            and sibling in keys

    class OldRootLib(LB.Library):
        def remove_root(self, path: str) -> bool:   # 旧写法：按 dirname 前缀清索引
            if path in self.roots:
                self.roots.remove(path)
                pre = os.path.abspath(path).lower() + os.sep
                for k in [k for k in self._index
                          if os.path.dirname(os.path.abspath(k)).lower().startswith(pre)]:
                    self._index.pop(k, None)
                self._items = [e for e in self._items
                               if not os.path.dirname(os.path.abspath(e["path"])).lower().startswith(pre)]
                return True
            return False

    direct_m = os.path.join(TREE, "direct.mp4")
    sub_m = os.path.join(TREE, "movies", "sample-2.mp4")
    sibling_m = os.path.join(OTHER, "keepme.mp4")
    seed = {direct_m: {"size": 1, "mtime": 1.0, "dur": 2.0},
            sub_m: {"size": 1, "mtime": 1.0, "dur": 3.0},
            sibling_m: {"size": 1, "mtime": 1.0, "dur": 4.0}}
    root_old = OldRootLib(FFMPEG, INDEX3, [TREE, OTHER])
    root_old._index = dict(seed)
    root_old._items = [LB.entry_for(p, v["dur"]) for p, v in seed.items()]
    root_old.save_index()
    leaked_old = not root_would_purge(root_old, TREE, direct_m, sibling_m)
    root_new = LB.Library(FFMPEG, INDEX3, [TREE, OTHER])
    root_new._index = dict(seed)
    root_new._items = [LB.entry_for(p, v["dur"]) for p, v in seed.items()]
    root_new.save_index()
    clean_new = root_would_purge(root_new, TREE, direct_m, sibling_m)
    mutated_root = OldRootLib.remove_root is not LB.Library.remove_root
    falsified.append(("remove_root 漏掉直属文件时必须被抓到（旧 dirname 写法漏／现写法清）",
                      mutated_root and leaked_old and clean_new,
                      f"变异生效={mutated_root} 旧写法漏={leaked_old} 现写法清={clean_new}"))

    # L8：正则若放宽到"任意 hh:mm:ss"，会从日志里的 time=00:00:03.21 编出一个时长
    liar = ("Duration: N/A, start: 0.000000, bitrate: N/A\n"
            "frame=  100 fps= 30 q=28.0 time=00:00:03.21 size=1024KiB\n")
    falsified.append(("没有 Duration 行时不许从 time= 那种进度行编出时长",
                      LB.parse_duration(liar) == 0.0, f"解析得 {LB.parse_duration(liar)}"))

    # L9：扫描异常之后 busy/running 必须落地，否则界面是一个永远转圈的加载态
    orig = LB.walk

    def raise_walk(*_a, **_k):
        raise RuntimeError("模拟扫描炸了")

    LB.walk = raise_walk
    try:
        box: list[str] = []
        bad = LB.Library(FFMPEG, INDEX2, [TREE])
        bad.sig_status.connect(box.append)
        bad.refresh()
        delivered = pump(30.0, lambda: bool(box))
    finally:
        LB.walk = orig
    falsified.append(("扫描抛异常后 running() 必须回 False（不许留永动加载态）",
                      delivered and bad.running() is False,
                      f"信号到达={delivered} running={bad.running()}"))

    # L7：去重键退回"原样比字符串"（这就是上一版的写法）——两种写法必须并排留下，
    #      而那正是用户在「最近播放」里看到的坏形状；现写法必须塌成一条。两条都成立
    #      才叫这条判据有牙：只看"现写法=1"的话，把键写成恒真也会过。
    def naive_push(recents: list[str], path: str) -> list[str]:
        out = [p for p in recents if p != path]           # noqa: SIM — 故意复刻旧写法
        out.insert(0, path)
        return out
    b_path, f_path = VID, VID.replace(chr(92), "/")
    n_old = len(naive_push([b_path], f_path))
    rcfg = cfgmod.Config(os.path.join(BASE, "recent-falsify.json"))
    rcfg.data["recent"] = [b_path]
    rcfg.push_recent(f_path)
    n_new = len(rcfg.data["recent"])
    falsified.append(("L7 去重键改回原样比就会留下两条（旧写法 2／现写法 1，两个数都得对上）",
                      n_old == 2 and n_new == 1, f"旧写法 {n_old} 条，现写法 {n_new} 条"))

    # L10：回包"整表照抄"（旧写法）——墓碑过滤、根过滤、在途补扫三件事一起没了。
    #      两条判据必须各自翻红；只看内存条数会被"盘上也复活"逃掉，所以两条都看。
    class OldDoneLib(LB.Library):
        def _on_done(self, items, index, detail):
            self._busy = False
            self._index = dict(index or {})
            self._items = list(items or [])
            err = self._save_index_note()
            if err:
                detail += f"（索引没存下：{err}）"
            self.last_status = detail
            self.sig_items.emit(list(self._items))
            self.sig_status.emit(detail)
            self.sig_busy.emit(False)

    m_old = in_flight_scenario(INDEX4, cls=OldDoneLib)
    ok_old, d_old = judge_in_flight(m_old)
    falsified.append(("扫描回包整表照抄（不滤墓碑、不按现根收口）必须被抓到",
                      not ok_old and OldDoneLib._on_done is not LB.Library._on_done, d_old))
    m_old2 = queued_scenario(INDEX5, cls=OldDoneLib)
    ok_old2, d_old2 = judge_queued(m_old2)
    falsified.append(("扫描回包后不补扫在途 refresh 必须被抓到",
                      not ok_old2 and OldDoneLib._on_done is not LB.Library._on_done, d_old2))

    miss = [n for n, ok, _ in falsified if not ok]
    for n, ok, d in falsified:
        print(f"[{'CAUGHT' if ok else 'MISSED'}] {n}  —— {d}", flush=True)
    if miss:
        print("!! 以下坏样本没被抓到，媒体库自检不可信：" + ", ".join(miss))
        return 1
    print(f"---- 证伪：{len(falsified)}/{len(falsified)} 条坏样本全部被抓到 ----")
    return 0


if __name__ == "__main__":
    sys.exit(falsify() if "--falsify" in sys.argv else main())
