import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace

from nes_static.project import InitRequest, LoadedGame, Region, initialize, inputs
from nes_static.support import Error
from test_manifest import rom

ROOT = Path(__file__).resolve().parents[1]


class ProjectTests(unittest.TestCase):
    def test_initialization_is_repeatable_and_preserves_original_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Original Cartridge (USA).nes'
            source.write_bytes(rom())
            request = InitRequest(source, 'nes-example', 'Example', Region.NTSC)
            initialize(root, request)
            first = (root / 'game.json').read_bytes()
            initialize(root, request)
            self.assertEqual(first, (root / 'game.json').read_bytes())
            game = LoadedGame.load(root)
            self.assertEqual(game.data, source.read_bytes())
            self.assertEqual(game.manifest.rom, 'roms/' + source.name)
            self.assertTrue((root / 'launch-nes-example').stat().st_mode & 0o111)
            with self.assertRaisesRegex(Error, 'another configuration'):
                initialize(root, replace(request, game_id='nes-another'))
            (root / game.manifest.rom).write_bytes(game.data[:-1] + b'\x01')
            with self.assertRaisesRegex(Error, 'checksum'):
                LoadedGame.load(root)

    def test_new_game_handles_spaces_and_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Original Cartridge.nes'
            source.write_bytes(rom())
            destination = root / 'with spaces' / 'nes-test'
            env = {**os.environ, 'ROM': str(source), 'DEST': str(destination),
                   'GAME_ID': '', 'TITLE': 'Test', 'REGION': 'auto'}
            template = root / 'template'
            shutil.copytree(ROOT, template, ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', '.mypy_cache'))
            (template / '.venv').mkdir(exist_ok=True)
            (template / '.venv/local-only').write_text('environment')
            (template / '.env').write_text('LOCAL_SETTING=example\n')
            (template / 'saves').mkdir(exist_ok=True)
            (template / 'saves/progress.state').write_bytes(b'local save')
            command = [sys.executable, '-m', 'nes_static', 'new']
            first = subprocess.run(command, cwd=template, env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            for name in ('.git', '.venv', '.env', 'saves'):
                self.assertFalse((destination / name).exists(), name)
            for name in ('pyproject.toml', 'uv.lock', '.python-version'):
                self.assertEqual((destination / name).read_bytes(), (template / name).read_bytes())
            self.assertEqual(json.loads((destination / 'game.json').read_text())['id'], 'nes-test')
            self.assertEqual((destination / 'roms' / source.name).read_bytes(), source.read_bytes())
            checked = subprocess.run([str(destination / 'tools/nes-static'), 'check'], cwd=root,
                                     env=env, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            # Recursive Python sources must participate in stale-binary detection.
            before = inputs(destination)
            (destination / 'nes_static/project.py').write_text('# changed\n')
            self.assertNotEqual(before, inputs(destination))
            second = subprocess.run(command, cwd=template, env=env, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('Destination exists', second.stderr)


if __name__ == '__main__':
    unittest.main()
