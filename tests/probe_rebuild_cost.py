# -*- coding: utf-8 -*-
"""一次性量具（跑完即可丢弃）：媒体库整表重建到底贵在哪一步。

为什么要先量再改：#53/#54 的修法取决于"每条目那笔开销是谁贡献的"——
如果主要是 os.stat/exists（文件系统），加防抖就够了；如果主要是 QPixmap 解码
（PosterStore._img 只留 60 张，重建到第 61 张就要重新从盘上解），那防抖只把卡顿
从"每键一次"推到"每次停顿一次"，3000 项的库还是卡一秒，得改成分批填图标。
判据不进契约（耗时不进台账），这份只用来选路，不改产品代码。

跑法（offscreen，不依赖屏幕醒着）：
    set PYTHONIOENCODING=utf-8 && set GELU_DATA=tests\\.selftest-data
    python tests/probe_rebuild_cost.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, ROOT)

from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine.posters import PosterStore  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
PROBE = os.path.join(cfgmod.CACHE_DIR, "_probe_lib")
VID = os.path.join(PROBE, "v")
POSTERS = os.path.join(cfgmod.CACHE_DIR, "posters")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    for d in (VID, POSTERS):
        os.makedirs(d, exist_ok=True)
    # 造 N 个"存在但不可能是真片"的条目：request() 只要 os.path.exists 过得去、
    # 并且 key 对应的封面 jpg 已在盘上，就不会起 ffmpeg 进程（这正是重建的路径）。
    keys = []
    store = PosterStore(cfgmod.FFMPEG_EXE, POSTERS)
    src = QImage(320, 180, QImage.Format_RGB32)
    src.fill(QColor(40, 90, 160))
    paths = []
    for i in range(N):
        p = os.path.join(VID, f"clip-{i:05d}.mp4")
        with open(p, "wb") as fh:
            fh.write(b"probe")
        k = store.key_for(p)
        ok = src.save(os.path.join(POSTERS, k + ".jpg"), "JPG", 85)
        assert ok, "造封面 jpg 失败"
        keys.append(k)
        paths.append(p)

    t0 = time.perf_counter()
    for p in paths:
        store.key_for(p)
    t_key = time.perf_counter() - t0

    t0 = time.perf_counter()
    for p, k in zip(paths, keys):
        store.request(p, 10.0)
    t_req = time.perf_counter() - t0

    t0 = time.perf_counter()
    for k in keys:
        store.image(k)
    t_img_cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    for k in keys[-40:]:
        store.image(k)
    t_img_hot = time.perf_counter() - t0

    print(f"N={N}  jpg 造出={len(keys)}  缓存封顶={len(store._img)}")
    print(f"key_for(os.stat×N)     {t_key * 1000:8.1f} ms   （{t_key / max(1, N) * 1e6:6.1f} µs/项）")
    print(f"request(exists×3N)     {t_req * 1000:8.1f} ms   （{t_req / max(1, N) * 1e6:6.1f} µs/项）")
    print(f"image() 全冷（超出 60 封顶）{t_img_cold * 1000:8.1f} ms   （{t_img_cold / max(1, N) * 1e6:6.1f} µs/项）")
    print(f"image() 命中缓存(最近40)  {t_img_hot * 1000:8.1f} ms   （{t_img_hot / 40 * 1e6:6.1f} µs/项）")

    from app import config as C
    cfg = C.Config()
    wp = cfg.watch_state_path
    os.makedirs(os.path.dirname(wp), exist_ok=True)
    import json
    table = {f"X:\\{p}": {"pos": 100.0, "dur": 200.0, "ts": 1.0, "done": False} for p in paths}
    with open(wp, "w", encoding="utf-8") as fh:
        json.dump(table, fh, ensure_ascii=False)
    sz = os.path.getsize(wp)
    # 旧口径（每问一次就重读整份文件）现场复现一遍来量它；新口径走 cfg.watch_state()。
    t0 = time.perf_counter()
    for _ in paths:
        with open(wp, "r", encoding="utf-8") as fh:
            json.load(fh)
    t_ws = time.perf_counter() - t0
    t0 = time.perf_counter()
    for p in paths:
        cfg.watch_state(f"X:\\{p}")
    t_ws_cached = time.perf_counter() - t0
    print(f"watch_state() 旧口径×N（表 {sz} 字节）{t_ws * 1000:8.1f} ms   （{t_ws / max(1, N) * 1e6:6.1f} µs/项）")
    print(f"watch_state() 新口径×N          {t_ws_cached * 1000:8.1f} ms，真读盘 {cfg.ws_reads} 次")
    print(f"合计每条目 ≈ {(t_key + t_req + t_img_cold + t_ws) / max(1, N) * 1e6:6.1f} µs"
          f" → 3000 项一趟 ≈ {(t_key + t_req + t_img_cold + t_ws) / max(1, N) * 3000:5.2f} s")

    for d in (PROBE,):
        shutil.rmtree(d, ignore_errors=True)
    for k in keys:
        try:
            os.remove(os.path.join(POSTERS, k + ".jpg"))
        except OSError:
            pass
    try:
        os.remove(wp)
    except OSError:
        pass
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
