"""离屏探针：媒体库「最近播放」范围 + 时间轴 A-B 区间可视化。

两件事都只有一条判据：**画出来的东西是不是来自内核/配置的真值**。
- 最近范围：配置 `recent` 里盘上还在的条目才出现；不在索引里的现造；顺序＝最近在前；
  排序下拉置灰；空态文案与"库空/搜空"分开。
- A-B：轨道上方那条细带只在真的设了 A 之后才出现，B 出现后带子从 A 连到 B；
  像素量的是主色墨水，不是读内部状态。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine.library import Library  # noqa: E402
from app.engine.posters import PosterStore  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.library_panel import SCOPE_ALL, SCOPE_RECENT, LibraryPanel  # noqa: E402
from app.ui.scrubber import Scrubber  # noqa: E402

TMP = os.path.join(HERE, ".probe-recents")
APP = QApplication.instance() or QApplication(sys.argv[:1])
theme.install(APP, str(cfgmod.DEFAULTS.get("theme_accent", "#4C8DFF")))


def pump(sec=0.05):
    import time
    end = time.monotonic() + sec
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)


def touch(dirpath, name, size=64):
    os.makedirs(dirpath, exist_ok=True)
    p = os.path.join(dirpath, name)
    with open(p, "wb") as fh:
        fh.write(b"x" * size)
    return p


def accent_ink(img: QImage, y0: int, y1: int) -> int:
    acc = QColor(theme.ACCENT)
    n = 0
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() < 60:
                continue
            d = (abs(c.red() - acc.red()) + abs(c.green() - acc.green())
                 + abs(c.blue() - acc.blue()))
            if d <= 90:
                n += 1
    return n


def ab_probe() -> dict:
    s = Scrubber()
    s.resize(320, 26)
    s.set_state(0.0, 120.0)
    img0 = s.grab().toImage()
    band_y = int(26 / 2 - 2 - 4.6)          # r.top()-4.6 附近
    ink_empty = accent_ink(img0, 0, 14)
    s.set_loop_range(24.0, 72.0)
    img1 = s.grab().toImage()
    ink_ab = accent_ink(img1, 0, 14)
    # 带子只该在 A..B 之间：A 左边 10px 与 B 右边 10px 都不该有主色墨水
    ax, bx = s._x_at(24.0), s._x_at(72.0)
    left = QImage(img1)
    right = QImage(img1)
    def ink_region(img, x0, x1):
        n = 0
        for y in range(0, 14):
            for x in range(int(x0), int(x1)):
                c = img.pixelColor(x, y)
                if c.alpha() < 60:
                    continue
                acc = QColor(theme.ACCENT)
                if (abs(c.red() - acc.red()) + abs(c.green() - acc.green())
                        + abs(c.blue() - acc.blue())) <= 90:
                    n += 1
        return n
    ink_before = ink_region(left, 0, max(0, ax - 6))
    ink_after = ink_region(right, min(img1.width(), bx + 6), img1.width())
    s.set_loop_range(24.0, None)
    img2 = s.grab().toImage()
    ink_a_only = accent_ink(img2, 0, 14)
    dot = ink_region(img2, ax - 4, ax + 5)
    s.set_loop_range(None, None)
    img3 = s.grab().toImage()
    ink_clear = accent_ink(img3, 0, 14)
    out = {"empty": ink_empty, "ab": ink_ab, "before": ink_before, "after": ink_after,
           "a_only": ink_a_only, "dot": dot, "cleared": ink_clear,
           "ax": round(ax, 1), "bx": round(bx, 1)}
    s.deleteLater()
    return out


def recent_probe() -> dict:
    lib_dir = os.path.join(TMP, "lib")
    cfg = cfgmod.Config()
    store = PosterStore(cfgmod.FFMPEG_EXE, os.path.join(TMP, "posters"), max_jobs=1)
    lib = Library(cfgmod.FFMPEG_EXE, os.path.join(TMP, "library.json"), [lib_dir])
    panel = LibraryPanel(lib, store, cfg)
    panel.resize(320, 640)
    panel.show()
    files = [touch(lib_dir, f"movie-{i}.mp4") for i in range(4)]
    items = [{"name": os.path.basename(p), "path": p, "ext": ".mp4", "dur": 60.0 + i,
              "size": 64, "mtime": 1.0 + i} for i, p in enumerate(files)]
    panel.set_items(items)
    pump(0.1)
    outside = touch(lib_dir, "outside.mp4")           # 不在索引里、但盘上还在
    gone = os.path.join(lib_dir, "gone.mp4")          # 记录里有、盘上没了
    cfg.data["recent"] = [files[2], gone, outside, files[0], files[2].replace("/", "\\")]
    panel.btn_scope.setChecked(True)
    pump(0.1)
    model = panel.grid.model()
    rows = [model.index(i, 0).data(Qt.UserRole) for i in range(model.rowCount())]
    out = {"scope": panel.scope, "recent": rows, "n": model.rowCount(),
           "sort_enabled": panel.sort.isEnabled(),
           "placeholder": panel.search.placeholderText(),
           "status": panel.lbl_status.text(), "tip": panel.lbl_status.toolTip(),
           "empty_visible": panel.empty_page.isVisible()}
    panel.search.setText("zzzz")
    pump(0.4)
    out["miss_title"] = panel.empty_title.text()
    out["miss_hint"] = panel.empty_hint.text()
    out["miss_btn"] = panel.empty_add.isVisible()
    panel.search.setText("")
    panel.btn_scope.setChecked(False)
    pump(0.4)
    out["all_n"] = panel.grid.model().rowCount()
    out["all_scope"] = panel.scope
    out["sort_enabled_back"] = panel.sort.isEnabled()
    panel.note_played(files[1])
    out["note_in_all_does_nothing"] = panel.scope == SCOPE_ALL
    panel.deleteLater()
    return out


def main() -> int:
    ab = ab_probe()
    rec = recent_probe()
    ok_ab = (ab["empty"] <= 2 and ab["ab"] > ab["empty"] + 30
             and ab["before"] <= 2 and ab["after"] <= 2
             and ab["a_only"] > ab["empty"] and ab["dot"] > 2 and ab["cleared"] <= 2)
    want = [os.path.normcase(os.path.abspath(p)) for p in
            [os.path.join(TMP, "lib", "movie-2.mp4"),
             os.path.join(TMP, "lib", "outside.mp4"),
             os.path.join(TMP, "lib", "movie-0.mp4")]]
    got = [os.path.normcase(os.path.abspath(p)) for p in rec["recent"]]
    ok_rec = (rec["scope"] == SCOPE_RECENT and got == want and rec["n"] == 3
              and not rec["sort_enabled"] and rec["placeholder"] == "搜索最近播放…"
              and "最近播放" in rec["status"] and rec["miss_btn"] is False
              and "没有找到" in rec["miss_title"] and rec["all_n"] == 4
              and rec["all_scope"] == SCOPE_ALL and rec["sort_enabled_back"])
    print(f"[{'PASS' if ok_ab else 'FAIL'}] A-B 区间像素：空={ab['empty']} AB={ab['ab']} "
          f"A 前={ab['before']} B 后={ab['after']} 只有A={ab['a_only']}(点={ab['dot']}) "
          f"清除后={ab['cleared']}（A@{ab['ax']}px B@{ab['bx']}px）")
    print(f"[{'PASS' if ok_rec else 'FAIL'}] 最近播放范围：{rec['n']} 行 "
          f"sort={rec['sort_enabled']} 状态={rec['status']!r} 空态={rec['miss_title']!r} "
          f"按钮={rec['miss_btn']} 回到全部={rec['all_n']}行 "
          f"行={[os.path.basename(str(p)) for p in rec['recent']]}")
    return 0 if (ok_ab and ok_rec) else 1


if __name__ == "__main__":
    sys.exit(main())
