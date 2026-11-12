"""窗口几何的纯规划器：换屏 / 拔掉显示器 / 改分辨率之后，把窗口收回看得见的地方。

为什么不直接 `restoreGeometry()`：Qt 自己那份字节串确实处理了"最大化/全屏"和一部分
屏外校正，但它把"哪块屏、可用区多高"一起烘进了私有格式，出了偏差只能整串重来，
也没法在测试里逐条喂"左屏负坐标 / 上沿在屏幕外 / 显示器被拔掉 / 分辨率从 4K 降到
1080p"这些已知坏样本。这里只存五个整数（x/y/w/h/max），规则全是纯函数——能离线
逐格验证，真机上出问题也能一眼看出是哪一格判错。

规则（顺序即优先级）：
  1. 没有存档 / 存档不是五个能用的整数 → 返回 None，调用方用 `default_rect()` 居中。
  2. 按**与存档矩形的交集面积**挑屏：窗口原来主要在哪块屏，就尽量留在哪块屏；
     完全不在任何屏上（显示器被拔了）→ 落到主屏正中。
  3. 尺寸夹进所选屏的可用区（任务栏之外）。屏幕比产品最小尺寸还小时，屏幕优先——
     宁可比下限小，也不许把控制条放到屏幕外。
  4. 位置**整窗夹进可用区**：启动时窗口必须完整可见，不接受"只露一条边"。
     多屏时窗口会贴到交集最大的那块屏里，仍保留左屏负坐标。
"""
from __future__ import annotations

from typing import Any


def _int(value: Any) -> int | None:
    """只认有限数值。bool 是 int 的子类，`True` 不该被当成 1 像素宽的窗口。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if -10_000_000 < n < 10_000_000 else None


def _rect(raw: dict | None) -> tuple[int, int, int, int, bool] | None:
    if not isinstance(raw, dict):
        return None
    x, y, w, h = (_int(raw.get(k)) for k in ("x", "y", "w", "h"))
    if x is None or y is None or w is None or h is None:
        return None
    if w < 1 or h < 1:
        return None
    return x, y, w, h, bool(raw.get("max"))


def _area(a: tuple[int, int, int, int], s: tuple[str, int, int, int, int]) -> int:
    ax, ay, aw, ah = a
    _, sx, sy, sw, sh = s
    dx = min(ax + aw, sx + sw) - max(ax, sx)
    dy = min(ay + ah, sy + sh) - max(ay, sy)
    return max(0, dx) * max(0, dy)


def _usable(screens: Any) -> list[tuple[str, int, int, int, int]]:
    """把外部给的屏表收成五个能用整数一项；坏项整项丢掉。"""
    out = [(str(s[0]), *(_int(s[i]) for i in range(1, 5))) for s in (screens or [])
           if isinstance(s, (tuple, list)) and len(s) >= 5]
    return [s for s in out if s[1] is not None and s[2] is not None
            and s[3] is not None and s[4] is not None and s[3] > 0 and s[4] > 0]


def default_rect(screens: list[tuple[str, int, int, int, int]],
                 default_size: tuple[int, int] = (1180, 720)) -> tuple[int, int, int, int] | None:
    """没有存档时的出厂摆法：按主屏可用区居中，屏幕放不下就缩到屏幕大小。

    老代码返回 None、交给 Windows 的默认摆放（左上角 + 级联）：在 150% 缩放机上
    窗口一开就贴着屏幕上沿，尺寸再大一点就有一截落到屏幕外。首次启动也必须是
    用户要的样子，所以出厂位置也走同一条"算好再放"的路。
    """
    usable = _usable(screens)
    if not usable:
        return None
    _, sx, sy, sw, sh = usable[0]
    w = min(max(1, int(default_size[0])), sw)
    h = min(max(1, int(default_size[1])), sh)
    return sx + (sw - w) // 2, sy + (sh - h) // 2, w, h


def plan(saved: Any, screens: list[tuple[str, int, int, int, int]],
         default_size: tuple[int, int] = (1180, 720),
         min_size: tuple[int, int] = (880, 560)) -> tuple[int, int, int, int, bool] | None:
    """算出该把窗口摆成什么矩形。`screens` 每项是 (名字, x, y, 宽, 高)，第一项是主屏。

    返回 (x, y, w, h, 是否最大化)；没有可用存档时返回 None（调用方用 default_rect）。
    """
    rect = _rect(saved if isinstance(saved, dict) else None)
    usable = _usable(screens)
    if rect is None or not usable:
        return None
    x, y, w, h, maxed = rect
    min_w, min_h = max(1, int(min_size[0])), max(1, int(min_size[1]))

    # ① 挑屏：交集最大；完全不在任何屏上时用主屏（第一项）居中。
    best = max(usable, key=lambda s: _area((x, y, w, h), s))
    on_screen = _area((x, y, w, h), best) > 0
    if not on_screen:
        best = usable[0]
    _, sx, sy, sw, sh = best

    # ② 尺寸：先保最小尺寸，再收进可用区；屏幕比最小尺寸还小时屏幕优先——
    #    否则窗口一定会有一截落到屏幕外（用户看到的就是"打开了但下面被切掉"）。
    w = min(max(w, min_w), sw)
    h = min(max(h, min_h), sh)

    # ③ 位置：整窗夹进可用区。窗口在屏外（显示器被拔掉）时居中回来。
    if on_screen:
        x = min(max(x, sx), sx + sw - w)
        y = min(max(y, sy), sy + sh - h)
    else:
        x = sx + (sw - w) // 2
        y = sy + (sh - h) // 2
    return x, y, w, h, bool(maxed)


def from_qt(app: Any) -> list[tuple[str, int, int, int, int]]:
    """把 QApplication 的屏列表转成 plan() 的入参；主屏永远在第一项。

    放在这里而不是 main_window：屏枚举只有这一处，`plan` 的测试不需要 QApplication，
    真窗口那一路也能在离线平台上用同一份数据形状走一遍。
    """
    primary = app.primaryScreen()
    out: list[tuple[str, int, int, int, int]] = []
    seen: set[int] = set()

    def add(screen) -> None:
        if screen is None or id(screen) in seen:
            return
        seen.add(id(screen))
        g = screen.availableGeometry()
        out.append((str(screen.name() or ""), g.x(), g.y(), g.width(), g.height()))

    add(primary)
    for screen in app.screens():
        add(screen)
    return out
