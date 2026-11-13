# -*- coding: utf-8 -*-
"""tests 包引导：把传输层钉在 bsk 基线。

运行时传输取值是 env > config > bsk 兜底；本机 config 已选 egolite
（2026-11-12 裁定）。但单测套件是**传输无关**的：绝大多数用例在 bsk 语义下
编写（mock wa.bsk / 参数翻译断言），若跟着机器配置走 op 队列，页面动作会
掉进 60 秒轮询（等一个不存在的 worker），表现为整套测试「挂死」。

所以测试进程内统一钉回 bsk。注意 discover 必须以包模式跑（-t 指到仓库根），
否则本文件不执行：

    python -m unittest discover -s tests -t .

egolite 专属用例自己显式切换 env，跑完必须还原成 bsk（见 test_pending_gate.TestTransport）。
"""
import os

os.environ["STARPILOT_TRANSPORT"] = "bsk"
