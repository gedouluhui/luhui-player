# -*- coding: utf-8 -*-
"""R17 收尾一次性核对（throwaway，不进常跑链）。

判据＝文档里本轮写进去的每个数字，回到盘上产物去数一遍。
`--selftest`：把盘上事实喂给同一套判据，另喂 len(MUTATIONS) 份已知坏样本（改过的文档文本），
逐条要求翻红，外加一条 NOP 闸门（注入必须真的改动文本）。判据不许恒真。
量具自己兜输出编码：接终端／`> 文件`／`2>&1` 三种形状都给同一份文本。
"""
import hashlib
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "tests", "out")


def read(p, enc="utf-8"):
    with io.open(p, encoding=enc, errors="replace") as f:
        return f.read()


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def read_any(p):
    """按字节兜编码：UTF-8 严格 → GBK 严格 → UTF-8 replace。

    判据要拿文档里的**中文串**去日志里逐字比，所以读错编码不会报错、只会让一条本来绿的
    判据悄悄变红（或者反过来：只比 ASCII 数字时读错编码也"过"）。哪个编码读通了由 facts
    记进 `enc`，回执里点出来——量具自己也得留证据。
    """
    for enc in ("utf-8", "gbk"):
        try:
            with io.open(p, encoding=enc, errors="strict") as f:
                return f.read(), enc
        except Exception:
            continue
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return f.read(), "utf-8/replace"


# 各轮"不可覆写留档"：文档点名的每一轮都要 md5 对得上；`ROUNDS[-1]` 是**最新那趟**，
# 链趟数、像素腿、环境不给这些口径一律以它为准（旧轮的历史读数不许再当现值）。
ROUNDS = (
    ("r17", "chain-r17.keep-1017-025027.log", "chain-r17-done.keep-1017-025027.flag"),
    ("r18", "chain-r18.keep-1017-095043.log", "chain-r18-done.keep-1017-095043.flag"),
    ("r19", "chain-r19.keep-1019-220225.log", "chain-r19-done.keep-1019-220225.flag"),
)
FINAL_RE = re.compile(r"\[FINAL\] 绿 (\d+)／真失败 (\d+)／环境不给 (\d+)／本轮不作数 (\d+) —— .*?共 (\d+) 趟")
# 真窗口那几本日志自己的收口行（判据只认这两行自打的，不认 grep 数标签）
SUMMARY_RE = re.compile(r"----\s*汇总：(\d+)/(\d+) 通过；(无失败|失败项[^\n]*?)\s*----")
TALLY_RE = re.compile(r"证伪台账自打：(共 \d+ 行 —— CAUGHT \d+／MISSED \d+／ENV \d+／非布尔 \d+)")
VO_KEY_LINE = "换过文件之后媒体信息仍有「输出"      # 6.10 那条真机判据的标题
# 链里那条像素腿的读数**不抄在这里**：它每轮都随判据数漂（r18 是 PASS 16，加一条判据就变 17），
# 手抄的那颗数下一轮必然过期。改成从最新链留档里现取（见 `facts()["pixel"]`），
# 文档必须写着现取到的那一串——两边都能塌，但塌的是"文档与最新留档不符"，不是"常数旧了"。
PIXEL_LEG_HEAD = "[FINAL] 端到端：全绿"       # 精确到这一条腿（别撞"跨进程转交端到端"／"GPU 加速端到端"）
READING_RE = re.compile(r"PASS \d+／FAIL \d+／ENV \d+")   # 文档里"某趟读数"的通用形状
CENSUS_NAME = "probe-file-scoped-table-v4-092438.txt"        # 整张缓存表逐格普查的留档
# 2026-10-19 那笔账的承重件与它的证伪留档：文档抄的 md5 一律回盘上现算，不抄字面。
KEY_PART = os.path.join("app", "engine", "player.py")
DRIFT_NAME = "e2e-loadfile-drift-1019-215401.txt"
# 2026-10-21 全屏换尺那一族：校准三本、收口小册、两张现场图的留底。
# 读数一律从日志/小册现取，文档必须逐字抄到同一串；留底 md5 回盘上现算。
R20_LOGS = ("probe-fullscreen-fit-selfcheck-0024.log",
            "probe-fullscreen-fit-mutate-0024.log",
            "probe-fullscreen-fit-recheck-0024.log")
R20_FINALS = {
    "probe-fullscreen-fit-selfcheck-0024.log": (
        "[FINAL] R20 像素尺自检：共 10 张已知答案，错 0 张 ⇒ rc=0",
        "[FINAL] R20 几何尺自检：共 13 档已知答案，错 0 档 ⇒ rc=0"),
    "probe-fullscreen-fit-mutate-0024.log": (
        "[FINAL] R20 自检证伪：共 12 条，判不到的（MISSED/NOP/CRASH）0 条 ⇒ rc=0",
        "[FINAL] R20 几何尺证伪：共 9 条，判不到的 0 条 ⇒ rc=0"),
    "probe-fullscreen-fit-recheck-0024.log": (
        "[FINAL] 复量：12 张，读不出 0 张 ⇒ rc=0",),
}
R20_ART = "r20-re2-ruler-artifact-20261021.md"
R20_KEEP = {
    "r20-re1-fs-3.00s.keep-1021.png": "68cb5190015c57ce811d96a890012242",
    "r20-re2-fs-8.00s.keep-1021.png": "7c425834127a0caa7e697d7f4d5545fc",
}
# 03:35 那趟为什么"整趟不作数"：它的被测短指纹与当前这一版不同，且点名的那三份逐文件也不同。
# 两串指纹都**现取**（0335 回日志、当前用 build_stamp 现算），判据里一个字面指纹都不手抄。
R20_PROBE = "probe-fullscreen-fit-0335.log"
R20_PROBE_DIFF = ("main_window.py", "video_surface.py", "player.py")
R20_PROBE_PHRASE = "`main_window.py`／`video_surface.py`／`player.py` 三份"
# 离屏状态机那两本（2026-10-21 00:45）：验收 15 条、证伪 7 条。收口行由脚本自打，这里只认原文。
R20_OFFLINE = {
    "fs-chrome-offline-20261021-005307.log":
        "[FINAL] 全屏 chrome 离线状态机：共 20 条判据，红 0 条 ⇒ rc=0",
    "fs-chrome-offline-falsify-20261021-005307.log":
        "[FINAL] 状态机证伪：共 9 条坏实现，判不到的（MISSED/NOP）0 条，基线绿 ⇒ rc=0",
}


def census():
    """普查留档自己的判决：每个桶的「标签＋条数」＋那串行对账算式。

    上一版文档把这两行**缩写**过（省掉两个 0 格的桶，读起来更像结论），所以这里按桶逐条比，
    而不是比整串——整串比会被换行打断，比缩写又核不出"少写了一格"。
    """
    p = os.path.join(OUT, CENSUS_NAME)
    if not os.path.exists(p):
        return [], "", False
    text, _ = read_any(p)
    final = next((l for l in text.splitlines() if "[FINAL] 逐格" in l), "")
    acct = next((l for l in text.splitlines() if "分桶对账" in l), "")
    body = final.split("：", 1)[1] if "：" in final else ""
    buckets = re.findall(r"([\u4e00-\u9fff（）]+) (\d+)", body)
    expr = re.search(r"\d+(?:\+\d+)+=\d+", acct)
    return buckets, (expr.group(0) if expr else ""), os.path.exists(p)


def scan_logs():
    """盘上真窗口日志的自打读数（与文档无关）：汇总行 A/B、判废行数、有没有 6.10、证伪收口行。"""
    import glob
    accepts, falsifies, encs = {}, {}, set()
    vo_lines = []
    for p in sorted(glob.glob(os.path.join(OUT, "accept-ui-*.log"))):
        text, enc = read_any(p)
        encs.add(enc)
        m = SUMMARY_RE.search(text)
        if not m:
            continue
        for ln in text.splitlines():
            if VO_KEY_LINE in ln and ln.startswith("[PASS]"):
                vo_lines.append(ln.split("—— ", 1)[1].strip() if "—— " in ln else ln)
        accepts[os.path.basename(p)] = {
            "ab": f"{m.group(1)}/{m.group(2)}",
            "no_fail": m.group(3) == "无失败",
            "env": len([l for l in text.splitlines() if l.startswith("[ENV]")]),
            "has_vo": VO_KEY_LINE in text,
        }
    for p in sorted(glob.glob(os.path.join(OUT, "falsify-ui-*.log"))):
        text, enc = read_any(p)
        encs.add(enc)
        m = TALLY_RE.search(text)
        if m:
            falsifies[os.path.basename(p)] = m.group(1)
    return accepts, falsifies, sorted(encs), vo_lines


