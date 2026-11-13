#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""statelock.py —— 任意锁路径的跨进程单写锁（纯 stdlib），C3-c 修复版。

历史与动机（架构评审 F3 → PROJECT-01-R1 C3-c → PROJECT-03-R1 任务书 §3）：
旧实现「进程已死 / 超时过久 / 文件损坏」三者任一即可接管（旧 :149），
导致**活进程仅因锁年龄超时就被抢锁**（C3-c，PROJECT-01-R1 实测 4/4 复现）。
本版按任务书重写接管语义：

七态（与 PROJECT-02-R1 ARCHITECTURE §4.2 一一对应）：
- LOCK_BUSY          别人正常持有，等或放弃（SP-GATE-001）
- LOCK_OWNER_ALIVE   持有者存活 —— 年龄只告警，**禁止接管**（SP-GATE-002）
- LOCK_OWNER_DEAD    持有者确认死亡 —— 允许受控接管（SP-GATE-003）
- LOCK_STATE_UNKNOWN 身份不可核验（PID 复用 / 查询失败 / 遗留锁且 pid 存活）
                     —— fail-closed，人工处置（SP-GATE-004）
- LOCK_CORRUPT       锁文件损坏 —— fail-closed，人工处置（SP-GATE-005）
- LOCK_RECOVERED     合法接管成功，fencing_generation 已 +1（SP-GATE-006）
- FENCED_OUT         旧 generation 在 dispatch/commit 入口被拒（SP-GATE-007，
                     由 execution_gate 判定，本模块提供 generation）

原子接管（终审补充 C：不得用「读→删→建」冒充 CAS）：
持有者进程在持锁期间**保持一个内核句柄**并对锁文件第 0 字节做独占范围锁
（Windows `LockFileEx(FAIL_IMMEDIATELY|EXCLUSIVE)`，POSIX `flock(LOCK_NB)`）。
接管者必须先赢得这个内核级互斥才允许改写 payload：
- 持有者进程死亡 → OS 自动关闭句柄 → 范围锁自动释放 → 接管可行；
- 持有者存活 → 范围锁必失败 → LOCK_BUSY，**不可能被抢**；
- 两个接管进程竞争 → 内核裁决唯一赢家，输家按普通竞争者循环。
改写 payload 的前提永远是「持有范围锁」；接管后还会复核 lock_id 与
（POSIX）inode 存活（st_nlink>0），防止持失效句柄写入已被替换的文件。
全程不存在「删除重建」，无绕过窗口。

fencing_generation：接管取 max(原持有者值, 旁路计数)+1；旁路计数文件
`<lock>.fencing` 在持锁期间更新，保证同一资源上的 generation 单调不回退。

身份（procidentity.py）：payload 记录 pid + process_start_token + executor_id +
lock_id + host。token 一致 = 持有者存活；token 不一致 = PID 已被复用 →
fail-closed（任务书 §3.1 明文禁止接管）。

遗留载荷兼容：无 token 字段的旧锁（历史测试与升级残留）——
- pid 死亡：范围锁可获取本身证明无存活持有者 → 允许接管（took_over:dead_pid）；
- pid 存活：身份不可核验 → LOCK_STATE_UNKNOWN fail-closed。
空文件（创建后、上锁前崩溃/创建中）：范围锁赢家才写 payload，所以空锁 +
范围锁可获取 = 无存活持有者 → 允许接管（took_over:empty_lock）；若「创建者」
还活着只是尚未上锁，它随后的范围锁会失败并按普通竞争者重试，不会出现双持有者。

