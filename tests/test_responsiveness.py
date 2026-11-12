# -*- coding: utf-8 -*-
"""界面响应性验收：**数次数，不掐表**。不起内核、不起 ffmpeg、不抓屏、offscreen 平台。

用法：
    python tests/test_responsiveness.py            # 正常验收：全部判据必须 PASS
    python tests/test_responsiveness.py --falsify  # 证伪：把实现换成「常见错误写法」，对应判据必须翻红

为什么这一份值得单开：卡顿不像错字，它没有"报错"那一步。这一层最坏的形状是
"看着都对，只是点下去没反应半秒"——它不会让任何一条像素判据翻红（画面确实画出来了），
也不会崩。所以这里量的不是耗时，而是**同一件事被做了几遍**：一次击键把整库重扫几遍、
往里拖一个文件夹把列表清造几遍、一张新缓存回来把全目录扫几遍。这些数不随机器快慢、
杀软、磁盘冷热变——耗时不进契约（同一份改动在别的机器上能差三倍），次数进。

四处纪律：
1. **每条修复配一条"拆回原样"的证伪**，而且**判据被复用**：正常跑用 `judge_x(m)` 判真代码，
   证伪跑用**同一个** `judge_x(m)` 判被改坏的代码，要求它返回 False。少了这条，绿灯只证明
   我写了一段能跑的代码，不证明它测的是那件事。
2. **改造前的形状也量出来**（detail 里"拆回原样时=…"）：那些数不是抄的，是在本机、这份代码上
   把改动撤掉之后跑出来的对照读数——不然"从 8 降到 1"这种话只有"1"这一半有证据。
   注意它证明的是**判据有牙**，不是耗时省了几秒：耗时这条本机不承诺。
3. **对偶必须成对**：回收那两条既要求"别每张扫一遍"，也要求"攒够一批必须真扫一次"——
   只写前一条的话，把回收整个删掉也能骗到绿。
4. **环境不满足单独报码**：offscreen 起不来、JPEG 插件写不出样图（判据要真解码）、
   量具依赖的属性/信号在产品里改名了，都退 2 并点名是哪条前提立不住；
   场景里真跑挂了（AttributeError 之类）记**产品红**退 1，不许顺手降级成 ENV。
"""
from __future__ import annotations

import ast
import json
import math
import os
import shutil
import sys
import tempfile
import textwrap
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="gelu-resp-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")     # 必须在 import config 之前
os.environ["QT_QPA_PLATFORM"] = "offscreen"             # 不在用户桌面长窗口，也不依赖屏幕醒着
sys.path.insert(0, ROOT)

from PyQt5.QtCore import Qt, QPoint                  # noqa: E402
from PyQt5.QtGui import QColor, QIcon, QImage          # noqa: E402
from PyQt5.QtWidgets import QApplication              # noqa: E402

from app import config as cfgmod                       # noqa: E402
from app.engine.library import Library                 # noqa: E402
from app.engine.player import MpvPlayer, is_media_file  # noqa: E402
from app.engine.posters import PosterStore             # noqa: E402
from app.engine.thumbs import ThumbStore               # noqa: E402
from app.ui import icons                               # noqa: E402
from app.ui import playlist_panel as PLP               # noqa: E402
from app.ui import library_panel as LP                                      # noqa: E402
from app.ui.library_panel import FILL_BATCH, SEARCH_DEBOUNCE_MS, LibraryPanel   # noqa: E402
from app.ui.playlist_panel import COALESCE_MS, PlaylistPanel                   # noqa: E402
from app.ui.video_surface import VideoSurface          # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])

N_LIB = 240          # 合成媒体库条目数：判据是"每条目做几遍"，240 足够把它拉开
N_PUSH = 200         # 合成 playlist 推送数＝"往里拖一个 200 个文件的文件夹"
N_DONE = 120         # 合成"缓存成功回来"的次数

results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []

# 量具赖以为生的接口名：缺任何一个＝这份量具对不上产品（不是播放器坏），单独退 2
INTERFACE = {
    "app.config.Config": ("watch_state", "watch_snapshot", "ws_reads",
                          "_load_watch_state", "_stamp", "remember_position"),
    "app.ui.library_panel.LibraryPanel": ("rebuilds", "keystrokes", "sync_decodes",
                                          "fill_batches", "placeholders_made",
                                          "poster_scans", "card_w", "card_cols",
                                          "card_layouts", "_fill_rows", "_fill_batch",
                                          "_apply_card_layout", "_on_typed", "_on_poster"),
    "app.ui.playlist_panel.PlaylistPanel": ("pushes", "rebuilds", "_rebuild_now", "rebuild"),
    "app.engine.posters.PosterStore": ("prunes", "_note_done", "PRUNE_EVERY",
                                       "FAILED_CAP", "failed_evictions", "_failed",
                                       "_note_failed"),
    "app.engine.thumbs.ThumbStore": ("prunes", "_note_done", "PRUNE_EVERY",
                                     "FAILED_CAP", "failed_evictions", "_failed",
                                     "_note_failed"),
    "app.ui.video_surface.VideoSurface": ("poll_ticks", "poll_fires", "fit_runs",
                                          "fit_skips", "refit", "sync_poll"),
    "app.engine.player.MpvPlayer": ("MAX_EXPAND", "expand_calls", "last_expand_truncated",
                                    "last_expand_count", "expand"),
}

# 模块级的东西（不是实例属性）：图标缓存没有"一份实例"可建，读的是模块自己的账。
MODULES = {
    "app.ui.icons": ("icon", "cache_stats", "ICON_CACHE_MAX", "_ICON_CACHE"),
}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def pump(seconds: float, pred=None) -> bool:
    """跑事件循环，最多 `seconds`；`pred()` 真了就提前收。

    `seconds` 是**上限保护**（别让量具挂死），不是判据：判据只看 pred 有没有成立和那些计数器。
    到了上限还没排空会如实记成产品失败——补图队列排不空就是真卡顿，不是量具的锅。
    """
    t0 = time.time()
    while time.time() - t0 < seconds:
        APP.processEvents()
        if pred is not None and pred():
            return True
        time.sleep(0.004)
    APP.processEvents()
    return bool(pred()) if pred is not None else True


# ---------- 造样 ----------

def jpg(path: str, color: tuple[int, int, int]) -> bool:
    img = QImage(320, 180, QImage.Format_RGB32)
    img.fill(QColor(*color))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return bool(img.save(path, "JPG", 85))


class Fixture:
    """一库合成条目：文件真实存在、封面真实在盘上，所以走的完全是产品那条真路径
    （`posters.request()` 不降级、`posters.image()` 真解码）。"""

    _seeded = False

    def __init__(self) -> None:
        self.lib_dir = os.path.join(TMP, "lib")
        self.poster_dir = os.path.join(cfgmod.CACHE_DIR, "posters")
        os.makedirs(self.lib_dir, exist_ok=True)
        os.makedirs(self.poster_dir, exist_ok=True)
        self.items: list[dict] = []
        self.need_jpg = True
        for i in range(N_LIB):
            ext = ".mkv" if i % 3 == 0 else ".mp4"
            name = f"clip-{i:04d}{ext}"
            p = os.path.join(self.lib_dir, name)
            with open(p, "wb") as fh:
                fh.write(b"exists-but-not-a-real-video")
            self.items.append({"name": name, "path": p, "ext": ext,
                               "dur": 100.0 + i, "size": 1024 * (i + 1),
                               "mtime": float(1000000 + i)})
        if not Fixture._seeded:
            self.seed_posters()                        # jpg 的名字只由 (绝对路径,大小,mtime) 决定
            Fixture._seeded = True
        else:
            self.need_jpg = all(os.path.exists(self.poster_dir) for _ in [0])

    def seed_posters(self) -> None:
        """给每个条目在盘上落一张真封面：这样 image() 会真解码（量到的才是那笔账）。"""
        probe = PosterStore(cfgmod.FFMPEG_EXE, self.poster_dir, max_jobs=1)
        for k, e in enumerate(self.items):
            if not jpg(os.path.join(self.poster_dir, probe.key_for(e["path"]) + ".jpg"),
                       (30 + k % 200, 60, 90)):
                self.need_jpg = False

    def panel(self, cfg) -> tuple[LibraryPanel, PosterStore, cfgmod.Config]:
        store = PosterStore(cfgmod.FFMPEG_EXE, self.poster_dir, max_jobs=1)
        lib = Library(cfgmod.FFMPEG_EXE, os.path.join(TMP, "library.json"), [self.lib_dir])
        return LibraryPanel(lib, store, cfg), store, cfg


def mk_panel(fx: Fixture) -> tuple[LibraryPanel, PosterStore, cfgmod.Config]:
    cfg = cfgmod.Config()
    return fx.panel(cfg)


def probe_interface() -> list[str]:
    """判据的前提检查：这些名字没了，"数次数"就是在数一个不存在的东西。

    看的是**实例**上的属性（计数器都是 `__init__` 里挂的），不是类上的——
    类上有、实例上没有（改了初始化顺序）同样是前提立不住。
    """
    missing: list[str] = []
    for cls, attrs in INTERFACE.items():
        try:
            inst = _scratch_instance(cls)
        except Exception as exc:
            missing.append(f"{cls}（建不出实例来看这些属性：{type(exc).__name__}: {exc}）")
            continue
        for a in attrs:
            if not hasattr(inst, a):
                missing.append(f"{cls}.{a}")
    from importlib import import_module
    for mod, attrs in MODULES.items():
        try:
            m = import_module(mod)
        except Exception as exc:
            missing.append(f"{mod}（导入失败：{type(exc).__name__}: {exc}）")
            continue
        for a in attrs:
            if not hasattr(m, a):
                missing.append(f"{mod}.{a}")
    return missing


