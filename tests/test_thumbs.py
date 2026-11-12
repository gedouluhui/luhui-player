"""时间轴缩略图自检：真跑 ffmpeg，验产物几何、验"这一格真的是那一秒"。

用法：
    python tests/test_thumbs.py            # 正常自检：全部 PASS 才算过
    python tests/test_thumbs.py --falsify  # 证伪：故意喂坏条件，检测器必须判 FAIL/抓到

为什么值得单开一份：缩略图这条链上"看起来有图"和"图对得上时间"是两回事。
裁格几何错一位、缓存键把改过的文件当成旧的、抽帧失败退化成空白——三种都长得像"能用的预览"，
所以主判据用**独立通道的直抽帧**当已知答案对照：同一秒用两条完全不同的路取图
（一条走 sheet 裁格，一条走 ffmpeg 精确单帧），像素均值差必须小。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, ROOT)

from PyQt5.QtGui import QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import thumbs as TH  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
VID = os.path.join(SAMPLES, "gelu-sample-1.mp4")          # 10s @30fps，画面自带秒表
VID2 = os.path.join(SAMPLES, "gelu-sample-2.mp4")         # 6s @60fps
DUR = 10.0
GRAY = QImage.Format_Grayscale8

APP = QApplication.instance() or QApplication(sys.argv)
# 缓存目录单独隔开：这份测试的 prune 会往里下刀，不能碰真播放器的缓存
CACHE = os.path.join(cfgmod.DATA_DIR, "thumbs-test")
results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def pump(seconds: float, pred) -> bool:
    """跑事件循环等条件成立（ThumbStore 的完成信号是跨线程 queued，没人转圈就永远收不到）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def fresh_store(max_jobs: int = 2) -> TH.ThumbStore:
    shutil.rmtree(CACHE, ignore_errors=True)
    os.makedirs(CACHE, exist_ok=True)
    return TH.ThumbStore(cfgmod.FFMPEG_EXE, CACHE, max_jobs=max_jobs)


def gray_bytes(img: QImage) -> tuple[bytes, int, int, int]:
    g = img.convertToFormat(GRAY)
    ptr = g.constBits()
    ptr.setsize(g.sizeInBytes())
    return bytes(ptr.asstring(g.sizeInBytes())), g.bytesPerLine(), g.width(), g.height()


def stats(img: QImage) -> dict:
    """像素统计：Qt 自己算，不引第二套口径（PIL）来判 Qt 画出来的东西。"""
    data, bpl, w, h = gray_bytes(img)
    vals = [data[y * bpl + x] for y in range(h) for x in range(w)]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"mean": mean, "std": var ** 0.5, "distinct": len(set(vals)), "size": (w, h)}


def cell_at(store: TH.ThumbStore, key: str, sec: float):
    pm = store.image_at(key, sec)
    return None if pm is None else pm.toImage()


