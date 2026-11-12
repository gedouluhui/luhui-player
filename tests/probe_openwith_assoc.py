"""探针（一次性，不算判据）：直接问 shell「这个扩展名的打开方式候选有哪些」。

为什么换这条路：四扇编程门（FolderItem 里根本没有 openas 动词、InvokeVerb('openas')、
rundll32 shell32 OpenAs_RunDLL、SHOpenWithDialog、OpenWith.exe）在这台 26300 上实测全哑，
而 UIA 也够不着——本趟实测：所有 CabinetWClass（含用户自己开着的三扇）在 UIA 里都只有
1 个子元素（只剩标题栏脚手架），资源管理器的内容树根本摊不下来。

shell32!SHAssocEnumHandlers 是「打开方式」列表的正规数据源（文档写明用于枚举某类型可用的
处理程序，返回 IEnumAssocHandlers）。这里只做只读枚举，不 Invoke、不写注册表。

对照组设计（判"零命中"是不是量具自己瞎，而不是真的没有）：
  · 同一扩展名上，本机已装的第三方播放器（迅雷/PotPlayer 等）应当出现——它们出现说明
    枚举器确实在列第三方应用，那"没有 LuHui播放器"才是产品侧结论；
  · 若连对照组也一个都不列，本脚本一律打 ENV，不许打"没有"。
"""
import ctypes as ct
import ctypes.wintypes as wt
import sys

shell32 = ct.windll.shell32
ole32 = ct.windll.ole32

IEnum_Next = 3          # IEnumAssocHandlers: 0-2 IUnknown, 3 Next, 4 Skip, 5 Reset, 6 Clone
IHandler_GetName = 7    # IAssocHandler: 3 Invoke, 4 GetMenuName, 5 SetMenuName,
IHandler_GetUIName = 8  #                6 IsDefault, 7 GetName, 8 GetUIName, 9 GetIconLocation
IHandler_IsDefault = 6

LPVOID = ct.c_void_p
HRESULT = ct.c_long
POINTER_H = ct.POINTER(ct.c_void_p)
POINTER_I = ct.POINTER(ct.c_int)
shell32.SHAssocEnumHandlers.restype = HRESULT
shell32.SHAssocEnumHandlers.argtypes = [ct.c_wchar_p, ct.c_int, POINTER_H]


def vt(obj, index):
    """取 obj 的 vtable 第 index 项里**存的那个函数地址**。
    （第一版在这里栽过一次：只算到槽地址 `tbl + 8*i` 就交给 CFUNCTYPE 当入口点，
    等于把一坨指针字节当代码执行，直接 access violation。槽地址要先解引用。）"""
    tbl = ct.cast(obj, POINTER_H).contents.value
    slot = ct.cast(tbl + 8 * index, POINTER_H)
    return slot.contents.value


NextT = ct.CFUNCTYPE(HRESULT, ct.c_void_p, ct.c_uint,
                     POINTER_H, ct.POINTER(ct.c_uint))
ReleaseT = ct.CFUNCTYPE(HRESULT, ct.c_void_p)
GetNameT = ct.CFUNCTYPE(HRESULT, ct.c_void_p, POINTER_H)
IsDefaultT = ct.CFUNCTYPE(HRESULT, ct.c_void_p, POINTER_I)


STEP = ["还没开跑"]


