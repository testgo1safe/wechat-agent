# -*- coding: utf-8 -*-
"""Gemini 网页端 Cookie 生图桥接（非官方 API，复用 gemini.google.com 登录态）。"""

import asyncio
import json
import os
import tempfile
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_COOKIE = os.path.join(_SCRIPT_DIR, "gemini_cookies.json")
_CDP_URLS = (
    "https://gemini.google.com",
    "https://www.google.com",
    "https://google.com",
)
_WEBAPI_CACHE_DIR = os.path.join(tempfile.gettempdir(), "gemini_webapi")


def _clear_webapi_cookie_cache(psid=None):
    """清除 gemini-webapi 本地 Cookie 缓存，避免旧 1PSIDTS 覆盖 CDP 新值。"""
    if not os.path.isdir(_WEBAPI_CACHE_DIR):
        return 0
    removed = 0
    if psid:
        path = os.path.join(_WEBAPI_CACHE_DIR, f".cached_cookies_{psid}.json")
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        return removed
    import glob
    for path in glob.glob(os.path.join(_WEBAPI_CACHE_DIR, ".cached_cookies_*.json")):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def _load_cookies_from_file(cfg):
    """从 gemini_cookies.json 读取；无文件或缺字段时返回 (None, None)。"""
    cfg = cfg or {}
    psid = (cfg.get("secure_1psid") or "").strip()
    psidts = (cfg.get("secure_1psidts") or "").strip()
    path = (cfg.get("cookie_file") or _DEFAULT_COOKIE).strip()
    if os.path.isfile(path):
        raw = json.load(open(path, encoding="utf-8"))
        if isinstance(raw, dict):
            psid = psid or (raw.get("__Secure-1PSID") or raw.get("Secure_1PSID") or "")
            psidts = psidts or (raw.get("__Secure-1PSIDTS") or raw.get("Secure_1PSIDTS") or "")
        elif isinstance(raw, list):
            for item in raw:
                name = item.get("name") or ""
                val = item.get("value") or ""
                if name == "__Secure-1PSID" and not psid:
                    psid = val
                if name == "__Secure-1PSIDTS" and not psidts:
                    psidts = val
    return (psid or None), (psidts or None)


def _load_cookies(cfg):
    psid, psidts = _load_cookies_from_file(cfg)
    if not psid:
        raise RuntimeError(
            "未配置 Gemini 网页 Cookie。请设置 cookie_source=auto 并保持浏览器已登录 gemini.google.com，"
            "或复制 gemini_cookies.example.json 为 gemini_cookies.json 填入 Cookie。")
    return psid, psidts


def _cdp_port_open(port):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _chrome_running():
    if os.name != "nt":
        return False
    try:
        import subprocess
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "chrome.exe" in (r.stdout or "").lower()
    except Exception:
        return False


def _cdp_collect_google_cookies(cookies):
    """从 CDP Cookie 列表提取全部 Google 域 Cookie（jar 供 gemini-webapi 初始化）。"""
    jar = {}
    for c in cookies:
        dom = (c.get("domain") or "").lower()
        if "google" not in dom:
            continue
        name = c.get("name")
        val = c.get("value")
        if name and val:
            jar[name] = val
    psid = jar.get("__Secure-1PSID")
    psidts = jar.get("__Secure-1PSIDTS")
    return psid, psidts, jar


def _cdp_ws_commands(ws_url, timeout=8):
    import websocket
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        def _cmd(cid, method, params=None):
            msg = {"id": cid, "method": method}
            if params is not None:
                msg["params"] = params
            ws.send(json.dumps(msg))
            while True:
                data = json.loads(ws.recv())
                if data.get("id") == cid:
                    return data

        _cmd(1, "Network.enable")
        resp = _cmd(2, "Network.getCookies", {"urls": list(_CDP_URLS)})
        cookies = (resp.get("result") or {}).get("cookies") or []
        psid, psidts, jar = _cdp_collect_google_cookies(cookies)
        if psid:
            return psid, psidts, jar
        resp = _cmd(3, "Network.getAllCookies")
        cookies = (resp.get("result") or {}).get("cookies") or []
        return _cdp_collect_google_cookies(cookies)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None, None, {}


