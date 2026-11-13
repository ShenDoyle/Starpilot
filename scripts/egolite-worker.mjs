// egolite-worker.mjs —— 消费星舵 op 队列，驱动 ego-browser（ego lite）。
//
// 与 iab-worker.mjs 消费同一协议（runtime/wa/iab-ops/ 的 <id>.req.json/.res.json），
// 区别只在驱动：ego-browser 的 TaskSpace API，且**可后台常驻**——
// `ego-browser nodejs` 每轮是新进程，但 TaskSpace 按名字跨轮持久，
// 因此 wa.py 生成的引导文件（runtime/wa/egolite-launch.mjs）以固定名字复用空间。
//
// 用法（正常由 wa.py 自动生成引导文件，这里是人肉运行的等价形式）：
//   ego-browser nodejs -e "await import('file:///<工作区>/runtime/wa/egolite-launch.mjs')"
//
// 支持动作：navigate / evaluate / fill / press / noop（协议见 scripts/transport.py）。
// 页面选择：优先当前活动标签页；没有就 newPage（TaskSpace 初始即有 p1）。
// 首次使用需在 ego-browser 里登录目标平台一次（登录态由 ego-browser 自己持有）。

const OPS_DIR_DEFAULT = "runtime/wa/iab-ops";   // 历史命名，iab/egolite 共用协议与目录
const SPACE_NAME = "starpilot";                 // 固定名：taskSpace(名字) 复用同一空间

function joinPath(root, ...parts) {
  return [String(root).replace(/\/+$/, ""), ...parts].join("/");
}

// DeepSeek 等 SPA 直接 goto 对话 URL 时内容区可能不挂载（只渲染侧栏）——
// 检测到消息区为空就点击侧栏会话项强制挂载（2026-10-31 IAB 实测，ego 同页面同源）。
async function forceMountConv(page, url) {
  if (!/\/a\/chat\/s\//.test(url || "") && !/\/chat\//.test(url || "")) return;
  try {
    const probe = JSON.parse(await page.evaluate(
      "JSON.stringify({msgs: document.querySelectorAll('div.ds-message').length})"));
    if (probe.msgs) return;
    const sid = (url.match(/[0-9a-f-]{16,}/) || [""])[0];
    if (!sid) return;
    await page.evaluate(`(() => {
      const a = document.querySelector('a[href*="${sid}"]');
      if (a) { a.click(); return "clicked"; }
      return "not-found";
    })()`);
    await page.waitForTimeout(2500);
  } catch {}
}

async function oneOp(fs, path, task, page, root) {
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
    const p = op.params || {};
    switch (op.action) {
      case "navigate": {
        await page.goto(p.url, { timeout: 30000 });
        await page.waitForLoadState("domcontentloaded").catch(() => {});
        await page.waitForTimeout(1500);
        await forceMountConv(page, p.url);
        finish(true, { url: String(await page.url()) });
        break;
      }
      case "evaluate": {
        const r = await page.evaluate(p.js);
        finish(true, typeof r === "string" ? r : JSON.stringify(r));
        break;
      }
      case "fill": {
        await page.fill(p.selector, p.value ?? "", { timeout: 10000 });
        finish(true, { filled: p.selector });
        break;
      }
      case "press": {
        const key = String(p.key || "");
        if (p.selector) {
          await page.press(p.selector, key, { timeout: 10000 });
        } else {
          // 组合键：编辑器焦点态下按 key（首键；星舵发送路径主要是 Enter/单键）
          await page.press("body", key, { timeout: 10000 }).catch(async () => {
            for (const k of key.split("+")) await page.keyboard.press(k.trim());
          });
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

// root 由调用方（引导文件）显式传入——ego-browser 脚本不继承 shell 环境变量。
export async function run(root, opts = {}) {
  const fs = await import("node:fs");
  const maxMs = opts.maxMs || 600000;   // 默认常驻 10 分钟；到点优雅退出，超时重跑即可
  const idleMs = opts.idleMs || 5000;
  const d = joinPath(root, OPS_DIR_DEFAULT);
  if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
  const task = await taskSpace(SPACE_NAME);
  let page = task.page("p1");
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
      const r = await oneOp(fs, joinPath(d, f), task, page, root);
      processed++;
      if (!r.ok) failed++;
    }
  }
  return { processed, failed, spaceId: task.spaceId };
}

// 直接以 node 执行时的说明（正常用法是 ego-browser nodejs 跑引导文件）
if (import.meta.url === `file://${process.argv[1]}`) {
  console.log("用法：ego-browser nodejs -e \"await import('file:///<工作区>/runtime/wa/egolite-launch.mjs')\"");
}
