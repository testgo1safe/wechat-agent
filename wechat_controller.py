# -*- coding: utf-8 -*-
"""WeChat PC client controller for Weixin 4.x (keyboard + optional UIA)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pyperclip
import win32con
import win32gui

logger = logging.getLogger(__name__)

WECHAT_TITLE = "微信"
FILE_HELPER = "文件传输助手"


@dataclass
class WeChatStatus:
    hwnd: int | None
    class_name: str | None
    uia_ready: bool
    message: str


def find_wechat_hwnd() -> int | None:
    found: list[int] = []

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == WECHAT_TITLE:
            found.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return found[0] if found else None


def activate_wechat() -> int | None:
    hwnd = find_wechat_hwnd()
    if not hwnd:
        return None
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Windows may block foreground switch from background scripts.
        try:
            import pyautogui

            rect = win32gui.GetWindowRect(hwnd)
            x = rect[0] + (rect[2] - rect[0]) // 2
            y = rect[1] + 20
            pyautogui.click(x, y)
        except Exception as exc:
            logger.warning("Could not focus WeChat window: %s", exc)
    time.sleep(0.5)
    return hwnd


def check_uia_ready() -> WeChatStatus:
    hwnd = find_wechat_hwnd()
    if not hwnd:
        return WeChatStatus(None, None, False, "WeChat window not found. Please login first.")

    class_name = win32gui.GetClassName(hwnd)
    if class_name != "mmui::MainWindow":
        return WeChatStatus(
            hwnd,
            class_name,
            False,
            "UIA not available (Qt rendering mode). Enable Windows Narrator once, restart WeChat, then retry.",
        )

    try:
        from wxauto4.uia import uiautomation as auto

        wx = auto.WindowControl(searchDepth=1, Name=WECHAT_TITLE, ClassName="mmui::MainWindow")
        if wx.Exists(0) and len(wx.GetChildren()) > 0:
            return WeChatStatus(hwnd, class_name, True, "UIA ready.")
    except Exception as exc:
        logger.debug("UIA check failed: %s", exc)

    return WeChatStatus(
        hwnd,
        class_name,
        False,
        "WeChat found but UIA tree is empty. Enable Windows Narrator once and restart WeChat.",
    )


def _paste_text(text: str) -> None:
    pyperclip.copy(text)
    time.sleep(0.15)
    import pyautogui

    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)


def send_message_keyboard(contact: str, message: str) -> bool:
    """Send a text message using keyboard shortcuts (works without UIA)."""
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.08

    hwnd = activate_wechat()
    if not hwnd:
        logger.error("Cannot find WeChat window.")
        return False

    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)

    _paste_text(contact)
    time.sleep(0.8)
    pyautogui.press("enter")
    time.sleep(0.6)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    logger.info("Sent message to [%s]", contact)
    return True


def send_message_uia(contact: str, message: str) -> bool:
    """Send via wxauto4 when UIA is available."""
    try:
        from wxauto4 import WeChat

        wx = WeChat(ads=False)
        result = wx.SendMsg(message, who=contact)
        return bool(result)
    except Exception as exc:
        logger.error("UIA send failed: %s", exc)
        return False


def send_message(contact: str, message: str) -> bool:
    status = check_uia_ready()
    if status.uia_ready:
        if send_message_uia(contact, message):
            return True
        logger.warning("UIA send failed, falling back to keyboard mode.")
    return send_message_keyboard(contact, message)
