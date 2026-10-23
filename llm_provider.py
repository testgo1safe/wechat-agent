# -*- coding: utf-8 -*-
"""统一大模型调用层：支持 Ollama 本地 + OpenAI 兼容 API（DeepSeek/Kimi 等）。"""

import base64
import http.client
import ipaddress
import json
import mimetypes
import random
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from reply_intelligence import (
    normalize_bubbles,
    normalize_decision,
    parse_smart_reply_config,
)

# provider -> 预设（base_url / 推荐模型 / 是否需 API Key）
PROVIDERS = {
    "ollama": {
        "label": "Ollama (本地)",
        "base_url": "http://localhost:11434/api/chat",
        "models": ["qwen2.5:7b", "qwen2.5:3b", "qwen2.5:1.5b", "qwen2.5:14b", "llama3.1:8b"],
        "vision_models": ["llava", "llava:13b", "qwen2-vl", "moondream"],
        "needs_key": False,
        "style": "ollama",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/chat/completions",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "vision_models": [],
        "needs_key": True,
        "style": "openai",
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/v1/chat/completions",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "vision_models": ["moonshot-v1-8k-vision-preview"],
        "needs_key": True,
        "style": "openai",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "vision_models": ["gpt-4o-mini", "gpt-4o"],
        "needs_key": True,
        "style": "openai",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models": ["gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-3-flash-preview"],
        "vision_models": ["gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-3-flash-preview"],
        "image_gen_models": ["gemini-2.5-flash-image", "gemini-3.1-flash-image"],
        "needs_key": True,
        "style": "openai",
        "auth_header": "Authorization",
    },
    "custom": {
        "label": "自定义 (OpenAI 兼容)",
        "base_url": "",
        "models": [],
        "vision_models": [],
        "needs_key": True,
        "style": "openai",
    },
}

DEFAULT_LLM = {
    "provider": "ollama",
    "model": "qwen2.5:7b",
    "api_key": "",
    "base_url": "",
    "temperature": 0.4,
}

DEFAULT_DNS_FALLBACK = {
    "enabled": False,
    "servers": ["223.5.5.5", "1.1.1.1"],
    "probe_timeout": 1.5,
    "dns_timeout": 2.0,
    "cache_seconds": 600,
}

_DNS_ROUTE_CACHE = {}
_DNS_ROUTE_LOCK = threading.Lock()

DEFAULT_VISION = {
    "enabled": False,
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_key": "",
    "base_url": "https://api.openai.com/v1/chat/completions",
    "temperature": 0.4,
}

DEFAULT_IMAGE_GEN = {
    "enabled": False,
    "provider": "jimeng",
    "mode": "smart",
    "model": "dall-e-3",
    "api_key": "",
    "base_url": "https://api.openai.com/v1/images/generations",
    "size": "1024x1024",
    "dreamina_path": "",
    "ratio": "1:1",
    "resolution_type": "2k",
    "model_version": "",
    "poll_seconds": 60,
    "trigger_pattern": r"画|生成.{0,4}图|draw|image of",
    "cookie_source": "auto",
    "cdp_port": 9222,
    "cookie_sync_interval": 300,
}

# 统一裁决：先决定 skip/reply，避免每句都硬回、乱接话
REPLY_PLAN_SYSTEM = (
    "私聊里连续省略号（如‘……’或‘。。。。’）通常是在表达情绪或等待回应，"
    "应结合紧邻上下文自然接一句，不要仅因信息少就跳过。\n"
    "【输出格式】只输出一行 JSON，不要 markdown、不要解释：\n"
    '{"action":"reply或skip","reply":"","send_image":false,"image_prompt":""}\n'
    "\n"
    "【何时 skip】以下必须 action=skip、reply 留空：\n"
    "- 语气词/确认/表情：嗯、哦、好的、收到、ok、哈哈、谢谢、拜拜等，且没有新问题\n"
    "- 明显说给别人、群里插话与你无关、不需要你表态的陈述\n"
    "- 你听不懂、缺关键信息、只能瞎猜时（宁可沉默）\n"
    "\n"
    "【何时 reply】对方明确在跟你说话，且需要回应（提问、拜托、商量、需要接话）。\n"
    "reply 只针对对方最后一句：贴切、克制、口语短句；"
    "禁止换话题、翻旧账、编造事实/行程、主动说教或加戏。\n"
    "\n"
    "【生图】仅当对方明确要看图/照片/画画时 send_image=true；"
    "否则必须 false。send_image=true 表示先尝试生图，reply 按成功来写简短配文，"
    "image_prompt 写具体画面（中文，50字内）；程序会按真实结果最终校正。\n"
)

