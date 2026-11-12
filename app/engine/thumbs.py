"""时间轴缩略图：ffmpeg 抽帧 → 拼成 sheet 落盘缓存 → 按秒数裁出那一格。

下面四条决定了这个模块的形状（都是实测/踩坑得来的，不是想当然）：

0. **退出时它是压死宿主的头号嫌疑人**。悬停一触发就起 ffmpeg 线程池作业，而用户关掉窗口
   的时候作业常常还在途——那一瞬的拆除顺序不由我们决定。`data/logs/crash.log` 22:56:34
   那桩与 `tests/_probe_exit_crash.py` 的分布（在途退出 15 趟 8 趟 0xC0000409）都在说同一件
   事，所以发射走 `poolkit.emit`、壳走 `poolkit.pin`、池子不挂 self、`request()` 认 closing 旗。
   四条的机制与"为什么不能写成 `waitForDone`"全在 `app/engine/poolkit.py` 开头。

1. **一次 ffmpeg 跑完全部缩略图**，不是每悬停一次抽一张。逐次抽帧要为每次 seek 重建
   解码器，4K 原盘上悬停几下就是几十秒卡顿；`fps=1/N` + `tile` 一条命令只解码一遍，
   产物是几张拼好的大图（一张 10×10=100 格），读盘+裁格是常数开销。
2. **只解关键帧**（`-skip_frame nokey` 放在 `-i` 之前，作用在解码器上）。缩略图不需要
   帧精确，误差被 step（最短 1 秒）吸收；换来的是长片首屏从"分钟级"降到"十几秒"。
   代价：GOP 很长的片子某些 step 网格会重复上一帧——`fps` 滤镜本来就靠重复补帧维持
   输出帧率，所以格数不缺口不空。
3. **裁格几何量出来，不假设**。cw=img.width()//COLS、ch=img.height()//ROWS 从真实产物的
   像素尺寸反推。曾经写过 scale=160:-1，某些奇高视频会得到非整数高，tile 出来的总高
   就不是 ROWS×期望格高，硬按假设值裁会整片错位——所以这里只信量到的数。

未命中（正在抽、抽失败、没装 ffmpeg）一律返回 None，调用方降级为纯时间气泡：
预览缺不缺是体验问题，绝不允许把时间轴本身拖下水。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from PyQt5.QtCore import QObject, QRect, QRunnable, pyqtSignal

from . import poolkit

# 秒/张 的档位：时长决定用哪一档，保证"再长的片也只这么多张"。
# 取"≥ duration/MAX_THUMBS 的最小档"，落在全整数上，读日志时一眼能对上。
STEP_LADDER = (1, 2, 3, 5, 10, 15, 20, 30, 60, 120, 240, 480)
MAX_THUMBS = 300          # 一部片最多抽这么多张（2h 片 → step=24s 左右）
COLS, ROWS = 10, 10       # 一张 sheet 存 COLS×ROWS 格
THUMB_W = 160             # 缩略图宽（高按比例，16:9 时是 90）
CELLS = COLS * ROWS
KF_ONLY_ABOVE = 900.0     # 秒：超过这个时长改成"只解关键帧"（见 ThumbJob.run 里的说明）


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def pick_step(duration: float) -> int:
    """按总时长挑一张几秒。时长未知/为 0 时给最小档，别一上来就抽成幻灯片。"""
    if duration <= 0:
        return STEP_LADDER[0]
    need = duration / MAX_THUMBS
    for s in STEP_LADDER:
        if s >= need:
            return s
    return STEP_LADDER[-1]


class _JobSignals(poolkit.JobSignals):
    """QRunnable 不能自带信号（它不是 QObject），所以信号挂在这个小对象上转发。

    这个壳就是 `data/logs/crash.log` 里被删掉的那个对象——它的生死原本跟着 store 的拆除
    和解释器收尾走；现在由 `poolkit.pin()` 在 `run()` 期间寄在模块级名册里，发射走
    `poolkit.emit()`（`done` 这个信号本身在 `poolkit.JobSignals` 上声明，这里不再挂第二份：
    同一个名字挂两个信号，运行时读到的到底是哪一个就变成运气了）。
    """


class ThumbJob(QRunnable):
    def __init__(self, exe: str, video: str, step: int, width: int, out_dir: str, count: int,
                 keyframe_only: bool = False):
        super().__init__()
        self.autoDelete = True
        self.exe, self.video, self.step, self.width = exe, video, step, width
        self.out_dir, self.count = out_dir, count
        self.keyframe_only = keyframe_only
        self.running = False                 # 真的在 run() 里＝占着一个线程池名额
        self.sig = _JobSignals()
        self.key = os.path.basename(out_dir)

    def run(self) -> None:                       # 在线程池线程里跑，绝不碰 Qt 对象
        self.running = True
        # sig 在入口处拿一次本地引用，整条路只用这个引用；壳同时寄进 poolkit 的名册。
        # 这两件事都是为 crash.log 那一桩做的（见 _JobSignals 的说明与 poolkit 开头）：
        # 原来的写法是每次发射都走 `self.sig`，而那时包装器可能已经被 store 拆除带走了。
        sig = self.sig
        poolkit.pin(self)
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            sheets = max(1, -(-self.count // CELLS))
            out = os.path.join(self.out_dir, "sheet-%02d.jpg")
            cmd = [
                self.exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            ]
            # 只在长片上跳非关键帧：短片（≤KF_ONLY_ABOVE）整片解一遍也就一秒级，
            # 换来的是每张都落在真正的那一刻；长片反过来——样片实测 10 秒 H.264 只有
            # 2 个关键帧（GOP=250），这种文件用 nokey 会得到大片重复帧的假预览，
            # 所以这一档必须由时长决定，不能全局打开。
            if self.keyframe_only:
                # 放在 -i 之前＝解码器参数：只解关键帧（长片提速的关键一条）
                cmd += ["-skip_frame", "nokey"]
            cmd += [
                "-i", self.video,
                "-an", "-sn", "-dn",
                "-vf", f"fps=1/{self.step},scale={self.width}:-2,tile={COLS}x{ROWS}",
                "-frames:v", str(sheets),
                out,
            ]
            # poolkit.run 而非 subprocess.run：同一份 argv、同样的返回形状，多的一件事是这个
            # 孩子被登记过——退出收口时杀得到它（实测过关掉窗口后 ffmpeg 还在往盘上写）。
            t0 = time.time()
            r = poolkit.run(self, cmd, timeout=1800, text=True, encoding="utf-8",
                            errors="replace")
            made = sorted(f for f in os.listdir(self.out_dir) if f.startswith("sheet-"))
            if r.returncode != 0 or not made:
                reason = (r.stderr or "").strip().splitlines()
                poolkit.emit(sig, "done", self.key, False,
                             f"rc={r.returncode} " + ("; ".join(reason[-2:]) if reason else "无产物"))
                return
            meta = {"video": self.video, "step": self.step, "width": self.width,
                    "cols": COLS, "rows": ROWS, "count": self.count, "sheets": len(made),
                    "keyframe_only": bool(self.keyframe_only),
                    "made_s": round(time.time() - t0, 2), "ts": time.time()}
            _atomic_write(os.path.join(self.out_dir, "meta.json"), json.dumps(meta, ensure_ascii=False))
            poolkit.emit(sig, "done", self.key, True, f"{len(made)} 张 sheet / {self.count} 格 / "
                                                     f"{meta['made_s']}s")
        except BaseException as exc:             # 抽缩略图失败只配降级，不配把宿主弄死
            # BaseException 而不是 Exception：crash.log 那一桩是 `except Exception` 里再发射
            # 时自己抛 RuntimeError 把进程带走；兜底这层如果只挡一半异常类型，就等于没挡。
            poolkit.emit(sig, "done", self.key, False, f"{type(exc).__name__}: {exc}")
        finally:
            self.running = False
            poolkit.drop_child(self)
            poolkit.unpin(self)


class ThumbStore(QObject):
    """按 (文件, step, 宽度) 缓存缩略图；对外只三句话：要、有没有、给我那一格。"""

    sig_done = pyqtSignal(str, bool, str)        # key, 成功?, 详情（给日志/验收用）
    PRUNE_EVERY = 20            # 攒够这么多份新 sheet，才做一次全目录回收
    FAILED_CAP = 4096           # 失败负缓存封顶（B11：坏文件再多也不许跟着库长）
    SHEET_CAP = 3               # 内存里最多留几张整图；见 _remember_sheet 的体积账

    def __init__(self, ffmpeg_exe: str, cache_dir: str, max_jobs: int = 2, parent=None):
        super().__init__(parent)
        self.exe = ffmpeg_exe
        self.cache_dir = cache_dir
        # 池子**不挂 self**：挂了就是"store 一拆，~QThreadPool 攥着 GIL 等线程收"那条死锁的
        # 来路（实测：主线程停在 del store、工作线程停在 os.makedirs 等 GIL，六份栈一模一样）。
        # 名字进 poolkit 的名册，退出收口时能点名是哪张池子还没排空。
        self._pool = poolkit.new_pool("thumbs", max_jobs)    # 并发节流：别把解码器全占满
        self._active: dict[str, ThumbJob] = {}
        self._failed: dict[str, str] = {}
        self.failed_evictions = 0                  # 计量：封顶挤出过几条（判据读它）
        self._meta: dict[str, dict[str, Any]] = {}
        # key -> (sheet 序号, QImage)：LRU，只留最近 SHEET_CAP 张。注释里写"只留一张"
        # 而字典按 key 只增不减，是另一回事——悬停过多少支片就会攥着多少张整图。
        self._sheet_cache: dict[str, Any] = {}
        self.sheet_evictions = 0                  # 计量：内存缓存挤出过几张（判据读它）
        self.last_info = ""                        # 最近一次成功的耗时/规模，验收报告读它
        self.prunes = 0                            # 计量：真做过几次全目录回收
        self._since_prune = 0

    # ---------- key ----------
    def key_for(self, path: str, duration: float, width: int = THUMB_W) -> str:
        try:
            st = os.stat(path)
            tag = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{duration}|{width}"
        except OSError:
            tag = f"{os.path.abspath(path)}|-|?|{duration}|{width}"
        return hashlib.md5(tag.encode("utf-8", "replace")).hexdigest()[:16]

    def dir_of(self, key: str) -> str:
        return os.path.join(self.cache_dir, key)

    # ---------- 请求 ----------
    def _note_failed(self, key: str, why: str) -> None:
        """失败负缓存封顶（B11）：键是 (文件, step, 宽)，坏文件多、档位多时条目只增不减。

        负缓存只是"别再试了"的备忘；到顶挤掉最旧的一条，代价是将来可能多起一次 ffmpeg。
        `failed_evictions` 给判据读——不做成静默的无限字典。
        """
        self._failed[key] = why
        while len(self._failed) > self.FAILED_CAP:
            self._failed.pop(next(iter(self._failed)))    # 插入序＝FIFO
            self.failed_evictions += 1

    def request(self, path: str, duration: float, width: int = THUMB_W) -> str:
        """确保这份片的缩略图在排队/在盘上，返回 key。已有缓存不再起进程。"""
        key = self.key_for(path, duration, width)
        if not os.path.exists(path):
            self._note_failed(key, "视频文件不存在")
            return key
        meta = self._load_meta(key)
        if meta:
            return key
        if poolkit.is_closing():
            # 退出收口之后不再起新趟：closeEvent 里那一句 shutdown 已经把队列清了，
            # 这时再起一个 ffmpeg 就是"窗口已经没了、进程还在后台抽帧"——用户看见的是
            # 任务管理器里多出来的一行，不是播放器。返回 key 让调用方照常降级为纯时间气泡。
            return key
        if key in self._active or key in self._failed or not os.path.exists(self.exe):
            return key
        step = pick_step(duration)
        count = max(1, -(-int(duration or 0) // step))
        job = ThumbJob(self.exe, path, step, width, self.dir_of(key), count,
                       keyframe_only=bool(duration and duration > KF_ONLY_ABOVE))
        job.sig.done.connect(self._on_done)
        self._active[key] = job
        self._pool.start(job)
        return key

    def ready(self, key: str) -> bool:
        return self._load_meta(key) is not None

    def running(self) -> int:
        """此刻真的在抽的个数。_active 里还包含"已提交、在池子里排队"的，
        拿它当并发数会永远看到 4，节流到底生没生效就测不出来了。"""
        return sum(1 for j in self._active.values() if j.running)

    def _load_meta(self, key: str) -> dict | None:
        if key in self._meta:
            return self._meta[key]
        p = os.path.join(self.dir_of(key), "meta.json")
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and os.path.exists(d.get("video", "")):
                self._meta[key] = d
                return d
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def _on_done(self, key: str, ok: bool, detail: str) -> None:
        self._active.pop(key, None)
        if ok:
            self._meta.pop(key, None)        # 让 _load_meta 从盘上重新读一次
            self.last_info = detail
            self._note_done()                # 回收按批扫，不在每张回来时扫（见 _note_done）
        else:
            self._note_failed(key, detail)
        self.sig_done.emit(key, ok, detail)

    # ---------- 取一格 ----------
    def image_at(self, key: str, sec: float):
        """返回 sec 那一格的 QImage；任何不确定的情况一律 None（调用方降级为纯时间气泡）。"""
        from PyQt5.QtGui import QImage, QPixmap       # 只有真取图时才要 QtGui
        meta = self._load_meta(key)
        if not meta:
            return None
        step = int(meta.get("step") or 0) or 1
        idx = int(max(0.0, sec) // step)
        if idx >= int(meta.get("count") or 0):
            idx = max(0, int(meta["count"]) - 1)
        sheet_no = idx // CELLS
        if sheet_no >= int(meta.get("sheets") or 0):
            return None
        img = self._sheet(key, meta, sheet_no)
        if img is None or img.isNull():
            return None
        cw, ch = img.width() // int(meta.get("cols") or COLS), img.height() // int(meta.get("rows") or ROWS)
        if cw <= 0 or ch <= 0:
            return None
        cell = idx % CELLS
        r, c = divmod(cell, int(meta.get("cols") or COLS))
        x, y = c * cw, r * ch
        if x + cw > img.width() or y + ch > img.height():
            return None
        return QPixmap.fromImage(img.copy(QRect(x, y, cw, ch)))

    def _remember_sheet(self, key: str, no: int, img) -> None:
        """把一张整图记进内存，超过 SHEET_CAP 就挤掉最久没碰的。

        体积账：160px 宽的 16:9 缩略图，一张 10×10 的 sheet 是 1600×900，
        RGB32 下约 5.8MB。按 key 只增不减的话，在媒体库里来回悬停几十支片就是
        一两百 MB——而且这账不会自己退，直到退出。留 3 张 ≈ 17MB 封顶，
        同时够覆盖"同一支片、相邻两支片"的来回悬停，命中率不受影响。

        命中也要挪到队尾（LRU 而不是 FIFO）：正在悬停的那支片不该被
        新抽完的别的片子挤走。挤出的张数记在 `sheet_evictions` 里给判据读。
        """
        self._sheet_cache.pop(key, None)          # 先摘再插＝挪到队尾
        self._sheet_cache[key] = (no, img)
        while len(self._sheet_cache) > self.SHEET_CAP:
            self._sheet_cache.pop(next(iter(self._sheet_cache)))
            self.sheet_evictions += 1

    def _sheet(self, key: str, meta: dict, no: int):
        """整张 sheet 读一次就记住（LRU 上限见 _remember_sheet）：同一张内换格是零成本。"""
        hit = self._sheet_cache.get(key)
        if hit and hit[0] == no:
            self._remember_sheet(key, no, hit[1])   # 命中即刷新次序
            return hit[1]
        from PyQt5.QtGui import QImage
        try:
            files = sorted(f for f in os.listdir(self.dir_of(key)) if f.startswith("sheet-"))
        except OSError:
            return None                 # 缓存目录被人清掉了：降级，不是异常
        if no >= len(files):
            return None
        p = os.path.join(self.dir_of(key), files[no])
        img = QImage(p)
        if img.isNull():
            return None
        self._remember_sheet(key, no, img)
        return img

    # ---------- 维护 ----------
    def _note_done(self) -> None:
        """记一笔"盘上多了一份"；攒够 PRUNE_EVERY 份才真做一次全目录回收。

        判据数的是 `prunes`（做过几次回收），不是耗时：`prune()` 的账是"每个缓存目录
        开一次 meta.json"，所以回收一次的成本随缓存规模涨；把它挂在"每份新缓存"上，
        一次拖窗滚动的悬停就能触发几十次全目录扫描，界面就是在这些地方卡的。
        按批回收换来的是"最多攒 20 份才扫一次"，代价是缓存可能临时超出 keep 一点——
        keep=40 本来就是粗粒度体积阀，不是硬配额。
        """
        self._since_prune += 1
        if self._since_prune >= self.PRUNE_EVERY:
            self._since_prune = 0
            self.prune()

    def prune(self, keep: int = 40) -> int:
        """按 meta 时间戳留下最近 keep 份，其余整目录删掉；返回删除份数。"""
        self.prunes += 1
        try:
            dirs = [d for d in os.listdir(self.cache_dir)
                    if os.path.isdir(os.path.join(self.cache_dir, d))]
        except OSError:
            return 0
        entries = []
        for d in dirs:
            mp = os.path.join(self.cache_dir, d, "meta.json")
            ts = 0.0
            try:
                with open(mp, encoding="utf-8") as fh:
                    meta = json.load(fh)
                # 手改坏/旧版本写坏的 meta 可能是列表、字符串：`.get` 会 AttributeError，
                # 而 prune 挂在 "每 20 份新缓存" 的收尾链上，异常穿进 Qt 槽就是整进程 abort。
                if isinstance(meta, dict):
                    ts = float(meta.get("ts") or 0.0)
            except (OSError, ValueError, TypeError, AttributeError):
                ts = 0.0
            if not ts:
                # 目录也可能在 listdir 之后被别的进程删掉：取不到时间就按"最旧"算，
                # 让它排到队尾、由下面带 ignore_errors 的删除收掉，绝不让 OSError 逃出去。
                try:
                    ts = os.path.getmtime(os.path.join(self.cache_dir, d))
                except OSError:
                    ts = 0.0
            entries.append((ts, d))
        entries.sort(reverse=True)
        gone = 0
        for _, d in entries[max(0, keep):]:
            import shutil
            shutil.rmtree(os.path.join(self.cache_dir, d), ignore_errors=True)
            self._meta.pop(d, None)
            self._sheet_cache.pop(d, None)
            gone += 1
        return gone

    def wait(self, ms: int = 0) -> bool:
        """等线程池排空（验收脚本用；UI 里永远不该调它）。"""
        t0 = time.time()
        while self._active and (ms <= 0 or (time.time() - t0) * 1000 < ms):
            time.sleep(0.05)
        # waitForDone 不是 waitForFinished：后者是 Qt6 才有的名字，本机 PyQt 5.15.2 的
        # QThreadPool 只有 activeThreadCount / waitForDone（实测 dir()）。写错的那一版每次
        # 调用都抛 AttributeError，只是过去没有一条测试真去调过这个 wait()。
        self._pool.waitForDone(200)
        return not self._active
