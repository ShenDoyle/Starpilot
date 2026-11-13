// PROJECT-04 P2-2-1：Envelope 验签（Node 侧权威验签，node:crypto 原生 Ed25519）。
//
// 与 scripts/envelope.py 共享同一规范（两端口装同一受保护字段表与序列化规则，
// 唯一签名输入字节序列见 envelope.py docstring / P2-2 设计 §4.1）：
// - 受保护字段 10 个，键名字典序，值全字符串，UTF-8，紧凑分隔符；
// - business_target_norm 允许 ""，其余必须非空；
// - signature/envelope_digest 不参与签名输入；
// - envelope_digest = sha256(canonical bytes) hex；
// - signature = Ed25519 原始 64 字节 hex。
// 所有异常默认拒绝（返回 ok:false + SP-OP-001 原因，绝不 throw 给调用方）。
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const PROTOCOL_VERSION = "1";
export const ALGORITHM = "ed25519";
export const SP_OP_001 = "SP-OP-001";

// 与 envelope.py PROTECTED_FIELDS 逐字一致（字典序）
const PROTECTED_FIELDS = [
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
];
const ALLOW_EMPTY = new Set(["business_target_norm"]);
const ALLOWED_TOP = new Set([...PROTECTED_FIELDS, "envelope_digest", "signature"]);

function rej(reason) {
  return { ok: false, code: SP_OP_001, reason };
}

// 原始 JSON 重复键检测（P2-2-4 闭环项）：JSON.parse 会静默保留最后一个
// 同名键，不能依赖其默认行为。这里对原始文本做一次轻量扫描：跟踪对象/数组
// 嵌套深度与当前对象已见键名；发现同层重复键 → 返回重复键名。
export function findDuplicateKey(rawText) {
  const stack = [];              // 每层：{type:'obj', keys:Set} 或 {type:'arr'}
  let i = 0, n = rawText.length;
  let expectKey = false;         // 在对象内期待键名
  while (i < n) {
    const c = rawText[i];
    if (c === '"') {
      let j = i + 1, out = "";
      while (j < n) {
        if (rawText[j] === "\\") { out += rawText[j + 1] ?? ""; j += 2; continue; }
        if (rawText[j] === '"') break;
        out += rawText[j]; j += 1;
      }
      const str = out;
      // 是否是键：前一个非空白字符是 { 或 , 且当前层是对象
      let k = i - 1;
      while (k >= 0 && /\s/.test(rawText[k])) k -= 1;
      const top = stack[stack.length - 1];
      const prev = k >= 0 ? rawText[k] : "";
      if (top && top.type === "obj" && (prev === "{" || prev === ",") && expectKey) {
        if (top.keys.has(str)) return str;
        top.keys.add(str);
        expectKey = false;
      }
      i = j + 1;
      continue;
    }
    if (c === "{" ) { stack.push({ type: "obj", keys: new Set() }); expectKey = true; i += 1; continue; }
    if (c === "[") { stack.push({ type: "arr" }); expectKey = false; i += 1; continue; }
    if (c === "}" || c === "]") { stack.pop(); expectKey = false; i += 1; continue; }
    if (c === ",") {
      const top = stack[stack.length - 1];
      if (top && top.type === "obj") expectKey = true;
      i += 1; continue;
    }
    i += 1;
  }
  return null;
}

// 规范化字节序列：严格镜像 Python canonical_bytes()
export function canonicalBytes(env) {
  if (!env || typeof env !== "object" || Array.isArray(env)) {
    throw new Error("envelope not an object");
  }
  const obj = {};
  for (const k of PROTECTED_FIELDS) {
    const v = env[k];
    if (typeof v !== "string") throw new Error("field " + k + " must be str");
    if (v.length === 0 && !ALLOW_EMPTY.has(k)) throw new Error("field " + k + " empty");
    obj[k] = v;
  }
  for (const k of Object.keys(env)) {
    if (!ALLOWED_TOP.has(k)) throw new Error("unknown field " + k);
  }
  // JSON.stringify 按插入序输出字符串键 —— obj 已按字典序插入，与 Python sort_keys 对齐
  return Buffer.from(JSON.stringify(obj), "utf8");
}