def _scratch_instance(cls: str):
    if cls.endswith("Config"):
        return cfgmod.Config()
    if cls.endswith("LibraryPanel"):
        store = PosterStore(cfgmod.FFMPEG_EXE, os.path.join(TMP, "scratch-posters"), max_jobs=1)
        lib = Library(cfgmod.FFMPEG_EXE, os.path.join(TMP, "scratch.json"), [])
        return LibraryPanel(lib, store, cfgmod.Config())
    if cls.endswith("PlaylistPanel"):
        return PlaylistPanel(MpvPlayer(cfgmod.Config()))
    if cls.endswith("PosterStore"):
        return PosterStore(cfgmod.FFMPEG_EXE, os.path.join(TMP, "scratch-posters2"), max_jobs=1)
    if cls.endswith("VideoSurface"):
        return VideoSurface(poll_ms=1000)          # 不 show：构造期本来就不该起表
    if cls.endswith("MpvPlayer"):
        return MpvPlayer(cfgmod.Config())
    return ThumbStore(cfgmod.FFMPEG_EXE, os.path.join(TMP, "scratch-thumbs"), max_jobs=1)


# ---------- 场景（正常跑与证伪跑共用同一份场景＋同一份 judge）----------

def scen_watch_read(cfg: cfgmod.Config, fx: Fixture) -> dict:
    """问 N 次"看到哪儿了"要读几遍盘：先把表写成封顶规模（240 条），再逐条问。"""
    wp = cfg.watch_state_path
    os.makedirs(os.path.dirname(wp), exist_ok=True)
    table = {e["path"]: {"pos": 30.0, "dur": 100.0, "ts": 1.0, "done": False} for e in fx.items}
    with open(wp, "w", encoding="utf-8") as fh:
        json.dump(table, fh, ensure_ascii=False)
    cfg._ws, cfg._ws_stamp = None, None          # 冷启动：缓存里没有，第一问必须真读盘
    r0 = cfg.ws_reads
    hits = sum(1 for e in fx.items if cfg.watch_state(e["path"]).get("pos") == 30.0)
    return {"env": "", "asks": len(fx.items), "reads": cfg.ws_reads - r0, "hits": hits}


