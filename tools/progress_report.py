#!/usr/bin/env python3
"""How much of Banjo-Tooie is decompiled, per segment and per overlay group.

    tools/progress_report.py [-v VERSION] [--all] [--html [FILE]]

For each segment (boot, core1, core2) and each overlay group
(src/overlays/<group>):

  code    bytes of .text in functions that are done (written in C, no .s left in
          asm/nonmatchings), out of all .text bytes
  funcs   functions that are done, out of all functions
  named   functions with a real name instead of func_XXXXXXXX
  data    bytes of .data + .rodata defined in C files, out of all of it
          (the rest is still in asm/data/*.s)
  bss     bytes of .bss declared in C files, out of all of it

libultra (linked from an archive, fully matched) and hand-written asm (hasm
segments: whole files in asm/, outside nonmatchings) count as done, as in
tools/progress.py.

The assets (the file table at assets_ROM_START: models, animations, ...) are
listed by type with their share of the ROM; they are game data, not code, so
they have no "done" measure.

Reads build/<version>/banjotooie_decompressed.map (run make first),
decompressed.<version>.z64 (asset table) and baserom.<version>.z64 (ROM size). Colours when the output is a terminal (NO_COLOR=1
turns them off). --all lists every overlay group, otherwise only those with
something done. --html also writes a page (default build/progress.html).
"""

import argparse
import html
import os
import re
import struct
import sys
from pathlib import Path

import mapfile_parser

ROOT = Path(__file__).resolve().parent.parent
NONMATCHINGS = ROOT / "asm" / "nonmatchings"
UNNAMED_RE = re.compile(r"^func_[0-9A-Fa-f]{8}(_\w+)?$")
SEGMENTS = ["boot", "core1", "core2"]
# What an overlay group holds, where it is clear from the names in it.
NOTES = {"ba": "player systems", "bs": "player states (moves)", "ch": "characters and objects",
         "fx": "effects", "ml": "math and splines"}

# Asset file table (see func_800D5A6C / func_800D5B34 in core2/1EAED30): a u32
# count, then count + 1 entries. Offset = entry >> 6 from ASSETS_ROM + 0xD9A4,
# low 4 bits = type, bit 4 = compressed.
ASSETS_ROM = 0x5180
ASSET_TYPES = {0: "models", 6: "animations", 10: "code (loaded as DLL)"}


class Stats:
    FIELDS = ("code", "code_done", "funcs", "funcs_done", "named", "data", "data_done", "bss", "bss_done")

    def __init__(self):
        for k in self.FIELDS:
            setattr(self, k, 0)

    def merge(self, other):
        for k in self.FIELDS:
            setattr(self, k, getattr(self, k) + getattr(other, k))


def group_of(obj_path, version, data=False):
    """(key, rel): key like ('core2', None) or ('overlays', 'ch'), rel the path
    inside src/ or asm/ without extensions (None for libultra). key is None for
    files outside the code (assets). asm/data/ files only count with data=True:
    their .text is RSP microcode, not game code."""
    parts = list(obj_path.parts)
    if parts[:2] == ["build", version]:
        parts = parts[2:]
    if parts and parts[0].startswith("libultra"):
        return ("boot" if "_boot.a" in parts[0] else "core1", None), None
    if len(parts) > 2 and parts[0] == "asm" and parts[1] == "data":
        if not data:
            return None, None
        parts = parts[:1] + parts[2:]
    if len(parts) < 2 or parts[0] not in ("src", "asm"):
        return None, None
    rel = Path(*parts[1:])
    while rel.suffix:
        rel = rel.with_suffix("")
    if parts[1] == "overlays" and len(parts) > 3:
        return ("overlays", parts[2]), rel
    return (parts[1], None), rel


def from_c(obj_path):
    parts = obj_path.parts
    return "src" in parts[:3] or any(p.startswith("libultra") for p in parts[:3])


