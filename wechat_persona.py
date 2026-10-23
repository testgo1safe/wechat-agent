# -*- coding: utf-8 -*-
"""按会话自定义回复人格与风格画像。"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent

BASE_REPLY_RULES = (
    "\n\n【回复规则·必须遵守】"
    "你正在代「我」用微信聊天。role=user 是对方，role=assistant 是我；"
    "按时间排列，最后一条 user 才是当前要处理的话。"
    "\n"
    "1) 先判断要不要回：不是每条消息都要回。"
    "对方只是嗯/哦/好的/收到/表情/语气词、或明显说给别人听、或陈述完毕不需要回应 → 不要回。"
    "信息不够、听不懂、只能瞎猜 → 也不要回（宁可沉默，也不要乱回）。"
    "\n"
    "2) 要回时：只接住对方「最后一句」的意思，答所问、接所聊。"
    "禁止自作主张：不要突然换话题、不要翻旧账、不要编造行程/事实/承诺、"
    "不要主动提建议/总结/说教（除非对方明确在问）。"
    "背景记忆仅在真正相关时轻轻带一句；禁止复述摘要。"
    "\n"
    "3) 语气：像本人平时的微信——口语、短句、克制；"
    "不要客服腔、不要公文、不要「首先/其次」、不要（动作描写）、不要堆表情。"
    "长度跟随话题：确认可极短，认真提问才稍长。"
    "若对方刚连发多句，综合理解后再回；不要逐句机械作答。"
    "最近对话中 role=assistant 的原话可作为本人表达习惯参考，但不能把旧事实搬到新话题。"
)

DEFAULT_PROMPT = (
    "你是「我」本人在微信里聊天，不是客服，不是助手旁白。"
    "懂就短回，不懂就别回。"
)


def _load_profile_overlay(cfg):
    """读取可选的外置画像配置；失败时保持主配置可用。"""
    raw_path = str((cfg or {}).get("persona_profile_file") or "").strip()
    if not raw_path:
        return {}
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_personas(cfg):
    """从配置解析 personas 字典 username -> prompt。"""
    raw = (cfg or {}).get("personas") or {}
    raw = dict(raw) if isinstance(raw, dict) else {}
    overlay = _load_profile_overlay(cfg).get("personas") or {}
    if isinstance(overlay, dict):
        raw.update(overlay)
    out = {}
    for k, v in raw.items():
        key = (k or "").strip()
        text = (v or "").strip()
        if key and text:
            out[key] = text
    return out


def parse_style_profiles(cfg):
    """解析每会话结构化风格画像，未知字段原样保留供后续兼容。"""
    raw = (cfg or {}).get("style_profiles") or {}
    raw = dict(raw) if isinstance(raw, dict) else {}
    overlay = _load_profile_overlay(cfg).get("style_profiles") or {}
    if isinstance(overlay, dict):
        raw.update(overlay)
    out = {}
    for username, value in raw.items():
        key = str(username or "").strip()
        if key and isinstance(value, dict):
            out[key] = value
    return out


def _list_text(value, limit=12):
    if not isinstance(value, list):
        return ""
    return "、".join(str(x).strip() for x in value[:limit] if str(x).strip())


def _profile_prompt(profile):
    if not isinstance(profile, dict):
        return ""
    lines = ["【与这个人的聊天风格】"]
    fields = (
        ("relationship", "关系"),
        ("address", "称呼"),
        ("tone", "语气"),
        ("length", "常用长度"),
        ("emoji", "表情习惯"),
    )
    for key, label in fields:
        value = str(profile.get(key) or "").strip()
        if value:
            lines.append(f"- {label}：{value[:160]}")
    catchphrases = _list_text(profile.get("catchphrases"))
    avoid = _list_text(profile.get("avoid"))
    if catchphrases:
        lines.append(f"- 常用表达：{catchphrases}")
    if avoid:
        lines.append(f"- 避免表达：{avoid}")
    examples = profile.get("examples")
    if isinstance(examples, list):
        pairs = []
        for item in examples[:12]:
            if not isinstance(item, dict):
                continue
            user = str(item.get("user") or "").strip()
            assistant = str(item.get("assistant") or "").strip()
            if user and assistant:
                pairs.append(f"对方：{user[:160]}\n我：{assistant[:160]}")
        if pairs:
            lines.append(
                "【本人真实表达示例·只模仿语气，不照搬事实】\n" + "\n".join(pairs))
    return "\n".join(lines)


def observed_style_prompt(history):
    """从最近我方消息提取轻量统计，不把无关旧事实再次注入。"""
    mine = [
        str(x.get("text") or "").strip()
        for x in (history or []) if x.get("role") == "assistant"
        and str(x.get("text") or "").strip()
    ][-20:]
    if not mine:
        return ""
    lengths = sorted(len(x) for x in mine)
    median = lengths[len(lengths) // 2]
    short_ratio = sum(1 for n in lengths if n <= 15) / len(lengths)
    punct = sum(sum(x.count(p) for p in "，。！？!?；;：:") for x in mine)
    punct_avg = punct / len(mine)
    return (
        "【本人近期表达统计·仅用于控制文风】"
        f"最近我方消息的中位长度约 {median} 字，"
        f"短消息占比约 {short_ratio:.0%}，平均标点约 {punct_avg:.1f} 个。"
        "请贴近这个分布，不要每次都写成完整说明。"
    )


def build_system_prompt(username, personas=None, style_profiles=None):
    """返回该会话的完整 system prompt（自定义人格 + 通用回复规则）。"""
    custom = ((personas or {}).get(username) or "").strip()
    base = custom if custom else DEFAULT_PROMPT
    profile = _profile_prompt((style_profiles or {}).get(username))
    if profile:
        base += "\n\n" + profile
    return base + BASE_REPLY_RULES
