"""菜单工厂：轨道菜单、设备菜单、倍速菜单、更多菜单、循环菜单。

只造 QMenu 并给每个动作挂上 data，不亲自执行——执行要么需要内核当前状态
（track-list 的真相在 mpv 那边），要么是跨模块动作（打开文件、设置页），
归主窗口统一派发。data 约定：二元/三元组 (kind, ...)。
"""
from __future__ import annotations

from PyQt5.QtWidgets import QAction, QMenu, QWidget

from ..engine.player import MpvPlayer
from .scrubber import fmt_time

TRACK_PROP = {"audio": "aid", "video": "vid", "sub": "sid"}


def _human(entry: dict, idx: int) -> str:
    name = entry.get("title") or entry.get("lang") or entry.get("codec") or ""
    extra = f"（{entry['codec']}）" if entry.get("codec") else ""
    label = f"轨道 {entry.get('id', idx)}"
    return f"{label} {name}{extra}".strip() if name else f"{label} {extra}".strip()


def track_menu(player: MpvPlayer, kind: str, parent: QWidget) -> QMenu:
    m = QMenu(parent)
    m.setObjectName(f"TrackMenu_{kind}")
    tracks = [t for t in (player.prop("track-list", []) or []) if t.get("type") == kind]
    cur = player.prop(TRACK_PROP[kind])
    auto = QAction("自动选择", m)
    auto.setData((kind, "auto"))
    auto.setCheckable(True)
    auto.setChecked(cur == "auto" or cur is None)
    m.addAction(auto)
    if kind == "sub":
        off = QAction("关闭字幕", m)
        off.setData((kind, "no"))
        off.setCheckable(True)
        off.setChecked(cur in ("no", False))
        m.addAction(off)
    if not tracks:
        dis = QAction(f"没有{ {'audio': '音轨', 'video': '视频轨', 'sub': '字幕轨'}[kind] }", m)
        dis.setEnabled(False)
        m.addAction(dis)
    m.addSeparator()
    for i, t in enumerate(tracks):
        a = QAction(_human(t, i), m)
        a.setCheckable(True)
        a.setChecked(bool(t.get("selected")))
        a.setData((kind, t.get("id")))
        m.addAction(a)
    if kind == "sub":
        m.addSeparator()
        dual = QAction("双语字幕（第二条为副）", m)
        dual.setData(("secondary", "toggle"))
        dual.setCheckable(True)
        dual.setChecked(player.secondary_sub_id() is not None)
        m.addAction(dual)
        ext = QAction("加载外部字幕…", m)
        ext.setData(("file", "sub"))
        m.addAction(ext)
    if kind == "audio":
        m.addSeparator()
        ext = QAction("加载外部音频…", m)
        ext.setData(("file", "audio"))
        m.addAction(ext)
    return m


def device_menu(player: MpvPlayer, parent: QWidget) -> QMenu:
    """音频输出设备菜单。

    `audio-device-list` 不是订阅属性（内核不会主动推变化），所以这里现问一次同步请求。
    这正是 ipc.py 那条分工的用途：读线程里绝不发请求，UI 线程在用户已经点开门、
    马上要 exec() 阻塞的时候问一次，代价看不见。
    """
    m = QMenu(parent)
    m.setObjectName("DeviceMenu")
    cur = str(player.prop("audio-device", "auto") or "auto")
    if not player.running:
        dis = QAction("播放内核未启动", m)
        dis.setEnabled(False)
        m.addAction(dis)
        return m
    rows = player.audio_devices() or []
    if not rows:
        dis = QAction("没问到音频设备", m)
        dis.setEnabled(False)
        m.addAction(dis)
        return m
    for r in rows:
        name = str(r.get("name") or "")
        if not name:
            continue
        desc = str(r.get("description") or name)
        label = (desc[:46] + "…") if len(desc) > 46 else desc
        a = QAction(label, m)
        a.setCheckable(True)
        a.setChecked(name == cur)
        a.setData(("device", name))
        m.addAction(a)
    return m


