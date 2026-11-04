#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星舵更新检查：用时顺手查「Starpilot 自身 + bsk 插件」有没有新稳定版，有就提示用户。

    python scripts/update_check.py               # 人话表格
    python scripts/update_check.py --json        # 机读
    python scripts/update_check.py --force       # 忽略缓存真查一次

设计原则（每条都是踩过坑换来的）：
- **只提示，不升级。** 升级是用户拍板的事：Starpilot 走
  `python scripts/upgrade.py --to-latest`；bsk 走 `python scripts/env.py --install`
  （活跃 bsk session 存在时禁升级，见 bsk update 运维红线）。
- **轻量**：GitHub API 直连，只拉元数据，不下载整包（那是 upgrade.py 的活）。
- **失败即 unknown，绝不猜。** 网络不通、API 变脸、限流都如实报，退出码保持可用，
  不阻塞正常工作流——更新检查挂了不能连累干活的会话。
- **缓存**：默认 24h 只真查一次，结果落 `runtime/state/update-check.json`（属运行数据，
  upgrade.py 的 KEEP_LOCAL 不覆盖）。
- **稳定版口径**（拿 tag 还是拿 release 是有讲究的）：
    Starpilot = ShenDoyle/Starpilot 的最高 `v*` tag。发版纪律：SKILL.md front-matter
    与 tag 同步 bump，所以本地版本（SKILL.md）与远端 tag 可直接比较。
    bsk       = Tencent/BrowserSkill 的最高 `cli-v*` release。该仓库 CLI 与扩展分渠道
    发版（`cli-v*` / `ext-v*`），CLI 的最新稳定版必须从 release 列表里按前缀过滤，
    直接拿第一个 release 会错拿成扩展的版本号。

纯标准库，Python 3.9+。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as envmod      # noqa: E402  复用 find_bsk / bsk_version（import 安全，有 main guard）
import upgrade as upg     # noqa: E402  复用 parse_version / local_version / fmt

SELF_REPO = "ShenDoyle/Starpilot"
BSK_REPO = "Tencent/BrowserSkill"
BSK_CLI_PREFIX = "cli-v"
SELF_TAG_PREFIX = "v"
UA = {"User-Agent": "starpilot-update-check/1.0 ( +%s )" % SELF_REPO}
DEFAULT_TTL_HOURS = 24.0


# ---------------------------------------------------------------- 版本工具


def parse_tag(tag: str, prefix: str) -> tuple:
    """'cli-v0.4.1' / 'v0.1.7' → (0,4,1) / (0,1,7)。不合式返回 ()。"""
    if not tag or not tag.startswith(prefix):
        return ()
    rest = tag[len(prefix):]
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", rest):
        return ()
    return tuple(int(x) for x in rest.split("."))


def latest_stable_from_releases(releases: list, prefix: str) -> tuple:
    """release 列表（API 顺序，新的在前）里取该渠道的最高稳定版。prerelease 排除。"""
    best = ()
    for r in releases or []:
        if r.get("prerelease") or r.get("draft"):
            continue
        v = parse_tag(r.get("tag_name") or "", prefix)
        if v > best:
            best = v
    return best


def latest_stable_from_tags(tags: list, prefix: str) -> tuple:
    best = ()
    for t in tags or []:
        v = parse_tag(t.get("name") or "", prefix)
        if v > best:
            best = v
    return best


def newer(remote: tuple, local: tuple) -> bool:
    """远端是否比本地新。任一边未知（空元组）都返回 False——不确定不算新。"""
    return bool(remote) and bool(local) and remote > local


# ---------------------------------------------------------------- 网络（fail-soft）


def gh_api(url: str, timeout: float) -> list:
    """GitHub API GET。任何失败抛异常，由调用方转 unknown——不在底层吞错。"""
    req = urllib.request.Request(url, headers=dict(UA, Accept="application/vnd.github+json"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2 ** 22).decode("utf-8", "replace"))


def fetch_remote(repo: str, prefix: str, timeout: float) -> dict:
    """查一个仓库的某渠道最新稳定版。

    先走 releases（有 prerelease 标记可排除，bsk 必须走这条）；
    release 404（仓库从不用 release 发版，如早期 Starpilot）退 tags；
    都不行如实报 unknown。返回 {version: tuple, source: str, err: str}。
    """
    base = "https://api.github.com/repos/%s" % repo
    try:
        rel = gh_api(base + "/releases?per_page=30", timeout)
        v = latest_stable_from_releases(rel, prefix)
        if v:
            return {"version": v, "source": "releases", "err": ""}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return {"version": (), "source": "", "err": "HTTP %d" % e.code}
    except Exception as e:
        return {"version": (), "source": "", "err": "%s: %s" % (type(e).__name__, str(e)[:80])}
    try:  # releases 为空/404 → tags 兜底（tags 无 prerelease 信息，按发版纪律 tag 即稳定版）
        tags = gh_api(base + "/tags?per_page=100", timeout)
        v = latest_stable_from_tags(tags, prefix)
        if v:
            return {"version": v, "source": "tags", "err": ""}
        return {"version": (), "source": "", "err": "无匹配 %s* 的 tag" % prefix}
    except Exception as e:
        return {"version": (), "source": "", "err": "%s: %s" % (type(e).__name__, str(e)[:80])}


# ---------------------------------------------------------------- 本地版本


def local_self(root: str) -> tuple:
    """Starpilot 本地版本：SKILL.md front-matter（与 upgrade.py 同源，单一事实）。"""
    return upg.local_version(root)


