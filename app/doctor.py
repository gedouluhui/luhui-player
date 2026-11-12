"""故障自检入口：`python -m app.doctor`。

为什么要单独一份，而不是让播放器自己弹窗报：最常见的两个故障（双击 .lnk 没反应、
放文件黑屏）恰恰发生在"界面开不起来"的时候，那时候指望界面自己报错是指望不上的。
这份脚本只要求解释器能跑起来：不开窗口、不看像素、锁屏也能出报告。

三条口径（和项目里验收脚本同一套规矩）：
1. 每一项都写**查了什么、看到什么、下一步做什么**，"看起来正常"不算结论；
2. 环境不满足要写成 WARN 并说明它不阻断什么，不许把环境问题报成产品坏了；
3. 判据要能被证伪：`--falsify` 喂每个判据一组已知好/已知坏的事实，坏的不翻红就是假绿光。

用法：
    python -m app.doctor            # 人读的报告（有 FAIL 时退出码 1）
    python -m app.doctor --keys     # 打印当前生效的键位表（改了键位也照这里查）
    python -m app.doctor --falsify  # 证伪：已知坏事实必须让对应判据翻红
    python -m app.doctor --neg      # 负对照：在没有内核的临时副本里跑，整份报告必须翻红
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

try:                                    # 非 Windows 没有这个模块：那一路只能"问不到"，不能判坏
    import winreg
    WINREG_ERR = ""
except ImportError as _exc:
    winreg = None
    WINREG_ERR = f"winreg 不可用：{_exc}"

from . import config as cfgmod

OK, WARN, FAIL = "OK", "WARN", "FAIL"
LEVEL_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
# 登记外的档位（将来加了第四档又忘了配标记）落在这里。单独定成常量是**给验收用的**：
# `tests/test_launchers.py` 数报告行时要把这个形状也数进来，然后单独要求它不许出现——
# 否则它只会表现为"行数莫名其妙少一行"，那是把产品 bug 伪装成缺行（见那里的 doctor_marks）。
UNKNOWN_MARK = "[????]"

# 单实例用的命名管道：main.py 里 QLocalServer 用的名字是 f"{BRAND_ID}-player-v1"，
# Windows 上 Qt 把它建成 \\.\pipe\<同名>，所以这里**不启 Qt、不开窗口**也能问出
# "是不是已经有一个实例在跑"——那正是"双击没反应"的头号原因。
PIPE_PREFIX = r"\\.\pipe" + "\\"


# ---------------------------------------------------------------- 采集（IO 只在这儿）
def mpv_version(exe: str) -> tuple[str, str]:
    """跑一次 `mpv --version` 取第一行。回 (版本行, 失败原因)。"""
    if not os.path.exists(exe):
        return "", f"文件不在：{exe}"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=15, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except BaseException as exc:  # noqa: BLE001 - 采不到就报原因，绝不让 doctor 自己崩
        return "", f"跑不起来：{type(exc).__name__}: {exc}"
    first = (r.stdout or "").splitlines()
    return (first[0].strip() if first else ""), ("" if r.returncode == 0 and first
                                                 else f"退出码 {r.returncode}")


def ffmpeg_version(exe: str) -> tuple[str, str]:
    if not os.path.exists(exe):
        return "", f"文件不在：{exe}"
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except BaseException as exc:  # noqa: BLE001
        return "", f"跑不起来：{type(exc).__name__}: {exc}"
    first = (r.stdout or "").splitlines()
    return (first[0].strip() if first else ""), ("" if r.returncode == 0 else
                                                 f"退出码 {r.returncode}")


def pipe_listening(name: str) -> bool | None:
    """命名管道有没有人开着（＝已有一个实例在听）。非 Windows 回 None（不判）。

    本机实测的第一版死在这儿：`restype=c_void_p` 失败时并不回 None，而是把
    INVALID_HANDLE_VALUE(-1) 当成 0xFFFFFFFFFFFFFFFF 那么个大整数交回来，
    拿它去 CloseHandle 就成了 `ArgumentError: int too long to convert`——
    所以判"没人听"要认那三个值（None / -1 / 全 F），而 CloseHandle 得先声明参数类型。
    """
    if sys.platform != "win32":
        return None
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    # GENERIC_READ|GENERIC_WRITE=0xC，OPEN_EXISTING=3：只是探一下有没有服务端，不发消息
    h = k.CreateFileW(PIPE_PREFIX + name, 0xC, 0, None, 3, 0, None)
    if h is None or h == -1 or h == 0xFFFFFFFFFFFFFFFF:
        return False
    k.CloseHandle(h)
    return True


def write_probe(dirs: list[str]) -> tuple[bool, str]:
    """可写性要**真写一次**再删：目录存在≠能写（只读盘、权限、防勒索软件都拦得住）。"""
    for d in dirs:
        p = os.path.join(d, f".gelu-doctor-{os.getpid()}.tmp")
        try:
            os.makedirs(d, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("probe")
            os.remove(p)
        except OSError as exc:
            return False, f"{d}：{type(exc).__name__}: {exc}"
    return True, "、".join(os.path.basename(d) or d for d in dirs)


def tail_lines(path: str, n: int = 3) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [l.rstrip() for l in fh if l.strip()][-n:]
    except OSError:
        return []


def measure_free(path: str) -> float:
    """量目录所在卷的剩余空间，单位 GB；量不到返回 -1（不许当成 0，也不许当成充足）。

    目录还没建（第一次用、或用户把截图目录指到别处）时往上找存在的祖先再量：同一个卷，
    数字是真的。指到没挂载的盘时祖先一路走到 `Z:/` 还是不存在，那就老实返回 -1，
    由 j_space 判 WARN——旧写法在这里得 -1 后被 min() 丢开，整项照样报 OK。
    """
    cur, guard = str(path), 0
    while cur and not os.path.exists(cur) and guard < 32:
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur, guard = parent, guard + 1
    try:
        return round(shutil.disk_usage(cur or str(path)).free / 1024 ** 3, 1)
    except OSError:
        return -1


def desktop_dir() -> str:
    if sys.platform != "win32":
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        # SHGetFolderPathW(CSIDL_DESKTOP=0)：不用装 pywin32 也能问到真实桌面
        # （OneDrive 重定向过的桌面路径，写死 %USERPROFILE%\\Desktop 会找错地方）
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf) == 0:
            return buf.value
    except BaseException:  # noqa: BLE001
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


# ============================================================ 外部打开那一路（只读）
# 为什么这三样要放在一起问：注册表里那句话（双击到底该跑谁）、那句话指着的文件（跑不跑得
# 起来）、以及上一次真双击时收到了什么（launch.log）。少看任何一样都能"报 OK 却放不出片"——
# 用户报的「设为默认后双击只开窗口、不加载文件」就是这么漏过去的：exe 在、键在、代码也没错，
# 缺的恰好是"当时到底递进来几个参数"这一列证据。
#
# **这里出现的 winreg 调用只有 OpenKey / QueryValueEx / EnumValue / EnumKey / CloseKey，
# 全是读。** 改默认程序要动 UserChoice（系统给它加了哈希保护），那是用户自己的选择，
# 本程序和这份自检都不碰；tests/check_openwith.py 里那套"变化只允许三种"的闸门也照同一口径。
BS = chr(92)


def kp(*parts: str) -> str:
    """相对 HKCU 的键路径。用 chr(92) 拼而不在字面量里写 `\\\\`：源码里的双反斜杠
    一旦经过"写文件时 \\ 被折成 \"那条路就会静默变味（本机踩过），键名错了的自检比没自检更坏。"""
    return BS.join([str(p) for p in parts if p])


