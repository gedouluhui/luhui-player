"""矢量图标工厂：全部用 QPainterPath 在 24×24 单位坐标里画，不依赖任何图标字体。

（不用字体图标的理由：缺字形会静默丢字，跨系统表现不一致，且无法做主题色描边粗细控制。）
"""
from __future__ import annotations

from typing import Callable

from PyQt5.QtCore import QPointF, QRectF, Qt, QSize
from PyQt5.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

_S = 24.0  # 设计坐标


def _col(p: QPainter) -> QColor:
    """取"当前图标颜色"。

    不要写 p.pen().color()：_fill()（或直接 setPen(Qt.NoPen)）之后笔里的颜色已经换成
    NoPen 自带的黑色，后面再 _stroke 就画成黑线——深色底上等于那半边图标隐身。
    笔刷颜色是 _paint 开头设的、全文件没人改过颜色（logo 只把样式改成 NoBrush，
    颜色还在），所以从笔刷取才是那个"用户传进来的颜色"。
    """
    return QColor(p.brush().color())


def _poly(p: QPainter, pts, close=True):
    path = QPainterPath()
    path.moveTo(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(QPointF(x, y))
    if close:
        path.closeSubpath()
    p.drawPath(path)


def _fill(p: QPainter, pts):
    p.setPen(Qt.NoPen)
    _poly(p, pts)


def _stroke(p: QPainter, pts, w=2.0, close=False, round_cap=True):
    pen = QPen(_col(p), w)
    if round_cap:
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(QPointF(x, y))
    if close:
        path.closeSubpath()
    p.drawPath(path)


D: dict[str, Callable[[QPainter], None]] = {}


def reg(name):
    def deco(fn):
        D[name] = fn
        return fn
    return deco


@reg("play")
def _(p):
    _fill(p, [(7, 4.6), (20, 12), (7, 19.4)])


@reg("pause")
def _(p):
    for x in (7.5, 14.5):
        p.drawRoundedRect(QRectF(x, 5, 3.4, 14), 1.6, 1.6)


@reg("stop")
def _(p):
    p.drawRoundedRect(QRectF(6.5, 6.5, 11, 11), 2, 2)


@reg("prev")
def _(p):
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(5.6, 5, 2.4, 14), 1.2, 1.2)
    _fill(p, [(20, 5.4), (20, 18.6), (9.4, 12)])


@reg("next")
def _(p):
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(16, 5, 2.4, 14), 1.2, 1.2)
    _fill(p, [(4, 5.4), (4, 18.6), (14.6, 12)])


@reg("back10")
def _(p):
    _stroke(p, [(16.5, 6.5), (19, 9), (16.5, 11.5)], w=2.0)
    _stroke(p, [(19, 9), (13.5, 9), (9, 12.5)], w=2.0)
    pen = QPen(_col(p), 2.0, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawArc(QRectF(5, 7.5, 9, 9.5), 200 * 16, 120 * 16)
    p.setFont(_font(7.2, True))
    p.drawText(QRectF(6.2, 9.5, 7, 6), Qt.AlignCenter, "10")


@reg("fwd30")
def _(p):
    _stroke(p, [(7.5, 6.5), (5, 9), (7.5, 11.5)], w=2.0)
    _stroke(p, [(5, 9), (10.5, 9), (15, 12.5)], w=2.0)
    pen = QPen(_col(p), 2.0, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawArc(QRectF(10, 7.5, 9, 9.5), 340 * 16, -120 * 16)
    p.setFont(_font(7.2, True))
    p.drawText(QRectF(10.8, 9.5, 8, 6), Qt.AlignCenter, "30")


@reg("step_fwd")
def _(p):
    _fill(p, [(5, 5.5), (13, 12), (5, 18.5)])
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(15.5, 5.5, 2.6, 13), 1.2, 1.2)


@reg("step_back")
def _(p):
    _fill(p, [(19, 5.5), (11, 12), (19, 18.5)])
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(5.9, 5.5, 2.6, 13), 1.2, 1.2)


