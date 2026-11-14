"""mpv 子进程生命周期 + 面向应用的播放 API。

内核职责：
  - 用 --wid=<hwnd> 把 mpv 的渲染窗口嵌进调用方给的容器（渲染与解码全交给 mpv，零拷贝硬解）
  - 通过 JSON IPC 发命令、订阅属性，把内核状态映射成 Qt 信号
  - 内核崩溃可检测、可自动重启；内核日志写 data/logs/mpv.log 便于排障

选项名与命令名均按本机 mpv v0.41.0 的 --list-options / --input-cmdlist 实测结果使用：
  存在：--wid --input-ipc-server --input-conf --input-vo-keyboard --input-default-bindings
        --input-builtin-bindings --osc --force-window --idle --keep-open --no-resume-playback
        --hwdec --gpu-api --vo --volume-max --sub-auto --slang --alang --cursor-autohide
        --screenshot-directory --screenshot-format --audio-device --demuxer-max-bytes --shuffle
        命令：loadfile seek set cycle frame-step frame-back-step screenshot sub-add audio-add
              playlist-remove playlist-clear playlist-shuffle playlist-play-index ignore
  不存在（因此不使用）：no-op、--ordered、--taskbar
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Iterable

from PyQt5.QtCore import QObject, pyqtSignal

from .. import config as cfgmod
from .. import win32
from . import perf, tweaks, transform
from . import equalizer
from .ipc import IpcError, MpvIpc

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
              ".ts", ".m2ts", ".mts", ".vob", ".ogv", ".3gp", ".f4v", ".rm", ".rmvb", ".asf",
              ".divx", ".mxf", ".nut", ".h264", ".hevc", ".avs", ".vpy", ".mpls", ".m3u8", ".m3u",
              ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".mka",
              ".aiff", ".wv", ".dsf", ".dff", ".ac3", ".eac3", ".dts", ".truehd"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".pgs"}


def mpv_path(p: str) -> str:
    """交给 mpv 子进程的路径一律收成绝对（命名管道原样返回，它不是文件路径）。

    为什么必须有这一步：mpv 是用 `cwd=<mpv 安装目录>` 启的（见 `MpvPlayer.start`），
    而 `--log-file` / `--input-conf` / `--screenshot-directory` 这几个参数里的相对路径
    是在**它那边**求值的。本机实测（数据目录给成相对路径，当时走的是 GELU_DATA 旧名）：三种 `screenshot` 模式全部
    回 "error running command"、shots 目录一个不长，而 mpv 那份解释失败原因的日志也被
    写到 `runtime/mpv/tests/…`（空壳目录），现场连一句原话都找不到——"功能坏了 + 无从排障"
    同时发生。用户手改 config.json 里的 `screenshot_dir` 为相对路径是同一场病，
    所以收口在"发参数给内核"这一道门口，而不是只修上游的 DATA_DIR。
    """
    # 前缀取到 "pipe" 就停：raw 字符串写不成 `r"\\.\pipe\"`（结尾反斜杠会吃掉引号），
    # 而多写一个 `\\` 变成"两个反斜杠结尾"——本机实测那样判据永远不成立，管道名被 abspath
    # 改成一串盘符路径，症状是"内核连不上、界面全死"。少一个字符比多一个安全。
    if p.startswith(r"\\.\pipe"):
        return p
    return os.path.abspath(os.path.expanduser(p))


_OBSERVED = [
    # 播放状态
    "pause", "time-pos", "duration", "volume", "mute", "speed", "eof-reached", "idle-active",
    "core-idle", "paused-for-cache", "playlist", "playlist-pos", "track-list",
    "chapter", "chapter-list", "media-title", "demuxer-cache-duration",
    # 诊断与元信息（UI 直接读缓存，避免在读线程里发同步请求）
    "hwdec-current", "video-out-params", "container-fps", "shuffle",
    "path", "video-codec", "audio-codec", "container-format", "width", "height", "current-vo",
    "aid", "vid", "sid", "secondary-sid", "sub-delay", "audio-delay",
    "loop-file", "loop-playlist", "ab-loop-a", "ab-loop-b", "volume-max", "audio-device",
    # 可调项（tweaks 表里那些）：订阅来的是"内核眼下的真值"，面板开局按它画滑块，
    # 而不是按配置文件猜——两边不一致时（比如老配置里留着 6 位颜色）用户看到的是内核。
    "brightness", "contrast", "gamma", "saturation", "hue",
    "sub-scale", "sub-border-size", "sub-shadow-offset", "sub-color", "sub-back-color",
    "sub-bold", "sub-visibility", "sub-use-margins", "sub-align-x", "sub-align-y",
    "tone-mapping", "gamut-mapping-mode", "target-peak",
    # 画面平滑四项：都是普通属性，实测可读可写可订阅（tests/probe_smoothness.py）。
    "video-sync", "interpolation", "tscale", "deinterlace",
    # 画面变换（engine/transform.py 的几何那一半）：同样订阅内核真值。
    # 注意这里**没有** "vf"——滤镜列表在硬解帧上会"名字照在、内核其实把滤镜扔了"
    # （实测 unsharp 单独发下去就是这样），所以链的真相只能靠"日志里有没有禁用行"
    # 与像素，订阅它只会给界面一个会骗人的清单。
    "video-aspect-override", "video-rotate", "keepaspect", "panscan",
    "video-pan-x", "video-pan-y",
]


# 没有专用信号的属性：统一从 sig_info 报出去（UI 侧按 key 分发）
_DEDICATED = {"time-pos", "duration", "pause", "volume", "mute", "speed", "eof-reached",
              "idle-active", "core-idle", "paused-for-cache", "playlist", "playlist-pos",
              "track-list", "chapter", "chapter-list", "media-title", "demuxer-cache-duration"}
_INFO_KEYS = {n for n in _OBSERVED if n not in _DEDICATED}


def is_media_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS


def is_sub_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUB_EXTS


def kernel_flag(value: Any, default: bool = False) -> bool:
    """把内核回报/配置里读到的"开关"收成一个布尔。

    为什么不用 `bool(v)`：`bool("no")` 是 True。mpv 对**布尔选项**通常推 JSON 的
    true/false，但 `loop-file`/`loop-playlist` 这一族在内核那边就是拿字符串 "no"
    表示"没开"；而配置是手可改的 JSON，写成 "false" 也不是没发生过。关窗回写若原样
    用 bool()，"没开"会被写成"开"，下次启动就真的循环了——而且现场没有任何报错。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "no", "false", "0", "off"):
            return False
        if s in ("yes", "true", "1", "on"):
            return True
        return default
    if isinstance(value, (int, float)):
        return value != 0
    return default


def chapter_target(count: int, current: int, delta: int) -> tuple[int | None, str]:
    """上一章/下一章要落在哪一个下标。返回 (目标, 边界)；没有章节时目标为 None。

    边界不靠"算出来超界就静默不动"：调用方要用它说一句"已是第一章/最后一章"。
    没有当前章节（`chapter` 是 −1，常见于刚载入/无章节文件）时，+1 落在 0、
    −1 落在最后一章；这支片没有章节时整条判据在调用方就说"没有章节"。
    """
    if count <= 0:
        return None, ""
    if current < 0:
        return (0 if delta > 0 else count - 1), ""
    target = current + delta
    if target < 0:
        return 0, "first"
    if target >= count:
        return count - 1, "last"
    return target, ""


