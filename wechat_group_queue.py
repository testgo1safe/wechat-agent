# -*- coding: utf-8 -*-
"""活跃群消息的持久化队列、合并窗口与保守触发规则。"""

import os
import re
import sqlite3
import time
import uuid


DEFAULT_GROUP_REPLY = {
    "enabled": True,
    "require_mention": True,
    "mention_names": [],
    "reply_on_question": False,
    "reply_on_image": False,
    "trigger_keywords": [],
    "settle_seconds": 6,
    "cooldown_seconds": 12,
    "expiry_seconds": 120,
    "max_batch_messages": 20,
    "max_batch_images": 3,
    "image_context_seconds": 30,
}


def parse_group_reply_config(cfg):
    out = dict(DEFAULT_GROUP_REPLY)
    block = (cfg or {}).get("group_reply")
    if isinstance(block, dict):
        for key in out:
            if key in block and block[key] is not None:
                out[key] = block[key]
    for key, lo, hi in (
        ("settle_seconds", 3, 20),
        ("cooldown_seconds", 0, 120),
        ("expiry_seconds", 30, 600),
        ("max_batch_messages", 2, 50),
        ("max_batch_images", 1, 5),
        ("image_context_seconds", 0, 120),
    ):
        try:
            out[key] = max(lo, min(hi, int(out[key])))
        except (TypeError, ValueError):
            out[key] = DEFAULT_GROUP_REPLY[key]
    for key in ("mention_names", "trigger_keywords"):
        value = out.get(key)
        if not isinstance(value, list):
            value = [value] if value else []
        out[key] = [str(x).strip() for x in value if str(x).strip()]
    for key in ("enabled", "require_mention", "reply_on_question", "reply_on_image"):
        out[key] = bool(out.get(key))
    return out


_QUESTION_RE = re.compile(
    r"[?？]|(?:怎么|怎样|咋|为什么|为啥|能否|可以吗|行吗|多少|哪里|哪儿|"
    r"什么时候|谁|是否|有没有|帮(?:我)?|看下|看看|说说|觉得呢)"
)


def trigger_reason(messages, cfg):
    """返回 (是否触发, 原因)。默认只有明确 @ 或配置关键词才插话。"""
    cfg = cfg or DEFAULT_GROUP_REPLY
    if not cfg.get("enabled", True):
        return False, "群自动回复已禁用"
    texts = [str(x.get("text") or "").strip() for x in messages]
    combined = "\n".join(x for x in texts if x)
    mention_names = cfg.get("mention_names") or []
    if any(bool(x.get("mentions_self")) for x in messages):
        mentioned = True
    elif mention_names:
        mentioned = any(("@" + name) in combined for name in mention_names)
    else:
        # 不知道账号在群里的昵称时不能把 @其他人 误判为 @账号。
        mentioned = False
    if mentioned:
        return True, "被@"
    for keyword in cfg.get("trigger_keywords") or []:
        if keyword and keyword in combined:
            return True, f"关键词:{keyword}"
    if cfg.get("require_mention", True):
        return False, "未@账号"
    if cfg.get("reply_on_question") and _QUESTION_RE.search(combined):
        return True, "明确提问"
    if cfg.get("reply_on_image") and any(x.get("kind") == "image" for x in messages):
        return True, "收到图片"
    return False, "普通群聊"


def format_batch(messages, name_resolver=None):
    lines = []
    resolver = name_resolver or (lambda x: x)
    for item in messages:
        sender = str(item.get("sender") or "群成员")
        label = resolver(sender) or sender or "群成员"
        body = "[图片]" if item.get("kind") == "image" else str(item.get("text") or "")
        lines.append(f"{label}: {body}")
    return "\n".join(lines)


