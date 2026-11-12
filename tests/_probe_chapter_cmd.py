"""一次性探针：mpv 的 chapter 属性到底怎么写、快速连按会不会丢步。

不建窗口（--vo=null --ao=null），全程后台。产物：tests/out/chapter-cmd.log。
用法：python tests/_probe_chapter_cmd.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from app.engine.ipc import IpcError, MpvIpc  # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
FF = os.path.join(ROOT, "runtime", "ffmpeg", "ffmpeg.exe")
OUT = os.path.join(ROOT, "tests", "out")
SRC = os.path.join(ROOT, "tests", "samples", "gelu-sample-1.mp4")
MKV = os.path.join(OUT, "chapter-probe.mkv")
META = os.path.join(OUT, "chapter-probe.meta.txt")
PIPE = r"\\.\pipe\gelu-chapter-probe-%d" % os.getpid()
CREATE_NO_WINDOW = 0x08000000


def log(s: str) -> None:
    print(s, flush=True)


def make_sample() -> bool:
    os.makedirs(OUT, exist_ok=True)
    with open(META, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(";FFMETADATA1\n")
        for i, (start, end, title) in enumerate(
                ((0, 3000, "片头"), (3000, 6000, "正片 一"), (6000, 9000, "正片 二"))):
            fh.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle={title}\n")
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y", "-i", SRC,
           "-f", "ffmetadata", "-i", META, "-map_metadata", "1", "-codec", "copy", MKV]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=CREATE_NO_WINDOW)
    if p.returncode != 0:
        log("ffmpeg 失败：" + p.stderr.decode("utf-8", "replace")[:800])
        return False
    return True


def main() -> int:
    if not make_sample():
        return 2
    proc = subprocess.Popen(
        [MPV, "--no-config", "--idle=yes", "--vo=null", "--ao=null",
         "--force-window=no", "--no-terminal", f"--input-ipc-server={PIPE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW)
    ipc = MpvIpc(PIPE)
    try:
        for _ in range(80):
            try:
                ipc.connect(timeout=0.5)
                break
            except IpcError:
                time.sleep(0.1)
        else:
            log("连不上 IPC")
            return 2

        def req(cmd, timeout=3.0):
            try:
                return ("ok", ipc.request(cmd, timeout=timeout))
            except IpcError as exc:
                return ("err", str(exc))

        def get_prop(name, default=None):
            ok, val = req(["get_property", name])
            return val if ok == "ok" else default

        def wait_prop(name, pred, timeout=4.0):
            end = time.monotonic() + timeout
            last = None
            while time.monotonic() < end:
                last = get_prop(name)
                if pred(last):
                    return last
                time.sleep(0.03)
            return last

        req(["loadfile", MKV, "replace"])
        rows = wait_prop("chapter-list", lambda v: isinstance(v, list) and len(v) >= 3)
        req(["set_property", "pause", True])
        time.sleep(0.2)
        log(f"chapter-list={rows}")
        cur = req(["get_property", "chapter"])
        log(f"载入后 chapter={cur}")

        r = req(["set_property", "chapter", 2])
        got = wait_prop("chapter", lambda v: int(v or -1) == 2)
        log(f"set_property chapter=2 -> {r} 回读={got}")

        r = req(["set_property", "chapter", 0])
        got = wait_prop("chapter", lambda v: int(v or -1) == 0)
        log(f"set_property chapter=0 -> {r} 回读={got}")

        r = req(["add", "chapter", 1])
        got = wait_prop("chapter", lambda v: int(v or -1) == 1)
        log(f"add chapter 1 -> {r} 回读={got}")

        # 快速连按：一口气发两次，看内核是否两步都走
        req(["set_property", "chapter", 0])
        wait_prop("chapter", lambda v: int(v or -1) == 0)
        r1 = req(["add", "chapter", 1])
        r2 = req(["add", "chapter", 1])
        got = wait_prop("chapter", lambda v: int(v or -1) >= 2, timeout=2.0)
        log(f"连发两次 add chapter 1 -> {r1} / {r2} 回读={got}（要 2）")

        # 在最后一章再 add 1：内核是夹住、还是报错？
        req(["set_property", "chapter", 2])
        wait_prop("chapter", lambda v: int(v or -1) == 2)
        r = req(["add", "chapter", 1])
        time.sleep(0.3)
        got = get_prop("chapter")
        log(f"末章 add chapter 1 -> {r} 回读={got}")

        # 在第一章再 add -1
        req(["set_property", "chapter", 0])
        wait_prop("chapter", lambda v: int(v or -1) == 0)
        r = req(["add", "chapter", -1])
        time.sleep(0.3)
        got = get_prop("chapter")
        log(f"首章 add chapter -1 -> {r} 回读={got}")

        # 越界 set_property 2->5
        r = req(["set_property", "chapter", 5])
        time.sleep(0.3)
        got = get_prop("chapter")
        log(f"set_property chapter=5 -> {r} 回读={got}")

        # —— 产品路径（MpvPlayer.chapter_step）对一个真内核：连按两次、末章不发命令 ——
        req(["loadfile", MKV, "replace"])      # 上面末章 add 可能已把文件走到尾，重新载入
        wait_prop("chapter-list", lambda v: isinstance(v, list) and len(v) >= 3)
        req(["set_property", "pause", True])
        time.sleep(0.2)
        from app import config as cfgmod
        from app.engine.player import MpvPlayer
        pl = MpvPlayer(cfgmod.Config())
        pl._ipc = ipc
        live = get_prop("chapter-list") or []
        pl._props["chapter-list"] = [dict(x) for x in live]
        req(["set_property", "chapter", 0])
        wait_prop("chapter", lambda v: v == 0)
        pl._props["chapter"] = 0
        r1 = pl.chapter_step(1)
        r2 = pl.chapter_step(1)
        got = wait_prop("chapter", lambda v: v == 2)
        log(f"chapter_step 连按两次：{r1['index']} → {r2['index']} 内核回读={got}（要 2）")
        pl._props["chapter"] = 2
        r3 = pl.chapter_step(1)
        time.sleep(0.4)
        got = get_prop("chapter")
        eof = get_prop("eof-reached")
        log(f"末章 chapter_step(1)：index={r3['index']} edge={r3['edge']} moved={r3['moved']} "
            f"回读={got} eof={eof}（要 2 / False）")
        pl._ipc = None
    finally:
        ipc.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
