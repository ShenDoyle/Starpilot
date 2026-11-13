# egolite 传输模式（2026-11-12 起，本机默认）

> 传输取值：`STARPILOT_TRANSPORT` env > config 的 `"transport"` 键 > bsk 兜底。
> 本机已经 `python scripts/env.py --set-transport egolite` 写入 config——页面动作走
> op 队列，由 ego-browser（ego lite）驱动。不装 bsk、不装浏览器扩展。
> 协议与 iab 传输完全一致，只换 worker 与运行方式。

## A. 为什么有这条模式

- bsk 需要 CLI + 浏览器扩展 + cooperate 协议，是三个依赖里最重的一条；
- ego-browser 本身就是「给人和 agent 共用」的 Chromium，登录态自持，
  `ego-browser nodejs` 的 TaskSpace 脚本能动态 `import("node:fs")`——
  意味着 worker 可以**以后台进程常驻**，修掉 IAB「agent 必须持续手动跑 worker、
  wait 期间 op 超时」的固有节奏。

## B. 启用与运行

1. 确认 `ego-browser` CLI 可用：`python scripts/env.py --json` 看
   `ego_browser: {ok, path}`；找不到就按 ego-browser skill 的
   `references/install-windows.md` 安装。
2. 在 ego-browser 里**登录目标平台一次**（登录态由 ego-browser 自持）。
3. 正常跑 `wa.py` 任意命令——首次触达页面动作时 wa.py 会自动生成引导文件
   `<工作区>/runtime/wa/egolite-launch.mjs`（幂等重写，工作区移动后自动更新）。
4. 后台启动 worker（一条命令，TaskSpace 固定名 `starpilot`）：

   ```bash
   ego-browser nodejs -e "await import('file:///<工作区>/runtime/wa/egolite-launch.mjs')"
   ```

   - 单轮常驻 10 分钟（`maxMs`）自动优雅退出；wait 长于 10 分钟就重跑同一条命令。
   - op 队列超时（rc=124）的第一反应就是：worker 没在跑 → 重新后台跑上面这条。
   - TaskSpace 跨轮持久（按名字复用），重跑不丢页面状态。

## C. 协议（与 iab 共用）

- 目录：`<工作区>/runtime/wa/iab-ops/`（历史命名，两个 op 队列传输共用）。
- 请求 `<id>.req.json`：`{"id","action","params"}`；结果 `<id>.res.json`：
  `{"ok","data","error"}`。
- 动作（`scripts/transport.py` `parse_bsk_args` 翻译）：`navigate` / `evaluate` /
  `fill` / `press` / `noop`（session 管理类命令映射 noop，tab/browsers/doctor 等
  在 op 队列传输下按健康空值处理）。
- worker：`scripts/egolite-worker.mjs`。动作映射：`page.goto` +
  domcontentloaded（DeepSeek SPA 挂载修复同 IAB 实测逻辑）、`page.evaluate`、
  `page.fill`、`page.press`。

## D. 门控行为（与 bsk 传输的差异面）

| 机制 | egolite 下的行为 |
|---|---|
| 写闸门四步（演练/批准/真发/核验） | **不变**，完全生效 |
| 对话归属门禁、地址漂移守卫、幂等拒重发 | **不变**，完全生效 |
| state 写锁、fail_streak、审计 journal | **不变** |
| 窗口纪律（单窗口/开窗限速/窗口归属验证） | TaskSpace 托管语义替代，不适用（同 IAB） |
| `session` / `tab` 生命周期 | 无概念，按健康空值处理（同 IAB） |
| 平台占用锁、分诊阶梯 | 不变（`triage` 照常可用） |

## E. 已知限制（诚实申报）

- 沿用 iab 的实验性标注（协议同源）：op 队列/路由/导航/写入/发送链路已实测跑通，
  但「goto 对话 URL 后内容区挂载」曾于 2026-10-31 晚间实测失效（msgs=0，判定平台侧
  行为，bsk 同命中）。egolite worker 内置了同款「侧栏点击强制挂载」修复，但
  **切平台前先跑一轮只读验证**（navigate + evaluate probe msgs>0）。
- `ego-browser nodejs` 不继承 shell 环境变量 → 工作区根由引导文件写死
  （wa.py 幂等重写，不需要手动同步）。
- 组合键（如 Ctrl+A）走 `page.press("body", ...)`，复杂组合的兼容性未全量实测；
  星舵发送路径主要是 Enter/单键，未受影响。
- DeepSeek 平台侧异常时段（页面不挂载）egolite 与 bsk 一样无解——那是平台问题，
  不是传输层问题（0.1.5 已判定过）。

## F. 版本锚点与特性清单评估（R1，2026-11-12 用户裁定：只记录不拦）

### F.1 锚点表（每次实测新版本后回填）

| 星舵版本 | 实测 ego CLI | 实测 guide | 日期 | 结论 |
|---|---|---|---|---|
| v0.1.7.1+（egolite 传输首发） | 0.6.2.10 | v2.2.0 | 2026-11-12 | 全链实测通过（op 往返/初始化） |
| v1.8.0 | 0.6.3.14 | v2.2.1 | 2026-11-14 | §F 评估通过：API 清单全在（guide 仅文档小版本），op 队列冒烟 evaluate/navigate/fill/press 正常（fill 拒非可编辑元素为正确语义），无感使用回填 |

锚点常量在 `scripts/env.py`（`EGO_ANCHOR` / `EGO_MIN` / `EGO_ANCHOR_GUIDE`），
`env.py --json` 与 `update_check.py` 据此给 status（ok / older / newer / unknown）。

### F.2 星舵依赖的 ego 特性清单（评估的比对基准）

worker（`egolite-worker.mjs`）与 wa.py 只依赖以下 ego 面：

1. `ego-browser nodejs` 能跑脚本：`-e` 一行入口 + `await import('file:///…')`
   动态导入本地模块；脚本内 `await import("node:fs")` 可用；
2. `taskSpace(name)` 按名字持久复用（星舵固定名 `starpilot`）；`task.page("p1")`、
   `page.url()`；
3. Page 动作：`page.goto(url,{timeout})`、`page.waitForLoadState`、
   `page.waitForTimeout`、`page.evaluate(js)`（返回字符串/可 JSON 化值）、
   `page.fill(selector, value, {timeout})`、`page.press(selector, key, {timeout})`、
   `page.keyboard.press`；
4. `ego-browser --version` 首行含版本号（env/update_check 解析依赖）；
5. `ego-browser skill` 输出含 guide 版本行（agent 读版本用）。

### F.3 评估流程（检测到 newer 时执行，结论进回执 + lessons）

1. 跑 `ego-browser skill`，对照 F.2 逐条确认 API 仍在、签名未变（改名/移除/参数
   语义变化都算「破坏性」）；
2. 用 scratch 工作区做一次 op 队列冒烟：手写一条 `evaluate` op + 跑 worker +
   验 `.res.json` 往返（不碰业务对话）；
3. **结论分两档**（用户裁定）：
   - **变化巨大**（F.2 任一条不成立）：提示用户「ego 新版本可能导致星舵流程异常，
     **请暂时不要使用无人值守模式**」；记 lessons（blocking=true 的 open 不拦 close）；
     锚点不回填，等星舵适配；
   - **无实质变化**：无感使用；回填 `EGO_ANCHOR` 与 F.1 锚点表（注明星舵版本）。
4. 评估本身**不阻塞当前会话**——先如实告知，再继续。
