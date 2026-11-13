#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""envelope_node.py —— P2-2-4R：实际派发边界的 **Ego Node 原生 Ed25519 验签**。

为什么必须走这里，而不是 Python 镜像验签：
- `envelope.verify_envelope` 是 Python 侧镜像（便于本地快速失败与单测），
  但生产执行端的权威验签必须在 worker 实际运行时（Ego 内置 Node）完成。
- 本模块把 **实际要交给 backend 的那一份 envelope**（同一 dict，序列化后）
  写盘 → 交给 `ego-browser nodejs` 执行 `scripts/ego/node_verify.mjs` →
  由 node:crypto 的 `crypto.verify(null, bytes, publicKey, sig)` 做原生 Ed25519 验签。
- 任何异常（可执行文件缺失、超时、非零退出、输出不可解析、结果文件缺失、
  Node 主版本不符）一律 fail-closed（ok=False），绝不抛给调用方、绝不降级。

纪律：
- **不提供任何“跳过 Node 验签”的开关**。测试若需模拟运行时故障，改的是
  `STARPILOT_EGO_EXE` 指向（坏运行时），失败方向仍是拒绝。
- 运行时版本会随结果返回（`runtime`），调用方必须据此留证。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile

# 星舵固定版本（2026-11-13 用户拍板：固定 0.6.2.10，升级需冷启动冒烟后再切）。
EGO_EXE_PINNED = r"C:\Program Files\Citro Labs\ego lite\Application\0.6.2.10\ego-browser.exe"
EGO_EXE_ENV = "STARPILOT_EGO_EXE"
# **严格白名单**（非「主版本下限」）：只有这里列出的运行时版本可用于验签。
# 当前固定的 ego 0.6.2.10 内置 Node 为 24.22.1；升级 ego 必须同步更新本清单
# 并重跑 NR 矩阵——这是显式的部署决策，不做隐式放行。
ALLOWED_NODE_RUNTIMES = ("24.22.1",)
MARKER = "SP_NODE_VERIFY_RESULT "
BOOTSTRAP_NAME = "node_verify.mjs"


class NodeVerifyUnavailable(Exception):
    """Node 运行时不可用（缺失/超时/非零退出/输出不可解析）。"""


def ego_exe() -> str:
    """返回 ego-browser 可执行文件路径（缺省为固定版本；可用环境变量覆盖）。"""
    return os.environ.get(EGO_EXE_ENV) or EGO_EXE_PINNED


def bootstrap_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ego", BOOTSTRAP_NAME)


