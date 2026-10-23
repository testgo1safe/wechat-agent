# -*- coding: utf-8 -*-
"""收消息核心：从 message_N.db 的 Msg_<md5(wxid)> 表读取完整消息原文。

结构（微信 4.1.9.52）：
  - 每个会话一张表 Msg_<md5(username)>，列含 message_content(完整正文)、
    local_type(1=文本)、real_sender_id(关联本库 Name2Id.rowid)、create_time。
  - 私聊：对方发来 <=> sender == 对方 wxid(=会话 username)
  - 群聊：成员发来 <=> sender != 自己的 wxid（群 id 不会作为 sender 出现）
  - 群消息正文常带 "wxid_xxx:\\n内容" 前缀，展示/喂模型前会剥掉
  - 用 session.db 的 SessionTable 做轻量触发（last_timestamp/unread 变化）。
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.request

try:
    import msvcrt
except ImportError:
    msvcrt = None

BASE = "http://127.0.0.1:30001"
QUERY_URL = BASE + "/QueryDB/execute"

_MSG_DB_RE = re.compile(r"^(message_\d+|biz_message_\d+|media_\d+)\.db$")
_GROUP_PREFIX_RE = re.compile(r"^wxid_[A-Za-z0-9_-]+:\n", re.DOTALL)

_self_wxid_cache = None
_hook_lock = threading.Lock()
_hook_file = None
_hook_file_path = os.path.join(tempfile.gettempdir(), "wechat_hook_global.lock")
_last_send_ts = 0.0
SEND_MIN_INTERVAL = 4.0  # 发送类 Hook 调用最小间隔（秒），避免微信崩溃


def _acquire_hook_file_lock(max_wait_sec=2.0):
    """跨进程互斥：autoreply / 网页面板 / 脚本不能同时打 Hook。

    max_wait_sec 默认较短：拿不到锁就快速失败，避免主循环被拖死。
    """
    global _hook_file
    if msvcrt is None:
        return
    if _hook_file is None:
        _hook_file = open(_hook_file_path, "a+b")
    deadline = time.monotonic() + max(0.2, float(max_wait_sec))
    while time.monotonic() < deadline:
        try:
            _hook_file.seek(0)
            msvcrt.locking(_hook_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Hook 繁忙（可能有多个程序在同时访问微信），请稍后重试")


def _release_hook_file_lock():
    if msvcrt is None or _hook_file is None:
        return
    try:
        msvcrt.locking(_hook_file.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def is_handle_missing_msg(msg):
    """库句柄当前未打开（不是瞬时锁，重试无意义）。"""
    s = (msg or "").lower()
    return "database handle" in s and "failed" in s


def is_handle_missing_error(exc):
    return is_handle_missing_msg(str(exc))


def is_db_busy_error(exc):
    """瞬时写库冲突（可重试）。句柄未打开不算 busy。"""
    return is_db_busy_msg(str(exc))


def is_circuit_open_error(exc):
    return "读库断路" in str(exc or "")


def is_db_unavailable_error(exc):
    """读库暂时不可用（句柄未打开、瞬时锁、或断路器打开）。"""
    return (
        is_handle_missing_error(exc)
        or is_db_busy_error(exc)
        or is_circuit_open_error(exc)
        or is_db_corrupt_signal(str(exc))
    )


def is_db_busy_msg(msg):
    s = (msg or "").lower()
    if is_handle_missing_msg(s):
        return False
    return (
        ("sqlite" in s and "busy" in s)
        or "locked" in s
        or "database is locked" in s
    )


def is_db_corrupt_signal(msg):
    """微信库可能已异常（弹「修复数据库」前常见信号）。"""
    s = (msg or "").lower()
    return (
        "hook db status=11" in s
        or "malformed" in s
        or "corrupt" in s
        or "disk image is malformed" in s
        or "http error 500" in s
        or "access violation" in s
        or "query interrupted" in s
    )


def is_db_fatal_signal(msg):
    """继续查询可能直接拖崩微信的信号；必须等微信进程重启后才恢复。"""
    s = (msg or "").lower()
    return (
        "http error 500" in s
        or "access violation" in s
        or "query interrupted" in s
        or "malformed" in s
        or "corrupt" in s
        or "disk image is malformed" in s
    )


# Hook 读库断路器：连续异常时暂停查询，避免把微信 SQLite 打到需修复
_hook_circuit = {
    "fail": 0,
    "open_until": 0.0,
    "last_reason": "",
    "latched": False,
}
_hook_process_id = None


def hook_circuit_open():
    return bool(_hook_circuit.get("latched")) or (
        time.monotonic() < float(_hook_circuit.get("open_until") or 0)
    )


def hook_circuit_info():
    left = max(0.0, float(_hook_circuit.get("open_until") or 0) - time.monotonic())
    return {
        "open": bool(_hook_circuit.get("latched")) or left > 0,
        "seconds_left": int(left),
        "fail": int(_hook_circuit.get("fail") or 0),
        "reason": _hook_circuit.get("last_reason") or "",
        "latched": bool(_hook_circuit.get("latched")),
    }


def _hook_circuit_success():
    if _hook_circuit["fail"]:
        _hook_circuit["fail"] = 0
    if not _hook_circuit.get("latched") and not hook_circuit_open():
        _hook_circuit["last_reason"] = ""


def _hook_circuit_fail(exc, open_seconds=90.0):
    _hook_circuit["fail"] = int(_hook_circuit.get("fail") or 0) + 1
    _hook_circuit["last_reason"] = str(exc)[:160]
    # 访问冲突/HTTP 500/库损坏信号不能定时后自动重试；仅在微信 PID 变化后恢复。
    if is_db_fatal_signal(str(exc)):
        _hook_circuit["latched"] = True
        _hook_circuit["open_until"] = 0.0
        _hook_circuit["fail"] = 0
    elif is_db_corrupt_signal(str(exc)) or _hook_circuit["fail"] >= 3:
        _hook_circuit["open_until"] = time.monotonic() + open_seconds
        _hook_circuit["fail"] = 0


def hook_post(url, payload, timeout=15, *, send_action=False, lock_wait=None):
    """所有 Hook HTTP 统一入口：进程间文件锁 + 线程锁 + 发送限速。"""
    global _last_send_ts
    # 发送类多等一会；普通查询快速失败以免拖死轮询
    if lock_wait is None:
        lock_wait = 8.0 if send_action else 2.0
    with _hook_lock:
        _acquire_hook_file_lock(max_wait_sec=lock_wait)
        try:
            if send_action:
                wait = SEND_MIN_INTERVAL - (time.monotonic() - _last_send_ts)
                if wait > 0:
                    time.sleep(wait)
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode("utf-8"))
            if send_action:
                _last_send_ts = time.monotonic()
            return out
        finally:
            _release_hook_file_lock()


def hook_get(url, timeout=3, *, lock_wait=1.5):
    """不触碰微信数据库的 Hook GET 请求。"""
    with _hook_lock:
        _acquire_hook_file_lock(max_wait_sec=lock_wait)
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        finally:
            _release_hook_file_lock()


def _observe_hook_process(process_id):
    """微信重启后旧句柄已全部作废，同时允许新进程重新进行读库探测。"""
    global _hook_process_id
    try:
        process_id = int(process_id or 0)
    except (TypeError, ValueError):
        process_id = 0
    if not process_id:
        return
    if _hook_process_id and process_id != _hook_process_id:
        _hook_circuit.update({
            "fail": 0,
            "open_until": 0.0,
            "last_reason": "",
            "latched": False,
        })
        invalidate_opened_dbs_cache()
    _hook_process_id = process_id


def hook_status(timeout=3):
    """读取 Hook/微信进程状态；该接口不会执行 SQL。"""
    status = hook_get(BASE + "/QueryDB/status", timeout=timeout)
    _observe_hook_process(status.get("processId"))
    return status


def hook_ready(timeout=3):
    """Hook HTTP 是否可用；只读状态接口，绝不拿真实 SQL 当心跳。"""
    try:
        status = hook_status(timeout=timeout)
        return int(status.get("hWeixin") or 0) != 0
    except Exception:
        return False


def _post(url, payload, timeout=15):
    return hook_post(url, payload, timeout=timeout, send_action=False)


def query(db, sql, timeout=15, retries=None):
    """查询 Hook 数据库；瞬时写库冲突才退避重试；句柄未打开立即失败。

    若断路器打开：直接失败，避免在微信库异常时继续猛打。
    """
    if hook_circuit_open():
        info = hook_circuit_info()
        if info.get("latched"):
            raise RuntimeError(f"Hook 读库已安全闭锁（等待微信重启）: {info['reason']}")
        raise RuntimeError(f"Hook 读库断路中({info['seconds_left']}s): {info['reason']}")
    if retries is None:
        retries = 2
    last = None
    for i in range(retries):
        try:
            r = _post(QUERY_URL, {"optDbName": db, "SQL": sql}, timeout=timeout)
            if r.get("status", 0) != 0:
                status = int(r.get("status") or -1)
                desc = r.get("desc") or r
                # Hook 的 desc 对 SQL 语法错误、字段不存在和真正损坏都可能只写
                # sqlite3_prepare failed。保留 SQLite 状态码才能正确分类。
                raise RuntimeError(f"Hook DB status={status}: {desc}")
            _hook_circuit_success()
            return r.get("data", [])
        except Exception as e:
            last = e
            # 句柄不存在：立刻放弃，不要指数退避占死 Hook
            if is_handle_missing_msg(str(e)):
                break
            if is_db_corrupt_signal(str(e)):
                _hook_circuit_fail(e, open_seconds=120.0)
                break
            if i + 1 < retries and is_db_busy_error(e):
                time.sleep(min(2.0, 0.25 * (2 ** i)))
            elif i + 1 < retries:
                time.sleep(0.1 * (i + 1))
            else:
                # 最终失败也计入断路（timeout/连接错误等）
                if "timed out" in str(e).lower() or "10054" in str(e) or "500" in str(e):
                    _hook_circuit_fail(e, open_seconds=60.0)
    raise last


_msg_dbs_cache = {"ts": 0, "dbs": []}
_opened_dbs_cache = {"ts": 0.0, "dbs": set()}
_fts_shard_cache = {"ts": 0.0, "good": [], "bad": {}}


_MSG_DB_FALLBACK = [
    "message_1.db", "message_0.db", "message_2.db", "biz_message_0.db", "media_0.db",
]


def list_dbs():
    r = _post(BASE + "/QueryDB/GetAllDBName", {})
    return [d["dbName"] for d in r]


def get_opened_dbs(ttl=60.0):
    """当前 Hook 已打开的库名；失败时沿用上次结果，绝不瞎猜。"""
    now = time.time()
    if _opened_dbs_cache["dbs"] and now - _opened_dbs_cache["ts"] < ttl:
        return set(_opened_dbs_cache["dbs"])
    # 断路期间不刷新列表，避免继续打 Hook
    if hook_circuit_open():
        return set(_opened_dbs_cache["dbs"] or ())
    try:
        dbs = set(list_dbs())
        _opened_dbs_cache["dbs"] = dbs
        _opened_dbs_cache["ts"] = now
        return set(dbs)
    except Exception:
        return set(_opened_dbs_cache["dbs"] or ())


def invalidate_opened_dbs_cache():
    """保活/恢复后强制刷新已打开库列表。"""
    _opened_dbs_cache["ts"] = 0.0
    _fts_shard_cache.update({"ts": 0.0, "good": [], "bad": {}})


def readable_fts_shards(ttl=60.0):
    """返回当前可读 FTS 分片，坏分片单独隔离，不能拖垮整个消息轮询。"""
    now = time.time()
    if _fts_shard_cache["ts"] and now - _fts_shard_cache["ts"] < ttl:
        return list(_fts_shard_cache["good"])
    good, bad = [], {}
    # 每个分片只做一次最小只读探测。这里不构造 UNION，避免一个坏分片
    # 使其余正常分片的查询也 prepare 失败。
    for i in range(4):
        table = f"message_fts_v4_{i}"
        try:
            query("message_fts.db", f"SELECT 1 AS ok FROM {table} LIMIT 1",
                  retries=1)
            good.append(i)
        except Exception as e:
            bad[i] = str(e)[:120]
            # 分片损坏是局部故障；清除它刚触发的定时断路，其他库仍可工作。
            # 访问冲突等 fatal 信号仍保持闭锁，绝不强行继续读微信库。
            if not is_db_fatal_signal(str(e)):
                _hook_circuit.update({
                    "fail": 0,
                    "open_until": 0.0,
                    "last_reason": "",
                })
    _fts_shard_cache.update({"ts": now, "good": good, "bad": bad})
    return list(good)


def fts_shard_health():
    """供心跳/监控读取的 FTS 健康状态。"""
    good = readable_fts_shards()
    return {
        "good": list(good),
        "bad": dict(_fts_shard_cache.get("bad") or {}),
    }


def message_sqlite_open():
    """message_N.db（非 kvdb）是否当前可查。"""
    for d in get_opened_dbs():
        if d.startswith("message_") and d.endswith(".db") and not d.endswith(".kvdb"):
            if _MSG_DB_RE.match(d):
                return True
    return False


def _candidate_msg_dbs():
    """只返回当前已打开的 message/biz_message sqlite，避免对关闭库反复超时。"""
    out = []
    for d in get_opened_dbs():
        if not _MSG_DB_RE.match(d):
            continue
        if d.endswith(".kvdb"):
            continue
        out.append(d)
    return out


def msg_dbs():
    now = time.time()
    if _msg_dbs_cache["dbs"] and now - _msg_dbs_cache["ts"] < 30:
        return _msg_dbs_cache["dbs"]
    dbs = [d for d in get_opened_dbs()
           if _MSG_DB_RE.match(d) and not d.endswith(".kvdb")]
    # 不再回退到未打开的常见库名（那会把 Hook 锁打死）
    _msg_dbs_cache["dbs"] = dbs
    _msg_dbs_cache["ts"] = now
    return dbs


def is_group(username):
    return (username or "").endswith("@chatroom")


def get_self_wxid():
    """获取当前登录账号 wxid（带缓存）。"""
    global _self_wxid_cache
    if _self_wxid_cache:
        return _self_wxid_cache

    # 1) Hook 资料接口（若已编译启用）
    try:
        r = _post(BASE + "/GetSelfProfile", {}, timeout=3)
        wxid = (r.get("wxid") or "").strip()
        if wxid:
            _self_wxid_cache = wxid
            return wxid
    except Exception:
        pass

    # 2) 群 owner 统计：最常见且不含 @ 的 owner 通常是本人
    try:
        rows = query("contact.db",
                     "SELECT owner FROM chat_room "
                     "WHERE owner IS NOT NULL AND owner != '' "
                     "AND owner NOT LIKE '%@%' "
                     "GROUP BY owner ORDER BY COUNT(1) DESC LIMIT 1")
        if rows and rows[0].get("owner"):
            _self_wxid_cache = rows[0]["owner"]
            return _self_wxid_cache
    except Exception:
        pass

    return None


def normalize_text(text, sender=None, group=False):
    """剥掉群消息里的 'wxid:\\n' 前缀，返回可读正文。"""
    if not text:
        return ""
    text = text.strip()
    if not group:
        return text
    if sender and text.startswith(sender + ":"):
        return text.split(":", 1)[1].lstrip("\n").strip()
    m = _GROUP_PREFIX_RE.match(text)
    if m:
        return text[m.end():].strip()
    if ":\n" in text[:48] and text.split(":\n", 1)[0].startswith("wxid_"):
        return text.split(":\n", 1)[1].strip()
    return text


def table_for(username):
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


_msg_table_cache = {}  # username -> {"ts": float, "found": (db, table)|None}


def _table_exists(db, table):
    k = table.replace("'", "''")
    rows = query(db, "SELECT 1 FROM sqlite_master WHERE type='table' "
                     f"AND name='{k}' LIMIT 1", retries=3)
    return bool(rows)


def find_msg_table(username):
    """返回 (db, table) 或 None（带缓存，减少反复扫库）。

    命中结果缓存较久；未命中只短缓存，避免微信刚重新打开 message_*.db 时
    被「空结果」挡住几分钟。
    """
    now = time.time()
    cached = _msg_table_cache.get(username)
    if cached:
        ttl = 600 if cached["found"] else 8
        if now - cached["ts"] < ttl:
            return cached["found"]

    table = table_for(username)
    found = None
    for db in _candidate_msg_dbs():
        try:
            if _table_exists(db, table):
                found = (db, table)
                break
        except Exception:
            continue
    _msg_table_cache[username] = {"ts": now, "found": found}
    return found


_fts_name2id_cache = {}  # username -> rowid | None


def _fts_rowid(username):
    """message_fts.db / name2id 中的 rowid（即 session_id / sender_id）。"""
    if not username:
        return None
    if username in _fts_name2id_cache:
        return _fts_name2id_cache[username]
    k = username.replace("'", "''")
    try:
        rows = query(
            "message_fts.db",
            f"SELECT rowid AS rid FROM name2id WHERE username='{k}' LIMIT 1",
            retries=1,
        )
        rid = int(rows[0]["rid"]) if rows else None
    except Exception:
        rid = None
    _fts_name2id_cache[username] = rid
    return rid


def _fts_username(rowid):
    try:
        rows = query(
            "message_fts.db",
            f"SELECT username FROM name2id WHERE rowid={int(rowid)} LIMIT 1",
            retries=1,
        )
        if rows:
            return rows[0].get("username") or ""
    except Exception:
        pass
    return ""


def last_incoming_from_fts(username):
    """message_N.db 未打开时，从常驻的 message_fts.db 读最近一条对方文本。"""
    sid = _fts_rowid(username)
    if not sid:
        return None
    self_wxid = get_self_wxid()
    self_rid = _fts_rowid(self_wxid) if self_wxid else None
    group = is_group(username)

    parts = []
    for i in readable_fts_shards():
        parts.append(
            "SELECT acontent AS text, message_local_id AS local_id, "
            "create_time, sender_id, local_type "
            f"FROM message_fts_v4_{i} "
            f"WHERE session_id={sid} AND local_type=1"
        )
    if not parts:
        return None
    sql = (
        "SELECT * FROM (" + " UNION ALL ".join(parts)
        + ") ORDER BY create_time DESC LIMIT 30"
    )
    try:
        rows = query("message_fts.db", sql, retries=1)
    except Exception:
        return None
    for r in rows:
        sender_id = int(r.get("sender_id") or 0)
        if not sender_id:
            continue
        if self_rid and sender_id == self_rid:
            continue
        if group:
            if not self_rid:
                continue
        else:
            # 私聊：对方发来 <=> sender_id == 会话 id
            if sender_id != sid:
                continue
        sender = _fts_username(sender_id) or username
        txt = normalize_text(r.get("text") or "", sender=sender, group=group)
        if not txt:
            continue
        return {
            "local_id": int(r["local_id"]),
            "create_time": int(r["create_time"]),
            "text": txt,
            "sender": sender,
            "from_fts": True,
            "kind": "text",
        }
    return None


def last_incoming_from_session(username, require_unread=False,
                               require_verified=False):
    """message_N.db 不可用时，用 SessionTable 摘要兜底（可能不完整）。"""
    k = username.replace("'", "''")
    try:
        rows = query(
            "session.db",
            "SELECT last_msg_locald_id, last_timestamp, summary, last_msg_sender, "
            "unread_count, last_clear_unread_timestamp, "
            f"last_msg_type FROM SessionTable WHERE username='{k}' LIMIT 1",
        )
    except Exception as e:
        if is_db_unavailable_error(e):
            return None
        raise
    if not rows:
        return None
    r = rows[0]
    if require_unread and int(r.get("unread_count") or 0) <= 0:
        # FTS 不可用时私聊 sender 可能为空。只对明确未读的会话降级，
        # 避免把我方刚发出的消息误当成对方消息再自动回复。
        return None
    if int(r.get("last_msg_type") or 0) != 1:
        return None
    self_wxid = get_self_wxid()
    sender = (r.get("last_msg_sender") or "").strip()
    group = is_group(username)
    unread_count = int(r.get("unread_count") or 0)
    create_time = int(r.get("last_timestamp") or 0)
    clear_unread_time = int(r.get("last_clear_unread_timestamp") or 0)
    direction_verified = False
    if group:
        if not self_wxid or not sender or sender == self_wxid:
            return None
        direction_verified = True
    else:
        if self_wxid and sender == self_wxid:
            return None
        if sender and sender != username:
            return None
        # 私聊 sender 在部分微信版本中恒为空。以下任一条件可证明是入站：
        # 1) 仍有未读；2) sender 明确等于联系人；3) 消息到达后才清除未读，
        #    即它曾作为入站消息出现，后来才被用户阅读。
        direction_verified = bool(
            unread_count > 0
            or sender == username
            or (not sender and create_time > 0
                and clear_unread_time >= create_time)
        )
        if require_verified and not direction_verified:
            return None
    txt = normalize_text(r.get("summary") or "", sender=sender, group=group)
    if not txt:
        return None
    local_id = int(r.get("last_msg_locald_id") or 0)
    if not local_id or not create_time:
        return None
    return {
        "local_id": local_id,
        "create_time": create_time,
        "text": txt,
        "sender": sender or username,
        "from_session": True,
        "direction_verified": direction_verified,
    }


def _is_incoming(username, sender, self_wxid):
    if is_group(username):
        return bool(self_wxid) and sender != self_wxid
    return sender == username


def incremental_messages_since(username, after_sort_seq=0, limit=100,
                               image_types=None):
    """按 sort_seq 增量读取一个会话的新消息，并返回推进后的安全游标。

    查询包含我方和暂不处理的消息类型，以便游标仍能前进；返回的 messages
    只保留对方/群成员发来的文本与图片。一次 SQL 即可取得活跃群一个窗口内
    的全部新消息，避免 LIMIT 1 覆盖前面的图片或其他成员发言。
    """
    found = find_msg_table(username)
    if not found:
        return {"messages": [], "cursor": int(after_sort_seq or 0),
                "has_more": False}
    db, table = found
    start = max(0, int(after_sort_seq or 0))
    cap = max(1, min(200, int(limit or 100)))
    sql = (
        "SELECT m.local_id AS local_id, m.sort_seq AS sort_seq, "
        "m.create_time AS create_time, m.local_type AS local_type, "
        "m.message_content AS content, n.user_name AS sender "
        f"FROM {table} m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id "
        f"WHERE m.sort_seq>{start} ORDER BY m.sort_seq ASC LIMIT {cap}"
    )
    rows = query(db, sql, retries=1)
    cursor = start
    out = []
    self_wxid = get_self_wxid()
    group = is_group(username)
    image_types = set(image_types or {3})
    for row in rows or []:
        seq = int(row.get("sort_seq") or 0)
        cursor = max(cursor, seq)
        sender = str(row.get("sender") or "")
        raw_content = str(row.get("content") or "")
        if not sender or not _is_incoming(username, sender, self_wxid):
            continue
        local_type = int(row.get("local_type") or 0)
        base = {
            "local_id": int(row.get("local_id") or 0),
            "sort_seq": seq,
            "create_time": int(row.get("create_time") or 0),
            "sender": sender,
            # 群 @ 元数据通常会携带被提及者 wxid；比仅匹配显示昵称可靠。
            "mentions_self": bool(
                group and self_wxid and self_wxid in raw_content),
        }
        if local_type == 1:
            text = normalize_text(raw_content, sender=sender,
                                  group=group)
            if text:
                out.append({**base, "kind": "text", "text": text})
        elif local_type in image_types:
            out.append({**base, "kind": "image", "text": "[图片]",
                        "raw_content": raw_content})
    return {"messages": out, "cursor": cursor,
            "has_more": len(rows or []) >= cap}


def latest_sort_seq(username):
    """读取会话当前最大 sort_seq，用于首次启用群队列时建立基线。"""
    found = find_msg_table(username)
    if not found:
        return 0
    db, table = found
    rows = query(
        db, f"SELECT MAX(sort_seq) AS seq FROM {table}", retries=1)
    return int(rows[0].get("seq") or 0) if rows else 0


def last_incoming_text(username):
    """返回该会话最近一条「对方/群成员发来的文本」。"""
    found = find_msg_table(username)
    if found:
        db, table = found
        self_wxid = get_self_wxid()
        group = is_group(username)

        if group:
            if not self_wxid:
                return last_incoming_from_session(username)
            sw = self_wxid.replace("'", "''")
            sql = (f"SELECT m.local_id AS local_id, m.create_time AS create_time, "
                   f"m.message_content AS text, n.user_name AS sender "
                   f"FROM {table} m JOIN Name2Id n ON n.rowid = m.real_sender_id "
                   f"WHERE m.local_type=1 AND n.user_name != '{sw}' "
                   f"ORDER BY m.sort_seq DESC LIMIT 1")
        else:
            u = username.replace("'", "''")
            sql = (f"SELECT m.local_id AS local_id, m.create_time AS create_time, "
                   f"m.message_content AS text, n.user_name AS sender "
                   f"FROM {table} m JOIN Name2Id n ON n.rowid = m.real_sender_id "
                   f"WHERE m.local_type=1 AND n.user_name = '{u}' "
                   f"ORDER BY m.sort_seq DESC LIMIT 1")

        rows = query(db, sql)
        if rows:
            r = rows[0]
            sender = r.get("sender") or ""
            txt = normalize_text(r.get("text") or "", sender=sender, group=group)
            if txt:
                return {"local_id": int(r["local_id"]),
                        "create_time": int(r["create_time"]),
                        "text": txt,
                        "sender": sender}

    return last_incoming_from_fts(username) or last_incoming_from_session(username)


def _fallback_incoming(username):
    """message_N.db 不可用时的兜底：优先 FTS（常开），再 session 摘要。"""
    return last_incoming_from_fts(username) or last_incoming_from_session(username)


def poll_incoming_text(username):
    """轮询专用：优先 FTS；FTS 缺失时仅用明确未读的 session 摘要。

    session 降级要求 unread_count>0，以免把我方消息认成对方消息。
    """
    fts = last_incoming_from_fts(username)
    session = last_incoming_from_session(
        username, require_unread=False, require_verified=True)
    if session and (
            not fts
            or int(session.get("create_time") or 0)
               > int(fts.get("create_time") or 0)):
        return session
    return fts or session


def last_incoming_message(username, image_types=None):
    """最近一条对方发来的消息（文本或图片）。

    策略：message_N.db 未打开时直接走 FTS（快、稳）；仅当 sqlite 消息库
    已打开时才扫 Msg_ 表（用于图片原文）。禁止对已关闭库反复超时查询。
    """
    image_types = image_types or {3}

    # 文本主路径：FTS 几乎一直开着
    fts = last_incoming_from_fts(username)

    if not message_sqlite_open():
        if fts:
            return fts
        sess = last_incoming_from_session(username)
        if sess:
            sess["kind"] = "text"
            return sess
        return None

    found = find_msg_table(username)
    if not found:
        return fts or _fallback_incoming(username)

    db, table = found
    self_wxid = get_self_wxid()
    group = is_group(username)
    types = ",".join(str(int(t)) for t in sorted(set(image_types) | {1}))

    if group:
        if not self_wxid:
            return fts or _fallback_incoming(username)
        sw = self_wxid.replace("'", "''")
        sql = (f"SELECT m.local_id AS local_id, m.create_time AS create_time, "
               f"m.local_type AS local_type, m.message_content AS content, "
               f"n.user_name AS sender "
               f"FROM {table} m JOIN Name2Id n ON n.rowid = m.real_sender_id "
               f"WHERE m.local_type IN ({types}) AND n.user_name != '{sw}' "
               f"ORDER BY m.sort_seq DESC LIMIT 1")
    else:
        u = username.replace("'", "''")
        sql = (f"SELECT m.local_id AS local_id, m.create_time AS create_time, "
               f"m.local_type AS local_type, m.message_content AS content, "
               f"n.user_name AS sender "
               f"FROM {table} m JOIN Name2Id n ON n.rowid = m.real_sender_id "
               f"WHERE m.local_type IN ({types}) AND n.user_name = '{u}' "
               f"ORDER BY m.sort_seq DESC LIMIT 1")

    rows = None
    try:
        rows = query(db, sql, retries=1)
    except Exception as e:
        if is_db_unavailable_error(e):
            return fts or _fallback_incoming(username)
        raise
    if not rows:
        return fts
    r = rows[0]
    sender = r.get("sender") or ""
    lt = int(r.get("local_type") or 1)
    create_time = int(r["create_time"])
    # sqlite 不比 FTS 新时，用 FTS（避免读到陈旧分表）
    if fts and int(fts.get("create_time") or 0) > create_time:
        return fts
    base = {
        "local_id": int(r["local_id"]),
        "create_time": create_time,
        "sender": sender,
        # Rows returned by this branch have been direction-checked against
        # Name2Id above, so callers may safely use them for restart catch-up.
        "from_message_db": True,
    }
    if lt in image_types:
        return {**base, "kind": "image",
                "raw_content": r.get("content") or "",
                "text": "[图片]"}
    txt = normalize_text(r.get("content") or "", sender=sender, group=group)
    if not txt:
        return fts
    return {**base, "kind": "text", "text": txt}


def recent_history_from_fts(username, limit=10):
    """从 message_fts.db 读最近 limit 条文本（message_N.db 关闭时用）。"""
    sid = _fts_rowid(username)
    if not sid:
        return []
    self_wxid = get_self_wxid()
    self_rid = _fts_rowid(self_wxid) if self_wxid else None
    group = is_group(username)
    parts = []
    for i in readable_fts_shards():
        parts.append(
            "SELECT acontent AS text, message_local_id AS local_id, "
            "sort_seq, create_time, sender_id "
            f"FROM message_fts_v4_{i} "
            f"WHERE session_id={sid} AND local_type=1"
        )
    if not parts:
        return []
    sql = (
        "SELECT * FROM (" + " UNION ALL ".join(parts) + ") "
        f"ORDER BY sort_seq DESC LIMIT {int(limit)}"
    )
    try:
        rows = query("message_fts.db", sql, retries=1)
    except Exception:
        return []
    out = []
    for r in reversed(rows or []):
        sender_id = int(r.get("sender_id") or 0)
        sender = _fts_username(sender_id) or ""
        txt = normalize_text(r.get("text") or "", sender=sender, group=group)
        if not txt:
            continue
        if group:
            incoming = bool(self_rid) and sender_id != self_rid
        else:
            incoming = sender_id == sid
        out.append({
            "role": "user" if incoming else "assistant",
            "text": txt,
            "sort_seq": int(r.get("sort_seq") or 0),
            "create_time": int(r.get("create_time") or 0),
            "local_id": int(r.get("local_id") or 0),
            "sender": sender or username,
        })
    return out


def recent_history(username, limit=10):
    """读该会话最近 limit 条文本消息，按时间升序返回。
    role: 对方/群成员= 'user'，我方= 'assistant'。
    返回 [{role, text, sort_seq, create_time, local_id, sender}]。"""
    # message_N.db 关闭时不要空返回——否则 AI 会突然“失忆”
    if not message_sqlite_open():
        return recent_history_from_fts(username, limit=limit)

    found = find_msg_table(username)
    if not found:
        return recent_history_from_fts(username, limit=limit)
    db, table = found
    self_wxid = get_self_wxid()
    group = is_group(username)

    sql = (f"SELECT m.local_id AS local_id, m.sort_seq AS sort_seq, "
           f"m.create_time AS create_time, "
           f"n.user_name AS sender, m.message_content AS text "
           f"FROM {table} m JOIN Name2Id n ON n.rowid = m.real_sender_id "
           f"WHERE m.local_type=1 AND m.message_content IS NOT NULL "
           f"ORDER BY m.sort_seq DESC LIMIT {int(limit)}")
    try:
        rows = query(db, sql, retries=1)
    except Exception as e:
        if is_db_unavailable_error(e):
            return recent_history_from_fts(username, limit=limit)
        raise
    out = []
    for r in reversed(rows):
        sender = r.get("sender") or ""
        txt = normalize_text(r.get("text") or "", sender=sender, group=group)
        if not txt:
            continue
        incoming = _is_incoming(username, sender, self_wxid)
        out.append({"role": "user" if incoming else "assistant",
                    "text": txt,
                    "sort_seq": int(r["sort_seq"]),
                    "create_time": int(r.get("create_time") or 0),
                    "local_id": int(r["local_id"]),
                    "sender": sender})
    # sqlite 读到空时再兜底 FTS
    return out or recent_history_from_fts(username, limit=limit)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("self_wxid:", get_self_wxid())
    print("message dbs:", msg_dbs())
    if len(sys.argv) > 1:
        who = sys.argv[1]
        print("table:", table_for(who), "->", find_msg_table(who))
        print("last incoming:", last_incoming_text(who))
