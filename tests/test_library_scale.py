# -*- coding: utf-8 -*-
"""媒体库规模自检：索引键规范化、并发探测、骨架先到、同一作业多孩子、O(1) 封面定位。

守的五件事：
  L1 索引键统一 `_norm_path`（normcase+normpath）：盘符/分隔符/大小写不同的同一路径
     必须命中缓存，而不是每次全量重探；
  L2 探测并发跑（≤ min(4, cpu)）：12 个文件不该串成一条 12 行的队；
  L3 大目录先把骨架列表交出去（每张卡先显示 "--:--"），时长随后补齐；
  L4 `poolkit` 同一作业可以有多个在途孩子（并发探测的前提），kill_children 一个不漏；
  L5 封面回来按 `_key_items` 字典直查（结构闸门：不再线性扫 grid）；
  L6 网格是真虚拟化的 `QListView + QAbstractListModel`，不再逐行造 QListWidgetItem；
  L7 三千行只存在模型里，封面回来只改命中的那一行（不重建）。

L1–L4 用假探测函数（不依赖 ffmpeg 内容），只量调度与键；L5/L6 是源码结构判据；
L7 造 3000 行真面板，量模型契约与海报直改。
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = tempfile.mkdtemp(prefix="lib-scale-", dir=HERE)
os.environ["LUHUI_DATA"] = DATA
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QObject, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QIcon, QPixmap  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QListView,  # noqa: E402
                             QListWidget)

from app.engine import library as LB  # noqa: E402
from app.engine import poolkit  # noqa: E402
from app.ui.library_panel import LibraryPanel  # noqa: E402

app = QApplication.instance() or QApplication([])


def _mk_files(root: str, n: int) -> list[str]:
    out = []
    for i in range(n):
        p = os.path.join(root, f"clip-{i:03d}.mp4")
        with open(p, "wb") as fh:
            fh.write(b"x" * 64)
        out.append(p)
    return out


def _run_job(tree: str, index: dict, timeout: float = 60.0) -> tuple[list, dict, str, list]:
    """同步跑一趟 ScanJob，直接收信号（同一线程，信号是直连）。"""
    job = LB.ScanJob(os.path.join(DATA, "no-ffmpeg.exe"), [tree], index)
    got: dict = {"done": None, "preview": []}

    def on_preview(items):
        got["preview"].append(list(items))

    def on_done(items, new_index, detail):
        got["done"] = (list(items), dict(new_index), detail)

    job.sig.preview.connect(on_preview)
    job.sig.done.connect(on_done)
    t0 = time.time()
    job.run()
    if got["done"] is None:
        raise RuntimeError("ScanJob 没有回报 done")
    return [*got["done"], got["preview"]], {"elapsed": time.time() - t0}


def l1_normalized_keys() -> tuple[bool, str]:
    tree = os.path.join(DATA, "L1")
    os.makedirs(tree)
    files = _mk_files(tree, 2)
    st = os.stat(files[0])
    upper = os.path.abspath(files[0]).upper()
    index = {upper: {"size": st.st_size, "mtime": float(st.st_mtime), "dur": 7.5}}
    (items, new_index, detail, _prev), _ = _run_job(tree, index)
    by_path = {os.path.normcase(os.path.normpath(e["path"])): e for e in items}
    key0 = LB._norm_path(files[0])
    if key0 not in by_path:
        return False, "扫描结果缺第一条"
    if abs(by_path[key0]["dur"] - 7.5) > 1e-6:
        return False, f"大写路径的缓存没命中（dur={by_path[key0]['dur']}，状态={detail}）"
    bad = [k for k in new_index if k != LB._norm_path(k)]
    if bad:
        return False, f"新索引仍有未规范化的键：{bad[:2]}"
    return True, ""


def l2_concurrent_probe() -> tuple[bool, str]:
    tree = os.path.join(DATA, "L2")
    os.makedirs(tree)
    _mk_files(tree, 12)
    active = [0]
    peak = [0]
    guard = threading.Lock()
    real = LB.probe_duration

    def fake(_exe, _path, timeout=20.0, owner=None):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.12)
        with guard:
            active[0] -= 1
        return 1.0

    LB.probe_duration = fake
    try:
        (items, new_index, detail, _prev), m = _run_job(tree, {})
    finally:
        LB.probe_duration = real
    want_workers = min(4, os.cpu_count() or 2)
    if peak[0] < 2:
        return False, f"探测完全串行（峰值并发 {peak[0]}；状态={detail}）"
    if peak[0] > want_workers:
        return False, f"并发超过上限：峰值 {peak[0]} > {want_workers}"
    if len(items) != 12 or any(abs(float(e["dur"]) - 1.0) > 1e-6 for e in items):
        return False, f"12 条时长没有全部落账：{[e['dur'] for e in items][:5]}…"
    if m["elapsed"] > 12 * 0.12 * 0.8:
        return False, f"总耗时 {m['elapsed']:.2f}s 接近串行下界 {12 * 0.12:.2f}s"
    return True, f"峰值并发 {peak[0]}（上限 {want_workers}），{m['elapsed']:.2f}s"


def l3_skeleton_first() -> tuple[bool, str]:
    tree = os.path.join(DATA, "L3")
    os.makedirs(tree)
    _mk_files(tree, LB.PREVIEW_MIN + 20)
    real = LB.probe_duration

    def slow(_exe, _path, timeout=20.0, owner=None):
        time.sleep(0.03)
        return 2.0

    LB.probe_duration = slow
    try:
        (items, _idx, detail, previews), _ = _run_job(tree, {})
    finally:
        LB.probe_duration = real
    if not previews:
        return False, "没有先发骨架列表"
    first = previews[0]
    if len(first) != LB.PREVIEW_MIN + 20:
        return False, f"骨架条数不对：{len(first)}"
    if sum(1 for e in first if float(e.get("dur") or 0) > 0) > 0:
        return False, "骨架里已经带了时长（说明探测跑完了才发，骨架失去了意义）"
    if any(float(e.get("dur") or 0) <= 0 for e in items):
        return False, "最终回包仍有 0 时长（慢探测的结果没补齐）"
    return True, ""


def l4_multi_children() -> tuple[bool, str]:
    owner = object()
    procs = [poolkit.spawn(owner, [sys.executable, "-c", "import time; time.sleep(30)"])
             for _ in range(3)]
    try:
        pids = poolkit.child_pids()
        missing = [p.pid for p in procs if p.pid not in pids]
        if missing:
            return False, f"同一作业只登记了最后一个孩子，漏 {missing}"
        n = poolkit.kill_children()
        if n < 3:
            return False, f"kill_children 只杀了 {n} 个"
        time.sleep(0.2)
        alive = [p.pid for p in procs if p.poll() is None]
        if alive:
            return False, f"杀完还活着：{alive}"
        return True, ""
    finally:
        poolkit.drop_child(owner)
        for p in procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass


def l5_poster_dict() -> tuple[bool, str]:
    path = os.path.join(ROOT, "app", "ui", "library_panel.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_poster":
            src = ast.dump(node)
            if "_key_items" not in src:
                return False, "_on_poster 没有用 _key_items 字典定位"
            if "grid.count" in src:
                return False, "_on_poster 仍在按 grid.count() 线性扫"
            return True, ""
    return False, "library_panel 里找不到 _on_poster"


def l6_model_view() -> tuple[bool, str]:
    """结构闸门：视图换成 QListView + QAbstractListModel，行不再是 QListWidgetItem。"""
    path = os.path.join(ROOT, "app", "ui", "library_panel.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    legacy = sorted(n for n in ("QListWidget", "QListWidgetItem") if n in names)
    if legacy:
        return False, f"library_panel 仍在用 {legacy}（没有真虚拟化）"
    if "QListView" not in names or "QAbstractListModel" not in names:
        return False, "library_panel 没有 QListView + QAbstractListModel"
    if "setModel" not in attrs:
        return False, "视图没有接上模型（找不到 setModel）"
    return True, ""


class _CfgStub:
    def watch_snapshot(self) -> dict:
        return {}


class _PosterStub(QObject):
    """可控封面源：补图阶段不返回图（只发键），`allow` 打开后才"回来"一张。"""

    sig_done = pyqtSignal(str, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.allow = False
        self.asked: set[str] = set()
        self._pm = QPixmap(24, 14)
        self._pm.fill(QColor("#3a7bd5"))

    def request(self, path: str, dur: float) -> str:
        key = "k:" + str(path)
        self.asked.add(key)
        return key

    def image(self, key: str):
        return self._pm if self.allow and key in self.asked else None

    def wait(self, ms: int = 0) -> bool:
        return True


def l7_model_rows() -> tuple[bool, str]:
    """3000 行只存在模型里；`_key_items` 存行号；封面回来只改那一行、不重建。"""
    n = 3000
    lib = LB.Library(os.path.join(DATA, "no-ffmpeg.exe"),
                     os.path.join(DATA, "view-index.json"), [])
    store = _PosterStub()
    panel = LibraryPanel(lib, store, _CfgStub())
    panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    panel.resize(520, 900)
    items = [{"name": f"clip-{i:04d}.mp4", "path": f"X:\\lib\\clip-{i:04d}.mp4",
              "ext": ".mp4", "dur": 60.0 + i, "size": 1024 * (i + 1),
              "mtime": float(i)} for i in range(n)]
    try:
        panel.set_items(items)
        if not isinstance(panel.grid, QListView) or isinstance(panel.grid, QListWidget):
            return False, f"grid 还是 {type(panel.grid).__name__}"
        model = panel.grid.model()
        if model is None or model.rowCount() != n:
            return False, f"模型行数不对：{None if model is None else model.rowCount()}"
        got = {str(model.index(i, 0).data(Qt.UserRole) or "") for i in range(n)}
        if got != {e["path"] for e in items}:
            return False, f"模型里的路径集合对不上（{len(got)}/{n}，默认排序后仍应是同一张表）"
        if not str(model.index(n - 1, 0).data(Qt.UserRole) or ""):
            return False, "第 3000 行取不到数据（模型没有持有整表）"
        while panel._fill_rows:
            before = len(panel._fill_rows)
            panel._fill_batch()
            if len(panel._fill_rows) >= before:
                return False, "补图队列没有前进"
        panel._fill_timer.stop()
        if len(panel._key_items) != n:
            return False, f"补图后键表只有 {len(panel._key_items)}/{n}"
        key, row = next(iter(panel._key_items.items()))
        if not isinstance(row, int) or not 0 <= row < n:
            return False, f"_key_items 的值不是行号：{type(row).__name__}"
        if str(model.index(row, 0).data(Qt.UserRole + 1) or "") != key:
            return False, "那一行的键和 _key_items 对不上"
        store.allow = True
        r0, s0 = panel.rebuilds, panel.poster_scans
        panel._on_poster(key, True, "")
        if panel.rebuilds != r0:
            return False, "封面回来触发了整表重建"
        if panel.poster_scans - s0 != 1:
            return False, f"封面回来摸了 {panel.poster_scans - s0} 张卡（要 1）"
        icon = model.index(row, 0).data(Qt.DecorationRole)
        if not isinstance(icon, QIcon) or icon.isNull():
            return False, "命中的那一行图标没换成真封面"
        return True, f"{n} 行在模型里；封面只改第 {row} 行，重建 0 次"
    finally:
        panel.close()
        panel.deleteLater()


def main() -> int:
    rows = [("L1 索引键规范化（大小写/写法都命中缓存）", l1_normalized_keys),
            ("L2 探测并发（≤ min(4, cpu)）", l2_concurrent_probe),
            ("L3 大目录先发骨架再补齐时长", l3_skeleton_first),
            ("L4 同一作业多个在途孩子全部登记/杀掉", l4_multi_children),
            ("L5 封面按 _key_items 字典直查", l5_poster_dict),
            ("L6 网格是 QListView + 模型（不再逐行造 item）", l6_model_view),
            ("L7 三千行在模型里，封面只改命中行", l7_model_rows)]
    red = 0
    for name, fn in rows:
        try:
            ok, detail = fn()
        except Exception as exc:            # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ｜ {detail}" if detail else ""))
        red += 0 if ok else 1
    shutil.rmtree(DATA, ignore_errors=True)
    print(f"[FINAL] 媒体库规模：{len(rows) - red}/{len(rows)} 通过；红 {red} 条")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
