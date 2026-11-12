"""只读判据：shell 的「打开方式」候选枚举里，每个媒体扩展名都得有本播放器。

为什么需要这本账（注册表那本账已经绿了，还差这一环）
------------------------------------------------------------------
· `tests/check_openwith.py` 证明注册表里该写的都写了（现数：48 个扩展名 + ProgID + App Paths
  + Applications，`--check` rc=0）。
· `tests/e2e_openwith.py` 证明照着注册表那条命令行执行，内核真能把文件播起来（12 条全绿）。
· 中间还差：**这台 shell 认不认这份注册**。注册姿势错了（漏 App Paths、ProgID 没友好名、
  写错 hive……）注册表照样"全绿"，可用户右键一看，列表里根本没这个播放器——等于没做。
  用户最初的原话就是这个：「根本都没有这个播放器的选项」。

为什么不点 UI 而是问数据源
------------------------------------------------------------------
这台 26300（Win11 24H2）上，四扇能把「打开方式」开出来的编程门实测全哑：FolderItem 动词表里
压根没有 openas；InvokeVerb('openas')、rundll32 shell32 OpenAs_RunDLL、SHOpenWithDialog、
OpenWith.exe 都开不出窗口（留档 tests/out/probe-openwith-uia*.txt、probe-openwith-explorer-*.txt）。
UIA 也够不着：实测**所有** CabinetWClass（含用户自己开着的三扇）在 UIA 里只有 1 个子元素
（只剩标题栏脚手架），资源管理器的内容树摊不下来（留档 tests/out/probe-openwith-cloak-1016-021550.txt）。

`shell32!SHAssocEnumHandlers(ext, ASSOC_FILTER_DEFAULT, &pEnum)` 是那个列表的正规枚举入口
（文档用途写明 "enumerate the app handlers registered for the given file type"）。
列表里有没有自己 ⇒ 那行菜单上有没有自己。这是数据源级证据，比"肉眼看见窗口里有一行"低一级，
但在本机拿不到 UI 的情况下是拿得到的最高一级，而且能复跑。

实测到的四个坑（写死在这里，别让下一个人重踩）
------------------------------------------------------------------
1. `IAssocHandler::GetUIName`（vtable 下标 8）在本机**调一次就 access violation**（25 个处理
   程序全崩在同一槽，ctypes 兜成 OSError）。默认不碰它；`--probe-dead-slots` 可当场复现留证据。
2. `GetName`（下标 7）hr=0 却把 NULL 写进出参，拿不到名字。能用的是 `GetMenuName`（下标 4），
   它返回的就是菜单里那行显示文案（本机实测："迅雷影音"、"Microsoft Clipchamp"、"LuHui播放器"）。
3. vtable 取法：槽地址**必须先解引用**再交给 CFUNCTYPE。第一版直接把 `tbl + 8*i` 当入口点，
   等于把一坨指针字节当代码执行，当场 AV（"access violation writing 0x7ffe…4818"，那正好是
   vtable+0x18 = 第 3 个槽的地址）。
4. **`ASSOC_FILTER_DEFAULT` 这条判据本身没牙**：本机实测拿一个名单外的假扩展名
   （`.qqqzzznotatype`）去问，DEFAULT 照样列出 19 条、里面也有我们——因为 App Paths 注册之后
   shell 会把这个 exe 当成"通用候选"附在任何类型后面。真能区分"是不是因为这个扩展名注册了才
   轮得到它"的是 `ASSOC_FILTER_EXE`：同一个假扩展名下它列出 **0** 条，而 48 个真扩展名下它列出
   1~10 条且每条都有我们。所以本脚本两条都跑，并另设一把鉴别力哨兵（拿假扩展名复查 EXE 过滤器，
   哨兵一旦也"命中"，EXE 那条判据就地降级为 ENV，不许当证据）。

纪律
------------------------------------------------------------------
全程只读：不 Invoke（下标 3 一次也不调）、不写注册表、不改默认程序、不点任何 UI、不发消息。
环境不给（导出缺失／COM 起不来／枚举器一个候选都列不出来／第三方对照组也没列出来）一律记
ENV——不许冒充产品红，也不许因为"环境不给"就顺手放宽判据。
反向对照：`--expect-red` 把 needle 换成一个不可能存在的名字，这本账要求必须翻红，而且同时
要求对照组仍然在列；否则那个"红"是量具瞎，不是名字不对。
退出码：0=全绿；1=有 FAIL；2=ENV（结论不作数）。
"""
from __future__ import annotations

