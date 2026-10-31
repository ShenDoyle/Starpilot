// iab-worker.mjs —— 消费星舵 IAB 传输层的 op 队列，驱动 ZCode 自带浏览器。
//
// 用法（在 node REPL / mcp__node_repl__js 中，每次调用都是新内核，需重新 bootstrap）：
//   const { setupBrowserRuntime } = await import(<plugin>/scripts/browser-client.mjs);
//   await setupBrowserRuntime({ globals: globalThis });
//   const { run } = await import("<skill>/scripts/iab-worker.mjs");
//   await run("<工作区根目录>", { agent });   // agent 必须显式传入（VM 跨 realm）
//
// 支持动作：navigate / evaluate / fill / press / noop。
// 页面操作全部通过 agent.browsers.get("iab") 的 Playwright/cua API（isTrusted）。

const OPS_DIR_DEFAULT = "runtime/wa/iab-ops";

function joinPath(root, ...parts) {
  // node:path 在调用方 bootstrap 后可用；这里用 URL/file 解析由调用方保证
  return [root.replace(/\/+$/, ""), ...parts].join("/");
}

async function oneOp(fs, path, browser, root) {
  const raw = fs.readFileSync(path, "utf-8");
  let op;
  try { op = JSON.parse(raw); } catch (e) {
    return { id: "?", ok: false, error: "bad req json: " + e.message };
  }
  const d = joinPath(root, OPS_DIR_DEFAULT);
  const finish = (ok, data, error) => {
    fs.writeFileSync(`${d}/${op.id}.res.json`,
      JSON.stringify({ ok, data, error: error || "" }), "utf-8");
    try { fs.unlinkSync(path); } catch {}
  };
  try {
    const tabs = await browser.tabs.list();
    let tab;
    if (tabs.length) tab = await browser.tabs.get(tabs[0].id);
    else { tab = await browser.tabs.new(); }
    const p = op.params || {};
    switch (op.action) {
      case "navigate": {
        await tab.goto(p.url);
        await tab.playwright.waitForLoadState({ state: "domcontentloaded" });
        await tab.playwright.waitForTimeout(1500);
        // DeepSeek 等 SPA 直接 goto 对话 URL 时内容区不挂载（只渲染侧栏）——
        // 检测到消息区为空就点击侧栏会话项强制挂载（2026-10-31 实测）
        if (/\/a\/chat\/s\//.test(p.url || "") || /\/chat\//.test(p.url || "")) {
          try {
            const probe = JSON.parse(await tab.playwright.evaluate(
              "JSON.stringify({msgs: document.querySelectorAll('div.ds-message').length})"));
            if (!probe.msgs) {
              const sid = (p.url.match(/[0-9a-f-]{16,}/) || [""])[0];
              if (sid) {
                const link = tab.playwright.locator(`a[href*="${sid}"]`).first();
                if (await link.count()) {
                  // Playwright click 对侧栏项常报不可交互（实测），改坐标点击（isTrusted）
                  try {
                    const r = await link.evaluate("JSON.stringify((() => {const b=this.getBoundingClientRect(); return {x: Math.round(b.x + b.width/2), y: Math.round(b.y + b.height/2)};})())");
                    const c = JSON.parse(r);
                    await tab.cua.click({ x: c.x, y: c.y });
                    await tab.playwright.waitForTimeout(2500);
                  } catch {}
                }
              }
            }
          } catch {}
        }
        finish(true, { url: String(await tab.url()) });
        break;
      }
      case "evaluate": {
        const r = await tab.playwright.evaluate(p.js);
        finish(true, typeof r === "string" ? r : JSON.stringify(r));
        break;
      }
      case "fill": {
        const loc = tab.playwright.locator(p.selector).first();
        await loc.fill(p.value ?? "", { timeout: 10000 });
        finish(true, { filled: p.selector });
        break;
      }
      case "press": {
        const key = String(p.key || "");
        if (p.selector) {
          await tab.playwright.locator(p.selector).first().press(key, { timeout: 10000 });
        } else {
          const combo = key.split("+").map(k => (k.toLowerCase() === "ctrl" ? "Control" : k));
          await tab.cua.keypress({ keys: combo });
        }
        finish(true, { pressed: key });
        break;
      }
      case "noop": {
        finish(true, { noop: true, sub: p.sub || "" });
        break;
      }
      default:
        finish(false, null, "unknown action: " + op.action);
    }
    return { id: op.id, ok: true };
  } catch (e) {
    finish(false, null, String(e && e.message || e).slice(0, 400));
    return { id: op.id, ok: false, error: String(e && e.message || e) };
  }
}

export async function run(root, opts = {}) {
  const fs = await import("node:fs");
  const maxMs = opts.maxMs || 40000;
  const idleMs = opts.idleMs || 2000;
  const d = joinPath(root, OPS_DIR_DEFAULT);
  if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
  // 前提：调用方已在同一 JS 调用里完成官方 bootstrap（setupBrowserRuntime），
  // 并把 agent 绑定**显式传入**（opts.agent = agent）。
  // 为什么不能自己取：node REPL 是 VM 跨 realm 环境，worker 模块内的 globalThis
  // 与宿主内核的不是同一个对象（实测 globalThis.agent 在模块内为 undefined，
  // 而重复 setup 会让桥接失效 "bridge is unavailable"）。
  const agent = opts.agent;
  if (!agent?.browsers) {
    throw new Error("opts.agent 缺失：调用方须先 bootstrap 再传 agent（见 SKILL §5.1 示例）");
  }
  const browser = await agent.browsers.get("iab");
  const t0 = Date.now();
  let processed = 0, failed = 0;
  let lastBusy = Date.now();
  while (Date.now() - t0 < maxMs) {
    const reqs = fs.readdirSync(d).filter(f => f.endsWith(".req.json")).sort();
    if (!reqs.length) {
      if (Date.now() - lastBusy >= idleMs) break;
      await new Promise(r => setTimeout(r, 300));
      continue;
    }
    lastBusy = Date.now();
    for (const f of reqs) {
      const r = await oneOp(fs, joinPath(d, f), browser, root);
      processed++;
      if (!r.ok) failed++;
    }
  }
  return { processed, failed };
}

// 直接以 node 执行时的说明（正常用法是在 node REPL 里 import run）
if (import.meta.url === `file://${process.argv[1]}`) {
  console.log("用法：在 ZCode node REPL 中 import { run } 并传入工作区根目录。");
}
