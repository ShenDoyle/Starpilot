#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pageidentity.py —— 星舵自有页面身份：page_fingerprint 与 page_generation 分离。

规范来源：PROJECT-02-R1 终审补充 A + PROJECT-03-R1 任务书 §4。

两条铁律：
1. **page_fingerprint**（pf-…）：页面身份摘要 = sha16(canonical(identity_tuple))。
   identity_tuple = {url, title, marker, timeOrigin}。label **绝不参与**
   （PROJECT-01-R1 a1-d-a2 证明 label 恒定时 url/marker/timeOrigin 可同时变）。
2. **page_generation**（pg-<N>）：星舵维护的**单调递增绑定代次**，绝不是
   fingerprint 的哈希。每次绑定事件（首绑 / 导航 / 重绑 / 身份失效后重验 /
   执行者更换 / fencing 变化）推进 +1。**指纹相同也不复用旧 generation**
   —— a1-d-a2 证明重新导航后指纹可能全同，旧 observation 不得因此复活。

持久化：JSON 状态文件（跨 run / 跨进程），按 page_key（= taskspace_id +
page_label 的星舵映射键）分桶。写操作全部「读-改-写 + 单文件原子替换」，
且只应由持锁的执行者调用（ExecutionGate 已持 statelock）。

本阶段不依赖真实 Ego Page：identity_tuple 由调用方传入（测试夹具或
未来 Ego 后端采集），接口与后端解耦。
"""
from __future__ import annotations

import hashlib
import json
import os
import time


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def canonical(obj) -> str:
    """规范化 JSON：排序键、紧凑分隔、去字符串首尾空白（值内部不动）。"""
    return json.dumps(_norm(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _norm(obj):
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        return {str(k): _norm(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm(v) for v in obj]
    return obj


def fingerprint_of(identity_tuple: dict) -> str:
    """page_fingerprint = pf- + sha16(canonical(identity_tuple))。

    identity_tuple 约定字段：url / title / marker / timeOrigin（缺字段视为
    空串参与规范化，时间戳必须原样传入不取整）。
    """
    return "pf-" + _sha16(canonical(identity_tuple or {}))


class PageIdentityManager:
    """绑定代次账本。page_key → {generation, fingerprint, binding…}。"""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self._load()

    # ---------------------------------------------------------- 持久化
    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self._db = json.load(f)
        except Exception:
            self._db = {"pages": {}}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)) or ".",
                    exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._db, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)  # 同目录原子替换

    # ---------------------------------------------------------- 核心 API
    def bind(self, page_key: str, identity_tuple: dict, executor_id: str,
             fencing_generation: int, reason: str = "bind") -> dict:
        """绑定页面 → generation **必然 +1**（首绑=1）。

        reason ∈ {bind, navigate, rebind_after_invalidate, executor_changed,
                  fencing_changed, manual}。即使 fingerprint 与上次完全相同，
        也推进代次（终审补充 A 的强制要求）。
        """
        rec = self._db["pages"].get(page_key) or {}
        prev_gen = int(rec.get("generation") or 0)
        prev_fp = rec.get("fingerprint") or ""
        new_gen = prev_gen + 1
        fp = fingerprint_of(identity_tuple)
        binding = {
            "page_key": page_key,
            "generation": new_gen,
            "page_generation": "pg-%d" % new_gen,
            "fingerprint": fp,
            "identity": identity_tuple or {},
            "executor_id": executor_id,
            "fencing_generation": int(fencing_generation),
            "bound_at": time.time(),
            "reason": reason,
            "prev_generation": prev_gen,
            "same_fingerprint_as_prev": bool(prev_fp and prev_fp == fp),
            "generation_never_reused": True,
        }
        self._db["pages"][page_key] = binding
        self._save()
        return dict(binding)

    def verify(self, page_key: str, identity_tuple: dict, page_generation: str,
               executor_id: str, fencing_generation: int) -> dict:
        """三重门之门 3：核验执行者声称的绑定仍然成立。

        返回统一 Result（错误码锚点 sp_errors）：
        - SP-OK                    绑定成立，返回 {fingerprint, generation,…}
        - SP-GATE-009 PAGE_IDENTITY_CHANGED  当前页面事实与绑定指纹不一致
        - SP-GATE-007 FENCED_OUT   执行者/fencing 与绑定记录不符
        - SP-GATE-010 OBSERVATION_INVALID  绑定不存在 / 代次对不上
        失败时调用方必须 invalidate + 重新 bind，旧 observation 不复活。
        """
        rec = self._db["pages"].get(page_key)
        if not rec:
            return self._fail("SP-GATE-010", page_key,
                              "no binding for page_key=%s" % page_key)
        gen = int(rec.get("generation") or 0)
        if page_generation not in ("pg-%d" % gen, gen, str(gen)):
            return self._fail("SP-GATE-010", page_key,
                              "claimed generation %r != current pg-%d"
                              % (page_generation, gen))
        if executor_id and rec.get("executor_id") != executor_id:
            return self._fail("SP-GATE-007", page_key,
                              "executor %r != binding executor %r"
                              % (executor_id, rec.get("executor_id")))
        if fencing_generation and int(rec.get("fencing_generation") or 0) != int(fencing_generation):
            return self._fail("SP-GATE-007", page_key,
                              "fencing %s != binding %s"
                              % (fencing_generation, rec.get("fencing_generation")))
        fp = fingerprint_of(identity_tuple)
        if fp != rec.get("fingerprint"):
            return self._fail("SP-GATE-009", page_key,
                              "live fingerprint %s != bound %s" % (fp, rec.get("fingerprint")))
        return {"ok": True, "code": "SP-OK", "data": dict(rec),
                "evidence": "binding verified", "fencing_generation": rec.get("fencing_generation")}

    def invalidate(self, page_key: str, reason: str = "identity_changed") -> dict:
        """作废绑定。作废后**不继承**：下一次 bind 产生全新 generation。"""
        rec = self._db["pages"].get(page_key)
        if not rec:
            return {"ok": False, "code": "SP-GATE-010", "data": {},
                    "evidence": "no binding to invalidate"}
        rec["invalidated"] = True
        rec["invalidated_at"] = time.time()
        rec["invalidated_reason"] = reason
        rec["invalidated_generation"] = rec.get("generation")
        self._db["pages"][page_key] = rec
        self._save()
        return {"ok": True, "code": "SP-OK", "data": dict(rec),
                "evidence": "binding invalidated; rebind will mint new generation"}

    def status(self, page_key: str) -> dict:
        rec = self._db["pages"].get(page_key)
        if not rec:
            return {"bound": False}
        return {"bound": not rec.get("invalidated"),
                "generation": rec.get("generation"),
                "page_generation": rec.get("page_generation"),
                "fingerprint": rec.get("fingerprint"),
                "invalidated": bool(rec.get("invalidated"))}

    # ---------------------------------------------------------- 内部
    @staticmethod
    def _fail(code: str, page_key: str, detail: str) -> dict:
        return {"ok": False, "code": code,
                "data": {"page_key": page_key, "detail": detail},
                "evidence": detail}