import ctypes as ct
import importlib.util
import os
import struct
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GEN = os.path.join(ROOT, "packaging", "make_launchers.py")
OUT_DIR = os.path.join(HERE, "out")

POINTER_H = ct.POINTER(ct.c_void_p)
HRESULT = ct.c_long
# IAssocHandler：0-2 IUnknown，3 Invoke，4 GetMenuName，5 SetMenuName，6 IsDefault，
#                7 GetName，8 GetUIName，9 GetIconLocation，10 SetDefault。
# IEnumAssocHandlers：0-2 IUnknown，3 Next，4 Skip，5 Reset，6 Clone。
S_RELEASE = 2
S_NEXT = 3
S_GET_MENU_NAME = 4
S_NAME_BROKEN = "<取名字崩>"

RESULTS: list[list[str]] = []
OUT_PATH = ""
DEAD_PROBE: list[str] = []


def say(tag: str, what: str, why: str = "") -> None:
    RESULTS.append([tag, what, why])
    line = f"[{tag}] {what}" + (f" —— {why}" if why else "")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def count(tag: str) -> int:
    return sum(1 for r in RESULTS if r[0] == tag)


def slot(obj: int, index: int) -> int:
    """取接口 obj 的 vtable 第 index 项里**存的函数地址**（槽地址要先解引用）。"""
    tbl = ct.cast(obj, POINTER_H).contents.value
    return ct.cast(tbl + 8 * index, POINTER_H).contents.value


NextT = ct.CFUNCTYPE(HRESULT, ct.c_void_p, ct.c_uint, POINTER_H, ct.POINTER(ct.c_uint))
NameT = ct.CFUNCTYPE(HRESULT, ct.c_void_p, POINTER_H)
ReleaseT = ct.CFUNCTYPE(ct.c_ulong, ct.c_void_p)


def load_generator():
    """扩展名名单和显示名都不许手抄：从生成器取（它再从 player.py 用 AST 取）。"""
    if not os.path.isfile(GEN):
        return None, f"生成器不在盘上：{GEN}"
    try:
        spec = importlib.util.spec_from_file_location("gelu_make_launchers_for_shell_check", GEN)
        ml = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ml)      # type: ignore[union-attr]
    except BaseException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return ml, ""


def open_enum(shell32, ext: str, flt: int = 0) -> tuple[int, str]:
    """返回 (枚举器指针, 错误说明)。指针为 0 ⇒ 说明非空。"""
    pp = ct.c_void_p(0)
    try:
        hr = shell32.SHAssocEnumHandlers(ct.c_wchar_p(ext), ct.c_int(flt), ct.byref(pp))
    except BaseException as exc:
        return 0, f"调用报 {type(exc).__name__}: {str(exc)[:70]}"
    if hr != 0:
        return 0, f"hr={hr & 0xFFFFFFFF:#010x}"
    if not pp.value:
        return 0, "hr=0 但出参是 NULL"
    return pp.value, ""


def collect_names(shell32, ole32, ext: str, flt: int = 0, cap: int = 400) -> tuple[list[str], str]:
    """把该扩展名的候选显示名一条条取出来。flt：0=DEFAULT（用户在列表里看到的那一池）、
    1=EXE（只列该类型解析得到的可执行处理程序，有鉴别力）、2=RECOMMENDED。"""
    enum, err = open_enum(shell32, ext, flt)
    if not enum:
        return [], err
    names: list[str] = []
    try:
        nxt = NextT(slot(enum, S_NEXT))
        while len(names) < cap:
            item = ct.c_void_p(0)
            fetched = ct.c_uint(0)
            r = nxt(enum, 1, ct.byref(item), ct.byref(fetched))
            if r != 0 or not item.value or fetched.value == 0:
                break
            h = item.value
            s = ct.c_void_p(0)
            try:
                hr = NameT(slot(h, S_GET_MENU_NAME))(h, ct.byref(s))
                if s.value:
                    names.append(ct.cast(s.value, ct.c_wchar_p).value or "")
                    ole32.CoTaskMemFree(s.value)
                else:
                    names.append(f"{S_NAME_BROKEN}(hr={hr & 0xFFFFFFFF:#010x})")
            except BaseException as exc:
                names.append(f"{S_NAME_BROKEN}({type(exc).__name__})")
            try:
                ReleaseT(slot(h, S_RELEASE))(h)
            except BaseException:
                pass
    except BaseException as exc:
        return names, f"枚举途中崩：{type(exc).__name__}: {str(exc)[:70]}"
    try:
        ReleaseT(slot(enum, S_RELEASE))(enum)
    except BaseException:
        pass
    return names, ""