# 两阶段智能回复：第一阶段只决定行为，不直接写文案。
REPLY_DECISION_SYSTEM = (
    "私聊里连续省略号（如‘……’或‘。。。。’）通常是在表达情绪或等待回应，"
    "应结合紧邻上下文判为 reply；群聊仍可保持克制。\n"
    "你是微信回复决策器。只输出一行 JSON，不要 markdown、不要解释：\n"
    '{"action":"reply|skip|handoff","intent":"","tone":"",'
    '"length":"very_short|short|normal|detailed","bubble_count":1,'
    '"confidence":0.0,"reason":"","needs_clarification":false,'
    '"send_image":false,"image_prompt":""}\n'
    "判断原则：\n"
    "- 先做说话人校验：role=assistant/我方原话属于账号主人，"
    "role=user/对方原话属于联系人；第一人称事实绝不能跨角色。\n"
    "- 不是每条都回。纯确认、表情、无关插话、信息不足时 skip。\n"
    "- 对方明确提问、拜托、商量或需要接话时 reply。\n"
    "- 涉及本人未知的金钱、隐私、明确承诺、行程确认、激烈冲突时 handoff，禁止代答。\n"
    "- very_short=2~8字，short=6~20字，normal=15~45字，"
    "detailed=认真问题才用；不要默认写长。\n"
    "- bubble_count 通常为1；只有两个自然表达单元时才为2，最多2。\n"
    "- 信息足够回答时 needs_clarification=false；只有缺少回答所必需的信息时才为 true，"
    "不能为了接话而追问。\n"
    "- 结合关系、最近我方表达和时间间隔判断语气，但不要编造事实；"
    "对方没有询问我方状态时，不要计划重述我方旧状态。\n"
    "- 仅当对方明确要看图/照片/画画时 send_image=true，并给出具体 image_prompt。\n"
    "- send_image=true 代表尝试生图；文字先按生图成功来配合，程序会根据真实"
    "生图/发送结果做最终校正。不要仅因文字想拒绝就关闭明确的图片请求。\n"
)

REPLY_WRITER_SYSTEM = (
    "你是本人微信消息的文字表达器。根据上面的决策计划写最终回复。"
    "只输出一行 JSON：{\"bubbles\":[\"第一条\",\"可选第二条\"]}\n"
    "要求：\n"
    "- 只回答当前最后一句；事实只能来自对话和背景记忆，绝不补造。\n"
    "- role=assistant 是我本人说的话，role=user 是对方说的话。"
    "assistant 中“我/我的”属于我，绝不能改口说成对方的“你/你的”。\n"
    "- 当前回复的对象永远是最后一条 role=user。对方陈述自己的状态时，"
    "先回应对方；不要拿 role=assistant 里的我方旧状态抢话题。\n"
    "- 即使我方旧状态确有依据，只要对方没问，也禁止把它换个说法重新宣布；"
    "例如我方刚说“太阳出来了”，对方说“雨停了”，只回应对方雨停，"
    "不要再写“我这边也出太阳了”。\n"
    "- 回答到已知信息边界就停。第一句已经答完时禁止为了显得热情再补一个问题，"
    "不得新增未出现的物品、人物、状态、结果或因果。\n"
    "- 模仿当前会话的人设、风格画像及最近 role=assistant 的表达习惯。\n"
    "- 像微信口语，不写客服话术、标题、列表、分析过程、动作描写。\n"
    "- 长度服从决策计划；能短就短，允许自然省略，不必每句语法完整。\n"
    "- bubbles 数量服从计划，绝不超过允许数量；不要为了分条而硬拆。\n"
    "- 若计划 send_image=true，文字必须与答应发图一致，不得写无法答应、"
    "不方便、太私人、犯规等拒绝措辞。\n"
    "- 不刻意添加错别字，不滥用语气词或表情，不复述长期摘要。\n"
)

# 兼容旧名
IMAGE_PLAN_SYSTEM = REPLY_PLAN_SYSTEM