fencing 作用域边界（终审补充 B，如实标注）：本锁只约束**同一台机器上能访问
同一锁目录**的执行者；不提供跨机分布式互斥。
"""
from __future__ import annotations

import errno
import json
import os
import socket
import time
import uuid

import procidentity
import sp_errors as _SP

# 锁状态 → 错误码（唯一定义在 sp_errors；这里只引用）
SP_GATE_002 = _SP.SP_GATE_002

# ---------------------------------------------------------------- 状态常量
ST_BUSY = "LOCK_BUSY"
ST_OWNER_ALIVE = "LOCK_OWNER_ALIVE"
ST_OWNER_DEAD = "LOCK_OWNER_DEAD"
ST_DEAD_LEGACY = "LOCK_DEAD_LEGACY"   # 分类内部态：遗留无 token 载荷且 pid 已死
ST_UNKNOWN = "LOCK_STATE_UNKNOWN"
ST_CORRUPT = "LOCK_CORRUPT"
ST_RECOVERED = "LOCK_RECOVERED"
ST_FENCED_OUT = "FENCED_OUT"
ST_FREE = "LOCK_FREE"        # 路径上无锁文件（分类辅助态，不是七态之一）
ST_EMPTY = "LOCK_EMPTY"      # 锁文件存在但为空（创建后、上锁前）

SCHEMA_VERSION = 2
_FENCING_SUFFIX = ".fencing"

_STATE_CODE = {
    ST_BUSY: "SP-GATE-001",
    ST_OWNER_ALIVE: "SP-GATE-002",
    ST_OWNER_DEAD: "SP-GATE-003",
    ST_UNKNOWN: "SP-GATE-004",
    ST_CORRUPT: "SP-GATE-005",
    ST_RECOVERED: "SP-GATE-006",
    ST_FENCED_OUT: "SP-GATE-007",
}


class LockBusy(RuntimeError):
    """锁不可得（含 fail-closed 态，用 code 区分）。异常里带持有者信息。"""

    code = "SP-GATE-001"

    def __init__(self, holder: dict, path: str, msg: str = "", code: str = ""):
        self.holder = holder or {}
        self.path = path
        if code:
            self.code = code
        super().__init__(msg or (
            "state 文件被占用：%s（pid=%s run=%s，持有 %ss）"
            % (path, self.holder.get("pid"), self.holder.get("run_id"),
               self.holder.get("age_s"))))


class LockStateUnknown(LockBusy):
    """持有者状态不明：fail-closed，禁止接管，人工处置。"""

    code = "SP-GATE-004"


class LockCorrupt(LockBusy):
    """锁文件损坏/缺关键字段：fail-closed，禁止自动接管。"""

    code = "SP-GATE-005"


def lock_path_for(state_path: str) -> str:
    return state_path + ".lock"


def read_holder_named(lock_path: str) -> dict:
    """读任意锁文件内容（不存在返回 {}；解析失败返回 {"broken": True}）。"""
    if not os.path.exists(lock_path):
        return {}
    try:
        with open(lock_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"broken": True}
    try:
        d["age_s"] = int(time.time() - float(d.get("ts") or 0))
    except Exception:
        d["age_s"] = -1
    return d


def read_holder(state_path: str) -> dict:
    return read_holder_named(lock_path_for(state_path))


def _pid_alive(pid: int) -> bool:
    """尽量判断 pid 是否还活着；判断不了就当作活着（宁可保守，不误抢锁）。

    不用 os.kill(pid, 0)：Windows 上 signal 0 会走到 TerminateProcess，风险太大。
    新代码请优先用 procidentity.token_status（能识别 PID 复用）。
    """
    if not pid or int(pid) <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM
    except Exception:
        return True


# ------------------------------------------------------------- 内核互斥原语

if os.name == "nt":
    import ctypes as _ct
    from ctypes import wintypes as _wt

    class _OVERLAPPED(_ct.Structure):
        _fields_ = [("Internal", _ct.c_void_p),
                    ("InternalHigh", _ct.c_void_p),
                    ("Offset", _ct.c_ulong),
                    ("OffsetHigh", _ct.c_ulong),
                    ("hEvent", _ct.c_void_p)]

    _K32 = _ct.windll.kernel32
    _INVALID_HANDLE = _ct.c_void_p(-1).value  # 0xFFFFFFFFFFFFFFFF
    _K32.CreateFileW.restype = _ct.c_void_p
    _K32.CreateFileW.argtypes = [_ct.c_wchar_p, _ct.c_ulong, _ct.c_ulong,
                                 _ct.c_void_p, _ct.c_ulong, _ct.c_ulong,
                                 _ct.c_void_p]
    _K32.LockFileEx.restype = _wt.BOOL
    _K32.LockFileEx.argtypes = [_ct.c_void_p, _ct.c_ulong, _ct.c_ulong,
                                _ct.c_ulong, _ct.c_ulong,
                                _ct.POINTER(_OVERLAPPED)]
    _K32.UnlockFileEx.restype = _wt.BOOL
    _K32.UnlockFileEx.argtypes = [_ct.c_void_p, _ct.c_ulong, _ct.c_ulong,
                                  _ct.c_ulong, _ct.POINTER(_OVERLAPPED)]
    _K32.SetFilePointer.restype = _ct.c_long
    _K32.SetFilePointer.argtypes = [_ct.c_void_p, _ct.c_long,
                                    _ct.POINTER(_ct.c_long), _ct.c_ulong]
    _K32.SetEndOfFile.restype = _wt.BOOL
    _K32.SetEndOfFile.argtypes = [_ct.c_void_p]
    _K32.WriteFile.restype = _wt.BOOL
    _K32.WriteFile.argtypes = [_ct.c_void_p, _ct.c_void_p, _ct.c_ulong,
                               _ct.POINTER(_ct.c_ulong), _ct.c_void_p]
    _K32.CloseHandle.restype = _wt.BOOL
    _K32.CloseHandle.argtypes = [_ct.c_void_p]

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READWRITEDELETE = 0x1 | 0x2 | 0x4
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_BEGIN = 0
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    # Windows 字节范围锁是**强制性**的：锁住 byte0 会连其他进程读整个文件都拒掉
    # （读必须跨过被锁字节）。所以把互斥字节放在文件末尾之外（64KB 处）——
    # 区域锁允许越过 EOF，JSON 载荷完全不受影响，任何读者照常可读。
    _LOCK_OFFSET = 0x10000


def _open_lock_handle(path: str, create_new: bool):
    """打开锁文件句柄（互斥载体）。共享模式含 FILE_SHARE_DELETE：
    持有者保留句柄期间，强制释放（release payload=None）仍能删文件。
    create_new=True 用 CREATE_NEW（原子 create-if-absent，等价 O_EXCL）。
    返回 (handle:int|None, win_error:int)。非 Windows 返回 (None, 0)。
    """
    if os.name != "nt":
        return (None, 0)
    h = _K32.CreateFileW(str(path), _GENERIC_READ | _GENERIC_WRITE,
                         _FILE_SHARE_READWRITEDELETE, None,
                         _CREATE_NEW if create_new else _OPEN_EXISTING,
                         _FILE_ATTRIBUTE_NORMAL, None)
    if h is None or h == _INVALID_HANDLE:
        return (None, _K32.GetLastError())
    return (h, 0)


def _range_lock_handle(h) -> bool:
    if os.name != "nt":
        return False
    ov = _OVERLAPPED()
    ov.Offset = _LOCK_OFFSET
    return bool(_K32.LockFileEx(h, _LOCKFILE_FAIL_IMMEDIATELY | _LOCKFILE_EXCLUSIVE_LOCK,
                                0, 1, 0, _ct.byref(ov)))


def _range_unlock_handle(h) -> None:
    if os.name != "nt":
        return
    try:
        ov = _OVERLAPPED()
        ov.Offset = _LOCK_OFFSET
        _K32.UnlockFileEx(h, 0, 1, 0, _ct.byref(ov))
    except Exception:
        pass


def _close_handle(h) -> None:
    if os.name != "nt" or not h:
        return
    try:
        _K32.CloseHandle(h)
    except Exception:
        pass


def _open_lock_fd(path: str, create_new: bool = False):
    """POSIX：普通 fd，配 flock 使用。"""
    flags = os.O_RDWR | ((os.O_CREAT | os.O_EXCL) if create_new else 0)
    return os.open(path, flags)


def _range_lock_fd(fd) -> bool:
    if os.name == "nt":
        return False
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _range_unlock_fd(fd) -> None:
    if os.name == "nt":
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass


# ---------------------------------------------------------------- payload

def _new_payload(owner: str, run_id: str, resource: str) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "pid": os.getpid(),
        "owner": owner or "wa",
        "run_id": run_id,
        "ts": time.time(),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "resource": resource,
        "executor_id": procidentity.executor_id(run_id=run_id, owner=owner or "wa"),
        "process_start_token": procidentity.self_token(),
        "fencing_generation": 0,
        "lock_id": "lk-" + uuid.uuid4().hex[:16],
        "host": socket.gethostname() or "unknown-host",
    }


def _write_payload_h(h, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    buf = _ct.create_string_buffer(data)
    written = _ct.c_ulong(0)
    _K32.SetFilePointer(h, 0, None, _FILE_BEGIN)
    _K32.SetEndOfFile(h)
    if not _K32.WriteFile(h, buf, len(data), _ct.byref(written), None):
        raise OSError("lock payload write failed")


def _write_payload_fd(fd, payload: dict) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _read_raw(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _fencing_file(lock_path: str) -> str:
    return lock_path + _FENCING_SUFFIX


def _read_fencing(lock_path: str) -> int:
    try:
        with open(_fencing_file(lock_path), encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _bump_fencing(lock_path: str, candidate: int) -> int:
    """持久化 generation 单调计数；调用方必须持锁。返回生效值。"""
    val = max(int(candidate), _read_fencing(lock_path))
    try:
        with open(_fencing_file(lock_path), "w", encoding="utf-8") as f:
            f.write(str(val))
    except Exception:
        pass  # 旁路计数失败不阻断；payload 内 generation 仍是权威
    return val


def _next_fencing(lock_path: str, holder: dict) -> int:
    return max(int(holder.get("fencing_generation") or 0),
               _read_fencing(lock_path)) + 1


# ---------------------------------------------------------------- 分类

def classify_holder_named(lock_path: str) -> dict:
    """把锁文件分类成七态之一（只读，不改状态）。

    返回 {"state": ST_*, "holder": {...}, "code": "SP-GATE-0NN", "detail": str}
    """
    if not os.path.exists(lock_path):
        return {"state": ST_FREE, "holder": {}, "code": "",
                "detail": "no lock file"}
    try:
        empty = os.path.getsize(lock_path) == 0
    except OSError:
        empty = False
    if empty:
        return {"state": ST_EMPTY, "holder": {}, "code": "SP-GATE-003",
                "detail": "empty lock file (created, never locked)"}
    holder = read_holder_named(lock_path)
    if holder.get("broken"):
        return {"state": ST_CORRUPT, "holder": holder, "code": "SP-GATE-005",
                "detail": "lock file not valid JSON"}
    pid = holder.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {"state": ST_CORRUPT, "holder": holder, "code": "SP-GATE-005",
                "detail": "missing/invalid pid"}
    token = holder.get("process_start_token")
    if not token:
        # 遗留载荷（无 token）：身份不可核验
        if _pid_alive(pid):
            return {"state": ST_UNKNOWN, "holder": holder, "code": "SP-GATE-004",
                    "detail": "legacy lock (no process_start_token) with live "
                              "pid — fail-closed"}
        return {"state": ST_DEAD_LEGACY, "holder": holder, "code": "SP-GATE-003",
                "detail": "legacy lock holder pid dead (range lock still "
                          "guards takeover)"}
    status = procidentity.token_status(pid, token)
    if status == "dead":
        return {"state": ST_OWNER_DEAD, "holder": holder, "code": "SP-GATE-003",
                "detail": "holder process confirmed dead"}
    if status == "match":
        return {"state": ST_OWNER_ALIVE, "holder": holder, "code": "SP-GATE-002",
                "detail": "holder alive, identity verified"}
    if status == "mismatch":
        return {"state": ST_UNKNOWN, "holder": holder, "code": "SP-GATE-004",
                "detail": "pid alive but process_start_token mismatch "
                          "(PID reused) — fail-closed"}
    return {"state": ST_UNKNOWN, "holder": holder, "code": "SP-GATE-004",
            "detail": "identity query failed — fail-closed"}


# ---------------------------------------------------------------- acquire

def _try_create_locked(lock_path: str, payload: dict) -> dict | None:
    """全新拿锁：原子创建 + 赢得范围锁 + 写 payload。输给竞争者返回 None。"""
    if os.name == "nt":
        h, _err = _open_lock_handle(lock_path, create_new=True)
        if h is None:
            return None
        locked = False
        try:
            if not _range_lock_handle(h):
                return None  # 防御：新建文件不应有人抢先锁
            locked = True
            payload["fencing_generation"] = _bump_fencing(
                lock_path, _read_fencing(lock_path) + 1)
            _write_payload_h(h, payload)
            return dict(payload, _h=int(h))
        except Exception:
            if locked:
                _range_unlock_handle(h)
            _close_handle(h)
            raise
    else:
        try:
            fd = _open_lock_fd(lock_path, create_new=True)
        except (FileExistsError, OSError):
            return None
        locked = False
        try:
            if not _range_lock_fd(fd):
                return None
            locked = True
            payload["fencing_generation"] = _bump_fencing(
                lock_path, _read_fencing(lock_path) + 1)
            _write_payload_fd(fd, payload)
            return dict(payload, _fd=int(fd))
        except Exception:
            if locked:
                _range_unlock_fd(fd)
            try:
                os.close(fd)
            except OSError:
                pass
            raise


def _try_takeover_locked(lock_path: str, new_payload: dict,
                         expected_lock_id: str) -> dict | None:
    """受控接管：先赢得内核范围锁，再复核，再改写 payload。赢家由内核裁决。

    复核三件事（任一失败视为输家，解锁返回 None 让外层重判）：
    - 路径上的文件仍在（Windows delete-pending / POSIX 被删均检出）；
    - POSIX：fd 指向的 inode 仍链接在目录里（st_nlink>0，防换 inode 双持有）；
    - 载荷 lock_id 与分类时一致（防他人已改写）。
    """
    if os.name == "nt":
        h, _err = _open_lock_handle(lock_path, create_new=False)
        if h is None:
            return None  # 文件消失/暂不可开：外层重判
        locked = False
        success = False
        try:
            if not _range_lock_handle(h):
                return None  # 有存活持有者 → busy
            locked = True
            if not os.path.exists(lock_path):
                return None  # 句柄已是 delete-pending 残影
            cur = read_holder_named(lock_path)
            if expected_lock_id and cur.get("lock_id") != expected_lock_id:
                return None
            new_payload["fencing_generation"] = _bump_fencing(
                lock_path, new_payload["fencing_generation"])
            _write_payload_h(h, new_payload)
            success = True
            return dict(new_payload, _h=int(h))
        except Exception:
            return None
        finally:
            if not success:
                if locked:
                    _range_unlock_handle(h)
                _close_handle(h)
    else:
        try:
            fd = _open_lock_fd(lock_path, create_new=False)
        except OSError:
            return None
        locked = False
        try:
            if not _range_lock_fd(fd):
                os.close(fd)
                return None
            locked = True
            if os.fstat(fd).st_nlink == 0 or not os.path.exists(lock_path):
                return None  # 持失效 inode，弃权重判
            cur = read_holder_named(lock_path)
            if expected_lock_id and cur.get("lock_id") != expected_lock_id:
                return None
            new_payload["fencing_generation"] = _bump_fencing(
                lock_path, new_payload["fencing_generation"])
            _write_payload_fd(fd, new_payload)
            return dict(new_payload, _fd=int(fd))
        except Exception:
            if locked:
                _range_unlock_fd(fd)
            try:
                os.close(fd)
            except OSError:
                pass
            return None


def acquire_named(lock_path: str, owner: str = "", run_id: str = "",
                  timeout: float = 10.0, stale_after: float = 300.0,
                  resource: str = "", allow_reentry: bool = False,
                  executor_id: str = "") -> dict:
    """对任意锁路径拿锁。返回持有者记录（含内部 _h/_fd）；拿不到抛 LockBusy 系。

    executor_id：调用方的执行者身份（procidentity.executor_id() 产出）。
    传入则入账该值（fencing 校验以「同一字符串」为准）；缺省由本模块按
    owner/run_id 自行计算（兼容旧调用方）。

    接管语义（C3-c 修复）：
    - 持有者存活（token 一致）→ LOCK_OWNER_ALIVE，年龄超 stale_after **只告警**；
    - 持有者死亡（token 复核 dead / 遗留死 pid / 空锁文件）→ 受控接管，
      范围锁仲裁唯一赢家，fencing_generation+1，payload.state=LOCK_RECOVERED；
    - PID 复用 / 身份查询失败 / 遗留锁且 pid 存活 → LOCK_STATE_UNKNOWN，
      立即 fail-closed（抛 LockStateUnknown，禁止接管）；
    - 锁文件损坏 / pid 字段缺失 → LOCK_CORRUPT，立即 fail-closed（抛 LockCorrupt）。
    """
    p = lock_path
    os.makedirs(os.path.dirname(os.path.abspath(p)) or ".", exist_ok=True)
    resource = resource or os.path.abspath(p)
    base = _new_payload(owner, run_id, resource)
    if executor_id:
        base["executor_id"] = executor_id
    deadline = time.time() + max(0.0, timeout)
    while True:
        won = _try_create_locked(p, dict(base))
        if won:
            return won
        # --- 竞争路径 ---
        cls = classify_holder_named(p)
        holder = cls["holder"]
        state = cls["state"]
        if state == ST_FREE:
            continue  # 分类与创建之间的窗口，重试
        # 同进程重入（仅 allow_reentry=True）：外层已持有（如 wa.cmd_send
        # 装饰器 → wa.do_send 再取锁）直接放行，release 不动锁文件。
        if allow_reentry and not holder.get("broken"):
            hpid = int(holder.get("pid") or 0)
            htok = holder.get("process_start_token") or ""
            same_proc = (hpid == os.getpid()
                         and (not htok or htok == procidentity.self_token()))
            if same_proc:
                return {"pid": hpid, "owner": holder.get("owner") or base["owner"],
                        "run_id": holder.get("run_id"), "reentrant": True,
                        "resource": resource, "at": base["at"],
                        "executor_id": holder.get("executor_id")}
        age = holder.get("age_s") or 0
        if state == ST_OWNER_ALIVE:
            # C3-c 修复点：年龄只告警，绝不触发接管。
            if age > stale_after:
                holder["stale_warning"] = (
                    "holder alive %ss > stale_after %ss (age never grants takeover)"
                    % (age, stale_after))
            if time.time() >= deadline:
                raise LockBusy(holder, p,
                               msg="LOCK_OWNER_ALIVE：%s 持有（pid=%s，run=%s）"
                                   "—— 存活持有者禁止接管（年龄仅告警）"
                                   % (p, holder.get("pid"), holder.get("run_id")),
                               code=SP_GATE_002)
            time.sleep(0.15)
            continue
        if state in (ST_UNKNOWN, ST_CORRUPT):
            exc = LockCorrupt if state == ST_CORRUPT else LockStateUnknown
            raise exc(holder, p,
                      msg="%s：%s（%s）—— fail-closed，禁止自动接管，人工处置"
                          % (state, cls.get("detail"), p),
                      code=cls["code"])
        if state in (ST_OWNER_DEAD, ST_DEAD_LEGACY, ST_EMPTY):
            note = ("took_over:dead_pid" if state in (ST_OWNER_DEAD, ST_DEAD_LEGACY)
                    else "took_over:empty_lock")
            cand = dict(base)
            cand["fencing_generation"] = _next_fencing(p, holder)
            cand["stale"] = note
            cand["state"] = ST_RECOVERED
            cand["took_over_from"] = {
                "pid": holder.get("pid"), "lock_id": holder.get("lock_id"),
                "state": state, "detail": cls.get("detail")}
            took = _try_takeover_locked(p, cand, holder.get("lock_id") or "")
            if took:
                return took
            if time.time() >= deadline:
                raise LockBusy(holder, p)
            time.sleep(0.05)
            continue
        # 未知分类结果：保守按 busy 等待
        if time.time() >= deadline:
            raise LockBusy(holder, p)
        time.sleep(0.15)


def acquire(state_path: str, owner: str = "", run_id: str = "",
            timeout: float = 10.0, stale_after: float = 300.0) -> dict:
    """拿锁。返回持有者记录；拿不到抛 LockBusy。"""
    return acquire_named(lock_path_for(state_path), owner, run_id,
                         timeout, stale_after, resource=os.path.abspath(state_path))


# ---------------------------------------------------------------- release

def release_named(lock_path: str, payload: dict = None) -> None:
    """放锁。payload 带内部句柄则解锁+关闭+删文件；payload=None 为管理强制释放。"""
    p = lock_path
    # 重入层不释放锁文件：真正的持锁者（外层）才负责放锁
    if payload and payload.get("reentrant"):
        return
    h = int(payload["_h"]) if (payload and payload.get("_h")) else None
    fd = int(payload["_fd"]) if (payload and payload.get("_fd")) else None
    if h:
        _range_unlock_handle(h)
        _close_handle(h)
    if fd is not None:
        _range_unlock_fd(fd)
        try:
            os.close(fd)
        except OSError:
            pass
    if h or fd is not None:
        # 我们持有权威句柄/fd，是真正持有者：解锁后删除锁文件
        try:
            os.remove(p)  # Windows 句柄带 FILE_SHARE_DELETE，删除可完成
        except OSError:
            pass
        return
    if payload is None:
        # 管理强制释放：尽力删除（存活持有者的句柄带 SHARE_DELETE，可删；
        # 删除后其后续 payload 改写会失败，重新竞争时按路径重判）
        try:
            os.remove(p)
        except OSError:
            pass
        return
    # 老式调用（无内部句柄）：只释放自己的锁，pid 对不上不动（防误删别人的锁）
    try:
        with open(p, encoding="utf-8") as f:
            cur = json.load(f)
        if int(cur.get("pid") or 0) not in (0, os.getpid()):
            return
    except Exception:
        pass
    try:
        os.remove(p)
    except OSError:
        pass


def release(state_path: str, payload: dict = None) -> None:
    release_named(lock_path_for(state_path), payload)


class state_lock(object):
    """`with state_lock(path, run_id=...)`：写 state 时用。"""

    def __init__(self, state_path: str, owner: str = "", run_id: str = "",
                 timeout: float = 10.0, stale_after: float = 300.0):
        self.state_path = state_path
        self.owner = owner
        self.run_id = run_id
        self.timeout = timeout
        self.stale_after = stale_after
        self.payload = None

    def __enter__(self):
        self.payload = acquire(self.state_path, self.owner, self.run_id,
                               self.timeout, self.stale_after)
        return self.payload

    def __exit__(self, *exc):
        release(self.state_path, self.payload)
        return False
