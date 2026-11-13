#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_lint.py —— 业务模块绕过 ExecutionGate 直调后端的静态检查。

规范来源：PROJECT-03-R1 任务书 §6（「静态检查必须能够识别并阻止业务模块
绕过 ExecutionGate 直接调用后端」）+ ARCHITECTURE INV-1。

规则：
- 后端层 = scripts/ 下模块名以 `_backend` 结尾，或属于 BACKEND_MODULES
  （backend_contract、ego_backend、bsk_backend…）。
- 允许 import 后端的只有执行层白名单：execution_gate / gate_lint / sp_errors
  及后端模块自身、测试文件（tests/ 不在扫描范围）。
- 检测方式：解析 AST 的 import / from-import，不做字符串 grep（防误报）。
- 违例：exit 1，列出违例文件、行号、import 名。CI / 阶段门控直接调用。

边界：本检查约束的是「未来接入的星舵后端层」。现行 wa.py → bskcompat 属于
PROJECT-05 迁移前的历史通道，不在违例名单里（迁移完成后再收编）。
"""
from __future__ import annotations

import ast
import os
import sys

# 后端模块定义（唯一名单）
BACKEND_MODULES = {
    "backend_contract",
    "ego_backend",
    "bsk_backend",
    "browser_backend",
}

# 执行层白名单：允许 import 后端
ALLOWED_IMPORTERS = {
    "execution_gate",
    "gate_lint",
    "sp_errors",
}


def is_backend_module(name: str) -> bool:
    n = name.strip().split(".")[0]
    return n in BACKEND_MODULES or (n.endswith("_backend") and n not in ALLOWED_IMPORTERS)


def lint_file(path: str) -> list:
    """返回违例列表 [{file, line, module}]。"""
    violations = []
    mod_name = os.path.splitext(os.path.basename(path))[0]
    if mod_name in ALLOWED_IMPORTERS or is_backend_module(mod_name):
        return violations
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except Exception as e:
        return [{"file": path, "line": 0, "module": "",
                 "why": "unparsable: %s" % e}]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_backend_module(alias.name):
                    violations.append({"file": path, "line": node.lineno,
                                       "module": alias.name,
                                       "why": "business module imports backend directly"})
        elif isinstance(node, ast.ImportFrom):
            src = node.module or ""
            if is_backend_module(src):
                violations.append({"file": path, "line": node.lineno,
                                   "module": src,
                                   "why": "business module imports backend directly"})
    return violations


def lint_files(paths: list) -> list:
    all_v = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    if fn.endswith(".py"):
                        all_v.extend(lint_file(os.path.join(root, fn)))
        elif p.endswith(".py"):
            all_v.extend(lint_file(p))
    return all_v


def main(argv: list) -> int:
    paths = argv[1:] or [os.path.join(os.path.dirname(os.path.abspath(__file__)))]
    violations = lint_files(paths)
    for v in violations:
        print("GATE-LINT VIOLATION %s:%s import=%s %s"
              % (v["file"], v["line"], v["module"], v["why"]))
    if violations:
        print("gate lint: %d violation(s) — business modules must go through "
              "ExecutionGate (INV-1)" % len(violations))
        return 1
    print("gate lint: clean (%d path(s))" % len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