@reg("logo")
def logo(p: QPainter, rays: bool = True):
    """品牌标记：一株五叶芦荟的剪影（不是又一颗同心圆）。

    上一版是"实心核 + 细环 + 四道芒"，远看像通用设置图标；而"芦荟"这个名字（LuHui 与它同音）
    本来就有一个具体的形。五片叶子用负空间分开：18px 时仍能看出是一株植物，
    56px 的首屏上也不会散成一团。几何全部落在 0..24 内（超出会被 pixmap 裁掉）。
    `rays` 参数留着是为了兼容旧调用点，叶片剪影不需要再添放射线。
    """
    c = _col(p)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    leaves = (
        "M12 2.6 C14.9 6.4 15.4 10.3 12 14.2 C8.6 10.3 9.1 6.4 12 2.6 Z",
        "M11.2 13.2 C8.8 12.5 6.3 10.2 4.1 6.0 C8.1 6.9 10.6 9.2 11.9 12.5 Z",
        "M12.8 13.2 C15.2 12.5 17.7 10.2 19.9 6.0 C15.9 6.9 13.4 9.2 12.1 12.5 Z",
        "M11.0 14.3 C8.3 14.8 5.5 16.5 3.8 20.1 C7.4 19.7 10.1 18.1 11.5 15.1 Z",
        "M13.0 14.3 C15.7 14.8 18.5 16.5 20.2 20.1 C16.6 19.7 13.9 18.1 12.5 15.1 Z",
    )
    for d in leaves:
        p.drawPath(_leaf_path(d))


def _leaf_path(d: str) -> QPainterPath:
    """把上面那五条固定的 SVG 子路径解析成 QPainterPath（只认 M/C/Z 与空格/逗号）。

    固定字符串不引入 SVG 依赖；写成小解析器是因为五片叶子各抄一遍 moveTo/cubicTo
    会把"叶形"埋在十几行坐标里，改一次形状要同时改五处。
    """
    import re
    path = QPainterPath()
    tokens = re.findall(r"[MCZ]|-?\d+(?:\.\d+)?", d)
    i = 0
    cmd = ""
    while i < len(tokens):
        t = tokens[i]
        if t in ("M", "C", "Z"):
            cmd = t
            i += 1
            continue
        if cmd == "M":
            path.moveTo(float(tokens[i]), float(tokens[i + 1]))
            i += 2
            cmd = "L"
        elif cmd == "C":
            path.cubicTo(float(tokens[i]), float(tokens[i + 1]),
                         float(tokens[i + 2]), float(tokens[i + 3]),
                         float(tokens[i + 4]), float(tokens[i + 5]))
            i += 6
            cmd = "L"
        elif cmd == "Z":
            path.closeSubpath()
        else:
            i += 1
    if not path.isEmpty():
        path.closeSubpath()
    return path


def _font(px, bold=False):
    from PyQt5.QtGui import QFont
    f = QFont("Segoe UI", -1)
    f.setPixelSize(int(px))
    f.setBold(bold)
    return f


@reg("volume_high")
def _(p):
    _fill(p, [(4, 9.5), (8, 9.5), (12.5, 5.5), (12.5, 18.5), (8, 14.5), (4, 14.5)])
    for r, arc in ((3.2, 70), (5.6, 90)):
        pen = QPen(_col(p), 1.7, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(12.5 - r / 2 + 1.5, 12 - r / 2, r, r), -int(arc / 2) * 16, int(arc) * 16)