def facts():
    """盘上事实：与文档无关，判据只拿这些数去比文档。"""
    sys.path.insert(0, ROOT)
    from app import doctor
    gpu_log = os.path.join(OUT, "e2e-gpu-1017-024833.log")
    opts_txt = os.path.join(OUT, "mpv-list-options-1017-0306.txt")
    missing = [p for p in (gpu_log, opts_txt, os.path.join(OUT, CENSUS_NAME)) if not os.path.exists(p)]
    cb, ce, _ = census()
    rounds = {}
    for rd, log_n, flag_n in ROUNDS:
        lp, fp = os.path.join(OUT, log_n), os.path.join(OUT, flag_n)
        if not os.path.exists(lp):
            missing.append(lp)
        if not os.path.exists(fp):
            missing.append(fp)
        kl = read(lp) if os.path.exists(lp) else ""
        fin = FINAL_RE.search(kl)
        rounds[rd] = {
            "log": log_n, "flag": flag_n, "text": kl,
            "log_md5": md5(lp) if os.path.exists(lp) else "?",
            "flag_md5": md5(fp) if os.path.exists(fp) else "?",
            "green": fin.group(1) if fin else "?",
            "bad": fin.group(2) if fin else "?",
            "env": fin.group(3) if fin else "?",
            "na": fin.group(4) if fin else "?",
            "legs": fin.group(5) if fin else "?",
        }
    src = read(os.path.join(ROOT, "tests", "chain_r17.py"))
    m = re.search(r"LEGS(?::[^=\n]*)?= \[\s*(.*?)\n\]|\bLEGS(?::[^=\n]*)?= \(\s*(.*?)\n\)", src, re.S)
    legs_block = (m.group(1) or m.group(2)) if m else ""
    # 最新链留档里那条像素腿的读数（现取，不手抄）：取不到就是空串，判据会红在"文档写着的一串
    # 读数在最新留档里找不到"上，而不是红在一颗过期的常数上。
    pixel = next((ln.strip() for ln in rounds[ROUNDS[-1][0]]["text"].splitlines()
                  if ln.strip().startswith(PIXEL_LEG_HEAD)), "")
    ot = read(opts_txt)
    accepts, falsifies, encs, vo_lines = scan_logs()
    part = os.path.join(ROOT, KEY_PART)
    drift = os.path.join(OUT, DRIFT_NAME)
    r20 = {}
    for n in R20_LOGS:
        p = os.path.join(OUT, n)
        if not os.path.exists(p):
            missing.append(p)
        text = read_any(p)[0] if os.path.exists(p) else ""
        r20[n] = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[FINAL]")]
    art_p = os.path.join(OUT, R20_ART)
    if not os.path.exists(art_p):
        missing.append(art_p)
    r20["artifact"] = read(art_p) if os.path.exists(art_p) else ""
    r20["keep_md5"] = {}
    for n in R20_KEEP:
        p = os.path.join(OUT, n)
        if not os.path.exists(p):
            missing.append(p)
        r20["keep_md5"][n] = md5(p) if os.path.exists(p) else "?"
    # 03:35 那趟"量的是哪一版"：短指纹与逐文件指纹回日志现取；当前这一版用 build_stamp
    # 现算（与探针印日志走同一份清单）。两串都进 facts，判据只做"相等/不等"的比较。
    p0335 = os.path.join(OUT, R20_PROBE)
    if not os.path.exists(p0335):
        missing.append(p0335)
    probe_line = ""
    if os.path.exists(p0335):
        probe_line = next((ln for ln in read_any(p0335)[0].splitlines()
                           if ln.lstrip().startswith("[被测]")), "")
    short_m = re.search(r"短=([0-9a-f]{12})", probe_line)
    r20["probe_short"] = short_m.group(1) if short_m else ""
    r20["probe_files"] = dict(re.findall(r"([A-Za-z0-9_]+\.py)=([0-9a-f]{12})", probe_line))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_build_stamp", os.path.join(ROOT, "tests", "build_stamp.py"))
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)
    r20["cur_files"] = {os.path.basename(rel): val[:12] for rel, val in bs.stamps()}
    r20["cur_short"] = bs.stamp_short()
    r20["offline"] = {}
    for n in R20_OFFLINE:
        p = os.path.join(OUT, n)
        if not os.path.exists(p):
            missing.append(p)
            r20["offline"][n] = ""
            continue
        text = read_any(p)[0]
        r20["offline"][n] = next((ln.strip() for ln in text.splitlines()
                                  if ln.strip().startswith("[FINAL]")), "")
    return {
        "missing": missing,
        "n_checks": len(doctor.CHECKS),
        "check_names": [c[0] for c in doctor.CHECKS],
        "rounds": rounds,
        "latest": rounds[ROUNDS[-1][0]],
        "legs_src": str(len(re.findall(r'^\s*\("', legs_block, re.M))),
        "pixel": pixel,
        "part_md5": md5(part) if os.path.exists(part) else "?",
        "drift_exists": os.path.exists(drift),
        "drift_text": read(drift) if os.path.exists(drift) else "",
        "opts_md5": md5(opts_txt),
        "gpu": read(gpu_log, "gbk"),
        "opts_lines": str(len(ot.splitlines())),
        "ngu": str(ot.count("ngu-")),
        "ancedx4": str(ot.count("ancedx4")),
        "opts_name": "mpv-list-options-1017-0306.txt",
        # 「recent 有没有界面读它」这条本轮订正过的话，必须回盘上数：
        "recent_artifact": "recent-consumers-1017-0322.txt",
        "recent_ui_reads": _count_recent_in_ui(),
        "census_buckets": cb,
        "census_expr": ce,
        # 真窗口那几本日志的自打读数（屏幕腿这一族判据的全部依据）
        "accepts": accepts,
        # "含 6.10 且零判废"的那几趟（现数）：判据与变异表都从这里取，不许各算一遍
        "clean": sorted(n for n, d in accepts.items()
                        if d["env"] == 0 and d["no_fail"] and d["has_vo"]
                        and d["ab"].split("/")[0] == d["ab"].split("/")[1]),
        "falsifies": falsifies,
        "log_encs": encs,
        "vo_lines": sorted(set(vo_lines)),
        "r20": r20,
    }


def _recent_consumer_lines(src: str) -> list[int]:
    """数这份源码里**碰** recent 这个键/属性的行号。AST 口径，认四种形状：
    属性（`x.recent`，含写）、下标字符串键（`cfg["recent"]`）、位置实参（`f(cfg.get("recent"))`）、
    关键字实参（`recent=…`）。

    为什么不用文本口径（第一版是 `"recent" in line`）：注释与 docstring 里的"历史叙述"
    会被算成消费者。2026-10-20 实发——`app/ui/main_window.py:841` 那句讲"这个键曾经是个
    没人读的摆设"的注释里有 `max_recent` 三个字，于是"界面没接这一页"这句文档订正被判红。
    文档那句说的是**没有消费者**，所以尺子必须只认代码；注释里提它不算。
    这条口径自己带一对反向哨兵（见 judge() 里那两条"尺子自检"）。

    它看不见什么（盲区不许假装看得见）：①拼出来的键名（`"re" + "cent"`、`f"{p}_recent"`）
    看不见；②分不出读与写（写也计，宁可多报一行让人来看，也不放过真消费者）；
    ③只扫 `app/ui/*.py`，别的目录里读它不算这一格。
    """
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    def is_key(c) -> bool:
        return (isinstance(c, ast.Constant) and isinstance(c.value, str)
                and "recent" in c.value.lower())

    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if "recent" in node.attr.lower():
                lines.add(node.lineno)
        elif isinstance(node, ast.Subscript):
            if is_key(node.slice):
                lines.add(node.lineno)
        elif isinstance(node, ast.Call):
            if any(is_key(a) for a in node.args):
                lines.add(node.lineno)
            for kw in (node.keywords or []):
                if (kw.arg and "recent" in kw.arg.lower()) or is_key(kw.value):
                    lines.add(node.lineno)
    return sorted(lines)


