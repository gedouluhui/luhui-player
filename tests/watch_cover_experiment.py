"""看门狗（R9）：等这块屏真的能出像素了，把"外来窗口遮挡 ⇒ 记 ENV 不记 FAIL"这条修法
拿到**真窗口**下再看一遍，然后把结论写进哨兵，别把尾巴留给人。

为什么要有它（第 30 条那条修法的验证缺口）：`test_ui.py --falsify` 的 U57 三条腿用的是
**接线桩**（把 `_hwnd_chain_at`／`_exempt_blocker` 调出来），它证的是"分流没接反"；
证不了"真有一扇别人的窗压上来时，收尾码真的是 3、汇总里真的没有失败项"。
本机 2026-10-11 02:14 试过一趟真遮挡（`tests/probe_foreign_cover.py` 起了 pid 32720 那扇灰窗），
结果那一趟在开跑的 `capture_health()` 上就判"屏幕抠不出像素"、整轮提前中止（rc=3 但**根本没走到
视频区那一条**）——那一轮的屏是暗的，不算这一条的验证。所以这条尾巴得等屏亮再跑，
而且**必须验到"真的走到了那一行"**才算数：只看 rc=3 会把"早退"当成"分流失效"。

判据三条（缺一即本轮不作数、继续等下一趟）：
  ① 日志里出现 `[ENV] 视频区在屏幕上真的有画面…`；
  ② 同一行**不许**以 `[FAIL]` 出现；
  ③ 汇总必须是"无失败"或"失败项：[]"，且 rc==3。
另外挡一道早退：日志若含"中止：屏幕此刻抠不出像素"就是那趟屏又暗了，记 SKIP 后重试。

跑法（后台，8 小时上限）：    python tests/watch_cover_experiment.py
日志：tests/out/watch-cover-exp.log（每 60 秒记一次此刻的屏幕读数）
哨兵：tests/out/cover-exp-done.flag（每趟一行 rc/verdict；没有它 = 还没验成）
"""
from __future__ import annotations

import os
import re
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
MAX_TRIES = 12          # 一趟真遮挡约 90 秒；屏亮着但中途又暗，就换下一趟
COVER_SECS = 200.0      # 遮挡窗的存活时长，必须大于验收一趟的墙钟


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def screen_ready() -> tuple[bool, str]:
    """与产品/test_ui 同口径的那一把尺子（另写一把迟早对不上）。"""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from app.ui import theme
    app.setStyleSheet(theme.qss())
    import test_ui                          # noqa: PLC0415  要先有 QApplication
    test_ui.APP = app
    return test_ui.capture_health()


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def judge(text: str, rc: int) -> tuple[bool, str]:
    """三条判据 + 一道早退闸门，全从**落盘的日志字节**上取，不看跑动时的内存状态。

    汇总那一条要**整段抓**（含"失败项：[…]"这一支）：只匹配"无失败"那一支的话，
    真出现失败项时正则会失配，读起来就像"这轮没跑到汇总"——那是把产品红误报成没证据，
    恰好是这一条修法要防的那类混淆（已知答案对照：人造的 fail-list 样本必须判 False 且点名）。
    """
    if "中止：屏幕此刻抠不出像素" in text:
        return False, "SKIP（那趟屏又暗了，整轮早退，根本没走到视频区那一条）"
    hit_env = bool(re.search(r"^\[ENV\] 视频区在屏幕上真的有画面", text, re.M))
    hit_fail = bool(re.search(r"^\[FAIL\] 视频区在屏幕上真的有画面", text, re.M))
    m = re.search(r"---- 汇总：(\d+)/(\d+) 通过；(无失败|失败项：.*?)\s*----", text)
    clean = bool(m) and m.group(3).strip() in ("无失败", "失败项：[]")
    ok = hit_env and not hit_fail and rc == 3 and clean
    detail = (f"ENV={hit_env} FAIL={hit_fail} rc={rc} "
              f"汇总={m.group(3).strip() if m else '没走到汇总'}"
              + (f"（{m.group(1)}/{m.group(2)}）" if m else ""))
    return ok, detail


def one_try(n: int) -> tuple[bool, str, int]:
    log_path = os.path.join(OUT_DIR, f"accept-ui-r9-covered-try{n:02d}.log")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cover = subprocess.Popen([sys.executable, os.path.join(HERE, "probe_foreign_cover.py"),
                              str(COVER_SECS)], cwd=ROOT, env=env,
                             stdout=open(os.path.join(OUT_DIR, "probe-foreign-cover.log"),
                                         "w", encoding="utf-8", errors="replace"),
                             stderr=subprocess.STDOUT)
    try:
        time.sleep(2.0)                     # 让那扇窗真的落到 Z 序顶上
        log(f"try{n:02d}：遮挡窗 pid={cover.pid} 已起，开跑验收 → {os.path.basename(log_path)}")
        with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
            rc = subprocess.call([sys.executable, os.path.join(HERE, "test_ui.py")],
                                 cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    finally:
        cover.kill()                        # 只杀**本脚本自己起的这一只 pid**
        cover.wait()
    text = read(log_path)
    ok, detail = judge(text, rc)
    log(f"try{n:02d}：rc={rc} {detail}")
    if ok:
        tail = [l for l in text.splitlines() if l.startswith("[ENV] 视频区")][:1]
        log("   那一行原文：" + (tail[0][:260] if tail else "(没抓到)"))
    return ok, detail, rc


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    done = os.path.join(OUT_DIR, "cover-exp-done.flag")
    if os.path.exists(done):
        os.remove(done)                     # 幂等：旧哨兵不许冒充这一趟的结论
    ok, detail = screen_ready()
    log(f"起步先看屏：{'能用' if ok else '不能用'} —— {detail[:200]}")
    while not ok:
        if time.monotonic() >= DEADLINE:
            log("超时 8 小时屏仍抠不出像素：这条验证没跑，不是跑败了。最后一次读数见上一行。")
            return 3
        time.sleep(POLL_SECS)
        ok, detail = screen_ready()
        log(f"等待中（起步）：{'能用' if ok else '还不能'} —— {detail[:160]}")
    results = []
    for n in range(1, MAX_TRIES + 1):
        ok2, d2 = screen_ready()
        log(f"try{n:02d} 之前复查屏幕：{'能用' if ok2 else '不能用'} —— {d2[:160]}")
        if not ok2:
            results.append((n, None, "SKIP（开跑之前屏又暗了）"))
            if time.monotonic() >= DEADLINE:
                break
            time.sleep(POLL_SECS)
            continue
        hit, detail, rc = one_try(n)
        results.append((n, rc, detail))
        if hit:
            log(f"验到了（try{n:02d}）：外来窗口遮挡这一趟记的是 ENV、汇总无失败、rc=3")
            break
        if rc == 0:
            log("   那一趟根本没挡住（rc=0＝屏幕全程没被压）：换下一趟，别把'没发生'当成'验过了'")
        if time.monotonic() >= DEADLINE:
            log("到 8 小时上限，停在这一趟。")
            break
        time.sleep(POLL_SECS)
    with open(done, "w", encoding="utf-8") as fh:
        fh.write(f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        for n, rc, d in results:
            fh.write(f"try{n:02d}: rc={rc if rc is not None else '未跑'} {d}\n")
    got = any(d.startswith("ENV=True") for _n, _rc, d in results)
    log("FINAL " + ("VERIFIED" if got else "NOT-VERIFIED")
        + f" 趟数={len(results)} 哨兵={done}")
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