def judge_watch_read(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = m["reads"] == 1 and m["hits"] == m["asks"]
    return ok, (f"问 {m['asks']} 次只读盘 {m['reads']} 次（要 1 次），读到进度 {m['hits']}/{m['asks']} 条；"
                f"拆回原样（每问一次重读整份表）时读盘={m['asks']} 次")


def scen_watch_external(cfg: cfgmod.Config, fx: Fixture) -> dict:
    """外部程序改了那份文件：记号必须认出"变过了"，并且要读到**新值**。"""
    wp = cfg.watch_state_path
    path = fx.items[0]["path"]
    with open(wp, "w", encoding="utf-8") as fh:
        json.dump({path: {"pos": 11.0, "dur": 100.0, "ts": 2.0, "done": False}}, fh)
    cfg.watch_state(path)                         # 先把这一份缓存下来
    r0 = cfg.ws_reads
    time.sleep(0.05)                              # 让改写落在下一个时钟刻度里
    with open(wp, "w", encoding="utf-8") as fh:
        json.dump({path: {"pos": 77.0, "dur": 100.0, "ts": 3.0, "done": False}}, fh)
    seen = float(cfg.watch_state(path).get("pos") or 0.0)
    return {"env": "", "reads": cfg.ws_reads - r0, "seen": seen}


def judge_watch_external(m: dict) -> tuple[bool, str]:
    ok = m["reads"] == 1 and abs(m["seen"] - 77.0) < 1e-6
    return ok, (f"外部改写后又读了 {m['reads']} 次（要 1 次），读到进度 {m['seen']}（要 77.0）；"
                f"记号要是假的，这里会停在 11.0——界面包着一张旧数还不知道")


def scen_watch_deleted(cfg: cfgmod.Config, fx: Fixture) -> dict:
    """那份文件被人删了：不许拿旧缓存继续报"看到 40%"。"""
    wp = cfg.watch_state_path
    path = fx.items[1]["path"]
    with open(wp, "w", encoding="utf-8") as fh:
        json.dump({path: {"pos": 40.0, "dur": 100.0, "ts": 4.0, "done": False}}, fh)
    cfg.watch_state(path)
    r0 = cfg.ws_reads
    os.remove(wp)
    seen = float(cfg.watch_state(path).get("pos") or 0.0)
    return {"env": "", "reads": cfg.ws_reads - r0, "seen": seen}


def judge_watch_deleted(m: dict) -> tuple[bool, str]:
    ok = m["seen"] == 0.0 and m["reads"] >= 1
    return ok, f"删表后再问：读到 {m['seen']}（要 0.0），期间真读盘 {m['reads']} 次（要 ≥1）"


def scen_watch_writeback(cfg: cfgmod.Config, fx: Fixture) -> dict:
    """自己写的（记住进度）必须立刻可见，而且不该为此再读一遍盘。"""
    path = fx.items[2]["path"]
    cfg._ws, cfg._ws_stamp = None, None
    cfg.watch_state(path)                         # 先来一次，把"表里没有这条"缓存下来
    r0 = cfg.ws_reads
    cfg.remember_position(path, 60.0, 100.0)
    seen = float(cfg.watch_state(path).get("pos") or 0.0)
    with open(cfg.watch_state_path, encoding="utf-8") as fh:
        disk = float(json.load(fh).get(path, {}).get("pos") or 0.0)
    return {"env": "", "reads": cfg.ws_reads - r0, "seen": seen, "disk": disk}


def judge_watch_writeback(m: dict) -> tuple[bool, str]:
    ok = abs(m["seen"] - 60.0) < 1e-6 and abs(m["disk"] - 60.0) < 1e-6 and m["reads"] == 0
    return ok, (f"记住进度后立刻读到 {m['seen']}，盘上也是 {m['disk']}，"
                f"为此多读盘 {m['reads']} 次（要 0 次——写穿，不靠重读那一遍）")


def scen_watch_stat(fx: Fixture) -> dict:
    """整表重建时，为了"外部有没有改过 watch-state"要 stat 几次。

    这里数的是 `Config._stamp`（每次 stat 都走它），不是 `ws_reads`：缓存命中也不读盘，
    但每问一次仍然 stat 一次。240 张卡逐卡问＝240 次 stat，这正是要修掉的那笔。
    """
    panel, store, cfg = mk_panel(fx)
    cfg._ws, cfg._ws_stamp = None, None          # 冷启动：第一问必须真 stat + 真读
    orig = cfgmod.Config.__dict__["_stamp"]
    calls = [0]

    def counting(p):
        calls[0] += 1
        return orig(p)

    cfgmod.Config._stamp = staticmethod(counting)
    try:
        panel.set_items(fx.items)
    finally:
        cfgmod.Config._stamp = orig
    out = {"env": "", "cards": panel.grid.count(), "want": len(fx.items),
           "stamps": calls[0]}
    panel.deleteLater()
    store.wait(300)
    return out


def judge_watch_stat(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = m["cards"] == m["want"] and m["stamps"] == 1
    return ok, (f"重建 {m['want']} 张卡片共 stat {m['stamps']} 次（要 1 次），画出行 {m['cards']}；"
                f"拆回逐卡问 watch_state 时 stat={m['want']} 次")


def scen_library_fill(fx: Fixture) -> dict:
    """整表重建这一趟里同步解码几张封面；以及补完之后有没有漏。"""
    panel, store, _cfg = mk_panel(fx)
    if not fx.need_jpg:
        return {"env": "写不出 jpg 样图（JPEG 插件没装？）——判据要真解码，这轮不算产品结论"}
    panel.set_items(fx.items)
    built = {"rebuilds": panel.rebuilds, "sync": panel.sync_decodes, "ph": panel.placeholders_made}
    drained = pump(25.0, lambda: not panel._fill_rows)
    keys = [str(panel.grid.item(i).data(Qt.UserRole + 1) or "") for i in range(panel.grid.count())]
    out = {"env": "", "rows": panel.grid.count(), "want": len(fx.items),
           "rebuilds": built["rebuilds"], "sync": built["sync"],
           "batches": panel.fill_batches, "ph": built["ph"],
           "no_key": sum(1 for k in keys if not k), "drained": drained,
           "with_icon": sum(1 for i in range(panel.grid.count())
                            if not panel.grid.item(i).icon().isNull())}
    store.wait(500)
    panel.deleteLater()
    return out


def judge_library_fill(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    need = math.ceil(m["want"] / FILL_BATCH)
    ok = (m["sync"] <= FILL_BATCH and m["batches"] >= need and m["no_key"] == 0
          and m["drained"] and m["rows"] == m["want"] and m["rebuilds"] == 1
          and m["with_icon"] == m["want"])
    return ok, (f"{m['want']} 项重建 {m['rebuilds']} 趟：同步解码 {m['sync']} 张（封顶 {FILL_BATCH}）、"
                f"分 {m['batches']} 片补完（要 ≥{need}）、图标齐 {m['with_icon']}/{m['rows']}、"
                f"缺 key {m['no_key']} 个、排空={m['drained']}、占位块只画 {m['ph']} 种；"
                f"拆回原样（重建那趟就地解码整库）时同步解码={m['want']} 张")


def scen_card_grid(fx: Fixture) -> dict:
    """侧栏 300 → 520：卡片要换列、单格变窄，而不是永远 192px 一列。"""
    panel, store, _cfg = mk_panel(fx)
    panel.resize(300, 700)
    panel.show()                              # offscreen：不会弹到桌面上
    # 只放 8 张：这个场景要的是"滚动条在换列后消失"那一瞬——300 档一列 8 张要竖滚，
    # 520 档三列 3 行不用滚，视口宽会因此在两次排版之间变。240 张时滚动条两档都在，
    # 视口宽不变，就量不出"有没有跟着视口走"。
    panel.set_items(fx.items[:8])
    pump(0.8, lambda: panel.grid.isVisible())
    pump(1.0, lambda: not panel._layout_timer.isActive())
    rows = []
    for w in (300, 520):
        panel.resize(w, 700)
        pump(1.0, lambda: abs(panel.width() - w) <= 2)
        pump(1.5, lambda: not panel._layout_timer.isActive())   # 等 60ms 的排布窗落地
        pump(0.4)          # 滚动条来去会再改一次视口宽：让第二趟排版也落定再量
        vw = panel.grid.viewport().width()
        cell = int(panel.grid.gridSize().width())
        cols = int(panel.card_cols)
        rows.append({"panel": panel.width(), "vw": vw, "cell": cell,
                     "cols": cols, "icon": panel.grid.iconSize().height(),
                     "grid_h": int(panel.grid.gridSize().height()),
                     "overflow": cell * cols + LP.CARD_GAP * (cols - 1) > vw + 1,
                     "hscroll": panel.grid.horizontalScrollBar().isVisible()})
    out = {"env": "", "rows": rows, "layouts": panel.card_layouts}
    panel.close()
    panel.deleteLater()
    store.wait(200)
    return out


def judge_card_grid(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    a, b = m["rows"]
    # 判据的参照不是手抄的固定数：按**当前**视口宽再算一遍，看格子有没有跟着视口走。
    # 滚动条来去会让视口变宽而面板宽度不变——不认识视口宽变化的实现会停在窄口径，
    # 格子与"现视口应得"对不上时这条会红（比"520 一定等于 160"更能说明问题）。
    exp_a = LP.card_metrics(a["vw"])[0]
    exp_b = LP.card_metrics(b["vw"])[0]
    ok = (a["panel"] < b["panel"] and a["cols"] == 1 and b["cols"] >= 2
          and a["cell"] >= 220 and b["cell"] < a["cell"]
          and a["cell"] == exp_a and b["cell"] == exp_b
          and not a["overflow"] and not b["overflow"]
          and not a["hscroll"] and not b["hscroll"]
          and a["grid_h"] > a["icon"] and b["grid_h"] > b["icon"])
    return ok, (f"300 宽：格 {a['cell']}×{a['grid_h']}／{a['cols']} 列；"
                f"520 宽：格 {b['cell']}×{b['grid_h']}／{b['cols']} 列；"
                f"现视口应得={exp_a}/{exp_b}；"
                f"越界={a['overflow']}/{b['overflow']} 横滚={a['hscroll']}/{b['hscroll']}；"
                f"固定 192 时两档都是 1 列 192（窄档空 86px、宽档空 120px）")


def scen_library_empty(fx: Fixture) -> dict:
    """库是空的 / 搜索没命中 / 清空搜索回来：三态各说各的话，不共用一句空白。"""
    panel, store, _cfg = mk_panel(fx)
    panel.resize(300, 640)
    panel.show()
    pump(0.5, lambda: not panel._layout_timer.isActive())

    def snap() -> dict:
        return {"title": panel.empty_title.text(), "hint": panel.empty_hint.text(),
                "btn": panel.empty_add.isVisible(), "grid": panel.grid.isVisible(),
                "empty": panel.empty_page.isVisible(), "foot": panel.foot_hint.isVisible()}

    panel.set_items([])
    pump(0.6, lambda: panel.empty_page.isVisible())
    a = snap()
    panel.set_items(fx.items)
    panel.search.setText("zzz-no-such-name")
    pump(2.0, lambda: panel.empty_page.isVisible() and not panel.grid.isVisible())
    b = snap()
    panel.search.setText("")
    pump(2.0, lambda: panel.grid.isVisible() and not panel.empty_page.isVisible())
    c = snap()
    out = {"env": "", "a": a, "b": b, "c": c}
    store.wait(300)
    panel.close()
    panel.deleteLater()
    return out


def judge_library_empty(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    a, b, c = m["a"], m["b"], m["c"]
    ok = (not a["grid"] and a["empty"] and a["btn"] and not a["foot"]
          and "空" in a["title"] and "添加" in a["hint"] and "拖" in a["hint"]
          and not b["grid"] and b["empty"] and not b["btn"] and not b["foot"]
          and "没有找到" in b["title"] and "zzz-no-such-name" in b["hint"]
          and c["grid"] and not c["empty"] and c["foot"])
    return ok, (f"库空：格={a['grid']} 空页={a['empty']} 按钮={a['btn']} 底注={a['foot']} "
                f"题='{a['title']}' 提示='{a['hint']}'；"
                f"搜空：格={b['grid']} 空页={b['empty']} 按钮={b['btn']} 题='{b['title']}'；"
                f"清词：格={c['grid']} 空页={c['empty']} 底注={c['foot']}；"
                f"三态共用一句/直接留一块空黑都会在这里翻红")


def _type_words(n: int = 8) -> list[str]:
    return [f"clip-000{k}" for k in range(n)]          # 每敲一下都是一个新词


def scen_typing(fx: Fixture, panel=None, store=None) -> dict:
    """打字途中重建几遍：敲 8 下、停手，看重建次数与最后画出来的是不是那一个词。"""
    if panel is None:
        panel, store, _cfg = mk_panel(fx)
    if not fx.need_jpg:
        return {"env": "写不出 jpg 样图（JPEG 插件没装？）"}
    panel.set_items(fx.items)
    pump(25.0, lambda: not panel._fill_rows)
    before = panel.rebuilds
    words = _type_words()
    for w in words:
        panel.search.setText(w)
    during = panel.rebuilds - before
    fired = pump(1.0 + SEARCH_DEBOUNCE_MS / 1000.0 * 3, lambda: panel.rebuilds > before)
    word = words[-1]
    want = [e for e in fx.items if word in str(e["name"]).lower()]
    out = {"env": "", "chars": len(words), "keystrokes": panel.keystrokes,
           "during": during, "fired": fired, "after": panel.rebuilds - before,
           "shown": len(panel._shown), "rows": panel.grid.count(), "want": len(want)}
    store.wait(500)
    panel.deleteLater()
    return out


def judge_typing(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (m["keystrokes"] == m["chars"] and m["during"] == 0 and m["fired"]
          and m["after"] == 1 and m["shown"] == m["want"] and m["rows"] == m["want"])
    return ok, (f"{m['chars']} 次击键：途中重建 {m['during']} 次（要 0）、停手后 {m['after']} 次、"
                f"最后一词真画出 {m['rows']} 项（要 {m['want']}）；"
                f"拆回原样（每键直连整表重建）时途中重建={m['chars']} 次")


def scen_immediate(fx: Fixture, panel=None, store=None) -> dict:
    """非击键那几路（扫描回包、排序下拉）必须**不等**防抖——防抖挂错地方就红在这。"""
    if panel is None:
        panel, store, _cfg = mk_panel(fx)
    if not fx.need_jpg:
        return {"env": "写不出 jpg 样图（JPEG 插件没装？）"}
    panel.set_items(fx.items[:20])
    r1 = panel.rebuilds
    panel.sort.setCurrentIndex(2)                # 换成"时长"：不是击键，该当场重排
    r2 = panel.rebuilds                          # 一次事件循环都没多跑
    out = {"env": "", "set": r1, "sort": r2 - r1}
    store.wait(500)
    panel.deleteLater()
    return out


def judge_immediate(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = m["set"] == 1 and m["sort"] == 1
    return ok, (f"扫描回包那一路重建 {m['set']} 次、排序那一路当场 {m['sort']} 次"
                f"（都不许等 {SEARCH_DEBOUNCE_MS}ms 的防抖窗）；"
                f"把防抖挂到 _rebuild 出口上时两路都是 0")


def scen_midrebuild(fx: Fixture) -> dict:
    """补图补到一半表变短了（这里用搜索过滤）：卡片只能挂**这一张表**里的封面。

    为什么用过滤，而不是"换成一份短列表"（本来写的是这个）：那个场景对这条 bug 是**空操作**——
    6 项内部怎么排都还在那 6 项里，而判据看的是"有没有跑到表外"，所以注入变异后照样 0 张外来，
    绿灯是假的（首轮证伪就是这么漏网的）。过滤才造出这个 bug 的真形状：`_shown` 是 `_items` 的
    真子集（网格 10 行、整库 240 项），按整库行号取项的那一片会直接取到表外的片上。
    """
    panel, store, _cfg = mk_panel(fx)
    if not fx.need_jpg:
        return {"env": "写不出 jpg 样图（JPEG 插件没装？）"}
    panel.set_items(fx.items)
    pump(2.0, lambda: panel.fill_batches >= 2)           # 只补两三片就动表
    word = "clip-001"                                    # 命中 clip-0010..0019 这 10 项
    want_items = [e for e in fx.items if word in str(e["name"]).lower()]
    panel.search.setText(word)
    # 先等防抖那一窗真的落地（`_shown` 换成过滤后那份），再等补图排空。
    # 少了这一步会两件事一起坏：判据读到的是"还没过滤"的 240 行，而窗口里留着
    # 一个正在跑的补图队列就 deleteLater，后面的场景会在一个没收尾的界面态上继续跑。
    fired = pump(3.0, lambda: len(panel._shown) == len(want_items))
    drained = pump(20.0, lambda: not panel._fill_rows)
    want = {store.key_for(e["path"]) for e in want_items}
    got = {str(panel.grid.item(i).data(Qt.UserRole + 1) or "") for i in range(panel.grid.count())}
    out = {"env": "", "rows": panel.grid.count(), "want": len(want), "want_rows": len(want_items),
           "fired": fired, "drained": drained, "foreign": len(got - want),
           "missing": len(want - got), "batches": panel.fill_batches}
    store.wait(500)
    panel.deleteLater()
    return out


def judge_midrebuild(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (m["fired"] and m["rows"] == m["want"] and m["drained"]
          and m["foreign"] == 0 and m["missing"] == 0)
    return ok, (f"过滤落地={m['fired']}、{m['rows']} 行（要 {m['want_rows']}）、"
                f"补图排空={m['drained']}、这一张表之外的封面 {m['foreign']} 张（要 0）、"
                f"该有却没有 {m['missing']} 张（要 0）；"
                f"补图那一片要是按**整库**行号取项（过滤后的第 r 张≠整库第 r 张），"
                f"这里就会串到别的片上")


def scen_playlist(n: int = N_PUSH) -> dict:
    """往里拖一个 N 个文件的文件夹：内核推 N 次，界面真画几遍、画的是不是最后一份。"""
    player = MpvPlayer(cfgmod.Config())
    panel = PlaylistPanel(player)
    panel.show()
    for i in range(1, n + 1):
        pl = [{"filename": f"C:\\lib\\f{k}.mp4", "id": k, "current": k == 0} for k in range(i)]
        player.sig_playlist.emit(pl)
    during = panel.rebuilds
    settled = pump(1.5, lambda: panel.list.count() == n and not panel._coalesce.isActive())
    out = {"env": "", "pushes": panel.pushes, "during": during, "settled": settled,
           "rows": panel.list.count(), "want": n, "rebuilds": panel.rebuilds}
    panel.deleteLater()
    return out


def judge_playlist(m: dict) -> tuple[bool, str]:
    cap = math.ceil(6 * m["want"] / max(1, COALESCE_MS))       # 每 60ms 最多画一遍，6 趟为上限
    ok = (m["pushes"] == m["want"] and m["settled"] and m["rows"] == m["want"]
          and m["rebuilds"] <= cap)
    return ok, (f"{m['pushes']} 次推送：途中画了 {m['during']} 遍、总共 {m['rebuilds']} 遍（上限 {cap} 遍，"
                f"窗口 {COALESCE_MS}ms），最后 {m['rows']} 行对得上内核那份 {m['want']}；"
                f"拆回原样（推送直连重建）时重建={m['pushes']} 遍＝"
                f"{m['pushes'] * (m['pushes'] + 1) // 2} 次行造销（O(N²)）")


def scen_playlist_last(fx: Fixture) -> dict:
    """合并只该"少画几遍"，不该"停在中间态"：窗外最后那份必须真画上去。"""
    player = MpvPlayer(cfgmod.Config())
    panel = PlaylistPanel(player)
    panel.show()
    for i in range(1, 6):
        player.sig_playlist.emit([{"filename": f"C:\\a{k}.mp4", "id": k} for k in range(i)])
    pump(1.0, lambda: panel.list.count() == 5)
    rows = panel.list.count()
    panel.deleteLater()
    return {"env": "", "rows": rows, "want": 5}


def judge_playlist_last(m: dict) -> tuple[bool, str]:
    ok = m["rows"] == m["want"]
    return ok, f"连推 1..5 行之后画了 {m['rows']} 行（要 {m['want']}：合并要合到**最后一份**）"


def _menu_probe(panel: PlaylistPanel, pos, pick: str | None) -> dict:
    """用假菜单跑一次 `_context_menu`：记下每项可用状态与是否抛异常。

    `QMenu.exec` 是阻塞的，真弹会把 offscreen 这一轮卡死；但这里要验的又不是
    菜单画得好不好，而是**空白处右键给出的动作是不是真能点**、以及点下去
    到底发出哪个信号。所以只替换模块里的 `QMenu`，产品代码一个字不动。
    """
    box = {"exc": "", "actions": [], "menu": None}

    class _Act:
        def __init__(self, text):
            self.text = text
            self.enabled = True

        def setEnabled(self, v):
            self.enabled = bool(v)

    class _Menu:
        def __init__(self, _parent=None):
            self._acts = []
            box["menu"] = self

        def addAction(self, text):
            a = _Act(text)
            self._acts.append(a)
            return a

        def addSeparator(self):
            return None

        def exec(self, *_a):
            return next((a for a in self._acts if a.text == pick), None)

    orig_menu = PLP.QMenu
    PLP.QMenu = _Menu
    try:
        panel._context_menu(pos)
    except Exception as exc:              # 这里正是旧版崩的地方：row(None) → TypeError
        box["exc"] = f"{type(exc).__name__}: {exc}"
    finally:
        PLP.QMenu = orig_menu
    menu = box["menu"]
    box["actions"] = [(a.text, a.enabled) for a in (menu._acts if menu is not None else [])]
    return box


def _state(snap: dict, text: str):
    return dict(snap["actions"]).get(text)


def scen_context_menu() -> dict:
    """播放列表右键：空白处的行动作必须置灰（本机 row(None) 回 −1，不抛也不崩）、
    清空仍可用，点行/点清空要发对信号。"""
    player = MpvPlayer(cfgmod.Config())
    panel = PlaylistPanel(player)
    panel.resize(320, 420)
    panel.show()
    APP.processEvents()
    row_none = panel.list.row(None)       # 记在 detail 里：这是"不崩"这条判断的出处

    got_play: list[int] = []
    got_remove: list[int] = []
    got_clear: list[int] = []
    panel.sig_play_index.connect(got_play.append)
    panel.sig_remove_index.connect(got_remove.append)
    panel.sig_clear.connect(lambda: got_clear.append(1))

    # 1) 空列表：旧写法 `self.list.row(self.list.itemAt(pos))` 在这里收 None
    panel.rebuild([])
    panel._rebuild_now()
    APP.processEvents()
    empty = _menu_probe(panel, panel.list.viewport().rect().center(), None)

    # 2) 有行：第一行上右键选播放/移除；下半片空白处选清空
    panel.rebuild([{"filename": "C:\\a.mp4", "id": 0, "current": True},
                   {"filename": "C:\\b.mp4", "id": 1}])
    panel._rebuild_now()
    APP.processEvents()
    item = panel.list.item(0)
    row_pos = panel.list.visualItemRect(item).center()
    on_row = panel.list.itemAt(row_pos) is item
    play = _menu_probe(panel, row_pos, "播放") if on_row else {"exc": "没点中第一行", "actions": []}
    remove = _menu_probe(panel, row_pos, "从列表移除") if on_row else {"exc": "没点中第一行",
                                                                    "actions": []}
    blank = _menu_probe(panel, panel.list.viewport().rect().bottomLeft() + QPoint(8, -8),
                        "清空列表")
    panel.deleteLater()
    return {"env": "", "on_row": on_row, "empty": empty, "play": play, "remove": remove,
            "blank": blank, "got_play": got_play, "got_remove": got_remove,
            "got_clear": got_clear, "row_none": row_none}


def judge_context_menu(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    empty, blank, play, remove = m["empty"], m["blank"], m["play"], m["remove"]
    ok = (empty["exc"] == "" and _state(empty, "播放") is False
          and _state(empty, "从列表移除") is False and _state(empty, "清空列表") is True
          and blank["exc"] == "" and _state(blank, "播放") is False
          and _state(blank, "从列表移除") is False and _state(blank, "清空列表") is True
          and m["on_row"] and play["exc"] == "" and _state(play, "播放") is True
          and remove["exc"] == "" and _state(remove, "从列表移除") is True
          and m["got_play"] == [0] and m["got_remove"] == [0] and m["got_clear"] == [1])
    return ok, (f"本机 row(None)={m['row_none']}（所以旧写法的毛病不是崩，是发 −1）；"
                f"空列表右键：{empty['exc'] or '不抛'}，动作={empty['actions']}；"
                f"行上右键：点中={m['on_row']}，播放={play['actions']}、移除={remove['actions']}；"
                f"有行时空白处={blank['actions']}；发信号 "
                f"play={m['got_play']} remove={m['got_remove']} clear={m['got_clear']}")


def scen_prune(n: int, every: int) -> dict:
    """缓存回收按批：n 张成功回来，全目录扫描该做几次。"""
    store = PosterStore(cfgmod.FFMPEG_EXE, os.path.join(TMP, "prune-posters"), max_jobs=1)
    for i in range(n):
        store._on_done(f"key{i:04d}", True, "0.01s")
    return {"env": "", "done": n, "prunes": store.prunes, "every": every}


def judge_prune_posters(m: dict) -> tuple[bool, str]:
    want = m["done"] // m["every"]
    ok = m["prunes"] == want
    return ok, (f"{m['done']} 张封面成功：全目录回收 {m['prunes']} 次（要 {want}＝每 {m['every']} 张一次）；"
                f"拆回原样（每张回来扫一遍全目录）时={m['done']} 次")


def judge_prune_never(m: dict) -> tuple[bool, str]:
    ok = m["prunes"] >= 1
    return ok, (f"{m['done']} 张成功之后回收做了 {m['prunes']} 次（要 ≥1：按批不等于不做，"
                f"封顶 400 张是体积阀，攒着不收就一路涨到硬盘上）")


def scen_prune_thumbs(n: int) -> dict:
    store = ThumbStore(cfgmod.FFMPEG_EXE, os.path.join(TMP, "prune-thumbs"), max_jobs=1)
    for i in range(n):
        store._on_done(f"k{i:04d}", True, "0.01s")
    return {"env": "", "done": n, "prunes": store.prunes, "every": ThumbStore.PRUNE_EVERY}


def judge_prune_thumbs(m: dict) -> tuple[bool, str]:
    want = m["done"] // m["every"]
    ok = m["prunes"] == want
    return ok, (f"{m['done']} 份 sheet 成功：全目录回收 {m['prunes']} 次（要 {want}＝每 {m['every']} 份一次）；"
                f"挂在界面那头的旧写法是每份一次={m['done']} 次")


# ---------- B5：一张封面回来，摸到它那一行就该停 ----------

N_SCAN = 24


def scen_poster_scan(fx: Fixture) -> dict:
    """让第一行的封面"再回来一次"，看 `_on_poster` 摸了几张卡。

    第一行命中＝正圆最好：旧写法（不 break）每一张都要把 24 行摸完；新写法一步就停，
    读数 1 对 24，不靠计时也拉得开。判据同时要求图标真的贴上——"提前退出"不能靠
    "什么都不做"来骗绿。
    """
    panel, store, _cfg = mk_panel(fx)
    if not fx.need_jpg:
        return {"env": "写不出 jpg 样图（JPEG 插件没装？）"}
    panel.set_items(fx.items[:N_SCAN])
    drained = pump(20.0, lambda: not panel._fill_rows)
    it = panel.grid.item(0)
    key = str(it.data(Qt.UserRole + 1) or "") if it is not None else ""
    s0 = panel.poster_scans
    panel._on_poster(key, True, "")
    scans = panel.poster_scans - s0
    icon_ok = bool(it is not None and not it.icon().isNull())
    rows = panel.grid.count()
    store.wait(500)
    panel.deleteLater()
    return {"env": "", "rows": rows, "key": bool(key), "drained": drained,
            "scans": scans, "icon": icon_ok}


def judge_poster_scan(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = m["key"] and m["drained"] and m["scans"] == 1 and m["icon"]
    return ok, (f"{m['rows']} 行里第一行的封面回来：摸了 {m['scans']} 张卡（要 1 张，命中即停）、"
                f"图标真贴上={m['icon']}、补图排空={m['drained']}；"
                f"拆回原样（不 break，每张封面扫完整张表）时摸卡={m['rows']} 张")


# ---------- B6：同一几何不必反复铺 mpv 的子窗口 ----------

def scen_refit() -> dict:
    """几何没变时 refit 不许再走 Win32 铺窗；尺寸一变必须铺一次。

    offscreen 下容器里没有 mpv 的孩子窗口，`_fit_n` 会是 0，而"跳过"这一支要求
    `_fit_n > 0`——所以这里把 `win32.fit_child_windows` 换成"铺到了 1 个"的替身：
    量的是**产品在真机上一定走得到的那条分支**，不是给量具开的后门（真机接线由
    `tests/test_ui.py` 的窗口腿覆盖）。替身同时数调用次数，防止只靠自报计数器假绿。
    """
    from app import win32
    surf = VideoSurface(poll_ms=1000)
    surf.set_empty(False)
    surf.resize(640, 360)
    surf.show()
    APP.processEvents()
    orig_fit = win32.fit_child_windows
    calls = {"n": 0}

    def fake_fit(hwnd):                     # 模拟"容器里有一个 mpv 子窗口"
        calls["n"] += 1
        return 1

    win32.fit_child_windows = fake_fit
    try:
        surf.refit(force=True)              # 立一个几何基准
        r0, s0, c0 = surf.fit_runs, surf.fit_skips, calls["n"]
        for _ in range(5):
            surf.refit()                    # 同一几何：全部该跳过
        dup_runs, dup_skips = surf.fit_runs - r0, surf.fit_skips - s0
        dup_calls = calls["n"] - c0
        surf.resize(800, 450)               # 几何真变了：resizeEvent 要铺一次
        APP.processEvents()
        new_runs = surf.fit_runs - r0 - dup_runs
        e0 = surf.fit_runs
        surf.set_empty(True)                # 空状态：铺窗没有意义
        surf.refit()
        empty_runs = surf.fit_runs - e0
    finally:
        win32.fit_child_windows = orig_fit
        surf.hide()
        surf.deleteLater()
    return {"env": "", "dup_calls": dup_calls, "dup_runs": dup_runs, "dup_skips": dup_skips,
            "new_runs": new_runs, "empty_runs": empty_runs}


def judge_refit(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (m["dup_runs"] == 0 and m["dup_calls"] == 0 and m["dup_skips"] == 5
          and m["new_runs"] == 1 and m["empty_runs"] == 0)
    return ok, (f"同一几何 refit 5 次：真铺 {m['dup_runs']} 次（要 0）、跳过 {m['dup_skips']} 次（要 5）、"
                f"底层 Win32 调用 {m['dup_calls']} 次（要 0）；尺寸变一次铺 {m['new_runs']} 次（要 1）、"
                f"空状态再 refit 铺 {m['empty_runs']} 次（要 0）；"
                f"拆回原样（每次都铺）时这 5 次全是底层调用")


# ---------- B8：图标工厂的缓存（命中要快，且不许无界） ----------

def scen_icon_cache() -> dict:
    """同一枚图标要两遍＝画一遍；换尺寸＝另一枚；灌满之后缓存必须封顶。"""
    cap = int(icons.ICON_CACHE_MAX)
    s0 = icons.cache_stats()
    a = icons.icon("ab_loop", 23, "#123456")
    s1 = icons.cache_stats()
    b = icons.icon("ab_loop", 23, "#123456")
    s2 = icons.cache_stats()
    icons.icon("ab_loop", 22, "#123456")              # 尺寸不同＝另一个 key
    s3 = icons.cache_stats()
    z = None
    color = ""
    for i in range(cap + 40):                         # 灌爆：每一项都是不同的 key
        color = "#%06x" % ((i * 7919) % 0xFFFFFF)
        z = icons.icon("shuffle", 23, color)
    s4 = icons.cache_stats()
    z2 = icons.icon("shuffle", 23, color)
    s5 = icons.cache_stats()
    return {"env": "", "cap": cap,
            "same": a is b, "build1": s1["built"] - s0["built"], "hit1": s2["hits"] - s1["hits"],
            "build2": s3["built"] - s2["built"], "hit2": s3["hits"] - s2["hits"],
            "size": s4["size"], "newest": z2 is z, "hit_new": s5["hits"] - s4["hits"],
            "size_after": s5["size"], "flood": cap + 40}


def judge_icon_cache(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (m["cap"] >= 16 and m["same"] and m["build1"] == 1 and m["hit1"] >= 1
          and m["build2"] == 1 and m["hit2"] == 0
          and m["size"] == m["cap"] and m["newest"] and m["hit_new"] == 1
          and m["size_after"] == m["cap"])
    return ok, (f"同一参数第二次要＝命中 {m['hit1']} 次、只画 {m['build1']} 枚、"
                f"拿到同一个对象={m['same']}；换尺寸再画 {m['build2']} 枚（不串 key）；"
                f"灌 {m['flood']} 个不同 key 后缓存 {m['size']} 项（封顶 {m['cap']}），"
                f"最新那枚仍命中={m['hit_new']} 次；拆回原样（每要一次画一遍）时 build1=2、"
                f"无界缓存时 size={m['flood']}")


# ---------- B1：最小化时 30ms 轮询必须停 ----------

def scen_poll_gate() -> dict:
    """显示才起表、最小化就停表、恢复再起；三段的拍数要对得上状态。"""
    surf = VideoSurface(poll_ms=5)              # 出厂＝隐藏的空状态
    f_hidden = surf.poll_fires
    pump(0.12)                                  # 还没 show：构造期不许起表
    hidden_fires = surf.poll_fires - f_hidden
    surf.set_empty(False)                       # 这一句让容器可见（showEvent 起表）
    surf.resize(640, 360)
    surf.show()
    APP.processEvents()
    t0, f0 = surf.poll_ticks, surf.poll_fires
    pump(0.2)
    active_ticks, active_fires = surf.poll_ticks - t0, surf.poll_fires - f0
    surf.showMinimized()
    APP.processEvents()
    surf.sync_poll()                            # main_window.changeEvent 里就是这一句
    t1, f1 = surf.poll_ticks, surf.poll_fires
    pump(0.2)
    min_ticks, min_fires = surf.poll_ticks - t1, surf.poll_fires - f1
    min_active = bool(surf._poll.isActive())
    surf.showNormal()
    APP.processEvents()
    surf.sync_poll()
    f2 = surf.poll_fires
    pump(0.2)
    back_fires = surf.poll_fires - f2
    surf.hide()
    surf.deleteLater()
    return {"env": "", "hidden": hidden_fires, "ticks": active_ticks, "fires": active_fires,
            "min_ticks": min_ticks, "min_fires": min_fires, "min_active": min_active,
            "back": back_fires}


def judge_poll_gate(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (m["hidden"] == 0 and m["ticks"] > 0 and m["fires"] >= m["ticks"]
          and m["min_ticks"] == 0 and m["min_fires"] == 0 and m["min_active"] is False
          and m["back"] > 0)
    return ok, (f"藏起来时轮询 {m['hidden']} 拍（要 0）；显示后 {m['ticks']} 拍；"
                f"最小化后 {m['min_ticks']} 拍、定时器在跑={m['min_active']}（都要 0/False）；"
                f"恢复后 {m['back']} 拍（要 >0）；拆回原样（不看最小化）时最小化后仍每 5ms 问一次系统")


def scen_poll_wiring() -> dict:
    """`sync_poll` 必须真的接在 `changeEvent` 的 WindowStateChange 上。

    运行期那条判据证的是 `VideoSurface.sync_poll` 本身；少了这条接线，最小化/恢复
    永远没人叫它，产品形态就是"表一直跑"——两者缺一不可。
    """
    import inspect
    from app.ui.main_window import MainWindow
    return {"env": "", "src": inspect.getsource(MainWindow.changeEvent)}


def judge_poll_wiring(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    try:
        fn = ast.parse(textwrap.dedent(m["src"])).body[0]
    except SyntaxError as exc:
        return False, f"changeEvent 的源码读不成语法树：{exc}"
    parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    calls = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "sync_poll"):
            continue
        v = n.func.value
        via_video = isinstance(v, ast.Attribute) and v.attr == "video"
        under_fs = False
        p = parents.get(n)
        while p is not None and p is not fn:
            if isinstance(p, ast.If) and "_fs" in ast.dump(p.test):
                under_fs = True
            p = parents.get(p)
        calls.append((via_video, under_fs))
    has_ws = "WindowStateChange" in ast.dump(fn)
    good = [c for c in calls if c[0] and not c[1]]
    ok = has_ws and bool(good)
    return ok, (f"changeEvent 里识别窗口态={has_ws}，`self.video.sync_poll()` 在"
                f"{'非全屏限制' if good else '全屏限制之内或根本没接'}（调用点 {len(calls)} 个）；"
                f"接线要是丢了（或塞进 `if self._fs`），最小化时轮询没人叫停")


# ---------- B3：拖进来的目录展开要封顶 ----------

def scen_expand_cap() -> dict:
    """一次展开交回几个、有没有如实标记"截了"；没到顶的目录一个都不许少。"""
    import inspect
    root = os.path.join(TMP, "expand-cap")
    for i in range(12):
        sub = os.path.join(root, f"d{i % 3}")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, f"m{i:02d}.mp4"), "wb") as fh:
            fh.write(b"x")
    player = MpvPlayer(cfgmod.Config())
    many = player.expand(root, max_files=5)
    many_trunc = bool(player.last_expand_truncated)
    many_count = int(player.last_expand_count)
    small = player.expand(os.path.join(root, "d0"), max_files=10)
    small_trunc = bool(player.last_expand_truncated)
    one = player.expand(os.path.join(root, "d0", "m00.mp4"), max_files=5)
    default_cap = inspect.signature(MpvPlayer.expand).parameters["max_files"].default
    return {"env": "", "many": len(many), "many_trunc": many_trunc, "many_count": many_count,
            "small": len(small), "small_trunc": small_trunc, "one": len(one),
            "cap": int(player.MAX_EXPAND), "default": int(default_cap)}


def judge_expand_cap(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok = (m["many"] == 5 and m["many_trunc"] and m["many_count"] == 5
          and m["small"] == 4 and not m["small_trunc"] and m["one"] == 1
          and 500 <= m["cap"] <= 20000 and m["default"] == m["cap"])
    return ok, (f"12 个文件的目录按上限 5 展开：交回 {m['many']} 个、截断标记={m['many_trunc']}、"
                f"记账 {m['many_count']}；没到顶的目录交回 {m['small']} 个（要 4）且未标记截断；"
                f"单文件 {m['one']} 个；默认封顶 {m['default']}＝类常量 {m['cap']}"
                f"（{500}–{20000} 之内）；拆回原样（os.walk 不封顶）时 12 个全交回、标记恒 False")


# ---------- B11：失败负缓存封顶（坏文件再多也不许跟着库长） ----------

def scen_failed_cap() -> dict:
    """用"文件不存在"这条真入口灌失败缓存：到顶挤最旧、最新那条仍记得。"""
    base = os.path.join(TMP, "failed-cap")
    ps = PosterStore(cfgmod.FFMPEG_EXE, os.path.join(base, "posters"), max_jobs=1)
    cap_p = int(ps.FAILED_CAP)
    n_p = cap_p + 17
    pkeys = [ps.request(os.path.join(base, "p", f"m{i}.mp4"), 100.0) for i in range(n_p)]
    ts = ThumbStore(cfgmod.FFMPEG_EXE, os.path.join(base, "thumbs"), max_jobs=1)
    cap_t = int(ts.FAILED_CAP)
    n_t = cap_t + 17
    tkeys = [ts.request(os.path.join(base, "t", f"m{i}.mp4"), 100.0) for i in range(n_t)]
    return {"env": "", "cap_p": cap_p, "n_p": n_p, "size_p": len(ps._failed),
            "ev_p": ps.failed_evictions, "new_p": pkeys[-1] in ps._failed,
            "old_p": pkeys[0] in ps._failed,
            "cap_t": cap_t, "n_t": n_t, "size_t": len(ts._failed),
            "ev_t": ts.failed_evictions, "new_t": tkeys[-1] in ts._failed,
            "old_t": tkeys[0] in ts._failed}


def judge_failed_cap(m: dict) -> tuple[bool, str]:
    if m.get("env"):
        return False, m["env"]
    ok_p = (m["size_p"] == m["cap_p"] and m["ev_p"] == m["n_p"] - m["cap_p"]
            and m["new_p"] and not m["old_p"])
    ok_t = (m["size_t"] == m["cap_t"] and m["ev_t"] == m["n_t"] - m["cap_t"]
            and m["new_t"] and not m["old_t"])
    ok = m["cap_p"] >= 64 and m["cap_t"] >= 64 and ok_p and ok_t
    return ok, (f"封面失败缓存灌 {m['n_p']} 条：留 {m['size_p']}（封顶 {m['cap_p']}）、"
                f"挤出 {m['ev_p']}、最新仍记得={m['new_p']}、最旧已挤掉={not m['old_p']}；"
                f"缩略图 {m['n_t']}→{m['size_t']}（封顶 {m['cap_t']}）、挤出 {m['ev_t']}、"
                f"最新={m['new_t']}；拆回原样（无界字典）时两条都长到各自的 n")


CHECKS = [
    ("watch-state 只在文件真变过时重读（整库逐条问 N 次＝读盘 1 次）",
     judge_watch_read, lambda fx, cfg: scen_watch_read(cfg, fx)),
    ("媒体库整表重建只 stat 一次进度表（不是每张卡各 stat 一次）",
     judge_watch_stat, lambda fx, cfg: scen_watch_stat(fx)),
    ("外部改写那份文件必须被记号认出、并且读到新值（缓存不许把界面包成旧数）",
     judge_watch_external, lambda fx, cfg: scen_watch_external(cfg, fx)),
    ("那份文件被删了不许拿旧缓存继续报进度",
     judge_watch_deleted, lambda fx, cfg: scen_watch_deleted(cfg, fx)),
    ("自己记住的进度立刻可见，且不为此多读一遍盘（写穿）",
     judge_watch_writeback, lambda fx, cfg: scen_watch_writeback(cfg, fx)),
    ("媒体库整表重建不许同步解码整库封面（按批补，补完不漏）",
     judge_library_fill, lambda fx, cfg: scen_library_fill(fx)),
    ("媒体库卡片随侧栏宽换列（300 一列放宽、520 多列不空半屏）",
     judge_card_grid, lambda fx, cfg: scen_card_grid(fx)),
    ("媒体库空态/搜空/清词三态各说各的话（不共用一句空白、不把底注留着）",
     judge_library_empty, lambda fx, cfg: scen_library_empty(fx)),
    ("打字途中不整表重建（防抖只在击键那一路），停手后画的就是那个词",
     judge_typing, lambda fx, cfg: scen_typing(fx)),
    ("非击键那几路（扫描回包/排序）不许被防抖拖住",
     judge_immediate, lambda fx, cfg: scen_immediate(fx)),
    ("补图途中表变短（搜索过滤）：卡片不许挂上这一张表之外的封面",
     judge_midrebuild, lambda fx, cfg: scen_midrebuild(fx)),
    ("拖入文件夹：N 次 playlist 推送合并成几遍重建（不是 N 遍）",
     judge_playlist, lambda fx, cfg: scen_playlist()),
    ("合并要合到最后一份（不许停在中间态）",
     judge_playlist_last, lambda fx, cfg: scen_playlist_last(fx)),
    ("播放列表空白处右键：行的动作必须置灰（本机 row(None)=−1，不崩但会发出被内核拒收的 −1）",
     judge_context_menu, lambda fx, cfg: scen_context_menu()),
    ("封面回收按批（每张回来扫全目录＝把回收做成第二个卡顿源）",
     judge_prune_posters, lambda fx, cfg: scen_prune(N_DONE, PosterStore.PRUNE_EVERY)),
    ("按批不等于不做：攒够一批必须真收一次（封顶 400 是体积阀不是摆设）",
     judge_prune_never, lambda fx, cfg: scen_prune(N_DONE, PosterStore.PRUNE_EVERY)),
    ("缩略图回收同样按批",
     judge_prune_thumbs, lambda fx, cfg: scen_prune_thumbs(ThumbStore.PRUNE_EVERY * 2 + 3)),
    ("一张封面回来只摸到它那一行就停（B5：命中即停，别把整表扫完）",
     judge_poster_scan, lambda fx, cfg: scen_poster_scan(fx)),
    ("同一几何的 refit 不许反复铺 mpv 子窗口（B6：尺寸变才铺，空状态不铺）",
     judge_refit, lambda fx, cfg: scen_refit()),
    ("图标工厂缓存命中且封顶（B8：同一枚画一遍，灌再多也不无界）",
     judge_icon_cache, lambda fx, cfg: scen_icon_cache()),
    ("画面轮询随最小化停、恢复重开（B1：藏起来就不该每 30ms 问一次系统）",
     judge_poll_gate, lambda fx, cfg: scen_poll_gate()),
    ("最小化停轮询的接线真的挂在 WindowStateChange 上、且不受全屏态限制（B1 接线）",
     judge_poll_wiring, lambda fx, cfg: scen_poll_wiring()),
    ("拖入目录的展开封顶并如实标记截断（B3：os.walk 不许无边跟着目录走）",
     judge_expand_cap, lambda fx, cfg: scen_expand_cap()),
    ("失败负缓存封顶、但最新那条仍记得（B11：封面与缩略图两个 store 都要）",
     judge_failed_cap, lambda fx, cfg: scen_failed_cap()),
]


def run() -> int:
    t0 = time.time()
    missing = probe_interface()
    if missing:
        for n in missing:
            print(f"[ENV] 判据的前提立不住：产品里没有 {n}", flush=True)
        print("\n---- 响应性自检：未开跑——量具对不上产品接口（不算产品失败） ----", flush=True)
        return 2
    fx = Fixture()
    if not fx.need_jpg:
        print("[ENV] 写不出 jpg 样图：JPEG 图像插件缺失，这份量具的解码判据没有依据", flush=True)
        return 2
    cfg = cfgmod.Config()
    env_block = 0
    for name, judge, make in CHECKS:
        m: dict | None = None
        try:
            m = make(fx, cfg)
        except Exception:             # 真跑挂了＝产品红（不许顺手降级成 ENV），带栈继续跑后面的
            import traceback
            check(name, False, "跑挂了：" +
                  traceback.format_exc(limit=3).strip().replace("\n", " ‖ "))
            continue
        if m.get("env"):              # 判据的前提立不住：单独一类，不进台账
            print(f"[ENV] {name}  —— {m['env']}", flush=True)
            env_block += 1
            continue
        try:
            ok, detail = judge(m)
        except Exception:             # 判据自己挂了也算产品红：那说明它读的字段没了
            import traceback
            ok, detail = False, "判据自己挂了：" + \
                traceback.format_exc(limit=3).strip().replace("\n", " ‖ ")
        check(name, ok, detail)
    return report(env_block, t0)


def report(env_block: int, t0: float) -> int:
    passed = sum(1 for _n, ok, _d in results if ok)
    fails = [n for n, ok, _d in results if not ok]
    print(f"\n---- 响应性自检：{passed}/{len(results)} 通过；"
          f"{'无失败' if not fails else '失败项：' + str(fails)}"
          f"{'；判废 ' + str(env_block) + ' 条' if env_block else ''}"
          f"，用时 {time.time() - t0:.1f}s ----", flush=True)
    if fails:
        return 1
    if env_block:
        return 2
    return 0


# ---------- 证伪：把实现换成「常见错误写法」，逐条要求对应判据翻红 ----------

def f_case(name: str, caught: bool, detail: str) -> None:
    falsified.append((name, bool(caught), detail))
    print(f"[{'CAUGHT' if caught else 'MISSED'}] {name}  —— {detail}", flush=True)


def falsify() -> int:
    t0 = time.time()
    fx = Fixture()
    cfg = cfgmod.Config()

    # 1) watch-state 调回"每问一次重读整份"
    orig_load = cfgmod.Config._load_watch_state
    def no_cache(self, *_a, **_k):
        try:
            with open(self.watch_state_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            self.ws_reads += 1
            return d if isinstance(d, dict) else {}
        except Exception:
            self.ws_reads += 1
            return {}
    cfgmod.Config._load_watch_state = no_cache
    ok, d = judge_watch_read(scen_watch_read(cfg, fx))
    f_case("watch-state 的记号缓存（调回每问一次重读整份，'只读 1 次'必须翻红）", not ok, d)
    ok3, d3 = judge_watch_writeback(scen_watch_writeback(cfg, fx))
    f_case("自己写的立刻可见、且不靠重读那一遍（同一条变异，'为此多读盘 0 次'必须翻红——"
           "写穿要是假了，这条判据就是唯一能发现它的地方）", not ok3, d3)
    cfgmod.Config._load_watch_state = orig_load

    # 1b) 重建时把整表丢掉、逐卡问 watch_state：stat 次数必须翻红
    orig_label = LibraryPanel._label

    def label_per_card(self, e, watch=None):
        return orig_label(self, e, None)          # 变异：无视整表快照，逐卡问

    LibraryPanel._label = label_per_card
    ok1b, d1b = judge_watch_stat(scen_watch_stat(fx))
    f_case("进度表整趟只 stat 一次（拆回逐卡问 watch_state，'stat 1 次'必须翻红）",
           not ok1b, d1b)
    LibraryPanel._label = orig_label

    # 2) 记号调成常量（文件永远"没变过"）：外部改写与被删都必须翻红
    orig_stamp = cfgmod.Config.__dict__["_stamp"]
    cfgmod.Config._stamp = staticmethod(lambda p: (1, 1))
    ok, d = judge_watch_external(scen_watch_external(cfg, fx))
    f_case("认得出外部改写（把文件记号调成常量，'读到新值'必须翻红——"
           "缓存最怕的就是把界面包成一张旧数）", not ok, d)
    ok2, d2 = judge_watch_deleted(scen_watch_deleted(cfg, fx))
    f_case("认得出文件被删（同一条变异，'删表后不许报旧进度'必须翻红）", not ok2, d2)
    cfgmod.Config._stamp = orig_stamp

    # 3) 防抖撤掉：textChanged 直连整表重建
    panel, store, _c = mk_panel(fx)
    panel.search.textChanged.disconnect()
    panel.search.textChanged.connect(panel._rebuild)
    ok, d = judge_typing(scen_typing(fx, panel, store))
    f_case("搜索防抖（撤成每键直连整表重建，'途中 0 次重建'必须翻红）", not ok, d)

    # 4) 防抖挂到出口（谁来了都先排窗）：非击键那几路必须翻红
    orig_rebuild = LibraryPanel._rebuild
    def debounced_everything(self, *_a):
        self._debounce.start()               # 变异：出口也走防抖，当场谁都不画
        return
    LibraryPanel._rebuild = debounced_everything
    panel2, store2, _c2 = mk_panel(fx)
    ok, d = judge_immediate(scen_immediate(fx, panel2, store2))
    f_case("防抖只挂在击键入口（挂到 _rebuild 出口上，'排序当场就重排'必须翻红）", not ok, d)
    LibraryPanel._rebuild = orig_rebuild
    del debounced_everything

    # 5) 重建那趟同步解码整库（拆掉分批）
    def sync_decode(self, *_a):
        orig_rebuild(self, *_a)
        self.sync_decodes = 0
        for r in range(self.grid.count()):
            e = self._shown[r]
            key = self.posters.request(str(e.get("path") or ""), float(e.get("dur") or 0))
            it = self.grid.item(r)
            it.setData(Qt.UserRole + 1, key)
            pm = self.posters.image(key)
            if pm is not None:
                self.sync_decodes += 1
                it.setIcon(QIcon(pm))
        self._fill_rows = []
    LibraryPanel._rebuild = sync_decode
    ok, d = judge_library_fill(scen_library_fill(fx))
    f_case("封面分批补（调回'重建那趟就地解码整库'，'同步解码 ≤ 一片'必须翻红）", not ok, d)
    LibraryPanel._rebuild = orig_rebuild
    del sync_decode

    # 5b) 卡片尺寸退回固定 192（不论侧栏多宽都一列）
    orig_metrics = LP.card_metrics
    LP.card_metrics = lambda avail: (192, 176, 99, 158, 1)
    ok, d = judge_card_grid(scen_card_grid(fx))
    f_case("卡片随侧栏宽换列（退回固定 192 时，520 一档的列数与格宽必须翻红）", not ok, d)
    LP.card_metrics = orig_metrics

    # 5b2) 只听面板宽、不听视口宽：滚动条消失后视口变宽 11px，卡片停在上一档的窄格
    orig_filter = LibraryPanel.eventFilter
    LibraryPanel.eventFilter = lambda self, obj, ev: False
    ok, d = judge_card_grid(scen_card_grid(fx))
    f_case("格子跟当前视口算（摘掉视口宽度监听时，'现视口应得'必须翻红）", not ok, d)
    LibraryPanel.eventFilter = orig_filter
    del orig_filter

    # 5c) 空态翻面失效：所有状态都留在一块黑网格上
    def no_empty_switch(self, _q, _n):
        pass
    orig_empty = LibraryPanel._sync_empty
    LibraryPanel._sync_empty = no_empty_switch
    ok, d = judge_library_empty(scen_library_empty(fx))
    f_case("空态翻面（摘成空操作时，'空页可见＋格隐藏'必须翻红）", not ok, d)
    LibraryPanel._sync_empty = orig_empty
    del no_empty_switch

    # 5d) 三态共用一句"媒体库还是空的"（搜空时答非所问）
    def same_empty(self, _q, _n):
        self.empty_title.setText("媒体库还是空的")
        self.empty_hint.setText("点右上角 + 添加文件夹\n或把文件夹拖进来")
        self.empty_add.show()
        self.grid.hide()
        self.foot_hint.hide()
        self.empty_page.show()
    LibraryPanel._sync_empty = same_empty
    ok, d = judge_library_empty(scen_library_empty(fx))
    f_case("三态共用一句（搜空仍说'媒体库还是空的'时，标题/按钮/清词回网格三处必须翻红）", not ok, d)
    LibraryPanel._sync_empty = orig_empty
    del same_empty

    # 6) 一片补完整表（分批名存实亡）
    orig_batch = LibraryPanel._fill_batch
    def one_batch(self):
        rows, self._fill_rows = list(self._fill_rows), []
        for r in rows:
            it = self.grid.item(r)
            if it is None or r >= len(self._shown):
                continue
            e = self._shown[r]
            key = self.posters.request(str(e.get("path") or ""), float(e.get("dur") or 0))
            it.setData(Qt.UserRole + 1, key)
            pm = self.posters.image(key)
            if pm is not None:
                self.sync_decodes += 1
                it.setIcon(QIcon(pm))
        self.fill_batches += 1
    LibraryPanel._fill_batch = one_batch
    ok, d = judge_library_fill(scen_library_fill(fx))
    f_case("每片只 12 张（改成'一次补完整表'，'分片数 ≥ 需求量'必须翻红——"
           "片的**大小**才是让界面回话的那件事）", not ok, d)
    LibraryPanel._fill_batch = orig_batch

    # 7) 补图按整库行号取项（过滤后的第 r 张≠整库第 r 张）
    def fill_from_items(self):
        rows = self._fill_rows[:FILL_BATCH]
        del self._fill_rows[:FILL_BATCH]
        for r in rows:
            it = self.grid.item(r)
            if it is None:
                continue
            e = self._items[r] if r < len(self._items) else None     # 变异：整库第 r 项
            if e is None:
                continue
            key = self.posters.request(str(e.get("path") or ""), float(e.get("dur") or 0))
            it.setData(Qt.UserRole + 1, key)
            pm = self.posters.image(key)
            if pm is not None:
                self.sync_decodes += 1
                it.setIcon(QIcon(pm))
        self.fill_batches += 1
        if self._fill_rows:
            self._fill_timer.start()
    LibraryPanel._fill_batch = fill_from_items
    ok, d = judge_midrebuild(scen_midrebuild(fx))
    f_case("补图认的是当前这张表（改成按整库行号取项，'外来封面 0 张'必须翻红）", not ok, d)
    LibraryPanel._fill_batch = orig_batch
    del fill_from_items, one_batch

    # 8) playlist 合并撤掉：推送直连重建
    orig_reb = PlaylistPanel.rebuild
    def straight(self, playlist):
        self.pushes += 1
        self._pending = list(playlist or [])
        self._rebuild_now()
    PlaylistPanel.rebuild = straight
    ok, d = judge_playlist(scen_playlist())
    f_case("playlist 按窗合并（调回推送直连重建，'重建 ≤ 上限'必须翻红）", not ok, d)
    PlaylistPanel.rebuild = orig_reb

    # 9) 合并只留第一份（停在中间态）
    def first_only(self, playlist):
        self.pushes += 1
        if self._pending is None:
            self._pending = list(playlist or [])
        if not self._coalesce.isActive():
            self._coalesce.start()
    PlaylistPanel.rebuild = first_only
    ok, d = judge_playlist_last(scen_playlist_last(fx))
    f_case("合并要合到最后一份（改成'只记第一份'，'画出行数=内核最后那份'必须翻红）", not ok, d)
    PlaylistPanel.rebuild = orig_reb
    del straight, first_only

    # 9b) 空白处右键调回旧写法：row=-1 不判、菜单项全亮（点了发 −1，内核拒收）
    orig_cm = PlaylistPanel._context_menu

    def naive_menu(self, pos):
        row = self.list.row(self.list.itemAt(pos))      # 旧写法：None → -1 也照发
        m = PLP.QMenu(self)
        a1 = m.addAction("播放")
        a2 = m.addAction("从列表移除")
        m.addSeparator()
        a3 = m.addAction("清空列表")
        chosen = m.exec(self.list.mapToGlobal(pos))
        if chosen is a1:
            self.sig_play_index.emit(row)
        elif chosen is a2:
            self.sig_remove_index.emit(row)
        elif chosen is a3:
            self.sig_clear.emit()

    PlaylistPanel._context_menu = naive_menu
    ok, d = judge_context_menu(scen_context_menu())
    mutated = PlaylistPanel._context_menu is not orig_cm
    f_case("空白处右键（旧写法不认 row=-1、菜单项全亮，'空白处播放=禁用'必须翻红）",
           mutated and not ok, f"变异生效={mutated} {d}")
    PlaylistPanel._context_menu = orig_cm

    # 9c) 对偶：怕发 −1 就把三项一起置灰 → 空白处连"清空列表"都点不动
    def all_disabled_menu(self, pos):
        item = self.list.itemAt(pos)
        row = self.list.row(item) if item is not None else -1
        m = PLP.QMenu(self)
        a1 = m.addAction("播放")
        a2 = m.addAction("从列表移除")
        m.addSeparator()
        a3 = m.addAction("清空列表")
        for a in (a1, a2, a3):
            a.setEnabled(row >= 0)                       # 变异：把清空也一起置灰
        chosen = m.exec(self.list.mapToGlobal(pos))
        if chosen is a1 and row >= 0:
            self.sig_play_index.emit(row)
        elif chosen is a2 and row >= 0:
            self.sig_remove_index.emit(row)
        elif chosen is a3:
            self.sig_clear.emit()

    PlaylistPanel._context_menu = all_disabled_menu
    ok, d = judge_context_menu(scen_context_menu())
    mutated = PlaylistPanel._context_menu is not orig_cm
    f_case("空白处「清空列表」仍要能点（把三项一起置灰，'清空=可用'必须翻红）",
           mutated and not ok, f"变异生效={mutated} {d}")
    PlaylistPanel._context_menu = orig_cm
    del naive_menu, all_disabled_menu

    # 10) 回收调回每张一次
    orig_note_p = PosterStore._note_done
    PosterStore._note_done = lambda self: self.prune()
    ok, d = judge_prune_posters(scen_prune(N_DONE, PosterStore.PRUNE_EVERY))
    f_case("封面回收按批（调回'每张回来扫全目录'，'回收次数=批数'必须翻红）", not ok, d)

    # 11) 回收干脆不做（对偶：证明上一条不是"越少越绿"）
    PosterStore._note_done = lambda self: None
    ok, d = judge_prune_never(scen_prune(N_DONE, PosterStore.PRUNE_EVERY))
    f_case("按批不等于不做（把回收摘干净，'攒够一批必须真收一次'必须翻红）", not ok, d)
    PosterStore._note_done = orig_note_p

    # 12) 缩略图那条同形，两个方向都要有牙
    orig_note_t = ThumbStore._note_done
    ThumbStore._note_done = lambda self: None
    ok, d = judge_prune_thumbs(scen_prune_thumbs(ThumbStore.PRUNE_EVERY * 2 + 3))
    f_case("缩略图回收按批（把它摘成永不回收，'次数=份数/批'必须翻红）", not ok, d)
    ThumbStore._note_done = lambda self: self.prune()
    ok2, d2 = judge_prune_thumbs(scen_prune_thumbs(ThumbStore.PRUNE_EVERY * 2 + 3))
    f_case("缩略图回收也不许每份扫一遍（调回逐份回收，同一条判据必须从另一头翻红）", not ok2, d2)
    ThumbStore._note_done = orig_note_t

    # 13) B5：封面回来不 break
    orig_on_poster = LibraryPanel._on_poster
    def scan_all(self, key, ok, _detail):
        if not ok:
            return
        pm = self.posters.image(key)
        if pm is None:
            return
        for i in range(self.grid.count()):
            it = self.grid.item(i)
            self.poster_scans += 1
            if it.data(Qt.UserRole + 1) == key:
                it.setIcon(QIcon(pm))            # 变异：不 break，继续摸完整张表
    LibraryPanel._on_poster = scan_all
    ok, d = judge_poster_scan(scen_poster_scan(fx))
    f_case("封面回来命中就停（拆掉 break，'摸 1 张卡'必须翻红——大库成批回来时这就是几百万次白摸）",
           not ok, d)
    LibraryPanel._on_poster = orig_on_poster
    del scan_all

    # 14) B6：每次都铺子窗口
    orig_refit = VideoSurface.refit
    def always_refit(self, force=False):
        self._fit_key = None                     # 变异：几何判定失效，每次必铺
        return orig_refit(self, force)
    VideoSurface.refit = always_refit
    ok, d = judge_refit(scen_refit())
    f_case("同一几何不重铺（改成每次都铺，'跳过 5 次＋底层 0 调用'必须翻红）", not ok, d)
    VideoSurface.refit = orig_refit
    del always_refit

    # 15) B8：缓存不命中 / 不封顶
    orig_icon = icons.icon
    def no_cache(name, size=20, color="#ffffff", hover_color=None):
        ic = QIcon()
        ic.addPixmap(icons._paint(name, size, color), QIcon.Normal, QIcon.Off)
        icons._ICON_BUILT += 1
        return ic                               # 变异：画完就扔，从不入缓存
    icons.icon = no_cache
    ok, d = judge_icon_cache(scen_icon_cache())
    f_case("图标要第二遍不重画（撤掉缓存命中，'同一个对象/只画 1 枚'必须翻红）", not ok, d)

    def unbounded_icon(name, size=20, color="#ffffff", hover_color=None):
        key = (name, int(size), str(color), str(hover_color))
        hit = icons._ICON_CACHE.get(key)
        if hit is not None:
            icons._ICON_HITS += 1
            return hit
        ic = QIcon()
        ic.addPixmap(icons._paint(name, size, color), QIcon.Normal, QIcon.Off)
        icons._ICON_BUILT += 1
        icons._ICON_CACHE[key] = ic             # 变异：只进不出
        return ic
    icons.icon = unbounded_icon
    ok, d = judge_icon_cache(scen_icon_cache())
    f_case("图标缓存封顶（去掉 FIFO 挤出，'灌满后 size==cap'必须翻红）", not ok, d)
    icons.icon = orig_icon
    del no_cache, unbounded_icon

    # 16) B1：最小化不停表
    orig_should = VideoSurface._should_poll
    VideoSurface._should_poll = lambda self: not self._empty
    ok, d = judge_poll_gate(scen_poll_gate())
    f_case("最小化停表（把 _should_poll 改成只看空/可见，'最小化后 0 拍'必须翻红）", not ok, d)
    VideoSurface._should_poll = orig_should

    # 17) B1 接线：塞进全屏分支 / 整句删掉（纯源码判据，两种坏法都要抓到）
    bad_src = textwrap.dedent('''
        def changeEvent(self, ev):
            super().changeEvent(ev)
            if ev.type() == QEvent.WindowStateChange:
                if self._fs:
                    self.video.sync_poll()
            if ev.type() != QEvent.WindowStateChange or not self._fs:
                return
    ''')
    ok, d = judge_poll_wiring({"env": "", "src": bad_src})
    f_case("接线不受全屏态限制（把 sync_poll 塞进 if self._fs 里，同一判据必须翻红）", not ok, d)
    ok, d = judge_poll_wiring({"env": "", "src": scen_poll_wiring()["src"].replace(
        "self.video.sync_poll()", "pass", 1)})
    f_case("接线真挂上（把 changeEvent 里那句删掉，'调用点'必须翻红）", not ok, d)

    # 18) B3：目录展开无边
    orig_expand = MpvPlayer.expand
    def unbounded_expand(self, path, max_files=MpvPlayer.MAX_EXPAND):
        self.expand_calls += 1
        self.last_expand_truncated = False
        if os.path.isdir(path):
            out = []
            for root, _dirs, files in os.walk(path):     # 变异：不封顶
                for f in files:
                    if is_media_file(f):
                        out.append(os.path.join(root, f))
            self.last_expand_count = len(out)
            return sorted(out, key=lambda p: (os.path.dirname(p).lower(),
                                              os.path.basename(p).lower()))
        out = [path] if is_media_file(path) else []
        self.last_expand_count = len(out)
        return out
    MpvPlayer.expand = unbounded_expand
    ok, d = judge_expand_cap(scen_expand_cap())
    f_case("目录展开封顶（调回无边 os.walk，'交回 5 个＋截断标记'必须翻红）", not ok, d)
    MpvPlayer.expand = orig_expand

    # 19) B11：负缓存无界 / 干脆不记（对偶：封顶不等于把备忘摘掉）
    orig_np, orig_nt = PosterStore._note_failed, ThumbStore._note_failed
    PosterStore._note_failed = lambda self, key: self._failed.__setitem__(key, None)
    ThumbStore._note_failed = lambda self, key, why: self._failed.__setitem__(key, why)
    ok, d = judge_failed_cap(scen_failed_cap())
    f_case("失败负缓存封顶（调回无界字典，'留=cap＋挤出数'必须翻红——两个 store 一起）",
           not ok, d)
    PosterStore._note_failed = lambda self, key: None
    ThumbStore._note_failed = lambda self, key, why: None
    ok, d = judge_failed_cap(scen_failed_cap())
    f_case("封顶不等于不记（把负缓存摘成空操作，'最新仍记得'必须翻红）", not ok, d)
    PosterStore._note_failed = orig_np
    ThumbStore._note_failed = orig_nt

    caught = sum(1 for _n, ok, _d in falsified if ok)
    missed = [n for n, ok, _d in falsified if not ok]
    print(f"\n---- 证伪：{caught}/{len(falsified)} 抓到；"
          f"{'无漏网' if not missed else '漏网：' + str(missed)}"
          f"，用时 {time.time() - t0:.1f}s ----", flush=True)
    return 1 if missed else 0


def cleanup() -> None:
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        rc = falsify() if "--falsify" in sys.argv else run()
    finally:
        cleanup()
    sys.exit(rc)