def _count_recent_in_ui() -> int:
    n = 0
    ui = os.path.join(ROOT, "app", "ui")
    for name in sorted(os.listdir(ui)):
        if not name.endswith(".py"):
            continue
        n += len(_recent_consumer_lines(read(os.path.join(ui, name))))
    return n


RECEIPT = {"official": "r17-doc-consistency-run.txt",     # 正式趟：文档↔盘上
           "selftest": "r17-doc-selftest-run.txt"}        # 证伪趟：已知坏样本
SHAPES_RECEIPT = "r17-doc-shapes.txt"                     # 量具自己的三种调用形状（--shapes 写）
# 这两个名字是**唯一真源**：留档由本脚本自己写到它们上（main 的 dest 直接取 RECEIPT），
# 判据读的、文档点名的也是它们。10-19 之前不是同一个源——main 写的是 `r17-doc-consistency.txt`
# （少 `-run`），判据与文档读的是 `r17-doc-consistency-run.txt`，于是每趟正式跑都在给一本
# 10-17 的旧档作保：旧档自打"共 40 条、红 0 条"，本趟实际 75 条，两本从没被比过
# （那条判据只搜子串"红 0 条"，命中就算绿）。原来的 receipt_verdicts() 想做这件事，
# 但它**从没被调用过**（死代码），10-19 把它的"按数字核"并进 judge 后把它删了。
#
# 2026-10-26：语料里**不许收这三本回执**。回执是本脚本自己的输出（judge 还会把文档里的读数
# 原样回显进 note），把它读进语料＝拿自己的回声给文档作保；更直接的祸是正式趟会把 official
# 那本重写，而 `--shapes` 的三个子进程是串行跑的，后一个读到前一个刚写的语料，长度一变输出
# 就不再逐字节相同（10-26 实撞：语料 5041915/5041916 两个值，md5 全相同被自己搅成 False）。
# 回执的内容由下面「量具留档对得上」那条按数字核，不回灌语料。


def _corpus(F, docs):
    """能查到的留档拼成的语料：最新那本链日志＋文档自己点名的每一本 `tests/out/…`。

    只收**文件**（文档里 `tests/out/pre-falsify2-…` 那种指向目录的串不算），读法走
    `read_any`——日志有 GBK 的，读错编码时数字照样在、中文串照样不在，判据会假红。
    三本量具回执自身不收（见 RECEIPT 上面那段的 2026-10-26 注）。
    """
    blob = F["latest"]["text"]
    meta = set(RECEIPT.values()) | {SHAPES_RECEIPT}
    for name in sorted(set(re.findall(r"tests/out/([A-Za-z0-9._-]+)(?![A-Za-z0-9._-])", docs))):
        if name in meta:
            continue
        p = os.path.join(OUT, name)
        if os.path.isfile(p):
            blob += "\n" + read_any(p)[0]
    return blob


