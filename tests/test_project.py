import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_manifest import manifest, rom


class ProjectTests(unittest.TestCase):
    def test_initialization_is_repeatable_and_preserves_original_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Original Cartridge (USA).nes'
            source.write_bytes(rom())
            env = {'ROM': str(source), 'GAME_ID': 'nes-example', 'TITLE': 'Example', 'REGION': 'ntsc'}
            with patch.object(manifest, 'ROOT', root), patch.dict(os.environ, env):
                manifest.initialize()
                first = (root / 'game.json').read_bytes()
                manifest.initialize()
                self.assertEqual(first, (root / 'game.json').read_bytes())
                game, data = manifest.read_manifest()
                self.assertEqual(data, source.read_bytes())
                self.assertEqual(game['rom'], 'roms/' + source.name)
                self.assertTrue((root / 'launch-nes-example').stat().st_mode & 0o111)
                with patch.dict(os.environ, {'GAME_ID': 'nes-another'}):
                    with self.assertRaisesRegex(ValueError, 'another configuration'):
                        manifest.initialize()
                (root / game['rom']).write_bytes(data[:-1] + b'\x01')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    manifest.read_manifest()

    def test_new_game_handles_spaces_and_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Original Cartridge.nes'; source.write_bytes(rom())
            destination = root / 'with spaces' / 'nes-test'
            env = {**os.environ, 'ROM': str(source), 'DEST': str(destination),
                   'GAME_ID': '', 'TITLE': 'Test', 'REGION': 'auto'}
            template = root / 'template'
            shutil.copytree(manifest.ROOT, template, ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__'))
            (template / '.venv').mkdir(exist_ok=True)
            (template / '.venv/local-only').write_text('environment')
            (template / '.env').write_text('LOCAL_SETTING=example\n')
            (template / 'saves').mkdir(exist_ok=True)
            (template / 'saves/progress.state').write_bytes(b'local save')
            command = [str(template / 'tools/new-game.py')]
            first = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse((destination / '.git').exists())
            self.assertFalse((destination / '.venv').exists())
            self.assertFalse((destination / '.env').exists())
            self.assertFalse((destination / 'saves').exists())
            for name in ('pyproject.toml', 'uv.lock', '.python-version'):
                self.assertEqual((destination / name).read_bytes(), (template / name).read_bytes())
            self.assertEqual(json.loads((destination / 'game.json').read_text())['id'], 'nes-test')
            self.assertEqual((destination / 'roms' / source.name).read_bytes(), source.read_bytes())
            checked = subprocess.run([str(destination / 'tools/manifest.py'), 'check'], cwd=root,
                                     env=env, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            second = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('Destination exists', second.stderr)


if __name__ == '__main__':
    unittest.main()
