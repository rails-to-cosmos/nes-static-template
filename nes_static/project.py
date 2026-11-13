"""Cartridge identity, independent game checkouts and generated C headers."""
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import cast

from .support import Error, atomic_write, file_hash, locked


class Region(StrEnum):
    AUTO = 'Auto'
    NTSC = 'NTSC'
    PAL = 'PAL'
    DENDY = 'Dendy'

    @classmethod
    def parse(cls, value: str) -> 'Region':
        for region in cls:
            if region.value.lower() == value.lower():
                return region
        raise Error('Region must be auto, ntsc, pal or dendy.')


@dataclass(frozen=True)
class CartridgeInfo:
    format: str
    mapper: int
    submapper: int
    battery: bool
    prg_bytes: int
    chr_bytes: int
    timing_hint: str


def inspect(data: bytes) -> CartridgeInfo:
    if len(data) < 16 or data[:4] != b'NES\x1a':
        raise Error('Expected an iNES or NES 2.0 cartridge (.nes).')
    h = data[:16]
    nes2 = h[7] & 12 == 8
    if h[7] & 3:
        raise Error('VS System, PlayChoice and extended console types are unsupported.')
    if not nes2 and (h[7] & 12 or any(h[12:])):
        raise Error('Ambiguous or dirty iNES header; use a correctly identified dump.')
    mapper = h[6] >> 4 | h[7] & 240 | ((h[8] & 15) << 8 if nes2 else 0)
    submapper = h[8] >> 4 if nes2 else 0

    def size(low: int, high: int, unit: int) -> int:
        return (1 << (low >> 2)) * ((low & 3) * 2 + 1) if high == 15 else (low | high << 8) * unit

    prg = size(h[4], h[9] & 15 if nes2 else 0, 16384)
    chr_size = size(h[5], h[9] >> 4 if nes2 else 0, 8192)
    if not prg or prg + chr_size > 32 * 1024 * 1024:
        raise Error('Empty or oversized cartridge; maximum PRG+CHR size is 32 MiB.')
    trainer = 512 if h[6] & 4 else 0
    if len(data) != 16 + trainer + prg + chr_size:
        raise Error('ROM length does not match the header (truncated, extra data or unsupported format).')
    if nes2 and (h[14] or h[15] not in (0, 1)):
        raise Error('Miscellaneous ROMs and special input peripherals are unsupported.')
    battery = bool(h[6] & 2)
    if nes2 and h[11] & 240:
        raise Error('Persistent CHR memory is not supported by this frontend.')
    if battery and (mapper not in {0, 1, 4} or submapper):
        raise Error('Battery persistence is currently supported for standard mapper 0, 1 and 4 cartridges only.')
    timing = ('NTSC', 'PAL', 'multi-region', 'Dendy')[h[12] & 3] if nes2 else ('PAL' if h[9] & 1 else 'unspecified/NTSC')
    return CartridgeInfo('NES2' if nes2 else 'iNES', mapper, submapper, battery, prg, chr_size, timing)


@dataclass(frozen=True)
class GameManifest:
    id: str
    title: str
    rom: str
    sha256: str
    region: Region
    cartridge: CartridgeInfo

    def __post_init__(self) -> None:
        if not re.fullmatch(r'nes-[a-z0-9]+(?:-[a-z0-9]+)*', self.id) or len(self.id) > 80:
            raise Error('Game id must start with nes- and use lowercase dash-separated words (maximum 80 characters).')
        if not self.title.isprintable() or not 1 <= len(self.title) <= 120:
            raise Error('Title must contain 1–120 printable characters.')

    def to_dict(self) -> dict[str, object]:
        return dict(id=self.id, title=self.title, rom=self.rom, sha256=self.sha256,
                    region=self.region.value, **asdict(self.cartridge))

    def json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + '\n'