def direct_frame(path: str, sec: float, width: int = TH.THUMB_W):
    """已知答案对照：和 sheet 那条路完全无关地，单独精确抽这一秒的一帧。

    -ss 放在 -i **之后**（输出侧 seek）才是解码到该时刻的精确帧；放前面是快进到最近
    关键帧——本样片 10 秒只有 2 个关键帧（GOP=250），快进会拿到 0 秒那一帧，对照就白做了。
    这条区别是整个测试的立足点，也是"缩略图到底准不准"这件事的分水岭。
    """
    out = os.path.join(CACHE, f"direct-{int(sec * 100)}.jpg")
    cmd = [cfgmod.FFMPEG_EXE, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", path, "-ss", f"{sec:.3f}", "-frames:v", "1", "-vf", f"scale={width}:-2", out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not os.path.exists(out):
        return None
    img = QImage(out)
    return None if img.isNull() else img


def mean_abs_diff(a: QImage, b: QImage) -> float:
    """灰度均值差：裁错格时这个数会跳到几十，对得上时是个位数。"""
    ga, gb = a.convertToFormat(GRAY), b.convertToFormat(GRAY)
    if ga.size() != gb.size():
        gb = gb.scaled(ga.size())
    ba, bpl_a, w, h = gray_bytes(ga)
    bb, bpl_b = gray_bytes(gb)[:2]
    tot = 0
    for y in range(h):
        for x in range(w):
            tot += abs(ba[y * bpl_a + x] - bb[y * bpl_b + x])
    return tot / float(w * h)


def write_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def fake_entry(name: str, meta: dict | None, ts: float | None = None) -> str:
    d = os.path.join(CACHE, name)
    os.makedirs(d, exist_ok=True)
    if meta is not None:
        m = dict(meta)
        if ts is not None:
            m["ts"] = ts
        write_json(os.path.join(d, "meta.json"), m)
    return d


def lru_probe() -> tuple[bool, str]:
    """合成若干份缓存，逐张取 sheet，验"内存里最多留几整张 + LRU 次序"。

    不碰真产物：每份只有一张 8×8 的合成 PNG，走的是 `_sheet` 的读盘→入内存那条真路。
    先塞 cap+2 张（必须挤掉 2 张），再命中一次最早剩下的那张（必须被挪到队尾），
    最后再塞一张新的——该淘汰的是"刚被碰过那张"后面的，而不是正在看的。
    """
    cache = os.path.join(CACHE, "lru-probe")
    shutil.rmtree(cache, ignore_errors=True)
    os.makedirs(cache, exist_ok=True)
    st = TH.ThumbStore(cfgmod.FFMPEG_EXE, cache)
    cap = TH.ThumbStore.SHEET_CAP
    if not 2 <= cap <= 8:
        return False, f"SHEET_CAP={cap} 不在合理区间 2..8"

    def make_sheet(k: str, shade: int) -> bool:
        d = os.path.join(cache, k)
        os.makedirs(d, exist_ok=True)
        img = QImage(8, 8, QImage.Format_RGB32)
        img.fill(shade)
        return bool(img.save(os.path.join(d, "sheet-01.png")))

    keys = []
    for i in range(cap + 2):
        k = f"lru-{i:02d}"
        if not make_sheet(k, 10 + i * 20):
            return False, f"合成 sheet 落盘失败：{k}"
        keys.append(k)
        st._sheet(k, {}, 0)
    if len(st._sheet_cache) != cap or st.sheet_evictions != 2:
        return False, f"塞 {cap + 2} 张后 cache={list(st._sheet_cache)} 挤出={st.sheet_evictions}"
    if keys[0] in st._sheet_cache or keys[1] in st._sheet_cache:
        return False, f"最旧的没被挤掉：cache={list(st._sheet_cache)}"
    st._sheet(keys[2], {}, 0)                     # 命中：必须挪到队尾
    if not make_sheet("lru-new", 250):
        return False, "合成 sheet 落盘失败：lru-new"
    st._sheet("lru-new", {}, 0)                   # 新的进来：挤掉 keys[3]，不是 keys[2]
    if keys[3] in st._sheet_cache or keys[2] not in st._sheet_cache:
        return False, f"LRU 次序不对（命中没续命）：cache={list(st._sheet_cache)}"
    if len(st._sheet_cache) != cap:
        return False, f"上限失守：cache={list(st._sheet_cache)}"
    return True, f"cap={cap} 挤出={st.sheet_evictions} 留={list(st._sheet_cache)}"


def main() -> int:
    t0 = time.time()
    if not os.path.exists(cfgmod.FFMPEG_EXE):
        check("runtime 里有 ffmpeg", False, cfgmod.FFMPEG_EXE)
        return report(t0)
    check("runtime 里有 ffmpeg", True, os.path.basename(cfgmod.FFMPEG_EXE))

    # 1. 档位：长片不许抽成千张
    step_2h = TH.pick_step(7200.0)
    check("pick_step 让 2 小时的片落在 MAX_THUMBS 内",
          -(-7200 // step_2h) <= TH.MAX_THUMBS,
          f"7200s → step={step_2h}s，张数={-(-7200 // step_2h)}")
    check("pick_step 短片用最密档（10s → 1s/张）", TH.pick_step(10.0) == 1, f"{TH.pick_step(10.0)}")

    # 2. 真抽一次
    store = fresh_store()
    done: list[tuple[str, bool, str]] = []
    store.sig_done.connect(lambda k, ok, d: done.append((k, ok, d)))
    key = store.request(VID, DUR)
    got = pump(90.0, lambda: store.ready(key))
    check("请求后缩略图真的落到盘上", got, f"key={key} 详情={done[-1] if done else '无'}")
    if not got:
        return report(t0)
    meta = store._load_meta(key)
    d = os.path.join(CACHE, key)
    sheets = sorted(f for f in os.listdir(d) if f.startswith("sheet-"))
    check("短片走整解档（没被跳关键帧）", meta.get("keyframe_only") is False,
          f"keyframe_only={meta.get('keyframe_only')}")
    check("meta 记的就是产物实况",
          int(meta["count"]) == 10 and int(meta["sheets"]) == len(sheets) == 1
          and int(meta["step"]) == 1,
          f"count={meta['count']} sheets={meta['sheets']} step={meta['step']} 盘上={len(sheets)}")
    img0 = QImage(os.path.join(d, sheets[0]))
    check("sheet 宽＝COLS×缩略图宽（几何由产物决定，不靠假设）",
          img0.width() == TH.COLS * TH.THUMB_W,
          f"sheet={img0.width()}×{img0.height()} 期望宽={TH.COLS * TH.THUMB_W}")

    # 3. 每格是真画面、且不同时刻不一样
    a, b = cell_at(store, key, 1.5), cell_at(store, key, 6.5)
    sa, sb = stats(a), stats(b)
    flat = QImage(TH.THUMB_W, 90, QImage.Format_RGB32)
    flat.fill(128)
    sf = stats(flat)
    check("两格都有画面（非单色）", sa["distinct"] >= 6 and sb["distinct"] >= 6,
          f"t1.5 颜色{sa['distinct']} 标准差{sa['std']:.1f} ／ t6.5 颜色{sb['distinct']} 标准差{sb['std']:.1f}")
    check("对照组：纯单色必须被同一判据否掉", sf["distinct"] == 1,
          f"单色图 颜色{sf['distinct']} 标准差{sf['std']:.1f}")
    dab = mean_abs_diff(a, b)
    check("两个时刻的缩略图不是同一张（时间在走）", dab > 6.0, f"均值差={dab:.1f}")

    # 4. 主判据：裁出来的那格 = 独立通道精确抽的那一帧
    worst, worst_t, note = 0.0, 0.0, ""
    for t in (1.0, 4.0, 7.0):
        ref = direct_frame(VID, t)
        if ref is None:
            worst, note = 999.0, "直抽帧失败"
            break
        v = mean_abs_diff(cell_at(store, key, t), ref)
        note += f" t{t}:{v:.1f}"
        worst = max(worst, v)
        worst_t = t if v == worst else worst_t
    check("格内容对得上时间（与独立精确抽帧比，均值差<12）", worst < 12.0,
          f"最大差={worst:.1f}（t={worst_t}）{note}")

    # 5. 缓存：第二次请求不得再起进程
    st2 = TH.ThumbStore(cfgmod.FFMPEG_EXE, CACHE, max_jobs=2)
    mtime = os.path.getmtime(os.path.join(CACHE, key, "meta.json"))
    k2 = st2.request(VID, DUR)
    check("同文件同时长命中同一 key", k2 == key, f"{k2} vs {key}")
    check("命中缓存时不再起 ffmpeg（盘上产物没被重写）",
          k2 == key and not st2._active
          and os.path.getmtime(os.path.join(CACHE, key, "meta.json")) == mtime,
          f"active={list(st2._active)}")

    # 6. 键要跟着文件内容与时长变，否则改过的文件会拿到旧预览
    check("时长变了必须换 key", st2.key_for(VID, 11.0) != key, st2.key_for(VID, 11.0))
    check("换了文件必须换 key", st2.key_for(VID2, DUR) != key, st2.key_for(VID2, DUR))
    tmpv = os.path.join(CACHE, "copy.mp4")
    shutil.copy2(VID, tmpv)
    old = st2.key_for(tmpv, DUR)
    with open(tmpv, "ab") as fh:
        fh.write(b"\0" * 1024)
    os.utime(tmpv, (time.time() + 3, time.time() + 3))
    check("同名文件被改动后必须换 key（按 size+mtime 入键）",
          st2.key_for(tmpv, DUR) != old, f"旧={old} 新={st2.key_for(tmpv, DUR)}")

    # 7. 并发节流：4 个请求同时在排，峰值不得超过配额
    #    （不清盘：第 9 条还要用第 2 步那份产物，清了就变成"测 prune 顺路把上一条测挂了"）
    st3 = TH.ThumbStore(cfgmod.FFMPEG_EXE, CACHE, max_jobs=2)
    for v, du in ((VID, DUR), (VID2, 6.0), (VID2, 12.0), (VID, 20.0)):
        st3.request(v, du)
    peak = 0
    t_end = time.monotonic() + 120.0
    while time.monotonic() < t_end:
        APP.processEvents()
        peak = max(peak, st3.running())      # 数"真的在抽"的，不是"已提交"的（后者永远=4）
        if not st3._active:
            break
        time.sleep(0.02)
    check("并发节流（max_jobs=2 → 同时最多 2 个在抽）", 0 < peak <= 2,
          f"峰值={peak} 剩余={list(st3._active)}")

    # 8. 降级：没有 ffmpeg 时不许抛、不许假装 ready（用一个空缓存目录：
    #    缓存里已经有货的话 ready() 为真才是对的，那条判据就测不到"缺内核"这条路）
    empty = os.path.join(CACHE, "empty-cache")
    os.makedirs(empty, exist_ok=True)
    st4 = TH.ThumbStore(os.path.join(CACHE, "no-such-ffmpeg.exe"), empty, max_jobs=1)
    k4 = st4.request(VID, DUR)
    check("内核缺失时 ready() 为假且不抛",
          st4.ready(k4) is False and st4.image_at(k4, 3.0) is None, f"key={k4}")

    # 9. 越界钳位与清理
    far = cell_at(store, key, 99999.0)
    check("越界时间点钳到最后一格（不是崩、不是空白）",
          far is not None and stats(far)["distinct"] >= 6,
          f"t=99999 → 尺寸{stats(far)['size'] if far else None} "
          f"颜色{stats(far)['distinct'] if far else 0}")
    fake_entry("old-a", meta, ts=1.0)
    fake_entry("old-b", meta, ts=2.0)
    shutil.copy(os.path.join(CACHE, key, "sheet-01.jpg"), os.path.join(CACHE, "old-a", "sheet-01.jpg"))
    dirs_before = [d for d in os.listdir(CACHE) if os.path.isdir(os.path.join(CACHE, d))]
    gone = store.prune(keep=1)
    left = sorted(d for d in os.listdir(CACHE) if os.path.isdir(os.path.join(CACHE, d)))
    check("prune 只留最近 keep 份", gone == len(dirs_before) - 1 and len(left) == 1,
          f"删前{len(dirs_before)}份 → 删了{gone}份，剩{left}")

    # 10. 内存里的整图缓存必须封顶：悬停过多少支片，都不许每支攥一张 5.8MB 的整图
    ok, why = lru_probe()
    check("sheet 记忆是 LRU 有上限的（命中续命，最久没碰的先走）", ok, why)
    return report(t0)


def report(t0: float) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 缩略图自检：{passed}/{len(results)} 通过"
          + (f"；失败：{fails}" if fails else "；无失败")
          + f"，用时 {time.time() - t0:.1f}s ----", flush=True)
    return 0 if passed == len(results) else 1


def falsify() -> int:
    """坏条件必须被抓到：几何错位、陈旧缓存、恒真判据、越界、半成品目录。"""
    print("---- 证伪模式：以下每一条都必须被抓到，否则缩略图自检不可信 ----")
    if not os.path.exists(cfgmod.FFMPEG_EXE):
        print("[SKIP] 没有 ffmpeg，证伪做不了")
        return 1
    store = fresh_store()
    key = store.request(VID, DUR)
    if not pump(90.0, lambda: store.ready(key)):
        print("[SKIP] 主链没跑通，证伪做不了")
        return 1
    meta = store._load_meta(key)
    img = cell_at(store, key, 5.0)
    ref = direct_frame(VID, 5.0)
    if ref is None:
        print("[SKIP] 对照通道（直抽帧）失败，证伪做不了")
        return 1

    # T1：把 meta 的网格当成 5×2（图还是那张 10×10）→ 裁格必须算出完全不同的像素
    bad = fake_entry("bad-grid", meta)
    m = dict(meta, cols=5, rows=2)
    write_json(os.path.join(bad, "meta.json"), m)
    shutil.copy(os.path.join(CACHE, key, "sheet-01.jpg"), os.path.join(bad, "sheet-01.jpg"))
    st_bad = TH.ThumbStore(cfgmod.FFMPEG_EXE, CACHE)
    st_bad._meta["bad-grid"] = m
    pm = st_bad.image_at("bad-grid", 5.0)
    diff = 999.0 if pm is None else mean_abs_diff(pm.toImage(), ref)
    falsified.append(("网格几何错一位时，对照判据必须报大差异", diff >= 12.0, f"均值差={diff:.1f}"))

    # T2：视频被移走/改名后，留着 meta 也不许说"我有"
    ghost = fake_entry("ghost", dict(meta, video=os.path.join(CACHE, "没了.mp4")))
    shutil.copy(os.path.join(CACHE, key, "sheet-01.jpg"), os.path.join(ghost, "sheet-01.jpg"))
    st_g = TH.ThumbStore(cfgmod.FFMPEG_EXE, CACHE)
    falsified.append(("源文件已不存在的缓存必须判为未就绪", st_g.ready("ghost") is False,
                      f"ready={st_g.ready('ghost')}"))

    # T3：尺子本身——同一格自比差异必须正好是 0（不为 0 说明均值差这条判据在胡说）
    same = mean_abs_diff(img, cell_at(store, key, 5.0))
    falsified.append(("同格自比差异必须为 0（否则尺子不可信）", same == 0.0, f"自比={same}"))

    # T4：假单色图必须过不了"有画面"那条判据（防止 distinct>=6 是句空话）
    flat = QImage(TH.THUMB_W, 90, QImage.Format_RGB32)
    flat.fill(200)
    falsified.append(("单色图必须被 has_picture 判据否掉", stats(flat)["distinct"] < 6,
                      f"颜色数={stats(flat)['distinct']}"))

    # T5：sheets 说"一张都没有"时必须回 None，而不是拿上一张糊过去
    st5 = TH.ThumbStore(cfgmod.FFMPEG_EXE, CACHE)
    st5._meta[key] = dict(meta, sheets=0)
    falsified.append(("sheets=0 时取格必须返回 None", st5.image_at(key, 5.0) is None,
                      f"得到 {st5.image_at(key, 5.0)!r}"))

    # T6：越界时间不许返回"第一格"冒充——钳到末格的话，末格和首格必须看得出不同
    #    （必须排在 prune 那条之前：prune(keep=0) 会把产物目录全删了，那之后就取不到格了）
    first, last = cell_at(store, key, 0.4), cell_at(store, key, 99999.0)
    dfl = -1.0 if not first or not last else mean_abs_diff(first, last)
    falsified.append(("越界取到的必须是末格而不是首格", dfl > 6.0,
                      f"首末均值差={dfl:.1f}（-1＝有一头取不到图）"))

    # T7：半成品目录（只有目录没有 meta）不许把 prune 弄崩
    fake_entry("half-done", None)
    n = store.prune(keep=0)
    falsified.append(("prune 遇到半成品目录不许崩", isinstance(n, int) and n >= 1, f"删了 {n} 份"))

    # T8：内存缓存不封顶（修复前的形状）必须被 LRU 判据抓到
    orig_remember = TH.ThumbStore._remember_sheet

    def bad_unbounded(self, key, no, img):
        self._sheet_cache.pop(key, None)
        self._sheet_cache[key] = (no, img)        # 只增不挤

    TH.ThumbStore._remember_sheet = bad_unbounded
    try:
        ok, why = lru_probe()
    finally:
        TH.ThumbStore._remember_sheet = orig_remember
    falsified.append(("sheet 记忆不封顶必须被 LRU 判据抓到", not ok, why))
    falsified.append(("T8b 打桩检查（函数对象真的换了）",
                      orig_remember is not bad_unbounded, ""))

    # T9：有上限但是 FIFO——命中不续命，正在看的那支会被新抽完的挤走
    def bad_fifo(self, key, no, img):
        if key not in self._sheet_cache:
            self._sheet_cache[key] = (no, img)
        while len(self._sheet_cache) > self.SHEET_CAP:
            self._sheet_cache.pop(next(iter(self._sheet_cache)))
            self.sheet_evictions += 1

    TH.ThumbStore._remember_sheet = bad_fifo
    try:
        ok, why = lru_probe()
    finally:
        TH.ThumbStore._remember_sheet = orig_remember
    falsified.append(("命中不续命（FIFO）必须被 LRU 次序判据抓到", not ok, why))
    falsified.append(("T9b 打桩检查（函数对象真的换了）", orig_remember is not bad_fifo, ""))

    miss = [n for n, ok, _ in falsified if not ok]
    for n, ok, d in falsified:
        print(f"[{'CAUGHT' if ok else 'MISSED'}] {n}  —— {d}", flush=True)
    if miss:
        print("!! 以下坏样本没被抓到，缩略图自检不可信：" + ", ".join(miss))
        return 1
    print(f"---- 证伪：{len(falsified)}/{len(falsified)} 条坏样本全部被抓到 ----")
    return 0


if __name__ == "__main__":
    sys.exit(falsify() if "--falsify" in sys.argv else main())
