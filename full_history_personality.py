# -*- coding: utf-8 -*-
"""基于单个微信会话的全量文本，生成分层、可断点续跑的人物性格报告。

原文仅在内存中分块发送给本机 Ollama；磁盘只保存阶段摘要和最终报告。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import wechat_recv as recv


ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_CHUNK_MODEL = "qwen2.5:1.5b"
FTS_TABLES = tuple(f"message_fts_v4_{i}" for i in range(4))


def _safe_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def ollama_chat(messages, model=DEFAULT_MODEL, timeout=900, temperature=0.2,
                num_predict=1800):
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": 32768,
            "num_predict": num_predict,
        },
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    text = ((data.get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError(f"Ollama 未返回正文: {data}")
    return text


def verify_ollama(model):
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    names = {str(x.get("name") or "") for x in data.get("models", [])}
    if model not in names and not any(x.startswith(model + ":") for x in names):
        raise RuntimeError(f"本机 Ollama 没有模型 {model}；当前: {sorted(names)}")


def _sql_int(value):
    return int(value or 0)


def fetch_table_rows(db, table, session_id, batch_size=1200):
    """按时间游标读取单张 FTS 表，避免 OFFSET 在大表上越来越慢。"""
    rows_out = []
    cursor_ts = -1
    cursor_id = -1
    while True:
        sql = (
            "SELECT message_local_id AS local_id, create_time, sender_id, "
            "acontent AS text FROM " + table + " "
            f"WHERE session_id={int(session_id)} AND local_type=1 AND ("
            f"create_time>{cursor_ts} OR (create_time={cursor_ts} "
            f"AND message_local_id>{cursor_id})) "
            "ORDER BY create_time ASC, message_local_id ASC "
            f"LIMIT {int(batch_size)}"
        )
        batch = recv.query(db, sql, timeout=30, retries=2)
        if not batch:
            break
        for row in batch:
            ts = _sql_int(row.get("create_time"))
            lid = _sql_int(row.get("local_id"))
            text = str(row.get("text") or "").strip()
            if text:
                rows_out.append({
                    "create_time": ts,
                    "local_id": lid,
                    "sender_id": _sql_int(row.get("sender_id")),
                    "text": text,
                })
        last = batch[-1]
        next_ts = _sql_int(last.get("create_time"))
        next_id = _sql_int(last.get("local_id"))
        if (next_ts, next_id) <= (cursor_ts, cursor_id):
            raise RuntimeError(f"FTS 游标未前进: {table} {(next_ts, next_id)}")
        cursor_ts, cursor_id = next_ts, next_id
        print(f"  {table}: {len(rows_out)} 条", flush=True)
        if len(batch) < batch_size:
            break
    return rows_out


def load_conversation(username, label="KI"):
    session_id = recv._fts_rowid(username)
    self_wxid = recv.get_self_wxid()
    self_id = recv._fts_rowid(self_wxid) if self_wxid else None
    if not session_id or not self_id:
        raise RuntimeError("无法确定会话 ID 或本人 ID，不能可靠区分双方说话人")

    opened = recv.get_opened_dbs(ttl=0)
    if "message_fts.db" not in opened:
        raise RuntimeError("message_fts.db 尚未打开")

    all_rows = []
    for table in FTS_TABLES:
        try:
            all_rows.extend(fetch_table_rows(
                "message_fts.db", table, session_id))
        except Exception as exc:
            # 某些版本可能没有全部 4 张分表；不存在时跳过，其他错误继续抛出。
            if "no such table" in str(exc).lower():
                continue
            raise

    seen = set()
    clean = []
    for row in sorted(
            all_rows,
            key=lambda x: (x["create_time"], x["local_id"], x["sender_id"])):
        signature = (row["create_time"], row["local_id"], row["sender_id"])
        if signature in seen:
            continue
        seen.add(signature)
        sender = row["sender_id"]
        if sender == self_id:
            role = "我"
        elif sender == session_id:
            role = label
        else:
            # 私聊中的异常系统行不用于人格归因。
            continue
        clean.append({**row, "role": role})
    if not clean:
        raise RuntimeError("没有读取到可分析的双方文本")
    return clean


def conversation_stats(rows, label):
    mine = [x for x in rows if x["role"] == "我"]
    theirs = [x for x in rows if x["role"] == label]
    return {
        "total_text_messages": len(rows),
        "my_messages": len(mine),
        "other_messages": len(theirs),
        "first_time": dt.datetime.fromtimestamp(rows[0]["create_time"]).isoformat(sep=" "),
        "last_time": dt.datetime.fromtimestamp(rows[-1]["create_time"]).isoformat(sep=" "),
        "my_text_chars": sum(len(x["text"]) for x in mine),
        "other_text_chars": sum(len(x["text"]) for x in theirs),
    }


def make_chunks(rows, max_chars=22000, max_messages=900):
    """按自然月份优先切块；超长月份再按字符数切分。"""
    chunks = []
    current = []
    chars = 0
    month = None
    for row in rows:
        stamp = dt.datetime.fromtimestamp(row["create_time"])
        row_month = stamp.strftime("%Y-%m")
        line = f"[{stamp:%Y-%m-%d %H:%M}] {row['role']}：{row['text']}"
        # 月份变化时，如果已有足够内容则自然收束；极短月份可以和下月合并。
        month_break = month and row_month != month and chars >= max_chars * 0.45
        size_break = current and (
            chars + len(line) > max_chars or len(current) >= max_messages)
        if month_break or size_break:
            chunks.append(current)
            current = []
            chars = 0
        current.append((row, line))
        chars += len(line) + 1
        month = row_month
    if current:
        chunks.append(current)
    return chunks


def coalesce_rows(rows, max_gap_seconds=300, max_group_chars=800):
    """合并短时间内同一人连续气泡；不删原文，只降低重复标记开销。"""
    grouped = []
    for row in rows:
        if grouped:
            prev = grouped[-1]
            same_turn = (
                prev["role"] == row["role"]
                and row["create_time"] - prev["_end_time"] <= max_gap_seconds
                and len(prev["text"]) + len(row["text"]) + 1 <= max_group_chars
            )
            if same_turn:
                prev["text"] += "｜" + row["text"]
                prev["_end_time"] = row["create_time"]
                prev["_message_count"] += 1
                continue
        grouped.append({
            **row,
            "_end_time": row["create_time"],
            "_message_count": 1,
        })
    return grouped


CHUNK_SYSTEM = """你是一名谨慎、重证据的人格与沟通模式分析师。
你只分析对话中标为“我”的说话者；另一方的话只能作为互动背景，绝不能把对方的性格或经历算到“我”身上。
禁止临床诊断，禁止仅凭一次情绪下结论，禁止把推测写成事实。注意反讽、玩笑、复述和情境差异。
输出中文阶段观察，必须包含：
1. 稳定倾向与证据强度；2. 沟通表达；3. 情绪触发与调节；4. 价值、需求与边界；
5. 与 KI 的互动模式；6. 决策/执行风格；7. 变化迹象；8. 反例和不确定性。
不要逐条复述聊天，不要泄露私密原句；可用概括性行为证据。控制在 900 个汉字以内。"""


def summarize_chunk(chunk, index, total, label, model):
    first = dt.datetime.fromtimestamp(chunk[0][0]["create_time"])
    last = dt.datetime.fromtimestamp(chunk[-1][0]["create_time"])
    transcript = "\n".join(line for _, line in chunk)
    prompt = (
        f"这是完整会话的第 {index}/{total} 个时间块，范围 {first:%Y-%m-%d} 至 "
        f"{last:%Y-%m-%d}，对方标为“{label}”。\n"
        f"本块共有 {sum(int(x[0].get('_message_count') or 1) for x in chunk)} 条原始文本。"
        "请按系统要求分析“我”，同时判断本块是否只是特殊情境。\n\n"
        + transcript
    )
    return ollama_chat([
        {"role": "system", "content": CHUNK_SYSTEM},
        {"role": "user", "content": prompt},
    ], model=model)


REDUCE_SYSTEM = """你负责合并多份阶段人物观察。只能保留各摘要中有依据的内容，合并重复项，明确跨时期稳定性、变化、反例和置信度。不得创造原摘要不存在的事实，不得临床诊断，不得泄露私密原句。分析对象始终是“我”，不是 KI。"""


def _group_texts(items, max_chars=24000):
    groups, current, size = [], [], 0
    for item in items:
        if current and size + len(item) > max_chars:
            groups.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item) + 40
    if current:
        groups.append(current)
    return groups


def reduce_summaries(summaries, model, checkpoint_dir):
    checkpoint_dir = Path(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    level = 1
    current = list(summaries)
    while len(current) > 1:
        groups = _group_texts(current)
        next_level = []
        for i, group in enumerate(groups, 1):
            path = checkpoint_dir / f"merge_l{level}_{i:03d}.md"
            cached = path.read_text(encoding="utf-8") if path.exists() else ""
            if cached.strip():
                merged = cached.strip()
            else:
                body = "\n\n".join(
                    f"### 阶段观察 {j + 1}\n{x}" for j, x in enumerate(group))
                merged = ollama_chat([
                    {"role": "system", "content": REDUCE_SYSTEM},
                    {"role": "user", "content": "合并以下阶段观察：\n\n" + body},
                ], model=model)
                path.write_text(merged + "\n", encoding="utf-8")
            next_level.append(merged)
            print(f"  合并层 {level}: {i}/{len(groups)}", flush=True)
        if len(next_level) == len(current) and len(current) > 1:
            # 每项本身过长导致无法合组时，强制两两合并。
            forced = []
            for i in range(0, len(current), 2):
                forced.append("\n\n".join(current[i:i + 2]))
            current = forced
        else:
            current = next_level
        level += 1
    return current[0]


FINAL_SYSTEM = """你是一名严谨的人格画像撰写者。根据覆盖三年、数万条文本的分层观察撰写最终中文报告。报告不是诊断或永久标签；要区分稳定特征、情境反应、关系特有模式和随时间变化。所有判断必须带置信度（高/中/低）并纳入反例，不引用可识别的私密原句。"""


def final_report(merged, stats, label, model):
    prompt = f"""数据统计：
{json.dumps(stats, ensure_ascii=False, indent=2)}
对话对象：{label}