def _make_gemini_client(psid, psidts, cfg=None, cookie_jar=None):
    """创建 GeminiClient，并清除 webapi 旧缓存以免覆盖 CDP 新 Cookie。"""
    from gemini_webapi import GeminiClient

    cfg = cfg or {}
    _clear_webapi_cookie_cache(psid)
    client = GeminiClient(psid, psidts, proxy=cfg.get("proxy"))
    jar = cookie_jar or cfg.get("_cdp_cookie_jar") or {}
    if jar:
        for name, value in jar.items():
            if name in ("__Secure-1PSID", "__Secure-1PSIDTS") or not value:
                continue
            client.cookies.set(name, value, domain=".google.com")
    return client


def _load_cookies_via_cdp(cfg=None):
    """通过 Chrome 远程调试端口读取 Cookie（Chrome 150+ 需独立 user-data-dir）。"""
    cfg = cfg or {}
    try:
        port = int(cfg.get("cdp_port") or 9222)
    except (TypeError, ValueError):
        port = 9222
    if not _cdp_port_open(port):
        return None, None, "", {}
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
            pages = json.loads(r.read().decode("utf-8"))

        # 优先从 gemini 标签页读取（与网页实际会话一致）
        gemini_pages = []
        other_pages = []
        for page in pages:
            url = (page.get("url") or "").lower()
            if "gemini.google.com" in url:
                gemini_pages.append(page)
            elif "google.com" in url:
                other_pages.append(page)

        for page in gemini_pages + other_pages:
            ws_url = page.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            psid, psidts, jar = _cdp_ws_commands(ws_url)
            if psid:
                cfg["_cdp_cookie_jar"] = jar
                return psid, psidts or None, f"cdp:{port}", jar

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            ver = json.loads(r.read().decode("utf-8"))
        browser_ws = ver.get("webSocketDebuggerUrl")
        if browser_ws:
            psid, psidts, jar = _cdp_ws_commands(browser_ws)
            if psid:
                cfg["_cdp_cookie_jar"] = jar
                return psid, psidts or None, f"cdp:{port}", jar
    except Exception:
        pass
    return None, None, "", {}


def diagnose_cookie_sources(cfg=None):
    """返回各 Cookie 来源探测结果，供 setup 脚本输出诊断信息。"""
    cfg = dict(cfg or {})
    cfg.setdefault("cookie_source", "auto")
    out = []

    try:
        import browser_cookie3 as bc3
        bc3_ok = True
    except ImportError:
        bc3_ok = False
        out.append(("browser-cookie3", False, "未安装 (pip install browser-cookie3)"))

    if bc3_ok:
        for name in ("chrome", "edge"):
            fn = getattr(bc3, name, None)
            if not fn:
                continue
            try:
                jar = fn(domain_name="google.com")
                names = {c.name for c in jar}
                ok = "__Secure-1PSID" in names
                detail = f"psid={'有' if ok else '无'} total={len(names)}"
                out.append((name, ok, detail))
            except Exception as e:
                err = str(e)
                if "RequiresAdmin" in type(e).__name__ or "admin" in err.lower():
                    err = "需要管理员权限（新版 Chrome 加密限制）"
                out.append((name, False, err))

    cdp_psid, _, cdp_src, _ = _load_cookies_via_cdp(cfg)
    port = cfg.get("cdp_port") or 9222
    if cdp_psid:
        out.append(("cdp", True, cdp_src))
    elif _cdp_port_open(port):
        out.append(("cdp", False,
                      "端口已开但未登录 Google，请在 bat 打开的 Chrome 登录 gemini.google.com"))
    elif _chrome_running():
        out.append(("cdp", False,
                      f"Chrome 在运行但未开调试端口！请关闭 Chrome 后运行 chrome_gemini_debug.bat"))
    else:
        out.append(("cdp", False, f"端口 {port} 无响应，请运行 chrome_gemini_debug.bat 启动 Chrome"))

    f_psid, _ = _load_cookies_from_file(cfg)
    out.append(("file", bool(f_psid), cfg.get("cookie_file") or _DEFAULT_COOKIE))
    return out


