"""应用内对话框的公共外壳：深色原生标题栏。

QDialog 的标题栏由系统画，样式表管不到它：深色播放器里弹出设置页时，顶上一条
浅色 caption 会把"同一套软件"打断。这里只在第一次 show 时把 DWM 的 caption /
border / text 三个颜色对齐主题令牌，拖动、贴边、无障碍行为都还是系统原生的。

没有做无边框自绘标题栏：对话框是短任务页面，系统标题栏的拖动/双击/贴边/屏幕
阅读器支持都是白送的；为了换一条 32px 的标题而自己实现这些，风险比收益大。
"""
from __future__ import annotations

from PyQt5.QtWidgets import QDialog

from .. import win32
from . import theme


class AppDialog(QDialog):
    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        win32.dark_caption(int(self.winId()), theme.BG_DEEP, theme.TEXT, theme.BORDER_SOFT)