def local_bsk(runner=None, timeout: float = 20.0) -> tuple:
    """bsk 本地版本：`bsk --version`。找不到/失败返回 ()。"""
    path = envmod.find_bsk()
    if not path:
        return ()
    v = envmod.bsk_version(path, runner=runner, timeout=timeout)
    return upg.parse_version("version: %s" % v) if v else ()


# ---------------------------------------------------------------- 缓存


def cache_path(root: str) -> str:
    return os.path.join(root, "runtime", "state", "update-check.json")


def load_cache(root: str, ttl_hours: float) -> dict:
    """新鲜缓存原样返回；过期/损坏/缺字段一律返回 {}（等于没缓存，不猜）。"""
    p = cache_path(root)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict) or "fetched_at" not in d:
        return {}
    if not isinstance(d.get("starpilot"), dict) or not isinstance(d.get("bsk"), dict):
        return {}
    age_h = (time.time() - float(d["fetched_at"])) / 3600.0
    if age_h < 0 or age_h >= ttl_hours:
        return {}
    d["_age_hours"] = round(age_h, 2)
    return d


def save_cache(root: str, data: dict) -> None:
    p = cache_path(root)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # 缓存写失败不影响本次结果，下次多查一次而已


# ---------------------------------------------------------------- 主流程


def check(root: str, timeout: float = 8.0, ttl_hours: float = DEFAULT_TTL_HOURS,
          force: bool = False, skip_self: bool = False, skip_bsk: bool = False,
          runner=None) -> dict:
    """完整检查。返回 {starpilot:..., bsk:..., cached:bool}，永不抛网络异常。"""
    root = os.path.abspath(root)
    out = {"cached": False}
    if not force:
        cached = load_cache(root, ttl_hours)
        if cached:
            out = {"starpilot": cached["starpilot"], "bsk": cached["bsk"],
                   "cached": True, "_age_hours": cached.get("_age_hours")}
            if not (skip_self or skip_bsk):
                return out

    lv_self = local_self(root)
    lv_bsk = () if skip_bsk else local_bsk(runner=runner)
    r_self = {"local": upg.fmt(lv_self), "version": (), "source": "", "err": "跳过"}
    r_bsk = {"local": upg.fmt(lv_bsk), "version": (), "source": "", "err": "跳过"}
    if not skip_self:
        f = fetch_remote(SELF_REPO, SELF_TAG_PREFIX, timeout)
        r_self.update(f)
    if not skip_bsk:
        f = fetch_remote(BSK_REPO, BSK_CLI_PREFIX, timeout)
        r_bsk.update(f)

    for key, r, lv in (("starpilot", r_self, lv_self), ("bsk", r_bsk, lv_bsk)):
        r["local"] = upg.fmt(lv)
        r["remote"] = upg.fmt(r.get("version") or ())
        r["newer"] = newer(r.get("version") or (), lv)
    out.update({"starpilot": r_self, "bsk": r_bsk, "cached": False,
                "fetched_at": time.time()})
    if not (skip_self and skip_bsk):
        save_cache(root, {"fetched_at": out["fetched_at"],
                          "starpilot": r_self, "bsk": r_bsk})
    return out


def human(rep: dict) -> str:
    lines = []
    if rep.get("cached"):
        lines.append("（缓存，%s 小时前查过；--force 可真查）" % rep.get("_age_hours", "?"))
    for label, key, hint in (
            ("Starpilot", "starpilot",
             "python scripts/upgrade.py --to-latest"),
            ("bsk 插件", "bsk",
             "python scripts/env.py --install（先确认无活跃 bsk session）")):
        r = rep.get(key) or {}
        if r.get("newer"):
            lines.append("⬆ %s 有新版：本地 v%s → 远端 v%s（%s）\n  升级命令：%s"
                         % (label, r.get("local"), r.get("remote"), r.get("source"), hint))
        elif r.get("remote") not in (None, "", "(未知)"):
            lines.append("✓ %s 已是最新（v%s）" % (label, r.get("local")))
        elif r.get("err") in ("跳过",):
            lines.append("- %s 本次未检查" % label)
        else:
            lines.append("? %s 未知（本地 v%s；远端查不到：%s）——如实报，不猜"
                         % (label, r.get("local"), r.get("err") or "无匹配"))
    return "\n".join(lines)


def main(argv=None) -> int:
    root_default = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        prog="update_check.py",
        description="检查 Starpilot 自身与 bsk 插件的最新稳定版（只提示，不升级）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机读）")
    ap.add_argument("--force", action="store_true", help="忽略缓存真查一次")
    ap.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS,
                    help="缓存有效期小时数（默认 24）")
    ap.add_argument("--timeout", type=float, default=8.0, help="单请求超时秒数（默认 8）")
    ap.add_argument("--skip-self", action="store_true", help="不查 Starpilot 自身")
    ap.add_argument("--skip-bsk", action="store_true", help="不查 bsk")
    ap.add_argument("--dir", default=root_default, help="仓库根目录（默认脚本上级）")
    a = ap.parse_args(argv)
    try:
        rep = check(a.dir, timeout=a.timeout, ttl_hours=a.ttl_hours,
                    force=a.force, skip_self=a.skip_self, skip_bsk=a.skip_bsk)
    except Exception as e:  # 双保险：检查器自己炸了也不能连累使用方
        rep = {"starpilot": {"err": "%s: %s" % (type(e).__name__, str(e)[:80])},
               "bsk": {"err": "跳过"}}
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(human(rep))
    any_newer = any((rep.get(k) or {}).get("newer") for k in ("starpilot", "bsk"))
    return 4 if any_newer else 0


if __name__ == "__main__":
    sys.exit(main())