class GroupTaskQueue:
    """SQLite 队列：入队与游标同事务提交，进程退出后不会丢已发现消息。"""

    def __init__(self, path, cfg=None, clock=None):
        self.path = os.path.abspath(path)
        self.cfg = parse_group_reply_config(cfg or {})
        self.clock = clock or time.time
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS group_cursor(
              group_id TEXT PRIMARY KEY,
              last_sort_seq INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS group_message(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              group_id TEXT NOT NULL,
              sort_seq INTEGER NOT NULL,
              local_id INTEGER NOT NULL,
              create_time INTEGER NOT NULL,
              sender TEXT NOT NULL,
              kind TEXT NOT NULL,
              text TEXT NOT NULL DEFAULT '',
              raw_content TEXT NOT NULL DEFAULT '',
              dat_path TEXT NOT NULL DEFAULT '',
              mentions_self INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'queued',
              inserted_at REAL NOT NULL,
              available_at REAL NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 0,
              batch_token TEXT NOT NULL DEFAULT '',
              UNIQUE(group_id, sort_seq, local_id, sender)
            );
            CREATE INDEX IF NOT EXISTS idx_group_message_status
              ON group_message(status, group_id, inserted_at);
            CREATE TABLE IF NOT EXISTS group_state(
              group_id TEXT PRIMARY KEY,
              last_served REAL NOT NULL DEFAULT 0,
              last_reply REAL NOT NULL DEFAULT 0
            );
            """
        )
        columns = {
            str(x[1]) for x in self.db.execute(
                "PRAGMA table_info(group_message)").fetchall()
        }
        if "mentions_self" not in columns:
            self.db.execute(
                "ALTER TABLE group_message ADD COLUMN mentions_self "
                "INTEGER NOT NULL DEFAULT 0")
        # 上次进程若在生成中退出，任务回到队列；发送成功后才会标 done。
        self.db.execute(
            "UPDATE group_message SET status='queued', batch_token='' "
            "WHERE status='processing'"
        )
        self.db.commit()

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    def update_config(self, cfg):
        self.cfg = parse_group_reply_config(cfg or {})

    def cursor(self, group_id):
        row = self.db.execute(
            "SELECT last_sort_seq FROM group_cursor WHERE group_id=?",
            (group_id,),
        ).fetchone()
        return int(row[0]) if row else None

    def set_cursor(self, group_id, sort_seq):
        self.db.execute(
            "INSERT INTO group_cursor(group_id,last_sort_seq) VALUES(?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET last_sort_seq="
            "MAX(last_sort_seq,excluded.last_sort_seq)",
            (group_id, int(sort_seq or 0)),
        )
        self.db.commit()

    def enqueue(self, group_id, messages, cursor):
        now = float(self.clock())
        added = 0
        with self.db:
            for item in messages or []:
                before = self.db.total_changes
                self.db.execute(
                    "INSERT OR IGNORE INTO group_message(" 
                    "group_id,sort_seq,local_id,create_time,sender,kind,text," 
                    "raw_content,dat_path,mentions_self,status,inserted_at,available_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,0)",
                    (
                        group_id,
                        int(item.get("sort_seq") or 0),
                        int(item.get("local_id") or 0),
                        int(item.get("create_time") or 0),
                        str(item.get("sender") or ""),
                        str(item.get("kind") or "text"),
                        str(item.get("text") or ""),
                        str(item.get("raw_content") or ""),
                        str(item.get("dat_path") or ""),
                        1 if item.get("mentions_self") else 0,
                        now,
                    ),
                )
                if self.db.total_changes > before:
                    added += 1
            self.db.execute(
                "INSERT INTO group_cursor(group_id,last_sort_seq) VALUES(?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET last_sort_seq="
                "MAX(last_sort_seq,excluded.last_sort_seq)",
                (group_id, int(cursor or 0)),
            )
        return added

    def _expire(self, now):
        cutoff = int(now) - int(self.cfg["expiry_seconds"])
        self.db.execute(
            "UPDATE group_message SET status='expired', batch_token='' "
            "WHERE status='queued' AND create_time>0 AND create_time<?",
            (cutoff,),
        )
        self.db.commit()

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def claim_next(self, active_groups=(), allowed_groups=None):
        now = float(self.clock())
        self._expire(now)
        excluded = set(active_groups or ())
        allowed = set(allowed_groups) if allowed_groups is not None else None
        rows = self.db.execute(
            "SELECT m.group_id, MIN(m.inserted_at) AS oldest, "
            "MAX(m.inserted_at) AS newest, COALESCE(s.last_served,0) AS served, "
            "COALESCE(s.last_reply,0) AS replied "
            "FROM group_message m LEFT JOIN group_state s ON s.group_id=m.group_id "
            "WHERE m.status='queued' AND m.available_at<=? GROUP BY m.group_id "
            "ORDER BY served ASC, oldest ASC",
            (now,),
        ).fetchall()
        chosen = None
        for row in rows:
            group_id = row["group_id"]
            if allowed is not None and group_id not in allowed:
                continue
            if group_id in excluded:
                continue
            if now - float(row["newest"] or 0) < self.cfg["settle_seconds"]:
                continue
            if now - float(row["replied"] or 0) < self.cfg["cooldown_seconds"]:
                continue
            chosen = group_id
            break
        if not chosen:
            return None
        batch_rows = self.db.execute(
            "SELECT * FROM group_message WHERE group_id=? AND status='queued' "
            "AND available_at<=? ORDER BY sort_seq,id LIMIT ?",
            (chosen, now, int(self.cfg["max_batch_messages"])),
        ).fetchall()
        if not batch_rows:
            return None
        token = uuid.uuid4().hex
        ids = [int(x["id"]) for x in batch_rows]
        marks = ",".join("?" for _ in ids)
        self.db.execute(
            f"UPDATE group_message SET status='processing',batch_token=? "
            f"WHERE id IN ({marks})", (token, *ids))
        self.db.commit()
        return {
            "group_id": chosen,
            "token": token,
            "ids": ids,
            "messages": [self._row(x) for x in batch_rows],
        }

    def recent_images(self, group_id, before_time=None):
        seconds = int(self.cfg.get("image_context_seconds") or 0)
        if seconds <= 0:
            return []
        before = int(before_time or self.clock())
        rows = self.db.execute(
            "SELECT * FROM group_message WHERE group_id=? AND kind='image' "
            "AND status='done' AND create_time BETWEEN ? AND ? "
            "ORDER BY sort_seq DESC LIMIT ?",
            (group_id, before - seconds, before,
             int(self.cfg["max_batch_images"])),
        ).fetchall()
        return [self._row(x) for x in reversed(rows)]

    def finish(self, batch, replied=False):
        ids = [int(x) for x in (batch or {}).get("ids") or []]
        if not ids:
            return
        now = float(self.clock())
        marks = ",".join("?" for _ in ids)
        with self.db:
            self.db.execute(
                f"UPDATE group_message SET status='done',batch_token='' "
                f"WHERE id IN ({marks})", ids)
            self.db.execute(
                "INSERT INTO group_state(group_id,last_served,last_reply) "
                "VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET "
                "last_served=excluded.last_served,last_reply="
                "CASE WHEN excluded.last_reply>0 THEN excluded.last_reply "
                "ELSE group_state.last_reply END",
                (batch["group_id"], now, now if replied else 0),
            )

    def retry(self, batch, delay):
        ids = [int(x) for x in (batch or {}).get("ids") or []]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.db.execute(
            f"UPDATE group_message SET status='queued',batch_token='', "
            f"attempts=attempts+1,available_at=? WHERE id IN ({marks})",
            (float(self.clock()) + max(1, int(delay)), *ids),
        )
        self.db.commit()

    def pending_count(self):
        row = self.db.execute(
            "SELECT COUNT(1) FROM group_message WHERE status IN ('queued','processing')"
        ).fetchone()
        return int(row[0] or 0)
