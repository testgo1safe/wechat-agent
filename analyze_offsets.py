# -*- coding: utf-8 -*-
"""Static verification of WeChat-Hook offsets against the local Weixin.dll.

We do NOT inject. We only disassemble at the hardcoded RVAs to judge whether
they land on plausible function prologues (code) or vtables (data in .rdata).
"""

import sys
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

DLL = r"D:\wx\4.1.9.52\Weixin.dll"

# namespace offset {} from include/global.h (send-text path)
CODE_OFFSETS = {
    "send_message": 0x1677A30,
    "create_param2": 0xDF40,
    "txt_message_ctr": 0x6B2C30,
    "dec_pic_call": 0x493E70,
}
DATA_OFFSETS = {
    "param1_vtable": 0x84EC9C8,
    "param2": 0xA0CE0B0,
    "param2_1": 0x8595F58,
    "param2_2": 0x8595E98,
    "param2_3": 0x8595DD8,
    "txt_message_vtbl": 0x8279358,
    "img_msg_vtbl": 0x84F96B8,
    "img_msg_vtb2": 0x84F9748,
}

PROLOGUE_HINTS = (
    "int3", "push", "sub rsp", "mov ", "lea ", "xor ", "test ", "cmp ",
    "call", "jmp", "and ", "movups", "movaps", "endbr64",
)


def section_of(pe, rva):
    for s in pe.sections:
        start = s.VirtualAddress
        end = start + max(s.Misc_VirtualSize, s.SizeOfRawData)
        if start <= rva < end:
            return s.Name.rstrip(b"\x00").decode(errors="replace")
    return "?"


def main():
    pe = pefile.PE(DLL, fast_load=True)
    image_base = pe.OPTIONAL_HEADER.ImageBase
    size = pe.OPTIONAL_HEADER.SizeOfImage
    print(f"Weixin.dll  ImageBase=0x{image_base:X}  SizeOfImage=0x{size:X}")
    print(f"Sections:")
    for s in pe.sections:
        nm = s.Name.rstrip(b'\x00').decode(errors='replace')
        print(f"  {nm:<10} VA=0x{s.VirtualAddress:08X} VSize=0x{s.Misc_VirtualSize:08X}")
    print("=" * 70)

    md = Cs(CS_ARCH_X86, CS_MODE_64)

    print("\n[CODE offsets] expect .text and a function prologue:\n")
    for name, rva in CODE_OFFSETS.items():
        sec = section_of(pe, rva)
        try:
            data = pe.get_data(rva, 40)
        except Exception as e:
            print(f"  {name:<16} RVA=0x{rva:07X}  section={sec}  ERROR {e}")
            continue
        insns = list(md.disasm(data, image_base + rva))
        head = "; ".join(f"{i.mnemonic} {i.op_str}".strip() for i in insns[:5])
        flag = "OK?" if sec == ".text" else "!! not .text"
        print(f"  {name:<16} RVA=0x{rva:07X}  section={sec:<8} {flag}")
        print(f"      {head}")

    print("\n[DATA offsets] expect .rdata/.data; vtable[0] should point into .text:\n")
    for name, rva in DATA_OFFSETS.items():
        sec = section_of(pe, rva)
        try:
            ptr_bytes = pe.get_data(rva, 8)
            ptr = int.from_bytes(ptr_bytes, "little")
            target_rva = ptr - image_base if ptr > image_base else None
            tgt_sec = section_of(pe, target_rva) if target_rva and 0 < target_rva < size else "OOB"
        except Exception as e:
            print(f"  {name:<16} RVA=0x{rva:07X}  section={sec}  ERROR {e}")
            continue
        note = f"-> 0x{ptr:X} ({tgt_sec})" if target_rva else f"raw=0x{ptr:X}"
        print(f"  {name:<16} RVA=0x{rva:07X}  section={sec:<8} {note}")


if __name__ == "__main__":
    main()
