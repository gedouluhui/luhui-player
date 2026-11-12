# -*- mode: python ; coding: utf-8 -*-
"""onedir 便携版：exe 旁边留 _internal/（运行时与只读资源），数据写在 exe 同级。

为什么是 onedir 而不是 onefile：onefile 每次启动都要把 250MB 内核解压到临时目录，
首播要多等数秒，杀软也会对每次自解压重新扫描。用户要的是"打开文件夹双击 exe"，
onedir 正好是这个形状，且升级时只需换掉 exe 与 _internal。
"""
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
NAME = "LuHui播放器"

assets = os.path.join(ROOT, "packaging", "assets")
runtime = os.path.join(ROOT, "runtime")

# (源, _MEIPASS 内的相对目录)。app/config.py 的 BUNDLE_DIR 与 theme._bundle_base()
# 都指向 _MEIPASS，所以这里的落点必须与它们拼出的路径一一对应。
DATA_MAP = [
    (os.path.join(assets, "ui"), "packaging/assets/ui"),
    (os.path.join(assets, "fonts"), "packaging/assets/fonts"),
    (os.path.join(assets, "app.ico"), "packaging/assets"),
    (os.path.join(assets, "app-256.png"), "packaging/assets"),
    (os.path.join(runtime, "mpv", "mpv.exe"), "runtime/mpv"),
    (os.path.join(runtime, "mpv", "mpv", "fonts.conf"), "runtime/mpv/mpv"),
    (os.path.join(runtime, "ffmpeg", "ffmpeg.exe"), "runtime/ffmpeg"),
]

for src, _dest in DATA_MAP:
    if not os.path.exists(src):
        raise SystemExit(f"缺少打包资源：{src}")

datas = DATA_MAP

a = Analysis(
    [os.path.join(ROOT, "packaging", "portable_entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "IPython",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(assets, "app.ico"),
    version=os.path.join(ROOT, "packaging", "portable_version.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
