# -*- coding: utf-8 -*-
"""微信自动回复 · 网页管理面板（纯标准库，无需第三方依赖）。

功能：
  - 浏览/搜索好友与群聊（数据来自 WeChat-Hook 只读接口 contact.db）
  - 一键把某人/某群加入或移出「自动回复白名单」(autoreply_config.json 的 reply_to)
  - 只有在白名单里的会话才会调用大模型自动回复；不在白名单=不自动回复
  - 调整全局开关：reply_all / include_system / interval / model

改动写入 autoreply_config.json 后，后台守护进程会热加载(约 1 个轮询周期内生效)，
无需重启。

启动：
  python wechat_web.py            # 默认 http://127.0.0.1:8848
  python wechat_web.py --port 9000
"""

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import wechat_recv as rc
import wechat_memory as mem
import wechat_knowledge as kb
from llm_provider import PROVIDERS, mask_api_key, parse_llm_config
import wechat_persona as persona_mod

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "autoreply_config.json")
JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_panel.js")

SYSTEM_ACCOUNTS = {
    "weixin", "fmessage", "medianote", "filehelper", "floatbottle",
    "qmessage", "qqmail", "tmessage", "newsapp", "notifymessage", "weibo",
    "brandsessionholder", "opencustomerservicemsg", "exmail_tool",
}

# 普通微信好友是 local_type=1；已添加的企业微信/OpenIM 好友是
# local_type=5（4 为 OpenIM 标志，1 为好友标志）。local_type=6 等并非
# 已添加好友，不应混入白名单选择器。
FRIEND_CONTACT_WHERE = (
    "(local_type=1 OR (local_type=5 AND username LIKE '%@openim'))"
)

MODEL_CHOICES = []  # 兼容旧前端，实际用 providers


def _public_config(cfg):
    """返回给前端的配置（隐藏 API Key）。"""
    out = dict(cfg or {})
    llm = parse_llm_config(cfg or {})
    key = (llm.get("api_key") or "").strip()
    out["llm"] = {
        "provider": llm["provider"],
        "model": llm["model"],
        "base_url": llm.get("base_url") or "",
        "temperature": llm.get("temperature", 0.4),
        "dns_fallback": llm.get("dns_fallback") or {},
        "api_key_set": bool(key),
        "api_key_mask": mask_api_key(key),
    }
    out["model"] = llm["model"]  # 兼容旧字段
    out["knowledge_base"] = kb.parse_config(cfg)
    return out


def _providers_for_ui():
    return {k: {"label": v["label"], "models": v.get("models", []),
                "needs_key": v.get("needs_key", False),
                "base_url": v.get("base_url", "")}
            for k, v in PROVIDERS.items()}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------- 配置读写 ----------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"interval": 3, "reply_to": [], "reply_all": False,
                "include_system": False, "model": "qwen2.5:7b"}
    except Exception:
        return {"interval": 3, "reply_to": [], "reply_all": False,
                "include_system": False, "model": "qwen2.5:7b"}


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def get_personas(cfg=None):
    return persona_mod.parse_personas(cfg or load_config())


def save_persona(username, text):
    cfg = load_config()
    personas = dict(cfg.get("personas") or {})
    username = (username or "").strip()
    text = (text or "").strip()
    if not username:
        return personas
    if text:
        personas[username] = text
    else:
        personas.pop(username, None)
    cfg["personas"] = personas
    save_config(cfg)
    return personas


def get_whitelist():
    cfg = load_config()
    rt = cfg.get("reply_to", [])
    if isinstance(rt, str):
        rt = [x.strip() for x in rt.split(",") if x.strip()]
    return list(rt)


def _contact_display_name(username):
    k = _esc(username)
    try:
        rows = _query_with_retry(
            "contact.db",
            f"SELECT remark, nick_name FROM contact WHERE username='{k}' LIMIT 1")
        if rows:
            return name_of(rows[0])
    except Exception:
        pass
    return username


