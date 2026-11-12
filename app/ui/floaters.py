"""浮在画面上的东西：OSD 气泡、时间轴悬停预览。

为什么用独立顶层窗口而不是子控件：mpv 的画面是 Qt 容器 HWND 的**原生子窗口**，
原生子窗口永远画在父表面之上，所以视频区里放不进任何 Qt 控件；但"另一个顶层窗口"
可以压在它上面。于是浮层 = Qt.Tool（不进任务栏、随主窗口一起最小化）+ 无边框 +
分层透明 + 不吃焦点，并把视频控件当 **parent 传给顶层窗口**——Qt 在 Win32 侧据此建立
拥有关系，浮层才保证压在主人窗口之上（传 None 时 z-order 无人负责，实测会画在下面）。

R1 用自绘半透明底，没上 DWM 亚克力：毛玻璃压在视频上=糊自己的画面，而
WA_TranslucentBackground（分层窗口）与 DWM backdrop 能否共存我没有实测过，
不做没验证过的取舍。win32.set_acrylic() 那段实现留在原地，等实测过这一条再接进来
（接的时候它才配有自己的配置键——之前 config 里那个 acrylic 谁也没读，已经删掉了）。
"""
from __future__ import annotations

from PyQt5.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from . import theme


class Osd(QWidget):
    """居中冒泡的提示：倍速/音量/截图/帧步进等一次性反馈。"""

    def __init__(self, anchor: QWidget):
        # anchor 既是定位基准也是**窗口拥有者**：Qt 的顶层窗口带 parent 时，Win32 那边
        # 会建成 owner 关系，浮层就保证压在主人窗口之上。早先传 None，z-order 没人管，
        # 实测出现过"气泡 show 了、屏幕截图里却是视频帧"（跨度从 219 掉到 39）——
        # 不是没画，是画在了底下。拥有关系还顺带解决"主窗最小化时浮层要跟下去"。
        super().__init__(anchor, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.anchor = anchor
        self._text = ""
        self._opacity = 1.0
        self.last_error: str | None = None      # 绘制品炸过就留痕，验收靠它而非"看着没事"

        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self._fade_out)

        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        # 只连一次：在 _fade_out 里反复 connect 会攒出重复连接，disconnect 又只能拆一条
        self._anim.finished.connect(self._after_fade)

    # ---------- 对外 ----------
    def show_text(self, text: str, ms: int = 1000) -> None:
        if not text:
            return
        self._anim.stop()          # 上一条正在淡出时，动画每帧都会把新透明度按回 0
        self.setWindowOpacity(1.0)
        self.setFont(theme.font_ui(15, 600))
        fm = self.fontMetrics()
        w = min(560, max(124, fm.horizontalAdvance(text) + 52))
        h = 44
        # 超长消息（内核报错原文就是一条）按**中间**省略：首尾才是"哪条命令/哪个文件"，
        # 砍掉两头只留中间等于什么都没说；而 drawText 在固定矩形里默认会两头一起裁。
        self._text = fm.elidedText(text, Qt.ElideMiddle, max(40, w - 48))
        geo = self._target_rect(w, h)
        self.setGeometry(geo)
        self.show()
        self.raise_()
        self._hide.start(ms)
        self.update()

    def _target_rect(self, w: int, h: int) -> QRect:
        """贴在画面下 1/3 处（迅雷影音/PotPlayer 的位置习惯）。

        全用逻辑像素：setGeometry 吃的就是逻辑坐标，之前把尺寸乘了 dpr、原点没乘，
        在 150% 缩放机上气泡会跑到画面右下外面去。
        """
        g = self.anchor.mapToGlobal(self.anchor.rect().topLeft())
        aw, ah = self.anchor.width(), self.anchor.height()
        x = g.x() + (aw - w) // 2
        y = g.y() + int(ah * 0.62) - h // 2
        return QRect(x, y, w, h)

    def _fade_out(self) -> None:
        if not self.isVisible():
            return
        self._anim.stop()
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _after_fade(self) -> None:
        self.hide()

    # ---------- 绘制 ----------
    def paintEvent(self, _ev):
        # 绘制品也是"C++ 调进 Python"的入口：这里抛出任何异常，PyQt5 会 abort 整个进程
        # （实测 rc=127、stdout/stderr 全空）。曾经的写法是
        # `self.rect().adjusted(2, 2, -2, -2).toRect()`——QRect.adjusted 返回的还是 QRect，
        # 它没有 toRect()，于是"一冒气泡播放器就无声消失"。兜底 + 记账，坏只坏这一个气泡。
        try:
            self._paint()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _paint(self) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rf = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        r = rf.toRect()
        path = QPainterPath()
        path.addRoundedRect(rf, 12, 12)
        fill = QColor(theme.BG_RAISE)
        fill.setAlpha(242)
        p.fillPath(path, fill)
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(QColor(theme.TEXT))
        p.setFont(theme.font_ui(15, 600))
        p.drawText(r, Qt.AlignCenter, self._text)
        p.end()


