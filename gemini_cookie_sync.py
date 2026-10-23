# -*- coding: utf-8 -*-
"""Gemini Cookie 自动续期：优先用请求轮换登录态，失效时回退 CDP。

用法:
  python gemini_cookie_sync.py              # 请求续期一次
  python gemini_cookie_sync.py --watch 600  # 每 10 分钟自动续期
  python gemini_cookie_sync.py --cdp-only   # 强制从调试 Chrome 重新取 Cookie

首次授权或 Google 强制重新登录时，仍需运行 chrome_gemini_debug.bat。
"""
import argparse
import json
import os
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(DIR, "autoreply_config.json")


def _load_image_gen_cfg():
    cfg = {}
    if os.path.isfile(CONFIG):
        try:
            raw = json.load(open(CONFIG, encoding="utf-8"))
            cfg = raw.get("image_gen") or {}
        except Exception:
            pass
    cfg.setdefault("cookie_file", os.path.join(DIR, "gemini_cookies.json"))
    cfg.setdefault("cookie_source", "auto")
    cfg.setdefault("cdp_port", 9222)
    return cfg


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Gemini Cookie 请求式自动续期")
    ap.add_argument("--verify", action="store_true",
                    help="CDP-only 模式下同步后验证登录态")
    ap.add_argument("--cdp-only", action="store_true",
                    help="跳过请求续期，强制从调试 Chrome 获取")
    ap.add_argument("--watch", type=int, default=0,
                    help="定时同步间隔（秒），0=只跑一次")
    args = ap.parse_args()

    import gemini_web_client as gw
    cfg = _load_image_gen_cfg()

    print("== Gemini Cookie 自动续期 ==")
    print("模式: " + ("仅 CDP" if args.cdp_only else "请求优先，CDP 兜底"))
    print(f"写入文件: {cfg.get('cookie_file')}")
    if args.watch > 0:
        print(f"定时模式: 每 {args.watch} 秒续期一次 (Ctrl+C 退出)")
    print()

    while True:
        ts = time.strftime("%H:%M:%S")
        if args.cdp_only:
            r = gw.sync_cookies(cfg, verify=args.verify)
        else:
            r = gw.refresh_cookies(cfg, fallback_to_cdp=True)
        if r.get("ok"):
            print(f"[{ts}] OK  {r.get('source')}  {r.get('message')}")
        else:
            print(f"[{ts}] FAIL  {r.get('message')}")
        if not args.watch:
            return 0 if r.get("ok") else 1
        time.sleep(max(30, args.watch))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止")
        raise SystemExit(0)
