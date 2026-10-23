# -*- coding: utf-8 -*-
"""收消息 -> 本地大模型 -> 自动回复。

- 触发：轮询 session.db 的 SessionTable（发现会话有新活动，轻量）。
- 取原文：从 message_N.db 的 Msg_<md5(wxid)> 表读「对方发来的文本」完整正文
  （见 wechat_recv.py），比会话摘要更完整。
- 回复：交本地 Ollama 生成，经 hook 发送。

安全默认：dry-run（只检测+打印拟回复，不真发）。
只有用 --reply-to 明确列出的联系人才会真正自动回复。
"""

import argparse
import atexit
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

import wechat_recv as rc
import wechat_memory as mem
import wechat_knowledge as kb
import wechat_persona as persona
import wechat_media as media
import wechat_send as wsend
import wechat_stickers as stickers
import wechat_group_queue as groupq
from llm_provider import (LLMClient, llm_label, parse_llm_config,
                          should_generate_image)
from reply_intelligence import (
    detect_handoff_reason,
    find_duplicate_reply,
    align_image_reply_outcome,
    inter_bubble_delay,
    normalize_reply_text,
    relative_time_label,
    settle_delay,
    trim_unprompted_followups,
    typing_delay,
)

# 默认值由 wechat_memory.DEFAULT_MEMORY_CONFIG 提供，运行时可由 memory 配置覆盖。
KEEP_RECENT = mem.DEFAULT_MEMORY_CONFIG["keep_recent"]
HISTORY_WINDOW = mem.DEFAULT_MEMORY_CONFIG["history_window"]
SUMMARY_BATCH = mem.DEFAULT_MEMORY_CONFIG["summary_batch"]

_personas = {}
_style_profiles = {}
_memory_cfg = mem.parse_memory_config({})
_kb_cfg = kb.parse_config({})
_summary_whitelist = set()
_recent_sent = {}
_sticker_cfg = stickers.parse_config({})

SUMMARY_PROMPT = (
    "你是微信对话的记忆整理器。融合【已有摘要】与【新增对话】，输出一份更新后的背景记忆。"
    "身份称谓必须固定：账号主人统一写「我方」，联系人统一写「对方」；"
    "禁止用容易混淆的「我、你、他、她」代替这两个身份。"
    "只记长期有用的：称呼/关系、持续话题、约定与待办、稳定偏好。"
    "写成连贯短段落；丢掉寒暄与一次性闲聊；冲突信息以新增对话为准。"
    "不要写成话术剧本，不要列出「下次必须提到」的条目。"
    "控制在 120～260 字，直接输出正文。"
)

# 群聊专用：必须按人名区分，禁止把多人混成「对方」
GROUP_SUMMARY_PROMPT = (
    "你是微信群聊的记忆整理器。融合【已有摘要】与【新增对话】，输出更新后的背景记忆。"
    "账号主人固定写「我方」，其他发言人必须写具体群成员名字；"
    "禁止用「我、你、他、她、对方」代替发言人。"
    "新增对话里每一行都带发言人名字；摘要时必须写清「谁说了/做了什么」，"
    "禁止把不同人混成同一个人，禁止用含糊的「对方」「有人」代替具体人名。"
    "可记：成员角色、约定与待办、持续话题。丢掉寒暄。"
    "不要写成话术剧本。控制在 150～320 字，直接输出正文。"
)

# 注入到模型时的软提示：避免模型把摘要当剧本逐条表演
MEMORY_SOFT_PREFIX = (
    "【背景记忆·尽量少用】以下为旧要点，默认忽略。"
    "称谓解释：我方/旧摘要中的“你”=账号主人；对方=联系人。"
    "若某项事实的主人不明确，必须丢弃，不得猜测。"
    "仅当与对方最后一句直接相关时才可轻带一句；"
    "禁止翻旧账、禁止复述、禁止为了「显得记得」硬扯。"
    "\n"
)

SYSTEM_ACCOUNTS = {
    "weixin", "fmessage", "medianote", "filehelper", "floatbottle",
    "qmessage", "qqmail", "tmessage", "newsapp", "notifymessage", "weibo",
    "brandsessionholder", "opencustomerservicemsg", "exmail_tool",
}

# 明显无需回复的短确认（无问号、无请求时）
_ACK_ONLY_RE = re.compile(
    r"^(嗯+|恩+|哦+|噢+|额+|啊+|唔+|好的?|行|可以|收到|知道了|了解|"
    r"ok|okay|yeah|yep|嗯嗯|哈哈+|呵呵+|嘿+|嗨+|谢谢|谢了|辛苦|"
    r"拜拜|再见|在的?|1+|～+|~+|\.+|。+|！+|!+)$",
    re.I,
)
_WX_FACE_RE = re.compile(r"\[[^\[\]]{1,16}\]")


def _is_emoji_or_face_only(text):
    t = _WX_FACE_RE.sub("", text or "")
    # 去掉空白与常见 emoji 区段后若为空，视为纯表情
    out = []
    for ch in t:
        o = ord(ch)
        if ch.isspace():
            continue
        if (
            0x1F300 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x27BF
            or 0x1F600 <= o <= 0x1F64F
            or 0x1F900 <= o <= 0x1F9FF
            or o in (0xFE0F, 0x200D)
        ):
            continue
        out.append(ch)
    return not out and bool((text or "").strip())


def heuristic_should_skip(text, *, group=False):
    """规则层快速跳过：明显不需要回的消息，避免模型硬编一句。"""
    t = (text or "").strip()
    if not t:
        return True, "空消息"
    if t.startswith("[") and t.endswith("]") and len(t) <= 20:
        # [动画表情] [视频] [位置] 等；图片另走识图
        if "图" not in t:
            return True, f"非文本消息{t}"
    # In a private chat, repeated ellipsis is usually an emotional cue or a
    # request for the other person to react. Let the model read its context;
    # groups still keep the conservative short-message rule below.
    if (not group
            and re.fullmatch(r"(?:[.。．…]\s*){2,}", t)):
        return False, ""
    if _ACK_ONLY_RE.match(t):
        return True, "仅确认/寒暄"
    if len(t) <= 12 and _is_emoji_or_face_only(t):
        return True, "纯表情"
    # 群里超短无问号插话，多数时候不需要机器人接
    if group and len(t) <= 4 and "?" not in t and "？" not in t:
        if not any(x in t for x in ("帮", "问", "怎", "吗", "嘛", "呢", "@")):
            return True, "群短句"
    return False, ""

PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply.pid")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply_state.json")
HEALTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply_health.json")
AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply_send_audit.log")
GROUP_QUEUE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "group_reply_queue.db")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply.log")
LOG_ALT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply_run.log")
LOG_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".autoreply_utf8_v2")
_LOG_TARGET = LOG_FILE
_LOG_ENCODING = "utf-8-sig"  # 带 BOM，记事本/PowerShell 默认识别 UTF-8
_MY_PID = os.getpid()
_INSTANCE_MUTEX_HANDLE = None


def _init_log_file():
    """初始化 UTF-8 日志；首次升级时将旧乱码 autoreply.log 备份为 .bak。"""
    global _LOG_TARGET
    if not os.path.isfile(LOG_MARKER):
        try:
            if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
                import shutil
                bak = LOG_FILE + ".bak"
                if os.path.isfile(bak):
                    bak = LOG_FILE + ".bak." + time.strftime("%Y%m%d_%H%M%S")
                shutil.move(LOG_FILE, bak)
            with open(LOG_MARKER, "w", encoding="utf-8") as f:
                f.write("utf-8-sig\n")
        except Exception:
            pass
    for path in (LOG_FILE, LOG_ALT_FILE):
        try:
            with open(path, "a", encoding=_LOG_ENCODING) as f:
                f.write("")
            _LOG_TARGET = path
            return path
        except PermissionError:
            continue
    _LOG_TARGET = LOG_ALT_FILE
    return LOG_ALT_FILE


def _load_handled_ts():
    """跨重启记住已处理过的消息时间戳，避免补检/重启重复回复。"""
    global _recent_sent
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("handled_ts") or {}
    except Exception:
        data, raw = {}, {}
    recent = data.get("recent_replies") or {}
    clean = {}
    now = int(time.time())
    if isinstance(recent, dict):
        for user, items in recent.items():
            valid = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                try:
                    ts = int(item.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
                if text and (not ts or now - ts <= 86400):
                    valid.append({"text": text, "ts": ts})
            if valid:
                clean[str(user)] = valid[-5:]
    if not clean:
        clean = _recent_replies_from_audit()
    _recent_sent = clean
    return {k: int(v) for k, v in raw.items()}


def _recent_replies_from_audit(max_lines=2000):
    """旧状态没有近期回复时，从发送审计恢复，覆盖重启后的索引延迟窗口。"""
    if not os.path.isfile(AUDIT_FILE):
        return {}
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[-max_lines:]
    except Exception:
        return {}
    out = {}
    now = int(time.time())
    pat = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
        r"pid=\d+ TEXT (\S+) \(.*\) (.*)$")
    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        try:
            ts = int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
        except (ValueError, OverflowError):
            continue
        if now - ts > 86400:
            continue
        user, text = m.group(2), m.group(3).strip()
        if text:
            out.setdefault(user, []).append({"text": text, "ts": ts})
            out[user] = out[user][-5:]
    return out


def _save_handled_ts(handled_ts):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "handled_ts": handled_ts,
                "recent_replies": _recent_sent,
            }, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _write_health(state, **details):
    """原子写入真实健康状态，供服务管理器识别“假运行”。"""
    try:
        value = {
            "state": str(state or "unknown"),
            "updated_at": int(time.time()),
            "pid": _MY_PID,
        }
        value.update(details)
        tmp = HEALTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HEALTH_FILE)
    except Exception:
        pass


def classify_health(hook_ok, session_ok, message_db_open, fts_good,
                    circuit_open=False):
    """Classify by the active message path; FTS is only a fallback."""
    if circuit_open or not hook_ok:
        return "blocked"
    if session_ok and message_db_open:
        return "healthy"
    if session_ok or (message_db_open and bool(fts_good)):
        return "degraded"
    return "blocked"


def _remember_sent_reply(username, text):
    value = str(text or "").strip()
    if not username or not value:
        return
    items = list(_recent_sent.get(username) or [])
    items.append({"text": value, "ts": int(time.time())})
    _recent_sent[username] = items[-5:]


