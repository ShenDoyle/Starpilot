#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星舵发布打包器：从仓库当前 HEAD 出两个 zip —— skill 全量包 + cli 子集包。

    python tools/release.py                  # 打包到 dist/
    python tools/release.py --verify         # 打包后各自解压、跑全量单测
    python tools/release.py --version 0.1.0  # 校验 SKILL.md 版本，不一致直接拒

包内文件一律取自 `git ls-files`：只含入库文件，`runtime/ runs/ state/ stage/`
这些运行现场天然进不来。zip 内时间戳固定，同样的 HEAD 打出来的包逐字节一致。

纯标准库，Python 3.9+。
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

# 固定时间戳：可复现构建，不受本机时钟影响
STAMP = (2026, 10, 30, 0, 0, 0)

# CLI 包里放什么：功能一个不少，只少了给 agent 看的 SKILL.md 与图标
CLI_PREFIXES = ("scripts/", "references/", "templates/", "tests/")
CLI_FILES = ("LICENSE", "docs/orchestrator-contract.md")
CLI_README = "tools/cli-readme.md"   # 进包后重命名为 README.md

SKIP_EXT = (".pyc", ".pyo")

# 不打包的历史残留：upgrade.py 装 skill 时也从不动 archive/，包里带上会跟装好的目录不一致
SKIP_PREFIX = ("archive/",)


def read_version(root=ROOT):
    p = os.path.join(root, "SKILL.md")
    with open(p, encoding="utf-8") as f:
        for line in f.read(4096).splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def git_tracked(root=ROOT):
    """入库文件清单（相对路径，正斜杠）。git 不可用时退回白名单遍历。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        ).stdout
        files = [f.decode("utf-8") for f in out.split(b"\0") if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "dist", "__pycache__")]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                files.append(rel.replace(os.sep, "/"))
    keep = []
    for f in sorted(files):
        if f.endswith(SKIP_EXT) or f.startswith("dist/") or f.startswith(SKIP_PREFIX):
            continue
        if os.path.exists(os.path.join(root, f)):
            keep.append(f)
    return keep


class _Text(object):
    """包内生成的正文，不对应磁盘上的文件。"""

    def __init__(self, body):
        self.body = body


def cli_subset(files, version):
    picked = [(f, f) for f in files if f.startswith(CLI_PREFIXES) or f in CLI_FILES]
    with open(os.path.join(ROOT, CLI_README), encoding="utf-8") as f:
        readme = f.read()
    # VERSION 由 SKILL.md 的版本号生成，CLI 包靠它自报版本（没有 SKILL.md 可读）
    return picked + [("README.md", _Text(readme)), ("VERSION", _Text(version + "\n"))]


def _put(zf, arcname, src=None, text=None, mode=0o644):
    """写一条 zip 记录：保留可执行位，路径一律正斜杠（Windows 解压才不串目录）。"""
    info = zipfile.ZipInfo(arcname, date_time=STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (mode & 0xFFFF) << 16
    info.create_system = 3  # unix
    if text is not None:
        zf.writestr(info, text.encode("utf-8"))
        return
    with open(src, "rb") as f:
        zf.writestr(info, f.read())


def build(name, version, entries, outdir=DIST):
    """entries: [(相对路径, 源文件绝对路径 or None, 正文 or None)]。"""
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, "%s-%s.zip" % (name, version))
    prefix = "%s-%s/" % (name, version)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, src, text in entries:
            mode = 0o755 if rel.endswith(".py") else 0o644
            _put(zf, prefix + rel, src=src, text=text, mode=mode)
    return dest


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB"):
        n /= 1024.0
        if n < 1024 or unit == "MB":
            return "%.1f %s" % (n, unit)


def verify(zip_path):
    """解压到临时目录跑全量单测。临时目录不主动删：删几百个文件会撞沙箱的文件删除配额。"""
    tmp = tempfile.mkdtemp(prefix="starpilot-verify-")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp)
    root = os.path.join(tmp, os.listdir(tmp)[0])
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-t", "tests"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    text = r.stdout.decode("utf-8", "replace")
    summary = [ln for ln in text.splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
    return r.returncode == 0, summary, root


def main():
    ap = argparse.ArgumentParser(description="打包星舵发布物")
    ap.add_argument("--version", default="", help="期望版本；与 SKILL.md 不一致则拒绝打包")
    ap.add_argument("--out", default=DIST, help="产物目录（默认 dist/）")
    ap.add_argument("--verify", action="store_true", help="解压两个包并跑全量单测")
    args = ap.parse_args()

    version = read_version()
    if not version:
        print("读不到 SKILL.md 的 version，拒绝打包", file=sys.stderr)
        return 2
    if args.version and args.version != version:
        print("版本不一致：SKILL.md 是 %s，要求 %s" % (version, args.version), file=sys.stderr)
        return 2

    tracked = git_tracked()
    if not tracked:
        print("仓库里没有可打包的文件", file=sys.stderr)
        return 2

    skill_entries = [(f, os.path.join(ROOT, f), None) for f in tracked]
    cli_entries = []
    for rel, payload in cli_subset(tracked, version):
        if isinstance(payload, _Text):
            cli_entries.append((rel, None, payload.body))
        else:
            cli_entries.append((rel, os.path.join(ROOT, payload), None))

    built = []
    for name, entries in (("starpilot-skill", skill_entries), ("starpilot-cli", cli_entries)):
        dest = build(name, version, entries, args.out)
        built.append((dest, len(entries)))

    print("版本 %s · %d 个入库文件\n" % (version, len(tracked)))
    print("%-34s %6s %10s %10s" % ("包", "文件", "解压后", "zip"))
    for dest, count in built:
        print("%-34s %6d %10s %10s" % (
            os.path.basename(dest), count,
            human(sum(i.file_size for i in zipfile.ZipFile(dest).infolist())),
            human(os.path.getsize(dest))))

    sums = os.path.join(args.out, "SHA256SUMS.txt")
    lines = ["%s  %s" % (sha256(d), os.path.basename(d)) for d, _ in built]
    with open(sums, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("\nSHA256")
    for ln in lines:
        print("  " + ln)
    print("\n产物目录：%s" % os.path.abspath(args.out))

    if args.verify:
        print("\n自测")
        bad = 0
        for dest, _ in built:
            ok, summary, root = verify(dest)
            print("  %-32s %s  %s" % (os.path.basename(dest), "PASS" if ok else "FAIL", " / ".join(summary)))
            if not ok:
                bad += 1
                print("    解压目录：%s" % root)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