def collect(map_path, version):
    mapfile = mapfile_parser.MapFile()
    mapfile.readMapFile(map_path)
    groups = {}

    for segment in mapfile.filterBySectionType(".text"):
        for file in segment:
            key, rel = group_of(Path(file.filepath), version)
            if key is None:
                continue
            hand_written = rel is not None and (ROOT / "asm" / rel.with_suffix(".s")).exists()
            stats = groups.setdefault(key, Stats())
            for sym in file:
                size = sym.size or 0
                if size == 0:
                    continue
                done = rel is None or hand_written or not (NONMATCHINGS / rel / f"{sym.name}.s").exists()
                stats.code += size
                stats.funcs += 1
                stats.named += not UNNAMED_RE.match(sym.name)
                if done:
                    stats.code_done += size
                    stats.funcs_done += 1

    for section, field in ((".data", "data"), (".rodata", "data"), (".bss", "bss")):
        for segment in mapfile.filterBySectionType(section):
            for file in segment:
                path = Path(file.filepath)
                key, _ = group_of(path, version, data=True)
                if key is None or not file.size:
                    continue
                stats = groups.setdefault(key, Stats())
                setattr(stats, field, getattr(stats, field) + file.size)
                if from_c(path):
                    setattr(stats, field + "_done", getattr(stats, field + "_done") + file.size)
    return groups


def assets(rom):
    """[(type name, count, bytes)] of the asset table, biggest first."""
    count = struct.unpack_from(">I", rom, ASSETS_ROM)[0]
    entries = struct.unpack_from(f">{count + 1}I", rom, ASSETS_ROM + 4)
    by_type = {}
    for e, nxt in zip(entries, entries[1:]):
        size = (nxt >> 6) - (e >> 6)
        if size <= 0:
            continue
        n, b = by_type.get(e & 0xF, (0, 0))
        by_type[e & 0xF] = (n + 1, b + size)
    out = [(ASSET_TYPES.get(t, f"type {t} (unknown)"), n, b) for t, (n, b) in by_type.items()]
    return sorted(out, key=lambda x: -x[2])


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "    - "


def size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB"


