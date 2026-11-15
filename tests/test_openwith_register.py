"""「打开方式」自登记判据：写得进、读得回、撤得掉，且绝不动别人的条目与默认程序。

用户报的症状是「右键视频 → 打开方式里找不到 LuHui播放器」。根因不在命令怎么写，而在
**整族注册键从没写进 HKCU**：模板 .reg 一直在仓库里，但便携版没有它，也从不自己登记。
所以这里验的是 `app/openwith.py` 那套自登记：

1. `plan()` 是纯函数：三根自己的键 + 每个媒体扩展名一条 OpenWithProgids，命令带 `%1`；
2. 真写进 **HKCU 的临时子树**（`Software\\LuHuiSelfTest-*`），逐条读回对账；
3. 撤销只摘自己那条值、只删自己那三根键：同键里别人的值与扩展名默认值必须原样；
4. 全程不出现 `UserChoice`、不写扩展名键的默认值——登记不等于抢默认程序。

跑法：python tests/test_openwith_register.py
退出码：0 全绿；1 有真失败；2 环境不给（非 Windows / winreg 不可用），不冒充产品红。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

BS = chr(92)
results: list[tuple[str, bool, str]] = []

try:
    import winreg
    WINREG_ERR = ""
except BaseException as exc:                    # noqa: BLE001
    winreg = None
    WINREG_ERR = f"{type(exc).__name__}: {exc}"

try:
    from app import openwith as ow
    from app.engine.player import MEDIA_EXTS
    IMPORT_ERR = ""
except BaseException as exc:                    # noqa: BLE001
    ow = None
    MEDIA_EXTS = set()
    IMPORT_ERR = f"{type(exc).__name__}: {exc}"


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def value_of(base: str, rel: str, name: str | None) -> tuple[object | None, str]:
    """读 HKCU 下一个值；键/值不存在回 (None, "")，读不了回 (None, 原因)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel) as h:
            got, _typ = winreg.QueryValueEx(h, name if name is not None else "")
            return got, ""
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def value_names(base: str, rel: str) -> list[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel) as h:
            out = []
            i = 0
            while True:
                try:
                    out.append(winreg.EnumValue(h, i)[0])
                except OSError:
                    return out
                i += 1
    except FileNotFoundError:
        return []


