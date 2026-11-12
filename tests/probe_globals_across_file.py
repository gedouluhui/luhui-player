"""小探测（一次性，不进常跑链）：换文件那一下，内核到底**改不改值、推不推**。

产出的留档就是 `tests/out/probe-globals-across-file.txt`——产品注释（`app/engine/player.py`
的 `_FILE_SCOPED` 上方）与 `tests/test_append_semantics.py` 的 E18/E19 都引它，所以这个
文件名与这套读数形状是有主的，不许随手改成本脚本的别用版本（2026-10-17 就发生过一次：
把它改成"整张表普查"之后，已引用的那份留档复现不出来了；普查版另存
`tests/probe_file_scoped_table.py`，这里收回成四格版）。

要判的机制（playlist-pos 那一笔）：外壳在 `start-file` 事件里把 `_FILE_SCOPED` 那几格的
缓存抹掉，指望内核在新文件上再推一发把缓存填回来。这个指望只在**"换文件时那一格的真值
确实会变"**时成立。所以每一格要问两件事，两件事都是这台机器上这个内核量的：

    真值变没变？（get_property 在换文件前后各问一次）
    推没推？  （订阅回调在 mark 之后有没有再来一发）

"值不变 + 不推"的格抹了就永远空着——依赖它的决策静默退化成默认值。
四格是 loop-file／ab-loop-a／ab-loop-b／playlist-pos：playlist-pos 是已知的病灶（值会变、
也推，但被抹过所以留过现场），前三格是同族嫌疑（"文件作用域"的名字听着像、真值其实跨文件）。
整张表的普查在 `tests/probe_file_scoped_table.py`，那一趟把"其余无害"从推测变成测量。

形状照 `tests/probe_append_idle.py`：留画面（`--force-window=yes`，有些解码路径要窗口才推进）、
只断声音（`--no-audio --mute=yes --volume=0`）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from app.engine.ipc import MpvIpc                      # noqa: E402
from player_harness import ask_soft, kill_pids, tell   # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
S1 = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
S2 = os.path.join(HERE, "samples", "gelu-sample-2.mp4")
WATCH = ["loop-file", "ab-loop-a", "ab-loop-b", "playlist-pos"]


def main() -> int:
    pipe = rf"\\.\pipe\gelu-probe-global-{int(time.time())}"
    cmd = [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
           "--idle=yes", "--force-window=yes", "--keep-open=yes",
           "--no-audio", "--mute=yes", "--volume=0", "--really-quiet"]
    p = subprocess.Popen(cmd, cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[probe] 内核 pid={p.pid} pipe={pipe}（有画面、无声音）", flush=True)
    ipc = MpvIpc(pipe)
    pushes: list[str] = []
    mark = threading.Event()
    try:
        if not ipc.connect(timeout=10.0, proc=p):
            print("[probe] 连不上 IPC", flush=True)
            return 2
        # 空载时 path 回的是 property unavailable（不是 None），所以用 ask_soft 把
        # "问不到"单列一栏，别让一条正常的不存在把整批读数作废。
        on0, miss0 = ask_soft(pipe, WATCH + ["path"], wait=8.0)
        print(f"[probe] ①起步基线 get_property：{on0} 读不到={miss0 or '（没有）'}", flush=True)
        for k in WATCH:
            ipc.observe(k, (lambda name: (lambda v: pushes.append(
                f"{name}={v!r} {'换文件后' if mark.is_set() else '换文件前'}")))(k))
        time.sleep(1.0)
        print(f"[probe]   订阅后推发（应有初始值一发）：{pushes}", flush=True)

        tell(pipe, ["loadfile", S1, "replace"])
        time.sleep(3.0)
        tell(pipe, ["set_property", "loop-file", "inf"])
        tell(pipe, ["set_property", "ab-loop-a", "1.0"])
        tell(pipe, ["set_property", "ab-loop-b", "2.0"])
        time.sleep(1.5)
        on1, miss1 = ask_soft(pipe, WATCH + ["path"], wait=8.0)
        print(f"[probe] ②第一支上设好之后：{on1}", flush=True)
        if not on1:
            return 2
        # 前提：这一格读回来的 path 必须真是第一支，否则后面"换文件前后"的比较没有对象。
        if on1.get("path") != S1:
            print(f"[probe] 前提立不住：path 读回来不是第一支（{on1.get('path')!r}），不作数",
                  flush=True)
            return 2

        pushes.clear()
        mark.set()
        tell(pipe, ["loadfile", S2, "append"])
        time.sleep(1.0)
        tell(pipe, ["set_property", "playlist-pos", "1"])
        time.sleep(4.0)
        on2, miss2 = ask_soft(pipe, WATCH + ["path"], wait=8.0)
        print(f"[probe] ③换到第二支之后（内核真值）：{on2}", flush=True)
        print(f"[probe] ④换文件那一下内核推了：{pushes}", flush=True)
        if miss2:
            print(f"[probe]   （第二支读不到的格：{miss2}）", flush=True)
        print("", flush=True)

        pushed = {s.split("=", 1)[0] for s in pushes}
        bad: list[str] = []
        for k in WATCH:
            if k == "playlist-pos":
                continue                       # 它是本趟的已知病灶，列在②③里作对照
            a, b = on1.get(k, "（读不到）"), on2.get(k, "读不到")
            same = str(a) == str(b)
            was_pushed = k in pushed
            if same and not was_pushed:
                verdict = ("⇒ 同型缺陷：真值没变（内核带着上一支的设置继续）而又不推"
                           " ⇒ 抹了缓存就永远空着")
                bad.append(k)
            elif same and was_pushed:
                verdict = "⇒ 无害：值没变，但内核会补一发，缓存会被填回来"
            else:
                verdict = "⇒ 无害：换文件时真值会变，抹掉正好防陈旧"
            print(f"[probe] 判决 {k}：换文件前后真值={a!r}→{b!r} 相同={same} "
                  f"换文件后被推过={was_pushed} {verdict}", flush=True)
        print(f"[probe] [FINAL] 判'同型缺陷' {len(bad)} 格 —— {bad or '（无）'}", flush=True)
        return 0
    finally:
        try:
            ipc.close()
        except BaseException:
            pass
        kill_pids([p.pid])
        print("[probe] 内核已收", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.exit(2)