class MpvPlayer(QObject):
    """把 mpv 内核包装成信号/方法形式的播放对象。"""

    sig_time = pyqtSignal(float)
    sig_duration = pyqtSignal(float)
    sig_pause = pyqtSignal(bool)
    sig_volume = pyqtSignal(int)
    sig_mute = pyqtSignal(bool)
    sig_speed = pyqtSignal(float)
    sig_eof = pyqtSignal(bool)
    sig_idle = pyqtSignal(bool)
    sig_cache = pyqtSignal(bool)
    sig_buffered = pyqtSignal(float)
    sig_playlist = pyqtSignal(list)
    sig_pos = pyqtSignal(int)
    sig_tracks = pyqtSignal(list)
    sig_chapters = pyqtSignal(list)
    sig_chapter = pyqtSignal(int)
    sig_title = pyqtSignal(str)
    sig_info = pyqtSignal(dict)
    sig_file_loaded = pyqtSignal()
    sig_saved = pyqtSignal(str)
    sig_died = pyqtSignal(int)
    sig_log = pyqtSignal(str)
    # 滚轮落在内核子窗口上，界面收不到 Qt 事件；内核按 input.conf 里那句
    # `script-message luhui-wheel <kind> <value>` 把动作回灌上来（kind: vol/seek）。
    sig_wheel = pyqtSignal(str, str)
    # 统计页按需轮询的回报（一次轮询一条，不在订阅里）：见 poll_stats 的注释
    sig_stats = pyqtSignal(dict)

    def __init__(self, cfg: cfgmod.Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.pipe_path = r"\\.\pipe\%s-%d" % (cfgmod.BRAND_ID, os.getpid())
        self.conf_dir = os.path.join(cfgmod.DATA_DIR, "mpv")
        self.log_path = os.path.join(cfgmod.LOG_DIR, "mpv.log")
        self._ipc: MpvIpc | None = None
        self._proc: subprocess.Popen | None = None
        self._props: dict[str, Any] = {}       # 订阅来的最新属性值（读它不阻塞）
        # `_props` 的写者是 IPC 读线程（_apply），读者是 UI 主线程（prop/open_files/set_tweak…）。
        # dict 的单条 get/set 自身原子，但"清一批键""读一份快照"这类复合操作不是——
        # 换片瞬间的 _FILE_SCOPED 清空与 file-loaded 的快照曾经可以交错。RLock 而不是 Lock：
        # `_apply` 持锁期间可能被同一个线程的属性回调重入（本机实测过 stats 持锁调 pools 的死锁）。
        self._props_lock = threading.RLock()
        # 内核 `vf` 列表里当前的滤镜名。**缓存这个而不是每次现问**：统计页与面板 redraw
        # 都要读它，同步往返会把 UI 线程拖住；且它从来不能单独当"滤镜在跑"的证据
        # （硬解帧下滤镜会被内核扔掉、列表照样好看），所以真判据在内核日志那几行，
        # 由 apply_filter_chain 每次下发时一并读（transform.filter_failures）。
        self._vf_names: list[str] = []
        # 上一条链下发时从内核日志里挑出的"这个滤镜其实没跑起来"的原话，与 _vf_names 同批更新。
        # 界面要在那一刻说出原因，而原因只存在于刚才那一小段日志里（下一轮就被覆盖），
        # 所以在这里存下来。它和 _vf_names 的分工＝"证据"与"清单"：名单会骗人，这几行不会。
        self._vf_failures: list[str] = []
        # 声音均衡器（engine/equalizer.py）：与上面两件同构，但形状不同——`af` 的回报不是
        # 名字表而是整串滤镜图，所以这里存的是**反解出来的逐档 dB**（读不懂的原话与失败行
        # 各存一份）。开面板时画的就是这三件，不是配置值：配置是愿望，这一族愿望落空的
        # 下场是"整个没声音"，界面上必须看得见。
        self._eq_vals: dict[str, float] = {}
        self._eq_problems: list[str] = []
        self._eq_failures: list[str] = []
        self._hwnd: int | None = None
        self._started = False
        # 启动握手拆成 begin_start/poll_start 之后的状态（GUI 用定时器轮询）。
        # 锁是必须的：看门狗线程会在内核死后自己 start()，而用户这时可能又拖进来
        # 一支片，GUI 那条路也会 begin_start——两遍收尾会重复订阅/重复挂看门狗。
        self._starting = False
        self._start_timeout = 10.0
        self._start_deadline = 0.0
        self._start_lock = threading.RLock()
        self._quitting = False
        self._restart_count = 0
        self._proc_started = 0.0       # Popen 成功的时刻：看门狗用它判"久跑"并重置额度
        self._perf_notes: list[str] = []
        self._stats_busy = False
        self._watchdog: threading.Thread | None = None
        self.last_files: list[str] = []
        self.last_pos = 0.0
        # 第一条 replace 命令已经用 `start=` 带到位置上的那条路径：file-loaded 回来时
        # 认出来、别再 seek 一次（seek 会打断内核刚做完的精确起播）。
        self._resume_skip = ""
        self.version = ""
        # expand() 的读数：走没走到封顶、这一趟交回几个、走过几个目录（验收读，不是给人看的提示）。
        # 拖一个盘根 / 下载夹进来时，os.walk 曾经是全仓唯一没有上限的目录遍历。
        self.last_expand_truncated = False
        self.last_expand_count = 0
        self.last_expand_dirs = 0
        self.expand_calls = 0
        # 章节连按的"期望值"：set_property 要等内核推回 chapter 才进缓存，连按时第二下
        # 若还读旧缓存就会原地踏步。存 (目标, 发送前缓存值, 时刻)，只在缓存还没翻面时用它。
        self._chapter_expect: tuple[int, int, float] | None = None

    # ================= 进程与连接 =================
    def _write_input_conf(self) -> str:
        """滚轮消息只会落在 mpv 子窗口上，故由 mpv 响应滚轮；键盘与按钮交给外壳。

        两件防护，都是真机上踩出来的：
          · 内容没变就不落盘。`build_args` 每次启动/自检都走这里，而 Windows 上连续
            `os.replace` 同一个目标会撞上杀软正在扫描的句柄——2026-10-25 跑判据时实测
            `WinError 5 拒绝访问`。这份内容几乎是常量，白写没有收益。
          · 真写的时候允许重试几次再认输；认输也只是抛给 build_args 去降级，
            不许让"输入配置写不进去"变成"播放器起不来"。
        """
        path = os.path.join(self.conf_dir, f"{cfgmod.BRAND_ID}-input.conf")
        # 滚轮由内核响应（wheel 事件只落在它的子窗口上），但界面得知道"滚了多少"，
        # 否则鼠标调音量时屏幕上一点反馈都没有。script-message 实测会把消息作为
        # client-message 事件回灌给 IPC 客户端（tests/probe_playlist_move.py 第③条）。
        #   音量那条带 ${volume}：内核在**执行到这一句时**展开属性（tests/probe_wheel_order.py
        # 实测 `add volume 2; … ${volume}` 得到加完后的 68，而不是旧的 66），于是界面
        # 不必去猜"缓存是不是还差一格"——client-message 比 volume 推送先到，读缓存会慢一格。
        #   seek 必须是 relative：原来的 `seek 5 absolute+exact` 是"跳到第 5 秒"，
        # 实测 Ctrl+滚轮永远从 1s 跳到 5s、再滚还是 5s（tests/probe_wheel_order.py 第③条）。
        body = (
            f"# {cfgmod.APP_TITLE}自动生成，改动会被覆盖\n"
            "WHEEL_UP        add volume 2;  script-message luhui-wheel vol ${volume}\n"
            "WHEEL_DOWN      add volume -2; script-message luhui-wheel vol ${volume}\n"
            "CTRL+WHEEL_UP   seek  5 relative+exact; script-message luhui-wheel seek +5\n"
            "CTRL+WHEEL_DOWN seek -5 relative+exact; script-message luhui-wheel seek -5\n"
        )
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if fh.read() == body:
                    return path
        except OSError:
            pass
        os.makedirs(self.conf_dir, exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            for attempt in range(3):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.06)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return path

    @staticmethod
    def _loop_args(c) -> list[str]:
        mode = str(c.get("loop_playlist", "no") or "no")
        args: list[str] = []
        if mode == "one":
            args.append("--loop-file=inf")
        elif mode in ("inf", "yes", "true"):
            args.append("--loop-playlist=inf")
        if bool(c.get("shuffle")):
            args.append("--shuffle=yes")
        return args

    def build_args(self, hwnd: int) -> list[str]:
        c = self.cfg
        # 硬解/GPU API/缩放预设/音量上限：全部出自 engine/perf.py 那张表。
        # 为什么不让配置里的原值直接拼进来：实测 --hwdec=bogus-value 时内核照样启动，
        # 但把 hwdec-current 落成 'no'（静默退回软解），用户只知道"变卡了"。
        # 表会把它回退成默认档，回退这件事由 startup_args 的第二返回值报出去（见 start）。
        perf_args, self._perf_notes = perf.startup_args(c)
        # hwnd=0 时不自嵌入（供无界面自检使用，mpv 自己开窗）
        embed = [f"--wid={hwnd}"] if hwnd else ["--force-window=yes"]
        # 输入配置写不进去只该丢滚轮绑定，不该让整次播放起不来——这条文件写在磁盘/
        # 杀软手里，不是我们能完全掌控的（文件头注释里记了实测的 WinError 5）。
        conf: list[str] = []
        try:
            conf = [f"--input-conf={mpv_path(self._write_input_conf())}"]
        except OSError as exc:
            self.sig_log.emit(f"输入配置写不进去（滚轮音量/快进会失效）：{exc}")
        return [
            cfgmod.MPV_EXE,
            *embed,
            f"--input-ipc-server={self.pipe_path}",
            *conf,
            f"--log-file={mpv_path(self.log_path)}",
            "--no-config",
            "--terminal=no",
            # 日志级别：all=v 会把 GLSL/HLSL 着色器全文写进日志（实测上百 KB，拖慢启动），
            # 界面只要 warn 以上；细粒度排查时用 LUHUI_MPV_VERBOSE（旧名 GELU_MPV_VERBOSE）临时提级。
            f"--msg-level={os.environ.get('LUHUI_MPV_VERBOSE') or os.environ.get('GELU_MPV_VERBOSE') or 'all=warn'}",
            # 输入路由：键盘=外壳，滚轮=内核（见 _write_input_conf）
            "--input-vo-keyboard=no",
            "--input-default-bindings=no",
            "--input-builtin-bindings=no",
            "--no-osc",
            "--osd-level=0",
            "--force-window=yes",
            "--idle=yes",
            "--keep-open=yes",
            "--no-resume-playback",
            "--no-window-dragging",
            "--cursor-autohide=1200",
            # 解码与画质（engine/perf.py）
            *perf_args,
            "--vo=gpu",
            "--hdr-compute-peak=auto",
            "--target-prim=auto",
            "--target-trc=auto",
            # 音量/速度
            f"--volume={c['volume']}",
            f"--mute={'yes' if c['mute'] else 'no'}",
            f"--speed={c['speed']}",
            # 循环/乱序：配置里这两个键以前只有一半闭环（菜单会写 loop、没人读回来；
            # 静音/倍速/乱序则连写都没写）。关窗时从内核真值回写（main_window.closeEvent），
            # 这里再带回内核，重启才是"上次什么样，这次还什么样"。
            *self._loop_args(c),
            "--sub-auto=all",
            "--slang=zh,chs,zh-CN,chinese",
            "--alang=zh,chs,zh-CN,jpn,eng",
            # 截图（模板必须在启动时给：set_property 改它不生效，实测产物仍是 mpv-shot0001.png）
            f"--screenshot-directory={mpv_path(c['screenshot_dir'])}",
            "--screenshot-format=png",
            "--screenshot-jpeg-quality=95",
            f"--screenshot-template={cfgmod.BRAND_ID}-%F-%n",
            f"--audio-device={c['audio_device']}",
            # 缓存
            "--cache=yes",
            "--demuxer-max-bytes=200MiB",
            "--demuxer-max-back-bytes=120MiB",
            # 字幕样式 + 画面均衡 + HDR：全部由 engine/tweaks.py 那张表拼出来。
            # 为什么启动就要下发而不是等面板来 set_property：第一次播放之前的那几秒
            # 必须已经是用户要的样子，而且——只有走同一条表才不会出现"界面能拖到 130、
            # 内核只收到 100"这种两边各写一遍必然对不上的错。
            *tweaks.startup_args(c),
            # 画面变换（比例/旋转/充满/平移）+ 画质增强滤镜链：engine/transform.py。
            # 裁剪用 iw/ih 表达式（实测内核自己按片子尺寸求值），所以启动就能下发、
            # 不必先知道分辨率；链为空时 startup_args 不发 --vf——裸 format=yuv420p
            # 会让硬解帧白跑一趟 CPU，实测像素动了 0.01，这钱不该为"没开任何滤镜"付。
            *transform.startup_args(c),
            # 声音均衡器（十段 + 前置）：engine/equalizer.py。链为空时**一条都不发**——
            # 实测 `--af=lavfi=[]`（空图）会让内核 `Audio filter initialized failed!`，
            # 下场不是"这一档没生效"而是整个进程没声音，所以宁可不发。
            *equalizer.startup_args(c),
        ]

    def begin_start(self, hwnd: int, wait: float = 10.0) -> bool:
        """把内核进程拉起来，但**不等**管道握手；结果由 poll_start() 一拍一拍取。

        为什么把等待拆出去：`connect(timeout=10)` 在 GUI 线程里最长能堵 10 秒。正常
        0.42s 的首播也会让窗口不重画（用户看到黑屏＋转圈）；内核真出问题时整整 10 秒
        "未响应"。这里只做 Popen（毫秒级），把"等管道出现"交给调用方的定时器。

        返回 False 只表示"连进程都没拉起来"；True 之后必须继续 poll_start 到 ok/fail。
        """
        with self._start_lock:
            if self._started or self._starting:
                return True
            if not os.path.exists(cfgmod.MPV_EXE):
                self.sig_log.emit(f"找不到播放内核：{cfgmod.MPV_EXE}")
                return False
            self._hwnd = hwnd
            os.makedirs(cfgmod.LOG_DIR, exist_ok=True)
            try:
                # build_args 也在这只 try 里：它现在会碰盘（input-conf）。GUI 首次播放
                # 走的就是这条路，任何异常从这里穿进 Qt 槽都会变成进程 abort——宁可
                # 少一个 --input-conf（build_args 自己已兜一层），也不能让窗口没了。
                self._proc = subprocess.Popen(
                    self.build_args(hwnd),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    cwd=os.path.dirname(cfgmod.MPV_EXE),
                )
            except Exception as exc:      # noqa: BLE001 - 见上：Qt 槽里不许抛
                self.sig_log.emit(f"启动 mpv 失败：{type(exc).__name__}: {exc}")
                return False
            self._proc_started = time.monotonic()
            # 与 poolkit.spawn 同一条理由：正常退出有 close() 杀它，主进程被强杀/崩溃时
            # 只有 Job 的 kill-on-close 能把 mpv 带走（否则窗口没了声音还在）。
            win32.assign_to_kill_job(self._proc.pid)
            self._ipc = MpvIpc(self.pipe_path)
            self._starting = True
            self._start_timeout = max(0.5, float(wait))
            self._start_deadline = time.monotonic() + self._start_timeout
            return True

    def poll_start(self) -> str:
        """非阻塞推进一次启动握手：'ok' / 'pending' / 'fail'。

        不睡、不等待：一次 CreateFileW 尝试 + 成功后的收尾（订阅、版本、看门狗）。
        超时、内核早退、收尾异常都在这里收干净（close + kill），再交一句人话给 sig_log。
        可被 GUI 定时器与看门狗线程同时调用，所以整段持锁。
        """
        with self._start_lock:
            if self._started:
                return "ok"
            if not self._starting:
                return "fail"
            proc = self._proc
            if proc is None or proc.poll() is not None:
                code = proc.poll() if proc is not None else "?"
                return self._start_failed(f"播放内核刚启动就退出了（code={code}）")
            ipc = self._ipc
            if ipc is None:
                return self._start_failed("播放内核的 IPC 通道没建起来")
            try:
                ready = ipc.try_connect(proc)
            except IpcError as exc:
                return self._start_failed(str(exc))
            if not ready:
                if time.monotonic() >= self._start_deadline:
                    return self._start_failed(
                        f"播放内核启动超时（等 IPC 管道 {self._start_timeout:.1f}s，"
                        f"GetLastError={ipc.last_open_error}）")
                return "pending"
            try:
                self._finish_start()
            except Exception as exc:      # noqa: BLE001 - 槽里穿出去就是整进程 abort
                return self._start_failed(
                    f"播放内核握手后初始化失败：{type(exc).__name__}: {exc}")
            return "ok"

    def _finish_start(self) -> None:
        """管道已连上之后的收尾：与旧 start() 里那一段逐行同源。"""
        assert self._ipc is not None
        self._wire_events()
        with self._props_lock:
            self._props.clear()
        # 滤镜名缓存跟着内核一起归零：换进程就是换内核，链上什么都没有。
        # 注意这里**不**清 `_FILE_SCOPED` 那条路径——实测链子跨 loadfile 留着，
        # 换片时清掉会让面板显示"没开滤镜"，而内核其实还在跑上一条。
        self._vf_names = []
        # 均衡器那三份缓存同理归零：`read_audio_eq` 在纯读时**故意**沿用上一次下发的失败行
        # （没有日志窗口就没有新证据），而换进程是另一条链、另一次表态，旧会话的坏消息
        # 不许跟着跨过内核重启。启动期那条 `--af` 到底挂没挂上，由 `_scan_log_for_fatal`
        # 与面板的 `sig_log` 那一路报（`AudioEqPanel._on_engine_log`）。
        self._eq_vals, self._eq_problems, self._eq_failures = {}, [], []
        obs_fail: list[str] = []
        for name in _OBSERVED:
            try:
                self._ipc.observe(name, self._make_cb(name))
            except IpcError as exc:
                obs_fail.append(f"{name}: {exc}")
        if obs_fail:
            # 订阅失败会让整个 UI 静默失效，绝不能吞掉
            self.sig_log.emit(f"属性订阅失败 {len(obs_fail)}/{len(_OBSERVED)} 项："
                              + "；".join(obs_fail[:3]))
        self._started = True
        self._starting = False
        self._start_deadline = 0.0
        for note in self._perf_notes:
            # 档位被回退必须喊出来：内核面对非法 hwdec 是静默降级（见 engine/perf.py），
            # 不报的话用户只会觉得"这播放器怎么变卡了"，一点线索都没有。
            self.sig_log.emit("性能档位：" + note)
        self.version = str(self.get("mpv-version", "?"))
        self._scan_log_for_fatal()
        self._start_watchdog()

    def _start_failed(self, message: str) -> str:
        """把半启动状态收干净（句柄 + 进程），把人话留给 sig_log，返回 'fail'。"""
        self._starting = False
        self._started = False
        self._start_deadline = 0.0
        if not self._quitting:
            self.sig_log.emit(message)
        ipc = self._ipc
        if ipc is not None:
            try:
                ipc.close()
            except Exception:
                pass
            self._ipc = None
        self._kill()
        return "fail"

    def start(self, hwnd: int, wait: float = 10.0) -> bool:
        """同步启动（自检与看门狗用）。GUI 走 begin_start/poll_start，别在这里等。"""
        if self._started:
            return True
        if not self.begin_start(hwnd, wait):
            return False
        while time.monotonic() < self._start_deadline:
            state = self.poll_start()
            if state == "ok":
                return True
            if state == "fail":
                return False
            time.sleep(0.02)
        with self._start_lock:
            self._start_failed(f"播放内核启动超时（等 IPC 管道 {wait:.1f}s）")
        return False

    def _scan_log_for_fatal(self) -> None:
        """内核换版本后可能出现选项名失效：这类失败是静默的，必须主动报出来。

        只读尾部 8KB：启动失败的原话总是在最后几行，而 mpv 的日志会跟着会话一直长
        （用户放一晚上就几十 MB）——整份 read() 既白占一次长读盘，也白拷一整份进内存。
        """
        try:
            size = os.path.getsize(self.log_path)
            with open(self.log_path, "rb") as fh:
                fh.seek(max(0, size - 8192))
                tail = fh.read().decode("utf-8", "ignore")
            bad = [ln for ln in tail.splitlines() if "option not found" in ln or "Fatal error" in ln]
            for ln in bad[:6]:
                self.sig_log.emit("内核参数不兼容：" + ln.strip())
        except OSError:
            pass

    def _wire_events(self) -> None:
        assert self._ipc is not None
        self._ipc.on("start-file", lambda m: self._on_start_file(m))
        self._ipc.on("file-loaded", lambda m: self._on_file_loaded())
        self._ipc.on("end-file", lambda m: self._on_end_file(m))
        # 注意：这里**没有** file-saved。实测本机 mpv v0.41.0-1101 截图时全量事件里不出现
        # file-saved，产物路径只在 screenshot 命令的应答 data.filename 中（见 snapshot）。
        self._ipc.on("log", lambda m: self._on_log(m))
        # 滚轮/测试用的 script-message 会以 client-message 事件回到这里
        self._ipc.on("client-message", lambda m: self._on_client_message(m))

    def quit(self) -> None:
        with self._start_lock:
            self._quitting = True
            # 正在异步握手的那一趟就此作废：GUI 的轮询定时器会在 closeEvent 里停掉，
            # 但看门狗线程可能正卡在 start() 的循环里，得让它下一次 poll 立刻拿到 fail。
            self._starting = False
            self._start_deadline = 0.0
            if self._ipc:
                try:
                    self._ipc.fire(["quit"])
                    time.sleep(0.04)
                except Exception:
                    pass
                self._ipc.close()
                self._ipc = None
            self._kill()
            self._started = False

    def _kill(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None

    def _start_watchdog(self) -> None:
        """内核退出（崩溃/被杀）时上报并有限次自动重启，尽量不丢播放位置。"""
        def loop():
            proc = self._proc
            if proc is None:
                return
            code = proc.wait()
            if self._quitting:
                return
            self._started = False
            self.sig_died.emit(int(code or -1))
            # 久跑过的内核崩溃是"新事故"，不是"这个内核反复起不来"：不重置额度的话，
            # 用户看了一整晚、偶发崩一次之后，后面再崩就永远不再自愈（额度早用完了）。
            ran = (time.monotonic() - self._proc_started) if self._proc_started else 0.0
            if ran >= 60.0:
                self._restart_count = 0
            # 只按"用户是否主动退出"决定要不要重启：hwnd=0（无界面自检）同样要能重启，
            # 之前写成 `and self._hwnd` 会让无窗口模式永远不自愈。
            if self._restart_count < 2 and not self._quitting:
                self._restart_count += 1
                self.sig_log.emit(f"播放内核退出(code={code})，第 {self._restart_count} 次自动重启")
                time.sleep(0.35)
                # 睡这一觉的时候用户可能已经关窗（quit 置了 _quitting）。旧写法睡醒
                # 直接 start：窗口没了、内核又被拉起来，声音继续放，任务管理器里还多一个
                # 没人认领的进程。睡醒必须重新问一次。
                if self._quitting:
                    return
                # 旧管道必须先关：读/写线程与句柄都挂在它身上，直接换引用等于把
                # 两个线程和一根命名管道丢给 GC——重启几次就在任务管理器里攒几个线程。
                # 关掉但**不置 None**：start() 成功会用新 IPC 覆盖；失败时留着一个
                # alive=False 的对象，后续请求得到的是立刻 IpcError，而不是 None 上的
                # AttributeError（那会把一次重启失败升级成槽里炸掉）。
                old_ipc = self._ipc
                if old_ipc is not None:
                    try:
                        old_ipc.close()
                    except Exception:
                        pass
                if self.start(self._hwnd):
                    files = self.last_files[:]
                    pos = self.last_pos
                    if files:
                        # 位置交给 loadfile 的 start=：在 file-loaded 之前发 seek 会被内核
                        # 丢弃或报 property unavailable（旧写法就是这样丢续播的）。
                        self.open_files(files, play_index=0,
                                        start_pos=pos if pos > 1 else 0.0)

        self._watchdog = threading.Thread(target=loop, name="mpv-watchdog", daemon=True)
        self._watchdog.start()

    _NOISY = ("cplayer", "voparam", "fdwrite", "client API")

    def _on_log(self, msg: dict) -> None:
        level = str(msg.get("level", ""))
        text = str(msg.get("data", ""))
        if level in ("error", "fatal") or any(k in text for k in ("error", "Failed", "failed")):
            self.sig_log.emit(f"[mpv:{level}] {text}")

    def _on_client_message(self, msg: dict) -> None:
        """script-message 的载荷：args = [名字, 类别, 值]。只认自己那条前缀。"""
        args = [str(a) for a in ((msg or {}).get("args") or [])]
        if len(args) >= 3 and args[0] == "luhui-wheel":
            self.sig_wheel.emit(args[1], args[2])

    # ================= 属性 → 信号 =================
    def _make_cb(self, name: str):
        def cb(value):
            self._apply(name, value)
        return cb

    def prop(self, name: str, default: Any = None) -> Any:
        """读订阅缓存（不阻塞、不发请求，可在任何线程安全调用）。"""
        with self._props_lock:
            v = self._props.get(name, default)
        return default if v is None else v

    # 没有媒体时内核把这些属性推成字符串 "no"（不是数字）。缓存里留着 "no"，
    # UI 侧任何一处 float(prop("duration")) 都会 ValueError——而 closeEvent 里就有
    # 一处（保存进度），异常从 Qt 回调里穿出去 = PyQt5 直接 abort，表现为"关窗口无声崩"。
    # 所以在入缓存这一道就归一成数字；ab-loop-a/b 不在其列，那里的 "no" 是"未设点"的语义。
    _NUMERIC = ("time-pos", "duration", "volume", "speed", "demuxer-cache-duration",
                "volume-max", "width", "height", "container-fps")

    @staticmethod
    def _num(value: Any) -> float:
        if isinstance(value, bool) or value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return 0.0

    def _apply(self, name: str, value: Any) -> None:
        if name in self._NUMERIC:
            value = self._num(value)
        # 入缓存在锁内、发信号在锁外：信号是 queued 到 UI 线程的，UI 反过来可能读缓存；
        # 若持着锁发信号，读线程等 UI、UI 等锁，一次换片就能把两边钉住。
        with self._props_lock:
            self._props[name] = value
        try:
            if name == "time-pos":
                self.sig_time.emit(float(value or 0.0))
            elif name == "duration":
                self.sig_duration.emit(float(value or 0.0))
            elif name == "pause":
                self.sig_pause.emit(bool(value))
            elif name == "volume":
                self.sig_volume.emit(int(round(float(value or 0))))
            elif name == "mute":
                self.sig_mute.emit(bool(value))
            elif name == "speed":
                self.sig_speed.emit(float(value or 1.0))
            elif name == "eof-reached":
                self.sig_eof.emit(bool(value))
            elif name in ("idle-active", "core-idle"):
                self.sig_idle.emit(bool(value))
            elif name == "paused-for-cache":
                self.sig_cache.emit(bool(value))
            elif name == "playlist":
                self.sig_playlist.emit(list(value or []))
            elif name == "playlist-pos":
                self.sig_pos.emit(int(value) if isinstance(value, (int, float)) else -1)
            elif name == "demuxer-cache-duration":
                self.sig_buffered.emit(float(value or 0.0))
            elif name == "track-list":
                self.sig_tracks.emit(list(value or []))
            elif name == "chapter-list":
                self.sig_chapters.emit(list(value or []))
            elif name == "chapter":
                idx = int(value) if isinstance(value, (int, float)) else -1
                exp = self._chapter_expect
                if exp is not None and idx != exp[1]:
                    self._chapter_expect = None      # 内核已翻面/被别处改了：期望值作废
                self.sig_chapter.emit(idx)
            elif name == "media-title":
                self.sig_title.emit(str(value or ""))
            elif name in _INFO_KEYS:
                self.sig_info.emit({name: value})
        except Exception as exc:
            self.sig_log.emit(f"状态更新异常({name}): {exc}")

    # ================= 命令封装 =================
    def _req(self, command: list, timeout: float = 2.5) -> Any:
        if self._ipc is None:
            return None
        try:
            return self._ipc.request(command, timeout=timeout)
        except IpcError as exc:
            self.sig_log.emit(str(exc))
            return None

    def _fire(self, command: list) -> None:
        if self._ipc is None:
            return
        try:
            self._ipc.fire(command)
        except IpcError as exc:
            self.sig_log.emit(str(exc))

    def get(self, name: str, default: Any = None) -> Any:
        if self._ipc is None:
            return default
        try:
            v = self._ipc.request(["get_property", name], timeout=1.5)
            return default if v is None else v
        except IpcError:
            return default

    def set_prop(self, name: str, value: Any) -> None:
        self._fire(["set_property", name, value])

    # ================= 统计页按需轮询 =================
    # 统计页要问、但**故意不进订阅**的属性。理由是本机实测的推送频率（5 秒播放内的
    # property-change 次数）：avsync 175 次≈每帧一次、estimated-vf-fps 19 次、
    # demuxer-cache-state 12 次、audio-bitrate 12 次；对照组 time-pos 也是 175 次，
    # 而它是控制条每一秒都要画的东西，那一份广播非付不可。统计页一秒只看一次，
    # 为它挂六个每帧级订阅会把 sig_info 刷满（调节页、控制条都收同一个信号），
    # 所以这一族走"页面可见时按需问一轮"。
    STATS_PROPS = ("estimated-vf-fps", "avsync", "demuxer-cache-state", "audio-bitrate",
                   "frame-drop-count", "decoder-frame-drop-count", "current-gpu-context",
                   # 设备清单本身是静态的，但它不是订阅属性（内核不推变化），而统计页要
                   # 把 wasapi/{GUID} 这种名字翻成人话，只能随这一轮一起问。
                   "audio-device-list")

    def stats_snapshot(self) -> dict:
        """同步问一轮 STATS_PROPS；问不到的项**不进结果**（界面据此整行不画，而不是写 0）。

        别在 IPC 读线程里调用：ipc.request 在那条线程上会直接抛 IpcError（自我死锁守卫），
        这条方法是给"页面可见时的一轮轮询"和自测用的。
        """
        ipc = self._ipc
        if ipc is None:
            return {}
        out: dict[str, Any] = {}
        for name in self.STATS_PROPS:
            try:
                out[name] = ipc.request(["get_property", name], timeout=1.2)
            except IpcError:
                continue
        return out

    def poll_stats(self) -> None:
        """把一轮 stats_snapshot 扔到后台线程，结果用 sig_stats 投回 UI 线程。

        为什么不用 request_async 一条条发：那会给每个属性起一条线程、同时抢管道写锁，
        一秒七个属性就是七个线程；这里一条线程串行问完。同时在途只允许一轮
        （上一轮没回来就跳过这一秒）——统计页晚一秒刷新没人看得出，轮询堆积会卡 UI。
        """
        if self._ipc is None or self._stats_busy:
            return
        self._stats_busy = True

        def work():
            try:
                out = self.stats_snapshot()
            finally:
                self._stats_busy = False
            if out:
                self.sig_stats.emit(out)

        threading.Thread(target=work, name="mpv-stats-poll", daemon=True).start()

    # 一个文件夹最多展开这么多（B3）。library.walk 早就有 3000 的上限，而这条路是
    # "拖一个目录进来播放"——它同样吃用户随手拖来的盘根/下载夹，却一直无边遍历：
    # 一次 os.walk 几十万项，既把界面线程占住，又把整份列表灌进播放列表。到顶就停走，
    # 并把 `last_expand_truncated` 置真（静默截断也要有读数，不能让"就这些"和"截了"分不清）。
    MAX_EXPAND = 3000
    # 只限文件数管不住"目录极多、媒体极少"（比如拖进来一个 node_modules 或盘根）：
    # os.walk 的空转成本来自**目录**，不是交回来的文件。到顶同样停走并记读数。
    MAX_WALK_DIRS = 20000

    def expand(self, path: str, max_files: int = MAX_EXPAND) -> list[str]:
        """文件或文件夹 → 媒体文件列表（目录展开有封顶，见 MAX_EXPAND / MAX_WALK_DIRS）。"""
        self.expand_calls += 1
        self.last_expand_truncated = False
        self.last_expand_dirs = 0
        limit = max(1, int(max_files))
        if os.path.isdir(path):
            out = []
            visited = 0
            for root, _dirs, files in os.walk(path):
                if visited >= self.MAX_WALK_DIRS:
                    self.last_expand_truncated = True
                    break
                visited += 1
                for f in files:
                    if is_media_file(f):
                        out.append(os.path.join(root, f))
                        if len(out) >= limit:
                            self.last_expand_truncated = True
                            break
                if self.last_expand_truncated:
                    break
            self.last_expand_dirs = visited
            self.last_expand_count = len(out)
            return sorted(out, key=lambda p: (os.path.dirname(p).lower(), os.path.basename(p).lower()))
        if is_media_file(path):
            self.last_expand_count = 1
            return [path]
        if os.path.splitext(path)[1].lower() in (".m3u", ".m3u8"):
            out = self._parse_m3u(path)
            self.last_expand_count = len(out)
            return out
        self.last_expand_count = 0
        return []

    def _parse_m3u(self, path: str) -> list[str]:
        lines: list[str] = []
        for enc in ("utf-8-sig", "gbk", "latin-1"):
            try:
                with open(path, "r", encoding=enc) as fh:
                    lines = fh.read().splitlines()
                break
            except (UnicodeDecodeError, OSError):
                continue
        base = os.path.dirname(path)
        out = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            full = line if os.path.isabs(line) else os.path.join(base, line)
            if os.path.exists(full):
                out.append(full)
        return out

    def open_files(self, paths: list[str], append: bool = False, play_index: int | None = None,
                   remember: bool = True, start_pos: float | None = None) -> dict[str, Any]:
        """把这批文件交给内核，并**回一句"这一趟让内核干什么"**给界面。

        返回值不是装饰，是 2026-10-19 那笔事故的正面修法：界面那句"已载入 N 个文件"以前是
        在 `open_files()` 之后**无条件**印的，而 `open_files` 只是把命令塞进写队列——命令被
        内核整条拒掉时写队列一切正常，于是用户看到的是"提示说加载了，画面是黑的"。
        现在界面拿 `plays`/`head` 去等**内核自己发回来的那一步**（`file-loaded` 事件、
        或 playlist 变长的推送），等不到就照实说。
        """
        # 存在性在**我们这边**查、路径要在**内核那边**用，所以这一步必须收口成绝对：
        # 相对路径过得了 exists()（我们 cwd 下有），到了 mpv（cwd=安装目录）就是"文件不存在"，
        # 表现为双击/右键打开时一声不响地不放。断点续播与"最近文件"的键也跟着变成绝对路径——
        # 相对键本来就是坏的（换个启动目录就换了一把键，上次的进度找不回来）。
        paths = [mpv_path(p) for p in paths if os.path.exists(p)]
        if not paths:
            return {"count": 0, "plays": False, "head": ""}
        self._resume_skip = ""
        self.last_files = paths
        # 追加时"当前其实没在播"这一档必须单独管，两条都是本机实测出来的
        # （tests/probe_append_idle.py，2026-10-17，内核 v0.41.0-1101）：
        # • 空列表上 `loadfile x append` 只把条目**放进**列表——`playlist-pos` 仍是 −1、
        #   `idle-active` 仍 True。于是"播放列表点添加"会变成"文件进了列表却一动不动"，
        #   用户看到的就是"我加了文件，它不播"。
        # • 也不能图省事改用 `append-play`：实测它在**已经在播**时同样跳到新条目
        #   （pos 0→1、path 跟着换），那是"改播这一条"，不是"加到队尾"——正好把
        #   "边播边往后加"这条最常用的路搞坏。
        # 所以自己判"有没有当前条目"：没有就把第一条发成 replace（顺手播起来），
        # 其余仍走 append。`prop()` 读的是订阅缓存，不阻塞、不发请求，所以 GUI 线程安全。
        # 这一判完全押在缓存的那一份真值上：缓存空着＝读成 −1＝"没在播"＝整表替换。
        # 2026-10-17 真机四条红就是坏在这里——`_on_start_file` 曾把 playlist-pos 列进
        # _FILE_SCOPED 抹掉（当时以为"防陈旧"），于是"正播着点添加"读到的永远是 −1。
        # 现在 _FILE_SCOPED 里不再有它，别再放回去；空列表那一档才是真该走 replace 的，
        # 那时内核推过 −1（启动时 observe_property 也会先推一次当前值），不是"没到货"。
        live = self.prop("playlist-pos", -1)
        append_first = bool(append) and isinstance(live, int) and live >= 0
        cmds: list[list[Any]] = []
        for i, p in enumerate(paths):
            first = i == 0 and not append_first
            cmd: list[Any] = ["loadfile", p, "replace" if first else "append"]
            if first:
                # `start_pos` 是"这一趟必须从这个位置开始"（看门狗崩溃恢复传进来的），
                # 优先于按路径回查的续播；由 loadfile 的 start= 交给内核，避免在
                # file-loaded 之前盲目 seek（那条命令会被内核丢弃/报 property unavailable）。
                if start_pos is not None:
                    pos = max(0.0, float(start_pos))
                else:
                    pos = (self.cfg.recall_position(p) if self.cfg["resume"] else 0.0) or 0.0
                self.last_pos = pos
                if pos > 1:
                    # 续播位置带在命令里。**形状是真内核量出来的**，别照文档改回去：
                    # `tests/probe_loadfile_index.py`（判决留档
                    # `tests/out/probe-loadfile-index-v3-1019-205812.txt`，内核 v0.41.0-1101）
                    # 里 loadfile 的位置参数是 `<url> <flags> <index> <options>`，
                    # ① `index` 是整数位，必须显式给 −1（＝不动当前条目）；
                    # ② `options` 在这个构建里是**裸字符串**——内核 echo 出来的就是
                    #    `options="start=3.000"`，列表/字典形态都被当成 index 收下去拒掉。
                    # 旧写法 `cmd.append([f"start=…"])` 正好踩在②上：那一项落进 index 这一位，
                    # 内核回 `Command loadfile: argument index has incompatible type.`，
                    # 整条命令没执行、片一条都没进列表（`tests/out/probe-loadfile-index-kernel.log`
                    # 里就是这一行原话），
                    # 而屏幕上只有界面那句"已载入"——用户 2026-10-19 双击《美国队长3》就是这一条。
                    # 同一趟还把两条备选判死了：`set_property start` 预置＝error accessing
                    # property；载入完成之前发任何定位命令＝property unavailable／error running
                    # command（"载入之后再 seek"这一族要等 file-loaded，量具里 S1–S4 才成立）。
                    # 所以能一步到位的只有这一条形状。
                    cmd += [-1, f"start={pos:.3f}"]
                    # 记下"这条已经由内核自己带到位置上了"：file-loaded 回来时别再
                    # 用同一条进度 seek 一次（那会把精确起播打断成一次显式跳转）。
                    self._resume_skip = os.path.normcase(os.path.normpath(p))
            cmds.append(cmd)
        for c in cmds:
            self._fire(c)
        # `if play_index:` 会把 0 当成"没传"——第 0 条永远是假值。今天上面那条
        # replace 已经把首条变成当前条目，所以看不出来；一旦调用方是"往队尾追加后要跳回
        # 第 0 条"，这条命令就被静默吞掉。判 None 与负数，不判真值。
        if play_index is not None and play_index >= 0:
            self._fire(["set_property", "playlist-pos", play_index])
        if remember:
            self.cfg.push_recent(paths[0])
            self.cfg.save()
        # `plays`＝这一趟会不会让内核**换当前条目**（往正在播的队尾追加＝False，那条路上
        # 永远不会有 file-loaded 事件，界面只能去等 playlist 变长）；`head`＝第一条的绝对路径，
        # 界面拿它和内核推回来的 `path` 对得上对不上，来分"命令没被收下"和"收下了但解不开"。
        return {"count": len(cmds), "plays": not append_first, "head": paths[0]}

    def play(self) -> None:
        self.set_prop("pause", False)

    def pause(self) -> None:
        self.set_prop("pause", True)

    def toggle_pause(self) -> None:
        # 交给内核做原子翻转：读缓存再取反会在连点时丢状态
        self._fire(["cycle", "pause"])

    def stop(self) -> None:
        self._fire(["stop"])

    def seek_rel(self, secs: float) -> None:
        self._fire(["seek", secs, "relative"])

    def seek_abs(self, secs: float, exact: bool = False) -> None:
        """绝对定位。`exact=True` 精确到帧（松手定帧/快捷键），否则按关键帧落点。

        拖动途中用关键帧档：每次拖动都要求解码到精确位置会把 CPU 与 IPC 队列同时灌满；
        松手那一下才值得一次精确 seek——用户盯着的那一帧必须就是他放下的位置。
        """
        mode = "absolute+exact" if exact else "absolute+keyframes"
        self._fire(["seek", max(0.0, float(secs)), mode])

    def frame_step(self, backward: bool = False) -> None:
        self.set_prop("pause", True)
        self._fire(["frame-back-step" if backward else "frame-step"])

    def set_volume(self, value: int) -> None:
        """上限问内核（--volume-max=200 是启动时给的，但界面别拿它当事实）。"""
        v = max(0, min(self.volume_max(), int(value)))
        self.set_prop("volume", v)
        self.cfg["volume"] = v

    def adjust_volume(self, delta: int) -> None:
        self._fire(["add", "volume", delta])

    def toggle_mute(self) -> None:
        self._fire(["cycle", "mute"])

    def set_speed(self, value: float) -> None:
        self.set_prop("speed", round(float(value), 3))

    def cycle_speed(self, steps: Iterable[float], forward: bool = True) -> float | None:
        order = sorted(steps, reverse=not forward)
        cur = float(self.prop("speed", 1.0) or 1.0)
        nxt = order[0]
        for s in order:
            if (forward and s > cur + 1e-6) or ((not forward) and s < cur - 1e-6):
                nxt = s
                break
        self.set_speed(nxt)
        return nxt

    def set_loop(self, mode: str) -> None:
        """mode: no | inf | 数字（次数）"""
        self.set_prop("loop-playlist", mode)

    def set_loop_file(self, mode: str) -> None:
        self.set_prop("loop-file", mode)

    def set_shuffle(self, on: bool) -> None:
        self.set_prop("shuffle", bool(on))
        if on:
            self._fire(["playlist-shuffle"])

    def next(self) -> None:
        self._fire(["playlist-next"])

    def prev(self) -> None:
        self._fire(["playlist-prev"])

    def goto_index(self, idx: int) -> None:
        self._fire(["set_property", "playlist-pos", idx])

    def remove_index(self, idx: int) -> None:
        self._fire(["playlist-remove", idx])

    def move_index(self, src: int, slot: int) -> None:
        """把第 src 项挪到**原表**第 slot 个坑位（实测语义，见 playlist_panel 文件头）。"""
        self._fire(["playlist-move", src, slot])

    def clear_playlist(self) -> None:
        """清空 = 把当前这条一起停掉，回到"没有媒体"的状态。

        不能只发 playlist-clear：mpv 那条命令的官方语义是"Remove all playlist entries
        **but the current**"，实测清空后列表仍剩 1 条、画面继续放，用户会觉得按钮失灵。
        stop 才是"停 + 清单"（--idle=yes 下进程不退），随后 playlist 推来空列表，
        外壳的 _on_playlist 就自动切回占位页。
        """
        self._fire(["stop"])

    # ================= 章节 =================
    def chapters(self) -> list[dict]:
        """内核缓存里的章节表；没有章节/内核没启动都回空列表（不阻塞读线程）。"""
        rows = self.prop("chapter-list", []) or []
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    def _chapter_pos(self) -> int:
        """当前章节下标：缓存值 + 上一拍命令的期望值（只在缓存还没翻面时用）。

        `set_property` 是异步的：连按下一章时第二次读缓存仍会拿到旧下标。
        期望值只在"缓存还等于发送前的值、且没过 1.5 秒"时生效；内核一推新值就清掉，
        用户用别的方式换了章节也清掉——它不能变成一个独立于内核的第二事实源。
        """
        raw = self.prop("chapter", -1)
        if raw is None or raw == "":
            cur = -1
        else:
            try:
                cur = int(raw)
            except (TypeError, ValueError):
                cur = -1
        exp = self._chapter_expect
        if exp is not None:
            exp_idx, exp_from, exp_at = exp
            if time.monotonic() - exp_at <= 1.5 and cur == exp_from:
                return exp_idx
            self._chapter_expect = None
        return cur

    def chapter_pos(self) -> int:
        """当前章下标（可能带一次尚未被内核确认的期望值），菜单勾选用。"""
        return self._chapter_pos()

    def chapter_step(self, delta: int) -> dict:
        """上一章/下一章：算目标、发命令、把"该说哪句话"的原料一并交回。

        读的是缓存里的 `chapter`/`chapter-list`（`prop()` 从不发请求），目标由纯函数
        `chapter_target()` 算；本章标题/时间取目标那一行，供界面拼提示。边界不静默：
        `edge` 回 "first"/"last"，调用方据此说"已是第一章/最后一章"，而不是按了没反应。

        为什么**不**用 mpv 默认键位的 `add chapter 1`（实测 tests/out/chapter-cmd.log）：
        在最后一章 `add chapter 1` 会让内核一路走到文件尾、`chapter` 随后变成不可用；
        而 `set_property chapter <夹住后的下标>` 只回到本章首、不碰文件尾。连按时缓存还没
        翻面，用 `_chapter_expect` 记住上一拍的目标，第二下从期望值继续走。
        """
        rows = self.chapters()
        cur = self._chapter_pos()
        target, edge = chapter_target(len(rows), cur, int(delta))
        out = {"index": target, "total": len(rows), "title": "", "time": 0.0,
               "edge": edge, "moved": False}
        if target is None:
            return out
        if target != cur and target >= 0:
            self.set_prop("chapter", target)
            self._chapter_expect = (target, cur, time.monotonic())
            out["moved"] = True
        row = rows[target]
        out["title"] = str(row.get("title") or "")
        try:
            out["time"] = float(row.get("time") or 0.0)
        except (TypeError, ValueError):
            out["time"] = 0.0
        return out

    def set_chapter(self, idx: int) -> dict:
        """跳到第 idx 章（菜单用）。越界/没有章节时返回同样的空账，不抛。"""
        rows = self.chapters()
        out = {"index": None, "total": len(rows), "title": "", "time": 0.0,
               "edge": "", "moved": False}
        if not rows:
            return out
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return out
        if i < 0 or i >= len(rows):
            return out
        # 菜单跳转是绝对落点：先看内核缓存里"跳之前"在哪，再把这步记成新的期望。
        # 不能只是清掉旧期望——清掉后第二次快速按键会拿旧缓存算目标，刚跳到第 3 章
        # 再按"下一章"可能算出第 2 章。也不能沿用旧期望——上一拍按键的目标与这次
        # 菜单落点无关，会把按键序列带偏。基线用**原始缓存**而不是 chapter_pos()：
        # 期望值只有在缓存仍等于它自己记的基线时才成立。
        raw = self.prop("chapter", -1)
        try:
            base = -1 if raw is None or raw == "" else int(raw)
        except (TypeError, ValueError):
            base = -1
        self.set_prop("chapter", i)
        self._chapter_expect = (i, base, time.monotonic()) if i != base else None
        row = rows[i]
        out.update(index=i, title=str(row.get("title") or ""), moved=True)
        try:
            out["time"] = float(row.get("time") or 0.0)
        except (TypeError, ValueError):
            out["time"] = 0.0
        return out

    def set_track(self, kind: str, track_id: Any) -> None:
        prop = {"audio": "aid", "video": "vid", "sub": "sid"}.get(kind, "sid")
        self.set_prop(prop, "auto" if track_id in (None, "auto") else track_id)

    def set_secondary_sub(self, track_id: Any) -> None:
        self.set_prop("secondary-sid", "no" if track_id in (None, "no") else track_id)

    def secondary_sub_id(self) -> Any | None:
        """当前副字幕轨的 id；关闭状态回 None。

        不能直接 `bool(prop("secondary-sid"))`：关闭时内核回的是字符串 `"no"`，
        它是真值——旧写法把"已关闭"读成"开着"，于是双语字幕永远只能关、打不开。
        """
        v = self.prop("secondary-sid")
        if v is None or v is False or v == "":
            return None
        if isinstance(v, str) and v.strip().lower() in ("no", "false", "none"):
            return None
        return v

    def add_subtitle(self, path: str) -> None:
        self._fire(["sub-add", path, "auto"])

    def add_audio(self, path: str) -> None:
        self._fire(["audio-add", path])

    def set_ab(self, which: str) -> None:
        pos = float(self.prop("time-pos", 0.0) or 0.0)
        self.set_prop(f"ab-loop-{which}", pos)

    def clear_ab(self) -> None:
        self.set_prop("ab-loop-a", "no")
        self.set_prop("ab-loop-b", "no")

    def snapshot(self, include_sub: bool = True) -> None:
        """截当前帧，路径通过 sig_saved 上报。

        四点实测约束（都踩过）：
          - 第二个位置参数是 mpv 的 legacy 开关，传 bool/字符串都会被拒（invalid
            parameter），所以只传 flags；
          - 文件名列由启动参数 --screenshot-template 决定，set_property 改它不生效
            （产物仍叫 mpv-shot0001.png）；
          - 首帧之前截图必然失败（Falling back to software screenshot →
            Taking screenshot failed），外壳要在 sig_file_loaded 之后才放开按钮；
          - 产物路径在命令应答的 data.filename 里，内核**不发** file-saved 事件，
            所以走后台线程等应答（request_async），不拿它卡 UI 线程。
        """
        if self._ipc is None:
            return
        mode = "subtitles" if include_sub else "video"

        def done(data, err):
            if err:
                self.sig_log.emit("截图失败：" + err)
                return
            path = (data or {}).get("filename")
            if path:
                self.sig_saved.emit(os.path.abspath(path))
            else:
                self.sig_log.emit(f"截图应答未带文件名：{data}")

        self._ipc.request_async(["screenshot", mode], done, timeout=8.0)

    def audio_devices(self) -> list[dict]:
        return list(self.get("audio-device-list", []) or [])

    def set_audio_device(self, dev_id: str) -> str:
        """切输出设备，返回**内核回报**的设备名。

        为什么存内核说的而不是我要求的：设备被独占、或拔掉了，mpv 会自己回落到默认
        设备；此时配置里若留着那个点不通的 GUID，下次启动还是不通，用户看到的就是
        "设置没存住"。存真值还能让启动参数与这一轮的实际行为一致。
        """
        self.set_prop("audio-device", dev_id)
        actual = str(self.get("audio-device", "") or "")
        self.cfg["audio_device"] = actual or dev_id
        if actual and actual != dev_id:
            self.sig_log.emit(f"音频设备请求 {dev_id}，内核实际用了 {actual}")
        return actual or dev_id

    def set_option(self, name: str, value: Any) -> None:
        """改当前文件生效的选项（如解码相关），比 set_property options/ 更可靠。"""
        self._fire(["change-list", "file-local-options", name, str(value)])

    # ================= 可调项：字幕样式 / 画面均衡 / HDR / 声音 =================
    def set_tweak(self, prop: str, value: Any, persist: bool = True,
                  confirm: bool = True) -> tuple[Any, bool]:
        """改 engine/tweaks.py 表里的一项，返回 (内核实际生效值, 是否与请求一致)。

        confirm=True 会补一次 get_property：OSD 和面板报的是**内核说的数**，
        不是"我发出去的数"。少这一步就会有过这种账：菜单里写着 butterlight，
        这一版内核根本不认，点了没反应——界面却已经把它标成当前档了。
        滑块拖动途中传 False（每动一格一次同步往返会拖住 UI 线程），松手时再确认。

        表里没有的属性一律不发命令：宁可这里静什么都不做（返回 False 让调用方
        报出来），也不要往内核塞一个没人核对过的名字。
        """
        item = tweaks.spec(prop)
        if item is None:
            self.sig_log.emit(f"可调项不在表里：{prop}")
            return None, False
        want = tweaks.mpv_value(item, value)          # 发往内核的形式（bool 要写成 yes/no）
        # 存进配置与返回值的是 Python 形式：布尔就是布尔、数字就是夹紧的浮点。
        # 直接把 "no" 当假值存进配置会踩字符串的老坑——bool("no") 是 True。
        if isinstance(item, tweaks.Flag):
            py: Any = bool(value)
        elif isinstance(item, tweaks.Num):
            py = item.clamp(value)
        else:
            py = want
        self.set_prop(prop, want)
        if persist:
            self.cfg[item.cfg] = py
        if not confirm:
            return py, True
        actual = self.get(prop, None)
        ok = tweaks.same_value(item, py, actual)
        if ok:
            with self._props_lock:
                self._props[prop] = actual   # 缓存换成内核的真值
            # 自己把这一发广播出去。多数可调项内核也会推 property-change，但实测
            # 本机 v0.41.0-1101 对 **sub-border-size 不推**（改了值、get 得到新值、
            # 事件永远不来，播着片也一样）。订阅缓存是界面唯一的真值来源，少了这一发
            # 就会出现"外壳明明改成功了，界面要等下次开页才动"。
            # 只在确认通过后发：失败时值没变，发了反而把脏值画到界面上。
            self.sig_info.emit({prop: actual})
        else:
            self.sig_log.emit(f"{prop} 请求 {want!r} 但内核回报 {actual!r}")
        return actual, ok

    def nudge_tweak(self, prop: str, delta: float) -> tuple[Any, bool]:
        """在当前值上加减（快捷键用）。起点读内核订阅缓存而不是配置：
        配置是"上次退出时的值"，这一轮内核可能已经被别的入口改过。"""
        item = tweaks.spec(prop)
        if not isinstance(item, tweaks.Num):
            return None, False
        base = self._num(self.prop(prop, item.default))
        return self.set_tweak(prop, base + delta)

    def toggle_flag(self, prop: str) -> tuple[Any, bool]:
        item = tweaks.spec(prop)
        if not isinstance(item, tweaks.Flag):
            return None, False
        return self.set_tweak(prop, not bool(self.prop(prop, item.default)))

    def reset_video_eq(self) -> dict[str, Any]:
        """画面均衡五项全部回 0；回报每一项的内核真值（面板据此重画滑块）。"""
        out: dict[str, Any] = {}
        for n in tweaks.EQ:
            actual, _ok = self.set_tweak(n.prop, n.default)
            out[n.prop] = actual
        return out

    def cycle_choice(self, prop: str, forward: bool = True) -> tuple[Any, bool]:
        """枚举项循环一档（Shift+T 这类快捷键）。返回 (内核真值, 是否生效)。"""
        item = tweaks.spec(prop)
        if not isinstance(item, tweaks.Choice):
            self.sig_log.emit(f"{prop} 不是枚举项，无法循环")
            return None, False
        nxt = tweaks.cycle_option(item, self.prop(prop, item.default), forward)
        return self.set_tweak(prop, nxt)

    # ================= 画面变换与画质增强（engine/transform.py） =================
    def _log_offset(self) -> int:
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def _log_since(self, off: int) -> str:
        """从上次记录的偏移读到现在的新日志。只看这一段，才不会把上一轮的失败行
        算到这一轮头上（实测内核日志是追加写、不清空的）。"""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="ignore") as fh:
                fh.seek(off)
                return fh.read()
        except OSError:
            return ""

    def apply_filter_chain(self) -> tuple[str, list[str], list[str]]:
        """把表里当前的滤镜项拼成整条链，一次 `change-list vf set` 替换。

        为什么整条替换而不是 append/remove：本机实测（`tests/out/probe-transform12.log`）
        append 一次只收一档——带逗号的整条链被内核整条拒收（原话 "-append takes only 1
        item (no ',')"，链表纹丝不动），而单档 append 两次就是链上两条（['hflip','hflip']）；
        remove 一次也只删一条同名的，重复项删不干净；只有 `set ""` 才是清空（实测回报 [])。
        一次一发也顺带避免了"改一个滑轨、链上顺序被别人抢先改掉"的竞态。
        返回值 (链串, 内核列表里的名字, 内核日志里的禁用行)——**第三项才是证据**：
        前两项在滤镜被内核扔掉时照样好看（见 transform.filter_failures 的注释）。
        """
        chain = transform.build_chain(self.cfg)
        off = self._log_offset()
        self._req(["change-list", "vf", "set", chain], timeout=3.0)
        listed = self.get("vf", []) or []
        names = [str(x.get("name")) for x in listed if isinstance(x, dict)]
        fails = transform.filter_failures(self._log_since(off))
        if fails:
            self.sig_log.emit("滤镜被内核禁用：" + "；".join(fails[:3]))
        self._vf_names = names
        self._vf_failures = fails
        return chain, names, fails

    def set_transform(self, key: str, value: Any, persist: bool = True,
                      confirm: bool = True) -> tuple[Any, bool]:
        """改画面变换/增强的一项，返回 (生效值, 是否与请求一致)。

        几何项走 set_property 并回读内核真值（和 set_tweak 同一套理由：界面要报的是
        内核说的数）；滤镜项改的是**整条链**，所以取值写进配置后要重新拼一遍。
        confirm=False 是滑轨拖动途中：只发不等（每动一格一次同步往返会拖住 UI 线程），
        松手时再补一发 confirm=True 的，顺带把"内核其实把这个滤镜扔了"这件事报出来。
        """
        spec = transform.spec_for_cfg(key)
        if spec is None:
            self.sig_log.emit(f"画面变换项不在表里：{key}")
            return None, False
        cfg_key = spec.cfg
        py = transform.normalize(cfg_key, value)
        prop = transform.prop_of(cfg_key)
        if persist:
            self.cfg[cfg_key] = py
        if prop:
            want = transform.mpv_value(cfg_key, py)
            self.set_prop(prop, want)
            if not confirm:
                return py, True
            actual = self.get(prop, None)
            ok = transform.same_value(cfg_key, py, actual)
            if ok:
                with self._props_lock:
                    self._props[prop] = actual
                self.sig_info.emit({prop: actual})
            else:
                self.sig_log.emit(f"{prop} 请求 {want!r} 但内核回报 {actual!r}")
            return (actual if ok else py), ok
        f = transform.filter_for_cfg(cfg_key)
        if f is None:
            self.sig_log.emit(f"滤镜项查不到：{cfg_key}")
            return py, False
        expect = bool(f.expr(py))
        if not confirm:
            self._fire(["change-list", "vf", "set", transform.build_chain(self.cfg)])
            return py, True
        _chain, names, fails = self.apply_filter_chain()
        ok = (not fails) and expect == (f.key in names)
        if not ok:
            self.sig_log.emit(f"{f.label} 没确认生效（链上={names or '空'}"
                              + (f"；{fails[0]}" if fails else "；内核没列出这一档")
                              + "）")
        return py, ok

    def cycle_transform(self, key: str, forward: bool = True,
                        confirm: bool = True) -> tuple[Any, bool]:
        """快捷键用的"这一项走一格"。当前值读内核订阅缓存而不是配置：
        配置是上次退出时的愿望，内核可能已经不这么想了。
        `confirm=False` 给"内核还没起来"的那条路用：那时 set_property 无人应答，
        确认只会换来一行"请求 X 但内核回报 None"的噪声，而值已经写进配置、
        启动参数会把它下发（transform.startup_args）。"""
        spec = transform.spec_for_cfg(key)
        if spec is None:
            self.sig_log.emit(f"画面变换项不在表里：{key}")
            return None, False
        cur = self.cfg.get(spec.cfg, spec.default)
        prop = transform.prop_of(spec.cfg)
        if prop:
            cur = self.prop(prop, cur)
        return self.set_transform(spec.cfg, transform.cycle(spec.cfg, cur, forward),
                                  confirm=confirm)

    def reset_transforms(self) -> dict[str, Any]:
        """画面变换整族复位：几何逐项下发、滤镜链一次清空，回报每一项的内核真值。"""
        for k, v in transform.reset_values().items():
            self.cfg[k] = v
        out: dict[str, Any] = {}
        for s in transform.PROPS:
            actual, _ok = self.set_transform(s.prop, self.cfg.get(s.cfg, s.default))
            out[s.prop] = actual
        _chain, names, fails = self.apply_filter_chain()
        out["vf"] = names
        out["vf_failed"] = fails
        return out

    def vf_names(self) -> list[str]:
        """滤镜链上内核**列出**的名字（读缓存，不发请求，可以在 UI 线程上随便调）。

        这一句必须连着下面这句一起看：它**不是**"这些滤镜在跑"的证据。实测硬解帧上
        被内核扔掉的滤镜照样出现在列表里（enabled=True、像素零变化），真相只在内核日志
        那几行（`transform.filter_failures`）与像素里，而日志已经在
        `apply_filter_chain` 每次下发时一并读过了。界面拿它画"链上有什么"，
        不能拿它判"生效了"。
        """
        return list(self._vf_names)

    def filter_failures(self) -> list[str]:
        """上一条链下发时内核日志里的"禁用/创建失败"原话（空＝那一次没听到坏消息）。

        为什么必须有这个出口：`vf_names()` 那份清单会骗人（上面那条实测），而画面页要
        在用户做完动作的那一刻说出真正的原因。两句一起给，才不会出现"界面说已开启、
        内核其实把它扔了"。注意它是**上一次**的结果：没下发过链就是空表，
        空表不等于"链上都在跑"，只等于"那一次没读到禁用行"。
        """
        return list(self._vf_failures)

    def transform_state(self) -> dict[str, Any]:
        """面板开局画一次用：几何项给内核真值（订阅缓存优先，没推到就问一次），
        滤镜链给内核列出的名字表。注意后者**不是**"这些滤镜在跑"的证据。"""
        out: dict[str, Any] = {}
        for s in transform.PROPS:
            raw = self.prop(s.prop, None)
            if raw is None:
                raw = self.get(s.prop, None)
            out[s.cfg] = raw
        out["vf"] = list(self._vf_names)
        return out

    # ================= 声音均衡器（engine/equalizer.py，af 链） =================
    def apply_audio_eq_chain(self) -> tuple[str, dict[str, float], list[str], list[str]]:
        """把表里当前的十段＋前置拼成整条 `af`，一次 `change-list af set` 替换。

        为什么整条替换而不是 append/remove（本机实测 `tests/out/eq-probe-r15b.log` 的 ⑥ 那一腿）：
        `append` 的值带逗号**整条拒收**（`error running command`，链表纹丝不动），
        `remove/toggle` 要精确匹配整串条目，"改一根滑轨"就变成"删一条＋补一条"，
        而补那条正好是带逗号的串；`set` 是唯一既能整条换、又能在归零时干净清空
        （`set ""` → 内核回报 `[]`）的形状。注意归零发的是**空串**而不是 `lavfi=[]`：
        后者内核真去建空图滤镜并失败（`Audio filter initialized failed!`），整个进程一声不出。

        返回值 (链串, 内核读回反解的逐档 dB, 读不懂之处, 内核日志里的失败行)。
        后两项才是证据：`get af` 在链路整条起不来时照样能回一个列表，而真相写在那一小段日志里
        （实测越界增益／不存在的滤镜名／空图三档的产物字节数是 −1，即一个样本都没落盘）。
        """
        chain = equalizer.build_chain(self.cfg)
        off = self._log_offset()
        self._req(["change-list", "af", "set", chain], timeout=3.0)
        return self.read_audio_eq(off)

    def read_audio_eq(self, log_off: int | None = None) -> tuple[str, dict[str, float],
                                                             list[str], list[str]]:
        """读一次内核的 `af` 并反解成逐档 dB（不发命令；下发后与开页面时都走这里）。

        `log_off` 给的是"发命令之前的日志偏移"：给了就把那一段新日志一起判掉，
        没给就是纯读（开页面那一次）——**没有日志窗口就没有新证据**，所以失败行沿用
        上一次下发的判决，不许把"这一次没读到坏消息"读成"链成立了"（实测整条起不来时
        `get af` 照样回一条好看的列表）。想清掉那句坏消息只有一条路：再下发一次并带上
        日志窗口，让内核在那一小段里重新表态。

        内核没在跑时**两句原话都不往日志里发**：那时 `get af` 必然回"读不到 af"，
        而这两句的句式是"链坏了"。没人应答的问句不构成对链的判断，发出去就是把一次
        正常的空档（没播放）报成一次事故，还会把 OSD 刷满（`set_audio_eq` 的早退同一条理由）。
        """
        chain = equalizer.build_chain(self.cfg)
        parsed, problems = equalizer.parse_entries(self.get("af", None))
        vals = parsed if parsed is not None else {}
        if log_off is None:
            fails = list(self._eq_failures)
            if problems and self.running:
                self.sig_log.emit("均衡器读回有读不懂的条目：" + "；".join(problems[:3]))
        else:
            fails = equalizer.failures(self._log_since(log_off))
            if self.running:
                if fails:
                    self.sig_log.emit("均衡器链路没成立：" + "；".join(fails[:3]))
                elif problems:
                    self.sig_log.emit("均衡器读回有读不懂的条目：" + "；".join(problems[:3]))
        self._eq_vals = vals
        self._eq_problems = list(problems)
        self._eq_failures = list(fails)
        return chain, vals, problems, fails

    def set_audio_eq(self, key: str, value: Any, persist: bool = True,
                     confirm: bool = True) -> tuple[float, bool]:
        """改均衡器一档，返回 (内核读回的那一档 dB, 是否与请求一致)。

        拖动途中 `confirm=False`：那时只发不等，免得每动一格一次同步往返把 UI 线程拖住；
        松手时补一发 `confirm=True`，把"内核其实没挂上这一档"和"挂的是别的串"都当场说清楚。
        表里没这一档一律不发命令——这一族的坏值不是"这一档没生效"，是"整个播放器没声音"。
        """
        spec = equalizer.spec_for(key)
        if spec is None:
            self.sig_log.emit(f"均衡器档位不在表里：{key}")
            return 0.0, False
        want = equalizer.normalize(key, value)
        if persist:
            self.cfg[key] = want
        if not confirm:
            self._fire(["change-list", "af", "set", equalizer.build_chain(self.cfg)])
            return want, True
        if not self.running:
            # 内核没起来：这条路上没有任何东西可确认，所以也**不去问 `af`**。
            # 问了只得到一个没人应答的"读不到 af"——那是问句的形状，不是链坏了的形状，
            # 把它当问题报出来就是外壳自造一句假警报（同 `read_audio_eq` 里那句 sig_log 闸门）。
            # 值已经进配置，播放时由 `equalizer.startup_args` 一次带上，因此这里既不能说
            # "已生效"（没内核点头）也不能说"没生效"（没有一次失败的读回）：返回 (want, False)，
            # 措辞由界面按 `running` 那一档给（"已记入设置，播放后生效"）。
            return want, False
        _chain, vals, problems, fails = self.apply_audio_eq_chain()
        got = float(vals.get(key, 0.0) or 0.0)
        ok = (not fails) and (not problems) and abs(got - want) < 1e-4
        if not ok:
            why = fails[0] if fails else (problems[0] if problems
                                          else f"内核那一档是 {got:+g}，请求的是 {want:+g}")
            self.sig_log.emit(f"{equalizer.label(key)} 没确认生效：{why}")
        return (got if not fails else want), ok

    def apply_audio_eq_preset(self, key: str) -> tuple[dict[str, float], list[str], list[str]]:
        """预设整档下发（一次替换整条链），回报内核读回的逐档值。

        为什么预设要整个一起发而不是逐档 set_audio_eq：十档就是十次往返＋十次重放，
        而且中间态会被用户听见（"从第 3 档开始突然响一下"）。
        """
        vals = equalizer.preset_values(key)
        if vals is None:
            self.sig_log.emit(f"均衡器预设不在表里：{key}")
            return {}, [], ["预设不在表里"]
        for k, v in vals.items():
            self.cfg[k] = v
        _chain, got, problems, fails = self.apply_audio_eq_chain()
        return got, problems, fails

    def reset_audio_eq(self) -> tuple[dict[str, float], list[str], list[str]]:
        """一键复位：整档归零并让内核把链清空（实测 `set ""` → 读回 `[]`）。"""
        return self.apply_audio_eq_preset("flat")

    def audio_eq_state(self) -> dict[str, Any]:
        """面板开局画一次用：逐档给**上一次内核读回**的值（没读回过就是空表→面板按 0 画）。

        这里刻意不回落到配置值：配置是"上次退出时的愿望"，而这一族的下发形状是整条链，
        愿望和实际不一致的时候（例如越界值被表夹过、或内核起不来）界面上必须看得见。
        """
        out: dict[str, Any] = dict(self._eq_vals)
        out["problems"] = list(self._eq_problems)
        out["failed"] = list(self._eq_failures)
        return out

    def volume_max(self) -> int:
        """音量上限向内核问。启动参数按设置下发（默认 200），但界面不该拿它当事实：
        万一内核自己夹得更低，滑块就该停在内核真能到的地方。
        内核还没推来时退回设置值——而不是退回硬编码 200（用户改成 300 后
        第一秒滑块只能到 200，那又是一次"界面动了、内核没动"）。"""
        fallback = int(self.cfg.get("volume_max", perf.DEFAULTS["volume_max"]) or 0)
        return int(max(0, min(1000, self._num(self.prop("volume-max", fallback) or fallback))))

    def ab_marks(self) -> tuple[float | None, float | None]:
        """A、B 两个点各自解出来（未设的那一端回 None）。

        和 `ab_range` 分开是给时间轴用的：用户按 Shift+A 之后、还没按 Shift+B 的这段时间里，
        `ab_range` 必须是 None（"没有完整区间"），但时间轴上应该已经能看见 A 点落在哪。
        内核回报的"未设"是字符串 "no"，不是 None——直接 float() 会炸在 Qt 的槽里。
        """
        def one(name: str) -> float | None:
            v = self.prop(name)
            if isinstance(v, bool) or v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v) if float(v) >= 0 else None
            s = str(v).strip().lower()
            if s in ("", "no", "none"):
                return None
            try:
                f = float(s)
            except (TypeError, ValueError):
                return None
            return f if f >= 0 else None

        return one("ab-loop-a"), one("ab-loop-b")

    def ab_range(self) -> tuple[float, float] | None:
        """当前 A-B 区间；没设点（内核回报字符串 "no" 或负数）时返回 None。"""
        a, b = self.ab_marks()
        if a is None or b is None:
            return None
        return (a, b) if b > a else None

    def ipc_stats(self) -> dict:
        return dict(self._ipc.stats) if self._ipc else {}

    # ================= 内部事件 =================
    # 换文件时必须清掉的缓存项，否则新文件的 UI 会读到上一个文件的值。
    # 判"该不该清"的标准不是"它跟着文件变吗"，是**两件事**：值会不会变、内核会不会再推一发。
    # 2026-10-17 用真内核逐格量过（tests/out/probe-globals-across-file.txt）：换文件那一下
    # 内核只推 playlist-pos（因为它真的从 0 变成 1），而 `loop-file`／`ab-loop-a`／`ab-loop-b`
    # 三格**真值从上一支带过来、且不推第二发**（③ 换文件后仍是 inf/1.0/2.0；④ 只推了 pos 那一格）。
    # 所以这四格都不许列在这里——抹掉它们不是"防陈旧"，是"让引擎忘记内核此刻是什么状态"，
    # 而且抹完没人补。实发的两个后果：
    #  • playlist-pos：`open_files(append=True)` 拿 `prop("playlist-pos", -1)` 判"当前有没有条目"，
    #    读成 −1 就退化成"第一条发 loadfile … replace"＝整表替换——用户实测的"点添加反而把正在播
    #    那条挤没了、列表从 3 条变成 1 条"就是这一条（留档 probe-playlist-pos-wipe.txt ④⑤⑥）。
    #  • ab-loop-a/b：`ab_range()` 读空缓存回 None，于是换一支片子之后调节页写「A-B：未设置」
    #    （`_paint_ab` 在建页与设点两处都调），而内核其实还在按上一支那两个时间点循环。
    #  • loop-file：今天只被 `controls_bar` 那一处 info 驱动读，抹掉之后 info 里没这格 ⇒ 不重画 ⇒
    #    暂时看不出来；但 `_loop_from_kernel()` 的口径是"以内核回报为准"，缓存空着不等于内核=不循环，
    #    留着它就是给下一个读者埋雷。
    #  • current-vo：输出驱动是**进程级**的，换文件不改它。本机两条腿都量到
    #    'gpu-next'→'gpu-next' 且不推第二发（留档 tests/out/probe-file-scoped-table-v4-092438.txt
    #    的逐格表），而统计页「输出驱动」那行（`app/ui/stats_panel.py`）与媒体信息那句
    #    "输出 gpu-next"（`app/ui/main_window.py` 的 `_media_info`）都读这一格——抹掉的后果就是
    #    换一支片子之后那一行变空。形状与 ab-loop 那一笔相同，只是更轻。
    # 上面这几格之外，**整张表其余 20 格也在同一趟里逐格量过**（两条腿：无声腿 720p→4K、
    # 有声腿两支带章节带双字幕的夹具互换；第二支必须也带章节，否则 `chapter` 换过去就
    # property unavailable，那一格只能记"判不了"）：19 格有实测依据留在表里（值会变，或内核会补一发），
    # 1 格 `container-format` 是"内核查无此格"（property not found，与 `stats_panel.py` 顶部那句
    # 实测记录一致）——它抹不抹都是空操作，留着只是历史条目，不写进"无害"。
    # 判据：tests/test_append_semantics.py 的 E18（playlist-pos）与 E19（loop-file/ab-loop/current-vo）＋
    # tests/test_ui.py 的 6.6–6.9（真窗口真内核）；普查脚本 tests/probe_file_scoped_table.py（一次性，不进链）。
    _FILE_SCOPED = ("path", "media-title", "duration", "time-pos", "chapter", "chapter-list",
                    "track-list", "video-codec", "audio-codec", "container-format", "width",
                    "height", "hwdec-current", "aid", "vid", "sid",
                    "secondary-sid", "eof-reached", "container-fps", "video-out-params")

    def _on_start_file(self, msg: dict) -> None:
        self._chapter_expect = None          # 换片子：上一支的连按期望不属于这一支
        with self._props_lock:
            for k in self._FILE_SCOPED:
                self._props.pop(k, None)

    def _on_file_loaded(self) -> None:
        """运行在 IPC 读线程：只准读订阅缓存，绝不能发同步请求（ipc.request 已加守卫）。

        duration 等属性是在 file-loaded 之后才推来的，所以这里不代替属性回调发值，
        只报"文件已载入"和一份当前缓存快照，缺的字段由后续 property-change 补齐。
        """
        self.sig_file_loaded.emit()
        # 续播要覆盖**每一条**，不只是第一支：`start=` 只能挂在 loadfile 命令上，而
        # 后面的条目是内核自己切过去的（播放列表双击、播完自动下一支），那条命令由内核
        # 生成，我们插不进参数。于是换片完成后按路径再问一次观看进度，有就异步 seek。
        # `seek_abs` 走 `_fire`（写队列），在读线程里调用是安全的；`recall_position`
        # 只碰本地配置缓存/文件，不发 IPC。
        with self._props_lock:
            path = str(self._props.get("path") or "")
            norm = os.path.normcase(os.path.normpath(path)) if path else ""
            # 只认"这条路径确实是我们用 start= 带过来的"：别的文件先到的旧 file-loaded
            # 不许把这条标记吃掉，否则真正的 file-loaded 会再 seek 一次、打断精确起播。
            matched = bool(norm) and norm == self._resume_skip
            if matched:
                self._resume_skip = ""
            info = {k: self._props.get(k) for k in self._FILE_SCOPED if k in self._props}
        if path:
            if matched:
                pass                          # 这一条已经由 start= 带过去了
            elif self.cfg["resume"]:
                pos = self.cfg.recall_position(path)
                if pos > 1:
                    self.seek_abs(pos)
                    self.last_pos = pos
        if info:
            self.sig_info.emit(info)

    def _on_end_file(self, msg: dict) -> None:
        reason = str((msg or {}).get("reason", ""))
        if reason in ("eof", "error"):
            self.sig_eof.emit(True)
        if reason == "error":
            self.sig_log.emit("打开/解码失败：" + str((msg or {}).get("file", "")))

    def remember_now(self) -> None:
        path = str(self.prop("path", "") or "")
        dur = float(self.prop("duration", 0.0) or 0.0)
        pos = float(self.prop("time-pos", 0.0) or 0.0)
        if path and dur > 0:
            self.cfg.remember_position(path, pos, dur)
            self.cfg.save()

    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None and self._started)

    @property
    def mpv_pid(self) -> int | None:
        """内核进程号，只在它真活着的时候给。

        给资源实测那一层用（app/engine/usage.py 按 pid 取 CPU/内存）。写成属性而不是
        暴露 `_proc`：调用方拿到 proc 就会想 terminate，而杀掉内核这条路上只有一个出口
        （`stop()`/看门狗），多一个入口就多一个把片子打断的地方。
        """
        proc = self._proc
        if proc is None:
            return None
        return proc.pid if proc.poll() is None else None