def probe_dead_slots(shell32, ole32, ext: str) -> None:
    """只为留证据：实测下标 7/8 在本机的行为。不参与任何判定，默认不跑。"""
    enum, err = open_enum(shell32, ext)
    if not enum:
        DEAD_PROBE.append(f"枚举器起不来（{err}），死槽没测到")
        return
    item = ct.c_void_p(0)
    fetched = ct.c_uint(0)
    if NextT(slot(enum, S_NEXT))(enum, 1, ct.byref(item), ct.byref(fetched)) != 0 or not item.value:
        DEAD_PROBE.append("Next 没给处理程序，死槽没测到")
        return
    h = item.value
    for idx, label in ((7, "GetName"), (8, "GetUIName")):
        s = ct.c_void_p(0)
        try:
            hr = NameT(slot(h, idx))(h, ct.byref(s))
            got = repr(ct.cast(s.value, ct.c_wchar_p).value) if s.value else "NULL"
            DEAD_PROBE.append(f"下标 {idx} {label}：hr={hr & 0xFFFFFFFF:#010x} 出参={got}")
            if s.value:
                ole32.CoTaskMemFree(s.value)
        except BaseException as exc:
            DEAD_PROBE.append(f"下标 {idx} {label}：崩 {type(exc).__name__}: {str(exc)[:60]}")
    try:
        ReleaseT(slot(h, S_RELEASE))(h)
        ReleaseT(slot(enum, S_RELEASE))(enum)
    except BaseException:
        pass


