# -*- coding: utf-8 -*-
"""一次性诊断（**不是判据**）：退出时线程池还在跑，这一档是"必坏"还是"撞运气"，坏在哪条通道。

盘上已有的三条真读数（都是真跑出来的，不是讲道理讲出来的）：
  ① `data/logs/crash.log` 2026-10-19 22:56:34 那一栏：`app/engine/thumbs.py:114` 的
     `self.sig.done.emit(...)` 抛 `RuntimeError: wrapped C/C++ object of type _JobSignals
     has been deleted`，紧接着 `:117` 那句**用来兜住上一条**的 except 分支自己又抛同一句
     ——第二抛没人接，就从 `run()` 逃进工作线程。
  ② `tests/out/probe-job-run-034040.txt` 的 C 档：子进程排 3 趟真活（含两份 4K）、第 0.4s
     `sys.exit` ⇒ **rc=0xC0000409（failfast）**。
  ③ 同一只探针紧接着用 2 趟的形状重跑（V1）⇒ **rc=0，干净退出**，但用了 3.03s（池子被等空了）。
  ④ 同一只探针的 B 档：还在跑就 `del store` ⇒ **主线程挂死**；faulthandler 每 45s 留一次栈，
     工作线程停在 `thumbs.py:78 os.makedirs`（要 GIL），主线程停在拆 store 那一行（持 GIL 等池子）。

②③一比，"在途退出"不是必然坏，是 **race**：成不成看收尾那一刻工作线程正好停在哪儿、
还剩几趟没跑。既然是 race，一次读数不算数——要看**分布**（记忆里"n=1 的高分不算证据"
同一口径，反过来也成立：n=1 的红灯也不算证据）。
另外②那一趟里有 3 趟活（2 份 4K），③只有 2 趟——"排在队里还没起跑的"会不会才是关键，
本趟用 R2／R3 两组把它分开量。

第一版还踩过一个更低级的坑，留在这里当反面教材：四组共用一份缓存目录，第二组起
`_load_meta` 直接命中、`request` 压根没起活，`active=0` 一路被读成"干净退出"——那是**量具失灵**。
所以这里每一趟单独 `mkdtemp`，且每趟子进程自己报 `PREM-FAIL`（没把活排进去就 exit 7）。

五组：
  R2 busy×2活 ×6：排 2 趟，第 0.4s 直接 sys.exit
  R3 busy×3活 ×6：排 3 趟（多一份 4K，队里压着没起跑的），同样第 0.4s 退出
  S  drain      ×3：与 R3 同排法，但先把池子排空再退 ⇒ 对照组，必须每次 rc=0；
                   S 里只要出现一次非 0，"在途退出"就不是差异来源，全部读数作废
  T  raise      ×2：阳性对照——工作线程里**故意抛**，看 `threading.excepthook` 的钩子文件
                   抓不抓得到。抓不到 ⇒ R/U 组的"钩子空"不能当成"没异常"
  U  busy+钩子  ×3：与 R3 同形状但钩子开着 ⇒ 真坏了时看它走的是异常通道还是硬 failfast

2026-10-20 加了两组（#109 的修复上线之后，"前后分布"必须在这同一台、同一把尺上量）：
  D  收口再退 ×8：与 R3 同排法，但退出前先调**产品真用的那句** `poolkit.shutdown(1500)`
                  ⇒ 期望 0 次 failfast、0 挂、钩子空、且收口用时看得见
  E  同 D 摘桩 ×3：把 `poolkit.shutdown` 换成空操作——D 的干净必须由"E 会翻红"来背书，
                  否则 0/8 可能只是这一台恰好又跑赢了 race（假绿光）。
                  2026-10-20 实测：E **并不翻红**（3/3 干净退出，但退出用掉 6.4~6.6s，
                  D 只用 0.8~1.0s）⇒ E 证明的是"收口买的是退出耗时＋不留残留子进程"，
                  **不是**"收口挡住了崩"；原先把它当崩的阳性对照是错的判据，已降级。
  F  只摘 pin   ×3：保留吞异常的 emit ⇒ 看第二道兜底接不接得住（看它吞掉的回报计数）
  G  回修前形状 ×6：pin 与 emit **一起**摘干净（"冗余双兜底 ⇒ 变异要整项能力摘干净"）
                  ⇒ 这才是"崩"这条通道的阳性对照：G 不翻红，D 的 0/N 就只能写成
                  "这一台此刻复现不了"，不许写成"修好了"。

跑法：python -u tests/_probe_exit_crash.py
退出码：0 五组都有读数；3 前提不成立；1 探测自己崩。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

DELETED = re.compile(r"wrapped C/C++ object of type (\w+) has been deleted")
FAILFAST = 0xC0000409

CHILD = r'''
import os, sys, time
HERE, cache, mode, hold, nj, hookfile = sys.argv[1:7]
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
# 卡住时把**每条线程的栈**留在 stderr 里（2026-10-20 实测 D 组出现过 120s 不退出，
# 光看 rc 只知道"挂"，不知道挂在哪一句——猜是猜不出来的，让栈自己说话）。
import faulthandler
faulthandler.dump_traceback_later(20, repeat=True, exit=False)


def note(txt):
    if not hookfile:
        return
    with open(hookfile, "a", encoding="utf-8") as f:
        f.write(txt + "\n")
        f.flush()
        os.fsync(f.fileno())


if hookfile:
    import threading
    _default = sys.excepthook
    sys.excepthook = lambda et, ev, tb: (note("MAIN %s: %s" % (et.__name__, ev)),
                                         _default(et, ev, tb))
    threading.excepthook = lambda a: note("THREAD %s: %s" % (a.exc_type.__name__, a.exc_value))
from PyQt5.QtWidgets import QApplication
from app import config as cfgmod
from app.engine import thumbs as tb
app = QApplication.instance() or QApplication(sys.argv[:1])
if mode == "raise":                      # T：工作线程里真抛一次（钩子的阳性对照）
    def boom(self):
        raise ValueError("注入：工作线程里抛")
    tb.ThumbJob.run = boom
if mode.startswith("shutdown") or mode == "prefix":   # D/E/F：走**修复后产品真正那条收口路**（closeEvent 的同款调用）
    from app.engine import poolkit
    if mode == "shutdown-stub":      # E：把收口这腿摘成空操作——修复的承重件必须能被摘红
        poolkit.shutdown = lambda *a, **k: {}
    if mode == "shutdown-nopin":     # F：只摘 pin（保留吞异常的 emit）——看第二道兜底接不接得住
        poolkit.pin = lambda job: False
    if mode == "prefix":             # G：整项能力摘干净（pin + emit 一起回修前形状）＝崩的阳性对照
        poolkit.pin = lambda job: False
        poolkit.emit = lambda sig, name, *a: getattr(sig, name).emit(*a)
store = tb.ThumbStore(cfgmod.FFMPEG_EXE, cache, max_jobs=1)
ALL = [("uhd/h264-4k.mp4", 60.0), ("gelu-sample-long.mp4", 32.0), ("uhd/hevc-4k10.mp4", 60.0)]
want = int(nj)
jobs = [(os.path.join(HERE, "samples", *n.split("/")), d, 1920) for n, d in ALL][:want]
jobs = [(p, d, w) for p, d, w in jobs if os.path.exists(p)]
keys = [store.request(p, d, w) for p, d, w in jobs]
# 前提：这一趟**必须真的把活排进去了**（每趟一份新缓存，所以不该命中）
if len(keys) != want or len(store._active) != want:
    print("PREM-FAIL 排了%d/%d 在册=%d" % (len(keys), want, len(store._active)), flush=True)
    sys.exit(7)
print("IN 排了%d趟 active=%d 在册=%d" % (len(keys), store._pool.activeThreadCount(),
                                        len(store._active)), flush=True)
if mode == "drain":
    t0 = time.monotonic()
    while store._active and time.monotonic() - t0 < 60.0:
        app.processEvents()
        time.sleep(0.02)
    print("IN 排空用时%.2fs 还在册=%d" % (time.monotonic() - t0, len(store._active)), flush=True)
else:
    time.sleep(float(hold))
    print("IN 退出前 active=%d 在册=%d" % (store._pool.activeThreadCount(), len(store._active)),
          flush=True)
    if mode.startswith("shutdown") or mode == "prefix":
        t0 = time.monotonic()
        r = poolkit.shutdown(1500)
        st = poolkit.stats()
        print("IN 收口用时%.2fs 读数=%s 还在册=%d 还在跑=%d"
              % (time.monotonic() - t0, r, len(store._active), store.running()), flush=True)
        print("IN stats swallowed=%s pinned=%s children=%s stuck=%s shutdowns=%s"
              % (st.get("swallowed"), st.get("pinned"), st.get("children"),
                 st.get("stuck"), st.get("shutdowns")), flush=True)
note("EXIT-REACHED")
sys.exit(0)
'''


def one(mode: str, hold: str, nj: int, hook: bool, tag: str) -> dict:
    cache = tempfile.mkdtemp(prefix="gelu-ec-")          # 每趟一份：缓存命中会让整组变瞎
    hf = os.path.join(cache, "hook.txt") if hook else ""
    so, se = os.path.join(cache, "out.txt"), os.path.join(cache, "err.txt")
    t0 = time.monotonic()
    try:
        with open(so, "wb") as fo, open(se, "wb") as fe:
            p = subprocess.Popen([sys.executable, "-u", "-c", CHILD, HERE, cache, mode,
                                  hold, str(nj), hf],
                                 stdout=fo, stderr=fe, cwd=ROOT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while p.poll() is None and time.monotonic() - t0 < 120.0:
                time.sleep(0.2)
            hung = p.poll() is None
            if hung:
                p.kill()
                p.wait(10)
        rc = p.returncode
        # Windows 的致命退出码从 Popen 回来是**负数**（0xC0000409 ⇒ -1073740791），
        # 先按 32 位无符号折算再查，否则最要紧那条会被读成一个陌生数。
        u = rc & 0xFFFFFFFF if rc is not None else None
        txt = open(so, encoding="utf-8", errors="replace").read() + \
            open(se, encoding="utf-8", errors="replace").read()
        hooktxt = (open(hf, encoding="utf-8", errors="replace").read()
                   if hf and os.path.exists(hf) else "")
        m_inf = re.search(r"退出前 active=\d+ 在册=(\d+)", txt)
        m_dr = re.search(r"收口用时(\d+\.\d+)s", txt)
        m_sw = re.search(r"stats swallowed=(\d+)", txt)
        return {"tag": tag, "rc": u, "hung": hung, "s": round(time.monotonic() - t0, 2),
                "premise_ok": "PREM-FAIL" not in txt and "IN " in txt,
                # 退出那一刻**在册**的作业数：D/E 组靠它证明"收口确实是在有活的时候做的"，
                # 否则"0 次崩"可能只是因为那 0.4s 里活早就跑完了（假绿光）。
                # 没有这一行（S 组）记 -1，不参与判定。
                "inflight": int(m_inf.group(1)) if m_inf else -1,
                "drain_s": float(m_dr.group(1)) if m_dr else -1.0,
                "swallowed": int(m_sw.group(1)) if m_sw else -1,
                # 挂住时把 stderr 尾巴（faulthandler 的栈）原样带回来——判决要能点名"卡在哪一句"
                "stack": txt[-1500:] if hung else "",
                "deleted": bool(DELETED.search(txt)),
                # 只数异常那两类：EXIT-REACHED 是"钩子装上了且走到退出"的前提读数，
                # 把它算进来会让"没异常"也恒 ≥1，判据当场变瞎。
                "hook_exc": len([l for l in hooktxt.splitlines()
                                 if re.match(r"^(MAIN|THREAD) ", l)]),
                # 异常那句**原文**：R/U 的 deleted=0 已经说明崩的不是 `_JobSignals has been
                # deleted`，那到底是哪一句，只有把原文捞出来才知道，也才知道该往哪儿修。
                "hook_lines": " ⏐ ".join(l.strip() for l in hooktxt.splitlines()
                                          if re.match(r"^(MAIN|THREAD) ", l))[:400],
                "hook_reached": "EXIT-REACHED" in hooktxt,
                "says": " ⏐ ".join(l.strip() for l in txt.splitlines() if l.strip())[:140]}
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def group(mode: str, hold: str, nj: int, hook: bool, n: int, tag: str) -> list:
    rows = []
    for i in range(n):
        r = one(mode, hold, nj, hook, f"{tag}{i + 1}")
        rows.append(r)
        rc_txt = f"0x{r['rc']:08X}" if r["rc"] is not None else "没退"
        print(f"  {r['tag']:6} rc={rc_txt} hung={int(r['hung'])} 前提={int(r['premise_ok'])}"
              f" 在册={r['inflight']} 收口={r['drain_s']}s 吞={r['swallowed']}"
              f" deleted={int(r['deleted'])} 钩子异常={r['hook_exc']}"
              f" 钩子走到={int(r['hook_reached'])} {r['s']}s ｜ {r['says']}"
              f" ｜ 钩子原文：{r['hook_lines']}", flush=True)
        if r["hung"]:
            print("      └ 卡住时的栈（faulthandler，stderr 尾巴）：\n"
                  + "\n".join("        " + l for l in r["stack"].splitlines()), flush=True)
    return rows


def main() -> int:
    import app.config as cfgmod
    need = {"ffmpeg": cfgmod.FFMPEG_EXE,
            "4K(h264)": os.path.join(HERE, "samples", "uhd", "h264-4k.mp4"),
            "长样片": os.path.join(HERE, "samples", "gelu-sample-long.mp4"),
            "4K(hevc)": os.path.join(HERE, "samples", "uhd", "hevc-4k10.mp4")}
    miss = [k for k, p in need.items() if not os.path.exists(p)]
    if miss:
        print(f"[前提不成立] 盘上缺：{miss}")
        return 3
    only = ""
    for a in sys.argv[1:]:
        if a.startswith("--only"):
            only = a.split("=", 1)[1] if "=" in a else ""
    def want(tag: str) -> bool:
        return not only or tag in [x.strip().upper() for x in only.split(",")]
    n_u = 6 if want("U") else 3
    n_d = (int(os.environ.get("GELU_EC_D", "8")) if want("D") else 0)
    print(f"[{time.strftime('%H:%M:%S')}] 起步：R2×{6 if want('R2') else 0} "
          f"R3×{6 if want('R3') else 0} S×{3 if want('S') else 0} T×{2 if want('T') else 0} "
          f"U×{n_u} D×{n_d} E×{3 if want('E') else 0} F×{3 if want('F') else 0} "
          f"G×{6 if want('G') else 0}（每一趟一份新缓存）；--only={only or '全跑'}", flush=True)
    r2 = group("busy", "0.4", 2, False, 6, "R2") if want("R2") else []
    r3 = group("busy", "0.4", 3, False, 6, "R3") if want("R3") else []
    s = group("drain", "0", 3, True, 3, "S") if want("S") else []
    t = group("raise", "1.0", 1, True, 2, "T") if want("T") else []
    u = group("busy", "0.4", 3, True, n_u, "U") if want("U") else []
    # D：修复后产品真走的那条路（closeEvent 里那句 poolkit.shutdown(1500) 的同款调用）
    # E：D 的**阳性对照**——把 shutdown 摘成空操作。E 若也全干净，D 的"0 次崩"就
    #    分不清是"修好了"还是"这一台本来就运气好"，那 D 的读数不作数。
    d = group("shutdown", "0.4", 3, True, n_d, "D") if want("D") else []
    e = group("shutdown-stub", "0.4", 3, True, 3, "E") if want("E") else []
    f = group("shutdown-nopin", "0.4", 3, True, 3, "F") if want("F") else []
    g = group("prefix", "0.4", 3, True, 6, "G") if want("G") else []

    rows = r2 + r3 + s + t + u + d + e + f + g
    if not rows:
        print("[前提不成立] --only 一个组都没点上，没读数")
        return 3
    bad = [x["tag"] for x in rows if not x["premise_ok"]]
    if bad:
        print(f"[前提不成立] 这些趟没把活排进去：{bad} ⇒ 读数不作数", flush=True)
        return 3

    ff = lambda g: [x["tag"] for x in g if x["rc"] == FAILFAST]
    nonzero = lambda g: [f"{x['tag']}=0x{x['rc']:08X}" for x in g if x["rc"] not in (0, FAILFAST)]
    bad_s = [x["tag"] for x in s if x["rc"] != 0 or x["hung"]]
    hook_t = sum(x["hook_exc"] for x in t)
    del_rows = [x["tag"] for x in r2 + r3 + u if x["deleted"]]
    exc_u = [x["tag"] for x in u if x["hook_exc"]]
    hung_all = [x["tag"] for x in rows if x["hung"]]

    print("-" * 78, flush=True)
    print(f"  R2（2 活·在途退出）：failfast {ff(r2) or '无'}／其他非 0 {nonzero(r2) or '无'}", flush=True)
    print(f"  R3（3 活·在途退出）：failfast {ff(r3) or '无'}／其他非 0 {nonzero(r3) or '无'}", flush=True)
    print(f"  S （排空再退）：不干净的 {bad_s or '无'}", flush=True)
    print(f"  T （注入线程异常）：钩子抓到 {hook_t} 条", flush=True)
    print(f"  U （在途退出＋钩子）：failfast {ff(u) or '无'}，钩子有异常的 {exc_u or '无'}，"
          f"读到 deleted 那句 {del_rows or '无'}", flush=True)
    print(f"  挂住没退的：{hung_all or '无'}", flush=True)
    if d or e or f or g:
        dirty = lambda x: (x["rc"] != 0 or x["hung"] or x["deleted"] or x["hook_exc"])
        bad_d = [x["tag"] for x in d if dirty(x)]
        e_red = [x["tag"] for x in e if dirty(x)]
        g_red = [x["tag"] for x in g if dirty(x)]
        f_swallow = [f"{x['tag']}={x['swallowed']}" for x in f if x["swallowed"] > 0]
        f_red = [x["tag"] for x in f if dirty(x)]
        no_live = [x["tag"] for x in d + e + f + g if x["inflight"] < 1]
        slow_d = [f"{x['tag']}={x['drain_s']}s" for x in d if x["drain_s"] > 4.0]
        print(f"  D （收口后再退·3 活在册）：不干净的 {bad_d or '无'}／收口超 4s {slow_d or '无'}",
              flush=True)
        print(f"  E （同形状但摘掉收口）：翻红的 {e_red or '无'}"
              f"／退出耗时 {sorted(x['s'] for x in e)}", flush=True)
        print(f"  F （只摘 pin，留吞异常）：翻红 {f_red or '无'}／吞掉的回报条数 {f_swallow or '无'}",
              flush=True)
        print(f"  G （pin+emit 一起回修前形状）：翻红的 {g_red or '无'}", flush=True)
        print(f"  前提：退出那一刻在册<1 的趟 {no_live or '无'}（这些趟没在『有活』时收口，不作数）",
              flush=True)
        d_live = [x for x in d if x["inflight"] >= 1]
        bad_live = [x["tag"] for x in d_live if dirty(x)]
        if not d_live:
            print("  ⇒ D 组没有一趟是『带着活收口』的 ⇒ 修复有效性没量到（量具失灵，不是产品坏）", flush=True)
        elif not g_red:
            print("  ⇒ 阳性对照 G 全干净：把 pin+emit 整项摘回修前形状也没崩 ⇒ 这一台此刻复现不了崩，"
                  "D 的『0 次崩』分不清是修好了还是本来就不坏 ⇒ 有效性未证（只能引用历史分布 "
                  "8/15：tests/out/probe-exit-crash-*）", flush=True)
        elif not bad_live:
            print(f"  ⇒ 修好了：D {len(d_live)} 趟在途收口全干净（rc=0／不挂／钩子空／无 deleted），"
                  f"而摘干净（G）翻红 {len(g_red)}/{len(g)} 趟 ⇒ 崩的承重件是 pin/emit 这一对；"
                  f"收口的贡献看 E：不排空时退出要用 {sorted(x['s'] for x in e)} 秒，排空只用 "
                  f"{sorted(x['s'] for x in d_live)} 秒", flush=True)
        else:
            print(f"  ⇒ 没修好：D 里仍有不干净的 {bad_live}", flush=True)
        if f and not f_red and not f_swallow:
            print("  ⇒ 注意 F（只摘 pin）既没崩也没记录到吞掉的回报：说明这一趟根本没走到"
                  "『信号壳被删』那一步，F 对『哪一条兜底在接活』不作数", flush=True)
        elif f_swallow:
            print(f"  ⇒ F 量到了第二道兜底真在接活：只摘 pin 后 emit 抛出的那条被吞掉并计数"
                  f"（{f_swallow}），进程仍干净退出 ⇒ 两条兜底各自都能挡崩，摘干净才露崩", flush=True)
    n_ff = len(ff(r2)) + len(ff(r3)) + len(ff(u))
    n_all = len(r2) + len(r3) + len(u)
    if not s or not t:
        print("  ⇒ 这次 --only 没点齐 S 或 T（对照与阳性对照）⇒ 只看捞出来的原文，"
              "不作『必坏／不坏』的判决；全跑一趟再定", flush=True)
    elif bad_s:
        print("  ⇒ 对照组 S 自己就不干净：差异不来自『在途退出』，本趟全部读数作废", flush=True)
    elif not hook_t:
        print("  ⇒ 阳性对照 T 没抓到：这把尺读不了异常通道，U 组的『钩子空』不能当『没异常』；"
              "R/S 的 rc 分布仍然作数（那条不吃钩子）", flush=True)
    elif n_ff or del_rows:
        chan = "走异常通道（钩子有货）" if exc_u else "硬 failfast（钩子一行没写）"
        rate = f"{n_ff}/{n_all}"
        print(f"  ⇒ 量到了：在途退出把进程推进 failfast 的有 {rate} 趟，机制是{chan}；"
              f"队里压着没起跑的那份是否加重，看 R2 与 R3 各自的次数", flush=True)
    else:
        print(f"  ⇒ 这一台没量到：在途退出 {n_all} 趟全干净 ⇒ #109 那句『退出必崩』只能写成"
              f"『crash.log ①那一次是真的，但这一台 {n_all} 趟复现不了』", flush=True)
    print("[FINAL] 探测 rc=0（五组读数齐；判决看上面四选一）", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        traceback.print_exc()
        print(f"[FINAL] 探测自己崩：{type(exc).__name__}: {exc} ⇒ rc=1", flush=True)
        raise SystemExit(1)
