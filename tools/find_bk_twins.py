#!/usr/bin/env python3
"""Find Banjo-Tooie functions that are the same code as a Banjo-Kazooie one.

    tools/find_bk_twins.py [--bk ../banjo-kazooie] [--min-insns 4]
                           [--fuzzy 0.9] [--include-done] [--csv FILE]
                           [--names] [--names-out FILE]

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

--names proposes names for Tooie symbols from the exact twins: in a twin pair
the n-th jal of the Tooie function calls the counterpart of the n-th jal of
the BK one, and the n-th lui + addiu / load / store pair addresses the same
data, and the Tooie function gets the BK function's name. Only one-to-one
twins are used: code that appears once in BK and once in Tooie. Short generic
code (a wrapper that only calls another function, say) matches many functions
on each side, and pairing those would give wrong names.
Calls between overlays go through the _<overlay>_entrypoint_N stubs of
tools/syscall_builder, so a name found for a stub goes to the function it
jumps to (<overlay>_entrypoint_N). Entrypoint names come from
overlays.us.toml, so those proposals are marked and need renaming there too.
Only generic Tooie names (func_XXXXXXXX, D_XXXXXXXX, *_entrypoint_N) get a proposal, and
only real BK names (not func_ / D_ / code_ / ...) are proposed. Proposals are
counted over all twins and listed with their votes; a symbol for which the
twins disagree is reported as a conflict. Nothing is applied: --names-out
writes the consistent proposals as symbol_addrs lines to review and paste.

Needs both projects built: build/us/banjotooie_decompressed.{map,z64} here
and build/us.v10/banjo.us.v10.{map,uncompressed.z64} in the BK decomp.
"""

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import mapfile_parser

ROOT = Path(__file__).resolve().parent.parent
NONMATCHINGS = ROOT / "asm" / "nonmatchings"
STUB_RE = re.compile(r"^_\w+_entrypoint_\d+$")
GENERIC_TOOIE_RE = re.compile(r"^(func|D|B|R|jtbl)_[0-9A-Fa-f]{8}(_\w+)?$|^\w+_entrypoint_\d+$")
ENTRYPOINT_RE = re.compile(r"^\w+_entrypoint_\d+$")
GENERIC_BK_RE = re.compile(r"^(func|D|B|code|jtbl|sub|data|unk|fn|bss)_[0-9A-Fa-f]|^\.L|_[0-9A-F]{8}$|[$/.]")
SEGMENT_SUFFIXES = ("_bss", "_header", "_data", "_rodata")

OP_J, OP_JAL, OP_ADDIU, OP_LUI = 0x02, 0x03, 0x09, 0x0F
# loads and stores whose immediate is an offset from the base register rs
MEM_OPS = {0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x28, 0x29, 0x2A, 0x2B, 0x2E,
           0x31, 0x35, 0x39, 0x3D}
REG_ZERO, REG_SP = 0, 29


def words_of(code):
    words = [int.from_bytes(code[i:i + 4], "big") for i in range(0, len(code) - len(code) % 4, 4)]
    while words and words[-1] == 0:
        words.pop()
    return words


def normalize(words):
    """Tuple of normalized instruction words (see the module docstring)."""
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


def unit_of(segment_name):
    """Group a segment with its _bss / _header parts: '.chwarppad_bss' -> 'chwarppad'."""
    name = segment_name.lstrip(".")
    for suffix in SEGMENT_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


class Symbols:
    """vram -> name lookup that understands overlays sharing addresses."""

    def __init__(self, mapfile):
        self.by_unit = defaultdict(dict)
        self.vram_of = {}
        names_at = defaultdict(set)
        for segment in mapfile:
            unit = unit_of(segment.name)
            for file in segment:
                for sym in file:
                    self.by_unit[unit].setdefault(sym.vram, sym.name)
                    self.vram_of.setdefault(sym.name, sym.vram)
                    names_at[sym.vram].add(sym.name)
        # addresses that only one symbol in the whole map uses (core code and data)
        self.unique = {vram: next(iter(n)) for vram, n in names_at.items() if len(n) == 1}

    def name(self, unit, vram):
        return self.by_unit[unit].get(vram) or self.unique.get(vram)


def functions(mapfile, rom_path, strip_prefix):
    """[dict] for every .text function: name, src, unit, vram, words, norm."""
    rom = rom_path.read_bytes()
    out = []
    for segment in mapfile:
        unit = unit_of(segment.name)
        for file in segment:
            if file.sectionType != ".text":
                continue
            path = Path(file.filepath)
            parts = path.parts[len(strip_prefix):] if path.parts[:len(strip_prefix)] == strip_prefix else path.parts
            src = Path(*parts) if parts else path
            for sym in file:
                if not sym.size or sym.vrom is None or STUB_RE.match(sym.name):
                    continue
                words = words_of(rom[sym.vrom:sym.vrom + sym.size])
                out.append({"name": sym.name, "src": src, "unit": unit, "vram": sym.vram,
                            "words": words, "norm": normalize(words)})
    return out


