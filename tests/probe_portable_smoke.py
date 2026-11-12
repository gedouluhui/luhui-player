"""打包版隐藏冒烟：offscrere 平台启动 exe，验证冷启动链路与包内资源，不亮任何窗口。

判据：
· exe / _internal 关键文件齐全（Qt 平台插件、随包字体、mpv、ffmpeg）
· GELU_DATA 指向临时目录时，launch.log 出现 "cold"（真正走到用户入口）
· 进程存活数秒不退（无启动即崩），且没有 crash.log
· 干净退出后临时数据目录仍在原处（配置/日志路径没有跑偏）

用法：python tests/probe_portable_smoke.py [dist 目录]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DIST = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(
    ROOT, "dist", "LuHui播放器")
EXE = os.path.join(DIST, "LuHui播放器.exe")
INTERNAL = os.path.join(DIST, "_internal")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def main() -> int:
    check("exe 存在", os.path.isfile(EXE), f"{os.path.getsize(EXE) / 1e6:.1f}MB" if os.path.isfile(EXE) else EXE)
    must = [
        ("Qt 平台插件", os.path.join(INTERNAL, "PyQt5", "Qt5", "plugins", "platforms", "qoffscreen.dll")),
        ("Qt windows 插件", os.path.join(INTERNAL, "PyQt5", "Qt5", "plugins", "platforms", "qwindows.dll")),
        ("随包字体", os.path.join(INTERNAL, "packaging", "assets", "fonts")),
        ("UI 资源", os.path.join(INTERNAL, "packaging", "assets", "ui")),
        ("mpv 内核", os.path.join(INTERNAL, "runtime", "mpv", "mpv.exe")),
        ("ffmpeg", os.path.join(INTERNAL, "runtime", "ffmpeg", "ffmpeg.exe")),
    ]
    for label, path in must:
        check(f"包内有{label}", os.path.exists(path), path)

    data = tempfile.mkdtemp(prefix="gelu-portable-smoke-")
    env = dict(os.environ)
    env.update({
        "GELU_DATA": data,
        "GELU_NO_MOTION": "1",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_OPENGL": "software",
    })
    proc = subprocess.Popen([EXE], env=env, cwd=DIST,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 25
        log_path = os.path.join(data, "logs", "launch.log")
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if os.path.isfile(log_path):
                try:
                    text = open(log_path, encoding="utf-8", errors="replace").read()
                except OSError:
                    text = ""
                if "cold" in text:
                    break
            time.sleep(0.25)

        alive = proc.poll() is None
        check("启动后存活（没有启动即崩）", alive, f"pid={proc.pid} rc={proc.returncode}")
        text = ""
        if os.path.isfile(log_path):
            text = open(log_path, encoding="utf-8", errors="replace").read()
        check("launch.log 记到 cold 启动", "cold" in text,
              (text.strip().splitlines() or ["(空)"])[-1][:120])
        crash = os.path.join(data, "logs", "crash.log")
        check("没有 crash.log", not os.path.isfile(crash),
              open(crash, encoding="utf-8", errors="replace").read()[:200] if os.path.isfile(crash) else "")
        check("数据目录落在 GELU_DATA 上",
              os.path.isdir(os.path.join(data, "logs")),
              data)

        # 再活两秒，确认不是"打完日志就退"的假活
        time.sleep(2.0)
        check("两秒后仍存活", proc.poll() is None, f"rc={proc.returncode}")
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(data, ignore_errors=True)

    bad = [r for r in results if not r[1]]
    print(f"\n---- 打包版隐藏冒烟：{len(results) - len(bad)}/{len(results)} 通过 ----", flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
