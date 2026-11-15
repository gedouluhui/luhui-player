"""HKCU「打开方式」自登记：只写自己添的那几根键，绝不抢默认程序。

为什么要有这一层：`packaging/reg/注册到打开方式.reg` 一直是**要人手动导入**的模板，而便携版
里既没有它、程序也从不自己登记。结果是右键视频 →「打开方式」里根本没有这个播放器——用户
报的那句话，在注册表侧就是"整族键不存在"。模板依赖仓库绝对路径，文件夹一搬家就失效；
这里改成由**正在运行的这一份 exe** 自己登记自己，搬到哪儿、重新双击一次就修好。

四条边界，全部由 tests/test_openwith_register.py 逐条盯着：
1. 只碰 HKCU：ProgID、`Applications\\<exe>`、`App Paths\\<exe>`，以及每个媒体扩展名
   `OpenWithProgids` 里**我们自己那一条值**；
2. 不碰 `UserChoice`，也不写扩展名键的默认值——登记只让播放器出现在候选列表里，
   "默认用谁打开"是用户自己的决定（UserChoice 还有系统哈希保护）；
3. 撤销只摘自己添的：不整根删 `OpenWithProgids`，别人在同一键里的条目原样保留；
4. 写完调一次 `SHChangeNotify`，让「打开方式」对话框当场刷新，不用等资源管理器重启。
"""
from __future__ import annotations

import ctypes
import os
import sys
import winreg
from dataclasses import dataclass
from typing import Iterable

from . import config as cfgmod
from .engine.player import MEDIA_EXTS

BS = chr(92)
PROGID = "LuHuiPlayer.Media"
APP_TITLE = cfgmod.APP_TITLE
SOURCE_LAUNCH_NAME = "luhui-player.exe"
PORTABLE_NAME = "LuHui播放器.exe"
DESCRIPTION = "本地视频播放器（libmpv 内核）"
HKCU = winreg.HKEY_CURRENT_USER
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000


@dataclass(frozen=True)
class Entry:
    key: str               # HKCU 下的相对键路径
    name: str | None       # None = 键的默认值
    value: str


def _base(base: str | None) -> str:
    return (base or "Software").strip(BS)


def _exts(exts: Iterable[str] | None) -> list[str]:
    out = list(MEDIA_EXTS if exts is None else exts)
    return sorted({e if e.startswith(".") else "." + e for e in out}, key=str.lower)


def plan(exe: str, exts: Iterable[str] | None = None, base: str | None = "Software",
         icon: str | None = None) -> list[Entry]:
    """登记计划的**纯函数**：不读也不写注册表，判据可以直接逐条对账。"""
    exe = os.path.abspath(exe)
    root = _base(base)
    name = os.path.basename(exe)
    progid_key = BS.join((root, "Classes", PROGID))
    app_key = BS.join((root, "Classes", "Applications", name))
    paths_key = BS.join((root, "Microsoft", "Windows", "CurrentVersion", "App Paths", name))
    if icon is None:
        ico = os.path.join(cfgmod.ASSETS_DIR, "app.ico")
        icon = f'"{ico if os.path.isfile(ico) else exe}",0'
    command = f'"{exe}" "%1"'
    rows = [
        Entry(progid_key, None, PROGID),
        Entry(progid_key, "FriendlyTypeName", APP_TITLE),
        Entry(progid_key, "ApplicationDescription", DESCRIPTION),
        Entry(progid_key + BS + "DefaultIcon", None, icon),
        Entry(progid_key + BS + "shell" + BS + "open" + BS + "command", None, command),
        Entry(app_key, "FriendlyAppName", APP_TITLE),
        Entry(app_key + BS + "DefaultIcon", None, icon),
        Entry(app_key + BS + "shell" + BS + "open" + BS + "command", None, command),
        Entry(paths_key, None, exe),
        Entry(paths_key, "Path", os.path.dirname(exe)),
    ]
    for ext in _exts(exts):
        rows.append(Entry(BS.join((root, "Classes", ext, "OpenWithProgids")), PROGID, ""))
    return rows


def register(exe: str, exts: Iterable[str] | None = None, base: str | None = "Software",
             notify: bool = True) -> list[Entry]:
    exe = os.path.abspath(exe)
    if not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    rows = plan(exe, exts=exts, base=base)
    for e in rows:
        with winreg.CreateKeyEx(HKCU, e.key, 0, winreg.KEY_SET_VALUE) as h:
            winreg.SetValueEx(h, e.name or "", 0, winreg.REG_SZ, e.value)
    if notify:
        _notify_shell()
    return rows


def unregister(exe: str, exts: Iterable[str] | None = None, base: str | None = "Software",
               notify: bool = True) -> int:
    """只撤自己添的：三根键整棵删；扩展名里只 DeleteValue 我们自己那一条。"""
    exe = os.path.abspath(exe)
    root = _base(base)
    name = os.path.basename(exe)
    for rel in (BS.join((root, "Classes", PROGID)),
                BS.join((root, "Classes", "Applications", name)),
                BS.join((root, "Microsoft", "Windows", "CurrentVersion", "App Paths", name))):
        _delete_tree(rel)
    dropped = 0
    for ext in _exts(exts):
        key = BS.join((root, "Classes", ext, "OpenWithProgids"))
        try:
            with winreg.OpenKey(HKCU, key, 0, winreg.KEY_SET_VALUE) as h:
                winreg.DeleteValue(h, PROGID)
                dropped += 1
        except FileNotFoundError:
            continue
        except OSError:
            continue
    if notify:
        _notify_shell()
    return dropped


