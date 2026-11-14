# -*- coding: utf-8 -*-
"""媒体库规模自检：索引键规范化、并发探测、骨架先到、同一作业多孩子、O(1) 封面定位。

守的五件事：
  L1 索引键统一 `_norm_path`（normcase+normpath）：盘符/分隔符/大小写不同的同一路径
     必须命中缓存，而不是每次全量重探；
  L2 探测并发跑（≤ min(4, cpu)）：12 个文件不该串成一条 12 行的队；
  L3 大目录先把骨架列表交出去（每张卡先显示 "--:--"），时长随后补齐；
  L4 `poolkit` 同一作业可以有多个在途孩子（并发探测的前提），kill_children 一个不漏；
  L5 封面回来按 `_key_items` 字典直查（结构闸门：不再线性扫 grid）。

L1–L4 用假探测函数（不依赖 ffmpeg 内容），只量调度与键；L5 是源码结构判据。
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

from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.engine import library as LB  # noqa: E402
from app.engine import poolkit  # noqa: E402

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


def main() -> int:
    rows = [("L1 索引键规范化（大小写/写法都命中缓存）", l1_normalized_keys),
            ("L2 探测并发（≤ min(4, cpu)）", l2_concurrent_probe),
            ("L3 大目录先发骨架再补齐时长", l3_skeleton_first),
            ("L4 同一作业多个在途孩子全部登记/杀掉", l4_multi_children),
            ("L5 封面按 _key_items 字典直查", l5_poster_dict)]
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
