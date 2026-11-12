"""隐藏预览：对话框、菜单、OSD 与时间轴缩略图。

和 `ui_preview.py` 同一条约束——任何窗口都不映射到桌面，只把渲染结果写成 PNG。
用法：python tests/ui_preview_overlays.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ["GELU_DATA"] = os.path.join(HERE, ".preview-data")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu, QWidget  # noqa: E402

from app.config import Config  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.floaters import Osd, ScrubPreview  # noqa: E402
from app.ui.gif_dialog import GifDialog  # noqa: E402
from app.ui.help_page import help_dialog_class  # noqa: E402
from app.ui.keymap import effective  # noqa: E402
from app.ui.keymap_dialog import KeymapDialog  # noqa: E402
from app.ui.settings_dialog import SettingsDialog  # noqa: E402
from app.ui.sleep_timer import SleepDialog, SleepTimer  # noqa: E402

OUT = os.path.join(HERE, "out", "preview")
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    _APP.setStyle("Fusion")
    theme.install(_APP, theme.ACCENT_DEFAULT)
    return _APP


def _poster(w: int, h: int) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#16252B"))
    g.setColorAt(1, QColor("#3D6B76"))
    p.fillRect(0, 0, w, h, g)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 16))
    p.drawEllipse(QPointF(w * 0.72, h * 0.3), w * 0.2, w * 0.2)
    p.end()
    return pm


def _grab(widget: QWidget, name: str) -> None:
    app = QApplication.instance()
    widget.setAttribute(Qt.WA_DontShowOnScreen, True)
    widget.show()
    for _ in range(4):
        app.processEvents()
    if widget.layout() is not None:
        widget.layout().activate()
    app.processEvents()
    pm = widget.grab()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.png")
    pm.save(path)
    print(f"{name}: {pm.width()}x{pm.height()} -> {path}", flush=True)
    # 每张抓完就关掉：前一个模态/活动窗口留着，后建的窗口会被 Qt 置灰，
    # 预览里所有颜色都像被蒙了一层，量出来的结论会是假的。
    widget.close()
    widget.deleteLater()
    app.processEvents()


def _menu() -> None:
    m = QMenu()
    for text, checked in (("播放", False), ("暂停", True), ("跳到下一章", False)):
        a = m.addAction(text)
        a.setCheckable(True)
        a.setChecked(checked)
    m.addSeparator()
    sub = m.addMenu("播放速度")
    for s in ("0.75x", "1x", "1.25x"):
        a = sub.addAction(s)
        a.setCheckable(True)
        a.setChecked(s == "1x")
    m.addAction("窗口置顶").setCheckable(True)
    _grab(m, "menu")


def render() -> int:
    _app()
    cfg = Config(os.path.join(HERE, ".preview-data", "overlay-config.json"))

    settings = SettingsDialog(cfg)
    _grab(settings, "dialog-settings")

    keys = KeymapDialog(cfg)
    _grab(keys, "dialog-keymap")

    gif = GifDialog(cfg, lambda: {"video": "C:/preview/海边的一天.mp4", "pos": 125.4,
                                  "duration": 6420.0, "ab": (None, None), "sub": ""})
    _grab(gif, "dialog-gif")

    Help = help_dialog_class()
    help_dlg = Help(effective(cfg.get("keymap")), "Ctrl+K")
    _grab(help_dlg, "dialog-help")

    sleep = SleepDialog(SleepTimer())
    _grab(sleep, "dialog-sleep")

    anchor = QWidget()
    anchor.resize(1000, 560)
    anchor.setAttribute(Qt.WA_DontShowOnScreen, True)
    anchor.show()
    QApplication.processEvents()
    osd = Osd(anchor)
    osd.show_text("1.25x · 声音 62%")
    _grab(osd, "osd")
    preview = ScrubPreview(anchor)
    preview.show_at(_poster(320, 180), "01:23:45", 500, 400)
    _grab(preview, "scrub-preview")

    _menu()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(render())
