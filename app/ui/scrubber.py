"""时间轴：已播/缓冲/章节刻度/悬停时间气泡/拖拽定位。

只做"像素 ↔ 秒"的换算与绘制，不碰播放逻辑： seek 由主窗口拿到秒数后决定用语速
（拖动中相对快进）还是精确帧（松手时 absolute+exact），所以对外只发信号。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from . import theme

M = theme.M


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec // 60) % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Scrubber(QWidget):
    sig_seek = pyqtSignal(float, bool)     # (秒, 是否精确到帧)
    sig_drag_state = pyqtSignal(bool)      # 开始/结束拖动：主窗口据此暂停 time-pos 刷新
    sig_hover_sec = pyqtSignal(float, int)  # 悬停/拖动中的 (秒, 该秒在轨道上的 x 像素)：
                                            # 缩略图浮层要钉在这个 x 上，光给秒数没法定位
    sig_hover_end = pyqtSignal()

    THICK = 3.0
    HOT = 22.0        # 命中区比可见轨道厚，好拖
    # 拖动节流的间隔：鼠标一次快速滑动能产生几百个 move 事件，每个都发 seek 会把
    # IPC 写队列和内核定位命令灌满（拖动反而变得一顿一顿）。60ms ≈ 16 次/秒，
    # 肉眼看到的跟随仍然连续，而内核每次要处理的关键帧 seek 少了一个数量级。
    DRAG_EMIT_MIN = 0.06
    # 松手后这段时间内，管道里在途的旧 time-pos（拖动开始前的值）会晚到；它离目标
    # 很远时一定是陈旧回写，丢弃。窗口只开 0.22s：真内核在这段时间内已经回报新位置。
    SEEK_GUARD = 0.22
    REBOUND_TOL = 0.6     # "离目标多远才算陈旧"（秒）：正常的属性推送误差远小于它

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Scrubber")
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(M.scrub_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._pos = 0.0
        self._dur = 0.0
        self._buffered = 0.0      # 秒
        self._chapters: list[dict] = []
        # A-B 循环的两个点：各自可以是 None（没设）。分开存而不是只存区间——
        # 按了 Shift+A、还没按 Shift+B 的那段时间里 A 点也该看得见。
        self._loop_a: float | None = None
        self._loop_b: float | None = None
        self._hover_x: int | None = None
        self._dragging = False
        self._drag_pos = 0.0
        self._last_drag_emit = 0.0
        # 松手 seek 的"目标点 + 守卫截止时刻"：见 set_state 里丢弃陈旧回写那一段。
        self._seek_target = 0.0
        self._seek_guard_until = 0.0

    # ---------- 外部喂数据 ----------
    def set_state(self, pos: float, dur: float) -> None:
        if self._dragging:
            return
        pos, dur = float(pos or 0), float(dur or 0)
        # 防回弹：松手 seek 之后，管道里还在路上的旧 time-pos 会把进度条短暂拉回
        # 拖动前的位置（用户看到"松手后跳一下再回来"）。守卫窗口内只要离目标超过
        # REBOUND_TOL 就判定为陈旧回写；目标附近的真实值照常采纳。
        if (time.monotonic() < self._seek_guard_until
                and abs(pos - self._seek_target) > self.REBOUND_TOL):
            self._dur = dur
            return
        self._pos, self._dur = pos, dur
        self.update()

    def set_buffered(self, seconds: float) -> None:
        self._buffered = float(seconds or 0)
        self.update()

    def set_chapters(self, chapters: list[dict]) -> None:
        self._chapters = list(chapters or [])
        self.update()

    def set_loop_range(self, a: float | None, b: float | None) -> None:
        """A-B 循环的两个点（未设的一端传 None）。值来自内核缓存，不是界面自己记的。"""
        na = None if a is None else max(0.0, float(a))
        nb = None if b is None else max(0.0, float(b))
        if na == self._loop_a and nb == self._loop_b:
            return
        self._loop_a, self._loop_b = na, nb
        self.update()

    # ---------- 几何 ----------
    def _track_rect(self) -> QRectF:
        # 通栏：两端不留 2px 缩进。轨道短一截会在左右各留一个视觉缺口，
        # 和整条控制条的通栏留白对不齐。圆角自己负责收头。
        return QRectF(0, self.height() / 2 - self.THICK / 2, max(1, self.width()), self.THICK)

    def _sec_at(self, x: int) -> float:
        r = self._track_rect()
        if self._dur <= 0 or r.width() <= 0:
            return 0.0
        f = min(1.0, max(0.0, (x - r.left()) / r.width()))
        return f * self._dur

    def _x_at(self, sec: float) -> float:
        r = self._track_rect()
        if self._dur <= 0:
            return r.left()
        return r.left() + min(1.0, max(0.0, sec / self._dur)) * r.width()

    # ---------- 鼠标 ----------
    def _emit_preview(self, x: int) -> None:
        """把"此刻光标指到哪一秒、该画在哪个像素"报给主窗口（缩略图浮层用）。"""
        sec = self._drag_pos if self._dragging else self._sec_at(x)
        self.sig_hover_sec.emit(sec, int(self._x_at(sec)))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._dur > 0:
            self._dragging = True
            self._drag_pos = self._sec_at(ev.pos().x())
            self.sig_drag_state.emit(True)
            self.sig_seek.emit(self._drag_pos, False)
            self._last_drag_emit = time.monotonic()
            self._emit_preview(ev.pos().x())
            self.update()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._dragging:
            self._drag_pos = self._sec_at(ev.pos().x())
            now = time.monotonic()
            if now - self._last_drag_emit >= self.DRAG_EMIT_MIN:
                self._last_drag_emit = now
                self.sig_seek.emit(self._drag_pos, False)
            # 预览/绘制每帧都跟手：只有"发给内核的 seek"需要节流，光标与气泡不需要。
            self._emit_preview(ev.pos().x())
            self.update()
        else:
            x = ev.pos().x()
            if self._hover_x != x:
                self._hover_x = x
                if self._dur > 0:
                    self._emit_preview(x)
                self.update()

    def leaveEvent(self, ev):
        self._hover_x = None
        if self._dur > 0 and not self._dragging:
            self.sig_hover_end.emit()
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._dragging and ev.button() == Qt.LeftButton:
            target = self._sec_at(ev.pos().x())
            self._dragging = False
            # 松手这一下不等节流：用户放下的位置就是他要看的帧，必须精确且立刻生效。
            self._pos = target
            self._seek_target = target
            self._seek_guard_until = time.monotonic() + self.SEEK_GUARD
            self.sig_seek.emit(target, True)   # 松手定帧：精确 seek
            self.sig_drag_state.emit(False)
            if not self.underMouse():
                self.sig_hover_end.emit()
            self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self._track_rect()
        frac = (self._drag_pos if self._dragging else self._pos) / self._dur if self._dur else 0.0

        # 底轨
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BORDER))
        p.drawRoundedRect(r, self.THICK / 2, self.THICK / 2)

        if self._dur > 0:
            # 缓冲
            bx = self._x_at(min(self._dur, self._buffered))
            if bx > r.left():
                p.setBrush(QColor(255, 255, 255, 30))
                p.drawRoundedRect(QRectF(r.left(), r.top(), bx - r.left(), r.height()),
                                  self.THICK / 2, self.THICK / 2)
            # 已播
            hx = self._x_at(frac * self._dur)
            # 悬停预览：从播放头到光标之间先铺一层淡主色，落点在哪一秒不用等点击才看见。
            if self._hover_x is not None and not self._dragging and self._hover_x > hx:
                p.setBrush(QColor(theme.ACCENT))
                p.setOpacity(0.28)
                p.drawRoundedRect(QRectF(hx, r.top(), self._hover_x - hx, r.height()),
                                  self.THICK / 2, self.THICK / 2)
                p.setOpacity(1.0)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(0.0, hx - r.left()), r.height()),
                              self.THICK / 2, self.THICK / 2)

            self._paint_loop(p, r)

            # 章节刻度：本地文件没有章节时就是一条干净的线，不画假标记
            if len(self._chapters) > 1:
                p.setPen(QPen(QColor(theme.TEXT_FAINT), 1.0))
                for c in self._chapters[1:]:
                    t = float(c.get("time") or 0.0)
                    if 0 < t < self._dur:
                        cx = self._x_at(t)
                        p.drawLine(int(cx), int(r.top() - 4), int(cx), int(r.bottom() + 4))

            # 手柄：静止时收起来，悬停或拖动才出现（成熟播放器的做法）。
            # 外层一圈淡主色是"光晕"，不是描边——让 12px 的圆在 3px 轨道上有个落点。
            if self._dragging or self._hover_x is not None:
                d = 14.0 if self._dragging else 11.0
                glow = QColor(theme.ACCENT)
                glow.setAlpha(56)
                p.setPen(Qt.NoPen)
                p.setBrush(glow)
                p.drawEllipse(QRectF(hx - d / 2 - 3, r.center().y() - d / 2 - 3, d + 6, d + 6))
                p.setBrush(QColor(theme.PRIMARY_FILL))
                p.setPen(QPen(QColor(theme.BG_DEEP), 2.0))
                p.drawEllipse(QRectF(hx - d / 2, r.center().y() - d / 2, d, d))

        # 悬停时间气泡
        if self._hover_x is not None and self._dur > 0 and not self._dragging:
            self._paint_bubble(p, self._hover_x, fmt_time(self._sec_at(self._hover_x)))
        if self._dragging and self._dur > 0:
            self._paint_bubble(p, int(self._x_at(self._drag_pos)), fmt_time(self._drag_pos))
        p.end()

    def _paint_loop(self, p: QPainter, r: QRectF) -> None:
        """A-B 循环：轨道上方一条细带＋两端竖线。只画内核真有的点。

        为什么不把区间铺在轨道本身上：那条 4px 的轨道已经同时在说"缓冲到哪"和"播到哪"，
        再压一层颜色就得靠透明度讲第三件事，深底上三种灰挤在一起谁也读不出来。
        细带画在轨道上方 4px（与章节刻度同一层空间），拖动时不会被手指挡住。
        """
        a = self._loop_a
        if a is None or self._dur <= 0:
            return
        a = min(a, self._dur)
        b = self._loop_b
        b = min(b, self._dur) if b is not None and b > a else None
        ax = self._x_at(a)
        bx = self._x_at(b) if b is not None else None
        band_y = r.top() - 4.6
        accent = QColor(theme.ACCENT)
        if bx is not None:
            fill = QColor(accent)
            fill.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(QRectF(ax, band_y, max(2.0, bx - ax), 2.4), 1.2, 1.2)
        else:
            # 只设了 A：给一个圆点，告诉用户"起点已经记下了，还缺终点"
            dot = QColor(accent)
            dot.setAlpha(200)
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QRectF(ax - 2.2, band_y - 1.2, 4.4, 4.4))
        pen = QPen(QColor(accent), 1.3)
        p.setPen(pen)
        p.drawLine(int(round(ax)), int(round(band_y - 1.0)), int(round(ax)),
                   int(round(r.bottom() + 1.6)))
        if bx is not None:
            p.drawLine(int(round(bx)), int(round(band_y - 1.0)), int(round(bx)),
                       int(round(r.bottom() + 1.6)))

    def _paint_bubble(self, p: QPainter, x: int, text: str) -> None:
        f = QFont(self.font())
        f.setFamily("Cascadia Mono")
        f.setPixelSize(11)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(text) + 18
        h = 21
        bx = min(max(2, x - w / 2), max(2.0, self.width() - w - 2))
        box = QRectF(bx, 0, w, h)
        path = QPainterPath()
        path.addRoundedRect(box, 7, 7)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawRoundedRect(box.translated(0, 1.5), 7, 7)
        p.setBrush(QColor(theme.BG_RAISE))
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.drawPath(path)
        p.setPen(QColor(theme.TEXT))
        p.drawText(box, Qt.AlignCenter, text)
