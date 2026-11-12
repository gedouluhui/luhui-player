"""目检探针（复现入口）：媒体库卡片在 300 / 520 宽侧栏里的排布（不弹前台）。

给本轮改动的 `card_metrics`/`_apply_card_layout` 出两张图；不写进常跑链，
但盘上那份 PNG 是目检的留档，重跑会覆写它。
"""
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ["GELU_DATA"] = os.path.join(os.path.dirname(__file__), "out", "_probe-lib-data")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QObject, Qt, pyqtSignal     # noqa: E402
from PyQt5.QtWidgets import QApplication             # noqa: E402

from app import config as cfgmod                     # noqa: E402
from app.ui import theme                             # noqa: E402
from app.engine.library import Library               # noqa: E402
from app.ui.library_panel import LibraryPanel        # noqa: E402

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


items = []
names = [
    ("2026 秋 京都漫步 4K HDR 章节版.mkv", 5423, 8_912_345_678),
    ("Interstellar.2014.2160p.BluRay.x265-TERMiNAL.mkv", 10140, 15_032_115_200),
    ("演唱会现场 · 第二夜.mp4", 7382, 6_442_450_944),
    ("纪录片：深海之下（中字）.mp4", 3120, 2_985_132_032),
    ("S01E04 - 长夜.mkv", 2640, 1_503_238_144),
    ("旅行 vlog 剪辑 final final v3.mp4", 845, 812_646_400),
    ("短片合集 01.mp4", 1260, 1_181_116_416),
    ("测试片 1080p60.mp4", 600, 512_000_000),
]
for i, (n, d, s) in enumerate(names):
    items.append({"name": n, "path": os.path.join("X:\\", n), "ext": os.path.splitext(n)[1],
                  "dur": float(d), "size": s, "mtime": float(1000 + i)})

lib = Library(cfgmod.FFMPEG_EXE, os.path.join(os.environ["GELU_DATA"], "x.json"), [])

for w in (300, 520):
    panel = LibraryPanel(lib, PosterStub(), CfgStub())
    panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    panel.resize(w, 640)
    panel.set_items(items)
    panel.show()
    # 布局活在 60ms 防抖定时器里：只 processEvents 两次不够（第一次跑出来还是 192/1 列）。
    # 这里按 10ms 一泵地转满 0.4s，等于真的等过那个防抖窗。
    t0 = time.time()
    while time.time() - t0 < 0.4:
        APP.processEvents()
        time.sleep(0.01)
    out = os.path.join(os.path.dirname(__file__), "out", f"library-layout-{w}.png")
    panel.grab().save(out, "PNG")
    print(f"{w}: panel={panel.width()} viewport={panel.grid.viewport().width()} "
          f"cell={panel.card_w} cols={panel.card_cols} "
          f"icon={panel.grid.iconSize().width()}x{panel.grid.iconSize().height()} "
          f"visible={panel.isVisible()} -> {os.path.basename(out)}", flush=True)
    panel.close()
    panel.deleteLater()
    APP.processEvents()

# 空状态两页：库里没有片 vs 搜索没命中。文案与按钮不是同一句。
for tag, pool, query in (("empty", [], ""), ("nomatch", items, "不存在的片名")):
    panel = LibraryPanel(lib, PosterStub(), CfgStub())
    panel.setAttribute(Qt.WA_DontShowOnScreen, True)
    panel.resize(300, 640)
    panel.set_items(pool)
    if query:
        panel.search.setText(query)
    panel.show()
    t0 = time.time()
    while time.time() - t0 < 0.7:
        APP.processEvents()
        time.sleep(0.01)
    out = os.path.join(os.path.dirname(__file__), "out", f"library-empty-{tag}.png")
    panel.grab().save(out, "PNG")
    print(f"{tag}: grid_visible={panel.grid.isVisible()} "
          f"empty_visible={panel.empty_page.isVisible()} "
          f"title={panel.empty_title.text()!r} hint={panel.empty_hint.text()!r} "
          f"add_btn={panel.empty_add.isVisible()} -> {os.path.basename(out)}", flush=True)
    panel.close()
    panel.deleteLater()
    APP.processEvents()
