# -*- coding: utf-8 -*-
"""每个会话独立的长期记忆存储（摘要式）。

- 每个会话一个独立的 SQLite 文件：memory/<md5(username)>.db
- 记忆 = 滚动“长期摘要” + last_summarized_seq(已摘要到的消息 sort_seq)
- 短期原文由微信库(message_N.db)提供，避免重复存储、且包含手动发的消息。
"""

import hashlib
import os
import sqlite3

MEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")

DEFAULT_MEMORY_CONFIG = {
    # prompt 中保留的最近原文；更早内容增量折叠进长期摘要。
    "keep_recent": 20,
    "history_window": 120,
    "summary_batch": 10,
    # 自动生成/更新摘要只对白名单会话生效；手动编辑摘要不受影响。
    "whitelist_only": True,
}


def parse_memory_config(cfg):
    out = dict(DEFAULT_MEMORY_CONFIG)
    raw = (cfg or {}).get("memory")
    if isinstance(raw, dict):
        for key in out:
            if key in raw and raw[key] is not None:
                out[key] = raw[key]
    for key, default, lo, hi in (
        ("keep_recent", 20, 6, 50),
        ("history_window", 120, 30, 300),
        ("summary_batch", 10, 5, 50),
    ):
        try:
            out[key] = max(lo, min(hi, int(out.get(key) or default)))
        except (TypeError, ValueError):
            out[key] = default
    out["history_window"] = max(
        out["history_window"],
        out["keep_recent"] + out["summary_batch"] + 5,
    )
    out["whitelist_only"] = bool(out.get("whitelist_only"))
    return out


def _path(username, ensure_dir=False):
    if ensure_dir:
        os.makedirs(MEM_DIR, exist_ok=True)
    h = hashlib.md5(username.encode("utf-8")).hexdigest()
    return os.path.join(MEM_DIR, h + ".db")


def _connect_for_write(username):
    conn = sqlite3.connect(_path(username, ensure_dir=True))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory (k TEXT PRIMARY KEY, v TEXT)")
    # 记录真实 username 便于人工查看
    conn.execute("INSERT OR IGNORE INTO memory(k, v) VALUES('username', ?)",
                 (username,))
    conn.commit()
    return conn


def load(username):
    """返回 (summary:str, last_summarized_seq:int)。"""
    path = _path(username)
    if not os.path.isfile(path):
        return "", 0
    # 读取上下文不应隐式建表/写 username。否则只读或跨权限创建的 DB
    # 会让整个回复循环失败并重复处理同一条消息。
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            rows = dict(conn.execute("SELECT k, v FROM memory").fetchall())
        except sqlite3.OperationalError:
            rows = {}
    finally:
        conn.close()
    summary = rows.get("summary", "") or ""
    try:
        last_seq = int(rows.get("last_summarized_seq", "0") or "0")
    except ValueError:
        last_seq = 0
    return summary, last_seq


def save(username, summary, last_summarized_seq):
    conn = _connect_for_write(username)
    try:
        conn.execute("INSERT OR REPLACE INTO memory(k, v) VALUES('summary', ?)",
                     (summary or "",))
        conn.execute(
            "INSERT OR REPLACE INTO memory(k, v) VALUES('last_summarized_seq', ?)",
            (str(int(last_summarized_seq)),))
        conn.commit()
    finally:
        conn.close()


def reset(username):
    """清空某会话的长期记忆。"""
    p = _path(username)
    if os.path.exists(p):
        os.remove(p)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) > 1:
        u = sys.argv[1]
        s, seq = load(u)
        print(f"username={u}")
        print(f"last_summarized_seq={seq}")
        print(f"summary=\n{s}")
    else:
        print("memory dir:", MEM_DIR)
        if os.path.isdir(MEM_DIR):
            for f in os.listdir(MEM_DIR):
                print(" ", f)
