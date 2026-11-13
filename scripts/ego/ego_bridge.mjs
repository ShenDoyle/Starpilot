// ego_bridge.mjs —— Ego 原生桥接器（唯一允许接触 Ego SDK 的 Node 侧文件）。
//
// 设计（PROJECT-03-R2 任务书 §3）：
// - Python ego_backend.py 通过 `ego-browser nodejs -e "import(<bridge>)"` 拉起
//   本文件（每次调用全新 Node 进程，符合 TaskSpace 跨进程模型）；
// - op 规范来自环境变量 EGO_BRIDGE_SPEC（JSON 文件路径），结果写
//   EGO_BRIDGE_OUT（JSON 文件）+ stdout 摘要；
// - 任何异常都落盘为 {ok:false, error:…}，绝不静默吞掉；
// - SDK 原生对象（task/page）只存在于本进程内，返回给 Python 的是纯 JSON。
//
// op 一览（能力声明见 ego_backend.py CAPABILITIES，二者一一对应）：
//   list_spaces / create_space / space_probe / page_identity / goto /
//   fill / click / press / snapshot / read_text / hand_off / wait_control /
//   finish_space / page_exists
//
// 路径注入：ego-browser 的 nodejs 运行器不透传自定义环境变量（实测
// ENOENT open ''），所以由 Python 生成的临时 wrapper 先设
// globalThis.EGO_BRIDGE_SPEC / EGO_BRIDGE_OUT 再 import 本文件。
import fs from "node:fs";

const SPEC_PATH = globalThis.EGO_BRIDGE_SPEC || process.env.EGO_BRIDGE_SPEC || "";
const OUT_PATH = globalThis.EGO_BRIDGE_OUT || process.env.EGO_BRIDGE_OUT || "";

const rec = { ok: false, op: null, data: null, error: null,
              at: new Date().toISOString(),
              sdk: { channel: "ego-browser nodejs", version: "0.6.2.10" } };
const save = () => {
  try { fs.writeFileSync(OUT_PATH, JSON.stringify(rec, null, 1), "utf8"); } catch {}
};
process.on("exit", () => save());

try {
  const spec = JSON.parse(fs.readFileSync(SPEC_PATH, "utf8"));
  rec.op = spec.op;
  const out = await runOp(spec);
  rec.ok = true;
  rec.data = out;
} catch (e) {
  rec.error = String(e && e.message || e).slice(0, 800);
  rec.errorRaw = String(e).slice(0, 800);
}
save();
console.log(JSON.stringify({ ok: rec.ok, op: rec.op, error: rec.error }));
process.exit(0);

// ------------------------------------------------------------------ ops

// 页面指纹（dom_changed 分层证据：url/title/bodyLen 前后比对）
async function pageFingerprint(page) {
  return page.evaluate(() => ({
    url: location.href,
    title: document.title,
    bodyLen: (document.body?.innerText || "").length,
  }));
}
function fpChanged(a, b) {
  if (!a || !b) return null;
  return a.url !== b.url || a.title !== b.title || a.bodyLen !== b.bodyLen;
}

