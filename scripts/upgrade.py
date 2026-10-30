#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星舵升级器：从 GitHub 拉最新版覆盖安装到 skill 目录。

用户说「升到最新版」时，agent 直接跑这个脚本即可，不需要 git、不需要梯子：

    python scripts/upgrade.py --check        # 只看有没有新版
    python scripts/upgrade.py --to-latest    # 升到最新版

网络策略（国内常态：raw.githubusercontent / codeload 直连不通）：
    并发探测「直连 + 一批公益加速前缀」，直连可用就直连，否则取最快的可用镜像；
    下载失败自动换下一个候选，不会死在一个源上。
    --mirror direct 强制直连；--mirror https://ghfast.top 指定前缀。

**版本号以安装包内的 SKILL.md 为准**：raw.githubusercontent 的 CDN 缓存会返回旧内容
（实测：仓库已是 v2.3.0，raw 仍回 v2.2.0），所以 raw 只用来测活，不用来判版本。

纯标准库，Python 3.9+。
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

REPO = "ShenDoyle/Starpilot"
REPO_URL = "https://github.com/%s" % REPO
BRANCH = "main"

# 公益加速前缀：把 https://github.com/... 或 https://raw.githubusercontent.com/...
# 拼在前缀后面即可。全部失败时回归直连，绝不静默「假成功」。
MIRRORS = (
    "https://ghfast.top",
    "https://gh-proxy.com",
    "https://ghproxy.net",
    "https://hub.gitmirror.com",
    "https://ghproxy.cc",
)

UA = {"User-Agent": "starpilot-upgrade/1.1 (+%s)" % REPO_URL}

# 升级时不覆盖的目录/文件：全是「用户自己的运行数据」，覆盖等于毁现场
KEEP_LOCAL = {".git", "runtime", "runs", "state", "stage", "archive", "__pycache__",
              ".wa-state.json", ".starpilot-manifest.json"}

MANIFEST = ".starpilot-manifest.json"


# ---------------------------------------------------------------- 版本


def parse_version(text: str) -> tuple:
    """从 SKILL.md 的 front-matter 里取 version，返回 (2, 3, 0)。取不到返回 ()。"""
    m = re.search(r'^version:\s*"?([0-9][0-9.]*)"?\s*$', text or "", re.M)
    if not m:
        return ()
    try:
        return tuple(int(x) for x in m.group(1).strip(".").split(".") if x != "")
    except ValueError:
        return ()


def local_version(root: str) -> tuple:
    p = os.path.join(root, "SKILL.md")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            v = parse_version(f.read(4096))
        if v:
            return v
    # CLI 包没有 SKILL.md，版本写在打包时生成的 VERSION 里（内容仍是 SKILL.md 的那一个版本号）
    vf = os.path.join(root, "VERSION")
    if os.path.exists(vf):
        with open(vf, encoding="utf-8") as f:
            return parse_version("version: %s" % f.read(64).strip())
    return ()


def fmt(v: tuple) -> str:
    return ".".join(str(x) for x in v) or "(未知)"


# ---------------------------------------------------------------- 网络


def raw_url(path: str, mirror: str = "") -> str:
    base = "https://raw.githubusercontent.com/%s/%s/%s" % (REPO, BRANCH, path)
    return (mirror.rstrip("/") + "/" + base) if mirror else base


def tarball_url(mirror: str = "") -> str:
    if mirror:
        return mirror.rstrip("/") + "/https://github.com/%s/archive/refs/heads/%s.tar.gz" % (REPO, BRANCH)
    return "https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (REPO, BRANCH)


def http_get(url: str, timeout: float = 8.0, limit: int = 0) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit) if limit else r.read()


def probe(mirror: str, timeout: float, path: str = "SKILL.md") -> dict:
    """测一个源通不通（读一小段 SKILL.md）。返回 {ok, mirror, ms, err}。

    注意：这里读到的版本**不可信**（CDN 缓存会回旧内容），只用它判断「能不能连上」。
    """
    t0 = time.time()
    try:
        body = http_get(raw_url(path, mirror), timeout=timeout, limit=4096)
        text = body.decode("utf-8", "replace")
        if "name: starpilot" not in text:
            return {"ok": False, "mirror": mirror, "err": "内容不像 SKILL.md"}
        return {"ok": True, "mirror": mirror, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # 网络异常一律视为不可用，交给下一个源
        return {"ok": False, "mirror": mirror, "err": "%s: %s" % (type(e).__name__, e)}


def candidate_list(mode: str = "auto", timeout: float = 6.0) -> list:
    """按优先级排好的源列表。空串 = 直连。

    auto：并发探测全部候选；直连可用则排第一（通常最快），其余按延迟升序。
    direct：只用直连，不通就抛错（不静默降级）。
    <前缀>：只用指定镜像。
    """
    if mode == "direct":
        r = probe("", timeout)
        if not r["ok"]:
            raise RuntimeError("直连不可用（%s）。去掉 --mirror direct 让脚本自动切加速镜像。"
                               % r.get("err"))
        return [""]
    if mode not in ("auto", "", None):
        r = probe(mode, timeout)
        if not r["ok"]:
            raise RuntimeError("指定的镜像不可用：%s（%s）" % (mode, r.get("err")))
        return [mode]

    candidates = [""] + list(MIRRORS)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates)) as ex:
        futs = [ex.submit(probe, m, timeout) for m in candidates]
        for f in concurrent.futures.as_completed(futs):
            results.append(f.result())
    ok = sorted([r for r in results if r["ok"]], key=lambda r: r.get("ms", 10 ** 6))
    if not ok:
        raise RuntimeError("直连与全部 %d 个加速镜像都不可用；请检查网络，"
                           "或用 --mirror <前缀> 指定可用镜像。" % len(MIRRORS))
    direct = [r for r in ok if r["mirror"] == ""]
    ordered = ([direct[0]] if direct else []) + [r for r in ok if r["mirror"] != ""]
    return [r["mirror"] for r in ordered]