def load_generators() -> tuple[Any, str]:
    """按**文件路径**加载 packaging/make_launchers.py：注册表键名的唯一真源。

    不走 `from packaging import ...`：本机 site-packages 里 `packaging` 是个真包（装依赖用的那个），
    会绑到那边然后 AttributeError。键名也不能在这份文件里手抄一遍——抄的那份会在生成器改名的
    那天悄悄变成"自检查着一根不存在的键，然后报 OK"。
    """
    path = os.path.join(cfgmod.ROOT, "packaging", "make_launchers.py")
    try:
        spec = importlib.util.spec_from_file_location("gelu_doctor_launchers", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, ""
    except BaseException as exc:  # noqa: BLE001 - 生成器读不起来是环境问题，不许冒充产品坏了
        return None, f"{type(exc).__name__}: {exc}"


def reg_value_at(hive: int, rel: str, name: str = "") -> tuple[str, bool, str]:
    """reg_value 的"任意顶层根"版：HKCU 之外还要问 HKCR（HKCR＝HKCU\\Classes ∪ HKLM\\Classes）。

    别人家的 ProgID 几乎都注册在 HKLM 那半边，只问 HKCU 就会把"迅雷影音"读成
    `xmp.mkv` 这种谁都念不出来的字符串——报告上要写清"现在归谁"，念不出来就等于没说。
    """
    if winreg is None:
        return "", False, WINREG_ERR or "winreg 不可用"
    try:
        h = winreg.OpenKey(hive, rel)
    except FileNotFoundError:
        return "", True, ""
    except PermissionError as exc:
        return "", False, f"没权限读：{exc}"
    except OSError as exc:
        return "", False, f"读不了：{exc}"
    try:
        try:
            data, _typ = winreg.QueryValueEx(h, name)
        except FileNotFoundError:
            return "", True, ""
        except OSError as exc:
            return "", False, f"读不了：{exc}"
        return ("" if data is None else str(data)), False, ""
    finally:
        try:
            winreg.CloseKey(h)
        except OSError:
            pass


def reg_value(rel: str, name: str = "") -> tuple[str, bool, str]:
    """HKCU 下一个值 → (值文本, 是否"确实没登记", 环境原因)。

    三个返回值各管一件事：读到东西 / 盘上真的没有 / **我读不了所以不知道**。第三种绝不允许
    判成产品坏了——旧写法把"没权限读"和"没注册"混成同一个 None，权限一紧整份报告就全红。
    """
    if winreg is None:
        return "", False, WINREG_ERR or "winreg 不可用"
    return reg_value_at(winreg.HKEY_CURRENT_USER, rel, name)


def reg_value_names(rel: str) -> tuple[list[str] | None, str]:
    """一根键下面的**值名**清单（OpenWithProgids 把每个候选程序放成一条值，不是子键）。
    回 (清单或 None, 原因)；None 只在"读不了"时出现——键不存在是空清单，那是事实。"""
    if winreg is None:
        return None, WINREG_ERR or "winreg 不可用"
    try:
        h = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel)
    except FileNotFoundError:
        return [], ""
    except OSError as exc:
        return None, f"读不了：{exc}"
    try:
        names, i = [], 0
        while True:
            try:
                n, _d, _t = winreg.EnumValue(h, i)
            except OSError:
                break
            i += 1
            names.append(str(n))
        return names, ""
    except OSError as exc:
        return None, f"枚举值失败：{exc}"
    finally:
        try:
            winreg.CloseKey(h)
        except OSError:
            pass


def progid_label(pid: str) -> str:
    """ProgID → 人认得的名字（"迅雷影音"而不是 `xmp.mkv`）。

    查不到就**回原文**：编一个好听的名字比留一串看不懂的值更容易骗人。
    问的顺序是 HKCU 先、HKCR 后：HKCR 是 HKCU\\Classes ∪ HKLM\\Classes 的合并视图，
    别人家的程序多数注册在 HKLM 那半边——只问 HKCU 会把迅雷读成 `xmp.mkv`，
    而报告上念不出来的名字等于没说。
    """
    if not pid or winreg is None:
        return ""
    hives = [getattr(winreg, "HKEY_CLASSES_ROOT", None),
             getattr(winreg, "HKEY_LOCAL_MACHINE", None)]
    probes = []
    for hive in (h for h in hives if h is not None):
        probes += [(hive, kp("Software", "Classes", pid), ""),
                   (hive, kp("Software", "Classes", pid), "FriendlyTypeName"),
                   (hive, kp("Software", "Classes", "Applications", pid), "FriendlyAppName")]
    for hive, rel, name in probes:
        try:
            text, _missing, _why = reg_value_at(hive, rel, name)
        except BaseException:  # noqa: BLE001 - 问不到就下一条，绝不许把整份自检带倒
            continue
        if text.strip():
            return text.strip()
    return pid


# 双击最常遇到的两种容器：一个"照片/视频混着放"的默认档，一个"下载下来就是它"的档。
DEFAULT_PROBE_EXTS = (".mkv", ".mp4")


def external_facts(ml: Any) -> dict[str, Any]:
    """外部打开那一路能问到的全部事实（IO 只在这儿；判据拿到的是纯数据）。"""
    f: dict[str, Any] = {
        "platform": sys.platform, "gen_err": "", "winreg_err": WINREG_ERR,
        "launch_exe": "", "exe_exists": False, "progid": "", "app_title": cfgmod.APP_TITLE,
        "cmd": "", "cmd_absent": False, "cmd_env": "", "cmd_exe_exists": False,
        "app_cmd": "", "apppaths": "",
        "exts": [], "ext_count": 0, "registered": 0, "missing": [], "env": [],
        "defaults": {},
    }
    if ml is None:
        f["gen_err"] = load_generators()[1]
        return f
    f["launch_exe"] = str(ml.LAUNCH_EXE)
    f["exe_exists"] = os.path.exists(str(ml.LAUNCH_EXE))
    f["progid"] = str(ml.PROGID)
    f["app_title"] = str(ml.APP_TITLE)
    cmd, absent, why = reg_value(kp(ml.KEY_PROGID, "shell", "open", "command"))
    f["cmd"], f["cmd_absent"], f["cmd_env"] = cmd, absent, why
    f["app_cmd"] = reg_value(kp(ml.KEY_APP, "shell", "open", "command"))[0]
    f["apppaths"] = reg_value(ml.KEY_APPPATHS)[0]
    # 命令里那句话指着的文件到底在不在盘上——**在采集侧问**，判据保持纯函数：
    # 判据里调 os.path.exists 会让 --falsify 喂进来的假路径在真机上得到真答案，
    # 同一条样本换台机器就换个颜色，那本证伪账就废了。
    f["cmd_exe_exists"] = os.path.exists(_cmd_exe_and_take(f["cmd"])[0]) if f["cmd"] else False
    try:
        f["exts"] = [str(e) for e in ml.media_exts()]
    except BaseException as exc:  # noqa: BLE001 - 名单读不到就只报"没验过"，别把整项判废
        f["exts"] = []
        f["env"].append(f"扩展名名单读不出：{type(exc).__name__}: {exc}")
    f["ext_count"] = len(f["exts"])
    for ext in f["exts"]:
        names, why = reg_value_names(kp("Software", "Classes", ext, "OpenWithProgids"))
        if names is None:
            f["env"].append(f"{ext}：{why}")
            continue
        if f["progid"] in names:
            f["registered"] += 1
        else:
            f["missing"].append(ext)
    for ext in DEFAULT_PROBE_EXTS:
        uc, uc_absent, uc_why = reg_value(
            kp("Software", "Microsoft", "Windows", "CurrentVersion",
               "Explorer", "FileExts", ext, "UserChoice"), "ProgID")
        cls, cls_absent, cls_why = reg_value(kp("Software", "Classes", ext))
        row = {"userchoice": uc, "class_default": "" if cls_absent else cls,
               "label": progid_label(uc) or progid_label("" if cls_absent else cls),
               # 空 ProgID 绝不能等于空我们的 PROGID：那会把"这台机器上什么都没登记"
               # 读成"默认就是我们"，于是自检在恰恰最需要说话的时候报 OK。
               "ours": bool(uc.strip()) and uc.strip().lower() == str(f["progid"]).lower(),
               # blind＝"读不了"（权限、路径异常）。它和"确实没登记"必须分开：前者只能报
               # "这一项没验过"，后者才是事实。混起来就会在权限紧的那一刻把整份报告刷红。
               "blind": bool(uc_why) or bool(cls_why)}
        if uc_why:
            f["env"].append(f"{ext} 的 UserChoice：{uc_why}")
        if cls_why:
            f["env"].append(f"{ext} 的类型默认值：{cls_why}")
        f["defaults"][ext] = row
    return f


LAUNCH_ROW = re.compile(r"(?P<time>[\d-]+ [\d:]+) \| pid=\S+ \| kind=(?P<kind>\S+) \| "
                        r"argv=(?P<argv>\d+) kept=(?P<kept>\d+) dropped=(?P<dropped>\d+) "
                        r"ignored=(?P<ignored>\d+)")