VISUAL_INTENT_RE = re.compile(
    r"看.{0,8}照片|照片|看看你|想看看|样子|自拍|发.{0,4}图|画.{0,12}|生成.{0,6}图|"
    r"长什么样|给我看看|出.{0,2}图|来张|拍.{0,4}照",
    re.I,
)


def parse_llm_config(cfg: dict) -> dict:
    """从 autoreply_config.json 解析 llm 配置，兼容旧版顶层 model 字段。"""
    cfg = cfg or {}
    llm = dict(DEFAULT_LLM)
    block = cfg.get("llm")
    if isinstance(block, dict):
        for k in ("provider", "model", "api_key", "base_url", "temperature"):
            if k in block and block[k] is not None:
                llm[k] = block[k]
    dns_cfg = dict(DEFAULT_DNS_FALLBACK)
    if isinstance(block, dict) and isinstance(block.get("dns_fallback"), dict):
        raw_dns = block["dns_fallback"]
        for k in ("enabled", "servers", "probe_timeout", "dns_timeout",
                  "cache_seconds"):
            if k in raw_dns and raw_dns[k] is not None:
                dns_cfg[k] = raw_dns[k]
    dns_cfg["enabled"] = bool(dns_cfg.get("enabled"))
    servers = dns_cfg.get("servers") or []
    if isinstance(servers, str):
        servers = [servers]
    dns_cfg["servers"] = [str(x).strip() for x in servers if str(x).strip()][:4]
    for key, low, high in (
            ("probe_timeout", 0.2, 5.0),
            ("dns_timeout", 0.5, 5.0)):
        try:
            dns_cfg[key] = max(low, min(high, float(dns_cfg.get(key) or low)))
        except (TypeError, ValueError):
            dns_cfg[key] = DEFAULT_DNS_FALLBACK[key]
    try:
        dns_cfg["cache_seconds"] = max(
            30, min(3600, int(dns_cfg.get("cache_seconds") or 600)))
    except (TypeError, ValueError):
        dns_cfg["cache_seconds"] = 600
    llm["dns_fallback"] = dns_cfg
    # 旧配置只有 model 时视为 Ollama
    if cfg.get("model") and (not block or not block.get("model")):
        llm["model"] = str(cfg["model"])
        if not block or not block.get("provider"):
            llm["provider"] = "ollama"
    llm["provider"] = str(llm.get("provider") or "ollama").lower()
    if llm["provider"] not in PROVIDERS:
        llm["provider"] = "custom"
    try:
        llm["temperature"] = float(llm.get("temperature", 0.4))
    except (TypeError, ValueError):
        llm["temperature"] = 0.4
    preset = PROVIDERS.get(llm["provider"], PROVIDERS["custom"])
    if not llm.get("base_url"):
        llm["base_url"] = preset.get("base_url", "")
    if not llm.get("model"):
        llm["model"] = (preset.get("models") or ["deepseek-chat"])[0]
    return llm


def parse_vision_config(cfg: dict) -> dict:
    cfg = cfg or {}
    out = dict(DEFAULT_VISION)
    block = cfg.get("vision")
    if isinstance(block, dict):
        for k in ("enabled", "provider", "model", "api_key", "base_url", "temperature"):
            if k in block and block[k] is not None:
                out[k] = block[k]
    if not out.get("api_key"):
        llm = parse_llm_config(cfg)
        if llm.get("api_key"):
            out["api_key"] = llm["api_key"]
    out["enabled"] = bool(out.get("enabled"))
    out["provider"] = str(out.get("provider") or "openai").lower()
    preset = PROVIDERS.get(out["provider"], PROVIDERS["custom"])
    if not out.get("base_url"):
        out["base_url"] = preset.get("base_url", "")
    if not out.get("model"):
        vmodels = preset.get("vision_models") or preset.get("models") or ["gpt-4o-mini"]
        out["model"] = vmodels[0]
    try:
        out["temperature"] = float(out.get("temperature", 0.4))
    except (TypeError, ValueError):
        out["temperature"] = 0.4
    return out