def references(func):
    """[(kind, vram)] of what the function calls (jal) and addresses (lui + lo), in order."""
    refs, hi = [], {}
    for i, w in enumerate(func["words"]):
        op = w >> 26
        rs, rt = (w >> 21) & 0x1F, (w >> 16) & 0x1F
        imm = w & 0xFFFF
        if op == OP_JAL:
            pc = func["vram"] + 4 * i
            refs.append(("func", (pc & 0xF0000000) | ((w & 0x03FFFFFF) << 2)))
        elif op == OP_LUI:
            hi[rt] = imm << 16
        elif (op == OP_ADDIU or op in MEM_OPS) and rs in hi:
            refs.append(("data", (hi[rs] + (imm - 0x10000 if imm & 0x8000 else imm)) & 0xFFFFFFFF))
            if op == OP_ADDIU and rt != rs:
                hi.pop(rt, None)
    return refs


OVERLAY_VRAM = 0x80800000


def overlay_of(name):
    """Overlay of a Tooie overlay symbol from its name: func_80800000_gcskyDll -> gcskyDll,
    gczoombox_entrypoint_7 -> gczoombox."""
    m = re.match(r"^\w+?_[0-9A-Fa-f]{8}_(\w+)$", name) or re.match(r"^(\w+)_entrypoint_\d+$", name)
    return m.group(1) if m else None


def pending(name, src):
    """True if this Tooie function is still a GLOBAL_ASM .s in asm/nonmatchings."""
    rel = src
    while rel.suffix:
        rel = rel.with_suffix("")
    if rel.parts and rel.parts[0] in ("src", "asm"):
        rel = Path(*rel.parts[1:])
    return (NONMATCHINGS / rel / f"{name}.s").exists()


def propose_names(pairs, tooie_syms, bk_syms):
    """{(tooie name, tooie vram): Counter(bk name)} from the one-to-one exact twins."""
    votes = defaultdict(Counter)
    tooie_count = Counter(t["norm"] for t, _ in pairs)

    def vote(t_name, t_vram, b_name):
        if t_name and STUB_RE.match(t_name) and t_name[1:] in tooie_syms.vram_of:
            t_name = t_name[1:]  # a syscall stub: name the function it jumps to
            t_vram = tooie_syms.vram_of[t_name]
        if t_name and b_name and GENERIC_TOOIE_RE.match(t_name) and not GENERIC_BK_RE.search(b_name):
            votes[(t_name, t_vram)][b_name] += 1

    for t, twins in pairs:
        if len(twins) != 1 or tooie_count[t["norm"]] != 1:
            continue
        b = twins[0]
        vote(t["name"], t["vram"], b["name"])
        t_refs, b_refs = references(t), references(b)
        if len(b_refs) != len(t_refs):
            continue
        for (kind, tv), (_, bv) in zip(t_refs, b_refs):
            vote(tooie_syms.name(t["unit"], tv), tv, bk_syms.name(b["unit"], bv))
    return votes


