# 浏览器通路对比：bsk vs Playwright vs agent 内置浏览器

> 结论先行：**星舵的主场景（驱动网页版 AI 平台干活）选 bsk**。Playwright 不是不行，
> 是它的强项不在星舵要解决的问题上，而且要多装一套不轻的依赖。

## 1. 一张表看懂

| | bsk（默认） | Playwright | agent 内置浏览器（builtin） |
|---|---|---|---|
| 是什么 | 本地 CLI + 浏览器扩展，通过 CDP 驱动**用户真实的 Chrome** | 微软出的自动化库，自己**另起一个浏览器内核**（默认 Chromium） | 宿主 agent 自带的浏览器工具，agent 手动按规程操作 |
| **登录态** | ✅ 直接复用你浏览器里的已有登录（平台早登过就用） | ❌ 全新空白环境，每个平台的登录/Cookie 都要单独维护或注入 | ⚠️ 看宿主：多数自带 profile 能保登录，但不受星舵管理 |
| **风控友好度** | ✅ 真实浏览器指纹 + 真实用户环境，网页 AI 平台认它 | ❌ 自动化特征明显（`navigator.webdriver`、CDP 痕迹、headless 特征），Cloudflare/Turnstile 类风控更容易拦 | ⚠️ 介于两者之间 |
| **自动化能力** | 够用：点击/输入/读 DOM/管 tab/Agent Window | ✅ 最强：网络拦截、多上下文、截图、trace、并发实例 | ❌ 最弱：全靠 agent 手动，无状态机 |
| **要装什么** | bsk CLI（单文件，用户级）+ 扩展（Chrome 商店） | **要装**：① `pip install playwright`（或 `npm i playwright`）② `playwright install chromium` 再下约 150–300MB 浏览器内核；换内核（firefox/webkit）还要再下 | 什么都不装 |
| **独立 Agent Window / 不打扰用户** | ✅ 0.4.0 起 | ❌ 它开的是自己的窗口，但要小心别抢焦点 | ⚠️ 视宿主 |
| **星舵配套（状态机/租约收割/平台锁/归属校验）** | ✅ 全接 | ❌ 都没有，得自己造 | ⚠️ 部分设计上兼容（builtin-mode.md） |

## 2. 为什么星舵不把 Playwright 当主通路

1. **登录态是死穴**：星舵驱动的是 ChatGPT / DeepSeek / Kimi / 豆包这类**要登录**的平台。
   bsk 直接用你已登录的真实 Chrome；Playwright 每个平台都要单独搞 storage_state，
   会话一过期就得人工重新注入——自动化收益被登录维护吃掉大半。
2. **风控更凶**：网页 AI 平台普遍挂着 Cloudflare/Turnstile 类防护，对 Playwright 的
   自动化指纹更敏感；bsk 走的是人眼看起来一模一样的真实浏览器。
3. **配套全断**：窗口租约、自动收割、平台占用锁、归属校验、实例唯一性……这些机制
   全部长在 bsk 的 CLI/扩展之上，换 Playwright 等于重写一遍执行层。

## 3. 什么时候 Playwright 是对的选择

- **不需要登录态**的活：公开页面抓取、批量截图、E2E 测试、静态站点验证；
- 需要**网络层控制**：拦截请求、mock 接口、统计页面资源；
- 需要**多实例并发**跑隔离任务（每个 context 独立 Cookie）。

这些场景星舵现在不做；若未来要做，Playwright 可以作为第三后端接入
（env.py 加 `playwright` backend，判断标准与 §0.2 弹窗一致），当前版本未实现。

## 4. 安装成本明细（如果哪天要装 Playwright）

```bash
pip install playwright        # Python 包本体，几 MB
playwright install chromium   # 浏览器内核，约 150–300MB 下载，解压后更大
# 系统依赖缺失时（Linux）还要：playwright install-deps chromium
```

对比 bsk：CLI 单文件 + 扩展商店点一下，无内核下载。