def key_exists(rel: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def rm_tree(rel: str) -> None:
    """测试自己的临时子树：递归清干净，失败也不许把异常抛到报告之外。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rel, 0, winreg.KEY_ALL_ACCESS) as h:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(h, i))
                except OSError:
                    break
                i += 1
        for sub in subs:
            rm_tree(rel + BS + sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rel)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def main() -> int:
    if sys.platform != "win32" or winreg is None:
        print(f"[ENV ] 非 Windows 或 winreg 不可用：{WINREG_ERR}")
        return 2
    if ow is None:
        check("app/openwith.py 可导入", False, IMPORT_ERR)
        return 1

    tag = f"LuHuiSelfTest-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    base = "Software" + BS + tag
    tmp = tempfile.mkdtemp(prefix="luhui-openwith-")
    exe = os.path.join(tmp, "LuHui播放器.exe")
    with open(exe, "wb") as fh:
        fh.write(b"MZ")
    exts = sorted(MEDIA_EXTS)
    progid_key = BS.join((base, "Classes", ow.PROGID))
    app_key = BS.join((base, "Classes", "Applications", os.path.basename(exe)))
    paths_key = BS.join((base, "Microsoft", "Windows", "CurrentVersion",
                         "App Paths", os.path.basename(exe)))
    try:
        entries = ow.plan(exe, exts=exts, base=base)
        table = {(e.key, e.name): e.value for e in entries}

        check("plan：ProgID 的描述、图标与 open 命令齐全",
              table.get((progid_key, None)) == ow.PROGID
              and table.get((progid_key, "FriendlyTypeName")) == "LuHui播放器"
              and table.get((progid_key, "ApplicationDescription")) == "本地视频播放器（libmpv 内核）"
              and bool(table.get((progid_key + BS + "DefaultIcon", None))),
              f"{progid_key} 下 {len([k for k in table if k[0].startswith(progid_key)])} 条")
        cmd = table.get((progid_key + BS + "shell" + BS + "open" + BS + "command", None), "")
        check("plan：open 命令用引号包住 exe，并带 %1 参数位（不是 %~1）",
              cmd == f'"{os.path.abspath(exe)}" "%1"' and "%~1" not in cmd, cmd)
        check("plan：Applications 键按 exe 文件名登记，FriendlyAppName 与命令齐备",
              table.get((app_key, "FriendlyAppName")) == "LuHui播放器"
              and table.get((app_key + BS + "shell" + BS + "open" + BS + "command", None)) == cmd,
              app_key)
        check("plan：App Paths 的默认值是 exe、Path 是它所在目录",
              table.get((paths_key, None)) == os.path.abspath(exe)
              and table.get((paths_key, "Path")) == os.path.dirname(os.path.abspath(exe)),
              f"{table.get((paths_key, None))}")
        our_ext = {(k, n): v for (k, n), v in table.items()
                   if n == ow.PROGID and (BS + "OpenWithProgids") in k}
        check(f"plan：{len(exts)} 种媒体格式各一条 OpenWithProgids，值为空串",
              len(our_ext) == len(exts)
              and all(v == "" for v in our_ext.values())
              and all(k.startswith(base + BS + "Classes" + BS) for k, _ in our_ext),
              f"实登记 {len(our_ext)} 种")
        check("plan：不碰 UserChoice，也不写扩展名键的默认值",
              all("userchoice" not in k.lower() for k, _ in table)
              and all(n is not None or (BS + "Classes" + BS + ".") not in k
                      or (BS + "OpenWithProgids") in k for k, n in table),
              "只写 ProgID/Applications/App Paths/OpenWithProgids")

        # 预置别人的东西：撤销时必须原样。
        foreign_ext = BS.join((base, "Classes", ".mp4"))
        foreign_ow = foreign_ext + BS + "OpenWithProgids"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, foreign_ext, 0, winreg.KEY_WRITE) as h:
            winreg.SetValueEx(h, "", 0, winreg.REG_SZ, "Someone.Else")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, foreign_ow, 0, winreg.KEY_WRITE) as h:
            winreg.SetValueEx(h, "Other.App", 0, winreg.REG_SZ, "")

        ow.register(exe, exts=exts, base=base, notify=False)
        missing = [(k, n) for (k, n), v in table.items() if value_of(base, k, n)[0] != v]
        check(f"register：{len(table)} 条逐条读回与计划一致", not missing,
              "；".join(f"{k[-36:]}/{n}" for k, n in missing[:4]))
        st = ow.status(exe=exe, exts=exts, base=base)
        check("status：三根键都在、命令指向本 exe、48 种全登记",
              st["progid"] and st["app"] and st["apppaths"] and st["command_ok"]
              and st["registered"] == len(exts) and not st["missing"],
              f"registered={st['registered']}/{len(exts)} command={st.get('command','')[:60]}")

        ow.register(exe, exts=exts, base=base, notify=False)
        st2 = ow.status(exe=exe, exts=exts, base=base)
        check("register 幂等：重复登记后条数不变、别人的值也没被挤掉",
              st2["registered"] == len(exts)
              and value_of(base, foreign_ow, "Other.App")[0] == ""
              and value_of(base, foreign_ext, None)[0] == "Someone.Else",
              f"registered={st2['registered']}")

        ow.unregister(exe, exts=exts, base=base, notify=False)
        gone = [k for k in (progid_key, app_key, paths_key) if key_exists(k)]
        check("unregister：自己那三根键整棵删掉", not gone, "；".join(gone))
        left = [e for e in exts if ow.PROGID in value_names(base, BS.join((base, "Classes", e, "OpenWithProgids")))]
        check("unregister：每种格式里我们那条值都摘掉了", not left, "；".join(left[:6]))
        check("unregister：同键里别人的值与扩展名默认值原样保留",
              value_of(base, foreign_ext, None)[0] == "Someone.Else"
              and value_of(base, foreign_ow, "Other.App")[0] == ""
              and key_exists(foreign_ext),
              "只摘自己添的那一条")

        pick = ow.pick_action
        check("命令行开关：只认自己的三个旗标，普通文件参数仍归播放器",
              pick(["D:" + BS + "某片.mkv"]) is None
              and pick(["--register-openwith"]) == "register"
              and pick(["--unregister-openwith"]) == "unregister"
              and pick(["--openwith-status"]) == "status"
              and pick(["--register-openwith", "x.mkv"]) == "register",
              "未知参数不会被误当登记")

        fake_launcher = os.path.join(tmp, "luhui-player.exe")
        with open(fake_launcher, "wb") as fh:
            fh.write(b"MZ")
        absent = os.path.join(tmp, "no-such", "luhui-player.exe")
        check("目标 exe：打包版用自己；源码版优先仓库启动器；启动器缺失时明确报错",
              ow.choose_target(frozen=True, sys_exe=exe, launcher=absent) == os.path.abspath(exe)
              and ow.choose_target(frozen=False, sys_exe=sys.executable, launcher=fake_launcher)
              == os.path.abspath(fake_launcher)
              and _raises(FileNotFoundError,
                          lambda: ow.choose_target(frozen=False, sys_exe=sys.executable,
                                                   launcher=absent)),
              "不存在就报错，绝不把 python.exe 写成播放方式")
    finally:
        rm_tree(base)
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in results if not r[1]]
    print(f"\n---- 打开方式自登记：{len(results) - len(bad)}/{len(results)} 通过 ----", flush=True)
    return 0 if not bad else 1


def _raises(kind, fn) -> bool:
    try:
        fn()
    except kind:
        return True
    except BaseException:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