@dataclass(frozen=True)
class LoadedGame:
    manifest: GameManifest
    data: bytes

    @classmethod
    def load(cls, root: Path) -> 'LoadedGame':
        path = root / 'game.json'
        if not path.is_file():
            raise Error('Run make init ROM=/absolute/path/game.nes first.')
        value: object = json.loads(path.read_text())
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise Error('Manifest must be a JSON object.')
        fields = cast(dict[str, object], value)

        def text(name: str) -> str:
            result = fields.get(name)
            if not isinstance(result, str):
                raise Error(f'Manifest field {name} must be a string.')
            return result

        relative = text('rom')
        rom = (root / relative).resolve()
        if not rom.is_relative_to((root / 'roms').resolve()) or rom.suffix.lower() != '.nes':
            raise Error('ROM must be a .nes file inside roms/.')
        data = rom.read_bytes()
        if hashlib.sha256(data).hexdigest() != text('sha256'):
            raise Error('ROM checksum mismatch; original bytes must be preserved.')
        info = inspect(data)
        if any(fields.get(key) != item for key, item in asdict(info).items()):
            raise Error('Manifest metadata disagrees with ROM header; restore the generated metadata.')
        manifest = GameManifest(text('id'), text('title'), relative, text('sha256'), Region(text('region')), info)
        return cls(manifest, data)

    def headers(self, directory: Path, revision: str) -> None:
        game = self.manifest
        defines = dict(GAME_ID=game.id, GAME_TITLE=game.title, GAME_REGION=game.region.value,
                       ROM_FILENAME=Path(game.rom).name, ROM_SHA256=game.sha256, CORE_REVISION=revision)
        text = ''.join(f'#define {key} {json.dumps(value, ensure_ascii=False)}\n' for key, value in defines.items())
        text += f'#define GAME_MAPPER {game.cartridge.mapper}\n#define GAME_BATTERY {int(game.cartridge.battery)}\n'
        (directory / 'game-identity.h').write_text(text)
        rows = [','.join(f'0x{v:02x}' for v in self.data[i:i+24]) + ',' for i in range(0, len(self.data), 24)]
        (directory / 'game-data.h').write_text('static const unsigned char embedded_rom[] = {\n' + '\n'.join(rows) + '\n};\n')


@dataclass(frozen=True)
class InitRequest:
    rom: Path
    game_id: str | None = None
    title: str | None = None
    region: Region = Region.AUTO


def initialize(root: Path, request: InitRequest) -> GameManifest:
    source = request.rom.expanduser()
    if not source.is_file():
        raise Error('Set ROM=/absolute/path/to/a/game.nes.')
    if source.suffix.lower() != '.nes' or any(ord(c) < 32 for c in source.name):
        raise Error('Expected a .nes filename without control characters.')
    data = source.read_bytes()
    slug = re.sub('[^a-z0-9]+', '-', source.stem.lower()).strip('-')
    game = GameManifest(request.game_id or (slug if slug.startswith('nes-') else 'nes-' + slug),
                        request.title or source.stem.replace('-', ' ').replace('_', ' ').title(),
                        'roms/' + source.name, hashlib.sha256(data).hexdigest(), request.region, inspect(data))
    with locked(root / '.init.lock'):
        if (root / 'game.json').exists():
            if LoadedGame.load(root).manifest != game:
                raise Error('This checkout is initialized for another configuration. Use a fresh template copy.')
            print(f'Already initialized: {game.id}')
            return game
        launcher = root / ('launch-' + game.id)
        if launcher.exists():
            raise Error('Launcher already exists; preserved.')
        destination = root / game.rom
        if destination.exists() and destination.read_bytes() != data:
            raise Error('Destination ROM exists with different bytes; preserved.')
        if not destination.exists():
            atomic_write(destination, data)
        atomic_write(launcher, b'#!/usr/bin/env bash\nset -euo pipefail\nroot=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\nexec "$root/tools/nes-static" run "$@"\n', 0o755)
        atomic_write(root / 'game.json', game.json().encode())
    print(f'Initialized {game.id}: mapper {game.cartridge.mapper}, region {game.region}, battery={game.cartridge.battery}')
    return game


def new_game(template: Path, destination: Path, request: InitRequest) -> None:
    destination = destination.expanduser().absolute()
    if destination.exists():
        raise Error('Destination exists; choose a new directory.')
    if not destination.name.startswith('nes-'):
        raise Error('Game directory names must start with nes-.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.nes-template-', dir=destination.parent))
    try:
        shutil.copytree(template, temporary, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
            '.git', '.venv', '.uv-cache', '.env', '.env.*', '.pytest_cache', '.ruff_cache', '.mypy_cache',
            'htmlcov', '.coverage', 'roms', 'saves', 'dist', 'build', 'game.json', 'game.json.tmp',
            '.init.lock', '__pycache__', '*.pyc', '*.log', '.DS_Store', '._*', 'launch-nes-*'))
        initialize(temporary, replace(request, rom=request.rom.expanduser().resolve(),
                                      game_id=request.game_id or destination.name))
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    print(f'Ready: {destination}\nNext: make -C "{destination}" run')


def inputs(root: Path) -> str:
    game = LoadedGame.load(root)
    files = [*(root / 'static').glob('*.c'), *(root / 'static').glob('*.h'), *(root / 'static').glob('*.lock'),
             *(root / 'nes_static').rglob('*.py'), *(root / 'tools').glob('*'), root / 'game.json',
             root / '.python-version', root / 'pyproject.toml', root / 'uv.lock']
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(bytes.fromhex(file_hash(path)))
    digest.update(bytes.fromhex(game.manifest.sha256))
    return digest.hexdigest()