def judge(readme, design, F, hand=None, counts=(0, 0), mode="official",
          defer_shapes=False, sh_path=None):
    """全部判据：吃三份文档文本（README／design／交账）+ 盘上事实。返回行列表（PASS/FAIL）。

    `defer_shapes=True` 只给 `--shapes` 的三个内层趟用：那三趟正要重写三形状回执，
    拿上一份（可能就是坏的）当判据会自锁——这一条改由 shapes() 自己的三形状逐字节比对接班，
    判据条数不变（仍是一条 PASS/FAIL 行），正常趟照旧严格读盘。`sh_path` 供自检注入已知坏样本。
    """
    r = []

    def ck(name, want, got, note=""):
        ok = str(want) == str(got)
        r.append(f"[{'PASS' if ok else 'FAIL'}] {name}：期望 {want} ／ 实得 {got}"
                 + (f"（{note}）" if note else ""))

    docs = readme + "\n" + design
    if F["missing"]:
        for p in F["missing"]:
            ck(f"出处文件在盘上：{os.path.basename(p)}", True, False)
        return r

    # 交账那份先读进来：屏幕腿那一族判据也要拿它比（它同样是被核的三份之一）
    handoff = os.path.join(ROOT, "docs", "交账-2026-10-17.md")
    if hand is None:
        hand = read(handoff) if os.path.exists(handoff) else ""

    # r17、r18 两对 keep 留档都核（design 两份都点了名；留档若被后来那趟覆写，md5 就对不上）
    for rd in F["rounds"]:
        rr = F["rounds"][rd]
        ck(f"{rd} 链留档 md5 有出处（`{rr['log']}`）", True, rr["log_md5"] in docs)
        ck(f"{rd} 链哨兵 md5 有出处（`{rr['flag']}`）", True, rr["flag_md5"] in docs)
    L = F["latest"]

    # doctor 项数与项名：代码 CHECKS ／ README 那张表（逐名对账，不只数行）
    n_checks = F["n_checks"]
    lines = readme.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("|") and "报的项" in ln:
            start = i + 1
            break
    tbl_rows = []
    if start is not None:
        for ln in lines[start:]:
            s = ln.strip()
            if not s.startswith("|"):
                break
            if "---" in s:
                continue
            first = s.split("|")[1].strip()
            if first:
                tbl_rows.append(first)
    n_rows = len(tbl_rows)
    ck("README 自检表行数 = app.doctor.CHECKS 长度", n_checks, n_rows,
       f"CHECKS={n_checks}，表头找到={start is not None}")
    ck("README 表里的项名与 doctor.CHECKS 逐名相同（不看顺序）",
       sorted(F["check_names"]), sorted(tbl_rows),
       f"表段起点={start}")
    ck(f"README 正文写着「{n_checks} 项」", True, f"{n_checks} 项" in readme)
    ck("README 留了『别信这句话，信报告自数』的退路", True, "别信这句话" in readme)
    ck("README 不留未登记项数", False, bool(re.search(r"\b(14|15|16)\b 项", readme)))

    # 链趟数：哨兵自打 ／ LEGS 源条数 ／ 两份文档（一律以**最新那一趟**为准）
    ck("链趟数：哨兵自打 = LEGS 源条数", L["legs"], F["legs_src"])
    ck(f"README 写的链趟数 = {L['legs']}", True, f"{L['legs']} 趟" in readme)
    ck(f"design 写的链趟数 = {L['legs']}", True, f"{L['legs']} 趟" in design)
    ck("最新一趟真失败必须 0", "0", L["bad"])
    ck("最新一趟环境不给必须 0（否则 README 那句『这回真跑了』没出处）", "0", L["env"])
    # 旧口径不许再当现值。上一版这里写的是 `共 (19|21) 趟` 这种**手抄清单**——每加一轮就得回来
    # 添一个数，忘了添就是"旧读数没人核"（典型的把闸写成会过期的字面量）。改成不枚举：
    # 文档里出现的每一串 `共 N 趟` 都必须等于最新哨兵自打的那个 N，别的值一律红并点名。
    quoted_legs = sorted({x for x in re.findall(r"共 (\d+) 趟", docs) if x != L["legs"]})
    all_legs = re.findall(r"共 \d+ 趟", docs)
    ck(f"文档里每一串『共 N 趟』都等于最新那趟的 {L['legs']}（旧轮的历史句子不许带这个尾巴）",
       [], quoted_legs, f"文档共出现 {len(all_legs)} 串")
    # 链里那条像素腿：读数从最新留档**现取**（见 facts 里的 pixel），文档必须写着取到的那一串。
    pixel_read = F["pixel"].split("—— ", 1)[1].split("／NOTE")[0] if F["pixel"] else ""
    pixel_all_green = bool(pixel_read) and pixel_read.endswith("FAIL 0／ENV 0")
    ck("README 说链里那条像素腿真跑了 ⇔ 最新链留档里有那条全绿读数",
       True, (("真跑了" in readme) == pixel_all_green), f"现取={F['pixel'][:74]!r}")
    ck("链留档里那条像素腿确实全绿（出处＝最新那本 keep 日志，读数现取）", True, pixel_all_green)
    ck(f"两份文档写的就是现取到的那串像素腿读数（`{pixel_read}`）", 2,
       sum(pixel_read in t for t in (readme, design)) if pixel_read else 0)
    # 文档里凡是"某趟读数"形状（PASS x／FAIL y／ENV z）的，必须在**能查到的留档**里逐字存在：
    # 语料＝最新链留档＋文档自己点名的每一本 tests/out 产物。编一个好看的数是这一族最容易漏的账。
    corpus = _corpus(F, docs)
    stray = [x for x in sorted(set(READING_RE.findall(docs))) if x not in corpus]
    ck("文档抄的每串 PASS/FAIL/ENV 读数都能在最新链留档或文档点名的留档里逐字找到", [], stray,
       f"核 {len(set(READING_RE.findall(docs)))} 串，语料 {len(corpus)} 字符")

    # 2026-10-19 那笔账（双击黑屏）的两件硬证据：承重件指纹＋证伪留档
    ck(f"文档抄的 {KEY_PART.replace(os.sep, '/')} md5 = 盘上现算（改过产品又忘了重抄＝红）",
       True, F["part_md5"] in docs, f"盘上 {F['part_md5']}")
    ck(f"证伪留档在盘上（`{DRIFT_NAME}`）", True, F["drift_exists"])
    ck("两份文档都点了那本证伪留档的名", 2, (DRIFT_NAME in design) + (DRIFT_NAME in hand))
    # 这两条都按**每份文档**数（上一版只比 `readme + design` 这一整坨：M24 注入 design 那份
    # 样片名，README 里还留着一份，整坨比对照样绿——等于这条尺只会放过"其中一份改了"的漂移）
    ck("样片名在 README 与 design 里都逐字写着（续播那一档的输入不许只在一处有账）", 2,
       ("gelu-sample-long.mp4" in readme) + ("gelu-sample-long.mp4" in design))
    ck("内核原话逐字进了 design 与交账（不许换成一句好看的转述）", 2,
       ("argument index has incompatible type" in design)
       + ("argument index has incompatible type" in hand))
    ck("样片 D 在盘上（文档说'量的是这支 32 秒的片子'就得有这支）", True,
       os.path.isfile(os.path.join(ROOT, "tests", "samples", "gelu-sample-long.mp4")))

    # GPU 段：README 点名的每个数都要在 e2e-gpu 原始输出里出现
    seg = re.search(r"\*\*GPU 加速\*\*(.*?)(?=\n### |\n---)", readme, re.S)
    ck("README 有「GPU 加速」段", True, bool(seg))
    if seg:
        body = seg.group(1)
        nums = sorted(set(re.findall(r"\d+\.\d+", body))
                      | {x for x in ("d3d11va", "d3d11") if x in body}
                      | ({"'gpu'"} if "current-vo" in body else set()))
        missing = [x for x in nums if x.strip("'") not in F["gpu"]]
        ck("README GPU 段的数都能在 e2e-gpu 留档里找到", [], missing,
           f"核 {len(nums)} 个：{nums}")

    # 超分那两数的出处
    ck("--list-options 行数 = 文档说的 1274", "1274", F["opts_lines"])
    ck("ngu- 命中数 = 文档说的 0", "0", F["ngu"])
    ck("ancedx4 命中数 = 文档说的 0", "0", F["ancedx4"])
    ck("opts 留档在两份文档里都有指向", 2,
       (F["opts_name"] in readme) + (F["opts_name"] in design))
    ck("两份文档写的是同一个行数", 2, ("1274" in readme) + ("1274" in design))

    # 迅雷影音那格：两处口径都要在
    ck("design §6 有 (g) 子目", True, "(g)" in design)
    ck("README 与 design 都写了那次现数归 xmp.mkv", 2,
       ("xmp.mkv" in readme) + ("xmp.mkv" in design))
    # 本轮订正过的那句（recent 只写不读）必须有盘上尺子撑着
    ck("app/ui/*.py 里读 recent 的行数 = 0（文档那句『界面没接这一页』）", 0, F["recent_ui_reads"])
    # 尺子自己的一对反向哨兵：口径改成 AST 之后，"注释不算"与"真读要算"两头都得当场证。
    # 只留一头的话，下一轮有人把判据改回文本口径（注释又算进来）或改成恒不匹配（消费者也不算），
    # 这格都能绿——那就是把"判据变准"和"守卫被松"混成一件事。
    ck("recent 尺子自检①：只有注释／docstring 提它时不计（计了就红）", [],
       _recent_consumer_lines('# 这个键曾经是个没人读的摆设（和 max_recent 一起被删过）\n'
                              'def f():\n    """讲历史：recent 曾经没人读。"""\n    return 1\n'))
    ck("recent 尺子自检②：真读它的一行必须计（不计就红）", [2, 3, 4],
       _recent_consumer_lines('def f(cfg, x):\n    a = cfg["recent"]\n    b = x.recent\n'
                              '    return list(reversed(cfg.get("recent_list", [])))\n'))
    ck("recent 普查留档在两份文档里都有指向", 2,
       (F["recent_artifact"] in readme) + (F["recent_artifact"] in design))
    ck("README 给了 ms-settings 那条命令", True, "ms-settings:defaultapps" in readme)
    # §6 第 37 条的子目：锚到那一**段**里数（拿全文比会恒真——"(a)" 这种串别处也有）
    seg37 = re.search(r"\n37\.\s.*?(?=\n\d+\.\s\*\*|\n## 7\.|\Z)", design, re.S)
    ck("design 有 §6 第 37 条这一整段（本轮的台账就在这里）", True, bool(seg37))
    body37 = seg37.group(0) if seg37 else ""
    for sub in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)", "(h)", "(i)"):
        ck(f"§6 37 段内有子目 {sub}", True, sub in body37)

    # ---- R20/R21 全屏这一族（2026-10-21 换尺）：结论、读数、留底指纹一律回盘上核 ----
    seg40 = re.search(r"\n40\.\s.*?(?=\n\d+\.\s\*\*|\n## 7\.|\Z)", design, re.S)
    ck("design 有 §6 第 40 条这一整段（全屏这一族的台账）", True, bool(seg40))
    body40 = seg40.group(0) if seg40 else ""
    for sub in ("(a)", "(b)", "(c)", "(d)", "(e)"):
        ck(f"§6 40 段内有子目 {sub}", True, sub in body40)
    for n in R20_LOGS:
        finals = F["r20"][n]
        ck(f"{n} 的 [FINAL] 行数", len(R20_FINALS[n]), len(finals), f"现取 {finals}")
        for line in R20_FINALS[n]:
            ck(f"{n} 自打 `{line[:44]}…`", True, line in finals)
            ck(f"design 逐字抄了这行（换尺后的现数，不许另编一句）", True, line in body40)
    ck("design 点名三本 0024 校准留档", 3, sum(n in body40 for n in R20_LOGS))
    ck(f"收口小册 `{R20_ART}` 在盘上且被 design 点名", 2,
       int(bool(F["r20"]["artifact"])) + int(R20_ART in design))
    bad_keep = [n for n, want in R20_KEEP.items() if F["r20"]["keep_md5"].get(n) != want]
    ck("两张现场图的留底 md5 = 文档写的那两颗（回盘上现算）", [], bad_keep,
       f"现算 {F['r20']['keep_md5']}")
    ck("小册与 design 都写着换尺前后的两串读数（判据不许只抄结论）", 4,
       sum(s in F["r20"]["artifact"] for s in ("(0, 274, 0, 274)", "(0, 273, 64, 273)"))
       + sum(s in body40 for s in ("(0, 274, 0, 274)", "(0, 273, 64, 273)")))
    ck("小册与 design 都写了 03:35 那趟的 B 结论不作数", 2,
       ("不作数" in F["r20"]["artifact"]) + ("不作数" in body40))
    ck("design 点名 03:35 那趟的坏读数在保留（二次+8.00s）", True, "二次+8.00s" in body40)

    # 03:35 那趟"整趟不作数"的机器依据：版本号回日志现取、当前这一版用 build_stamp 现算，
    # 判据只比两串现取值——文档里一个指纹字面都不许出现（抄了下一轮就过期）。
    rt = F["r20"]
    ps, cs = rt["probe_short"], rt["cur_short"]
    ck("03:35 探针日志里取得到 [被测] 短指纹（现取，判据不手抄）", True, bool(ps),
       f"0335={ps!r}｜当前={cs!r}")
    ck("design 写着的 03:35 短指纹 = 日志里那一串（『整趟不作数』的版本号不许抄错）",
       True, bool(ps) and ps in body40)
    ck("03:35 的短指纹 ≠ 当前这一版（两串都是现取的，不是两行字面比）", True,
       bool(ps) and bool(cs) and ps != cs, f"{ps} vs {cs}")
    diff3 = [n for n in R20_PROBE_DIFF if rt["probe_files"].get(n) != rt["cur_files"].get(n)]
    ck("design 点名的三份确实都和 0335 不同（逐文件回盘上现算，凑不齐就是那句话过期了）",
       len(R20_PROBE_DIFF), len(diff3),
       f"不同 {diff3}｜0335 {[rt['probe_files'].get(n) for n in R20_PROBE_DIFF]}"
       f"｜当前 {[rt['cur_files'].get(n) for n in R20_PROBE_DIFF]}")
    ck("design 的归因句点名了那三份（『三份在那之后改过』这句话还在段里）", True,
       R20_PROBE_PHRASE in body40)
    ck("design 没把当前短指纹抄进正文（抄了下一轮必过期；文档自己说了现打）", False,
       bool(cs) and cs in body40)
    ck("交账也写了 03:35 那趟整趟不作数（用户面那本不许把作废读数当现值）", True,
       "03:35 那趟" in hand and "整趟不作数" in hand)
    for n, line in R20_OFFLINE.items():
        ck(f"{n} 自打收口行（现取，判据不手抄）", True,
           F["r20"]["offline"].get(n) == line,
           f"现取 {F['r20']['offline'].get(n)!r}")
        ck(f"design 逐字抄了 `{n}` 的收口行（条数与坏实现数都是会漂的数，由 R20_OFFLINE 现钉）",
           True, line in body40)

    # ---- 屏幕腿这一族：文档点名的日志、抄的读数、缺的那一格，一律回盘上现数 ----
    ck("真窗口日志全部按 UTF-8 严格读通（读错编码只会让中文串比对假红，量具自己也要留证据）",
       ["utf-8"], F["log_encs"])
    named = sorted(set(re.findall(r"(?:accept|falsify)-ui-[A-Za-z0-9._-]*\.log", docs)))
    ck("文档点名的每本真窗口日志都在盘上", [],
       [n for n in named if not os.path.exists(os.path.join(OUT, n))], f"点名 {len(named)} 本")
    # 三份文档里每一个 `tests/out/<产物>` 指向都要在盘上（`.flag` 是哨兵，本轮可能还没落，不算）。
    # 抓**最长**那一串：留底名是 `user-config.json.pre-1017-022241` 这种"扩展名后面还有后缀"的形状，
    # 按扩展名截断会把一个本来在盘上的名字读成"文档编了一个不存在的文件"（第一趟就红在这）。
    named_art = sorted(set(re.findall(r"tests/out/([A-Za-z0-9._-]+)(?![A-Za-z0-9._-])",
                                      docs + "\n" + hand)))
    named_art = [n for n in named_art if "." in n and not n.endswith(".flag")]
    ck("文档里每个 `tests/out/…` 指向都在盘上（哨兵 .flag 除外）", [],
       [n for n in named_art if not os.path.exists(os.path.join(OUT, n))],
       f"点名 {len(named_art)} 本")
    quoted_ab = sorted(set(re.findall(r"汇总 `(\d+/\d+)`", docs))
                       | set(re.findall(r"(\d+/\d+) 通过", docs)))
    # 语料＝文档点名的每一本 `.log`（不止 accept-ui-\*）。收窄到只有 UI 那几本的话，
    # 别的套件的汇总行永远"找不到"，如实抄数反而红——2026-10-20 加退出收口守卫时就这么红的。
    # 放宽的是**来源集合**，不是**比对方式**：仍然要求这串比值逐字出现在某本**文档自己指向的**
    # 日志的汇总行里，所以 M13（凭空编一串盘上没有的比值）照样翻红。
    disk_ab = {d["ab"] for d in F["accepts"].values()}
    for n in named_art:
        if not n.endswith(".log"):
            continue
        p = os.path.join(OUT, n)
        if os.path.exists(p):
            m = SUMMARY_RE.search(read(p))
            if m:
                disk_ab.add(f"{m.group(1)}/{m.group(2)}")
    ck("文档抄的每串验收汇总读数都能在某本日志**自己的汇总行**里逐字找到", [],
       [x for x in quoted_ab if x not in disk_ab],
       f"核 {len(quoted_ab)} 串：{quoted_ab}｜语料 {len(disk_ab)} 串汇总行")
    quoted_t = sorted(set(re.findall(r"共 \d+ 行 —— CAUGHT \d+／MISSED \d+／ENV \d+／非布尔 \d+", docs)))
    disk_t = set(F["falsifies"].values())
    ck("文档抄的每串证伪收口行都能在某本 falsify 日志**自己的自打行**里逐字找到", [],
       [x for x in quoted_t if x not in disk_t], f"核 {len(quoted_t)} 串")
    with_vo = sorted(n for n, d in F["accepts"].items() if d["has_vo"])
    ck("6.10 那条真机判据在盘上至少绿着两趟（现数，不抄）", True, len(with_vo) >= 2,
       f"{len(with_vo)} 本：{with_vo}")
    if F["vo_lines"]:
        ck("盘上 6.10 那句读数逐字出现在 design 与交账里（不许另编一句好看的）", 2,
           sum(F["vo_lines"][0] in t for t in (design, hand)))
    # 口径闸：盘上有没有"含 6.10 且零判废"的整趟，两份文档的写法必须跟着盘上走。
    # 上一版这里只认"还缺"那一种状态（`not clean ⇔ 文档写缺`）——那一格真被跑出来之后，
    # 文档如实改成"有了"反而会红，等于逼着文档一直装着还没做完。两个状态各自一套判据。
    clean = F["clean"]
    ck("交账必须提『零判废』这一口径（有没有那趟都要提，只是方向不同）", True, "零判废" in hand)
    ck("两份文档都点名接班的看门狗（那一格无论有没有补上，机制都得在账上）", 2,
       ("watch_ui_r18" in design) + ("watch_ui_r18" in hand))
    if clean:
        ck("盘上已有那趟 ⇒ design 不许还写着『缺的就是』（补完了还装缺＝假口径）",
           False, "缺的就是" in body37)
        ck("盘上已有那趟 ⇒ 两份文档各点名其中一本日志（现数，不抄）", 2,
           sum(bool([n for n in clean if n in t]) for t in (design, hand)),
           f"盘上 {len(clean)} 本：{clean[:3]}")
    else:
        ck("design 锚句『缺的就是』⇔ 盘上还没有那趟（写成跑过＝红）", True, "缺的就是" in body37)

    # 「三形状输出逐字节相同」这句话在文档不许只有一句话＋一颗手抄 md5：必须有回执，
    # 且回执**两半都得绿**。只核「md5 全相同」会漏掉"三趟一起失败、输出当然一样"那种假绿；
    # 2026-10-26 之前不敢核 rc，是因为内层趟会读上一代回执而自锁（第一趟没回执→记红→写 rc=False
    # →第二趟又读 False）。现在内层趟带 --shapes-inner 跳过这一格，rc 这半也就不再依赖上一代了。
    sh = sh_path or os.path.join(OUT, SHAPES_RECEIPT)
    sh_txt = read(sh) if os.path.exists(sh) else ""
    sh_ok = "md5 全相同=True" in sh_txt and "rc 全 0=True" in sh_txt
    ck("三形状回执在盘上且自打『md5 全相同=True／rc 全 0=True』", True,
       True if defer_shapes else sh_ok,
       (os.path.basename(sh) + "；本轮是 --shapes 的驱动趟，回执正要被重写，"
        "这一条由 shapes() 的三形状逐字节比对接班") if defer_shapes else os.path.basename(sh))
    ck("design 指向三形状回执（那句『逐字节相同』不许只手抄一颗 md5）", True,
       SHAPES_RECEIPT in design)

    # 普查判决行：文档不许缩写（上一版就缩写过），按桶逐条比＋对账算式逐字比
    ck(f"普查留档在两份文档里都有指向（`{CENSUS_NAME}`）", 2,
       (CENSUS_NAME in design) + (CENSUS_NAME in hand))
    for lab, num in F["census_buckets"]:
        ck(f"普查判决的桶「{lab} {num}」两份文档都逐字写着", 2,
           (f"{lab} {num}" in design) + (f"{lab} {num}" in hand), f"盘上 {len(F['census_buckets'])} 个桶")
    ck(f"普查那串行对账算式（{F['census_expr']}）两份文档都逐字写着", 2,
       (F["census_expr"] in design) + (F["census_expr"] in hand))

    # 交账那份（面向用户）存在，且数字与同一批留档对得上
    ck("交账文件在盘上", True, os.path.exists(handoff))
    if hand:
        h = hand
        ck("交账里的链读数 = 最新那趟哨兵自打", True,
           f"绿 {L['green']}／真失败 {L['bad']}／环境不给 {L['env']}／本轮不作数 {L['na']}" in h
           and f"{L['legs']} 趟" in h)
        # 交账抄的两个计数：条数拿**本趟自打**的数比（不是上一趟留档——留档里的"红 N"会被文档
        # 里那句"红 0"污染成死循环），证伪条数拿源码里 MUTATIONS 的条数比。留档本身另有一条"在不在盘上"。
        total, n_mut = counts
        ck("交账抄的对账条数 = 本趟自打（加了判据没重跑就红）", True,
           f"{total} 条判据" in h, f"本趟 {total} 条")
        ck("交账抄的证伪条数 = MUTATIONS 条数", True,
           f"CAUGHT {n_mut}" in h, f"len(MUTATIONS)={n_mut}")
        # 「每趟只许读对方那本，而且按数字读」：自己那本只查存在——上一趟的红会把自己那本写成
        # "红 1 条"，若这趟再去要求它"红 0 条"就成了死循环（绿要等上一趟绿）。10-19 改判据条数时
        # 真撞上过一次。自己这趟绿不绿由本趟 [FINAL] 行与 rc 担保；对方那本由对方趟担保、由本趟
        # 拿它自打的数与本趟现算比（子串会把一本换了条数的旧档读成绿，见 RECEIPT 上面那段）。
        own, sib = RECEIPT["official" if mode == "official" else "selftest"], \
            RECEIPT["selftest" if mode == "official" else "official"]
        own_ok = os.path.isfile(os.path.join(OUT, own))
        sib_p = os.path.join(OUT, sib)
        sib_txt = read(sib_p) if os.path.isfile(sib_p) else ""
        if os.path.isfile(sib_p) and not sib_txt.strip():
            sib_got = "空文件——十成是把『自己正要写的那本』读进来了（自锁），或那趟被中途打断"
            m = None
        elif mode == "official":
            m = re.search(r"\[FINAL\] selftest —— CAUGHT (\d+)／MISSED (\d+)／NOP (\d+)"
                          r"（共 (\d+) 条变异）", sib_txt)
            _base = next((l for l in sib_txt.splitlines() if l.startswith(("[PASS] 基线", "[FAIL] 基线"))),
                         "没有基线行")
            sib_got = (f"CAUGHT {m.group(1)}／MISSED {m.group(2)}／NOP {m.group(3)}"
                       f"／变异表 {m.group(4)}／基线＝{_base[:14]}…（这一格**只报不闸**：对方趟的基线"
                       f"判的就是本趟自己也在判的那些行，拿去当闸就成了『绿要等上一趟绿』的死循环）"
                       if m else "那本没有收口行")
            sib_want = (m is not None and int(m.group(1)) == n_mut and
                        int(m.group(2)) == 0 and int(m.group(3)) == 0 and
                        int(m.group(4)) == n_mut)
        else:
            m = re.search(r"\[FINAL\] 文档↔盘上一致性：共 (\d+) 条判据，红 (\d+) 条"
                          r".*?（([A-Za-z0-9._-]+\.log)）", sib_txt, re.S)
            sib_got = (f"共 {m.group(1)} 条、红 {m.group(2)}、链日志={m.group(3)}"
                       if m else "那本没有收口行")
            sib_want = (m is not None and int(m.group(1)) == total and int(m.group(2)) == 0 and
                        m.group(3) == F["latest"]["log"])
        ck(f"量具留档对得上：对方那本（{mode} 的反向趟）自打的数＝本趟现算，自己那本在盘上"
           f"（本趟只读对方那本的内容，读自己正要写的那份会自锁）", 2,
           int(os.path.isfile(os.path.join(OUT, own))) + int(bool(sib_want)),
           f"对方＝{sib}：{'对得上' if sib_want else '对不上'}（那本自打 {sib_got}；"
           f"本趟现算＝条数 {total}／变异 {n_mut}／红 0／链日志 {F['latest']['log']}）"
           f"／自己＝{own}（只查存在：{'在' if os.path.isfile(os.path.join(OUT, own)) else '不在'}）")
        ck("交账点名了这两份量具留档（名字取 RECEIPT＝脚本真正写的那两本，不是另一串相似的字）", 2,
           sum(n in h for n in RECEIPT.values()))
        ck("交账点了那三本留档（修前／修后成对＋普查那本，缺陷这一节不许只讲道理不给现场）", 3,
           sum(bool(re.search(p, h)) for p in (r"probe-playlist-pos-wipe\.txt",
                                               r"probe-playlist-pos-wipe-postfix\.txt",
                                               r"probe-file-scoped-table-v4-\d+\.txt")))
    return r