def main() -> int:
    t0 = time.time()
    global OUT_PATH
    OUT_PATH = os.path.join(OUT_DIR, f"check-openwith-shell-{time.strftime('%m%d-%H%M%S')}.log")

    ml, gen_err = load_generator()
    if ml is None:
        say("ENV", "拿不到生成器（名单与显示名不许手抄，取不到就不判）", gen_err)
        return finish(2, t0)
    try:
        exts = ml.media_exts()
    except BaseException as exc:
        say("ENV", "扩展名名单取不出来", f"{type(exc).__name__}: {exc}")
        return finish(2, t0)

    needle = ml.APP_TITLE
    if "--expect-red" in sys.argv:
        needle = f"{ml.APP_TITLE}·这台机器上不存在"
        say("NOTE", "反向对照开着", f"needle 换成不可能存在的 {needle!r}，这本账要求必须翻红")
    say("NOTE", "名单来源与要匹配的名字",
        f"{len(exts)} 个扩展名（AST 取自 player.py 的 MEDIA_EXTS）；needle=APP_TITLE={needle!r}")

    if struct.calcsize("P") != 8:
        say("ENV", "这本账按 64 位指针写死（vtable 步长 8 字节）",
            f"当前指针宽度 {struct.calcsize('P')} 字节")
        return finish(2, t0)
    try:
        shell32 = ct.windll.shell32
        ole32 = ct.windll.ole32
        _ = shell32.SHAssocEnumHandlers
    except BaseException as exc:
        say("ENV", "shell32!SHAssocEnumHandlers 这个导出拿不到", f"{type(exc).__name__}: {exc}")
        return finish(2, t0)
    hr_init = ole32.CoInitializeEx(None, 2)
    if hr_init not in (0, 1, -2147417850):        # S_OK / S_FALSE / RPC_E_CHANGED_MODE 都算能用
        say("ENV", "COM 起不来", f"CoInitializeEx hr={hr_init & 0xFFFFFFFF:#010x}")
        return finish(2, t0)
    ole32.CoTaskMemFree.argtypes = [ct.c_void_p]
    ole32.CoTaskMemFree.restype = None
    shell32.SHAssocEnumHandlers.restype = HRESULT
    shell32.SHAssocEnumHandlers.argtypes = [ct.c_wchar_p, ct.c_int, POINTER_H]
    say("PASS", "量具前提：64 位、SHAssocEnumHandlers 导出在、COM 起来了",
        f"指针 8 字节，CoInitializeEx hr={hr_init & 0xFFFFFFFF:#010x}")

    if "--probe-dead-slots" in sys.argv:
        probe_dead_slots(shell32, ole32, exts[0])
        for ln in DEAD_PROBE:
            say("NOTE", "死槽实测（不参与判定，只是把坑写在这本账里）", ln)

    todo = [e if e.startswith(".") else "." + e for e in exts]
    for i, a in enumerate(sys.argv):
        if a == "--only" and i + 1 < len(sys.argv):
            todo = [e if e.startswith(".") else "." + e for e in sys.argv[i + 1].split(",")]

    missing: list[str] = []
    missing_exe: list[str] = []
    blind: list[str] = []
    errs: list[str] = []
    rows: list[tuple[str, int, int, int]] = []        # (扩展名, DEFAULT 条数, 第三方条数, EXE 条数)
    names_total = 0
    broken = 0
    broken_by_ext: dict[str, int] = {}
    broken_hr: set[str] = set()
    all_broken: list[str] = []
    for ext in todo:
        names, err = collect_names(shell32, ole32, ext, 0)
        if err:
            errs.append(f"{ext}：{err}")
            continue
        exe_names, _e2 = collect_names(shell32, ole32, ext, 1)
        for n in names:
            if S_NAME_BROKEN in n:
                broken_hr.add(n)
        foreign = [n for n in names if n and S_NAME_BROKEN not in n and n != needle]
        rows.append((ext, len(names), len(foreign), len(exe_names)))
        names_total += len(names)
        nb = sum(1 for n in names if S_NAME_BROKEN in n)
        broken += nb
        broken_by_ext[ext] = nb
        if names and nb == len(names):
            all_broken.append(ext)
        if not any(n == needle for n in names):
            missing.append(ext)
        if not any(n == needle for n in exe_names):
            missing_exe.append(ext)
        if len(foreign) < 3:
            blind.append(f"{ext}（第三方只有 {len(foreign)} 条）")

    # 鉴别力哨兵：拿一个名单外的假扩展名当对照。本机实测 ASSOC_FILTER_EXE 在它上面列出 0 条，
    # 而 DEFAULT 照样列出 19 条（含我们自己）——所以"看得见"那条判据本身没牙，
    # 有牙的是 EXE 这条。哨兵要是也"命中"，说明 EXE 这条同样没牙，必须点名而不是闭眼绿。
    probe_ext = ".qqqzzznotatype"
    ctl_default, _c1 = collect_names(shell32, ole32, probe_ext, 0)
    ctl_exe, _c2 = collect_names(shell32, ole32, probe_ext, 1)
    teeth = (needle not in ctl_exe)

    if errs:
        say("ENV", f"{len(errs)} 个扩展名枚举器直接报错（这些条不作数，也不算产品红）", "；".join(errs[:4]))

    if not rows:
        say("ENV", "一条都没枚举成，这本账整体不作数", "")
        return finish(2, t0)

    if blind:
        say("ENV", "对照组没列够第三方候选 ⇒ 枚举器在本机可能是瞎的，命中与缺失都不许当真",
            f"{len(blind)} 个扩展名受影响，前 5 个：" + "；".join(blind[:5]))

    if missing and blind:
        say("ENV", "既没命中、对照组也没列出来——分不清是产品没注册还是量具瞎，不判产品红",
            "缺：" + "、".join(missing[:10]))
    elif missing:
        say("FAIL", f"有 {len(missing)} 个扩展名的「打开方式」列表里没有〈{needle}〉（按全名相等比，不比子串）",
            "缺：" + "、".join(missing[:14]) + (f"…（共 {len(missing)} 个）" if len(missing) > 14 else ""))
    else:
        say("PASS", f"{len(rows)} 个扩展名的「打开方式」列表里都看得见〈{needle}〉（按全名相等比，不比子串）",
            f"DEFAULT 候选总条数 {names_total}；单扩展名最少 {min(r[1] for r in rows)} 条、"
            f"最多 {max(r[1] for r in rows)} 条，其中第三方最少 {min(r[2] for r in rows)} 条")

    # 第二条判据：EXE 过滤器——这条才真正在问"是不是因为这个扩展名注册了才轮得到它"。
    if not teeth:
        say("ENV", f"EXE 这条判据在本机没有鉴别力：假扩展名 {probe_ext} 也照样列出我们 ⇒ 不许拿它当证据",
            f"对照 EXE 候选 {len(ctl_exe)} 条：{ctl_exe[:5]}")
    elif missing_exe:
        say("FAIL", f"有 {len(missing_exe)} 个扩展名在 EXE 过滤器下解析不出我们（= 该类型的注册没生效）",
            "缺：" + "、".join(missing_exe[:14]))
    else:
        say("PASS", f"{len(rows)} 个扩展名在 ASSOC_FILTER_EXE 下都解析出〈{needle}〉"
                    f"（假扩展名对照下列出 {len(ctl_exe)} 条、没有我们 ⇒ 这条判据有鉴别力）",
            f"EXE 候选最少 {min(r[3] for r in rows)} 条、最多 {max(r[3] for r in rows)} 条")

    say("NOTE", "逐扩展名候选条数（脚本自己数；格式 扩展名:DEFAULT 总条数/第三方/EXE 条数）",
        " ".join(f"{e}:{a}/{b}/{c}" for e, a, b, c in rows[:8])
        + (f" …（共 {len(rows)} 条，只列前 8）" if len(rows) > 8 else ""))

    per_max = max(broken_by_ext.values()) if broken_by_ext else 0
    if all_broken:
        say("ENV", f"{len(all_broken)} 个扩展名的候选**全部**取不到名字 ⇒ 取名字这条腿断了，本账不作数",
            "全空的扩展名：" + "、".join(all_broken[:8]))
    elif broken == 0:
        say("PASS", "每条候选的显示名都是真取到的", f"{names_total} 条候选，占位 0 条")
    elif len(broken_hr) == 1 and per_max <= 1:
        say("NOTE", f"外来处理程序里有 {broken} 条自己不吐显示名（每个扩展名恰好 1 条、返回码全相同）",
            f"这一条是 {sorted(broken_hr)[0]!r}：每个扩展名都只摊到 1 条（共跑了 {len(rows)} 个扩展名），"
            f"是某一个外来件自己不吐名字，不是取名字的腿断了——真名取到 {names_total - broken} 条")
    else:
        say("FAIL", f"取名字这条腿不可信：占位 {broken} 条、返回码 {len(broken_hr)} 种、"
                    f"单扩展名最多 {per_max} 条", "样本：" + " | ".join(sorted(broken_hr)[:4]))

    if "--expect-red" in sys.argv:
        ok = (len(missing) >= 1 and len(missing_exe) >= 1 and teeth
              and not blind and names_total > 0)
        say("FAIL" if not ok else "PASS",
            "反向对照：换个不可能存在的名字，两条判据都得翻红，且鉴别力哨兵与对照组都得还在位",
            f"DEFAULT 缺 {len(missing)}／EXE 缺 {len(missing_exe)}／哨兵有牙={teeth}／"
            f"失明扩展名={len(blind)}／候选总数={names_total}")
        return finish(0 if ok else 1, t0)

    rc = 1 if count("FAIL") else (2 if count("ENV") else 0)
    return finish(rc, t0)


def finish(rc: int, t0: float) -> int:
    line = (f"[FINAL] shell 打开方式候选核对 —— PASS {count('PASS')}／FAIL {count('FAIL')}／"
            f"ENV {count('ENV')}／NOTE {count('NOTE')} —— 耗时 {time.time() - t0:.1f}s rc={rc}")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n[DONE]\n")
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        print(f"[ENV] 量具自己崩：{type(exc).__name__}: {exc}", flush=True)
        for ln in traceback.format_exc().strip().splitlines()[-6:]:
            print("      " + ln, flush=True)
        sys.exit(2)