def get_ai_context(username):
    """返回自动回复实际使用的上下文（长期摘要 + 短期原文）。"""
    username = (username or "").strip()
    if not username:
        return None
    cfg = load_config()
    memory_cfg = mem.parse_memory_config(cfg)
    summary, last_seq = mem.load(username)
    try:
        history = rc.recent_history(
            username, limit=memory_cfg["history_window"])
    except Exception as e:
        history = []
        hist_err = str(e)
    else:
        hist_err = ""
    unsummarized = [h for h in history if h["sort_seq"] > last_seq]
    recent = history[-memory_cfg["keep_recent"]:]
    found = rc.find_msg_table(username)
    personas = get_personas(cfg)
    persona_text = personas.get(username) or ""
    raw_wl = cfg.get("reply_to") or []
    if isinstance(raw_wl, str):
        raw_wl = [x.strip() for x in raw_wl.split(",") if x.strip()]
    whitelist = set(raw_wl if isinstance(raw_wl, list) else [])
    auto_summary = (
        not memory_cfg["whitelist_only"] or username in whitelist)
    return {
        "username": username,
        "display_name": _contact_display_name(username),
        "summary": summary,
        "last_summarized_seq": last_seq,
        "history_total": len(history),
        "unsummarized_count": len(unsummarized),
        "keep_recent": memory_cfg["keep_recent"],
        "history_window": memory_cfg["history_window"],
        "auto_summary_enabled": auto_summary,
        "auto_summary_threshold": (
            memory_cfg["keep_recent"] + memory_cfg["summary_batch"] + 1),
        "recent_for_prompt": [
            {"role": h["role"], "text": h["text"],
             "sender": h.get("sender") or ""}
            for h in recent
        ],
        "msg_db": found[0] if found else None,
        "has_persona": bool(persona_text),
        "persona_text": persona_text,
        "persona_preview": persona_text[:300] + ("…" if len(persona_text) > 300 else ""),
        "history_error": hist_err,
    }


def save_memory_summary(username, summary):
    username = (username or "").strip()
    if not username:
        return None
    _, last_seq = mem.load(username)
    mem.save(username, (summary or "").strip(), last_seq)
    return get_ai_context(username)


def reset_memory(username):
    username = (username or "").strip()
    if not username:
        return None
    mem.reset(username)
    return get_ai_context(username)


# ---------------- 联系人查询 ----------------

def _esc(s):
    return s.replace("'", "''")


def _is_system(username):
    return username in SYSTEM_ACCOUNTS or username.startswith("gh_")


def name_of(c):
    return (c.get("remark") or c.get("nick_name")
            or c.get("alias") or c.get("username"))


def _query_with_retry(db, sql):
    """Hook 偶发失败时重试（query 内已有退避，这里再兜一层）。"""
    last = None
    for _ in range(3):
        try:
            return rc.query(db, sql, timeout=12, retries=3)
        except Exception as e:
            last = e
            time.sleep(0.2)
    raise last


_CONTACTS_CACHE = {}  # kind -> (ts, data)
_CONTACTS_TTL = 60


def list_friends(keyword=""):
    if not keyword:
        hit = _CONTACTS_CACHE.get("friend")
        if hit and time.time() - hit[0] < _CONTACTS_TTL:
            return hit[1]
    data = _list_friends_raw(keyword)
    if not keyword:
        _CONTACTS_CACHE["friend"] = (time.time(), data)
    return data


def _list_friends_raw(keyword=""):
    where = f"WHERE {FRIEND_CONTACT_WHERE}"
    if keyword:
        k = _esc(keyword)
        where += (f" AND (remark LIKE '%{k}%' OR nick_name LIKE '%{k}%' "
                  f"OR alias LIKE '%{k}%' OR username LIKE '%{k}%')")
    sql = ("SELECT username, alias, remark, nick_name FROM contact "
           + where + " ORDER BY remark, nick_name LIMIT 500")
    rows = _query_with_retry("contact.db", sql)
    out = []
    for c in rows:
        if _is_system(c["username"]):
            continue
        out.append({"kind": "friend", "username": c["username"],
                    "name": name_of(c), "alias": c.get("alias") or ""})
    return out


def list_rooms(keyword=""):
    if not keyword:
        hit = _CONTACTS_CACHE.get("room")
        if hit and time.time() - hit[0] < _CONTACTS_TTL:
            return hit[1]
    data = _list_rooms_raw(keyword)
    if not keyword:
        _CONTACTS_CACHE["room"] = (time.time(), data)
    return data