def coerce_names(ext, filter_):
    enum_pp = ct.c_void_p(0)
    STEP[0] = f"SHAssocEnumHandlers({ext}, filter={filter_})"
    hr = shell32.SHAssocEnumHandlers(ct.c_wchar_p(ext), ct.c_int(filter_), ct.byref(enum_pp))
    if hr != 0 or not enum_pp.value:
        return None, hr
    enum = enum_pp.value
    out = []
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ct.c_void_p]
    for _ in range(400):
        item = ct.c_void_p(0)
        fetched = ct.c_uint(0)
        STEP[0] = f"第 {len(out)+1} 条：IEnumAssocHandlers::Next"
        r = NextT(vt(enum, IEnum_Next))(enum, 1, ct.byref(item), ct.byref(fetched))
        if r != 0 or not item.value or fetched.value == 0:
            break
        h = item.value
        nm = ct.c_void_p(0)
        ui = ct.c_void_p(0)
        dft = ct.c_int(0)
        STEP[0] = f"第 {len(out)+1} 条：IAssocHandler::GetName（vtable 下标 {IHandler_GetName}）"
        GetNameT(vt(h, IHandler_GetName))(h, ct.byref(nm))
        STEP[0] = f"第 {len(out)+1} 条：IAssocHandler::GetUIName（vtable 下标 {IHandler_GetUIName}）"
        GetNameT(vt(h, IHandler_GetUIName))(h, ct.byref(ui))
        STEP[0] = f"第 {len(out)+1} 条：IAssocHandler::IsDefault（vtable 下标 {IHandler_IsDefault}）"
        IsDefaultT(vt(h, IHandler_IsDefault))(h, ct.byref(dft))
        STEP[0] = "已取完名字，回到循环体"
        s_name = ct.cast(nm.value, ct.c_wchar_p).value if nm.value else ""
        s_ui = ct.cast(ui.value, ct.c_wchar_p).value if ui.value else ""
        is_d = bool(dft.value)
        for p in (nm, ui):
            if p.value:
                ole32.CoTaskMemFree(p.value)
        out.append((s_name, s_ui, is_d))
        print(f"     · name={s_name!r} ui={s_ui!r}", flush=True)
        ReleaseT(vt(h, 2))(h)
    ReleaseT(vt(enum, 2))(enum)
    return out, 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("本脚本只读枚举，不 Invoke 任何处理程序，不写注册表。")
    rc = ole32.CoInitializeEx(None, 2)
    print(f"CoInitializeEx rc={rc:#x}（0=S_OK 1=RPC_E_CHANGED_MODE 都算能用）")
    ext = sys.argv[1] if len(sys.argv) > 1 else ".mkv"
    total_ui = []
    for flt in (0, 1, 2):
        lst, hr = coerce_names(ext, flt)
        tag = {0: "ASSOC_FILTER_DEFAULT", 1: "ASSOC_FILTER_EXE", 2: "ASSOC_FILTER_RECOMMENDED"}[flt]
        if lst is None:
            print(f"[ENV] {tag} 枚举失败 hr={hr:#010x}")
            continue
        print(f"---- {tag} @{ext} 共 {len(lst)} 条（逐条已在上面打过分名）----")
        total_ui += [ui for _n, ui, _d in lst]
    joined = " | ".join(total_ui)
    mine = [x for x in total_ui if ("LuHui" in x or "芦荟" in x or "gelu" in x.lower())]
    third = [x for x in total_ui if any(k in x for k in ("迅雷", "PotPlayer", "VLC", "Clipchamp", "Media Player", "Windows Media"))]
    print(f"合计 {len(total_ui)} 条候选；命中自己 {len(mine)} 条：{mine}")
    print(f"对照组（第三方/系统播放器）命中 {len(third)} 条：{third}")
    if not mine and not third:
        print("[ENV] 连第三方播放器都没列出来 => 这个枚举器在本机不作数，不能据此说『列表里没有自己』")
        return 2
    if not mine:
        print("[FAIL] 枚举器工作正常（对照组在列），但候选里没有格斗芦荟 => 注册姿势没喂到这台 shell")
        return 1
    print("[PASS] shell 的类型处理程序枚举里就有格斗芦荟（=打开方式那个列表的数据源里有自己）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        tb = traceback.format_exc().strip().splitlines()
        print(f"[ENV] 量具自己报：{type(exc).__name__}: {exc}")
        print(f"      崩在：{STEP[0]}")
        for ln in tb[-6:]:
            print("      " + ln)
        sys.exit(2)
