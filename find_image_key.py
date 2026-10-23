# -*- coding: utf-8 -*-
"""从微信进程内存提取 V2 .dat 图片 AES 密钥，写入 autoreply_config.json。"""

import json
import os
import sys

import dat_decrypt as dd
import wechat_media as media

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoreply_config.json")


def attach_dirs():
    dirs = []
    for acct in media._account_dirs():
        p = os.path.join(acct, "msg", "attach")
        if os.path.isdir(p):
            dirs.append(p)
    return dirs


def load_cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cfg(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    dirs = attach_dirs()
    print("attach:", dirs or "(empty)")
    if not dirs:
        print("[X] 找不到 msg/attach 目录")
        return 1

    sample = dd.find_v2_sample(dirs)
    if not sample:
        print("[X] 无 V2 .dat 样本；请先在微信里点开 2-3 张图片")
        return 1
    path, ct = sample
    print("样本:", path)
    print("密文块: 已读取")

    xor = dd.find_xor_key_from_attach(dirs[0])
    print("XOR key:", f"0x{xor:02x}" if xor is not None else "unknown")

    cfg = load_cfg()
    block = cfg.setdefault("image_decrypt", {})
    old = block.get("image_aes_key")
    if old:
        if dd.try_aes_key(old.encode("ascii")[:16], ct):
            print("[OK] 配置中已有有效 AES key")
            return 0
        print("[!] 配置中的 key 无效，重新扫描…")

    pids = dd.find_weixin_pids()
    print("Weixin PIDs:", pids)
    if not pids:
        print("[X] 微信未运行")
        return 1

    aes = None
    for pid in pids:
        print(f"扫描 PID {pid} …")
        aes = dd.scan_memory_for_aes_key(pid, ct)
        if aes:
            break

    if not aes:
        if xor is not None:
            block["image_xor_key"] = xor
            save_cfg(cfg)
            print(f"[!] 已保存 XOR key 0x{xor:02x}，AES key 仍需查看大图后重试")
        print("[X] 未找到 AES key；请在微信里查看大图后立即重试")
        return 1

    block["image_aes_key"] = aes
    if xor is not None:
        block["image_xor_key"] = xor
    save_cfg(cfg)
    print("[OK] 已保存 image_aes_key（内容已隐藏）")
    if xor is not None:
        print("[OK] image_xor_key =", f"0x{xor:02x}")

    out, fmt = dd.decrypt_dat_file(path, aes_key=aes, xor_key=xor or 0x88)
    print("[OK] 验证解密 ->", out, fmt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
