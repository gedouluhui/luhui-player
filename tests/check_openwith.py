"""「打开方式」注册的**只读**核对器：对照盘上那份 .reg 与 HKCU 的现数，一个字都不写。

它存在的理由：`packaging/reg/注册到打开方式.reg` 一旦导进 HKCU 就是不可逆的用户决定，
而"导没导成功、导的时候有没有顺手改了别人的默认程序"这两件事只有注册表自己知道。
所以这个文件只干三件事：读 .reg（用生成器自己的解码口径）、读 HKCU（winreg，只 OpenKey
不调用任何写接口）、把两份对账。**绝不**跑 `reg import`、**绝不**改产品文件。

跑法：
    python tests/check_openwith.py --check [输出.json]     # 默认：模板 lint ＋ 注册表现数对账
    python tests/check_openwith.py --snapshot [路径]        # 把相关现状抄成 JSON（只写到这个路径）
    python tests/check_openwith.py --diff 甲.json 乙.json   # 证明导入没动它不该动的东西
    python tests/check_openwith.py --selftest              # 自证有牙：每种坏法必须被点名

退出码：**0** 全部通过；**1** 至少一条真失败（包括"键根本不存在＝还没导入"）；
**2** 环境不可用（winreg 读不到、快照文件解不开、生成器加载不了）。环境问题绝不允许
冒充产品失败，读不到的项一律记 NOTE 并**不计入 PASS**。

四道口径：
1. **键路径在代码里一律用 BS（chr(92)）拼（kp / vpath / show），注释与正文里写路径一律用
   正斜杠**：源码字面量里的双反斜杠会被写文件的工具静默折成单反斜杠，本机踩过；
   所以这份文件里除了 BS 本身，不出现任何带反斜杠的字符串字面量。
2. **读文本一律 newline 传空串**（CRLF 是要核对的东西之一），**写 JSON 一律 newline 传 LF**：
   io.open 的默认换行会把整份 LF 文件换成 CRLF，那份"快照"下一步对账时字节就变了。
   写完自己回读字节核对一次。
3. **扩展名名单从生成器现取**（`media_exts()`，本机 48 条），不手抄：抄的名单会在 app
   加格式那天悄悄落后，而注册表里的落后是看不见的。
4. **--diff 只允许三种变化**：① 我们那三根键**新出现**；② 每个 `OpenWithProgids` 名单里
   **多出我们那条 PROGID**；③ 扩展名键的默认值由 `None`（那本快照里这根键/这个值根本不存在）
   变成 `''`（空串）——`reg import` 建键时必然留下这个空默认值，它不是"改了默认程序"。
   反过来「把用户原有的默认值清空」（如 `'mpegvideo' → ''`，自测里的 ⑫）与任何两个非空值
   之间的变化都必须翻红，`None → 别人的 PROGID`（⑬）同样翻红。
   别的一切变化（UserChoice、别人在 OpenWithProgids 里的条目、App Paths 里别人的键）一律算
   真失败——这正是这份文件要证明的事。三条豁免各配一条哨兵（自测里的 ⑪⑫⑬），见 `--selftest`。

键路径一律写成 `HKCU 下 Software/Classes/...` 这种不带反斜杠的形式，见第 1 条。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GEN = os.path.join(ROOT, "packaging", "make_launchers.py")

# 按**文件路径**加载生成器：`packaging` 这个名字在本机 site-packages 里是个真包
# （anaconda 装了 packaging 库），`from packaging import ...` 会绑到那边去。
try:
    _spec = importlib.util.spec_from_file_location("gelu_make_launchers", GEN)
    ml = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ml)
    GEN_ERR = ""
except BaseException as exc:      # 生成器读不起来＝环境不可用，不许冒充"产品不合格"
    ml = None
    GEN_ERR = f"{type(exc).__name__}: {exc}"

try:
    import winreg
    WINREG_ERR = ""
except ImportError as exc:        # 非 Windows／没有 winreg：只能算环境不可用（rc=2）
    winreg = None
    WINREG_ERR = f"winreg 不可用：{exc}"

PROGID = "" if ml is None else ml.PROGID
KEY_PROGID = "" if ml is None else ml.KEY_PROGID
KEY_APP = "" if ml is None else ml.KEY_APP
KEY_APPPATHS = "" if ml is None else ml.KEY_APPPATHS
HKCU = "" if ml is None else ml.HKCU
LAUNCH_EXE = os.path.join(ROOT, "luhui-player.exe") if ml is None else ml.LAUNCH_EXE
APP_TITLE = "LuHui播放器" if ml is None else ml.APP_TITLE
REG_OW = "" if ml is None else ml.REG_OW
REG_OW_DEL = "" if ml is None else ml.REG_OW_DEL
BS = chr(92) if ml is None else ml.BS

OWN_KEYS = (KEY_PROGID, KEY_APP, KEY_APPPATHS)
DEFAULT_SNAPSHOT = os.path.join(HERE, "out", "openwith-snapshot.json")
SNAP_KEYS = ("generated_at", "progid", "reg_md5", "reg_del_md5", "exe_md5",
             "own_keys", "extensions", "class_defaults")
MAX_LIST = 10


# ---------------------------------------------------------------- 小工具
def kp(*parts: str) -> str:
    """拼一条相对 HKCU 的键路径（反斜杠只从这里来）。"""
    return BS.join(parts)


def show(rel: str) -> str:
    """把相对路径打印成完整键路径。"""
    return rel if rel.upper().startswith(HKCU.upper()) else HKCU + BS + rel


def vpath(path: str, name: str) -> str:
    """打印成一个值的位置：键路径 + 值名（默认值用 @ 表示）。反斜杠只从 BS 来。"""
    return BS.join((path, name if name else "@"))


def say(tag: str, name: str, detail: str = "") -> None:
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))


def md5_or_dash(path: str) -> str:
    try:
        return hashlib.md5(open(path, "rb").read()).hexdigest()
    except OSError:
        return "-"


def exts() -> list[str]:
    return ml.media_exts()


# ---------------------------------------------------------------- 只读注册表
def value_text(data, typ) -> str:
    """把一个注册表值归一成可比较、可进 JSON 的字符串。
    非 REG_SZ（DWORD／二进制／可扩展字符串……）一律带上类型标记——类型也是内容的一部分，
    把 DWORD:0 当成 "0" 放行就是瞎判据。"""
    if typ == getattr(winreg, "REG_SZ", 1) and isinstance(data, str):
        return data
    return f"#{typ}:{data!r}"


def reg_values(rel: str) -> tuple[dict | None, str, bool]:
    """读 HKCU 下某根键的全部值 → (值, 原因, 是否环境问题)。
    值 None 时：第二项给原因；第三项 True 表示"读不了"而不是"不存在"（那种情况绝不判红）。
    这个函数里出现的 winreg 调用只有 OpenKey/QueryInfoKey/EnumValue/CloseKey，全是读。"""
    if winreg is None:
        return None, WINREG_ERR or "winreg 不可用", True
    try:
        h = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel)
    except FileNotFoundError:
        return None, "键不存在", False
    except PermissionError as exc:
        return None, f"没权限读：{exc}", True
    except OSError as exc:
        return None, f"读不了：{exc}", True
    try:
        out: dict[str, str] = {}
        i = 0
        while True:
            try:
                name, data, typ = winreg.EnumValue(h, i)
            except OSError:
                break
            i += 1
            out[name] = value_text(data, typ)
        return out, "", False
    except OSError as exc:
        return None, f"枚举值失败：{exc}", True
    finally:
        try:
            winreg.CloseKey(h)
        except OSError:
            pass


def reg_children(rel: str, warn: list[str]) -> list[str]:
    """某根键的直接子键名（只读）。读不到就记一条 warn，不抛。"""
    if winreg is None:
        return []
    try:
        h = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel)
    except OSError as exc:
        warn.append(f"{show(rel)} 的子键读不到：{exc}")
        return []
    try:
        names = []
        i = 0
        while True:
            try:
                names.append(winreg.EnumKey(h, i))
            except OSError:
                break
            i += 1
        return sorted(names)
    finally:
        try:
            winreg.CloseKey(h)
        except OSError:
            pass


def reg_default(rel: str, warn: list[str]) -> str | None:
    """扩展名键的**默认值**（那就是"这类文件默认用谁打开"的一半真相）。None＝没有。"""
    vals, why, env = reg_values(rel)
    if env:
        warn.append(f"{show(rel)}：{why}")
    if vals is None:
        return None
    return vals.get("", "")


def reg_userchoice(ext: str, warn: list[str]) -> str | None:
    """Explorer/FileExts/<ext>/UserChoice 里的 ProgID（Win10/11 的默认程序真源）。"""
    rel = kp("Software", "Microsoft", "Windows", "CurrentVersion",
             "Explorer", "FileExts", ext, "UserChoice")
    vals, why, env = reg_values(rel)
    if env:
        warn.append(f"{show(rel)}：{why}")
    if vals is None:
        return None
    return vals.get("ProgID", "")


# ---------------------------------------------------------------- .reg → 期望
def parse_value_line(line: str) -> tuple[str, str] | None:
    """一行值 → (值名, 值)；`@` 的值名用空串。认不出来的形态回 None（由上层记 NOTE）。"""
    head, sep, rest = line.partition("=")
    if not sep:
        return None
    head = head.strip()
    rest = rest.strip()
    if head == "@":
        name = ""
    elif head.startswith('"') and head.endswith('"') and len(head) > 1:
        name = ml._reg_unescape(head[1:-1])
    else:
        return None
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        return name, ml._reg_unescape(rest[1:-1])
    return None            # DWORD:／hex(...)/`-`（删除）这类这份模板用不着的形态


def expected_from_reg(body: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """把 .reg 正文拆成「相对 HKCU 的键路径 → 该键应有的值」，用生成器自己的分节器。"""
    sections = ml._reg_sections(body)
    want: dict[str, dict[str, str]] = {}
    skipped: list[str] = []
    for key, dele, vals in sections:
        if dele:
            skipped.append(f"这节是删除件（不在本模板的活儿）：{show(key)}")
            continue
        if not key.upper().startswith((HKCU + BS).upper()):
            skipped.append(f"根不是 {HKCU}，跳过：{show(key)}")
            continue
        rel = key[len(HKCU) + 1:]
        cur = want.setdefault(rel, {})
        for line in vals:
            got = parse_value_line(line)
            if got is None:
                skipped.append(f"认不出的值行（不猜）：{show(rel)} :: {line[:56]}")
                continue
            cur[got[0]] = got[1]
    return sorted(want.items()), skipped


# ---------------------------------------------------------------- --check
def check() -> int:
    items: list[tuple[str, bool, str]] = []
    env: list[str] = []

    if winreg is None:
        say("ENV", "读注册表的能力", WINREG_ERR)
        return 2

    # (a) 盘上那份 .reg 过生成器自己的 lint
    body = ml.read_reg(REG_OW)
    if not body:
        items.append(("盘上那份 .reg 读得出来（UTF-16LE 带 BOM）", False, REG_OW))
        items.append(("模板过 lint_openwith", False, "正文都没读出来，没法判"))
    else:
        problems = ml.lint_openwith(body)
        items.append(("盘上那份 .reg 读得出来（UTF-16LE 带 BOM）", True,
                      f"{len(body)} 字符、md5={md5_or_dash(REG_OW)}"))
        items.append(("模板过 lint_openwith（生成器自己的闸门，本文件不另立一套）",
                      not problems, "；".join(problems)[:300] or "干净"))

    want, skipped = ([], [])
    if body:
        want, skipped = expected_from_reg(body)
        for s in skipped:
            say("NOTE", s)
        ids = {k.upper() for k, _v in want}
        for top in OWN_KEYS:
            items.append((f"模板里有我们那三根键之一：{show(top)}", top.upper() in ids,
                          "" if top.upper() in ids else "这份 .reg 里没有这一根"))
    for s in skipped:
        items.append((f"值行可解析：{s[:60]}", False, "这一条没能核对，不算通过"))

    # (b) 注册表现数：逐条对账
    matched = missing = mismatched = unknown = 0
    bad_names: list[str] = []
    for rel, vals in want:
        live, why, is_env = reg_values(rel)
        if is_env:
            unknown += len(vals)
            env.append(f"{show(rel)}：{why}（{len(vals)} 条值没核对，不算通过）")
            continue
        for name, exp in vals.items():
            label = vpath(show(rel), name)
            if live is None:
                missing += 1
                bad_names.append(f"{label} —— 键不存在（{why}）")
                continue
            if name not in live:
                missing += 1
                bad_names.append(f"{label} —— 值不存在（键在，里面 {len(live)} 条值是："
                                 + "、".join(sorted(live))[:70] + "）")
                continue
            if live[name] != exp:
                mismatched += 1
                bad_names.append(f"{label} —— 现数 {live[name]!r} 与模板 {exp!r} 不一致")
            else:
                matched += 1
    total = matched + missing + mismatched
    if want:
        ok = missing == 0 and mismatched == 0 and unknown == 0
        items.append((f"注册表现数逐条对账（共 {total} 条值：一致 {matched}／不符 {mismatched}"
                      f"／缺失 {missing}／没核对 {unknown}）", ok,
                      "都对上" if ok else f"有 {mismatched + missing} 条没对上"))
    for line in bad_names[:MAX_LIST]:
        print("       · " + line)
    if len(bad_names) > MAX_LIST:
        print(f"       · 还有 {len(bad_names) - MAX_LIST} 条同类问题没打印（计数已算进去）")
    for e in env:
        say("NOTE", "环境：这条没核对，不算通过", e)

    # (c) 启动器：文件在 ＋ FileDescription 就是播放器名（列表里那行字就是它）
    exe_ok = os.path.exists(LAUNCH_EXE)
    items.append(("启动器 exe 在盘上", exe_ok, LAUNCH_EXE))
    if exe_ok:
        try:
            got = ml.file_description(LAUNCH_EXE)
        except Exception as exc:
            got = None
            items.append(("启动器的 FileDescription ＝「" + APP_TITLE + "」", False,
                          f"读不出（{type(exc).__name__}: {exc}）"))
        else:
            items.append(("启动器的 FileDescription ＝「" + APP_TITLE + "」",
                          got == APP_TITLE, f"实测 {got!r}；md5={md5_or_dash(LAUNCH_EXE)}"))
    else:
        say("NOTE", "启动器的 FileDescription", "exe 不在盘上，这一条没核对，不算通过")

    # (d) 汇总
    print(f"\n== 对账：模板 {len(want)} 节 / {total} 条值 —— 一致 {matched}、"
          f"不符 {mismatched}、缺失 {missing}、没核对 {unknown} ==")
    for name, ok, detail in items:
        say("PASS" if ok else "FAIL", name, detail)
    fails = [n for n, ok, _d in items if not ok]
    if missing and not fails:
        fails.append("内部不一致：缺失计数没进判定")
    print(f"\n共 {len(items)} 项判定，失败 {len(fails)} 项"
          + (f"：{'；'.join(fails)[:300]}" if fails else ""))
    if not fails:
        say("NOTE", "注册表里已经是我们那套注册了（本文件只读，什么都没改）",
            f"要撤就双击 {REG_OW_DEL}")
    else:
        say("NOTE", "缺失＝还没导入", f"要导入就双击 {REG_OW}；本文件绝不代劳")
    if env:
        say("ENV", f"有 {len(env)} 处环境读不到（见上面的 NOTE），不算通过也不算产品失败")
        return 2
    return 1 if fails else 0


# ---------------------------------------------------------------- 快照
def snapshot_own_keys(warn: list[str]) -> dict:
    """把三根自己的键（连同子键）抄成 {相对键路径: {值名: 值}}；读不到记 null。"""
    out: dict[str, object] = {}

    def walk(rel: str) -> None:
        vals, why, env = reg_values(rel)
        if vals is None:
            if env:
                warn.append(f"{show(rel)}：{why}")
            out[rel] = None
            return
        out[rel] = {k: vals[k] for k in sorted(vals)}
        for sub in reg_children(rel, warn):
            walk(kp(rel, sub))

    for top in OWN_KEYS:
        walk(top)
    return out


def snapshot_data() -> dict:
    warn: list[str] = []
    ext_list = exts()
    exts_map: dict[str, dict] = {}
    defaults: dict[str, object] = {}
    for ext in ext_list:
        dflt = reg_default(kp("Software", "Classes", ext), warn)
        vals, why, env = reg_values(kp("Software", "Classes", ext, "OpenWithProgids"))
        if env:
            warn.append(f"{show(kp('Software', 'Classes', ext, 'OpenWithProgids'))}：{why}")
        names = sorted(vals) if vals is not None else None
        exts_map[ext] = {"default": dflt, "openwith": names,
                         "userchoice": reg_userchoice(ext, warn)}
        defaults[ext] = dflt
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "progid": PROGID,
        "reg_md5": md5_or_dash(REG_OW),
        "reg_del_md5": md5_or_dash(REG_OW_DEL),
        "exe_md5": md5_or_dash(LAUNCH_EXE),
        "ext_count": len(ext_list),
        "own_key_paths": [show(k) for k in OWN_KEYS],
        "warnings": warn,
        "own_keys": snapshot_own_keys(warn),
        "extensions": exts_map,
        "class_defaults": defaults,
    }


def write_snapshot(path: str) -> int:
    if winreg is None:
        say("ENV", "读注册表的能力", WINREG_ERR)
        return 2
    data = snapshot_data()
    try:
        os.makedirs(os.path.dirname(path) or HERE, exist_ok=True)
    except OSError as exc:
        say("ENV", "快照目录建不出来", f"{path} —— {exc}")
        return 2
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        say("ENV", "快照写不出去", f"{path} —— {exc}")
        return 2
    raw = open(path, "rb").read()
    try:
        raw.decode("utf-8", "strict")
        dec = "UTF-8 严格解得开"
        dec_ok = True
    except UnicodeDecodeError as exc:
        dec, dec_ok = f"UTF-8 解不开：{exc}", False
    cr_bytes = raw.count(bytes([13]))      # 只数真 CR 字节：JSON 里的反斜杠是转义串，不掺和
    say("PASS" if dec_ok else "FAIL", f"快照写出来了：{path}", f"{len(raw)} 字节；{dec}")
    say("PASS" if not cr_bytes else "FAIL",
        "快照是 LF-only（没被 io.open 的默认换行改成 CRLF）",
        f"CR 字节数={cr_bytes}，LF 字节数={raw.count(bytes([10]))}")
    return 0 if (dec_ok and not cr_bytes) else 1


def load_snapshot(path: str) -> tuple[dict | None, str]:
    try:
        raw = open(path, "rb").read()
    except OSError as exc:
        return None, f"读不了：{exc}"
    try:
        got = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"不是合法的 UTF-8 JSON：{type(exc).__name__}: {exc}"
    if not isinstance(got, dict):
        return None, f"顶层不是对象（是 {type(got).__name__}）"
    return got, ""


# ---------------------------------------------------------------- --diff
def as_snap(x, who: str) -> tuple[dict | None, str]:
    if isinstance(x, dict):
        return x, ""
    got, err = load_snapshot(str(x))
    return got, (f"{who}本（{x}）{err}" if err else "")


def names_of(entry: dict) -> list[str] | None:
    """某扩展名的 OpenWithProgids 值名清单；None＝没读到/键不存在。"""
    if not isinstance(entry, dict):
        return None
    return entry.get("openwith")


def compare(a, b) -> tuple[list[str], list[str], list[str]]:
    """两本快照对账（纯函数，a/b 可以是 dict 或 json 路径）→ (真失败, 允许的变化, 环境问题)。
    允许的只有两件事：我们那三根键从没有到有、每个 OpenWithProgids 名单里多出 PROGID。
    其余一切变化都进第一格——包括 UserChoice 与扩展名键的默认值（改默认程序的那两扇门）。"""
    problems: list[str] = []
    allowed: list[str] = []
    env: list[str] = []

    sa, ea = as_snap(a, "甲")
    sb, eb = as_snap(b, "乙")
    env += [e for e in (ea, eb) if e]
    if env:
        return problems, allowed, env
    for snap, who in ((sa, "甲"), (sb, "乙")):
        lack = [k for k in SNAP_KEYS if k not in snap]
        if lack:
            env.append(f"{who}本快照缺字段 {lack}：这一本没法对账，不算通过")
    if env:
        return problems, allowed, env

    # 1) 我们那三根键（连同子键）
    oa, ob = sa["own_keys"], sb["own_keys"]
    if not isinstance(oa, dict) or not isinstance(ob, dict):
        env.append("own_keys 不是对象：两本快照有一本没抄到这一层")
    else:
        for rel in sorted(set(oa) | set(ob)):
            va, vb = oa.get(rel), ob.get(rel)
            path = show(rel)
            if va is None and isinstance(vb, dict):
                allowed.append(f"允许：我们的键新出现了 —— {path}（{len(vb)} 条值）")
                continue
            if vb is None and isinstance(va, dict):
                problems.append(f"我们的键没了 —— {path}（甲本有 {len(va)} 条值）")
                continue
            if not (isinstance(va, dict) and isinstance(vb, dict)):
                continue
            for name in sorted(set(va) | set(vb)):
                x, y = va.get(name), vb.get(name)
                if x == y:
                    continue
                here = vpath(path, name)
                if x is None:
                    problems.append(f"我们的键里冒出甲本没有的值 —— {here}＝{y!r}")
                elif y is None:
                    problems.append(f"我们键里的值消失了 —— {here}（甲本 {x!r}）")
                else:
                    problems.append(f"我们键里的值被改了 —— {here}：{x!r} → {y!r}")

    # 2) 扩展名的三个通道
    efa, efb = sa["extensions"], sb["extensions"]
    cda, cdb = sa["class_defaults"], sb["class_defaults"]
    if not all(isinstance(x, dict) for x in (efa, efb, cda, cdb)):
        env.append("extensions / class_defaults 有一本不是对象：这一层没法对账")
        return problems, allowed, env
    blank = {"default": None, "openwith": None, "userchoice": None}
    for ext in sorted(set(efa) | set(efb) | set(cda) | set(cdb)):
        xa = efa.get(ext, blank)
        xb = efb.get(ext, blank)
        extp = show(kp("Software", "Classes", ext))
        owp = show(kp("Software", "Classes", ext, "OpenWithProgids"))
        ucp = show(kp("Software", "Microsoft", "Windows", "CurrentVersion",
                      "Explorer", "FileExts", ext, "UserChoice"))
        # 2a) 默认程序（UserChoice）：一个字节都不许动
        ua, ub = xa.get("userchoice"), xb.get("userchoice")
        if ua != ub:
            problems.append("UserChoice 的 ProgID 变了（那就是替用户改了默认打开方式） —— "
                            + vpath(ucp, "ProgID") + f"：{ua!r} → {ub!r}")
        # 2b) 扩展名键的默认值（"默认程序"的另一半）：两条独立通道各查一次
        da, db = xa.get("default"), xb.get("default")
        cca, ccb = cda.get(ext), cdb.get(ext)

        def dflt_exempt(a, b) -> bool:
            """`None → ''` 这一种不是"改了默认程序"：None＝那本快照里这根键根本不存在，
            ''＝键在、但默认值一个字节都没写（QueryValue 读出来就是空）。注册
            `Classes\\.ext\\OpenWithProgids` 必然连带新建那根父键，所以这是导入**必然**
            产生的一种变化——本机实测导入后 `.eac3` 是 0 个值，HKCR 也读不到默认值，
            用户原来的关联状态一点没动。
            反过来，`'xmp.mkv' → ''`（清空）或任何非空值之间的变化都必须翻红。
            "键在、有人把默认值写成空串"这一种在这里读不出来，它由**字节闸**兜住：
            lint_openwith 与本文件的 items_reg_openwith 都要求模板里不许出现裸扩展名节
            的 `@=` 行——所以这条豁免只放行"没写过"，不放过"写了空的"。
            """
            return a is None and b == ""

        both_exempt = dflt_exempt(da, db) and dflt_exempt(cca, ccb)
        if da != db:
            if both_exempt:
                allowed.append("允许：这根扩展名键是导入新建的，默认值没写 —— " + vpath(extp, ""))
            else:
                problems.append(f"通道①：Classes 下扩展名键的默认值变了 —— "
                                f"{vpath(extp, '')}：{da!r} → {db!r}")
        if cca != ccb and not both_exempt:
            problems.append(f"通道②：class_defaults 里同一处变了 —— "
                            f"{vpath(extp, '')}：{cca!r} → {ccb!r}")
        # 同一本快照的两条通道也得自洽——有一本抄歪了要当场点名，不许拿它当对账依据
        if ext in efa and da != cca:
            problems.append(f"甲本自相矛盾 —— {extp}：extensions.default 与 class_defaults 不同")
        if ext in efb and db != ccb:
            problems.append(f"乙本自相矛盾 —— {extp}：extensions.default 与 class_defaults 不同")
        # 2c) OpenWithProgids：只许多我们那一条，别的一条都不许动
        la, lb = names_of(xa), names_of(xb)
        ga = sorted(la) if la else []
        gb = sorted(lb) if lb else []
        for n in ga:
            if n not in gb:
                problems.append("OpenWithProgids 里的值消失了 —— " + vpath(owp, n)
                                + ("" if n == PROGID else "（那是别人添的条目）"))
        for n in gb:
            if n not in ga:
                if n == PROGID:
                    allowed.append("允许：我们的值新添进了 OpenWithProgids —— " + vpath(owp, n))
                else:
                    problems.append("OpenWithProgids 里冒出了别人的值 —— " + vpath(owp, n))
    return problems, allowed, env


def compare_flat(pair) -> list[str]:
    """自测用的判据：把真失败与环境问题合成一份差异清单（"允许的变化"不算差异）。"""
    problems, _allowed, env = compare(pair[0], pair[1])
    return problems + env


def diff_files(path_a: str, path_b: str) -> int:
    problems, allowed, env = compare(path_a, path_b)
    for line in problems:
        say("FAIL", "改动超出允许范围", line)
    for line in allowed:
        say("NOTE", "允许的变化（导入本来就该产生这一种）", line)
    for line in env:
        say("ENV", "对账没跑成", line)
    print(f"\n== 对账：{os.path.basename(path_a)} → {os.path.basename(path_b)} —— "
          f"真失败 {len(problems)} 条／允许的变化 {len(allowed)} 条／环境问题 {len(env)} 条 ==")
    if env:
        return 2
    return 1 if problems else 0


# ---------------------------------------------------------------- 自测（自证有牙）
def synthetic_snapshot() -> dict:
    """自测的底本＝**导入之前**那本：完全离线、字段与 --snapshot 一致，绝不碰注册表。
    底本必须真的没有我们的东西（own_keys 空、名单里没 PROGID）——上一版把底本写成了
    "已经有了"，于是⑩那条"导入的正常样子"反过来被判成"值消失了"，好样本恒红。
    """
    return {
        "generated_at": "2026-10-20T08:00:00+0800",
        "progid": PROGID,
        "reg_md5": "a" * 32,
        "reg_del_md5": "b" * 32,
        "exe_md5": "c" * 32,
        "own_key_paths": [],
        "warnings": [],
        "own_keys": {},
        "extensions": {
            ".mkv": {"default": "mpegvideo", "openwith": sorted(["Xmp.mkv", "bilipc"]),
                     "userchoice": "WMP11.mkv"},
            ".mp4": {"default": "mp4file", "openwith": sorted(["bilipc", "Xmp.mp4"]),
                     "userchoice": "WMP11.mp4"},
            ".ape": {"default": None, "openwith": None, "userchoice": None},
        },
        "class_defaults": {".mkv": "mpegvideo", ".mp4": "mp4file", ".ape": None},
    }


def own_keys_data() -> dict:
    """导入之后我们那四节**应该**长什么样——和 .reg 里的写法逐字对得上，值名用 `""` 表示默认值。"""
    return {
        KEY_PROGID: {"": PROGID, "FriendlyTypeName": APP_TITLE},
        kp(KEY_PROGID, "shell", "open", "command"): {"": '"' + LAUNCH_EXE + '" "%1"'},
        KEY_APP: {"FriendlyAppName": APP_TITLE},
        KEY_APPPATHS: {"": LAUNCH_EXE, "Path": ROOT},
    }


def after_import(base: dict) -> dict:
    """模拟"导入之后该有的样子"：我们的键出现、每个名单多我们那一条，别的一个字节都不动。"""
    out = json.loads(json.dumps(base, ensure_ascii=False))
    out["own_key_paths"] = [show(k) for k in OWN_KEYS]
    out["own_keys"] = own_keys_data()
    for ext, entry in out["extensions"].items():
        entry["openwith"] = sorted(set(entry["openwith"] or []) | {PROGID})
    return out


def mutate(base: dict, path: tuple, op: str = "set", arg=None) -> dict:
    """所有坏样本都从这里出。三道证：① 路径真存在；② 要动的东西真在里面（要加的真不在）；
    ③ 改完之后与底本不一样。任一道不成立就抛异常，由分拣记成 NOP——静默落空等于这条
    案例根本没测，绝不允许冒充"抓到"或"干净"（生成器那边就是这么被抓过一次）。"""
    out = json.loads(json.dumps(base, ensure_ascii=False))
    cur = out
    for p in path[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            raise AssertionError("路径不存在，注入落空：" + "/".join(str(x) for x in path))
        cur = cur[p]
    last = path[-1]
    if op in ("set", "delkey"):
        if not isinstance(cur, dict) or last not in cur:
            raise AssertionError("这一处键不存在，注入落空：" + "/".join(str(x) for x in path))
        if op == "set":
            if cur[last] == arg:
                raise AssertionError("注入的值与原值相同，等于没改：" + str(arg)[:40])
            cur[last] = arg
        else:
            del cur[last]
    elif op in ("drop", "add"):
        lst = cur.get(last) if isinstance(cur, dict) else None
        if not isinstance(lst, list):
            raise AssertionError("这一处不是名单：" + "/".join(str(x) for x in path))
        if op == "drop" and arg not in lst:
            raise AssertionError(f"名单里没有 {arg}，注入落空")
        if op == "add" and arg in lst:
            raise AssertionError(f"名单里已经有 {arg}，等于没改")
        (lst.remove if op == "drop" else lst.append)(arg)
    else:
        raise AssertionError(f"不认识的注入操作：{op}")
    if out == base:
        raise AssertionError("注入之后快照一个字没变")
    return out


def verdict(fn, make, expect_red: bool) -> tuple[str, str]:
    """每条已知答案的分拣。标签只由这一个函数产出 ⇒ 末尾那行计数与标签同源。
    CAUGHT 坏样本被点名／MISSED 坏样本没抓到／CLEAN 好样本干净／
    NOP 注入落空（那条等于没测）／JUNK 判据自己崩了（崩≠抓到，单独一家）。

    `expect_red` 说的是"**一台好仪器**该不该翻红"，不是"我希望这本账写什么"：
    那条恒真对照就是拿坏样本喂给一台瞎判据，expect_red=True 而它不翻红 ⇒ 记 MISSED。
    上一版把"期望标签"直接当参数传进来，于是 MISSED 这一家永远拿不到——恒真对照
    被分拣成 CLEAN，这本账看着全绿、其实根本没牙。
    """
    try:
        sample = make() if callable(make) else make
    except BaseException as exc:
        return "NOP", f"{type(exc).__name__}: {exc}"[:96]
    try:
        got = fn(sample)
    except BaseException as exc:
        return "JUNK", f"判据崩：{type(exc).__name__}: {exc}"[:96]
    if not isinstance(got, list):
        return "JUNK", f"判据回的是 {type(got).__name__} 不是差异清单"
    detail = ("差异=" + "；".join(str(g) for g in got))[:96] if got else "干净"
    red = bool(got)
    if red == expect_red:
        return ("CAUGHT" if red else "CLEAN"), detail
    return "MISSED", detail


def selftest() -> int:
    """判据必须能在这些坏样本面前翻红，在好样本面前闭嘴——恒真的对账比没有对账更坏。"""
    if winreg is None:
        say("NOTE", "自测里有几条要走 --snapshot 落盘（temp 目录）；"
                    "没有 winreg 也能跑，因为底本是内存里造的")
    base = synthetic_snapshot()
    good = after_import(base)
    tmp = tempfile.mkdtemp(prefix="gelu-openwith-selftest-")

    def bad_json_pair():
        p = os.path.join(tmp, "坏.json")
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('{"generated_at": "2026')       # 截断：不是合法 JSON
        return (p, base)

    def missing_key_pair():
        p = os.path.join(tmp, "缺字段.json")
        slim = json.loads(json.dumps(base, ensure_ascii=False))
        del slim["class_defaults"]
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(slim, ensure_ascii=False, sort_keys=True))
        return (p, base)

    def blind(pair) -> list[str]:
        return []                                  # 故意做的"瞎判据"：它必须被这本账记成 MISSED

    cases = [
        # 名字, 判据, 样本, 好仪器该不该翻红, 这本账期望的标签
        # 坏样本一律从 `good`（导入之后的样子）出发：对账比的是两本快照的差异，
        # 拿 `base`（还没导入）当甲本去注入"我们的东西"必然落空——上一版 ⑥⑦ 就是这样
        # 悄悄变成 NOP 的，而且路径还少写了 "own_keys" 一层。
        ("① UserChoice 的 ProgID 被改了（改默认程序）", compare_flat,
         lambda: (good, mutate(good, ("extensions", ".mkv", "userchoice"),
                               "set", "PotPlayerMKV")), True, "CAUGHT"),
        ("② Classes 下扩展名键的默认值被改了（另一扇默认程序门）", compare_flat,
         lambda: (good, mutate(good, ("class_defaults", ".mp4"), "set", "Thunder.MP4")),
         True, "CAUGHT"),
        ("③ 别人的值从 OpenWithProgids 名单里消失（撤销件写歪就会这样）", compare_flat,
         lambda: (good, mutate(good, ("extensions", ".mkv", "openwith"),
                               "drop", "Xmp.mkv")), True, "CAUGHT"),
        ("④ 我们那条 PROGID 在某个扩展名里缺席（注册没到位）", compare_flat,
         lambda: (good, mutate(good, ("extensions", ".ape", "openwith"),
                               "drop", PROGID)), True, "CAUGHT"),
        ("⑤a 快照文件不是合法 JSON（截断）", compare_flat, bad_json_pair, True, "CAUGHT"),
        ("⑤b 快照缺 class_defaults 字段", compare_flat, missing_key_pair, True, "CAUGHT"),
        ("⑥ 我们的键被人删了一根（甲本有、乙本没）", compare_flat,
         lambda: (good, mutate(good, ("own_keys", KEY_APPPATHS), "delkey")), True, "CAUGHT"),
        ("⑦ 我们键里的值被改（App Paths 的默认值换成别人的 exe）", compare_flat,
         lambda: (good, mutate(good, ("own_keys", KEY_APPPATHS, ""), "set",
                               "C:" + BS + "who" + BS + "exe.exe")), True, "CAUGHT"),
        ("⑧ OpenWithProgids 里冒出第三条不是我们的值", compare_flat,
         lambda: (good, mutate(good, ("extensions", ".mp4", "openwith"),
                               "add", "SomePlayer.mp4")), True, "CAUGHT"),
        ("⑨ 两本快照一模一样（好样本）", compare_flat, lambda: (good, good), False, "CLEAN"),
        ("⑩ 只有我们的键出现＋名单里多我们那一条（导入的正常样子）", compare_flat,
         lambda: (base, good), False, "CLEAN"),
        # 三条给"None → '' 算允许"这条豁免配的反向哨兵：一条必须绿、两条必须红。
        # 没有这三条，豁免就是句空话——它到底放过了什么、有没有顺手把真改动也放过，看不出来。
        ("⑪ 新建扩展名键但默认值没写（None→''，导入必然产生）＝允许", compare_flat,
         lambda: (good, mutate(mutate(good, ("extensions", ".ape", "default"), "set", ""),
                               ("class_defaults", ".ape"), "set", "")), False, "CLEAN"),
        ("⑫ 把用户原有的默认值清空了（'mpegvideo'→''）＝真失败", compare_flat,
         lambda: (good, mutate(mutate(good, ("extensions", ".mkv", "default"), "set", ""),
                               ("class_defaults", ".mkv"), "set", "")), True, "CAUGHT"),
        ("⑬ 原本没这根键、现在写成别人的 ProgID（None→'Other.Player'）＝真失败", compare_flat,
         lambda: (good, mutate(mutate(good, ("extensions", ".ape", "default"), "set",
                                       "Other.Player"),
                               ("class_defaults", ".ape"), "set", "Other.Player")),
         True, "CAUGHT"),
        ("对照：故意做的恒真判据（瞎仪器必须被这本账记成 MISSED）", blind,
         lambda: (good, mutate(good, ("extensions", ".mkv", "userchoice"),
                               "set", "PotPlayerMKV")), True, "MISSED"),
        ("对照：注入落空必须记 NOP，不许冒充抓到或干净", compare_flat,
         lambda: (good, mutate(good, ("extensions", ".zzz", "userchoice"),
                               "set", "x")), True, "NOP"),
    ]
    tally: dict[str, int] = {}
    wrong: list[str] = []
    for name, fn, make, expect_red, want in cases:
        tag, detail = verdict(fn, make, expect_red)
        tally[tag] = tally.get(tag, 0) + 1
        if tag != want:
            wrong.append(f"{name}：期望 {want}，实测 {tag}")
        say(tag, name, detail)
    order = ("CAUGHT", "CLEAN", "MISSED", "NOP", "JUNK")
    parts = [f"{t} {tally.get(t, 0)}" for t in order]
    for t in sorted(set(tally) - set(order)):
        parts.append(f"未知标签 {t} {tally[t]}")
    inv = []
    if sum(tally.values()) != len(cases):
        inv.append(f"分桶之和 {sum(tally.values())} != 条数 {len(cases)}")
    if set(tally) - set(order):
        inv.append("出现了未知标签：" + "、".join(sorted(set(tally) - set(order))))
    if tally.get("MISSED", 0) < 1:
        inv.append("那条恒真对照没被抓出来 ⇒ 这本账自己就是瞎的")
    if tally.get("NOP", 0) < 1:
        inv.append("没有一条注入落空被记成 NOP ⇒ NOP 这一家没被检验过")
    for w in wrong:
        say("FAIL", "自测结论与声明不符", w)
    try:
        os.remove(os.path.join(tmp, "坏.json"))
        os.remove(os.path.join(tmp, "缺字段.json"))
        os.rmdir(tmp)
    except OSError as exc:
        say("NOTE", "临时目录没清干净（只影响 temp，不影响结论）", str(exc)[:80])
    print(f"\n---- 已知答案自打：共 {len(cases)} 条 —— " + "／".join(parts)
          + f"；与声明不符 {len(wrong)} 条；账目异常 {len(inv)} 条"
          + (f"：{'；'.join(inv)}" if inv else "") + " ----")
    return 1 if (wrong or inv) else 0


# ---------------------------------------------------------------- 入口
def usage() -> int:
    print(__doc__)
    return 2


def main(argv: list[str]) -> int:
    if ml is None:
        say("ENV", "生成器加载不了（所有判据都从它取）", f"{GEN} —— {GEN_ERR}")
        return 2
    pos = [a for a in argv if not a.startswith("--")]
    if "--selftest" in argv:
        return selftest()
    if "--snapshot" in argv:
        return write_snapshot(pos[0] if pos else DEFAULT_SNAPSHOT)
    if "--diff" in argv:
        if len(pos) < 2:
            say("ENV", "--diff 要两本快照", f"实测参数：{argv}")
            return 2
        return diff_files(pos[0], pos[1])
    if "--check" in argv or not argv:
        return check()
    say("ENV", "认不出的参数", " ".join(argv))
    return usage()


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
