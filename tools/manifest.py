#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Validate cartridge metadata and generate a game without modifying the source ROM."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
REGIONS = {'auto': 'Auto', 'ntsc': 'NTSC', 'pal': 'PAL', 'dendy': 'Dendy'}
BATTERY_MAPPERS = {0, 1, 4}


def inspect(data):
    if len(data) < 16 or data[:4] != b'NES\x1a':
        raise ValueError('Expected an iNES or NES 2.0 cartridge (.nes).')
    h = data[:16]
    nes2 = h[7] & 12 == 8
    if h[7] & 3:
        raise ValueError('VS System, PlayChoice and extended console types are unsupported.')
    if not nes2 and (h[7] & 12 or any(h[12:])):
        raise ValueError('Ambiguous or dirty iNES header; use a correctly identified dump.')
    mapper = h[6] >> 4 | h[7] & 240 | ((h[8] & 15) << 8 if nes2 else 0)
    submapper = h[8] >> 4 if nes2 else 0
    def size(low, high, unit):
        return (1 << (low >> 2)) * ((low & 3) * 2 + 1) if high == 15 else (low | high << 8) * unit
    prg = size(h[4], h[9] & 15 if nes2 else 0, 16384)
    chr_size = size(h[5], h[9] >> 4 if nes2 else 0, 8192)
    if not prg or prg + chr_size > 32 * 1024 * 1024:
        raise ValueError('Empty or oversized cartridge; maximum PRG+CHR size is 32 MiB.')
    trainer = 512 if h[6] & 4 else 0
    if len(data) != 16 + trainer + prg + chr_size:
        raise ValueError('ROM length does not match the header (truncated, extra data or unsupported format).')
    if nes2 and (h[14] or h[15] not in (0, 1)):
        raise ValueError('Miscellaneous ROMs and special input peripherals are unsupported.')
    battery = bool(h[6] & 2)
    if nes2 and h[11] & 240:
        raise ValueError('Persistent CHR memory is not supported by this frontend.')
    if battery and (mapper not in BATTERY_MAPPERS or submapper):
        raise ValueError('Battery persistence is currently supported for standard mapper 0, 1 and 4 cartridges only.')
    return dict(format='NES2' if nes2 else 'iNES', mapper=mapper, submapper=submapper,
                battery=battery, prg_bytes=prg, chr_bytes=chr_size,
                timing_hint=('NTSC', 'PAL', 'multi-region', 'Dendy')[h[12] & 3] if nes2 else ('PAL' if h[9] & 1 else 'unspecified/NTSC'))


def read_manifest():
    path = ROOT / 'game.json'
    if not path.is_file():
        raise ValueError('Run make init ROM=/absolute/path/game.nes first.')
    game = json.loads(path.read_text())
    if not isinstance(game.get('id'), str) or not re.fullmatch(r'nes-[a-z0-9]+(?:-[a-z0-9]+)*', game['id']) or len(game['id']) > 80:
        raise ValueError('Game id must start with nes- and use lowercase dash-separated words (maximum 80 characters).')
    if not isinstance(game.get('title'), str) or not game['title'].isprintable() or not 1 <= len(game['title']) <= 120:
        raise ValueError('Title must contain 1–120 printable characters.')
    if game.get('region') not in REGIONS.values():
        raise ValueError('Region must be Auto, NTSC, PAL or Dendy.')
    rom = (ROOT / game['rom']).resolve()
    if not rom.is_relative_to((ROOT / 'roms').resolve()) or rom.suffix.lower() != '.nes':
        raise ValueError('ROM must be a .nes file inside roms/.')
    data = rom.read_bytes()
    if hashlib.sha256(data).hexdigest() != game.get('sha256'):
        raise ValueError('ROM checksum mismatch; original bytes must be preserved.')
    metadata = inspect(data)
    if any(game.get(key) != value for key, value in metadata.items()):
        raise ValueError('Manifest metadata disagrees with ROM header; restore the generated metadata.')
    return game, data


