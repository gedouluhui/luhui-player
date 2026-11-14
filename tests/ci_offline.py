# -*- coding: utf-8 -*-
"""离线回归跑者：GitHub Actions / 本地门禁共用（审计“离线与探针分离”）。

为什么要单独一个跑者，而不是 `pytest tests/`：
  · 这里的测试是几十份独立脚本，判据是 `[PASS]/[FAIL]` 与退出码，不依赖 pytest；
  · `unit` 与 `probe` 必须分开：真内核（mpv）、真屏抓帧、真 ffmpeg 的探针在
    windows-latest 上没有素材也不该跑；它们留在本地按需执行。
  · 每个脚本隔离一份数据目录、强制 offscreen、限时；**rc=0 但一个字都没输出**
    判红——那是"起错解释器/空跑"的形状，门禁不能把沉默当通过。

用法：
    python tests/ci_offline.py              # 跑全部离线套件
    python tests/ci_offline.py --list       # 只列清单（判据用）
    python tests/ci_offline.py --only test_keymap.py --verbose
"""
from __future__ import annotations

import argparse
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

# unit：纯离线，无 mpv、无 ffmpeg、无真实桌面抓帧。CI 必跑。
SUITE = [
    "test_hardening.py",        # IPC 上限 / 回调快照 / crash.log 轮转
    "test_dpi_ci.py",           # 混合 DPI / CI 门禁 / 供应链
    "test_library_scale.py",    # 索引规范化 + 并发探测 + 骨架
    "test_responsiveness.py",   # 界面响应性（数次数，不掐表）
    "test_win_geom.py",         # 窗口几何记忆（纯规划器 + 真窗口接线）
    "test_seek_polish.py",      # 拖动节流 / 精确 seek / 防回弹
    "test_session_state.py",    # 会话四态闭环
    "test_job_kill.py",         # Job Object kill-on-close + 启动清扫
    "test_append_semantics.py", # 播放列表追加语义
    "test_chapter.py",          # 章节与副字幕 id
    "test_equalizer.py",        # 十段均衡器（--no-kernel：只跑离线那半）
    "test_gif_wiring.py",       # 截图工坊接线（不跑 ffmpeg）
    "test_gif_dialog.py",       # 工坊对话框（不抓屏）
    "test_help_page.py",        # 快捷键一览页
    "test_keymap.py",           # 快捷键表
    "test_keymap_dialog.py",    # 快捷键编辑对话框
    "test_open_receipt.py",     # “已载入 N 个文件”回执
    "test_round25.py",          # R25 四轴离线判据
    "test_subtitle_drop.py",    # 字幕拖入路由
    "test_ui_polish.py",        # 窄窗控制条与按钮态（offscreen）
    "test_usage.py",            # 资源实测
    "test_fs_chrome_offline.py",# 全屏 chrome 状态机
    "test_engine_async.py",     # 内核启动排队/失败告知（假内核）
]

# probe：需要真实 mpv / 真 ffmpeg / 真屏幕像素，只在本地按需跑，不进 CI。
PROBE = [
    "test_engine.py", "test_engine_async_real.py", "test_gif.py",
    "test_gif_real.py", "test_thumbs.py", "test_transform.py", "test_ui.py",
    # 下面四份要本机素材或本地留底，干净 checkout 里没有：
    "test_library.py",          # 真 ffmpeg 探测时长/封面
    "test_shutdown_quiet.py",   # 真 ffmpeg + 4K 样片做在途收口
    "test_instance.py",         # tests/out 里的改前留底对照
    "test_launchers.py",        # 本地生成的启动器/注册表产物
]

# 需要固定附加参数的 unit：均衡器在 CI 里没有播放内核，只跑离线判据那半。
ARGS = {"test_equalizer.py": ["--no-kernel"]}


def run_one(name: str, data_root: str, timeout: int, verbose: bool) -> tuple[str, int, str]:
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        return "MISSING", -1, ""
    data = os.path.join(data_root, name.replace(".py", ""))
    os.makedirs(data, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": ROOT,
        "LUHUI_DATA": data,
        "GELU_DATA": data,
    })
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, "-u", path] + ARGS.get(name, []), cwd=ROOT, env=env,
                           capture_output=True, timeout=timeout)
        rc = p.returncode
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if exc.stdout else ""
        return f"TIMEOUT({timeout}s)", 124, out
    dt = time.monotonic() - t0
    if verbose:
        print(out, end="" if out.endswith("\n") else "\n")
    if rc == 0 and not out.strip():
        return "EMPTY(rc=0 无输出)", 1, out
    if rc == 0:
        return f"PASS({dt:.1f}s)", 0, out
    if name in ARGS and rc == 3:
        # 脚本自己声明"点名跳过的真机半不作数"（均衡器 --no-kernel 就是这样），
        # 不是产品失败；只有参数在 ARGS 里、且脚本明确退 3 才放行。
        return f"PASS-ENV(rc=3, {dt:.1f}s)", 0, out
    return f"FAIL(rc={rc}, {dt:.1f}s)", rc, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线回归套件跑者")
    ap.add_argument("--list", action="store_true", help="列出 unit/probe 清单后退出")
    ap.add_argument("--only", action="append", default=None, help="只跑指定脚本（可重复）")
    ap.add_argument("--timeout", type=int, default=300, help="每个脚本的时限（秒）")
    ap.add_argument("--verbose", action="store_true", help="通过也打印完整输出")
    args = ap.parse_args(argv)

    if args.list:
        for n in SUITE:
            print(n)
        return 0

    names = args.only or SUITE
    unknown = [n for n in names if n not in SUITE and n not in PROBE]
    if unknown:
        print(f"[FAIL] 不在清单里的脚本：{unknown}")
        return 2

    data_root = tempfile.mkdtemp(prefix="luhui-ci-")
    bad: list[tuple[str, str]] = []
    total_checks = 0
    t0 = time.monotonic()
    try:
        for name in names:
            status, rc, out = run_one(name, data_root, args.timeout, args.verbose)
            lines = out.splitlines()
            passes = sum(1 for x in lines if x.lstrip().startswith("[PASS]"))
            fails = sum(1 for x in lines if x.lstrip().startswith("[FAIL]"))
            total_checks += passes
            print(f"[{status.split('(')[0]}] {name:<28} {status}  "
                  f"PASS={passes} FAIL={fails}")
            if rc != 0:
                bad.append((name, out))
    finally:
        shutil.rmtree(data_root, ignore_errors=True)

    for name, out in bad:
        print(f"\n===== {name} 失败输出（末 60 行）=====")
        lines = out.rstrip().splitlines()
        for line in lines[-60:]:
            print(line)
    dt = time.monotonic() - t0
    print(f"\n[FINAL] 离线回归：{len(names) - len(bad)}/{len(names)} 份脚本通过；"
          f"累计 PASS={total_checks}；用时 {dt:.1f}s"
          + ("" if not bad else f"；红：{[n for n, _ in bad]}"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
