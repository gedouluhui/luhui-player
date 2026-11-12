"""在桌面建「LuHui播放器」快捷方式（可重复跑：已存在就原地更新）。

指向 pythonw.exe -m app.main，工作目录=项目根，图标=packaging/assets/app.ico。
用 pythonw 而不是 python 是为了不弹黑色控制台窗口。

附带好处：把视频文件**拖到图标上**就等于"用播放器打开这个文件"——Windows 会把
文件路径追加为参数，正好走 app.main 的命令行入口（多实例会自动转交给已开实例）。

用法：python packaging/make_shortcut.py [--dest 目录]
"""
from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from app.config import APP_TITLE  # noqa: E402

ICON = os.path.join(HERE, "assets", "app.ico")
LNK_NAME = f"{APP_TITLE}.lnk"


def launcher() -> list[str]:
    """返回 [解释器路径, 参数串]——优先 pythonw（不弹控制台），没有才退回 python。"""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return [exe, "-m app.main"]


def desktop_dir() -> str:
    try:
        from win32com.shell import shell, shellcon
        return shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOP, 0, True)
    except Exception:
        # 没有 pywin32 时退回注册表里的用户桌面（OneDrive 重定向过的那台机器上
        # expanduser('~/Desktop') 会指到不存在的目录，所以这里读注册表）
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                raw, _ = winreg.QueryValueEx(k, "Desktop")
                return os.path.expandvars(raw)
        except Exception:
            return os.path.join(os.path.expanduser("~"), "Desktop")


def retire_old_links(dest_dir: str, sh, exe: str, args: str) -> list[str]:
    """清掉**本项目**留下的旧名字快捷方式（改名后桌面上会同时躺两个图标）。

    删除判据是"三条都指向这台机器的这个项目"：目标解释器 + 参数 + 工作目录全等，
    且文件名不是当前该建的那个。只按文件名猜就动手会误删用户自己的链接，所以
    判据宁可窄：任何一条对不上就留着不动，只报告。
    """
    gone = []
    try:
        cands = [f for f in os.listdir(dest_dir) if f.lower().endswith(".lnk")]
    except OSError:
        return gone
    for f in cands:
        full = os.path.join(dest_dir, f)
        if os.path.normcase(full) == os.path.normcase(os.path.join(dest_dir, LNK_NAME)):
            continue
        try:
            chk = sh.CreateShortcut(full)
            same = (str(chk.TargetPath).lower() == exe.lower()
                    and str(chk.Arguments).strip() == args.strip()
                    and str(chk.WorkingDirectory).lower() == ROOT.lower())
        except Exception:
            same = False
        if same:
            try:
                os.remove(full)
                gone.append(f)
            except OSError as exc:
                print(f"[warn] 旧快捷方式删不掉：{f}（{exc}）——请手动删除")
    return gone


def make(dest_dir: str) -> int:
    exe, args = launcher()
    if not os.path.exists(exe):
        print(f"[FAIL] 找不到启动用的解释器：{exe}")
        return 1
    if not os.path.exists(ICON):
        print(f"[warn] 没有 {ICON}，先跑 python packaging/make_assets.py（这次用默认图标）")
    os.makedirs(dest_dir, exist_ok=True)
    lnk = os.path.join(dest_dir, LNK_NAME)
    try:
        from win32com.client import Dispatch
    except Exception as exc:
        print(f"[FAIL] 需要 pywin32 才能建 .lnk（win32com.client）：{exc}")
        print(f"       手工替代：在桌面新建快捷方式，目标填 \"{exe}\" {args} ")
        return 1
    sh = Dispatch("WScript.Shell")
    for old in retire_old_links(dest_dir, sh, exe, args):
        print(f"[ok] 已移除旧名字的快捷方式：{old}")
    sc = sh.CreateShortcut(lnk)
    sc.TargetPath = exe
    sc.Arguments = args
    sc.WorkingDirectory = ROOT
    sc.Description = f"{APP_TITLE}（libmpv 内核的本地播放器）"
    if os.path.exists(ICON):
        sc.IconLocation = f"{ICON},0"
    sc.Save()
    ok = os.path.exists(lnk) and os.path.getsize(lnk) > 0
    print(f"{'[ok]' if ok else '[FAIL]'} {lnk}")
    if not ok:
        return 1

    # 回读校验：建完立刻按 .lnk 里真存的东西核对，不拿"我刚才传了什么"当结论
    chk = sh.CreateShortcut(lnk)
    fields = {"TargetPath": str(chk.TargetPath), "Arguments": str(chk.Arguments),
              "WorkingDirectory": str(chk.WorkingDirectory), "IconLocation": str(chk.IconLocation)}
    bad = []
    if fields["TargetPath"].lower() != exe.lower():
        bad.append(f"目标={fields['TargetPath']}")
    if fields["Arguments"].strip() != args.strip():
        bad.append(f"参数={fields['Arguments']!r}")
    if fields["WorkingDirectory"].lower() != ROOT.lower():
        bad.append(f"工作目录={fields['WorkingDirectory']}")
    if os.path.exists(ICON) and ICON.lower() not in fields["IconLocation"].lower():
        bad.append(f"图标={fields['IconLocation']}")
    for k, v in fields.items():
        print(f"     {k} = {v}")
    print(f"{'[ok] 回读一致' if not bad else '[FAIL] 回读不一致：' + '; '.join(bad)}")
    return 0 if not bad else 1


def main() -> int:
    dest = desktop_dir()
    if "--dest" in sys.argv:
        dest = sys.argv[sys.argv.index("--dest") + 1]
    print(f"桌面目录：{dest}")
    return make(dest)


if __name__ == "__main__":
    raise SystemExit(main())
