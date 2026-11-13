"""PROJECT-04 P2-2-2：dispatch marker——浏览器派发前的本地持久化证据屏障。

语义边界（GPT P2-2-2 任务书）：
- Marker 不是普通日志，也不是派发成功证明；只证明「派发前的本地证据已按规定写入」。
- 派发权威事件由 P2-2-3 的 DISPATCH_DELEGATED 承担（本包不实现）。
- Marker 必须绑定：op_id + envelope_digest + fencing + worker_id + key_version
  + marker 协议版本 + 审计时间；字段严格校验，缺一或类型不符即拒绝。

写入顺序（S2-S5，任一失败 → MarkerPersistFailed，上层禁止派发）：
  S2 O_EXCL 排他创建（禁止覆盖已有 Marker；同 op_id 二次创建必失败）
  S3 完整内容单次写入
  S4 文件 fsync
  S5 目录持久化——平台分支：
     POSIX：目录 fd fsync（标准保证）
     Windows：哨兵文件 fsync（尽力而为；明确不等价于目录项持久性保证——
       D2 裁定接受，保证边界如实标注，丢失场景由「marker 无+DELEGATED 在
       → UNKNOWN」保守合并兜底）

文件名稳定性：文件名只由 op_id 派生（<op_id>.marker.json，op_id 字符集
[A-Za-z0-9._-] 且拒绝路径分隔符/ ".."）——同一操作无法通过改名/改目录规避
排他创建；跨层防绕过由 P2-1 L-opd（action_id 主键）保证同一意图唯一 op_id。

无删除接口：本模块刻意不提供任何删除/覆盖函数；清理只属于 reconcile
（P2-3），worker/Gate 运行期永不删 Marker。

半成品不误认完整：crash 留下的空/半写文件无法通过 read_marker 的严格校验
（完整 JSON + 全字段 + 类型），恢复侧一律按「存在但无效」保守处理（UNKNOWN），
且不清理、不覆盖（O_EXCL 二次创建必失败 → fail-closed）。

rename 说明（如实声明）：本实现 O_EXCL 直接落在最终路径，不经 rename——
「rename 源/目标目录 fsync」要求不适用于本模块；若未来引入 rename 必须补
源/目标双目录 fsync 并过 MK05。ops 认领 rename（S0）属栈A transport，不在本包。

POSIX 分支在本机（Windows）无法实跑，已在 WSL Ubuntu-24.04 冒烟验证（见报告）；
Windows 分支以本机实测为准。
"""

import errno
import json
import os
import re
import time

MARKER_PROTOCOL_VERSION = "1"
_OP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^[0-9]{1,19}$")
SENTINEL_NAME = ".dirsync"
_MARKER_SUFFIX = ".marker.json"


class MarkerError(Exception):
    """marker 基类（所有失败 → 上层禁止派发）。"""


class MarkerExists(MarkerError):
    """O_EXCL 排他冲突：同 op_id Marker 已存在（绝不覆盖）。"""


class MarkerPersistFailed(MarkerError):
    """S2-S5 任一持久化步骤失败（绝不自动退化「写了就算成功」）。"""


class MarkerInvalid(MarkerError):
    """已有文件不能通过严格校验（半成品/篡改/身份不一致）。"""


def dispatching_dir(ops_root: str) -> str:
    """marker 目录：<ops_root>/dispatching/（同卷；禁临时目录）。"""
    d = os.path.join(ops_root, "dispatching")
    os.makedirs(d, exist_ok=True)
    _ensure_permissions(d)
    _ensure_sentinel(d)
    return d


def _ensure_permissions(d: str) -> None:
    """目录权限收紧（POSIX 0700；Windows ACL 用户独占为部署责任）。"""
    if os.name == "posix":
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def _ensure_sentinel(d: str) -> None:
    """Windows 哨兵文件（目录项持久化尽力而为的载体；POSIX 亦创建，无害）。"""
    p = os.path.join(d, SENTINEL_NAME)
    if not os.path.exists(p):
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise


def _validate_op_id(op_id: str) -> None:
    if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id) or ".." in op_id:
        raise MarkerInvalid("op_id not filename-stable: %r" % (op_id,))


def marker_path(dispatching: str, op_id: str) -> str:
    """稳定文件名：只由 op_id 派生（路径安全校验）。"""
    _validate_op_id(op_id)
    return os.path.join(dispatching, op_id + _MARKER_SUFFIX)


