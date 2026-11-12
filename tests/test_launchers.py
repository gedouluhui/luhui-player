"""启动脚本、启动器 exe 与注册表模板的验收（一项都不看像素）+ 证伪。

覆盖的是 packaging/make_launchers.py 那**七份文本产物加 luhui-player.exe**，而不是它的 lint：
lint 的已知答案对照在 `python packaging/make_launchers.py --selftest` 里，条数由它自己打印
（本文件只要求"没抓到 0 条"，**不抄它的条数**——抄了就会在下一轮加案例时变成滞后数字），
而本文件第一件事就是把它当子进程跑一遍——判据没校准过，后面所有的绿都不算数。

跑法：
    python tests/test_launchers.py              # 验收：全过 rc=0，缺产物/判据翻红 rc=1
    python tests/test_launchers.py --falsify    # 盘上逐份改坏 → 判据必须点名 → 还原后 md5 对账

注册表**现状**由 tests/check_openwith.py 核对（它才读 HKCU）；本文件只验字节，绝不导入。

六条为什么这样判：
1. **.bat 必须真让 cmd 读一遍**。上一版产物是 LF 行尾，lint 全绿，而 cmd 把带中文的行
   从中间劈开当命令执行，`python -m app.doctor` 那一行根本没跑到（现场只剩两行
   "不是内部或外部命令"＋"系统找不到指定的路径"，退出码还是 0）。所以这里既跑 lint、
   也真的 `cmd /c` 跑一次：判据来自执行，不来自文本匹配。
2. **.reg 只验字节，绝不导入**。导入是不可逆的用户决定；这里额外只读看一眼注册表当前
   状态，作为观察项（2026-10-16 起「打开方式」那套**经用户明确授权**由助手代导入过 HKCU，
   所以"键不存在"不再是结论——它归 tests/check_openwith.py 按注册表现数判）。
3. **open_file.py 在陌生 cwd + 干净解释器里验**：注册表那条命令没有"工作目录"，所以
   "换个目录还能 import 到 app.main"才是它真正要证明的事。用 runpy 以非 __main__ 的名字
   跑模块体，拿到 ROOT 与 main 就叫停，不建窗口；不在本进程里跑——本文件自己就往
   sys.path 塞过根目录，那样这条判据恒真。
4. **证伪跑两道闸**：改坏一份产物后，既要求生成器的 `--check` 退回非 0，也要求**本文件
   自己的**逐份判据里至少有一条翻红。只验前一道，等于把"我这双眼睛"排除在对照之外。
5. **启动器的 FileDescription 要两份独立实现读**：「打开方式」列表里那行名字就是它，
   而生成器自带的 ctypes 读法**本机实测会把 7 个字读成 4 个**（VS_VERSIONINFO 文本节点的
   wValueLength 数的是宽字符个数，VerQueryValue 原样回给你，按字节读就咬掉半截）。
   修好之后再拿 PowerShell 的 VersionInfo（.NET 那套自己解析资源）当对照组，两份实现
   逐字相同才算这条判据立住——单靠一份"看着对"的读数，2026-10-16 那次差点把量具的
   半截串写成"资源被截"。
6. **打开方式那两份 .reg 的判据不许只靠 lint**：本文件自己按**键名与值名**数一遍
   四个位置（ProgID / Applications / App Paths / 每个扩展名的 OpenWithProgids）和扩展名
   全集，并单独查两件事——`UserChoice` 一个字都不许出现、扩展名键的**默认值**没被写。
   这两处是"改默认程序"的门，越界就等于替用户做了他没点头的决定。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GEN = os.path.join(ROOT, "packaging", "make_launchers.py")

# 按**文件路径**加载生成器：`packaging` 这个名字在本机 site-packages 里是个真包
# （anaconda 装了 packaging 库），`from packaging import …` 会绑到那边去。
_spec = importlib.util.spec_from_file_location("gelu_make_launchers", GEN)
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)

BAT_RUN, BAT_DOC, OPEN_PY = ml.BAT_RUN, ml.BAT_DOC, ml.OPEN_PY
REG_ADD, REG_DEL = ml.REG_ADD, ml.REG_DEL
REG_OW, REG_OW_DEL = ml.REG_OW, ml.REG_OW_DEL
LAUNCH_EXE, LAUNCH_CS = ml.LAUNCH_EXE, ml.LAUNCH_CS
ALL5 = [BAT_RUN, BAT_DOC, OPEN_PY, REG_ADD, REG_DEL]
# 文本产物全集（证伪的对照组与 md5 台账都数这份；启动器 exe 单独一条，它是二进制）
ALLTEXT = ALL5 + [REG_OW, REG_OW_DEL]

results: list[tuple[str, bool, str]] = []


def say(tag: str, name: str, detail: str = "") -> None:
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))


def emit(items: list[tuple[str, bool, str]]) -> None:
    for name, ok, detail in items:
        results.append((name, ok, detail))
        say("PASS" if ok else "FAIL", name, detail)


def note(text: str) -> None:
    say("NOTE", text)


# ---------------------------------------------------------------- 盘上真字节
def read_gbk(path: str) -> tuple[str, str]:
    """.bat 按 GBK + 原始换行读：通用换行会把 CRLF 读成 LF，行尾判据就自缚了。
    读不出来自觉降级成"这项 FAIL"，而不是让 traceback 冒充结论。"""
    try:
        with open(path, encoding="gbk", errors="strict", newline="") as fh:
            return fh.read(), ""
    except (UnicodeDecodeError, OSError) as exc:
        return "", f"{type(exc).__name__}: {exc}"


def read_utf8(path: str) -> tuple[str, str]:
    try:
        with open(path, encoding="utf-8", errors="strict", newline="") as fh:
            return fh.read(), ""
    except (UnicodeDecodeError, OSError) as exc:
        return "", f"{type(exc).__name__}: {exc}"


def reg_text(path: str) -> tuple[str, bytes, str]:
    try:
        raw = open(path, "rb").read()
    except OSError as exc:
        return "", b"", f"读不了：{exc}"
    if raw[:2] != b"\xff\xfe":
        return "", raw, "没有 UTF-16LE 的 BOM（前两字节不是 FF FE）"
    try:
        return raw[2:].decode("utf-16-le"), raw, ""
    except UnicodeDecodeError as exc:
        return "", raw, f"UTF-16LE 解不开：{exc}"


def md5(path: str) -> str:
    return hashlib.md5(open(path, "rb").read()).hexdigest() if os.path.exists(path) else "-"


def gen(mode: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, GEN, mode], capture_output=True, timeout=180)


# ---------------------------------------------------------------- 逐份判据（纯列表，可复算）
def items_bat(path: str, need: str) -> list[tuple[str, bool, str]]:
    nm = os.path.basename(path)
    if not os.path.exists(path):
        return [(f"{nm} 在盘上", False, path)]
    txt, err = read_gbk(path)
    if not txt:
        return [(f"{nm} 能用 GBK 解码（不是 UTF-8 存的）", False, err)]
    problems = ml.lint_bat(txt)
    seg = [s for s in txt.split('"') if s.lower().endswith(".exe")]
    return [
        (f"{nm} 能用 GBK 解码（不是 UTF-8 存的）", True, f"{len(txt)} 字符"),
        # 这条也**不走 lint**：字节层面自己数一遍裸 LF，lint 恒真时这道闸还得会翻红。
        (f"{nm} 一个裸 LF 都没有（cmd 劈行的源头）",
         "\n" not in txt.replace("\r\n", "") and txt.endswith("\r\n"),
         f"{txt.count(chr(13))} 个 CR / {txt.count(chr(10))} 个 LF"),
        (f"{nm} 整份 CRLF 且过 lint（LF-only 会被 cmd 劈行）", not problems,
         "；".join(problems)[:130]),
        (f"{nm} 先 cd 到脚本所在目录，且命令行里有「{need}」",
         'cd /d "%~dp0"' in txt and need in txt, need),
        (f"{nm} 里写死的解释器真存在", bool(seg) and all(os.path.exists(s) for s in seg),
         "、".join(seg)[:90]),
    ]


def items_reg(path: str, tag: str, lints: list) -> list[tuple[str, bool, str]]:
    nm = os.path.basename(path)
    if not os.path.exists(path):
        return [(f"{tag}模板在盘上（{nm}）", False, path)]
    txt, raw, err = reg_text(path)
    if not txt:
        return [(f"{tag}模板在盘上（{nm}）", True, path),
                (f"{tag}模板是 UTF-16LE 带 BOM 且解得开", False, err)]
    problems = [p for fn in lints for p in fn(txt)]
    keys = [l for l in txt.split("\r\n") if l.startswith("[")]
    want = ml.HKCU + "\\" + ml.SHELL_KEY
    out = [
        (f"{tag}模板在盘上（{nm}）", True, path),
        (f"{tag}模板是 UTF-16LE 带 BOM 且解得开", True, f"{len(raw)} 字节"),
        (f"{tag}模板整份 CRLF（自己数裸 LF，不走 lint）",
         "\n" not in txt.replace("\r\n", ""),
         f"CRLF {txt.count(chr(13))} 对 / 裸 LF {txt.replace(chr(13)+chr(10), '').count(chr(10))} 个"),
        (f"{tag}模板整份 CRLF 且过 lint", not problems, "；".join(problems)[:130]),
        (f"{tag}模板只落在 HKCU 的 SystemFileAssociations\\video 这一根键下",
         bool(keys) and all(k[1:].lstrip("-").startswith(want) for k in keys),
         f"{len(keys)} 根键"),
        (f"{tag}模板没碰默认打开方式（不写 UserChoice / Applications）",
         "UserChoice" not in txt and "\\Applications\\" not in txt, ""),
    ]
    if tag == "新增":
        cmd = ml.command_value(txt)
        segs = [s for s in cmd.split('"') if s.lower().endswith((".exe", ".py"))]
        ic = [l for l in txt.split("\r\n") if l.strip().startswith('"Icon"')]
        out += [
            ("command 取参是 %1、不是 %~1", "%1" in cmd and "%~1" not in cmd, cmd[:110]),
            ("command 指向的解释器与入口脚本真存在",
             bool(segs) and all(os.path.exists(s) for s in segs), "、".join(segs)[:110]),
            ("command 用 pythonw（右键播放不该弹黑窗）", "pythonw" in cmd, cmd[:70]),
            ("新增模板带了图标行（app.ico 在就该有）", bool(ic), ic[0][:70] if ic else "没写 Icon 行"),
        ]
        if ic:
            p = ml._reg_unescape(ic[0].split('="', 1)[1].rstrip('"'))
            out.append(("图标行指向的 .ico 真存在（不存在就该整行不写）",
                        os.path.exists(p), p))
    else:
        # 这一条**不走 lint**：证伪时要能证明"我这双眼睛"独立于生成器的判据，
        # 否则 lint_del 一旦恒真，两道闸会一起放行。
        out.append(("撤销模板那根键名以 [- 开头（regedit 认这个才是删除）",
                    bool(keys) and keys[0].startswith("[-" + want) and len(keys) == 1,
                    keys[0][:80] if keys else "一根键都没有"))
    return out


def powershell_description(path: str) -> tuple[str | None, str]:
    """第二份**独立实现**读 FileDescription：.NET 的 VersionInfo 自己解析版本资源，
    不走 version.dll。它和生成器里那把 ctypes 尺逐字相同才算这条判据立住
    （单靠一份读数，本机那次"7 个字读成 4 个"就会被写成"资源被截"）。
    读不出来回 (None, 原因)——调用方必须把它记成 NOTE，不许当 PASS。
    """
    tmp = os.path.join(tempfile.mkdtemp(prefix="gelu-fd-"), "fd.txt")
    cmd = ("(Get-Item -LiteralPath '" + path + "').VersionInfo.FileDescription"
           " | Out-File -Encoding utf8 '" + tmp + "'")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
        return None, f"powershell 跑不起来（这条对照本轮没验）：{type(exc).__name__}"
    try:
        if not os.path.exists(tmp):
            return None, f"powershell 没产出（rc={r.returncode}）：" + \
                r.stderr.decode("gbk", "replace").strip()[:80]
        got = open(tmp, encoding="utf-8-sig", errors="replace").read().strip()
    except OSError as exc:
        return None, f"读不了对照输出：{exc}"
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    return got or None, f"powershell 读到 {len(got)} 字"


def items_launcher() -> list[tuple[str, bool, str]]:
    """启动器 exe 这一份是**二进制**：文本 lint 管不到它，所以三条判据都跑在执行上。"""
    out: list[tuple[str, bool, str]] = []
    if not os.path.exists(LAUNCH_EXE):
        return [(f"{ml.LAUNCH_NAME} 在盘上", False, LAUNCH_EXE)]
    out.append((f"{ml.LAUNCH_NAME} 在盘上", True, f"{os.path.getsize(LAUNCH_EXE)} 字节"))
    cs, err = "", ""
    if os.path.exists(LAUNCH_CS):
        try:
            cs = open(LAUNCH_CS, encoding="ascii", errors="strict", newline="").read()
        except (UnicodeDecodeError, OSError) as exc:
            err = f"{type(exc).__name__}: {exc}"
    out.append(("launcher.cs 在盘上且整份 ASCII（中文全走 \\uXXXX）", bool(cs),
                err or "读不出来的 .cs 就不是 csc 读进去的那份"))
    if cs:
        problems = ml.lint_cs(cs)
        out.append(("launcher.cs 过 lint_cs", not problems, "；".join(problems)[:120]))
        out.append(("launcher.cs 与本次要生成的那份逐字相同（exe 的源就是它）",
                    cs == ml.launcher_cs(), "不同就重跑 python packaging" + chr(92) + "make_launchers.py"))
    mine = ml.file_description(LAUNCH_EXE)
    out.append(("启动器的 FileDescription 等于播放器名（列表里那行就是它）",
                mine == ml.APP_TITLE, f"ctypes 读到 {mine!r}，应为 {ml.APP_TITLE!r}"))
    theirs, why = powershell_description(LAUNCH_EXE)
    if theirs is None:
        note("FileDescription 的第二份实现不可用，这条本轮没验：" + why)
    else:
        out.append(("两份独立实现读出的 FileDescription 逐字相同",
                    theirs == mine, f"ctypes={mine!r} / dotnet={theirs!r}"))
    # 探针档：不起 GUI、不开文件，只把它**打算**执行的命令行写出来。带空格＋中文的文件名
    # 是故意的：没加引号就会被拆成两个参数，而这一条只能靠执行看到，文本匹配看不出来。
    tmp = os.path.join(tempfile.mkdtemp(prefix="gelu-probe-"), "cmdline.txt")
    arg = "D:" + chr(92) + "某 一片.mkv"
    try:
        r = subprocess.run([LAUNCH_EXE, arg], cwd=ROOT,
                           env=dict(os.environ, GELU_LAUNCHER_PROBE=tmp),
                           capture_output=True, timeout=60)
        got = open(tmp, encoding="utf-8", errors="replace").read().strip() \
            if os.path.exists(tmp) else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        r, got = None, ""
        note(f"探针档跑不起来（这条没验）：{type(exc).__name__}")
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    out.append(("探针档退出码 0 且写出了它打算执行的命令行",
                bool(r) and r.returncode == 0 and bool(got), (got or "空")[:120]))
    if got:
        out.append(("探针命令行里解释器与入口脚本都是当前这台机器上的真路径",
                    ml.interpreter() in got and ml.OPEN_PY in got, got[:120]))
        out.append(("文件参数被引号包住且原样带在最后（带空格的路径不该被拆成两个）",
                    '"' + arg + '"' in got and got.rstrip().endswith('"' + arg + '"'), got[-80:]))
    return out


def items_reg_openwith(path: str, tag: str) -> list[tuple[str, bool, str]]:
    """「打开方式」那两份的**独立**判据：不借 lint_openwith，自己按键名与值名数一遍。
    证伪那一趟要求两道闸都翻红，如果两边读的是同一份代码，那道闸等于只有一道。
    """
    nm = os.path.basename(path)
    if not os.path.exists(path):
        return [(f"{tag}件在盘上（{nm}）", False, path)]
    txt, raw, err = reg_text(path)
    if not txt:
        return [(f"{tag}件在盘上（{nm}）", True, path),
                (f"{tag}件是 UTF-16LE 带 BOM 且解得开", False, err)]
    secs = ml._reg_sections(txt)
    exts = set(ml.media_exts())
    ow: dict[str, list[str]] = {}
    cmds: dict[str, str] = {}
    apppaths_default = friendly = ""
    for key, dele, vals in secs:
        parts = key.split(chr(92))
        if parts[-1].lower() == "openwithprogids" and len(parts) >= 5:
            ow.setdefault(parts[-2].lower(), []).extend(vals)
        if parts[-1].lower() == "command" and not dele:
            for v in vals:
                head, sep, rest = v.partition("=")
                if sep and head.strip() == "@":
                    cmds[key] = ml._reg_unescape(rest.strip()[1:-1])
        if key.lower() == (ml.HKCU + chr(92) + ml.KEY_APPPATHS).lower():
            for v in vals:
                if v.startswith('@="'):
                    apppaths_default = ml._reg_unescape(v[3:-1])
        if key.lower() == (ml.HKCU + chr(92) + ml.KEY_APP).lower():
            for v in vals:
                # 比较的两边都得是 lowercase：上一版这里写的是 `"friendlyappName"`（N 大写），
                # 值先 lower 了、针却留着大写 ⇒ 这条**恒红**，把好产物判成坏。
                if v.lower().startswith('"friendlyappname"'):
                    friendly = ml._reg_unescape(v.split("=", 1)[1].strip()[1:-1])
    roots = sorted({k.split(chr(92))[0] for k, _d, _v in secs})
    bare = [k for k, _d, _v in secs
            if len(k.split(chr(92))) == 4 and k.split(chr(92))[-1].startswith(".")]
    out = [
        (f"{tag}件在盘上（{nm}）", True, path),
        (f"{tag}件是 UTF-16LE 带 BOM 且解得开", True, f"{len(raw)} 字节"),
        (f"{tag}件整份 CRLF（自己数裸 LF，不走 lint）",
         "\n" not in txt.replace("\r\n", ""),
         f"CRLF {txt.count(chr(13))} 对 / 裸 LF {txt.replace(chr(13) + chr(10), '').count(chr(10))} 个"),
        (f"{tag}件只落在 HKCU（根节点：{'、'.join(roots) or '无'}）",
         bool(roots) and roots == [ml.HKCU], f"{len(secs)} 节"),
        (f"{tag}件里 UserChoice 一个字都没有（那是默认程序，碰不得）",
         "userchoice" not in txt.lower(), ""),
    ]
    if tag == "注册":
        want3 = {(ml.HKCU + chr(92) + k).lower() for k in
                 (ml.KEY_PROGID, ml.KEY_APP, ml.KEY_APPPATHS)}
        have = {k.lower() for k, _d, _v in secs}
        out += [
            ("三根自己的键都在（ProgID／Applications／App Paths）",
             want3 <= have, "缺：" + "、".join(sorted(want3 - have)) if want3 - have else "3 根齐"),
            ("覆盖的扩展名集合 == MEDIA_EXTS 现取的那份",
             set(ow) == exts,
             f"模板 {len(ow)} 种 / 播放器 {len(exts)} 种；差集 "
             + str(sorted(set(ow) ^ exts))[:70]),
            ("每个扩展名那条值都叫我们的 ProgID 且是空串",
             bool(ow) and all(v == f'"{ml.PROGID}"=""' for vs in ow.values() for v in vs),
             "、".join(sorted({v for vs in ow.values() for v in vs}))[:80]),
            ("两处 shell\\open\\command 都有且都落到启动器＋%1",
             len(cmds) == 2 and all(c.split('"')[1].lower() == LAUNCH_EXE.lower()
                                    and '"%1"' in c and "%~1" not in c for c in cmds.values()),
             "；".join(sorted(cmds))[:120]),
            ("App Paths 的默认值就是启动器，FriendlyAppName 就是播放器名",
             apppaths_default.lower() == LAUNCH_EXE.lower() and friendly == ml.APP_TITLE,
             f"{os.path.basename(apppaths_default)!r} / {friendly!r}"),
            ("扩展名键自己那节没被写默认值（写了就是改默认打开方式）",
             not bare, "、".join(bare)[:80]),
            ("命令里的启动器 exe 真存在（不在就是注册了个空壳）",
             os.path.exists(LAUNCH_EXE), LAUNCH_EXE),
            ("过生成器的 lint_openwith", not ml.lint_openwith(txt), ""),
        ]
    else:
        dels = {k.lower() for k, d, _v in secs if d}
        out += [
            ("删的正是我们那三根键（一根不多一根不少）",
             dels == {(ml.HKCU + chr(92) + k).lower() for k in
                      (ml.KEY_PROGID, ml.KEY_APP, ml.KEY_APPPATHS)},
             f"{len(dels)} 根：{'、'.join(sorted(os.path.split(d)[-1] for d in dels))[:110]}"),
            ("没有整根删扩展名键或整根删 OpenWithProgids（那会连别人的条目一起没）",
             # 上一版写的是 `len(parts) == 4`——我们自己那根 ProgID 键正好也是四节
             # （HKCU\Software\Classes\LuHuiPlayer.Media），于是这条**恒红**。要加的是
             # "最后一节以点开头"，那才是裸扩展名键。
             not [d for d in dels
                  if d.split(chr(92))[-1].lower() == "openwithprogids"
                  or (d.split(chr(92))[-1].startswith(".") and len(d.split(chr(92))) == 4)],
             "、".join(sorted(d.split(chr(92))[-1] for d in dels))[:80]),
            ("每个扩展名都只按名字摘我们那一条值",
             set(ow) == exts and all(v == f'"{ml.PROGID}"=-' for vs in ow.values() for v in vs),
             f"{len(ow)} 种扩展名；值形如 " + "、".join(sorted({v for vs in ow.values() for v in vs}))[:60]),
            ("过生成器的 lint_openwith_del", not ml.lint_openwith_del(txt), ""),
        ]
    return out


def items_open_py() -> list[tuple[str, bool, str]]:
    txt, err = read_utf8(OPEN_PY)
    if not txt:
        return [("packaging\\open_file.py 在盘上且能按 UTF-8 解码", False, err or OPEN_PY)]
    problems = ml.lint_open_py(txt)
    tmp = tempfile.mkdtemp(prefix="gelu-launcher-cwd-")
    code = ("import runpy,sys;"
            f"g=runpy.run_path(r'{OPEN_PY}',run_name='_gelu_probe');"
            "sys.stdout.buffer.write(('ROOT=' + g['ROOT'] + '\\n'"
            " + 'INPATH=' + str(g['ROOT'] in sys.path) + '\\n'"
            " + 'MAIN=' + str(callable(g['main'])) + '\\n').encode('utf-8'))")
    got, err2, dec_err = "", "", ""
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=tmp,
                           capture_output=True, timeout=180)
        # 子进程是裸 `python -c`，它的 print 跟着本机 ANSI（GBK）走，而 ROOT 里有中文。
        # 上一版按 UTF-8 硬解那份 GBK 输出，把「赛博鸡蛋」解成一堆替换符，于是这条判据在
        # **好产物**上翻红——误报和漏报一样是判据坏了。现在让子进程自己写 UTF-8 字节、
        # 父进程严格解码：解不开就单独点名"解码口径"，绝不再冒充"项目根算错"。
        try:
            got = r.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            dec_err = f"探针子进程的输出不是 UTF-8（编码口径漂了，别拿它的回执当结论）：{exc}"
            got = r.stdout.decode("utf-8", "replace")
        err2 = r.stderr.decode("gbk", "replace").strip()
    except subprocess.TimeoutExpired:
        err2 = "180 s 没返回"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    first = got.strip().splitlines()[0] if got.strip() else ""
    return [
        ("packaging\\open_file.py 在盘上且能按 UTF-8 解码", True, f"{len(txt)} 字符"),
        ("open_file.py 过生成器的 lint（语法/转交/根都在册）", not problems,
         "；".join(problems)[:120]),
        ("探针子进程的输出严格解得开 UTF-8（解不开就不许判产品）", not dec_err,
         dec_err or "UTF-8 无替换符"),
        ("陌生 cwd 下 open_file.py 算出的项目根就是本仓库根", f"ROOT={ROOT}" in got,
         first[:90] or err2[-90:]),
        ("陌生 cwd 下 app.main 真被 import 到了（.reg 那条命令的前提）",
         "MAIN=True" in got, first[:90] or err2[-120:]),
        ("项目根进了 sys.path（换目录也 import 得动 app）", "INPATH=True" in got, ""),
    ]


# ---------------------------------------------------------------- 真跑（判据来自执行）
def doctor_marks() -> tuple[str, ...]:
    """报告行首那三个标记**从 doctor 自己取**，连它登记外的落点一起取，这里不手抄。

    为什么要单列这把尺：本文件原先用 `re.match(r"^\\s*\\[ (OK|WARN|FAIL) \\]", l)` 数行，
    那个形状只长得像 `[ OK ]`——doctor 给提醒/阻断印的是 `[WARN]`/`[FAIL]`（内侧没有空格），
    于是这两档的行对那把尺**完全隐形**。2026-10-17 chain-r17 真踩中的样子：CHECKS 17 项、
    报告实际 17 行、数出来 15，判据把"这台机器上有两条提醒"误读成"报告缺两行"，
    一条假红挂成"启动器验收（正跑）真失败"。反向更坏：真出现 FAIL 行时它同样数不到，
    "报告齐不齐"就悄悄退化成"OK 行够不够"。
    """
    sys.path.insert(0, ROOT)                      # 本文件已在 tests/，但判据要能独立跑
    from app.doctor import LEVEL_MARK, UNKNOWN_MARK
    return tuple(sorted({*LEVEL_MARK.values(), UNKNOWN_MARK}))


def unknown_mark() -> str:
    sys.path.insert(0, ROOT)
    from app.doctor import UNKNOWN_MARK
    return UNKNOWN_MARK


def count_doctor_rows(text: str) -> tuple[int, int]:
    """数 doctor 报告：回 (报告行数, 其中"标记没登记"的行数)。

    只认**行首**是标记的行：doctor 的"下一步："续行是缩进的另一行、结论行也没有方括号标记，
    两条都不许数进来（数进来会把"缺行"洗成"齐"）。
    """
    marks = doctor_marks()
    unk = unknown_mark()
    n = u = 0
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith(marks):
            n += 1
            if s.startswith(unk):
                u += 1
    return n, u


def run_bat_through_cmd() -> None:
    """真跑：故障自检.bat 必须把**每一项**报告打到 stdout，且 stderr 一行脏字都没有。

    项数从 `len(app.doctor.CHECKS)` 取，不写死数字：写死的那版每加一项就得记着回来改这里，
    而"忘了改"不会让门禁翻红、只会让它悄悄变松。行数按 `count_doctor_rows` 数——它数的是
    **三档全部**（外加"标记没登记"那一档），不是只数 OK 行：把"报告齐不齐"和"这台机器绿不绿"
    分成两格，绿不绿由后面两条专门判。这条尺自己有没有牙，由 `--falsify` 里的
    `count_rows_selftest()` 用七种已知答案的样本验（其中一条就是本轮踩到的坏法：
    把一行 `[ OK ]` 改成 `[WARN]`，行数**不许**变——旧尺正是在这里少数 1 行造出假红）。
    """
    if not os.path.exists(BAT_DOC):
        emit([("故障自检.bat 真跑（cmd /c）", False, "盘上没有这份")])
        return
    try:
        r = subprocess.run(["cmd", "/c", BAT_DOC], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        emit([("故障自检.bat 真跑（cmd /c）", False, "120 s 没跑完：报告卡住了")])
        return
    out = r.stdout.decode("utf-8", "replace")     # doctor 自己把输出切成 UTF-8
    err = r.stderr.decode("gbk", "replace")
    sys.path.insert(0, ROOT)                      # 本文件已在 tests/，但判据要能独立跑
    from app.doctor import CHECKS as _CHECKS      # 项数的真源；只 import，不采集
    want_n = len(_CHECKS)
    row_n, unknown_n = count_doctor_rows(out)
    bad_line = any(k in err for k in ("不是内部或外部命令", "系统找不到", "命令行太长"))
    emit([
        (f"故障自检.bat 真跑完且 {want_n} 项报告都在（项数取 doctor.CHECKS，不写死）",
         r.returncode == 0 and row_n >= want_n,
         f"rc={r.returncode}，报告行数={row_n}，CHECKS={want_n}，stderr={len(r.stderr)} 字节；"
         f"数行用的标记={[m for m in doctor_marks()]}（取自 doctor.LEVEL_MARK）"),
        ("故障自检.bat 没把中文行劈成命令（stderr 干净）", not bad_line,
         err.strip()[:120] or "stderr 无「不是内部或外部命令」类报错"),
        ("故障自检.bat 报告里没有 FAIL", "[FAIL]" not in out,
         "；".join(l for l in out.splitlines() if "[FAIL]" in l)[:120]),
        # 单独一条管"标记没登记"：将来 doctor 加第四档又忘了配 LEVEL_MARK，那行会印成
        # [????]——它既是一行（要数进来），又是一个必须当场点名的缺陷（不许靠"行数对了"蒙过去）。
        ("故障自检.bat 报告里没有 [????]（doctor 加档忘了配标记就会落到这）", unknown_n == 0,
         f"{unknown_n} 行" if unknown_n else "0 行：三档标记都登记在册"),
    ])


def run_launcher_bat_probe() -> None:
    """启动那条不能真开窗口（屏幕还没给像素），但可以证明 cmd 能把这份文件**读到底**：
    把盘上那份原样拷到根目录，只把 `start …` 那一行换成 echo，其余字节不动。"""
    txt, _ = read_gbk(BAT_RUN)
    lines = txt.split("\r\n")
    hit = [i for i, l in enumerate(lines) if l.startswith("start ")]
    if not txt or not hit:
        emit([("启动播放器.bat 被 cmd 读到底（探针副本）", False, "读不出正文或没有 start 行")])
        return
    probe = lines[:]
    probe[hit[0]] = "echo PROBE_LAUNCH_LINE_OK"
    p = os.path.join(ROOT, "_probe_启动播放器.bat")
    with open(p, "w", encoding="gbk", newline="") as fh:
        fh.write("\r\n".join(probe))
    try:
        r = subprocess.run(["cmd", "/c", p], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60, cwd=ROOT)
        out = r.stdout.decode("gbk", "replace")
        err = r.stderr.decode("gbk", "replace")
        emit([("启动播放器.bat 被 cmd 读到底（中文 rem 行没被劈开，%~dp0 也解析对了）",
               "PROBE_LAUNCH_LINE_OK" in out and "不是内部或外部命令" not in err,
               f"rc={r.returncode}，stderr={err.strip()[:80] or '干净'}")])
    finally:
        if os.path.exists(p):
            os.remove(p)


def observe_registry_untouched() -> None:
    """只读观察项，**不判红**：本文件验字节，注册表现数归 tests/check_openwith.py。
    这里只看一眼"产物对应的键在不在"，好让读日志的人知道该跑哪一个下一步。
    """
    try:
        import winreg
    except ImportError:
        note("这个平台没有 winreg，注册表现数这条观察本轮没看")
        return
    for label, key in (("右键菜单", ml.SHELL_KEY),
                       ("打开方式（ProgID）", ml.KEY_PROGID),
                       ("打开方式（App Paths）", ml.KEY_APPPATHS)):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, key))
            note(f"{label}已落在 HKCU：{key}")
        except OSError:
            note(f"{label}在 HKCU 查不到：双击 packaging\\reg 里对应那份 .reg 导入"
                 f"（生成器本身不代劳）— {key}")


def check_all() -> None:
    r = gen("--selftest")
    out = r.stdout.decode("utf-8", "replace")
    tail = [l for l in out.splitlines() if "已知答案自打" in l]
    line = tail[-1] if tail else "(没打印自打结论)"
    # 条数**只从它自己打印的那行里取**：抄进本文件就成了滞后数字（加案例时不会提醒我回来改）。
    # 这里不核"是不是 68 条"，核的是那行自己报的账对不对——分桶相加必须等于总数，
    # 坏法数必须等于 CAUGHT＋MISSED＋NOP＋JUNK（CLEAN 是好样本，不算坏法）。
    m = re.search(r"共 (\d+) 条 —— CAUGHT (\d+)／CLEAN (\d+)／MISSED (\d+)／NOP (\d+)／JUNK (\d+)；"
                  r"坏法合计 (\d+) 条，其中没抓到 (\d+) 条", line)
    v = [int(g) for g in m.groups()] if m else []
    emit([
        ("生成器自打跑完（rc=0）", r.returncode == 0, f"rc={r.returncode}"),
        ("生成器自打说了「没抓到 0 条」", bool(m) and v[7] == 0,
         line[-58:] if m else line[:58]),
        ("生成器自打的账自洽（分桶相加==总数，坏法数也对得上）",
         bool(m) and sum(v[1:6]) == v[0] and v[6] == v[1] + v[3] + v[4] + v[5],
         f"共 {v[0]}＝C{v[1]}＋K{v[2]}＋M{v[3]}＋N{v[4]}＋J{v[5]}，坏法 {v[6]}"
         if m else "末行解析不出计数（它改格式了，这条尺得跟着改）"),
        # 下限不是"条数"，是"别从几十条塌成两三条"：真数由上一格里那条自洽判据管。
        ("生成器自打的案例数没塌方（>= 30 条在册）", bool(m) and v[0] >= 30,
         f"{v[0] if m else 0} 条"),
    ])
    r2 = gen("--check")
    emit([(f"--check 说盘上 {len(ALLTEXT)} 份文本产物加启动器都与当前解释器/路径一致",
           r2.returncode == 0,
           "；".join(l for l in r2.stdout.decode("utf-8", "replace").splitlines()
                     if l.startswith("[FAIL]"))[:150]
           or f"{len(ALLTEXT)} 份加启动器都在且一致")])
    emit(items_bat(BAT_RUN, "%*"))
    emit(items_bat(BAT_DOC, "-m app.doctor"))
    a, _ = read_gbk(BAT_RUN)
    b, _ = read_gbk(BAT_DOC)
    emit([("启动那条用 pythonw（双击不带黑窗），自检那条用带控制台的 python",
           "pythonw.exe" in a and "pythonw" not in b, "见两份 .bat 的最后一条命令行")])
    run_bat_through_cmd()
    run_launcher_bat_probe()
    emit(items_open_py())
    emit(items_reg(REG_ADD, "新增", [ml.lint_reg, ml.lint_command_line]))
    emit(items_reg(REG_DEL, "撤销", [ml.lint_reg, ml.lint_del]))
    emit(items_reg_openwith(REG_OW, "注册"))
    emit(items_reg_openwith(REG_OW_DEL, "撤销"))
    emit(items_launcher())
    add, _, _ = reg_text(REG_ADD)
    note("command 展开后长这样：%s" % ml.command_value(add)[:150])
    observe_registry_untouched()


# ---------------------------------------------------------------- 证伪
def _disk_text(path: str, enc: str) -> str:
    """按证伪那几种编码读盘上正文（读不出来回空串，交给调用方判 NOP）。"""
    try:
        if enc == "reg":
            return reg_text(path)[0]
        return open(path, encoding=enc, errors="strict", newline="").read()
    except (UnicodeDecodeError, OSError):
        return ""


def mutate_and_check(path: str, bad: str | None, enc: str, want: tuple[str, ...],
                     mine_fn) -> tuple[str, str]:
    """把盘上那份换成坏样本 → 两道闸都要翻红 → 立刻还原并核对 md5。

    回的是**标签**不是布尔：CAUGHT／MISSED／NOP 三家。注入本身没改到任何字节时必须记
    NOP 而不是漏网——"坏样本其实和好消息本一模一样"是量具的坑，不是产品的坑，
    把它记成 MISSED 会诱导我去松判据（这正是本文件自己的恒真防线）。

    `mine_fn` 必须是**函数**而不是算好的列表：判据要在坏字节躺在盘上之后再跑一遍，
    拿改动前的结果当对照等于什么都没验。

    备份用**字节**（.reg 是 UTF-16LE、.bat 是 GBK、.cs 是 ASCII）：文本模式读写会换算
    CRLF，那种"还原"等于悄悄改了源文件。
    """
    if not os.path.exists(path):
        return "MISSED", "盘上没有这份，没法证伪"
    keep = open(path, "rb").read()
    before = md5(path)
    if enc != "nobom":
        if _disk_text(path, enc) == (bad or ""):
            return "NOP", "注入后的字节与盘上那份逐字相同——这条坏法根本没生效"
    try:
        if enc == "nobom":
            with open(path, "wb") as fh:          # 只砍前两字节的 BOM，正文一个字节不动
                fh.write(keep[2:])
        elif enc == "reg":
            ml.write_reg(path, bad or "")
        else:
            with open(path, "w", encoding=enc, newline="") as fh:
                fh.write(bad or "")
        r = subprocess.run([sys.executable, GEN, "--check"], capture_output=True, timeout=180)
        out = r.stdout.decode("utf-8", "replace")
        gen_caught = r.returncode != 0 and any(k in out for k in want)
        items = mine_fn()
        mine_caught = any(not ok for _, ok, _ in items)
        if gen_caught and mine_caught:
            return "CAUGHT", "生成器与本机判据都点名"
        return "MISSED", (f"生成器{'抓到' if gen_caught else '没抓到'}、"
                          f"本文件判据{'翻红' if mine_caught else '仍然放行'}："
                          + "；".join(l for l in out.splitlines() if l.startswith("[FAIL]"))[:130])
    finally:
        with open(path, "wb") as fh:
            fh.write(keep)
        if md5(path) != before:
            print("       [!] 还原后 md5 变了——本轮不作数")


def first_hit(txt: str, needle: str, repl) -> str:
    """把**第一条**含 needle 的行按 repl 改坏；找不到就回原文（调用方靠 NOP 闸门点名）。
    按行找而不是整串 replace：一处改坏才是"注册表里真写错一行"的那种坏法。
    """
    lines = txt.split("\r\n")
    for i, l in enumerate(lines):
        if needle in l:
            lines[i] = repl(l)
            return "\r\n".join(lines)
    return txt


# ---------------------------------------------------------------- 数行那把尺的自检
def count_rows_selftest() -> list[tuple[str, bool, str]]:
    """给 `count_doctor_rows` 喂七种**已知答案**的报告文本，答案对不上就是这把尺瞎。

    为什么要有这一段：`run_bat_through_cmd` 那条判据只在这台机器"恰好有几条提醒"时才会暴露
    数法错误——一台全绿的机器上，旧尺（只数 `[ OK ]`）和新尺读数相同，跑一百遍都不吭声。
    所以这条尺的牙不能等真环境给它出题，得自己出题自己答：其中"改一行成 [WARN] 之后行数
    不许变"就是 2026-10-17 那次假红的形状，旧尺在这一格必然答错。
    """
    sys.path.insert(0, ROOT)
    from app.doctor import CHECKS, LEVEL_MARK, UNKNOWN_MARK
    want = len(CHECKS)
    ok_m, warn_m, fail_m = LEVEL_MARK["OK"], LEVEL_MARK["WARN"], LEVEL_MARK["FAIL"]

    def report(rows: list[str]) -> str:
        return "\n".join([f"LuHui播放器 · 故障自检   2026-10-17 02:00:00", "", *rows,
                          f"结论：{len(rows)} 项正常。"])

    base_rows = [f"{ok_m} 第{i}项        一切正常" for i in range(want)]
    base = report(base_rows)
    n0, u0 = count_doctor_rows(base)
    cases = [
        # (说明, 文本, 期望行数, 期望 [????] 行数, 是否必须与基线不同)
        ("基线：CHECKS 项全 [ OK ]", base, want, 0, False),
        ("少印一行（真缺行的形状）", report(base_rows[:-1]), want - 1, 0, True),
        ("整份报告没跑出来（空输出）", "", 0, 0, True),
        # ↑ 本轮踩到的坏法：旧尺把 [WARN]/[FAIL] 行当隐形，所以这两条行数**不许**跟着变
        ("一行从 [ OK ] 改成 [WARN]（本机真实形状：两条提醒）",
         report(base_rows[:-1] + [warn_m + " 末项        有提醒"]), want, 0, True),
        ("一行从 [ OK ] 改成 [FAIL]（旧尺在这儿同样数不到）",
         report(base_rows[:-1] + [fail_m + " 末项        阻断了"]), want, 0, True),
        ("每条后面都挂一行缩进的『下一步：』续行（不许过数）",
         report([x for r in base_rows for x in (r, "        下一步：贴出来就能定位")]),
         want, 0, True),
        ("末行标记写成未登记那一档（要数进来、同时要能被点名）",
         report(base_rows[:-1] + [UNKNOWN_MARK + " 末项        新加的档"]), want, 1, True),
    ]
    items: list[tuple[str, bool, str]] = []
    for name, txt, want_n, want_u, must_differ in cases:
        got_n, got_u = count_doctor_rows(txt)
        if must_differ and txt == base:
            items.append((f"数行尺·{name}", False, "NOP：注入是空操作（文本一个字没变），"
                                                 "这一格不作数，得先修注入"))
            continue
        ok = (got_n, got_u) == (want_n, want_u)
        items.append((f"数行尺·{name}", ok,
                      f"期望 行={want_n} 未登记={want_u}／实得 行={got_n} 未登记={got_u}"
                      + ("" if ok else " ⇒ 这把尺不许上闸门")
                      + f"（基线自证：行={n0} 未登记={u0}，CHECKS={want}）"))
    return items


def falsify() -> int:
    print("\n== 证伪：盘上产物逐条改坏，生成器 --check 与本文件判据**都**要翻红 ==")
    add_txt, _, _ = reg_text(REG_ADD)
    del_txt, _, _ = reg_text(REG_DEL)
    ow_txt, _, _ = reg_text(REG_OW)
    owd_txt, _, _ = reg_text(REG_OW_DEL)
    run_txt, _ = read_gbk(BAT_RUN)
    doc_txt, _ = read_gbk(BAT_DOC)
    op_txt, _ = read_utf8(OPEN_PY)
    cs_txt = _disk_text(LAUNCH_CS, "ascii")
    esc_exe = ml._reg_escape(ml.LAUNCH_EXE)
    esc_pw = ml._reg_escape(ml.interpreter())        # interpreter() 就是 pythonw.exe
    mine_ow = lambda: items_reg_openwith(REG_OW, "注册")          # noqa: E731
    mine_owd = lambda: items_reg_openwith(REG_OW_DEL, "撤销")     # noqa: E731
    cases = [
        ("command 写成 %~1（那是批处理语法）", REG_ADD,
         add_txt.replace("%1", "%~1"), "reg", ("%~1", "不一致"),
         lambda: items_reg(REG_ADD, "新增", [ml.lint_reg, ml.lint_command_line])),
        ("新增模板掉了 BOM", REG_ADD, None, "nobom", ("BOM", "读不出来", "不一致"),
         lambda: items_reg(REG_ADD, "新增", [ml.lint_reg, ml.lint_command_line])),
        ("新增模板换成 LF 行尾", REG_ADD, add_txt.replace("\r\n", "\n"), "reg",
         ("裸 LF", "不一致"),
         lambda: items_reg(REG_ADD, "新增", [ml.lint_reg, ml.lint_command_line])),
        ("撤销模板掉了键名前的 '-'", REG_DEL, del_txt.replace("[-", "["), "reg",
         ("新增而不是删除", "不一致"),
         lambda: items_reg(REG_DEL, "撤销", [ml.lint_reg, ml.lint_del])),
        ("启动 .bat 换成 LF 行尾（上一版的真实坏法）", BAT_RUN,
         run_txt.replace("\r\n", "\n"), "gbk", ("裸 LF", "劈开", "不一致"),
         lambda: items_bat(BAT_RUN, "%*")),
        ("自检 .bat 里的解释器改成不存在的路径", BAT_DOC,
         doc_txt.replace(ml.console_python(), "C:/nope/python.exe"), "gbk",
         ("不存在", "不一致"), lambda: items_bat(BAT_DOC, "-m app.doctor")),
        ("open_file.py 删掉 sys.path.insert", OPEN_PY,
         op_txt.replace("sys.path.insert(0, ROOT)", "pass"), "utf-8", ("sys.path",),
         items_open_py),
        # 这一条是给"判据太松"补的：只 grep `__file__` 在不在时，把 ROOT 写成只跳一层
        # （算出 packaging\）两道闸都会放行，直到真跑才炸。现在生成器的 lint 与本文件的
        # 陌生 cwd 探针都必须点名。
        ("open_file.py 的 ROOT 只跳一层（算出 packaging\\）", OPEN_PY,
         op_txt.replace("os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
                        "os.path.dirname(os.path.abspath(__file__))"),
         "utf-8", ("ROOT",), items_open_py),
        # ---- 「打开方式」那两份＋启动器源。每条的 want 都点名**哪一规则**抓的，
        # 只要求 --check 退回非 0 等于把"随便哪儿红了"当成过关。
        ("打开方式模板掉了 BOM", REG_OW, None, "nobom", ("读不出来", "BOM"), mine_ow),
        ("打开方式模板换成 LF 行尾", REG_OW, ow_txt.replace("\r\n", "\n"), "reg",
         ("裸 LF",), mine_ow),
        ("OpenWithProgids 那条值写成非空串", REG_OW,
         ow_txt.replace(f'"{ml.PROGID}"=""', f'"{ml.PROGID}"="{ml.PROGID}"'), "reg",
         ("不是空串",), mine_ow),
        ("命令落点换成 pythonw（列表里那行就叫 pythonw 了）", REG_OW,
         ow_txt.replace(esc_exe, esc_pw), "reg", ("pythonw",), mine_ow),
        ("多写一行 UserChoice（那是默认程序的门）", REG_OW,
         ow_txt.replace('"FriendlyTypeName"', '"UserChoice"', 1), "reg",
         ("UserChoice",), mine_ow),
        ("Applications 那节写成别的 exe（列表里就没我们了）", REG_OW,
         ow_txt.replace("Applications" + chr(92) + ml.LAUNCH_NAME,
                        "Applications" + chr(92) + "other.exe"), "reg",
         ("少了这一根键",), mine_ow),
        ("撤销件整根删掉一个扩展名的 OpenWithProgids", REG_OW_DEL,
         first_hit(owd_txt, "OpenWithProgids]", lambda l: l.replace("[HKEY", "[-HKEY")),
         "reg", ("整根删掉",), mine_owd),
        ("撤销件漏删 App Paths 那根键", REG_OW_DEL,
         owd_txt.replace("App Paths", "App Patsh", 1), "reg",
         ("没有删这一根",), mine_owd),
        ("撤销件的摘值行写成清空而不是按名字删", REG_OW_DEL,
         owd_txt.replace(f'"{ml.PROGID}"=-', f'"{ml.PROGID}"=""'), "reg",
         ("不是一条按名字的删除",), mine_owd),
        ("launcher.cs 改了没重编（exe 与源不是同一份）", LAUNCH_CS,
         cs_txt.replace("Gelu Player", "Gelu Player X"), "ascii", ("不一致",), items_launcher),
    ]
    tally = {"CAUGHT": 0, "MISSED": 0, "NOP": 0}
    for name, path, bad, enc, want, mine_fn in cases:
        tag2, why = mutate_and_check(path, bad, enc, want, mine_fn)
        tally[tag2] = tally.get(tag2, 0) + 1
        say(tag2, name, why)
    # 对照组：全部还原之后再跑一次，必须回到"全绿"——否则"抓到"可能只是文件被我搞丢了
    r = gen("--check")
    gone = [p for p in ALLTEXT if not os.path.exists(p)]
    if not os.path.exists(LAUNCH_EXE):
        gone.append(LAUNCH_EXE)
    ok_back = r.returncode == 0 and not gone
    results.append(("全部还原后 --check 重新变绿（对照组）", ok_back, ""))
    say("PASS" if ok_back else "FAIL", "全部还原后 --check 重新变绿（对照组）",
        "；".join(l for l in r.stdout.decode("utf-8", "replace").splitlines()
                  if l.startswith("[FAIL]"))[:150]
        or (f"{len(ALLTEXT)} 份文本产物加启动器一致" if ok_back else "丢了：" + str(gone)))
    ruler = count_rows_selftest()
    print(f"\n== 数行那把尺的自检（{len(ruler)} 格已知答案样本，不改盘上文件）==")
    emit(ruler)
    ruler_bad = [n for n, ok, _ in ruler if not ok]
    print(f"数行尺自检 {len(ruler)} 格："
          + ("每格都答对" if not ruler_bad else f"答错 {len(ruler_bad)} 格：" + "；".join(ruler_bad)))

    print(f"\n== 盘上 {len(ALLTEXT)} 份文本产物加启动器的 md5（本轮终态）==")
    for p in ALLTEXT + [LAUNCH_EXE]:
        print(f"  {md5(p)}  {os.path.relpath(p, ROOT)}")
    print(f"\n证伪 {len(cases)} 条：CAUGHT {tally['CAUGHT']}／MISSED {tally['MISSED']}／"
          f"NOP {tally['NOP']}（漏网与注入失效都必须修，不许为此放宽判据）"
          f"；数行尺自检 {len(ruler)} 格，答错 {len(ruler_bad)} 格")
    return 1 if tally["MISSED"] or tally["NOP"] or ruler_bad else 0


# ---------------------------------------------------------------- 主流程
def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    if "--falsify" in argv:
        return falsify()
    if not os.path.exists(GEN):
        say("FAIL", "生成器不在", GEN)
        return 1
    check_all()
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，失败 {len(fails)} 项"
          + (f"：{'；'.join(fails)}" if fails else ""))
    if not fails:
        print("本文件只验字节，没碰注册表。要「打开方式」里出现播放器就双击 "
              r"packaging\reg\注册到打开方式.reg"
              "（只写 HKCU\\Software\\Classes，不动默认打开方式）；想撤就双击 "
              r"packaging\reg\撤销打开方式注册.reg"
              "。导入之后用 python tests/check_openwith.py --check 核对注册表现数。")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