def _load_browser_cookies_psid(cfg=None):
    """从本机 Chrome/Edge 等浏览器读取 Google 登录 Cookie。"""
    cfg = cfg or {}
    try:
        from gemini_webapi.utils.load_browser_cookies import HAS_BC3, load_browser_cookies
    except ImportError:
        return None, None, ""
    if not HAS_BC3:
        return None, None, ""

    browser_pref = str(cfg.get("browser") or "auto").strip().lower()
    if browser_pref != "auto":
        try:
            import browser_cookie3 as bc3
            fn = getattr(bc3, browser_pref, None)
            if fn:
                jar = fn(domain_name="google.com")
                temp = {c.name: c.value for c in jar if not c.is_expired()}
                psid = temp.get("__Secure-1PSID")
                if psid:
                    return psid, temp.get("__Secure-1PSIDTS") or None, browser_pref
        except Exception:
            pass
        return None, None, ""

    data = load_browser_cookies(domain_name="google.com")
    if not data:
        return None, None, ""
    prefer = ("chrome", "edge", "chromium", "brave", "firefox", "opera")
    ordered = []
    for key in prefer:
        for browser, cookies in data.items():
            if key in browser.lower() and (browser, cookies) not in ordered:
                ordered.append((browser, cookies))
    for browser, cookies in data.items():
        if (browser, cookies) not in ordered:
            ordered.append((browser, cookies))
    for browser, cookies in ordered:
        temp = {c["name"]: c["value"] for c in cookies}
        psid = temp.get("__Secure-1PSID")
        if psid:
            return psid, temp.get("__Secure-1PSIDTS") or None, browser
    return None, None, ""


def _resolve_cookies(cfg):
    """解析 Cookie 来源。

    auto 模式优先使用程序上次通过请求续期后保存的文件，避免每次生图都
    依赖调试 Chrome；仅在本地没有可用记录时再尝试 CDP/浏览器。
    """
    source = str((cfg or {}).get("cookie_source") or "auto").strip().lower()

    if source == "file":
        psid, psidts = _load_cookies_from_file(cfg)
        if not psid:
            raise RuntimeError(
                "cookie_source=file 但未找到有效 Cookie 文件。"
                "请设置 cookie_source=auto 或填入 gemini_cookies.json。")
        return psid, psidts, "file"

    if source in ("auto", "auto_then_file"):
        file_psid, file_psidts = _load_cookies_from_file(cfg)
        if file_psid:
            return file_psid, file_psidts, "file"

        cdp_psid, cdp_psidts, cdp_src, cdp_jar = _load_cookies_via_cdp(cfg)
        if cdp_psid:
            cfg["_cdp_cookie_jar"] = cdp_jar
            return cdp_psid, cdp_psidts, cdp_src

        browser_psid, browser_psidts, browser_name = _load_browser_cookies_psid(cfg)
        if browser_psid:
            return browser_psid, browser_psidts, f"browser:{browser_name}"
        return None, None, "browser_auto"

    # 未知值沿用旧行为：按文件模式处理，避免意外扫描浏览器。
    psid, psidts = _load_cookies_from_file(cfg)
    if not psid:
        raise RuntimeError(
            "cookie_source=file 但未找到有效 Cookie 文件。"
            "请设置 cookie_source=auto 或填入 gemini_cookies.json。")
    return psid, psidts, "file"


def _ensure_generate_prompt(prompt, cfg):
    prefix = (cfg.get("prompt_prefix") or "请用 AI 生成一张图片：").strip()
    p = (prompt or "").strip()
    if not p:
        raise RuntimeError("生图提示词为空")
    low = p.lower()
    if "生成" in p or "generate" in low or "画" in p:
        return p
    return f"{prefix}{p}"


