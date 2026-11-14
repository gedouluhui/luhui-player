"""隐藏窗口预览：不把任何窗口放到桌面上，只把渲染结果写成 PNG。

为什么要有这一份：机器正在被用户使用，任何前台窗口都会挡人。`WA_DontShowOnScreen`
让 Qt 照常走完 show/layout/paint 全过程，但平台层从不把窗口映射到桌面——比"从不 show、
只 activate 布局"更接近真机：QStackedWidget 的换页、QSS 的 :hover 之外的一切、子控件
的 show/hide 传播都会真实发生。离屏平台（QT_QPA_PLATFORM=offscreen）在本机实测**不画文字**，
所以这里必须留在 windows 平台插件上。

用法：
    python tests/ui_preview.py                  # 六种状态各出一张
    python tests/ui_preview.py empty library    # 只出这几张
输出目录：tests/out/preview/
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# 预览用的数据目录必须与用户真配置隔离：PosterStore/Config 都会往这里写。
os.environ["GELU_DATA"] = os.path.join(HERE, ".preview-data")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt5.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter,  # noqa: E402
                         QPainterPath, QPixmap)
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.config import Config  # noqa: E402
from app.ui import icons, theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

OUT = os.path.join(HERE, "out", "preview")
W, H = 1440, 900


_APP: QApplication | None = None


def _app() -> QApplication:
    """全局持有一份 QApplication：局部拿到却不留引用时，Python 会在函数返回后把它析构，
    后面每次建控件都落在一个已经不存在的 application 上（实测 rc=127、日志全空）。"""
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    _APP.setStyle("Fusion")
    theme.install(_APP, theme.ACCENT_DEFAULT)
    return _APP


def _poster(w: int, h: int, seed: int, title: str) -> QPixmap:
    """一张像海报的抽象图：渐变 + 弧线 + 片名。不依赖 ffmpeg。"""
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    palettes = [
        ("#1B2B24", "#4F7A63", "#C8D8C4"),
        ("#241E2B", "#5C4A72", "#E2D3C4"),
        ("#2A2119", "#7A5A3A", "#EAD9B8"),
        ("#16252B", "#3D6B76", "#C5E0DD"),
        ("#2B1A1E", "#7A3F4C", "#EED2D2"),
        ("#1C1D22", "#4A4E5C", "#D8DAE2"),
    ]
    top, mid, ink = palettes[seed % len(palettes)]
    g = QLinearGradient(0, 0, w * 0.6, h)
    g.setColorAt(0, QColor(top))
    g.setColorAt(0.55, QColor(mid))
    g.setColorAt(1, QColor(top).darker(130))
    p.fillRect(0, 0, w, h, g)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 14))
    p.drawEllipse(QPointF(w * (0.2 + 0.12 * (seed % 3)), h * 1.02), w * 0.9, w * 0.9)
    p.setBrush(QColor(0, 0, 0, 60))
    p.drawRect(QRectF(0, h * 0.62, w, h * 0.38))
    f = QFont("Noto Sans SC")
    f.setPixelSize(max(10, int(h * 0.095)))
    f.setWeight(QFont.DemiBold)
    p.setFont(f)
    p.setPen(QColor(ink))
    p.drawText(QRectF(w * 0.09, h * 0.66, w * 0.82, h * 0.24),
               Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, title)
    p.end()
    return pm


def _lib_items() -> list[dict]:
    names = ["海边的一天", "夜航西飞", "山与雾", "深海来信", "城市之光",
             "缓慢的春天", "北纬三十度", "风经过的街道", "无声的雪", "黎明之前",
             "旧唱片店", "南方车站"]
    out = []
    for i, n in enumerate(names):
        out.append({
            "path": f"C:/preview/{n}.mp4",
            "name": f"{n}.mp4",
            "dur": 5400 + i * 731,
            "size": 1_200_000_000 + i * 90_000_000,
            "mtime": 1_760_000_000 - i * 86400,
        })
    return out


def _playlist_items() -> list[dict]:
    names = ["海边的一天", "夜航西飞", "山与雾", "深海来信", "城市之光", "缓慢的春天"]
    return [{"filename": f"C:/preview/{n}.mp4", "title": n,
             "playing": i == 1, "current": i == 1} for i, n in enumerate(names)]


def _fill_library(win: MainWindow) -> None:
    items = _lib_items()
    win.lib_panel.set_items(items)
    win.lib_panel._fill_timer.stop()
    win.lib_panel._fill_rows.clear()
    model = win.lib_panel.grid.model()
    for i, e in enumerate(items):
        if 0 <= i < model.rowCount():
            pm = _poster(320, 180, i, os.path.splitext(e["name"])[0])
            model.set_icon(i, QIcon(pm))
            model.set_key(i, "preview")
    # 真机是停手 60ms 后才排卡片；预览没有等待窗，手动让这一趟落定。
    win.lib_panel._layout_timer.stop()
    win.lib_panel._apply_card_layout()


def _fill_playlist(win: MainWindow) -> None:
    win.panel._pending = _playlist_items()
    win.panel._rebuild_now()


def _sidebar(win: MainWindow, page: int) -> None:
    win.sidebar.setVisible(True)
    win._sidebar_page(page)
    win._fit_sidebar()


def _grab(win: MainWindow, name: str) -> str:
    app = QApplication.instance()
    for _ in range(4):
        app.processEvents()
    win.layout().activate()
    app.processEvents()
    if name == "library":
        # 真 Library 的扫描回包/清空会在切页后再来一次；等所有事件落定，最后再铺假数据，
        # 否则前面铺的 12 张卡会被一条空推送整表覆盖掉。
        _fill_library(win)
        app.processEvents()
    pm = win.grab()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.png")
    pm.save(path)
    print(f"{name}: {pm.width()}x{pm.height()} -> {path}")
    return path


def render(states: list[str]) -> int:
    def step(msg: str) -> None:
        print(f"[preview] {msg}", flush=True)

    step("app")
    _app()
    cfg = Config(os.path.join(HERE, ".preview-data", "preview-config.json"))
    cfg["sidebar_width"] = 336
    step("window")
    import traceback
    try:
        win = MainWindow(cfg)
    except BaseException:
        traceback.print_exc()
        step("构造 MainWindow 抛异常")
        return 2
    step("constructed")
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(W, H)
    step("resized")
    win.show()
    step("shown")
    QApplication.instance().processEvents()
    step("events")
    win.layout().activate()
    step("laid out")
    step("library")
    _fill_library(win)
    step("playlist")
    _fill_playlist(win)

    try:
        for s in states:
            win.sidebar.setVisible(False)
            if s == "empty":
                win.empty.set_notice("")
            elif s == "empty-notice":
                win.empty.point_out([("C:/preview/坏文件.mkv", "找不到解码器")])
            elif s == "playlist":
                _sidebar(win, 0)
            elif s == "library":
                _sidebar(win, 1)
                # 切页会走 page_shown / 扫描回包，假数据要在这之后再铺一次——
                # 上一版预览里媒体库永远 0 张卡，就是被这一趟覆盖掉的。
                _fill_library(win)
            elif s == "tweak":
                _sidebar(win, 2)
            elif s == "transform":
                _sidebar(win, 3)
            elif s == "eq":
                _sidebar(win, 4)
            elif s == "stats":
                _sidebar(win, 5)
            else:
                raise SystemExit(f"未知状态 {s}")
            win.layout().activate()
            _grab(win, s)
    finally:
        # 隐藏窗口 + Win32 chrome 的析构在解释器退出时会段错误（本机实测 rc=139），
        # 这跟预览内容无关；图片已经落盘，直接跳过 QApplication 的析构。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    names = sys.argv[1:] or ["empty", "playlist", "library", "tweak", "transform", "eq", "stats"]
    raise SystemExit(render(names))
