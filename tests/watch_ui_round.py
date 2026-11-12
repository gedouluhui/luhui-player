"""看门狗：等这块屏真的能出像素了，自己把吃真抓屏的那几趟补跑完，别把尾巴留给人。

为什么要它：`tests/test_ui.py` 的像素判据与 `tests/capture_sweep.py` 的逐屏留档都吃真抓屏。
显示器休眠／锁屏时整屏抠出来全是 0，`capture_health()` 会判"本轮不作数"（rc=3），
于是台账停在半截。停在半截不等于做完了，也不等于产品坏了——它就是环境没给东西。
所以这一趟不硬跑，写个看门狗等它给。

跑法（后台，8 小时上限；R15 八趟：UI 证伪 → UI 验收 → **UI 验收再来一趟**（#73 那条红已定性成
产品坏并修掉，这一趟改问"修好之后是不是每趟都绿"，理由写在 `ROUNDS` 里）→ 逐屏抓档标准档 → 1.3 档 →
1.8 档 → 字号/药丸读数 → 截图工坊真机验收（补 §6 第 34 条欠的那笔『实跑数』账）：
    python tests/watch_ui_round.py            # 屏幕一亮就跑，跑完写 DONE 哨兵
日志：tests/out/watch-ui-r15.log（每 60 秒记一次"此刻的屏幕读数"，超时也留着可诊断的最后一行；
      r13 之前那份同名文件 `watch-ui-round.log` 不再覆写，已另抄为
      `watch-ui-round-pre-r15-20261013-0141.log`，两份都在盘上）
      各趟自己的日志在 tests/out/{falsify,accept}-ui-r15.log、tests/out/accept-ui-r15b.log、
      tests/out/sweep-ui-r15*.log、tests/out/probe-type-r15.log、tests/out/gif-real-r15.log
哨兵：tests/out/ui-round-done.flag（里面是八趟各自的 rc；没有它 = 还没跑完，
      某一行写"跳过" = 那一趟等到超时屏幕仍不给，同样不作数）

R7 比 R5 多两趟档位，是为了 #49 那笔账：`content_width` 的滚动条兜底
（`bar = max(排过版的实时差值, 滚动条自己的 sizeHint)`）当初是在**没跑 `theme.install`
的探针进程**里量出来的（构造期 378 vs 排过版 402，行尾胶囊「1.00x」需 45px 只露 40px）。
同一处算法在出厂装配（`theme.install`）下量到的是需求 306／内容块 274、滚动条那三条来源
10/10/26（`tests/probe_shape_guards.py`、`tests/probe_scrollbar_allow.py`、
`tests/probe_u52_teeth.py`）——两套装配的数字不能互证。所以这一趟把 1.3 与 1.8 两档
（1.3 是文档里"视口 268 / 内容 304"那笔旧账的档，1.8 是兜底真正吃重的档）在**出厂装配 +
真抓屏**下重跑，让"修完之后还切不切"落到产品口径的日志里，而不是停在探针进程。

判据与产品同口径：屏幕可用性用的是 test_ui.capture_health 那**同一个**度量器
（sample_stats 的"整屏至少两种亮度且最亮 >8"），不是这里另写一把尺子——
两把尺子迟早对不上，那就会有一边报"亮了"另一边报"还是黑的"。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(HERE, "out")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

POLL_SECS = 60.0
DEADLINE = time.monotonic() + 8 * 3600.0
# (参数, 日志名, 脚本名)。八趟**都**要真平台通路：`--screen` 那三条自不必说，
# test_ui 的像素半与逐屏留档同理——屏黑着抓出来的是一排全黑 PNG，
# 那比不抓更坏：黑图会冒充成"看过这一屏"的证据。
ROUNDS = ((("--falsify",), "falsify-ui-r15.log", "test_ui.py"),
          ((), "accept-ui-r15.log", "test_ui.py"),
          # 验收连跑两趟不是浪费时间，是**可复现性**那一问。r14 那趟 165/166，唯一一条红是
          # 「空格暂停：内核 pause 与按钮提示同步」——01:35 已经量死它是**产品坏**
          # （`_play_saying` 写成状态口径，暂停时说「暂停」；见 §6 第 35 条 ①），
          # 而 r13 及更早那六趟的绿读的是**跨线程送达之前的旧话**（旧映射下播放态那句正是「播放」，
          # 恰好长得像判据想要的那句；`_apply` 先写缓存、`sig_pause` 排队到主线程）。
          # 判据已换成 `play_tip_flip`：方向比全文、且必须与按之前那句不同 ⇒ 旧话顶不过去。
          # 所以这两趟现在问的**不是**"哪一边坏"（那一问已经回答完了），而是"修完之后这条是不是
          # 每趟都绿"：两趟都绿 ⇒ 收口；只绿一趟 ⇒ 送达窗口还在决定读数，那要继续修判据而不是改文案。
          # 复用现成的 `test_ui.py`，不改判据来"造"一个绿。
          ((), "accept-ui-r15b.log", "test_ui.py"),
          (("--screen",), "sweep-ui-r15.log", "capture_sweep.py"),
          (("--font-scale", "1.3", "--screen"), "sweep-ui-r15-font1.3.log", "capture_sweep.py"),
          (("--font-scale", "1.8", "--screen"), "sweep-ui-r15-font1.8.log", "capture_sweep.py"),
          # V3/V5 那两把读数尺：字色分档与数值药丸右缘齐平。`QLabel.grab()` 在 offscreen
          # 平台上整张图 alpha 全 0，所以它和上面几趟一样**要真平台通路**；判瞎自己会回 3。
          ((), "probe-type-r15.log", "probe_type.py"),
          # 第 34 条末尾欠的那笔账：`test_gif_real.py` 那句"23 项／12 组／4 项"文档里一直没验
          # （它的静态 `check(` 是 27 处，但静态≠实跑）。这一趟把它真跑一遍，好让 §6 那条能写
          # "实测口径的收尾行"。它同样吃真抓屏（工坊要拍得出图），所以挂在这条链的尾巴上、
          # 和上面几趟共用同一个"每趟开跑前复查屏幕"的闸门。
          ((), "gif-real-r15.log", "test_gif_real.py"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def screen_ready() -> tuple[bool, str]:
    """借用 test_ui 的那把尺子；建 QApplication 之前就得起，否则 grabWindow 没有屏幕可问。"""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from app.ui import theme
    app.setStyleSheet(theme.qss())          # 与 test_ui.main() 同环境，免得量到裸 Qt 外观
    import test_ui                          # noqa: PLC0415  延迟导入：要先有 QApplication
    test_ui.APP = app
    return test_ui.capture_health()


def wait_screen(tag: str) -> tuple[bool, str]:
    """一直等到屏幕给像素；到 8 小时上限仍不给就返回 False，由调用方把这一趟记成"跳过"。"""
    ok, detail = screen_ready()
    while not ok:
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：等到上限仍抠不出像素，这一趟不跑（不是跑败了）。最后一次读数见上一行")
            return False, detail
        time.sleep(POLL_SECS)
        ok, detail = screen_ready()
        log(f"等待中（{tag}）：{'能用' if ok else '还不能'} —— {detail[:160]}")
    return ok, detail


def run_round(args: tuple[str, ...], logfile: str, script: str) -> int:
    path = os.path.join(OUT_DIR, logfile)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, os.path.join(HERE, script), *args]
    log(f"开跑：{' '.join(cmd)}  → {logfile}")
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        rc = subprocess.call(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    tail = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip() for l in fh if l.strip()]
        tail = " | ".join(lines[-3:])
    except OSError:
        pass
    log(f"跑完 rc={rc}：{tail[:300]}")
    return rc


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    done_flag = os.path.join(OUT_DIR, "ui-round-done.flag")
    if os.path.exists(done_flag):
        os.remove(done_flag)               # 幂等：旧哨兵不许冒充这一趟的结论
    ok, detail = screen_ready()
    log(f"起步先看屏：{'能用' if ok else '不能用'} —— {detail}")
    if not ok:
        ok, detail = wait_screen("起步")
        if not ok:
            log("超时 8 小时屏仍抠不出像素：这八趟没跑，不是跑败了。最后一次读数见上一行；"
                "把屏点亮后重跑 `python tests/watch_ui_round.py` 或直接 "
                "`python tests/test_ui.py --falsify` + `python tests/test_ui.py`。")
            return 3
    rcs = {}
    for args, logfile, script in ROUNDS:
        # 每一趟开跑之前都再看一次屏：中途可能又暗下去（暗了就等，等到上限才认跳过）
        ok2, d2 = wait_screen(logfile)
        log(f"开跑 {logfile} 之前复查屏幕：{'能用' if ok2 else '不能用'} —— {d2[:160]}")
        if not ok2:
            rcs[logfile] = None
            log(f"{logfile} 跳过（屏幕不给），不作数；屏亮后重跑看门狗")
            continue
        rcs[logfile] = run_round(args, logfile, script)
    lines = [f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}"]
    for logfile, rc in rcs.items():
        lines.append(f"{logfile}: rc={'跳过（屏幕不给）' if rc is None else rc}")
    with open(done_flag, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log("FINAL " + " ".join(f"{k}={v}" for k, v in rcs.items()) + f" 哨兵={done_flag}")
    real = [v for v in rcs.values() if v is not None]
    return 0 if real and all(v == 0 for v in real) else 1


if __name__ == "__main__":
    sys.exit(main())
