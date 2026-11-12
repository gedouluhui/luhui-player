"""统计面板（侧栏第六页）：解码通路、丢帧、音画同步、缓存的实时读数 + 一轮资源实测。

数据来源分三路，前两路的分法依据是**本机实测的推送频率**（5 秒播放内的
property-change 次数，见 player.STATS_PROPS 的注释）：
  - 订阅缓存 `player.prop()`：静态或语义变化的项 —— hwdec-current、current-vo、
    video-codec、audio-codec、width/height、container-fps、volume/volume-max、
    track-list、playlist、media-title、path。这些本来就在订阅里，读缓存零成本。
  - 一秒一轮的按需请求 `player.sig_stats`：avsync（实测每帧推一次，175 次/5s）、
    estimated-vf-fps、demuxer-cache-state、audio-bitrate、frame-drop-count、
    decoder-frame-drop-count、current-gpu-context。给这一页挂每帧级订阅，会把
    控制条和调节页一起收的 sig_info 刷满，而统计页一秒只看一次。
  - **第三路不是内核给的**：底部那几行"资源占用"来自 app/engine/usage.py
    （CPU/内存按 pid 量，GPU 取 nvidia-smi 的整机读数）。它要睡一个采样窗口，
    所以只在用户点按钮时跑，且跑在线程池里；没点之前那几行整行不画。
页面不可见就停表（hideEvent），内核没起来时整页只问一次都不问。

一条纪律：**内核没回报的项整行不画**。本机 v0.41.0-1101 上 fps、estimated-display-fps、
video-bitrate、container-format、aspect-ratio、dithering 这些属性不存在（实测
property not found）。把它们画成 0 或空白，等于把"没测到"伪装成"测到 0"——
丢帧那两行尤其危险："0 帧丢弃"和"这一版内核没这个数"对用户是完全不同的结论。
"""
from __future__ import annotations

import os
from typing import Any

from PyQt5.QtCore import (QRunnable, QObject, QTimer, Qt, pyqtSignal)
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..engine import perf, poolkit, usage
from ..engine.player import MpvPlayer
from . import scroll_feel, theme
from .form_rows import mk_hint

REFRESH_MS = 1000
# 采样窗口。3 秒是两头凑出来的：短于 ~1 秒时 psutil 的进程 CPU 还在"第一个读数"的
# 意义不明期，长于 5 秒用户点了会以为卡住。窗口里的活都在线程池里干，不碰画面。
USAGE_WINDOW_S = 3.0
# 资源实测这一族的四行：行 key → 显示名。写成有序元组而不是 dict，因为这里要的正是
# 顺序本身（页面上从上到下就是这四行的次序），而 dict 的顺序在 Python 3.6 是实现细节。
USAGE_ROWS = (("usage_mpv", "内核"),
              ("usage_shell", "外壳"),
              ("usage_gpu", "GPU（整机）"),
              ("usage_clock", "时钟/功耗/限流"))


class _SampleSignals(QObject):
    """工作线程 → 界面线程的交接信号（Qt 的跨线程信号是队列投递，回界面线程才落地）。"""

    done = pyqtSignal(dict, dict, str, str)   # procs, gpu, gpu 原因, 出错文本


class _SampleJob(QRunnable):
    """跑 usage.sample_window：它会睡 `window` 秒，所以绝不能在这条 UI 线程上调。"""

    def __init__(self, pids: dict[str, int | None], window: float):
        super().__init__()
        self.autoDelete = True
        self.pids = pids
        self.window = window
        self.sig = _SampleSignals()

    def run(self) -> None:
        sig = self.sig                        # 入口取一次本地引用，见 app/engine/poolkit.py
        poolkit.pin(self)
        try:
            try:
                procs, gpu, why = usage.sample_window(self.pids, self.window)
            except BaseException as exc:      # 采样失败不能把线程池里这条任务变成静默失踪
                poolkit.emit(sig, "done", {}, {}, "", f"{type(exc).__name__}: {exc}")
                return
            poolkit.emit(sig, "done", procs, gpu, why, "")
        finally:
            poolkit.unpin(self)


def _fmt_num(v: Any, unit: str = "", digits: int = 1, plus: bool = False) -> str | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    s = f"{x:+.{digits}f}" if plus else f"{x:.{digits}f}"
    if s.endswith("." + "0" * digits):
        s = s[:-(digits + 1)]
    return f"{s}{unit}"