@reg("volume_low")
def _(p):
    _fill(p, [(4, 9.5), (8, 9.5), (12.5, 5.5), (12.5, 18.5), (8, 14.5), (4, 14.5)])
    pen = QPen(_col(p), 1.7, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    r = 3.2
    p.drawArc(QRectF(12.5 - r / 2 + 1.5, 12 - r / 2, r, r), -35 * 16, 70 * 16)


@reg("volume_mute")
def _(p):
    _fill(p, [(4, 9.5), (8, 9.5), (12.5, 5.5), (12.5, 18.5), (8, 14.5), (4, 14.5)])
    _stroke(p, [(15, 9.5), (20.5, 15)], w=1.9)
    _stroke(p, [(20.5, 9.5), (15, 15)], w=1.9)


@reg("speed")
def _(p):
    pen = QPen(_col(p), 1.9, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawArc(QRectF(4.5, 5.5, 15, 15), 200 * 16, 140 * 16)
    p.setPen(Qt.NoPen)
    _fill(p, [(12, 12), (15.5, 8.6), (16.8, 10)])


@reg("playlist")
def _(p):
    for i, y in enumerate((7, 11.5)):
        _stroke(p, [(4, y), (15, y)], w=1.9)
    _stroke(p, [(4, 16), (11, 16)], w=1.9)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(16.2, 12.2, 4.2, 4.2))
    _stroke(p, [(20.2, 14.2), (20.2, 8.2)], w=1.7)


@reg("subs")
def _(p):
    pen = QPen(_col(p), 1.8)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawRoundedRect(QRectF(3, 5.5, 18, 13), 3, 3)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(6, 11.5, 5.4, 2.2), 1.1, 1.1)
    p.drawRoundedRect(QRectF(13.2, 11.5, 4.8, 2.2), 1.1, 1.1)


@reg("audio")
def _(p):
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(5.2, 13.2, 5.4, 5.4))
    _stroke(p, [(10.6, 15.9), (10.6, 6.2), (18.5, 8)], w=1.9)


@reg("fullscreen")
def _(p):
    for pts in ([[4, 9], [4, 4], [9, 4]], [[15, 4], [20, 4], [20, 9]],
               [[20, 15], [20, 20], [15, 20]], [[9, 20], [4, 20], [4, 15]]):
        _stroke(p, pts, w=2.0)


@reg("fullscreen_exit")
def _(p):
    for pts in ([[4, 10], [10, 10], [10, 4]], [[14, 20], [14, 14], [20, 14]],
               [[20, 10], [14, 10], [14, 4]], [[4, 14], [10, 14], [10, 20]]):
        _stroke(p, pts, w=2.0)


@reg("camera")
def _(p):
    pen = QPen(_col(p), 1.8)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawRoundedRect(QRectF(3, 7.5, 18, 12), 3, 3)
    _stroke(p, [(8.5, 7.5), (10, 5.2), (14, 5.2), (15.5, 7.5)], w=1.8, close=False)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(9.2, 10.5, 5.6, 5.6))


@reg("more")
def _(p):
    for x in (6, 12, 18):
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(x - 1.5, 10.5, 3, 3))


@reg("close")
def _(p):
    _stroke(p, [(6, 6), (18, 18)], w=1.7)
    _stroke(p, [(18, 6), (6, 18)], w=1.7)


@reg("minimize")
def _(p):
    _stroke(p, [(6, 12.5), (18, 12.5)], w=1.7)


@reg("maximize")
def _(p):
    pen = QPen(_col(p), 1.6)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawRect(QRectF(6.5, 6.5, 11, 11))


@reg("restore")
def _(p):
    pen = QPen(_col(p), 1.6)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawRect(QRectF(6.5, 8.5, 9, 9))
    _stroke(p, [(9, 6.5), (17.5, 6.5), (17.5, 15), (15.5, 15)], w=1.6, close=True)


@reg("pin")
def _(p):
    _fill(p, [(9, 3.5), (15, 3.5), (14.2, 9), (17, 11.5), (7, 11.5), (9.8, 9)])
    _stroke(p, [(12, 11.5), (12, 20)], w=1.9)


@reg("folder")
def _(p):
    pen = QPen(_col(p), 1.8)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    _stroke(p, [(3.5, 7), (9, 7), (10.6, 9), (20.5, 9), (20.5, 18), (3.5, 18), (3.5, 7)], w=1.8, close=True)