MUTATIONS = {
    "M1 项数改回旧的 14": ("README.md", "17 项", "14 项"),
    # 下面这几条写的是 `@占位符`＝"注入当前盘上真的写着的那一串"。上一版这里写死了
    # `共 23 趟`／`170/170 通过` 这种字面量：文档一改口径，replace 就落在不存在的文本上、
    # 文本没变、记成 NOP——红的是变异表自己，不是判据。占位符保证每轮注的都是活文本。
    "M2 链趟数改小 1（文档与最新哨兵不符）": ("design.md", "@LEGS", None),
    "M3 文档里某颗留档 md5 改一个字符": ("design.md", None, None),
    "M4 GPU 段硬解 CPU 改 99.9": ("README.md", "10.9", "99.9"),
    "M5 删掉 opts 留档的指向": ("README.md", "mpv-list-options-1017-0306.txt", "mpv-list-options.txt"),
    "M6 表里删掉一行（16 行 vs 17 项）": ("README.md", "| 默认程序现状 |", "| ~~默认程序现状~~ |"),
    "M7 撤掉 recent 普查留档的指向（那句订正就没人核了）": ("README.md",
                                                    "recent-consumers-1017-0322.txt", "recent-consumers.txt"),
    # 交账那份是面向用户的，它的数同样得有人核——这几条注入证明那几条判据有牙
    "M8 交账里的对账条数改小 1（手抄漂移）": ("交账-2026-10-17.md", "@DOC_COUNT", None),
    "M9 交账把还缺的那格写成已补齐（口径闸的交账这一半）": ("交账-2026-10-17.md", "零判废", "零判绿"),
    "M10 撤掉三形状回执的指向（文档又变回手抄 md5）": ("design.md", "r17-doc-shapes.txt", "r17-doc-shapes-old.txt"),
    "M11 交账抄的证伪条数改小 1": ("交账-2026-10-17.md", "@CAUGHT_COUNT", None),
    "M12 撤掉两份量具留档的名字（对账从此没有可看的留档）": ("交账-2026-10-17.md",
                                                    "r17-doc-consistency-run.txt", "r17-doc-consistency.txt"),
    # 本轮新添的三族尺各配一条注入——不加这几条，"新判据"就只是新写的句子，没人证明它们会红
    "M13 把一串验收汇总读数改成盘上没有的比值": ("design.md", "@AB_INVENT", None),
    "M14 把文档点名的某本日志改成不存在的名": ("design.md", "accept-ui-r17c-0925.log", "accept-ui-r17c-0999.log"),
    "M15 把证伪收口那句的 CAUGHT 改小 1": ("design.md", "CAUGHT 85／MISSED 0", "CAUGHT 84／MISSED 0"),
    "M16 那一格的状态写反（有了还装缺／缺了写成有了）": ("design.md", "@VO_GAP", None),
    "M17 撤掉接班看门狗的点名（记 ENV 就变成把尾巴丢回用户）": ("交账-2026-10-17.md", "watch_ui_r18", "watch_ui"),
    "M18 把普查判决行里那两个 0 格的桶删掉（缩回上一版那句好看的）": ("交账-2026-10-17.md",
                                                             "该摘但没人读 0 （无）；", ""),
    "M19 README 把链里那条像素腿说成没跑成（两边都会红的闸，这是另一边的注入）":
        ("README.md", "真跑了", "没跑成"),
    "M20 把 6.10 那句读数改一个字（文档不许自己编一句好看的）":
        ("交账-2026-10-17.md", "信息句='软件解码'", "信息句='硬件解码'"),
    # ---- 2026-10-19 那笔账新增的四族尺，各自配一条注入（没配注入的新判据＝没人证明它会红）----
    "M21 把文档抄的 player.py md5 改一个字符（改过产品没重抄指纹）": ("design.md", "@PLAYER_MD5", None),
    "M22 把文档抄的某串 PASS/FAIL/ENV 读数改成留档里没有的": ("design.md", "@FINAL_READING", None),
    "M23 撤掉 loadfile 证伪留档的指向（那句『有牙』从此没出处）": ("design.md", "@DRIFT_NAME", None),
    "M24 把样片名改成没有的那支（续播那一档的输入从此没人核）": ("design.md",
                                                        "gelu-sample-long.mp4", "gelu-sample-9.mp4"),
    # ---- 2026-10-21 全屏换尺这一族：读数抄错、读数只抄一半、小册指针失效，各自必须翻红 ----
    "M25 把换尺后自检的『错 0 张』改成『错 1 张』（校准读数抄错）": ("design.md",
                                                        "错 0 张", "错 1 张"),
    "M26 把换尺后的内缩读数改成盘上没有的一串（新旧两串只剩一半）": ("design.md",
                                                        "(0, 274, 0, 274)", "(0, 200, 0, 200)"),
    "M27 把换尺归因小册的名字改成不存在的（那句『不是产品缺陷』从此没出处）": ("design.md",
                                                        "r20-re2-ruler-artifact-20261021.md",
                                                        "r20-re2-ruler-artifact-20261020.md"),
    # ---- 03:35 整趟不作数这一族：版本号抄错、把当前版本号抄进正文，各自必须翻红 ----
    "M28 把 03:35 的短指纹改成另一串（『整趟不作数』的版本号抄错）": ("design.md",
                                                        "@R20_PROBE_SHORT", None),
    "M29 把当前短指纹抄进正文（下一轮必过期；文档自己说了现打）": ("design.md",
                                                        "@CUR_SHORT_BAIT", None),
    "M30 交账把『整趟不作数』写成『整趟算数』（用户面那本把作废读数当现值）": ("交账-2026-10-17.md",
                                                        "整趟不作数", "整趟算数"),
    "M31 把离线状态机的『20 条判据』改小 1（收口行抄错）": ("design.md",
                                                        "共 20 条判据", "共 19 条判据"),
}