class ScrubPreview(QWidget):
    """时间轴悬停/拖动预览：一帧缩略图 + 时间，钉在光标那一格的上方。

    同样是独立顶层窗口（视频区是原生子窗口，塞不进 Qt 控件），另外三条：
    - **鼠标穿透**：WA_TransparentForMouseEvents。浮层就贴在轨道上方，一旦它吃掉
      事件，拖动会中途落到浮层上而断掉。
    - **只在尺寸真的变了才 resize**：同一份片每格宽高恒定，逐次 move 只挪位置。
      分层窗口每次 resize 都要重算区域，150% 缩放下鼠标一扫就能看出抖。
    - **没图就整块不出现**：调用方（主窗口）拿不到帧时直接 hide，时间轴自己那颗
      时间气泡还在，所以降级是"少张图"，不是"少了反馈"。
    """

    W = 200            # 显示宽（逻辑像素）。源图 160 宽，再放大会糊得明显
    PAD = 6
    TEXT_H = 22

    def __init__(self, anchor: QWidget):
        super().__init__(anchor, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.anchor = anchor
        self.last_error: str | None = None
        self._pm: QPixmap | None = None
        self._text = ""
        self._iw, self._ih = self.W, 113
        self.setFixedSize(self.W + 2 * self.PAD, self._ih + self.TEXT_H + self.PAD)
        self.hide()

    # ---------- 对外 ----------
    def show_at(self, pm, text: str, center_x: int, bottom_y: int) -> None:
        """在 (center_x, bottom_y) 处贴出这一格；坐标是**全局逻辑像素**。

        调用方给的是"想要钉住的那个点"（轨道上的格心 x、轨道上沿 y），本类只负责
        把它换算成一个不超出画面的窗口位置——至于该显示第几秒，是主窗口的事。
        """
        if pm is None or pm.isNull() or pm.width() <= 0 or pm.height() <= 0:
            self.hide_preview()
            return
        self._pm, self._text = pm, text or ""
        iw = self.W
        ih = max(1, int(round(iw * pm.height() / pm.width())))
        w, h = iw + 2 * self.PAD, ih + self.TEXT_H + self.PAD
        self._iw, self._ih = iw, ih
        if (w, h) != (self.width(), self.height()):
            self.setFixedSize(w, h)
        x, y = self._place(w, h, center_x, bottom_y)
        self.move(x, y)
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def hide_preview(self) -> None:
        if self.isVisible():
            self.hide()

    def _place(self, w: int, h: int, cx: int, by: int) -> tuple[int, int]:
        """把窗口摆到画面里：水平夹在锚点内，竖直贴在给定上沿之上。"""
        g = self.anchor.mapToGlobal(QPoint(0, 0))
        lo_x = g.x() + 4
        hi_x = max(lo_x, g.x() + self.anchor.width() - w - 4)
        x = max(lo_x, min(int(cx - w / 2), hi_x))
        top = g.y() + 4
        y = max(top, int(by - h))
        return x, y

    # ---------- 绘制 ----------
    def paintEvent(self, _ev):
        # 同 Osd：绘制品里的异常会被 PyQt5 升级成 abort，必须在这里止住并记账
        try:
            self._paint()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _paint(self) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rf = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        body = QPainterPath()
        body.addRoundedRect(rf, 10, 10)
        fill = QColor(theme.BG_RAISE)
        fill.setAlpha(246)
        p.fillPath(body, fill)
        if self._pm is not None and not self._pm.isNull():
            img = QRectF(self.PAD, self.PAD, self._iw, self._ih)
            clip = QPainterPath()
            clip.addRoundedRect(img, 6, 6)
            p.setClipPath(clip)
            p.drawPixmap(img.toRect(), self._pm)
            p.setClipping(False)
        p.setFont(theme.font_mono(12, 600))
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(self.PAD, self.PAD + self._ih, self.width() - 2 * self.PAD, self.TEXT_H),
                   Qt.AlignCenter, self._text)
        # 描边放最后：填充式画一圈会把刚画好的帧盖掉一层白纱
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(body)
        p.end()