def initialize():
    source = Path(os.environ.get('ROM', '')).expanduser()
    if not source.is_file():
        raise ValueError('Set ROM=/absolute/path/to/a/game.nes.')
    if source.suffix.lower() != '.nes' or any(ord(c) < 32 for c in source.name):
        raise ValueError('Expected a .nes filename without control characters.')
    data = source.read_bytes(); metadata = inspect(data)
    slug = re.sub('[^a-z0-9]+', '-', source.stem.lower()).strip('-')
    identity = os.environ.get('GAME_ID') or ('nes-' + slug if not slug.startswith('nes-') else slug)
    title = os.environ.get('TITLE') or source.stem.replace('-', ' ').replace('_', ' ').title()
    region = REGIONS.get((os.environ.get('REGION') or 'auto').lower())
    game = dict(id=identity, title=title, rom='roms/' + source.name,
                sha256=hashlib.sha256(data).hexdigest(), region=region, **metadata)
    if not re.fullmatch(r'nes-[a-z0-9]+(?:-[a-z0-9]+)*', identity) or len(identity) > 80 or not region:
        raise ValueError('Invalid GAME_ID or REGION (use auto, ntsc, pal or dendy).')
    if not title.isprintable() or not 1 <= len(title) <= 120:
        raise ValueError('TITLE must contain 1–120 printable characters.')
    with (ROOT / '.init.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ROOT / 'game.json').exists():
            existing, _ = read_manifest()
            if existing != game:
                raise ValueError('This checkout is initialized for another configuration. Use a fresh template copy.')
            print(f"Already initialized: {identity}"); return
        destination = ROOT / game['rom']; destination.parent.mkdir(exist_ok=True)
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError('Destination ROM exists with different bytes; preserved.')
        if not destination.exists():
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as f:
                temporary = Path(f.name); f.write(data)
            temporary.replace(destination)
        launcher = ROOT / ('launch-' + identity)
        if launcher.exists():
            raise ValueError('Launcher already exists; preserved.')
        launcher.write_text('#!/usr/bin/env bash\nset -euo pipefail\nroot=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\nexec bash "$root/tools/run-game" "$@"\n')
        launcher.chmod(0o755)
        (ROOT / 'game.json.tmp').write_text(json.dumps(game, indent=2) + '\n')
        (ROOT / 'game.json.tmp').replace(ROOT / 'game.json')
        print(f"Initialized {identity}: mapper {metadata['mapper']}, region {region}, battery={metadata['battery']}")
        print(f'ROM copied unchanged: {destination}')


def inputs():
    game, data = read_manifest()
    digest = hashlib.sha256()
    files = sorted([* (ROOT / 'static').glob('*.c'), * (ROOT / 'static').glob('*.h'),
                    * (ROOT / 'static').glob('*.lock'), * (ROOT / 'tools').glob('*'),
                    ROOT / 'game.json', ROOT / '.python-version', ROOT / 'pyproject.toml', ROOT / 'uv.lock'])
    for path in files:
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def header(directory, revision):
    game, data = read_manifest(); directory = Path(directory)
    defines = {'GAME_ID':game['id'], 'GAME_TITLE':game['title'], 'GAME_REGION':game['region'],
               'ROM_FILENAME':Path(game['rom']).name, 'ROM_SHA256':game['sha256'], 'CORE_REVISION':revision}
    text = ''.join(f'#define {key} {json.dumps(value, ensure_ascii=False)}\n' for key, value in defines.items())
    text += f'#define GAME_MAPPER {game["mapper"]}\n#define GAME_BATTERY {int(game["battery"])}\n'
    (directory / 'game-identity.h').write_text(text)
    text = 'static const unsigned char embedded_rom[] = {\n'
    text += '\n'.join(','.join(f'0x{v:02x}' for v in data[i:i+24])+',' for i in range(0,len(data),24))
    (directory / 'game-data.h').write_text(text + '\n};\n')


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'check'
    if command == 'init': initialize()
    elif command == 'inputs': print(inputs())
    elif command == 'header': header(*sys.argv[2:])
    elif command == 'value': print(read_manifest()[0][sys.argv[2]])
    elif command == 'check': print(json.dumps(read_manifest()[0], indent=2))
    else: raise ValueError('Unknown manifest command.')

if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError) as error: sys.exit(f'Error: {error}')