def resolve(tok, src, F):
    """@占位符 → （文档里现在写着的那串, 改成盘上查无的那串）。拿不到活文本就原样返回，
    由下面的 NOP 闸门自己认下来——变异失效要记 NOP，不许冒充"判据漏网"。"""
    L = F["latest"]
    if tok == "@LEGS":
        a = f"共 {L['legs']} 趟"
        return (a, f"共 {int(L['legs']) - 1} 趟") if a in src else (tok, tok)
    if tok == "@AB_INVENT":
        m = re.search(r"\d+/\d+ 通过", src)
        if not m:
            return tok, tok
        a, bN = m.group(0), int(m.group(0).split("/")[0])
        return a, f"{bN + 3}/{m.group(0).split('/')[1].split(' 通')[0]} 通过"
    if tok == "@VO_GAP":
        if F["clean"]:
            n = next((x for x in F["clean"] if x in src), None)
            return (n, n.replace(".log", "9.log")) if n else (tok, tok)
        return ("缺的就是", "补齐的就是") if "缺的就是" in src else (tok, tok)
    if tok == "@PLAYER_MD5":
        d = F["part_md5"]
        return (d, ("0" if d[0] != "0" else "1") + d[1:]) if d in src else (tok, tok)
    if tok == "@FINAL_READING":
        m = READING_RE.search(src)
        if not m:
            return tok, tok
        p, fl, ev = (int(x) for x in re.findall(r"\d+", m.group(0)))
        cand = f"PASS {p + 7}／FAIL {fl}／ENV {ev}"
        if cand in _corpus(F, src):
            cand = "PASS 404／FAIL 0／ENV 0"
        return m.group(0), cand
    if tok == "@DRIFT_NAME":
        return (DRIFT_NAME, DRIFT_NAME.replace("215401", "235959")) if DRIFT_NAME in src else (tok, tok)
    if tok == "@R20_PROBE_SHORT":
        ps = F["r20"].get("probe_short", "")
        if ps and ps in src:
            return ps, ("0" if ps[0] != "0" else "1") + ps[1:]
        return tok, tok
    if tok == "@CUR_SHORT_BAIT":
        cs = F["r20"].get("cur_short", "")
        a = "03:35 那一趟整趟不作数"
        return (a, f"03:35 那一趟（{cs}）整趟不作数") if cs and a in src else (tok, tok)
    if tok == "@SAMPLE_NAME":
        s = "gelu-sample-long.mp4"
        return (s, "gelu-sample-9.mp4") if s in src else (tok, tok)
    raise KeyError(f"变异表里有不认识的占位符 {tok}")