class Style:
    def __init__(self, on):
        self.on = on

    def __call__(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bar(self, a, b, width):
        f = a / b if b else 0
        full, eighths = divmod(round(f * width * 8), 8)
        text = "█" * full + ("▏▎▍▌▋▊▉"[eighths - 1] if eighths else "")
        colour = "32" if f >= 0.999 else "33" if f >= 0.25 else "36"
        return self(text, colour) + self("·" * (width - len(text)), "2")


def rows(groups, show_all):
    """[(label, stats, indent)] in display order, and the hidden overlay groups."""
    out = [(seg, groups.get((seg, None), Stats()), False) for seg in SEGMENTS]
    overlays = Stats()
    ovl = [(k[1], s) for k, s in groups.items() if k[0] == "overlays" and k[1]]
    for _, s in ovl:
        overlays.merge(s)
    overlays.merge(groups.get(("overlays", None), Stats()))
    out.append(("overlays", overlays, False))
    hidden = []
    for name, s in sorted(ovl, key=lambda x: -x[1].code):
        if show_all or s.funcs_done or s.named or s.data_done:
            out.append((name, s, True))
        else:
            hidden.append(name)
    return out, hidden


def summary(total):
    """[(label, done, all, text)] for the top lines."""
    return [
        ("code", total.code_done, total.code, f"{size(total.code_done)} of {size(total.code)} done"),
        ("funcs", total.funcs_done, total.funcs, f"{total.funcs_done} of {total.funcs} functions done"),
        ("named", total.named, total.funcs, f"{total.named} of {total.funcs} functions named"),
        ("data", total.data_done, total.data, f"{size(total.data_done)} of {size(total.data)} of .data + .rodata in C"),
        ("bss", total.bss_done, total.bss, f"{size(total.bss_done)} of {size(total.bss)} of .bss in C"),
    ]


def terminal(groups, total, asset_rows, rom_size, show_all, st, version):
    table, hidden = rows(groups, show_all)
    lines = [st(f"Banjo-Tooie ({version.upper()}) - decomp progress", "1"),
             f"ROM {size(rom_size)} · {size(total.code)} of code in {total.funcs} functions", ""]
    for label, a, b, text in summary(total):
        lines.append(f"  {label:<7} {st.bar(a, b, 30)} {pct(a, b)}  {text}")
    lines.append("")

    lines.append(st("  assets by type, share of the ROM:", "1"))
    for name, n, b in asset_rows:
        lines.append(f"          {name:<22} {pct(b, rom_size)}  {size(b):>8}  {n:5} files")
    lines.append("")

    head = (f"  {'':12} {'code done':<27} {'KB':>15}   {'functions done':>20}   {'named':>6}"
            f"   {'data':>6}   contents")
    lines.append(st(head, "1"))
    for label, s, indent in table:
        name = ("  " if indent else "") + label
        note = NOTES.get(label, "") if indent else ""
        lines.append(f"  {name:<12} {st.bar(s.code_done, s.code, 20)} {pct(s.code_done, s.code)}"
                     f" {s.code_done / 1024:7.1f}/{s.code / 1024:7.1f}"
                     f"   {s.funcs_done:6}/{s.funcs:6} {pct(s.funcs_done, s.funcs)}"
                     f"   {s.named:6}   {pct(s.data_done, s.data)}   {note}")
    if hidden:
        lines.append(st(f"  {len(hidden)} more overlay groups with nothing done yet: "
                        f"{' '.join(sorted(hidden))} (--all to list them)", "2"))
    return "\n".join(lines)


def page(groups, total, asset_rows, rom_size, version):
    table, _ = rows(groups, True)

    def bar(a, b):
        f = a / b if b else 0
        return f'<div class="bar"><div style="width:{100 * f:.2f}%"></div></div>'

    def tr(label, s, indent=False):
        cls = ' class="sub"' if indent else ""
        note = html.escape(NOTES.get(label, "")) if indent else ""
        return (f"<tr{cls}><th>{html.escape(label)}</th><td>{bar(s.code_done, s.code)}</td>"
                f"<td>{pct(s.code_done, s.code)}</td><td>{s.code_done / 1024:.1f} / {s.code / 1024:.1f} KB</td>"
                f"<td>{s.funcs_done} / {s.funcs}</td><td>{s.named}</td><td>{pct(s.data_done, s.data)}</td>"
                f"<td>{note}</td></tr>")

    top = "".join(f"<tr><th>{k}</th><td>{bar(a, b)}</td><td>{pct(a, b)}</td><td>{html.escape(t)}</td></tr>"
                  for k, a, b, t in summary(total))
    asset_trs = "".join(f"<tr><th>{html.escape(name)}</th><td>{bar(b, rom_size)}</td><td>{pct(b, rom_size)}</td>"
                        f"<td>{size(b)}</td><td>{n} files</td></tr>" for name, n, b in asset_rows)
    body = "".join(tr(label, s, indent) for label, s, indent in table)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Banjo-Tooie progress</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1f1d1a; --muted:#6b665e; --line:#e4e0d8; --track:#ece8df; --fill:#c9761c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17161a; --fg:#ece9e4; --muted:#9a958c; --line:#2c2a30; --track:#2a282e; --fill:#e0953f; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 ui-sans-serif, system-ui, sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:980px; margin:0 auto; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:0 0 8px; }}
p {{ color:var(--muted); margin:0 0 20px; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:28px; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }}
thead th {{ color:var(--muted); font-weight:600; font-size:12px; }}
tr.sub th {{ padding-left:26px; font-weight:400; }}
.bar {{ width:200px; height:10px; background:var(--track); border-radius:5px; overflow:hidden; }}
.bar div {{ height:100%; background:var(--fill); }}
.wrap {{ overflow-x:auto; }}
</style></head><body><main>
<h1>Banjo-Tooie ({version.upper()}) - decomp progress</h1>
<p>ROM {size(rom_size)} · {size(total.code)} of code in {total.funcs} functions.</p>
<div class="wrap"><table>{top}</table></div>
<h2>Assets by type, share of the ROM</h2>
<div class="wrap"><table>{asset_trs}</table></div>
<h2>Per segment and overlay group</h2>
<div class="wrap"><table><thead><tr><th></th><th>code done</th><th></th><th>KB</th><th>functions done</th>
<th>named</th><th>data in C</th><th>contents</th></tr></thead>
<tbody>{body}</tbody></table></div>
</main></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description="Decomp progress per segment and overlay group.")
    parser.add_argument("-v", "--version", default="us")
    parser.add_argument("--all", action="store_true", help="list every overlay group")
    parser.add_argument("--html", nargs="?", const="build/progress.html", metavar="FILE",
                        help="also write an HTML page (default build/progress.html)")
    args = parser.parse_args()

    map_path = ROOT / "build" / args.version / "banjotooie_decompressed.map"
    rom_path = ROOT / f"decompressed.{args.version}.z64"
    base_path = ROOT / f"baserom.{args.version}.z64"
    for p in (map_path, rom_path, base_path):
        if not p.exists():
            sys.exit(f"{p.relative_to(ROOT)} not found: run make setup and make first")

    groups = collect(map_path, args.version)
    total = Stats()
    for s in groups.values():
        total.merge(s)
    asset_rows = assets(rom_path.read_bytes())
    rom_size = base_path.stat().st_size

    colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    print(terminal(groups, total, asset_rows, rom_size, args.all, Style(colour), args.version))
    if args.html:
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page(groups, total, asset_rows, rom_size, args.version), encoding="utf-8")
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