async function runOp(spec) {
  const a = spec.args || {};
  // R3-C：input_mode 二选一 —— "trusted"=SDK 真实输入管线（R3-B B-PASS 实测，
  // isTrusted=true）；"dom_synthesized"=evaluate DOM 合成（R2 实现，保留）。
  // 两种模式在返回值里如实标注，绝不互相冒充。
  const im = a.input_mode === "trusted" ? "trusted" : "dom_synthesized";
  switch (spec.op) {
    case "list_spaces": {
      const spaces = await listTaskSpaces();
      return { spaces: spaces.map(normalizeSpace) };
    }
    case "create_space": {
      const task = await taskSpace(a.name);
      return { space: normalizeSpace(task), };
    }
    case "space_probe": {
      // 真实全局 ownership：listTaskSpaces 全局列表为准（01-R1 F7 证据：
      // task.ownership 可能是陈旧快照，绝不作为执行权判据）
      // 实测：全局列表条目的原始字段是 `id`（非 spaceId）——匹配必须
      // 同时兼容两种字段名，否则 find 永不命中 → 假 not_found → 误 fail-closed
      const spaces = await listTaskSpaces();
      const hit = spaces.find(s => String(s.spaceId ?? s.id) === String(a.spaceId) ||
                                   s.name === a.spaceId);
      if (!hit) return { found: false, spaces: spaces.map(normalizeSpace) };
      const task = await taskSpace(a.spaceId);
      return { found: true, space: normalizeSpace(task),
               global: normalizeSpace(hit) };
    }
    case "page_exists": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      return { exists: !!page, label: page ? page.label : null };
    }
    case "page_identity": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      const obs = await page.evaluate(collectIdentity);
      return { identity: obs };
    }
    case "page_viewport": {
      // R3-B：页面渲染状态只读探针（viewport 0×0 = 窗口不在屏幕渲染，
      // 真实输入 API 将超时；2026-11-13 实测根因）
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      return await page.evaluate(() => ({
        innerW: window.innerWidth, innerH: window.innerHeight,
        vis: document.visibilityState,
      }));
    }
    case "goto": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      await page.goto(a.url);
      await page.waitForTimeout(a.waitMs || 2500);
      const obs = await page.evaluate(collectIdentity);
      return { identity: obs };
    }
    case "fill": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      if (im === "trusted") {
        // R3-C trusted 路径：SDK page.fill()（guide v2.2.0 一级 API，真实
        // 文本输入管线）。R3-B localhost 夹具实测产生 isTrusted=true 的
        // input 事件、受控组件状态真实同步。dom_changed 以填写后重读为准。
        const before = await pageFingerprint(page);
        await page.fill(a.selector, a.value);
        const set = await page.evaluate(([sel, val]) => {
          const el = document.querySelector(sel);
          return el ? el.value : null;
        }, [a.selector, a.value]);
        const after = await pageFingerprint(page);
        const obs = await page.evaluate(collectIdentity);
        return { identity: obs, filled: a.value, applied: set,
                 input_mode: "trusted",
                 dom_changed: String(set) === String(a.value) || fpChanged(before, after) };
      }
      // dom_synthesized 路径（R2 保留）：evaluate DOM 合成
      const set = await page.evaluate(([sel, val]) => {
        const el = document.querySelector(sel);
        if (!el) throw new Error("fill target not found: " + sel);
        el.focus();
        el.value = val;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return el.value;
      }, [a.selector, a.value]);
      const obs = await page.evaluate(collectIdentity);
      return { identity: obs, filled: a.value, applied: set,
               input_mode: "dom-synthesized",
               dom_changed: String(set) === String(a.value) };
    }
    case "click": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      const before = await pageFingerprint(page);
      if (im === "trusted") {
        // R3-C trusted 路径：SDK page.click() 真实指针管线（R3-B 实测
        // isTrusted=true）。dom_changed 以 url/title/bodyLen 前后变化为准。
        await page.click(a.selector);
        await page.waitForTimeout(a.waitMs || 700);
      } else {
        // dom_synthesized 路径（R2 保留）：evaluate 合成 el.click()
        await page.evaluate((sel) => {
          const el = document.querySelector(sel);
          if (!el) throw new Error("click target not found: " + sel);
          el.scrollIntoView?.();
          el.click();
        }, a.selector);
        await page.waitForTimeout(a.waitMs || 700);
      }
      const after = await pageFingerprint(page);
      const obs = await page.evaluate(collectIdentity);
      return { identity: obs, clicked: a.selector,
               input_mode: im === "trusted" ? "trusted" : "dom-synthesized",
               dom_changed: fpChanged(before, after) };
    }
    case "press": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      if (im === "trusted") {
        // R3-C trusted 路径：SDK page.press() 真实键盘管线（R3-B 实测
        // keydown isTrusted=true）。
        await page.press(a.selector || "body", a.key);
        const obs = await page.evaluate(collectIdentity);
        return { identity: obs, pressed: a.key,
                 input_mode: "trusted",
                 dom_changed: null };
      }
      // dom_synthesized 路径（R2 保留）：合成 KeyboardEvent
      const handled = await page.evaluate(([sel, key]) => {
        const el = sel ? document.querySelector(sel) : document.activeElement;
        if (!el) throw new Error("press target not found: " + sel);
        el.focus?.();
        const opts = { key, bubbles: true, cancelable: true };
        el.dispatchEvent(new KeyboardEvent("keydown", opts));
        el.dispatchEvent(new KeyboardEvent("keypress", opts));
        el.dispatchEvent(new KeyboardEvent("keyup", opts));
        return el.tagName;
      }, [a.selector || null, a.key]);
      const obs = await page.evaluate(collectIdentity);
      return { identity: obs, pressed: a.key, target: handled,
               input_mode: "dom-synthesized" };
    }
    case "read_text": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      const text = await page.evaluate(
        () => (document.body?.innerText || "").slice(0, 4000));
      return { text };
    }
    case "snapshot": {
      const task = await taskSpace(a.spaceId);
      const page = task.page(a.label);
      // SDK 签名：{ scope?, root?, includeActionMarks?, includeStableLocator? }
      // —— 不接受 format（实测报 unknown option）
      const snap = await page.snapshot({});
      return { snapshot: typeof snap === "string" ? snap.slice(0, 4000) : JSON.stringify(snap).slice(0, 4000) };
    }
    case "hand_off": {
      const task = await taskSpace(a.spaceId);
      await task.handOff();
      const spaces = await listTaskSpaces();
      // 同 space_probe：条目原始字段是 `id`，需兼容
      const hit = spaces.find(s => String(s.spaceId ?? s.id) === String(a.spaceId));
      return { handedOff: true, globalAfter: hit ? normalizeSpace(hit) : null };
    }
    case "wait_control": {
      const task = await taskSpace(a.spaceId);
      const r = await task.waitForControl({ timeoutMs: a.timeoutMs || 10000 });
      return { waited: true, raw: r == null ? null : String(r).slice(0, 300) };
    }
    case "finish_space": {
      // 仅限一次性隔离测试空间；keep 语义按 guide v2.2.0
      const task = await taskSpace(a.spaceId);
      await task.finish({ keep: a.keep || [] });
      return { finished: true, keep: a.keep || [] };
    }
    case "reclaim_space": {
      // R3-E 资源自清理（2026-11-13 用户铁律：测试用例结束必须自清理，
      // 不给桌面留「等你接管」的窗口）。仅限夹具自建的一次性空间；
      // 业务流禁用（CAPABILITIES 声明 requires_execution_gate）。
      // 委托态 → takeOverTaskSpace；user 态 → claimTaskSpace。
      // ⚠️ 非托管标签（openedBy:"unknown"）会阻止销毁（finish 返回
      // closedSpace:false + preservedUnmanagedCount>0，2026-11-13 实测），
      // 必须先收编关闭全部标签再 finish，并校验 closedSpace:true。
      const id = a.spaceId;
      let task;
      try { task = await takeOverTaskSpace(id); }
      catch (e1) { task = await claimTaskSpace(id); }
      const tabs = await task.tabs();
      const closed = [];
      for (const tb of tabs) {
        try {
          const pg = tb.label ? task.page(tb.label) : await task.adopt(tb.page);
          await pg.close();
          closed.push(tb.label || "adopted-unmanaged");
        } catch (e) { closed.push("skip:" + String(e && e.message || e).slice(0, 80)); }
      }
      const r = await task.finish({ keep: [] });
      return { reclaimed: true, tabs_closed: closed, finish: r,
               closed_space: !!(r && r.closedSpace === true) };
    }
    default:
      throw new Error("unknown op: " + spec.op);
  }
}

function collectIdentity() {
  return {
    url: location.href,
    title: document.title,
    marker: (document.getElementById("m")?.innerText || "").slice(0, 120),
    timeOrigin: performance.timeOrigin,
    readyState: document.readyState,
    bodyLen: (document.body?.innerText || "").length,
    hasEditorHint: !!document.querySelector("input,button"),
  };
}

function normalizeSpace(s) {
  // 只挑 JSON 安全字段，不泄漏 SDK 对象
  return {
    spaceId: s.spaceId !== undefined ? s.spaceId : (s.id !== undefined ? s.id : null),
    name: s.name !== undefined ? s.name : null,
    ownership: s.ownership !== undefined ? s.ownership : null,
    rawKeys: Object.keys(s).slice(0, 20),
  };
}
