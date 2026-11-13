"""Shared file, source acquisition and build transaction operations."""
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from typing import TextIO


class Error(RuntimeError):
    pass


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(paths: Sequence[Path], extra: str = '') -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
    digest.update(extra.encode())
    return digest.hexdigest()


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.write-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if mode is not None:
                temporary.chmod(mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def publish(source: Path, destination: Path) -> None:
    # Cache and game directory can be on different filesystems.
    atomic_write(destination, source.read_bytes(), 0o755)


def complete(marker: Path, outputs: Sequence[Path]) -> bool:
    return marker.is_file() and all(p.is_file() and p.stat().st_size for p in outputs)


def stage(marker: Path, outputs: Sequence[Path], action: Callable[[], None]) -> None:
    if complete(marker, outputs):
        return
    marker.unlink(missing_ok=True)
    action()
    missing = [str(p) for p in outputs if not p.is_file() or not p.stat().st_size]
    if missing:
        raise Error('Build did not produce: ' + ', '.join(missing))
    atomic_write(marker, b'complete\n')


@dataclass(frozen=True)
class Runner:
    log: TextIO
    log_path: Path

    def run(self, args: Sequence[str | Path], cwd: Path, env: Mapping[str, str]) -> None:
        command = [str(value) for value in args]
        print('+ ' + ' '.join(command), file=self.log, flush=True)
        result = subprocess.run(command, cwd=cwd, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise Error(f'{command[0]} failed ({result.returncode}); see {self.log_path}')


@contextmanager
def build_log(path: Path) -> Iterator[Runner]:
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f'Build log: {path}', flush=True)
    with path.open('w') as stream:
        yield Runner(stream, path)


@dataclass(frozen=True)
class SourceArchive:
    sha256: str
    filename: str
    url: str
    directory: str

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256):
            raise Error(f'Invalid source checksum: {self.filename}')
        for name in (self.filename, self.directory):
            if Path(name).name != name or name in ('', '.', '..'):
                raise Error(f'Invalid source path: {name}')

    def extract(self, downloads: Path, destination: Path, runner: Runner, env: Mapping[str, str]) -> Path:
        downloads.mkdir(parents=True, exist_ok=True)
        destination.mkdir(parents=True, exist_ok=True)
        archive = downloads / self.filename
        if not archive.exists():
            partial = archive.with_name(archive.name + '.part')
            try:
                runner.run(['curl', '--fail', '--location', '--retry', '3', self.url, '-o', partial], downloads, env)
                self.verify(partial)
                partial.replace(archive)
            finally:
                partial.unlink(missing_ok=True)
        self.verify(archive)
        extracted = destination / self.directory
        if not extracted.exists():
            with tempfile.TemporaryDirectory(prefix='extract-', dir=destination) as temporary:
                with tarfile.open(archive) as bundle:
                    bundle.extractall(temporary, filter='data')
                entries = list(Path(temporary).iterdir())
                expected = Path(temporary) / self.directory
                if entries != [expected] or not expected.is_dir() or expected.is_symlink():
                    raise Error(f'Unexpected archive root: {self.filename}')
                expected.rename(extracted)
        if not extracted.is_dir() or extracted.is_symlink():
            raise Error(f'Invalid extracted source directory: {extracted}')
        return extracted

    def verify(self, archive: Path) -> None:
        if file_hash(archive) != self.sha256:
            raise Error(f'Source checksum mismatch: {archive}')


def read_sources(path: Path) -> dict[str, SourceArchive]:
    sources: dict[str, SourceArchive] = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split()
        if len(fields) not in (3, 4):
            raise Error(f'Invalid source row in {path}: {line}')
        checksum, filename, url = fields[:3]
        directory = fields[3] if len(fields) == 4 else filename.split('.tar.')[0]
        if directory in sources:
            raise Error(f'Duplicate source in {path}: {directory}')
        sources[directory] = SourceArchive(checksum, filename, url, directory)
    return sources


def copy_files(source: Path, pattern: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    files = list(source.glob(pattern))
    if not files:
        raise Error(f'No files match {source / pattern}')
    for path in files:
        shutil.copy2(path, destination / path.name)