def _resolve_image_model(client, cfg):
    """网页 UI 会选具体模型；默认 UNSPECIFIED 会误报额度用完，需指定 Flash 等生图模型。"""
    from gemini_webapi.constants import Model

    explicit = (cfg.get("model") or "").strip()
    # 配置里可能残留 dall-e-3 等非 Gemini 网页模型名，需忽略并自动选 Flash
    _skip = {"", "dall-e-3", "dall-e-2", "dall-e"}
    if explicit.lower() not in _skip and not explicit.lower().startswith("dall-"):
        try:
            return Model.from_name(explicit)
        except ValueError:
            try:
                return client._resolve_model_by_name(explicit)
            except (ValueError, Exception):
                pass

    for m in client.list_models() or []:
        if not m.is_available:
            continue
        name = (m.model_name or "").lower()
        display = (m.display_name or "").lower()
        if "flash" in name or "flash" in display or "快速" in (m.display_name or ""):
            return m

    return Model.BASIC_FLASH


def _cookie_value(cookies, name):
    for c in cookies.jar:
        if c.name == name:
            return c.value
    return ""


def _persist_cookies(client, cfg):
    """把刷新后的 Cookie 写回 gemini_cookies.json，延长有效期。"""
    path = (cfg.get("cookie_file") or _DEFAULT_COOKIE).strip()
    if not path:
        return
    jar = client.cookies
    psid = _cookie_value(jar, "__Secure-1PSID")
    psidts = _cookie_value(jar, "__Secure-1PSIDTS")
    if not psid:
        return
    if os.path.isfile(path):
        try:
            raw = json.load(open(path, encoding="utf-8"))
        except Exception:
            raw = {}
    else:
        raw = {}
    if isinstance(raw, dict):
        raw["__Secure-1PSID"] = psid
        if psidts:
            raw["__Secure-1PSIDTS"] = psidts
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")


