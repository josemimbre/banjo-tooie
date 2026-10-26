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

The whole batch is refused when a NEW name is not a valid identifier or
appears twice. Single renames are skipped, and listed at the end, when NEW
is already used in the project (comments do not count), when OLD is not
found, or when OLD is a generic name whose address already has another name
(in the symbol files or in build/us/banjotooie_decompressed.map from the
last build).
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
        """Identifiers used in the project, ignoring comments."""
        found = set()
        for path, t in self.text.items():
            t = re.sub(r"/\*.*?\*/", " ", t, flags=re.S)
            t = re.sub(r"//[^\n]*", " ", t)
            if path == TOML:
                t = re.sub(r"#[^\n]*", " ", t)
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


def map_names():
    """{(segment or None, vram): name} from the last build's map, if there is one."""
    path = ROOT / "build" / "us" / "banjotooie_decompressed.map"
    if not path.exists():
        return {}
    import mapfile_parser
    mapfile = mapfile_parser.MapFile()
    mapfile.readMapFile(path)
    names = {}
    for segment in mapfile:
        seg = segment.name.lstrip(".")
        for suffix in ("_bss", "_header"):
            seg = seg[:-len(suffix)] if seg.endswith(suffix) else seg
        for file in segment:
            for sym in file:
                key = (seg if sym.vram >= OVERLAY_VRAM else None, sym.vram)
                names.setdefault(key, sym.name)
    return names


def address_names(project, vram, segment, built):
    """Names already given to vram (in that overlay segment, for overlays)."""
    pattern = re.compile(rf"^\s*(\w+)\s*=\s*0x0*{vram:X};(.*)$", re.M | re.I)
    names = []
    for f in SYMBOL_FILES:
        for name, rest in pattern.findall(project.text[f]):
            if vram < OVERLAY_VRAM or (segment and f"segment:{segment}" in rest):
                names.append(name)
    name = built.get((segment if vram >= OVERLAY_VRAM else None, vram))
    if name and not GENERIC_RE.match(name):
        names.append(name)
    return names


def rename(project, old, new, log, built):
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
        taken = address_names(project, vram, m.group(2), built)
        if taken:
            log.append(f"address 0x{vram:08X} is already named {', '.join(taken)}")
            return False
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
        elif new in seen:
            errors.append(f"{new}: given for both {seen[new]} and {old}")
        seen[new] = old
    if errors:
        sys.exit("refusing to rename:\n  " + "\n  ".join(errors))

    built = map_names()
    rebuild_tools, done, skipped = False, 0, []
    for old, new in pairs:
        if new in used:
            skipped.append(f"{old} -> {new}: {new} is already used in the project")
            continue
        if old not in used and not GENERIC_RE.match(old):
            skipped.append(f"{old} -> {new}: {old} not found")
            continue
        log, before = [], dict(project.text)
        if not rename(project, old, new, log, built):
            project.text = before
            skipped.append(f"{old} -> {new}: " + (log[-1] if log else "no symbol definition found"))
            continue
        done += 1
        used.add(new)
        rebuild_tools |= any(SYSCALL_BUILDER in l for l in log)
        print(f"{old} -> {new}")
        for line in log:
            print(f"    {line}")
    if skipped:
        print(f"\nskipped {len(skipped)}:")
        for line in skipped:
            print(f"    {line}")

    changed = project.changed()
    if args.dry_run:
        print(f"\ndry run: {len(changed)} files would change, nothing written")
        return
    project.write()
    print(f"\n{done} renamed, {len(changed)} files changed. Next: "
          + ("make -C tools && " if rebuild_tools else "") + "make split && make check")


if __name__ == "__main__":
    main()
