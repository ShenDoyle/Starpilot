// PROJECT-04 P2-2-4R：实际派发边界的 **Ego Node v24 原生 Ed25519 验签**实现。
//
// 由 Python（scripts/envelope_node.py）以 stdin 方式交给 `ego-browser nodejs`
// 执行（worker 实际运行时 v24.22.1，不是系统 node）。实测该运行时**不透传
// 环境变量**，因此配置由 Python 生成一小段 bootstrap 内嵌传入：
//
//   const mod = await import("file:///.../node_verify.mjs");
//   await mod.runNodeVerify({ dir, envPath, keyDir, outPath });
//
// 本函数职责：
//   读取 **实际要交给 backend 的那一份 envelope**（同一文件）→ 原始文本重复键
//   扫描（JSON.parse 之前）→ 按 key_version 取注册表公钥 → 由 node:crypto 做
//   原生 Ed25519 验签 → 写结果文件 + 打印 marker。
// 纪律：任何异常 → ok:false（fail-closed），绝不 throw 给调用方。
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const MARKER = "SP_NODE_VERIFY_RESULT ";

/** 结果输出（同步写文件 + 打印 marker；ego 运行时 console.log 走 stderr）。
 *
 * P2-2-4R 补充核验 ②：结果必须**绑定本次调用**（nonce 一次性随机串 +
 * 本次 envelope 的 digest），防止旧文件/半成品/串号结果被误认成功。
 */
export function emit(result, outPath, bind) {
  result.runtime = process.versions.node;
  result.nonce = (bind && bind.nonce) || "";
  result.digest = (bind && bind.digest) || "";
  if (outPath) {
    try { fs.writeFileSync(outPath, JSON.stringify(result), "utf8"); } catch {}
  }
  try { console.log(MARKER + JSON.stringify(result)); } catch {}
  return result;
}

/** 按 key_version 从注册表取公钥（未知/退役版本 → 抛错 → 上层判拒）。 */
export function publicKeyFor(keyDir, keyVersion) {
  const reg = JSON.parse(fs.readFileSync(path.join(keyDir, "registry.json"), "utf8"));
  if (!Array.isArray(reg.valid_versions) || !reg.valid_versions.includes(keyVersion)) {
    throw new Error("key_version " + keyVersion + " not valid (retired=" +
      JSON.stringify(reg.retired || []) + ")");
  }
  return fs.readFileSync(path.join(keyDir, "v" + keyVersion, "public.pem"), "utf8");
}

/** 权威验签入口。cfg: {dir, envPath, keyDir, outPath, nonce, expectDigest} */
export async function runNodeVerify(cfg) {
  const { dir, envPath, keyDir, outPath, nonce, expectDigest } = cfg || {};
  let result = { ok: false, code: "SP-OP-001", reason: "not started" };
  const bind = { nonce: nonce || "", digest: expectDigest || "" };
  try {
    if (!dir || !envPath || !keyDir || !nonce) {
      throw new Error("missing dir/envPath/keyDir/nonce");
    }
    const url = "file:///" + String(dir).replace(/\\/g, "/").replace(/\/+$/, "") +
                "/envelope_verify.mjs";
    const mod = await import(url);
    const raw = fs.readFileSync(envPath, "utf8");
    const dup = mod.findDuplicateKey(raw);
    if (dup !== null) {
      result = { ok: false, code: "SP-OP-001",
                 reason: "duplicate key rejected: " + dup };
    } else {
      const env = JSON.parse(raw);
      // 结果回写的 digest 取**实际被验签对象**的 digest，供 Python 侧绑定核对
      bind.digest = typeof env.envelope_digest === "string" ? env.envelope_digest : "";
      const pem = publicKeyFor(keyDir, env.key_version);
      result = mod.verifyEnvelope(env, pem);
    }
  } catch (e) {
    result = { ok: false, code: "SP-OP-001",
               reason: "node verify exception: " +
                       String(e && e.message).slice(0, 160) };
  }
  return emit(result, outPath, bind);
}