def launch_facts(deep: bool = True) -> dict[str, Any]:
    """把 launch.log 读成结构化事实：哪一行算"外部打开"、最近一次收到了几条。

    判据只吃这里的结果，不再自己 parse——同一份正则有两处就会有两处行为。
    """
    path = os.path.join(cfgmod.LOG_DIR, "launch.log")
    out: dict[str, Any] = {"path": path, "exists": os.path.exists(path),
                           "rows": 0, "external": 0, "silent": 0, "truncated": 0,
                           "last": None, "read_err": ""}
    if not out["exists"]:
        return out
    # 留痕那本账自己 capped 在 400 行（app/main.py 的 LAUNCH_LOG_KEEP），所以这里给一个
    # **宽得多**的常数就够了——不把 400 抄成第二处真源：抄了的那份会在改上限那天变成
    # "只看到最近 400 条之外的启动，于是报『没有外部打开记录』"。
    lines = tail_lines(path, 5000)
    out["rows"] = len(lines)
    for line in lines:
        m = LAUNCH_ROW.search(line)
        if not m:
            continue
        rec = {"time": m.group("time"), "kind": m.group("kind"),
               "argv": int(m.group("argv")), "kept": int(m.group("kept")),
               "dropped": int(m.group("dropped")), "ignored": int(m.group("ignored")),
               "raw": line}
        if rec["kind"] == "truncated":
            out["truncated"] += 1
        if rec["argv"] > 0:
            out["external"] += 1
            if rec["kept"] == 0:
                out["silent"] += 1
            out["last"] = rec
    return out


def _newest_source() -> tuple[float, str]:
    """`app/` 下最新改过的 .py（含修改时间）。crash.log 比它还早 ⇒ 那次栈来自旧代码。

    只扫产品源码，不扫 tests/runtime：测试脚本天天变，拿它当基准会让**每一次**历史
    crash 都显得"比代码旧"，这条分流就失去意义了。
    """
    best: tuple[float, str] = (0.0, "")
    base = os.path.join(cfgmod.ROOT, "app")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt > best[0]:
                best = (mt, p)
    return best


def _fmt_ts(ts: Any) -> str:
    try:
        n = float(ts)
    except (TypeError, ValueError):
        return "?"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n)) if n > 0 else "?"


def collect(deep: bool = True) -> dict[str, Any]:
    """把事实一次采齐，判据只做纯计算（--falsify 靠的就是这个分工）。"""
    cfg_path = os.path.join(cfgmod.DATA_DIR, "config.json")
    raw = ""
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raw = f"\x00{type(exc).__name__}: {exc}"
    crash = os.path.join(cfgmod.LOG_DIR, "crash.log")
    mpv_log = os.path.join(cfgmod.LOG_DIR, "mpv.log")
    qt_err = ""
    qt_ver = ""
    try:
        from PyQt5 import QtCore            # 只 import，不建 QApplication
        qt_ver = f"PyQt5 {QtCore.PYQT_VERSION_STR} / Qt {QtCore.QT_VERSION_STR}"
        import PyQt5.QtGui                  # noqa: F401
        import PyQt5.QtWidgets              # noqa: F401
    except BaseException as exc:  # noqa: BLE001
        qt_err = f"{type(exc).__name__}: {exc}"
    out: dict[str, Any] = {
        "py": sys.version.split()[0], "platform": sys.platform,
        "qt_err": qt_err, "qt_ver": qt_ver,
        "root": cfgmod.ROOT, "runtime": cfgmod.RUNTIME_DIR,
        "mpv_exists": os.path.exists(cfgmod.MPV_EXE), "mpv_ver": "", "mpv_run_err": "",
        "ff_exists": os.path.exists(cfgmod.FFMPEG_EXE), "ff_ver": "", "ff_run_err": "",
        "data_dir": cfgmod.DATA_DIR, "cfg_path": cfg_path, "cfg_raw": raw,
        "crash_exists": os.path.exists(crash), "crash_path": crash,
        "crash_tail": tail_lines(crash), "crash_bytes": (os.path.getsize(crash)
                                                         if os.path.exists(crash) else 0),
        # 崩溃记录的**年龄**：老崩溃留在盘上是有价值的病史，不是"这次坏了"的证据。
        # 判据（j_crash）拿它和最新源码的修改时间比：早于源码 ⇒ 历史记录，晚于 ⇒ 真事。
        "crash_mtime": (os.path.getmtime(crash) if os.path.exists(crash) else 0.0),
        "newest_src_mtime": 0.0, "newest_src_path": "",
        "mpv_log_exists": os.path.exists(mpv_log), "mpv_log_path": mpv_log,
        "mpv_err_lines": [],
        "pipe": None, "pipe_name": f"{cfgmod.BRAND_ID}-player-v1",
        "writable": (True, ""), "shot_dir": "", "free_gb": {},
        "lnk_dir": desktop_dir(), "lnk_exists": False,
        "key_rows": 0, "key_dead": [], "key_shadow": [],
        "perf": {}, "perf_warn": [],
        # 资源占用那一路（统计页下半页）：两个可选依赖，缺席的**后果不一样**，所以分开采。
        # rows 用三态：None＝这一轮没问（浅检查），0＝问了且没读数，>0＝读到几块卡。
        "usage_psutil_why": "", "usage_smi": "", "usage_smi_rows": None, "usage_smi_err": "",
    }
    if out["crash_exists"]:
        out["newest_src_mtime"], out["newest_src_path"] = _newest_source()
    if out["mpv_exists"] and deep:
        out["mpv_ver"], out["mpv_run_err"] = mpv_version(cfgmod.MPV_EXE)
    if out["ff_exists"] and deep:
        out["ff_ver"], out["ff_run_err"] = ffmpeg_version(cfgmod.FFMPEG_EXE)
    # 统计页「资源占用」那四行的两条数据源。分工照 usage.py 的口径来：
    # psutil 缺＝提醒（那几行整页写"没装 psutil"，但不影响播放）；没有 nvidia-smi＝**正常情况**
    # （非 N 卡或没装驱动），对它喊狼会让人学会忽略整份报告；找得到却跑不通／没读数＝提醒。
    from .engine import usage as usage_mod      # 不认 Qt，纯采集
    out["usage_psutil_why"] = usage_mod.psutil_unavailable()
    out["usage_smi"] = usage_mod.nvidia_smi_path() or ""
    if out["usage_smi"] and deep:
        smi_rows, smi_why = usage_mod.query_smi()
        out["usage_smi_rows"], out["usage_smi_err"] = len(smi_rows), smi_why
    out["pipe"] = pipe_listening(out["pipe_name"])
    out["writable"] = write_probe([cfgmod.DATA_DIR, cfgmod.CACHE_DIR, cfgmod.LOG_DIR])
    try:
        out["shot_dir"] = str(json.loads(raw).get("screenshot_dir") or "") if raw else ""
    except BaseException:
        out["shot_dir"] = ""
    for d in (cfgmod.DATA_DIR, out["shot_dir"] or cfgmod.SHOT_DIR):
        out["free_gb"][d] = measure_free(d)
    out["lnk_exists"] = bool(out["lnk_dir"]) and os.path.exists(
        os.path.join(out["lnk_dir"], f"{cfgmod.APP_TITLE}.lnk"))
    if out["mpv_log_exists"]:
        out["mpv_err_lines"] = [l for l in tail_lines(mpv_log, 400)
                               if re.search(r"\berror\b|\bfatal\b", l, re.I)][:3]
    try:
        from .ui import keymap
        rows = keymap.effective(json.loads(raw).get("keymap") if raw else None)
        out["key_rows"] = len(rows)
        out["key_dead"] = [r[0] for r in rows if keymap.dead(r[0])]
        out["key_shadow"] = [(a[0], b[0]) for a, b in keymap.shadowed(rows)]
    except BaseException:
        pass
    try:
        from .engine import perf
        cfg = cfgmod.Config()
        out["perf"] = {k: str(v) for k, v in perf.resolve(cfg).items()
                       if k in ("hwdec", "gpu_api", "profile")}
        out["perf_warn"] = list(perf.warnings(cfg))
    except BaseException:
        pass
    # 外部打开那一路：注册表现数 + 上一次真双击收到了什么。都在**只读**函数里，
    # 这一项绝不写注册表（写的那次是用户点了头才做的，见 tests/check_openwith.py 的口径）。
    ml, _gen_err = load_generators()
    out["external"] = external_facts(ml)
    out["launchlog"] = launch_facts(deep)
    return out


# ---------------------------------------------------------------- 判据（纯函数）
def j_python(f: dict) -> tuple[str, str, str]:
    seen = f"Python {f.get('py')}（{f.get('platform')}），目录 {f.get('root')}"
    if f.get("platform") != "win32":
        return FAIL, seen, "本播放器依赖 Win32 窗口嵌入与 DWM，只能在 Windows 上跑"
    try:
        major, minor = (int(x) for x in str(f.get("py")).split(".")[:2])
    except (TypeError, ValueError):
        return WARN, seen, "读不出解释器版本，请核对 python --version"
    if (major, minor) < (3, 9):
        return FAIL, seen, "需要 Python 3.9+（代码用了 match 之外的 3.9 语法与类型写法）"
    return OK, seen, "——"


