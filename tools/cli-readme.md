# 星舵 CLI

这是星舵的**命令行部分**，不依赖 AI 客户端的 skill 机制。完整版（含 `SKILL.md` 那套给 agent 的行为约束）见 [Starpilot 主仓库](https://github.com/ShenDoyle/Starpilot)。

没有 `SKILL.md` 意味着：编排、闸门、状态机、经验库都在，但「agent 该怎么向网页 AI 汇报」这类规矩得你自己守（或者让随便哪个 agent 读主仓库的 `SKILL.md`）。

## 装什么

| 项 | 说明 |
| --- | --- |
| Python 3.9+ | 仅标准库，没有任何第三方包 |
| [bsk](https://github.com/Tencent/BrowserSkill) | 浏览器层：CLI + Chrome/Edge 扩展，用你已经登录的浏览器干活 |

先跑一遍自检，缺什么它会说：

```bash
python scripts/env.py            # 人读报告
python scripts/env.py --json     # 机器读
python scripts/env.py --install  # 缺 bsk 时自动装
```

## 三个入口

```bash
python scripts/env.py --json     # 环境：Python / bsk / 扩展开关
python scripts/wa.py platforms   # 桥接层：绑定、读回复、发消息、等完成
python scripts/orchestrator.py --help   # 编排层：状态机、闸门、收口
```

`orchestrator.py` 是主入口，一轮完整流程大致是：

```bash
python scripts/orchestrator.py init --goal "<目标>" --stage O1 --card docs/requirements/x.md
python scripts/orchestrator.py bind --reuse
python scripts/orchestrator.py plan --file runtime/wa/turn-1.md
python scripts/orchestrator.py loop --once --file runtime/wa/feedback-1.md   # 默认只演练
python scripts/orchestrator.py approve --approval <id> --ttl 900
python scripts/orchestrator.py loop --once                                   # 批准后才真发
python scripts/orchestrator.py close --stage O1 --require-lessons
```

## 目录

```
scripts/         11 个纯标准库脚本（编排、桥接、闸门、风险分级、锁、升级）
references/      平台接入、DOM 探针、经验库、流程章程
templates/       需求卡片、确认单、反馈、阶段报告
docs/            状态机契约（orchestrator-contract.md）——改行为先改它
tests/           292 条单测，纯标准库
```

## 自测

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests
```

## 改行为之前

状态机、判据口径、闸门语义都冻结在 `docs/orchestrator-contract.md`。改代码不改契约，下一个接手的人（或下一轮的你）一定会踩到不一致。

平台适配写在 `scripts/platforms.py`，新增平台的六步流程见 `references/web-ai-platforms.md`。

## 许可

MIT © 2026 左立方（TOPcbm）
