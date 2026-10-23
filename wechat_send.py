# -*- coding: utf-8 -*-
"""微信消息发送：文本 / 图片（经 Hook HTTP，串行限速）。"""

import os

import wechat_recv as rc

SEND_TEXT_URL = rc.BASE + "/SendTextMsg"
SEND_IMG_URL = rc.BASE + "/SendImgMsg"


def _valid_wxid(wxid):
    if not wxid or len(wxid) > 128:
        return False
    return True


def send_text(username, msg, timeout=15):
    """发送文本，成功返回 True。"""
    if not _valid_wxid(username):
        raise ValueError(f"invalid wxid: {username!r}")
    if not msg or len(msg) > 6000:
        raise ValueError("empty or too long message")
    r = rc.hook_post(
        SEND_TEXT_URL,
        {"wxidorgid": username, "msg": msg},
        timeout=timeout,
        send_action=True,
    )
    return r.get("ret") == 0


def send_image(username, image_path, timeout=30):
    """发送本地图片（jpg/png 等），成功返回 True。"""
    if not _valid_wxid(username):
        raise ValueError(f"invalid wxid: {username!r}")
    path = os.path.abspath(image_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    r = rc.hook_post(
        SEND_IMG_URL,
        {"wxidorgid": username, "path": path},
        timeout=timeout,
        send_action=True,
    )
    return r.get("ret") == 0


def decode_image(src_dat, dst_jpg, timeout=30):
    """已禁用 Hook 解密（会崩微信）。请用 wechat_media.decode_to_jpeg。"""
    raise RuntimeError(
        "Hook Decode_Pic 已禁用。请用 wechat_media.decode_to_jpeg 或 dat_decrypt.py")