def j_pyqt(f: dict) -> tuple[str, str, str]:
    if f.get("qt_err"):
        return FAIL, f"import PyQt5 失败：{f['qt_err']}", \
            "这个解释器没装或装坏了 PyQt5：`python -m pip install \"PyQt5==5.15.10\"`，或换一个装过它的解释器"
    return OK, f.get("qt_ver") or "已装（版本没读到）", "——"


def j_mpv(f: dict) -> tuple[str, str, str]:
    if not f.get("mpv_exists"):
        return FAIL, f"内核不在：{os.path.join(str(f.get('runtime')), 'mpv', 'mpv.exe')}", \
            "把 mpv 的 dll/exe 放进 runtime\\mpv\\（缺它完全放不了片）"
    if f.get("mpv_run_err") or not f.get("mpv_ver"):
        return FAIL, f"内核在，但 `mpv --version` 跑不通：{f.get('mpv_run_err') or '没有输出'}", \
            "多半是缺依赖 dll 或架构不匹配（要 64 位）；重装一份完整版 mpv 到 runtime\\mpv\\"
    return OK, f["mpv_ver"], "——"


def j_ffmpeg(f: dict) -> tuple[str, str, str]:
    if not f.get("ff_exists"):
        return WARN, f"没找到 ffmpeg：{os.path.join(str(f.get('runtime')), 'ffmpeg', 'ffmpeg.exe')}", \
            "只影响 GIF 导出与时间轴缩略图，正常播放不受影响；要用就把 ffmpeg.exe 放进 runtime\\ffmpeg\\"
    if f.get("ff_run_err"):
        return WARN, f"ffmpeg 在但跑不通：{f['ff_run_err']}", "同上：GIF/缩略图会不成，播放不受影响"
    return OK, f["ff_ver"], "——"


def j_writable(f: dict) -> tuple[str, str, str]:
    ok, seen = f.get("writable") or (False, "没测")
    return (OK, f"可写：{seen}", "——") if ok else \
           (FAIL, f"写不进去：{seen}", "换目录（LUHUI_DATA 环境变量可指到别处；旧名 GELU_DATA 也认）或给它写权限；"
                                     "配置、缓存、截图都要落在这里")


def j_space(f: dict) -> tuple[str, str, str]:
    free = f.get("free_gb") or {}
    seen = "；".join(f"{os.path.basename(k) or k} 剩 {v} GB" for k, v in free.items())
    blind = [str(k) for k, v in free.items() if v < 0]
    worst = min([v for v in free.values() if v >= 0], default=-1)
    if not free:
        return WARN, "一处剩余空间都没量到（data 与截图目录都没量上）", \
            "这一项没被验证过，别当成「空间充足」；两处都读不到通常是权限问题"
    if worst >= 0 and worst < 0.5:
        return FAIL, seen, "盘快满了：截图与 GIF 产物写不下，播放器也会存不了配置"
    # 量不出来的路径（目录还不存在、指向没挂载的盘、没权限 stat）绝不能被 min() 丢开：
    # 真机实测旧写法把「截图 剩 -1 GB」和 40 GB 一起报成 OK——判不了就是判不了，
    # 得让报告点名"这条没验过"，否则它就是个吞掉盲区的恒真分支。
    if blind:
        return WARN, f"{seen}（其中 {'、'.join(blind)} 量不出来）", \
            "这一项没法判断：路径当前不存在或读不了。第一次用是正常的（目录还没建）；" \
            "要是你把截图目录指到别的盘，先确认那个盘在不在"
    if worst < 2:
        return WARN, seen, "建议留 2 GB 以上（连拍与 GIF 一趟就能吃掉几百 MB）"
    return OK, seen, "——"


def j_config(f: dict) -> tuple[str, str, str]:
    raw = f.get("cfg_raw") or ""
    path = f.get("cfg_path")
    if not raw:
        return OK, f"没有配置文件（第一次用），{path}", "——"
    if raw.startswith("\x00"):
        return FAIL, f"读文件就失败：{raw[1:]}", "多半是权限或被别的程序锁住；关掉编辑器再试"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return FAIL, f"不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）", \
            f"启动时它会被自愈成默认值（你的自定义会丢）。先备份再改：{path}"
    if not isinstance(data, dict):
        return FAIL, f"顶层不是对象（是 {type(data).__name__}）", f"删掉它让播放器重建：{path}"
    keys = list(data)[:6]
    bad = []
    if "screenshot_dir" in data and not str(data["screenshot_dir"] or "").strip():
        bad.append("screenshot_dir 是空的")
    if "volume" in data and not isinstance(data["volume"], (int, float, str)):
        bad.append(f"volume 类型不对（{type(data['volume']).__name__}）")
    if bad:
        return WARN, f"能解析，但有 {len(bad)} 处可疑：{'、'.join(bad)}（键数 {len(data)}）", \
            "这些键会被兜成可用值；想照你的意思生效就改成正确类型"
    return OK, f"合法 JSON，{len(data)} 个键（前几个：{'、'.join(keys)}）", "——"


def j_instance(f: dict) -> tuple[str, str, str]:
    if f.get("pipe") is None:
        return WARN, "非 Windows，没法问命名管道", "——"
    name = f.get("pipe_name")
    if f.get("pipe"):
        return WARN, f"已经有一个实例在听管道 {name}", \
            "「双击没反应」多半不是坏了：新的那一次只是把文件转交给它。" \
            "去任务栏、其他虚拟桌面或托盘找那个窗口；确实找不到就在任务管理器里关掉播放器进程"
    return OK, f"没有实例在听 {name}（这次是第一次开）", "——"


def j_crash(f: dict) -> tuple[str, str, str]:
    if not f.get("crash_exists"):
        return OK, "没有 crash.log（没记到未捕获异常）", "——"
    tail = " ‖ ".join((f.get("crash_tail") or [])[-2:]) or "(空)"
    try:
        crash_mt = float(f.get("crash_mtime") or 0)
        src_mt = float(f.get("newest_src_mtime") or 0)
    except (TypeError, ValueError):
        crash_mt = src_mt = 0.0
    if crash_mt > 0 and src_mt > 0 and crash_mt < src_mt:
        # 早于最新源码 ⇒ 那次栈说的是旧代码。留在盘上仍然有价值（病史），但把它报成
        # "现在坏了"会训练用户忽略整份报告——用户 10-20 已实测到这条永久 WARN。
        return OK, (f"crash.log 是历史记录（{_fmt_ts(crash_mt)}），早于最近一次改过的源码"
                    f"（{_fmt_ts(src_mt)}，{os.path.basename(str(f.get('newest_src_path') or '')) or 'app'}）："
                    f"这之后没有新的崩溃记录；最后几行：{tail}"), \
            "如果又出现「突然没了」，先看这个文件的修改时间有没有更新到当时；更新了再把整份发出来"
    when = f"（{_fmt_ts(crash_mt)}）" if crash_mt > 0 else ""
    return WARN, f"crash.log{when} 有 {f.get('crash_bytes')} 字节，最后几行：{tail}", \
        "这就是「突然没了」那一次留下的栈：把整份文件连同这一屏报告一起发出来定位"


def j_mpvlog(f: dict) -> tuple[str, str, str]:
    if not f.get("mpv_log_exists"):
        return WARN, "没有 mpv.log（内核还没跑过，或日志被清了）", "——"
    errs = f.get("mpv_err_lines") or []
    if errs:
        return WARN, f"mpv.log 里有 error/fatal 行：{' ‖ '.join(errs[:2])}", \
            "黑屏/无声/花屏先看这几行；常见是 hwdec 档位与本机驱动不合（改性能档位后重启播放器）"
    return OK, "mpv.log 里没有 error/fatal 行", "——"


def j_lnk(f: dict) -> tuple[str, str, str]:
    if not f.get("lnk_dir"):
        return WARN, "问不到桌面目录", "——"
    lnk = os.path.join(str(f["lnk_dir"]), f"{cfgmod.APP_TITLE}.lnk")
    if f.get("lnk_exists"):
        return OK, f"桌面快捷方式在：{lnk}", "——"
    return WARN, f"桌面上没有 {cfgmod.APP_TITLE}.lnk", \
        "跑一次 `python packaging/make_shortcut.py` 重建（它会写 pythonw -m app.main 的工作目录与图标）"


