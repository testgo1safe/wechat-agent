# -*- coding: utf-8 -*-
"""自动回复的纯逻辑层：配置、风险接管、节奏与气泡归一化。

本模块不依赖微信 Hook，便于离线测试，也避免把“真人感”逻辑散落在主循环里。
"""

import hashlib
import math
import re
import time
import unicodedata
from difflib import SequenceMatcher


DEFAULT_SMART_REPLY = {
    "enabled": True,
    "two_stage": True,
    "learn_recent_style": False,
    "dynamic_timing": True,
    "settle_min": 3.0,
    "settle_max": 9.0,
    "typing_min": 1.0,
    "typing_max": 9.0,
    "typing_chars_per_second": 7.0,
    "max_bubbles": 2,
    "inter_bubble_min": 0.8,
    "inter_bubble_max": 2.2,
    "handoff_on_risk": True,
    "handoff_keywords": [],
    "dedupe_enabled": True,
    "dedupe_window_seconds": 600,
    "dedupe_similarity": 0.88,
    "dedupe_min_chars": 6,
    "regenerate_on_duplicate": True,
    "grounding_guard_enabled": True,
    "suppress_unprompted_followups": True,
}

_DEFAULT_HANDOFF_PATTERNS = (
    ("转账/付款", re.compile(
        r"转账|付款|收款码|银行卡|支付宝|微信支付|借(?:我|你)?钱|打(?:款|钱)")),
    ("验证码/隐私", re.compile(
        r"验证码|支付密码|登录密码|身份证(?:号)?|银行卡号|家庭住址")),
    ("行程承诺", re.compile(
        r"几点(?:见|到)|什么时候(?:见|有空|过来)|"
        r"能不能(?:来|到|参加)|确认(?:下)?时间|帮我答应")),
    ("冲突/法律", re.compile(
        r"报警|起诉|律师函|投诉你|赔偿|追责")),
)

_LENGTHS = {"very_short", "short", "normal", "detailed"}
_ACTIONS = {"reply", "skip", "handoff"}


def _num(value, default, lo, hi):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    return max(float(lo), min(float(hi), value))


def parse_smart_reply_config(cfg):
    """解析 smart_reply，缺省即启用；旧配置无需迁移。"""
    out = dict(DEFAULT_SMART_REPLY)
    raw = (cfg or {}).get("smart_reply")
    if isinstance(raw, dict):
        for key in out:
            if key in raw and raw[key] is not None:
                out[key] = raw[key]

    out["enabled"] = bool(out.get("enabled"))
    out["two_stage"] = bool(out.get("two_stage"))
    out["learn_recent_style"] = bool(out.get("learn_recent_style"))
    out["dynamic_timing"] = bool(out.get("dynamic_timing"))
    out["handoff_on_risk"] = bool(out.get("handoff_on_risk"))
    out["dedupe_enabled"] = bool(out.get("dedupe_enabled"))
    out["regenerate_on_duplicate"] = bool(
        out.get("regenerate_on_duplicate"))
    out["grounding_guard_enabled"] = bool(
        out.get("grounding_guard_enabled"))
    out["suppress_unprompted_followups"] = bool(
        out.get("suppress_unprompted_followups"))
    out["settle_min"] = _num(out.get("settle_min"), 3, 1, 30)
    out["settle_max"] = _num(out.get("settle_max"), 9, 1, 60)
    if out["settle_max"] < out["settle_min"]:
        out["settle_min"], out["settle_max"] = (
            out["settle_max"], out["settle_min"])
    out["typing_min"] = _num(out.get("typing_min"), 1, 0, 20)
    out["typing_max"] = _num(out.get("typing_max"), 9, 0, 60)
    if out["typing_max"] < out["typing_min"]:
        out["typing_min"], out["typing_max"] = (
            out["typing_max"], out["typing_min"])
    out["typing_chars_per_second"] = _num(
        out.get("typing_chars_per_second"), 7, 2, 30)
    try:
        out["max_bubbles"] = max(1, min(3, int(out.get("max_bubbles") or 2)))
    except (TypeError, ValueError):
        out["max_bubbles"] = 2
    out["inter_bubble_min"] = _num(
        out.get("inter_bubble_min"), 0.8, 0, 10)
    out["inter_bubble_max"] = _num(
        out.get("inter_bubble_max"), 2.2, 0, 15)
    if out["inter_bubble_max"] < out["inter_bubble_min"]:
        out["inter_bubble_min"], out["inter_bubble_max"] = (
            out["inter_bubble_max"], out["inter_bubble_min"])
    kws = out.get("handoff_keywords")
    out["handoff_keywords"] = [
        str(x).strip() for x in (kws if isinstance(kws, list) else [])
        if str(x).strip()
    ][:50]
    out["dedupe_window_seconds"] = int(_num(
        out.get("dedupe_window_seconds"), 600, 30, 86400))
    out["dedupe_similarity"] = _num(
        out.get("dedupe_similarity"), 0.88, 0.70, 1.0)
    try:
        out["dedupe_min_chars"] = max(
            2, min(30, int(out.get("dedupe_min_chars") or 6)))
    except (TypeError, ValueError):
        out["dedupe_min_chars"] = 6
    return out