# ---------------------------------------------------------------- 安装


def _walk_files(root: str):
    """源树里的待装文件（相对路径），跳过用户运行数据。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in KEEP_LOCAL]
        for fn in filenames:
            if fn in KEEP_LOCAL or fn.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def read_manifest(dst: str) -> list:
    p = os.path.join(dst, MANIFEST)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return list(json.load(f).get("files") or [])
    except Exception:
        return []


def install(src: str, dst: str, prune: bool = True) -> dict:
    """把 src 覆盖安装到 dst。先整目录备份，再逐文件覆盖。

    prune 只删「上一次安装清单里有、这一次没有」的文件，绝不碰用户数据；
    清单不存在（首次装）时不删任何东西。
    """
    os.makedirs(dst, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = ""
    if os.listdir(dst):
        backup = "%s.bak-%s" % (dst.rstrip("\\/"), stamp)
        n = 1
        while os.path.exists(backup):          # 同一秒连装两次也不撞名
            n += 1
            backup = "%s.bak-%s-%d" % (dst.rstrip("\\/"), stamp, n)
        shutil.copytree(dst, backup)
    old = set(read_manifest(dst))
    files = _walk_files(src)
    for rel in files:
        s = os.path.join(src, rel)
        d = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
        shutil.copy2(s, d)
    removed = []
    if prune:
        for rel in sorted(old - set(files)):
            p = os.path.join(dst, rel)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    removed.append(rel)
                except OSError:
                    pass
    with open(os.path.join(dst, MANIFEST), "w", encoding="utf-8") as f:
        json.dump({"repo": REPO_URL, "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "version": fmt(local_version(src)), "files": files}, f,
                  ensure_ascii=False, indent=1)
    return {"backup": backup, "written": len(files), "removed": removed}


def fetch_tarball(dest_dir: str, mirror: str = "", timeout: float = 60.0) -> str:
    url = tarball_url(mirror)
    blob = http_get(url, timeout=timeout)
    pkg = os.path.join(dest_dir, "starpilot.tar.gz")
    with open(pkg, "wb") as f:
        f.write(blob)
    with tarfile.open(pkg, "r:gz") as tf:
        root = os.path.commonprefix([m.name for m in tf.getmembers() if m.name])
        try:
            tf.extractall(dest_dir, filter="data")   # 3.12+：防路径穿越
        except TypeError:                            # 老版本没有 filter 参数
            tf.extractall(dest_dir)
    return os.path.join(dest_dir, root.strip("/"))


def download_latest(mode: str = "auto", timeout: float = 6.0) -> dict:
    """挑源 → 下载 → 读出包内版本。返回 {mirror, ver, root, tmp}。

    失败自动换下一个候选源；全部失败才抛 RuntimeError。调用方负责删 tmp。
    """
    cands = candidate_list(mode, timeout)
    errors = []
    for m in cands:
        tmp = tempfile.mkdtemp(prefix="starpilot-ug-")
        try:
            root = fetch_tarball(tmp, m, timeout=max(timeout * 5, 30))
            ver = local_version(root)
            if not ver:
                raise RuntimeError("包内 SKILL.md 没有 version")
            return {"mirror": m, "ver": ver, "root": root, "tmp": tmp}
        except Exception as e:
            errors.append("%s → %s: %s" % (m or "直连", type(e).__name__, str(e)[:80]))
            shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError("所有候选源都下载失败：\n  " + "\n  ".join(errors))


def run_tests(dst: str) -> dict:
    """装完立刻跑一遍单测：装坏了要当场发现，不能等下次用的时候才发现。"""
    tests = os.path.join(dst, "tests")
    if not os.path.isdir(tests):
        return {"ran": False, "why": "无 tests/ 目录"}
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", tests],
                           cwd=dst, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "tail": "测试超时（900s）"}
    text = (r.stderr or "") + "\n" + (r.stdout or "")
    ran = re.findall(r"^Ran (\d+) tests?", text, re.M)
    verdict = "OK" if re.search(r"^OK\b", text, re.M) else (
        "FAILED" if re.search(r"^FAILED\b", text, re.M) else "未知")
    bad = [ln for ln in text.splitlines() if ln.startswith(("FAIL:", "ERROR:"))]
    tail = "%s（%s 个测试）%s" % (verdict, ran[-1] if ran else "?",
                                 ("；" + "; ".join(bad[:3])) if bad else "")
    return {"ran": True, "ok": r.returncode == 0 and verdict == "OK", "tail": tail}


# ---------------------------------------------------------------- CLI


def cmd_check(a) -> int:
    lv = local_version(a.dir)
    src = download_latest(a.mirror, a.timeout)
    rv = src["ver"]
    try:
        newer = bool(rv) and bool(lv) and rv > lv
        out = {"local": fmt(lv), "remote": fmt(rv), "newer": newer,
               "source": src["mirror"] or "直连 github.com", "repo": REPO_URL}
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print("仓库：%s" % REPO_URL)
            print("本地：v%s    远端：v%s（源：%s）" % (fmt(lv), fmt(rv), out["source"]))
            print("有新版本，可升级：python scripts/upgrade.py --to-latest" if newer
                  else "已是最新（远端版本不高于本地）。")
        return 4 if newer else 0
    finally:
        shutil.rmtree(src["tmp"], ignore_errors=True)


def cmd_upgrade(a) -> int:
    dst = os.path.abspath(a.dir)
    lv = local_version(dst)
    print("[1/4] 选源并下载安装包…")
    src = download_latest(a.mirror, a.timeout)
    rv = src["ver"]
    src_name = src["mirror"] or "直连 github.com"
    print("      源：%s；本地 v%s → 远端 v%s" % (src_name, fmt(lv), fmt(rv)))
    try:
        if lv and rv <= lv and not a.force:
            print("已是最新（v%s），不重装。要强制重装加 --force。" % fmt(lv))
            return 0
        if os.path.isdir(os.path.join(dst, ".git")) and not a.force:
            print("检测到 %s 是 git 仓库。开发副本请用：git -C \"%s\" pull --ff-only\n"
                  "确要用本脚本覆盖（会留 .bak 备份）请加 --force。" % (dst, dst))
            return 1
        print("[2/4] 覆盖安装到 %s（runtime/runs/state/stage 等运行数据不动）…" % dst)
        info = install(src["root"], dst, prune=not a.no_prune)
        if info["backup"]:
            print("      旧版已备份：%s" % info["backup"])
        print("      写入 %d 个文件%s" % (info["written"],
                                     "，清理旧版残留 %d 个" % len(info["removed"])
                                     if info["removed"] else ""))
        print("[3/4] 安装后自检（单测）…")
        if a.no_test:
            print("      跳过（--no-test）")
        else:
            t = run_tests(dst)
            if not t.get("ran"):
                print("      跳过：%s" % t.get("why"))
            elif not t["ok"]:
                print("      失败：%s" % t.get("tail"))
                print("      新版单测没过，回滚：rm -rf \"%s\" && mv \"%s\" \"%s\""
                      % (dst, info["backup"], dst))
                return 1
            else:
                print("      通过：%s" % t.get("tail"))
        print("[4/4] 完成：v%s → v%s。skill 若被宿主缓存，重载一次 skill 生效。"
              % (fmt(lv), fmt(local_version(dst))))
        return 0
    finally:
        shutil.rmtree(src["tmp"], ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="upgrade.py",
        description="星舵升级器（从 %s 拉取最新版；国内网络自动切公益加速镜像）" % REPO_URL)
    ap.add_argument("--check", action="store_true", help="只检查有没有新版（有新版本退出码 4）")
    ap.add_argument("--to-latest", action="store_true", help="升到最新版并自检")
    ap.add_argument("--dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="安装目标目录（默认本脚本所在仓库/skill 根目录）")
    ap.add_argument("--mirror", default="auto",
                    help="auto（默认，自动挑可用源）/ direct（只直连）/ <加速前缀>（如 https://ghfast.top）")
    ap.add_argument("--timeout", type=float, default=6.0, help="单源探测超时秒数（默认 6）")
    ap.add_argument("--force", action="store_true", help="同版本也重装；git 仓库也允许覆盖")
    ap.add_argument("--no-prune", action="store_true",
                    help="不删除旧版残留（默认按上次安装清单清理）")
    ap.add_argument("--no-test", action="store_true", help="安装后不跑自检单测")
    ap.add_argument("--json", action="store_true", help="--check 输出 JSON")
    a = ap.parse_args(argv)
    if not (a.check or a.to_latest):
        a.check = True
    try:
        return cmd_upgrade(a) if a.to_latest else cmd_check(a)
    except RuntimeError as e:
        print("失败：%s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