def j_keymap(f: dict) -> tuple[str, str, str]:
    rows = int(f.get("key_rows") or 0)
    dead = f.get("key_dead") or []
    shadow = f.get("key_shadow") or []
    seen = f"生效键位 {rows} 条"
    if dead:
        return FAIL, f"{seen}，其中 {len(dead)} 条按不响：{dead}", \
            "这些写法 Qt 解析成 Key_UNKNOWN；在设置里重录一遍这几把键"
    if shadow:
        return WARN, f"{seen}，有 {len(shadow)} 组同键撞车：{shadow[:3]}", \
            "撞车时后装的那条生效；把其中一条改成别的键"
    if rows == 0:
        return WARN, "读不到键位表（app.ui.keymap 没起来）", "看看是不是缺了 PyQt5（上面那一项会点名）"
    return OK, seen, "——"


def j_perf(f: dict) -> tuple[str, str, str]:
    p = f.get("perf") or {}
    warn = f.get("perf_warn") or []
    seen = "启动档位：" + ("、".join(f"{k}={v}" for k, v in p.items()) or "读不出")
    if warn:
        return WARN, f"{seen}；提示：{' ‖ '.join(warn[:2])}", \
            "hwdec/gpu-api 是启动期参数：改完要**重启播放器**才生效，不是立刻变"
    return OK, seen, "——"


def j_usage(f: dict) -> tuple[str, str, str]:
    """统计页「资源占用」那四行的两条数据源在不在、跑不跑得通。"""
    why = f.get("usage_psutil_why") or ""
    if why:
        return WARN, f"psutil 不可用：{why}", \
            "内核/外壳那两行会整页写这句话（不是 0%）。装上才有数：`pip install psutil`；播放本身不受影响"
    smi = f.get("usage_smi") or ""
    if not smi:
        return OK, "psutil 在；机器上没有 nvidia-smi（非 N 卡或没装驱动时这是**正常情况**）", \
            "GPU 与时钟那两行会藏起来并在原因位写「没找到 nvidia-smi」；不是坏了，也不值得报警"
    rows = f.get("usage_smi_rows")
    err = f.get("usage_smi_err") or ""
    # 原因行**排在**"没实跑"前面：这两件事同时成立时（采集器哪天改成"跑挂了也把 rows 留 None"），
    # 先判"没实跑"就会把一次失败说成"这轮没问"——那是把红写成灰。当前采集器不会造出这种组合，
    # 但判据的顺序就是为这种组合准备的，别指望调用方永远不出错。
    if err:
        return WARN, f"nvidia-smi 在 {smi}，但这一轮没跑通：{err}", \
            "统计页会把这句原文显示出来。常见是驱动正在更新、或被安全软件拦了一次"
    if rows is None:
        return OK, f"psutil 在；nvidia-smi 在 {smi}（浅检查这一轮没实跑它）", "——"
    if int(rows) <= 0:
        return WARN, f"nvidia-smi 跑通了却没给出一块卡（{smi}）", \
            "那两行会藏起来、不留 0；能贴出这段报告来定位就好判了"
    return OK, f"psutil 在；nvidia-smi 查到 {rows} 块卡（GPU 那两行是**整机**读数，不是本播放器独占）", "——"


# ---------------------------------------------------------------- 外部打开那一路的判据
def _cmd_exe_and_take(cmd: str) -> tuple[str, bool]:
    """注册表里那句命令 → (它指着的可执行文件, 有没有带参数位)。

    参数位只认 `%1` / `%~1` 这两种占位符（它们在 cmd 语义里不等价，但作为"双击时把文件名
    塞进 argv"这一件事都算数）。回 False 就是那句命令长这样：`"…luhui-player.exe"`——
    **双击只开得出一扇空窗口，文件永远进不来**，正是用户报的那个症状在注册表侧的样子。
    """
    s = str(cmd or "").strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        exe = s[1:end] if end > 0 else s[1:]
    else:
        exe = s.split(" ")[0]
    low = s.lower()
    return exe.strip('"').strip(), ("%1" in low) or ("%~1" in low)


def j_openlink(f: dict) -> tuple[str, str, str]:
    """双击那条路走不走得通：注册表指着的启动器在不在、那句话带不带参数位、名单里登记了几种。"""
    c = f.get("external") or {}
    if c.get("gen_err"):
        return WARN, f"读不出注册表键名，这一项没验过：{c['gen_err']}", \
            "跑 `python packaging/make_launchers.py --check`：生成器坏了，注册表和这份自检会一起失去真源"
    if c.get("platform") != "win32":
        return WARN, "非 Windows，问不到注册表", "——"
    if c.get("winreg_err"):
        return WARN, f"问不到注册表，这一项没验过：{c['winreg_err']}", "——"
    if not c.get("exe_exists"):
        return FAIL, f"注册表该指着的启动器不在盘上：{c.get('launch_exe')}", \
            "跑 `python packaging/make_launchers.py` 重编 luhui-player.exe；双击要落的就是它"
    cmd = str(c.get("cmd") or "")
    if c.get("cmd_absent"):
        return WARN, f"启动器在，但「打开方式」里还没登记：ProgID 的 open command 键不存在（{c.get('launch_exe')}）", \
            "双击 `packaging\\reg\\注册到打开方式.reg` 导入；这份自检只读，不替你写注册表"
    if c.get("cmd_env"):
        return WARN, f"读不到那条命令，这一项没验过：{c['cmd_env']}", \
            "读不了不等于没注册：换个有权限的会话再跑一次这份自检"
    exe, has_take = _cmd_exe_and_take(cmd)
    if not has_take:
        return FAIL, f"注册表里那句命令不带文件参数位：{cmd}", \
            "这就是「双击只开一扇空窗口、文件不进来」的注册表侧成因：重跑生成器、再导入一次那份 .reg"
    want = os.path.normpath(os.path.normcase(str(c.get("launch_exe") or "")))
    got = os.path.normpath(os.path.normcase(exe))
    if not c.get("cmd_exe_exists"):
        return FAIL, f"命令指着 {exe}，那个文件不在盘上（本播放器的启动器在 {c.get('launch_exe')}）", \
            "整个文件夹搬过家就会这样：重跑 `python packaging/make_launchers.py` 生成新的 .reg，再导入一次"
    if got != want:
        return WARN, f"命令指的是 {exe}，而这份播放器自己的启动器在 {c.get('launch_exe')}（两份并存）", \
            "双击响的那一份不一定是你现在这份：把旧位置删掉或重导一次 .reg，别让两份互相盖"
    exts_n = int(c.get("ext_count") or 0)
    reg_n = int(c.get("registered") or 0)
    if not exts_n:
        return WARN, "扩展名名单读不出来，这一项没验过（启动器与参数位那两半已核对）", \
            "跑 `python tests/test_launchers.py`：它管生成器从 app/engine/player.py 取名单那一步"
    seen = f"启动器在、命令带参数位（{exe}）；「打开方式」名单已登记 {reg_n}/{exts_n} 种格式"
    if reg_n == 0:
        return WARN, f"启动器与命令都对，但一种格式都没登记进「打开方式」（名单 {exts_n} 种）", \
            "双击 `packaging\\reg\\注册到打开方式.reg`，导完重跑这份自检"
    if reg_n < exts_n:
        return WARN, f"{seen}；没登记的比如 {(c.get('missing') or [])[:4] or '（枚举没给出来）'}", \
            "多半是导入之后 app/engine/player.py 又加过格式：重跑生成器再导一次那份 .reg"
    env = c.get("env") or []
    if env:
        return WARN, f"{seen}；但有 {len(env)} 处读不到：{env[:2]}", "读不到的那些没被验证过，别当成全绿"
    return OK, seen, "——"


