#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Apply the source adjustments required by the static library builds."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('component', choices=['core', 'pulse', 'wayland'])
parser.add_argument('path', type=Path)
parser.add_argument('--cc')
args = parser.parse_args()

if args.component == 'core':
    path = args.path / 'Makefile.common'
    path.write_text(path.read_text().replace('ifneq ($(STATIC_LINKING), 1)', 'ifneq ($(STATIC_LINKING), never)'))
elif args.component == 'pulse':
    for name in ('src/meson.build', 'src/pulse/meson.build'):
        path = args.path / name
        path.write_text(path.read_text().replace('shared_library(', 'library('))
else:
    if not args.cc:
        parser.error('--cc is required for the Wayland cross file')
    args.path.write_text(f"""[binaries]
c = {args.cc!r}
ar = 'ar'
strip = 'strip'
pkg-config = 'pkg-config'
[host_machine]
system = 'linux'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
""")
