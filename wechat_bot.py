# -*- coding: utf-8 -*-
"""WeChat + Ollama auto-reply bot."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import requests

from wechat_controller import (
    FILE_HELPER,
    check_uia_ready,
    send_message,
    send_message_keyboard,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434/api/generate",
    "model": "qwen2.5:1.5b",
    "listen_contacts": [FILE_HELPER],
    "system_prompt": "你是微信上的AI助手，请用简洁友好的中文回复，控制在100字以内。",
    "poll_interval": 2,
    "reply_prefix": "",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        merged.update(data)
        return merged
    return DEFAULT_CONFIG.copy()


def ask_ollama(cfg: dict, user_text: str) -> str:
    prompt = (
        f"{cfg['system_prompt']}\n\n"
        f"用户说：{user_text}\n\n"
        "请回复："
    )
    payload = {
        "model": cfg["model"],
        "prompt": prompt,
        "stream": False,
    }
    resp = requests.post(cfg["ollama_url"], json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def run_uia_listener(cfg: dict) -> None:
    from wxauto4 import WeChat

    wx = WeChat(ads=False)
    replied: set[str] = set()
    contacts = cfg["listen_contacts"]

    logger.info("UIA listener started for: %s", ", ".join(contacts))

    for contact in contacts:
        def make_callback(name: str):
            def on_message(msg, chat):
                try:
                    if getattr(msg, "type", "") not in ("friend", "text", ""):
                        return
                    content = (getattr(msg, "content", "") or "").strip()
                    sender = getattr(msg, "sender", "") or getattr(msg, "sender_remark", "")
                    if not content or sender == "Self":
                        return
                    key = f"{name}:{content}:{getattr(msg, 'time', '')}"
                    if key in replied:
                        return
                    replied.add(key)
                    logger.info("[%s] %s: %s", name, sender, content[:80])
                    reply = ask_ollama(cfg, content)
                    if cfg.get("reply_prefix"):
                        reply = cfg["reply_prefix"] + reply
                    chat.SendMsg(reply)
                    logger.info("Replied: %s", reply[:80])
                except Exception as exc:
                    logger.error("Callback error: %s", exc)
            return on_message

        wx.AddListenChat(contact, make_callback(contact))

    wx.KeepRunning()


def run_keyboard_demo(cfg: dict) -> None:
    """Fallback mode: send a test message, then poll manually via CLI."""
    contact = cfg["listen_contacts"][0]
    test_reply = ask_ollama(cfg, "你好，请做一句自我介绍")
    full_reply = cfg.get("reply_prefix", "") + test_reply
    logger.info("Ollama reply: %s", full_reply)
    ok = send_message_keyboard(contact, full_reply)
    if ok:
        logger.info("Test message sent to [%s]. Keyboard send works.", contact)
    else:
        logger.error("Failed to send test message.")


def main() -> int:
    cfg = load_config()
    status = check_uia_ready()

    print("=" * 50)
    print("  WeChat + Ollama Bot")
    print("=" * 50)
    print(f"  WeChat window : {'OK' if status.hwnd else 'NOT FOUND'}")
    print(f"  Window class  : {status.class_name or 'N/A'}")
    print(f"  UIA ready     : {'YES' if status.uia_ready else 'NO'}")
    print(f"  Ollama model  : {cfg['model']}")
    print(f"  Listen        : {', '.join(cfg['listen_contacts'])}")
    print("=" * 50)

    if not status.hwnd:
        print("\nError: WeChat is not running or not logged in.")
        return 1

    try:
        requests.get("http://localhost:11434/api/tags", timeout=5).raise_for_status()
    except Exception:
        print("\nError: Ollama is not running. Start Ollama first.")
        return 1

    if status.uia_ready:
        print("\nStarting auto-reply listener (Ctrl+C to stop)...\n")
        run_uia_listener(cfg)
        return 0

    print(f"\nNote: {status.message}")
    print("\nTo enable auto-reply (message listening):")
    print("  1. Press Win key, search 'Narrator' / 讲述人, turn it ON")
    print("  2. You can turn Narrator OFF immediately after")
    print("  3. Fully quit WeChat and login again")
    print("  4. Re-run this script")
    print("\nRunning keyboard send test instead...\n")
    run_keyboard_demo(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
