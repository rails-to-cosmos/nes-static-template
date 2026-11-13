import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from nes_static.build import host_environment
from nes_static.support import Error, Runner, SourceArchive, file_hash, publish, stage


class BuildTests(unittest.TestCase):
    def test_verified_extraction_rejects_changed_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                entry = tarfile.TarInfo('source/configure')
                entry.size = 4
                entry.mode = 0o755
                bundle.addfile(entry, io.BytesIO(b'test'))
            source = SourceArchive(file_hash(archive), archive.name, 'unused', 'source')
            runner = Runner(io.StringIO(), root / 'log')
            extracted = source.extract(root, root / 'work', runner, {})
            self.assertEqual((extracted / 'configure').read_bytes(), b'test')
            self.assertTrue(os.access(extracted / 'configure', os.X_OK))
            self.assertEqual(source.extract(root, root / 'work', runner, {}), extracted)
            archive.write_bytes(b'corrupt cache')
            with self.assertRaisesRegex(Error, 'checksum'):
                source.extract(root, root / 'work', runner, {})

    def test_unsafe_archive_never_publishes_sources(self):
        for member in ('../escaped', 'unexpected/file'):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / 'source.tar.gz'
                with tarfile.open(archive, 'w:gz') as bundle:
                    bundle.addfile(tarfile.TarInfo(member))
                source = SourceArchive(file_hash(archive), archive.name, 'unused', 'source')
                with self.assertRaises((tarfile.TarError, Error)):
                    source.extract(root, root / 'work', Runner(io.StringIO(), root / 'log'), {})
                self.assertEqual(list((root / 'work').iterdir()), [])
                self.assertFalse((root / 'escaped').exists())

    def test_failed_download_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = SourceArchive('0' * 64, 'source.tar.gz', 'unused', 'source')

            def download(_runner, arguments, cwd, env):
                Path(arguments[-1]).write_bytes(b'wrong checksum')

            with patch.object(Runner, 'run', download), self.assertRaisesRegex(Error, 'checksum'):
                source.extract(root, root / 'work', Runner(io.StringIO(), root / 'log'), {})
            self.assertFalse((root / 'source.tar.gz').exists())
            self.assertFalse((root / 'source.tar.gz.part').exists())

    def test_stage_retries_failed_and_missing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, output = root / 'done', root / 'library.a'
            calls = 0

            def build():
                nonlocal calls
                calls += 1
                output.write_bytes(b'archive')

            with self.assertRaisesRegex(Error, 'did not produce'):
                stage(marker, [output], lambda: None)
            self.assertFalse(marker.exists())
            stage(marker, [output], build)
            stage(marker, [output], build)
            self.assertEqual(calls, 1)
            output.unlink()
            stage(marker, [output], build)
            self.assertEqual(calls, 2)
            output.unlink()
            with self.assertRaisesRegex(Error, 'did not produce'):
                stage(marker, [output], lambda: None)
            self.assertFalse(marker.exists())

    def test_publication_preserves_old_binary_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, candidate = root / 'game', root / 'candidate'
            binary.write_bytes(b'working')
            candidate.write_bytes(b'new')
            with patch('nes_static.support.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    publish(candidate, binary)
            self.assertEqual(binary.read_bytes(), b'working')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['candidate', 'game'])
            publish(candidate, binary)
            self.assertEqual(binary.read_bytes(), b'new')
            self.assertTrue(os.access(binary, os.X_OK))

    def test_runner_preserves_arguments_and_reports_failure_log(self):
        with tempfile.TemporaryDirectory(prefix='nes space ') as directory:
            root = Path(directory)
            output = root / 'arguments'
            with (root / 'build.log').open('w') as stream:
                runner = Runner(stream, root / 'build.log')
                runner.run([sys.executable, '-c',
                            'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
                            output, 'literal $HOME; `false`'], root, os.environ)
                self.assertEqual(output.read_text(), 'literal $HOME; `false`')
                with self.assertRaisesRegex(Error, 'build.log'):
                    runner.run([sys.executable, '-c', 'raise SystemExit(4)'], root, os.environ)

    def test_host_environment_is_independent_of_target_flags(self):
        env = {'PATH': '/bin', 'CC': 'musl-gcc', 'CFLAGS': '-static', 'PKG_CONFIG_LIBDIR': '/target',
               'CPATH': '/target/include', 'MAKEFLAGS': 'CC=broken', 'NES_DISPLAY': 'wayland'}
        actual = host_environment(env)
        self.assertEqual(actual['PATH'], '/bin')
        self.assertEqual(actual['NES_DISPLAY'], 'wayland')
        for key in ('CC', 'CFLAGS', 'PKG_CONFIG_LIBDIR', 'CPATH', 'MAKEFLAGS'):
            self.assertNotIn(key, actual)
        self.assertEqual(env['CC'], 'musl-gcc')


if __name__ == '__main__':
    unittest.main()