def persist_cookies_to_file(psid, psidts, cfg=None):
    """将 Cookie 写入 gemini_cookies.json（CDP/手动同步用）。"""
    if not psid:
        return False
    cfg = cfg or {}
    path = (cfg.get("cookie_file") or _DEFAULT_COOKIE).strip()
    raw = {}
    if os.path.isfile(path):
        try:
            raw = json.load(open(path, encoding="utf-8"))
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["__Secure-1PSID"] = psid
    if psidts:
        raw["__Secure-1PSIDTS"] = psidts
    raw["_synced_at"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def _invalidate_cookie_file(cfg):
    """清除失效的本地 Cookie 备份，避免反复使用过期值。"""
    path = (cfg.get("cookie_file") or _DEFAULT_COOKIE).strip()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


async def _check_auth_async(psid, psidts, cfg, extra_jar):
    c = _make_gemini_client(psid, psidts, cfg, extra_jar)
    await c.init(timeout=30, auto_close=True, auto_refresh=False)
    st = c.account_status
    await c.close()
    return st


async def sync_cookies_async(cfg=None, verify=False):
    """从 CDP 读取最新 Cookie 并写入 gemini_cookies.json。返回 {ok, source, message}。"""
    cfg = dict(cfg or {})
    port = cfg.get("cdp_port") or 9222
    psid, psidts, src, jar = _load_cookies_via_cdp(cfg)
    cfg["_cdp_cookie_jar"] = jar
    if not psid:
        if _cdp_port_open(port):
            return {
                "ok": False,
                "source": "cdp",
                "message": "CDP 已连接，请在调试 Chrome 打开 gemini.google.com 并登录 Google 账号",
            }
        return {
            "ok": False,
            "source": "",
            "message": "CDP 未就绪，请先运行 chrome_gemini_debug.bat 并保持 Chrome 打开",
        }
    if not psidts:
        return {
            "ok": False,
            "source": src,
            "message": "缺少 __Secure-1PSIDTS，请在调试 Chrome 重新登录 gemini.google.com",
        }

    _clear_webapi_cookie_cache(psid)

    if verify:
        try:
            st = await _check_auth_async(psid, psidts, cfg, jar)
            if st.name == "UNAUTHENTICATED":
                _clear_webapi_cookie_cache(psid)
                psid2, psidts2, src2, jar2 = _load_cookies_via_cdp(cfg)
                if psid2:
                    cfg["_cdp_cookie_jar"] = jar2
                    psid, psidts, src, jar = psid2, psidts2 or psidts, src2 or src, jar2
                    st = await _check_auth_async(psid, psidts, cfg, jar)
            if st.name == "UNAUTHENTICATED":
                _invalidate_cookie_file(cfg)
                return {
                    "ok": False,
                    "source": src,
                    "message": (
                        "CDP 读到 Cookie 但 API 认证失败（已清除 webapi 旧缓存）。"
                        "请在调试 Chrome 打开 gemini.google.com，随便发一条消息刷新会话后重试。"
                        "（网页能生图 ≠ API Cookie 有效，需在同一 bat 打开的 Chrome 里操作）"),
                }
        except Exception as e:
            _invalidate_cookie_file(cfg)
            return {"ok": False, "source": src, "message": f"登录态验证失败: {e}"}

    persist_cookies_to_file(psid, psidts, cfg)
    if verify:
        return {"ok": True, "source": src, "message": "Cookie 已同步且登录态有效"}
    return {"ok": True, "source": src, "message": "Cookie 已同步"}


def sync_cookies(cfg=None, verify=False):
    """CDP 同步入口：仅用于首次授权或请求续期失效后的兜底。"""
    return asyncio.run(sync_cookies_async(cfg, verify))


async def _request_refresh_async(psid, psidts, cfg, source="file"):
    """使用 Google rotation 请求续期，不读取浏览器窗口。"""
    from gemini_webapi.constants import AccountStatus
    from gemini_webapi.utils.rotate_1psidts import rotate_1psidts

    client = _make_gemini_client(
        psid, psidts, cfg, cfg.get("_cdp_cookie_jar") or {})
    timeout = min(60, int(cfg.get("init_timeout") or 45))
    try:
        await client.init(
            timeout=timeout, auto_close=False, auto_refresh=False)
        if client.account_status == AccountStatus.UNAUTHENTICATED:
            raise RuntimeError("Gemini 网页 Cookie 已失效")

        rotated = False
        if client.client:
            # init 可能刚写过 gemini-webapi 自身缓存；清掉它，确保这里真正
            # 发起一次 rotation 请求，而不是因“缓存小于 60 秒”直接跳过。
            _clear_webapi_cookie_cache(psid)
            new_psidts = await rotate_1psidts(client.client, False)
            rotated = bool(new_psidts)
        _persist_cookies(client, cfg)
        return {
            "ok": True,
            "source": f"request:{source}",
            "rotated": rotated,
            "message": "Cookie 已通过请求自动续期",
        }
    except Exception as e:
        return {
            "ok": False,
            "source": f"request:{source}",
            "message": f"请求续期失败: {e}",
        }
    finally:
        try:
            _persist_cookies(client, cfg)
            await client.close()
        except Exception:
            pass


async def refresh_cookies_async(cfg=None, fallback_to_cdp=True):
    """优先纯请求续期；只有登录态失效时才尝试 CDP 重新取登录 Cookie。"""
    cfg = dict(cfg or {})
    try:
        psid, psidts, source = _resolve_cookies(cfg)
    except Exception as e:
        psid, psidts, source = None, None, ""
        resolve_error = str(e)
    else:
        resolve_error = ""

    result = None
    if psid:
        result = await _request_refresh_async(
            psid, psidts, cfg, source=source or "saved")
        if result.get("ok"):
            return result

    if fallback_to_cdp:
        synced = await sync_cookies_async(cfg, verify=False)
        if synced.get("ok"):
            psid, psidts = _load_cookies_from_file(cfg)
            if psid:
                return await _request_refresh_async(
                    psid, psidts, cfg, source=synced.get("source") or "cdp")
        if result:
            result["fallback"] = synced.get("message") or "CDP 同步失败"
            return result
        return synced

    if result:
        return result
    return {
        "ok": False,
        "source": source,
        "message": resolve_error or "未找到可续期的 Gemini Cookie",
    }


def refresh_cookies(cfg=None, fallback_to_cdp=True):
    """请求式自动续期入口，供常驻服务后台定时调用。"""
    return asyncio.run(refresh_cookies_async(cfg, fallback_to_cdp))


def _ensure_authenticated(client):
    from gemini_webapi.constants import AccountStatus

    status = getattr(client, "account_status", None)
    if status == AccountStatus.UNAUTHENTICATED:
        raise RuntimeError(
            "Gemini 网页 Cookie 已过期。请双击 chrome_gemini_debug.bat 用调试模式启动 "
            "Chrome 并登录 gemini.google.com，或重新复制 Cookie 到 gemini_cookies.json。")


def _wrap_auth_error(exc):
    msg = str(exc)
    low = msg.lower()
    if "401" in msg or "unauthorized" in low or "autherror" in low.replace(" ", ""):
        return RuntimeError(
            "Gemini 网页 Cookie 已失效（401）。请重新从浏览器复制 Cookie 到 "
            "gemini_cookies.json 后重试。")
    return exc


def _is_auth_error(exc):
    msg = str(exc).lower()
    return any(k in msg for k in (
        "过期", "失效", "unauthorized", "401", "unauthenticated", "cookie",
    ))


async def _prepare_cookies_async(cfg, verify=False):
    """优先使用请求续期后保存的 Cookie；缺失时才从 CDP 获取。"""
    cfg = dict(cfg or {})
    src_mode = str(cfg.get("cookie_source") or "auto").strip().lower()
    psid, psidts, source = _resolve_cookies(cfg)
    if psid:
        return psid, psidts, source
    if src_mode in ("auto", "auto_then_file"):
        r = await sync_cookies_async(cfg, verify=verify)
        if not r.get("ok"):
            raise RuntimeError(r.get("message") or "Cookie 同步失败")
    return _resolve_cookies(cfg)


async def _init_generate_client(cfg, psid, psidts):
    """初始化生图客户端；UNAUTHENTICATED 时自动从 CDP 刷新并重试。"""
    from gemini_webapi.constants import AccountStatus
    from gemini_webapi.utils.rotate_1psidts import rotate_1psidts

    timeout = min(60, int(cfg.get("init_timeout") or 45))

    async def _open(p, t, jar=None):
        c = _make_gemini_client(p, t, cfg, jar)
        await c.init(timeout=timeout, auto_close=False, auto_refresh=False)
        return c

    client = await _open(psid, psidts)
    if client.account_status != AccountStatus.UNAUTHENTICATED:
        # 每次实际使用时顺手通过请求轮换 PSIDTS 并保存；失败不阻断本次
        # 生图，真正的鉴权错误仍会由 generate_content 捕获并走 CDP 兜底。
        if client.client:
            try:
                _clear_webapi_cookie_cache(psid)
                await rotate_1psidts(client.client, False)
                _persist_cookies(client, cfg)
            except Exception:
                pass
        return client, psid, psidts

    _clear_webapi_cookie_cache(psid)
    psid2, psidts2, _, jar2 = _load_cookies_via_cdp(cfg)
    if psid2:
        cfg["_cdp_cookie_jar"] = jar2
        await client.close()
        psid, psidts = psid2, psidts2 or psidts
        client = await _open(psid, psidts, jar2)
        if client.account_status != AccountStatus.UNAUTHENTICATED:
            return client, psid, psidts

    if client.client:
        try:
            await rotate_1psidts(client.client, False)
            await client._init_rpc()
        except Exception:
            pass

    return client, psid, psidts


async def _generate_once(prompt, cfg, psid, psidts, timeout=180):
    full_prompt = _ensure_generate_prompt(prompt, cfg)
    client, psid, psidts = await _init_generate_client(cfg, psid, psidts)
    try:
        _persist_cookies(client, cfg)
        model = _resolve_image_model(client, cfg)
        response = await asyncio.wait_for(
            client.generate_content(full_prompt, model=model),
            timeout=timeout,
        )

        images = list(getattr(response, "images", None) or [])
        if not images:
            txt = (getattr(response, "text", None) or str(response) or "").strip()
            low = txt.lower()
            if "limit" in low or "usage" in low or "设置" in txt or "用量" in txt:
                raise RuntimeError(
                    "Gemini 网页生图额度已用完或受限，请稍后再试或在 gemini.google.com 设置里查看用量。")
            raise RuntimeError(
                "Gemini 网页未返回图片（可能未触发生图，或账号/地区不支持）。"
                f" 文字回复: {txt[:200]}")

        try:
            from gemini_webapi import GeneratedImage
            gen = [img for img in images if isinstance(img, GeneratedImage)]
            pick = gen or images
        except Exception:
            pick = images

        out_dir = tempfile.mkdtemp(prefix="wx_gemini_web_")
        saved = None
        for i, img in enumerate(pick):
            try:
                result = await img.save(path=out_dir, filename=f"gen_{i}.png", verbose=False)
                if result:
                    saved = result if isinstance(result, str) else getattr(result, "path", None)
                    if saved and os.path.isfile(saved):
                        break
            except Exception:
                continue
            for name in os.listdir(out_dir):
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    saved = os.path.join(out_dir, name)
                    break
            if saved:
                break

        if not saved or not os.path.isfile(saved):
            raise RuntimeError("图片已生成但保存到本地失败")

        import shutil
        suffix = os.path.splitext(saved)[1] or ".png"
        fd, keep = tempfile.mkstemp(suffix=suffix, prefix="wx_gemini_web_")
        os.close(fd)
        shutil.copy2(saved, keep)
        _persist_cookies(client, cfg)
        return keep
    except Exception as e:
        raise _wrap_auth_error(e) from e
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def _generate_async(prompt, cfg, timeout=180):
    try:
        from gemini_webapi import GeminiClient  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "请先安装: pip install gemini-webapi") from e

    cfg = dict(cfg or {})
    src_mode = str(cfg.get("cookie_source") or "auto").strip().lower()
    try:
        psid, psidts, cookie_src = await _prepare_cookies_async(cfg, verify=False)
    except RuntimeError as e:
        raise RuntimeError(str(e)) from e

    if not psid:
        port = cfg.get("cdp_port") or 9222
        if src_mode in ("auto", "auto_then_file"):
            if _cdp_port_open(port):
                raise RuntimeError(
                    "CDP 已连接但未读到 Google Cookie，请在调试 Chrome 登录 gemini.google.com")
            raise RuntimeError(
                "调试 Chrome 未运行，请先运行 chrome_gemini_debug.bat 并保持窗口打开")
        raise RuntimeError("未获取到 Gemini 网页 Cookie")

    last_err = None
    for attempt in range(2):
        try:
            return await _generate_once(prompt, cfg, psid, psidts, timeout=timeout)
        except RuntimeError as e:
            last_err = e
            if attempt == 0 and _is_auth_error(e) and src_mode in ("auto", "auto_then_file"):
                _invalidate_cookie_file(cfg)
                _clear_webapi_cookie_cache(psid)
                r = await sync_cookies_async(cfg, verify=False)
                if not r.get("ok"):
                    port = cfg.get("cdp_port") or 9222
                    hint = r.get("message") or "CDP 刷新失败"
                    if not _cdp_port_open(port):
                        hint = "调试 Chrome 未运行，请运行 chrome_gemini_debug.bat"
                    raise RuntimeError(f"Cookie 失效且自动刷新失败: {hint}") from e
                psid, psidts, cookie_src = _resolve_cookies(cfg)
                if not psid:
                    raise RuntimeError("Cookie 失效，CDP 刷新后仍无有效 Cookie") from e
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("生图失败")


def text2image(prompt, cfg=None, timeout=180):
    """同步入口：网页 Cookie 生图，返回本地图片路径。"""
    cfg = cfg or {}
    timeout = int(cfg.get("poll_seconds") or timeout or 180)
    return asyncio.run(_generate_async(prompt, cfg, timeout=timeout))
