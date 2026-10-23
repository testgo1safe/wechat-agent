# -*- coding: utf-8 -*-
"""微信图片：定位 .dat 缓存、Hook 解密、供 Vision 模型使用。"""

import os
import re
import tempfile
import time

import wechat_recv as rc
import wechat_send as ws

# 微信 4.1.x：1=文本 3=图片 43=语音 47=表情/贴纸
IMAGE_LOCAL_TYPES = {3}
SESSION_IMAGE_TYPES = {3}

_MD5_RE = re.compile(r"[a-f0-9]{32}", re.I)
_CACHE = {"bases": None, "accounts": None, "ts": 0}


def _md5_from_packed(packed_hex):
    if not packed_hex:
        return None
    try:
        raw = bytes.fromhex(packed_hex)
        text = raw.decode("ascii", errors="ignore")
        m = _MD5_RE.search(text)
        if m:
            return m.group(0).lower()
    except Exception:
        pass
    m = _MD5_RE.search(str(packed_hex))
    return m.group(0).lower() if m else None


def _xwechat_bases():
    """返回 xwechat_files 根目录列表（含 hardlink.db 记录的原始路径）。"""
    now = time.time()
    if _CACHE["bases"] and now - _CACHE["ts"] < 300:
        return _CACHE["bases"]

    bases = []
    try:
        rows = rc.query("hardlink.db", "SELECT ValueStdStr FROM db_info WHERE Key='uuid'")
        if rows:
            uuid = rows[0].get("ValueStdStr") or ""
            # 例: 16369_C8D9D21D61A5_E:\hh\xwechat_files
            if "_" in uuid:
                path = uuid.split("_", 2)[-1]
                if os.path.isdir(path):
                    bases.append(path)
    except Exception:
        pass

    home = os.path.expanduser("~")
    for p in (
        os.path.join(home, "Documents", "xwechat_files"),
        os.path.join(home, "Documents", "WeChat Files"),
        os.path.join(home, "xwechat_files"),
        r"D:\wx\xwechat_files",
        r"E:\hh\xwechat_files",
    ):
        if os.path.isdir(p) and p not in bases:
            bases.append(p)

    _CACHE["bases"] = bases
    _CACHE["accounts"] = None
    _CACHE["ts"] = now
    return bases


def _account_dirs():
    now = time.time()
    if _CACHE["accounts"] and _CACHE["bases"] and now - _CACHE["ts"] < 300:
        return _CACHE["accounts"]

    accounts = []
    wxid = rc.get_self_wxid() or ""
    for base in _xwechat_bases():
        try:
            for name in os.listdir(base):
                if not name.startswith("wxid_"):
                    continue
                if wxid and wxid not in name:
                    continue
                p = os.path.join(base, name)
                if os.path.isdir(p):
                    accounts.append(p)
        except OSError:
            continue
    if not accounts:
        for base in _xwechat_bases():
            try:
                for name in os.listdir(base):
                    if name.startswith("wxid_"):
                        accounts.append(os.path.join(base, name))
            except OSError:
                continue
    accounts = list(dict.fromkeys(accounts))
    _CACHE["accounts"] = accounts
    return accounts


def _attach_dat_path(u1, u2, file_name):
    """构造 msg/attach/{hash}/{YYYY-MM}/Img/{file}.dat 路径。"""
    subdirs = (
        ("msg", "attach", u1, u2, "Img", file_name),
        ("msg", "attach", u1, u2, file_name),
        ("cache", u2, "Message", u1, "Img", file_name),
    )
    for acct in _account_dirs():
        for parts in subdirs:
            p = os.path.join(acct, *parts)
            if os.path.isfile(p):
                return p
    return None


def _hardlink_dat(md5):
    if not md5:
        return None
    md5 = md5.lower().replace("'", "''")
    try:
        rows = rc.query(
            "hardlink.db",
            "SELECT h.file_name, d1.username AS u1, d2.username AS u2 "
            "FROM image_hardlink_info_v4 h "
            "LEFT JOIN dir2id d1 ON d1.rowid = h.dir1 "
            "LEFT JOIN dir2id d2 ON d2.rowid = h.dir2 "
            f"WHERE lower(h.md5) = '{md5}' "
            f"OR lower(h.file_name) LIKE '%{md5}%' LIMIT 5",
        )
        for r in rows:
            p = _attach_dat_path(r.get("u1"), r.get("u2"), r.get("file_name"))
            if p:
                return p
    except Exception:
        pass
    return None


def find_dat_by_md5(md5, max_depth=10):
    if not md5:
        return None
    hit = _hardlink_dat(md5)
    if hit:
        return hit
    md5 = md5.lower()
    for acct in _account_dirs():
        depth = acct.count(os.sep)
        for dirpath, dirnames, files in os.walk(acct):
            if dirpath.count(os.sep) - depth > max_depth:
                dirnames.clear()
                continue
            for f in files:
                if md5 in f.lower() and f.endswith(".dat"):
                    return os.path.join(dirpath, f)
    return None