def _stable_fraction(seed):
    raw = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big") / float(2 ** 64 - 1)


def stable_range(seed, lo, hi):
    lo, hi = float(lo), float(hi)
    if hi <= lo:
        return lo
    return lo + (hi - lo) * _stable_fraction(seed)


def relative_time_label(timestamp, now=None):
    """把消息时间压缩成适合注入上下文的相对标签。"""
    try:
        ts = int(timestamp or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return ""
    now = int(now or time.time())
    delta = max(0, now - ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{max(1, delta // 60)}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    if delta < 172800:
        return "昨天"
    if delta < 7 * 86400:
        return f"{delta // 86400}天前"
    return time.strftime("%m月%d日", time.localtime(ts))


def settle_delay(text, seed, smart_cfg, group=False):
    """等待对方连发的窗口。短消息稍短、长消息稍长，并保持可复现抖动。"""
    cfg = smart_cfg or DEFAULT_SMART_REPLY
    if not cfg.get("enabled") or not cfg.get("dynamic_timing"):
        return None
    lo = float(cfg.get("settle_min", 3))
    hi = float(cfg.get("settle_max", 9))
    length = len((text or "").strip())
    # 长消息更可能仍在补充；群聊略多等一会儿，降低插话概率。
    weight = min(1.0, math.log2(max(2, length + 1)) / 7.0)
    center = lo + (hi - lo) * (0.25 + 0.55 * weight)
    if group:
        center += 0.8
    spread = max(0.6, (hi - lo) * 0.18)
    return max(lo, min(hi, stable_range(seed, center - spread, center + spread)))


def typing_delay(bubbles, seed, smart_cfg, generation_seconds=0.0):
    """按回复长度估算“组织文字”总时长，并扣除模型已经消耗的时间。"""
    cfg = smart_cfg or DEFAULT_SMART_REPLY
    if not cfg.get("enabled") or not cfg.get("dynamic_timing"):
        return 0.0
    chars = sum(len(x) for x in (bubbles or []) if x)
    cps = float(cfg.get("typing_chars_per_second", 7))
    base = chars / max(2.0, cps)
    jitter = stable_range(
        f"typing:{seed}",
        float(cfg.get("typing_min", 1)),
        min(float(cfg.get("typing_max", 9)), base + 3.0),
    )
    target = max(base, jitter)
    target = max(float(cfg.get("typing_min", 1)),
                 min(float(cfg.get("typing_max", 9)), target))
    return max(0.0, target - max(0.0, float(generation_seconds or 0)))


def inter_bubble_delay(seed, index, smart_cfg):
    cfg = smart_cfg or DEFAULT_SMART_REPLY
    return stable_range(
        f"bubble:{seed}:{index}",
        float(cfg.get("inter_bubble_min", 0.8)),
        float(cfg.get("inter_bubble_max", 2.2)),
    )


def detect_handoff_reason(text, smart_cfg):
    cfg = smart_cfg or DEFAULT_SMART_REPLY
    if not cfg.get("enabled") or not cfg.get("handoff_on_risk"):
        return ""
    value = (text or "").strip()
    if not value:
        return ""
    for label, pattern in _DEFAULT_HANDOFF_PATTERNS:
        if pattern.search(value):
            return label
    for word in cfg.get("handoff_keywords") or []:
        if word in value:
            return f"自定义关键词:{word}"
    return ""


def normalize_reply_text(text):
    """用于防重比较：统一全半角、大小写并去掉空白和标点。"""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(
        ch for ch in value
        if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "S"))
    )


_IMAGE_REFUSAL_RE = re.compile(
    r"(?:没法|没办法|无法|不能|不可以|不方便|不太方便|不合适|不太合适|"
    r"不答应|不发|发不了|不给(?:你)?发|拒绝|别想|算了吧|有点犯规|"
    r"太私人|涉及隐私|隐私照片)"
)
_IMAGE_REFUSAL_FALSE_POSITIVES = re.compile(
    r"(?:不是|并非|也不是)(?:不能|不可以|不方便|没法|没办法)|"
    r"(?:不能不发|没有不方便)"
)


def image_reply_refuses_action(bubbles):
    """最终文字明确拒绝时，禁止同时执行发图动作。

    决策和文案由两个模型阶段产生，文案偶尔会推翻前一阶段。这里是在真实
    生图/发送前执行的确定性一致性闸门，避免出现“嘴上拒绝、实际发图”。
    """
    if isinstance(bubbles, str):
        text = bubbles
    else:
        text = "\n".join(str(x or "") for x in (bubbles or []))
    text = _IMAGE_REFUSAL_FALSE_POSITIVES.sub("", text.strip())
    return bool(text and _IMAGE_REFUSAL_RE.search(text))


def align_image_reply_outcome(plan, image_generated):
    """让配文服从实际生图结果，而不是让配文反过来阻止生图。"""
    result = dict(plan or {})
    bubbles = normalize_bubbles(
        result.get("bubbles") or [result.get("reply") or ""],
        max_bubbles=2,
    )
    refuses = image_reply_refuses_action(bubbles)
    if image_generated:
        if refuses or not bubbles:
            bubbles = ["给你看一张～"]
            result["image_text_rewritten"] = "generated_success"
        result["send_image"] = True
    else:
        if not refuses:
            bubbles = ["这张没办法发出来哦"]
            result["image_text_rewritten"] = "generated_failed"
        result["send_image"] = False
        result["image_prompt"] = ""
    result["bubbles"] = bubbles
    result["reply"] = "\n".join(bubbles)
    return result


def reply_similarity(left, right):
    a = normalize_reply_text(left)
    b = normalize_reply_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def find_duplicate_reply(bubbles, recent_replies, smart_cfg, now=None):
    """返回最相似的近期回复信息；未达到阈值返回 None。"""
    cfg = smart_cfg or DEFAULT_SMART_REPLY
    if not cfg.get("dedupe_enabled"):
        return None
    now = int(now or time.time())
    window = int(cfg.get("dedupe_window_seconds") or 600)
    threshold = float(cfg.get("dedupe_similarity") or 0.88)
    min_chars = int(cfg.get("dedupe_min_chars") or 6)
    recent = []
    for item in recent_replies or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "")
            try:
                ts = int(item.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0
        else:
            text, ts = str(item or ""), 0
        if not text:
            continue
        if ts and now - ts > window:
            continue
        recent.append((text, ts))

    best = None
    for bubble in bubbles or []:
        normalized = normalize_reply_text(bubble)
        if len(normalized) < min_chars:
            continue
        for old_text, old_ts in recent:
            old_normalized = normalize_reply_text(old_text)
            if len(old_normalized) < min_chars:
                continue
            score = reply_similarity(bubble, old_text)
            if score >= threshold and (
                    best is None or score > best["similarity"]):
                best = {
                    "new_text": str(bubble),
                    "old_text": old_text,
                    "old_ts": old_ts,
                    "similarity": score,
                }
    return best


_QUESTION_END_RE = re.compile(
    r"(?:[？?]|吗|么|呢|不|没|咋|怎么|怎样|如何|哪(?:个|里)?|谁|啥|"
    r"多少|有没有|能不能|可不可以|行不行)[。！!…~～]*$")


def _looks_like_question(text):
    value = str(text or "").strip()
    return bool(value and _QUESTION_END_RE.search(value))


def _sentence_chunks(text):
    return [
        x.strip() for x in re.findall(r"[^。！？!?]+[。！？!?]?", str(text or ""))
        if x.strip()
    ]


def trim_unprompted_followups(bubbles, decision, smart_cfg):
    """移除回答后模型自行追加的尾部追问，避免为了“接话”补剧情。

    只有决策阶段明确标记 needs_clarification 时才保留追问。
    返回 (清理后的气泡, 被移除的文本列表)。
    """
    cfg = smart_cfg or DEFAULT_SMART_REPLY
    values = normalize_bubbles(
        bubbles, max_bubbles=int(cfg.get("max_bubbles") or 2))
    if (not cfg.get("grounding_guard_enabled")
            or not cfg.get("suppress_unprompted_followups")
            or bool((decision or {}).get("needs_clarification"))):
        return values, []

    removed = []
    # 独立的末尾问句气泡直接去掉，但至少保留前面的有效回答。
    while len(values) > 1 and _looks_like_question(values[-1]):
        removed.insert(0, values.pop())

    if not values:
        return values, removed
    chunks = _sentence_chunks(values[-1])
    while len(chunks) > 1 and _looks_like_question(chunks[-1]):
        removed.insert(0, chunks.pop())
    if chunks:
        values[-1] = "".join(chunks).strip()
    values = [x for x in values if x]
    return values, removed


def normalize_decision(obj):
    obj = obj if isinstance(obj, dict) else {}
    action = str(obj.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        action = "reply"
    length = str(obj.get("length") or "short").strip().lower()
    if length not in _LENGTHS:
        length = "short"
    try:
        confidence = float(obj.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    try:
        bubble_count = int(obj.get("bubble_count") or 1)
    except (TypeError, ValueError):
        bubble_count = 1
    return {
        "action": action,
        "intent": str(obj.get("intent") or "").strip()[:80],
        "tone": str(obj.get("tone") or "自然").strip()[:40],
        "length": length,
        "bubble_count": max(1, min(2, bubble_count)),
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(obj.get("reason") or "").strip()[:120],
        "needs_clarification": bool(obj.get("needs_clarification")),
        "send_image": bool(obj.get("send_image")),
        "image_prompt": str(obj.get("image_prompt") or "").strip()[:200],
    }


def _clean_bubble(text):
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json|text)?\s*|\s*```$", "", value, flags=re.I)
    value = re.sub(r"^(?:回复|我回复|assistant)\s*[：:]\s*", "", value, flags=re.I)
    value = value.strip().strip('"').strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def normalize_bubbles(values, max_bubbles=2):
    """清理模型气泡；去空、去重并限制数量，但不粗暴截断正文。"""
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, (list, tuple)):
        raw = list(values)
    else:
        raw = []
    out = []
    for item in raw:
        value = _clean_bubble(item)
        if not value or value in out:
            continue
        out.append(value)
        if len(out) >= max(1, int(max_bubbles or 1)):
            break
    return out