def _recent_sent_for_context(username, limit=2):
    now = int(time.time())
    items = [
        x for x in (_recent_sent.get(username) or [])
        if isinstance(x, dict) and str(x.get("text") or "").strip()
        and (not int(x.get("ts") or 0) or now - int(x.get("ts") or 0) <= 900)
    ]
    return items[-max(1, int(limit)):]


def _is_recent_session_self_echo(username, info, max_seconds=30):
    """Reject SessionTable's private-chat echo of text we just sent.

    Some WeChat builds leave last_msg_sender empty for private sessions.  When
    FTS has not indexed that contact, the session summary can otherwise be
    mistaken for a new incoming message.  Exact text plus a tight send-time
    window makes this guard conservative.
    """
    if not info or not info.get("from_session"):
        return False
    text = normalize_reply_text(info.get("text") or "")
    create_time = int(info.get("create_time") or 0)
    if not text or not create_time:
        return False
    for item in reversed(_recent_sent.get(username) or []):
        sent_text = normalize_reply_text(item.get("text") or "")
        sent_at = int(item.get("ts") or 0)
        if (sent_text == text and sent_at
                and -1 <= create_time - sent_at <= int(max_seconds)):
            return True
    return False


def _pid_alive(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            k.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_is_autoreply(pid):
    """确认 PID 是否为 wechat_autoreply.py 进程（避免 pid 文件残留/复用）。"""
    if pid <= 0 or not _pid_alive(pid):
        return False
    if sys.platform != "win32":
        return True
    try:
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction SilentlyContinue).CommandLine",
            ],
            timeout=8,
            creationflags=flags,
        )
        return b"wechat_autoreply.py" in out
    except Exception:
        return False


def acquire_single_instance():
    """禁止多个 autoreply 同时跑（并发 Hook 极易崩微信）。"""
    global _INSTANCE_MUTEX_HANDLE
    my_pid = os.getpid()
    if sys.platform == "win32":
        try:
            kernel = __import__("ctypes").windll.kernel32
            identity = hashlib.md5(
                os.path.abspath(__file__).lower().encode("utf-8")
            ).hexdigest()
            name = f"Local\\WeChatAutoReply_{identity}"
            handle = kernel.CreateMutexW(None, False, name)
            # ERROR_ALREADY_EXISTS=183。命名互斥量不依赖 pid 文件和命令行权限，
            # 能阻止服务脚本漏识别高权限旧进程后再启动第二个实例。
            if not handle or kernel.GetLastError() == 183:
                if handle:
                    kernel.CloseHandle(handle)
                log("[!] 已有 autoreply 持有系统互斥锁，本进程退出")
                return False
            _INSTANCE_MUTEX_HANDLE = handle

            def _close_mutex():
                global _INSTANCE_MUTEX_HANDLE
                if _INSTANCE_MUTEX_HANDLE:
                    kernel.CloseHandle(_INSTANCE_MUTEX_HANDLE)
                    _INSTANCE_MUTEX_HANDLE = None

            atexit.register(_close_mutex)
        except Exception as e:
            log(f"[!] 创建单实例互斥锁失败，回退 pid 检查: {e}")
    if os.path.isfile(PID_FILE):
        try:
            old = int(open(PID_FILE, encoding="utf-8").read().strip())
            if old != my_pid and _pid_is_autoreply(old):
                log(f"[!] 已有 autoreply 在运行 (PID {old})，本进程退出")
                return False
            if old != my_pid:
                try:
                    os.remove(PID_FILE)
                except Exception:
                    pass
        except Exception:
            pass
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(my_pid))

    def _cleanup():
        try:
            if os.path.isfile(PID_FILE):
                cur = int(open(PID_FILE, encoding="utf-8").read().strip())
                if cur == my_pid:
                    os.remove(PID_FILE)
        except Exception:
            pass

    atexit.register(_cleanup)
    return True


def _log_lifecycle(event):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    path = _init_log_file()
    try:
        with open(path, "a", encoding=_LOG_ENCODING) as f:
            f.write(f"==== autoreply {event} {ts} pid={_MY_PID} ====\n")
    except Exception:
        pass

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    global _LOG_TARGET
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [pid={_MY_PID}] {msg}"
    for attempt in range(5):
        try:
            with open(_LOG_TARGET, "a", encoding=_LOG_ENCODING) as f:
                f.write(line + "\n")
            break
        except PermissionError:
            _init_log_file()
            if attempt + 1 < 5:
                time.sleep(0.15 * (attempt + 1))
        except Exception:
            break
    try:
        if sys.stdout.isatty():
            print(line, flush=True)
    except Exception:
        pass