def _fsync_dir_posix(d: str) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir_windows(d: str) -> None:
    """哨兵文件 fsync——证明范围 = 哨兵内容持久化请求完成，**不等于**
    marker 所在目录全部目录项已持久化（D2 裁定如实标注）。"""
    p = os.path.join(d, SENTINEL_NAME)
    fd = os.open(p, os.O_WRONLY)
    try:
        os.write(fd, b"1")
        os.fsync(fd)
    finally:
        os.close(fd)


def write_marker(dispatching: str, op_id: str, envelope_digest: str,
                 fencing: str, worker_id: str, key_version: str) -> dict:
    """S2-S5 全序列；全部成功才返回（上层才可认为 Marker 已持久化）。

    任一步失败：MarkerExists（排他冲突）或 MarkerPersistFailed —— 两种情况
    上层都必须禁止派发。绝不覆盖、绝不半途当成功。
    """
    fields = {
        "op_id": op_id,
        "envelope_digest": envelope_digest,
        "fencing": fencing,
        "worker_id": worker_id,
        "key_version": key_version,
    }
    _validate_fields(fields)
    path = marker_path(dispatching, op_id)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "marker_version": MARKER_PROTOCOL_VERSION,
        "created_at": now,
        **fields,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")

    # S2 排他创建
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as e:
        raise MarkerExists("marker already exists (never overwritten): %s" % path) from e
    except OSError as e:
        raise MarkerPersistFailed("create failed: %s" % e) from e
    try:
        # S3 完整内容写入（单次 write）
        try:
            n = os.write(fd, blob)
            if n != len(blob):
                raise MarkerPersistFailed("short write %d/%d" % (n, len(blob)))
        except MarkerError:
            raise
        except OSError as e:
            raise MarkerPersistFailed("write failed: %s" % e) from e
        # S4 文件 fsync
        try:
            os.fsync(fd)
        except OSError as e:
            raise MarkerPersistFailed("fsync file failed: %s" % e) from e
    finally:
        os.close(fd)
    # S5 目录持久化（平台分支）
    try:
        if os.name == "posix":
            _fsync_dir_posix(dispatching)
        else:
            _fsync_dir_windows(dispatching)
    except OSError as e:
        raise MarkerPersistFailed("dir persistence failed (%s): %s"
                                  % (os.name, e)) from e
    return {"path": path, "dirsync": os.name, "created_at": now}


def _validate_fields(fields: dict) -> None:
    for k in ("op_id", "envelope_digest", "fencing", "worker_id", "key_version"):
        v = fields.get(k)
        if not isinstance(v, str) or not v:
            raise MarkerInvalid("field %s must be non-empty str" % k)
    if not _DIGEST_RE.match(fields["envelope_digest"]):
        raise MarkerInvalid("envelope_digest not sha256 hex64")
    if not _DECIMAL_RE.match(fields["fencing"]):
        raise MarkerInvalid("fencing not decimal string")
    _validate_op_id(fields["op_id"])


def read_marker(path: str) -> dict:
    """严格读取（恢复/对账用）。任何不完整/篡改/身份不一致 → MarkerInvalid。"""
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        raise MarkerInvalid("unreadable: %s" % e) from e
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise MarkerInvalid("not complete JSON (half-written?): %s" % e) from e
    if not isinstance(payload, dict):
        raise MarkerInvalid("payload not an object")
    allowed = {"marker_version", "created_at", "op_id", "envelope_digest",
               "fencing", "worker_id", "key_version"}
    if set(payload) != allowed:
        raise MarkerInvalid("field set mismatch: %s" % sorted(set(payload) ^ allowed))
    if payload["marker_version"] != MARKER_PROTOCOL_VERSION:
        raise MarkerInvalid("marker_version mismatch")
    _validate_fields(payload)
    # 文件名与内容身份一致性（防改文件名错位）
    expect = os.path.basename(path)
    if expect != payload["op_id"] + _MARKER_SUFFIX:
        raise MarkerInvalid("filename/content identity mismatch")
    return payload


def verify_marker_identity(path: str, expected: dict) -> dict:
    """身份核验（MK07/MK08）：内容完整 + 与期望身份逐字段一致。"""
    payload = read_marker(path)
    mismatch = [k for k in ("op_id", "envelope_digest", "fencing",
                            "worker_id", "key_version")
                if k in expected and payload[k] != expected[k]]
    if mismatch:
        raise MarkerInvalid("identity mismatch: %s" % mismatch)
    return payload
