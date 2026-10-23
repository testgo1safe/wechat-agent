# -*- coding: utf-8 -*-
"""通过 WeChat-Hook 的只读数据库接口查询联系人，拿到 wxid。

用法：
  python wechat_contacts.py                # 列出普通/企业微信好友，排除系统号
  python wechat_contacts.py --all          # 列出全部联系人(含公众号等)
  python wechat_contacts.py -s 张三         # 按昵称/备注/微信号/wxid 搜索
  python wechat_contacts.py --rooms        # 列出群聊(chat_room)
  python wechat_contacts.py --resolve 张三  # 只输出匹配到的第一个 wxid(给发送用)

依赖：微信已侧载 version.dll 且 30001 端口在监听。
"""

import argparse
import json
import sys
import urllib.request

HOOK = "http://127.0.0.1:30001/QueryDB/execute"

# 常见系统/服务账号，默认从“好友”里过滤掉
SYSTEM_ACCOUNTS = {
    "weixin", "fmessage", "medianote", "filehelper", "floatbottle",
    "qmessage", "qqmail", "tmessage", "newsapp", "notifymessage",
    "weibo", "brandsessionholder", "gh_", "opencustomerservicemsg",
}

FRIEND_CONTACT_WHERE = (
    "(local_type=1 OR (local_type=5 AND username LIKE '%@openim'))"
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def q(db, sql):
    body = json.dumps({"optDbName": db, "SQL": sql}).encode("utf-8")
    req = urllib.request.Request(
        HOOK, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if obj.get("status", 0) != 0:
        raise RuntimeError(obj.get("desc") or obj)
    return obj.get("data", [])


def esc(s):
    return s.replace("'", "''")


def is_system(username):
    if username in SYSTEM_ACCOUNTS:
        return True
    return username.startswith("gh_")  # 公众号


def fetch_contacts(where):
    sql = ("SELECT username, alias, remark, nick_name, local_type "
           "FROM contact " + where + " ORDER BY local_type, remark, nick_name")
    return q("contact.db", sql)


def name_of(c):
    return c.get("remark") or c.get("nick_name") or c.get("alias") or c["username"]


def print_rows(rows):
    if not rows:
        print("（无结果）")
        return
    print(f"{'wxid(username)':<28} {'微信号(alias)':<18} {'显示名'}")
    print("-" * 78)
    for c in rows:
        disp = c.get("remark") or c.get("nick_name") or ""
        print(f"{c['username']:<28} {c.get('alias',''):<18} {disp}")
    print(f"\n共 {len(rows)} 个")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--search", help="按昵称/备注/微信号/wxid 搜索")
    ap.add_argument("--all", action="store_true", help="包含公众号等全部联系人")
    ap.add_argument("--rooms", action="store_true", help="列出群聊")
    ap.add_argument("--resolve", help="输出匹配到的第一个 wxid（脚本用）")
    args = ap.parse_args()

    try:
        if args.rooms:
            rows = q("contact.db",
                     "SELECT c.username, c.nick_name, c.remark "
                     "FROM chat_room r JOIN contact c ON c.username=r.username "
                     "ORDER BY c.nick_name LIMIT 200")
            if not rows:
                rows = q("contact.db",
                         "SELECT username, nick_name, remark FROM contact "
                         "WHERE username LIKE '%@chatroom' ORDER BY nick_name LIMIT 200")
            if not rows:
                print("（无群聊记录）")
                return 0
            for c in rows:
                print(f"{c['username']:<40} {c.get('remark') or c.get('nick_name','')}")
            print(f"\n共 {len(rows)} 个群")
            return 0

        kw = args.search or args.resolve
        if kw:
            k = esc(kw)
            where = (f"WHERE (remark LIKE '%{k}%' OR nick_name LIKE '%{k}%' "
                     f"OR alias LIKE '%{k}%' OR username LIKE '%{k}%')")
            rows = fetch_contacts(where)
            if args.resolve:
                rows = [c for c in rows if not is_system(c["username"])] or rows
                if rows:
                    print(rows[0]["username"])
                    return 0
                print("", end="")
                return 1
            print_rows(rows)
            return 0

        # 默认：好友
        rows = fetch_contacts(f"WHERE {FRIEND_CONTACT_WHERE}")
        if not args.all:
            rows = [c for c in rows if not is_system(c["username"])]
        print_rows(rows)
        return 0
    except Exception as e:
        print(f"[x] 查询失败: {e}", file=sys.stderr)
        print("    确认微信已启动并登录，且 30001 端口在监听。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
