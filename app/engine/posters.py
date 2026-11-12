"""媒体库封面：一条 ffmpeg 命令取一张代表帧，落盘缓存。

为什么不复用 thumbs.py 的 sheet：sheet 是给时间轴用的（160 宽、一部片几百格），
封面要的只是"一张能认出来的图"。让封面去走 sheet，等于为了一个格子抽出整片，
代价差两个数量级。

为什么取 10% 而不是第 0 秒：真实电影的头一两秒常常是黑场/台标/淡入（样片是干净的
合成图，所以这条在样片上看不出差别——但它是按真片的行为定的）。10% 对两小时的片子
也才 12 分钟，不会剧透到中段。

`-ss` 放在 `-i` **之前**＝快速定位（解到最近关键帧就停），封面不需要帧精确，
换来的是 4K 原盘上也是几十毫秒级。要精确的话得挪到 -i 之后，那是时间轴预览的账。

流开关是 `-an -sn -dn`（关音频/字幕/数据），**不是 `-vn`**：`-vn` 关的正是视频流，
于是 image2 muxer 拿到零流直接 "Output file does not contain any stream" 退出——
每个封面都静默失败、缓存永远空、界面一直画占位块。这条在真机上跑了才看得见。
"""
from __future__ import annotations

import hashlib
import os
import time

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal

from . import poolkit

POSTER_W = 320
POSTER_AT = 0.10        # 取全片 10% 处那一帧
_MIN_AT = 1.0           # 至少 1 秒：0 秒基本是黑场


class _PosterSignals(poolkit.JobSignals):
    """信号壳：机制与坑同 `thumbs._JobSignals`（一份说明写在 `app/engine/poolkit.py` 开头）。"""


class PosterJob(QRunnable):
    def __init__(self, exe: str, video: str, out: str, at: float, width: int):
        super().__init__()
        self.autoDelete = True
        self.exe, self.video, self.out, self.at, self.width = exe, video, out, at, width
        self.sig = _PosterSignals()
        self.key = os.path.splitext(os.path.basename(out))[0]
        self.running = False

    def run(self) -> None:
        self.running = True
        sig = self.sig                      # 入口取一次本地引用，见 poolkit 第 1 条
        poolkit.pin(self)
        try:
            os.makedirs(os.path.dirname(self.out), exist_ok=True)
            tmp = self.out + ".part.jpg"
            cmd = [self.exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-ss", f"{self.at:.3f}", "-i", self.video,
                   "-an", "-sn", "-dn", "-frames:v", "1",
                   "-vf", f"scale={self.width}:-2", tmp]
            t0 = time.time()
            r = poolkit.run(self, cmd, timeout=120, text=True, encoding="utf-8",
                            errors="replace")
            ok = r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 512
            if not ok:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            err = " / ".join([l for l in (r.stderr or "").splitlines() if l.strip()][-1:])
            if ok:
                os.replace(tmp, self.out)      # 只留成品：半张图不该被 ready() 认下来
            poolkit.emit(sig, "done", self.key, ok,
                         f"{time.time() - t0:.2f}s" + ("" if ok else f" {err[:80]}"))
        except BaseException as exc:
            poolkit.emit(sig, "done", self.key, False, f"{type(exc).__name__}: {exc}")
        finally:
            self.running = False
            poolkit.drop_child(self)
            poolkit.unpin(self)


