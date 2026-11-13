#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Create an independent game directory from the reusable template."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

root = Path(__file__).resolve().parent.parent
try:
    rom = Path(os.environ.get('ROM', '')).expanduser().resolve()
    destination = Path(os.environ.get('DEST', '')).expanduser().absolute()
    if not os.environ.get('DEST') or not rom.is_file():
        raise ValueError('Use make new ROM=/path/game.nes DEST=/path/nes-game [TITLE="Game" REGION=auto].')
    if destination.exists(): raise ValueError('Destination exists; choose a new directory.')
    if not destination.name.startswith('nes-'): raise ValueError('Game directory names must start with nes-.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.nes-template-', dir=destination.parent))
    try:
        shutil.copytree(root, temporary, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.git', '.venv', '.uv-cache', '.env', '.env.*',
                                                     '.pytest_cache', '.ruff_cache', 'htmlcov', '.coverage',
                                                     'roms', 'saves', 'dist', 'build', 'game.json', 'game.json.tmp',
                                                     '.init.lock', '__pycache__', '*.pyc', '*.log',
                                                     '.DS_Store', '._*', 'launch-nes-*'))
        env = {**os.environ, 'ROM':str(rom), 'GAME_ID':os.environ.get('GAME_ID') or destination.name}
        subprocess.run([str(temporary/'tools/manifest.py'), 'init'], env=env, check=True)
        temporary.rename(destination)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    print(f'Ready: {destination}\nNext: make -C "{destination}" run')
except (ValueError, OSError, subprocess.CalledProcessError) as error:
    sys.exit(f'Error: {error}')
