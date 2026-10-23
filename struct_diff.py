# -*- coding: utf-8 -*-
"""Compare the TextMessage constructor (txt_message_ctr) between the reference
4.1.10.27 and target 4.1.9.52 to detect struct-layout drift.

We disassemble both constructors and list the [reg+disp] memory-write offsets
they touch (the object is built via rcx). If the offset sequences match, the
struct layout is unchanged; if not, the diffs reveal how fields moved.
"""

import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_OP_IMM, X86_REG_RIP

REF = r"E:\AIagent\ref\Weixin.dll"
TGT = r"D:\wx\4.1.9.52\Weixin.dll"

REF_CTR = 0x6B2C30
TGT_CTR = 0x6A5ED0

N_BYTES = 0xC00


def disasm_writes(path, rva, n):
    pe = pefile.PE(path, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    data = pe.get_data(rva, n)
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    lines = []
    writes = []  # (disp, mnemonic, op_str)
    for ins in md.disasm(bytes(data), base + rva):
        lines.append((ins.address - base, ins.mnemonic, ins.op_str, ins.bytes))
        # detect memory operand with base reg (not rip) and a displacement
        if ins.operands:
            dst = ins.operands[0]
            if dst.type == X86_OP_MEM and dst.mem.base not in (0, X86_REG_RIP):
                if ins.mnemonic in ("mov", "movups", "movaps", "movdqu", "movq",
                                    "lea", "and", "or"):
                    writes.append((dst.mem.disp, ins.mnemonic, ins.op_str))
        # stop at ret if we've gone a bit
        if ins.mnemonic == "ret" and (ins.address - base) - rva > 0x40:
            break
    return writes, lines


def summarize(path, rva, tag):
    writes, lines = disasm_writes(path, rva, N_BYTES)
    disps = sorted(set(d for d, _, _ in writes if 0 <= d < 0x2000))
    print(f"== {tag}  ctr@0x{rva:X}  ({len(lines)} insns) ==")
    print(f"   distinct write offsets: " +
          ", ".join(f"0x{d:X}" for d in disps))
    return disps, writes


def main():
    rd, rw = summarize(REF, REF_CTR, "REF 4.1.10.27")
    print()
    td, tw = summarize(TGT, TGT_CTR, "TGT 4.1.9.52")
    print()
    print("== offsets in REF but not TGT ==")
    print("   " + ", ".join(f"0x{d:X}" for d in rd if d not in td))
    print("== offsets in TGT but not REF ==")
    print("   " + ", ".join(f"0x{d:X}" for d in td if d not in rd))
    same = rd == td
    print(f"\nSTRUCT LAYOUT {'IDENTICAL' if same else 'DIFFERS'}")


if __name__ == "__main__":
    main()