def parse_image_gen_config(cfg: dict) -> dict:
    cfg = cfg or {}
    out = dict(DEFAULT_IMAGE_GEN)
    block = cfg.get("image_gen")
    if isinstance(block, dict):
        for k in ("enabled", "provider", "mode", "model", "api_key", "base_url",
                  "size", "trigger_pattern", "dreamina_path", "ratio",
                  "resolution_type", "model_version", "poll_seconds",
                  "cookie_file", "cookie_source", "cdp_port", "cookie_sync_interval",
                  "browser", "secure_1psid", "secure_1psidts", "prompt_prefix"):
            if k in block and block[k] is not None:
                out[k] = block[k]
    if not out.get("api_key"):
        llm = parse_llm_config(cfg)
        if llm.get("api_key"):
            out["api_key"] = llm["api_key"]
    out["enabled"] = bool(out.get("enabled"))
    out["provider"] = str(out.get("provider") or "jimeng").lower()
    out["mode"] = str(out.get("mode") or "smart").lower()
    if out["mode"] not in ("smart", "keyword"):
        out["mode"] = "smart"
    if not out.get("base_url"):
        out["base_url"] = "https://api.openai.com/v1/images/generations"
    if not out.get("model"):
        prov = str(out.get("provider") or "").lower()
        if prov == "gemini_web":
            out["model"] = "gemini-3-flash"
        elif prov == "gemini":
            out["model"] = "gemini-2.5-flash-image"
        else:
            out["model"] = "dall-e-3"
    try:
        out["poll_seconds"] = int(out.get("poll_seconds") or 60)
    except (TypeError, ValueError):
        out["poll_seconds"] = 60
    return out


def visual_intent(text):
    return bool(VISUAL_INTENT_RE.search(text or ""))


def image_prompt_from_context(client, messages, latest_text, timeout=60):
    """视觉需求兜底：单独要一条生图提示词。"""
    sys_p = (
        "你是生图提示词助手。根据对话和对方的最新要求，"
        "写一句中文即梦文生图提示词（具体描述人物/场景/风格，50字内）。"
        "只输出提示词本身，不要引号、不要解释。"
    )
    ctx = "\n".join(
        f"{'对方' if m['role']=='user' else '我'}: {m['content']}"
        for m in messages[-6:] if m["role"] in ("user", "assistant")
    )
    user = f"【对话】\n{ctx}\n\n【对方最新】{latest_text}\n\n生图提示词："
    return client.chat(
        [{"role": "system", "content": sys_p}, {"role": "user", "content": user}],
        timeout=timeout,
    )


def parse_image_plan(raw: str) -> dict:
    """解析 AI 返回的 JSON 回复计划（含 skip / 生图）。"""
    empty = {
        "action": "skip",
        "reply": "",
        "send_image": False,
        "image_prompt": "",
    }
    raw = (raw or "").strip()
    if not raw:
        return empty
    text = raw
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if m:
            text = m.group(1).strip()
    # 模型偶尔直接输出 SKIP
    low = text.lower().strip().strip('"').strip("'")
    if low in ("skip", "silence", "no_reply", "noreply", "不回", "跳过"):
        return empty
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                action = str(obj.get("action") or "").strip().lower()
                reply = str(obj.get("reply") or "").strip()
                send = bool(obj.get("send_image"))
                prompt = str(obj.get("image_prompt") or "").strip()
                if send and not prompt:
                    send = False
                # 未写 action 时：有回复内容视为 reply，否则 skip
                if action not in ("reply", "skip"):
                    action = "reply" if reply else "skip"
                if action == "skip":
                    reply = ""
                    send = False
                    prompt = ""
                # 模型把 skip 写进 reply 字段
                if reply.lower() in ("skip", "silence", "no_reply", "不回", "跳过"):
                    action, reply, send, prompt = "skip", "", False, ""
                return {
                    "action": action,
                    "reply": reply,
                    "send_image": send,
                    "image_prompt": prompt,
                }
        except json.JSONDecodeError:
            pass
    # 非 JSON：当作普通文字回复（兼容旧模型）
    if len(raw) > 200:
        raw = raw[:200].rstrip()
    return {
        "action": "reply",
        "reply": raw,
        "send_image": False,
        "image_prompt": "",
    }