def j_defaultapp(f: dict) -> tuple[str, str, str]:
    """双击到底进谁：UserChoice（默认程序的真源）现在写着谁的名字。**只读，不改。**"""
    c = f.get("external") or {}
    rows = c.get("defaults") or {}
    if c.get("platform") != "win32" or c.get("winreg_err"):
        return WARN, "问不到默认程序（非 Windows 或注册表读不了）", "——"
    if not rows:
        return WARN, "一条默认程序都没读到，这一项没验过", \
            f"（读数问题：{(c.get('env') or ['未知'])[:2]}）"
    blind = [e for e, r in rows.items() if r.get("blind")]
    # "是不是我们"这个比较只在 external_facts 里算一次（那儿才知道 PROGID 的真源）。
    # 判据再自己比一遍就是第二处真源：改大小写/去空格的那天，事实与结论会各说一套。
    ours = [e for e, r in rows.items() if r.get("ours")]
    # 名字后面**必须跟着原始 ProgID**：`媒体文件(.mkv)` 这种 FriendlyTypeName 三个播放器
    # 能有两个同名，用户照着念给我们听也定位不到；把值名一起印出来才有用。
    seen = "、".join(f"{e} → {rows[e].get('label') or '（没有记录）'}"
                     f"〔{(rows[e].get('userchoice') or rows[e].get('class_default') or '无')}〕"
                     for e in sorted(rows))
    if blind and len(blind) == len(rows):
        return WARN, f"默认程序全都读不到，这一项没验过：{blind}", \
            "读不到 ≠ 没设：这份自检只读，权限紧或路径异常时它宁可说「没验过」也不替系统圆场"
    if ours and len(ours) == len(rows):
        return OK, f"双击这些类型直接进本播放器：{seen}", "——"
    tail = f"（其中 {', '.join(blind)} 读不到，只算没验过）" if blind else ""
    if ours:
        return WARN, f"只有一半类型进本播放器：{seen}{tail}", \
            "默认程序是你的选择，本程序和这份自检都不改它：设置 → 应用 → 默认应用 → 按文件类型指定"
    return WARN, f"双击现在进的不是本播放器：{seen}{tail}", \
        "这不是坏了——登记进「打开方式」只让本播放器出现在候选列表里，" \
        "「默认用谁打开」住在 UserChoice 里，那格由系统加哈希保护、只能你自己点：" \
        "设置 → 应用 → 默认应用 → 按文件类型，或右键 → 打开方式 → 勾选「始终」"


def j_launchlog(f: dict) -> tuple[str, str, str]:
    """上一次外部打开到底收到了什么。这本账是唯一能回答「当时」的东西——注册表只能证明「现在」。"""
    c = f.get("launchlog") or {}
    if c.get("read_err"):
        return WARN, f"launch.log 读不到：{c['read_err']}", "——"
    if not c.get("exists"):
        return WARN, f"还没有 launch.log（{c.get('path')}）：播放器一次都没启动过", \
            "第一次双击片文件后回来看这一项：它会写清当时收到了几条参数、丢了几条为什么丢"
    last = c.get("last")
    if last is None:
        return WARN, f"launch.log 有 {c.get('rows')} 行，但没有一行带文件参数", \
            "那就是双击/右键这条路**从没把文件名递进来过**：先看上面「外部打开链路」那两项说的是什么"
    seen = (f"最近一次外部打开 {last['time']} 走 {last['kind']}："
            f"argv={last['argv']} kept={last['kept']} dropped={last['dropped']} "
            f"ignored={last['ignored']}")
    trunc = int(c.get("truncated") or 0)
    if int(last.get("kept") or 0) > 0:
        extra = f"；另有 {int(c.get('silent') or 0)} 次一条都没收下" if c.get("silent") else ""
        extra += f"、{trunc} 次半截帧作废" if trunc else ""
        return OK, f"{seen}{extra}", "——"
    if int(last.get("dropped") or 0) > 0:
        return WARN, f"{seen}：一条都没收下，但每一条的丢弃原因都写在那行里", \
            f"把 {c.get('path')} 最后一行贴出来就能定位；窗口的空状态页上也逐条点过名"
    if trunc:
        return WARN, f"{seen}：参数进来了却一条都没收下，账上另有 {trunc} 次半截帧作废", \
            "半截作废是发送方写到一半就断了（崩溃/被杀）；这种情况窗口会自己开，不至于静默"
    return WARN, f"{seen}：参数进来了却一条都没收下，也没记成丢弃", \
        "这类形状通常是选项类参数或非媒体后缀；确认双击的确实是能播的文件，再把最后一行贴出来"



CHECKS = [
    ("解释器/平台", j_python), ("PyQt5", j_pyqt), ("播放内核 mpv", j_mpv),
    ("ffmpeg", j_ffmpeg), ("数据目录可写", j_writable), ("磁盘剩余", j_space),
    ("配置文件", j_config), ("已有实例（转交）", j_instance), ("崩溃记录", j_crash),
    ("内核日志", j_mpvlog), ("桌面快捷方式", j_lnk), ("键位表", j_keymap),
    ("性能档位", j_perf), ("资源占用实测", j_usage),
    # 外部打开那一路排在最后：它读的是注册表与 launch.log，报的是"双击进得来吗、
    # 现在归谁、上次收到了什么"——这三句恰好是用户报"双击没加载"时唯一想知道的。
    ("外部打开链路", j_openlink), ("默认程序现状", j_defaultapp),
    ("上次外部打开的留痕", j_launchlog),
]


