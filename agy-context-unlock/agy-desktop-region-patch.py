#!/usr/bin/env python3
"""Antigravity Desktop (GUI) language_server Windows x64 eligibility patch.

Idempotent, dynamic Go symbol & instruction relocation.
Bypasses the region/account eligibility gate in language_server by redirecting
authclient.ValidateAndOnboardAccount to its internal valid-auth success epilogue:
    hasValidAuth = 1, failureDetails = nil, err = nil

Usage:
    python agy-desktop-region-patch.py                 # dry-run
    python agy-desktop-region-patch.py --apply         # apply in-place
"""

import os
import shutil
import struct
import sys

SYM = b"google3/third_party/jetski/language_server/auth_client/authclient.ValidateAndOnboardAccount"
# Target success signature in ValidateAndOnboardAccount:
# movb $1, 0x8(%rax) ; movb $0, 0x20(%rax) ; xor %ebx,%ebx ; xor %ecx,%ecx ; leave ; ret
SUCCESS_SIG = bytes.fromhex("c6400801c640200031db31c9c9c3")


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def default_target():
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = os.path.join(
            local, r"Programs\antigravity\resources\bin\language_server.exe"
        )
        if os.path.exists(candidate):
            return candidate
    sys.exit("[ERROR] language_server.exe not found; pass the path")


def pe_text(data):
    if data[:2] != b"MZ":
        raise RuntimeError("not a Windows PE")
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
    raise RuntimeError("ValidateAndOnboardAccount not found in pclntab")


def find_patch_sites(body):
    """Finds entry jump site (after stack frame allocation) and success target site."""
    # Look for success signature
    sig_pos = body.find(SUCCESS_SIG)
    if sig_pos < 0:
        raise RuntimeError("success signature not found in function")

    # The block preceding SUCCESS_SIG should load the object template
    # (typically starts around 12 bytes before SUCCESS_SIG with lea ..., %rax ; call runtime.newobject)
    target_rel = sig_pos - 12
    if target_rel < 0:
        target_rel = sig_pos

    # Prologue frame setup:
    # Look for sub $imm, %rsp right after mov %rsp, %rbp (48 89 e5)
    rbp_setup = body.find(b"\x55\x48\x89\xe5")
    if rbp_setup < 0:
        raise RuntimeError("standard rbp frame setup not found in prologue")
    
    # Check for sub $imm, %rsp (48 83 ec .. or 48 81 ec .. .. .. ..)
    sub_pos = rbp_setup + 4
    if body[sub_pos:sub_pos + 3] == b"\x48\x83\xec":
        jmp_site = sub_pos + 4
    elif body[sub_pos:sub_pos + 3] == b"\x48\x81\xec":
        jmp_site = sub_pos + 7
    else:
        raise RuntimeError("stack allocation instruction not recognized in prologue")

    return jmp_site, target_rel


def patch_binary(target, apply=False):
    if not os.path.exists(target):
        sys.exit(f"[ERROR] not found: {target}")
    print(f"[*] target: {target}")
    with open(target, "rb") as f:
        data = bytearray(f.read())

    text_va, text_raw, text_size = pe_text(data)
    print(f"[*] .text va={hex(text_va)} raw={hex(text_raw)} size={hex(text_size)}")
    func_va = find_func(data, text_va)
    foff = text_raw + (func_va - text_va)
    print(f"[*] ValidateAndOnboardAccount fileoff={hex(foff)} va={hex(func_va)}")

    # Read function body (limit to 2048 bytes for scanning)
    body = data[foff:foff + 2048]
    jmp_site, target_rel = find_patch_sites(body)
    
    rel32 = target_rel - (jmp_site + 5)
    jmp_blob = b"\xe9" + struct.pack("<i", rel32)
    cur_blob = data[foff + jmp_site:foff + jmp_site + 5]

    if cur_blob == jmp_blob:
        print("[SUCCESS] already patched")
        return True

    print(f"[*] patch queued at fileoff={hex(foff + jmp_site)} (va={hex(func_va + jmp_site)}):")
    print(f"    {cur_blob.hex()} -> {jmp_blob.hex()} (jmp to +{hex(target_rel)})")

    if not apply:
        print("[dry-run] nothing written. Pass --apply to patch.")
        return True

    backup = target + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(target, backup)
        print(f"[*] backup: {backup}")
    else:
        print(f"[=] backup already exists: {backup}")

    data[foff + jmp_site:foff + jmp_site + 5] = jmp_blob
    with open(target, "wb") as f:
        f.write(data)

    print("[SUCCESS] language_server.exe patched. Restart Antigravity.")
    return True


def main():
    apply = "--apply" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--apply"]
    target = args[0] if args else default_target()
    patch_binary(target, apply=apply)


if __name__ == "__main__":
    main()
