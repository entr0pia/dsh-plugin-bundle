#!/usr/bin/env python3
"""agy Windows x64 eligibility patch. Idempotent, not tied to one build.

Versions do not share file offsets. This script relocates both sites from
the Go symbol and the instruction shape:

  cmp/test byte ptr [reg+disp], 0
  jcc  success          ; skip the failure path

Patch 1 is that jump inside IneligibilityFromResult, so the function's own
nil return runs (whatever registers that build uses). Patch 2 is the same
jump at the call that prints "Account ineligible".

A jump that already goes to that target is left alone. Anything that is
not this shape aborts with the file unchanged. Direct calls only; the
binary has to be Windows PE32+ x64.

    python agy-elig-patch.py                 # dry-run
    python agy-elig-patch.py --apply
    python agy-elig-patch.py --apply PATH
"""
import os
import shutil
import struct
import sys

SYM = b"google3/third_party/jetski/cli/backend/backend.IneligibilityFromResult"
# Older builds of this script stomped the prologue with this stub.
ENTRY_STUB = bytes.fromhex("31c031db31c931ff31f64531c04531c94531d2c3")
FLAG_DISP_MAX = 64
FUNC_SCAN = 0x180
CALL_LOOKBACK = 48


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def default_target():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = os.path.join(local, "agy", "bin", "agy.exe")
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found
    sys.exit("[ERROR] agy.exe not found; pass the path")


def pe_text(data):
    if data[:2] != b"MZ":
        raise RuntimeError("not a Windows PE. The macOS ARM64 build needs the other script")
    e_lfanew = u32(data, 0x3C)
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise RuntimeError("bad PE signature")
    coff = e_lfanew + 4
    nsects = u16(data, coff + 2)
    opt_size = u16(data, coff + 16)
    opt = coff + 20
    if u16(data, opt) != 0x20B:
        raise RuntimeError("not PE32+ / x64")
    image_base = u64(data, opt + 24)
    sect = opt + opt_size
    for i in range(nsects):
        off = sect + i * 40
        name = data[off:off + 8].rstrip(b"\x00")
        vsize, va, rawsize, raw = struct.unpack_from("<IIII", data, off + 8)
        if name == b".text":
            return image_base + va, raw, min(vsize, rawsize)
    raise RuntimeError(".text not found")


def iter_pcln(data):
    magics = (
        b"\xf0\xff\xff\xff\x00\x00\x01\x08",
        b"\xf1\xff\xff\xff\x00\x00\x01\x08",
        b"\xf2\xff\xff\xff\x00\x00\x01\x08",
        b"\xfa\xff\xff\xff\x00\x00\x01\x08",
    )
    start = 0
    while True:
        found = [i for i in (data.find(m, start) for m in magics) if i != -1]
        if not found:
            return
        hdr = min(found)
        yield hdr
        start = hdr + 8


def find_func(data, text_va):
    for hdr_idx in iter_pcln(data):
        if hdr_idx + 72 > len(data):
            continue
        nfunc = u64(data, hdr_idx + 8)
        text_start = u64(data, hdr_idx + 24)
        funcname_off = u64(data, hdr_idx + 32)
        pcln_off = u64(data, hdr_idx + 64)
        if not (1 <= nfunc <= 5_000_000):
            continue
        if funcname_off >= len(data) or pcln_off >= len(data):
            continue
        name_base = hdr_idx + funcname_off
        sym_at = data.find(SYM + b"\x00", name_base, min(len(data), name_base + 80_000_000))
        if sym_at < 0:
            continue
        sym_off = sym_at - name_base
        tab = hdr_idx + pcln_off
        if tab + nfunc * 8 > len(data):
            continue
        for i in range(nfunc):
            entry_off, func_off = struct.unpack_from("<II", data, tab + i * 8)
            fpos = tab + func_off
            if fpos + 8 > len(data):
                continue
            if struct.unpack_from("<i", data, fpos + 4)[0] != sym_off:
                continue
            base = text_start if text_start else text_va
            return base + entry_off
    raise RuntimeError("IneligibilityFromResult not in pclntab (symbol renamed or binary stripped)")


def va_of(off, text_va, text_raw):
    return text_va + (off - text_raw)


