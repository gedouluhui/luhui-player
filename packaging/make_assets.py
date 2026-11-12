"""生成应用图标 packaging/assets/app.ico（与界面内 logo 同一套几何，不是另画一个）。

为什么用代码画而不是找一张图：图标必须和 title_bar 里那个 18px 的 mark 同源，
否则桌面/任务栏图标和窗口左上角长得不一样，观感上就是"两套品牌"。所以这里不复制
几何，直接调 icons.logo() 画，只负责加底、按档缩放。

小尺寸减细节：16/24 档不画四道射线（1.5 单位粗的线缩到 16px 只剩 1 个像素，糊成一团），
并把 mark 放大到占满画布——图标在小档"看不清楚"多半不是分辨率问题，是留白太多。

用法：python packaging/make_assets.py
产物：packaging/assets/app.ico（16/24/32/48/64/128/256 七档，每档各自画）
"""
from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.ui import theme  # noqa: E402
from app.ui.icons import logo  # noqa: E402   # 唯一的那份 mark 几何

ASSETS = os.path.join(HERE, "assets")
SIZES = (16, 24, 32, 48, 64, 128, 256)
GLYPH_R = 10.6          # icons.logo 里 mark 的最外半径（射线末端），单位是 24 框


def draw_tile(px: int) -> QImage:
    """画一档：深色圆角底 + 主色 mark，mark 直径占画布 ~80%。"""
    img = QImage(px, px, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    if px >= 32:        # 小档不铺底：16px 的圆角方块加同心圆只会糊成一坨
        s = px / 24.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.BG))
        p.drawRoundedRect(QRectF(0.4 * s, 0.4 * s, (24 - 0.8) * s, (24 - 0.8) * s),
                          5.6 * s, 5.6 * s)
        p.setBrush(QColor(255, 255, 255, 16))       # 顶部那层薄高光，和面板一致
        p.drawRoundedRect(QRectF(0.4 * s, 0.4 * s, (24 - 0.8) * s, (24 - 0.8) * s),
                          5.6 * s, 5.6 * s)

    u = (0.40 * px) / GLYPH_R                       # mark 最外沿落在半径 40% 画布处
    p.save()
    p.setPen(QPen(QColor(theme.ACCENT), 1.0))       # logo() 从当前 pen 取色
    p.setBrush(QColor(theme.ACCENT))
    p.translate(px / 2.0, px / 2.0)
    p.scale(u, u)
    p.translate(-12.0, -12.0)                       # logo 画在 0..24 框里，中心是 12,12
    logo(p, rays=px >= 32)
    p.restore()
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    from PyQt5.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


def write_ico(path: str, tiles: list[QImage]) -> int:
    """手写 .ico 容器：每个尺寸装自己那一档的 PNG，不让 PIL 去重采样 256。

    为什么不用 PIL 一把梭：小尺寸要减细节、改留白（见模块开头），而 PIL 的 sizes
    是从一张大图 resize 出来的，等于把我按档画的几何又抹平了。
    PNG-in-ICO 是 Vista 之后的标准写法，资源管理器/任务栏都认。
    """
    import struct
    n = len(tiles)
    body = b""
    entries = b""
    off = 6 + 16 * n
    for img in tiles:
        data = png_bytes(img)
        entries += struct.pack("<BBBBHHII", img.width() % 256, img.height() % 256,
                               0, 0, 1, 32, len(data), off)
        body += data
        off += len(data)
    blob = struct.pack("<HHH", 0, 1, n) + entries + body
    with open(path, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    return len(blob)


def main() -> int:
    os.makedirs(ASSETS, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    tiles = [draw_tile(px) for px in SIZES]
    png = os.path.join(ASSETS, "app-256.png")
    tiles[-1].save(png)
    ico = os.path.join(ASSETS, "app.ico")
    size = write_ico(ico, tiles)
    print(f"[ok] 写出 {ico}（{size} 字节，{len(tiles)} 档：{SIZES}）")
    print(f"[ok] 单档预览 {png}")

    # 回读校验：容器解析得开、七档都在、每档 load 得动（坏一档这里就抛）
    try:
        from PIL import Image
    except Exception as exc:
        print(f"[warn] 没有 PIL，跳过回读校验（图标本身已写出）：{exc}")
        return 0
    got = set(Image.open(ico).info.get("sizes", set()))
    want = {(n, n) for n in SIZES}
    ok = want <= got
    print(f"{'[ok]' if ok else '[FAIL]'} 回读 .ico：档位 {sorted(got)}")
    for n in SIZES:
        one = Image.open(ico)
        one.size = (n, n)
        one.load()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
