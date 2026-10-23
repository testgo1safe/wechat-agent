# -*- coding: utf-8 -*-
"""Check WeChat UIA availability."""

from wechat_controller import check_uia_ready, send_message_keyboard, FILE_HELPER

if __name__ == "__main__":
    status = check_uia_ready()
    print("HWND     :", status.hwnd)
    print("Class    :", status.class_name)
    print("UIA ready:", status.uia_ready)
    print("Message  :", status.message)
    if status.hwnd and input("\nSend test to File Helper? (y/n): ").strip().lower() == "y":
        ok = send_message_keyboard(FILE_HELPER, "WeChat bot test OK")
        print("Send result:", ok)