def parse_cmp_mem8(data, off):
    """cmp/test byte ptr [reg+disp], 0. Returns total length, or None."""
    if off >= len(data):
        return None
    i = off
    if data[i] in (0x40, 0x41):  # REX, including REX.B for r8-r15
        i += 1
    if i + 3 > len(data):
        return None
    op, modrm = data[i], data[i + 1]
    if op == 0x80 and (modrm >> 3) & 7 == 7:
        pass
    elif op == 0xF6 and (modrm >> 3) & 7 == 0:
        pass
    else:
        return None
    mod, rm = modrm >> 6, modrm & 7
    p = i + 2
    if rm == 4:
        if p >= len(data):
            return None
        p += 1
    if mod == 1:
        disp = struct.unpack_from("<b", data, p)[0]
        p += 1
    elif mod == 2:
        if p + 4 > len(data):
            return None
        disp = struct.unpack_from("<i", data, p)[0]
        p += 4
    elif mod == 0 and rm != 5:
        disp = 0
    else:
        return None
    if p >= len(data):
        return None
    imm = data[p]
    p += 1
    if imm != 0 or not (0 <= disp <= FLAG_DISP_MAX):
        return None
    return p - off


def parse_branch(data, off, va):
    """Conditional or unconditional jump. Returns (length, target, taken_already)."""
    if off >= len(data):
        return None
    b0 = data[off]
    if 0x70 <= b0 <= 0x7F and off + 2 <= len(data):
        rel = struct.unpack_from("<b", data, off + 1)[0]
        return 2, va + 2 + rel, False
    if b0 == 0xEB and off + 2 <= len(data):
        rel = struct.unpack_from("<b", data, off + 1)[0]
        return 2, va + 2 + rel, True
    if b0 == 0x0F and off + 6 <= len(data) and 0x80 <= data[off + 1] <= 0x8F:
        rel = i32(data, off + 2)
        return 6, va + 6 + rel, False
    if b0 == 0xE9 and off + 5 <= len(data):
        rel = i32(data, off + 1)
        return 5, va + 5 + rel, True
    return None


def force_taken(va, length, target):
    """Same-length unconditional jump to target. 6-byte jcc becomes jmp+nop."""
    if length == 2:
        rel = target - (va + 2)
        if not (-128 <= rel <= 127):
            raise RuntimeError("short jump target does not fit")
        return bytes((0xEB, rel & 0xFF))
    if length == 6:
        rel = target - (va + 5)
        if not (-0x80000000 <= rel < 0x80000000):
            raise RuntimeError("near jump target does not fit")
        return b"\xE9" + struct.pack("<i", rel) + b"\x90"
    if length == 5:
        rel = target - (va + 5)
        return b"\xE9" + struct.pack("<i", rel)
    raise RuntimeError(f"unsupported branch length {length}")


def flag_branches(data, start, end, text_va, text_raw):
    """cmp/test byte [reg+disp],0 followed by a jump, inside [start, end)."""
    hits = []
    for off in range(start, max(start, end - 4)):
        n = parse_cmp_mem8(data, off)
        if not n:
            continue
        joff = off + n
        gap = 0
        while gap < 4 and joff + gap < end and data[joff + gap] == 0x90:
            gap += 1
        joff += gap
        if joff >= end:
            continue
        br = parse_branch(data, joff, va_of(joff, text_va, text_raw))
        if not br:
            continue
        length, target, already = br
        hits.append((joff, length, target, already, off))
    return hits


def direct_calls(data, text_va, text_raw, text_size, func_va):
    sites = []
    end = text_raw + text_size - 5
    for off in range(text_raw, end):
        if data[off] != 0xE8:
            continue
        va = va_of(off, text_va, text_raw)
        if va + 5 + i32(data, off + 1) == func_va:
            sites.append(off)
    return sites


def has_ret(data, off, text_raw, text_size, n=0x80):
    end = min(text_raw + text_size, off + n)
    return b"\xc3" in data[off:end]


def queue(writes, data, off, blob, label):
    cur = data[off:off + len(blob)]
    if cur == blob:
        print(f"[=] {label} already applied at {hex(off)}")
        return
    writes.append((off, blob))
    print(f"[*] {label} queued at {hex(off)}: {cur.hex()} -> {blob.hex()}")


