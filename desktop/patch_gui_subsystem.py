#!/usr/bin/env python3
"""Change a Windows PE executable from Console to Windows GUI subsystem."""

from __future__ import annotations

from pathlib import Path
import struct
import sys


IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3


def patch(path: Path) -> None:
    image = bytearray(path.read_bytes())
    if len(image) < 0x40 or image[:2] != b"MZ":
        raise ValueError(f"{path} is not a DOS/PE executable")
    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    if image[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError(f"{path} has no PE signature")
    optional_header = pe_offset + 4 + 20
    magic = struct.unpack_from("<H", image, optional_header)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"{path} has unsupported optional-header magic {magic:#x}")
    subsystem_offset = optional_header + 68
    subsystem = struct.unpack_from("<H", image, subsystem_offset)[0]
    if subsystem not in (IMAGE_SUBSYSTEM_WINDOWS_GUI, IMAGE_SUBSYSTEM_WINDOWS_CUI):
        raise ValueError(f"{path} has unexpected subsystem {subsystem}")
    struct.pack_into("<H", image, subsystem_offset, IMAGE_SUBSYSTEM_WINDOWS_GUI)
    path.write_bytes(image)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_gui_subsystem.py <executable>")
    patch(Path(sys.argv[1]).resolve())