def _resource_md5(username, local_id):
    u = username.replace("'", "''")
    try:
        rows = rc.query(
            "message_resource.db",
            "SELECT m.packed_info FROM MessageResourceInfo m "
            "JOIN ChatName2Id c ON c.rowid = m.chat_id "
            f"WHERE c.user_name = '{u}' "
            f"AND m.message_local_id = {int(local_id)} "
            "AND m.message_local_type = 3 LIMIT 1",
        )
        if rows:
            return _md5_from_packed(rows[0].get("packed_info"))
    except Exception:
        pass
    return None


def resolve_dat_path(username, local_id, message_content=None):
    md5 = _resource_md5(username, local_id)
    if not md5 and message_content:
        md5 = _md5_from_packed(message_content)
    if not md5:
        return None
    return _hardlink_dat(md5) or find_dat_by_md5(md5)


def incoming_image_from_session(username):
    """Build a verified incoming-image candidate without opening message_N.db.

    SessionTable tells us which local message changed, while message_resource.db
    contains the image sender and file metadata.  Requiring the resource sender
    to be the peer (or a non-self group member) prevents replying to our own
    image when the main message shard is closed.
    """
    u = str(username or "").replace("'", "''")
    if not u:
        return None
    try:
        sessions = rc.query(
            "session.db",
            "SELECT last_msg_locald_id, last_timestamp, last_msg_type "
            f"FROM SessionTable WHERE username='{u}' LIMIT 1",
            retries=1,
        )
        if not sessions:
            return None
        sess = sessions[0]
        if int(sess.get("last_msg_type") or 0) not in SESSION_IMAGE_TYPES:
            return None
        local_id = int(sess.get("last_msg_locald_id") or 0)
        create_time = int(sess.get("last_timestamp") or 0)
        if not local_id or not create_time:
            return None

        resources = rc.query(
            "message_resource.db",
            "SELECT m.message_create_time AS create_time, "
            "m.packed_info AS packed_info, s.user_name AS sender "
            "FROM MessageResourceInfo m "
            "JOIN ChatName2Id c ON c.rowid=m.chat_id "
            "LEFT JOIN SenderName2Id s ON s.rowid=m.sender_id "
            f"WHERE c.user_name='{u}' "
            f"AND m.message_local_id={local_id} "
            "AND m.message_local_type=3 LIMIT 1",
            retries=1,
        )
    except Exception:
        return None
    if not resources:
        return None

    row = resources[0]
    resource_time = int(row.get("create_time") or 0)
    if resource_time and resource_time != create_time:
        return None
    sender = (row.get("sender") or "").strip()
    self_wxid = rc.get_self_wxid()
    if rc.is_group(username):
        if not self_wxid or not sender or sender == self_wxid:
            return None
    elif sender != username:
        return None

    md5 = _md5_from_packed(row.get("packed_info"))
    dat_path = (_hardlink_dat(md5) or find_dat_by_md5(md5)) if md5 else None
    if not dat_path or not os.path.isfile(dat_path):
        return None
    return {
        "local_id": local_id,
        "create_time": create_time,
        "sender": sender,
        "kind": "image",
        "text": "[图片]",
        "dat_path": dat_path,
        "from_session_resource": True,
    }


def _image_decrypt_cfg():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "autoreply_config.json")
    try:
        import json
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    import dat_decrypt as dd
    return dd.parse_image_decrypt_config(cfg)


def decode_to_jpeg(dat_path, username=None, local_id=None):
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_cache")
    os.makedirs(cache_dir, exist_ok=True)
    tag = f"{username or 'u'}_{local_id or 'x'}_{int(time.time())}"
    out = os.path.join(cache_dir, f"{tag}.jpg")

    keys = _image_decrypt_cfg()
    aes = keys.get("aes_key")
    xor = keys.get("xor_key", 0x88)

    import dat_decrypt as dd
    path, fmt = dd.decrypt_dat_file(dat_path, out, aes_key=aes, xor_key=xor)
    if path:
        return path

    raise RuntimeError(
        "图片解密失败：V2 .dat 需要 image_aes_key。"
        "请先在微信里点开 2~3 张大图，再运行 python find_image_key.py")


def prepare_incoming_image(info, username):
    dat = info.get("dat_path") or resolve_dat_path(
        username, info.get("local_id"), info.get("raw_content"))
    if not dat or not os.path.isfile(dat):
        raise FileNotFoundError(
            f"找不到图片 .dat (local_id={info.get('local_id')})")
    return decode_to_jpeg(dat, username, info.get("local_id"))
