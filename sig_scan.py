# -*- coding: utf-8 -*-
"""Port WeChat-Hook offsets from reference 4.1.10.27 to target 4.1.9.52.

Method:
  CODE offsets: disassemble the reference function prologue, wildcard the
    address-like operand bytes (rip-relative disp32 and 4-byte immediates that
    look like addresses), then search the target .text for a unique match.
  DATA/VTABLE offsets: find a rip-relative xref (lea/mov) in the reference
    .text that targets the vtable RVA, build a wildcarded signature of that
    referencing instruction's neighborhood, locate it in the target, and
    re-derive the vtable RVA from the new rip-relative displacement.
"""

import sys
from capstone import (
    Cs, CS_ARCH_X86, CS_MODE_64, CS_OP_MEM, CS_OP_IMM,
)
from capstone.x86 import X86_OP_MEM, X86_OP_IMM, X86_REG_RIP

REF = r"E:\AIagent\ref\Weixin.dll"          # 4.1.10.27 (known-good offsets)
TGT = r"D:\wx\4.1.9.52\Weixin.dll"          # 4.1.9.52 (target)

CODE_OFFSETS = {
    "send_message": 0x1677A30,
    "create_param2": 0xDF40,
    "txt_message_ctr": 0x6B2C30,
    "dec_pic_call": 0x493E70,
}

# vtable/global data offsets -> located via code xref
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

WILDCARD = -1