# ---------------------------------------------------------------- 证伪
# 每一项：(名字, 判据, 该 OK 的好事实, 该翻红的坏事实…)
# 好样本必须 OK，坏样本必须**不是** OK——反过来判绿的那条就是恒真判据，不许留。
FALSIFY = [
    ("平台不是 win32 必须 FAIL", j_python,
     ({"py": "3.11.9", "platform": "win32", "root": "R"}, OK),
     [({"py": "3.11.9", "platform": "linux", "root": "R"},),
      ({"py": "3.8.13", "platform": "win32", "root": "R"},),
      ({"py": "未知", "platform": "win32", "root": "R"}, WARN)]),
    ("PyQt5 import 断了必须 FAIL", j_pyqt,
     ({"qt_err": "", "qt_ver": "PyQt5 5.15.10 / Qt 5.15.2"}, OK),
     [(  {"qt_err": "ImportError: DLL load failed"},),
      ({"qt_err": "ModuleNotFoundError: No module named 'PyQt5'"},)]),
    ("内核缺失/跑不通各自都要 FAIL", j_mpv,
     ({"mpv_exists": True, "mpv_ver": "mpv 0.38.0", "mpv_run_err": "", "runtime": "R"}, OK),
     [({"mpv_exists": False, "mpv_ver": "", "mpv_run_err": "", "runtime": "R"},),
      ({"mpv_exists": True, "mpv_ver": "", "mpv_run_err": "退出码 1", "runtime": "R"},),
      ({"mpv_exists": True, "mpv_ver": "", "mpv_run_err": "", "runtime": "R"},)]),
    ("ffmpeg 缺席只能 WARN（它不阻断播放）", j_ffmpeg,
     ({"ff_exists": True, "ff_ver": "ffmpeg version 7.1", "ff_run_err": "", "runtime": "R"}, OK),
     [({"ff_exists": False, "ff_ver": "", "ff_run_err": "", "runtime": "R"}, WARN),
      ({"ff_exists": True, "ff_ver": "", "ff_run_err": "退出码 3", "runtime": "R"}, WARN)]),
    ("资源实测：psutil 缺席／跑通没读数／跑不通，三种坏法各自只能 WARN（都不阻断播放）", j_usage,
     ({"usage_psutil_why": "", "usage_smi": "C:/Windows/System32/nvidia-smi.exe",
        "usage_smi_rows": 1, "usage_smi_err": ""}, OK),
     [({"usage_psutil_why": "ModuleNotFoundError: No module named 'psutil'",
        "usage_smi": "C:/Windows/System32/nvidia-smi.exe", "usage_smi_rows": 1,
        "usage_smi_err": ""}, WARN),
      ({"usage_psutil_why": "", "usage_smi": "C:/x/nvidia-smi.exe",
        "usage_smi_rows": 0, "usage_smi_err": ""}, WARN),
      ({"usage_psutil_why": "", "usage_smi": "C:/x/nvidia-smi.exe",
        "usage_smi_rows": None, "usage_smi_err": "退出码 1"}, WARN)]),
    ("资源实测：没有 nvidia-smi 是**正常机器**，不许报警——但 psutil 缺席仍要翻红（别让前一条把它盖住）",
     j_usage,
     ({"usage_psutil_why": "", "usage_smi": "", "usage_smi_rows": None, "usage_smi_err": ""}, OK),
     [({"usage_psutil_why": "没装 psutil", "usage_smi": "",
        "usage_smi_rows": None, "usage_smi_err": ""}, WARN)]),
    ("资源实测：浅检查没实跑（rows=None）与真没读数（rows=0）是两回事", j_usage,
     ({"usage_psutil_why": "", "usage_smi": "C:/x/nvidia-smi.exe",
        "usage_smi_rows": None, "usage_smi_err": ""}, OK),
     [({"usage_psutil_why": "", "usage_smi": "C:/x/nvidia-smi.exe",
        "usage_smi_rows": 0, "usage_smi_err": ""}, WARN)]),
    ("写不进去必须 FAIL（存在≠能写）", j_writable,
     ({"writable": (True, "data、cache")}, OK),
     [({"writable": (False, "data：PermissionError: [Errno 13] 拒绝访问")},)]),
    ("盘满/快满/量不到各有各的红", j_space,
     ({"free_gb": {"D:/data": 40.2, "D:/截图": 30.0}}, OK),
     [({"free_gb": {"D:/data": 0.2}},),
      ({"free_gb": {"D:/data": 1.1}}, WARN),
      ({"free_gb": {"D:/data": 40.2, "D:/截图": -1}}, WARN),
      ({"free_gb": {"D:/截图": -1}}, WARN),
      ({"free_gb": {}}, WARN)]),
    ("坏 JSON / 空键 / 错类型", j_config,
     ({"cfg_raw": '{"volume": 80}', "cfg_path": "P"}, OK),
     [({"cfg_raw": "{volume: }", "cfg_path": "P"},),
      ({"cfg_raw": "[1,2]", "cfg_path": "P"},),
      ({"cfg_raw": "\x00OSError: 被锁住", "cfg_path": "P"},),
      ({"cfg_raw": '{"screenshot_dir": ""}', "cfg_path": "P"}, WARN),
      ({"cfg_raw": '{"volume": null}', "cfg_path": "P"}, WARN)]),
    ("已有实例是 WARN（不是坏了）；非 win32 不判", j_instance,
     ({"pipe": False, "pipe_name": "luhui-player-v1"}, OK),
     [({"pipe": True, "pipe_name": "luhui-player-v1"}, WARN),
      ({"pipe": None, "pipe_name": "x"}, WARN)]),
    ("crash.log 在就必须点名", j_crash,
     ({"crash_exists": False}, OK),
     [({"crash_exists": True, "crash_bytes": 512, "crash_tail": ["Traceback", "ValueError"]}, WARN)]),
    ("crash.log 早于最新源码＝历史记录（OK）；不早于/判不了年龄＝WARN（两条必须成对）", j_crash,
     ({"crash_exists": True, "crash_bytes": 728, "crash_tail": ["RuntimeError"],
       "crash_mtime": 1000.0, "newest_src_mtime": 2000.0, "newest_src_path": "app/x.py"}, OK),
     [({"crash_exists": True, "crash_bytes": 728, "crash_tail": ["RuntimeError"],
        "crash_mtime": 3000.0, "newest_src_mtime": 2000.0, "newest_src_path": "app/x.py"}, WARN),
      ({"crash_exists": True, "crash_bytes": 728, "crash_tail": ["RuntimeError"],
        "crash_mtime": 2000.0, "newest_src_mtime": 2000.0, "newest_src_path": "app/x.py"}, WARN),
      ({"crash_exists": True, "crash_bytes": 728, "crash_tail": ["RuntimeError"],
        "crash_mtime": 0.0, "newest_src_mtime": 2000.0}, WARN),
      ({"crash_exists": True, "crash_bytes": 728, "crash_tail": ["RuntimeError"],
        "crash_mtime": 1000.0}, WARN)]),
    ("mpv.log 里的 error 行必须端出来", j_mpvlog,
     ({"mpv_log_exists": True, "mpv_err_lines": []}, OK),
     [({"mpv_log_exists": True, "mpv_err_lines": ["[vo/gpu] error: no backend"]}, WARN),
      ({"mpv_log_exists": False, "mpv_err_lines": []}, WARN)]),
    ("没有快捷方式要给出重建命令", j_lnk,
     ({"lnk_dir": "D:", "lnk_exists": True}, OK),
     [({"lnk_dir": "D:", "lnk_exists": False}, WARN), ({"lnk_dir": ""}, WARN)]),
    ("死写法 FAIL、撞车 WARN、表读空 WARN", j_keymap,
     ({"key_rows": 54, "key_dead": [], "key_shadow": []}, OK),
     [({"key_rows": 54, "key_dead": ["NumpadEnter"], "key_shadow": []},),
      ({"key_rows": 54, "key_dead": [], "key_shadow": [("F", "F")]}, WARN),
      ({"key_rows": 0, "key_dead": [], "key_shadow": []}, WARN)]),
    ("档位带警告时不许报 OK", j_perf,
     ({"perf": {"hwdec": "auto-safe"}, "perf_warn": []}, OK),
     [({"perf": {"hwdec": "d3d11va"}, "perf_warn": ["gpu-api=vulkan 在本机驱动上回退"]}, WARN)]),
    # 外部打开那一路：这三组是用户报"双击没加载"之后新加的。每条坏样本都**点名期望的级别**，
    # 因为"不是 OK"太宽——把读不到的环境问题一路报成 FAIL 也能骗过它，那正好是这项最坏的假绿。
    ("外部打开链路：命令缺参数位／指错文件必须 FAIL，其余坏法各自只能 WARN", j_openlink,
     ({"external": {"platform": "win32", "gen_err": "", "winreg_err": "", "exe_exists": True,
                    "launch_exe": "C:/r/luhui-player.exe", "progid": "LuHuiPlayer.Media",
                    "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": False, "cmd_env": "",
                    "cmd_exe_exists": True,
                    "ext_count": 48, "registered": 48, "missing": [], "env": []}}, OK),
     [({"external": {"platform": "win32", "exe_exists": True,
                      "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe"', "cmd_absent": False, "cmd_env": "",
                      "cmd_exe_exists": True, "ext_count": 48, "registered": 48},}, FAIL),
      ({"external": {"platform": "win32", "exe_exists": True,
                      "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/搬走前的位置/luhui-player.exe" "%1"', "cmd_absent": False,
                      "cmd_env": "", "cmd_exe_exists": False,
                      "ext_count": 48, "registered": 48},}, FAIL),
      ({"external": {"platform": "win32", "exe_exists": False,
                      "launch_exe": "C:/r/luhui-player.exe", "cmd": "", "cmd_absent": False,
                      "cmd_env": "", "cmd_exe_exists": False,
                      "ext_count": 48, "registered": 48},}, FAIL),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": True, "cmd_env": "",
                      "cmd_exe_exists": True, "ext_count": 48, "registered": 48},}, WARN),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/两份并存/luhui-player.exe" "%1"', "cmd_absent": False,
                      "cmd_env": "", "cmd_exe_exists": True,
                      "ext_count": 48, "registered": 48},}, WARN),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%~1"', "cmd_absent": False,
                      "cmd_env": "", "cmd_exe_exists": True,
                      "ext_count": 48, "registered": 48},}, OK),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": False, "cmd_env": "",
                      "cmd_exe_exists": True, "ext_count": 48, "registered": 0},}, WARN),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": False, "cmd_env": "",
                      "cmd_exe_exists": True, "ext_count": 48, "registered": 40,
                      "missing": [".abc", ".xyz"]},}, WARN),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": False, "cmd_env": "",
                      "cmd_exe_exists": True, "ext_count": 0, "registered": 0},}, WARN),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": False,
                      "cmd_env": "没权限读：[WinError 5]", "cmd_exe_exists": True},}, WARN),
      ({"external": {"platform": "win32", "exe_exists": True, "launch_exe": "C:/r/luhui-player.exe",
                      "cmd": '"C:/r/luhui-player.exe" "%1"', "cmd_absent": False, "cmd_env": "",
                      "cmd_exe_exists": True, "ext_count": 48, "registered": 48,
                      "env": [".mkv：读不了"]},}, WARN),
      ({"external": {"platform": "linux", "gen_err": "", "winreg_err": ""},}, WARN),
      ({"external": {"platform": "win32", "gen_err": "FileNotFoundError: 生成器没了"},}, WARN)]),
    ("默认程序现状：别人占着默认位不许报 OK；读不到只算没验过", j_defaultapp,
     ({"external": {"platform": "win32", "winreg_err": "", "progid": "LuHuiPlayer.Media",
                    "defaults": {".mkv": {"userchoice": "LuHuiPlayer.Media", "label": "LuHui播放器",
                                          "ours": True, "blind": False},
                                  ".mp4": {"userchoice": "luhuiplayer.media", "label": "LuHui播放器",
                                           "ours": True, "blind": False}}}}, OK),
     [({"external": {"platform": "win32", "winreg_err": "", "progid": "LuHuiPlayer.Media",
                      "defaults": {".mkv": {"userchoice": "VODPlayer.mkv", "label": "迅雷影音",
                                             "ours": False, "blind": False},
                                    ".mp4": {"userchoice": "VODPlayer.mp4", "label": "迅雷影音",
                                              "ours": False, "blind": False}}}}, WARN),
      ({"external": {"platform": "win32", "winreg_err": "", "progid": "LuHuiPlayer.Media",
                      "defaults": {".mkv": {"userchoice": "LuHuiPlayer.Media", "label": "LuHui播放器",
                                             "ours": True, "blind": False},
                                    ".mp4": {"userchoice": "AppXqvw0s", "label": "媒体播放器",
                                              "ours": False, "blind": False}}}}, WARN),
      ({"external": {"platform": "win32", "winreg_err": "", "progid": "LuHuiPlayer.Media",
                      "defaults": {".mkv": {"userchoice": "", "label": "", "ours": False,
                                             "blind": True},
                                    ".mp4": {"userchoice": "", "label": "", "ours": False,
                                              "blind": True}}}}, WARN),
      ({"external": {"platform": "win32", "winreg_err": "", "progid": "LuHuiPlayer.Media",
                      "defaults": {".mkv": {"userchoice": "", "label": "", "ours": False,
                                             "blind": True},
                                    ".mp4": {"userchoice": "LuHuiPlayer.Media", "label": "LuHui播放器",
                                              "ours": True, "blind": False}}}}, WARN),
      ({"external": {"platform": "win32", "winreg_err": "", "progid": "LuHuiPlayer.Media",
                      "defaults": {}}}, WARN),
      ({"external": {"platform": "win32", "winreg_err": "winreg 不可用", "progid": "LuHuiPlayer.Media",
                      "defaults": {".mkv": {"userchoice": "LuHuiPlayer.Media", "ours": True}}}}, WARN)]),
    ("上次外部打开的留痕：没有一行带参数时必须翻红（旧写法在这里只会报「日志正常」）", j_launchlog,
     ({"launchlog": {"exists": True, "path": "L", "rows": 12, "read_err": "", "silent": 0,
                     "truncated": 0, "external": 5,
                     "last": {"time": "2026-10-17 01:00:00", "kind": "forward",
                              "argv": 1, "kept": 1, "dropped": 0, "ignored": 0}}}, OK),
     [({"launchlog": {"exists": False, "path": "L", "rows": 0, "read_err": "", "last": None}}, WARN),
      ({"launchlog": {"exists": True, "path": "L", "rows": 7, "read_err": "", "last": None}},),
      ({"launchlog": {"exists": True, "path": "L", "rows": 7, "read_err": "",
                       "last": {"time": "t", "kind": "cold", "argv": 1, "kept": 0,
                                "dropped": 1, "ignored": 0}}}, WARN),
      ({"launchlog": {"exists": True, "path": "L", "rows": 7, "read_err": "",
                       "last": {"time": "t", "kind": "cold", "argv": 2, "kept": 0,
                                "dropped": 0, "ignored": 2}}}, WARN),
      ({"launchlog": {"exists": True, "path": "L", "rows": 7, "read_err": "PermissionError"}}, WARN),
      # 反向哨兵：最近一次收了文件、但历史上有一次半截帧作废——仍算 OK，那句要印出来但不阻断
      ({"launchlog": {"exists": True, "path": "L", "rows": 7, "read_err": "", "silent": 1,
                       "truncated": 2, "last": {"time": "t", "kind": "forward", "argv": 1,
                                                 "kept": 1, "dropped": 0, "ignored": 0}}, }, OK)]),
]


