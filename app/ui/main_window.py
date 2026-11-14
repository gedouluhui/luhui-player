"""主窗口：装配引擎与界面，拥有全部快捷键与菜单动作。

一条纪律：界面**从不**用同步请求（get_property）刷新自己，所有状态都来自 player 的
订阅缓存 prop() 或信号。原因见 engine/ipc.py——在读线程里发同步请求会自我死锁，
在 UI 线程里发则把卡顿从"可能"变成"必然"。
"""
from __future__ import annotations

import os
import sys
import time

from PyQt5.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QRadialGradient
from PyQt5.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                             QMenu, QPushButton, QShortcut, QSizePolicy,
                             QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import config as cfgmod
from .. import win32
from ..engine import gif as gifmod
from ..engine.gif import GifStore
from ..engine.library import Library
from ..engine.perf import hwdec_state
from ..engine import poolkit
from ..engine.player import MpvPlayer, is_sub_file, kernel_flag
from ..engine.posters import PosterStore
from ..engine.thumbs import ThumbStore
from . import form_rows, help_page, icons, keymap, material, menus, motion, theme, win_geom
from .audio_eq_panel import AudioEqPanel
from .chrome import Chrome
from .controls_bar import SPEEDS, ControlsBar
from .floaters import ChromeHost, Osd, ScrubPreview
from .gif_dialog import GifDialog
from .library_panel import LibraryPanel
from .keymap_dialog import KeymapDialog
from .playlist_panel import PlaylistPanel
from .scrubber import fmt_time
from .settings_dialog import INT_RANGE, SettingsDialog
from .sleep_timer import SleepDialog, SleepTimer
from .stats_panel import StatsPanel
from .title_bar import TitleBar
from .transform_panel import TransformPanel
from .tweak_panel import TweakPanel
from .video_surface import VideoSurface

M = theme.M
MIN_W, MIN_H = 880, 560
HIDE_AFTER_MS = 2600      # 全屏后无操作多久收起条与标题栏
SAVE_EVERY = 5.0          # 断点位置落盘的最小间隔（秒）
# 那句"已载入 N 个文件"要等内核回执，最多等多久。给到 20 秒而不是 2 秒，是因为 4K REMUX
# 从机械盘起播＋带 `start=` 定位，首帧本来就慢；超时的报法把两种结局分开写（列表里有没有
# 这一条），所以"多等"不会把"命令被拒"拖成一句含糊的"没反应"。
OPEN_RECEIPT_MS = 20000

# 关窗口时给后台作业的最长收口时间。1.5s 是这么定的：一次缩略图/封面 ffmpeg 是秒级，
# 一次 ffprobe 是几十毫秒，而"关窗口"这个动作的用户预期是"立刻没了"。到点还活着的作业
# 由 poolkit 的 emit/pin 兜住（最坏吞掉一条回报），不值得让整块界面卡在关闭里。
SHUTDOWN_GRACE_MS = 1500


def chapter_notice(info: dict) -> str:
    """章节动作之后要说的那句话（纯函数：不建窗口也能钉住每种结局）。

    边界要说出来（"已是最后一章"），否则用户按一下看见画面没动会以为键坏了；
    没有章节也要说，而不是吞掉。时间只在非 0 秒时出现——第一章写「00:00」是噪声。
    """
    total = int(info.get("total") or 0)
    if total <= 0:
        return "这支片子没有章节"
    if total == 1:
        return "这支片子只有 1 章"
    edge = str(info.get("edge") or "")
    if edge == "first":
        return f"已是第一章 · 共 {total} 章"
    if edge == "last":
        return f"已是最后一章 · 共 {total} 章"
    idx = info.get("index")
    if not isinstance(idx, int) or idx < 0:
        return f"共 {total} 章"
    bits = [f"第 {idx + 1}/{total} 章"]
    try:
        t = float(info.get("time") or 0.0)
    except (TypeError, ValueError):
        t = 0.0
    if t > 0:
        bits.append(fmt_time(t))
    title = str(info.get("title") or "").strip()
    if title:
        bits.append(title)
    return " · ".join(bits)

# 快捷键默认表迁到了 ui/keymap.py（R3-1a）：那张表现在同时负责 diff 存储模型、合法性
# 判据与导入导出——"哪些键按得响"只能有一份事实。这里留着 KEYMAP 这个名字是为了不动
# 测试与帮助页的引用；真正的**生效表**是 keymap.effective(cfg["keymap"])，
# 由 _install_shortcuts 装、由 _show_help 显示，两处同一个来源。
KEYMAP = keymap.DEFAULT

# 这些动作要有内核在场才做得成（发 set_property、读当前播放位置）。
# 和传送类分开列的理由：按空格时内核没起来，"没反应"是正常预期；但按 Shift+G
# 时用户看不出是快捷键坏了还是没起内核，所以这一组必须回一句话。
KERNEL_ONLY = ("menu_sub", "menu_audio", "menu_device", "tone_cycle", "eq_reset",
               "ab_a", "ab_b", "ab_clear", "sub_delay_down", "sub_delay_up",
               "sub_size_down", "sub_size_up", "export_gif", "burst_shot",
               "prev_chapter", "next_chapter")

# 画面变换那一族（engine/transform.py）**不**进上面的 KERNEL_ONLY，这是刻意的：
# 那些值全部进启动参数（`transform.startup_args`），内核没起来时按快捷键也有意义——
# 记进设置、播放时一次带上，面板会说"已记入设置，播放后生效"。
# 与之相对，"字幕延迟/音量/截图"这类动作离开内核就什么都不是，才需要那句兜底提示。
# 所以判据不是"屏幕上有没有画面可改"，而是**这条动作在没内核时是否还做得成事**。
# 那几把键（R / Shift+R / H / Shift+H / Shift+P / Ctrl+G）在 dispatch 里各有一条分支，
# 而且**全部只调 transform_panel 的方法**：键盘与点控件必须走同一段代码，
# 否则就会出现"面板改了内核没改"或"快捷键的 OSD 文案和面板对不上"——
# 而 tests/test_ui.py 拿 dispatch 的源码文本对账，分支写在哪一处是判据能跑的前提。

# 右键菜单与"更多"按钮里那几条视图/工具项。提成常量有两个理由：
#   1. 验收要在**不弹模态菜单**的前提下核对"每条菜单项的动作名都在快捷键表里"——
#      菜单项写个 dispatch 不认的动作名是静默失效（点了没反应，也不报错），
#      而 QMenu.exec 是阻塞的，测试里弹起来就把整轮卡死。
#   2. 两处入口（右键 / 更多按钮）共用一份，不会出现"右键有统计、更多里没有"。
VIEW_ACTIONS: tuple[tuple[str, str], ...] = (
    ("toggle_playlist", "播放列表"), ("toggle_library", "媒体库"),
    ("toggle_tweak", "调节面板"), ("toggle_transform", "画面面板"),
    ("toggle_stats", "统计面板"),
    ("always_on_top", "窗口置顶"),
    ("export_gif", "截图工坊"),
    ("sleep_timer", "睡眠定时"), ("settings", "设置"), ("keymap_edit", "快捷键设置"),
    ("help_keys", "快捷键一览"),
)
# "更多"按钮只收那三个没有专属按钮的：列表/媒体库/调节在控制条上各有一个灯，
# 再往"更多"里放一遍就会出现"两处勾一个不勾"的双份状态。
# 画面页与统计页同一类（控制条上没有灯），所以两个都在"更多"里。
MORE_ACTIONS: tuple[str, ...] = ("toggle_transform", "toggle_stats", "always_on_top",
                                 "export_gif",
                                 "sleep_timer", "settings", "keymap_edit")
# 动作名 → 菜单文字（菜单和快捷键共用左边那个字符串，右边只是措辞）
CONTEXT_LABELS: dict[str, str] = {a: lbl for a, lbl in VIEW_ACTIONS}


def menu_action_names() -> set[str]:
    """菜单里出现过的动作名，验收拿它和 KEYMAP 对账。"""
    return {a for a, _ in VIEW_ACTIONS}


# ================= 类型收口：从缓存/配置/spec 里拿来的东西都要过这两道 =================
# 为什么要有它们：这三处（内核订阅缓存、人手改过的 config.json、对话框发来的 spec）都能
# 交出字符串、None、甚至字符串 "no"（内核在没有媒体时就这么推 time-pos）。而它们全坐在
# Qt 槽的调用链上——槽里一个 ValueError 不是 traceback，是**进程当场没了**（rc=127、
# stderr 全空，日志断在半路）。本仓已经为此付过一次学费（GifDialog._persist 的 KeyError）。
# 所以收口写在取值这一道，不写在 try/except 那一道：兜底值是一个能读出来的数，
# 而不是"这条界面暂时画不出来"。
def _f(v: object, default: float = 0.0) -> float:
    """任意值 → float；认不出来就回 default（NaN/无穷也当认不出来，它们会污染时长）。"""
    try:
        x = float(v)                      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if x == x and x not in (float("inf"), float("-inf")) else default


def _i(v: object, default: int) -> int:
    """任意值 → int；四舍五入而不是截断：连拍 3.7 张这种数来自滑块/手改的配置，
    截断会少要一张，而用户看得见的那个值是 4。"""
    try:
        return int(round(float(v)))       # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def open_receipt(count: int, plays: bool, head: str, pl_before: int, pl_now: int,
                 kernel_path: str, loaded: bool, timed_out: bool) -> tuple[bool, str, bool]:
    """那句"已载入/已追加"该不该说、该说什么（**纯函数**，界面上那句只是它的输出）。

    为什么要有这一层：`add_paths` 以前在 `open_files()` 之后**无条件**印一句"已载入 N 个
    文件"，而 `open_files` 只是把命令塞进写队列——命令在内核那一头被整条拒掉时，写队列一切
    正常、界面照印成功。2026-10-19 用户双击《美国队长3》看到的就是这个形状："只是弹出一个
    提示说加载了而已，实际上并没有加载任何东西"。现在这句话必须由**内核自己发回来的东西**撑着：
      · `plays=True`（第一条发成 replace，会换当前条目）→ 等证据是内核的 `file-loaded` 事件；
      · `plays=False`（往正在播的队尾追加，这条路上永远不会有 file-loaded）→ 等证据是
        内核推来的 playlist 真的变长了。
    `loaded`/`pl_now`/`kernel_path` 全部来自内核推送，界面一个字都不许自己猜；`timed_out`
    那一路才用 `kernel_path` 做**分诊**（列表里有没有这一条），把"命令被拒"和"收了但解不开"
    分开报，不许糊成一句"没反应"。

    写成纯函数是为了能被"已知坏的读数"喂（tests/test_open_receipt.py）：真机观察本身不证明
    自己有牙——判据整条写成 `check(..., True, ...)` 也照样跑绿。
    """
    if count <= 0:
        return False, "", False
    same = bool(head) and (head == (kernel_path or "") or
                           os.path.basename(head) == os.path.basename(kernel_path or ""))
    if plays:
        if loaded:
            return True, f"已载入 {count} 个文件", False
        if not timed_out:
            return False, "", False
        if same:
            return True, ("内核收下也摆上列表了，但一直没出画面：这支片的首帧可能就是这么慢"
                          "（大文件＋机械盘），也可能是解码器解不开——原话在 data\\logs\\mpv.log"), True
        return True, ("没放起来：内核没把这些文件收进播放列表（命令被那一边拒了，"
                      "原话在 data\\logs\\mpv.log）"), True
    if pl_now >= pl_before + count:
        return True, f"已追加 {count} 个文件（没打断当前播放）", False
    if not timed_out:
        return False, "", False
    grew = max(0, pl_now - max(0, pl_before))
    return True, (f"没追加成功：内核的列表还是 {pl_now} 条，一条都没收进去" if grew <= 0
                  else f"只追加了 {grew} 个（要 {count} 个）：剩下的命令内核没收下"), True


