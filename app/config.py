"""设置持久化：读写 JSON，含默认值、损坏自愈、键迁移。不认识 UI。"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from .engine import equalizer, perf, tweaks, transform   # 四张事实表：可调项 / 启动档位 / 画面变换 / 声音均衡

APP_TITLE = "LuHui播放器"        # 用户看见的名字：窗口标题、桌面快捷方式、对话框标题
APP_NAME = "LuHuiPlayer"         # Qt 的 applicationName/organizationName（ASCII，进 QSettings 路径）
BRAND_ID = "luhui"               # ASCII 令牌：管道名 / 截图文件名 / mpv 输入配置 / 测试标记的前缀。
                                 # 目录名仍叫 lumen-player（改它会扯断已建好的快捷方式与工作路径），
                                 # 所以品牌串一律从这三个常量取，别再往代码里撒字面量。
                                 # 2026-11-11 由「格斗芦荟播放器 / GeluPlayer / gelu」改名而来；
                                 # 旧环境变量 GELU_DATA 仍作为别名兼容（见 DATA_DIR），
                                 # 测试夹具与旧数据文件里的 gelu-* 名字保持原样，属于历史命名。

# 目录布局：项目内 runtime/ 存放内核二进制，data/ 存放用户数据（可用环境变量覆盖）
#
# 打包（PyInstaller onedir）之后有**两个根**，不能混：
#   · ROOT      = exe 所在目录。data/ 与 截图/ 落在这里——用户看得见、拷走文件夹也带得走；
#   · BUNDLE_DIR= 只读资源根（sys._MEIPASS，onedir 下就是 _internal/）。runtime/ 内核、
#     packaging/assets/ 字体与图标都在这里。
# 都写成"exe 旁边"会把内核露在用户面前、且升级时覆盖不掉；都写成 _MEIPASS 则用户数据
# 会藏进 _internal，删掉重装就丢。源码运行时两者同一个目录，行为与从前完全一致。
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = os.path.abspath(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BUNDLE_DIR = ROOT
RUNTIME_DIR = os.path.join(BUNDLE_DIR, "runtime")
ASSETS_DIR = os.path.join(BUNDLE_DIR, "packaging", "assets")
# 为什么这里要 abspath（而不是"就用用户给的那串"）：DATA_DIR 之下所有的东西都是**当成参数
# 交给 mpv 子进程**的——`--log-file`、`--input-conf`、`--screenshot-directory`。而 mpv 是用
# `cwd=runtime/mpv` 启的（见 engine/player.py:start），相对路径在它那边会重新落到 mpv 安装目录：
# 本机实测 `GELU_DATA=tests/.selftest-data`（相对）时，`screenshot` 三种模式**全部**回
# "error running command"、shots 一个不长，而 mpv 自己那份解释失败原因的日志也被写到
# `runtime/mpv/tests/…`（空目录），于是现场连一句原话都没留下。用户把数据目录指到相对路径
# （或以后 settings 里 screenshot_dir 被手改成相对）就是同一场病：收口在这里，一处修全。
# 变量名首选 LUHUI_DATA；GELU_DATA 是改名前的旧名，继续认，免得老脚本/老文档一夜失效。
DATA_DIR = os.path.abspath(
    os.environ.get("LUHUI_DATA") or os.environ.get("GELU_DATA") or os.path.join(ROOT, "data"))
CACHE_DIR = os.path.join(DATA_DIR, "cache")
LOG_DIR = os.path.join(DATA_DIR, "logs")
SHOT_DIR = os.path.join(ROOT, "截图")

MPV_EXE = os.path.join(RUNTIME_DIR, "mpv", "mpv.exe")
FFMPEG_EXE = os.path.join(RUNTIME_DIR, "ffmpeg", "ffmpeg.exe")

DEFAULTS: dict[str, Any] = {
    # 播放
    "volume": 100,
    "mute": False,
    "speed": 1.0,
    "loop_playlist": "no",          # no | inf | 数字
    "shuffle": False,
    "resume": True,                 # 断点续播
    # 界面
    "theme_accent": "#8BC9A3",
    "sidebar_width": 300,
    # 窗口几何：上次关窗时的普通态位置/尺寸（x/y/w/h）与是否最大化。
    # 存五个**逻辑像素**整数，不存 Qt 的 saveGeometry() 字节串——多屏收口规则在
    # ui/win_geom.py 里是纯函数，能离线逐条喂"显示器被拔掉/分辨率降档"的坏样本。
    # 空 dict 是"没有存档"（第一次运行、旧配置），不是"宽高为 0"。
    "win_geom": {},
    # 全屏时控制条浮在画面上（顶层浮层），False 退回"占一行布局"的旧行为。
    # 唯一读者是 `ui/main_window.py:_float_bar`——这个名字**曾经**是个没人读的摆设（见下面
    # 那段"四个没人读的键"），现在功能落地了，键才跟着回来。留着 False 那一档不是怀旧：
    # 浮层是另一个顶层窗口，"从无焦点的顶层窗弹 QMenu"这类事只在真机上量得准，
    # 万一某台机器上不弹，用户改一个数就能退回，不用等版本。
    "float_controls_in_fs": True,
    # 窗口置顶：改 Z 序而不是 Qt 的 WindowStaysOnTopHint（后者会重建 HWND，
    # 挂在下面的 mpv 子窗口会失去父窗口）。启动时由 showEvent 兑现，之后菜单/快捷键切换。
    "always_on_top": False,
    # 截图
    "screenshot_dir": SHOT_DIR,
    "screenshot_include_sub": True,
    # 截图工坊（ui/gif_dialog.py 里那六个控件各自对应一个键，改完即写盘；
    # gif_include_sub 会在打开对话框时把勾选状态读回来，不是存了没人看的摆设）
    "gif_width": 480,
    "gif_fps": 12,
    "gif_duration": 4.0,
    "gif_include_sub": False,
    "burst_count": 8,
    "burst_gap_ms": 250,
    # 其它
    "recent": [],
    "library_roots": [],
    "audio_device": "auto",
    # 快捷键：**存 diff 不存整张表**（为什么存 diff，见 ui/keymap.py 顶部那段）。
    # 空 diff = 全用默认表。这里必须给默认值，否则老配置里没有这个键时
    # `cfg["keymap"]` 回 None，effective(None) 虽然能用，但保存时会被原样写回去，
    # 下次读进来就成了 JSON 的 null——给个明确形状比到处兜 None 省事。
    "keymap": {"off": [], "on": []},
    # 字幕样式 / 画面均衡 / HDR 的默认值**不在这里写**：它们和取值范围是同一份事实，
    # 归 engine/tweaks.py 的表，由下面的 DEFAULTS.update 合进来。
    # 硬解档位、GPU API、缩放预设、音量上限同理，归 engine/perf.py（那一族是启动期参数，
    # 非法值会被内核静默降级成软解，所以校验也必须和默认值同处一张表）。
    # 画面比例/旋转/充满/平移 + 锐化/降噪/去带/裁剪/镜像同理，归 engine/transform.py
    # （滤镜项没有独立属性，全部拼成一条 vf 链，所以配置键必须和拼链的表同源）。
    #
    # 这里**曾经**有四个没人读的键：acrylic / hide_cursor_when_idle /
    # float_controls_in_fs / max_recent。"设了没用"比"没这个功能"更坏——用户改了值、
    # 重启、什么也没变，只能怀疑自己操作错了。所以规则是：功能落地的那一次提交才带它自己的
    # 键进来（max_recent 早就被同文件上面的 _RECENT_KEEP 取代，属于纯重复）。
    # 上面那个 `float_controls_in_fs` 就是照这条规矩回来的：R21 浮层落地那天它才有了读者。
    # load() 只认 DEFAULTS 里的键，老配置里剩的那四个会在下一次 save 时自然消失。
}
DEFAULTS.update(tweaks.defaults())
DEFAULTS.update(perf.DEFAULTS)
DEFAULTS.update(transform.defaults())
# 声音均衡器（十段 + 前置）同理，归 engine/equalizer.py：配置键、夹夹紧的取值范围、
# 拼成 af 链的形状必须同源，否则"界面能拖到 ±12、内核收到 999"这种错就没有第二个地方能发现。
DEFAULTS.update(equalizer.defaults())

# 旧配置里可能留着 6 位颜色（那时还没做字幕颜色），mpv 要的是 #AARRGGBB。
# 读进来时就归一，省得每个使用点各自记得补一遍。
_COLOR_KEYS = ("sub_color", "sub_back_color")

# 主题色在 2026-10 换过一版：旧六档是亮蓝/亮紫/荧光青绿这一族，铺在主按钮和进度条上
# 正是"廉价"的来源。这里把老值原地迁到同色相的新档（蓝→雾蓝、紫→鸢尾……），
# 否则设置页会把它当成"当前 #4C8DFF"的自定义色列出来，一保存就被 is_accent 拦下。
# 只列旧默认表里那六个，用户自填的其它合法颜色原样留着。
_ACCENT_MIGRATION = {
    "#4C8DFF": "#8FB3D9",
    "#7C5CFF": "#A79BD9",
    "#12B886": "#8BC9A3",
    "#F76707": "#D2B27C",
    "#ED6A9C": "#C99AAE",
    "#A6ABB8": "#B9BDB4",
}

_RECENT_KEEP = 24


class Config:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(DATA_DIR, "config.json")
        self.data: dict[str, Any] = dict(DEFAULTS)
        # 观看进度的读盘缓存：`_ws` 永远是"上一次落盘成功的那份"，`_ws_stamp` 是它对应的
        # 文件记号（mtime_ns + size）。记号对得上就直接用，对不上才重读——见 _load_watch_state。
        self._ws: dict[str, Any] | None = None
        self._ws_stamp: tuple[int, int] | None = None
        # 计量用：真正 open+json.load 的次数。判据要的是"重扫 3000 张卡片期间这个数不涨"，
        # 不是"快了几毫秒"——耗时不进契约（同机并发、杀软、磁盘冷热都能把时间尺吹歪，
        # 读盘次数却只会因代码而变）。
        self.ws_reads = 0
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in DEFAULTS:
                        self.data[k] = v
            for k in _COLOR_KEYS:      # 迁移：老配置里的 "#FFFFFF" 补成 "#FFFFFFFF"
                self.data[k] = tweaks.norm_color(self.data.get(k))
            accent = self.data.get("theme_accent")
            if isinstance(accent, str):
                self.data["theme_accent"] = _ACCENT_MIGRATION.get(accent.upper(), accent)
        except FileNotFoundError:
            pass
        except Exception as exc:  # 损坏不阻塞启动
            self._backup_bad_file(exc)

    def _backup_bad_file(self, exc: Exception) -> None:
        try:
            bad = self.path + ".bad-" + time.strftime("%Y%m%d-%H%M%S")
            os.replace(self.path, bad)
            print(f"[config] 配置文件损坏已备份: {bad} ({exc})", file=sys.stderr)
        except Exception:
            pass

    def save(self) -> bool:
        """整份落盘。失败只记一行、返回 False——**绝不把异常扔进 Qt 回调**。

        关窗那一路会经过 `closeEvent`：只读目录/磁盘满时 `open()` 抛 OSError，从 Qt 槽里
        穿出去是 PyQt5 的 abort（窗口无声消失，栈也没有），比"设置没存上"严重得多。
        形如 `os.replace` 失败（杀软占着目标文件）也一并收在这里。
        """
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            print(f"[config] 保存失败 {self.path}：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    # ---------- 访问 ----------
    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    @staticmethod
    def recent_key(p: str) -> str:
        r"""「最近播放」的**去重键**：同一部片不管用哪种写法打开，只占一格。

        为什么不是原样比字符串：Windows 上 `/`、`\` 与大小写指的是同一个文件，而
        原始串会一路带到界面里。本机在用户真配置里实测到后果——命令行形状探针喂进去的
        `F:/…Atmos.mkv` 与用户自己双击过的 `F:\\…Atmos.mkv` 并排留在 recent 里，
        同一部电影显示成两条、还白占 `_RECENT_KEEP` 一格。
        注意这里**只**动列表去重，不动 watch-state 的键：那本账是按打开时那个原始路径存的，
        改键形状等于把用户已经存下的进度读不到（丢真数据比"并排两条"严重得多）。
        读不出规范形（奇奇怪怪的 URI/空串）就退回原样——去重失败只是多显示一条，
        比把路径改错、点开打不开要好。
        """
        try:
            return os.path.normcase(os.path.normpath(p)) if p else ""
        except BaseException:                                  # noqa: BLE001
            return p

    def push_recent(self, path: str) -> None:
        key = self.recent_key(path)
        recents = [p for p in self.data.get("recent", []) if self.recent_key(p) != key]
        recents.insert(0, path)
        self.data["recent"] = recents[:_RECENT_KEEP]

    # 观看进度（按文件路径记忆）
    @property
    def watch_state_path(self) -> str:
        return os.path.join(DATA_DIR, "watch-state.json")

    def remember_position(self, path: str, pos: float, duration: float) -> None:
        if duration <= 0 or pos <= 1:
            return
        # 片尾 15 秒或看完 92% 以上视为看完，下次从头播
        done = pos > duration - 15 or pos / duration > 0.92
        table = dict(self._load_watch_state())      # 拷一份再改：缓存本体只等于"盘上那份"
        table[path] = {"pos": 0.0 if done else pos, "dur": duration, "ts": time.time(), "done": done}
        self._save_watch_state(table)

    def watch_state(self, path: str) -> dict:
        """某个文件的观看记录（无记录返回 {}）。界面只读，不自己解析 watch-state。

        返回值是缓存里那份**活对象**（不是每问一次拷一份）：这一句会被媒体库按卡片逐条调用，
        拷一份就是 N 次 N 项字典复制。所以调用方只许读、不许改——要改走 remember_position。
        """
        entry = self._load_watch_state().get(path)
        return entry if isinstance(entry, dict) else {}

    def watch_snapshot(self) -> dict:
        """整份观看记录的只读快照，供"一次重建 N 张卡片"那种批量读取。

        为什么不是继续逐卡调 `watch_state`：那句本身不读盘，但每次都要 stat 一次
        watch-state.json 来确认"外部有没有改过"（`_load_watch_state` 的记号）。240 张卡就是
        240 次 stat，3000 张卡在机械盘/杀软在场时会攒成一笔可感的停顿——而**同一趟重建
        里这份表不可能变**（同一线程、重建期间没有写入口）。所以一次 stat 换整张表。
        返回值仍是缓存里那份活对象：调用方只许读。
        """
        return self._load_watch_state()

    def recall_position(self, path: str) -> float:
        entry = self._load_watch_state().get(path)
        # 手改过的 watch-state 里 pos 可能是字符串/None：播放器启动/换片时会问这一句，
        # 而调用点在槽链上——一个 ValueError 就足以让整个进程 abort。
        if isinstance(entry, dict) and not entry.get("done"):
            try:
                return float(entry.get("pos", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    @staticmethod
    def _stamp(p: str) -> tuple[int, int] | None:
        """文件的"记号"：改过内容/换过大小都会变；取不到（文件没了）返回 None。"""
        try:
            st = os.stat(p)
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

    def _load_watch_state(self) -> dict:
        """读整份 watch-state，但只在文件真的变过之后再读盘。

        为什么不干脆每次读：媒体库每张卡片要问一次"看到哪儿了"，N 张卡片就是 N 次
        open + json.load 整份表（500 条上限、几十 KB）——那是"打字时窗口卡住"里最贵的
        一笔，而它换回来的正确性只有一个：万一别的程序改了这份文件，我们要看见。
        所以把"看见改动"的活儿交给文件记号（mtime_ns+size）：记号没变就用缓存，
        变了（包括文件被删、被外部重写）就重读。写盘走 _save_watch_state，写完顺手把
        缓存钉成新那份，自己写自己永远立刻可见，不必靠重读。

        已知的失效半径：外部程序在**同一个 mtime_ns 且同样大小**的位置改写内容，记号认不出。
        这要求对方精确复刻字节数、并落在同一时钟刻度里（本机 NTFS 记号约 100ns 粒度、
        系统时钟粒度约 15.6ms）——而这份文件除了本程序只有自己写的路径（single-instance
        转交把第二个实例收进同一个进程），所以这一档不单独设防，真出现"进度不刷新"
        时按这条查即可。
        """
        p = self.watch_state_path
        stamp = self._stamp(p)
        # None == None 也算"没变过"：文件还没诞生（第一次运行、还没记住任何进度）时，
        # 不缓存就意味着每张卡片都 open 一次必然失败——而首次运行恰恰是最常走的那条路。
        # 文件真出现时记号从 None 变成实值，下一问自然重读，不会把"还没有进度"包成永久状态。
        if self._ws is not None and stamp == self._ws_stamp:
            return self._ws
        self.ws_reads += 1
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            d = d if isinstance(d, dict) else {}
        except Exception:
            d = {}
        self._ws, self._ws_stamp = d, stamp
        return d

    def _save_watch_state(self, table: dict) -> bool:
        """整份进度落盘。**绝不把 OSError 扔给调用方**：remember_position 挂在
        `time-pos` 槽上（每 5 秒一次），只读目录/磁盘满时从槽里穿出去就是 PyQt5 的
        abort——用户看到的是"看着看着播放器自己没了"。

        失败时仍然把刚记下的这份写进内存缓存（并保留/更新文件记号），这一趟会话里的
        进度照常可见；盘上那份留到下次成功时再补。非字典条目（手改坏的表）直接丢掉，
        它们没有 pos/dur，留着只会在下一次排序时再炸一次。
        """
        def ts_of(kv) -> float:
            try:
                return float(kv[1].get("ts", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                return 0.0

        # 控制体积：只留最近 500 条
        items = sorted(((k, v) for k, v in table.items() if isinstance(v, dict)),
                       key=ts_of, reverse=True)[:500]
        saved = dict(items)
        tmp = self.watch_state_path + ".tmp"
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(saved, fh, ensure_ascii=False)
            os.replace(tmp, self.watch_state_path)
        except OSError as exc:
            print(f"[config] 观看进度保存失败 {self.watch_state_path}："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                os.remove(tmp)
            except OSError:
                pass
            self._ws, self._ws_stamp = saved, self._stamp(self.watch_state_path)
            return False
        # 写穿：缓存直接等于刚写下去的这份。不写穿的话，紧接着的 watch_state() 会拿
        # 旧记号去比对新文件、白读一次盘（而 remember_position 之后正是界面要刷新那一条）。
        self._ws, self._ws_stamp = saved, self._stamp(self.watch_state_path)
        return True


def ensure_dirs(cfg: Config) -> None:
    for d in (DATA_DIR, CACHE_DIR, LOG_DIR, str(cfg["screenshot_dir"])):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def same_media_path(a: str, b: str) -> bool:
    """两个路径是否指同一个文件（与「最近播放」用同一套规范形）。空串一律不相等。

    用途是"这一批字幕/进度是给哪支片的"：快速 A→B 连切时，A 的 file-loaded 回包
    可能比 B 的晚到，拿原字符串比会把 A 的字幕挂到 B 上。只做比较，不碰文件系统。
    """
    ka, kb = Config.recent_key(a or ""), Config.recent_key(b or "")
    return bool(ka) and ka == kb


def sweep_cache_temp(cache_dir: str | None = None) -> int:
    """清掉缓存里上次"非正常死亡"留下的半成品：`*.part.jpg`（封面写了一半）与 `*.tmp`。

    为什么值得在启动时扫：封面任务先写 `<out>.part.jpg` 再 `os.replace` 成成品；主进程
    被 `taskkill /F` 或崩溃带走时，一半的 jpg 就永远留在盘上——它不会被 `ready()` 认成
    成品（文件名不同），却会一直占着体积，而"启动时顺手清"是唯一不需要另一条清理
    线程/计时器的时机（此刻单实例保证没有别的本程序在写这些文件）。
    删不掉（被占用/权限）就跳过：这是打扫，不是启动的硬依赖。
    """
    root = cache_dir or CACHE_DIR
    n = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".part.jpg") or fn.endswith(".tmp"):
                try:
                    os.remove(os.path.join(dirpath, fn))
                    n += 1
                except OSError:
                    pass
    return n
