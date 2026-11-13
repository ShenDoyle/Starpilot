"""PROJECT-04 P2-2-1：Envelope 协议 + Ed25519 签名（Gate 侧，Python）。

安全协议（GPT P2-2-1 任务书 + D5 裁定）：
- 签名算法：Ed25519（cryptography==45.0.7，wheel sha256 锁定；缺失即
  EnvelopeUnavailable → 拒签 fail-closed，绝不退化手写实现——D1 裁定）。
- 唯一签名输入字节序列（跨语言规范，D5 裁定强制）：
  1) 受保护字段集合固定（PROTECTED_FIELDS，10 字段）；
  2) 字段顺序 = 键名字典序（json.dumps sort_keys=True，无重复键可能）；
  3) 值全部为字符串（协议版本/密钥版本/fencing 均以十进制字符串表示，
     禁止 int/float/bool/None —— 消除数值表示歧义）；
  4) 空值处理：business_target_norm 允许 ""（无业务对象的非副作用 op），
     其余字段必须非空字符串；
  5) UTF-8 编码、紧凑分隔符 (",", ":")；
  6) signature 与 envelope_digest 不参与签名输入与摘要计算；
  7) envelope_digest = sha256(签名输入字节) 十六进制；
  8) signature = Ed25519(签名输入字节) 原始 64 字节的十六进制。
- Node 侧权威验签：scripts/ego/envelope_verify.mjs（node:crypto 原生），
  两端必须对同一字节序列签名/验签（T-ED-01 证明）。
- 密钥管理：
  - 密钥目录：$STARPILOT_ENVELOPE_KEYS 或 <root>/runtime/keys/（gitignore
    双保险：runtime/ 与 *.pem 均不进仓库）；
  - 布局：<key_dir>/v<key_version>/private.pem(PKCS8) + public.pem(SPKI)；
  - 注册表：<key_dir>/registry.json = {"active_version","valid_versions","retired"}；
    验签方按 key_version 选公钥；未知/退役版本 → 拒（ED06）；
  - 私钥权限：POSIX 0600（Windows ACL 用户独占为部署责任，文档声明）；
  - 测试密钥由测试在临时目录动态生成，生产密钥与测试密钥严格分离，
    任何私钥不进 git/worker/日志。
- 所有异常默认拒绝（verify_* 返回 ok=False + SP-OP-001 原因）。
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import sp_errors as SP

PROTOCOL_VERSION = "1"
ALGORITHM = "ed25519"
SIGNATURE_HEX_LEN = 128  # 64 字节 Ed25519 裸签名

# 受保护字段（签名输入 = 这些字段按字典序的规范 JSON）。顺序即文档约定，
# 实际排序由 sort_keys 保证；两端口装此表。
PROTECTED_FIELDS = (
    "algorithm",
    "business_target_norm",
    "fencing",
    "intent_key",
    "issued_at",
    "key_version",
    "op_id",
    "task_id",
    "version",
    "worker_id",
)
# business_target_norm 允许空串（无业务对象的非副作用操作）；其余必须非空
_FIELDS_ALLOW_EMPTY = ("business_target_norm",)


class EnvelopeUnavailable(Exception):
    """签名依赖缺失（D1：fail-closed，绝不退化手写实现）。"""


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.exceptions import InvalidSignature  # noqa: F401

        return Ed25519PrivateKey
    except ImportError as e:  # pragma: no cover - 依赖在场时不触发
        raise EnvelopeUnavailable(
            "cryptography unavailable (%s) — fail-closed, refusing to sign" % e
        ) from e


def canonical_bytes(fields: dict) -> bytes:
    """唯一签名输入字节序列（见模块 docstring 规范）。违规即 ValueError。"""
    if not isinstance(fields, dict):
        raise ValueError("fields must be a dict")
    for k in PROTECTED_FIELDS:
        if k not in fields:
            raise ValueError("missing field: %s" % k)
        v = fields[k]
        if not isinstance(v, str):
            raise ValueError("field %s must be str, got %s" % (k, type(v).__name__))
        if not v and k not in _FIELDS_ALLOW_EMPTY:
            raise ValueError("field %s must be non-empty" % k)
    extra = set(fields) - set(PROTECTED_FIELDS)
    if extra:
        raise ValueError("unknown protected fields: %s" % sorted(extra))
    return json.dumps(
        {k: fields[k] for k in PROTECTED_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def envelope_digest(fields: dict) -> str:
    """envelope_digest = sha256(canonical_bytes) 十六进制。"""
    return hashlib.sha256(canonical_bytes(fields)).hexdigest()


# ---------------------------------------------------------------- 密钥管理

def default_key_dir(root: str = "") -> str:
    d = os.environ.get("STARPILOT_ENVELOPE_KEYS") or os.path.join(
        root or os.getcwd(), "runtime", "keys")
    os.makedirs(d, exist_ok=True)
    return d


def ensure_keypair(key_dir: str, key_version: str) -> dict:
    """生成（若缺失）v<key_version> 密钥对；返回 {private.pem, public.pem} 路径。

    私钥 PKCS8 PEM，POSIX 0600；公钥 SPKI PEM（Node createPublicKey 直接可用）。
    生产密钥轮换 = 新版本目录 + registry 更新；旧版本移入 retired。
    """
    Ed25519PrivateKey = _ed25519()
    d = os.path.join(key_dir, "v%s" % key_version)
    os.makedirs(d, exist_ok=True)
    priv_p = os.path.join(d, "private.pem")
    pub_p = os.path.join(d, "public.pem")
    if not os.path.exists(priv_p):
        key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization

        priv_b = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        pub_b = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        fd = os.open(priv_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, priv_b)
        finally:
            os.close(fd)
        with open(pub_p, "wb") as f:
            f.write(pub_b)
    try:  # POSIX 收紧权限；Windows 由部署 ACL 兜底（文档声明）
        os.chmod(priv_p, 0o600)
    except OSError:
        pass
    return {"private.pem": priv_p, "public.pem": pub_p}


def write_registry(key_dir: str, active_version: str,
                   valid_versions: list, retired: list = ()) -> str:
    p = os.path.join(key_dir, "registry.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"active_version": active_version,
                   "valid_versions": sorted(set(valid_versions)),
                   "retired": sorted(retired)}, f, indent=2)
    return p


def load_registry(key_dir: str) -> dict:
    p = os.path.join(key_dir, "registry.json")
    if not os.path.exists(p):
        raise EnvelopeUnavailable("key registry missing: %s" % p)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:   # 损坏/不可读 → fail-closed
        raise EnvelopeUnavailable("key registry unreadable/corrupt: %s" % e)


def public_key_pem(key_dir: str, key_version: str, registry: dict | None = None) -> str:
    """按注册表取公钥 PEM；未知/退役版本 → EnvelopeUnavailable（拒）。"""
    reg = registry or load_registry(key_dir)
    if key_version not in reg.get("valid_versions", []):
        raise EnvelopeUnavailable(
            "key_version %s not valid (retired=%s)" % (key_version, reg.get("retired")))
    p = os.path.join(key_dir, "v%s" % key_version, "public.pem")
    if not os.path.exists(p):
        raise EnvelopeUnavailable("public key missing: %s" % p)
    with open(p, encoding="utf-8") as f:
        return f.read()


def private_key_path(key_dir: str, key_version: str) -> str:
    p = os.path.join(key_dir, "v%s" % key_version, "private.pem")
    if not os.path.exists(p):
        raise EnvelopeUnavailable("private key missing: %s" % p)
    return p


# ---------------------------------------------------------------- 签名 / 验证

def build_and_sign(op_id: str, task_id: str, intent_key: str, fencing: int,
                   worker_id: str, business_target_norm: str,
                   key_dir: str, key_version: str | None = None) -> dict:
    """构造并签名 envelope（Gate 唯一签发口）。

    返回完整 envelope dict（含 envelope_digest + signature）。
    依赖缺失 → EnvelopeUnavailable（调用方必须 fail-closed 禁止派发）。
    """
    from cryptography.hazmat.primitives import serialization

    Ed25519PrivateKey = _ed25519()
    reg = load_registry(key_dir)
    kv = key_version or reg.get("active_version")
    if not kv:
        raise EnvelopeUnavailable("no active key_version in registry")
    fields = {
        "version": PROTOCOL_VERSION,
        "algorithm": ALGORITHM,
        "key_version": str(kv),
        "op_id": op_id,
        "task_id": task_id,
        "intent_key": intent_key,
        "fencing": str(int(fencing)),
        "worker_id": worker_id,
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "business_target_norm": business_target_norm or "",
    }
    raw = canonical_bytes(fields)
    digest = hashlib.sha256(raw).hexdigest()
    with open(private_key_path(key_dir, str(kv)), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise EnvelopeUnavailable("private key is not Ed25519")
    sig = key.sign(raw)
    env = dict(fields)
    env["envelope_digest"] = digest
    env["signature"] = sig.hex()
    return env


def verify_envelope(env: dict, public_pem: str) -> dict:
    """Python 侧镜像验签（审计/测试用；worker 权威验签在 Node 侧）。

    所有异常默认拒绝：返回 {"ok": bool, "code": SP-OP-001, "reason": str}。
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        if not isinstance(env, dict):
            return _rej("envelope not a dict")
        if env.get("version") != PROTOCOL_VERSION:
            return _rej("version mismatch")
        if env.get("algorithm") != ALGORITHM:
            return _rej("algorithm mismatch")
        sig_h = env.get("signature")
        if not isinstance(sig_h, str) or len(sig_h) != SIGNATURE_HEX_LEN \
                or any(c not in "0123456789abcdef" for c in sig_h):
            return _rej("signature length/format")
        sig = bytes.fromhex(sig_h)
        unknown = set(env) - set(PROTECTED_FIELDS) - {"envelope_digest", "signature"}
        if unknown:
            return _rej("unknown fields: %s" % sorted(unknown))
        try:
            raw = canonical_bytes({k: env[k] for k in PROTECTED_FIELDS})
        except ValueError as e:
            return _rej("canonical: %s" % e)
        if env.get("envelope_digest") != hashlib.sha256(raw).hexdigest():
            return _rej("digest mismatch")
        try:
            pub = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        except Exception:
            return _rej("public key unparseable")
        if not isinstance(pub, Ed25519PublicKey):
            return _rej("public key is not Ed25519")
        try:
            pub.verify(sig, raw)
        except InvalidSignature:
            return _rej("signature verify failed")
        return {"ok": True, "code": "", "reason": ""}
    except Exception as e:  # 所有异常默认拒绝
        return _rej("exception: %s" % str(e)[:120])


