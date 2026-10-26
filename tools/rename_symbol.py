#!/usr/bin/env python3
"""Rename symbols everywhere the project stores their names.

    tools/rename_symbol.py [--dry-run] OLD NEW [OLD NEW ...]
    tools/rename_symbol.py [--dry-run] [--entrypoints] --file FILE

A name lives in several places, and missing one breaks the build:

  symbol files     symbol_addrs / ovl_symbol_addrs / syscall_symbol_addrs /
                   undefined_syms .us.txt: the "OLD = 0x...;" line is renamed
                   and keeps its comment. A generic name with no line yet
                   (func_800C1414, func_80800000_gcmapDll) gets one: core
                   symbols in symbol_addrs, overlay ones in ovl_symbol_addrs
                   with "// segment:<overlay>".
  relocations      reloc_addrs / ovl_reloc_addrs .us.txt ("symbol:OLD"), where
                   splat takes the names in overlay code from
  overlays.us.toml the entrypoint lists. Renaming <overlay>_entrypoint_N also
                   renames its syscall stub _<overlay>_entrypoint_N, and the
                   xori_entrypoints list in tools/syscall_builder.cpp (then
                   rebuild it: make -C tools)
  source           .c, .h and .s files in src/ and include/ (the .bss of
                   core1 and core2 is declared in .s files in src/)

asm/ is generated, so it is not touched: run `make split` afterwards, then
`make check`.

FILE has one rename per line, "OLD NEW" (or tab separated), or is the
output of `tools/find_bk_twins.py --names-out`; there the commented
entrypoint lines are only used with --entrypoints.

A rename is refused when NEW is not a valid identifier, is already used
anywhere in the project, or appears twice, or when OLD is not found.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYMBOL_FILES = ["symbol_addrs.us.txt", "ovl_symbol_addrs.us.txt", "syscall_symbol_addrs.us.txt",
                "undefined_syms.us.txt"]
RELOC_FILES = ["reloc_addrs.us.txt", "ovl_reloc_addrs.us.txt"]
TOML = "overlays.us.toml"
SYSCALL_BUILDER = "tools/syscall_builder.cpp"
SOURCE_GLOBS = ["src/**/*.c", "src/**/*.h", "src/**/*.s", "include/**/*.h"]

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GENERIC_RE = re.compile(r"^(?:func|D|B|R)_([0-9A-Fa-f]{8})(?:_(\w+))?$")
NAMES_OUT_RE = re.compile(r"^(//[^:]*:\s*)?(\w+)\s*=\s*0x[0-9A-Fa-f]+;.*\bwas (\w+)")
OVERLAY_VRAM = 0x80800000


def read_pairs(args):
    pairs = []
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            if not line.strip() or (line.startswith("#")):
                continue
            m = NAMES_OUT_RE.match(line)
            if m:
                if m.group(1) and not args.entrypoints:
                    continue
                pairs.append((m.group(3), m.group(2)))
            elif line.startswith("//"):
                continue
            else:
                old, new = line.split()[:2]
                pairs.append((old, new))
    if len(args.names) % 2:
        sys.exit("give names as OLD NEW pairs")
    pairs += list(zip(args.names[::2], args.names[1::2]))
    return pairs


class Project:
    """All the text files that hold names, read once and written at the end."""

    def __init__(self):
        paths = SYMBOL_FILES + RELOC_FILES + [TOML, SYSCALL_BUILDER]
        for pattern in SOURCE_GLOBS:
            paths += sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern))
        self.text = {p: (ROOT / p).read_text(encoding="latin1") for p in dict.fromkeys(paths)}
        self.original = dict(self.text)

    def words(self):
        found = set()
        for t in self.text.values():
            found.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", t))
        return found

    def sub(self, path, pattern, repl):
        new, n = re.subn(pattern, repl, self.text[path], flags=re.M)
        self.text[path] = new
        return n

    def append(self, path, line):
        t = self.text[path]
        if t and not t.endswith("\n"):
            t += "\n"
        self.text[path] = t + line + "\n"

    def changed(self):
        return [p for p in self.text if self.text[p] != self.original[p]]

    def write(self):
        for p in self.changed():
            (ROOT / p).write_text(self.text[p], encoding="latin1")


def rename(project, old, new, log):
    is_entrypoint = f'"{old}"' in project.text[TOML]
    names = [(old, new)] + ([("_" + old, "_" + new)] if is_entrypoint else [])
    defined = False
    for o, n in names:
        for f in SYMBOL_FILES:
            if project.sub(f, rf"^(\s*){re.escape(o)}(\s*=)", rf"\g<1>{n}\g<2>"):
                defined = True
                log.append(f"{f}: {o} -> {n}")
        for f in RELOC_FILES:
            c = project.sub(f, rf"\bsymbol:{re.escape(o)}\b", f"symbol:{n}")
            if c:
                log.append(f"{f}: {c} relocations")
        for f, text in list(project.text.items()):
            if f in SYMBOL_FILES or f in RELOC_FILES or f in (TOML, SYSCALL_BUILDER):
                continue
            c = project.sub(f, rf"\b{re.escape(o)}\b", n)
            if c:
                log.append(f"{f}: {c}x {o}")
    if is_entrypoint:
        project.sub(TOML, rf'"{re.escape(old)}"', f'"{new}"')
        log.append(f"{TOML}: entrypoint {old} -> {new}")
        if project.sub(SYSCALL_BUILDER, rf'"{re.escape(old)}"', f'"{new}"'):
            log.append(f"{SYSCALL_BUILDER}: xori_entrypoints (rebuild: make -C tools)")
    if not defined:
        m = GENERIC_RE.match(old)
        if not m:
            return False
        vram = int(m.group(1), 16)
        if vram >= OVERLAY_VRAM:
            if not m.group(2):
                return False
            project.append("ovl_symbol_addrs.us.txt", f"{new} = 0x{vram:08X}; // segment:{m.group(2)} (was {old})")
            log.append(f"ovl_symbol_addrs.us.txt: added {new} (segment {m.group(2)})")
        else:
            project.append("symbol_addrs.us.txt", f"{new} = 0x{vram:08X}; // (was {old})")
            log.append(f"symbol_addrs.us.txt: added {new}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Rename symbols everywhere the project stores their names.")
    parser.add_argument("names", nargs="*", help="OLD NEW pairs")
    parser.add_argument("--file", help="file with renames (OLD NEW lines or find_bk_twins.py --names-out output)")
    parser.add_argument("--entrypoints", action="store_true", help="with --file, also apply the commented entrypoint lines")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()

    pairs = read_pairs(args)
    if not pairs:
        parser.error("nothing to rename")

    project = Project()
    used = project.words()
    errors, seen = [], {}
    for old, new in pairs:
        if not IDENT_RE.match(new):
            errors.append(f"{new}: not a valid identifier")
        elif new in used:
            errors.append(f"{new}: already used in the project (for {old})")
        elif new in seen:
            errors.append(f"{new}: given for both {seen[new]} and {old}")
        elif old not in used and not GENERIC_RE.match(old):
            errors.append(f"{old}: not found")
        seen[new] = old
    if errors:
        sys.exit("refusing to rename:\n  " + "\n  ".join(errors))

    rebuild_tools = False
    for old, new in pairs:
        log = []
        if not rename(project, old, new, log):
            sys.exit(f"{old}: no symbol definition found and not a generic name; nothing written")
        rebuild_tools |= any(SYSCALL_BUILDER in l for l in log)
        print(f"{old} -> {new}")
        for line in log:
            print(f"    {line}")

    changed = project.changed()
    if args.dry_run:
        print(f"\ndry run: {len(changed)} files would change, nothing written")
        return
    project.write()
    print(f"\n{len(pairs)} renamed, {len(changed)} files changed. Next: "
          + ("make -C tools && " if rebuild_tools else "") + "make split && make check")


if __name__ == "__main__":
    main()
