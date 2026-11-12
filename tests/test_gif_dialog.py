"""截图工坊对话框验收：真对话框、真配置落盘，但不碰引擎也不抓屏。

用法：
    python tests/test_gif_dialog.py            # 正常验收：全部判据必须 PASS
    python tests/test_gif_dialog.py --falsify  # 证伪：把实现换成"常见错误写法"，对应判据必须翻红

为什么这份能脱离播放器跑：对话框与内核之间只有**一个接缝** `state_of()`——一个回
{video,pos,duration,ab,sub} 字典的回调（主窗口那边接的是 player 的订阅缓存）。这里给它
一个假字典，整条链（起点/A-B/字幕可用性/区间夹住/落盘键/回流槽）就全能跑完：不起 mpv、
不需要真片子、不需要屏幕。这一条是当初把状态读取留在主窗口而不是对话框里的原因。

分工：`tests/test_gif.py` 证明"引擎算出来的命令与产物是对的"，这份证明"界面上的数确实
按那些规则收口、并且存得下来"。GIF 的像素好不好看得由人眼判（见 docs 的验收清单）。

三条老规矩（本仓所有验收脚本共用）：
  · 判据不许恒真 —— 每条都由 --falsify 里对应的坏桩证伪；
  · 判据自己不许崩 —— run()/falsify() 把每条包在 try/except BaseException 里；
  · 坏样本要能点名 —— 详情里写清"哪个控件/哪个键没做到"。

还有一条只在真点按钮时才成立：`click()` 会走 Qt 的槽投递，槽里抛出**不返回 traceback，
进程当场没了**（rc=127 + 日志断在半路）。所以这里只有"接线是否正确"用 click 验，其余
一律直接调方法，让抛出变成一条能读判据的 FAIL。看到 rc=127 就是这个形状被踩中了。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="gelu-gifdlg-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")          # 必须在 import config 之前
sys.path.insert(0, ROOT)

from PyQt5.QtCore import Qt                                   # noqa: E402
from PyQt5.QtGui import QKeySequence                          # noqa: E402
from PyQt5.QtWidgets import QApplication, QShortcut           # noqa: E402

from app import config as cfgmod                             # noqa: E402
from app.engine import gif                                   # noqa: E402
from app.ui import keymap as KM                              # noqa: E402
from app.ui.gif_dialog import GifDialog                      # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)

SUB_FILE = os.path.join(TMP, "zh.srt")
with open(SUB_FILE, "w", encoding="utf-8") as _fh:
    _fh.write("1\n00:00:01,000 --> 00:00:02,000\n喂\n\n")

BASE = {"video": r"X:\movies\大侦探.mp4", "pos": 1234.5, "duration": 3600.0,
        "ab": (None, None), "sub": ""}


def cfg_file() -> str:
    return os.path.join(TMP, "cfg", "config.json")


def fresh_cfg(raw: dict | None = None) -> cfgmod.Config:
    """每判一份新配置：既避开上一判留下的键，也顺带验 Config 的落盘/读回。"""
    p = cfg_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.isfile(p):
        os.remove(p)
    c = cfgmod.Config(p)
    if raw:
        for k, v in raw.items():
            c[k] = v
        c.save()
    return c


def on_disk() -> dict:
    try:
        with open(cfg_file(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class FakeState:
    """`state_of()` 的替身：测试里改 .data 就等于"片子换了/位置动了"。"""

    def __init__(self, **kw):
        self.data = dict(BASE)
        self.data.update(kw)

    def __call__(self) -> dict:
        return self.data


class BoomState:
    """取状态就抛：对话框必须留在旧值上，绝不能把进程带走。"""

    def __call__(self) -> dict:
        raise RuntimeError("IPC 管道断了")


def dlg_for(cfg=None, state=None) -> tuple[GifDialog, FakeState]:
    st = state if state is not None else FakeState()
    return GifDialog(cfg or fresh_cfg(), st), st


def spy(d: GifDialog) -> dict:
    got = {"export": [], "cancel": [], "burst": [], "reveal": []}
    d.sig_export.connect(lambda sp: got["export"].append(dict(sp)))
    d.sig_cancel.connect(lambda: got["cancel"].append(True))
    d.sig_burst.connect(lambda n, g: got["burst"].append((n, g)))
    d.sig_reveal.connect(lambda p: got["reveal"].append(p))
    return got


# ============================================================ 判据
def c_spec_clamps_range() -> tuple[bool, str]:
    """spec() 交出去的区间必须已经夹过——用的是引擎里同一个 clamp_range。

    两个方向都要判：贴片尾要**夹**（否则导出一个越过片尾的区间），时长未知要**不夹**
    （否则"内核还没报时长"会变成"从起点导 0.5 秒"，把用户想要的东西悄悄吃掉）。
    第二腿刻意在改完 duration 之后**不刷界面**：控件的 maximum 这时还是旧的（30 秒），
    夹住这件事只能靠 spec() 自己做。少这一句，"只靠控件夹"的写法就能骗过判据
    （证伪表里 G1 就是这条坏写法，它必须让这一腿变红）。
    """
    cfg = fresh_cfg()
    st = FakeState(pos=0.0, duration=0.0)
    d, _ = dlg_for(cfg, st)
    d.spin_start.setValue(3595.0)
    d.spin_dur.setValue(20.0)
    sp1 = d.spec()
    unknown = (sp1["start"], sp1["dur"])
    st.data["duration"] = 3600.0                  # 内核随后把片长报上来，界面一个字没动
    sp2 = d.spec()
    tail = (sp2["start"], sp2["dur"])
    return (unknown == (3595.0, 20.0) and tail == (3595.0, 5.0)
            and sp2["video"] == BASE["video"]), \
        f"时长未知={unknown}（该照填 20 秒） 贴片尾={tail}（该夹成 5 秒） 文件名={sp2['video']!r}"


def c_dur_maximum_follows_remaining() -> tuple[bool, str]:
    """时长框的上界跟着"离片尾还剩多少"走，文案在导不出时必须点名原因。"""
    st = FakeState()
    d, _ = dlg_for(fresh_cfg(), st)
    d.spin_start.setValue(3599.9)                   # 只剩 0.1 秒，比最短时长还短
    d._repaint_note()
    m1, t1 = d.spin_dur.maximum(), d.lbl_range.text()
    d.spin_start.setValue(100.0)
    d._repaint_note()
    m2, t2 = d.spin_dur.maximum(), d.lbl_range.text()
    st.data["duration"] = 0.0
    d._repaint_note()
    m3, t3 = d.spin_dur.maximum(), d.lbl_range.text()
    return (abs(m1 - gif.MIN_DURATION) < 0.01 and "导不出" in t1
            and abs(m2 - gif.MAX_DURATION) < 0.01
            and "还剩 3500" in t2 and m3 == gif.MAX_DURATION and "时长未知" in t3), \
        f"贴尾 max={m1}（该 {gif.MIN_DURATION}） 文案={t1!r}；中段 max={m2} 文案={t2!r}；" \
        f"未知 max={m3} 文案={t3!r}"


def c_btn_here_uses_pos() -> tuple[bool, str]:
    """「用当前位置」把播放位置搬进起点框，并顺手把上界/文案刷新。"""
    st = FakeState(pos=77.25)
    d, _ = dlg_for(fresh_cfg(), st)
    d.spin_start.setValue(0.0)
    d.btn_here.click()
    return (abs(d.spin_start.value() - 77.25) < 0.005 and "还剩" in d.lbl_range.text()), \
        f"点完起点={d.spin_start.value()}（该 77.25） 文案={d.lbl_range.text()!r}"


def c_ab_gate_and_span() -> tuple[bool, str]:
    """A-B 两个点都齐了才能用；用了就按**排序后**的区间填（倒着打点也得对）。

    pos 故意取 90.0 = A 点：refresh() 每次都会把起点搬回播放位置，若 pos 取别的值，
    "清空 A-B 后数值不动"这半判据会被 refresh 自己改掉的值顶掉（测出来的就是假红）。
    """
    st = FakeState(pos=90.0)
    d, _ = dlg_for(fresh_cfg(), st)
    off = d.btn_ab.isEnabled()
    st.data["ab"] = (90.0, 94.5)
    d.refresh()
    on = d.btn_ab.isEnabled()
    d.btn_ab.click()
    a1 = (d.spin_start.value(), d.spin_dur.value())
    st.data["ab"] = (94.5, 90.0)                    # 倒着打 A、B
    d.refresh()
    d.btn_ab.click()
    a2 = (d.spin_start.value(), d.spin_dur.value())
    st.data["ab"] = (None, None)
    d.refresh()
    d._use_ab()                                     # 没有 A-B 时不许把数清成 0
    a3 = (d.spin_start.value(), d.spin_dur.value())
    return (not off and on and a1 == (90.0, 4.5) and a2 == (90.0, 4.5) and a3 == (90.0, 4.5)), \
        f"无标记时可用={off} 有标记时可用={on} 正序={a1} 倒序={a2} 清空后={a3}"


def c_sub_gate_text() -> tuple[bool, str]:
    """字幕这行必须说人话：没外挂文件时选项灰掉，并点名"内嵌轨烧不进去"。

    为什么值得单独判：这个限制来自 ffmpeg 的 subtitles 滤镜只吃磁盘文件（见 engine/gif.py
    顶部第 3 条）。灰掉而不解释，用户会以为是 bug；解释了，他就知道要先解字幕。
    """
    st = FakeState()
    d, _ = dlg_for(fresh_cfg(), st)
    disabled, why = d.chk_sub.isEnabled(), d.lbl_sub.text()
    st.data["sub"] = SUB_FILE
    d.refresh()
    enabled, shown = d.chk_sub.isEnabled(), d.lbl_sub.text()
    return (not disabled and "内嵌" in why and enabled and os.path.basename(SUB_FILE) in shown), \
        f"无文件时可用={disabled} 说明={why!r} 有文件时可用={enabled} 说明={shown!r}"


def c_include_sub_roundtrip() -> tuple[bool, str]:
    """勾上的字幕才进 spec()；上次勾过、这次打开就还勾着（读回来的是真读回来）。"""
    cfg = fresh_cfg({"gif_include_sub": True})
    st = FakeState(sub=SUB_FILE)
    d, _ = dlg_for(cfg, st)
    back = d.chk_sub.isChecked()
    sp = d.spec()
    d.chk_sub.setChecked(False)
    sp2 = d.spec()
    cfg2 = fresh_cfg({"gif_include_sub": False})
    d2, _ = dlg_for(cfg2, st)
    back2 = d2.chk_sub.isChecked()
    return (back and sp["sub"] == SUB_FILE and sp2["sub"] == "" and not back2), \
        f"配置 True→界面勾={back} spec.sub={sp['sub']!r}；取消勾后 spec.sub={sp2['sub']!r}；" \
        f"配置 False→界面勾={back2}"


def c_persist_keys() -> tuple[bool, str]:
    """导出一次，界面上的四个规格就必须真的躺在 config.json 里。

    判盘上的 JSON 而不是 cfg 内存值：只写内存的话，重启播放器一切回到出厂值，
    用户下次导出又要重挑一遍宽度——那种"记不住"是这条判据唯一能抓住的形式。
    """
    cfg = fresh_cfg()
    st = FakeState(sub=SUB_FILE)
    d, _ = dlg_for(cfg, st)
    d.combo_w.setCurrentIndex(list(gif.WIDTHS).index(800))
    d.combo_f.setCurrentIndex(list(gif.FPS_LADDER).index(15))
    d.spin_start.setValue(10.0)
    d.spin_dur.setValue(6.0)
    d.chk_sub.setChecked(True)
    got = spy(d)
    d._export()                                    # 真发活由 c_export_locks… 用 click 验；这里只看落盘
    disk = on_disk()
    sp = got["export"][0] if got["export"] else {}
    return (disk.get("gif_width") == 800 and disk.get("gif_fps") == 15
            and abs(float(disk.get("gif_duration") or 0) - 6.0) < 0.01
            and disk.get("gif_include_sub") is True and sp.get("width") == 800
            and sp.get("fps") == 15 and sp.get("sub") == SUB_FILE), \
        f"盘上={ {k: disk.get(k) for k in ('gif_width', 'gif_fps', 'gif_duration', 'gif_include_sub')} }" \
        f" 发出去={ {k: sp.get(k) for k in ('width', 'fps', 'sub')} }"


def c_export_locks_and_empty_video() -> tuple[bool, str]:
    """发出去以后按钮锁住、进度归零；没有正在播放的文件时**一个信号都不许发**。"""
    d, _ = dlg_for(fresh_cfg())
    got = spy(d)
    d.btn_go.click()
    locked = (d.btn_go.isEnabled(), d.btn_stop.isEnabled(), d.bar.value(), len(got["export"]))
    d.btn_stop.click()
    st = FakeState(video="")
    d2, _ = dlg_for(fresh_cfg(), st)
    got2 = spy(d2)
    d2.btn_go.click()
    empty = (len(got2["export"]), d2.lbl_state.text(), d2.btn_go.isEnabled())
    return (locked == (False, True, 0, 1) and got["cancel"] == [True]
            and empty == (0, "没有正在播放的文件，导不出片段", True)), \
        f"正常导出(按钮锁,停可点,进度,信号数)={locked} 取消信号={got['cancel']} 空文件={empty}"


def c_progress_types() -> tuple[bool, str]:
    """进度槽里不许抛：类型乱七八糟也要把进程留住（PyQt 槽里抛＝静默没了，rc=127）。"""
    d, _ = dlg_for(fresh_cfg())
    d.on_progress(1, 3.0, 12.0)
    mid = (d.bar.value(), d.lbl_state.text())
    d.on_progress(1, 1.0, 0.0)                      # 除零
    zero = d.bar.value()
    noise = ""
    for args in ((1, "x", 12.0), (1, None, None), (1, [], {}), (1, 1.0, -3.0)):
        try:
            d.on_progress(*args)
        except BaseException as exc:                 # noqa: BLE001
            noise += f"{args}→{type(exc).__name__} "
    return (mid == (250, "正在导出… 3.0 / 12.0 秒") and zero == 0 and not noise), \
        f"半程={mid} 除零后 bar={zero} 垃圾类型抛出的={noise.strip() or '无'}"


def c_done_restores_and_survives_repaint() -> tuple[bool, str]:
    """结果回灌：按钮恢复、成功才给"打开目录"，而且说明行刷新**不能盖掉**上一条结果。

    这条是被真踩出来的：_repaint_note() 曾经往 lbl_state 上写文案，于是"已导出 xxx"
    在用户下一次改数值时就被盖没了——看不见上一条是成是败，比报错坏。
    第一版判据从出厂态直接喂结果，btn_go 压根没灰过，G15（删掉"放开导出按钮"那句）
    被摸成 MISSED——那才是它该有的用法：坏桩红了，先怀疑判据太弱，别急着说实现没问题。
    """
    d, _ = dlg_for(fresh_cfg())
    ok_txt = os.path.join(TMP, "out", "clip.gif")
    d._export()                                     # 先进"导出中"：导出按钮灰掉、取消亮起来
    mid = (d.btn_go.isEnabled(), d.btn_stop.isEnabled())
    d.on_done(1, True, "30 帧 / 320×180", ok_txt)
    after_ok = (d.btn_go.isEnabled(), d.btn_stop.isEnabled(), d.btn_open.isEnabled(),
                d.bar.value(), d.last_path)
    d._repaint_note()
    kept = d.lbl_state.text()
    d.on_done(1, False, "帧数不够：解出 12", "")
    bad = (d.btn_go.isEnabled(), d.btn_open.isEnabled(), d.bar.value(), d.lbl_state.text(),
           d.last_path)
    return (mid == (False, True) and after_ok == (True, False, True, 1000, ok_txt)
            and kept.startswith("已导出：")
            and "clip.gif" in kept
            and bad == (True, False, 0, "导出失败：帧数不够：解出 12", "")), \
        f"导出中(导/停)={mid} 成功后(导/停/开/进度/路径)={after_ok} 刷说明后仍在={kept!r}；" \
        f"失败后(导/开/进度/文案/路径)={bad}"


def c_burst_persist_emit() -> tuple[bool, str]:
    """连拍的张数与间隔：写盘、且把这两个数原样发出去（主窗口只管照着截）。"""
    cfg = fresh_cfg()
    d, _ = dlg_for(cfg)
    got = spy(d)
    d.spin_n.setValue(12)
    d.spin_gap.setValue(400)
    d.btn_burst.click()
    disk = on_disk()
    return (disk.get("burst_count") == 12 and disk.get("burst_gap_ms") == 400
            and got["burst"] == [(12, 400)]), \
        f"盘上=({disk.get('burst_count')}, {disk.get('burst_gap_ms')}) 发出={got['burst']}"


def c_state_of_raises_keeps_dialog() -> tuple[bool, str]:
    """取状态就抛：界面停在旧值、说明里点名失败原因、spec() 照样给得出可用的一份。"""
    d, _ = dlg_for(fresh_cfg())
    d.state_of = BoomState()
    noise = ""
    try:
        d.refresh()
        d._repaint_note()
        sp = d.spec()
    except BaseException as exc:                     # noqa: BLE001
        noise = f"{type(exc).__name__}: {exc}"
        sp = {}
    txt = d.lbl_range.text()
    return (not noise and "读当前播放状态失败" in txt and "IPC" in txt
            and sp.get("video") == "" and sp.get("start") >= 0.0), \
        f"抛出的={noise or '无'} 文案={txt!r} spec={sp}"


def c_reveal_guard() -> tuple[bool, str]:
    """「打开目录」只在**手上有产物**时可点：失败一次就要收回去。

    失败以后 last_path 清空、按钮灰掉这一半很重要——导崩一趟还留着上一张的路径，
    用户点开的会是**另一部片**的 GIF 所在目录，那种错认比灰掉难查得多。
    """
    d, _ = dlg_for(fresh_cfg())
    got = spy(d)
    before = (d.btn_open.isEnabled(), len(got["reveal"]))
    d._reveal()
    mid = len(got["reveal"])
    p = os.path.join(TMP, "out", "gif", "a.gif")
    d.on_done(1, True, "ok", p)
    d.btn_open.click()
    d.on_done(1, False, "帧数不够：解出 12", "")
    after = (d.btn_open.isEnabled(), d.last_path)
    d.btn_open.setEnabled(True)                     # 就算按钮被别处点亮，硬调也不该发
    d._reveal()
    return (before == (False, 0) and mid == 0 and got["reveal"] == [p]
            and after == (False, "") and len(got["reveal"]) == 1), \
        f"初始(可用,信号数)={before} 硬调后={mid} 成功后={got['reveal']} " \
        f"失败后(可用,路径)={after} 最终信号数={len(got['reveal'])}"


def c_defaults_from_cfg() -> tuple[bool, str]:
    """新建对话框用的宽度/帧率/时长来自配置，不是出厂常量。

    为什么单判：界面初值写死出厂常量也是一种"记不住"，而且它藏得很好——只有第二次
    打开对话框才看得出来，人眼验收很容易漏过去。
    """
    cfg = fresh_cfg({"gif_width": 640, "gif_fps": 20, "gif_duration": 6.0, "burst_count": 16,
                     "burst_gap_ms": 500})
    d, _ = dlg_for(cfg)
    vals = (d.combo_w.currentData(), d.combo_f.currentData(), d.spin_dur.value(),
            d.spin_n.value(), d.spin_gap.value())
    weird = fresh_cfg({"gif_width": 12345, "gif_fps": 7})   # 配置被手改成不存在的档位
    d2, _ = dlg_for(weird)
    return (vals == (640, 20, 6.0, 16, 500) and d2.combo_w.currentData() in gif.WIDTHS
            and d2.combo_f.currentData() in gif.FPS_LADDER), \
        f"按配置={vals} 非法档位折回=({d2.combo_w.currentData()}, {d2.combo_f.currentData()})"


def c_owner_keys_mirror() -> tuple[bool, str]:
    """把播放器键位镜像到工坊自己那份上：装得对、只响一次、面板那三把键留给面板。

    为什么需要这份镜像（真机实测，tests/probe_gif3.py B1–B4）：`QShortcut(context=
    WindowShortcut)` 只认**当前活动窗口**，工坊一成为活动窗，主窗那 58 条键就整体收不到
    ——按 B 命中的动作是 `[]`，把主窗抢回活动才又是 `['burst_shot']`。

    这里量的是"挂上去的那一份"：① 装几条、键值对不对、父对象是不是本窗（父对象错了
    就等于挂在主窗上，症状和没修一样）；② 命中交回调用方给的 sink，一条键一次只响一次
    （旧的一组没摘干净就是"按一次做两件事"，见 main_window 装表那段的注释）；
    ③ Esc/Return/Enter 不进来——那是这个窗自己的"关掉/确认"，抢走用户就出不去了。

    说明白这条测不到什么：触发走的是 `sc.activated.emit()`，证的是**连接**通、连了几次；
    "工坊是活动窗时那把物理键真的走这里"要靠真机按键（tests/test_gif_real.py 的 B 段），
    因为键匹配发生在 Qt 的窗口激活与焦点里，不在本进程的 widget 树上。
    """
    rows = [("Space", "toggle_play", "播放/暂停"), ("B", "burst_shot", "连拍"),
            ("Page Down", "next", "下一个"), ("Esc", "close_panel", "关闭"),
            ("Return", "confirm", "确认"), ("Enter", "confirm2", "确认"),
            ("", "noop", "空串")]
    calls: list[str] = []
    d, _ = dlg_for(fresh_cfg())
    before = list(rows)
    n = d.install_owner_keys(rows, calls.append)
    mine = d.findChildren(QShortcut)
    codes = sorted(int(sc.key()[0]) for sc in mine if sc.key().count() >= 1)
    want = sorted(KM.parsed(s) for s, a, _d in rows[:3])
    kept = {KM.parsed(k) for k in KM.KEEP_FOR_WINDOWS}
    first = (n == 3 and len(mine) == 3 and codes == want
             and all(sc.parent() is d for sc in mine) and not set(codes) & kept
             and rows == before)
    for sc in mine:
        sc.activated.emit()
    routed = sorted(calls) == sorted(["toggle_play", "burst_shot", "next"])
    calls.clear()
    d.install_owner_keys(rows, calls.append)                    # 装第二次：不许叠两份
    again = (len(d.findChildren(QShortcut)) == 3
             and len(d._owner_shortcuts) == 3)
    d._owner_shortcuts[0].activated.emit()
    once = len(calls) == 1
    d.install_owner_keys(rows + [("F9", "mute", "静音")], calls.append)
    grew = len(d.findChildren(QShortcut)) == 4                   # 键位换了要跟着换
    noise = ""
    for args in ((None, calls.append), ("垃圾", calls.append), (rows, None), (rows, "不是可调用")):
        try:
            got = d.install_owner_keys(*args)
            if got != 0:
                noise += f"{args}→装了 {got} 条 "
        except BaseException as exc:                             # noqa: BLE001
            noise += f"{args}→{type(exc).__name__}: {exc} "
    junk = not noise and len(d.findChildren(QShortcut)) == 0     # sink 不可用时旧镜像也要摘干净
    bad = [name for name, ok in (("装三条且键值/父对象/留三把都对", first),
                                 ("命中交回 sink 且动作对", routed), ("装两次不叠两份", again),
                                 ("一次按键只响一次", once), ("键位换了镜像跟着换", grew),
                                 ("垃圾入参不抛且不装东西", junk)) if not ok]
    return not bad, (f"没做到的={bad} 首装={n} 条 键值={[hex(c) for c in codes]} "
                     f"期望={[hex(c) for c in want]} 二次={len(d.findChildren(QShortcut))} "
                     f"发一次收到={calls} 垃圾={noise.strip() or '无'}")


HANDLERS = ("refresh", "_repaint_note", "_use_pos", "_use_ab", "spec", "_export", "_cancel",
            "_burst", "_reveal", "on_progress", "on_done")


def c_no_handler_throws() -> tuple[bool, str]:
    """八种状态 × 十一个入口，一个都不许抛——这是防 rc=127 的网，不是装饰。

    为什么单开一条：本次实测就是这条抓到的第一个真货——`_persist` 按配置里的键名
    （gif_width/gif_fps）去 spec() 那份 dict 取值，而 spec 给的键是 width/fps，于是
    每次点"导出 GIF"都是 KeyError。它发生在 Qt 槽里，表现不是 traceback 而是**进程
    当场没了**（rc=127、stderr 全空），用户那边就是"播放器闪退"。
    这条判据把入口**直接调一遍**（不是点按钮）：点按钮抛出会连测试一起带走，直接调能
    把"哪一格抛了什么"写进详情里。真点按钮的接线由 c_export_locks… 那条负责。
    """
    states = [
        ("正常", FakeState(sub=SUB_FILE, ab=(5.0, 9.0))),
        ("空文件", FakeState(video="")),
        ("时长未知", FakeState(duration=0.0)),
        ("贴片尾", FakeState(pos=3599.99)),
        ("只有一个 A 点", FakeState(ab=(5.0, None))),
        ("类型全是垃圾", FakeState(video=7, pos="abc", duration=None, ab="xy", sub=9)),
        ("状态回 None", lambda: None),
        ("状态就抛", BoomState()),
    ]
    noise, n = "", 0
    for tag, st in states:
        d, _ = dlg_for(fresh_cfg(), st)
        for name in HANDLERS:
            fn = getattr(d, name)
            n += 1
            try:
                if name == "on_progress":
                    fn(1, 1.0, 4.0)
                elif name == "on_done":
                    fn(1, True, "30 帧 / 320×180", os.path.join(TMP, "a.gif"))
                else:
                    fn()
            except BaseException as exc:             # noqa: BLE001 - 抛出来就点名，别让它带走进程
                noise += f"[{tag}] {name}→{type(exc).__name__}: {exc}  "
    return (not noise), f"调了 {n} 次（{len(states)} 种状态 × {len(HANDLERS)} 个入口），抛出的={noise.strip() or '无'}"


CHECKS = [
    ("spec() 交出去的区间已夹过：贴片尾收时长、时长未知照界面值", c_spec_clamps_range),
    ("时长框上界跟着剩余时间走，导不出时文案点名原因", c_dur_maximum_follows_remaining),
    ("「用当前位置」把播放位置搬进起点框并刷新说明", c_btn_here_uses_pos),
    ("A-B 两个点齐了才可用，用了按排序后的区间填，倒着打点也一样", c_ab_gate_and_span),
    ("没外挂字幕文件时选项灰掉并说明「内嵌轨烧不进去」", c_sub_gate_text),
    ("字幕勾与配置双向一致：勾上的才进 spec，上次勾过这次还勾着", c_include_sub_roundtrip),
    ("导出一次，四个规格键真的躺在 config.json 里且与发出去的一致", c_persist_keys),
    ("导出后按钮锁住进度归零，空文件一个信号都不发，取消把信号发出去", c_export_locks_and_empty_video),
    ("进度槽兜住类型：除零/字符串/None/负数都不许把进程带走", c_progress_types),
    ("结果回灌恢复按钮，且说明行刷新盖不掉上一条结果", c_done_restores_and_survives_repaint),
    ("连拍张数与间隔：落盘 + 原样发出去", c_burst_persist_emit),
    ("取状态就抛时界面留在旧值、文案点名失败、spec 照样可用", c_state_of_raises_keeps_dialog),
    ("没有产物时「打开目录」灰掉且硬调不发信号", c_reveal_guard),
    ("新建对话框的初值来自配置，非法档位折回可用档位", c_defaults_from_cfg),
    ("键位镜像：装对键值与父对象、命中交回 sink、装两次只响一次、Esc/Return/Enter 留给本窗",
     c_owner_keys_mirror),
    ("八种状态 × 十一个入口直接调一遍，一个都不许抛（防槽里异常＝进程静默没了）", c_no_handler_throws),
]


# ============================================================ 正常自检
def run(tmp: str) -> int:
    bad = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except BaseException as exc:                 # noqa: BLE001 - 崩了也要留下是哪条
            ok, detail = False, f"判据自身抛 {type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  —— {detail}", flush=True)
        if not ok:
            bad.append(name)
    print(f"\n合计 {len(CHECKS)} 条，失败 {len(bad)} 条" + (f"：{bad}" if bad else ""))
    return 1 if bad else 0


# ============================================================ 证伪
def falsify(tmp: str) -> int:
    """把实现换成"看起来对"的写法，对应那条判据必须翻红。翻不红＝那条判据是摆设。"""
    print("---- 证伪：削弱对话框实现以后，对应判据必须变红 ----")
    saved = {n: getattr(GifDialog, n) for n in
             ("__init__", "refresh", "_state", "_repaint_note", "_use_pos", "_use_ab",
              "spec", "_export", "_cancel", "_burst", "_reveal", "_persist",
              "on_progress", "on_done", "install_owner_keys")}
    hits: list[tuple[str, bool, str]] = []
    covered: set = set()

    def restore():
        for k, v in saved.items():
            setattr(GifDialog, k, v)

    def target(fn):
        for _n, f in CHECKS:
            if f is fn:
                try:
                    return fn()
                except BaseException as exc:         # noqa: BLE001
                    return False, f"判据崩了 {type(exc).__name__}: {exc}"
        raise AssertionError(f"证伪表指向的函数不在 CHECKS 里：{fn.__name__}")

    def add(label, fn, stub):
        covered.add(fn)
        restore()
        stub()
        ok, detail = target(fn)
        hits.append((label, not ok, f"削弱后该条判据 ok={ok} —— {detail}"))
        restore()

    # G1 spec 不夹区间（把越界区间原样发给引擎）
    def g1():
        def naive(self):
            st = self._state()
            return {"video": str(st.get("video") or ""), "start": float(self.spin_start.value()),
                    "dur": float(self.spin_dur.value()),
                    "width": int(self.combo_w.currentData() or gif.DEFAULT_WIDTH),
                    "fps": int(self.combo_f.currentData() or gif.DEFAULT_FPS),
                    "sub": str(st.get("sub") or "") if self.chk_sub.isChecked() else ""}
        GifDialog.spec = naive
    add("G1 spec 不夹越界区间 → 贴片尾那条必须被否", c_spec_clamps_range, g1)

    # G2 时长框上界永远给满（用户可以填出一个导不出的区间，文案还说"还剩"）
    def g2():
        def naive(self):
            st = self._state()
            self.spin_dur.setMaximum(gif.MAX_DURATION)
            dur = float(st.get("duration") or 0.0)
            self.lbl_range.setText(f"还剩 {max(0.0, dur - float(self.spin_start.value())):.1f} 秒可导")
        GifDialog._repaint_note = naive
    add("G2 上界不夹 + 文案永远说能导 → 剩余时间那条必须被否", c_dur_maximum_follows_remaining, g2)

    # G3 「用当前位置」写死 0
    def g3():
        GifDialog._use_pos = lambda self: self.spin_start.setValue(0.0)
    add("G3 用当前位置写成固定 0 → 搬位置那条必须被否", c_btn_here_uses_pos, g3)

    # G4 A-B 按钮恒可用，且不排序（倒着打点会得到负时长）
    def g4():
        def naive(self):
            st = self._state()
            a, b = (st.get("ab") or (None, None))[:2]
            if a is None or b is None:
                self.spin_start.setValue(0.0)
                self.spin_dur.setValue(gif.MIN_DURATION)
                return
            self.spin_start.setValue(float(a))
            self.spin_dur.setValue(max(gif.MIN_DURATION, float(b) - float(a)))
        GifDialog._use_ab = naive
        GifDialog.refresh = lambda self: None
    add("G4 A-B 不排序且无标记也照用 → 那一半必须被否", c_ab_gate_and_span, g4)

    # G5 字幕选项恒可用、说明永远为空（内嵌轨也会被"烧"）
    def g5():
        def naive(self):
            self.chk_sub.setEnabled(True)
            self.lbl_sub.setText("")
            st = self._state()
            dur = float(st.get("duration") or 0.0)
            start = float(self.spin_start.value())
            self.spin_dur.setMaximum(max(gif.MIN_DURATION, min(gif.MAX_DURATION, dur - start)))
        GifDialog._repaint_note = naive
        GifDialog.refresh = lambda self: (self.chk_sub.setEnabled(True), self._repaint_note())
    add("G5 字幕选项恒可用 + 不解释 → 灰掉并点名那条必须被否", c_sub_gate_text, g5)

    # G6 勾不读回来；spec 又不看勾（两种"记不住/删不掉"合在一个桩里）
    def g6():
        def naive(self):
            st = self._state()
            self.spin_start.setValue(float(st.get("pos") or 0.0))
            a, b = st.get("ab") or (None, None)
            self.btn_ab.setEnabled(bool(a is not None and b is not None))
            sub = str(st.get("sub") or "")
            self.chk_sub.setEnabled(bool(sub))
            self._repaint_note()
        GifDialog.refresh = naive
    add("G6 打开时不读回 gif_include_sub → 双向一致那条必须被否", c_include_sub_roundtrip, g6)

    # G7 _persist 只写内存不 save（重启回到出厂值）
    def g7():
        def naive(self, sp):
            for k in ("gif_width", "gif_fps"):
                self.cfg[k] = sp[k]
            self.cfg["gif_duration"] = sp["dur"]
            self.cfg["gif_include_sub"] = bool(sp["sub"])
        GifDialog._persist = naive
    add("G7 存了内存没 save → 落盘那条必须被否", c_persist_keys, g7)

    # G8 导出前不查 video（发一个 video 为空的活给引擎）
    def g8():
        def naive(self):
            sp = self.spec()
            self._persist(sp)
            self.job_id += 1
            self.sig_export.emit(dict(sp))
            self.bar.setValue(0)
            self.btn_stop.setEnabled(True)
            self.btn_go.setEnabled(False)
            self.lbl_state.setText("正在导出…")
        GifDialog._export = naive
    add("G8 空文件照样发导出 → 一个信号都不发那条必须被否", c_export_locks_and_empty_video, g8)

    # G9 进度槽不兜类型（真机上这就等于进程没了）
    def g9():
        def naive(self, _jid, sec, total):
            frac = 0.0 if float(total) <= 0 else max(0.0, min(1.0, float(sec) / float(total)))
            self.bar.setValue(int(frac * 1000))
            self.lbl_state.setText(f"正在导出… {sec:.1f} / {total:.1f} 秒")
        GifDialog.on_progress = naive
    add("G9 进度槽不兜类型 → 垃圾类型不许带走进程那条必须被否", c_progress_types, g9)

    # G10 结果回灌后把"打开目录"永远点亮（失败也能点，点开一个不存在的目录）
    def g10():
        def naive(self, _jid, ok, detail, path):
            self.btn_go.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.bar.setValue(1000 if ok else 0)
            self.last_path = str(path or "")
            self.btn_open.setEnabled(True)
            self.lbl_state.setText(("已导出：" + os.path.basename(self.last_path) + " —— "
                                    if ok else "导出失败：") + str(detail or ""))
        GifDialog.on_done = naive
    add("G10 失败也点亮打开目录 → 成功后那半必须能区分出来", c_reveal_guard, g10)

    # G11 连拍不落盘（下次打开回到出厂 8 张 250 毫秒）
    def g11():
        def naive(self):
            self.sig_burst.emit(int(self.spin_n.value()), int(self.spin_gap.value()))
        GifDialog._burst = naive
    add("G11 连拍只发信号不写盘 → 落盘那条必须被否", c_burst_persist_emit, g11)

    # G12 _state 不兜异常（IPC 断一次＝播放器没了）
    def g12():
        GifDialog._state = lambda self: self.state_of()
    add("G12 取状态不兜异常 → 留在旧值那条必须被否", c_state_of_raises_keeps_dialog, g12)

    # G13 说明行刷新盖掉上一条结果（真踩过的：lbl_state 被 _repaint_note 写花）
    def g13():
        def naive(self):
            st = self._state()
            sub = str(st.get("sub") or "")
            self.lbl_sub.setText(f"字幕：{os.path.basename(sub)}" if sub else "")
            dur = float(st.get("duration") or 0.0)
            start = float(self.spin_start.value())
            if dur > 0:
                self.spin_dur.setMaximum(max(gif.MIN_DURATION,
                                             min(gif.MAX_DURATION, dur - start)))
                self.lbl_state.setText(f"还剩 {dur - start:.1f} 秒可导")
            else:
                self.spin_dur.setMaximum(gif.MAX_DURATION)
        GifDialog._repaint_note = naive
    add("G13 刷新说明时盖掉 lbl_state → 盖不掉那条必须被否",
        c_done_restores_and_survives_repaint, g13)

    # G14 初值写死出厂常量（配置的档位没人看）
    def g14():
        orig = GifDialog.__init__
        def naive(self, cfg, state_of, parent=None):
            orig(self, cfg, state_of, parent)
            i = list(gif.WIDTHS).index(gif.DEFAULT_WIDTH)
            self.combo_w.setCurrentIndex(i)
            self.combo_f.setCurrentIndex(list(gif.FPS_LADDER).index(gif.DEFAULT_FPS))
            self.spin_dur.setValue(gif.DEFAULT_DURATION)
            self.spin_n.setValue(8)
            self.spin_gap.setValue(250)
        GifDialog.__init__ = naive
    add("G14 初值无视配置 → 配置驱动初值那条必须被否", c_defaults_from_cfg, g14)

    # G15 结果回灌忘了把导出按钮放开（一次导出以后这个框就点不动了，只能关掉重开）
    def g15():
        def naive(self, _jid, ok, detail, path):
            self.btn_stop.setEnabled(False)
            self.bar.setValue(1000 if ok else 0)
            self.last_path = str(path or "")
            self.btn_open.setEnabled(bool(self.last_path))
            self.lbl_state.setText(("已导出：" + os.path.basename(self.last_path) + " —— "
                                    if ok else "导出失败：") + str(detail or ""))
        GifDialog.on_done = naive
    add("G15 回灌后不放开导出按钮 → 失败后仍可再导那半必须被否",
        c_done_restores_and_survives_repaint, g15)

    # G16 本次实测抓到的真货：_persist 按配置键名去 spec() 的 dict 里取值
    #     （spec 给的键是 width/fps）→ 每次点导出都是 KeyError，而在 Qt 槽里
    #     KeyError 的表现不是报错框，是进程静默没了（rc=127、stderr 全空）。
    def g16():
        def naive(self, sp):
            for k in ("gif_width", "gif_fps"):
                self.cfg[k] = sp[k]
            self.cfg["gif_duration"] = sp["dur"]
            self.cfg["gif_include_sub"] = bool(sp["sub"])
            self.cfg.save()
        GifDialog._persist = naive
    add("G16 搬运键名拼错（真 bug 复现）→ 防 rc=127 那张网必须点名 _export",
        c_no_handler_throws, g16)

    # G17 门口不收类型：上游给个 "abc" 就当数字用（refresh 里 float("abc") 即抛）
    def g17():
        def naive(self):
            try:
                st = self.state_of()
            except Exception as exc:                # noqa: BLE001
                return {"error": f"{type(exc).__name__}: {exc}"}
            return st if isinstance(st, dict) else {}
        GifDialog._state = naive
    add("G17 状态不做类型收口 → 垃圾类型那一格必须被点名", c_no_handler_throws, g17)

    # G18 装新镜像前不摘旧的：第二次打开工坊就变成"按一次做两件事"
    #     （main_window 装主窗那张表时踩过同一形状，注释里写着： dropping the list
    #     leaves live shortcuts → 一次按键两条都响）
    def g18():
        def naive(self, rows, sink):
            for seq, act, _d in KM.mirrorable(rows):
                sc = QShortcut(QKeySequence(str(seq)), self, context=Qt.WindowShortcut)
                sc.activated.connect(lambda checked=False, a=str(act): sink(a))
                self._owner_shortcuts.append(sc)
            return len(self._owner_shortcuts)
        GifDialog.install_owner_keys = naive
    add("G18 旧镜像不摘 → 装两次只响一次那半边必须被否", c_owner_keys_mirror, g18)

    # G19 父对象留空：QShortcut 挂在顶层而不是工坊窗上——症状与"根本没修"一模一样
    #     （WindowShortcut 只认活动窗，父对象不在那个窗就等于收不到键）
    #     注意要显式写 None：`QShortcut(seq, context=…)` 在本机 SIP 里没有这个重载，
    #     那样写只会让坏桩自己 TypeError，测到的是"桩崩了"而不是"父对象错了"。
    def g19():
        def naive(self, rows, sink):
            for sc in self._owner_shortcuts:
                sc.setEnabled(False)
                sc.setParent(None)
                sc.deleteLater()
            self._owner_shortcuts = []
            for seq, act, _d in KM.mirrorable(rows):
                sc = QShortcut(QKeySequence(str(seq)), None, context=Qt.WindowShortcut)
                sc.activated.connect(lambda checked=False, a=str(act): sink(a))
                self._owner_shortcuts.append(sc)
            return len(self._owner_shortcuts)
        GifDialog.install_owner_keys = naive
    add("G19 父对象写错（没挂在本窗） → 父对象那半边必须被否", c_owner_keys_mirror, g19)

    # G20 整表镜像，连 Esc/Return/Enter 也抢过来：用户关不掉这个面板
    def g20():
        def naive(self, rows, sink):
            for sc in self._owner_shortcuts:
                sc.setEnabled(False)
                sc.setParent(None)
                sc.deleteLater()
            self._owner_shortcuts = []
            for seq, act, _d in (rows or ()):
                if KM.parsed(str(seq)) == 0:
                    continue
                sc = QShortcut(QKeySequence(str(seq)), self, context=Qt.WindowShortcut)
                sc.activated.connect(lambda checked=False, a=str(act): sink(a))
                self._owner_shortcuts.append(sc)
            return len(self._owner_shortcuts)
        GifDialog.install_owner_keys = naive
    add("G20 把面板自己的三把键也抢走 → 留给本窗那半边必须被否", c_owner_keys_mirror, g20)

    # G21 命中了不交回 sink（装了等于没装）；顺带 sink 不可用时也不判
    def g21():
        def naive(self, rows, sink):
            for sc in self._owner_shortcuts:
                sc.setEnabled(False)
                sc.setParent(None)
                sc.deleteLater()
            self._owner_shortcuts = []
            for seq, _act, _d in KM.mirrorable(rows):
                self._owner_shortcuts.append(
                    QShortcut(QKeySequence(str(seq)), self, context=Qt.WindowShortcut))
            return len(self._owner_shortcuts)
        GifDialog.install_owner_keys = naive
    add("G21 装了键但不接派发 → 命中交回 sink 那半边必须被否", c_owner_keys_mirror, g21)

    bad = 0
    for label, hit, detail in hits:
        if not hit:
            bad += 1
        print(f"[{'CAUGHT' if hit else 'MISSED'}] {label}  —— {detail}", flush=True)
    holes = [n for n, fn in CHECKS if fn not in covered]
    for n in holes:
        print(f"[HOLE ] 这条判据没有任何坏桩指向它：{n}")
    print(f"\n证伪 {len(hits)} 组，漏网 {bad} 组，没人证的判据 {len(holes)} 条")
    return 1 if (bad or holes) else 0


if __name__ == "__main__":
    try:
        sys.exit(falsify(TMP) if "--falsify" in sys.argv else run(TMP))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
