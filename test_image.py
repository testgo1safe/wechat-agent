# -*- coding: utf-8 -*-
"""实测 Hook 发图/收图（Decode_Pic + SendImgMsg）。"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

import wechat_media as media
import wechat_recv as rc
import wechat_send as ws

BASE = "http://127.0.0.1:30001"


def post(path, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def hook_ok():
    try:
        post("/QueryDB/execute", {"optDbName": "session.db", "SQL": "SELECT 1"})
        return True
    except Exception:
        return False


def routes_ok():
    """只检查路由是否存在，不发送假数据（假 SendImgMsg 会崩微信）。"""
    out = {}
    for path in ("/SendImgMsg", "/Decode_Pic"):
        try:
            req = urllib.request.Request(
                BASE + path, data=b"{}", method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                out[path] = r.status
        except urllib.error.HTTPError as e:
            out[path] = e.code
        except Exception as e:
            out[path] = str(e)
    return out


def find_sample_dat():
    hardcoded = (
        r"E:\hh\xwechat_files\<your_wxid>\msg\attach"
        r"\9e20f478899dc29eb19741386f9343c8\2026-07\Img"
        r"\54f9476881e90d7163c6574311dae281.dat"
    )
    if os.path.isfile(hardcoded):
        return "filehelper", {"local_id": 0}, hardcoded

    u = "<your_wxid>"
    info = rc.last_incoming_message(u)
    if info and info.get("kind") == "image":
        dat = media.resolve_dat_path(u, info["local_id"], info.get("raw_content"))
        if dat:
            return u, info, dat
    try:
        rows = rc.query(
            "biz_message_0.db",
            "SELECT m.local_id FROM Msg_8b022bf5f267d4fb3d8edae145c02712 m "
            "WHERE m.local_type=3 ORDER BY m.sort_seq DESC LIMIT 1",
        )
        if rows:
            chat = "20734700269@chatroom"
            lid = int(rows[0]["local_id"])
            dat = media.resolve_dat_path(chat, lid)
            if dat:
                return chat, {"local_id": lid}, dat
    except Exception:
        pass
    return None, None, None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    do_send = "--send" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--send"]

    print("== Hook 图片接口实测 ==")
    print("Hook DB:", hook_ok())
    if not hook_ok():
        print("[X] Hook 未就绪，请先登录微信")
        return 1

    u, info, dat = find_sample_dat()
    print("Sample dat:", dat)
    if dat and os.path.isfile(dat):
        out = os.path.join(tempfile.gettempdir(), "wx_decode_test.jpg")
        try:
            path = media.decode_to_jpeg(dat, u, info.get("local_id") if info else None)
            print("[OK] decode ->", path, "size", os.path.getsize(path))
        except Exception as e:
            print("[X] decode failed:", e)
            print("    勿用 Hook Decode_Pic（会崩溃）；请运行: python find_image_key.py")
    else:
        print("[!] 无 .dat 样本，跳过解密测试")

    sample_jpg = None
    for p in (
        r"C:\Windows\Web\Wallpaper\Theme1\img1.jpg",
        r"C:\Windows\Web\Wallpaper\Spotlight\img14.jpg",
    ):
        if os.path.isfile(p):
            sample_jpg = p
            break
    if not sample_jpg:
        print("[!] 找不到测试 jpg，跳过发图")
        return 0

    if not do_send:
        print("[!] 跳过 SendImgMsg（加 --send 才实测发图，避免误触崩溃）")
        return 0

    to = args[0] if args else "filehelper"
    print("SendImgMsg ->", to, sample_jpg)
    try:
        ok = ws.send_image(to, sample_jpg)
        print("[OK] SendImgMsg" if ok else "[X] SendImgMsg ret!=0")
    except Exception as e:
        print("[X] SendImgMsg failed:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
