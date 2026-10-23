# -*- coding: utf-8 -*-
"""Sanity-check the ported 4.1.9.52 offsets against the target Weixin.dll.

vtables: vtbl[0..] must point into .text.
code:    must land in .text and look like a prologue.
param2:  .data global (runtime-initialized) -> only section is checkable.
"""

import struct
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

TGT = r"D:\wx\4.1.9.52\Weixin.dll"

CODE = {
    "send_message": 0x1623940,
    "create_param2": 0xDD90,
    "txt_message_ctr": 0x6A5ED0,
}
VTABLES = {
    "param1_vtable": 0x83D2238,
    "txt_message_vtbl": 0x806DFF8,
    "param2_1": 0x847D998,
    "param2_2": 0x847D8D8,
    "param2_3": 0x847D818,
}
GLOBALS = {
    "param2": 0x9F74EB0,
}


def main():
    pe = pefile.PE(TGT, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    size = pe.OPTIONAL_HEADER.SizeOfImage

    def sec(rva):
        for s in pe.sections:
            st = s.VirtualAddress
            en = st + max(s.Misc_VirtualSize, s.SizeOfRawData)
            if st <= rva < en:
                return s.Name.rstrip(b"\x00").decode(errors="replace")
        return "?"

    md = Cs(CS_ARCH_X86, CS_MODE_64)

    print("== CODE (expect .text + prologue) ==")
    for name, rva in CODE.items():
        s = sec(rva)
        data = pe.get_data(rva, 24)
        ins = list(md.disasm(bytes(data), base + rva))[:4]
        head = "; ".join(f"{i.mnemonic} {i.op_str}".strip() for i in ins)
        print(f"  {name:<16} 0x{rva:07X} [{s}]  {head}")

    print("\n== VTABLES (vtbl[0..3] must -> .text) ==")
    for name, rva in VTABLES.items():
        s = sec(rva)
        raw = pe.get_data(rva, 32)
        ok = True
        tgts = []
        for i in range(4):
            v = struct.unpack_from("<Q", raw, i * 8)[0]
            r = v - base
            ts = sec(r) if 0 < r < size else "OOB"
            tgts.append(ts)
            if ts != ".text":
                ok = False
        flag = "OK" if ok else "?? not all .text"
        print(f"  {name:<16} 0x{rva:07X} [{s}]  entries-> {tgts}  {flag}")

    print("\n== GLOBALS (.data, runtime-init; section only) ==")
    for name, rva in GLOBALS.items():
        s = sec(rva)
        raw = pe.get_data(rva, 8)
        v = struct.unpack_from("<Q", raw, 0)[0]
        print(f"  {name:<16} 0x{rva:07X} [{s}]  file_value=0x{v:X}")


if __name__ == "__main__":
    main()