def _json_object(raw):
    text = (raw or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if m:
            text = m.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_reply_decision(raw):
    """解析两阶段回复的行为计划；格式异常时倾向继续交给表达阶段判断。"""
    obj = _json_object(raw)
    if obj is None:
        low = (raw or "").strip().lower()
        if low in ("skip", "silence", "no_reply", "noreply", "不回", "跳过"):
            obj = {"action": "skip", "reason": "模型返回 skip"}
        elif low in ("handoff", "人工", "人工接管"):
            obj = {"action": "handoff", "reason": "模型要求人工接管"}
        else:
            obj = {"action": "reply", "intent": (raw or "")[:80]}
    return normalize_decision(obj)


def parse_reply_bubbles(raw, max_bubbles=2):
    obj = _json_object(raw)
    if obj is not None:
        values = obj.get("bubbles")
        if values is None and obj.get("reply"):
            values = [obj.get("reply")]
        return normalize_bubbles(values, max_bubbles=max_bubbles)
    return normalize_bubbles(raw, max_bubbles=max_bubbles)


def should_generate_image(text, image_gen_cfg):
    if not image_gen_cfg.get("enabled"):
        return False
    pat = image_gen_cfg.get("trigger_pattern") or DEFAULT_IMAGE_GEN["trigger_pattern"]
    return bool(re.search(pat, text or "", re.I))


def llm_label(llm: dict) -> str:
    preset = PROVIDERS.get(llm.get("provider", ""), {})
    name = preset.get("label") or llm.get("provider", "?")
    return f"{name}/{llm.get('model', '')}"


def _auth_headers(llm_cfg, preset):
    api_key = (llm_cfg.get("api_key") or "").strip()
    hdr = preset.get("auth_header") or "Authorization"
    if hdr == "x-goog-api-key":
        return {"x-goog-api-key": api_key}
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _skip_dns_name(packet, pos):
    while pos < len(packet):
        size = packet[pos]
        if size == 0:
            return pos + 1
        if size & 0xC0 == 0xC0:
            return pos + 2
        pos += size + 1
    raise ValueError("invalid DNS name")


def _public_dns_a(host, server, timeout=2.0):
    """Minimal UDP DNS A query; TLS still validates the original API host."""
    txid = random.randrange(0, 65536)
    labels = host.rstrip(".").split(".")
    qname = b"".join(bytes([len(x.encode("idna"))]) + x.encode("idna")
                     for x in labels) + b"\0"
    packet = (struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
              + qname + struct.pack("!HH", 1, 1))
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    target = (server, 53, 0, 0) if family == socket.AF_INET6 else (server, 53)
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(float(timeout))
        sock.sendto(packet, target)
        data, _ = sock.recvfrom(4096)
    if len(data) < 12:
        return []
    rid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    if rid != txid or flags & 0x000F:
        return []
    pos = 12
    for _ in range(qd):
        pos = _skip_dns_name(data, pos) + 4
    out = []
    for _ in range(an + ns + ar):
        pos = _skip_dns_name(data, pos)
        if pos + 10 > len(data):
            break
        rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        rdata = data[pos:pos + rdlen]
        pos += rdlen
        if rtype == 1 and rclass == 1 and len(rdata) == 4:
            ip = socket.inet_ntoa(rdata)
            if ip not in out and ipaddress.ip_address(ip).is_global:
                out.append(ip)
    return out


def _tcp_reachable(ip, port, timeout):
    try:
        with socket.create_connection((ip, int(port)), timeout=float(timeout)):
            return True
    except OSError:
        return False


def _fallback_route(host, port, cfg):
    if not cfg or not cfg.get("enabled") or not cfg.get("servers"):
        return None
    now = time.monotonic()
    with _DNS_ROUTE_LOCK:
        cached = _DNS_ROUTE_CACHE.get((host, int(port)))
        if cached and now < cached[1]:
            return cached[0]

    probe_timeout = float(cfg.get("probe_timeout") or 1.5)
    try:
        system_ips = []
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            ip = item[4][0]
            if ip not in system_ips:
                system_ips.append(ip)
        if any(_tcp_reachable(ip, port, probe_timeout) for ip in system_ips):
            return None
    except OSError:
        pass

    for server in cfg.get("servers") or []:
        try:
            candidates = _public_dns_a(
                host, server, timeout=float(cfg.get("dns_timeout") or 2.0))
        except OSError:
            continue
        for ip in candidates:
            if not _tcp_reachable(ip, port, probe_timeout):
                continue
            expires = now + int(cfg.get("cache_seconds") or 600)
            with _DNS_ROUTE_LOCK:
                _DNS_ROUTE_CACHE[(host, int(port))] = (ip, expires)
            return ip
    return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a fallback IP while preserving hostname SNI/cert checks."""
    def __init__(self, host, connect_ip, port=443, timeout=180):
        super().__init__(host, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._connect_ip = connect_ip

    def connect(self):
        sock = socket.create_connection(
            (self._connect_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _http_post_pinned(url, connect_ip, data, headers, timeout):
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port or 443
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/",
                                    parsed.query, ""))
    conn = _PinnedHTTPSConnection(
        parsed.hostname, connect_ip, port=port, timeout=timeout)
    try:
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise urllib.error.HTTPError(
                url, resp.status, resp.reason, resp.headers, None)
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def _http_post(url, payload, headers=None, timeout=180, dns_fallback=None):
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        fallback_ip = _fallback_route(
            parsed.hostname, parsed.port or 443, dns_fallback or {})
        if fallback_ip:
            return _http_post_pinned(url, fallback_ip, data, hdrs, timeout)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class LLMClient:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.llm = parse_llm_config(self.cfg)
        self.vision = parse_vision_config(self.cfg)
        self.image_gen = parse_image_gen_config(self.cfg)
        self.smart_reply = parse_smart_reply_config(self.cfg)

    def update(self, cfg):
        self.cfg = cfg or {}
        self.llm = parse_llm_config(self.cfg)
        self.vision = parse_vision_config(self.cfg)
        self.image_gen = parse_image_gen_config(self.cfg)
        self.smart_reply = parse_smart_reply_config(self.cfg)

    def chat(self, messages, timeout=180):
        return self._chat_with_cfg(self.llm, messages, timeout=timeout)

    def _chat_two_stage(self, messages, latest_text="", timeout=180):
        focus = (latest_text or "").strip()
        plan_msgs = list(messages)
        if focus:
            plan_msgs.append({
                "role": "system",
                "content": (
                    f"【当前待处理】对方最后一句是：「{focus[:300]}」\n"
                    "只针对这一句决定行为，不要答成旧话题。"
                ),
            })
        plan_msgs.append({"role": "system", "content": REPLY_DECISION_SYSTEM})
        if not self.image_gen.get("enabled") or self.image_gen.get("mode") == "keyword":
            plan_msgs.append({
                "role": "system",
                "content": "本次 send_image 必须为 false，image_prompt 留空。",
            })
        raw_plan = self.chat(plan_msgs, timeout=timeout)
        plan = parse_reply_decision(raw_plan)
        plan["reply"] = ""
        plan["bubbles"] = []
        if plan.get("action") != "reply":
            return plan

        max_bubbles = min(
            int(self.smart_reply.get("max_bubbles") or 2),
            int(plan.get("bubble_count") or 1),
        )
        writer_msgs = list(messages)
        if focus:
            writer_msgs.append({
                "role": "system",
                "content": f"【当前最后一句】{focus[:300]}",
            })
        plan_for_writer = {
            k: plan.get(k) for k in
            ("intent", "tone", "length", "bubble_count",
             "needs_clarification", "send_image")
        }
        writer_msgs.append({
            "role": "system",
            "content": (
                "【回复决策计划】" +
                json.dumps(plan_for_writer, ensure_ascii=False)
            ),
        })
        writer_msgs.append({"role": "system", "content": REPLY_WRITER_SYSTEM})
        raw_reply = self.chat(writer_msgs, timeout=timeout)
        bubbles = parse_reply_bubbles(raw_reply, max_bubbles=max_bubbles)
        if not bubbles and not plan.get("send_image"):
            plan["action"] = "skip"
            plan["reason"] = "表达阶段未生成有效文字"
            return plan
        plan["bubbles"] = bubbles
        plan["reply"] = "\n".join(bubbles)

        if self.image_gen.get("enabled") and self.image_gen.get("mode") == "smart":
            if not plan.get("send_image") and visual_intent(latest_text):
                try:
                    prompt = image_prompt_from_context(
                        self, messages, latest_text, timeout=min(timeout, 90))
                    if prompt:
                        plan["send_image"] = True
                        plan["image_prompt"] = prompt.strip()
                        if not plan["bubbles"]:
                            plan["bubbles"] = ["行，发你一张"]
                            plan["reply"] = plan["bubbles"][0]
                except Exception:
                    pass
        return plan

    def rewrite_duplicate_reply(
            self, messages, latest_text, previous_reply, plan, timeout=180):
        """只重写正文一次，不重复做行为决策；用于发送前相似回复拦截。"""
        current = dict(plan or {})
        max_bubbles = min(
            int(self.smart_reply.get("max_bubbles") or 2),
            max(1, int(current.get("bubble_count") or 1)),
        )
        writer_msgs = list(messages)
        writer_msgs.append({
            "role": "system",
            "content": f"【当前最后一句】{(latest_text or '').strip()[:300]}",
        })
        writer_msgs.append({
            "role": "system",
            "content": (
                "【刚刚已经发过的回复】"
                f"{(previous_reply or '').strip()[:300]}\n"
                "上一版候选与这条回复重复。请只承接对方最新补充的信息，"
                "换一个贴切角度重新表达；不得复述、改几个字后照发，"
                "也不要否认或撤回刚发过的话。"
            ),
        })
        plan_for_writer = {
            k: current.get(k) for k in
            ("intent", "tone", "length", "bubble_count",
             "needs_clarification", "send_image")
        }
        writer_msgs.append({
            "role": "system",
            "content": (
                "【回复决策计划】" +
                json.dumps(plan_for_writer, ensure_ascii=False)
            ),
        })
        writer_msgs.append({"role": "system", "content": REPLY_WRITER_SYSTEM})
        raw = self.chat(writer_msgs, timeout=timeout)
        bubbles = parse_reply_bubbles(raw, max_bubbles=max_bubbles)
        current["bubbles"] = bubbles
        current["reply"] = "\n".join(bubbles)
        if not bubbles and not current.get("send_image"):
            current["action"] = "skip"
            current["reason"] = "重复重写未生成有效文字"
        return current

    def chat_with_image_plan(self, messages, latest_text="", timeout=180):
        """统一回复裁决。返回 {action, reply, send_image, image_prompt}。

        无论是否开启生图，都走 JSON 计划，以便模型可以 skip（不每句硬回）。
        """
        if self.smart_reply.get("enabled") and self.smart_reply.get("two_stage"):
            return self._chat_two_stage(
                messages, latest_text=latest_text, timeout=timeout)

        focus = (latest_text or "").strip()
        plan_msgs = list(messages)
        if focus:
            plan_msgs.append({
                "role": "system",
                "content": (
                    f"【当前待处理】对方最后一句是：「{focus[:200]}」\n"
                    "只针对这一句裁决 skip/reply；不要答成别的话题。"
                ),
            })
        plan_msgs.append({"role": "system", "content": REPLY_PLAN_SYSTEM})
        # 未开生图时，禁止模型乱开 send_image
        if not self.image_gen.get("enabled") or self.image_gen.get("mode") == "keyword":
            plan_msgs.append({
                "role": "system",
                "content": "本次 send_image 必须为 false，image_prompt 留空。",
            })
        raw = self.chat(plan_msgs, timeout=timeout)
        plan = parse_image_plan(raw)
        if plan.get("action") == "skip":
            return plan
        # keyword 模式：文字由计划给出，生图另按关键词触发（上层处理）
        if self.image_gen.get("enabled") and self.image_gen.get("mode") == "smart":
            if not plan.get("send_image") and visual_intent(latest_text):
                try:
                    prompt = image_prompt_from_context(
                        self, messages, latest_text, timeout=min(timeout, 90))
                    if prompt:
                        plan["send_image"] = True
                        plan["image_prompt"] = prompt.strip()
                        if not plan.get("reply"):
                            plan["reply"] = "好，发你一张。"
                            plan["action"] = "reply"
                except Exception:
                    pass
        if plan.get("send_image") or (plan.get("reply") or "").strip():
            plan["action"] = "reply"
        else:
            plan["action"] = "skip"
        return plan

    def chat_vision(self, messages, image_paths, prompt_extra="", timeout=180):
        """多模态识图：messages 为文本上下文，image_paths 为本地图片路径列表。"""
        if not self.vision.get("enabled"):
            raise RuntimeError("vision.enabled 未开启")
        paths = [p for p in (image_paths or []) if p]
        if not paths:
            raise RuntimeError("无图片路径")
        vcfg = dict(self.vision)
        preset = PROVIDERS.get(vcfg["provider"], PROVIDERS["custom"])
        style = preset.get("style", "openai")

        if style == "ollama":
            imgs_b64 = [_file_to_b64(p) for p in paths]
            ollama_msgs = []
            for m in messages:
                ollama_msgs.append({"role": m["role"], "content": m["content"]})
            user_text = prompt_extra or "请结合图片内容，用简短自然的中文回复对方。"
            ollama_msgs.append({
                "role": "user",
                "content": user_text,
                "images": imgs_b64,
            })
            return self._chat_with_cfg(vcfg, ollama_msgs, timeout=timeout)

        # OpenAI 兼容 vision
        parts = [{"type": "text",
                  "text": prompt_extra or "请结合图片内容，用简短自然的中文回复对方。"}]
        for p in paths:
            mime = mimetypes.guess_type(p)[0] or "image/jpeg"
            b64 = _file_to_b64(p)
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        openai_msgs = []
        for m in messages:
            if m["role"] == "system":
                openai_msgs.append(m)
        openai_msgs.append({"role": "user", "content": parts})
        return self._chat_with_cfg(vcfg, openai_msgs, timeout=timeout)

    def generate_image(self, prompt, timeout=180):
        """文生图，返回本地临时文件路径。"""
        gcfg = self.image_gen
        if not gcfg.get("enabled"):
            raise RuntimeError("image_gen.enabled 未开启")
        provider = str(gcfg.get("provider") or "jimeng").lower()
        if provider == "jimeng":
            import jimeng_client
            return jimeng_client.text2image(prompt, gcfg, timeout=timeout)
        if provider == "gemini_web":
            import gemini_web_client
            gcfg = dict(gcfg)
            m = str(gcfg.get("model") or "").strip().lower()
            if not m or m.startswith("dall-") or "gemini" not in m:
                gcfg["model"] = "gemini-3-flash"
            return gemini_web_client.text2image(prompt, gcfg, timeout=timeout)
        api_key = (gcfg.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("生图需要 API Key")
        url = (gcfg.get("base_url") or "").strip()
        if not url:
            raise RuntimeError("未配置生图 API 地址")
        payload = {
            "model": gcfg.get("model") or "dall-e-3",
            "prompt": prompt,
            "n": 1,
            "size": gcfg.get("size") or "1024x1024",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        r = _http_post(url, payload, headers=headers, timeout=timeout)
        data = (r.get("data") or [{}])[0]
        if data.get("b64_json"):
            import tempfile
            import os as os_mod
            raw = base64.b64decode(data["b64_json"])
            fd, path = tempfile.mkstemp(suffix=".png", prefix="wx_gen_")
            os_mod.close(fd)
            with open(path, "wb") as f:
                f.write(raw)
            return path
        img_url = data.get("url")
        if not img_url:
            raise RuntimeError(r)
        return _download_file(img_url, suffix=".png", timeout=timeout)

    def _chat_with_cfg(self, llm_cfg, messages, timeout=180):
        llm_cfg = llm_cfg or self.llm
        provider = llm_cfg["provider"]
        preset = PROVIDERS.get(provider, PROVIDERS["custom"])
        style = preset.get("style", "openai")
        url = (llm_cfg.get("base_url") or "").strip()
        if not url:
            raise RuntimeError(f"未配置 {provider} 的 API 地址")

        if style == "ollama":
            payload = {
                "model": llm_cfg["model"],
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": llm_cfg["temperature"],
                    "num_ctx": 8192,
                },
            }
            r = _http_post(
                url, payload, timeout=timeout,
                dns_fallback=llm_cfg.get("dns_fallback"))
            return ((r.get("message") or {}).get("content") or "").strip()

        api_key = (llm_cfg.get("api_key") or "").strip()
        if preset.get("needs_key") and not api_key:
            raise RuntimeError(f"{preset.get('label', provider)} 需要 API Key")
        payload = {
            "model": llm_cfg["model"],
            "messages": messages,
            "temperature": llm_cfg["temperature"],
        }
        headers = _auth_headers(llm_cfg, preset)
        r = _http_post(
            url, payload, headers=headers, timeout=timeout,
            dns_fallback=llm_cfg.get("dns_fallback"))
        choices = r.get("choices") or []
        if not choices:
            raise RuntimeError(r.get("error", r))
        return ((choices[0].get("message") or {}).get("content") or "").strip()


def _file_to_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _download_file(url, suffix=".png", timeout=120):
    import tempfile
    import os as os_mod
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="wx_gen_")
    os_mod.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return path


def mask_api_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
