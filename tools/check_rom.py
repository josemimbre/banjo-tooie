#!/usr/bin/env python3
"""Check that the built compressed ROM matches the base ROM.

rom_compressor does not write the 0xFF padding that fills the original
cartridge image up to its full size, so the built ROM may be shorter than
the base ROM. It matches when every byte it has equals the base ROM and the
rest of the base ROM is only 0xFF padding.

Prints one line describing the result. Exit code is 0 on match, 1 otherwise.

Usage: tools/check_rom.py BUILT_ROM BASE_ROM
"""

import hashlib
import sys
from pathlib import Path


def first_diff(a, b):
    """Offset of the first differing byte of two equal-length buffers, or None."""
    step = 1 << 16
    for start in range(0, len(a), step):
        if a[start:start + step] != b[start:start + step]:
            return next(i for i in range(start, start + step) if a[i] != b[i])
    return None


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    built_path, base_path = map(Path, sys.argv[1:])
    built, base = built_path.read_bytes(), base_path.read_bytes()

    if len(built) > len(base):
        print(f"{built_path} is larger than {base_path} (0x{len(built):X} > 0x{len(base):X} bytes)")
        sys.exit(1)

    diff = first_diff(built, base[:len(built)])
    if diff is not None:
        print(f"{built_path} differs from {base_path} starting at ROM offset 0x{diff:X}")
        sys.exit(1)

    tail = base[len(built):]
    if tail.strip(b"\xff"):
        print(f"{built_path} is missing data: {base_path} has non-padding bytes after 0x{len(built):X}")
        sys.exit(1)

    padded_sha1 = hashlib.sha1(built + tail).hexdigest()
    note = f" (+0x{len(tail):X} bytes of 0xFF padding)" if tail else ""
    print(f"{built_path} matches {base_path}{note}, sha1 {padded_sha1}")


if __name__ == "__main__":
    main()
