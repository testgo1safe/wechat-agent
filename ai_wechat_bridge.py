# -*- coding: utf-8 -*-
"""本地大模型 -> 微信 发送桥接。

链路：
  你的输入/提示词  ->  Ollama(本地 qwen2.5:1.5b) 生成  ->  WeChat-Hook 发送

注意：当前 hook 仅实现「发送」，未实现「接收」。因此这里提供：
  - 交互模式：命令行输入内容，AI 生成后发到指定微信对象
  - 单次模式：命令行参数直接发送

依赖：本机 Ollama 正在运行；微信已侧载 version.dll 且 30001 端口在监听。
"""

import argparse
import json
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
HOOK_URL = "http://127.0.0.1:30001/SendTextMsg"
QUERY_URL = "http://127.0.0.1:30001/QueryDB/execute"
MODEL = "qwen2.5:1.5b"

SYSTEM_PROMPT = (
    "你是一个通过微信发消息的助手。回答要简洁、口语化，"
    "适合直接作为微信聊天内容发送。不要使用 Markdown 标题或代码块。"
)


def http_post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask_ollama(prompt):
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
    }
    try:
        r = http_post_json(OLLAMA_URL, payload)
        return (r.get("response") or "").strip()
    except Exception as e:
        print(f"[x] Ollama 调用失败: {e}", file=sys.stderr)
        return None


def resolve_wxid(name):
    """按备注/昵称/微信号/wxid 模糊匹配，返回第一个非系统号的 wxid。"""
    k = name.replace("'", "''")
    sql = ("SELECT username, local_type FROM contact WHERE "
           f"(remark LIKE '%{k}%' OR nick_name LIKE '%{k}%' OR "
           f"alias LIKE '%{k}%' OR username LIKE '%{k}%') "
           "ORDER BY local_type LIMIT 1")
    try:
        r = http_post_json(QUERY_URL, {"optDbName": "contact.db", "SQL": sql},
                           timeout=15)
        data = r.get("data") or []
        if data:
            return data[0]["username"]
    except Exception as e:
        print(f"[x] 解析联系人失败: {e}", file=sys.stderr)
    return None


def wechat_send(wxid, msg):
    try:
        r = http_post_json(HOOK_URL, {"wxidorgid": wxid, "msg": msg}, timeout=15)
        return r.get("ret") == 0
    except Exception as e:
        print(f"[x] 微信发送失败(hook 未运行?): {e}", file=sys.stderr)
        return False


def one_shot(wxid, prompt, raw):
    text = prompt if raw else ask_ollama(prompt)
    if not text:
        return 1
    if not raw:
        print(f"[AI] {text}")
    ok = wechat_send(wxid, text)
    print("[ok] 已发送" if ok else "[x] 发送失败")
    return 0 if ok else 1


def interactive(wxid, raw):
    mode = "原文直发" if raw else f"AI({MODEL}) 生成后发送"
    print(f"== 微信 AI 桥接 ==  目标: {wxid}  模式: {mode}")
    print("   输入内容回车发送；/to <wxid> 切换目标；/raw 切换直发；exit 退出")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        if line.startswith("/to "):
            wxid = line[4:].strip()
            print(f"   目标已切换: {wxid}")
            continue
        if line == "/raw":
            raw = not raw
            print(f"   直发模式: {'开' if raw else '关'}")
            continue
        text = line if raw else ask_ollama(line)
        if not text:
            continue
        if not raw:
            print(f"AI> {text}")
        ok = wechat_send(wxid, text)
        print("   [ok] 已发送\n" if ok else "   [x] 发送失败\n")


def main():
    ap = argparse.ArgumentParser(description="本地大模型 -> 微信 发送桥接")
    ap.add_argument("--to", default="filehelper",
                    help="接收方 wxid 或群 id（默认 filehelper 文件传输助手）")
    ap.add_argument("--name",
                    help="按备注/昵称/微信号解析接收方（优先于 --to）")
    ap.add_argument("--msg", help="一次性发送的内容/提示词；省略则进入交互模式")
    ap.add_argument("--raw", action="store_true",
                    help="直接发送原文，不经过大模型")
    args = ap.parse_args()

    to = args.to
    if args.name:
        wxid = resolve_wxid(args.name)
        if not wxid:
            print(f"[x] 未找到联系人: {args.name}", file=sys.stderr)
            return 1
        print(f"[i] {args.name} -> {wxid}")
        to = wxid

    if args.msg is not None:
        return one_shot(to, args.msg, args.raw)
    interactive(to, args.raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