class PosterStore(QObject):
    """按文件身份（路径+大小+修改时间）缓存封面；对外三句话：要、有没有、给我图。"""

    sig_done = pyqtSignal(str, bool, str)
    PRUNE_EVERY = 50            # 攒够这么多张新封面，才做一次全目录回收（keep 是 400）
    FAILED_CAP = 4096           # 失败负缓存封顶：坏文件再多也不许跟着库长（B11）

    def __init__(self, ffmpeg_exe: str, cache_dir: str, max_jobs: int = 2, parent=None):
        super().__init__(parent)
        self.exe = ffmpeg_exe
        self.cache_dir = cache_dir
        # 池子不挂 self（实测死锁：拆 store ⇒ ~QThreadPool 攥着 GIL 等工作线程，而工作线程
        # 正等 GIL）。名字进名册，退出收口按名点名。见 app/engine/poolkit.py。
        self._pool = poolkit.new_pool("posters", max_jobs)
        self._active: dict[str, PosterJob] = {}
        # 失败负缓存：dict 而不是 set——set.pop() 是**任意**一条，实测会先把刚记下的
        # 新失败挤掉、把很旧的留着（判据 B11 第一版就是被这个形状弄红的）。dict 的插入序
        # 让"到顶挤最旧"成为真契约，将来再失败一次也不会立刻又去起一趟 ffmpeg。
        self._failed: dict[str, None] = {}
        self.failed_evictions = 0                  # 计量：封顶挤出过几条（判据读它）
        self._img: dict[str, object] = {}         # key -> QPixmap（界面重复绘制时零成本）
        self._order: list[str] = []
        self.last_info = ""
        self.prunes = 0                            # 计量：做过几次全目录回收
        self._since_prune = 0

    def key_for(self, path: str, width: int = POSTER_W) -> str:
        try:
            st = os.stat(path)
            tag = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{width}"
        except OSError:
            tag = f"{os.path.abspath(path)}|-|?|{width}"
        return hashlib.md5(tag.encode("utf-8", "replace")).hexdigest()[:16]

    def file_of(self, key: str) -> str:
        return os.path.join(self.cache_dir, key + ".jpg")

    def _note_failed(self, key: str) -> None:
        """失败负缓存封顶（B11）：一个坏文件一条，库里有几万个坏文件就是几万条。

        负缓存只是"别再试了"的备忘，丢掉一条的代价是将来可能多起一次 ffmpeg，
        而不丢的代价是内存跟着库长——所以到了上限就按插入序挤掉**最旧**的一条，
        `failed_evictions` 留给判据读（不静默地无限长）。
        """
        if key in self._failed:
            return
        self._failed[key] = None
        while len(self._failed) > self.FAILED_CAP:
            self._failed.pop(next(iter(self._failed)))   # 插入序＝FIFO
            self.failed_evictions += 1

    def request(self, path: str, duration: float = 0.0, width: int = POSTER_W) -> str:
        """确保封面在排队/在盘上，返回 key。已有缓存不再起进程。"""
        key = self.key_for(path, width)
        if not os.path.exists(path) or not os.path.exists(self.exe):
            self._note_failed(key)
            return key
        if os.path.exists(self.file_of(key)) or key in self._active or key in self._failed:
            return key
        if poolkit.is_closing():
            # 退出收口后不再起新趟（同 thumbs.request）；这里**不**记进 _failed——
            # "窗口在关了"与"这张封面取不到"是两回事，混在一起会把一次正常退出说成一次失败。
            return key
        at = max(_MIN_AT, (float(duration or 0) * POSTER_AT) if duration else _MIN_AT)
        job = PosterJob(self.exe, path, self.file_of(key), at, width)
        job.sig.done.connect(self._on_done)
        self._active[key] = job
        self._pool.start(job)
        return key

    def _on_done(self, key: str, ok: bool, detail: str) -> None:
        self._active.pop(key, None)
        if ok:
            self.last_info = detail
            self._img.pop(key, None)          # 盘上有了，让 image() 重新读
            self._note_done()                 # 回收按批扫，不在每张回来时扫
        else:
            self._note_failed(key)
        self.sig_done.emit(key, ok, detail)

    def _note_done(self) -> None:
        """记一笔"盘上多了一张"；攒够 `PRUNE_EVERY` 张才真做一次全目录回收。

        为什么不在每次成功回调里都 `prune()`：`prune()` 是"列目录 + 对每张 jpg 取
        修改时间"，账随缓存规模涨（keep=400 就是最多 400 次 getmtime），而这个回调跑在
        **界面线程**上。扫描一个大库时会连续回来成百上千张封面，逐张回收＝成百上千次
        全目录扫描——用户感觉到的"媒体库一开就卡"正是这种。按批回收把次数降到 1/50，
        代价只是盘上可能临时多留 50 张（每张几十 KB，本来就是粗粒度体积阀不是硬配额）。
        """
        self._since_prune += 1
        if self._since_prune >= self.PRUNE_EVERY:
            self._since_prune = 0
            self.prune()

    def ready(self, key: str) -> bool:
        return os.path.exists(self.file_of(key))

    def running(self) -> int:
        return sum(1 for j in self._active.values() if j.running)

    def image(self, key: str):
        """缓存好的 QPixmap；没就绪/读失败返回 None（界面自己画占位）。"""
        hit = self._img.get(key)
        if hit is not None:
            return hit
        from PyQt5.QtGui import QPixmap
        p = self.file_of(key)
        if not os.path.exists(p):
            return None
        pm = QPixmap(p)
        if pm.isNull():
            return None
        self._img[key] = pm
        self._order.append(key)
        while len(self._order) > 60:            # 只留最近 60 张，滚动大库不吃内存
            self._img.pop(self._order.pop(0), None)
        return pm

    def prune(self, keep: int = 400) -> int:
        """按修改时间留下最近 keep 张封面，其余删掉。"""
        self.prunes += 1
        try:
            files = [f for f in os.listdir(self.cache_dir) if f.endswith(".jpg")]
        except OSError:
            return 0

        def age(f: str) -> float:
            # listdir 之后文件可能被别的进程/清理工具删掉：getmtime 一抛，
            # 整个 prune 就从 Qt 槽里穿出去（PyQt5 直接 abort）。取不到按最旧算。
            try:
                return os.path.getmtime(os.path.join(self.cache_dir, f))
            except OSError:
                return -1.0

        files.sort(key=age, reverse=True)
        gone = 0
        for f in files[max(0, keep):]:
            k = os.path.splitext(f)[0]
            try:
                os.remove(os.path.join(self.cache_dir, f))
            except OSError:
                continue
            self._img.pop(k, None)
            gone += 1
        return gone

    def wait(self, ms: int = 0) -> bool:
        t0 = time.time()
        while self._active and (ms <= 0 or (time.time() - t0) * 1000 < ms):
            time.sleep(0.05)
        self._pool.waitForDone(200)        # PyQt 5 的名字是 waitForDone（Qt6 才叫 waitForFinished）
        return not self._active