class ChromeHost(QWidget):
    """全屏控制条的**浮层宿主**：一条无边框、贴在主人窗口下沿的顶层窗。

    为什么条不能留在主窗的布局里（2026-10-20 全屏审查 §1/§5 的根因；用户那句
    "全屏之后画面只在屏幕中间一块、四周都是空出来的黑边"就是它）：
    mpv 的画面是主窗 HWND 的**原生子窗口**，原生子窗口永远画在父表面之上
    ⇒ "把条盖在画面上"用 Qt 子控件做不到，条只能占 QVBoxLayout 的一行；
    占行就把画面区压矮一条——1920×1080 的屏上压掉一条 76px 得到 1.96:1 的盒子，
    16:9 的片子只能**按高适配** ⇒ 左右各让出一条纯黑；唤醒/收起来回切那一行，
    画面还要跟着抖一次尺寸。
    能把东西放回画面**上面**的只有"另一个顶层窗口"（本文件 Osd/ScrubPreview 已实测可行），
    于是全屏时把 ControlsBar 搬进这里、退出时搬回布局——**同一个实例**搬来搬去，
    不造第二条控制条：两条就要同步播放状态、按钮图标、轨道位置，那才是新的坏法之源。

    三条与 Osd 不一样的地方，全是"这一条是要点的"带来的：
      - **不设** `WA_TransparentForMouseEvents`：按钮与轨道必须收得到点击。
      - **不上** `WA_TranslucentBackground`：底板不透明（贴屏幕下沿的通栏）。分层透明窗口
        按像素透明度做命中测试，边缘那几像素会点不中；而浮层压在视频上也不需要透出画面。
        底色由 theme 里 `#ChromeHost` 那条铺，别指望通用那条 `QWidget{background:transparent}`——
        它只给子控件兜底，顶层窗不铺就是没定义的表面对象。
      - **留着** `WindowDoesNotAcceptFocus` + `WA_ShowWithoutActivating`：晃一下鼠标唤出条，
        不许把键盘焦点从主窗抢走，否则空格/Esc 会随唤醒时断时续。
    """

    def __init__(self, owner: QWidget):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setObjectName("ChromeHost")
        self.owner_widget = owner
        self._bar: QWidget | None = None
        self.last_error: str | None = None      # 搬动/定位炸过就留痕（验收读它，不读"看着没事"）
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        # 显式压掉布局默认那条"minimumSize 由内容算"的约束：条的内容最小宽是 1040（十七颗按钮
        # 加轨道），宿主要是跟着它长，`place()` 说的"通栏＝主人窗口那么宽"就不作数——
        # 1366/1024 这种窄屏上浮层会比屏幕还宽，右边一截伸出桌面。主窗那边同理是靠
        # `setMinimumSize(880,560)` 压住的，这里照同一个办法。
        self.setMinimumSize(1, 1)
        self.hide()

    # ---------- 搬进 / 搬回 ----------
    def take(self, bar: QWidget) -> None:
        """把条接进来（幂等）。

        为什么显式 `removeWidget` 而不是赌"setParent 会顺手从旧布局里摘掉"：
        Qt 对"重设父窗口时旧布局会不会放手"没有可依赖的承诺（文档只保证 addWidget 那一头）。
        摘不干净的后果是**根布局还留着这一行**，画面照样被压矮一条，浮层白做——
        这一条在 tests/test_ui.py 里按 `root.count()` 数得出来。
        """
        if self._bar is bar:
            return
        old = bar.parentWidget()
        if old is not None and old is not self:
            lay = old.layout()
            if lay is not None:
                lay.removeWidget(bar)
        bar.setParent(self)
        self._lay.addWidget(bar)
        self._bar = bar

    def release(self) -> QWidget | None:
        """把条交回调用方（调用方负责 `addWidget` 回根布局，那一步会顺手改父窗口）。"""
        b = self._bar
        if b is None:
            return None
        self._lay.removeWidget(b)
        self._bar = None
        self.hide()
        return b

    def is_hosting(self) -> bool:
        return self._bar is not None

    # ---------- 定位与出现 ----------
    def place(self) -> None:
        """贴到主人窗口的下沿（通栏、按条自己的高度）。

        全用**逻辑像素**：`setGeometry` 吃的就是逻辑坐标。Osd 那边记过一次坏——
        尺寸乘了 dpr、原点没乘，150% 缩放机上整块浮层跑到画面外；这里一次都不乘。
        """
        if self._bar is None:
            return
        o = self.owner_widget
        h = max(1, self._bar.sizeHint().height())
        g = o.mapToGlobal(QPoint(0, 0))
        self.setGeometry(QRect(g.x(), g.y() + max(0, o.height() - h), max(1, o.width()), h))

    def reveal(self) -> None:
        """出现（先定位再 show：窗口尺寸刚变过时旧几何是上一拍的）。"""
        if self._bar is None:
            return
        self.place()
        self.show()
        self.raise_()

    def veil(self) -> None:
        if self.isVisible():
            self.hide()

    def event(self, ev):
        # 顶层窗也是"C++ 调进 Python"的入口：这里抛出去就是整进程 abort（rc=127、日志全空），
        # 而形态是"用户一晃鼠标播放器就没了"。兜住、留痕，坏只坏这一条浮层。
        try:
            return super().event(ev)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