def speed_menu(parent: QWidget, cur: float, speeds: list[float]) -> QMenu:
    m = QMenu(parent)
    for s in speeds:
        a = QAction(f"{s:g}x", m)
        a.setCheckable(True)
        a.setChecked(abs(s - cur) < 1e-3)
        a.setData(("speed", s))
        m.addAction(a)
    m.addSeparator()
    reset = QAction("复位 1x", m)
    reset.setData(("speed", 1.0))
    m.addAction(reset)
    return m


def loop_menu(player: MpvPlayer, parent: QWidget) -> QMenu:
    m = QMenu(parent)
    for label, data in (("不循环", ("loop", "no")), ("列表循环", ("loop", "inf")),
                        ("单曲循环", ("loop", "one"))):
        a = QAction(label, m)
        a.setCheckable(True)
        a.setData(data)
        m.addAction(a)
    m.addSeparator()
    ab = QAction("设定 A-B 区间（当前点为 A）", m)
    ab.setData(("ab", "a"))
    m.addAction(ab)
    ab2 = QAction("设定 B 并循环", m)
    ab2.setData(("ab", "b"))
    m.addAction(ab2)
    ab0 = QAction("清除 A-B 循环", m)
    ab0.setData(("ab", "clear"))
    m.addAction(ab0)
    return m


def chapter_menu(player: MpvPlayer, parent: QWidget) -> QMenu:
    """章节子菜单：上一章/下一章 + 全片章节表（标出当前章）。

    列表读的是内核缓存；没有章节时给一条不可点的说明，而不是一扇空菜单——
    "点开什么都没有"和"这支片子没有章节"是两句不同的话。
    """
    m = QMenu("章节", parent)
    m.setObjectName("ChapterMenu")
    if not player.running:
        # 没起内核时 chapter-list 必为空，但"还没开播"和"这支片子没有章节"是两句话：
        # 前者等片子起来就会变，后者等多久都一样。
        dis = QAction("播放内核还没启动", m)
        dis.setEnabled(False)
        m.addAction(dis)
        return m
    rows = player.chapters()
    if not rows:
        dis = QAction("这支片子没有章节", m)
        dis.setEnabled(False)
        m.addAction(dis)
        return m
    prev_a = QAction("上一章", m)
    prev_a.setData(("do", "prev_chapter"))
    m.addAction(prev_a)
    next_a = QAction("下一章", m)
    next_a.setData(("do", "next_chapter"))
    m.addAction(next_a)
    m.addSeparator()
    cur = player.chapter_pos()
    for i, c in enumerate(rows):
        title = str(c.get("title") or "").strip() or f"第 {i + 1} 章"
        try:
            t = float(c.get("time") or 0.0)
        except (TypeError, ValueError):
            t = 0.0
        a = QAction(f"{fmt_time(t)}  {title}", m)
        a.setCheckable(True)
        a.setChecked(i == cur)
        a.setData(("chapter", i))
        m.addAction(a)
    return m


def more_menu(cfg, parent: QWidget, extra: list[tuple] | None = None,
              submenus: list[tuple[str, QMenu]] | None = None) -> QMenu:
    """更多菜单：主窗口负责补自己的动作（打开文件、性能档位、统计等）。

    extra 的每一项是 `(文字, data)` 或 `(文字, data, 勾选)`：第三位为 None 时是普通项，
    给 True/False 就画成勾选项（现在只有"窗口置顶"用得上）。勾选只是**显示**，
    真正的状态与切换都在主窗口，菜单不另存一份。
    """
    m = QMenu(parent)
    items = [("打开文件…", ("file", "open")), ("打开文件夹…", ("file", "open_dir")),
             ("截图（含字幕）", ("shot", "with_sub")), ("截图（仅画面）", ("shot", "video")),
             None, ("复制当前媒体信息", ("info", "copy"))]
    if submenus:
        items += [None] + list(submenus)
    if extra:
        items += [None] + list(extra)
    for it in items:
        if it is None:
            m.addSeparator()
            continue
        label, data = it[0], it[1]
        if isinstance(data, QMenu):
            data.setTitle(label)
            data.setParent(m)        # 没有 Python 引用的子菜单会被 sip 连 C++ 对象一起收走
            m.addMenu(data)
            continue
        a = QAction(label, m)
        checked = it[2] if len(it) > 2 else None
        if checked is not None:
            a.setCheckable(True)
            a.setChecked(bool(checked))
        a.setData(data)
        m.addAction(a)
    return m