def _list_rooms_raw(keyword=""):
    """群聊列表：chat_room 只有 username，群名在 contact 表，需 JOIN。"""
    where = ""
    if keyword:
        k = _esc(keyword)
        where = (f"WHERE (c.nick_name LIKE '%{k}%' OR c.remark LIKE '%{k}%' "
                 f"OR c.username LIKE '%{k}%')")
    sql = ("SELECT c.username, c.nick_name, c.remark, c.alias "
           "FROM chat_room r JOIN contact c ON c.username = r.username "
           + where + " ORDER BY c.nick_name, c.remark LIMIT 500")
    try:
        rows = _query_with_retry("contact.db", sql)
    except Exception:
        # 回退：部分版本 contact 里直接有 @chatroom 记录
        fb_where = "WHERE c.username LIKE '%@chatroom'"
        if keyword:
            k = _esc(keyword)
            fb_where += (f" AND (c.nick_name LIKE '%{k}%' OR c.remark LIKE '%{k}%' "
                         f"OR c.username LIKE '%{k}%')")
        try:
            rows = _query_with_retry(
                "contact.db",
                "SELECT c.username, c.nick_name, c.remark, c.alias "
                "FROM contact c " + fb_where +
                " ORDER BY c.nick_name, c.remark LIMIT 500")
        except Exception:
            return []
    out = []
    for c in rows:
        out.append({"kind": "room", "username": c["username"],
                    "name": name_of(c), "alias": c.get("alias") or ""})
    return out