class _Row(QWidget):
    """一行：左边名称、右边数值。值为 None 就整行藏起来（不占布局，也不留空槽）。

    数值这一格是"吃掉剩余宽度＋放不下就省略号"的，不是"按文字要多宽就多宽"的。
    实测过原来那条路的代价：QLabel 不折行时 minimumSizeHint 就是整串文字的宽度
    （视频编码那行 271px、轨道数 305px），于是表单的最小宽度被顶到 354px，
    而侧栏可视只有 268px——横向滚动条又被 AlwaysOff 关着，右端整截被裁掉：
    "H.264 / AVC / MPEG-4 AVC / MP" 后面是什么看不见，而这正是用户开这一页要看的数。
    所以这里给数值格 Ignored 策略（布局不再听它的 sizeHint）、把剩余宽度全给它，
    文字超了就中段省略，完整值挂在 tooltip 上。
    """

    def __init__(self, name: str, parent: QWidget):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.lbl = QLabel(name, self)
        self.lbl.setObjectName("AppTitle")
        self.val = QLabel("—", self)
        self.val.setObjectName("StatValue")
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.val.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.val.setMinimumWidth(60)      # 再窄就连"30 fps"这类短值都放不下
        self._full = "—"
        row.addWidget(self.lbl)
        row.addWidget(self.val, 1)
        self._elide()

    def show_value(self, text: str | None) -> None:
        if text is None:
            self.setVisible(False)
            return
        if not self.isVisible():
            self.setVisible(True)
        self._full = str(text)
        self._elide()

    def value_text(self) -> str:
        """整句（没裁过的版本）。

        判"这一行填进去的是哪几个数"要读这个，判"屏幕上露出了多少"才读 `val.text()`——
        宽度不够时后者是带省略号的。两个入口分开，测试就不会在某一档侧栏宽度上
        因为"少看了中间一段"而误判成"界面没写这个数"。
        """
        return self._full

    def _elide(self) -> None:
        """按数值格**当前真实宽度**裁一遍：放得下就原样，放不下留中段省略号。"""
        t = self._full
        fm = self.val.fontMetrics()
        avail = max(0, self.val.width() - 2)
        if t and avail and fm.horizontalAdvance(t) > avail:
            t = fm.elidedText(t, Qt.ElideMiddle, avail)
        if self.val.text() != t:
            self.val.setText(t)
        self.val.setToolTip("" if t == self._full else self._full)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()