class MainWindow(QWidget):
    def __init__(self, cfg: cfgmod.Config, files: list[str] | None = None):
        super().__init__()
        self.cfg = cfg
        self.setObjectName("Root")
        self.setWindowTitle(cfgmod.APP_TITLE)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(MIN_W, MIN_H)
        self.resize(1180, 720)
        # 几何记忆（ui/win_geom.py）：`_geom_save_timer` 只在拖动/缩放**停下来**之后
        # 落盘，拖动途中一次都不写——否则一次拖窗就是几十次整份 config 写盘。
        self._start_max = False         # 启动时是否按存档恢复最大化（showEvent 里消费一次）
        self._geom_pre_fs = None        # 进全屏前的普通态矩形（全屏中关窗时用它存档）
        self._max_pre_fs = False
        self._geom_save_timer = QTimer(self)
        self._geom_save_timer.setSingleShot(True)
        self._geom_save_timer.setInterval(1200)
        self._geom_save_timer.timeout.connect(self._save_geometry)

        self.player = MpvPlayer(cfg, self)
        self._files = list(files or [])
        self._fs = False
        # 窗口置顶：配置是唯一事实，`_apply_topmost` 负责把它摆成 Z 序。
        # 不走 Qt 的 WindowStaysOnTopHint——那会重建顶层 HWND，挂在上面的 mpv 子窗口
        # 会变成孤儿（黑屏且救不回来），细节见 app/win32.py:set_topmost。
        self._topmost = bool(cfg["always_on_top"])
        # 进全屏之前侧栏开没开。`_side_saved` 记的是"有没有这笔账"，不是"侧栏开着"——
        # 用户在**全屏里**自己开合时会把账销掉，退出时就不再替他改回去。见 `_side_for_fs`。
        self._side_saved = False
        self._side_before = False
        self._drag_scrub = False
        self._last_pos = 0.0
        self._next_save = 0.0
        self._installed = False
        self._lib_scanned = False
        # 上一次键位落定时重画了几处提示（按键提示行 + 悬停提示），给判据核"刷新不是空转"。
        # 见 `_apply_keymap`。构造期是 0：一次都没刷过，这是事实而不是兜底值。
        self.last_hint_refresh = 0

        # ---- 布局：标题栏 / [视频区｜侧栏] / 控制条 ----
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 存下来是因为控制条会被**搬进搬出**这条布局（全屏浮层），搬回来要有一个明确的家。
        self.root_layout = root

        self.title = TitleBar(self, on_min=self.showMinimized, on_max=self._toggle_max,
                              on_close=self.close, cfg=cfg)
        root.addWidget(self.title)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        self.video = VideoSurface(self)
        self.empty = _EmptyPage(self)
        self.empty.sig_open_files.connect(self.open_files_dialog)
        self.empty.sig_open_dir.connect(self.open_dir_dialog)
        # 同一个格子叠放：并排放的话"内核已启动、列表还没建起来"那一瞬会左右各半，
        # 黑屏占掉半个窗口。叠放时谁可见谁占满，切换不留缝隙。
        self.stage = QWidget(self)
        stage = QGridLayout(self.stage)
        stage.setContentsMargins(0, 0, 0, 0)
        stage.addWidget(self.video, 0, 0)
        stage.addWidget(self.empty, 0, 0)
        body.addWidget(self.stage, 1)
        self.video.set_empty(True)      # 空状态：容器隐藏 ⇒ mpv 的子窗口一并消失，露出占位页

        # ---- 侧栏：一个槽位六页（播放列表 / 媒体库 / 调节 / 画面 / 均衡器 / 统计），顶部页签切换 ----
        self.panel = PlaylistPanel(self.player, self, cfg)
        self.panel.sig_play_index.connect(self.player.goto_index)
        self.panel.sig_remove_index.connect(self.player.remove_index)
        self.panel.sig_remove_rows.connect(self._remove_playlist_rows)
        self.panel.sig_move_steps.connect(self._apply_playlist_moves)
        self.panel.sig_clear.connect(self.player.clear_playlist)
        self.panel.sig_add_paths.connect(self.add_to_playlist)
        self.panel.sig_shuffle_toggle.connect(self._on_panel_shuffle)

        self.posters = PosterStore(cfgmod.FFMPEG_EXE, os.path.join(cfgmod.CACHE_DIR, "posters"),
                                   parent=self)
        self.library = Library(cfgmod.FFMPEG_EXE, os.path.join(cfgmod.DATA_DIR, "library.json"),
                               [str(r) for r in (cfg["library_roots"] or [])], parent=self)
        self.lib_panel = LibraryPanel(self.library, self.posters, cfg, self)
        self.lib_panel.sig_play_path.connect(lambda p: self.add_paths([p]))
        self.lib_panel.sig_play_list.connect(self.add_paths)
        self.lib_panel.sig_refresh.connect(lambda: self.library.refresh(force=True))
        self.lib_panel.sig_add_root.connect(self._lib_add_root)
        self.lib_panel.sig_remove_root.connect(self._lib_remove_root)
        self.lib_panel.sig_forget.connect(self._lib_forget)
        # 封面缓存的回收不在这里挂钩：`PosterStore` 自己按批回收（见 engine/posters.py 的
        # `_note_done`）。以前这里是"每张封面回来就 prune 一次全目录"，扫一个大库时
        # 全目录扫描要做成百上千遍——那是"一开媒体库就卡"里最实在的一笔账。

        self.tweak_panel = TweakPanel(self.player, cfg, self)
        self.tweak_panel.sig_status.connect(self._osd)

        # 画面页紧跟在调节页后面（索引 3，统计让到 4）：两页都是"改给内核看的东西"，
        # 分成两页而不是塞成一页，是因为它们的下发形状不同（一条属性 vs 一条滤镜链），
        # 混在一页里就会出现"同一个滑轨在页面上半段是即时生效、下半段要整链重发"。
        self.transform_panel = TransformPanel(self.player, cfg, self)
        self.transform_panel.sig_status.connect(self._osd)

        # 均衡器页紧跟在画面页后面（索引 4，统计让到 5）：第三、四、五页都是"改给内核看的东西"，
        # 分三页而不是合并，是因为**下发的形状与坏值的下场**各不相同——
        # 一条 GPU 属性（越界内核自己夹）/ 一条 vf 链（坏值＝画面不动、列表照显示）/
        # 一条 af 链（坏值＝整条起不来、一声不出）。合并就会在同一个页面上出现三种
        # "这个滑轨动了之后内核会怎么回答"，而用户没法从界面猜出脚下是哪一种。
        self.audio_eq_panel = AudioEqPanel(self.player, cfg, self)
        self.audio_eq_panel.sig_status.connect(self._osd)

        self.stats_panel = StatsPanel(self.player, cfg, self)

        # 睡眠定时：只有一份绝对截止时刻和一个"到点做什么"，对话框按需现造
        self.sleep = SleepTimer(self)
        self.sleep.sig_state.connect(self._osd)
        self.sleep.sig_expire.connect(self._on_sleep_expire)
        self.settings_dlg: SettingsDialog | None = None
        self.keymap_dlg: KeymapDialog | None = None
        # 截图工坊：GIF 导出走 ffmpeg（和缩略图/海报同一个二进制），连拍走内核 screenshot。
        # 对话框留着同一个实例（和设置页一个道理：改完能立刻接着改，宽度帧率这些数还在），
        # 连拍用一个常驻 QTimer + 剩余张数，不是一发一张地堆 singleShot（那样取消不了）。
        self.gif = GifStore(cfgmod.FFMPEG_EXE, self)
        self.gif_dlg: GifDialog | None = None
        self._burst_left = 0
        self._burst_timer = QTimer(self)
        self._burst_timer.setSingleShot(True)
        self._burst_timer.timeout.connect(self._burst_tick)

        self.sidebar = material.Surface(self, role="side")
        self.sidebar.setObjectName("Sidebar")
        sv = QVBoxLayout(self.sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)
        tabs = QWidget(self.sidebar)
        tabs.setObjectName("SidebarTabs")
        self.tab_strip = tabs
        # 一条纵向盒、每排是一个独立的横向盒（落位见 `_place_tab_rows`），而不是一张网格：
        # 页签排到超字号（`--font-scale 3.0` 实测单排最窄也要 ~700px，侧栏加宽上限 520）时要**换行**。
        # 上一版这里用的是 `QGridLayout`，五颗按钮共享同一套列，于是"每一排都排得开"
        # 并**不等于**"网格排得开"——4.0 档字号实测（tests/logs/probe-tab-rows-042303.log）：
        # 第 0 排要 223+175、第 1 排要 127×3，按排算最宽那排只要 416，可网格的列宽是
        # **各排同一列取最大**（223/175/127），真实需求 223+175+127＋边距 18＝543 > 520，
        # Qt 就把列压回 520，而按钮钉着固定宽不肯缩 ⇒「播放列表」占 10..233、
        # 「媒体库」从 210 起，两格又叠在一起 23×72px（logs/sweep-font4.0-*.log 那条"重叠"）。
        # 一排一个独立盒之后，列与列不互相牵连，`_layout_tabs` 返回的"最宽那排要多宽"
        # 才是网格真正会去要的那个数。一排的时候行为和以前一样：从左往右排，末尾一条弹性空白推到左边。
        th = QVBoxLayout(tabs)
        th.setContentsMargins(10, 8, 8, 2)
        th.setSpacing(2)      # 换行时两排之间的缝：比横向缝窄，看着还是一栏而不是一堆
        self.tab_row_margins = th.contentsMargins()
        self.tab_row_spacing = 4   # 每排内部的横向缝（`_layout_tabs` 按档位收，换排时重挂）
        self.tab_row_layout = th   # 纵向那条；横向的缝在每排的盒上，由 _place_tab_rows 落地
        self._tab_rows: list[QWidget] = []
        # 标签文字与 tooltip 都写全：页签上看到的是"播放列表"，悬停还有一句人话。
        # 那句人话**不带键名**：键名由 `form_rows.bind_key_tip` 按生效表现算（原来抄的是
        # 「（Ctrl+L）」这一族，用户改键之后它继续说旧键；而页签上的键恰好是没人背的那几把）。
        # 元组形状保持 `(标签, 说法)` 不变：好几把尺子按下标读 tab_defs[i][0] 认页签，
        # 加一位字段会让它们全部错位——键名因此只能挂在控件上，不能挂在这张表里。
        self.tab_defs: tuple[tuple[str, str], ...] = (
            ("播放列表", "这一场要放什么"),
            ("媒体库", "这台机器上有什么"),
            ("调节", "字幕 / 亮度色彩 / HDR / 声音"),
            ("画面", "比例 / 旋转 / 充满 / 增强"),
            ("声音均衡", "十段 / 前置 / 预设"),
            ("统计", "解码通路与实时读数"))
        # 与 tab_defs 一一对应的动作名（下标同一个页签）。为什么单独一张表而不是塞进
        # tab_defs：上面那条形状约束。两张表错位由判据负责抓（`tips_agree` 数颗数）。
        # 均衡器那一格是**空表**：这一页本轮没有新增快捷键（键盘入口在面板里留着
        # `bump()`，将来绑键只改键位表与这一格，不回来改页签文案）。空表意味着
        # `tip_note` 归成空串，也就是悬停里只有"十段 / 前置 / 预设"这一句人话。
        self.tab_actions: tuple[tuple[str, ...], ...] = (
            ("toggle_playlist",), ("toggle_library",), ("toggle_tweak",),
            ("toggle_transform",), (), ("toggle_stats",))
        self.tab_btns: list[QToolButton] = []
        for i, (label, tip) in enumerate(self.tab_defs):
            b = QToolButton(tabs)
            b.setObjectName("TabBtn")
            b.setText(label)
            form_rows.bind_key_tip(b, cfg, tip, self.tab_actions[i])
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _c=False, k=i: self._sidebar_page(k))
            self.tab_btns.append(b)
        # 这里不往布局里摆：摆哪一排是 `_place_tab_rows` 的活，而它由下面第一句
        # `_fit_sidebar()` 触发。构造期按钮暂时不在任何排里（还没 show，看不见）。
        sv.addWidget(tabs)
        self.sidebar_stack = QStackedWidget(self.sidebar)
        self.sidebar_stack.addWidget(self.panel)
        self.sidebar_stack.addWidget(self.lib_panel)
        self.sidebar_stack.addWidget(self.tweak_panel)
        self.sidebar_stack.addWidget(self.transform_panel)
        self.sidebar_stack.addWidget(self.audio_eq_panel)
        self.sidebar_stack.addWidget(self.stats_panel)
        self.sidebar_stack.setCurrentIndex(0)
        self.tab_btns[0].setChecked(True)
        # 换页淡入的"底色纱"挂在栈上（不是页上）：换页后它仍在最上层，退到 0 就收掉。
        # 减少动画时 PageFade.run() 自己什么都不做（见 ui/motion.py）。
        self.page_fade = motion.PageFade(self.sidebar_stack)
        sv.addWidget(self.sidebar_stack, 1)
        self.sidebar_width = self._clamped_sidebar_width(cfg["sidebar_width"])
        self._fit_sidebar()       # 排不开会自己加宽（见 _fit_sidebar），所以这里不再单独钉宽度
        self.sidebar.setVisible(False)
        body.addWidget(self.sidebar)

        self.bar = ControlsBar(self.player, cfg, self)
        root.addWidget(self.bar)
        # 全屏时把这条搬进浮层宿主（见 ChromeHost 那段：留在布局里就必然压矮画面一行）。
        # 宿主此刻是空的、隐藏的——"搬不搬"由 _apply_mode 按窗口态与配置键决定。
        self.host = ChromeHost(self)
        self.bar.sig_menu.connect(self._show_menu)
        self.bar.sig_status.connect(self._osd)
        self.bar.sig_toggle_playlist.connect(self.toggle_playlist)
        self.bar.sig_toggle_library.connect(self.toggle_library)
        self.bar.sig_toggle_tweak.connect(self.toggle_tweak)
        self.bar.sig_toggle_fullscreen.connect(self.toggle_fullscreen)
        self.bar.scrub.sig_drag_state.connect(self._on_scrub_drag)

        self.osd = Osd(self.video)

        # ---- 时间轴缩略图：抽帧在引擎侧排队，浮层只负责"有图就贴出来" ----
        self.thumbs = ThumbStore(cfgmod.FFMPEG_EXE, os.path.join(cfgmod.CACHE_DIR, "thumbs"),
                                 parent=self)
        self.preview = ScrubPreview(self.video)
        self._thumb_key = ""
        self._preview_at: tuple[float, int] | None = None
        self.thumbs.sig_done.connect(self._on_thumbs_done)
        self.bar.scrub.sig_hover_sec.connect(self._on_scrub_hover)
        self.bar.scrub.sig_hover_end.connect(self._on_scrub_hover_end)

        # ---- 引擎信号 → 界面 ----
        self.player.sig_title.connect(self._on_title)
        self.player.sig_file_loaded.connect(self._on_file_loaded)
        self.player.sig_duration.connect(self._on_duration)
        self.player.sig_time.connect(self._track_position)
        self.player.sig_log.connect(self._on_engine_log)
        self.player.sig_died.connect(self._on_engine_died)
        self.player.sig_saved.connect(self._on_saved)
        self.player.sig_eof.connect(self._on_eof)
        self.player.sig_playlist.connect(self._on_playlist)
        self.player.sig_pause.connect(self._on_pause_state)
        self.player.sig_wheel.connect(self._on_wheel)

        # ---- 视频区输入 ----
        self.video.sig_click.connect(self._on_video_click)
        self.video.sig_hover.connect(self._wake_ui)

        # ---- 全屏后的自动收起 ----
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._maybe_autohide)

        # ---- "已载入/已追加"那句话的凭据（见 open_receipt）----
        # claim ＝ 这一趟让内核干什么：条数、会不会换当前条目、第一条的绝对路径、
        # 发命令之前内核列表有几条。pl_len ＝ **内核推来的**列表长度（不是我们以为的）。
        self._open_claim: dict | None = None
        self._pl_len = 0
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(lambda: self._settle_open_receipt(timed_out=True))
        # 和视频一起拖进来的字幕：`sub-add` 要等 file-loaded 之后才认，先记在这里
        self._pending_subs: tuple[str, list[str]] | None = None   # (目标片头路径, 字幕表)
        # 内核握手的轮询（见 _begin_engine/_poll_engine_start）：40ms 一拍，直到 ok/失败。
        # 排队里的每一项是 (files, subs, replace)——原样保留"这一趟是替换还是追加"，
        # 否则内核启动期间连拖两批会把第一批的语义悄悄改掉。
        self._engine_timer = QTimer(self)
        self._engine_timer.setInterval(40)
        self._engine_timer.timeout.connect(self._poll_engine_start)
        self._pending_open: list[tuple[list[str], list[str], bool]] = []

        self.chrome = Chrome(self)
        self.chrome.on_dpi_changed = self._on_dpi_changed
        self._restore_geometry()
        self._install_shortcuts()

    # ================= 启动与生命周期 =================
    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._installed:
            self._installed = True
            self.chrome.install()      # 需要窗口已有 HWND，show 之后才保险
        elif not self.chrome._installed:
            self.chrome.install()      # 关窗时卸载过（见 closeEvent），再次显示要装回去
        self.chrome.refresh_frame()
        # 启动时兑现"上次置顶"：HWND 要等 show 之后才真、且这里不能抢焦点，
        # set_topmost 用 SWP_NOACTIVATE（用户可能正开着别的窗）。
        self._apply_topmost()
        if self._start_max:
            # 存档说上次是最大化：等窗口真的显示出来再切（在 showEvent 里直接切会递归）。
            # 只消费一次——用户之后手动还原，不许每次 show 又被拽回最大化。
            self._start_max = False
            QTimer.singleShot(0, self._restore_maximized)
        if self._files:
            files, self._files = self._files, []
            QTimer.singleShot(0, lambda: self.add_paths(files))
        if self.library.roots and not self.library.items() and not self._lib_scanned:
            # 延迟一点起扫：开机那 1 秒属于窗口/内核装配，媒体库探测（每个文件一次
            # ffmpeg）挤在同一时刻会把首帧拖慢，而它晚两秒没人会察觉。
            self._lib_scanned = True
            QTimer.singleShot(1500, lambda: self.library.refresh())

    def changeEvent(self, ev) -> None:
        """窗口态被**外力**改掉时把 `_fs` 拉回真值。

        为什么需要这条（2026-10-20 全屏审查 §3 点名）：全屏的 chrome 收成什么样，全凭
        `self._fs` 这一个布尔，而早先**全仓没有任何地方监听窗口态**。于是"外力把窗口从
        全屏里拽出来"（任务栏点击、Win+↓、别的进程调 showNormal、拖回普通态）之后，
        窗口已经是普通尺寸、`_fs` 却还留在 True ⇒ 标题栏被判定"该藏"、控制条被判定
        "到点就藏"，剩下的是一扇无边框、标题栏和控制条都没有的窗口——用户只能靠 Esc 出来。
        最小化不算退出全屏（恢复时 Qt 自己把 FullScreen 态带回来），所以只在
        "窗口可见、且既不是全屏也不是最小化"时才降级。
        """
        super().changeEvent(ev)
        if ev.type() == QEvent.WindowStateChange:
            # 最小化/恢复都走这一下：画面那块 30ms 的轮询表在任务栏里没有存在的理由
            # （B1 的账；Qt 把最小化的窗口仍当 visible，所以不能只靠 hideEvent）。
            self.video.sync_poll()
        if ev.type() != QEvent.WindowStateChange or not self._fs:
            return
        st = self.windowState()
        if st & Qt.WindowMinimized:
            self.host.veil()      # 最小化：宿主是**顶层窗**，不跟着收就会孤零零留在桌面上
            return
        if st & (Qt.WindowFullScreen | Qt.WindowMinimized):
            return
        if not self.isVisible():
            return
        self._fs = False
        self._apply_mode()
        QTimer.singleShot(0, self._save_geometry)
        self._sync_geometry_save_timer()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 浮层是顶层窗，不会跟着主人窗口自动挪（布局里的控件才会）。每次主窗尺寸变了都重贴一次。
        if self.host.is_hosting():
            self.host.place()
        self._schedule_geometry_save()

    def moveEvent(self, ev):
        super().moveEvent(ev)
        if self.host.is_hosting():
            self.host.place()
        self._schedule_geometry_save()

    # ================= 窗口几何记忆（ui/win_geom.py） =================
    def _restore_geometry(self) -> None:
        """按存档摆窗口；首次启动 / 存档坏了就按主屏居中放，绝不留 Windows 的默认位。

        位置和尺寸都在 `win_geom` 里整窗夹进可用区：用户看到的第一帧必须完整
        （标题栏在屏幕内、控制条不被任务栏切掉），而不是"先越界，再自己去拖"。
        """
        try:
            screens = win_geom.from_qt(QApplication)
            g = win_geom.plan(self.cfg.get("win_geom"), screens, (1180, 720), (MIN_W, MIN_H))
            if not g:
                d = win_geom.default_rect(screens, (1180, 720))
                g = (*d, False) if d else None
        except Exception as exc:                       # 读屏失败不许拖住启动
            print(f"[win_geom] 恢复失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            g = None
        if not g:
            return
        x, y, w, h, maxed = g
        # 屏比产品下限还小的极端情况下，把下限也压到屏幕内——否则 setGeometry 会被
        # minimumSize 弹回去，又变成"有一截在屏幕外"。
        self.setMinimumSize(min(MIN_W, max(1, w)), min(MIN_H, max(1, h)))
        self.setGeometry(x, y, w, h)
        self._start_max = bool(maxed)

    def _restore_maximized(self) -> None:
        if self._fs:
            return
        self.showMaximized()
        self.title.set_maximized(True)
        QTimer.singleShot(80, self.video.refit)

    def _remember_window_geometry(self) -> None:
        """把"普通态"的矩形写进配置（未落盘）。全屏/最大化时取 normalGeometry。"""
        if not self.isVisible():
            return
        g = self.normalGeometry()
        if self._fs and self._geom_pre_fs is not None and self._geom_pre_fs.isValid():
            g = self._geom_pre_fs
        if not g.isValid() or g.width() < 1 or g.height() < 1:
            g = self.geometry()
        if g.width() < 1 or g.height() < 1:
            return
        rec = {"x": int(g.x()), "y": int(g.y()), "w": int(g.width()), "h": int(g.height()),
               "max": bool(self.isMaximized() or (self._fs and self._max_pre_fs))}
        screen = QApplication.screenAt(g.center())
        if screen is not None:
            rec["screen"] = str(screen.name() or "")   # 只作诊断；规划器按矩形交集选屏
        self.cfg["win_geom"] = rec

    def _save_geometry(self) -> None:
        self._remember_window_geometry()
        self.cfg.save()

    def _schedule_geometry_save(self) -> None:
        """拖动/缩放停手 1.2 秒后落盘。最大化也存——存的是 normalGeometry()，下次还原
        按普通态尺寸回来；全屏不存（F11 不是用户对窗口大小的选择）。启动恢复（未显示）
        也不写，免得把用户的存档改成出厂值。"""
        if not self.isVisible() or self._fs:
            return
        self._geom_save_timer.start()

    def present_for_external(self) -> None:
        """外部打开（双击文件 / 单实例转交）时把窗口带到眼前，但**不许破坏全屏态**。

        接收方原先无条件 `showNormal()`：正在全屏时那一句把窗口拽回普通态，而 `_fs`
        留在 True（上面那条 changeEvent 之前没人监听），全屏的 chrome 状态就此错位。
        这里全屏中就 `showFullScreen()`（幂等，且从最小化回来时跨屏/DPI 由 Qt 自己算），
        普通态只在最小化时才 showNormal。
        """
        if self._fs:
            self.showFullScreen()
        elif self.isMinimized():
            self.showNormal()
        self.activateWindow()
        self.video.refit()

    def _begin_engine(self) -> bool:
        """让内核开始启动，但**不在 UI 线程里等握手**。返回 False 只在进程都拉不起来时。

        旧写法 `self.player.start(...)` 在 GUI 线程里等 IPC，最长 10 秒。正常启动实测
        0.42s（tests/out/engine.log），这 0.42s 里窗口不重画、鼠标转圈；内核真出问题时
        是整整 10 秒"未响应"。现在 Popen 立即返回，握手交给 40ms 的轮询
        （`_poll_engine_start`），等待期间窗口照常呼吸，文件先排队、能收命令时才发。
        """
        if self.player.running:
            return True
        self.video.set_empty(False)
        QApplication.processEvents()          # 让容器完成一次真实布局再取 HWND
        ok = self.player.begin_start(self.video.hwnd)
        if ok:
            self._engine_timer.start()
        return ok

    def _poll_engine_start(self) -> None:
        """内核握手的定时器：pending 就等下一拍，ok 才把排队的文件交给它。

        为什么要排队而不是在 add_paths 里直接 open_files：`loadfile` 走 IPC，管道没连上
        时只会失败；而"内核正在启动"和"内核坏了"外观完全一样，必须有一条路把这段时间
        的请求保留下来，并在成功后一次交出去（失败则统一收掉并说人话）。
        """
        state = self.player.poll_start()
        if state == "pending":
            return
        self._engine_timer.stop()
        if state != "ok":
            self._engine_failed("播放内核没能启动：原话在 data\\logs\\mpv.log")
            return
        self.video.refit()
        queue, self._pending_open = self._pending_open, []
        for files, subs, replace in queue:
            self._open_files_now(files, subs, replace)

    def _engine_failed(self, why: str) -> None:
        """启动失败的统一收口：排队的请求作废、占位页回来、把原因留在屏幕上看得到。"""
        self._pending_open.clear()
        self._engine_timer.stop()
        self.video.set_empty(True)
        self.empty.setVisible(True)
        self.empty.set_notice(why, sticky=True)
        self._osd("播放内核启动失败")

    def _cancel_pending_open(self) -> None:
        """关窗/退出时把"还在等内核"的那一批收掉（见 closeEvent）。

        定时器是 self 的孩子，窗口拆掉时 Qt 也会停，但**排队里的路径和 pending 回调**
        得在我们还活着的时候清掉；否则关窗那一瞬的内核启动失败会去碰一扇已经没了的窗。
        """
        self._engine_timer.stop()
        self._pending_open.clear()

    def _open_files_now(self, files: list[str], subs: list[str], replace: bool) -> None:
        """内核已就绪后真正下发的这一段（从 add_paths 原样搬出来，只做一件事）。"""
        # 播起来了就把上一趟"为什么没放起来"收掉：那句话解释的是空状态，画面都出来了还挂着，
        # 就成了新的误导。
        self.empty.set_notice("")
        self.empty.setVisible(False)
        res = self.player.open_files(files, append=not replace, remember=True) or {}
        # 这一趟会换当前条目（plays=True）才等 file-loaded 再挂字幕；往正在播的队尾
        # 追加时根本不会来 file-loaded（见 open_files 的说明），字幕就挂当前这一支。
        # 等回执的这一档必须带上**目标片的头路径**：快速 A→B 连切时，A 的 file-loaded
        # 可能晚到，只认"当前路径对得上哪一批"，否则 A 的字幕会被挂到 B 上。
        if int(res.get("count", 0)) > 0 and subs and bool(res.get("plays")):
            self._pending_subs = (str(res.get("head", "")), list(subs))
        else:
            self._pending_subs = None
            if int(res.get("count", 0)) > 0 and subs:
                self._attach_subtitles(subs)
        # 这句"已载入/已追加"不再当场印：以前它紧跟在 open_files() 后面无条件印出去，而
        # open_files 只是把命令塞进写队列——2026-10-19 内核把整条 loadfile 拒了（`argument
        # index has incompatible type`），屏幕上黑的、气泡却写着"已载入 1 个文件"，用户那句
        # "只是弹出一个提示说加载了而已"就是这一条。现在改等内核自己的回执（见方法注释）。
        self._arm_open_receipt(int(res.get("count", 0)), bool(res.get("plays")),
                               str(res.get("head", "")))

    def _log_shutdown(self, readings: dict) -> None:
        """把这一趟收口的读数落到 data/logs/shutdown.log（写完 fsync）。

        为什么要单独留一份：`poolkit.shutdown` 之后如果还有作业没走完，它的异常是被
        **有意吞掉**的（吞掉是为了不再让一次回报带走整条进程）。吞掉不等于没发生——
        没有这一行日志，"这回放没放过"就只能靠复跑去猜。验收脚本读的是进程内 `stats()`，
        这一份是给"用户已经关了、我事后想知道"准备的。

        记日志本身不许再把退出弄死，所以整块兜在 OSError 里；兜住之后什么都不做，
        因为此时能做的只剩继续关窗口。
        """
        try:
            os.makedirs(cfgmod.LOG_DIR, exist_ok=True)
            with open(os.path.join(cfgmod.LOG_DIR, "shutdown.log"), "a",
                      encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                         f"收口={readings}\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    def closeEvent(self, ev):
        # 先收自己的尾巴，再谈退出：导出的那一趟 ffmpeg 是线程池里的子进程，窗口的生死
        # 管不着它——真机实测（tests/test_gif_real.py --residue）关掉窗口之后 pid 还在，
        # 产物从 0 字节一路写到 5 MB，用户以为"取消了"其实是"关掉了个还在后台写盘的导出"。
        # 连拍同理：定时器还 armed，下一次 tick 会去催一个已经不存在的窗口截帧。
        self._burst_left = 0
        self._burst_timer.stop()
        # 内核握手还在途时关窗：排队里的路径要在这里作废，别让下一拍 poll 去碰一扇
        # 已经开始拆的窗口（定时器是 self 的孩子，但排队的元组不会自己消失）。
        self._cancel_pending_open()
        self.gif.cancel()
        # 线程池的收口必须在"窗口还没拆、store 还活着"的时候做，而不是留给解释器去收尾：
        # 实测两桩都出在这一瞬——① 作业还在途就退进程 ⇒ 15 趟里 8 趟 0xC0000409（PyQt 把
        # 从 C++ 回调进来的异常直接 abort，stdout/stderr 全空，用户那边就是"播放器突然没了"）；
        # ② 作业还在跑时拆 store ⇒ ~QThreadPool 攥着 GIL 等工作线程，而工作线程正等 GIL，
        #    主线程死锁（六份 faulthandler 栈一模一样）。机制与四条件写在 app/engine/poolkit.py。
        # grace 只给 1.5s：关窗口不该让人等第二部片子的缩略图。到点还没收干净的那条会被点名，
        # 由 emit/pin 兜住最坏结果（一条被吞掉的回报），不再是一次整进程。
        readings = poolkit.shutdown(SHUTDOWN_GRACE_MS)
        self._log_shutdown(readings)
        self._geom_save_timer.stop()
        self._remember_window_geometry()
        path = str(self.player.prop("path", ""))
        if path:
            self.cfg.remember_position(path, self._last_pos,
                                       float(self.player.prop("duration", 0) or 0))
        if self.player.running:
            self._remember_playback_state()
        self.cfg.save()
        win32.keep_awake(False)
        self.chrome.uninstall()
        # 无条件 quit：看门狗在"内核刚死、还没拉起下一趟"的那 0.35s 里会走到重启分支，
        # 旧写法只在 running（= _started）时 quit，正好把这一档漏掉，窗口没了内核/声音还在。
        # quit() 自己会置 _quitting 并杀掉可能还活着的进程，没起过内核时也是无害的空操作。
        self.player.quit()
        super().closeEvent(ev)

    def _remember_playback_state(self, kernel=None) -> dict:
        """关窗那一瞬，把这一趟的播放开关从**内核真值**回写成配置，返回五样读数。

        为什么读内核而不是读界面意图：界面只代表"用户点过什么"，而音量/静音/倍速/循环/
        乱序都会被内核自己（滚轮、快捷键、文件属性）改掉；配置要记住的是用户最后听到、
        看到的那一个。`build_args` 下次启动再把这些带回内核，两趟才闭环。

        为什么单独立成方法：这段跑在 `closeEvent`（Qt 槽）里，槽里抛一个 ValueError 不是
        traceback 而是**进程当场 abort**。所以每个值都过类型收口，且能被离线判据拿一个
        假内核逐项喂（tests/test_session_state.py）。`kernel` 参数只给判据用。

        两条旧账：
          · volume 旧写法 `int(prop(...) or 100)` 把**真值 0** 当成"没读到"，下次启动从
            静音变回 100——用户把音量拖到底，重启又响了。
          · mute/shuffle 旧写法 `bool(prop(...))` 遇到字符串 "no"/"false" 会得 True
            （见 engine/player.py:kernel_flag），"没开"被写成"开"。
        """
        p = self.player if kernel is None else kernel
        cfg = self.cfg
        vol = _i(p.prop("volume", cfg["volume"]), int(cfg["volume"]))
        vmax = _i(cfg.get("volume_max", 200), 200)
        speed = _f(p.prop("speed", cfg["speed"]), 1.0)
        if speed <= 0.0:                     # 内核没有媒体时可能回 0；--speed=0 启动会被拒
            speed = 1.0
        state = {
            "volume": max(0, min(max(1, vmax), vol)),
            "mute": kernel_flag(p.prop("mute", cfg["mute"]), bool(cfg["mute"])),
            "speed": round(min(speed, 16.0), 3),
            "shuffle": kernel_flag(p.prop("shuffle", cfg["shuffle"]), bool(cfg["shuffle"])),
            "loop_playlist": self._loop_mode_from_kernel(p),
        }
        for key, value in state.items():
            cfg[key] = value
        return state

    def _loop_mode_from_kernel(self, kernel=None) -> str:
        p = self.player if kernel is None else kernel
        lf = str(p.prop("loop-file", "no") or "no").lower()
        lp = str(p.prop("loop-playlist", "no") or "no").lower()
        if lf not in ("no", "false", "0", ""):
            return "one"
        if lp not in ("no", "false", "0", ""):
            return "inf"
        return "no"

    # ================= 快捷键 =================
    def _install_shortcuts(self) -> None:
        """按**生效表**装快捷键；重装前先把旧的卸干净。

        为什么不能只 `self._shortcuts = []`：QShortcut 是 self 的子对象，把列表一丢它并
        没有死，仍然挂在窗口上继续响应——于是"编辑完键位重装一次"以后按一次键会触发
        两次，而且第一次跑的正是用户刚删掉那条。这个症状（一次按键两件事）比装不上更难
        查，所以 tests/test_ui.py 里有一条专门数触发次数。

        为什么是 `setEnabled(False)` 而不是 `clearShortcut()`：PyQt5 5.15.2 的 QShortcut
        **没有** clearShortcut 这个方法（本机 hasattr 实测 False，C++ 头文件里有、SIP 没包）。
        写在槽里的 AttributeError 不是 traceback，是**进程当场没了**（rc=127、stderr 全空），
        症状正好是"保存完快捷键播放器闪退"。摘父 + deleteLater 负责真把它从窗口上拿掉。
        """
        for sc in getattr(self, "_shortcuts", None) or []:
            sc.setEnabled(False)
            sc.setParent(None)
            sc.deleteLater()
        self._shortcuts = []
        self.key_rows = keymap.effective(self.cfg.get("keymap"))
        for seq, act, _desc in self.key_rows:
            sc = QShortcut(QKeySequence(seq), self, context=Qt.WindowShortcut)
            sc.activated.connect(lambda checked=False, a=act: self.dispatch(a))
            self._shortcuts.append(sc)
        self._install_gif_shortcuts()

    def _install_gif_shortcuts(self) -> None:
        """截图工坊还开着时，把「该照样响」的那几把键在**它自己那份**上重装一遍。

        为什么要装两份：`QShortcut(context=WindowShortcut)` 只认当前活动窗口。真机实测
        （tests/probe_gif3.py 的 B1–B4）：工坊一开，把按键递给主窗，命中的动作是 `[]`；
        把主窗 `activateWindow()` 抢回来立刻又是 `['burst_shot']`。也就是说用户开着工坊
        想按空格暂停、按 B 连拍，得先用鼠标点一下播放器——「工具窗吞掉整张键位表」这件事
        在别的播放器里不是这样的（PotPlayer/迅雷的截图窗开着照样能放停）。

        为什么动作落在对话框那边、这里只递两样东西：键位事实仍然只有 `self.key_rows`
        一份（递进去的是筛好的行），命中后交回 `self.dispatch`（不另建第二张派发表）；
        而 QShortcut 的**父对象必须是那个窗口**，才在它成为活动窗时收得到键。
        """
        dlg = self.gif_dlg
        if dlg is None:
            return
        dlg.install_owner_keys(self.key_rows, self.dispatch)

    def dispatch(self, action: str) -> None:
        b = self.bar
        t = self.tweak_panel
        g = self.transform_panel
        transport = ("toggle_play", "prev", "next", "stop", "screenshot", "shuffle", "loop",
                     "mute", "vol_up", "vol_down", "speed_down", "speed_up", "speed_reset",
                     "step_back", "step_fwd", "back5", "fwd5", "menu_sub", "menu_audio",
                     "media_info")
        if action in transport and not self.player.running:
            return
        if action in KERNEL_ONLY and not self.player.running:
            self._osd("播放内核还没启动")
            return
        if action == "toggle_play":
            b.toggle_play()
        elif action == "prev":
            b.prev()
        elif action == "next":
            b.next()
        elif action == "prev_chapter":
            self._chapter_step(-1)
        elif action == "next_chapter":
            self._chapter_step(1)
        elif action == "stop":
            b.stop()
        elif action == "screenshot":
            b.screenshot()
        elif action == "burst_shot":
            self.start_burst()
        elif action == "export_gif":
            self._open_gif()
        elif action == "shuffle":
            b.toggle_shuffle()
        elif action == "loop":
            b.cycle_loop()
        elif action == "mute":
            b.toggle_mute()
        elif action == "vol_up":
            b.adjust_volume(5)
        elif action == "vol_down":
            b.adjust_volume(-5)
        elif action == "speed_down":
            b.cycle_speed(False)
        elif action == "speed_up":
            b.cycle_speed(True)
        elif action == "speed_reset":
            self.player.set_speed(1.0)
            self._osd("速度 1x")
        elif action == "step_back":
            b.step(True)
        elif action == "step_fwd":
            b.step(False)
        elif action == "back5":
            self.player.seek_rel(-5)
            self._osd("-5 秒")
        elif action == "fwd5":
            self.player.seek_rel(5)
            self._osd("+5 秒")
        elif action.startswith("jump_"):
            self._jump_pct(int(action.split("_")[1]))
        elif action == "menu_sub":
            self._show_menu("sub", b.btn_sub)
        elif action == "menu_audio":
            self._show_menu("audio", b.btn_audio)
        elif action == "menu_device":
            self._show_menu("device", b.btn_tweak)
        elif action == "sub_delay_down":
            t.step("sub-delay", False)
        elif action == "sub_delay_up":
            t.step("sub-delay", True)
        elif action == "sub_size_down":
            t.step("sub-scale", False)
        elif action == "sub_size_up":
            t.step("sub-scale", True)
        elif action == "tone_cycle":
            t.cycle_choice("tone-mapping")
        elif action == "eq_reset":
            t.reset_eq()
        elif action == "ab_a":
            t.mark_ab("a")
        elif action == "ab_b":
            t.mark_ab("b")
        elif action == "ab_clear":
            t.mark_ab("clear")
        # 画面那一族：每一个都只调 transform_panel 的方法（和上面 eq/ab 同一个理由——
        # 一份逻辑才不会出现"按钮改了内核没改"或"键盘的 OSD 文案和面板对不上"）。
        elif action == "rotate_cycle":
            g.cycle("rotate")
        elif action == "aspect_cycle":
            g.cycle("aspect_override")
        elif action == "crop_cycle":
            g.cycle("crop_ratio")
        elif action == "keepaspect_toggle":
            g.toggle("keep_aspect")
        elif action == "mirror_h_toggle":
            g.toggle("mirror_h")
        elif action == "mirror_v_toggle":
            g.toggle("mirror_v")
        elif action == "panscan_toggle":
            g.toggle_panscan()
        elif action == "transform_reset":
            g.reset_all()
        elif action == "toggle_tweak":
            self.toggle_tweak()
        elif action == "toggle_transform":
            self.toggle_transform()
        elif action == "toggle_stats":
            self.toggle_stats()
        # 置顶不需要内核在场（纯窗口态），所以既不进 transport 也不进 KERNEL_ONLY：
        # 内核没起来时按 Shift+W 照样该把窗口钉在最前。
        elif action == "always_on_top":
            self.toggle_topmost()
        elif action == "sleep_timer":
            self._open_sleep()
        elif action == "settings":
            self._open_settings()
        elif action == "keymap_edit":
            self._open_keymap()
        elif action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "exit_fullscreen":
            if self._fs:
                self.toggle_fullscreen(False)
        elif action == "toggle_playlist":
            self.toggle_playlist()
        elif action == "toggle_library":
            self.toggle_library()
        elif action == "open_files":
            self.open_files_dialog()
        elif action == "open_dir":
            self.open_dir_dialog()
        elif action == "media_info":
            self._show_media_info()
        elif action == "help_keys":
            self._show_help()
        elif action == "quit":
            self.close()

    # ================= 打开 =================
    def add_paths(self, paths: list[str], replace: bool = True) -> None:
        # 字幕不是片源：旧写法把拖进来的 .srt 也交给 expand()，于是回一句
        # "没有找到可播放的文件"，字幕根本不进内核。拖字幕**现在正在播的那支片**上，
        # 正确语义是 sub-add；和视频一起拖时不能立刻发（内核还没 file-loaded），
        # 记进 _pending_subs，由 _on_file_loaded 接上。
        subs = [p for p in paths if is_sub_file(p)]
        rest = [p for p in paths if not is_sub_file(p)]
        files: list[str] = []
        for p in rest:
            files += self.player.expand(p)
        if not files:
            if subs:
                self._attach_subtitles(subs)
                return
            self._osd("没有找到可播放的文件")
            return
        if not self.player.running:
            # 内核还没就绪（第一趟双击/拖放）：请求原样排队，Popen 立刻返回，握手交给
            # 40ms 轮询。不能在这里 open_files——管道没连上时 loadfile 只会被拒。
            self._pending_open.append((list(files), list(subs), bool(replace)))
            if not self._begin_engine():
                self._pending_open.clear()
                self._engine_failed("播放内核没能启动：原话在 data\\logs\\mpv.log")
                return
            self.empty.set_notice("")     # 先收起上一趟"为什么没放起来"的解释
            self.empty.setVisible(False)
            self._osd("正在启动播放内核…")
            return
        self._open_files_now(files, subs, replace)

    def _arm_open_receipt(self, count: int, plays: bool, head: str) -> None:
        """记下"这一趟该听见内核的哪一声"，然后等（分工见 `open_receipt`）。

        `before` 取的是**内核上一次推来的**列表长度，不是我们以为的那一份：追加那一路的
        凭据就是"它自己说列表变长了"。
        """
        if count <= 0:
            self._open_claim = None
            self._restore_empty("没放起来：这批路径在界面上存在，但递给内核时被变成了空列表")
            return
        self._open_claim = {"n": count, "plays": plays, "head": head, "before": self._pl_len}
        self._open_timer.start(OPEN_RECEIPT_MS)
        # 到这里就收手：下面那一句"已载入/已追加"由 _settle_open_receipt 说，而它只会被
        # 内核的推送（file-loaded／playlist 变长）或超时叫醒。当场说话＝回到那句谎。

    def _settle_open_receipt(self, loaded: bool = False, timed_out: bool = False) -> None:
        """只有三种时刻会走到这里：内核的 file-loaded 到了、内核推的列表变长了、超时了。

        claim **只在真的说话那一支才摘**（2026-10-20 订正；R20 真机那条「气泡=''」的根因）。
        旧写法第一行就是 `self._open_claim = None`，理由写的是"一次回执只许说一遍"——可它
        顺手把"这一趟还没听到证据"的那一支也一起摘了。而 `_on_playlist` 是**每一条 playlist
        推送**都会走进来的，内核在追加这一趟里推的不止一条、也不都变长（本机实测：一次初始
        载入里 `playlist` 连推 4 次、长度全是 2，见 `tests/out/probe-append-receipt-hidden2.log`
        的推送计数）。于是坏形状是：一条"没变长"的推送把 claim 吃掉却什么也没说
        （`open_receipt` 那一边返回 `say=False`），随后那条真变长的推送没人认账 ⇒ 界面上那句
        话永远不出现，连 20 秒后的超时那一声也没有了（claim 已没了，超时进来第一行就 return）。
        用户看到的正是"点了添加，什么表示都没有"——而列表其实明明加上了。
        留账不会留成长挂：`say=False` 只可能出现在 `not timed_out` 那一支（`open_receipt` 里
        超时后两条分支都必有话），表还在走，最迟 `OPEN_RECEIPT_MS` 一定落一个结局。
        "只许说一遍"这条老规矩没撤：说了才摘，之后再有推送进来，claim 已是 None，第一行就返回。
        """
        claim = self._open_claim
        if claim is None:
            return
        say, text, bad = open_receipt(claim["n"], claim["plays"], claim["head"],
                                      claim["before"], self._pl_len,
                                      str(self.player.prop("path", "") or ""),
                                      loaded, timed_out)
        if not say:
            return                       # 证据没到：话不说，账也不销，等下一声或超时
        self._open_claim = None          # 摘在说之后：同一次回执还是只许说一遍
        self._open_timer.stop()
        if bad:
            self._restore_empty(text)
        self._osd(text)

    def _restore_empty(self, why: str) -> None:
        """没放起来就不许让占位页继续藏着——那句话就是说给"这一屏什么都没有"的用户听的。"""
        if self._pl_len:
            return
        self.video.set_empty(True)
        self.empty.setVisible(True)
        self.empty.set_notice(why)

    def add_to_playlist(self, paths: list[str]) -> None:
        """播放列表那一页的"添加媒体文件"与"把文件拖到列表上"＝**追加**。

        为什么它要单独立一个槽位，而不是复用 `add_paths`：这两条以前直接连到
        `add_paths`（`replace` 默认 True），于是用户在一支十几条的列表上点"添加"，
        按钮说的是"添加"，做的却是"整表换成新挑的那几个"——列表被清空、正在播的那条
        也没了。同一句话在窗口上（拖到画面＝"打开这些"）和标题栏"打开文件"里仍是替换，
        那是"打开"的语义，没改。判据在 tests/test_ui.py 里把这两条钉在一起：
        列表那条 append=True 且不抢当前条目，窗口那条仍整表替换。
        """
        self.add_paths(paths, replace=False)

    def open_files_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "打开媒体文件", os.path.expanduser("~"),
                                                "媒体文件 (*)")
        if paths:
            self.add_paths(paths)
    def open_dir_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "打开文件夹", os.path.expanduser("~"))
        if d:
            self.add_paths([d])

    # ================= 视图切换 =================
    def toggle_fullscreen(self, on: bool | None = None) -> None:
        want = (not self._fs) if on is None else bool(on)
        if want and not self._fs:
            # 进全屏前先留一份普通态矩形与"原来是不是最大化"：全屏中关窗时，
            # normalGeometry() 在不同窗口管理器下不一定还可靠，而这两个值是我们自己记的。
            self._geom_pre_fs = self.normalGeometry()
            if not self._geom_pre_fs.isValid():
                self._geom_pre_fs = self.geometry()
            self._max_pre_fs = self.isMaximized()
        self._fs = want
        self._apply_mode()
        if not self._fs:
            self._geom_pre_fs = None
            self._max_pre_fs = False
            QTimer.singleShot(0, self._save_geometry)
        self._sync_geometry_save_timer()

    def _sync_geometry_save_timer(self) -> None:
        if self._fs or not self.isVisible():
            self._geom_save_timer.stop()

    # ---- 窗口置顶 ----
    def _apply_topmost(self) -> None:
        """把 Z 序摆成 `_topmost` 此刻该有的样子：不碰配置、不出声。

        只在窗口可见时动手——`winId()` 背后得有一个真 HWND。进/出全屏、启动显示、
        用户显式切换都从这里兑现；`_apply_mode` 里再喊一次，是因为全屏切换会让
        Windows 重排一次 Z 序，置顶态不该"进一趟全屏就悄悄丢了"。
        """
        if not self.isVisible():
            return
        win32.set_topmost(int(self.winId()), self._topmost)

    def set_topmost(self, on: bool) -> None:
        """置顶开关的唯一落地处：快捷键、右键菜单、"更多"菜单都只调这一支。"""
        on = bool(on)
        if on == self._topmost:
            return
        self._topmost = on
        self.cfg["always_on_top"] = on
        self.cfg.save()
        self._apply_topmost()
        self._osd("窗口已置顶" if on else "已取消置顶")

    def toggle_topmost(self) -> None:
        self.set_topmost(not self._topmost)

    @property
    def _float_bar(self) -> bool:
        """配置键 `float_controls_in_fs`：全屏时控制条浮在画面上（True）还是占一行布局（False）。

        这个键**曾经是个没人读的摆设**（和 acrylic / max_recent 一起被删过，见 config.py 那段），
        现在它有了唯一的读者就是这里，也就有了真的效果——关掉它随时退回旧行为，
        这是"浮层在别的机器上万一不弹菜单"那条退路，不是一句"我们试过了应该没问题"。
        """
        return bool(self.cfg["float_controls_in_fs"])

    def _apply_mode(self) -> None:
        """把 chrome 摆成"`_fs` 此刻该有的样子"。进出全屏、改配置键都走这一个出口。"""
        fs = bool(self._fs)
        self.chrome.set_fullscreen(fs)
        self.bar.set_fullscreen(fs)
        self.title.setVisible(not fs)
        if fs and self._float_bar:
            self.host.take(self.bar)          # 搬出根布局：舞台这才可能等于整块屏
        else:
            b = self.host.release()           # 搬回去（没搬过就回 None，release 幂等）
            if b is not None:
                self.root_layout.addWidget(b)   # addWidget 顺手把它认回主窗当父
        self._side_for_fs(fs)                   # 侧栏也要让位，否则舞台只是从"矮一条"换成"窄一栏"
        self._set_bar_shown(True)
        if fs:
            self._arm_autohide()
        else:
            self._hide_timer.stop()
        self.video.refit()
        self._apply_topmost()

    def _on_dpi_changed(self) -> None:
        """跨屏 100%↔175%：重铺 mpv 的原生子窗口。

        Qt 会按 WM_DPICHANGED 的建议矩形改自己的逻辑几何，但 `--wid` 嵌进来的子
        窗口是 mpv 建的原生窗，不会跟着 Qt 缩放；强制铺一遍（force 绕过"几何键没变"
        的跳过），按新的物理客户区把宽高校准回来。异常必须吞掉：这是在原生消息的
        定时回调里跑，抛出去会 abort 整个进程。
        """
        try:
            self.video.refit(force=True)
        except Exception:
            pass

    def _side_for_fs(self, fs: bool) -> None:
        """全屏把侧栏收起、退出全屏再还给用户。

        为什么这一笔和浮层是同一笔债的两半（2026-10-20 全屏审查 §6）：控制条占行压的是**纵向**
        （舞台矮一条 ⇒ 16:9 按高适配 ⇒ 左右留黑），侧栏排在 body 那条 QHBoxLayout 的左列、压的是
        **横向**（舞台窄一栏 ⇒ 按宽适配 ⇒ 上下留黑）。用户那句"全屏之后周围显示不全"两边都得让位
        才算修完；只搬浮层不收侧栏，3840 宽屏上开着 260 的侧栏就还剩 3580 的盒子，照样不是整块屏。
        快照只记"进全屏之前开没开"这一位：停在哪一页不动它，退出时回到那一页。
        全屏里用户显式开合（`_set_sidebar`）时那笔账被销掉——显式操作优先于进出全屏的默认值，
        退出时不把他刚打开的东西强行收掉。
        """
        if fs:
            if not self._side_saved:
                self._side_saved = True
                self._side_before = self.sidebar.isVisible()
            if self.sidebar.isVisible():
                self.sidebar.setVisible(False)
                self._sync_tab_lights()       # 控制条上那三盏灯别留着亮着（页开着却看不见＝假状态）
            return
        if self._side_saved:
            self._side_saved = False
            if self._side_before:
                self.sidebar.setVisible(True)
                self._sync_tab_lights()

    def _set_bar_shown(self, on: bool) -> None:
        """控制条出现/收起。**搬进浮层之后不能再只动 bar 的 setVisible**：
        条住在宿主里时只把条藏掉，宿主那块顶层窗还留着——一块又挡鼠标又没人认领的空底板，
        形态是"全屏里画面下面凭空多一条按了没反应的黑边"。所以收的时候连宿主一起收。"""
        if on:
            self.bar.setVisible(True)
            if self.host.is_hosting():
                self.host.reveal()
        else:
            self.bar.setVisible(False)
            self.host.veil()

    def _toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.title.set_maximized(self.isMaximized())
        QTimer.singleShot(80, self.video.refit)   # 最大化动画落定后再铺一次 mpv 子窗口
        QTimer.singleShot(0, self._save_geometry)
        self._sync_geometry_save_timer()

    # ================= 侧栏（播放列表 / 媒体库 / 调节 / 画面 / 统计） =================
    TAB_PAD = (10, 4)       # 页签左右留白两档：宽档好看，窄档保证 240（设置页下限）也排得开
    TAB_SPACING = (4, 2, 0)  # 留白已经收到最窄还排不开时，再收按钮之间的缝（肉眼看不出差别）

    def _clamped_sidebar_width(self, raw) -> int:
        """把配置里的侧栏宽度收进设置页那档量程（量程只有一份出处：`settings_dialog.INT_RANGE`）。

        管的是**用户意图**那一头：设置页的 spinbox 夹得住"人按出来的值"，但 `config.json`
        是用户能手改的（README 与 `python -m app.doctor` 都教人改它）。收口之后
        `self.sidebar_width` 才是那个"能在设置页里原样显示回来"的数——不然盘上写 99999、
        设置页显示 520，用户一应用就把他真正想要的东西改掉了一次。
        另一头由 `_fit_sidebar()` 管（排不开就加宽、最多加到量程上限），两边不重复兜同一件事。
        盘上那份不改：下次打开设置页时 spinbox 显示收口后的值，用户一应用就自然对齐。
        """
        lo, hi = INT_RANGE["sidebar_width"]
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return int(cfgmod.DEFAULTS["sidebar_width"])
        return max(lo, min(hi, v))

    @staticmethod
    def _wrap_rows(ws: list[int], avail: int, spacing: int) -> list[list[int]]:
        """贪心装箱：从左到右，当前排还塞得下就接着塞，塞不下就另起一排；返回每排的下标列表。

        单格比整排可用宽还宽时它**独占一排**（不是"挤进去裁一半"，也不是"永远排不进去"）——
        `avail` 很小（侧栏 240 而超字号下一个页签就要 300）时这一档会真的出现。
        """
        rows: list[list[int]] = []
        cur: list[int] = []
        used = 0
        for i, w in enumerate(ws):
            add = w if not cur else spacing + w
            if cur and used + add > avail:
                rows.append(cur)
                cur, used = [i], w
            else:
                cur.append(i)
                used += add
        if cur:
            rows.append(cur)
        return rows

    def _place_tab_rows(self, rows: list[list[int]]) -> None:
        """把页签按 `rows` 摆成"每排一个独立横向盒"，排尾挂一条弹性空白（＝把这一排推到左边）。

        为什么每排要各自一个盒、不共享一张网格的列：共享列时"最宽那排要多少"这个数
        **不是**布局真正要的那个数（列宽按各排同一列取最大，跨排互相牵连），
        `_layout_tabs` 报 416 而网格实际要 543，侧栏上限 520 ⇒ 列被压窄、固定宽的按钮不肯缩，
        两格压在一起 23×72px（4.0 档字号实测，见 __init__ 里那段与 logs/sweep-font4.0-*.log）。
        拆开之后每一排只算自己那几颗，返回的数就是布局要的数。

        为什么每次都重摆、不判断"行数变了才动"：`_fit_sidebar` 本来就要排两遍（先按用户宽度问
        需求、加宽之后再落一次留白档位），两遍的行数可能不同；"只在变化时动"要多存一份
        "上次是几排"的状态——那份状态本身就是个会记错的数，
        记错了就是"侧栏都加宽了页签还赖在两排"这种看得见的坏。
        """
        host = self.tab_row_layout.parentWidget()
        for r in self._tab_rows:
            self.tab_row_layout.removeWidget(r)
            r.setParent(None)
            r.deleteLater()                  # 交给 Qt 在事件循环里回收，正在被重排的按钮不会悬空
        self._tab_rows = []
        for idxs in rows:
            if not idxs:
                continue
            box = QWidget(host)
            rl = QHBoxLayout(box)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(self.tab_row_spacing)
            for i in idxs:
                rl.addWidget(self.tab_btns[i])
            rl.addStretch(1)
            self.tab_row_layout.addWidget(box)
            self._tab_rows.append(box)

    def _layout_tabs(self, sidebar_width: int, wrap: bool = False) -> int:
        """按侧栏宽度决定页签的留白与按钮间距，并把每个按钮钉在「文字＋留白」和「样式自己要的宽」的大值上。

        为什么宽度要自己钉、不交给布局：Qt 给 QToolButton 的 sizeHint 在文字与 QSS padding 之外
        还自己加了 11px（本机实测 12px 字号下「播放列表」文字 48px、sizeHint 79px），
        五个页签就是 55px——正好把默认 300px 的侧栏顶到放不下，于是布局把每个按钮压回 53px，
        字被左右各切掉半截（这张图在 tests/out/sweep/sidebar-*.png 里留档过。那句 55px 是
        五页签时代的读数：2026-10-12 加第六页之后每格仍带同样的 11px 松弛，只是变成 6×11，
        "松弛按格数累加"这条算式没变，所以宽度照旧由这里钉）。
        钉成"文字＋留白"之后，这一排要多宽是个**能算的数**，两档留白让设置页的 240 到 520
        都排得开，也就不需要"嫌窄就把用户设的宽度悄悄改大"那种事。

        为什么还要取 `sizeHint` 那个大值（240 那一档才起作用）：留白收到 4px 之后
        「文字＋2×留白」= 文字＋8，比 Qt 自己报的 59/47/35/35/35 还窄 3px。这一回**没有**切字
        （QSS 的 padding 已经是 0，整格都给字用），但它仍然是"布局被迫压破控件的最小需求"——
        换一台字号更大的机器（125%/175% 缩放、或以后界面字体可自定义）那 3px 就会变成真的切字。
        所以宁可把缝从 4px 收到 2px（240 下这一排 237px，仍排得开），也不留一个"靠运气不切字"的形状。

        `wrap` 是这一版新加的那一级，**默认 False**：只按单排算，排不开就把每格钉在最窄档、
        老实返回"单排要多宽"，由调用方（`_fit_sidebar`）先去把容器加宽——240~520 各档今天的
        行为一点没动。只有"加宽已到量程上限、单排仍然排不开"时才 `wrap=True`：
        3.0 档字号（`capture_sweep --font-scale 3.0`）实测单排最窄也要 700px 上下，侧栏上限
        只有 520，旧行为是布局去压 x，于是「播放列表」与「媒体库」叠在一起 63×57px
        （`logs/sweep-font3.0-*.log` 里那两条"重叠"），字压字、点到的格和看到的格不是同一个。
        换行才是这一档的正解：每排都排得开、每格都完整可点，代价只是页签条多占一排高。
        为什么不在这里"排不开就自己换行"、而要调用方给开关：那样
        `tests/test_ui.py` 的 `tabs_fit(no_grow=True)` 就什么都测不到了——那条注入的意义正是
        证明"排不开"是**被加宽那一步**保住的；换成能自动换行它就永远绿，判据当场失去牙。

        返回值是**排出来的那个形状**里最宽一排需要的宽度（单排时就是这一排的总需求）：
        调用方与 tests/test_ui.py 的页签几何判据都拿它对账，所以换行之后它必然 ≤ 容器宽。
        """
        n = len(self.tab_btns)
        frame = int(self.tab_row_margins.left()) + int(self.tab_row_margins.right())
        texts, hints = [], []
        for b, (label, _tip) in zip(self.tab_btns, self.tab_defs):
            b.ensurePolished()        # QSS 的 font-size 要先落到 QFontMetrics 上，否则量的是默认字号
            texts.append(QFontMetrics(b.font()).horizontalAdvance(label))
            hints.append(b.sizeHint().width())
        pad, spacing, widths = self.TAB_PAD[-1], self.TAB_SPACING[0], None
        for cand_pad in self.TAB_PAD:
            ws = [max(t + 2 * cand_pad, h) for t, h in zip(texts, hints)]
            for cand_sp in self.TAB_SPACING:
                if sum(ws) + frame + cand_sp * (n - 1) <= sidebar_width:
                    pad, spacing, widths = cand_pad, cand_sp, ws
                    break
            if widths is not None:
                break
        rows = [list(range(n))]
        if widths is None:            # 连最窄一档都排不开：把缝收到 0、留白收到最窄，报出真实下限
            pad = self.TAB_PAD[-1]
            spacing = self.TAB_SPACING[-1]
            widths = [max(t + 2 * pad, h) for t, h in zip(texts, hints)]
            if wrap:                  # 容器已经加到顶了，才真的换行（每排都 ≤ sidebar_width）
                rows = self._wrap_rows(widths, max(0, int(sidebar_width) - frame), spacing)
        if spacing != self.tab_row_spacing:
            # 缝变了只记账，不直接改布局：横向缝长在"每排那个盒"上，
            # 下面 `_place_tab_rows` 重排时每排都会照这个新值挂一遍。
            self.tab_row_spacing = spacing
        for b, w in zip(self.tab_btns, widths):
            b.setFixedWidth(w)
        self._place_tab_rows(rows)
        # 逐排各自算"这一排要多宽"再取最大：拿"最宽那排"和"格子最多的那排"混在一起算，
        # 换行时会出现"格子少的那排其实更宽"的那种错配（每格的宽不一样）。
        # 这个数**就是**布局真正要占的宽（每排独立之后不再跨排取列最大），
        # 所以 `_fit_sidebar` 拿它当加宽目标是安全的——上一版用网格时它比真实需求小 127px，
        # 4.0 档字号因此"报了排得开、其实压在一起"。
        need = 0
        for r in rows:
            need = max(need, sum(widths[i] for i in r) + spacing * (len(r) - 1))
        return need + frame

    def _pages_need(self) -> int:
        """侧栏各页里最宽那一页"一个字都不被切"要的宽（算法与理由见 `form_rows.content_width`）。

        为什么取**所有页**而不是当前页：切页就变宽会抖——用户在 300 与 336 之间来回跳，
        每点一次页签画面就重新排一次，比"侧栏比设置值宽一点"坏看得多。
        """
        return max((form_rows.content_width(self.sidebar_stack.widget(i))
                    for i in range(self.sidebar_stack.count())), default=0)

    def _fit_sidebar(self) -> int:
        """排一次页签与每一页内容，排不开就把**侧栏**加宽（不是把内容压小），返回侧栏实际宽度。

        （下面引用的"五格各要 85/67/49/49/49px"是**五页签时代**的读数：2026-10-12 加了
        「声音均衡」那一格之后颗数与每颗的宽都变了，机制一字未变，所以这段留着当事故记录，
        当前形状的真值由 `tests/capture_sweep.py` 逐档字号现场量。）

        为什么必须有这一步：`_layout_tabs()` 把每格钉成固定宽（min==max），而盒布局在空间
        不够时不会去破坏固定宽，它**照压缩后的间距摆 x**——1.5 档字号实测：五格各要
        85/67/49/49/49px、侧栏只有 300px，于是「播放列表」占 10..95 而「媒体库」从 74 开始，
        两格叠在一起（`capture_sweep.py --font-scale 1.5` 报的那两条"重叠"）。字压字、
        点到的不是看见的那格，比"侧栏比用户设的宽一点"坏得多。
        页签之外还要管页内容：调节/画面/声音均衡/统计四页肚里是 QScrollArea（这四处由
        `tests/probe_scrollbar_allow.py` 现场按栈点名，不是这里抄的——播放列表与媒体库没有滚动区），
        横向滚动条又关着，
        内容顶宽超出视口时**既不滚动也不压缩，直接从右边掉出去**（1.3 档实测切掉「1.00x」），
        所以取两边的最大值。
        所以设置页那个数从"侧栏宽度"改成"侧栏宽度下限"：够用时照用户的来，不够时加到刚好排开，
        上限取设置页量程的最大值（520，同一份出处）——那一档在标准字号下是量过的安全形状。
        真到 520 还排不开单排（`--font-scale 3.0` 实测单排要 700px 上下）时不再硬撑一行：
        交给 `_layout_tabs(..., wrap=True)` 把页签条**换成两排**，每排都排得开。
        以前这种情况是"布局去压 x、两格叠在一起"，那是能看见的坏（点到的不是看见的）；
        而"侧栏被裁掉最后一格"那种更隐蔽的坏由裁而未告尺点名，两条都不靠这里猜。
        """
        tabs_need = self._layout_tabs(self.sidebar_width)
        page_need = self._pages_need()
        need = max(tabs_need, page_need)
        # 两个数分开留：`tab_row_need` 说的是"这一排页签要多宽"，`sidebar_page_need` 说的是
        # "最宽那一页内容要多宽"。判据各查各的，混成一个就没法说红的是页签还是页面。
        self.tab_row_need = tabs_need
        self.sidebar_page_need = page_need
        cap = INT_RANGE["sidebar_width"][1]
        w = min(max(self.sidebar_width, need), cap)
        if self.sidebar.width() != w:
            self.sidebar.setFixedWidth(w)
            self.video.refit()
        # 宽度落定后再排一次：加宽之后缝能回到宽松那一档；
        # 而"加宽加到量程上限仍然排不开单排"（3.0 档字号就是这么个形状）时把 `wrap` 打开，
        # 让页签条换行——这一步顺序不能反：先试加宽、加无可加才换行，
        # 因为一排页签是这一栏本来的形状，520 以内不该出现两排。
        self.tab_row_need = self._layout_tabs(w, wrap=(tabs_need > w))
        return w

    def _sync_tab_lights(self) -> None:
        """控制条上那三盏页灯（列表/媒体库/调节）跟侧栏的真实开合与页码对齐。

        单独拎出来是因为现在**有两条路**会改侧栏可见性：用户点灯（`_set_sidebar`）与进出全屏
        （`_side_for_fs`）。后者不经过 `_sidebar_page`（它不该顺手重跑各页的 `page_shown` 那些
        IPC／扫描回填），但灯必须跟着灭——全屏里侧栏收起来了、灯还亮着，用户按一下就成"灯亮着
        却看不见东西"的假状态。
        """
        i = self.sidebar_stack.currentIndex()
        for btn, page in ((self.bar.btn_list, 0), (self.bar.btn_library, 1),
                          (self.bar.btn_tweak, 2)):
            btn.setChecked(self.sidebar.isVisible() and i == page)

    def _sidebar_page(self, i: int) -> None:
        i = max(0, min(i, self.sidebar_stack.count() - 1))
        # 点当前页签（或 _set_sidebar 重放同一个索引）时内容根本没换：再淡入一次
        # 就是一次没有信息量的闪烁，连续点两下会像界面卡了一下。
        changed = i != self.sidebar_stack.currentIndex()
        self.sidebar_stack.setCurrentIndex(i)
        for k, b in enumerate(self.tab_btns):
            b.setChecked(k == i)
        # 控制条上只有前三页各有自己的灯（列表/媒体库/调节），画面、声音均衡与统计没有——
        # 所以切到那三页时三个灯必须全灭，留着上一页的灯就是双份状态。
        self._sync_tab_lights()
        if i == 1 and not self.library.items() and not self.library.running():
            self.library.refresh()
        if i == 1:
            # 最近播放这一档按观看记录排：切回来时补画一次，进度与顺序可能已经变了。
            self.lib_panel.page_shown()
        if i == 2:
            # 每次切到这一页都重问一遍：设备可能刚插上，A-B 可能刚被快捷键改过
            self.tweak_panel.page_shown()
        if i == 3:
            # 画面页不订阅滤镜链（链没有可订阅的属性），所以开页时按"配置 + 内核几何真值"
            # 重画一次；这一步只读缓存，不发命令、也不发同步请求。
            self.transform_panel.page_shown()
        if i == 4:
            # 声音均衡页和画面页相反，开页时**要问一次** `af`：这一族没有订阅，
            # 而启动参数按配置带上去的链可能在跑而面板从没打开过——不问就照配置画，
            # 两边不一致时界面会画一个假的现在（详见 AudioEqPanel.page_shown）。
            self.audio_eq_panel.page_shown()
        # 开页之后补排一次，而且要等布局跑完再补（`singleShot(0)`）：这一页的需求会在
        # "真的排过版"之后才见真值——设备清单要等 IPC、缩略图要等扫描回填，而 `_fit_sidebar`
        # 只在建窗与设置应用时跑过，构造期那份读数就算已经订正（见 `form_rows.content_width`
        # 里 2px/26px 那条），也可能被后到的内容顶高。差的那几像素落在行尾数值胶囊上，
        # 用户看到的是"少半截字"，而布局自己一句不报。
        QTimer.singleShot(0, self._fit_sidebar)
        # 淡入放在最后：page_shown 里同步重建的内容也要在第一帧就被纱盖住，
        # 否则新旧两版内容会在头几帧同时露出来。只在真换页时放。
        if changed:
            self.page_fade.run()

    def _set_sidebar(self, vis: bool, page: int | None = None) -> None:
        if self._fs:
            # 全屏里用户显式开合 ⇒ 那笔"退出时还原"的账作废（见 `_side_for_fs`）：
            # 不销掉的话，退出全屏会把他刚打开的那一栏强行收回去。
            self._side_saved = False
        self.sidebar.setVisible(vis)
        if page is not None:
            self._sidebar_page(page)
        else:
            self._sidebar_page(self.sidebar_stack.currentIndex())
        self.video.refit()      # 侧栏占掉/让出的宽度必须让 mpv 子窗口跟着铺

    def toggle_playlist(self) -> None:
        """Ctrl+L：关着就开到列表页；开着但在库页就切回列表；在列表页则收起。"""
        if not self.sidebar.isVisible():
            self._set_sidebar(True, 0)
        elif self.sidebar_stack.currentIndex() == 0:
            self._set_sidebar(False)
        else:
            self._sidebar_page(0)

    def toggle_library(self) -> None:
        if not self.sidebar.isVisible():
            self._set_sidebar(True, 1)
        elif self.sidebar_stack.currentIndex() == 1:
            self._set_sidebar(False)
        else:
            self._sidebar_page(1)

    def toggle_tweak(self) -> None:
        if not self.sidebar.isVisible():
            self._set_sidebar(True, 2)
        elif self.sidebar_stack.currentIndex() == 2:
            self._set_sidebar(False)
        else:
            self._sidebar_page(2)

    def toggle_transform(self) -> None:
        """Ctrl+P：与 Ctrl+L/M/T 同一条规则——关着就开到这一页，开着但在别的页就切过来，
        已经在这一页才收起。"""
        if not self.sidebar.isVisible():
            self._set_sidebar(True, 3)
        elif self.sidebar_stack.currentIndex() == 3:
            self._set_sidebar(False)
        else:
            self._sidebar_page(3)

    def toggle_stats(self) -> None:
        if not self.sidebar.isVisible():
            self._set_sidebar(True, 5)
        elif self.sidebar_stack.currentIndex() == 5:
            self._set_sidebar(False)
        else:
            self._sidebar_page(5)

    # ================= 睡眠定时 / 设置页 =================
    def _open_sleep(self) -> None:
        SleepDialog(self.sleep, self).exec()

    def _on_sleep_expire(self, action: str) -> None:
        """到点动作统一走 dispatch：内核没起来时它自己会回一句"播放内核还没启动"，
        而不是在这里静悄悄什么都不做。"""
        if action == "pause":
            if self.player.running:
                self.player.pause()
        elif action == "stop":
            if self.player.running:
                self.dispatch("stop")
        elif action == "quit":
            self.close()

    def _open_settings(self) -> None:
        # 复用同一个实例：反复 new 会留下上一份对话框（Qt 的父子关系让它一直活着），
        # 而设置页要能在"改完→看效果→再改"之间来回，留着它更顺手。
        if self.settings_dlg is None:
            self.settings_dlg = SettingsDialog(self.cfg, self)
            self.settings_dlg.sig_applied.connect(self._apply_settings)
        self.settings_dlg.reload_from_cfg()
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def _apply_settings(self, changed: dict, restart: list) -> None:
        """设置页只写配置；界面这边负责把"立即生效"那几项真的落到界面/内核上。"""
        if "theme_accent" in changed:
            self.setStyleSheet(theme.set_accent(str(changed["theme_accent"])))
            # QSS 管不到 QIcon 与自绘/inline 颜色：logo、倍速胶囊、选中态的图标都要
            # 各自重画一次，否则换完主色只有"底色变了"，品牌标记还是上一套。
            theme.notify_accent()
            QApplication.processEvents()
        if "sidebar_width" in changed:
            self.sidebar_width = self._clamped_sidebar_width(changed["sidebar_width"])
            self._fit_sidebar()          # 里面会 setFixedWidth＋refit，不再各写一遍
        if "float_controls_in_fs" in changed:
            # 这一项改了必须**当场重摆一次**：正全屏时把条从浮层搬回布局（或反过来），
            # 只写盘不重摆的形态是"关掉开关、黑边还在"——用户会以为设置没生效，接着再点一次。
            self._apply_mode()
        if restart:
            self._osd("有 " + str(len(restart)) + " 项要重启播放器才生效")

    def _open_keymap(self) -> None:
        """快捷键编辑页。和设置页同一个道理：留着同一个实例，改完能立刻接着改。"""
        if self.keymap_dlg is None:
            self.keymap_dlg = KeymapDialog(self.cfg, self)
            self.keymap_dlg.sig_saved.connect(self._apply_keymap)
        self.keymap_dlg.reload_from_cfg()
        self.keymap_dlg.show()
        self.keymap_dlg.raise_()
        self.keymap_dlg.activateWindow()

    def _apply_keymap(self, rows: list) -> None:
        """键位落定：重装快捷键，顺带把帮助页和**侧栏那些按键提示**叫回正轨。

        重装而不是"改一条就 patch 一条"：QShortcut 与动作是多对多（同一动作可以好几把
        键，同一把键只准一个动作），逐条改很容易留下上一条没摘掉——那才是真按一次响两件
        事。整表重装一次就几十个小对象，代价看不出来。

        提示行必须跟着重画：侧栏页脚那句「R 旋转 · Shift+R 比例 · H 镜像」里的键名是从
        这张表算出来的（`form_rows.KeyHintLabel`），装完新键位却不重画，屏幕上就留着
        按旧键位写的那句——用户照它按键没反应，而这里刚"更新成功"过。
        悬停提示走同一条收口（`form_rows.refresh_key_tips`，在 `refresh_key_hints` 里面）：
        控制条那十七颗按钮的「（L）」「（V）」是同一族文案、同一种坏法，而它们比页脚那几行
        更没人看——只有忘了按键的人才会悬上去读一句，读到的恰好是过期那句。
        """
        self._install_shortcuts()
        # 这句里的数字是给**判据**看的，不是给用户看的：它记的是"上一次键位落定时真的重画了几处
        # 提示"。名字是公开的（`last_hint_refresh`），因为判据要在控件树外面读它；而将来某次改名
        # 让 `refresh_key_hints` 变成空转时，这个数会掉到 0，屏幕上却什么都不会报错。
        # 用户看得见的那句 OSD 不带它——"重画了 21 处"是量具的账，不是人话。
        self.last_hint_refresh = form_rows.refresh_key_hints(self)
        n = len(self.key_rows)
        self._osd(f"快捷键已更新：现在 {n} 条绑定")

    # ================= 截图工坊：GIF 导出 / 连拍 =================
    def gif_state(self) -> dict:
        """对话框要的此刻事实，全部取自订阅缓存（不发一次同步请求）。

        video 用 path 而不是 media-title：ffmpeg 要的是磁盘上那个文件，而 media-title
        对流、对某些容器可能是个根本不存在于盘上的名字。
        四个数值都过 _f/_i 收口：缓存里躺着字符串 "no"（内核没媒体时就这么推 time-pos），
        而这里抛一个 ValueError 的代价是整个播放器静默没了，不是这一行画歪。
        """
        p = self.player
        tracks = p.prop("track-list") or []
        return {"video": str(p.prop("path") or ""),
                "pos": _f(p.prop("time-pos")),
                "duration": _f(p.prop("duration")),
                "ab": p.ab_range(),
                "sub": gifmod.sub_file_of(tracks)}

    def _open_gif(self) -> None:
        if self.gif_dlg is None:
            self.gif_dlg = GifDialog(self.cfg, self.gif_state, self)
            self.gif_dlg.sig_export.connect(self._gif_export)
            self.gif_dlg.sig_cancel.connect(self._gif_cancel)
            self.gif_dlg.sig_burst.connect(self.start_burst)
            self.gif_dlg.sig_reveal.connect(self._reveal_path)
            self.gif.sig_progress.connect(self.gif_dlg.on_progress)
            self.gif.sig_done.connect(self.gif_dlg.on_done)
        self._install_gif_shortcuts()   # 每次打开都重递一遍：键位可能在工坊没开时被改过
        self.gif_dlg.refresh()
        self.gif_dlg.show()
        self.gif_dlg.raise_()
        self.gif_dlg.activateWindow()

    def _gif_export(self, spec: dict) -> None:
        """对话框只负责把数收好；起 ffmpeg、拼命令、夹区间都在这边（engine/gif.py）。

        取值全部走 `.get` + 类型收口，不写 `spec["video"]`：这个槽直连对话框的 sig_export，
        而 spec 少一个键就是 KeyError —— 发生在槽里不等于一条 traceback，是进程当场没了。
        区间不在这里再夹一遍：`GifStore.start` 里已经有同一个 `clamp_range`，这里夹第二次
        只会让"界面上的数"和"真发出去的数"多一道可能不一致的换算。
        """
        sp = spec if isinstance(spec, dict) else {}
        _jid, err = self.gif.start(
            str(sp.get("video") or ""), _f(sp.get("start")), _f(sp.get("dur")),
            str(self.cfg["screenshot_dir"]),
            width=_i(sp.get("width"), gifmod.DEFAULT_WIDTH),
            fps=_i(sp.get("fps"), gifmod.DEFAULT_FPS),
            sub_file=str(sp.get("sub") or ""))
        if err and self.gif_dlg is not None:
            # 开不成的那句原因必须回灌到界面上：不然用户只看到按钮灰掉、进度条停在 0
            self.gif_dlg.on_done(0, False, err, "")

    def _gif_cancel(self) -> None:
        self.gif.cancel()

    def start_burst(self, count: int | None = None, gap_ms: int | None = None) -> None:
        """连拍＝按间隔催内核截帧。已经在拍就先停（再按一次 B 是"取消"，不是"再起一轮"，
        两轮叠在同一条时间轴上会截出双份的乱序图）。

        张数与间隔都过 _i：这两个键躺在 config.json 里，是用户可以拿编辑器改的（"burst_count":
        "很多" 就是一份合法的 JSON），而 int() 抛在 Qt 槽里的表现是按一下 B 播放器静默没了。
        夹住的上下限另有理由：下限 2 张/50 毫秒（一张不叫连拍，1 毫秒等于把内核和磁盘一起捶），
        上限 60 张/3 秒（长到没人能中途停住的连拍不该存在）。
        """
        if self._burst_left:
            self._burst_left = 0
            self._burst_timer.stop()
            self._osd("连拍已取消")
            return
        n = _i(count, _i(self.cfg.get("burst_count", 8), 8))
        gap = _i(gap_ms, _i(self.cfg.get("burst_gap_ms", 250), 250))
        self._burst_left = max(2, min(60, n))
        self._burst_timer.setInterval(max(50, min(3000, gap)))
        self._osd(f"连拍 {self._burst_left} 张")
        self._burst_tick()

    def _burst_tick(self) -> None:
        if not self._burst_left:
            self._burst_timer.stop()
            return
        self.player.snapshot(bool(self.cfg["screenshot_include_sub"]))
        self._burst_left -= 1
        if self._burst_left:
            self._burst_timer.start()
        else:
            self._osd("连拍完成")

    def _reveal_path(self, path: str) -> None:
        """在资源管理器里打开截图目录。只给目录、不给 /select：select 那个参数走的是
        shell 命令行解析，路径里有中文与空格时实测不稳，而"打开到那个文件夹"已经够用了。"""
        d = os.path.dirname(str(path or ""))
        if not (d and os.path.isdir(d)):
            self._osd("目录已经不在了")
            return
        try:
            os.startfile(d)                        # 只有 Windows 有；本项目只跑 Windows
        except OSError as exc:
            self._osd(f"打不开目录：{exc}")

    # ---------- 媒体库动作 ----------
    def _lib_add_root(self, path: str) -> None:
        if not self.library.add_root(path):
            return
        self._save_roots()
        self.library.refresh()
        self._osd("已加入媒体库：" + (os.path.basename(os.path.normpath(path)) or path))

    def _lib_remove_root(self, path: str) -> None:
        if not self.library.remove_root(path):
            return
        self._save_roots()
        self.lib_panel.set_items(self.library.items())
        self.library.refresh()

    def _lib_forget(self, path: str) -> None:
        self.library.forget(path)
        self.lib_panel.set_items(self.library.items())

    def _save_roots(self) -> None:
        self.cfg["library_roots"] = list(self.library.roots)
        self.cfg.save()

    def _arm_autohide(self) -> None:
        self._hide_timer.start(HIDE_AFTER_MS)

    def _maybe_autohide(self) -> None:
        """全屏里到点收 chrome——但**光标还压在控制条上时不能收**，且收不成也必须重挂。

        为什么要重挂（2026-10-20 全屏审查 §2 点名）：`_hide_timer` 是 singleShot，
        旧写法在守卫失败时"什么都不做"，于是这一趟定时器就死了。用户最常见的动作恰恰是
        把鼠标移到条上准备点按钮、或拖完进度条停在原地——守卫失败 ⇒ 不重挂 ⇒
        控制条**永久占着一行布局**，画面被压成 2.00:1 的盒子、左右两条黑边长驻不消。
        这正是"全屏之后周围显示不全"里最顽固的那一支：它不靠晃鼠标回来，晃鼠标反而把它续上。
        """
        if not self._fs:
            return
        if self._drag_scrub:
            # 正按着轨道拖：不许在拖动途中把条收走。拖到条外再停住时 `bar.underMouse()`
            # 已经是假（光标在画面上），而晃鼠标那一路只在"画面里移动"时才重挂——按住不动
            # 2.6 秒正好落进"条从手底下消失"：Qt 隐藏带鼠标抓取的控件时不会补发
            # mouseReleaseEvent，`sig_drag_state(False)` 再也不来，`_drag_scrub` 从此卡在
            # True（续播位置不再按间隔落盘、只剩关窗那一次，缩略图浮层留在画面上没人收）。
            self._arm_autohide()
            return
        if QApplication.activePopupWidget() is not None:
            # 菜单开着不收。条上那几本菜单（倍速/音轨/字幕/更多）都是条的**孩子窗**，
            # `exec()` 起的是嵌套事件循环，这只 2.6 秒的表照样在里面响；到点把条收了，
            # 菜单跟着消失。全屏里挑一条字幕轨或一档倍速，根本不止 2.6 秒。
            self._arm_autohide()
            return
        if self.bar.underMouse() or self.title.underMouse():
            self._arm_autohide()          # 还压着：这一趟不收，但下一趟还得问
            return
        self._set_bar_shown(False)
        self.title.setVisible(False)
        self.video.refit()

    def _wake_ui(self, _pt=None) -> None:
        """全屏里晃一下鼠标只唤回**控制条**——标题栏一律不跟着回来。

        为什么这一条要单独按住（2026-10-19 用户实测截图量出来的根因）：`title` 与 `bar`
        都排在根布局里，不是浮在画面上的，所以它们一显示就把视频区从 3840×2160 压成
        3840×1920。16:9 的片子放进 2.00:1 的盒子只能按高适配 ⇒ 画面左右各让出 213px 纯黑，
        用户看到的就是"全屏之后画面只在中间一块、四周都是空出来的黑边"。
        截图上量到的纯黑区是 x[0..3838]、y[88..2007]：mpv 的子窗口**横向是铺满的**
        （不是没跟着缩放），被压扁的是它嵌进去的那块容器。
        收起那一头本来也是两条一起藏（`_maybe_autohide`），所以"唤醒"只该唤醒用户要点的那条。
        """
        if not self._fs:
            return
        self._set_bar_shown(True)
        self._arm_autohide()

    def _on_scrub_drag(self, dragging: bool) -> None:
        self._drag_scrub = dragging

    def _on_scrub_hover(self, sec: float, x: int) -> None:
        """记住"鼠标此刻指在第几秒、那一格在轨道的哪个像素"，并尝试贴出预览。

        x 是**轨道控件内**的逻辑像素，这里不换算成全局——因为浮层要跟着光标走，
        而只有主窗口知道 scrubber 在屏幕上的位置（窗口可能正被拖动/全屏切换）。
        """
        self._preview_at = (float(sec), int(x))
        self._show_preview()

    def _on_scrub_hover_end(self) -> None:
        self._preview_at = None
        self.preview.hide_preview()

    def _show_preview(self) -> None:
        if self._preview_at is None:
            return
        if not self._thumb_key:
            # 兜底：极少数情况下 duration 的 property-change 比 path 先到，_on_duration
            # 那会儿还拿不到路径。这里用订阅缓存补一次（request 幂等，不会重复起进程）。
            path = str(self.player.prop("path", "") or "")
            dur = float(self.player.prop("duration", 0) or 0)
            if path and dur > 0:
                self._thumb_key = self.thumbs.request(path, dur)
                if not self.thumbs.ready(self._thumb_key):
                    self.preview.hide_preview()
                    return
            else:
                return
        sec, x = self._preview_at
        pm = self.thumbs.image_at(self._thumb_key, sec)
        if pm is None or pm.isNull():
            self.preview.hide_preview()    # 抽帧还没回来：留时间气泡，不画空框
            return
        tl = self.bar.scrub.mapToGlobal(QPoint(0, 0))
        self.preview.show_at(pm, fmt_time(sec), tl.x() + x, tl.y() - 8)

    def _on_thumbs_done(self, key: str, ok: bool, detail: str) -> None:
        if not ok:
            return
        if key == self._thumb_key:
            self._show_preview()           # 鼠标还停在轨道上时，图一到就补出来
        # 老缓存的回收交给 `ThumbStore` 自己按批做（engine/thumbs.py 的 `_note_done`）：
        # 这里原来每张 sheet 回来就 `prune()` 一次全目录，而拖一次时间轴能连着回来十几张——
        # "回收"的账（每个缓存目录开一次 meta.json）比它省下的那点空间先到，
        # 且全跑在界面线程上。

    def _on_panel_shuffle(self) -> None:
        if self.player.running:
            self.bar.toggle_shuffle()

    def _remove_playlist_rows(self, rows: list) -> None:
        """批量移除：从大到小发，前面的行号不会因为后面的删除而错位。"""
        for r in sorted({int(x) for x in (rows or [])}, reverse=True):
            self.player.remove_index(r)

    def _apply_playlist_moves(self, steps: list) -> None:
        """面板算好的一串 `playlist-move` 步骤；顺序真相仍由随后的推送回填。"""
        for src, slot in (steps or []):
            self.player.move_index(int(src), int(slot))

    # ================= 视频区事件 =================
    def _on_video_click(self, kind: int, raw: QPoint) -> None:
        if kind == 2:                       # 左键双击：播放/暂停（单击留给未来手势，不抢即时性）
            if self.player.running:
                self.bar.toggle_play()
        elif kind == 3:                     # 右键：上下文菜单
            self._popup_context(raw)

    def _popup_context(self, raw: QPoint) -> None:
        """raw 是 GetCursorPos 的物理像素；Qt 用逻辑像素，必须除以 dpr，否则 150% 缩放会偏。"""
        dpr = self.video.devicePixelRatioF() or 1.0
        g = QPoint(int(raw.x() / dpr), int(raw.y() / dpr))
        m = QMenu(self)
        paused = bool(self.player.prop("pause", False))
        acts = [("暂停" if not paused else "播放", "toggle_play"), None,
                ("上一个", "prev"), ("下一个", "next"), ("停止", "stop"),
                ("截图存帧", "screenshot"), ("媒体信息", "media_info"),
                ("音频输出设备", "menu_device")]
        acts += [(CONTEXT_LABELS[a], a) for a, _ in VIEW_ACTIONS]
        acts += [None, ("全屏", "fullscreen"), ("打开文件…", "open_files"),
                 ("打开文件夹…", "open_dir")]
        for it in acts:
            if it is None:
                m.addSeparator()
            else:
                label, act = it
                a = m.addAction(label)
                a.setData(act)
                if act == "always_on_top":
                    # 勾选态让菜单自己说明当前是开是关；点一下仍然只走 dispatch
                    # （菜单不存第二份状态，避免和 _topmost 说两套话）。
                    a.setCheckable(True)
                    a.setChecked(self._topmost)
        chosen = m.exec(g)
        if chosen:
            self.dispatch(str(chosen.data()))

    # ================= 菜单 =================
    def _show_menu(self, kind: str, anchor: QWidget) -> None:
        if kind == "speed":
            m = menus.speed_menu(self.bar, float(self.player.prop("speed", 1.0) or 1.0), SPEEDS)
        elif kind in ("audio", "video", "sub"):
            m = menus.track_menu(self.player, kind, self.bar)
        elif kind == "device":
            m = menus.device_menu(self.player, self.bar)
        elif kind == "loop":
            m = menus.loop_menu(self.player, self.bar)
        elif kind == "more":
            # 第三位只给"窗口置顶"：勾选态读的是 `_topmost`（和快捷键/右键同一个事实）；
            # 其余条目第三位给 None，画出来仍是普通项。
            extra = [(CONTEXT_LABELS[a], ("do", a),
                      self._topmost if a == "always_on_top" else None)
                     for a in MORE_ACTIONS]
            chapter = menus.chapter_menu(self.player, self.bar)
            m = menus.more_menu(self.cfg, self.bar, extra=extra,
                                submenus=[("章节", chapter)])
        else:
            return
        m.triggered.connect(self._on_menu_action)
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _on_menu_action(self, act) -> None:
        data = act.data()
        if not isinstance(data, tuple):
            return
        kind = data[0]
        if kind in ("audio", "video", "sub"):
            self.player.set_track(kind, data[1])
            label = {"audio": "音轨", "video": "视频", "sub": "字幕"}[kind]
            self._osd(f"{label}：{data[1]}")
        elif kind == "speed":
            self.player.set_speed(float(data[1]))
            self._osd(f"倍速 {float(data[1]):g}x")
        elif kind == "loop":
            self._set_loop_mode(data[1])
        elif kind == "device":
            actual = self.player.set_audio_device(str(data[1]))
            self.tweak_panel.mark_device(actual)
            self._osd("输出设备 " + str(actual))
        elif kind == "ab":
            if data[1] == "clear":
                self.player.clear_ab()
                self._osd("清除 A-B")
            else:
                self.player.set_ab(data[1])
                self._osd(f"A-B 点 {data[1].upper()} 已设")
        elif kind == "secondary":
            self._toggle_secondary_sub()
        elif kind == "file":
            what = data[1]
            # "file" 这个 kind 被两类动作共用：more 菜单里是"打开文件/文件夹"，
            # 轨道菜单里是"加载外部字幕/音频"。不分支就会把 open 当字幕后缀传给文件对话框。
            if what == "open":
                self.open_files_dialog()
            elif what == "open_dir":
                self.open_dir_dialog()
            else:
                self._add_external(what)
        elif kind == "shot":
            self.player.snapshot(data[1] == "with_sub")
        elif kind == "info":
            if data[1] == "copy":
                QApplication.clipboard().setText(self._media_info())
                self._osd("媒体信息已复制")
            else:
                self._osd(self._media_info())
        elif kind == "do":
            # 菜单条目只想"按同一个动作名再按一次"：交给 dispatch，
            # 于是菜单项和快捷键的行为必然一致（包括内核未启动时的提示）。
            self.dispatch(str(data[1]))
        elif kind == "chapter":
            try:
                idx = int(data[1])
            except (TypeError, ValueError):
                return
            self._osd(chapter_notice(self.player.set_chapter(idx) or {}))

    def _chapter_step(self, delta: int) -> None:
        """上一章/下一章：动作只在这里落地，键盘与菜单子菜单都走它。"""
        self._osd(chapter_notice(self.player.chapter_step(delta) or {}))

    def _set_loop_mode(self, mode: str) -> None:
        if mode == "no":
            self.player.set_loop("no")
            self.player.set_loop_file("no")
        elif mode == "inf":
            self.player.set_loop("inf")
            self.player.set_loop_file("no")
        else:
            self.player.set_loop("no")
            self.player.set_loop_file("inf")
        self.cfg["loop_playlist"] = mode
        self._osd({"no": "不循环", "inf": "列表循环", "one": "单曲循环"}[mode])

    def _toggle_secondary_sub(self) -> None:
        if self.player.secondary_sub_id() is not None:
            self.player.set_secondary_sub("no")
            self._osd("关闭双语字幕")
            return
        others = [t for t in (self.player.prop("track-list", []) or [])
                  if t.get("type") == "sub" and not t.get("selected")]
        if others:
            self.player.set_secondary_sub(others[0].get("id"))
            self._osd("双语字幕：取下一条字幕轨")
        else:
            self._osd("没有可用作副字幕的字幕轨")

    def _add_external(self, kind: str) -> None:
        flt = "字幕文件 (*.srt *.ass *.ssa *.sub *.vtt *.idx)" if kind == "sub" else "音频文件 (*)"
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", os.path.expanduser("~"), flt)
        if not path:
            return
        if kind == "sub":
            self._attach_subtitles([path])
        else:
            self.player.add_audio(path)

    def _attach_subtitles(self, paths: list[str]) -> None:
        """把外挂字幕挂到当前这支片上。拖入与对话框两条路共用这一段。

        没有当前视频时 `sub-add` 只会静默失败（内核回 property unavailable），
        所以这里先说话，别让用户以为"拖了但没反应"。和视频一起拖进来的那一份
        不走这里——那时内核还没 file-loaded，由 `_on_file_loaded` 接上。
        """
        paths = [p for p in (paths or []) if p]
        if not paths:
            return
        if not self.player.running or not str(self.player.prop("path", "") or ""):
            self._osd("先播放一支视频，再加载字幕")
            return
        for p in paths:
            self.player.add_subtitle(p)
        if len(paths) == 1:
            self._osd(f"外挂字幕：{os.path.basename(paths[0])}")
        else:
            self._osd(f"外挂字幕：{len(paths)} 份")

    # ================= 内核事件 =================
    def _on_title(self, t: str) -> None:
        if t:
            self.title.set_document(t)

    def _on_file_loaded(self) -> None:
        # 内核亲口说"这支文件我装载完了"——界面那句"已载入 N 个文件"就等这一声（见
        # open_receipt）。放在最前面：下面几行是刷标题与铺画面，跟回执无关。
        self._settle_open_receipt(loaded=True)
        self.video.refit()
        self.preview.hide_preview()
        name = str(self.player.prop("media-title") or
                   os.path.basename(str(self.player.prop("path", ""))))
        self.title.set_document(name)
        self.setWindowTitle(f"{name} - {cfgmod.APP_TITLE}" if name else cfgmod.APP_TITLE)
        win32.keep_awake(not bool(self.player.prop("pause", False)))
        # 媒体库的"最近播放"档：内核刚换的这支片会顶到最前（recent 在 open_files 里已写）。
        self.lib_panel.note_played(str(self.player.prop("path", "") or ""))
        # 和这支片一起拖进来的字幕：现在内核才认得 sub-add。放在最后——上面那条
        # `path` 刚被内核推回来，_attach_subtitles 的守卫读的就是它。
        # 路径必须对得上：不等回执的那一批已经挂在"发起时"的片上，而这一批是给
        # 某支**特定**文件的；晚到的旧 file-loaded 不许把它的字幕错挂到当前这支队上。
        if self._pending_subs:
            head, subs = self._pending_subs
            loaded = str(self.player.prop("path", "") or "")
            if not head or cfgmod.same_media_path(head, loaded):
                self._pending_subs = None
                self._attach_subtitles(subs)

    def _on_duration(self, dur: float) -> None:
        """时长落定＝这一格该抽多少秒一张才算得出来，于是排队抽帧。

        为什么挂在 duration 而不是 file-loaded：换文件时引擎会把 duration 从缓存里清掉
        （player._FILE_SCOPED），file-loaded 那一瞬读到的还是上一条片的时长，
        拿它算 key 会把新片的缩略图错认成旧片的。
        """
        self._thumb_key = ""
        self.preview.hide_preview()
        path = str(self.player.prop("path", "") or "")
        if path and dur > 0:
            self._thumb_key = self.thumbs.request(path, float(dur))

    def _on_pause_state(self, paused: bool) -> None:
        win32.keep_awake(not paused and self.player.running)

    def _on_playlist(self, pl: list) -> None:
        # 列表长度先记账：这一格是**内核推来的**，界面那句"已追加 N 个文件"就等它变长，
        # 不等"我们把命令写掉了"（见 open_receipt）。
        self._pl_len = len(pl or [])
        if self._open_claim and not self._open_claim["plays"]:
            self._settle_open_receipt()
        """列表空不空决定"占位页 vs 视频容器"谁在台前——两层都必须管。

        非空分支不能省：订阅 playlist 时内核会先把初值 [] 推一次（那时 _begin_engine
        刚把容器打开），容器随即被隐藏；等真加载出文件，如果这里只处理空列表，
        容器就永远停在隐藏态。实测过这个 bug：mpv 在放、时间在走，屏幕上却是
        视频容器和占位页**两层都藏着**，只剩窗口底色。
        """
        if pl:
            self.video.set_empty(False)
            self.empty.set_notice("")    # 上一趟的解释别跟着画面一起回来
            self.empty.setVisible(False)
            self.video.refit()      # 容器刚从隐藏变可见，mpv 子窗口要重新铺一次
            return
        self.video.set_empty(True)
        self.empty.setVisible(True)
        self.title.set_document("")
        # 任务栏/Alt-Tab 的标题也是"上一支片名"的载体：列表清空后它必须回正，
        # 否则用户关了片、窗口顶上还挂着刚放完的那一支。
        self.setWindowTitle(cfgmod.APP_TITLE)

    def _on_engine_log(self, text: str) -> None:
        if text:
            self._osd(text[:64])

    def _on_engine_died(self, code: int) -> None:
        self._osd(f"播放内核退出（code={code}），自动重启中")

    def _on_saved(self, path: str) -> None:
        self._osd("已保存：" + os.path.basename(path))

    def _on_eof(self, eof: bool) -> None:
        if eof:
            win32.keep_awake(False)

    def _on_wheel(self, kind: str, value: str) -> None:
        """内核滚轮的回声（见 `MpvPlayer._write_input_conf`）。

        音量值由内核用 ${volume} 展开好带上来，是**加完之后**的数——实测 client-message
        比 volume 的属性推送先到（tests/probe_wheel_order.py），读缓存会永远慢一格。
        """
        if kind == "vol":
            try:
                n = int(round(float(value)))
            except (TypeError, ValueError):
                return
            muted = bool(self.player.prop("mute", False))
            self._osd(f"音量 {n}%" + ("（静音中）" if muted else ""))
        elif kind == "seek":
            self._osd("+5 秒" if value.lstrip().startswith("+") else "-5 秒")

    def _track_position(self, pos: float) -> None:
        self._last_pos = float(pos or 0)
        now = time.monotonic()
        if self._drag_scrub or now < self._next_save:
            return
        self._next_save = now + SAVE_EVERY
        path = str(self.player.prop("path", ""))
        dur = float(self.player.prop("duration", 0) or 0)
        if path and dur > 0:
            self.cfg.remember_position(path, self._last_pos, dur)

    def _jump_pct(self, pct: int) -> None:
        dur = float(self.player.prop("duration", 0) or 0)
        if dur > 0:
            self.player.seek_abs(dur * pct / 100.0)
            self._osd(f"{pct}%")

    # ================= 杂项 =================
    def notify_external_drop(self, dropped) -> None:
        """外部（双击/右键/命令行）递来的路径里有没认出来的：在能看见的地方点名。

        空状态时这一句走占位页（用户正看着"什么都没放"那一屏）；**播放中**占位页是
        隐藏的、挂它身上的那条通知同样看不见，而视频上的 OSD 是唯一能说话的地方——
        所以两种台前状态各走各的，不能只写占位页。
        """
        self.empty.point_out(dropped)
        if dropped and not self.empty.isVisible():
            names = []
            for item in list(dropped)[:2]:
                raw = item[0] if isinstance(item, (tuple, list)) and item else item
                names.append(os.path.basename(os.path.normpath(str(raw))) or str(raw))
            more = f" 等 {len(dropped)} 个" if len(dropped) > 2 else ""
            self._osd("打不开：" + "、".join(names) + more)

    def _osd(self, text: str) -> None:
        if not text:
            return
        self.osd.show_text(text)
        # 视频容器还藏着（空状态）时那句 OSD 没人看得见，抄一份到占位页；
        # 正在播的时候不许抄——那会儿屏幕上是有画面的，别拿一行字去挡。
        if self.empty.isVisible():
            self.empty.set_notice(text)

    def _media_info(self) -> str:
        """从订阅缓存拼一行媒体信息（不发同步请求，字段缺了就跳过）。"""
        p = self.player
        parts = []
        w, h = int(p.prop("width", 0) or 0), int(p.prop("height", 0) or 0)
        if w and h:
            parts.append(f"{w}×{h}")
        for key in ("container-format", "video-codec", "audio-codec"):
            v = p.prop(key)
            if v:
                parts.append(str(v))
        fps = float(p.prop("container-fps", 0) or 0)
        if fps:
            parts.append(f"{fps:.2f}fps")
        # 'no' 是真值字符串：直接 str() 会念成"硬解 no"。判"在不在硬解"交给 hwdec_state。
        hard, dec = hwdec_state(p.prop("hwdec-current"))
        parts.append(("硬解 " + dec) if hard else dec)
        vo = p.prop("current-vo")
        if vo:
            parts.append(f"输出 {vo}")
        ch = p.chapters()
        if ch:
            parts.append(f"{len(ch)} 章")
        return " ｜ ".join(parts)

    def _show_media_info(self) -> None:
        self._osd(self._media_info())

    def _show_help(self) -> None:
        """快捷键一览：读**生效表**，不是默认表。

        用户改了键位以后，这一页还说"按 PgDn 下一个"就是骗人；而它恰好是最容易被当成
        事实的一页。同一个动作的多把键并到一行（"Space 或 K"），比列两遍"播放 / 暂停"
        好读，也不会让人以为按一次能做两件事。排版走 `ui/help_page.py` 的表格：
        旧版按字符数 `.ljust()` 对齐，而比例字体下中文与字母的宽度根本不同，
        每一行的说明文字起点都不一样——那不是"差一点"，是整页看不出是一张表。
        """
        rows = getattr(self, "key_rows", None) or keymap.effective(self.cfg.get("keymap"))
        # 页脚那句"怎么改"里的键名同样从这张表取：这一页就是照着表生成的，结尾却抄死一个
        # 「（Ctrl+K）」——用户把改键入口挪到别的键之后，这张"按键说明书"的最后一页开始说谎。
        edit_keys = "/".join(keymap.keys_of(rows, ("keymap_edit",)))
        d = help_page.help_dialog_class()(rows, edit_keys, self)
        d.exec()

    # ---------- 拖放 ----------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.add_paths(paths)


