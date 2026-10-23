# -*- coding: utf-8 -*-
"""微信 .dat 图片解密（旧 XOR / V1 / V2 AES+XOR）。"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import struct
from ctypes import wintypes

V2_MAGIC = b"\x07\x08\x56\x32"
V2_MAGIC_FULL = b"\x07\x08V2\x08\x07"
V1_MAGIC_FULL = b"\x07\x08V1\x08\x07"

RE_KEY32 = re.compile(rb"(?<![0-9A-Za-z])([0-9A-Za-z]{32})(?![0-9A-Za-z])")
RE_KEY16 = re.compile(rb"(?<![0-9A-Za-z])([0-9A-Za-z]{16})(?![0-9A-Za-z])")

IMAGE_MAGIC = {
    "png": [0x89, 0x50, 0x4E, 0x47],
    "gif": [0x47, 0x49, 0x46, 0x38],
    "jpg": [0xFF, 0xD8, 0xFF],
}


def aligned_aes_block_size(aes_size: int) -> int:
    if aes_size % 16:
        return aes_size + (16 - aes_size % 16)
    return aes_size + 16


def detect_image_format(header: bytes) -> str:
    if header[:3] == bytes([0xFF, 0xD8, 0xFF]):
        return "jpg"
    if header[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
        return "png"
    if header[:3] == b"GIF":
        return "gif"
    if header[:2] == b"BM":
        return "bmp"
    if header[:4] == b"RIFF" and len(header) >= 12 and header[8:12] == b"WEBP":
        return "webp"
    if header[:4] == b"wxgf":
        return "hevc"
    return "bin"


def detect_xor_key(dat_path: str) -> int | None:
    with open(dat_path, "rb") as f:
        header = f.read(16)
    if len(header) < 4 or header[:4] == V2_MAGIC:
        return None
    for magic in IMAGE_MAGIC.values():
        key = header[0] ^ magic[0]
        ok = True
        for i in range(1, len(magic)):
            if i >= len(header) or (header[i] ^ key) != magic[i]:
                ok = False
                break
        if ok:
            return key
    return None


def _parse_image_keys(cfg: dict | None) -> tuple[str | None, int]:
    cfg = cfg or {}
    block = cfg.get("image_decrypt") if isinstance(cfg.get("image_decrypt"), dict) else cfg
    aes = block.get("image_aes_key") or block.get("aes_key")
    xor = block.get("image_xor_key", block.get("xor_key", 0x88))
    if isinstance(xor, str):
        xor = int(xor, 0)
    return (str(aes) if aes else None, int(xor))


def parse_image_decrypt_config(cfg: dict | None) -> dict:
    aes, xor = _parse_image_keys(cfg)
    return {"aes_key": aes, "xor_key": xor}


def v2_decrypt_bytes(data: bytes, aes_key, xor_key=0x88) -> bytes | None:
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    if isinstance(aes_key, str):
        aes_key = aes_key.encode("ascii")[:16]
    if not aes_key or len(aes_key) < 16:
        return None
    if isinstance(xor_key, str):
        xor_key = int(xor_key, 0)

    if len(data) < 15:
        return None
    sig = data[:6]
    if sig not in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        return None

    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    key = b"cfcd208495d565ef" if sig == V1_MAGIC_FULL else aes_key[:16]
    aligned = aligned_aes_block_size(aes_size)
    offset = 15
    if offset + aligned > len(data):
        return None

    try:
        cipher = AES.new(key, AES.MODE_ECB)
        dec_aes = Padding.unpad(
            cipher.decrypt(data[offset:offset + aligned]), AES.block_size)
    except (ValueError, KeyError):
        return None
    offset += aligned

    raw_end = len(data) - xor_size
    raw_data = data[offset:raw_end] if offset < raw_end else b""
    xor_data = data[offset + len(raw_data):]
    dec_xor = bytes(b ^ xor_key for b in xor_data)
    out = dec_aes + raw_data + dec_xor

    fmt = detect_image_format(out[:16])
    if fmt == "bin":
        return None
    if xor_size >= 2:
        if fmt == "jpg" and out[-2:] != b"\xff\xd9":
            return None
        if fmt == "png" and b"IEND" not in out[-12:]:
            return None
    return out


def xor_decrypt_bytes(data: bytes, key: int | None = None) -> bytes | None:
    if key is None:
        return None
    out = bytes(b ^ key for b in data)
    if detect_image_format(out[:16]) == "bin":
        return None
    return out


def decrypt_dat_file(dat_path, out_path=None, aes_key=None, xor_key=0x88):
    """解密 .dat，成功返回 (输出路径, 格式)。"""
    with open(dat_path, "rb") as f:
        data = f.read()

    if data[:6] in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        plain = v2_decrypt_bytes(data, aes_key, xor_key)
    else:
        key = detect_xor_key(dat_path) if xor_key is None else xor_key
        plain = xor_decrypt_bytes(data, key)

    if not plain:
        return None, None

    fmt = detect_image_format(plain[:16])
    if out_path is None:
        base = os.path.splitext(dat_path)[0]
        for suffix in ("_t", "_h"):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        out_path = f"{base}.{fmt}"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(plain)
    return out_path, fmt


def extract_v2_ciphertext(dat_path: str) -> bytes | None:
    with open(dat_path, "rb") as f:
        data = f.read(32)
    if len(data) < 32 or data[:6] != V2_MAGIC_FULL:
        return None
    aes_size = struct.unpack_from("<L", data, 6)[0]
    aligned = aligned_aes_block_size(aes_size)
    if 15 + 16 > len(data):
        with open(dat_path, "rb") as f:
            data = f.read(15 + aligned)
    if len(data) < 15 + 16:
        return None
    return data[15:15 + 16]


def find_xor_key_from_attach(attach_dir: str) -> int | None:
    patterns = [
        os.path.join(attach_dir, "*", "*", "Img", "*_t.dat"),
        os.path.join(attach_dir, "*", "*", "Img", "*.dat"),
    ]
    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    files = sorted(set(files), key=os.path.getmtime, reverse=True)
    counts: dict[int, int] = {}
    for path in files[:40]:
        try:
            with open(path, "rb") as f:
                data = f.read()
            if data[:6] != V2_MAGIC_FULL:
                continue
            xor_size = struct.unpack_from("<L", data, 10)[0]
            if xor_size < 2:
                continue
            tail = data[-2:]
            key = tail[0] ^ 0xFF
            if (tail[1] ^ key) == 0xD9:
                counts[key] = counts.get(key, 0) + 1
        except OSError:
            continue
    if not counts:
        return None
    return max(counts, key=counts.get)


def try_aes_key(key_bytes: bytes, ciphertext: bytes) -> str | None:
    from Crypto.Cipher import AES
    try:
        dec = AES.new(key_bytes[:16], AES.MODE_ECB).decrypt(ciphertext)
    except Exception:
        return None
    if dec[:3] == b"\xff\xd8\xff":
        return "jpg"
    if dec[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
        return "png"
    if dec[:4] == b"RIFF":
        return "webp"
    if dec[:4] == b"wxgf":
        return "hevc"
    return None


def find_weixin_pids() -> list[int]:
    import subprocess
    pids = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            text=True, errors="replace",
        )
        for line in out.splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[0].lower() == "weixin.exe":
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    except Exception:
        pass
    return pids


def _scan_regions_win(h_process, kernel32, regions, ciphertext):
    candidates = 0
    for base_addr, region_size, _ in regions:
        if region_size > 50 * 1024 * 1024:
            continue
        buffer = ctypes.create_string_buffer(region_size)
        nread = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            h_process, ctypes.c_void_p(base_addr),
            buffer, region_size, ctypes.byref(nread),
        )
        if not ok or nread.value < 32:
            continue
        data = buffer.raw[:nread.value]
        for m in RE_KEY32.finditer(data):
            candidates += 1
            key = m.group(1)
            if try_aes_key(key[:16], ciphertext):
                return key[:16].decode("ascii")
        for m in RE_KEY16.finditer(data):
            candidates += 1
            key = m.group(1)
            if try_aes_key(key, ciphertext):
                return key.decode("ascii")
    return None


def scan_memory_for_aes_key(pid: int, ciphertext: bytes) -> str | None:
    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT = 0x1000
    PAGE_NOACCESS = 0x01
    PAGE_GUARD = 0x100
    PAGE_READWRITE = 0x04
    PAGE_WRITECOPY = 0x08
    PAGE_EXECUTE_READWRITE = 0x40
    PAGE_EXECUTE_WRITECOPY = 0x80

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    kernel32 = ctypes.windll.kernel32
    h_process = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h_process:
        return None
    try:
        address = 0
        mbi = MEMORY_BASIC_INFORMATION()
        rw_regions = []
        all_regions = []
        while address < 0x7FFFFFFFFFFF:
            result = kernel32.VirtualQueryEx(
                h_process, ctypes.c_void_p(address),
                ctypes.byref(mbi), ctypes.sizeof(mbi),
            )
            if result == 0:
                break
            if (mbi.State == MEM_COMMIT and mbi.Protect != PAGE_NOACCESS
                    and (mbi.Protect & PAGE_GUARD) == 0
                    and mbi.RegionSize <= 50 * 1024 * 1024):
                region = (mbi.BaseAddress, mbi.RegionSize, mbi.Protect)
                all_regions.append(region)
                rw_flags = (PAGE_READWRITE | PAGE_WRITECOPY
                            | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)
                if (mbi.Protect & rw_flags) != 0:
                    rw_regions.append(region)
            nxt = address + mbi.RegionSize
            if nxt <= address:
                break
            address = nxt

        hit = _scan_regions_win(h_process, kernel32, rw_regions, ciphertext)
        if hit:
            return hit
        rw_set = {(a, s) for a, s, _ in rw_regions}
        other = [r for r in all_regions if (r[0], r[1]) not in rw_set]
        return _scan_regions_win(h_process, kernel32, other, ciphertext)
    finally:
        kernel32.CloseHandle(h_process)


def find_v2_sample(attach_dirs: list[str]) -> tuple[str, bytes] | None:
    for attach in attach_dirs:
        pattern = os.path.join(attach, "*", "*", "Img", "*.dat")
        files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        for path in files[:50]:
            ct = extract_v2_ciphertext(path)
            if ct:
                return path, ct
    return None


def discover_image_aes_key(attach_dirs: list[str] | None = None) -> tuple[str | None, int | None]:
    """从微信进程内存提取 V2 AES key，并推导 XOR key。"""
    if not attach_dirs:
        attach_dirs = []
    sample = find_v2_sample(attach_dirs)
    if not sample:
        return None, None
    sample_path, ciphertext = sample
    attach = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(sample_path))))
    xor_key = find_xor_key_from_attach(attach)

    for pid in find_weixin_pids():
        key = scan_memory_for_aes_key(pid, ciphertext)
        if key:
            return key, xor_key
    return None, xor_key
