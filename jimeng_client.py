# -*- coding: utf-8 -*-
"""即梦官方 dreamina CLI 封装（文生图）。"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_EXE = os.path.join(_SCRIPT_DIR, "tools", "dreamina", "dreamina.exe")


def resolve_dreamina_exe(cfg=None):
    cfg = cfg or {}
    p = (cfg.get("dreamina_path") or "").strip()
    if p and os.path.isfile(p):
        return os.path.abspath(p)
    w = shutil.which("dreamina") or shutil.which("dreamina.exe")
    if w:
        return os.path.abspath(w)
    if os.path.isfile(_DEFAULT_EXE):
        return _DEFAULT_EXE
    raise RuntimeError(
        "找不到 dreamina CLI。请运行 curl -s https://jimeng.jianying.com/cli | bash "
        "或配置 image_gen.dreamina_path，并执行 dreamina login")


def _extract_json(text):
    text = text or ""
    for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    return None


def _image_url_from_payload(payload):
    if not payload:
        return None
    item = payload[0] if isinstance(payload, list) else payload
    if not isinstance(item, dict):
        return None
    for path in (
        ("result_json", "images", 0, "image_url"),
        ("result_json", "images", 0, "url"),
        ("result", "images", 0, "image_url"),
        ("result", "images", 0, "url"),
    ):
        cur = item
        ok = True
        for key in path:
            if isinstance(key, int):
                if not isinstance(cur, list) or len(cur) <= key:
                    ok = False
                    break
                cur = cur[key]
            else:
                if not isinstance(cur, dict):
                    ok = False
                    break
                cur = cur.get(key)
        if ok and isinstance(cur, str) and cur.startswith("http"):
            return cur
    for key in ("image_url", "url"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _run_cli(exe, args, timeout=120):
    cmd = [exe] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"dreamina 超时 ({timeout}s): {' '.join(args)}") from e
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "未检测到有效登录态" in combined or "请先执行 dreamina login" in combined:
        raise RuntimeError("dreamina 未登录，请先执行: dreamina login")
    if "没有 dreamina_cli 使用权限" in combined or "仅限高级" in combined:
        raise RuntimeError(
            "即梦 CLI 需要「高级」及以上会员（当前账号等级不足）。"
            "请在即梦官网升级会员，或使用 provider=openai。")
    if proc.returncode != 0 and not _extract_json(combined):
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"dreamina 失败 (code={proc.returncode})")
    return combined


def _download_url(url, suffix=".jpg"):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="wx_jimeng_")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _pick_local_image(download_dir):
    exts = (".png", ".jpg", ".jpeg", ".webp")
    found = []
    for root, _, names in os.walk(download_dir):
        for name in names:
            if name.lower().endswith(exts):
                found.append(os.path.join(root, name))
    if not found:
        return None
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def _query_and_download(exe, submit_id, download_dir, timeout=90):
    out = _run_cli(
        exe,
        ["query_result", f"--submit_id={submit_id}",
         f"--download_dir={download_dir}"],
        timeout=timeout,
    )
    payload = _extract_json(out)
    url = _image_url_from_payload(payload)
    if url:
        return _download_url(url)
    local = _pick_local_image(download_dir)
    if local:
        return local
    item = payload if isinstance(payload, dict) else (payload[0] if payload else {})
    status = str((item or {}).get("gen_status") or "").lower()
    if status in ("fail", "failed", "error"):
        reason = (item or {}).get("fail_reason") or (item or {}).get("error_msg") or out[:200]
        raise RuntimeError(f"即梦生图失败: {reason}")
    return None


def text2image(prompt, cfg=None, timeout=180):
    """调用 dreamina text2image，返回本地图片路径。"""
    cfg = cfg or {}
    exe = resolve_dreamina_exe(cfg)
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("生图提示词为空")

    poll = max(0, int(cfg.get("poll_seconds") or 60))
    ratio = cfg.get("ratio") or "1:1"
    resolution = cfg.get("resolution_type") or "2k"
    model_version = (cfg.get("model_version") or "").strip()

    args = [
        "text2image",
        f"--prompt={prompt}",
        f"--ratio={ratio}",
        f"--resolution_type={resolution}",
        f"--poll={poll}",
    ]
    if model_version:
        args.append(f"--model_version={model_version}")

    out = _run_cli(exe, args, timeout=max(timeout, poll + 30))
    payload = _extract_json(out)
    url = _image_url_from_payload(payload)
    if url:
        return _download_url(url)

    item = payload if isinstance(payload, dict) else (payload[0] if payload else {})
    status = str((item or {}).get("gen_status") or "").lower()
    submit_id = (item or {}).get("submit_id")

    if status in ("success",) and submit_id:
        with tempfile.TemporaryDirectory(prefix="wx_jimeng_dl_") as tmp:
            path = _query_and_download(exe, submit_id, tmp, timeout=60)
            if path:
                fd, keep = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".jpg",
                                            prefix="wx_jimeng_")
                os.close(fd)
                shutil.copy2(path, keep)
                return keep

    if submit_id and status in ("querying", "pending", "processing", "running", ""):
        deadline = time.time() + max(poll, 30)
        with tempfile.TemporaryDirectory(prefix="wx_jimeng_dl_") as tmp:
            while time.time() < deadline:
                path = _query_and_download(exe, submit_id, tmp, timeout=45)
                if path:
                    fd, keep = tempfile.mkstemp(suffix=os.path.splitext(path)[1] or ".jpg",
                                                prefix="wx_jimeng_")
                    os.close(fd)
                    shutil.copy2(path, keep)
                    return keep
                time.sleep(2)

    if status in ("fail", "failed", "error"):
        reason = (item or {}).get("fail_reason") or out[:300]
        raise RuntimeError(f"即梦生图失败: {reason}")

    raise RuntimeError(f"即梦未返回图片: {out[:300]}")