# ---------------- HTTP ----------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>微信自动回复 · 管理面板</title>
<style>
:root{--bg:#0f1420;--card:#1a2130;--line:#2a3346;--fg:#e6ebf5;--mut:#8a97ad;--acc:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--danger:#ef4444}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:17px;margin:0}
.badge{font-size:12px;color:var(--mut)}
.wrap{display:grid;grid-template-columns:1fr 360px;gap:18px;padding:18px 22px;align-items:start}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:14px;margin:0 0 12px;color:var(--mut);font-weight:600;letter-spacing:.3px}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{padding:6px 14px;border-radius:8px;background:#141b28;border:1px solid var(--line);cursor:pointer;font-size:13px;color:var(--mut)}
.tab.active{background:var(--acc);color:#fff;border-color:var(--acc)}
input,select{background:#141b28;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;width:100%}
.row{display:flex;align-items:center;justify-content:space-between;padding:10px 6px;border-bottom:1px solid var(--line);gap:10px}
.row:last-child{border-bottom:none}
.who{display:flex;flex-direction:column;min-width:0}
.who b{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who small{color:var(--mut);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tag{font-size:10px;padding:1px 6px;border-radius:6px;margin-left:6px}
.tag.friend{background:#173a2a;color:#4ade80}
.tag.room{background:#2a2440;color:#c4b5fd}
button{cursor:pointer;border:none;border-radius:8px;padding:7px 12px;font-size:12px;font-weight:600}
.add{background:var(--acc);color:#fff}
.rm{background:transparent;color:var(--danger);border:1px solid var(--danger)}
.list{max-height:60vh;overflow:auto}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;color:var(--mut);margin-bottom:6px}
.switch{display:flex;align-items:center;gap:8px;font-size:13px}
.pill{display:inline-flex;align-items:center;gap:6px;background:#141b28;border:1px solid var(--line);border-radius:20px;padding:5px 10px;margin:4px 6px 0 0;font-size:12px}
.pill button{background:transparent;color:var(--danger);padding:0 2px}
.field textarea{min-height:120px;resize:vertical;font-family:inherit;line-height:1.5}
.btn2{background:#141b28;border:1px solid var(--line);color:var(--fg);margin-right:8px}
.btnPersona{background:#141b28;border:1px solid var(--line);color:var(--fg)}
.btnPersona.on{border-color:var(--warn);color:var(--warn)}
.row-actions{display:flex;gap:6px;flex-shrink:0}
.hint{font-size:11px;color:var(--mut);margin-top:10px;line-height:1.5}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px;z-index:99}
.modal.hidden{display:none}
.modal-box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;width:min(520px,96vw)}
.modal-box h3{margin:0 0 12px;font-size:15px}
.modal-box textarea{min-height:160px;width:100%}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}
.empty{color:var(--mut);font-size:13px;padding:16px;text-align:center}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#111827;border:1px solid var(--line);padding:10px 16px;border-radius:10px;opacity:0;transition:.25s;font-size:13px}
.toast.show{opacity:1}
.btnCtx{background:#141b28;border:1px solid var(--line);color:var(--fg)}
.kb-area{background:#141b28;border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;width:100%;min-height:95px;font-family:inherit;resize:vertical;line-height:1.5}
.kb-doc{border-top:1px solid var(--line);padding:10px 0}
.kb-doc strong{display:block;font-size:13px}
.kb-doc small{display:block;color:var(--mut);font-size:11px;margin:5px 0;word-break:break-all}
.kb-doc select{min-height:64px;margin:6px 0}
.kb-actions{display:flex;gap:7px;flex-wrap:wrap}
.kb-result{border-top:1px solid var(--line);padding:8px 0;font-size:12px;line-height:1.5;white-space:pre-wrap}
.ctx-section{margin-bottom:14px}
.ctx-section h4{margin:0 0 8px;font-size:12px;color:var(--mut);font-weight:600}
.ctx-box{background:#141b28;border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.55;white-space:pre-wrap;max-height:140px;overflow:auto}
.ctx-meta{font-size:11px;color:var(--mut);margin-bottom:12px}
.ctx-chat{max-height:280px;overflow:auto;display:flex;flex-direction:column;gap:8px}
.ctx-bubble{max-width:92%;padding:8px 11px;border-radius:10px;font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.ctx-bubble.user{align-self:flex-start;background:#1e293b;border:1px solid var(--line)}
.ctx-bubble.assistant{align-self:flex-end;background:#1a3a2a;border:1px solid #22543d}
.ctx-bubble small{display:block;font-size:10px;color:var(--mut);margin-bottom:4px}
#contextSummary{min-height:120px;resize:vertical;font-family:inherit;line-height:1.5}
#contextErr.hidden{display:none}
</style>
</head>
<body>
<header>
  <h1>微信自动回复 · 管理面板</h1>
  <span class="badge" id="status">加载中…</span>
</header>
<div class="wrap">
  <div class="card">
    <div class="tabs">
      <div class="tab active" data-kind="friend">好友</div>
      <div class="tab" data-kind="room">群聊</div>
    </div>
    <input id="search" placeholder="搜索昵称 / 备注 / 微信号 / wxid …"/>
    <div class="list" id="list"><div class="empty">加载中…</div></div>
  </div>

  <div style="display:flex;flex-direction:column;gap:18px">
    <div class="card">
      <h2>当前白名单（会自动回复）</h2>
      <div id="whitelist"><div class="empty">空</div></div>
      <div class="hint">白名单里的人/群会调用大模型自动回复。每条会话可单独设置「人格」提示词（技术群、家人等）。改动即时生效。</div>
    </div>
    <div class="card">
      <h2>全局设置</h2>
      <div class="field">
        <label>大模型提供商</label>
        <select id="provider"></select>
      </div>
      <div class="field">
        <label>模型名称</label>
        <input id="model" list="modelList" placeholder="如 deepseek-chat / moonshot-v1-8k / qwen2.5:7b"/>
        <datalist id="modelList"></datalist>
      </div>
      <div class="field" id="apiKeyBox">
        <label>API Key <small id="apiKeyHint" style="color:var(--mut)"></small></label>
        <input id="api_key" type="password" placeholder="留空表示不修改已保存的 Key"/>
      </div>
      <div class="field" id="baseUrlBox" style="display:none">
        <label>API 地址 (自定义提供商)</label>
        <input id="base_url" placeholder="https://api.example.com/v1/chat/completions"/>
      </div>
      <div class="field">
        <label>轮询间隔（秒）</label>
        <input id="interval" type="number" min="1" step="1"/>
      </div>
      <div class="field switch"><input type="checkbox" id="reply_all" style="width:auto"/><label style="margin:0">回复所有非系统联系人（危险）</label></div>
      <div class="field switch"><input type="checkbox" id="include_system" style="width:auto"/><label style="margin:0">也处理系统号/公众号</label></div>
      <button class="add" id="saveSettings" style="width:100%">保存设置（即时生效）</button>
      <div class="hint">支持 Ollama 本地、DeepSeek、Kimi(Moonshot)、OpenAI 及自定义 OpenAI 兼容 API。保存后守护进程约 3 秒内热加载。</div>
    </div>
    <div class="card" id="kbCard">
      <h2>本地知识库 · 按会话授权</h2>
      <div class="field switch"><input type="checkbox" id="kbEnabled" style="width:auto"/><label style="margin:0">启用知识库辅助回复</label></div>
      <div class="field switch"><input type="checkbox" id="kbAllowCloud" style="width:auto"/><label style="margin:0">允许向云模型发送命中的文档片段</label></div>
      <button class="btn2" id="kbSaveSettings">保存知识库开关</button>
      <div class="hint" id="kbState">知识库默认关闭；文档还需单独授权给具体白名单会话。</div>
      <div class="field" style="margin-top:16px">
        <label>授权会话（按 Ctrl 可多选）</label>
        <select id="kbTargets" multiple size="4"></select>
      </div>
      <div class="field">
        <label>手动录入</label>
        <input id="kbTitle" placeholder="文档标题，如：信息安全常见问答"/>
        <textarea class="kb-area" id="kbText" placeholder="粘贴已获准使用的制度、流程或 FAQ 文字"></textarea>
        <button class="add" id="kbAddText" style="margin-top:7px">保存文本</button>
      </div>
      <div class="field">
        <label>导入文件（TXT、MD、PDF、DOCX；最多 5 MB）</label>
        <input id="kbFile" type="file" accept=".txt,.md,.pdf,.docx"/>
        <button class="add" id="kbUpload" style="margin-top:7px">导入所选文件</button>
      </div>
      <div class="hint">请勿导入未经批准的客户资料或密钥。白名单只决定谁可触发回复，文档授权另行设置。</div>
      <h2 style="margin-top:18px">已导入文档</h2>
      <div id="kbDocs"><div class="empty">加载中…</div></div>
      <h2 style="margin-top:18px">检索预览（不会发送消息）</h2>
      <select id="kbPreviewTarget"></select>
      <input id="kbQuery" placeholder="输入问题，如：内部资料能对外发吗？" style="margin-top:7px"/>
      <button class="btn2" id="kbSearch" style="margin-top:7px">预览命中片段</button>
      <div id="kbResults"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div id="personaModal" class="modal hidden">
  <div class="modal-box">
    <h3>回复人格 · <span id="personaTarget"></span></h3>
    <p class="hint" style="margin-top:0">仅对此会话生效。例：技术群写专业简洁；对老婆写老公口吻。</p>
    <textarea id="personaText" placeholder="输入此人/此群的回复人格与语气要求…"></textarea>
    <div class="modal-actions">
      <button class="rm" id="personaClear">清除人格</button>
      <button class="btn2" id="personaCancel">取消</button>
      <button class="add" id="personaSave">保存</button>
    </div>
  </div>
</div>
<div id="contextModal" class="modal hidden">
  <div class="modal-box" style="width:min(560px,96vw)">
    <h3>AI 上下文 · <span id="contextTarget"></span></h3>
    <div id="contextMeta" class="ctx-meta"></div>
    <div id="contextErr" class="hint hidden" style="color:var(--warn)"></div>
    <div class="ctx-section">
      <h4>长期记忆摘要 <span style="font-weight:400;color:var(--mut)">（可编辑，保存后立即生效）</span></h4>
      <textarea id="contextSummary" placeholder="填写 AI 应长期记住的背景信息，如约定、偏好、重要事件…"></textarea>
    </div>
    <div class="ctx-section">
      <h4>短期原文 <span style="font-weight:400;color:var(--mut)">（来自微信消息库，只读）</span></h4>
      <div id="contextRecent" class="ctx-chat"><div class="empty" style="padding:8px">加载中…</div></div>
    </div>
    <div class="modal-actions">
      <button class="rm" id="contextReset">清空记忆</button>
      <button class="btn2" id="contextEditPersona">编辑人格</button>
      <button class="btn2" id="contextRefresh">刷新</button>
      <button class="add" id="contextSave">保存摘要</button>
      <button class="btn2" id="contextClose">关闭</button>
    </div>
  </div>
</div>
<script src="/web_panel.js?v=__JS_VER__"></script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _js_version(self):
        try:
            return str(int(os.path.getmtime(JS_PATH)))
        except OSError:
            return "0"

    def _send(self, code, body, ctype="application/json; charset=utf-8", nocache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if nocache:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, code, data, ctype, nocache=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if nocache:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _hook_ok(self):
        try:
            rc.query("contact.db", "SELECT 1 LIMIT 1")
            return True
        except Exception:
            return False

    def _kb_admin_allowed(self, write=False):
        """KB endpoints are local-admin only, including when --host is broad."""
        try:
            local_ip = ipaddress.ip_address(self.client_address[0])
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            allowed = local_ip.is_loopback and host in ("127.0.0.1", "localhost")
        except ValueError:
            allowed = False
        if write:
            allowed = allowed and self.headers.get("X-KB-Admin") == "1"
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urllib.parse.urlparse(origin).netloc.lower()
                allowed = allowed and origin_host == (
                    self.headers.get("Host") or "").lower()
        if not allowed:
            self._send(403, {"error": "知识库管理仅允许本机同源页面访问"})
        return allowed

    @staticmethod
    def _kb_targets_from_body(body):
        targets = body.get("targets")
        if not isinstance(targets, list) or not targets:
            raise kb.KnowledgeError("请从当前白名单选择授权会话")
        allowed = set(get_whitelist())
        if any(not isinstance(target, str) or target not in allowed
               for target in targets):
            raise kb.KnowledgeError("只能授权当前自动回复白名单内的会话")
        return targets

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            html = PAGE.replace("__JS_VER__", self._js_version())
            self._send(200, html, "text/html; charset=utf-8", nocache=True)
        elif path == "/web_panel.js":
            try:
                with open(JS_PATH, "rb") as f:
                    data = f.read()
            except OSError as e:
                self._send(404, {"error": str(e)})
                return
            self._send_bytes(200, data, "application/javascript; charset=utf-8")
        elif path == "/api/kb/documents":
            if self._kb_admin_allowed():
                self._send(200, {"documents": kb.list_documents()})
        elif path == "/api/kb/search":
            if not self._kb_admin_allowed():
                return
            username = (qs.get("username") or [""])[0].strip()
            query = (qs.get("q") or [""])[0].strip()
            opts = kb.parse_config(load_config())
            self._send(200, {"hits": kb.search(
                username, query, get_whitelist(),
                max_hits=opts["max_hits"], max_chars=opts["max_chars"],
            )})
        elif path == "/api/state":
            cfg = load_config()
            self._send(200, {
                "config": _public_config(cfg),
                "whitelist": get_whitelist(),
                "personas": get_personas(cfg),
                "providers": _providers_for_ui(),
                "hook_ok": self._hook_ok(),
            })
        elif path == "/api/contacts":
            kind = (qs.get("kind", ["friend"])[0])
            kw = (qs.get("q", [""])[0]).strip()
            try:
                data = list_rooms(kw) if kind == "room" else list_friends(kw)
                self._send(200, data)
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/context":
            username = (qs.get("username", [""])[0]).strip()
            if not username:
                self._send(400, {"error": "missing username"})
                return
            try:
                self._send(200, get_ai_context(username))
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def _handle_kb_post(self, parsed):
        if not self._kb_admin_allowed(write=True):
            return
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = (kb.MAX_FILE_BYTES if path == "/api/kb/upload"
                     else kb.MAX_TEXT_CHARS * 4 + 4096)
            if length < 1 or length > limit:
                self._send(413, {"error": "知识库请求为空或超过大小限制"})
                return
            raw = self.rfile.read(length)
            if path == "/api/kb/upload":
                qs = urllib.parse.parse_qs(parsed.query)
                filename = (qs.get("filename") or [""])[0]
                title = (qs.get("title") or [""])[0] or os.path.basename(filename)
                targets = json.loads((qs.get("targets") or ["[]"])[0])
                targets = self._kb_targets_from_body({"targets": targets})
                content = kb.extract_text(filename, raw)
                doc_id = kb.add_document(
                    title, content, targets,
                    source_name=os.path.basename(filename),
                )
                self._send(200, {"id": doc_id, "documents": kb.list_documents()})
                return
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise kb.KnowledgeError("请求数据格式无效")
            if path == "/api/kb/documents":
                action = body.get("action") or "add"
                if action == "add":
                    targets = self._kb_targets_from_body(body)
                    doc_id = kb.add_document(
                        body.get("title"), body.get("text"), targets,
                        source_name="管理页面手动录入",
                    )
                elif action == "set_targets":
                    targets = self._kb_targets_from_body(body)
                    doc_id = str(body.get("id") or "")
                    kb.set_targets(doc_id, targets)
                elif action == "delete":
                    doc_id = str(body.get("id") or "")
                    kb.delete_document(doc_id)
                else:
                    raise kb.KnowledgeError("未知知识库操作")
                self._send(200, {"id": doc_id, "documents": kb.list_documents()})
            elif path == "/api/kb/settings":
                cfg = load_config()
                current = kb.parse_config(cfg)
                for key in ("enabled", "allow_cloud"):
                    if key in body:
                        if not isinstance(body[key], bool):
                            raise kb.KnowledgeError(f"{key} 必须是布尔值")
                        current[key] = body[key]
                cfg["knowledge_base"] = current
                save_config(cfg)
                self._send(200, {"knowledge_base": current})
            else:
                self._send(404, {"error": "not found"})
        except (kb.KnowledgeError, ValueError, UnicodeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            print(f"[知识库] 操作失败: {exc}", file=sys.stderr)
            self._send(500, {"error": "知识库操作失败，请检查服务日志"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/kb/"):
            self._handle_kb_post(parsed)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}

        if parsed.path == "/api/whitelist":
            action = body.get("action")
            username = (body.get("username") or "").strip()
            cfg = load_config()
            wl = cfg.get("reply_to", [])
            if isinstance(wl, str):
                wl = [x.strip() for x in wl.split(",") if x.strip()]
            if action == "add" and username and username not in wl:
                wl.append(username)
            elif action == "remove" and username in wl:
                wl.remove(username)
            cfg["reply_to"] = wl
            save_config(cfg)
            self._send(200, {"whitelist": wl})
        elif parsed.path == "/api/persona":
            username = (body.get("username") or "").strip()
            if not username:
                self._send(400, {"error": "missing username"})
                return
            personas = save_persona(username, body.get("text", ""))
            self._send(200, {"personas": personas})
        elif parsed.path == "/api/context":
            username = (body.get("username") or "").strip()
            if not username:
                self._send(400, {"error": "missing username"})
                return
            action = (body.get("action") or "save").strip()
            try:
                if action == "reset":
                    self._send(200, reset_memory(username))
                elif action == "save":
                    self._send(200, save_memory_summary(username, body.get("summary", "")))
                else:
                    self._send(400, {"error": "unknown action"})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif parsed.path == "/api/settings":
            cfg = load_config()
            for k in ("reply_all", "include_system"):
                if k in body:
                    cfg[k] = body[k]
            if "interval" in body:
                try:
                    cfg["interval"] = max(1, int(body["interval"]))
                except Exception:
                    pass
            if "model" in body and "llm" not in body:
                cfg["model"] = body["model"]
            if "llm" in body and isinstance(body["llm"], dict):
                old = parse_llm_config(cfg)
                new_llm = dict(old)
                for k in ("provider", "model", "base_url", "temperature"):
                    if k in body["llm"] and body["llm"][k] is not None:
                        new_llm[k] = body["llm"][k]
                new_key = (body["llm"].get("api_key") or "").strip()
                if new_key:
                    new_llm["api_key"] = new_key
                cfg["llm"] = {
                    "provider": new_llm.get("provider", "ollama"),
                    "model": new_llm.get("model", "qwen2.5:7b"),
                    "api_key": new_llm.get("api_key", ""),
                    "base_url": new_llm.get("base_url", ""),
                    "temperature": new_llm.get("temperature", 0.4),
                    "dns_fallback": new_llm.get("dns_fallback") or {},
                }
                cfg["model"] = cfg["llm"]["model"]
            save_config(cfg)
            self._send(200, {"config": _public_config(cfg)})
        else:
            self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="微信自动回复网页管理面板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8848)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"管理面板已启动: http://{args.host}:{args.port}")
    print(f"配置文件: {CONFIG_PATH}")
    print("Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("退出。")


if __name__ == "__main__":
    main()