def _rej(reason: str) -> dict:
    return {"ok": False, "code": SP.SP_OP_001, "reason": reason}


# ---------------------------------------------------------------- 信任边界
# P2-2-4 闭环项：公钥注册表不可被普通执行路径修改；异常/损坏/缺失 fail-closed。
def check_registry_trust(key_dir: str) -> dict:
    """注册表信任边界检查（SecurityPlane 启动自检调用）。

    检查项（任一失败 → EnvelopeUnavailable，绝不放行）：
    1. registry.json 存在、可解析、字段类型/取值合法（版本冲突即拒）；
    2. registry 与全部公钥文件不得是符号链接（防路径替换）；
    3. POSIX：registry/私钥目录不得对 group/other 可写（部署 ACL 兜底 Windows）；
    4. 每个 valid_version 的公钥文件存在且可解析为 Ed25519 SPKI；
    5. 私钥不接受 group/other 可读（POSIX）。
    """
    reg = load_registry(key_dir)  # 缺失 → EnvelopeUnavailable
    if not isinstance(reg, dict):
        raise EnvelopeUnavailable("registry not an object")
    valid = reg.get("valid_versions")
    retired = reg.get("retired", [])
    active = reg.get("active_version")
    if not isinstance(valid, list) or not all(isinstance(v, str) for v in valid):
        raise EnvelopeUnavailable("registry.valid_versions invalid")
    if not isinstance(retired, list):
        raise EnvelopeUnavailable("registry.retired invalid")
    both = set(valid) & set(retired)
    if both:
        raise EnvelopeUnavailable("registry version conflict: %s" % sorted(both))
    if not isinstance(active, str) or active not in valid:
        raise EnvelopeUnavailable("registry.active_version not in valid_versions")
    rp = os.path.join(key_dir, "registry.json")
    paths = [rp] + [os.path.join(key_dir, "v%s" % v, "public.pem") for v in valid]
    for p in paths:
        if os.path.islink(p):
            raise EnvelopeUnavailable("symlink not allowed: %s" % p)
        if not os.path.exists(p):
            raise EnvelopeUnavailable("missing file: %s" % p)
        if os.name == "posix" and (os.stat(p).st_mode & 0o022):
            raise EnvelopeUnavailable("group/other writable: %s" % p)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    for v in valid:
        with open(os.path.join(key_dir, "v%s" % v, "public.pem"),
                  encoding="utf-8") as f:
            pem = f.read()
        try:
            pub = serialization.load_pem_public_key(pem.encode("utf-8"))
        except Exception as e:
            raise EnvelopeUnavailable("public key unparseable v%s: %s" % (v, e))
        if not isinstance(pub, Ed25519PublicKey):
            raise EnvelopeUnavailable("public key not Ed25519 v%s" % v)
        priv = os.path.join(key_dir, "v%s" % v, "private.pem")
        if os.path.exists(priv) and os.name == "posix" and \
                (os.stat(priv).st_mode & 0o044):
            raise EnvelopeUnavailable("private key group/other readable: %s" % priv)
    return {"ok": True, "versions": valid, "active": active}