class StatsPanel(QWidget):
    """侧栏第六页。不做任何写操作——它只报内核说了什么。"""

    def __init__(self, player: MpvPlayer, cfg, parent=None):
        super().__init__(parent)
        self.p = player
        self.cfg = cfg
        self.setObjectName("Panel")
        self.setMinimumWidth(240)
        self._polled: dict[str, Any] = {}
        self._rows: dict[str, _Row] = {}
        # 资源实测的三条状态：上一次采样的读数、拼好的文本报表、有没有活正在跑。
        # 它们和 _polled 分开存——polled 是"内核一秒一轮的数"，资源是"用户点一次量三秒"，
        # 混在一个 dict 里就得在每一轮刷新里判断这行该不该跟着刷，白多一条会过期的判据。
        self._usage_procs: dict[str, dict[str, Any]] = {}
        self._usage_gpu: dict[str, Any] = {}
        self._usage_pids: dict[str, int | None] = {}
        self._usage_why = ""
        self._report = ""
        self._job: _SampleJob | None = None
        # 没有在播媒体时，上半页只剩一排小标题（解码/画面/声音/缓存）挂在空白里，
        # 看起来像"这些数正在加载"。把它们整体收起，换成一句说明；资源占用那一段
        # 仍然保留——没有片子时量一轮空闲占用本身是有意义的。
        self._media_sections: list[QLabel] = []
        self._usage_section: QLabel | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 10, 10)
        outer.setSpacing(6)
        # 页头就是这一页唯一一组动作的位置：采样要随时按得到，复制是它的后续动作。
        # 原先两个按钮拖在页脚，没有实时读数时中间会空出一大块，像两张表掉在页面两端。
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self._title("统计"))
        head.addStretch(1)
        self.btn_copy = QPushButton("复制", self)
        self.btn_copy.setObjectName("PageAction")
        self.btn_copy.setFocusPolicy(Qt.NoFocus)
        self.btn_copy.setToolTip("把上一次采样拼成纯文本，粘给帖子或群里问「为什么卡」的人")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_report)
        self.btn_usage = QPushButton(f"采样 {USAGE_WINDOW_S:g} 秒", self)
        self.btn_usage.setObjectName("PageAction")
        self.btn_usage.setFocusPolicy(Qt.NoFocus)
        self.btn_usage.setToolTip("量一轮 CPU/内存（按进程）与 GPU（nvidia-smi 的整机读数）")
        self.btn_usage.clicked.connect(self._sample_now)
        head.addWidget(self.btn_copy)
        head.addWidget(self.btn_usage)
        outer.addLayout(head)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("StatsScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self._body = QWidget(self)
        self._form = QVBoxLayout(self._body)
        self._form.setContentsMargins(0, 0, 6, 0)
        self._form.setSpacing(6)
        self.scroll.setWidget(self._body)
        outer.addWidget(self.scroll, 1)
        self.scroll_feel = scroll_feel.attach(self.scroll)

        self._empty = QLabel("还没有在播的片子。载入后这里会显示解码、丢帧、音画同步的实时读数。",
                            self._body)
        self._empty.setObjectName("EmptyNotice")
        self._empty.setWordWrap(True)
        self._empty.hide()
        self._form.addWidget(self._empty)

        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

        self.p.sig_info.connect(self._on_info)
        self.p.sig_stats.connect(self._on_stats)
        # 换片时把轮询结果清掉：avsync/码率/丢帧这些是新文件的数，留着旧值会在一轮
        # 轮询回来之前把上一条片子的读数是为本条片子的（引擎那边同样会清订阅缓存）
        self.p.sig_file_loaded.connect(self._reset_polled)
        self.refresh()

    def _reset_polled(self) -> None:
        self._polled = {}
        self._paint()

    # ================= 构建 =================
    def _title(self, text: str, obj: str = "PanelTitle") -> QLabel:
        lab = QLabel(text, self)
        lab.setObjectName(obj)
        return lab

    def _hint(self) -> QLabel:
        return mk_hint("上半页每秒刷新 · 只在本页可见时问内核", self._body)

    def _section(self, name: str, media: bool = True) -> None:
        lab = self._title(name, "SectionTitle")
        self._form.addWidget(lab)
        if media:
            self._media_sections.append(lab)
        else:
            self._usage_section = lab

    def _row(self, key: str, name: str) -> None:
        r = _Row(name, self._body)
        self._form.addWidget(r)
        self._rows[key] = r

    def _build(self) -> None:
        self._section("解码与输出")
        self._row("hwdec-current", "硬解（内核回报）")
        self._row("current-vo", "输出驱动")
        self._row("current-gpu-context", "GPU 上下文")
        self._row("quality_preset", "缩放预设")
        self._section("画面")
        self._row("size", "尺寸")
        self._row("aspect", "显示宽高比")
        self._row("video-codec", "视频编码")
        self._row("container-fps", "源帧率")
        self._row("estimated-vf-fps", "估计渲染帧率")
        self._row("frame-drop-count", "显示丢帧")
        self._row("decoder-frame-drop-count", "解码丢帧")
        self._section("声音")
        self._row("audio-codec", "音频编码")
        self._row("audio-bitrate", "音频码率")
        self._row("volume", "音量")
        self._row("audio-delay", "声音延迟")
        self._row("audio-device", "输出设备")
        self._section("缓存与同步")
        self._row("demuxer-cache-duration", "已缓冲")
        self._row("demuxer-cache-state", "缓存状态")
        self._row("avsync", "音画偏差")
        self._section("文件与内核")
        self._row("tracks", "轨道数")
        self._row("playlist-count", "列表条目")
        self._row("mpv-version", "内核版本")
        self._row("ipc", "IPC 往返")
        # 这一族的数据源不是内核，是 app/engine/usage.py：CPU/内存按 pid 量，
        # GPU 是 nvidia-smi 的**整机**读数（报表里必须带"整机"两个字）。
        # 没点采样按钮之前这些行一律藏着——藏着比写"0%"诚实。
        self._section("资源占用", media=False)
        # 资源占用那一段在没采过样时只有一行标题，下面空着会像"读数没加载出来"。
        # 用一行低对比度的说明占住那个位置，并把入口指向页头那颗按钮。
        self._usage_empty = QLabel("还没量过。点右上角「采样 3 秒」量一轮。", self._body)
        self._usage_empty.setObjectName("HeroHint")
        self._usage_empty.setWordWrap(True)
        self._form.addWidget(self._usage_empty)
        for key, name in USAGE_ROWS:
            self._row(key, name)
            # `_Row` 建出来是**可见**的、右侧还挂着占位符"—"。只在注释里写"没量过就不画"
            # 不算数：那样页面上会摆出"内核  —"这种"有这一行、但说不清是 0 还是没测到"的样子，
            # 正是本页第一条纪律要躲开的。所以建完立刻 show_value(None) 把它按下去。
            self._rows[key].show_value(None)
        self._form.addSpacing(6)
        self._form.addWidget(self._hint())
        self._form.addStretch(1)

    # ================= 取值 =================
    def _get(self, key: str) -> Any:
        """轮询结果优先（它更新），其余读订阅缓存。两边都没有就返回 None。"""
        if key in self._polled:
            return self._polled[key]
        return self.p.prop(key, None)

    # ================= 刷新 =================
    def refresh(self) -> None:
        if self.p.running:
            self.p.poll_stats()        # 结果异步回来后再走一次 _on_stats → _paint
        self._paint()

    def _paint(self) -> None:
        rows = self._rows
        raw_hw = self.p.prop("hwdec-current")
        if raw_hw is None:
            rows["hwdec-current"].show_value(None)      # 还没推来：不猜"软件解码"
        else:
            hard, text = perf.hwdec_state(raw_hw)
            rows["hwdec-current"].show_value(("硬件 " + text) if hard else text)
        vo = self.p.prop("current-vo")
        rows["current-vo"].show_value(str(vo) if vo else None)
        rows["current-gpu-context"].show_value(self._gpu_ctx())
        rows["quality_preset"].show_value(perf.label("quality_preset",
                                                     perf.resolve(self.cfg)["quality_preset"]))
        w, h = int(self.p.prop("width", 0) or 0), int(self.p.prop("height", 0) or 0)
        rows["size"].show_value(f"{w}×{h}" if w and h else None)
        rows["aspect"].show_value(self._aspect())
        rows["video-codec"].show_value(self._text("video-codec"))
        rows["container-fps"].show_value(_fmt_num(self.p.prop("container-fps"), " fps")
                                         if self.p.prop("container-fps") else None)
        rows["estimated-vf-fps"].show_value(_fmt_num(self._get("estimated-vf-fps"), " fps"))
        rows["frame-drop-count"].show_value(self._drops("frame-drop-count"))
        rows["decoder-frame-drop-count"].show_value(self._drops("decoder-frame-drop-count"))
        rows["audio-codec"].show_value(self._text("audio-codec"))
        rows["audio-bitrate"].show_value(self._bitrate())
        rows["volume"].show_value(self._volume())
        rows["audio-delay"].show_value(_fmt_num(self.p.prop("audio-delay"), " s", 2, True)
                                       if self.p.prop("audio-delay") is not None else None)
        rows["audio-device"].show_value(self._device())
        rows["demuxer-cache-duration"].show_value(
            _fmt_num(self.p.prop("demuxer-cache-duration"), " s")
            if self.p.prop("demuxer-cache-duration") is not None else None)
        rows["demuxer-cache-state"].show_value(self._cache_state())
        rows["avsync"].show_value(self._avsync())
        rows["tracks"].show_value(self._tracks())
        pl = self.p.prop("playlist")
        rows["playlist-count"].show_value(str(len(pl)) if isinstance(pl, list) else None)
        rows["mpv-version"].show_value(self.p.version or None)
        rows["ipc"].show_value(self._ipc())
        # 有没有片子只看内核推来的 path：running=True 也可能停在 idle（没有媒体），
        # 而"内核起来了"不等于"有数可看"。
        self._set_media_visible(bool(str(self.p.prop("path") or "")))

    def _set_media_visible(self, on: bool) -> None:
        """有媒体就照常画四段；没有就把它们整体收起，只留一句空态说明。

        资源占用那一段不参与：它是用户主动点出来的，和有没有片子无关。
        收起时不碰 `_Row` 的值，只按可见性；下一轮 `_paint` 会把有数的行重新
        `show_value` 出来（没数的行仍然由各自的值决定藏不藏）。
        """
        on = bool(on)
        self._empty.setVisible(not on)
        for lab in self._media_sections:
            lab.setVisible(on)
        if not on:
            usage = {k for k, _ in USAGE_ROWS}
            for key, row in self._rows.items():
                if key not in usage:
                    row.setVisible(False)

    def _gpu_ctx(self) -> str | None:
        v = self._get("current-gpu-context")
        return str(v) if v else None

    def _text(self, key: str) -> str | None:
        v = self.p.prop(key)
        s = str(v).strip() if v is not None else ""
        return s or None

    def _aspect(self) -> str | None:
        params = self.p.prop("video-out-params")
        if isinstance(params, dict):
            return _fmt_num(params.get("aspect"), "", 3)
        return None

    def _drops(self, key: str) -> str | None:
        """丢帧数：内核没回报过就整行不画（画 0 会被读成"没丢帧"，那是两回事）。"""
        if key not in self._polled and self.p.prop(key) is None:
            return None
        v = self._get(key)
        try:
            return f"{int(v)} 帧"
        except (TypeError, ValueError):
            return None

    def _bitrate(self) -> str | None:
        v = self._get("audio-bitrate")
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return f"{x / 1000.0:.0f} kbps" if x > 0 else None

    def _volume(self) -> str | None:
        if not self.p.running:
            return None
        v = self.p.prop("volume")
        if v is None:
            return None
        return f"{int(float(v))}% / {self.p.volume_max()}%"

    def _device(self) -> str | None:
        """设备名那串 GUID 又长又难读：有描述就用描述，截到能看下的宽度。"""
        name = str(self.p.prop("audio-device") or "")
        if not name:
            return None
        for row in (self._polled.get("audio-device-list") or []):
            if str(row.get("name")) == name and row.get("description"):
                return str(row["description"])[:40]
        return name[:40]

    def _cache_state(self) -> str | None:
        st = self._get("demuxer-cache-state")
        if not isinstance(st, dict):
            return None
        parts = []
        if st.get("cache-duration") is not None:
            parts.append(f"前方 {_fmt_num(st['cache-duration'], ' s') or '?'}")
        if st.get("reader-pts") is not None:
            parts.append(f"读到 {_fmt_num(st['reader-pts'], ' s') or '?'}")
        if st.get("underrun"):
            parts.append("已抽空")
        if st.get("eof"):
            parts.append("已到文件尾")
        return " · ".join(parts) or None

    def _avsync(self) -> str | None:
        v = self._get("avsync")
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return f"{x * 1000.0:+.1f} ms"

    def _tracks(self) -> str | None:
        pl = self.p.prop("track-list")
        if not isinstance(pl, list):
            return None
        n = {"audio": 0, "video": 0, "sub": 0}
        for t in pl:
            k = str(t.get("type", ""))
            if k in n:
                n[k] += 1
        sel = [str(t.get("type")) for t in pl if t.get("selected")]
        text = f"视频 {n['video']} · 音频 {n['audio']} · 字幕 {n['sub']}"
        if sel:
            text += "（在用 " + "/".join(sorted(set(sel))) + "）"
        return text

    def _ipc(self) -> str | None:
        s = self.p.ipc_stats()
        if not s:
            return None
        n = int(s.get("rtt_n", 0) or 0)
        if not n:
            return f"收 {int(s.get('received', 0))} · 无同步请求"
        avg = float(s.get("rtt_ms_sum", 0.0)) / n
        return f"均 {avg:.1f}ms · 最大 {float(s.get('rtt_ms_max', 0.0)):.1f}ms"

    # ================= 资源实测 =================
    def _sample_now(self) -> None:
        """起一轮采样：睡窗口的那三秒在线程池里睡，UI 线程一步都不许停。"""
        if self._job is not None:
            return
        pids = {"内核": self.p.mpv_pid, "外壳": os.getpid()}
        self._usage_pids = pids
        self._job = _SampleJob(pids, USAGE_WINDOW_S)
        self._job.sig.done.connect(self._on_sample_done)
        self.btn_usage.setEnabled(False)
        self.btn_usage.setText("采样中…")
        poolkit.adopt_global_pool("stats").start(self._job)

    def _on_sample_done(self, procs: dict, gpu: dict, why: str, err: str) -> None:
        """回界面线程填行。**不要**在这里做耗时事——报表文本拼接是纯字符串，可以。"""
        self._job = None
        self._usage_procs = dict(procs)
        # 采样时被过滤掉的 pid（内核还没起来就是 None）在这儿补回一行说明，
        # 否则用户只看见"外壳有数、内核那行没了"，分不清是没起来还是量不到。
        # 补的话得按**真实原因**补：pid 空、和采样任务自己崩了，是两回事。都写成
        # "pid 是空的"就是把程序的故障说成用户没放片子——那正是本页要躲的第二类谎。
        for name, pid in self._usage_pids.items():
            if name in self._usage_procs:
                continue
            if err:
                status = "采样没跑成：" + err[:48]
            elif pid:
                status = "这一路没量到（进程侧没回报，见时钟行）"
            else:
                status = "采样时这一路没起来（pid 是空的）"
            self._usage_procs[name] = {"cpu": None, "cpu_share": None, "rss_mb": None,
                                       "status": status}
        self._usage_gpu = dict(gpu)
        self._usage_why = err or why
        frame = {k: self._get(k) for k in ("estimated-vf-fps", "frame-drop-count",
                                           "decoder-frame-drop-count",
                                           "demuxer-cache-duration", "avsync")}
        self._report = usage.build_report(self._usage_procs, self._usage_gpu, frame,
                                          self._usage_why, USAGE_WINDOW_S)
        self.btn_usage.setEnabled(True)
        self.btn_usage.setText(f"再采 {USAGE_WINDOW_S:g} 秒")
        self.btn_copy.setEnabled(True)
        self._usage_empty.hide()
        self._paint_usage()

    def _copy_report(self) -> None:
        if not self._report:
            return
        QApplication.clipboard().setText(self._report)
        self.btn_copy.setText("已复制")
        QTimer.singleShot(1600, lambda: self.btn_copy.setText("复制"))

    def _paint_usage(self) -> None:
        rows = self._rows
        rows["usage_mpv"].show_value(self._proc_text("内核"))
        rows["usage_shell"].show_value(self._proc_text("外壳"))
        if self._usage_gpu:
            g = self._usage_gpu
            rows["usage_gpu"].show_value(
                f"{usage.pct_text(g.get('util'))} · 显存 {usage.num_text(g.get('mem_used'))}"
                f"/{usage.num_text(g.get('mem_total'))} MiB · "
                f"{usage.num_text(g.get('temp'))}°C")
            limits = g.get("limits") or []
            state = [n for n in (g.get("throttle") or []) if n not in limits]
            rows["usage_clock"].show_value(
                f"{usage.num_text(g.get('clock'))} MHz · "
                f"{usage.num_text(g.get('power'), 1)} W · "
                + ("限流：" + "、".join(limits) if limits else "不限流")
                + ("（" + "、".join(state) + "）" if state else ""))
        else:
            rows["usage_gpu"].show_value(None)
            rows["usage_clock"].show_value(
                self._usage_why[:60] if self._usage_why else None)

    def _proc_text(self, name: str) -> str | None:
        d = self._usage_procs.get(name)
        if not d:
            return None
        if d.get("cpu") is None:
            return d.get("status") or "没测到"
        return (f"CPU {usage.pct_text(d['cpu'])}（单核基准）"
                f"· 约占整机 {usage.pct_text(d.get('cpu_share'))} "
                f"· 内存 {usage.num_text(d.get('rss_mb'))} MB")


    # ================= 事件 =================
    def _on_stats(self, info: dict) -> None:
        if info:
            self._polled.update(info)
        self._paint()

    def _on_info(self, info: dict) -> None:
        """订阅推来的东西只在这页可见时重画：页面藏着时没必要跟着每一发刷新。"""
        if self.isVisible() and info:
            self._paint()

    def showEvent(self, ev):
        super().showEvent(ev)
        # 停表/起表挂在显示事件上：QStackedWidget 翻页会 hide 非当前页，
        # 侧栏整体收起时这里同样收到 hideEvent，不需要主窗口来管。
        self._timer.start() if self.p.running else self._timer.stop()
        self.refresh()

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)
