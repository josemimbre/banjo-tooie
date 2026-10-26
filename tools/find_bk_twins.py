#!/usr/bin/env python3
"""Find Banjo-Tooie functions that are the same code as a Banjo-Kazooie one.

    tools/find_bk_twins.py [--bk ../banjo-kazooie] [--min-insns 4]
                           [--fuzzy 0.9] [--include-done] [--csv FILE]

Rare reused a lot of Banjo-Kazooie code in Banjo-Tooie, and the BK decomp is
fully in C. Both games are built with IDO 5.3 -O2 -mips2, so a BK C function
whose instructions match a pending Tooie function can usually be copied over.

Each function is read from the linked, uncompressed ROM of each project (the
.map gives its ROM offset and size) and normalized so that only the code
itself is compared:

  - jal / j targets are dropped (functions live at other addresses)
  - lui immediates are dropped (%hi of addresses, but also float constants)
  - immediates of addiu and of loads / stores are dropped unless their base
    register is $zero or $sp (those are %lo of addresses; constants via
    $zero and stack offsets are kept)
  - trailing nops (alignment padding) are dropped

Registers, opcodes, other constants and branch offsets are kept, so a match
means the same instructions in the same order with the same registers. An
exact match is a very strong hint (the BK C will usually match as is, after
renaming the callees and data to their Tooie names). A fuzzy match (same
length, at least --fuzzy of the instructions equal) points at the BK function
Rare most likely edited.

Needs both projects built: build/us/banjotooie_decompressed.{map,z64} here
and build/us.v10/banjo.us.v10.{map,uncompressed.z64} in the BK decomp.
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import mapfile_parser

ROOT = Path(__file__).resolve().parent.parent
NONMATCHINGS = ROOT / "asm" / "nonmatchings"
STUB_RE = re.compile(r"^_\w+_entrypoint_\d+$")

OP_J, OP_JAL, OP_ADDIU, OP_LUI = 0x02, 0x03, 0x09, 0x0F
# loads and stores whose immediate is an offset from the base register rs
MEM_OPS = {0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x28, 0x29, 0x2A, 0x2B, 0x2E,
           0x31, 0x35, 0x39, 0x3D}
REG_ZERO, REG_SP = 0, 29


def normalize(code):
    """Tuple of normalized instruction words (see the module docstring)."""
    words = [int.from_bytes(code[i:i + 4], "big") for i in range(0, len(code) - len(code) % 4, 4)]
    while words and words[-1] == 0:
        words.pop()
    out = []
    for w in words:
        op = w >> 26
        rs = (w >> 21) & 0x1F
        if op in (OP_J, OP_JAL):
            w &= 0xFC000000
        elif op == OP_LUI:
            w &= 0xFFFF0000
        elif (op == OP_ADDIU or op in MEM_OPS) and rs not in (REG_ZERO, REG_SP):
            w &= 0xFFFF0000
        out.append(w)
    return tuple(out)


def functions(map_path, rom_path, strip_prefix):
    """[(name, source path, rom offset, normalized)] for every .text function."""
    mapfile = mapfile_parser.MapFile()
    mapfile.readMapFile(map_path)
    rom = rom_path.read_bytes()
    out = []
    for segment in mapfile.filterBySectionType(".text"):
        for file in segment:
            path = Path(file.filepath)
            parts = path.parts[len(strip_prefix):] if path.parts[:len(strip_prefix)] == strip_prefix else path.parts
            src = Path(*parts) if parts else path
            for sym in file:
                if not sym.size or sym.vrom is None or STUB_RE.match(sym.name):
                    continue
                out.append((sym.name, src, sym.vrom, normalize(rom[sym.vrom:sym.vrom + sym.size])))
    return out


def pending(name, src):
    """True if this Tooie function is still a GLOBAL_ASM .s in asm/nonmatchings."""
    rel = src
    while rel.suffix:
        rel = rel.with_suffix("")
    if rel.parts and rel.parts[0] in ("src", "asm"):
        rel = Path(*rel.parts[1:])
    return (NONMATCHINGS / rel / f"{name}.s").exists()


def main():
    parser = argparse.ArgumentParser(description="Find Tooie functions that are BK code.")
    parser.add_argument("--bk", type=Path, default=ROOT.parent / "banjo-kazooie", help="BK decomp checkout")
    parser.add_argument("--bk-version", default="us.v10")
    parser.add_argument("-v", "--version", default="us")
    parser.add_argument("--min-insns", type=int, default=4, help="skip shorter functions (default 4)")
    parser.add_argument("--fuzzy", type=float, default=0.9,
                        help="report same-length functions with at least this share of equal instructions (0 to disable)")
    parser.add_argument("--include-done", action="store_true", help="also list Tooie functions already in C")
    parser.add_argument("--csv", type=Path, help="also write the results to a CSV file")
    args = parser.parse_args()

    bk_build = args.bk / "build" / args.bk_version
    bk_map = bk_build / f"banjo.{args.bk_version}.map"
    bk_rom = bk_build / f"banjo.{args.bk_version}.uncompressed.z64"
    tooie_map = ROOT / "build" / args.version / "banjotooie_decompressed.map"
    tooie_rom = ROOT / "build" / args.version / "banjotooie_decompressed.z64"
    for p in (bk_map, bk_rom, tooie_map, tooie_rom):
        if not p.exists():
            sys.exit(f"{p} not found: build both projects first")

    bk = functions(bk_map, bk_rom, ("build", args.bk_version))
    tooie = functions(tooie_map, tooie_rom, ("build", args.version))

    by_code = defaultdict(list)
    by_len = defaultdict(list)
    for name, src, _, code in bk:
        by_code[code].append((name, src))
        by_len[len(code)].append((name, src, code))

    results = []
    for name, src, rom_off, code in tooie:
        if len(code) < args.min_insns:
            continue
        is_pending = pending(name, src)
        if not is_pending and not args.include_done:
            continue
        twins = by_code.get(code)
        if twins:
            results.append((name, src, len(code), "exact", 1.0, twins, is_pending))
            continue
        if args.fuzzy > 0:
            best, best_score = None, 0.0
            for bk_name, bk_src, bk_code in by_len.get(len(code), ()):
                same = sum(a == b for a, b in zip(code, bk_code))
                score = same / len(code)
                if score > best_score:
                    best, best_score = (bk_name, bk_src), score
            if best and best_score >= args.fuzzy:
                results.append((name, src, len(code), "fuzzy", best_score, [best], is_pending))

    results.sort(key=lambda r: (r[3] != "exact", -r[2]))
    exact = [r for r in results if r[3] == "exact"]
    fuzzy = [r for r in results if r[3] == "fuzzy"]
    print(f"BK functions: {len(bk)}   Tooie functions checked: "
          f"{sum(1 for t in tooie if len(t[3]) >= args.min_insns)}")
    print(f"exact twins: {len(exact)} ({sum(r[2] for r in exact) * 4 / 1024:.1f} KB)   "
          f"fuzzy (>= {args.fuzzy:.0%}): {len(fuzzy)} ({sum(r[2] for r in fuzzy) * 4 / 1024:.1f} KB)\n")
    print(f"  {'tooie function':<34} {'insns':>5}  {'match':>6}  {'bk function':<34} bk file")
    for name, src, n, kind, score, twins, is_pending in results:
        bk_name, bk_src = twins[0]
        more = f" (+{len(twins) - 1} more)" if len(twins) > 1 else ""
        mark = "" if is_pending else " [done]"
        match = "exact" if kind == "exact" else f"{score:.0%}"
        print(f"  {name + mark:<34} {n:5}  {match:>6}  {bk_name + more:<34} {bk_src}")

    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["tooie_function", "tooie_file", "instructions", "match", "score",
                        "bk_function", "bk_file", "bk_alternatives", "tooie_pending"])
            for name, src, n, kind, score, twins, is_pending in results:
                w.writerow([name, src, n, kind, f"{score:.3f}", twins[0][0], twins[0][1],
                            " ".join(t[0] for t in twins[1:]), is_pending])
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