def audit_send(action, user, detail):
    """发送审计：即使主日志分流/双进程，也能追溯谁发了什么。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    who = contact_name(user) if user else ""
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} pid={_MY_PID} {action} {user} ({who}) {detail}\n")
    except Exception:
        pass


_db_busy_log = {}  # key -> {"ts": float, "n": int}


def _log_db_busy(key, msg):
    """限频记录数据库繁忙（瞬时冲突，非致命错误）。"""
    now = time.monotonic()
    rec = _db_busy_log.get(key, {"ts": 0.0, "n": 0})
    rec["n"] += 1
    if now - rec["ts"] >= 120:
        suffix = f"（近120s共{rec['n']}次）" if rec["n"] > 1 else ""
        log(f"   [~] {msg}{suffix}")
        rec["ts"] = now
        rec["n"] = 0
    _db_busy_log[key] = rec


_last_keepalive_ts = 0.0
_last_heartbeat_ts = 0.0
_last_msg_activity_ts = 0.0  # monotonic：上次观察到会话时间戳前进或处理消息

# 稳定性优先：只用 SessionTable 的时间戳做高频变化检测；具体消息库仅在
# 会话变化、待发送/连发状态或低频兜底时读取。此前白名单每轮（默认 3 秒）
# 都读取具体消息，长时间空闲也会持续打入进程内 Hook。
SESSION_PROBE_EVERY = 2       # 默认 interval=3s，即约 6 秒
FALLBACK_POLL_EVERY = 20      # 默认约 60 秒，白名单联系人轮转兜底
SOFT_KEEPALIVE_ENABLED = False  # ShowWindow 对 DB 保活无可靠收益，默认禁用
# Long enough to survive a service restart/health probe cycle, but still short
# enough not to resurrect an old conversation after a long offline period.
STARTUP_CATCHUP_SECONDS = 30 * 60


def polling_schedule(loop_no, target_count, session_fail_streak=0):
    """返回 (本轮是否探测会话, 低频兜底联系人索引)。

    纯函数便于验证轮询节奏；session 异常时保持原来的慢速重试，避免故障期
    继续施压。兜底联系人按轮次轮转，防止固定只检查第一个白名单联系人。
    """
    session_every = SESSION_PROBE_EVERY if session_fail_streak == 0 else 30
    do_session = int(loop_no) % session_every == 0
    fallback_index = None
    if target_count and int(loop_no) % FALLBACK_POLL_EVERY == 0:
        fallback_round = int(loop_no) // FALLBACK_POLL_EVERY - 1
        fallback_index = fallback_round % int(target_count)
    return do_session, fallback_index


def generation_retry_delay(attempt):
    """模型/网络瞬时失败后的退避秒数，避免同一消息被高频重复处理。"""
    delays = (30, 90, 300, 600)
    i = max(0, min(int(attempt) - 1, len(delays) - 1))
    return delays[i]


def _soft_keepalive_wechat(min_interval=120.0):
    """不抢焦点地轻触微信窗口，降低 DB 句柄被长期释放的概率。"""
    global _last_keepalive_ts
    if not SOFT_KEEPALIVE_ENABLED or os.name != "nt":
        return False
    now = time.monotonic()
    if now - _last_keepalive_ts < min_interval:
        return False
    _last_keepalive_ts = now
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, "微信")
        if not hwnd:
            return False
        # SW_SHOWNOACTIVATE=4：显示但不抢前台
        u.ShowWindow(hwnd, 4)
        try:
            rc.invalidate_opened_dbs_cache()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _is_hook_transport_error(err):
    s = str(err or "")
    return (
        "10054" in s or "10061" in s or "积极拒绝" in s
        or "Hook 繁忙" in s or "远程主机" in s
        or "Connection" in s
        or "timed out" in s.lower()
        or "HTTP Error 500" in s
    )


def _pause_if_hook_circuit():
    """断路器打开时整轮休眠，避免继续打微信库导致「修复数据库」。"""
    if not rc.hook_circuit_open():
        return False
    # 状态接口不执行 SQL；若检测到微信 PID 已变化，wechat_recv 会自动解除旧闭锁。
    rc.hook_ready(timeout=2)
    if not rc.hook_circuit_open():
        log("   [Hook] 检测到微信已重启，解除旧进程读库闭锁")
        return False
    info = rc.hook_circuit_info()
    if info.get("latched"):
        _write_health("blocked", hook_ready=rc.hook_ready(timeout=2),
                      reason=info.get("reason", "")[:160])
        log(f"[!] Hook 读库已安全闭锁 60s（需重启微信后恢复）: {info.get('reason', '')[:80]}")
        time.sleep(60)
        return True
    wait = max(5, min(int(info.get("seconds_left") or 30), 90))
    _write_health("degraded", hook_ready=rc.hook_ready(timeout=2),
                  reason=info.get("reason", "")[:160], retry_in=wait)
    log(f"[!] Hook 读库断路 {wait}s（防微信库损坏）: {info.get('reason', '')[:80]}")
    time.sleep(wait)
    return True


def http_post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_contact_name_cache = {}  # username -> (name, mono_ts)


def contact_name(username):
    """联系人备注/昵称（缓存 10 分钟，减少 contact.db 查询）。"""
    now = time.monotonic()
    cached = _contact_name_cache.get(username)
    if cached and now - cached[1] < 600:
        return cached[0]
    name = username
    k = username.replace("'", "''")
    try:
        rows = rc.query(
            "contact.db",
            f"SELECT remark, nick_name FROM contact WHERE username='{k}' LIMIT 1",
            retries=1,
        )
        if rows:
            name = rows[0].get("remark") or rows[0].get("nick_name") or username
    except Exception:
        if cached:
            return cached[0]
    _contact_name_cache[username] = (name, now)
    return name


def resolve_targets(names):
    out = set()
    for n in names:
        n = n.strip()
        if not n:
            continue
        k = n.replace("'", "''")
        try:
            rows = rc.query("contact.db",
                            "SELECT username FROM contact WHERE "
                            f"(username='{k}' OR remark LIKE '%{k}%' OR "
                            f"nick_name LIKE '%{k}%' OR alias LIKE '%{k}%') LIMIT 1")
            out.add(rows[0]["username"] if rows else n)
        except Exception:
            out.add(n)
    return out


def llm_chat(messages, timeout=180):
    return _llm_client.chat(messages, timeout=timeout)


_llm_client = LLMClient()


def _turns_to_text(turns, *, group=False):
    """把历史轮次打成摘要用文本。群聊必须带发言人，避免多人身份混淆。"""
    lines = []
    for t in turns:
        if t.get("role") == "assistant":
            who = "我方"
        elif group:
            sender = (t.get("sender") or "").strip()
            who = contact_name(sender) if sender else "群成员"
            if sender and who == sender:
                # contact 查不到时至少保留 wxid 片段，避免全叫「对方」
                who = sender if len(sender) <= 24 else sender[:20] + "…"
        else:
            who = "对方"
        lines.append(f"{who}: {t.get('text') or ''}")
    return "\n".join(lines)


def summarize(username, prev_summary, turns):
    """把新增对话折叠进长期摘要；失败返回 None，调用方不得推进游标。"""
    if not turns:
        return None
    group = (username or "").endswith("@chatroom")
    prompt = GROUP_SUMMARY_PROMPT if group else SUMMARY_PROMPT
    content = (f"【已有摘要】\n{prev_summary or '（暂无）'}\n\n"
               f"【新增对话】\n{_turns_to_text(turns, group=group)}")
    try:
        new_sum = llm_chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ])
        return (new_sum or "").strip() or None
    except Exception as e:
        log(f"[!] 摘要生成失败({username}): {e}")
        return None


def _can_auto_summarize(username):
    if not _memory_cfg.get("whitelist_only", True):
        return True
    return username in _summary_whitelist


def _authorized_knowledge_hits(username, latest_text, provider=None):
    """Retrieve only for an explicitly whitelisted and document-authorized chat."""
    if (not _kb_cfg["enabled"] or not str(latest_text or "").strip()
            or username not in _summary_whitelist):
        return []
    provider = provider or _llm_client.llm.get("provider")
    if (provider != "ollama"
            and not _kb_cfg["allow_cloud"]):
        return []
    try:
        return kb.search(
            username, latest_text, _summary_whitelist,
            max_hits=_kb_cfg["max_hits"],
            max_chars=_kb_cfg["max_chars"],
        )
    except Exception as exc:
        log(f"   [知识库] 检索失败，跳过引用: {exc}")
        return []


def _cite_knowledge(plan, hits):
    if not hits or (plan.get("action") or "").lower() != "reply":
        return plan
    bubbles = list(plan.get("bubbles") or [])
    if not bubbles:
        reply = str(plan.get("reply") or "").strip()
        bubbles = [reply] if reply else []
    if not bubbles:
        return plan
    marker = kb.citation(hits)
    if marker and not any("资料来源：" in item for item in bubbles):
        bubbles[-1] = bubbles[-1].rstrip() + "\n" + marker
    plan["bubbles"] = bubbles
    plan["reply"] = "\n".join(bubbles)
    plan["knowledge_sources"] = list(dict.fromkeys(
        hit["title"] for hit in hits))
    return plan


def _speaker_ownership_prompt(username, recent):
    """生成显式说话人/事实归属表，避免仅靠 API role 被模型读反。"""
    group = (username or "").endswith("@chatroom")
    if group:
        identity = (
            "role=assistant /「我方原话」=账号主人本人；"
            "role=user /「群成员原话」=标签中写明的那位群成员。"
        )
    else:
        other = contact_name(username) or "当前联系人"
        identity = (
            "role=assistant /「我方原话」=账号主人本人；"
            f"role=user /「对方原话」=联系人“{other}”。"
        )
    lines = [
        "【说话人归属校验·最高优先级】",
        identity,
        "每句话里的“我/我的/我这边”只属于该句说话人，绝不跨角色转换。",
        "当前回复只能回应最后一条对方/群成员原话。",
        "我方历史仅用于理解上下文和语气；除非对方明确询问，"
        "不要复述、重报或改写我方之前的状态。",
        "最近归属摘录（仅用于核对主人，不是要求逐句回应）：",
    ]
    for h in (recent or [])[-12:]:
        text = str(h.get("text") or "").replace("\n", " ").strip()[:120]
        if not text:
            continue
        if h.get("role") == "assistant":
            who = "我方"
        elif group:
            sender = (h.get("sender") or "").strip()
            who = contact_name(sender) if sender else "群成员"
            if sender and who == sender and len(who) > 24:
                who = who[:20] + "…"
        else:
            who = "对方"
        lines.append(f"- {who}说：{text}")
    return "\n".join(lines)


def build_context(username, knowledge_hits=None):
    """构建带【长期摘要 + 最近原文】的多轮上下文；必要时增量摘要。

    记忆按 username 独立存储在 memory/<md5>.db。
    """
    keep_recent = int(_memory_cfg.get("keep_recent") or KEEP_RECENT)
    history_window = int(_memory_cfg.get("history_window") or HISTORY_WINDOW)
    summary_batch = int(_memory_cfg.get("summary_batch") or SUMMARY_BATCH)
    history = rc.recent_history(username, limit=history_window)
    summary, last_seq = mem.load(username)
    # 微信库暂不可读时，至少保留长期摘要，避免突然“失忆”
    if not history:
        if not summary:
            return None
        messages = [
            {"role": "system",
             "content": persona.build_system_prompt(
                 username, _personas, _style_profiles)},
            {"role": "system",
             "content": _speaker_ownership_prompt(username, [])},
            {"role": "system",
             "content": MEMORY_SOFT_PREFIX + summary},
        ]
        if knowledge_hits:
            messages.append({
                "role": "system",
                "content": kb.format_for_prompt(knowledge_hits),
            })
        return messages
    # 尚未摘要的原文（sort_seq 严格大于已摘要位置）
    unsummarized = [h for h in history if h["sort_seq"] > last_seq]

    # 对话变长：把较早的一批折叠进长期摘要
    if (_can_auto_summarize(username)
            and len(unsummarized) > keep_recent + summary_batch):
        to_sum = unsummarized[:-keep_recent]
        new_summary = summarize(username, summary, to_sum)
        if new_summary:
            new_last_seq = to_sum[-1]["sort_seq"]
            try:
                mem.save(username, new_summary, new_last_seq)
            except Exception as e:
                log(f"   [记忆] 保存失败，保留原游标等待重试: {e}")
            else:
                summary = new_summary
                last_seq = new_last_seq
                log(f"   [记忆] 已摘要 {len(to_sum)} 条 -> "
                    f"长期记忆(seq<= {last_seq})")
        else:
            log("   [记忆] 本轮摘要未更新，保留原游标等待重试")

    # 始终带最近 KEEP_RECENT 条原文，优先接当前话头（比只喂摘要更像真人）
    recent = history[-keep_recent:]
    group = (username or "").endswith("@chatroom")

    messages = [{"role": "system",
                 "content": persona.build_system_prompt(
                     username, _personas, _style_profiles)}]
    messages.append({
        "role": "system",
        "content": _speaker_ownership_prompt(username, recent),
    })
    # 微信库无法区分本人手发与旧机器人回复，默认不拿它们自动学习，
    # 避免把历史机器味自我强化；确认历史以本人手发为主时可显式开启。
    if _llm_client.smart_reply.get("learn_recent_style"):
        observed = persona.observed_style_prompt(history)
        if observed:
            messages.append({"role": "system", "content": observed})
    now_ts = int(time.time())
    if any(int(h.get("create_time") or 0) > 0 for h in recent):
        messages.append({
            "role": "system",
            "content": (
                f"【时间信息】现在是 {time.strftime('%Y-%m-%d %H:%M')}。"
                "消息前的〔时间〕只用于判断话题是否连续；"
                "不要在回复中复述这些时间标签。"
            ),
        })
    if summary:
        prefix = MEMORY_SOFT_PREFIX
        if group:
            prefix = (
                "【背景记忆·仅供参考】群聊要点如下。"
                "我方=账号主人；其他事实必须归属于明确写出的群成员姓名。"
                "注意区分不同群成员；回复时认准最后发言的那个人，"
                "不要张冠李戴，不要把别人的话安到另一人头上。"
                "\n"
            )
        messages.append({"role": "system", "content": prefix + summary})
    if knowledge_hits:
        messages.append({
            "role": "system",
            "content": kb.format_for_prompt(knowledge_hits),
        })
    just_sent = _recent_sent_for_context(username)
    if just_sent:
        sent_text = "\n".join(
            f"- {str(x.get('text') or '')[:200]}" for x in just_sent)
        messages.append({
            "role": "system",
            "content": (
                "【本程序刚刚已经发出的消息】\n" + sent_text +
                "\n微信消息库可能尚未同步。以上内容已经发过，"
                "除非对方明确要求重复，否则不得再次输出相同或近似回复；"
                "只承接对方最新补充的信息。"
            ),
        })
    for h in recent:
        text = h.get("text") or ""
        if group and h.get("role") == "user":
            sender = (h.get("sender") or "").strip()
            label = contact_name(sender) if sender else "群成员"
            if sender and label == sender and len(label) > 24:
                label = label[:20] + "…"
            text = f"〔群成员“{label}”原话〕{text}"
        elif h.get("role") == "assistant":
            text = f"〔我方原话〕{text}"
        else:
            text = f"〔对方原话〕{text}"
        label = relative_time_label(h.get("create_time"), now=now_ts)
        if label:
            text = f"〔{label}〕{text}"
        messages.append({"role": h["role"], "content": text})
    return messages


def ask_ollama_chat(username):
    """基于【长期摘要 + 最近原文】做多轮上下文回复。"""
    messages = build_context(username)
    if not messages:
        return None
    try:
        return llm_chat(messages)
    except Exception as e:
        log(f"[x] 大模型失败: {e}")
        return None


def ask_vision_chat(username, image_path):
    """识图回复。"""
    messages = build_context(username) or [
        {"role": "system",
         "content": persona.build_system_prompt(
             username, _personas, _style_profiles)},
    ]
    try:
        return _llm_client.chat_vision(
            messages, [image_path],
            prompt_extra="对方发来了一张图片。请结合对话上下文理解图片内容，"
                         "用简短自然的中文回复，像真人微信聊天。",
        )
    except Exception as e:
        log(f"[x] 识图失败: {e}")
        return None


def ask_group_batch(username, info):
    """处理一个已合并的群批次；多张图片按顺序一次送入识图模型。"""
    batch_messages = list(info.get("group_messages") or [])
    context_images = list(info.get("context_images") or [])
    image_items = [x for x in batch_messages if x.get("kind") == "image"]
    image_items.extend(context_images)
    max_images = max(1, int(info.get("max_batch_images") or 3))
    image_items = image_items[-max_images:]
    latest_text = next(
        (str(x.get("text") or "") for x in reversed(batch_messages)
         if x.get("kind") == "text" and str(x.get("text") or "").strip()),
        "",
    )
    if not image_items:
        return ask_chat_with_optional_image(username, latest_text)
    if not _llm_client.vision.get("enabled"):
        log("   [群跳过] 批次含图片但识图未开启")
        return None, None, True, {
            "action": "skip", "reason": "识图未开启", "bubbles": []}

    try:
        image_paths = list(info.get("_image_paths") or [])
        if not image_paths:
            seen = set()
            for item in image_items:
                fp = (int(item.get("sort_seq") or 0),
                      int(item.get("local_id") or 0),
                      str(item.get("sender") or ""))
                if fp in seen:
                    continue
                seen.add(fp)
                image_paths.append(media.prepare_incoming_image(item, username))
            info["_image_paths"] = image_paths
        knowledge_hits = _authorized_knowledge_hits(
            username, latest_text,
            provider=_llm_client.vision.get("provider"))
        context = build_context(
            username, knowledge_hits=knowledge_hits) or [
            {"role": "system", "content": persona.build_system_prompt(
                username, _personas, _style_profiles)},
        ]
        transcript = groupq.format_batch(batch_messages, contact_name)
        latest_sender = contact_name(
            str(batch_messages[-1].get("sender") or "群成员"))
        prompt = (
            "微信群在一个短时间窗口内出现了以下消息：\n" + transcript +
            f"\n主要回应最后发言人“{latest_sender}”。"
            "图片按消息出现顺序提供；结合文字理解引用关系。"
            "不要把不同成员混为一人，不要逐条机械回复，只发一条自然、克制的群聊回复。"
        )
        reply = _llm_client.chat_vision(
            context, image_paths, prompt_extra=prompt, timeout=180)
        plan = {
            "action": "reply" if reply else "skip",
            "bubbles": [reply] if reply else [],
            "reply": reply or "",
        }
        plan = _cite_knowledge(plan, knowledge_hits)
        return (plan.get("reply") or None), None, False, plan
    except Exception as e:
        log(f"[x] 群批次识图失败: {e}")
        return None, None, False, {}


def ask_chat_with_optional_image(username, latest_text=""):
    """文字回复 + 可否跳过 +（可选）生图。返回 (reply, gen_img, skipped, plan)。"""
    handoff = detect_handoff_reason(latest_text, _llm_client.smart_reply)
    if handoff:
        log(f"   [人工接管] {handoff}，不自动回复")
        audit_send("HANDOFF", username, handoff)
        return None, None, True, {
            "action": "handoff", "reason": handoff, "bubbles": []}
    # 规则层先挡掉纯寒暄，避免每句都打扰对方
    skip, reason = heuristic_should_skip(
        latest_text, group=(username or "").endswith("@chatroom"))
    if skip:
        log(f"   [跳过] {reason}")
        return None, None, True, {"action": "skip", "reason": reason}

    knowledge_hits = _authorized_knowledge_hits(username, latest_text)
    messages = build_context(username, knowledge_hits=knowledge_hits)
    if not messages:
        return None, None, False, {}

    gcfg = _llm_client.image_gen
    gen_img = None

    try:
        plan = _llm_client.chat_with_image_plan(messages, latest_text)
    except Exception as e:
        log(f"[x] 回复失败: {e}")
        return None, None, False, {}

    action = (plan.get("action") or "").lower()
    if action == "handoff":
        reason = plan.get("reason") or "模型判断需本人处理"
        log(f"   [人工接管] {reason}")
        audit_send("HANDOFF", username, reason)
        return None, None, True, plan
    if action == "skip" or not (
            (plan.get("reply") or "").strip() or plan.get("send_image")):
        log("   [跳过] 模型判断无需回复")
        return None, None, True, plan

    bubbles, removed = trim_unprompted_followups(
        plan.get("bubbles") or [plan.get("reply") or ""],
        plan,
        _llm_client.smart_reply,
    )
    if removed:
        log(f"   [事实约束] 已移除无依据追问: {' / '.join(removed)[:120]}")
    plan["bubbles"] = bubbles
    plan["reply"] = "\n".join(bubbles)
    if not bubbles and not plan.get("send_image"):
        log("   [事实约束] 清理后无有效回复，本次跳过")
        return None, None, True, plan

    duplicate = find_duplicate_reply(
        bubbles, _recent_sent.get(username) or [], _llm_client.smart_reply)
    if duplicate and _llm_client.smart_reply.get("regenerate_on_duplicate"):
        log(f"   [重复重写] 与近期回复相似度 {duplicate['similarity']:.0%}，重写一次")
        try:
            plan = _llm_client.rewrite_duplicate_reply(
                messages,
                latest_text,
                duplicate["old_text"],
                plan,
            )
        except Exception as e:
            log(f"   [!] 重复重写失败: {e}")
        bubbles, removed = trim_unprompted_followups(
            plan.get("bubbles") or [plan.get("reply") or ""],
            plan,
            _llm_client.smart_reply,
        )
        if removed:
            log(f"   [事实约束] 重写后移除无依据追问: {' / '.join(removed)[:120]}")
        plan["bubbles"] = bubbles
        plan["reply"] = "\n".join(bubbles)
        if ((plan.get("action") or "").lower() != "reply"
                or (not bubbles and not plan.get("send_image"))):
            log("   [重复拦截] 重写后无有效新回复，本次跳过")
            return None, None, True, plan
        duplicate = find_duplicate_reply(
            bubbles, _recent_sent.get(username) or [], _llm_client.smart_reply)

    if duplicate:
        if plan.get("send_image"):
            log("   [重复拦截] 文字与近期回复重复，本次仅保留图片")
            plan["bubbles"] = []
            plan["reply"] = ""
        else:
            detail = f"similarity={duplicate['similarity']:.3f}"
            log(f"   [重复拦截] {detail}，不再次发送")
            audit_send("BLOCK_DUPLICATE", username, detail)
            plan["action"] = "skip"
            plan["reason"] = "与近期已发送回复重复"
            return None, None, True, plan

    reply = (plan.get("reply") or "").strip()
    if gcfg.get("enabled") and gcfg.get("mode") == "keyword":
        attempted_image = should_generate_image(latest_text, gcfg)
        if attempted_image:
            prompt = latest_text or reply or ""
            try:
                gen_img = _llm_client.generate_image(prompt)
                log(f"   [关键词生图] {prompt[:60]}")
            except Exception as e:
                log(f"[x] 生图失败: {e}")
            plan = align_image_reply_outcome(plan, bool(gen_img))
            plan["image_outcome_managed"] = True
            reply = plan.get("reply") or ""
            if gen_img and plan.get("image_text_rewritten"):
                log("   [图文一致性] 生图成功，已将拒绝配文改为同意语气")
            elif not gen_img:
                log("   [图文一致性] 生图失败，已改为明确说明无法发送")
        plan = _cite_knowledge(plan, knowledge_hits)
        return (plan.get("reply") or None), gen_img, False, plan

    if plan.get("send_image"):
        image_prompt = (
            (plan.get("image_prompt") or "").strip()
            or (latest_text or "").strip()
            or reply
        )
        if knowledge_hits and not _kb_cfg["allow_cloud"]:
            # A local text model may draft an image prompt from the KB.
            # Only the contact's own words may go to an external image service.
            image_prompt = (latest_text or "").strip()
        log(f"   [AI决定发图] {image_prompt[:80]}")
        try:
            if not image_prompt:
                raise RuntimeError("缺少可用的生图描述")
            gen_img = _llm_client.generate_image(image_prompt)
        except Exception as e:
            err = str(e)
            log(f"[x] 生图失败: {e}")
            if "cookie" in err.lower() or "过期" in err or "失效" in err or "cdp" in err.lower():
                log("   [!] 请确认 chrome_gemini_debug.bat 打开的 Chrome 在运行且已登录 gemini")
        plan = align_image_reply_outcome(plan, bool(gen_img))
        plan["image_outcome_managed"] = True
        reply = plan.get("reply") or ""
        if gen_img and plan.get("image_text_rewritten"):
            log("   [图文一致性] 生图成功，已将拒绝配文改为同意语气")
        elif not gen_img:
            log("   [图文一致性] 生图失败，已改为明确说明无法发送")
    plan = _cite_knowledge(plan, knowledge_hits)
    return (plan.get("reply") or None), gen_img, False, plan


def maybe_generate_image(text):
    """兼容旧逻辑；smart 模式下由 ask_chat_with_optional_image 处理。"""
    if not should_generate_image(text, _llm_client.image_gen):
        return None
    try:
        return _llm_client.generate_image(text)
    except Exception as e:
        log(f"[x] 生图失败: {e}")
        return None


def send(username, msg):
    if not rc.hook_ready():
        log("[x] Hook 未就绪，取消发送")
        return False
    time.sleep(1.0)
    try:
        return wsend.send_text(username, msg)
    except Exception as e:
        log(f"[x] 发送失败: {e}")
        return False


def send_image(username, image_path):
    if not rc.hook_ready():
        log("[x] Hook 未就绪，取消发图")
        return False
    time.sleep(1.0)
    try:
        return wsend.send_image(username, image_path)
    except Exception as e:
        log(f"[x] 发图失败: {e}")
        return False


def active_sessions(only_users=None, retries=3):
    """返回 [(username, last_timestamp, last_msg_type)]，含文本与图片会话。

    only_users: 仅查白名单（减轻 session.db 锁冲突）；None 表示全表。
    retries: session.db 重试次数（默认较少，避免整轮阻塞过久）。
    """
    if only_users:
        parts = []
        for u in only_users:
            k = str(u).replace("'", "''")
            parts.append(f"'{k}'")
        in_clause = ",".join(parts)
        sql = (
            "SELECT username, last_timestamp, last_msg_type FROM SessionTable "
            f"WHERE last_msg_type IN (1, 3) AND username IN ({in_clause})"
        )
    else:
        sql = (
            "SELECT username, last_timestamp, last_msg_type FROM SessionTable "
            "WHERE last_msg_type IN (1, 3)"
        )
    rows = rc.query("session.db", sql, retries=retries)
    return [
        (r["username"], int(r["last_timestamp"]), int(r.get("last_msg_type") or 0))
        for r in rows
    ]


def poll_reply_candidate(username, vision_enabled=False, probe_image=False):
    """读取本轮候选消息；图片探测时按真实时间取文本/图片中的最新一条。

    不能先固定返回“最近文本”再把图片当兜底，否则联系人只要历史上发过
    文本，后续新图片就会永远被旧文本挡住。
    """
    if vision_enabled and probe_image:
        full = None
        if rc.message_sqlite_open():
            full = rc.last_incoming_message(
                username, image_types=media.IMAGE_LOCAL_TYPES)
        session_image = media.incoming_image_from_session(username)
        if session_image and (
                not full
                or int(session_image.get("create_time") or 0)
                >= int(full.get("create_time") or 0)):
            return session_image
        if full:
            return full
    return rc.poll_incoming_text(username)


def settled_candidate(current, username, burst_info, settle_until, now=None):
    """Keep a candidate alive if unread_count drops during burst settling."""
    if current:
        return current
    snapshot = burst_info.get(username)
    if not snapshot:
        return None
    clock = time.monotonic() if now is None else float(now)
    if clock < float(settle_until.get(username) or 0):
        return None
    return dict(snapshot)


def _message_key(user, info):
    """去重键：优先 create_time（比 local_id 稳定），群聊再加 sender。"""
    cts = int(info.get("create_time") or 0)
    lid = int(info.get("local_id") or 0)
    if user.endswith("@chatroom"):
        return (user, cts, lid, info.get("sender") or "")
    return (user, cts, lid)


def _already_handled(user, info, replied, handled_ts):
    """同一条消息是否已处理过（内存 + 持久化时间戳）。"""
    msg_key = _message_key(user, info)
    cts = int(info.get("create_time") or 0)
    lid = int(info.get("local_id") or 0)
    if replied.get(msg_key) == lid:
        return True
    # 群批次由 SQLite 队列的逐消息状态去重；同一秒内可能有多位成员发言，
    # 不能再用会话级 create_time 把后面的消息一起误判为已处理。
    if info.get("kind") == "group_batch":
        return False
    if cts and cts <= int(handled_ts.get(user) or 0):
        return True
    return False


def _mark_handled(user, info, replied, handled_ts):
    msg_key = _message_key(user, info)
    cts = int(info.get("create_time") or 0)
    lid = int(info.get("local_id") or 0)
    replied[msg_key] = lid
    if cts:
        handled_ts[user] = max(int(handled_ts.get(user) or 0), cts)
        _save_handled_ts(handled_ts)


def _delivery_done(pending_item, reply, gen_img, skip_text=False):
    """文本/图片是否都已发送完成（发图最多尝试一次，防 Hook 误报重复发）。"""
    if not pending_item:
        return False
    bubbles = pending_item.get("bubbles") or ([reply] if reply else [])
    states = pending_item.get("bubble_states") or []
    if bubbles and not skip_text:
        if states:
            if any(not x.get("attempted") for x in states):
                return False
        elif not pending_item.get("text_sent"):
            if not pending_item.get("text_attempted"):
                return False
    if gen_img and not pending_item.get("img_sent"):
        if pending_item.get("img_attempted"):
            return True  # 发图已尝试过（含失败），不再重试
        return False
    return bool(reply or gen_img)


def load_config(path):
    """读配置文件，返回 dict；文件不存在/出错返回 {}。"""
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"[!] 读取配置失败: {e}")
        return {}


def resolve_runtime(cfg, args):
    """把配置+命令行合并成运行时参数。"""
    interval = float(cfg.get("interval", args.interval))
    reply_to = args.reply_to
    if not reply_to:
        rt = cfg.get("reply_to", [])
        reply_to = ",".join(rt) if isinstance(rt, list) else str(rt)
    reply_all = args.reply_all or bool(cfg.get("reply_all", False))
    include_system = args.include_system or bool(cfg.get("include_system", False))
    llm_cfg = parse_llm_config(cfg)
    if args.model:
        llm_cfg["model"] = args.model
        if llm_cfg.get("provider") == "ollama" or not cfg.get("llm"):
            llm_cfg["provider"] = "ollama"
    targets = resolve_targets(reply_to.split(",")) if reply_to else set()
    try:
        settle_seconds = float(cfg.get("settle_seconds", 15))
    except (TypeError, ValueError):
        settle_seconds = 15.0
    settle_seconds = max(5.0, min(60.0, settle_seconds))
    return targets, reply_all, include_system, interval, llm_cfg, settle_seconds


def main():
    ap = argparse.ArgumentParser(description="微信收消息->本地大模型->自动回复")
    ap.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒")
    ap.add_argument("--reply-to", default="",
                    help="逗号分隔名字/wxid，仅这些联系人真正自动回复；留空=dry-run")
    ap.add_argument("--include-system", action="store_true",
                    help="也处理系统号/公众号（默认忽略）")
    ap.add_argument("--reply-all", action="store_true",
                    help="对所有非系统联系人自动回复（危险，谨慎）")
    ap.add_argument("--model", default="", help="覆盖使用的 Ollama 模型")
    ap.add_argument("--config", help="JSON 配置文件（interval/reply_to/reply_all/include_system/model）")
    args = ap.parse_args()

    if not acquire_single_instance():
        return 1

    atexit.register(lambda: _log_lifecycle("exited"))
    atexit.register(lambda: _write_health("stopped"))
    _init_log_file()
    _log_lifecycle("started")
    _write_health("starting")

    global _llm_client, _personas, _style_profiles
    global _memory_cfg, _summary_whitelist, _sticker_cfg, _kb_cfg

    cfg = load_config(args.config)
    autoreply_enabled = bool(cfg.get("enabled", True))
    targets, reply_all, include_system, interval, llm_cfg, settle_seconds = \
        resolve_runtime(cfg, args)
    _llm_client.update(cfg)
    _personas = persona.parse_personas(cfg)
    _style_profiles = persona.parse_style_profiles(cfg)
    _memory_cfg = mem.parse_memory_config(cfg)
    _kb_cfg = kb.parse_config(cfg)
    _sticker_cfg = stickers.parse_config(cfg)
    group_cfg = groupq.parse_group_reply_config(cfg)
    _summary_whitelist = set(targets)
    cfg_mtime = (os.path.getmtime(args.config)
                 if args.config and os.path.exists(args.config) else 0)
    dry = not targets and not reply_all

    log("== 微信自动回复 ==")
    log(f"大模型: {llm_label(llm_cfg)}  自定义人格: {len(_personas)} 个  "
        f"风格画像: {len(_style_profiles)} 个  "
        f"短期原文: {_memory_cfg['keep_recent']}条  "
        f"历史窗口: {_memory_cfg['history_window']}")
    log(f"长期记忆: 超过 {_memory_cfg['keep_recent'] + _memory_cfg['summary_batch']} "
        f"条未摘要消息后增量更新；"
        f"{'仅白名单' if _memory_cfg['whitelist_only'] else '所有已处理会话'}")
    log(f"知识库: {'开' if _kb_cfg['enabled'] else '关'}  "
        f"云模型读取: {'允许' if _kb_cfg['allow_cloud'] else '禁止'}")
    v = _llm_client.vision
    g = _llm_client.image_gen
    log(f"表情包: {'开' if _sticker_cfg['enabled'] else '关'}  "
        f"概率:{_sticker_cfg['probability']:.0%}/强语境:"
        f"{_sticker_cfg['strong_probability']:.0%}  "
        f"冷却:{_sticker_cfg['cooldown_seconds'] // 60}分钟")
    g_label = (f"开/{g.get('provider', 'jimeng')}/{g.get('mode', 'smart')}"
               if g.get("enabled") else "关")
    log(f"识图: {'开/' + v.get('model','') if v.get('enabled') else '关'}  "
        f"生图: {g_label}")
    mode = ("已禁用" if not autoreply_enabled else
            "dry-run(只打印)" if dry
            else ("回复所有非系统联系人" if reply_all else f"回复 -> {sorted(targets)}"))
    smart = _llm_client.smart_reply
    timing_label = (
        f"动态{smart['settle_min']:.0f}~{smart['settle_max']:.0f}s"
        if smart.get("enabled") and smart.get("dynamic_timing")
        else f"固定{settle_seconds:.0f}s"
    )
    log(f"智能回复: {'两阶段' if smart.get('two_stage') else '兼容模式'}  "
        f"连发等待: {timing_label}")
    log("活跃群队列: "
        f"{'开' if group_cfg['enabled'] else '关'}  "
        f"合并:{group_cfg['settle_seconds']}s  "
        f"过期:{group_cfg['expiry_seconds']}s  "
        f"触发:{'必须@' if group_cfg['require_mention'] else '规则判断'}")
    log(f"轮询 {interval}s  模式: {mode}")

    start_ts = int(time.time())
    replied = {}          # message_key -> 已完成的 local_id
    pending = {}          # message_key -> 已生成待发送的回复缓存
    generation_failures = {}  # message_key -> {attempts, retry_at}
    handled_ts = _load_handled_ts()  # username -> 已完成的最大 create_time
    group_queue = groupq.GroupTaskQueue(GROUP_QUEUE_FILE, cfg)
    atexit.register(group_queue.close)
    active_group_batches = {}  # group_id -> 已领取、正在生成/发送的持久化批次
    last_seen_ts = {}     # username -> SessionTable last_timestamp
    # session 时间戳滞后时，启动后补检白名单内最近漏掉的一条（10 分钟内）
    catchup_once = {}     # username -> verified message snapshot
    last_image_probe = {} # username -> monotonic；低频探测 Msg_ 表中的图片

    init_scope = None if reply_all or dry else sorted(targets)
    try:
        for u, ts, _msg_type in active_sessions(init_scope, retries=2):
            last_seen_ts[u] = ts
    except Exception as e:
        log(f"[!] 初始化 session 暂不可用，将靠消息库轮询: {e}")

    group_targets = {u for u in targets if u.endswith("@chatroom")}
    for group_id in sorted(group_targets):
        try:
            if group_queue.cursor(group_id) is None:
                group_queue.set_cursor(group_id, rc.latest_sort_seq(group_id))
                log(f"[群队列] {contact_name(group_id)} 已建立消息游标基线")
            else:
                result = rc.incremental_messages_since(
                    group_id, group_queue.cursor(group_id), limit=100,
                    image_types=media.IMAGE_LOCAL_TYPES)
                added = group_queue.enqueue(
                    group_id, result["messages"], result["cursor"])
                if added:
                    log(f"[群队列] {contact_name(group_id)} 恢复 {added} 条未处理消息")
        except Exception as e:
            log(f"[!] 群队列初始化失败 {contact_name(group_id)}: {e}")

    log(f"已建立基线，仅处理 {start_ts} 之后收到的新消息。Ctrl+C 退出。")

    def _bg_catchup():
        if reply_all:
            return
        for user in list(targets):
            if user.endswith("@chatroom"):
                continue
            try:
                # Read the real message shard as well as SessionTable/FTS.
                # A later system event (type 10000) can replace the session
                # summary before we restart, while the incoming peer message
                # is still present and direction-verified in message_N.db.
                info = poll_reply_candidate(
                    user,
                    vision_enabled=bool(_llm_client.vision.get("enabled")),
                    probe_image=True,
                )
            except Exception as e:
                log(f"[!] 启动补检失败 {contact_name(user)}: {e}")
                continue
            if not info:
                continue
            sess_ts = last_seen_ts.get(user, 0)
            recent_verified_image = (
                info.get("kind") == "image"
                and info.get("from_session_resource")
                and int(info.get("create_time") or 0)
                    > int(handled_ts.get(user) or 0)
            )
            recent_verified_session = (
                info.get("from_session")
                and info.get("direction_verified")
                and int(info.get("create_time") or 0)
                    > int(handled_ts.get(user) or 0)
            )
            recent_verified_message_db = (
                info.get("from_message_db")
                and int(info.get("create_time") or 0)
                    > int(handled_ts.get(user) or 0)
            )
            if ((info["create_time"] > sess_ts
                 or recent_verified_image or recent_verified_session
                 or recent_verified_message_db)
                    and 0 <= start_ts - info["create_time"]
                        < STARTUP_CATCHUP_SECONDS):
                # Keep the verified snapshot itself.  Re-reading through the
                # normal fast path can return an older FTS row after a system
                # event has replaced SessionTable's latest-message summary.
                catchup_once[user] = dict(info)
                log(f"[补检] {contact_name(user)} 发现近期未处理的对方消息，将补处理")

    # Startup DB reads must be serialized with the main polling loop.  The
    # Hook is not reliably re-entrant; running this in a background thread can
    # lose the one-shot catch-up exception before the loop has even started.
    _bg_catchup()

    import threading

    def _bg_sticker_sync():
        if not _sticker_cfg.get("enabled"):
            return
        try:
            result = stickers.refresh_catalog(_sticker_cfg, _llm_client)
            log("[表情学习] "
                f"下载{result['downloaded']}个，标注{result['classified']}个，"
                f"当前可用{result['available']}个")
        except Exception as e:
            log(f"[!] 表情学习暂不可用: {type(e).__name__}")

    threading.Thread(target=_bg_sticker_sync, daemon=True).start()

    def _ingest_group(group_id):
        """一个 Hook 查询增量收取群消息；游标与任务由 SQLite 原子提交。"""
        cursor = group_queue.cursor(group_id)
        if cursor is None:
            group_queue.set_cursor(group_id, rc.latest_sort_seq(group_id))
            return False
        result = rc.incremental_messages_since(
            group_id, cursor, limit=100,
            image_types=media.IMAGE_LOCAL_TYPES)
        added = group_queue.enqueue(
            group_id, result["messages"], result["cursor"])
        if added:
            log(f"[群队列] {contact_name(group_id)} 入队 {added} 条，"
                f"待处理 {group_queue.pending_count()} 条")
        return bool(result.get("has_more"))

    hook_down_streak = 0
    session_fail_streak = 0  # 仅 session.db 繁忙；不阻断消息库轮询
    settle_until = {}   # user -> monotonic，连发安静后才回复
    burst_fp = {}       # user -> (create_time, local_id) 当前连发窗口最新一条
    burst_info = {}     # user -> 等待连发期间保留的候选消息快照
    burst_logged = set()  # 已打过「等待连发」日志的 user
    group_ingest_pending = set()  # 每轮最多增量读取一个群，保护 Hook
    loop_no = 0
    last_cookie_sync = time.monotonic()  # 启动后先等一个完整间隔再同步
    global _last_msg_activity_ts, _last_heartbeat_ts
    _last_msg_activity_ts = time.monotonic()
    _last_heartbeat_ts = 0.0  # 首轮立即产出健康状态，之后每 5 分钟更新
    while True:
        try:
            time.sleep(interval)

            # 配置热加载：网页面板改了白名单/人格/参数即时生效，无需重启
            if args.config and os.path.exists(args.config):
                m = os.path.getmtime(args.config)
                if m != cfg_mtime:
                    cfg_mtime = m
                    cfg = load_config(args.config)
                    autoreply_enabled = bool(cfg.get("enabled", True))
                    targets, reply_all, include_system, interval, llm_cfg, settle_seconds = \
                        resolve_runtime(cfg, args)
                    _llm_client.update(cfg)
                    _personas = persona.parse_personas(cfg)
                    _style_profiles = persona.parse_style_profiles(cfg)
                    _memory_cfg = mem.parse_memory_config(cfg)
                    _kb_cfg = kb.parse_config(cfg)
                    _sticker_cfg = stickers.parse_config(cfg)
                    group_cfg = groupq.parse_group_reply_config(cfg)
                    group_queue.update_config(cfg)
                    _summary_whitelist = set(targets)
                    previous_group_targets = set(group_targets)
                    group_targets = {
                        u for u in targets if u.endswith("@chatroom")}
                    for group_id in previous_group_targets - group_targets:
                        active = active_group_batches.pop(group_id, None)
                        if active:
                            group_queue.finish(
                                active.get("group_batch") or {}, replied=False)
                        for key in [k for k in pending if k[0] == group_id]:
                            pending.pop(key, None)
                        group_ingest_pending.discard(group_id)
                    for group_id in sorted(
                            group_targets - previous_group_targets):
                        try:
                            if group_queue.cursor(group_id) is None:
                                group_queue.set_cursor(
                                    group_id, rc.latest_sort_seq(group_id))
                                log(f"[群队列] {contact_name(group_id)} 已建立消息游标基线")
                        except Exception as e:
                            log(f"[!] 群队列热加载失败 {contact_name(group_id)}: {e}")
                    dry = not targets and not reply_all
                    mode = ("已禁用" if not autoreply_enabled else
                            "dry-run(只打印)" if dry else
                            ("回复所有非系统联系人" if reply_all
                             else f"回复 -> {sorted(targets)}"))
                    smart = _llm_client.smart_reply
                    timing_label = (
                        f"{smart['settle_min']:.0f}~{smart['settle_max']:.0f}s"
                        if smart.get("dynamic_timing") else f"{settle_seconds:.0f}s")
                    log(f"[配置已更新] 大模型:{llm_label(llm_cfg)} "
                        f"人格:{len(_personas)} 风格:{len(_style_profiles)} "
                        f"短期:{_memory_cfg['keep_recent']} "
                        f"轮询:{interval}s 连发等待:{timing_label} 模式:{mode}")

            loop_no += 1

            # 真正的热加载总开关。与 dry-run 不同，禁用后不读消息、不调用模型。
            if not autoreply_enabled:
                continue

            # Gemini Cookie：后台优先纯请求续期；只有失效时才回退 CDP。
            gsync = _llm_client.image_gen
            if gsync.get("enabled") and str(gsync.get("provider") or "") == "gemini_web":
                sync_iv = max(600, int(gsync.get("cookie_sync_interval") or 600))
                if sync_iv > 0 and time.monotonic() - last_cookie_sync >= sync_iv:
                    last_cookie_sync = time.monotonic()

                    def _bg_cookie_sync(cfg_snapshot):
                        try:
                            import gemini_web_client as _gw
                            r = _gw.refresh_cookies(
                                cfg_snapshot, fallback_to_cdp=True)
                            if r.get("ok"):
                                log(f"   [Cookie续期] {r.get('source')}")
                            else:
                                extra = r.get("fallback")
                                detail = r.get("message") or "未知错误"
                                if extra:
                                    detail += f"；{extra}"
                                log(f"   [!] Cookie续期: {detail}")
                        except Exception as e:
                            log(f"   [!] Cookie续期失败: {e}")

                    import threading
                    threading.Thread(
                        target=_bg_cookie_sync,
                        args=(dict(gsync),),
                        daemon=True,
                    ).start()

            # 断路中：整轮暂停，不查 session / 不扫 FTS / 不保活
            if _pause_if_hook_circuit():
                continue

            # SessionTable 是轻量变化检测主链路；时间戳变化后才读具体消息。
            session_scope = None if reply_all or dry else sorted(targets)
            sessions = []
            session_image_users = set()
            target_count = len(targets) if not reply_all and not dry else 0
            do_session, fallback_index = polling_schedule(
                loop_no, target_count, session_fail_streak)
            if do_session:
                try:
                    sessions = active_sessions(session_scope, retries=1)
                    hook_down_streak = 0
                    if session_fail_streak:
                        log("   [session.db] 已恢复")
                    session_fail_streak = 0
                except Exception as e:
                    err = str(e)
                    if rc.is_db_unavailable_error(e) or _is_hook_transport_error(err):
                        session_fail_streak = max(1, session_fail_streak + 1)
                        _log_db_busy("sessions", "session.db 暂不可用，改扫 FTS")
                    else:
                        log(f"[!] 读取会话失败: {e}")
                    if _is_hook_transport_error(err):
                        hook_down_streak += 1
                        if hook_down_streak >= 2:
                            log("[!] Hook 短暂不可用，退避 8s …")
                            if not rc.hook_circuit_open():
                                _soft_keepalive_wechat(min_interval=30)
                            time.sleep(8)
                            hook_down_streak = 0
                        # 不 continue：本轮仍用 FTS 扫白名单

            users_to_check = set()
            fallback_probe_users = set()
            for user, ts, msg_type in sessions:
                prev = last_seen_ts.get(user, 0)
                if ts > prev:
                    last_seen_ts[user] = ts
                    _last_msg_activity_ts = time.monotonic()
                    if reply_all or dry or user in targets:
                        if user in group_targets:
                            group_ingest_pending.add(user)
                        else:
                            users_to_check.add(user)
                            if msg_type in media.SESSION_IMAGE_TYPES:
                                session_image_users.add(user)
            # 低频兜底只用于补偿偶发的 SessionTable 滞后；正常新消息由上面的
            # 时间戳变化立即触发。不能每 3 秒扫具体消息库。
            if reply_all:
                if loop_no % FALLBACK_POLL_EVERY == 0:
                    users_to_check.update(u for u, _, _ in sessions)
            elif not dry and targets:
                tlist = sorted(targets)
                if fallback_index is not None:
                    fallback_user = tlist[fallback_index]
                    if fallback_user in group_targets:
                        group_ingest_pending.add(fallback_user)
                    else:
                        users_to_check.add(fallback_user)
                        fallback_probe_users.add(fallback_user)

            # 活跃群每轮最多做一次增量 SQL；多个群采用轮转，避免同时猛查 Hook。
            if group_ingest_pending and not rc.hook_circuit_open():
                group_id = sorted(group_ingest_pending)[
                    loop_no % len(group_ingest_pending)]
                try:
                    has_more = _ingest_group(group_id)
                    if not has_more:
                        group_ingest_pending.discard(group_id)
                except Exception as e:
                    if rc.is_db_unavailable_error(e) or _is_hook_transport_error(e):
                        _log_db_busy(
                            f"group:{group_id}",
                            f"群增量读取暂不可用 {contact_name(group_id)}")
                    else:
                        log(f"[!] 群增量读取失败 {contact_name(group_id)}: {e}")

            # 跨群按最久未服务优先领取一个稳定批次；同一群始终只有一个活动批次。
            if group_cfg.get("enabled"):
                claimed = group_queue.claim_next(
                    active_groups=active_group_batches,
                    allowed_groups=group_targets)
                if claimed:
                    should_reply, trigger = groupq.trigger_reason(
                        claimed["messages"], group_cfg)
                    if not should_reply:
                        group_queue.finish(claimed, replied=False)
                        log(f"   [群跳过] {contact_name(claimed['group_id'])}: {trigger}")
                    else:
                        messages = list(claimed["messages"])
                        latest_ts = max(
                            int(x.get("create_time") or 0) for x in messages)
                        if not any(x.get("kind") == "image" for x in messages):
                            recent_images = group_queue.recent_images(
                                claimed["group_id"], before_time=latest_ts)
                        else:
                            recent_images = []
                        latest = messages[-1]
                        active_group_batches[claimed["group_id"]] = {
                            "local_id": int(latest.get("local_id") or 0),
                            "create_time": int(latest.get("create_time") or 0),
                            "sort_seq": int(latest.get("sort_seq") or 0),
                            "sender": latest.get("sender") or "",
                            "kind": "group_batch",
                            "text": groupq.format_batch(messages, contact_name),
                            "group_batch": claimed,
                            "group_messages": messages,
                            "context_images": recent_images,
                            "trigger_reason": trigger,
                            "max_batch_images": group_cfg["max_batch_images"],
                        }

            users_to_check.update(active_group_batches.keys())

            # 心跳仅做状态采样；空闲时不再调用微信窗口 API。
            now_m = time.monotonic()
            idle_sec = now_m - _last_msg_activity_ts
            if now_m - _last_heartbeat_ts >= 300:
                _last_heartbeat_ts = now_m
                msg_open = rc.message_sqlite_open()
                fts = rc.fts_shard_health()
                cir = rc.hook_circuit_info()
                hook_ok = rc.hook_ready(timeout=2)
                session_ok = session_fail_streak == 0
                health_state = classify_health(
                    hook_ok,
                    session_ok,
                    msg_open,
                    fts["good"],
                    circuit_open=bool(cir.get("open")),
                )
                _write_health(
                    health_state,
                    hook_ready=hook_ok,
                    session_ok=session_ok,
                    fts_good=fts["good"],
                    fts_bad=sorted(fts["bad"]),
                    fts_role="backup",
                    message_sqlite_open=msg_open,
                    idle_seconds=int(idle_sec),
                    targets=len(targets),
                    reason=(cir.get("reason") or "")[:160],
                )
                if not hook_ok:
                    cir_s = "离线"
                elif cir.get("open"):
                    cir_s = f"断路{cir['seconds_left']}s"
                else:
                    cir_s = "正常"
                log(f"[心跳] idle={int(idle_sec)}s session_fail={session_fail_streak} "
                    f"message_db={'开' if msg_open else '关'} "
                    f"fts={len(fts['good'])}/4 hook={cir_s} targets={len(targets)}")
            # 启动补检队列：直到处理完才移除
            if catchup_once:
                users_to_check.update(catchup_once.keys())

            # 有待发送缓存时，下轮继续尝试（不必等 session 变化）
            for key in list(pending.keys()):
                users_to_check.add(key[0])

            # 连发等待中的用户每轮都要 peek，才能延长等待 / 到期回复
            users_to_check.update(burst_fp.keys())
            users_to_check.update(burst_info.keys())
            users_to_check.update(settle_until.keys())

            handled_one = False
            for user in sorted(users_to_check):
                if handled_one:
                    break
                if not include_system and user in SYSTEM_ACCOUNTS:
                    continue
                # 待发送重试：跳过连发等待
                if user in {k[0] for k in pending}:
                    settle_until.pop(user, None)
                    burst_fp.pop(user, None)
                    burst_info.pop(user, None)
                    burst_logged.discard(user)

                try:
                    if user in active_group_batches:
                        info = active_group_batches[user]
                    elif user in catchup_once:
                        info = dict(catchup_once[user])
                    else:
                        # 私聊继续走原有稳定链路；群聊由持久化增量队列提供。
                        now_probe = time.monotonic()
                        probe_image = (
                            user in session_image_users
                            or (
                                user in fallback_probe_users
                                and now_probe - float(
                                    last_image_probe.get(user) or 0) >= 60
                            )
                        )
                        if probe_image:
                            last_image_probe[user] = now_probe
                        info = poll_reply_candidate(
                            user,
                            vision_enabled=bool(_llm_client.vision.get("enabled")),
                            probe_image=probe_image,
                        )
                except Exception as e:
                    if rc.is_db_unavailable_error(e) or _is_hook_transport_error(e):
                        _log_db_busy(f"msg:{user}", f"读消息暂不可用 {contact_name(user)}")
                    else:
                        log(f"[!] 取消息失败 {user}: {e}")
                    continue
                if info and _is_recent_session_self_echo(user, info):
                    # 自己刚发出的消息不能覆盖仍在等待的入站快照。
                    if user not in burst_info:
                        log(f"   [skip] ignored recent self-sent SessionTable echo: {contact_name(user)}")
                        _mark_handled(user, info, replied, handled_ts)
                    info = None
                info = settled_candidate(
                    info, user, burst_info, settle_until)
                if not info:
                    continue
                if info["create_time"] < start_ts:
                    catchup = catchup_once.get(user) or {}
                    if int(catchup.get("local_id") or 0) != info["local_id"]:
                        continue
                msg_key = _message_key(user, info)
                if _already_handled(user, info, replied, handled_ts):
                    catchup_once.pop(user, None)
                    if info.get("kind") == "group_batch":
                        group_queue.finish(
                            info.get("group_batch") or {}, replied=False)
                        active_group_batches.pop(user, None)
                    burst_fp.pop(user, None)
                    burst_info.pop(user, None)
                    settle_until.pop(user, None)
                    burst_logged.discard(user)
                    continue

                failure = generation_failures.get(msg_key)
                if failure and time.monotonic() < float(
                        failure.get("retry_at") or 0):
                    continue

                # 私聊用动态连发等待；群批次已在持久化队列中完成 5~8 秒合并。
                if (info.get("kind") != "group_batch"
                        and user not in {k[0] for k in pending}):
                    fp = (int(info.get("create_time") or 0),
                          int(info.get("local_id") or 0))
                    prev_fp = burst_fp.get(user)
                    if prev_fp != fp:
                        burst_fp[user] = fp
                        burst_info[user] = dict(info)
                        smart_window = settle_delay(
                            info.get("text") or "",
                            f"{user}:{fp[0]}:{fp[1]}",
                            _llm_client.smart_reply,
                            group=user.endswith("@chatroom"),
                        )
                        window = smart_window if smart_window is not None else settle_seconds
                        settle_until[user] = time.monotonic() + window
                        if user not in burst_logged:
                            who0 = contact_name(user)
                            log(f"[等待连发] {who0}: {window:.1f}s 内若继续发则一并回复")
                            burst_logged.add(user)
                        continue
                    if time.monotonic() < settle_until.get(user, 0):
                        continue
                    # 已安静足够久，开始综合回复
                    burst_fp.pop(user, None)
                    settle_until.pop(user, None)
                    burst_logged.discard(user)

                pend = pending.get(msg_key)
                reusing = pend and (
                    pend.get("create_time") == info.get("create_time")
                    or pend.get("local_id") == info.get("local_id")
                )

                who = contact_name(user)
                kind = info.get("kind", "text")
                text = info.get("text") or ("[图片]" if kind == "image" else "")
                if not reusing:
                    _last_msg_activity_ts = time.monotonic()
                    if kind == "group_batch":
                        batch_messages = info.get("group_messages") or []
                        image_count = sum(
                            1 for x in batch_messages if x.get("kind") == "image")
                        log(f"[收到] 群 {who}: 合并{len(batch_messages)}条/"
                            f"图片{image_count}张，触发={info.get('trigger_reason')}")
                    elif user.endswith("@chatroom") and info.get("sender"):
                        sender = contact_name(info["sender"])
                        tag = "[图片]" if kind == "image" else text
                        log(f"[收到] 群 {who} / {sender}: {tag}")
                    else:
                        tag = "[图片]" if kind == "image" else text
                        log(f"[收到] {who} ({user}): {tag}")

                will_reply = reply_all or (user in targets)
                if not dry and not will_reply:
                    # The web panel may remove a target while a restart
                    # catch-up/burst snapshot is still active.  Drop every
                    # transient state immediately so it neither sends nor
                    # keeps re-reading/logging the same removed conversation.
                    catchup_once.pop(user, None)
                    burst_fp.pop(user, None)
                    burst_info.pop(user, None)
                    settle_until.pop(user, None)
                    burst_logged.discard(user)
                    continue

                if reusing:
                    reply = pend.get("reply")
                    gen_img = pend.get("gen_img")
                    bubbles = pend.get("bubbles") or ([reply] if reply else [])
                else:
                    reply = None
                    gen_img = None
                    plan = {}
                    generation_started = time.monotonic()
                    if kind == "group_batch":
                        reply, gen_img, skipped, plan = ask_group_batch(
                            user, info)
                        if skipped:
                            group_queue.finish(
                                info.get("group_batch") or {}, replied=False)
                            active_group_batches.pop(user, None)
                            _mark_handled(user, info, replied, handled_ts)
                            pending.pop(msg_key, None)
                            handled_one = True
                            continue
                    elif kind == "image":
                        if _llm_client.vision.get("enabled"):
                            try:
                                full = (info if (info.get("raw_content")
                                                or info.get("dat_path"))
                                        else rc.last_incoming_message(user))
                                if not full or full.get("kind") != "image":
                                    log("   [!] 图片消息暂不可读（message 库未打开）")
                                    continue
                                info = full
                                msg_key = _message_key(user, info)
                                img_path = media.prepare_incoming_image(info, user)
                                reply = ask_vision_chat(user, img_path)
                            except Exception as e:
                                log(f"   [!] 图片处理失败: {e}")
                        else:
                            log("   [跳过] 收到图片但 vision.enabled=false")
                            _mark_handled(user, info, replied, handled_ts)
                            handled_one = True
                            continue
                    else:
                        sticker_item = stickers.choose(
                            user,
                            text,
                            f"{info.get('create_time')}:{info.get('local_id')}",
                            _sticker_cfg,
                        )
                        if sticker_item:
                            reply, gen_img, skipped = None, sticker_item["path"], False
                            plan = {
                                "action": "reply",
                                "bubbles": [],
                                "sticker_md5": sticker_item.get("md5"),
                                "sticker_category": sticker_item.get("category"),
                            }
                        else:
                            reply, gen_img, skipped, plan = ask_chat_with_optional_image(
                                user, text)
                        if skipped:
                            _mark_handled(user, info, replied, handled_ts)
                            burst_info.pop(user, None)
                            pending.pop(msg_key, None)
                            handled_one = True
                            continue
                    bubbles = plan.get("bubbles") or ([reply] if reply else [])
                    # 兼容模型偶尔把多气泡正文只放进 reply。
                    bubbles = [str(x).strip() for x in bubbles if str(x).strip()]
                    if bubbles:
                        reply = "\n".join(bubbles)
                    generation_seconds = time.monotonic() - generation_started
                    ready_delay = typing_delay(
                        bubbles,
                        f"{user}:{info.get('create_time')}:{info.get('local_id')}",
                        _llm_client.smart_reply,
                        generation_seconds=generation_seconds,
                    )
                    pending[msg_key] = {
                        "local_id": info["local_id"],
                        "create_time": info.get("create_time"),
                        "reply": reply,
                        "bubbles": bubbles,
                        "bubble_states": [
                            {"attempted": False, "sent": False}
                            for _ in bubbles
                        ],
                        "ready_at": time.monotonic() + ready_delay,
                        "gen_img": gen_img,
                        "text_sent": False,
                        "img_sent": False,
                        "text_attempted": False,
                        "img_attempted": False,
                        "image_outcome_managed": bool(
                            plan.get("image_outcome_managed")),
                        "sticker_md5": plan.get("sticker_md5"),
                        "sticker_category": plan.get("sticker_category"),
                    }
                    pend = pending[msg_key]

                if not reply and not gen_img:
                    # Never pin a transient model/network failure in pending.
                    # Removing the empty item lets a later pass call the model
                    # again；退避则防止同一图片十几秒内无限解密/请求。
                    pending.pop(msg_key, None)
                    old_failure = generation_failures.get(msg_key) or {}
                    attempts = int(old_failure.get("attempts") or 0) + 1
                    retry_delay = generation_retry_delay(attempts)
                    generation_failures[msg_key] = {
                        "attempts": attempts,
                        "retry_at": time.monotonic() + retry_delay,
                    }
                    log(f"   [!] 未生成回复，{retry_delay}s 后重试"
                        f"（第{attempts}次失败）")
                    continue

                generation_failures.pop(msg_key, None)

                burst_info.pop(user, None)

                if dry or not will_reply:
                    if bubbles:
                        for i, bubble in enumerate(bubbles, 1):
                            suffix = f" {i}/{len(bubbles)}" if len(bubbles) > 1 else ""
                            log(f"   [拟回复{suffix}] {bubble}   （未发送）")
                    if gen_img:
                        log(f"   [拟生图] {gen_img}   （未发送）")
                    _mark_handled(user, info, replied, handled_ts)
                    if kind == "group_batch":
                        group_queue.finish(
                            info.get("group_batch") or {}, replied=False)
                        active_group_batches.pop(user, None)
                    pending.pop(msg_key, None)
                    handled_one = True
                    continue

                if time.monotonic() < float(pend.get("ready_at") or 0):
                    continue

                if not rc.hook_ready():
                    log("   [!] Hook 未就绪，稍后重试发送（已缓存回复，不重复调用模型）")
                    continue

                gcfg = _llm_client.image_gen
                skip_text = (
                    bool(gen_img)
                    and gcfg.get("send_text_with_image") is False
                )

                sent_any = False
                states = pend.get("bubble_states") or []

                # Generated-image requests are delivered image-first.  Only
                # after the image has really reached WeChat do we send the
                # affirmative companion text.  A send failure rewrites the
                # still-unsent text into an explicit failure response.
                managed_image = bool(pend.get("image_outcome_managed"))
                if (managed_image and gen_img
                        and not pend.get("img_sent")
                        and not pend.get("img_attempted")):
                    pend["img_attempted"] = True
                    if send_image(user, gen_img):
                        log(f"   [已发图] {gen_img}")
                        audit_send("IMAGE", user, gen_img)
                        pend["img_sent"] = True
                        sent_any = True
                    else:
                        log("   [x] 发图失败（不再重试，避免重复发送）")
                        fallback = align_image_reply_outcome({
                            "reply": reply,
                            "bubbles": bubbles,
                            "send_image": True,
                        }, False)
                        bubbles = fallback["bubbles"]
                        reply = fallback["reply"]
                        gen_img = None
                        skip_text = False
                        pend["reply"] = reply
                        pend["bubbles"] = bubbles
                        pend["gen_img"] = None
                        pend["bubble_states"] = [
                            {"attempted": False, "sent": False}
                            for _ in bubbles
                        ]
                        states = pend["bubble_states"]
                        log("   [图文一致性] 发图失败，已改为明确说明无法发送")

                if bubbles and not skip_text:
                    for idx, bubble in enumerate(bubbles):
                        state = states[idx]
                        if state.get("attempted"):
                            continue
                        if idx > 0:
                            time.sleep(inter_bubble_delay(
                                f"{user}:{info.get('create_time')}",
                                idx,
                                _llm_client.smart_reply,
                            ))
                        state["attempted"] = True
                        pend["text_attempted"] = True
                        if send(user, bubble):
                            tag = (f" {idx + 1}/{len(bubbles)}"
                                   if len(bubbles) > 1 else "")
                            log(f"   [已回复{tag}] {bubble}")
                            _remember_sent_reply(user, bubble)
                            audit_send("TEXT", user, bubble[:120])
                            state["sent"] = True
                            sent_any = True
                        else:
                            log("   [x] 回复失败（剩余气泡取消，避免半截对话）")
                            for later in states[idx + 1:]:
                                later["attempted"] = True
                            break
                    pend["text_sent"] = bool(states) and all(
                        x.get("sent") for x in states)
                elif skip_text and bubbles and not pend.get("text_sent"):
                    log("   [跳过文字] 已成功生图，仅发图片")
                    pend["text_sent"] = True
                    for state in states:
                        state["attempted"] = True
                        state["sent"] = True

                if gen_img and not pend.get("img_sent"):
                    if pend.get("img_attempted"):
                        log("   [!] 发图已尝试过，跳过防重复")
                    else:
                        pend["img_attempted"] = True
                        if send_image(user, gen_img):
                            log(f"   [已发图] {gen_img}")
                            if pend.get("sticker_md5"):
                                stickers.record_sent(user, pend.get("sticker_md5"))
                                audit_send(
                                    "STICKER", user,
                                    pend.get("sticker_md5"))
                                log("   [已发表情] "
                                    f"{pend.get('sticker_category') or 'reaction'}")
                            else:
                                audit_send("IMAGE", user, gen_img)
                            pend["img_sent"] = True
                            sent_any = True
                        else:
                            log("   [x] 发图失败（不再重试，避免重复发送）")

                if _delivery_done(pend, reply, gen_img, skip_text=skip_text):
                    _mark_handled(user, info, replied, handled_ts)
                    if kind == "group_batch":
                        group_replied = bool(
                            pend.get("img_sent")
                            or any(x.get("sent") for x in
                                   (pend.get("bubble_states") or [])))
                        group_queue.finish(
                            info.get("group_batch") or {},
                            replied=group_replied)
                        active_group_batches.pop(user, None)
                    pending.pop(msg_key, None)
                    handled_one = True
                elif sent_any:
                    pending[msg_key] = pend
        except KeyboardInterrupt:
            log("退出。")
            return 0
        except Exception as e:
            log(f"[!] 轮询异常: {e}")
            if _is_hook_transport_error(e):
                if not rc.hook_circuit_open():
                    _soft_keepalive_wechat(min_interval=30)
                time.sleep(8)
            else:
                time.sleep(2)


if __name__ == "__main__":
    main()
