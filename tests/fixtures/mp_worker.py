#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp_worker.py —— 真实多进程测试的独立 OS 进程 worker（PROJECT-03-R1 组 E）。

由 test_lock_mp.py 以 subprocess 启动（每模式一个全新进程，绝不复用线程）。
结果一律写 JSON 文件，由父进程读取断言；worker 崩溃也必须留下结果。
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import statelock  # noqa: E402
import procidentity  # noqa: E402
import pageidentity  # noqa: E402
import actionledger  # noqa: E402
import execution_gate  # noqa: E402
import sp_errors as SP  # noqa: E402


def _save(out_path: str, rec: dict) -> None:
    rec["pid"] = os.getpid()
    rec["executor_id"] = procidentity.executor_id()
    rec["at"] = time.time()
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)


def mode_acquire(args):
    """acquire --lock P --out O --hold S [--stale S2] [--kill-after S3]"""
    lp, out = args["lock"], args["out"]
    rec = {"mode": "acquire"}
    t0 = time.time()
    try:
        h = statelock.acquire_named(lp, owner="mp", run_id="w",
                                    timeout=float(args.get("timeout", 5)),
                                    stale_after=float(args.get("stale_after", 300)),
                                    resource=args.get("resource", lp))
    except statelock.LockStateUnknown as e:
        rec.update(verdict="LOCK_STATE_UNKNOWN", code=e.code, detail=str(e))
        return _save(out, rec)
    except statelock.LockCorrupt as e:
        rec.update(verdict="LOCK_CORRUPT", code=e.code, detail=str(e))
        return _save(out, rec)
    except statelock.LockBusy as e:
        rec.update(verdict="LOCK_BUSY", code=e.code,
                   holder=e.holder, detail=str(e))
        return _save(out, rec)
    except Exception as e:
        rec.update(verdict="ERROR", detail=repr(e))
        return _save(out, rec)
    rec.update(verdict="ACQUIRED",
               fencing_generation=h.get("fencing_generation"),
               lock_id=h.get("lock_id"), stale=h.get("stale"),
               took=h.get("took_over_from"),
               acquired_elapsed=time.time() - t0)
    _save(out, rec)  # 先落「已拿锁」证据
    hold = float(args.get("hold", 0))
    kill_after = float(args.get("kill_after", 0))
    if kill_after > 0:
        time.sleep(kill_after)
        os._exit(9)  # 模拟强制终止（不清理锁，让接管路径处理）
    if hold > 0:
        time.sleep(hold)
    statelock.release_named(lp, h)
    rec["released"] = True
    _save(out, rec)


def mode_gate_dispatch(args):
    """gate_dispatch --ledger L --pages P --lock LP --out O --fencing N
    --page-gen PG --page-key K [--identity JSON] --aid A
    用**外部传入的**（可能过期的）fencing/执行者尝试受控派发。"""
    rec = {"mode": "gate_dispatch"}
    try:
        ledger = actionledger.ActionLedger(args["ledger"])
        pi = pageidentity.PageIdentityManager(args["pages"])
        gate = execution_gate.ExecutionGate(
            args["lock"], ledger, pi,
            ownership_probe=lambda tid: "agent",  # 测试桩（显式标注，非真实 Ego）
            resource=args.get("resource", args["lock"]))
        ident = json.loads(args["identity"]) if args.get("identity") else {
            "url": "data:x", "title": "T", "marker": "M", "timeOrigin": 1.0}
        ctx = {"executor_id": args.get("executor_id") or procidentity.executor_id(),
               "run_id": "w", "fencing_generation": int(args["fencing"]),
               "page_generation": args.get("page_gen", "")}
        r = gate.dispatch(ctx, "send",
                          lambda op, a, c: {"ok": True, "result": "mock-post"},
                          {"x": 1}, taskspace_id=32, page_key=args.get("page_key", ""),
                          identity_tuple=ident, observation=None,
                          action_id=args["aid"])
        rec.update(ok=r["ok"], code=r["code"], evidence=r["evidence"],
                   data=r.get("data"))
        ledger.close()
    except Exception as e:
        rec.update(verdict="ERROR", detail=repr(e))
    _save(out := args["out"], rec)


def mode_ledger_reserve(args):
    """ledger_reserve --ledger L --out O --task T --intent JSON [--seq N]"""
    rec = {"mode": "ledger_reserve"}
    import time as _t
    import sqlite3
    for attempt in range(5):
        try:
            ledger = actionledger.ActionLedger(args["ledger"])
            r = ledger.reserve(args["task"], json.loads(args["intent"]),
                               seq=int(args.get("seq", 0)),
                               budget_units=int(args.get("budget", 10)),
                               executor_id=procidentity.executor_id())
            rec.update(ok=r["ok"], code=r["code"], data=r.get("data"),
                       evidence=r["evidence"])
            ledger.close()
            break
        except sqlite3.OperationalError as e:
            if ("locked" in str(e).lower() or "busy" in str(e).lower()) and attempt < 4:
                _t.sleep(0.5 + 0.5 * attempt)
                continue
            rec.update(ok=False, code="SQLITE_BUSY", detail=repr(e))
            break
        except Exception as e:
            rec.update(verdict="ERROR", detail=repr(e))
            break
    _save(args["out"], rec)


def mode_dispatch_then_die(args):
    """dispatch_then_die --ledger L --lock LP --out O --task T --intent JSON
    派发后、outcome 前强制退出（os._exit，不留任何清理机会）。"""
    rec = {"mode": "dispatch_then_die"}
    try:
        ledger = actionledger.ActionLedger(args["ledger"])
        r = ledger.reserve(args["task"], json.loads(args["intent"]),
                           budget_units=10, executor_id=procidentity.executor_id())
        rec["reserve"] = {"ok": r["ok"], "code": r["code"]}
        if r["ok"]:
            aid = r["data"]["action_id"]
            d = ledger.mark_dispatched(aid, 1, procidentity.executor_id())
            rec["dispatch"] = {"ok": d["ok"], "code": d["code"], "aid": aid}
            _save(args["out"], rec)
            os._exit(9)  # 绝不 commit、绝不放锁
        _save(args["out"], rec)
    except Exception as e:
        rec.update(verdict="ERROR", detail=repr(e))
        _save(args["out"], rec)


def mode_recover(args):
    """recover --ledger L --out O [--task T --intent JSON]"""
    rec = {"mode": "recover"}
    try:
        ledger = actionledger.ActionLedger(args["ledger"])
        recov = ledger.recover_unknown()
        rec["recovered"] = recov["data"]
        if args.get("task"):
            r = ledger.reserve(args["task"], json.loads(args["intent"]),
                               seq=int(args.get("seq", 77)), budget_units=10)
            rec["re_reserve"] = {"ok": r["ok"], "code": r["code"]}
            locked = ledger.locked_intents(args["task"])
            rec["locked_intents"] = locked
        ledger.close()
    except Exception as e:
        rec.update(verdict="ERROR", detail=repr(e))
    _save(args["out"], rec)


MODES = {
    "acquire": mode_acquire,
    "gate_dispatch": mode_gate_dispatch,
    "ledger_reserve": mode_ledger_reserve,
    "dispatch_then_die": mode_dispatch_then_die,
    "recover": mode_recover,
}


def main():
    mode = sys.argv[1]
    args = {}
    for kv in sys.argv[2:]:
        k, _, v = kv.partition("=")
        args[k.lstrip("-")] = v
    MODES[mode](args)


if __name__ == "__main__":
    main()