def main():
    parser = argparse.ArgumentParser(description="Find Tooie functions that are BK code.")
    parser.add_argument("--bk", type=Path, default=ROOT.parent / "banjo-kazooie", help="BK decomp checkout")
    parser.add_argument("--bk-version", default="us.v10")
    parser.add_argument("-v", "--version", default="us")
    parser.add_argument("--min-insns", type=int, default=4, help="skip shorter functions (default 4)")
    parser.add_argument("--fuzzy", type=float, default=0.9,
                        help="report same-length functions with at least this share of equal instructions (0 to disable)")
    parser.add_argument("--include-done", action="store_true", help="also list Tooie functions already in C")
    parser.add_argument("--csv", type=Path, help="also write the twins to a CSV file")
    parser.add_argument("--names", action="store_true", help="propose names for Tooie symbols from the exact twins")
    parser.add_argument("--names-out", type=Path, help="with --names, write the consistent proposals as symbol_addrs lines")
    args = parser.parse_args()

    bk_build = args.bk / "build" / args.bk_version
    bk_map = bk_build / f"banjo.{args.bk_version}.map"
    bk_rom = bk_build / f"banjo.{args.bk_version}.uncompressed.z64"
    tooie_map = ROOT / "build" / args.version / "banjotooie_decompressed.map"
    tooie_rom = ROOT / "build" / args.version / "banjotooie_decompressed.z64"
    for p in (bk_map, bk_rom, tooie_map, tooie_rom):
        if not p.exists():
            sys.exit(f"{p} not found: build both projects first")

    bk_mf, tooie_mf = mapfile_parser.MapFile(), mapfile_parser.MapFile()
    bk_mf.readMapFile(bk_map)
    tooie_mf.readMapFile(tooie_map)
    bk = functions(bk_mf, bk_rom, ("build", args.bk_version))
    tooie = functions(tooie_mf, tooie_rom, ("build", args.version))

    by_code = defaultdict(list)
    by_len = defaultdict(list)
    for b in bk:
        by_code[b["norm"]].append(b)
        by_len[len(b["norm"])].append(b)

    results, pairs = [], []
    for t in tooie:
        n = len(t["norm"])
        if n < args.min_insns:
            continue
        is_pending = pending(t["name"], t["src"])
        twins = by_code.get(t["norm"])
        if twins:
            pairs.append((t, twins))
        if not is_pending and not args.include_done:
            continue
        if twins:
            results.append((t, n, "exact", 1.0, twins, is_pending))
            continue
        if args.fuzzy > 0:
            best, best_score = None, 0.0
            for b in by_len.get(n, ()):
                score = sum(x == y for x, y in zip(t["norm"], b["norm"])) / n
                if score > best_score:
                    best, best_score = b, score
            if best and best_score >= args.fuzzy:
                results.append((t, n, "fuzzy", best_score, [best], is_pending))

    results.sort(key=lambda r: (r[2] != "exact", -r[1]))
    exact = [r for r in results if r[2] == "exact"]
    fuzzy = [r for r in results if r[2] == "fuzzy"]
    print(f"BK functions: {len(bk)}   Tooie functions checked: "
          f"{sum(1 for t in tooie if len(t['norm']) >= args.min_insns)}")
    print(f"exact twins: {len(exact)} ({sum(r[1] for r in exact) * 4 / 1024:.1f} KB)   "
          f"fuzzy (>= {args.fuzzy:.0%}): {len(fuzzy)} ({sum(r[1] for r in fuzzy) * 4 / 1024:.1f} KB)\n")

    if not args.names:
        print(f"  {'tooie function':<34} {'insns':>5}  {'match':>6}  {'bk function':<34} bk file")
        for t, n, kind, score, twins, is_pending in results:
            more = f" (+{len(twins) - 1} more)" if len(twins) > 1 else ""
            mark = "" if is_pending else " [done]"
            match = "exact" if kind == "exact" else f"{score:.0%}"
            print(f"  {t['name'] + mark:<34} {n:5}  {match:>6}  {twins[0]['name'] + more:<34} {twins[0]['src']}")

    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["tooie_function", "tooie_file", "instructions", "match", "score",
                        "bk_function", "bk_file", "bk_alternatives", "tooie_pending"])
            for t, n, kind, score, twins, is_pending in results:
                w.writerow([t["name"], t["src"], n, kind, f"{score:.3f}", twins[0]["name"], twins[0]["src"],
                            " ".join(b["name"] for b in twins[1:]), is_pending])
        print(f"wrote {args.csv}")

    if args.names:
        votes = propose_names(pairs, Symbols(tooie_mf), Symbols(bk_mf))
        consistent = {k: c for k, c in votes.items() if len(c) == 1}
        conflicts = {k: c for k, c in votes.items() if len(c) > 1}
        # a BK name proposed for several Tooie symbols is not safe to apply as is
        uses = Counter(next(iter(c)) for c in consistent.values())
        print(f"name proposals: {len(consistent)} consistent, {len(conflicts)} with conflicting votes\n")
        print(f"  {'tooie symbol':<34} {'address':>10}  {'votes':>5}  proposed name")
        for (t_name, vram), c in sorted(consistent.items(), key=lambda kv: (-sum(kv[1].values()), kv[0][1])):
            b_name, n = next(iter(c.items()))
            note = f"  (also proposed for {uses[b_name] - 1} other)" if uses[b_name] > 1 else ""
            if ENTRYPOINT_RE.match(t_name):
                note += "  [entrypoint: rename in overlays.us.toml]"
            print(f"  {t_name:<34} 0x{vram:08X}  {n:5}  {b_name}{note}")
        if conflicts:
            print("\n  conflicts:")
            for (t_name, vram), c in sorted(conflicts.items(), key=lambda kv: kv[0][1]):
                print(f"  {t_name:<34} 0x{vram:08X}  " + ", ".join(f"{b} x{n}" for b, n in c.most_common()))
        if args.names_out:
            with args.names_out.open("w") as f:
                for (t_name, vram), c in sorted(consistent.items(), key=lambda kv: kv[0][1]):
                    b_name = next(iter(c))
                    if uses[b_name] == 1:
                        prefix = "// entrypoint, rename in overlays.us.toml: " if ENTRYPOINT_RE.match(t_name) else ""
                        segment = overlay_of(t_name) if vram >= OVERLAY_VRAM else None
                        where = f" segment:{segment}" if segment else ""
                        f.write(f"{prefix}{b_name} = 0x{vram:08X}; //{where} was {t_name}, from find_bk_twins.py\n")
            print(f"\nwrote {args.names_out} (consistent proposals whose name is used once)")


if __name__ == "__main__":
    main()
