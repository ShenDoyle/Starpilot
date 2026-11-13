#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ego_r2_worker.py —— E5/E6/E7 用例 worker：真实 fill 经 Gate，
桥接完成后、commit 前 os._exit(9)（零清理），父进程负责恢复验证。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

import ego_backend as eb
import ego_r2_harness as H
import pageidentity
import actionledger
import procidentity
import envelope
import execution_gate
import time

lock_path = os.environ["R2_LOCK"]
ledger_path = os.environ["R2_LEDGER"]
pages_path = os.environ["R2_PAGES"]
space_id = int(os.environ["R2_SPACE"])
url = os.environ["R2_URL"]
marker = os.environ["R2_MARKER"]

led = actionledger.ActionLedger(ledger_path)
pi = pageidentity.PageIdentityManager(pages_path)

# P2-2-4：跨进程同样不得绕过派发前屏障（Envelope/Marker/DELEGATED）。
# 密钥与 ops 目录由父进程通过环境变量指定，均落在临时目录内（零入库）。
_key_dir = os.environ["R2_KEYS"]
if not os.path.exists(os.path.join(_key_dir, "registry.json")):
    envelope.ensure_keypair(_key_dir, "1")
    envelope.write_registry(_key_dir, "1", ["1"])
plane = execution_gate.SecurityPlane(
    key_dir=_key_dir, worker_id="w-e567", ops_root=os.environ["R2_OPS"])
gate = eb.make_gate(lock_path, led, pi, resource="r2-e567", plane=plane)
exec_id = procidentity.executor_id(run_id="r2worker")

ident = H.wait_page_ready(space_id, "p1")
ac = gate.acquire({"executor_id": exec_id, "run_id": "r2worker"})
assert ac["ok"], ac
gen = ac["data"]["fencing_generation"]
b = pi.bind("sp%d|p1" % space_id, ident, exec_id, gen, "bind")
obs = eb.build_observation(space_id, "p1", b["page_generation"], exec_id,
                           gen, ident)
r = led.reserve("t-e567", {"op": "fill", "target": "isolated localhost"},
                budget_units=10, executor_id=exec_id,
                business_target="space:%d|p1|t-e567|fill" % space_id)
aid = r["data"]["action_id"]
ctx = {"executor_id": exec_id, "run_id": "r2worker",
       "fencing_generation": gen, "page_generation": b["page_generation"]}
backend = lambda o, a, c: eb.page_op(o, space_id, "p1", **a)
out = gate.dispatch(ctx, "fill", backend,
                    {"selector": "#inp", "value": "killed-mid-flight"},
                    taskspace_id=space_id, page_key="sp%d|p1" % space_id,
                    identity_tuple=ident, observation=obs, action_id=aid)
# dispatch 内部 mark_dispatched 已落账；无论 outcome 如何 —— 模拟崩溃：
json.dump({"action_id": aid, "dispatch_state": "committed_in_ledger_or_unknown",
           "out_code": out["code"]}, open(marker, "w", encoding="utf-8"))
sys.stdout.flush()
os._exit(9)   # 绝不 commit outcome、绝不 release 锁
