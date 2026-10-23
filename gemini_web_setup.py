# -*- coding: utf-8 -*-
"""Gemini 网页 Cookie 自动检测与连通性测试。

首次授权方式（无需管理员）：
  1. 双击运行 chrome_gemini_debug.bat（会用调试模式启动 Chrome）
  2. 确认 gemini.google.com 已登录
  3. 本脚本: python gemini_web_setup.py

授权成功后程序会用请求自动续期，无需保持调试 Chrome 窗口。
"""
import os
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE = os.path.join(DIR, "gemini_cookies.json")


def _test_auth(psid, psidts, label, cfg):
    import asyncio
    from gemini_webapi.constants import AccountStatus

    async def _run():
        c = gw._make_gemini_client(psid, psidts, cfg)
        await c.init(timeout=45, auto_close=True, auto_refresh=False)
        return c.account_status, c

    status, client = asyncio.run(_run())
    if status == AccountStatus.UNAUTHENTICATED:
        gw._clear_webapi_cookie_cache(psid)
        psid2, psidts2, src2, jar2 = gw._load_cookies_via_cdp(cfg)
        if psid2:
            cfg["_cdp_cookie_jar"] = jar2
            async def _retry():
                c = gw._make_gemini_client(psid2, psidts2 or psidts, cfg, jar2)
                await c.init(timeout=45, auto_close=True, auto_refresh=False)
                return c.account_status, c
            status, client = asyncio.run(_retry())
    if status == AccountStatus.UNAUTHENTICATED:
        print(f"  {label}: 失败（API 认证未通过，已清除 webapi 旧缓存）")
        return False, None
    print(f"  {label}: OK ({status.name})")
    return True, client


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("== Gemini 网页 Cookie 检测 ==\n")

    import gemini_web_client as gw

    cfg = {
        "cookie_file": COOKIE,
        "cookie_source": "auto",
        "cdp_port": 9222,
    }

    print("【来源探测】")
    for name, ok, detail in gw.diagnose_cookie_sources(cfg):
        mark = "OK" if ok else "×"
        print(f"  [{mark}] {name}: {detail}")

    psid, psidts, src = gw._resolve_cookies(cfg)
    print(f"\n最终选用: {src}")
    if psid:
        print(f"  __Secure-1PSID: ...{psid[-8:]}")
        if psidts:
            print(f"  __Secure-1PSIDTS: ...{psidts[-8:]}")
        else:
            print("  __Secure-1PSIDTS: 缺失（会导致认证失败）")
        jar = cfg.get("_cdp_cookie_jar") or {}
        if jar:
            print(f"  CDP 附带 Cookie 数: {len(jar)}")
    elif gw._cdp_port_open(cfg.get("cdp_port") or 9222):
        print("  9222 端口正常，但尚未登录 Google")
        print("\n【还差一步】请在 bat 打开的那个 Chrome 里：")
        print("  1. 访问 https://gemini.google.com")
        print("  2. 用你的 Google 账号登录（独立配置，首次必须登录一次）")
        print("  3. 确认网页里能正常聊天/生图")
        print("  4. 再运行: python gemini_web_setup.py")
        return 1
    else:
        print("  未获取到有效 Cookie")
        if gw._chrome_running() and not gw._cdp_port_open(cfg.get("cdp_port") or 9222):
            print("\n【检测到问题】Chrome 在运行，但未开调试端口 9222。")
            print("  请用 chrome_gemini_debug.bat 启动，不要用桌面图标。")
        print("\n【请按顺序操作】")
        print("  1. 双击 chrome_gemini_debug.bat")
        print("  2. 看到 Port 9222 ready 后，在打开的 Chrome 登录 gemini.google.com")
        print("  3. 再运行本脚本")
        print("\n备选：F12 手动复制 Cookie 到 gemini_cookies.json，设 cookie_source=file")
        return 1

    print("\n【测试登录态】")
    ok, client = _test_auth(psid, psidts, src, cfg)
    if not ok:
        gw._invalidate_cookie_file(cfg)
        print("\n网页已登录但 API 认证失败（常见原因：gemini-webapi 旧缓存或 1PSIDTS 未刷新）。")
        print("请在该 Chrome 窗口（chrome_gemini_debug.bat 打开的）:")
        print("  1. 打开 https://gemini.google.com")
        print("  2. 随便发一条消息（刷新 1PSIDTS）")
        print("  3. 再运行: python gemini_web_setup.py")
        return 1

    try:
        import asyncio
        if client:
            async def _snap():
                gw._persist_cookies(client, cfg)
                await client.close()
            asyncio.run(_snap())
        else:
            gw.persist_cookies_to_file(psid, psidts, cfg)
        print(f"\n已同步最新 Cookie 到 {COOKIE}")
    except Exception as e:
        print(f"\n同步到文件失败（不影响使用）: {e}")

    print("\n生图配置已就绪（cookie_source=auto, cdp_port=9222）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