分层合并观察：
{merged}

请形成完整报告，结构为：
# 人物性格画像
## 结论摘要
## 核心性格维度（每项含置信度、支持证据类型、反例）
## 沟通与表达方式
## 情绪模式：触发、外显、恢复与调节
## 价值观、核心需求与边界
## 决策和执行风格
## 与 KI 互动中呈现的关系模式（注明哪些可能仅限这段关系）
## 2023—2026 的变化趋势
## 优势、潜在盲点与压力下风险
## 哪些结论仍不能确定
## 给本人使用这份画像的建议

语气直接、细腻但不讨好，避免星座式空话。"""
    return ollama_chat([
        {"role": "system", "content": FINAL_SYSTEM},
        {"role": "user", "content": prompt},
    ], model=model, temperature=0.15)


def run(username, label, model, chunk_model, max_chars, force=False):
    verify_ollama(model)
    verify_ollama(chunk_model)
    token = hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]
    out_dir = ROOT / "personality_reports" / token
    chunks_dir = out_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    print("读取全量文本索引…", flush=True)
    rows = load_conversation(username, label=label)
    stats = conversation_stats(rows, label)
    grouped_rows = coalesce_rows(rows)
    chunks = make_chunks(grouped_rows, max_chars=max_chars)
    snapshot = {
        **stats,
        "username_hash": token,
        "label": label,
        "model": model,
        "chunk_model": chunk_model,
        "coalesced_turns": len(grouped_rows),
        "chunk_count": len(chunks),
        "generated_at": dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    _write_json(out_dir / "snapshot.json", snapshot)
    print(f"共 {len(rows)} 条文本，切为 {len(chunks)} 块", flush=True)

    summaries = []
    for i, chunk in enumerate(chunks, 1):
        path = chunks_dir / f"chunk_{i:04d}.md"
        if path.exists() and path.stat().st_size > 30 and not force:
            summary = path.read_text(encoding="utf-8").strip()
        else:
            print(f"分析 {i}/{len(chunks)}…", flush=True)
            summary = summarize_chunk(chunk, i, len(chunks), label, chunk_model)
            path.write_text(summary + "\n", encoding="utf-8")
        summaries.append(summary)

    print("分层合并阶段观察…", flush=True)
    merged = reduce_summaries(summaries, model, out_dir / "merges")
    (out_dir / "merged_observations.md").write_text(
        merged + "\n", encoding="utf-8")
    report = final_report(merged, stats, label, model)
    report_path = out_dir / "personality_report.md"
    header = (
        f"<!-- 本地 Ollama 分层分析；原文未写入本目录。\n"
        f"覆盖 {stats['first_time']} 至 {stats['last_time']}，"
        f"共 {stats['total_text_messages']} 条文本。 -->\n\n"
    )
    report_path.write_text(header + report.strip() + "\n", encoding="utf-8")
    print(f"完成: {report_path}", flush=True)
    return report_path


def main():
    parser = argparse.ArgumentParser(description="微信全量聊天人物性格分层分析")
    parser.add_argument("--username", required=True, help="会话 username/wxid")
    parser.add_argument("--label", default="KI", help="报告中的对方称呼")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="本机 Ollama 模型")
    parser.add_argument("--chunk-model", default=DEFAULT_CHUNK_MODEL,
                        help="原文阶段分析使用的本机 Ollama 模型")
    parser.add_argument("--max-chars", type=int, default=18000,
                        help="每个原文块最大字符数")
    parser.add_argument("--force", action="store_true", help="忽略阶段摘要缓存")
    args = parser.parse_args()
    run(args.username, args.label, args.model, args.chunk_model,
        args.max_chars, force=args.force)


if __name__ == "__main__":
    main()
