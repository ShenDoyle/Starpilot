# BRM-P1-risks.md

BRM P1 · 未解决风险、假设与待 Web AI 裁定事项（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

## 1. 未解决风险（按严重度）

| # | 风险 | 影响 | 缓解/状态 |
|---|---|---|---|
| R1 | idle 计时机制未实测：不知道哪些命令刷新计时 | 方案 A-D 的选择悬空；若全不刷新，任何保活形态都不存在，D 为唯一出路 | P1-T1 实验组，方案选择等实测 |
| R2 | daemon 单实例假设未验证 | 独立测试环境形态不定（T0.1）；若 daemon 全局唯一且绑定业务浏览器，T1/T2 实验需另开浏览器实例 | P1-T0 前置 |
| R3 | 「账本 ACTIVE 但 daemon 已回收」5 分钟窗口 | 窗口内写操作被 bsk 拒（fail-closed 安全方向），但任务报错误导 | 任务侧错误映射 not_found→IDLE_EXPIRED（实施轮） |
| R4 | daemon 日志格式/轮换未审计 | takeover 判据 b/c 依赖日志解析 | 状态机把日志证据列为增强项而非必要项 |
| R5 | STOP_UNKNOWN 无解场景 | 异常占用保护可能长期锁住资源 | 接受：宁可占用，不可抢（原则）。人工释放路径=用户手动确认 |
| R6 | 长思考任务频繁被 idle 中断（方案 D 代价） | 任务体验下降 | 动作批化设计（NOT_TESTED）+ 若 T1 证实无副作用刷新命令则升级方案 |
| R7 | 现 winlease 读损坏当「无主」缺口 | 与 fail-closed 裁定冲突 | 已在统一账本设计中修正，实施轮落地 |
| R8 | statelock 旧 PID 判据残留 | 同 v6 缺陷同源 | 实施轮替换为 run 级判据 |
| R9 | 观察期 5m 可能不足覆盖迟到操作 | takeover 后旧操作迟到 | T3 竞态实验提供数据后再定观察期 |
| R10 | shim 边界不完整（绝对路径/其他安装位绕过） | shim 只是提示层（v7 裁定接受），真实强制靠账本+wa.py 单入口 | 记录为长期边界，不再补救 shim |

## 2. 关键假设汇总（跨文档）

| # | 假设 | 标记 | 出处 |
|---|---|---|---|
| A1 | daemon 日志格式稳定 | INFERRED | state-machine §7 |
| A2 | 观察期 5m 足够 | INFERRED | state-machine §7 |
| A3 | not_found 只映射「已回收/已停止」 | INFERRED | state-machine §7 |
| A4 | request 队列本地可靠 | INFERRED | state-machine §7 |
| A5 | DAEMON_UNAVAILABLE 与 not_found 可区分 | 部分 | state-machine §7 / T2.4 |
| S1 | window_id 稳定可得 | REAL_PASS | schema §7 |
| S2 | 双权威不产生 | 设计保证 | schema §7 |
| S3 | SQLite 并发性能足够 | INFERRED | schema §7 |
| — | 真实动作刷新 idle 计时 | NOT_TESTED | architecture §2-C |
| — | 非动作命令刷新 idle 计时 | NOT_TESTED | architecture §2-B |
| — | daemon 重启后 session 不可恢复 | INFERRED | architecture §6 |
| — | request 令牌不持久化 | INFERRED | architecture §6 |
| — | 多 daemon 实例共存 | INFERRED | test-plan T0.1 |

## 3. 待 Web AI 裁定的岔路（architecture §8 汇总）

1. 方案 D 为基线 + C 监测模式待 T1 实测的组合，是否批准？
2. 长思考任务被 idle 中断为常态，是否接受？
3. 统一账本 brm.db 迁移，是否批准先出迁移子设计（仍只读）？
4. takeover 判据 c（心跳超时+回收记录+在途队列空=候选起点）是否接受？
5. P1 实测计划（T0-T4）本身是否批准执行？执行仍需另行放行（本轮禁令含「不启动/停止业务 daemon」，测试用独立 daemon 的启动也在禁令字面内，需明确授权）。

## 4. 明确未做 / 超出本轮范围

- 未写任何实施代码；未改 idle 配置；未启动/停止任何 daemon。
- 未提交/推送/升版/同步安装位（工作区维持 v5.1 冻结现状）。
- Web AI 问答闭环（wait 依赖的消息计数选择器失效）仍未修复——已知问题，未在本轮范围。
- 跨主机接管：长期不支持（v7 裁定）。

## 修订追加（2026-11-03，bsk 0.4.1 语义核验 + 裁定）

| # | 风险 | 状态 | 标记 | 缓解 |
|---|---|---|---|---|
| R-AU1 | BSK_AUTO_UPDATE：daemon 自动升级默认开启（30 分钟检查；源码 update.rs 确认 restart_daemon 默认=true） | 业务 daemon 实测 foreground 模式 + runtime auto_update=false + 用户级持久变量已 off（survey-1103-auto-update.md）；**后台 daemon 形态是否自换版本 UNKNOWN** | CODE_CONFIRMED(本机) / UNKNOWN(其他形态) | 维护窗口以 BSK_AUTO_UPDATE=off 启动（本机已是 off，无需动作）；部署规范纳入 |
| R-AU2 | daemon pair 跨设备接入：授权层双 TTL + 一次性凭证 + revoke_all（authorization.rs 源码确认）；**在途请求 revoke 后行为 UNKNOWN** | 裁定维持不启用 | CODE_CONFIRMED(授权层) / UNKNOWN(在途语义) | 禁用 + P1-A 账本将远程 owner 一律记 OWNER_UNKNOWN |
| R-ST1 | step --expect 原生原子性：CLI 层 9 种动作 never retried 源码确认，**执行端（daemon/扩展层）未实测** | 仅批准为 T3 设计对照组 | CODE_CONFIRMED(CLI) / NOT_TESTED(执行端) | T3 对照实测（未授权，待排期） |
| R-UP1 | bsk update 默认重启 daemon | 源码证实 restart_daemon 默认 true；--no-restart-daemon 参数真实存在 | CODE_CONFIRMED | 运维红线：活跃 session 禁默认 update；升级必须 --no-restart-daemon 或空闲窗 |
| R-ID1 | request-id（10 分钟）与 BRM lease 有效期混用风险 | 设计红线：request-id 仅为创建期幂等凭证，不进 ownership/fencing 证明链 | 设计约束 | 见 brm-db-migration.md §10 |
| R-SEL1 | ChatGPT 计数选择器失效 → ESC 循环累积（32 条已核验） | 修复设计已交付（testing/reply-count-selector-redesign.md）：新 DOM 契约实测（消息 UUID data-chatgpt-search-message-ids / selection-message-id；fallback-turn-N 编号不稳定不可作身份） | NOT_TESTED(设计待验收) | 5/5 独立问答验收后 verified 翻真；期间逃生门继续逐条报备 |