class _EmptyPage(QWidget):
    """空状态页：没有文件时顶替视频容器（容器一隐藏，mpv 子窗口就跟着不见了）。

    这一页还兼"解释在哪儿"。`Osd` 是视频容器的孩子，而空状态时那个容器是**隐藏**的
    （`VideoSurface.set_empty`），所以"没有找到可播放的文件"这类话以前只会进一块看不见的
    画布——用户双击了视频、播放器开起来了、界面什么都不说，看上去就像"它没收到我的文件"。
    `notice` 那一行就是把这句话从隐藏的画布搬到能看见的地方。
    """

    sig_open_files = pyqtSignal()
    sig_open_dir = pyqtSignal()

    NOTICE_MS = 9000        # 从 OSD 抄来的那种短话自己收掉；点名那一条走 set_notice(sticky)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EmptyPage")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 巨幅水印：只在构造期画一次。低透明度让它是"墙上的影子"，不是第二枚 logo。
        self._art = icons.pixmap("logo", 420, theme.TEXT_FAINT)

        lay = QVBoxLayout(self)
        lay.setSpacing(0)
        lay.setContentsMargins(30, 30, 30, 30)
        lay.addStretch(4)

        eyebrow = QLabel("LOCAL CINEMA  ·  本地影院", self)
        eyebrow.setObjectName("Eyebrow")
        eyebrow.setAlignment(Qt.AlignCenter)
        lay.addWidget(eyebrow)
        lay.addSpacing(14)

        t = QLabel("今晚，看点什么。", self)
        t.setObjectName("HeroTitle")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        lay.addSpacing(14)

        s = QLabel("把片子拖进窗口，或者从媒体库挑一部。", self)
        s.setObjectName("HeroSub")
        s.setAlignment(Qt.AlignCenter)
        lay.addWidget(s)
        lay.addSpacing(26)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.addStretch(1)
        b1 = QPushButton("打开文件", self)
        b1.setObjectName("Primary")
        b1.setMinimumSize(132, 40)
        b1.setCursor(Qt.PointingHandCursor)
        b1.setFocusPolicy(Qt.NoFocus)
        b2 = QPushButton("打开文件夹", self)
        b2.setObjectName("Ghost")
        b2.setMinimumSize(132, 40)
        b2.setCursor(Qt.PointingHandCursor)
        b2.setFocusPolicy(Qt.NoFocus)
        b1.clicked.connect(lambda: self.sig_open_files.emit())
        b2.clicked.connect(lambda: self.sig_open_dir.emit())
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(18)

        hint = QLabel("拖放即播  ·  MKV / MP4  ·  HDR  ·  外挂字幕", self)
        hint.setObjectName("HeroHint")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)

        # 解释行：默认收起，只在"该说点什么"的时候出现（见 `set_notice`）。
        self.notice = QLabel("", self)
        self.notice.setObjectName("EmptyNotice")
        self.notice.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.notice.setWordWrap(True)
        self.notice.setMaximumWidth(620)
        self.notice.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.notice.setVisible(False)
        lay.addSpacing(16)
        lay.addWidget(self.notice, 0, Qt.AlignHCenter)
        self._notice_sticky = False
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(lambda: self.set_notice(""))
        lay.addStretch(5)

    def paintEvent(self, ev):
        """首屏的气氛：两团极淡的雾 + 一面巨幅芦荟水印。

        为什么不用子控件拼背景：RadialGradient 铺在 QLabel 底下会把文字对比度搅浑，
        而空状态页只画一次静态底——这里画比挂一层透明控件省事，也不会拦截鼠标。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.BG))
        w, h = max(1, self.width()), max(1, self.height())

        haze = QRadialGradient(w * 0.24, h * 0.26, max(w, h) * 0.62)
        c = QColor(theme.ACCENT)
        c.setAlpha(30)
        haze.setColorAt(0.0, c)
        haze.setColorAt(0.55, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), haze)

        warm = QRadialGradient(w * 0.84, h * 0.82, max(w, h) * 0.5)
        c2 = QColor(theme.PRIMARY_FILL)
        c2.setAlpha(12)
        warm.setColorAt(0.0, c2)
        warm.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), warm)

        side = min(w, h) * 0.78
        p.setOpacity(0.045)
        p.drawPixmap(QRectF(w * 0.66 - side / 2, h * 0.16, side, side), self._art,
                     QRectF(self._art.rect()))
        p.setOpacity(1.0)
        p.end()

    def set_notice(self, text: str, sticky: bool = False) -> None:
        """把"这一趟为什么没放起来"写在脸上。空串就是收起。

        `sticky` 给外部递来的点名清单用（那些路径用户不会第二次看到，话不该自己消失）；
        从 OSD 抄来的短话（音量、倍速那种）到点自己收，免得占位页挂着上一条无关状态。
        定时器是孩子对象：窗口一销毁它跟着销毁，不会出现"定时回来打一个已经没了的窗口"。
        """
        text = (text or "").strip()
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        self._notice_sticky = bool(text) and bool(sticky)
        if not text:
            self._notice_timer.stop()
        elif not sticky:
            self._notice_timer.start(self.NOTICE_MS)
        else:
            self._notice_timer.stop()

    def point_out(self, dropped) -> None:
        """按"路径 + 原因"点名：一行一条，文件名留着让人对得上是哪个。"""
        rows = []
        for item in dropped or []:
            raw, why = (list(item) + [""])[:2] if isinstance(item, (tuple, list)) else (item, "")
            name = os.path.basename(os.path.normpath(str(raw))) or str(raw)
            rows.append(f"{name}——{why}" if why else str(name))
        if not rows:
            self.set_notice("")
            return
        head = f"这 {len(rows)} 个路径没能开始播放：" if len(rows) > 1 else "这个路径没能开始播放："
        self.set_notice(head + "\n" + "\n".join(rows), sticky=True)

