#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feedback.py —— 收集用户提交 GitHub Issue 所需的最小信息，产出可直接粘贴的报告块。

用法（agent 在用户**同意**提交 issue 后运行，2026-11-13 裁定：先问再收，不主动上报）：

    python scripts/feedback.py            # 打印整理好的粘贴块（含 issues 链接）
    python scripts/feedback.py --out p.md # 另存文件
    python scripts/feedback.py --kind platform-drift   # 平台改版类（选择器失效）

隐私红线（硬规则）：本脚本只收集版本、环境自检与状态**摘要**；不读正文、
不贴绑定 URL 的查询串、不带凭证。agent 转述用户描述时同样遵守。

纯标准库，Python 3.9+。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as envmod      # noqa: E402
import platforms          # noqa: E402
import upgrade as upg     # noqa: E402

REPO_ISSUES = "https://github.com/%s/issues" % upg.REPO


def state_summary(root: str) -> dict:
    """state 摘要：平台与对话身份，不含完整 URL/正文（隐私红线）。"""
    p = os.path.join(root, ".wa-state.json")
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return {}
    url = st.get("url") or ""
    host = urlparse(url).netloc if url else ""
    return {"platform": st.get("platform") or "",
            "url_host": host,
            "conv_id": st.get("conv_id") or "",
            "transport": envmod.transport(),
            "browser_backend": st.get("browser") or "",
            "ego_version": st.get("ego_version") or "",
            "ego_status": st.get("ego_status") or ""}


def env_snapshot() -> dict:
    rep = envmod.check(with_doctor=False)
    keep = ("ok", "platform", "backend", "transport", "needs_decision",
            "python", "bsk", "ego_browser")
    return {k: rep.get(k) for k in keep if k in rep}


def build(kind: str, root: str = "") -> str:
    root = os.path.abspath(root or os.getcwd())
    envrep = env_snapshot()
    st = state_summary(root)
    lv = upg.fmt(upg.local_version(root))
    versions = {
        "starpilot": lv,
        "ego_browser": (envrep.get("ego_browser") or {}).get("version") or "(未装/未知)",
        "ego_anchor": (envrep.get("ego_browser") or {}).get("anchor") or "(未知)",
        "bsk": (envrep.get("bsk") or {}).get("version") or "(未装)",
        "transport": envrep.get("transport"),
        "python": (envrep.get("python") or {}).get("version"),
    }
    lines = [
        "### 环境（自动收集，请人工核对无敏感信息后提交）",
        "```json",
        json.dumps({"versions": versions, "state_summary": st,
                    "env_check": envrep}, ensure_ascii=False, indent=2),
        "```",
        "",
        "### 问题描述（用户填写）",
        "- 现象：",
        "- 期望：",
        "- 复现步骤：",
        "- 回执三栏（已完成/未完成/未验证存疑，如涉本轮任务请附）",
        "",
        "请提交到：%s" % REPO_ISSUES,
    ]
    head = {
        "bug": "# Bug 报告",
        "platform-drift": "# 平台改版报告（选择器失效/流程异常）\n\n"
                         "- 平台：`%s`\n- 现象（哪一步判定失效、probe 输出摘录）：\n"
                         "- 改版时间（大约）：" % (st.get("platform") or "(填平台)"),
    }.get(kind, "# 问题报告")
    return head + "\n\n" + "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="整理 GitHub Issue 粘贴块（隐私红线见文件头）")
    ap.add_argument("--kind", choices=["bug", "platform-drift"], default="bug")
    ap.add_argument("--out", default="", help="另存文件路径（缺省只打印）")
    a = ap.parse_args(argv)
    text = build(a.kind)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("已写入：%s" % os.path.abspath(a.out))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
