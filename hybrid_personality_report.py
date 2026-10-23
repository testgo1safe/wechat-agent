# -*- coding: utf-8 -*-
"""全量统计 + 分月代表语义片段的人物画像快速分析。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import full_history_personality as full


ROOT = Path(__file__).resolve().parent

CATEGORY_PATTERNS = {
    "关心亲密": r"想你|爱你|喜欢|抱抱|亲亲|晚安|早安|担心你|照顾|陪你",
    "愤怒挫败": r"生气|气死|烦死|好烦|无语|崩溃|讨厌|不爽|委屈|受不了",
    "焦虑不确定": r"焦虑|害怕|担心|怎么办|不知道|不确定|可能吧|也许|是不是",
    "计划行动": r"计划|打算|准备|决定|安排|先.{0,8}再|明天|以后|我要去|准备去",
    "边界拒绝": r"不要|不想|不行|不能|必须|别这样|拒绝|算了|到此为止",
    "修复道歉": r"对不起|抱歉|我的错|我错了|原谅|和好|别生气|没事的",
    "感谢认可": r"谢谢|感谢|辛苦|真好|厉害|支持你|理解你",
    "幽默缓冲": r"哈哈|笑死|hhh|emmm|嘿嘿|草|绷不住|乐死",
    "需求请求": r"能不能|可以吗|希望|想要|陪我|告诉我|帮我|需要你",
    "工作成就": r"工作|项目|客户|老板|同事|赚钱|工资|学习|考试|目标|完成",
}
COMPILED = {k: re.compile(v, re.I) for k, v in CATEGORY_PATTERNS.items()}


def quantile(values, q):
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def build_features(rows, label):
    mine = [x for x in rows if x["role"] == "我"]
    lengths = [len(x["text"]) for x in mine]
    categories = Counter()
    month_rows = defaultdict(list)
    hours = Counter()
    punctuation = Counter()
    for row in mine:
        stamp = dt.datetime.fromtimestamp(row["create_time"])
        month_rows[stamp.strftime("%Y-%m")].append(row)
        hours[stamp.hour] += 1
        text = row["text"]
        punctuation["问句"] += int("?" in text or "？" in text)
        punctuation["感叹"] += int("!" in text or "！" in text)
        punctuation["省略"] += int("..." in text or "……" in text)
        for name, pattern in COMPILED.items():
            if pattern.search(text):
                categories[name] += 1

    turns = full.coalesce_rows(rows)
    reply_delays = []
    self_initiations = 0
    for i, turn in enumerate(turns):
        if turn["role"] != "我" or i == 0:
            continue
        prev = turns[i - 1]
        gap = turn["create_time"] - prev["_end_time"]
        if prev["role"] == label and 0 <= gap <= 24 * 3600:
            reply_delays.append(gap)
        if gap >= 6 * 3600:
            self_initiations += 1

    monthly = []
    for month in sorted(month_rows):
        xs = month_rows[month]
        month_categories = {
            name: sum(bool(pattern.search(x["text"])) for x in xs)
            for name, pattern in COMPILED.items()
        }
        monthly.append({
            "month": month,
            "my_messages": len(xs),
            "my_chars": sum(len(x["text"]) for x in xs),
            "avg_chars": round(sum(len(x["text"]) for x in xs) / max(1, len(xs)), 2),
            "signals": month_categories,
        })

    n = max(1, len(mine))
    return {
        "说明": "词汇信号只反映表达出现频率，不直接等同于人格；最终分析必须结合语境和反例。",
        "我的文本数": len(mine),
        "平均每条字数": round(statistics.mean(lengths), 2) if lengths else 0,
        "每条字数中位数": round(quantile(lengths, 0.5) or 0, 2),
        "每条字数P90": round(quantile(lengths, 0.9) or 0, 2),
        "表达信号_每千条": {
            k: round(v * 1000 / n, 2) for k, v in categories.items()
        },
        "标点信号_每千条": {
            k: round(v * 1000 / n, 2) for k, v in punctuation.items()
        },
        "活跃时段": {
            "凌晨00-05": sum(hours[h] for h in range(0, 6)),
            "上午06-11": sum(hours[h] for h in range(6, 12)),
            "下午12-17": sum(hours[h] for h in range(12, 18)),
            "晚间18-23": sum(hours[h] for h in range(18, 24)),
        },
        "回复延迟秒": {
            "样本数": len(reply_delays),
            "中位数": round(quantile(reply_delays, 0.5) or 0, 1),
            "P75": round(quantile(reply_delays, 0.75) or 0, 1),
            "P90": round(quantile(reply_delays, 0.9) or 0, 1),
        },
        "间隔六小时后由我开启的对话轮次": self_initiations,
        "分月趋势": monthly,
    }


def semantic_score(row):
    text = row["text"]
    hits = sum(bool(p.search(text)) for p in COMPILED.values())
    punct = int(any(x in text for x in "？！!?"))
    # 长文本更有信息，但封顶，避免全被极长粘贴内容占据。
    return min(len(text), 180) + hits * 60 + punct * 10


def stratified_sample(rows, label, per_month=38):
    """每月按高信息文本 + 时间均匀抽取，并带一条相邻上下文。"""
    by_month = defaultdict(list)
    for idx, row in enumerate(rows):
        if row["role"] == "我":
            month = dt.datetime.fromtimestamp(row["create_time"]).strftime("%Y-%m")
            by_month[month].append((idx, row))

    chosen = set()
    for month, items in sorted(by_month.items()):
        top_n = max(1, int(per_month * 0.65))
        for idx, _ in sorted(items, key=lambda x: semantic_score(x[1]), reverse=True)[:top_n]:
            chosen.add(idx)
        even_n = max(1, per_month - top_n)
        if len(items) <= even_n:
            chosen.update(idx for idx, _ in items)
        else:
            for j in range(even_n):
                pos = round(j * (len(items) - 1) / max(1, even_n - 1))
                chosen.add(items[pos][0])

    with_context = set(chosen)
    for idx in list(chosen):
        if idx > 0:
            with_context.add(idx - 1)
        if idx + 1 < len(rows):
            with_context.add(idx + 1)
    return [rows[i] for i in sorted(with_context)]


def feature_digest(features, model):
    return full.ollama_chat([
        {"role": "system", "content": (
            "你是统计解释助手。把全量聊天量化特征总结成谨慎的中文观察。"
            "词汇命中不是人格本身，必须说明局限；重点解释跨月趋势、表达风格、"
            "主动性、回复节奏和作息。不要诊断，不要编造。")},
        {"role": "user", "content": json.dumps(features, ensure_ascii=False)},
    ], model=model, temperature=0.1)


def run(username, label, chunk_model, final_model, force=False):
    full.verify_ollama(chunk_model)
    full.verify_ollama(final_model)
    token = full.hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]
    out_dir = ROOT / "personality_reports" / token
    stage_dir = out_dir / "hybrid_chunks"
    stage_dir.mkdir(parents=True, exist_ok=True)

    print("读取全量文本…", flush=True)
    rows = full.load_conversation(username, label)
    stats = full.conversation_stats(rows, label)
    print("计算全部文本的量化特征…", flush=True)
    features = build_features(rows, label)
    full._write_json(out_dir / "full_features.json", features)

    sample = stratified_sample(rows, label)
    grouped = full.coalesce_rows(sample, max_gap_seconds=300)
    chunks = full.make_chunks(grouped, max_chars=15000, max_messages=700)
    snapshot = {
        **stats,
        "analysis_mode": "全量量化 + 分月分层语义抽样",
        "semantic_sample_messages": len(sample),
        "semantic_chunks": len(chunks),
        "chunk_model": chunk_model,
        "final_model": final_model,
        "generated_at": dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    full._write_json(out_dir / "hybrid_snapshot.json", snapshot)
    print(f"全量 {len(rows)} 条；语义代表片段 {len(sample)} 条，{len(chunks)} 块", flush=True)

    summaries = []
    for i, chunk in enumerate(chunks, 1):
        path = stage_dir / f"chunk_{i:03d}.md"
        if path.exists() and path.stat().st_size > 30 and not force:
            text = path.read_text(encoding="utf-8").strip()
        else:
            print(f"语义分析 {i}/{len(chunks)}…", flush=True)
            text = full.summarize_chunk(chunk, i, len(chunks), label, chunk_model)
            path.write_text(text + "\n", encoding="utf-8")
        summaries.append(text)

    digest_path = out_dir / "feature_digest.md"
    if digest_path.exists() and not force:
        digest = digest_path.read_text(encoding="utf-8").strip()
    else:
        print("解释全量量化趋势…", flush=True)
        digest = feature_digest(features, chunk_model)
        digest_path.write_text(digest + "\n", encoding="utf-8")

    print("7B 模型合并最终人物画像…", flush=True)
    merged_input = ["## 全量量化观察\n" + digest] + summaries
    merged = full.reduce_summaries(
        merged_input, final_model, out_dir / "hybrid_merges")
    report = full.final_report(
        merged,
        {**stats, "semantic_sample_messages": len(sample),
         "method": "全量量化 + 分月代表性语义分析"},
        label,
        final_model,
    )
    report_path = out_dir / "personality_report.md"
    report_path.write_text(
        "<!-- 原文未写入报告目录；量化统计覆盖全量文本，语义分析按月分层抽样。 -->\n\n"
        + report.strip() + "\n",
        encoding="utf-8",
    )
    print(f"完成: {report_path}", flush=True)
    return report_path


def main():
    p = argparse.ArgumentParser(description="微信全量混合人物画像")
    p.add_argument("--username", required=True)
    p.add_argument("--label", default="KI")
    p.add_argument("--chunk-model", default="qwen2.5:1.5b")
    p.add_argument("--final-model", default="qwen2.5:7b")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    run(a.username, a.label, a.chunk_model, a.final_model, force=a.force)


if __name__ == "__main__":
    main()
