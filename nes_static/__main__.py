"""Project CLI; environment and command-line values enter the typed API here."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest

from .build import bootstrap, build_game, run_game
from .project import InitRequest, LoadedGame, Region, initialize, new_game
from .support import Error


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(prog='nes-static', description='Build standalone NES games with X11 and Wayland support.')
    commands = parser.add_subparsers(dest='command', required=True)
    for name, description in (
        ('init', 'Initialize this checkout: ROM=path [GAME_ID=nes-example TITLE=Example REGION=auto]'),
        ('new', 'Create an independent game checkout: ROM=path DEST=path/nes-game'),
        ('bootstrap', 'Build pinned dependencies and the emulator core'),
        ('build', 'Embed the cartridge into dist/<game id>'),
        ('run', 'Build if stale, then play; accepts executable options'),
        ('controls', 'Configure controls; accepts --set key=value'),
        ('controls-wasd', 'Apply WASD, J/K, Tab and Enter; preserve other settings'),
        ('check', 'Validate the manifest and original ROM checksum'),
        ('test', 'Run cartridge, project and build transaction tests'),
        ('typecheck', 'Check the Python module with strict mypy'),
        ('test-runtime', 'Test synthetic battery cartridge in a private display session'),
    ):
        command = commands.add_parser(name, help=description, description=description,
                                      add_help=name not in ('run', 'controls'))
        if name in ('init', 'new'):
            command.add_argument('--rom', type=Path, default=os.environ.get('ROM') or None)
            command.add_argument('--id', default=os.environ.get('GAME_ID') or None)
            command.add_argument('--title', default=os.environ.get('TITLE') or None)
            command.add_argument('--region', default=os.environ.get('REGION') or 'auto')
        if name == 'new':
            command.add_argument('--dest', type=Path, default=os.environ.get('DEST') or None)
    args, remaining = parser.parse_known_args()
    if remaining and args.command not in ('run', 'controls'):
        parser.error('unrecognized arguments: ' + ' '.join(remaining))
    try:
        if args.command in ('init', 'new'):
            if args.rom is None:
                raise Error('Set ROM=/absolute/path/to/game.nes or pass --rom.')
            request = InitRequest(args.rom, args.id, args.title, Region.parse(args.region))
            if args.command == 'new':
                if args.dest is None:
                    raise Error('Set DEST=/path/to/nes-game or pass --dest.')
                new_game(root, args.dest, request)
            else:
                initialize(root, request)
        elif args.command == 'check':
            print(LoadedGame.load(root).manifest.json(), end='')
        elif args.command == 'bootstrap':
            bootstrap(root)
        elif args.command == 'build':
            build_game(root)
        elif args.command in ('run', 'controls', 'controls-wasd'):
            if remaining[:1] == ['--']:
                remaining = remaining[1:]
            if args.command == 'controls-wasd':
                bindings = ('up=w', 'left=a', 'down=s', 'right=d', 'a=j', 'b=k', 'select=Tab', 'start=Return')
                remaining = [part for binding in bindings for part in ('--set', binding)]
            if args.command != 'run':
                remaining.insert(0, '--configure')
            run_game(root, remaining)
        elif args.command == 'test':
            suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        elif args.command == 'typecheck':
            return subprocess.call([sys.executable, '-m', 'mypy'], cwd=root)
        elif args.command == 'test-runtime':
            return subprocess.call([sys.executable, root / 'tests/check_runtime.py'], cwd=root)
    except (Error, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