def _read_result(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "ok" not in data:
        return None
    return data


def _parse_stdout(text: str) -> dict | None:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(MARKER):
            try:
                data = json.loads(line[len(MARKER):])
            except ValueError:
                return None
            return data if isinstance(data, dict) and "ok" in data else None
    return None


def verify_via_node(envelope: dict, key_dir: str, timeout: float = 30.0) -> dict:
    """把 envelope 交给 Ego Node 原生验签。永远返回 dict，永不抛异常。

    返回 {ok, code, reason, runtime, stage, exit_code}
    - ok=True 仅在：退出码 0 + 结果可解析 + **nonce/digest 与本次调用绑定一致**
      + 字段类型合法 + 运行时在批准清单内 + Node 明确 ok:true；
    - 其余一切情形 ok=False（fail-closed），reason 说明阶段。

    单次调用隔离（P2-2-4R 补充核验 ②）：
    - 每次调用新建独立临时目录（mkdtemp），结果文件名固定但目录唯一；
    - 调用前**断言结果文件不存在**（新建目录天然为空，缺失即 abort）；
    - 结果必须回写本次随机 nonce 与本次 envelope 的 digest，Python 侧逐一比对；
    - 退出码、结果存在性、字段完整性与类型全部纳入成功条件。
    """
    stage = "start"
    exe = ego_exe()
    tmpdir = ""
    nonce = secrets.token_hex(16)
    expect_digest = envelope.get("envelope_digest") if isinstance(envelope, dict) else ""
    try:
        if not isinstance(envelope, dict):
            return {"ok": False, "code": "SP-OP-001", "stage": "input",
                    "reason": "envelope not a dict", "runtime": "", "exit_code": None}
        boot = bootstrap_path()
        if not os.path.isfile(boot):
            return {"ok": False, "code": "SP-OP-001", "stage": "bootstrap",
                    "reason": "node verify bootstrap missing: %s" % boot,
                    "runtime": "", "exit_code": None}
        stage = "exe_lookup"
        if not os.path.isfile(exe):
            return {"ok": False, "code": "SP-OP-001", "stage": "runtime_unavailable",
                    "reason": "ego node runtime not found: %s" % exe,
                    "runtime": "", "exit_code": None}

        stage = "materialize"
        tmpdir = tempfile.mkdtemp(prefix="sp-nodeverify-")
        env_path = os.path.join(tmpdir, "envelope.json")
        out_path = os.path.join(tmpdir, "result.json")
        if os.path.exists(out_path):          # 单次调用隔离：调用前必须无旧结果
            return {"ok": False, "code": "SP-OP-001", "stage": "stale_result_present",
                    "reason": "result file already exists before node run",
                    "runtime": "", "exit_code": None}
        # 与 backend 收到的对象完全同源：同一 dict 的规范化序列化
        with open(env_path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        # ego 的 node 运行时**不透传环境变量**（实测），故把配置内嵌进 bootstrap。
        module_url = "file:///" + os.path.dirname(boot).replace("\\", "/") + "/" + BOOTSTRAP_NAME
        cfg = json.dumps({"dir": os.path.dirname(boot).replace("\\", "/"),
                          "envPath": env_path.replace("\\", "/"),
                          "keyDir": str(key_dir).replace("\\", "/"),
                          "outPath": out_path.replace("\\", "/"),
                          "nonce": nonce,
                          "expectDigest": str(expect_digest or "")},
                         ensure_ascii=False)
        bootstrap_src = (
            "const mod = await import(%s);\n"
            "await mod.runNodeVerify(%s);\n" % (json.dumps(module_url), cfg)
        )

        child_env = dict(os.environ)
        child_env["PYTHONUTF8"] = "1"
        stage = "node_run"
        try:
            proc = subprocess.run(
                [exe, "nodejs"], input=bootstrap_src, env=child_env,
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "SP-OP-001", "stage": "node_timeout",
                    "reason": "ego node verify timed out after %.1fs" % timeout,
                    "runtime": "", "exit_code": None}
        except OSError as e:
            return {"ok": False, "code": "SP-OP-001", "stage": "node_spawn",
                    "reason": "ego node spawn failed: %s" % str(e)[:160],
                    "runtime": "", "exit_code": None}

        rc = proc.returncode
        # 退出状态是成功条件之一（先于结果采信）
        if rc != 0:
            return {"ok": False, "code": "SP-OP-001", "stage": "node_nonzero_exit",
                    "reason": "ego node exited rc=%r" % rc,
                    "runtime": "", "exit_code": rc}

        stage = "parse"
        # ego 运行时的 console.log 走 stderr（实测）；结果文件优先，两条路都过同样的
        # 绑定校验——旧文件/半成品/串号结果都会因 nonce 不匹配被拒。
        data = (_read_result(out_path)
                or _parse_stdout(proc.stdout)
                or _parse_stdout(proc.stderr))
        if data is None:
            return {"ok": False, "code": "SP-OP-001", "stage": "result_unparseable",
                    "reason": "node verify produced no parseable result (rc=%r)" % rc,
                    "runtime": "", "exit_code": rc}

        # 字段完整性与类型严格校验
        if not isinstance(data.get("ok"), bool):
            return {"ok": False, "code": "SP-OP-001", "stage": "result_invalid",
                    "reason": "result field ok is not bool", "runtime": "",
                    "exit_code": rc}
        for fld in ("code", "reason", "runtime", "nonce", "digest"):
            if not isinstance(data.get(fld), str):
                return {"ok": False, "code": "SP-OP-001", "stage": "result_invalid",
                        "reason": "result field %s missing/not str" % fld,
                        "runtime": "", "exit_code": rc}
        runtime = data["runtime"]
        # 绑定校验：结果必须属于本次调用
        if not secrets.compare_digest(data["nonce"], nonce):
            return {"ok": False, "code": "SP-OP-001", "stage": "result_not_bound",
                    "reason": "result nonce mismatch (stale/foreign result)",
                    "runtime": runtime, "exit_code": rc}
        if str(expect_digest or "") and data["digest"] != str(expect_digest):
            return {"ok": False, "code": "SP-OP-001", "stage": "result_not_bound",
                    "reason": "result digest does not match current envelope",
                    "runtime": runtime, "exit_code": rc}
        # 运行时版本约束（严格白名单，见 ALLOWED_NODE_RUNTIMES）
        if runtime not in ALLOWED_NODE_RUNTIMES:
            return {"ok": False, "code": "SP-OP-001", "stage": "runtime_untrusted",
                    "reason": "node runtime %r not in approved list %s"
                              % (runtime, list(ALLOWED_NODE_RUNTIMES)),
                    "runtime": runtime, "exit_code": rc}
        if data.get("ok") is not True:
            return {"ok": False, "code": data.get("code") or "SP-OP-001",
                    "stage": "verify_rejected",
                    "reason": str(data.get("reason") or "node verification failed")[:200],
                    "runtime": runtime, "exit_code": rc}
        return {"ok": True, "code": "", "stage": "verified", "reason": "",
                "runtime": runtime, "exit_code": rc}
    except Exception as e:  # 兜底：任何未预期异常一律拒绝
        return {"ok": False, "code": "SP-OP-001", "stage": stage,
                "reason": "unexpected: %s" % str(e)[:160],
                "runtime": "", "exit_code": None}
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
