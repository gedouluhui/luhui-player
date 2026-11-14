"""媒体库：扫描文件夹 → 建索引（带时长）→ 整表交给界面。

三条设计决定，都不是想当然：

1. **扫描跑在线程池里**，界面只收"整表更新"。走一遍片库是秒级的事（每个新文件还要
   探一次容器头），放主线程就是每次打开侧栏卡一下——而侧栏恰恰是每次都开的。
2. **索引落盘，按 (size, mtime) 决定要不要重新探测**。没变过的文件直接复用上次的
   时长：一次探测 ≈ 一次 ffmpeg 起进程（实测本机 0.05~0.15s），200 个文件每次全跑
   就是 10~30s，而其中真正变过的通常只有几个。
3. **时长用 ffmpeg 探，不依赖 ffprobe**（runtime 里只带了 ffmpeg，见 config 的说明）：
   `-i 文件` 不带输出会立刻退出并把容器信息打在 stderr，那行 `Duration:` 就是我们要的。
   所以这里**不能拿返回码当判据**——它必然非 0。

界面降级：探测失败/容器没写时长 → dur=0，卡片显示"--:--"，绝不显示假数据。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

from PyQt5.QtCore import QCoreApplication, QObject, QRunnable, pyqtSignal

from . import poolkit

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".mts",
             ".m2ts", ".mpg", ".mpeg", ".vob", ".ogv", ".rm", ".rmvb", ".3gp", ".divx", ".asf",
             ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".aif",
             ".aiff", ".dtshd", ".dts", ".mka"}
# 不递归进去的目录名（前缀匹配）：系统回收站、同步缓存、缩略图缓存这类噪声
SKIP_DIR_PREFIX = ("$", ".", "@eaDir")
MAX_DEPTH = 6
MAX_FILES = 3000
# 超过这个数先发骨架列表：卡片（和 "--:--"）先全部出现，时长随后补。小于它时
# 一趟探测本来就是一秒内的事，多发一帧只会让列表闪一下。
PREVIEW_MIN = 80
# 探测是"起 ffmpeg 读容器头"的 IO+进程活；4 路已经足够把 200 项压进几秒，
# 再高就是拿风扇换零点几秒（池子里每个孩子都要登记、收口时要杀）。
PROBE_WORKERS_MAX = 4

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", re.IGNORECASE)


def is_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def parse_duration(text: str) -> float:
    """从 ffmpeg 的 stderr 里取 Duration。取不到就 0.0，不猜。"""
    m = _DUR_RE.search(text or "")
    if not m:
        return 0.0
    h, mi, s, frac = m.group(1), m.group(2), m.group(3), m.group(4) or "0"
    return int(h) * 3600 + int(mi) * 60 + int(s) + float("0." + frac)


def probe_duration(ffmpeg_exe: str, path: str, timeout: float = 20.0, owner: Any = None) -> float:
    """起一次 ffmpeg 只读容器头。返回秒；探不到一律 0.0（调用方按"无时长"降级）。

    `owner` 是那个正在跑的 ScanJob：把它交给 `poolkit.run` 登记，退出收口才杀得到这个
    ffprobe 孩子（不登记的话，关窗后 F 盘上还有几百个 ffprobe 一个个排队跑完，
    用户看到的是"进程没了但风扇还在转"）。不传就走老路（离线单测里直接调这一句用）。
    """
    if not os.path.exists(ffmpeg_exe):
        return 0.0
    cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "info", "-nostdin", "-i", path]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if owner is not None:
            r = poolkit.run(owner, cmd, timeout=timeout, text=True, encoding="utf-8",
                            errors="replace")
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=flags, timeout=timeout)
    except Exception:
        return 0.0
    # 注意：这条命令**必然**非 0 退出（"At least one output file must be specified"），
    # 判据只能是 stderr 里有没有那行 Duration。拿 rc 当判据会把每个文件都探成 0。
    return parse_duration(r.stderr or "")


def walk(roots: Iterable[str], max_files: int = MAX_FILES) -> list[str]:
    """把若干文件夹摊平成媒体文件列表。目录不存在/没权限就跳过，不抛。"""
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = os.path.abspath(root).rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(os.path.abspath(root), followlinks=False):
            depth = dirpath.rstrip("\\/").count(os.sep) - base_depth
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if not any(d.startswith(p) for p in SKIP_DIR_PREFIX)
                           and d.lower() not in ("system volume information",)]
            for fn in filenames:
                if not is_media(fn):
                    continue
                p = os.path.normpath(os.path.join(dirpath, fn))
                key = p.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(p)
                if len(out) >= max_files:
                    return out
    return out


def entry_for(path: str, dur: float = 0.0) -> dict[str, Any]:
    try:
        st = os.stat(path)
        size, mtime = int(st.st_size), float(st.st_mtime)
    except OSError:
        size, mtime = 0, 0.0
    return {"path": path, "name": os.path.basename(path), "size": size, "mtime": mtime,
            "dur": float(dur or 0.0), "ext": os.path.splitext(path)[1].lower()}


def _norm_path(path: str) -> str:
    """比较用的规范形：绝对、小写（Windows）、去掉尾随分隔符。

    只是比较键，不拿去访问文件系统——`os.path.normcase` 在别的平台上不做小写，
    而这套界面只跑 Windows；去掉尾随分隔符是为了让 `D:\\片库` 与 `D:\\片库\\` 算同一个根。
    盘根 `C:\\` 去尾后是 `C:`，下面拼 `+ os.sep` 仍然是 `C:\\`，不会拼坏。
    """
    return os.path.normcase(os.path.abspath(path or "")).rstrip("\\/")


def _is_under(path: str, root: str) -> bool:
    """path 是否在 root 里面（**含 root 的直属文件**）。

    旧判据写的是 `dirname(path).startswith(root + sep)`：根本身的直属文件
    dirname 恰好等于 root，不带尾分隔符，于是永远落选——"移除文件夹"之后
    它们的旧时长还留在索引里，重新加回来时又冒充缓存。这里直接比路径前缀，
    并且保留分隔符，`tree` 不会误伤 `tree2`。
    """
    r = _norm_path(root)
    if not r:
        return False
    p = _norm_path(path)
    return p == r or p.startswith(r + os.sep)


def _live(path: str, gone: set[str], roots: list[str]) -> bool:
    """这一条还该不该出现在库/索引里：没被移除（墓碑）且仍在某个当前根下。

    扫描在工作线程里拿着旧快照跑，回包与骨架帧都要过这一道——两处必须是同一条判据，
    所以提取成函数，而不是在两个信号处理里各抄一份。
    """
    np = _norm_path(path)
    return np not in gone and any(_is_under(np, r) for r in roots)


class _ScanSignals(QObject):
    """扫描作业的回报壳。它不继承 `poolkit.JobSignals`：那边 `done` 的签名是
    (str, bool, str)，这里是 (list, dict, str)——同一个名字挂两个签名不同的信号，
    运行时读到哪一个就变成继承顺序的运气了。壳的寄管与发射照样走 poolkit。
    """
    done = pyqtSignal(list, dict, str)     # 条目表, 新索引, 摘要（给日志/验收）
    progress = pyqtSignal(int, str)        # 已处理数, 当前目录
    preview = pyqtSignal(list)             # 骨架表：文件摊平完先发一帧，时长还没探


class ScanJob(QRunnable):
    """一趟扫描：摊平文件 → 先发骨架 → 并发探测"变了/没见过"的时长 → 整表回报。"""

    def __init__(self, exe: str, roots: list[str], index: dict[str, dict],
                 max_files: int = MAX_FILES):
        super().__init__()
        self.autoDelete = True
        self.exe, self.roots = exe, list(roots)
        # 索引键在入口就统一成规范形：盘上的旧索引可能是任何写法（大写盘符、
        # 正斜杠、尾分隔符），不先归一的话 `get()` 永远打不中，增量缓存整库失效。
        self.index = {_norm_path(k): v for k, v in dict(index).items()
                      if k and isinstance(v, dict)}
        self.max_files = max_files
        self.sig = _ScanSignals()
        self.running = False

    def run(self) -> None:
        self.running = True
        sig = self.sig                      # 入口取一次本地引用，见 app/engine/poolkit.py
        poolkit.pin(self)
        t0 = time.time()
        probed = reused = 0
        cut = ""
        try:
            paths = walk(self.roots, self.max_files)
            out: list[dict] = [entry_for(p) for p in paths]
            todo: list[dict] = []
            ready: set[str] = set()          # 已定时长（复用命中）的规范键
            for e in out:
                key = _norm_path(e["path"])
                old = self.index.get(key)
                # 只有"大小和修改时间都没变"才算没变过。mtime 会因复制/下载而变，
                # 那种情况重探一次是诚实的代价，宁可慢也不要拿旧时长贴新片。
                if old and abs(float(old.get("size") or 0) - e["size"]) < 1 \
                        and abs(float(old.get("mtime") or 0) - e["mtime"]) < 1.0:
                    e["dur"] = float(old.get("dur") or 0.0)
                    ready.add(key)
                    reused += 1
                else:
                    todo.append(e)
            if len(out) >= PREVIEW_MIN:
                # 骨架帧：先让整片库"出现"。发的是**浅拷贝**——这些 dict 随后会被
                # 探测结果原地改写，直接发引用的话界面收到的骨架会跟着变成终值，
                # 这一帧就白发了（验收脚本读到的也是"骨架已带时长"）。
                poolkit.emit(sig, "preview", [dict(e) for e in out])
            done_keys: set[str] = set()
            if todo and poolkit.is_closing():
                # 扫描是"几百个 ffmpeg 起进程"的活，退出收口时不能让它整串跑完。
                # 按**已经处理过的那一段**出账：new_index 只收已定时长的条目，
                # 剩下的下一趟再补（索引按 size/mtime 增量，跳过不是丢数据）。
                cut = f"（退出收口，探到 0/{len(todo)} 就停了）"
            elif todo:
                workers = min(PROBE_WORKERS_MAX, os.cpu_count() or 2)
                pool = ThreadPoolExecutor(max_workers=workers)
                try:
                    futs = {pool.submit(probe_duration, self.exe, e["path"], 20.0, self): e
                            for e in todo}
                    finished = 0
                    for fut in as_completed(futs):
                        e = futs[fut]
                        try:
                            e["dur"] = float(fut.result() or 0.0)
                        except Exception:    # 单个探测炸了只降级这一条
                            e["dur"] = 0.0
                        done_keys.add(_norm_path(e["path"]))
                        probed += 1
                        finished += 1
                        if finished % 20 == 0:
                            poolkit.emit(sig, "progress", reused + finished,
                                         os.path.dirname(e["path"]))
                        if poolkit.is_closing():
                            cut = f"（退出收口，探到 {finished}/{len(todo)} 就停了）"
                            break
                finally:
                    # 不等在途探测：它们的孩子登记在 poolkit，退出收口会统一杀掉；
                    # 排队的直接取消（Python 3.9+ 的 cancel_futures）。
                    pool.shutdown(wait=False, cancel_futures=True)
            new_index: dict[str, dict] = {}
            for e in out:
                key = _norm_path(e["path"])
                if key in ready or key in done_keys:
                    new_index[key] = {"size": e["size"], "mtime": e["mtime"], "dur": e["dur"]}
            poolkit.emit(sig, "done", out, new_index,
                         f"{len(out)} 个媒体文件（探测 {probed} / 复用 {reused}）"
                         f"，用时 {time.time() - t0:.2f}s{cut}")
        except BaseException as exc:         # 扫描炸了只该让媒体库空着，不该带走宿主
            poolkit.emit(sig, "done", [], self.index, f"{type(exc).__name__}: {exc}")
        finally:
            self.running = False
            poolkit.drop_child(self)
            poolkit.unpin(self)


class Library(QObject):
    """媒体库的门面：管 roots、索引落盘、扫描去重，把整表交给界面。"""

    sig_items = pyqtSignal(list)         # 条目表（已按界面给的排序键排好序之前）
    sig_status = pyqtSignal(str)         # 一行状态：扫描中/完成/失败原因
    sig_busy = pyqtSignal(bool)

    def __init__(self, ffmpeg_exe: str, index_path: str, roots: list[str] | None = None,
                 max_jobs: int = 1, parent=None):
        super().__init__(parent)
        self.exe = ffmpeg_exe
        self.index_path = index_path
        self.roots = [r for r in (roots or []) if r]
        self._index: dict[str, dict] = {}
        self._items: list[dict] = []
        self._pool = poolkit.new_pool("library", max_jobs)   # 扫描是 IO+进程风暴，只跑一个；
        # 池子不挂 self——拆 store 触发 ~QThreadPool 攥 GIL 等线程是实测死锁（见 poolkit 开头）
        self._job: ScanJob | None = None
        # 忙标记由主线程自己掌管：refresh() 里在 pool.start() **之前**置真，_on_done 置假。
        # 若改成读 job.running（工作线程里才置位），start 与 run() 之间有一个窗口，
        # 那窗口的第二次 refresh 会排进第二个任务——同一趟扫描被跑两遍。
        self._busy = False
        # 扫描途中发生的"移除"要能盖过这一趟的回包：扫描开始时拿的是旧索引、结束时
        # 整表替换，不记墓碑的话，刚移除的条目会被这份旧结果原样带回来（盘上也会被写回）。
        self._gone: set[str] = set()
        # 扫描途中又有人要扫（加入/移除根之后都会 refresh）：不能静默丢掉，
        # 记下来等这一趟回包后再起一趟。force 取或，重探请求不会被普通刷新吃掉。
        self._pending = False
        self._pending_force = False
        self.followups = 0               # 排队的再扫真起了几趟（判据读这个数）
        self.last_status = ""
        self.load_index()

    # ---------- 索引落盘 ----------
    def load_index(self) -> dict:
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                # 读的时候就归一：旧版本写下的键可能是大写盘符/正斜杠/带尾分隔符，
                # 扫描侧的查找键是规范形，两边不统一等于索引整库失效。
                self._index = {_norm_path(k): v for k, v in d.items()
                               if k and isinstance(v, dict)}
        except (OSError, ValueError):
            self._index = {}
        return self._index

    def save_index(self) -> None:
        os.makedirs(os.path.dirname(self.index_path) or ".", exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._index, fh, ensure_ascii=False)
        os.replace(tmp, self.index_path)

    def _save_index_note(self) -> str:
        """写一次索引并返回失败原因（成功是空串）。

        单独提出来是因为"从媒体库移除/移除文件夹"这两条以前只改内存、不落盘：
        本次会话里看着是删了，重启后又从索引里复活——而界面上根本没有"正在保存"
        这一档，用户看到的是一次"撤销"。索引写不下（盘满/权限）时只回一句话，
        不许把点击整趟弄崩。
        """
        try:
            self.save_index()
        except OSError as exc:
            return str(exc)
        return ""

    # ---------- 扫描 ----------
    def refresh(self, force: bool = False) -> bool:
        """起一趟扫描。已在跑就直接返回 False（去重），并把这趟需求记下来，回包后再起。

        为什么不是单纯丢掉：加根/移除根之后调用方都会 refresh，而这两件事完全可能发生在
        一趟大扫描途中。丢掉那次 refresh，新加的目录要等用户第二次手动刷新才出现，
        移除的目录则会被这一趟的旧结果复活。`wait()` 也不会提前返回——回包时下一趟
        紧接着起，`_busy` 没有变假过。
        """
        if self._busy:
            self._pending = True
            self._pending_force = self._pending_force or bool(force)
            return False
        if poolkit.is_closing():
            # 收口之后不起新趟：那会先把 _busy 置真、再排一个永远不会被投递的作业，
            # 于是"扫描中…"钉在状态行上，等待方（验收脚本）一直耗到超时。
            self.last_status = "窗口在关闭，扫描没起"
            return False
        if force:
            self._index = {}
        self._gone.clear()               # 新一趟开始：上一趟的墓碑已经随回包清过，防御性清空
        self._job = ScanJob(self.exe, self.roots, self._index)
        self._job.sig.done.connect(self._on_done)
        self._job.sig.preview.connect(self._on_preview)
        self._job.sig.progress.connect(lambda n, d: self.sig_status.emit(f"扫描中… {n} 个"))
        self._busy = True
        self.sig_busy.emit(True)
        self._pool.start(self._job)
        return True

    def _on_preview(self, items: list) -> None:
        """骨架帧：整表先出现（时长还没探），不落盘、不动 `_busy`/`_items`。

        过滤规则与 `_on_done` 完全一致（墓碑 + 当前根）：移除动作可能恰好发生在
        骨架帧与终帧之间，那一份旧快照同样不许把删掉的条目带回来。
        """
        roots = list(self.roots)
        gone = self._gone
        self.sig_items.emit([e for e in list(items or [])
                             if _live(str(e.get("path") or ""), gone, roots)])

    def _on_done(self, items: list, index: dict, detail: str) -> None:
        """扫描回包：先按"这一趟开始之后发生的移除"与"现在的根集合"过滤，再落盘。

        这两道过滤都是承重的。扫描在工作线程里跑，开始时快照的是旧索引与旧根：
          · `forget` 的墓碑（`_gone`）——用户刚移除的文件不许被这份旧结果带回来；
          · 当前 `roots`——扫描途中移除的根，整棵子树都不该进索引/界面。
        不做这两道，界面上会出现"删了又回来"，盘上的索引也被写回，重启仍在。
        """
        self._busy = False
        roots = list(self.roots)
        gone = self._gone

        def keep(p: str) -> bool:
            return _live(p, gone, roots)

        self._index = {k: v for k, v in dict(index or {}).items() if keep(k)}
        self._items = [e for e in list(items or [])
                       if keep(str(e.get("path") or ""))]
        self._gone.clear()
        err = self._save_index_note()
        if err:
            detail += f"（索引没存下：{err}）"
        self.last_status = detail
        self.sig_items.emit(list(self._items))
        self.sig_status.emit(detail)
        self.sig_busy.emit(False)
        if self._pending:
            force, self._pending_force = self._pending_force, False
            self._pending = False
            self.followups += 1
            # 放在三个信号之后：界面先看见这一趟的结果，再进入下一趟"扫描中…"。
            # refresh 里会再判 closing；收口途中不会白排一个永不投递的作业。
            self.refresh(force=force)

    def items(self) -> list[dict]:
        return list(self._items)

    def running(self) -> bool:
        return self._busy

    def wait(self, ms: int = 0) -> bool:
        """等扫描跑完**并且信号已投递**（验收脚本用；UI 里永远不该调它，主循环本来就在转）。

        这里必须 processEvents：完成回报是跨线程 queued 连接，_busy 是在投递时的
        _on_done 里落假的。只 sleep 不转事件循环的话，工作线程早就结束了，
        _busy 却还是 True——等待方会一直耗到超时。
        """
        t0 = time.time()
        while self._busy and (ms <= 0 or (time.time() - t0) * 1000 < ms):
            QCoreApplication.processEvents()
            time.sleep(0.01)
        QCoreApplication.processEvents()
        self._pool.waitForDone(200)        # PyQt 5 的名字是 waitForDone（Qt6 才叫 waitForFinished）
        return not self._busy

    # ---------- 集合维护 ----------
    def add_root(self, path: str) -> bool:
        p = os.path.abspath(path) if path else ""
        if not p or any(_norm_path(r) == _norm_path(p) for r in self.roots):
            return False
        self.roots.append(p)
        return True

    def remove_root(self, path: str) -> bool:
        """移出一个根，并把根下（含直属文件）的索引条目一起清掉、立刻落盘。

        判据必须用"路径前缀 + 分隔符"（见 `_is_under`）：旧写法按 dirname 开头比，
        根目录的直属文件 dirname 恰好等于根本身（没有尾分隔符），永远清不掉；
        不加分隔符又会把 `tree2` 当成 `tree` 的人。
        """
        p = os.path.abspath(path) if path else ""
        if not p:
            return False
        match = next((r for r in self.roots if _norm_path(r) == _norm_path(p)), None)
        if match is None:
            return False
        self.roots.remove(match)
        self._index = {k: v for k, v in self._index.items() if not _is_under(k, p)}
        self._items = [e for e in self._items
                       if not _is_under(str(e.get("path") or ""), p)]
        err = self._save_index_note()
        if err:
            self.last_status = f"索引没存下：{err}"
            self.sig_status.emit(self.last_status)
        return True

    def forget(self, path: str) -> bool:
        """从库里摘掉一个文件（不动磁盘上的文件）：索引 + 当前表都去掉，并落盘。

        落盘这一步是承重的：只改内存的版本在重启后会让"移除"变成一场空。
        """
        target = _norm_path(path)
        if self._busy and target:
            # 这一趟扫描的结果里还会带着它；记下墓碑，回包时过滤掉。
            self._gone.add(target)
        hit_key = next((k for k in self._index if _norm_path(k) == target), None)
        before = len(self._items)
        if hit_key is not None:
            self._index.pop(hit_key, None)
        self._items = [e for e in self._items
                       if _norm_path(str(e.get("path") or "")) != target]
        hit = hit_key is not None or len(self._items) != before
        if hit_key is not None:
            err = self._save_index_note()
            if err:
                self.last_status = f"索引没存下：{err}"
                self.sig_status.emit(self.last_status)
        return hit
