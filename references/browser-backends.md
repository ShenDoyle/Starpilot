# 浏览器通路对比：ego-browser（默认）vs bsk（备用）vs Playwright vs 内置浏览器

> 结论先行（2026-11-21 依赖政策）：**ego-browser 是唯一默认/首选依赖**。
> bsk 降级为**备用，计划完全移除**；只有用户明确表示「不愿使用 ego」时才允许启用。
> Playwright 等其他工具也只在同样前提下作最后手段。macOS 上 ego 的稳定性更高，
> 不能把「Mac 上想用 bsk」当成合理默认。
>
> 内置浏览器（builtin）仍可作为「什么都不装」的零依赖兜底。

## 1. 一张表看懂

| | ego-browser（默认） | bsk（备用/计划移除） | Playwright | agent 内置浏览器（builtin） |
|---|---|---|---|---|
| 是什么 | ego lite 自带浏览器 + `ego-browser` CLI；星舵经 op 队列 + 后台 worker 驱动 | 本地 CLI + 浏览器扩展，通过 CDP 驱动**用户真实的 Chrome/Edge** | 微软自动化库，自己**另起浏览器内核** | 宿主 agent 自带的浏览器工具，agent 手动按规程操作 |
| **登录态** | ✅ ego 自持登录态（首次在 ego 内登录一次） | ✅ 复用用户浏览器已有登录 | ❌ 全新空白环境，需要单独维护 storage_state | ⚠️ 看宿主：多数能保登录，但不受星舵管理 |
| **风控友好度** | ✅ 真实浏览器环境，实测稳定 | ✅ 真实浏览器指纹 + 用户环境 | ❌ 自动化特征明显，Cloudflare/Turnstile 更易拦 | ⚠️ 介于两者之间 |
| **自动化能力** | ✅ 够用：点击/输入/读 DOM/管标签页，op 队列协议 | ✅ 够用 | ✅ 最强：网络拦截、多上下文、trace、并发 | ❌ 最弱：全靠 agent 手动，无状态机 |
| **要装什么** | ego lite GUI（含 CLI），无扩展 | bsk CLI + 扩展 | ① `pip/npm install playwright` ② `playwright install chromium`（约 150–300MB 内核） | 什么都不装 |
| **窗口/并发** | TaskSpace 托管，无 bsk 式空闲回收 | Agent Window，空闲 5 分钟回收 | 自管窗口/上下文 | 视宿主 |
| **星舵配套** | ✅ 状态机/写闸门/租约收割/平台锁全接 | ✅ 全接（历史实现） | ❌ 需要另写执行层 | ⚠️ 仅手动规程 |

## 2. 依赖政策（为什么这样排）

1. **ego 是既定默认**：2026-11-12 起即为主通路，2026-11-21 再次确认。新环境先装 ego，不再先问 bsk。
2. **bsk 只做兼容**：历史上大量判据、探针、lessons 都基于 bsk 沉淀，保留一段时间便于回退；
   但它不再是推荐项，后续版本计划完全移除。
3. **只有用户拒绝 ego 才降级**：用户明确说「不用 ego」时，才可以在 bsk / 内置浏览器 / Playwright
   之间选择，并把选择写进 config（`--set-transport bsk` / `--set-backend builtin`）留痕。
4. **不要用「已经装了」当理由**：本机同时存在 bsk 和 ego 时，默认仍走 ego。

## 3. 什么时候 bsk 仍可能被用到

- 用户明确拒绝安装/使用 ego；
- 需要复现历史 bsk 专属问题（扩展归属、Agent Window、cooperate 语义）做对照；
- ego 出现阻断性故障且用户接受临时回退（回退后应记录 lessons，并尽快回到 ego）。

## 4. 什么时候 Playwright 是对的选择

- 用户拒绝 ego，且任务**不需要登录态**：公开页面抓取、批量截图、E2E、静态站验证；
- 需要**网络层控制**：拦截请求、mock 接口、统计资源；
- 需要**多实例并发**跑隔离任务。

这些不是星舵默认场景；接入前必须单独说明「另装内核、登录态要自行维护、风控更敏感」的代价。

## 5. 安装成本明细

ego-browser：GUI 安装包 + 应用内 onboarding（导入日常浏览器的登录态），CLI 随安装注册进 PATH。

bsk（仅备用）：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.sh | sh
```

Windows：`irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex`，再启用扩展。

Playwright（仅最后手段）：

```bash
pip install playwright        # 或 npm i playwright
playwright install chromium   # 约 150–300MB
# Linux 缺系统依赖时：playwright install-deps chromium
```