@reg("search")
def _(p):
    pen = QPen(_col(p), 1.9, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawEllipse(QRectF(5, 5, 9.5, 9.5))
    _stroke(p, [(14.2, 14.2), (19, 19)], w=2.1)


@reg("history")
def _(p):
    # 表盘 + 两根指针＝"最近"。不画回转箭头：16px 上那枚小箭头会和表盘挤成一团，
    # 而这颗按钮的完整意思由 tooltip 与状态行那四个字承担。
    pen = QPen(_col(p), 1.8, Qt.SolidLine, Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawEllipse(QRectF(4.6, 4.6, 14.8, 14.8))
    _stroke(p, [(12, 7.9), (12, 12.2), (15.1, 13.8)], w=1.8)


@reg("chevron_down")
def _(p):
    _stroke(p, [(6, 9.5), (12, 15), (18, 9.5)], w=1.9)


@reg("chevron_right")
def _(p):
    _stroke(p, [(9.5, 6), (15, 12), (9.5, 18)], w=1.9)


@reg("check")
def _(p):
    _stroke(p, [(5, 12.6), (9.8, 17.4), (19, 7.6)], w=2.2)


@reg("shuffle")
def _(p):
    _stroke(p, [(4, 17), (8, 17), (16, 7), (20, 7)], w=1.9)
    _stroke(p, [(4, 7), (8, 7), (16, 17), (20, 17)], w=1.9)
    _stroke(p, [(17.5, 4.5), (20, 7), (17.5, 9.5)], w=1.9)
    _stroke(p, [(17.5, 14.5), (20, 17), (17.5, 19.5)], w=1.9)


@reg("repeat")
def _(p):
    pen = QPen(_col(p), 1.9, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawArc(QRectF(5, 6, 14, 12), 40 * 16, 240 * 16)
    _stroke(p, [(6.4, 4.2), (8.6, 6.4), (6.2, 8.8)], w=1.9, close=True)


@reg("repeat_one")
def _(p):
    D["repeat"](p)
    p.setFont(_font(8.5, True))
    p.setPen(QPen(_col(p)))
    p.drawText(QRectF(9, 8, 6, 8), Qt.AlignCenter, "1")


@reg("loop_off")
def _(p):
    pen = QPen(_col(p), 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawArc(QRectF(5, 6, 14, 12), 40 * 16, 240 * 16)
    _stroke(p, [(4.5, 4.5), (19.5, 19.5)], w=1.9)


@reg("clock")
def _(p):
    pen = QPen(_col(p), 1.8)
    p.setPen(pen)
    p.drawEllipse(QRectF(4, 4, 16, 16))
    _stroke(p, [(12, 8), (12, 12), (15.5, 14)], w=1.9)


@reg("ab_loop")
def _(p):
    pen = QPen(_col(p), 1.8)
    p.setPen(pen)
    p.drawArc(QRectF(4.5, 7, 15, 10), 30 * 16, 300 * 16)
    _stroke(p, [(7.5, 5.5), (7.5, 9)], w=1.8)
    _stroke(p, [(16.5, 15), (16.5, 18.5)], w=1.8)
    p.setFont(_font(6.6, True))
    p.drawText(QRectF(3.5, 9.2, 8, 6), Qt.AlignCenter, "A")
    p.drawText(QRectF(13.5, 9.2, 8, 6), Qt.AlignCenter, "B")


@reg("plus")
def _(p):
    _stroke(p, [(12, 5.5), (12, 18.5)], w=2.0)
    _stroke(p, [(5.5, 12), (18.5, 12)], w=2.0)


@reg("info")
def _(p):
    pen = QPen(_col(p), 1.8)
    p.setPen(pen)
    p.drawEllipse(QRectF(4, 4, 16, 16))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(11.1, 7.4, 1.9, 1.9))
    p.drawRoundedRect(QRectF(11.1, 10.8, 1.9, 6), 0.9, 0.9)


@reg("sliders")
def _(p):
    """三根滑轨 + 三个错开的旋钮＝"调节"。旋钮位置故意各不相同，
    免得远看成了"列表/汉堡菜单"（那才是三条等长平行线）。"""
    for y, kx in ((7.5, 15.5), (12.0, 8.0), (16.5, 17.5)):
        _stroke(p, [(4.2, y), (19.8, y)], w=1.7)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(kx - 2.2, y - 2.2, 4.4, 4.4))


@reg("settings")
def _(p):
    pen = QPen(_col(p), 1.8)
    p.setPen(pen)
    p.drawEllipse(QRectF(9, 9, 6, 6))
    import math
    for i in range(8):
        a = i * math.pi / 4
        x1, y1 = 12 + 6.2 * math.cos(a), 12 + 6.2 * math.sin(a)
        x2, y2 = 12 + 8.6 * math.cos(a), 12 + 8.6 * math.sin(a)
        _stroke(p, [(x1, y1), (x2, y2)], w=1.7)


@reg("window")
def _(p):
    pen = QPen(_col(p), 1.8)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawRoundedRect(QRectF(3.5, 5, 17, 14), 3, 3)
    _stroke(p, [(3.5, 9.5), (20.5, 9.5)], w=1.5)


def _screen_dpr() -> float:
    """当前屏幕的设备像素比（1.0–4.0，两位小数）。

    不能写死 2.0：150% 缩放的机器上 Qt 会报 2.0，但 125% 是 1.25、100% 是 1.0；
    按 2.0 画出去的位图在 1.0 的屏上被缩一半（线条发虚），在 2.5 的屏上又不够密。
    取不到屏幕（无头/初始化极早）就退回 1.0——比猜测一个高倍率诚实。
    """
    try:
        scr = QGuiApplication.primaryScreen()
        dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
    except Exception:
        dpr = 1.0
    if not (dpr >= 1.0):
        dpr = 1.0
    return round(min(dpr, 4.0), 2)


def _paint(name: str, size: int, color: str, dpr: float | None = None) -> QPixmap:
    if dpr is None:
        dpr = _screen_dpr()
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.scale(size / _S, size / _S)
    c = QColor(color)
    p.setPen(QPen(c))
    p.setBrush(c)
    fn = D.get(name)
    if fn is None:
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(9, 9, 6, 6))
    else:
        fn(p)
    p.end()
    return pm


def pixmap(name: str, size: int = 20, color: str = theme.TEXT) -> QPixmap:
    return _paint(name, size, color)


# 同一枚图标在一次会话里会被要很多遍：播放/暂停每翻一次面、音量每动一格、循环切档、
# 全屏进出，还有整条工具栏在换片时重贴。每要一次就 QPainter 画 3–4 张位图（Normal Off/On、
# Disabled，带悬停色时还有 Active），这些画出来的字节一模一样。缓存的是不可变句柄：
# 20–28px、dpr 2 的位图约 10KB 一张，封顶 128 项 ≈ 一两 MB，远小于重画带来的卡顿。
# key 里带上当时的颜色串：主题一换，旧图标自然落在新 key 之外，不会把上一套配色发出去。
ICON_CACHE_MAX = 128
_ICON_CACHE: dict[tuple, QIcon] = {}
_ICON_BUILT = 0          # 真画过几枚（判据读它；tests/test_responsiveness.py）
_ICON_HITS = 0           # 命中缓存几次


def cache_stats() -> dict:
    return {"built": _ICON_BUILT, "hits": _ICON_HITS, "size": len(_ICON_CACHE)}


def icon(name: str, size: int = 20, color: str = theme.TEXT, hover_color: str | None = None) -> QIcon:
    global _ICON_BUILT, _ICON_HITS
    # DPR 进键：同一枚图标在 100% 与 150% 屏上该是不同密度的位图，混用会让
    # 其中一档发虚。窗口换屏时下一次取用自然落在另一档键上，不需要手动清缓存。
    dpr = _screen_dpr()
    key = (name, int(size), str(color), str(hover_color), dpr)
    hit = _ICON_CACHE.get(key)
    if hit is not None:
        _ICON_HITS += 1
        return hit
    ic = QIcon()
    ic.addPixmap(_paint(name, size, color, dpr), QIcon.Normal, QIcon.Off)
    ic.addPixmap(_paint(name, size, color, dpr), QIcon.Normal, QIcon.On)
    if hover_color:
        ic.addPixmap(_paint(name, size, hover_color, dpr), QIcon.Active, QIcon.Off)
    ic.addPixmap(_paint(name, size, theme.TEXT_FAINT, dpr), QIcon.Disabled, QIcon.Off)
    _ICON_BUILT += 1
    if len(_ICON_CACHE) >= ICON_CACHE_MAX:
        _ICON_CACHE.pop(next(iter(_ICON_CACHE)))     # 插入序＝FIFO，丢最旧的一枚
    _ICON_CACHE[key] = ic
    return ic


def size_hint(px: int) -> QSize:
    return QSize(px, px)
