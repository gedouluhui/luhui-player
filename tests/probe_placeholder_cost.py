# -*- coding: utf-8 -*-
"""一次性量具：3000 张卡片的占位块补画 / 整表重建各要多久（offscreen，不弹窗）。

只回答一个问题：卡档位变化时 `_refresh_placeholders()` 会不会把一帧预算吃穿。
跑完把日志留在 tests/out/probe-placeholder-cost-r22i.log；它是复现入口，不进口袋。
"""
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["GELU_DATA"] = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "out", "_probe-ph-data")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, pyqtSignal             # noqa: E402
from PyQt5.QtWidgets import QApplication                 # noqa: E402

from app import config as cfgmod                         # noqa: E402
from app.ui import theme                                 # noqa: E402
from app.engine.library import Library                   # noqa: E402
from app.ui.library_panel import LibraryPanel            # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])
theme.install(APP, theme.ACCENT_DEFAULT)


class PosterStub(QObject):
    sig_done = pyqtSignal(str, bool, str)

    def request(self, path, dur):
        return ""

    def image(self, key):
        return None

    def wait(self, ms=0):
        return True


class CfgStub:
    def watch_snapshot(self):
        return {}


N = 3000
items = [{"name": f"clip-{i:04d}.{'mkv' if i % 3 == 0 else 'mp4'}",
          "path": f"X:\\lib\\clip-{i:04d}.mp4", "ext": ".mkv" if i % 3 == 0 else ".mp4",
          "dur": 100.0 + i, "size": 1024 * (i + 1), "mtime": float(i)}
         for i in range(N)]

lib = Library(cfgmod.FFMPEG_EXE, os.path.join(os.environ["GELU_DATA"], "x.json"), [])
panel = LibraryPanel(lib, PosterStub(), CfgStub())
panel.resize(520, 900)

t0 = time.perf_counter()
panel.set_items(items)
t_rebuild = (time.perf_counter() - t0) * 1000.0

panel.show()
APP.processEvents()
t0 = time.perf_counter()
panel._refresh_placeholders()
t_refresh = (time.perf_counter() - t0) * 1000.0

# 第二趟走缓存：判断"重复换档"时的量级
t0 = time.perf_counter()
panel._refresh_placeholders()
t_refresh2 = (time.perf_counter() - t0) * 1000.0

print(f"N={N}  items={panel.grid.model().rowCount()}  "
      f"rebuild={t_rebuild:.1f}ms  sync_decodes={panel.sync_decodes}  "
      f"placeholders_made={panel.placeholders_made}", flush=True)
print(f"refresh(首趟，画图标)={t_refresh:.1f}ms  refresh(缓存命中)={t_refresh2:.1f}ms  "
      f"one_frame_budget=16.7ms", flush=True)
panel.close()
panel.deleteLater()