def selftest(base_readme, base_design, F):
    """已知答案对照：基线必须绿，每份坏样本必须各自翻红，且注入必须真的改动文本（NOP 闸门）。"""
    r = []
    handoff = os.path.join(ROOT, "docs", "交账-2026-10-17.md")
    base_hand = read(handoff) if os.path.exists(handoff) else ""
    probe = judge(base_readme, base_design, F, base_hand, mode="selftest")
    total, n_mut = len(probe), len(MUTATIONS)
    base = judge(base_readme, base_design, F, base_hand, (total, n_mut), mode="selftest")
    base_red = [l for l in base if l.startswith("[FAIL]")]
    r.append(f"[{'PASS' if not base_red else 'FAIL'}] 基线：文档↔盘上判据全绿（{len(base)} 条，红 {len(base_red)}）"
             f"，MUTATIONS {n_mut} 条")
    # 已知答案对照：三形状回执那条判据读的是**盘上的文件**，文档变异表管不到它，单独摆三份
    # 临时回执喂给正常趟（defer_shapes=False）：md5 坏、rc 坏、两半都好。只测坏样本不够——路径
    # 写错时它恒红也算"抓到"；配好样本，必须两坏一好才算这条判据真在分辨（两半各有一条坏样本，
    # 漏掉哪一半都会当场露馅）。临时文件在本函数内建、在本函数内删。
    bad_p = os.path.join(OUT, "_selftest-shapes-md5bad.txt")
    rcbad_p = os.path.join(OUT, "_selftest-shapes-rcbad.txt")
    good_p = os.path.join(OUT, "_selftest-shapes-good.txt")
    try:
        for p, md5v, rcv in ((bad_p, "False", "True"),
                             (rcbad_p, "True", "False"),
                             (good_p, "True", "True")):
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write("[SHAPE] 接管道 rc=0 md5=x bytes=0 stderr=0B\n"
                        f"[FINAL] 三种形状：md5 全相同={md5v}／rc 全 0={rcv}\n")
        # 只取**内容那条**判据（`[PASS] 三形状回执在盘上…`）；另一条 `design 指向三形状回执…`
        # 也含这四个字，不按行首收窄会把两条一起数进来，对照就成了"2 行为什么不是 1 行"。
        _head = ("[PASS] 三形状回执", "[FAIL] 三形状回执")

        def _row(path):
            got = [l for l in judge(base_readme, base_design, F, base_hand, (total, n_mut),
                                    mode="selftest", sh_path=path) if l.startswith(_head)]
            return got

        rb, rr, rg = _row(bad_p), _row(rcbad_p), _row(good_p)
    finally:
        for p in (bad_p, rcbad_p, good_p):
            try:
                os.remove(p)
            except OSError:
                pass

    def _verdict(rows):
        if len(rows) != 1:
            return f"{len(rows)} 行"
        return "红" if rows[0].startswith("[FAIL]") else "绿"

    ctrl_ok = (len(rb) == 1 and rb[0].startswith("[FAIL]") and
               len(rr) == 1 and rr[0].startswith("[FAIL]") and
               len(rg) == 1 and rg[0].startswith("[PASS]"))
    r.append(f"[{'PASS' if ctrl_ok else 'FAIL'}] 对照：三形状回执判据分得开红绿 —— "
             f"md5 坏／rc 好→{_verdict(rb)}（应红），"
             f"md5 好／rc 坏→{_verdict(rr)}（应红），"
             f"两半都好→{_verdict(rg)}（应绿）")
    caught = missed = nop = 0
    for name, (fn, a, b) in MUTATIONS.items():
        rd, ds, hd = base_readme, base_design, base_hand
        src = rd if fn == "README.md" else (hd if fn.startswith("交账") else ds)
        if isinstance(a, str) and a.startswith("@") and a != "@DOC_COUNT" and a != "@CAUGHT_COUNT":
            a, b = resolve(a, src, F)          # 注的是"文档现在真的写着的那串"
        if fn == "README.md":
            rd = rd.replace(a, b, -1)
        elif fn.startswith("交账"):
            if a == "@DOC_COUNT":
                a, b = f"{total} 条判据", f"{total - 1} 条判据"
            elif a == "@CAUGHT_COUNT":
                a, b = f"CAUGHT {n_mut}", f"CAUGHT {n_mut - 1}"
            hd = hd.replace(a, b, -1)
        else:
            if name.startswith("M3"):
                # 文档引用的某颗链留档 md5 改一个字符：那本留档的指针必须失效
                for rr in F["rounds"].values():
                    for real in (rr["log_md5"], rr["flag_md5"]):
                        if real in ds:
                            ds = ds.replace(real,
                                            ("0" if real[0] != "0" else "1") + real[1:], -1)
                            break
                    else:
                        continue
                    break
            else:
                ds = ds.replace(a, b, -1)
        if rd == base_readme and ds == base_design and hd == base_hand:
            nop += 1
            r.append(f"[NOP] {name}：文本一个字没变（变异失效，不是漏网）")
            continue
        rows = judge(rd, ds, F, hd, (total, n_mut), mode="selftest")
        red = [l for l in rows if l.startswith("[FAIL]") and l not in base_red]
        if red:
            caught += 1
            r.append(f"[CAUGHT] {name} → {red[0][7:120]}")
        else:
            missed += 1
            r.append(f"[MISSED] {name}：判据没翻红（恒真）")
    r.append(f"[FINAL] selftest —— CAUGHT {caught}／MISSED {missed}／NOP {nop}（共 {len(MUTATIONS)} 条变异）")
    return r