def patch_function(data, foff, text_va, text_raw, text_size, writes):
    if data[foff:foff + len(ENTRY_STUB)] == ENTRY_STUB or data[foff:foff + 3] == b"\x31\xc0\xc3":
        print("[=] patch 1 already applied (entry returns nil)")
        return
    hits = flag_branches(data, foff, foff + FUNC_SCAN, text_va, text_raw)
    chosen = []
    for joff, length, target, already, cmp_off in hits:
        t_off = text_raw + (target - text_va)
        if not (foff < t_off < foff + 0x400):
            continue
        if not has_ret(data, t_off, text_raw, text_size):
            continue
        chosen.append((joff, length, target, already, cmp_off))
    if not chosen:
        print("[!] patch 1 shape not found inside the function; relying on patch 2")
        return
    # One flag check, or several that share a single nil-return target.
    targets = {c[2] for c in chosen}
    if len(targets) != 1:
        raise RuntimeError(f"patch 1: {len(targets)} different nil-return targets, aborting")
    for joff, length, target, already, cmp_off in chosen:
        blob = force_taken(va_of(joff, text_va, text_raw), length, target)
        # A 5-byte jmp that replaced a 6-byte jcc is followed by the nop we wrote.
        if length == 5 and data[joff + 5] == 0x90:
            blob = blob + b"\x90"
        queue(writes, data, joff, blob, "patch 1")


def patch_callers(data, sites, text_va, text_raw, writes):
    gated = []
    for call_off in sites:
        call_va = va_of(call_off, text_va, text_raw)
        start = max(0, call_off - CALL_LOOKBACK)
        for joff, length, target, already, cmp_off in flag_branches(
                data, start, call_off, text_va, text_raw):
            if target <= call_va:
                continue
            if call_off - (joff + length) > 16:
                continue
            gated.append((call_off, joff, length, target, already))
    print(f"[*] direct calls: {len(sites)}, flag gates: {len(gated)}")
    if not gated:
        raise RuntimeError(
            "no flag check in front of a call to IneligibilityFromResult; "
            "this build does not match, file not modified"
        )
    for call_off, joff, length, target, already in gated:
        blob = force_taken(va_of(joff, text_va, text_raw), length, target)
        if length == 5 and joff + 6 <= call_off and data[joff + 5] == 0x90:
            blob += b"\x90"
        queue(writes, data, joff, blob, "patch 2")


def main():
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    target = args[0] if args else default_target()
    if not os.path.exists(target):
        sys.exit(f"[ERROR] not found: {target}")
    print(f"[*] target: {target}")
    data = open(target, "rb").read()
    text_va, text_raw, text_size = pe_text(data)
    print(f"[*] .text va={hex(text_va)} raw={hex(text_raw)} size={hex(text_size)}")
    func_va = find_func(data, text_va)
    if not (text_va <= func_va < text_va + text_size):
        sys.exit(f"[ERROR] function va {hex(func_va)} is outside .text")
    foff = text_raw + (func_va - text_va)
    print(f"[*] IneligibilityFromResult fileoff={hex(foff)} va={hex(func_va)}")

    writes = []
    patch_function(data, foff, text_va, text_raw, text_size, writes)
    patch_callers(data, direct_calls(data, text_va, text_raw, text_size, func_va),
                  text_va, text_raw, writes)

    if not apply:
        print("[dry-run] nothing written. Pass --apply to patch.")
        return
    pending = [(off, blob) for off, blob in writes if data[off:off + len(blob)] != blob]
    if not pending:
        print("[SUCCESS] already patched")
        return
    backup = target + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(target, backup)
        print(f"[*] backup: {backup}")
    else:
        print(f"[=] backup already exists: {backup}")
    with open(target, "r+b") as f:
        for off, blob in pending:
            f.seek(off)
            f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    check = open(target, "rb").read()
    for off, blob in pending:
        if check[off:off + len(blob)] != blob:
            sys.exit("[ERROR] verify failed")
    print("[SUCCESS] agy patched. Restart agy.")


if __name__ == "__main__":
    main()

