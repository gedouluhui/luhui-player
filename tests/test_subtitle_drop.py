# -*- coding: utf-8 -*-
"""拖入字幕的路由：**字幕不是片源**，别让它跟着 `expand()` 一起被丢掉。

用户形状：把一支 .mp4 和它的 .srt 一起拖进窗口；或者片子正播着，再把 .srt 拖进来。
旧写法把每个路径都交给 `player.expand()`——它对 .srt 回空表，于是界面只回一句
"没有找到可播放的文件"，字幕压根没进内核；和视频一起拖时更隐蔽：片能放，字幕静默消失。

三种时序必须分开（`sub-add` 只在 `file-loaded` 之后认）：
  · 一起拖、这一趟会**换当前条目**（plays=True）→ 字幕先记进 `_pending_subs`，等
    内核亲口说 file-loaded 再挂；
  · 往**正在播**的队尾追加（plays=False）→ 这条路上永远没有 file-loaded，字幕立刻挂
    当前这一支；
  · 只拖字幕 → 有片就立刻挂，没片就说"先播放一支视频"（不许向内核发必失败的 sub-add）。

跑法：
    python tests/test_subtitle_drop.py
    python tests/test_subtitle_drop.py --falsify   # 每种坏写法都必须让至少一条判据翻红
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("GELU_DATA", os.path.join(tempfile.gettempdir(), f"gelu-subs-{os.getpid()}"))
sys.path.insert(0, ROOT)

from app.engine.player import MpvPlayer                    # noqa: E402
from app.ui import main_window as mw                       # noqa: E402

VID = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-1.mp4"))
VID2 = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-2.mp4"))
SRT = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-1.srt"))

RESULTS: list[tuple[str, bool, str]] = []
FALSIFIED: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def f_case(name: str, caught: bool, detail: str = "") -> None:
    FALSIFIED.append((name, bool(caught), detail))
    print(f"[{'CAUGHT' if caught else 'MISSED'}] {name}" + (f"  —— {detail}" if detail else ""),
          flush=True)


class FakePlayer:
    """只回答 `add_paths`/`_on_file_loaded` 真正会问的那几件事。

    `expand()` 故意走**真的** `MpvPlayer.expand`：旧路线的毛病正是"它对 .srt 回空表"，
    换成假 expand 就没有这条判据了。其余属性只为让真方法跑得下去。
    """

    def __init__(self, path: str = "", plays: bool = True, running: bool = True):
        self.running = running
        self._path = path
        self._plays = plays
        self.expand_calls = 0
        self.last_expand_truncated = False
        self.last_expand_count = 0
        self.opened: list[tuple[list[str], bool, bool]] = []
        self.subs: list[str] = []

    def expand(self, p: str) -> list[str]:
        return MpvPlayer.expand(self, p)

    def open_files(self, files, append: bool = False, remember: bool = True) -> dict:
        files = list(files)
        self.opened.append((files, bool(append), bool(remember)))
        return {"count": len(files), "plays": self._plays, "head": files[0] if files else ""}

    def prop(self, name: str, default=None):
        if name == "path":
            return self._path
        if name == "media-title":
            return os.path.basename(self._path) if self._path else ""
        if name == "pause":
            return False
        return default

    def add_subtitle(self, p: str) -> None:
        self.subs.append(p)


def make_win(player: FakePlayer):
    w = mw.MainWindow.__new__(mw.MainWindow)
    w.player = player
    w._pending_subs = None
    w.said: list[str] = []
    w.receipts: list[tuple] = []
    w.settles: list[tuple] = []
    w._osd = lambda s: w.said.append(str(s))
    # 内核已就绪（FakePlayer.running=True）：add_paths 走"直接下发"那一支。
    # 旧写法在这里 stub 掉 ensure_engine；异步启动之后判断改读 player.running，
    # 不再有这个方法可 stub——留着那行只会让这条腿在启动期静默走排队分支。
    w.empty = SimpleNamespace(set_notice=lambda _s: None, setVisible=lambda _v: None)
    w._arm_open_receipt = lambda *a: w.receipts.append(tuple(a))
    w._settle_open_receipt = lambda loaded=False, timed_out=False: w.settles.append(
        (bool(loaded), bool(timed_out)))
    w.video = SimpleNamespace(refit=lambda: None)
    w.preview = SimpleNamespace(hide_preview=lambda: None)
    w.title = SimpleNamespace(set_document=lambda _t: None)
    # `_on_file_loaded` 还要把刚播的这支片顶进媒体库的「最近播放」（R1 加的）。
    # 那一路有自己的验收；这条尺只关心字幕路由，给一个记账用的空壳。
    w.lib_panel = SimpleNamespace(note_played=lambda _p: None)
    w.setWindowTitle = lambda *_a: None
    return w


def run(paths: list[str], *, path: str = "", plays: bool = True, running: bool = True,
        replace: bool = True, loaded: bool = False) -> dict:
    """拖一次，必要时再递一条 file-loaded；把"内核收到了什么"原样带回来。"""
    p = FakePlayer(path=path, plays=plays, running=running)
    w = make_win(p)
    exc = ""
    orig_keep = mw.win32.keep_awake
    mw.win32.keep_awake = lambda *_a, **_k: None      # 这条链不验防睡眠，别碰系统状态
    try:
        mw.MainWindow.add_paths(w, paths, replace=replace)
        after_drop = {"pending": list(w._pending_subs[1]) if w._pending_subs else [],
                      "subs": list(p.subs),
                      "opened": list(p.opened), "said": list(w.said)}
        if loaded:
            # 内核推回来的 path 是**视频**那条；字幕先拖时 paths[0] 是字幕，
            # 拿它冒充会让"目标片对得上"的守卫（正确地）拒绝挂字幕。
            p._path = p._path or next(
                (x for x in paths if not mw.is_sub_file(x)), paths[0] if paths else "")
            mw.MainWindow._on_file_loaded(w)
    except BaseException as e:                 # 量具崩了也要把类型带回来，不许整趟没了
        exc = f"{type(e).__name__}: {e}"
        after_drop = {"pending": [], "subs": [], "opened": [], "said": []}
    finally:
        mw.win32.keep_awake = orig_keep
    return {"exc": exc, "after_drop": after_drop,
            "pending": list(w._pending_subs[1]) if w._pending_subs else [],
                      "subs": list(p.subs),
            "opened": list(p.opened), "said": list(w.said), "settles": list(w.settles)}


def evaluate() -> list[tuple[str, bool, str]]:
    """六条形状的判据。正常跑与证伪跑共用这一份，证伪只要求对应项翻红。"""
    out: list[tuple[str, bool, str]] = []

    # 1) 片子＋字幕一起拖：片源进 open_files，字幕挂起；file-loaded 之后才真挂
    r = run([VID, SRT], path="", plays=True, loaded=True)
    drop = r["after_drop"]
    files = drop["opened"][0][0] if drop["opened"] else []
    ok = (not r["exc"] and len(drop["opened"]) == 1 and files == [VID]
          and drop["pending"] == [SRT] and drop["subs"] == []
          and r["pending"] == [] and r["subs"] == [SRT]
          and r["settles"] == [(True, False)])
    out.append(("视频＋字幕一起拖：字幕先挂起、file-loaded 后才挂上（片源只交出视频）", ok,
                f"拖入后 opened={files} pending={drop['pending']} subs={drop['subs']}；"
                f"file-loaded 后 pending={r['pending']} subs={r['subs']} 回执={r['settles']}"
                + (f" 异常={r['exc']}" if r["exc"] else "")))

    # 2) 往正在播的队尾追加：这条路上没有 file-loaded，字幕必须当场挂到当前这支
    r = run([VID2, SRT], path=VID, plays=False, replace=False)
    drop = r["after_drop"]
    opened = drop["opened"][0] if drop["opened"] else ([], None, None)
    ok = (not r["exc"] and opened[0] == [VID2] and opened[1] is True
          and drop["pending"] == [] and drop["subs"] == [SRT]
          and not any("没有找到" in s for s in drop["said"]))
    out.append(("往正在播的队尾追加：字幕当场挂当前片（不等永远不来的 file-loaded）", ok,
                f"open_files={opened} pending={drop['pending']} subs={drop['subs']}"
                + (f" 异常={r['exc']}" if r["exc"] else "")))

    # 3) 只拖字幕、有片在播：立刻挂，不许回"没有找到可播放的文件"
    r = run([SRT], path=VID, plays=True)
    drop = r["after_drop"]
    ok = (not r["exc"] and drop["opened"] == [] and drop["subs"] == [SRT]
          and drop["pending"] == [] and any("外挂字幕" in s for s in drop["said"]))
    out.append(("只拖字幕（片子正播）：立刻 sub-add，不经过 open_files", ok,
                f"opened={drop['opened']} subs={drop['subs']} said={drop['said']}"
                + (f" 异常={r['exc']}" if r["exc"] else "")))

    # 4) 只拖字幕、没有片子：明确说"先播放一支视频"，不许向内核发必失败的 sub-add
    r = run([SRT], path="", plays=True)
    drop = r["after_drop"]
    ok = (not r["exc"] and drop["opened"] == [] and drop["subs"] == []
          and any("先播放一支视频" in s for s in drop["said"]))
    out.append(("只拖字幕（没片）：明确提示且不向内核发 sub-add", ok,
                f"opened={drop['opened']} subs={drop['subs']} said={drop['said']}"
                + (f" 异常={r['exc']}" if r["exc"] else "")))

    # 5) 只拖视频：不许产生任何字幕动作
    r = run([VID], path="", plays=True, loaded=True)
    drop = r["after_drop"]
    opened_ok = bool(drop["opened"]) and drop["opened"][0][0] == [VID]
    ok = (not r["exc"] and opened_ok and drop["pending"] == []
          and r["pending"] == [] and r["subs"] == [])
    out.append(("只拖视频：不挂字幕、也不留挂起项", ok,
                f"opened={drop['opened']} pending={r['pending']} subs={r['subs']}"
                + (f" 异常={r['exc']}" if r["exc"] else "")))

    # 6) 顺序反过来（字幕在前）：路由按扩展名分，不按拖动顺序
    r = run([SRT, VID], path="", plays=True, loaded=True)
    drop = r["after_drop"]
    ok = (not r["exc"] and drop["opened"][0][0] == [VID]
          and drop["pending"] == [SRT] and drop["subs"] == []
          and r["subs"] == [SRT] and r["pending"] == [])
    out.append(("字幕先、视频后也能分开（路由按扩展名，不按拖动顺序）", ok,
                f"opened={drop['opened']} 拖入后 pending={drop['pending']}；"
                f"加载后 subs={r['subs']} pending={r['pending']}"
                + (f" 异常={r['exc']}" if r["exc"] else "")))
    return out


def report(t0: float) -> int:
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    fails = [n for n, ok, _d in RESULTS if not ok]
    print(f"\n---- 字幕拖入自检：{passed}/{len(RESULTS)} 通过"
          + (f"；失败：{fails}" if fails else "；无失败")
          + f"，用时 {time.time() - t0:.2f}s ----", flush=True)
    return 0 if passed == len(RESULTS) else 1


def falsify() -> int:
    """每种坏写法必须让至少一条判据翻红；同时断言替换真的生效（防 NOP）。"""
    t0 = time.time()
    orig_add = mw.MainWindow.add_paths
    orig_attach = mw.MainWindow._attach_subtitles

    def failing() -> list[str]:
        return [n for n, ok, _d in evaluate() if not ok]

    # M1：旧路线——所有路径都交给 expand()，字幕跟着一起被丢掉
    def old_add(self, paths, replace=True):
        files: list[str] = []
        for p in paths:
            files += self.player.expand(p)
        if not files:
            self._osd("没有找到可播放的文件")
            return
        if not self.ensure_engine():
            return
        self.empty.set_notice("")
        self.empty.setVisible(False)
        res = self.player.open_files(files, append=not replace, remember=True) or {}
        self._arm_open_receipt(int(res.get("count", 0)), bool(res.get("plays")),
                               str(res.get("head", "")))

    mw.MainWindow.add_paths = old_add
    bad = failing()
    f_case("旧路线（字幕也交给 expand→回空→『没有找到可播放的文件』）必须被抓到",
           mw.MainWindow.add_paths is not orig_add and len(bad) >= 1, f"翻红={bad}")

    # M2：一起拖时立刻挂（file-loaded 之前），字幕会被内核静默丢掉
    def early_add(self, paths, replace=True):
        subs = [p for p in paths if mw.is_sub_file(p)]
        rest = [p for p in paths if not mw.is_sub_file(p)]
        files: list[str] = []
        for p in rest:
            files += self.player.expand(p)
        if not files:
            if subs:
                self._attach_subtitles(subs)
                return
            self._osd("没有找到可播放的文件")
            return
        self.empty.set_notice("")
        self.empty.setVisible(False)
        res = self.player.open_files(files, append=not replace, remember=True) or {}
        self._pending_subs = []
        if int(res.get("count", 0)) > 0 and subs:
            self._attach_subtitles(subs)

    mw.MainWindow.add_paths = early_add
    bad = failing()
    f_case("一起拖时立刻 sub-add（内核还没 file-loaded）必须被抓到",
           mw.MainWindow.add_paths is not orig_add and "视频＋字幕一起拖" in "".join(bad),
           f"翻红={bad}")
    mw.MainWindow.add_paths = orig_add

    # M3：`_attach_subtitles` 不查"有没有当前片"——没片时也发，用户只看到没反应
    def blind_attach(self, paths):
        paths = [p for p in (paths or []) if p]
        for p in paths:
            self.player.add_subtitle(p)
        if paths:
            self._osd(f"外挂字幕：{os.path.basename(paths[-1])}")

    mw.MainWindow._attach_subtitles = blind_attach
    bad = failing()
    f_case("没片也发 sub-add（去掉当前片守卫）必须被抓到",
           mw.MainWindow._attach_subtitles is not orig_attach
           and "只拖字幕（没片）" in "".join(bad), f"翻红={bad}")
    mw.MainWindow._attach_subtitles = orig_attach

    # M4：往正在播的队尾追加也挂起——那条路永远等不到 file-loaded，字幕彻底丢
    def always_queue(self, paths, replace=True):
        subs = [p for p in paths if mw.is_sub_file(p)]
        rest = [p for p in paths if not mw.is_sub_file(p)]
        files: list[str] = []
        for p in rest:
            files += self.player.expand(p)
        if not files:
            if subs:
                self._attach_subtitles(subs)
                return
            self._osd("没有找到可播放的文件")
            return
        self.empty.set_notice("")
        self.empty.setVisible(False)
        res = self.player.open_files(files, append=not replace, remember=True) or {}
        self._pending_subs = (str(res.get("head", "")), list(subs))
        self._arm_open_receipt(int(res.get("count", 0)), bool(res.get("plays")),
                               str(res.get("head", "")))

    mw.MainWindow.add_paths = always_queue
    bad = failing()
    f_case("追加也挂起（等于等一条不会来的 file-loaded）必须被抓到",
           mw.MainWindow.add_paths is not orig_add and "往正在播的队尾追加" in "".join(bad),
           f"翻红={bad}")
    mw.MainWindow.add_paths = orig_add

    caught = sum(1 for _n, ok, _d in FALSIFIED if ok)
    missed = [n for n, ok, _d in FALSIFIED if not ok]
    print(f"\n---- 字幕拖入证伪：{caught}/{len(FALSIFIED)} 抓到"
          + (f"；漏网：{missed}" if missed else "；无漏网")
          + f"，用时 {time.time() - t0:.2f}s ----", flush=True)
    return 0 if not missed else 1


if __name__ == "__main__":
    if "--falsify" in sys.argv:
        sys.exit(falsify())
    # 正常跑：样片检查在前，再逐条判
    if not (os.path.exists(VID) and os.path.exists(VID2) and os.path.exists(SRT)):
        print("[ENV] 样片缺失，这条链没有依据", flush=True)
        sys.exit(2)
    _t0 = time.time()
    for _n, _ok, _d in evaluate():
        check(_n, _ok, _d)
    sys.exit(report(_t0))