// 密钥注册表加载：{active_version, valid_versions, retired}；按 key_version 选公钥。
// 未知/退役版本 → 拒（ED06）。worker 只持公钥，永不接触私钥。
export function loadRegistry(keyDir) {
  const p = path.join(keyDir, "registry.json");
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

export function selectPublicKey(keyDir, keyVersion, registry) {
  const reg = registry || loadRegistry(keyDir);
  if (!Array.isArray(reg.valid_versions) || !reg.valid_versions.includes(keyVersion)) {
    throw new Error("key_version " + keyVersion + " not valid (retired=" +
      JSON.stringify(reg.retired || []) + ")");
  }
  const p = path.join(keyDir, "v" + keyVersion, "public.pem");
  return fs.readFileSync(p, "utf8");
}

// 权威验签入口。env: envelope 对象；publicKeyPem: SPKI PEM 字符串。
export function verifyEnvelope(env, publicKeyPem) {
  try {
    if (!env || typeof env !== "object" || Array.isArray(env)) return rej("envelope not an object");
    if (env.version !== PROTOCOL_VERSION) return rej("version mismatch");
    if (env.algorithm !== ALGORITHM) return rej("algorithm mismatch");           // ED05
    const sigHex = env.signature;
    if (typeof sigHex !== "string" || sigHex.length !== 128 ||
        !/^[0-9a-f]+$/.test(sigHex)) return rej("signature length/format");      // ED08
    const sig = Buffer.from(sigHex, "hex");
    let bytes, recomputed;
    try {
      bytes = canonicalBytes(env);                                               // ED07（missing/类型）
      recomputed = crypto.createHash("sha256").update(bytes).digest("hex");
    } catch (e) {
      return rej("canonical: " + String(e.message).slice(0, 100));
    }
    if (env.envelope_digest !== recomputed) return rej("digest mismatch");        // ED04（篡改受保护字段）
    let pub;
    try {
      pub = crypto.createPublicKey(publicKeyPem);
    } catch (e) {
      return rej("public key unparseable");                                      // ED02（错钥格式）
    }
    if (pub.asymmetricKeyType !== "ed25519") return rej("public key is not Ed25519");
    const ok = crypto.verify(null, bytes, pub, sig);
    return ok ? { ok: true, code: "", reason: "" }
              : rej("signature verify failed");                                  // ED02/ED03
  } catch (e) {
    return rej("exception: " + String(e).slice(0, 120));                          // 所有异常默认拒绝
  }
}

// CLI：node envelope_verify.mjs <envelope.json> <key_dir> <result.json>
//   —— 按 envelope.key_version 查注册表选公钥后验签（T-ED-01 / worker 接入路径）。
if (process.argv[1] && process.argv[1].endsWith("envelope_verify.mjs") && process.argv.length >= 5) {
  const [, , envPath, keyDir, outPath] = process.argv;
  const rawText = fs.readFileSync(envPath, "utf8");
  const dupKey = findDuplicateKey(rawText);
  let result;
  if (dupKey !== null) {
    result = rej("duplicate key rejected: " + dupKey);
  } else {
    let env;
    try { env = JSON.parse(rawText); }
    catch (e) { result = rej("json parse: " + String(e).slice(0, 120)); env = null; }
    if (env) {
      try {
        const pem = selectPublicKey(keyDir, env.key_version);
        result = verifyEnvelope(env, pem);
      } catch (e) {
        result = rej("key select: " + String(e).slice(0, 120));
      }
    }
  }
  fs.writeFileSync(outPath, JSON.stringify(result, null, 2), "utf8");
  process.exit(0);
}
