# 更新日志

版本号只从 `SKILL.md` front-matter 读。对外版本从 `0.1.0` 起算；开发期的内部编号（→ 2.3.4）是当时的迭代序号，不对外。

## 0.1.0 — 2026-10-30 · 首次公开发布（预览版）

还不是正式版。第一个打包对外发的版本，能力已经能跑完整项目，接口还会动。

**发布物**

| 包 | 内容 | 给谁用 |
| --- | --- | --- |
| `starpilot-skill-0.1.0.zip` | 完整 skill：`SKILL.md` + `scripts/` + `references/` + `templates/` + `docs/` + `assets/` + `tests/` | 装进 AI 客户端的 skills 目录，让 agent 按 SKILL 的规矩干活 |
| `starpilot-cli-0.1.0.zip` | 完整 CLI：`scripts/` + `references/` + `templates/` + 契约 + `tests/`，不含 `SKILL.md` | 只要命令行，自己写调度脚本 / cron / 别的 agent 调用 |

两个包功能不缺：CLI 包里的脚本本来就是纯标准库，`SKILL.md` 只是给 agent 的指令，少了它命令行照样跑全。

**能力**

- 六环循环：需求建立 → 规划 → 拆步 → 执行 → 如实回报 → 收口，每环有产物模板
- 14 态状态机 + 18 个 orchestrator 子命令；写操作走四步闸门（演练 → 批准 → 真发 → 写后核验），崩溃恢复只复核、绝不重发
- 三种自愈：失败两次 `replan` 换方案、对话漂移 `rebind` 换对话、新歧义 `decide` 留痕；真正需要停下的只剩四种
- 判定「网页 AI 回完了」靠 DOM 锚点（消息 ID > 正文指纹 > 计数），判据 `effective_delta == 1`，不靠截图
- 5 个平台实测定型（DeepSeek / ChatGPT / Kimi / Gemini / 豆包）；新平台跑一次 `probe` 标定即可接入
- `env.py` 环境自举：查 Python、找 bsk、看扩展开关，缺什么装什么
- 268 条单元测试，纯标准库，无第三方依赖

**已知缺口**：升级包没有签名和哈希校验；prompt A/B 只做过 2×1 轮；auto 模式没跑过跨天长任务；Claude 平台未标定。完整清单见 `SKILL.md` §8。

**从开发期版本迁过来**：开发期最后编号是 `2.3.4`，公开版从 `0.1.0` 重算，升级器的「远端 > 本地」判据会认为不必升级。用 `python scripts/upgrade.py --to-latest --force` 重装一次即可，`runtime/`、`state/`、`stage/` 不会被覆盖。