def shapes():
    """量具自己的三种调用形状（接管道／`> 文件`／`> 文件 2>&1`）：rc 必须全 0、stdout 必须逐字节相同。

    回执落 `tests/out/r17-doc-shapes.txt`，md5 由本函数自打——文档只许指向这本回执，
    不许把 md5 抄进正文（抄进正文的那颗就是下一轮会过期的手抄值）。返回 rc。
    """
    import subprocess
    me = os.path.abspath(__file__)
    rows, digests, rcs = [], [], []
    for label, mode in (("接管道", "pipe"), ("> 文件", "file"), ("> 文件 2>&1", "both")):
        err_len = 0
        if mode == "pipe":
            p = subprocess.run([sys.executable, me, "--shapes-inner"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            raw, rc = p.stdout, p.returncode
            err_len = len(p.stderr)
        else:
            tmp = os.path.join(OUT, "_shape_%s.raw" % mode)
            with io.open(tmp, "wb") as fh:
                kw = {"stdout": fh, "stderr": fh if mode == "both" else subprocess.DEVNULL}
                rc = subprocess.run([sys.executable, me, "--shapes-inner"], **kw).returncode
            raw = open(tmp, "rb").read()
            if mode != "both":
                err_len = 0
            os.remove(tmp)
        d = hashlib.md5(raw).hexdigest()
        digests.append(d)
        rcs.append(rc)
        rows.append(f"[SHAPE] {label:<12} rc={rc} md5={d} bytes={len(raw)} stderr={err_len}B")
    same = len(set(digests)) == 1
    all_zero = all(rc == 0 for rc in rcs)
    rows.append("[SHAPE] 注：三趟都带 --shapes-inner——三形状回执正要被本函数重写，"
                "『回执为绿』那一格由本函数的三形状逐字节比对接班；正常官方趟没有这个开关，严格读盘。"
                "（2026-10-26 之前内层趟读上一份回执，坏一次就永久自锁）")
    rows.append(f"[FINAL] 三种形状：md5 全相同={same}／rc 全 0={all_zero} —— 计数与 md5 由本脚本自打，文档别抄")
    text = "\n".join(rows) + "\n"
    dest = os.path.join(OUT, SHAPES_RECEIPT)
    with io.open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0 if (same and all_zero) else 1


def main():
    if "--shapes" in sys.argv:
        return shapes()
    selftest_mode = "--selftest" in sys.argv
    # --shapes 的三个内层趟：三形状回执正要被本趟重写，不许拿上一份（可能就是坏的）当判据，
    # 否则坏一次就永久自锁。正常官方趟没有这个开关。
    defer_shapes = "--shapes-inner" in sys.argv
    readme = read(os.path.join(ROOT, "README.md"))
    design = read(os.path.join(ROOT, "docs", "design.md"))
    F = facts()
    L = F["latest"]
    if selftest_mode:
        rows = selftest(readme, design, F)
    else:
        # 先探一趟拿条数（那条判据比的就是"本趟自打"），再拿这个数正式判——两条判据自己也在账上，
        # 所以探到的条数＝正式的条数；谁加了判据没重跑，交账抄的那句话当场红。
        probe = judge(readme, design, F, defer_shapes=defer_shapes)
        rows = judge(readme, design, F, None, (len(probe), len(MUTATIONS)),
                     defer_shapes=defer_shapes)
        rows.append(f"[FINAL] 文档↔盘上一致性：共 {len([l for l in rows if l.startswith(('[PASS]','[FAIL]'))])} 条判据，"
                    f"红 {len([l for l in rows if l.startswith('[FAIL]')])} 条 —— 条数由脚本自己数，"
                    f"链 [FINAL] 原文=绿{L['green']}／真失败{L['bad']}／环境{L['env']}／不作数{L['na']}／共{L['legs']}趟"
                    f"（{L['log']}）")
    text = "\n".join(rows) + "\n"
    dest = os.path.join(OUT, RECEIPT["selftest" if selftest_mode else "official"])
    with io.open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.stdout.write(text)
    sys.stdout.flush()
    if selftest_mode:
        bad = any(l.startswith(("[MISSED]", "[NOP]", "[FAIL]")) for l in rows) or \
            not any(l.startswith("[FINAL] selftest") for l in rows)
        return 1 if bad else 0
    return 1 if any(l.startswith("[FAIL]") for l in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