def status(exe: str | None = None, exts: Iterable[str] | None = None,
           base: str | None = "Software") -> dict:
    """只读体检：三根键在不在、命令指向谁、每种格式登记了没。"""
    exe = os.path.abspath(exe or target_exe())
    root = _base(base)
    name = os.path.basename(exe)
    progid_key = BS.join((root, "Classes", PROGID))
    app_key = BS.join((root, "Classes", "Applications", name))
    paths_key = BS.join((root, "Microsoft", "Windows", "CurrentVersion", "App Paths", name))
    command = _value(progid_key + BS + "shell" + BS + "open" + BS + "command")
    registered = 0
    missing = []
    for ext in _exts(exts):
        names = _value_names(BS.join((root, "Classes", ext, "OpenWithProgids")))
        if PROGID in names:
            registered += 1
        else:
            missing.append(ext)
    return {
        "exe": exe,
        "progid": _key_exists(progid_key),
        "app": _key_exists(app_key),
        "apppaths": _key_exists(paths_key),
        "command": command,
        "command_ok": (_norm(_cmd_exe(command)) == _norm(exe)
                       and "%1" in command and os.path.isfile(exe)),
        "registered": registered,
        "missing": missing,
    }


def target_exe() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return choose_target(frozen=cfgmod.FROZEN, sys_exe=sys.executable,
                         launcher=os.path.join(here, SOURCE_LAUNCH_NAME))


def choose_target(frozen: bool, sys_exe: str, launcher: str) -> str:
    """打包版登记自己；源码版登记仓库里那个无黑窗启动器。

    源码版找不到启动器时**必须报错**：退回 `python.exe` 会把「打开方式」写成一条
    "用 Python 打开视频"的坏命令——点了不会播，反而更难排查。
    """
    if frozen:
        return os.path.abspath(sys_exe)
    path = os.path.abspath(launcher)
    if os.path.isfile(path):
        return path
    raise FileNotFoundError(
        f"源码版启动器不在：{path}；先跑 `python packaging/make_launchers.py` 生成它")


def pick_action(argv: Iterable[str]) -> str | None:
    for arg in argv or ():
        if arg == "--register-openwith":
            return "register"
        if arg == "--unregister-openwith":
            return "unregister"
        if arg == "--openwith-status":
            return "status"
    return None


def cli(argv: Iterable[str]) -> int | None:
    """app.main 的最前置开关；不归它管就回 None，让正常播放流程继续。"""
    action = pick_action(argv)
    if action is None:
        return None
    try:
        exe = target_exe()
        if action == "register":
            rows = register(exe)
            n = sum(1 for e in rows if e.name == PROGID)
            _say(f"[OK] 已登记到「打开方式」：{exe}")
            _say(f"     {n} 种媒体格式；右键视频 → 打开方式 里会看到 {APP_TITLE}。")
            _say("     没有改动默认打开方式；要撤回请运行 --unregister-openwith。")
        elif action == "unregister":
            n = unregister(exe)
            _say(f"[OK] 已撤销「打开方式」登记：{exe}（摘掉 {n} 种格式里的候选值）")
            _say("     默认打开方式从未被改动，所以这里也不需要恢复。")
        else:
            st = status(exe=exe)
            _say(f"exe: {st['exe']}")
            _say(f"ProgID/Applications/App Paths: {st['progid']}/{st['app']}/{st['apppaths']}")
            _say(f"open 命令: {st['command'] or '（不存在）'}")
            _say(f"登记格式: {st['registered']}；未登记: {len(st['missing'])}")
            return 0 if (st["progid"] and st["command_ok"] and not st["missing"]) else 1
        return 0
    except BaseException as exc:              # noqa: BLE001 - 这一层只负责把原因说出来
        _say(f"[FAIL] 打开方式登记没有完成：{type(exc).__name__}: {exc}")
        return 1


def _say(text: str) -> None:
    """GUI 版（pythonw / 打包 exe）没有控制台，print 到 None 会抛异常；这里安静降级。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                print(text, file=stream, flush=True)
                return
            except (OSError, ValueError):
                continue


def _notify_shell() -> None:
    try:
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except BaseException:                     # noqa: BLE001 - 刷新失败不影响登记结果
        pass


def _value(rel: str, name: str = "") -> str:
    try:
        with winreg.OpenKey(HKCU, rel) as h:
            got, _typ = winreg.QueryValueEx(h, name)
            return str(got)
    except OSError:
        return ""


def _value_names(rel: str) -> list[str]:
    try:
        with winreg.OpenKey(HKCU, rel) as h:
            out = []
            i = 0
            while True:
                try:
                    out.append(winreg.EnumValue(h, i)[0])
                except OSError:
                    return out
                i += 1
    except OSError:
        return []


def _key_exists(rel: str) -> bool:
    try:
        with winreg.OpenKey(HKCU, rel):
            return True
    except OSError:
        return False


def _delete_tree(rel: str) -> None:
    try:
        with winreg.OpenKey(HKCU, rel, 0, winreg.KEY_ALL_ACCESS) as h:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(h, i))
                except OSError:
                    break
                i += 1
        for sub in subs:
            _delete_tree(rel + BS + sub)
        winreg.DeleteKey(HKCU, rel)
    except OSError:
        return


def _cmd_exe(command: str) -> str:
    s = (command or "").strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        return (s[1:end] if end > 0 else s[1:]).strip()
    return s.split(" ")[0].strip()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip(BS)


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