class PE:
    def __init__(self, path):
        import pefile
        self.pe = pefile.PE(path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.size = self.pe.OPTIONAL_HEADER.SizeOfImage
        self.text = None
        for s in self.pe.sections:
            nm = s.Name.rstrip(b"\x00")
            if nm == b".text":
                self.text_rva = s.VirtualAddress
                self.text = s.get_data()
                self.text_end = s.VirtualAddress + len(self.text)

    def data_at(self, rva, n):
        return self.pe.get_data(rva, n)

    def text_bytes(self):
        return self.text, self.text_rva

    def section_of(self, rva):
        for s in self.pe.sections:
            start = s.VirtualAddress
            end = start + max(s.Misc_VirtualSize, s.SizeOfRawData)
            if start <= rva < end:
                return s.Name.rstrip(b"\x00").decode(errors="replace")
        return "?"


def make_code_signature(pe, rva, min_len=32):
    """Disassemble at rva; return list of ints (0..255) or WILDCARD."""
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    data = pe.data_at(rva, min_len * 2 + 96)
    va = pe.base + rva
    sig = []
    consumed = 0
    for insn in md.disasm(bytes(data), va):
        ins_bytes = list(insn.bytes)
        mask = [b for b in ins_bytes]  # copy of raw bytes
        # Determine wildcard ranges: rip-relative disp, and imm32 address-like.
        wc_positions = set()
        if insn.disp_size and insn.disp_offset:
            # wildcard displacement bytes if rip-relative or large disp
            is_rip = False
            for op in insn.operands:
                if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
                    is_rip = True
            if is_rip or abs(insn.disp) > 0x1000:
                for k in range(insn.disp_offset, insn.disp_offset + insn.disp_size):
                    wc_positions.add(k)
        if insn.imm_size and insn.imm_offset:
            for op in insn.operands:
                if op.type == X86_OP_IMM and abs(op.imm) > 0x10000:
                    for k in range(insn.imm_offset, insn.imm_offset + insn.imm_size):
                        wc_positions.add(k)
        for k, b in enumerate(ins_bytes):
            sig.append(WILDCARD if k in wc_positions else b)
        consumed += insn.size
        if consumed >= min_len:
            break
    return sig


def _best_anchor(sig):
    """Find longest contiguous run of non-wildcard bytes; return (offset, bytes)."""
    best_start, best_len = 0, 0
    i = 0
    m = len(sig)
    while i < m:
        if sig[i] != WILDCARD:
            j = i
            while j < m and sig[j] != WILDCARD:
                j += 1
            if j - i > best_len:
                best_len = j - i
                best_start = i
            i = j
        else:
            i += 1
    return best_start, bytes(sig[best_start:best_start + best_len])


def find_signature(hay, sig, limit=5):
    """Return list of indices where sig (with wildcards) matches hay."""
    n = len(hay)
    m = len(sig)
    anchor_off, anchor = _best_anchor(sig)
    if not anchor:
        return []
    matches = []
    start = 0
    while True:
        idx = hay.find(anchor, start)
        if idx < 0:
            break
        s = idx - anchor_off
        if s >= 0 and s + m <= n:
            ok = True
            for k in range(m):
                v = sig[k]
                if v != WILDCARD and hay[s + k] != v:
                    ok = False
                    break
            if ok:
                matches.append(s)
                if len(matches) >= limit:
                    break
        start = idx + 1
    return matches


def sig_to_str(sig):
    return " ".join("??" if b == WILDCARD else f"{b:02X}" for b in sig)


def resolve_unique(ref, rva, thay, lengths=(48, 80, 120, 200, 300, 450, 650, 900)):
    """Progressively lengthen the signature until exactly one target match."""
    last = None
    for L in lengths:
        sig = make_code_signature(ref, rva, min_len=L)
        matches = find_signature(thay, sig, limit=8)
        last = matches
        if len(matches) == 1:
            return matches[0], L, matches
        if len(matches) == 0:
            return None, L, matches
    return None, lengths[-1], last


def port_code(ref, tgt):
    print("=" * 72)
    print("CODE offsets (signature scan):")
    print("=" * 72)
    thay, trva = tgt.text_bytes()
    results = {}
    for name, rva in CODE_OFFSETS.items():
        pos, L, matches = resolve_unique(ref, rva, thay)
        if pos is not None:
            new_rva = trva + pos
            results[name] = new_rva
            print(f"  [OK ] {name:<16} 0x{rva:07X} -> 0x{new_rva:07X}  (siglen~{L})")
        elif matches is not None and len(matches) == 0:
            print(f"  [MISS] {name:<16} 0x{rva:07X} -> no match (siglen~{L})")
        else:
            rvas = [trva + m for m in (matches or [])]
            print(f"  [AMB] {name:<16} 0x{rva:07X} -> {len(rvas)} matches @siglen~{L}: " +
                  ", ".join(f"0x{r:07X}" for r in rvas[:6]))
    return results


def fast_xref_scan(pe, target_rvas):
    """Fast byte-level scan for rip-relative lea/mov instructions in .text.

    Recognizes 7-byte forms:
      REX.W(48/49/4C/4D) 8D/8B/89 modrm(mod=00,rm=101) disp32
    Returns dict: target_rva -> list of insn_rva (7-byte instruction start).
    """
    text, trva = pe.text_bytes()
    base = pe.base
    want = {}
    for r in target_rvas:
        want[base + r] = r  # target_va -> target_rva
    out = {r: [] for r in target_rvas}
    n = len(text)
    import struct
    i = 0
    REX = (0x48, 0x49, 0x4C, 0x4D)
    OPS = (0x8D, 0x8B, 0x89)
    tb = text
    while i < n - 7:
        b0 = tb[i]
        if b0 in REX and tb[i + 1] in OPS and (tb[i + 2] & 0xC7) == 0x05:
            disp = struct.unpack_from("<i", tb, i + 3)[0]
            insn_end_va = base + trva + i + 7
            tgt_va = insn_end_va + disp
            if tgt_va in want:
                out[want[tgt_va]].append(trva + i)
            i += 7
            continue
        i += 1
    return out


def decode_rip_disp(pe, insn_rva):
    """Decode one rip-relative insn at insn_rva; return target rva or None."""
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    data = pe.data_at(insn_rva, 16)
    try:
        ti = next(md.disasm(bytes(data), pe.base + insn_rva))
    except StopIteration:
        return None
    for op in ti.operands:
        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
            return (ti.address + ti.size + op.mem.disp) - pe.base
    return None


def port_data(ref, tgt):
    print("=" * 72)
    print("DATA/VTABLE offsets (xref porting):")
    print("=" * 72)
    thay, trva = tgt.text_bytes()
    print("  scanning reference .text for xrefs ...")
    ref_xrefs = fast_xref_scan(ref, list(DATA_OFFSETS.values()))
    results = {}
    for name, rva in DATA_OFFSETS.items():
        xrefs = ref_xrefs.get(rva, [])
        if not xrefs:
            print(f"  [MISS] {name:<16} 0x{rva:07X} -> no rip xref in reference")
            continue
        # Voting: each xref site yields candidate positions; decode each to a
        # candidate target data-rva; tally votes across all sites.
        from collections import Counter
        votes = Counter()
        support = {}
        cap = 200 if len(xrefs) > 20 else len(xrefs)
        for xrva in xrefs[:cap]:
            for L in (100, 180, 300, 480):
                sig = make_code_signature(ref, xrva, min_len=L)
                matches = find_signature(thay, sig, limit=10)
                if not matches:
                    break
                if len(matches) <= 8:
                    for mpos in matches:
                        d = decode_rip_disp(tgt, trva + mpos)
                        if d is None:
                            continue
                        sec = tgt.section_of(d)
                        if sec in (".rdata", ".data"):
                            votes[d] += 1
                            support.setdefault(d, set()).add(xrva)
                    break
        # Prefer a target rva supported by the most distinct xref sites.
        best = None
        if votes:
            best = max(votes, key=lambda k: (len(support[k]), votes[k]))
        if best is not None and len(support[best]) >= 1:
            results[name] = best
            print(f"  [OK ] {name:<16} 0x{rva:07X} -> 0x{best:07X}  "
                  f"(votes={votes[best]}, sites={len(support[best])}, sec={tgt.section_of(best)})")
        else:
            print(f"  [AMB] {name:<16} 0x{rva:07X} -> {len(xrefs)} xrefs, no data-section consensus")
    return results


def read_ptr_rvas(pe, rva, n):
    """Read n 8-byte pointers at rva; return list of target rvas (or None)."""
    out = []
    try:
        raw = pe.data_at(rva, n * 8)
    except Exception:
        return out
    import struct
    for i in range(n):
        v = struct.unpack_from("<Q", raw, i * 8)[0]
        r = v - pe.base
        out.append(r if 0 < r < pe.size else None)
    return out


def port_vtable_content(ref, tgt, vtbl_rva, thay, trva, nptr=16,
                        allowed_sections=(".rdata",)):
    """Port a vtable by porting its function pointers, then voting: every ported
    entry i that appears at target address A implies vtable base = A - i*8. The
    true base collects votes from multiple entries."""
    import struct
    from collections import Counter
    ref_fns = read_ptr_rvas(ref, vtbl_rva, nptr)
    ported = []
    for fr in ref_fns:
        if fr is None or ref.section_of(fr) != ".text":
            ported.append(None)
            continue
        pos, L, matches = resolve_unique(ref, fr, thay,
                                         lengths=(64, 120, 220, 360, 560))
        ported.append(trva + pos if pos is not None else None)
    nfn = sum(1 for p in ported if p is not None)
    mapped = tgt.pe.get_memory_mapped_image()
    votes = Counter()
    for i, p in enumerate(ported):
        if p is None:
            continue
        needle = struct.pack("<Q", tgt.base + p)
        start = 0
        found = 0
        while found < 40:
            idx = mapped.find(needle, start)
            if idx < 0:
                break
            if idx % 8 == 0:  # vtable pointers are 8-byte aligned
                base_candidate = idx - i * 8
                votes[base_candidate] += 1
            start = idx + 1
            found += 1
    best = None
    for cand, v in votes.most_common():
        if v >= 2 and tgt.section_of(cand) in allowed_sections:
            best = (cand, v)
            break
    if best:
        return best[0], nfn, best[1]
    return None, nfn, 0


def build_data_hist(pe):
    """Histogram of rip-relative data references: target_rva -> count, for
    targets landing in .data/.rdata. Fast byte scan (lea/mov/mov-store)."""
    import struct
    from collections import Counter
    text, trva = pe.text_bytes()
    tb = text
    n = len(tb)
    base = pe.base
    hist = Counter()
    REX = (0x48, 0x49, 0x4C, 0x4D)
    OPS = (0x8D, 0x8B, 0x89, 0x63, 0x03, 0x2B, 0x3B, 0x39, 0x01)
    i = 0
    # precompute section ranges for data
    data_ranges = []
    for s in pe.pe.sections:
        nm = s.Name.rstrip(b"\x00")
        if nm in (b".data", b".rdata"):
            st = s.VirtualAddress
            en = st + max(s.Misc_VirtualSize, s.SizeOfRawData)
            data_ranges.append((st, en))

    def in_data(rva):
        for st, en in data_ranges:
            if st <= rva < en:
                return True
        return False

    while i < n - 7:
        if tb[i] in REX and tb[i + 1] in OPS and (tb[i + 2] & 0xC7) == 0x05:
            disp = struct.unpack_from("<i", tb, i + 3)[0]
            tgt_rva = trva + i + 7 + disp
            if in_data(tgt_rva):
                hist[tgt_rva] += 1
            i += 7
            continue
        i += 1
    return hist


def port_global_by_frequency(ref, tgt, glob_rva):
    """Match a very-hot global across versions by rip-reference frequency rank."""
    rh = build_data_hist(ref)
    th = build_data_hist(tgt)
    ref_count = rh.get(glob_rva, 0)
    if ref_count == 0:
        return None
    # rank of glob among reference .data globals
    ref_sorted = rh.most_common()
    ref_rank = next((k for k, (a, _) in enumerate(ref_sorted) if a == glob_rva), None)
    # candidates in target: those with count closest to ref_count AND in .data
    tgt_sorted = th.most_common()
    # restrict to .data (mutable global) like param2 (0xA0CE0B0 is in .data)
    glob_sec = ref.section_of(glob_rva)
    cands = [(rva, c) for rva, c in tgt_sorted if tgt.section_of(rva) == glob_sec]
    cands.sort(key=lambda kv: abs(kv[1] - ref_count))
    print(f"      ref_count={ref_count} ref_rank={ref_rank} sec={glob_sec}")
    print("      top target candidates (rva, count, |dcount|):")
    for rva, c in cands[:6]:
        print(f"        0x{rva:07X}  count={c}  d={abs(c - ref_count)}")
    if not cands:
        return None
    # rank-based pick: same rank position within same section
    ref_sec_sorted = [(rva, c) for rva, c in ref_sorted
                      if ref.section_of(rva) == glob_sec]
    ref_sec_rank = next((k for k, (a, _) in enumerate(ref_sec_sorted)
                         if a == glob_rva), None)
    rank_pick = None
    if ref_sec_rank is not None and ref_sec_rank < len(cands):
        # cands sorted by closeness; instead sort by count desc for rank
        tgt_sec_by_count = sorted(cands, key=lambda kv: -kv[1])
        if ref_sec_rank < len(tgt_sec_by_count):
            rank_pick = tgt_sec_by_count[ref_sec_rank][0]
            print(f"      rank-based pick (rank {ref_sec_rank}): 0x{rank_pick:07X}")
    return rank_pick if rank_pick is not None else cands[0][0]


def find_call_sites(pe, target_rva):
    """Find E8 rel32 direct calls whose target == target_rva. Return insn rvas."""
    import struct
    text, trva = pe.text_bytes()
    tb = text
    n = len(tb)
    sites = []
    i = 0
    while i < n - 5:
        if tb[i] == 0xE8:
            rel = struct.unpack_from("<i", tb, i + 1)[0]
            tgt_rva = trva + i + 5 + rel
            if tgt_rva == target_rva:
                sites.append(trva + i)
        i += 1
    return sites


def port_global_near_call(ref, tgt, glob_rva, callee_rva, thay, trva, window=0x180):
    """Port a hot global by picking the xref that sits just before a call to
    callee_rva (create_param2), which uniquely identifies the send-setup site."""
    xrefs = fast_xref_scan(ref, [glob_rva]).get(glob_rva, [])
    if not xrefs:
        return None
    calls = find_call_sites(ref, callee_rva)
    xset = sorted(xrefs)
    cand = []
    for c in calls:
        for xr in xset:
            if c - window <= xr <= c:
                cand.append(xr)
    # de-dup, keep order
    seen = set()
    cand = [x for x in cand if not (x in seen or seen.add(x))]
    for xr in cand:
        pos, L, matches = resolve_unique(ref, xr, thay,
                                         lengths=(64, 120, 220, 360))
        if pos is not None:
            d = decode_rip_disp(tgt, trva + pos)
            if d is not None and tgt.section_of(d) in (".data", ".rdata"):
                return d, xr, len(calls)
    return None, None, len(calls)


def main():
    print("Loading reference 4.1.10.27 ...")
    ref = PE(REF)
    print(f"  base=0x{ref.base:X} .text rva=0x{ref.text_rva:X} size=0x{len(ref.text):X}")
    print("Loading target 4.1.9.52 ...")
    tgt = PE(TGT)
    print(f"  base=0x{tgt.base:X} .text rva=0x{tgt.text_rva:X} size=0x{len(tgt.text):X}")
    print()

    code_results = port_code(ref, tgt)
    print()
    data_results = port_data(ref, tgt)

    print()
    print("=" * 72)
    print("TARGETED porting for text-send offsets still missing:")
    print("=" * 72)
    thay, trva = tgt.text_bytes()

    if "txt_message_vtbl" not in data_results:
        vb, nfn, nv = port_vtable_content(ref, tgt, DATA_OFFSETS["txt_message_vtbl"],
                                          thay, trva)
        if vb is not None:
            data_results["txt_message_vtbl"] = vb
            print(f"  [OK ] txt_message_vtbl via content -> 0x{vb:07X} "
                  f"({nfn} fns ported, votes={nv}, sec={tgt.section_of(vb)})")
        else:
            print(f"  [FAIL] txt_message_vtbl content porting ({nfn} fns ported)")

    if "param2" not in data_results:
        print("  porting param2 by reference-frequency rank ...")
        d = port_global_by_frequency(ref, tgt, DATA_OFFSETS["param2"])
        if d is not None:
            data_results["param2"] = d
            print(f"  [OK?] param2 via frequency -> 0x{d:07X} (sec={tgt.section_of(d)})")
        else:
            print(f"  [FAIL] param2 frequency porting")

    print()
    print("=" * 72)
    print("SUMMARY (new 4.1.9.52 offsets):")
    print("=" * 72)
    all_results = {**code_results, **data_results}
    for name in list(CODE_OFFSETS) + list(DATA_OFFSETS):
        if name in all_results:
            print(f"    {name:<18} = 0x{all_results[name]:X};")
        else:
            print(f"    {name:<18} = <UNRESOLVED>")


if __name__ == "__main__":
    main()
