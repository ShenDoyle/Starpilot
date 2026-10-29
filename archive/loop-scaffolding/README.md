# 循环脚手架存档

> 这里的脚本是星舵**正式通用化之前**的一次性脚手架，已被 `scripts/platforms.py` + `scripts/wa.py` 取代。
> 保留它们是因为它们记录了当时才想通的实现细节——看得见「演进」比只留结果有用。

| 文件 | 是什么 | 现在归谁管 |
|---|---|---|
| `bridge.py` | 早期单文件桥接器：`send` / `wait` / `status` 三个动作，用剪贴板事件写入输入框 | `platforms.js_fill`（write_method=paste）+ `wa.py send/wait` |
| `send2.py` | bridge 的补丁版：修正「正文区混着 UI 文案」导致的末条回复读错 | `platforms.js_probe` 的 `prefer_marker` / `markers` / `tail_junk` |
| `send3.py` | bridge 的补丁版：修正「回复长度稳定」判定过早成立 | `platforms.js_probe` 的 `last_len` + `wa.py wait` 的双信号判定 |

## 它们暴露的两个关键设计（已被正式版吸收）

1. **剪贴板事件写入优于 execCommand**：对 ProseMirror 这类富文本编辑器，`execCommand('insertText')` 常常只写进去一部分；派发一个带 `text/plain` 的 `paste` 事件更可靠。正式版把它做成平台配置项 `write_method`。
2. **正文区不能直接读**：回复正文周围混着「复制」「重新生成」等操作按钮的字，直接 `innerText` 会把按钮文案算进长度，导致「文本稳定」永远不成立。正式版用「先按标记切正文 → 再切掉尾部 UI 文案」解决。

> **不要直接用这些脚本跑生产。** 它们只适配 ChatGPT 的一套选择器，没有多平台路由、没有防重复发送、没有状态机。
> 平台适配的唯一权威是 `scripts/platforms.py`。
