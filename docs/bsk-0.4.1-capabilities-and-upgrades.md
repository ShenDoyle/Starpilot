# bsk 0.4.1（2026-11-02 更新）能力对照与星舵优化

> 实测日期：2026-11-03 | CLI 版本 0.4.1（bsk.bin.exe 2026-11-02 18:07，备份 bak-0.4.0-20261102）
> 探测方式：`bsk --help` + 各子命令 `--help` 全量对比；未触碰业务 daemon（PID 32036）与活 session（difg）。

## 一、0.4.1 新能力清单（实测确认）

| 能力 | 命令 | 说明 |
|---|---|---|
| **session 任务名归属** | `session start --name` | 任务名进 bsk 本地操作历史（`session list` 仍不显示，归属显示仍是缺口） |
| **可恢复请求令牌** | `session start --request-id` | `<expiry-unix-ms>:<UUID>`，≤10 分钟有效 |
| **隐身 Agent Window** | `session start --incognito` | 需扩展 Allow in incognito |
| **窗口尺寸/焦点控制** | `session start --width/--height/--focus/--no-focus` | no-focus 为默认 |
| **原子动作原语** | `step --action --params --expect --policy` | 一动作+一条件+同步状态，**never retried**；`--expect` 目标事实前置校验 |
| **目标控制策略** | `cooperate/step --policy auto/strict/target` | `--depends-on` 降级为 legacy |
| **显式观察 ID** | `--observation` | 视觉/无目标动作必须显式给，禁止从缓存推断 |
| **观测落盘离线读** | `observe` + `view <file>` | VOM 语义观测保存后本地可读（历史证据，不再碰浏览器） |
| **原生对话框决策** | `dialog status/…` + `operation` | alert/confirm/prompt/beforeunload 可查可决；`operation` 可取回/取消被对话框挂起的原始操作 |
| **human-in-loop** | `request-help --prompt` | 官方人工介入通道（captcha/登录/确认） |
| **语义录制/回放** | `record start` → trace.json | 用户动作录成 LLM 教科书 |
| **任务录屏** | `video start` → H.264 MP4 | 审计证据形态 |
| **结构化提取** | `extract discover/table/list` | 带列定义与来源元数据 |
| **移动模拟** | `emulate` | viewport/UA/touch |
| **深度调试** | `debug performance/requests/rules/replay/export/operations/…` | 请求规则、replay、**被挂起操作检查** |
| **远程浏览器配对** | `daemon pair / devices / revoke` | 一次性配对链接接入远程浏览器 |
| **窗口管理** | `window resize` | Agent Window 尺寸调整 |
| **CLI 自更新** | `bsk update --check / --no-restart-daemon` | 注意 update 默认会重启 daemon |

## 二、星舵优化落地（本轮已做，592 测试全过）

### 1. L2 shim 对齐仓库版（修复审计缺陷）✅

- **缺陷**（GPT v6 审计标记）：安装位 `~/.local/bin/bsk_shim.py` 是旧 fail-open 版（101 行/4087B，认可伪造的 `STARPILOT_WINDOW_GATE=1` 布尔量，无票据兑换，shim 故障时放行）。
- **修复**：`tools/install_l2_shim.py` 重装 → 安装位与仓库版逐字节一致（127 行/5672B，HMAC 一次性票据 `gate_redeem` 兑换后即焚，fail-closed）。
- **验证**：`tests/test_bsk_shim.py` 18 用例全过（TestShimFailClosed 语义）；`bsk --version` 直通自检 rc=0。

### 2. session start 注入任务名（回应「session 无归属」审计缺口）✅

- `wa.py` 开窗处新增 `--name starpilot-MMDD-HHMMSS`，并落 `state.session_name`。
- 旧版兼容：失败且 `unexpected argument` 时自动去掉 `--name` 降级重试。
- 局限如实记录：`session list` 仍**不**显示 name，归属可追溯性依赖 bsk 本地操作历史，BRM 的账本级归属（P1-A brm.db）仍是正解。

### 3. 回归验证 ✅

- `unittest discover tests`：**592 tests OK**（75.7s），含 shim/bskcompat/winlease/tabclaims/orchestrator 全链。

## 三、建议后续采用的新能力（按价值排序，未实施）

| 优先 | 能力 | 用在哪 | 理由 |
|---|---|---|---|
| 高 | `view <file>` + `observe` 落盘 | wa.py `read`/`triage` 增加证据落盘 | GPT 审计一贯要求证据链；观测 JSON 落 `runtime/wa/obs/` 即为只读证据，且离线可复核 |
| 高 | `step --expect` | act.py 触页动作 | 「动作+前置事实校验+同步状态」原子化，never retried，直接减少 observed-changed 竞态补救 |
| 高 | `dialog` | takeover/收窗前检查 | beforeunload 挂起是收窗失败常见因；`dialog status` 先查再关，`operation` 可取消挂起操作 |
| 中 | `request-help` | wa.py 卡点提示 | 官方 human-in-loop 通道替代口头指引 |
| 中 | `video` | P1-B/T1-T4 实测取证 | REAL_PASS/REAL_FAIL 附 MP4 证据 |
| 中 | `extract` | 京东采集/老浦家 SKU 场景 | 比 evaluate 手写 JS 稳，带来源元数据 |
| 低 | `--policy target` | 逐步替换默认 auto | 显式目标控制强化归属模型；需先摸清 target 语义 |
| 低 | `emulate` / `record` | 移动端场景 / 技能沉淀 | 按需 |

## 四、对 BRM 设计的影响（需报 GPT 裁定，代码未动）

1. **`daemon pair/devices/revoke`（远程浏览器配对）改变了「跨主机接管为长期边界」的前提**——官方已提供跨设备浏览器接入通道。BRM v7 C 方案的边界陈述、P1-A 的资源账本设计（远程浏览器实例如何入账、pair 授权如何映射 owner）需要补一节评估。**在 GPT 裁定前不启用 pair。**
2. **`step` + `--expect` 是官方原子性原语**：P1-B 测试设计的动作竞态用例（T3）可增加「官方 step 原语 vs 自研序列」对照组，可能简化 fencing 设计的论证。
3. **`--observation` 禁止从缓存推断**：与 BRM「不复读缓存、状态以实测为准」原则同构，可作为设计佐证引用。
4. **`session list` 归属字段仍缺失**（本轮实测确认）：P1-A brm.db 作为统一账本的必要性不变。
5. **`bsk update` 默认重启 daemon**：星舵升级 CLI 时必须带 `--no-restart-daemon` 或择业务空闲时窗，写入手册。

## 五、纪律声明

- 未操作业务 daemon（PID 32036）/活 session（difg）/业务窗口与标签页
- 未执行 `bsk update`（仅 `--check`，且因网络失败，无副作用）
- 未提交、未升版、未推送（git 冻结维持）；改动=安装位 shim 对齐 + wa.py 一处增强 + 本文档
