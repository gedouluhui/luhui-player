# -*- coding: utf-8 -*-
"""发布前防泄漏门禁：扫描仓库里的个人绝对路径（审计“发布前自检门禁化”）。

只抓"会跟着仓库/Release 发出去的东西"：有 git 时只扫 `git ls-files`，没有 git
（判据里的临时目录）才退回整树遍历；二进制/无法按 UTF-8 解码的文件跳过——
它们在仓库里本来就不该承载可读路径，硬扫只会制造假红。

用法：
    python tests/ci_leak_scan.py            # 扫本仓库，命中返回 1
    python tests/ci_leak_scan.py --root D  # 扫指定目录（判据用）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

# 拼出来而不是写字面量：扫描器自己也会被扫，源码里留一条完整样例就会自报家门。
_USER_HOME = re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"'<>|:*?]+")

SKIP_DIRS = {".git", "dist", "build", "data", "runtime", "截图", "__pycache__",
             ".qoder-credits", "node_modules", ".venv", "venv", "tests\\out",
             "tests\\logs"}
SKIP_EXT = {".exe", ".dll", ".pyd", ".so", ".dylib", ".pyc", ".pyo", ".bin",
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
            ".mp4", ".mkv", ".mp3", ".wav", ".avi", ".mov", ".flac", ".webm",
            ".zip", ".7z", ".gz", ".rar", ".pdf", ".ttf", ".otf", ".woff", ".woff2"}


def _git_files(root: str) -> list[str] | None:
    try:
        p = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    out = p.stdout.decode("utf-8", "replace")
    return [rel for rel in out.split("\0") if rel]


def _walk_files(root: str):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns
                  if d not in SKIP_DIRS
                  and not d.startswith("app.backup-")
                  and not d.startswith(".selftest")]
        for fn in fns:
            yield os.path.relpath(os.path.join(dp, fn), root)


def _iter(root: str):
    tracked = _git_files(root) if os.path.isdir(os.path.join(root, ".git")) else None
    return tracked if tracked is not None else _walk_files(root)


def scan(root: str) -> tuple[int, list[str]]:
    root = os.path.abspath(root)
    hits: list[str] = []
    scanned = 0
    for rel in _iter(root):
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        path = os.path.join(root, rel)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            m = _USER_HOME.search(line)
            if m:
                hits.append(f"{rel}:{lineno}: {m.group(0)}")
    return scanned, hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描仓库中的个人绝对路径")
    ap.add_argument("--root", default=ROOT, help="要扫描的目录（默认仓库根）")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"[FAIL] 目录不存在：{args.root}")
        return 2
    scanned, hits = scan(args.root)
    for h in hits:
        print(f"[FAIL] 个人绝对路径：{h}")
    if hits:
        print(f"[FINAL] 防泄漏扫描：扫了 {scanned} 个文本文件，命中 {len(hits)} 条")
        return 1
    print(f"[PASS] 防泄漏扫描：{scanned} 个文本文件，未发现个人绝对路径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