def _judge(fn, facts: dict) -> tuple[str, str, str]:
    """判据自己崩了 ≠ 判出问题：单独标 CRASH，免得 traceback 冒充结论。"""
    try:
        return fn(facts)
    except BaseException as exc:  # noqa: BLE001
        return "CRASH", f"{type(exc).__name__}: {exc}", ""


def falsify() -> int:
    miss = 0
    for name, fn, (good, want_good), bads in FALSIFY:
        lv_g, seen_g, _ = _judge(fn, good)
        ok_g = lv_g == want_good
        rows, crashed = [], []
        for b in bads:
            facts, expect = b[0], (b[1] if len(b) > 1 else None)
            lv_b, seen_b, _n = _judge(fn, facts)
            if lv_b == "CRASH":
                crashed.append(seen_b[:70])
            hit = (lv_b != "OK") if expect is None else (lv_b == expect)
            rows.append(lv_b + ("" if hit else "≠"))
        caught = ok_g and not crashed and all("≠" not in r for r in rows)
        miss += 0 if caught else 1
        print(f"[{'CAUGHT' if caught else 'MISSED'}] {name} —— 好样本={lv_g}（该 {want_good}）"
              f" 坏样本={rows}{' 崩了=' + str(crashed) if crashed else ''}")
    # 计数由这里自己数：手抄的"共 N 条"会在加样本那天变成假账（本项目的老规矩）
    print(f"\n证伪 {len(FALSIFY)} 组 —— CAUGHT {len(FALSIFY) - miss}／MISSED {miss}")
    return 1 if miss else 0


def print_keys() -> int:
    from .ui import keymap
    try:
        cfg = cfgmod.Config()
        rows = keymap.effective(cfg.get("keymap"))
    except BaseException as exc:  # noqa: BLE001
        print(f"读不出键位表：{type(exc).__name__}: {exc}")
        return 1
    width = max((len(r[0]) for r in rows), default=6)
    print(f"生效键位 {len(rows)} 条（改过的以这里为准）：")
    for seq, act, desc in rows:
        mark = "  ← 按不响" if keymap.dead(seq) else ""
        print(f"  {seq:<{width}}  {desc or keymap.desc_of(act)}{mark}")
    return 0


def negative_control() -> int:
    """整份报告的负对照：把 app/ 原样搬到临时目录（那边没有 runtime/），在那儿跑一遍。

    --falsify 逐条喂事实，只能证明**单个判据**不是恒真；这条证明的是"从采集到退出码整条链
    在真缺内核的机器上会翻红"。报告如果哪天变成"永远 0 项阻断"，这条会当场抓住它。
    """
    tmp = tempfile.mkdtemp(prefix="gelu-doctor-neg-")
    try:
        shutil.copytree(os.path.dirname(os.path.abspath(__file__)),
                        os.path.join(tmp, "app"), ignore=shutil.ignore_patterns("__pycache__"))
        env = dict(os.environ, GELU_DATA=os.path.join(tmp, "data"))
        run = subprocess.run([sys.executable, "-m", "app.doctor"], cwd=tmp, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=120)
        out = run.stdout or ""
        rows = [l for l in out.splitlines() if l.startswith("[")]
        fails = [l for l in rows if l.startswith("[FAIL]")]
        warns = [l for l in rows if l.startswith("[WARN]")]
        hit = any("mpv" in l for l in fails)
        print(f"临时目录：{tmp}")
        print(f"[{'CAUGHT' if run.returncode else 'MISSED'}] 退出码非 0（真缺内核时） —— 实际 rc={run.returncode}")
        print(f"[{'CAUGHT' if hit else 'MISSED'}] 内核那条翻红且点名 —— {[l[:44] for l in fails] or '没有 FAIL 行'}")
        print(f"[{'INFO'}] 顺带看到的提醒 —— {[l[:40] for l in warns]}")
        if run.returncode == 0 or not hit:
            print(out)
            print(f"stderr：{(run.stderr or '')[:400]}")
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    # 本机控制台默认 GBK，中文报告直接变乱码（同一个坑在 tests/ 里踩过）：
    # 自己把输出切成 UTF-8，乱码就只可能出现在"用户自己改代码页"这一种情况下。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--falsify" in argv:
        return falsify()
    if "--neg" in argv:
        return negative_control()
    if "--keys" in argv:
        return print_keys()
    print(f"{cfgmod.APP_TITLE} · 故障自检   {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    facts = collect()
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for name, fn in CHECKS:
        level, seen, nxt = _judge(fn, facts)
        if level == "CRASH":   # 判据自己崩了也是阻断：doctor 不许把 traceback 当结论
            level = FAIL
            seen = f"这一项自己崩了（是 doctor 的 bug，请连同命令行一起报回来）：{seen}"
            nxt = "这一条的结论不可信：其余项照常看，这条单独报回来"
        counts[level] += 1
        print(f"{LEVEL_MARK.get(level, UNKNOWN_MARK)} {name:<12} {seen}")
        # "——"就是"没什么下一步"：空提示也印成一行"下一步：——"，看着像真有个动作要做
        if level != OK and nxt and nxt != "——":
            print(f"        下一步：{nxt}")
    print(f"\n结论：{counts[FAIL]} 项阻断、{counts[WARN]} 项提醒、{counts[OK]} 项正常。"
          f"崩溃记录在 {facts['crash_path']}；内核日志在 {facts['mpv_log_path']}。")
    return 1 if counts.get(FAIL) else 0


if __name__ == "__main__":
    raise SystemExit(main())
