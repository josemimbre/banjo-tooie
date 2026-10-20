#!/usr/bin/env python3
"""Check individual functions against the original ROM.

For each function name given, reports:
  - whether it is written in C or still included through GLOBAL_ASM
  - whether its object file is older than its source (stale build)
  - whether its bytes in the built ROM match the original ROM, and if not,
    which instructions differ

Run `make` first. Exit code is non-zero if any function fails.

Usage: tools/check_func.py [-v VERSION] FUNC [FUNC ...]
       tools/check_func.py [-v VERSION] --file src/core2/1ECA640.c
"""

import argparse
import re
import sys
from pathlib import Path

import mapfile_parser
import rabbitizer

GLOBAL_ASM_RE = re.compile(r'#pragma\s+GLOBAL_ASM\("[^"]*/([^/"]+)\.s"\)')
C_FUNC_RE = re.compile(r'^[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)\s*\([^;{]*\)\s*\{', re.MULTILINE)


def c_functions(c_path):
    return C_FUNC_RE.findall(c_path.read_text(encoding="latin1"))


def disasm(word):
    return rabbitizer.Instruction(word).disassemble()


def check(name, mapfile, built, expected):
    found = mapfile.findSymbolByName(name)
    if found is None:
        print(f"{name}: not found in map file")
        return False

    sym = found.symbol
    obj = Path(found.section.filepath)
    src = Path(*obj.parts[2:]).with_suffix("") if obj.parts[:1] == ("build",) else None

    ok = True
    kind = "?"
    if src is not None and src.suffix == ".c" and src.exists():
        text = src.read_text(encoding="latin1")
        kind = "ASM" if name in GLOBAL_ASM_RE.findall(text) else "C"
        if obj.exists() and obj.stat().st_mtime < src.stat().st_mtime:
            print(f"{name}: STALE, {obj} is older than {src}; run make")
            ok = False

    if sym.vrom is None or sym.size is None:
        print(f"{name}: no ROM address/size in map file")
        return False

    start, size = sym.vrom, sym.size
    a, b = built[start:start + size], expected[start:start + size]
    diffs = [i for i in range(0, size - size % 4, 4) if a[i:i + 4] != b[i:i + 4]]

    status = "OK" if not diffs else f"MISMATCH ({len(diffs)}/{size // 4} instructions)"
    print(f"{name}: {kind:3} 0x{sym.vram:08X} rom 0x{start:X} {size:4} bytes  {status}  [{src or obj}]")
    for i in diffs[:10]:
        wa = int.from_bytes(a[i:i + 4], "big")
        wb = int.from_bytes(b[i:i + 4], "big")
        print(f"    +0x{i:03X}  built {wa:08X} {disasm(wa):32}  expected {wb:08X} {disasm(wb)}")
    return ok and not diffs


def main():
    parser = argparse.ArgumentParser(description="Check functions byte by byte against the original ROM.")
    parser.add_argument("funcs", nargs="*", help="function names")
    parser.add_argument("--file", type=Path, help="check every function written in C in this source file")
    parser.add_argument("-v", "--version", default="us")
    args = parser.parse_args()

    names = list(args.funcs)
    if args.file:
        names += c_functions(args.file)
    if not names:
        parser.error("give function names or --file")

    build = Path("build") / args.version
    built_rom = build / "banjotooie_decompressed.z64"
    map_path = build / "banjotooie_decompressed.map"
    expected_rom = Path(f"decompressed.{args.version}.z64")

    mapfile = mapfile_parser.MapFile()
    mapfile.readMapFile(map_path)
    built = built_rom.read_bytes()
    expected = expected_rom.read_bytes()

    results = [check(n, mapfile, built, expected) for n in names]
    print(f"\n{sum(results)}/{len(results)} OK")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
