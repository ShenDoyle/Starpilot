"""越界请求的风险分级——不是「一律拒绝」。

用户裁定（2026-10-30）：越界请求要先分析它本身是否安全无害——
  · 安全且无害 → **通过**，只留痕，不打断任务；
  · 风险较大  → 不放这条过，但**给替代方案**，让项目继续往前跑（"先完成项目"），
               除非它是那种一旦放过就会污染数据/发错对话的硬风险。
所以三级：
  low    allow=True   放行（附 human 说明 + 留痕）
  medium allow=False  换方案继续（alternative 必填；任务不中断）
  high   allow=False  必须人工/重新绑定（alternative 必填；不许猜）

判据必须是**保守可复算**的纯函数：同样的入参永远同样的结论，
不依赖时间/随机/网络，便于单测与事后追责。
"""

# 每类越界的「换方案」建议：medium/high 必带，否则等于把决策成本丢回给 agent
ALTERNATIVES = {
    "url_drift_same_conv": "URL 只是查询参数变了、对话本体没变：直接沿用（state 里的 url 会被更新）",
    "url_drift_other_conv": "页面已不在原对话：先 wa.py read 确认对方是谁；确实要换就重新 bind --reuse-existing",
    "url_drift_cross_origin": "跨站了：立刻回到绑定地址；确需换平台就 bind 新对话，不要在同一个 run 里跨站写",
    "platform_unverified": "改用已验证平台（推荐顺序 Kimi > ChatGPT > DeepSeek，再 Gemini/豆包）；"
                           "或只跑 dry-run 出计划、不动真实页面",
    "write_method": "按平台 write_allowed 的写入路径（exec / paste）；不要临时换路径硬试",
    "guard": "先 wa.py probe：有弹窗先关掉（限流/确认框），输入框不干净先 wa.py clear，再重试",
    "duplicate": "确认平台是否已收到（probe 的 reply_count）；要重发就换一条独立表述，别原样重发",
    "prompt_drift": "重新 dry-run 一次生成新计划，让批准跟着新正文走；不要复用旧批准硬发",
    "platform_mismatch": "计划是给另一个平台做的：回到原平台继续，或对新平台重新 dry-run",
}


def _norm(u: str) -> tuple:
    """把 URL 拆成 (origin, 对话本体, query) —— 只看结构，不猜语义。"""
    from urllib.parse import urlsplit
    try:
        s = urlsplit(u or "")
    except Exception:
        return "", "", ""
    origin = "%s://%s" % ((s.scheme or "").lower(), (s.netloc or "").lower())
    return origin, (s.path or "").rstrip("/"), s.query or ""


# reason 是机器码（回溯/统计用）；人看到的是这句中文——别拿码去糊人。
REASON_HUMAN = {
    "url_drift_same_conv": "还是同一条对话，只是地址尾巴上的参数变了",
    "url_drift_other_conv": "页面已经跳到了另一条对话",
    "url_drift_cross_origin": "页面跨站了，已经不在绑定的那个平台",
    "platform_unverified": "这个平台还没实测过写入路径",
    "write_method": "这条写入路径不在该平台已实测的白名单里",
    "guard": "页面当前状态不允许写（有弹窗挡着 / 输入框不干净 / 不能发送）",
    "duplicate": "这条内容已经发过一次了",
    "prompt_drift": "正文和计划里的不一样（被改过）",
    "platform_mismatch": "计划是给另一个平台做的",
}


def _result(level: str, allow: bool, reason: str, alternative: str = "") -> dict:
    what = REASON_HUMAN.get(reason) or reason
    return {
        "level": level,
        "allow": allow,
        "reason": reason,
        # 人话：给不看代码的人（用户/别的 agent）一句能懂的解释
        "human": {
            "low": "这条不危险，直接放行：%s" % what,
            "medium": "有点风险，这次不做，换个做法继续：%s" % what,
            "high": "风险较高，这次不能做，需要重新确认：%s" % what,
        }.get(level, what),
        "alternative": alternative or ALTERNATIVES.get(reason, ""),
    }


def classify_url_drift(bound_url: str, current_url: str) -> dict:
    """地址漂移分级：同一个对话换参数 ≠ 换对话 ≠ 跨站。"""
    bo, bp, _ = _norm(bound_url)
    co, cp, _ = _norm(current_url)
    if not bo or not co:
        return _result("high", False, "url_drift_cross_origin",
                       ALTERNATIVES["url_drift_cross_origin"])
    if bo != co:
        return _result("high", False, "url_drift_cross_origin",
                       ALTERNATIVES["url_drift_cross_origin"])
    if bp == cp:
        # 同站同路径，只有 query/hash 变化：页面本体没变（Kimi 实测会带 ?chat_enter_method=home）
        return _result("low", True, "url_drift_same_conv",
                       ALTERNATIVES["url_drift_same_conv"])
    return _result("high", False, "url_drift_other_conv",
                   ALTERNATIVES["url_drift_other_conv"])


def classify(kind: str, **ctx) -> dict:
    """统一入口。kind 与 write_gate 的拦截点一一对应。"""
    if kind == "url_drift":
        return classify_url_drift(ctx.get("bound_url") or "", ctx.get("current_url") or "")
    if kind == "platform_unverified":
        return _result("medium", False, "platform_unverified",
                       ALTERNATIVES["platform_unverified"])
    if kind == "write_method":
        return _result("high", False, "write_method", ALTERNATIVES["write_method"])
    if kind == "guard":
        return _result("medium", False, "guard", ALTERNATIVES["guard"])
    if kind == "duplicate":
        return _result("medium", False, "duplicate", ALTERNATIVES["duplicate"])
    if kind == "prompt_drift":
        return _result("high", False, "prompt_drift", ALTERNATIVES["prompt_drift"])
    if kind == "platform_mismatch":
        return _result("high", False, "platform_mismatch", ALTERNATIVES["platform_mismatch"])
    return _result("high", False, kind)
