# -*- coding: utf-8 -*-
"""Port the sqlite3 api_routines and vfs struct offsets from reference
4.1.10.27 to target 4.1.9.52, reusing the content-voting method (these structs
are just big arrays of function pointers into .text, like vtables)."""

import struct
from sig_scan import PE, port_vtable_content

REF = r"E:\AIagent\ref\Weixin.dll"
TGT = r"D:\wx\4.1.9.52\Weixin.dll"

# reference offsets (from global.h, valid for 4.1.10.27)
REF_API = 0x8BB0D38
REF_VFS = 0xA6C0490


def dump_ptrs(pe, rva, n, tag):
    raw = pe.data_at(rva, n * 8)
    print(f"  {tag} @0x{rva:X} first {n} entries:")
    for i in range(n):
        v = struct.unpack_from("<Q", raw, i * 8)[0]
        r = v - pe.base
        sec = pe.section_of(r) if 0 < r < pe.size else "OOB/0"
        print(f"    [{i:2}] 0x{v:016X} ({sec})")


def main():
    print("Loading reference ...")
    ref = PE(REF)
    print("Loading target ...")
    tgt = PE(TGT)
    thay, trva = tgt.text_bytes()

    print("\n== reference api_routines sanity ==")
    dump_ptrs(ref, REF_API, 8, "api_routines")
    print("\n== reference vfs sanity ==")
    dump_ptrs(ref, REF_VFS, 12, "vfs")

    print("\n== porting api_routines (content voting) ==")
    api_base, api_nfn, api_votes = port_vtable_content(
        ref, tgt, REF_API, thay, trva, nptr=40)
    if api_base is not None:
        print(f"  [OK ] api_routines -> 0x{api_base:07X} "
              f"(ported {api_nfn} fns, votes={api_votes}, sec={tgt.section_of(api_base)})")
    else:
        print(f"  [FAIL] api_routines (ported {api_nfn} fns)")

    print(f"\n  (ref vfs section = {ref.section_of(REF_VFS)})")
    print("== porting vfs (content voting) ==")
    vfs_base, vfs_nfn, vfs_votes = port_vtable_content(
        ref, tgt, REF_VFS, thay, trva, nptr=24,
        allowed_sections=(".data", ".rdata"))
    if vfs_base is not None:
        print(f"  [OK ] vfs -> 0x{vfs_base:07X} "
              f"(ported {vfs_nfn} fns, votes={vfs_votes}, sec={tgt.section_of(vfs_base)})")
    else:
        print(f"  [FAIL] vfs (ported {vfs_nfn} fns)")

    print("\n== verify ported target structs ==")
    if api_base is not None:
        dump_ptrs(tgt, api_base, 8, "TGT api_routines")
    if vfs_base is not None:
        dump_ptrs(tgt, vfs_base, 12, "TGT vfs")

    print("\n== SUMMARY ==")
    if api_base is not None:
        print(f"    XWECHAT_SQLITE3_API_ROUTINES_OFFSET = 0x{api_base:X};")
    if vfs_base is not None:
        print(f"    XWECHAT_SQLITE3_VFS_OFFSET          = 0x{vfs_base:X};")


if __name__ == "__main__":
    main()
