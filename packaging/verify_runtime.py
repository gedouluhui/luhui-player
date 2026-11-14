"""运行时二进制核对：打包之前按仓库里的 checksums.sha256 算一遍 SHA-256。

为什么要有这一层：mpv/ffmpeg 不进 Git（体积与许可），用户或下一位接手的人是
"自行下载放进来"。少了核对，打包脚本只能信文件名——换来的是"换了个来路不明的
内核、行为全变、却没人知道是哪一步换的"。清单固定版本，打包脚本开打前核对；
换内核必须同时改清单与 README 的版本行，否则 `portable.spec` 直接停下。

用法：
    python packaging/verify_runtime.py                 # 核对本仓库（打包前自动调用）
    python packaging/verify_runtime.py --root <目录> --checksums <清单>
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_CHECKSUMS = os.path.join(HERE, "runtime_checksums.sha256")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_checksums(path: str) -> list[tuple[str, str]]:
    """→ [(sha256, 相对路径)]；格式错误立刻抛 ValueError（不猜、跳过更危险）。"""
    entries: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 64:
                raise ValueError(f"{path}:{lineno} 不是 sha256sum 行：{line!r}")
            try:
                int(parts[0], 16)
            except ValueError:
                raise ValueError(f"{path}:{lineno} 哈希不是十六进制：{parts[0]!r}")
            rel = parts[1].lstrip("*").strip().replace("\\", "/")
            entries.append((parts[0].lower(), rel))
    if not entries:
        raise ValueError(f"{path} 是空清单")
    return entries


def check(root: str, checksums: str | None = None) -> tuple[list[str], int]:
    """→ (问题列表, 核对条数)。问题为空即全部通过。"""
    path = checksums or DEFAULT_CHECKSUMS
    entries = load_checksums(path)
    problems: list[str] = []
    for want, rel in entries:
        rel_os = os.path.normpath(rel)
        if os.path.isabs(rel_os) or rel_os.startswith(".."):
            problems.append(f"非法相对路径：{rel}")
            continue
        target = os.path.join(root, rel_os)
        if not os.path.isfile(target):
            problems.append(f"缺少文件：{rel}")
            continue
        try:
            got = sha256_file(target)
        except OSError as exc:
            problems.append(f"读不了 {rel}：{exc}")
            continue
        if got != want:
            problems.append(f"哈希不符：{rel}（清单 {want[:12]}…，实际 {got[:12]}…）")
    return problems, len(entries)


def verify(root: str, checksums: str | None = None) -> None:
    """打包脚本用：不通过就 SystemExit，绝不让来路不明的内核进便携包。"""
    problems, n = check(root, checksums)
    if problems:
        raise SystemExit("运行时二进制校验失败：\n  " + "\n  ".join(problems) +
                         "\n按 README 的版本行重新下载，或更新 packaging/runtime_checksums.sha256。")
    print(f"[runtime] 校验通过：{n} 个文件的 SHA-256 与清单一致")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 checksums.sha256 核对运行时二进制")
    ap.add_argument("--root", default=ROOT, help="仓库根目录（默认本文件上一级）")
    ap.add_argument("--checksums", default=None, help="清单路径（默认 runtime_checksums.sha256）")
    args = ap.parse_args(argv)
    try:
        problems, n = check(os.path.abspath(args.root), args.checksums)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] 清单读不了：{exc}")
        return 1
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        print(f"[FINAL] 运行时校验：{n - len(problems)}/{n} 通过；红 {len(problems)} 条")
        return 1
    print(f"[PASS] 运行时校验：{n}/{n} 个文件与清单一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
