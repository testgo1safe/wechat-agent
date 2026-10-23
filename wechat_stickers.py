# -*- coding: utf-8 -*-
"""Learn and conservatively send the account owner's favorite stickers."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.request
from pathlib import Path

import wechat_recv as rc


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "sticker_cache"
CATALOG_FILE = CACHE_DIR / "catalog.json"
STATE_FILE = CACHE_DIR / "state.json"

CATEGORIES = {
    "laugh", "agree", "comfort", "speechless", "cute", "thanks", "other"
}

DEFAULT_CONFIG = {
    "enabled": False,
    "learn_from_favorites": True,
    "catalog_limit": 24,
    "classify_per_sync": 6,
    "sync_hours": 6,
    "probability": 0.12,
    "strong_probability": 0.35,
    "cooldown_seconds": 7200,
    "max_per_day": 3,
    "max_message_chars": 30,
}


def parse_config(cfg):
    raw = cfg.get("stickers") if isinstance(cfg, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_CONFIG)
    out.update(raw)
    out["enabled"] = bool(out.get("enabled"))
    out["learn_from_favorites"] = bool(out.get("learn_from_favorites", True))
    out["catalog_limit"] = max(4, min(100, int(out.get("catalog_limit") or 24)))
    out["classify_per_sync"] = max(0, min(20, int(out.get("classify_per_sync") or 0)))
    out["sync_hours"] = max(1, min(168, float(out.get("sync_hours") or 6)))
    out["probability"] = max(0.0, min(0.5, float(out.get("probability") or 0)))
    out["strong_probability"] = max(
        out["probability"], min(0.8, float(out.get("strong_probability") or 0)))
    out["cooldown_seconds"] = max(
        300, min(86400, int(out.get("cooldown_seconds") or 7200)))
    out["max_per_day"] = max(1, min(10, int(out.get("max_per_day") or 3)))
    out["max_message_chars"] = max(
        8, min(80, int(out.get("max_message_chars") or 30)))
    return out


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except Exception:
        return default


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_catalog():
    value = _read_json(CATALOG_FILE, {"items": [], "updated_at": 0})
    if not isinstance(value, dict):
        return {"items": [], "updated_at": 0}
    value["items"] = [x for x in (value.get("items") or []) if isinstance(x, dict)]
    return value


def _favorite_rows(limit):
    return rc.query(
        "emoticon.db",
        "SELECT f.rowid AS favorite_order, n.md5, n.cdn_url "
        "FROM kFavEmoticonOrderTable f "
        "JOIN kNonStoreEmoticonTable n ON n.md5=f.md5 "
        "WHERE LENGTH(n.md5)=32 AND LENGTH(n.cdn_url)>0 "
        f"ORDER BY f.rowid DESC LIMIT {int(limit)}",
        retries=1,
    )


def _format(data):
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _download(row, timeout=20):
    md5 = str(row.get("md5") or "").lower()
    url = str(row.get("cdn_url") or "")
    if not re.fullmatch(r"[a-f0-9]{32}", md5) or not url:
        return None
    for ext in ("gif", "png", "jpg", "webp"):
        old = CACHE_DIR / f"{md5}.{ext}"
        if old.is_file():
            return old
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(8 * 1024 * 1024 + 1)
    if not data or len(data) > 8 * 1024 * 1024:
        return None
    if hashlib.md5(data).hexdigest() != md5:
        return None
    ext = _format(data)
    if not ext:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{md5}.{ext}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def _ensure_sendable(path, md5):
    """Return a jpg/png path accepted reliably by the current image Hook."""
    path = Path(path)
    if path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return path
    out = CACHE_DIR / f"{md5}.send.png"
    if out.is_file():
        return out
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.seek(0)
            frame = image.convert("RGBA")
            tmp = out.with_suffix(out.suffix + ".tmp")
            frame.save(tmp, format="PNG", optimize=True)
        os.replace(tmp, out)
        return out
    except Exception:
        return None


def _json_object(raw):
    match = re.search(r"\{[\s\S]*\}", str(raw or ""))
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _classify(client, path):
    prompt = (
        "这是微信收藏表情。只输出一行JSON："
        '{"category":"laugh|agree|comfort|speechless|cute|thanks|other",'
        '"description":"不超过20字","safe":true}。'
        "按表情实际含义分类；涉及色情、暴力、侮辱、政治或含义不清时 safe=false。"
    )
    raw = client.chat_vision([], [str(path)], prompt_extra=prompt, timeout=90)
    obj = _json_object(raw)
    category = str(obj.get("category") or "other").strip().lower()
    if category not in CATEGORIES:
        category = "other"
    return {
        "category": category,
        "description": str(obj.get("description") or "").strip()[:40],
        "safe": bool(obj.get("safe")),
        "classified_at": int(time.time()),
    }


def refresh_catalog(config, client=None):
    """Download recent favorites and classify a bounded number of new ones."""
    cfg = parse_config({"stickers": config})
    if not cfg["enabled"] or not cfg["learn_from_favorites"]:
        return {"downloaded": 0, "classified": 0, "available": 0}
    catalog = load_catalog()
    by_md5 = {str(x.get("md5") or ""): x for x in catalog["items"]}
    downloaded = 0
    for row in _favorite_rows(cfg["catalog_limit"]):
        md5 = str(row.get("md5") or "").lower()
        item = by_md5.setdefault(md5, {"md5": md5, "source": "favorite"})
        try:
            path = _download(row)
        except Exception:
            path = None
        if path:
            if not item.get("file"):
                downloaded += 1
            item["file"] = str(path.relative_to(ROOT))
            send_path = _ensure_sendable(path, md5)
            if send_path:
                item["send_file"] = str(send_path.relative_to(ROOT))
            item["favorite_order"] = int(row.get("favorite_order") or 0)

    classified = 0
    if client and getattr(client, "vision", {}).get("enabled"):
        pending = sorted(
            (x for x in by_md5.values()
             if x.get("file") and not x.get("classified_at")),
            key=lambda x: int(x.get("favorite_order") or 0), reverse=True,
        )[:cfg["classify_per_sync"]]
        for item in pending:
            try:
                item.update(_classify(client, ROOT / item["file"]))
                classified += 1
            except Exception:
                continue
    catalog = {
        "updated_at": int(time.time()),
        "items": sorted(
            by_md5.values(),
            key=lambda x: int(x.get("favorite_order") or 0), reverse=True,
        )[:cfg["catalog_limit"]],
    }
    _write_json(CATALOG_FILE, catalog)
    available = sum(
        bool(x.get("safe") and x.get("category") != "other" and x.get("file"))
        for x in catalog["items"]
    )
    return {"downloaded": downloaded, "classified": classified, "available": available}


_CATEGORY_PATTERNS = {
    "laugh": re.compile(r"哈哈|笑死|好笑|逗|乐死|绷不住|hhh", re.I),
    "speechless": re.compile(r"无语|离谱|服了|尴尬|(?:[.。．…]\s*){2,}"),
    "comfort": re.compile(r"难受|委屈|烦死|心累|好累|疼|不开心|崩溃"),
    "cute": re.compile(r"可爱|宝[宝贝]?|想你|爱你|亲亲|么么"),
    "thanks": re.compile(r"谢谢|谢啦|辛苦|麻烦你了"),
    "agree": re.compile(r"^(?:行|好呀?|可以|确实|对的?|没错|就这样)[啊呀吧嘛]*$"),
}
_SERIOUS_RE = re.compile(
    r"转账|银行卡|密码|验证码|报警|住院|急诊|合同|离婚|死亡|欠款|投诉|律师|法院"
)


def _message_category(text):
    value = str(text or "").strip()
    for category, pattern in _CATEGORY_PATTERNS.items():
        if pattern.search(value):
            return category
    return ""


def _today(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def choose(username, text, seed, config):
    """Return a safe sticker item for a short reaction, otherwise None."""
    cfg = parse_config({"stickers": config})
    value = str(text or "").strip()
    if (not cfg["enabled"] or not value
            or len(value) > cfg["max_message_chars"]
            or "?" in value or "？" in value or _SERIOUS_RE.search(value)):
        return None
    category = _message_category(value)
    if not category:
        return None

    state = _read_json(STATE_FILE, {"users": {}})
    user_state = (state.get("users") or {}).get(username) or {}
    now = int(time.time())
    if now - int(user_state.get("last_sent_at") or 0) < cfg["cooldown_seconds"]:
        return None
    if (user_state.get("day") == _today(now)
            and int(user_state.get("day_count") or 0) >= cfg["max_per_day"]):
        return None

    strong = category in {"laugh", "speechless", "comfort", "cute", "thanks"}
    probability = cfg["strong_probability"] if strong else cfg["probability"]
    digest = hashlib.sha256(f"{username}|{seed}|{category}".encode()).digest()
    if int.from_bytes(digest[:4], "big") / 2**32 >= probability:
        return None

    items = []
    last_md5 = str(user_state.get("last_md5") or "")
    for item in load_catalog()["items"]:
        path = ROOT / str(item.get("send_file") or item.get("file") or "")
        if (item.get("safe") and item.get("category") == category
                and item.get("md5") != last_md5 and path.is_file()):
            items.append({**item, "path": str(path)})
    if not items:
        return None
    rng = random.Random(int.from_bytes(digest[4:12], "big"))
    return rng.choice(items)


def record_sent(username, md5):
    state = _read_json(STATE_FILE, {"users": {}})
    users = state.setdefault("users", {})
    old = users.get(username) or {}
    day = _today()
    users[username] = {
        "last_sent_at": int(time.time()),
        "last_md5": str(md5 or ""),
        "day": day,
        "day_count": (int(old.get("day_count") or 0) + 1
                      if old.get("day") == day else 1),
    }
    _write_json(STATE_FILE, state)
