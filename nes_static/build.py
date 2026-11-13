"""Pinned native recipes and publication of a standalone game executable."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

from .project import LoadedGame, inputs
from .support import (Error, Runner, SourceArchive, atomic_write, build_log,
                      copy_files, fingerprint, locked, publish, read_sources, stage)


def host_environment(environment: Mapping[str, str]) -> dict[str, str]:
    env = dict(environment)
    for key in ('CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH', 'LD_LIBRARY_PATH',
                'CC', 'CXX', 'AR', 'RANLIB', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                'PKG_CONFIG_LIBDIR', 'PKG_CONFIG_PATH', 'PKG_CONFIG_SYSROOT_DIR', 'PYTHONPATH',
                'MAKEFLAGS', 'MAKEOVERRIDES'):
        env.pop(key, None)
    env.update(SOURCE_DATE_EPOCH='1735689600', LC_ALL='C', TZ='UTC', PYTHON=sys.executable)
    return env


@dataclass(frozen=True)
class BuildContext:
    root: Path
    cache: Path
    logs: Path
    jobs: int
    host: dict[str, str]
    recipe_key: str
    core_key: str
    wayland_key: str
    core_source: SourceArchive

    @classmethod
    def create(cls, root: Path) -> 'BuildContext':
        if os.geteuid() == 0:
            raise Error('Build as your regular user.')
        if (platform.system(), platform.machine()) != ('Linux', 'x86_64'):
            raise Error('The template currently targets Linux x86-64.')
        for command in ('curl', 'gcc', 'g++', 'ar', 'ranlib', 'make', 'cmake', 'ninja',
                        'pkg-config', 'readelf', 'ld', 'stat', 'cc', 'bison', 'strip'):
            if shutil.which(command) is None:
                raise Error(f'Missing {command}; install the build prerequisites listed in README.md.')
        cache_home = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
        state_home = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
        cache = Path(os.environ.get('NES_BUILD_DIR') or cache_home / 'nes-static')
        logs = Path(os.environ.get('NES_LOG_DIR') or state_home / 'nes-static-build')
        if not cache.is_absolute() or not logs.is_absolute():
            raise Error('NES_BUILD_DIR and NES_LOG_DIR must be absolute paths.')
        if any(c.isspace() for c in str(cache)):
            raise Error('NES_BUILD_DIR must not contain whitespace (upstream build-system limitation).')
        jobs = int(os.environ.get('NES_BUILD_JOBS') or '4')
        if jobs < 1:
            raise Error('NES_BUILD_JOBS must be a positive integer.')
        cache.mkdir(parents=True, exist_ok=True)
        if subprocess.check_output(['stat', '-f', '-c', '%T', cache], text=True).strip() in ('exfat', 'vfat', 'msdos'):
            raise Error('NES_BUILD_DIR must be on a Linux filesystem.')
        host = host_environment(os.environ)
        versions = ''.join(subprocess.check_output([tool, '--version'], env=host, text=True)
                           for tool in ('gcc', 'ld'))
        recipe_key = fingerprint([root / name for name in (
            'static/sources.lock', 'nes_static/build.py', 'nes_static/support.py',
            '.python-version', 'pyproject.toml', 'uv.lock')], versions)[:16]
        revision, checksum, url = (root / 'static/core.lock').read_text().split()
        source = SourceArchive(checksum, f'libretro-fceumm-{revision}.tar.gz', url,
                               f'libretro-fceumm-{revision}')
        return cls(root, cache, logs, jobs, host, recipe_key,
                   fingerprint([root / 'static/core.lock'], recipe_key)[:16],
                   fingerprint([root / 'static/wayland.lock'], recipe_key)[:16], source)

    @property
    def prefix(self) -> Path:
        return self.cache / f'deps-{self.recipe_key}'

    @property
    def work(self) -> Path:
        return self.cache / f'build-{self.recipe_key}'

    @property
    def core(self) -> Path:
        return self.cache / f'core-{self.core_key}' / self.core_source.directory

    @property
    def revision(self) -> str:
        return self.core_source.directory.removeprefix('libretro-fceumm-')

    @property
    def wayland_work(self) -> Path:
        return self.cache / f'wayland-{self.wayland_key}'

    @property
    def wayland_prefix(self) -> Path:
        return self.wayland_work / 'install'

    @property
    def cc(self) -> Path:
        return self.prefix / 'bin/cc'

    def target_environment(self, *, wayland: bool = False) -> dict[str, str]:
        prefixes = [self.wayland_prefix, self.prefix] if wayland else [self.prefix]
        return dict(self.host, CC=str(self.cc), CXX='g++', AR='ar', RANLIB='ranlib',
                    CFLAGS='-O2 -g0', CXXFLAGS='-O2 -g0', LDFLAGS='-static', PKG_CONFIG_PATH='',
                    PKG_CONFIG_LIBDIR=':'.join(str(p / directory / 'pkgconfig')
                                               for p in prefixes for directory in ('lib', 'share')),
                    PYTHONPATH=str(self.prefix / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'))


@dataclass(frozen=True)
class Recipes:
    context: BuildContext
    runner: Runner

    def run(self, args: Sequence[str | Path], cwd: Path, *, host: bool = False, wayland: bool = False) -> None:
        env = self.context.host if host else self.context.target_environment(wayland=wayland)
        self.runner.run(args, cwd, env)

    def sources(self, lock: str, destination: Path) -> None:
        for source in read_sources(self.context.root / 'static' / lock).values():
            source.extract(self.context.cache / 'downloads', destination, self.runner, self.context.host)

    def autotools(self, source: Path, prefix: Path, extra: Sequence[str] = (), *, wayland: bool = False) -> None:
        self.run(['./configure', f'--prefix={prefix}', f'--libdir={prefix}/lib',
                  '--disable-shared', '--enable-static', *extra], source, wayland=wayland)
        self.run(['make', '-j', str(self.context.jobs)], source, wayland=wayland)
        self.run(['make', 'install'], source, wayland=wayland)

    def meson(self, args: Sequence[str | Path], *, host: bool = False, wayland: bool = False) -> None:
        self.run([sys.executable, '-m', 'mesonbuild.mesonmain', *args], self.context.cache,
                 host=host, wayland=wayland)

    def musl(self) -> None:
        c = self.context
        env = dict(c.host, CC='gcc', CFLAGS='-O2 -g0 -march=x86-64 -mtune=generic')
        for command in (['./configure', f'--prefix={c.prefix}/musl', '--disable-shared'],
                        ['make', '-j', str(c.jobs)], ['make', 'install']):
            self.runner.run(command, c.work / 'musl-1.2.5', env)

    def sndfile(self) -> None:
        c = self.context
        directory = c.work / 'sndfile-build'
        self.run(['cmake', '-S', c.work / 'libsndfile-1.2.2', '-B', directory, '-G', 'Ninja',
                  '-DCMAKE_POLICY_VERSION_MINIMUM=3.5', f'-DCMAKE_INSTALL_PREFIX={c.prefix}',
                  '-DCMAKE_INSTALL_LIBDIR=lib', '-DCMAKE_BUILD_TYPE=Release',
                  '-DCMAKE_C_FLAGS_RELEASE=-O2 -DNDEBUG', '-DCMAKE_CXX_FLAGS_RELEASE=-O2 -DNDEBUG',
                  f'-DCMAKE_FIND_ROOT_PATH={c.prefix}', '-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY',
                  '-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY', '-DBUILD_SHARED_LIBS=OFF',
                  '-DBUILD_TESTING=OFF', '-DBUILD_PROGRAMS=OFF', '-DBUILD_EXAMPLES=OFF',
                  '-DENABLE_EXTERNAL_LIBS=OFF', '-DENABLE_MPEG=OFF'], c.cache)
        self.run(['cmake', '--build', directory, '-j', str(c.jobs)], c.cache)
        self.run(['cmake', '--install', directory], c.cache)

    def pulse(self) -> None:
        c = self.context
        source, directory = c.work / 'pulseaudio-17.0', c.work / 'pulse-build'
        # Upstream hardcodes shared_library for its client libraries.
        for name in ('src/meson.build', 'src/pulse/meson.build'):
            path = source / name
            path.write_text(path.read_text().replace('shared_library(', 'library('))
        self.meson(['setup', '--reconfigure', directory, source, '--prefix=/usr', '--sysconfdir=/etc',
                    '--localstatedir=/var', '--libdir=lib', '--buildtype=release', '--default-library=static',
                    '--auto-features=disabled', '-Ddaemon=false', '-Dclient=true', '-Dtests=false',
                    '-Ddoxygen=false', '-Dman=false', '-Ddatabase=simple', '-Drunning-from-build-tree=false',
                    '-Dbashcompletiondir=no', '-Dzshcompletiondir=no'])
        archives = ['src/libpulsecommon-17.0.a', 'src/pulse/libpulse.a', 'src/pulse/libpulse-simple.a']
        self.run(['ninja', '-C', directory, '-j', str(c.jobs), *archives], c.cache)
        copy_files(source / 'src/pulse', '*.h', c.prefix / 'include/pulse')
        copy_files(directory / 'src/pulse', 'version.h', c.prefix / 'include/pulse')
        for archive in archives:
            shutil.copy2(directory / archive, c.prefix / 'lib' / Path(archive).name)

    def dependencies(self) -> None:
        c = self.context
        outputs = [c.prefix / 'lib' / f'lib{name}.a' for name in
                   ('xcb', 'xcb-shm', 'xcb-xkb', 'Xau', 'pulse', 'pulse-simple', 'pulsecommon-17.0', 'sndfile')]
        outputs += [c.cc, c.prefix / 'musl/lib/libc.a']

        def build() -> None:
            self.sources('sources.lock', c.work)
            stage(c.work / 'musl.done', [c.prefix / 'musl/lib/libc.a', c.prefix / 'musl/bin/musl-gcc'], self.musl)
            command = shlex.join([str(c.prefix / 'musl/bin/musl-gcc'), '-march=x86-64', '-mtune=generic',
                                  f'-ffile-prefix-map={c.cache}=/build', f'-ffile-prefix-map={c.root}=/source'])
            atomic_write(c.cc, f'#!/usr/bin/env bash\nexec {command} "$@"\n'.encode(), 0o755)
            for name, output, extra in (
                ('xorgproto-2024.1', 'include/X11/X.h', ()),
                ('libXau-1.0.12', 'lib/libXau.a', ()),
                ('xcb-proto-1.17.0', 'share/pkgconfig/xcb-proto.pc', ()),
                ('libxcb-1.17.0', 'lib/libxcb.a', ('--disable-devel-docs', '--disable-doxygen-doc', '--disable-xdmcp')),
            ):
                stage(c.work / f'{name}.done', [c.prefix / output],
                      partial(self.autotools, c.work / name, c.prefix, extra))
            stage(c.work / 'sndfile.done', [c.prefix / 'lib/libsndfile.a'], self.sndfile)
            stage(c.work / 'pulse.done', [c.prefix / 'lib' / f'lib{name}.a' for name in
                                        ('pulse', 'pulse-simple', 'pulsecommon-17.0')], self.pulse)
            shutil.copy2(c.root / 'static/sources.lock', c.prefix / 'sources.lock')

        stage(c.prefix / 'complete', outputs, build)

    def core(self) -> None:
        c = self.context

        def build() -> None:
            c.core_source.extract(c.cache / 'downloads', c.core.parent, self.runner, c.host)
            # Static libretro builds otherwise omit support functions this frontend needs.
            path = c.core / 'Makefile.common'
            path.write_text(path.read_text().replace('ifneq ($(STATIC_LINKING), 1)',
                                                     'ifneq ($(STATIC_LINKING), never)'))
            self.runner.run(['make', '-f', 'Makefile.libretro', '-j', str(c.jobs), 'platform=unix',
                             'STATIC_LINKING=1', 'HAVE_HDPACK=0', 'HAVE_NTSC=0', 'TARGET=fceumm.a',
                             f'CC={c.cc}', 'AR=ar', f'GIT_VERSION={c.revision}'], c.core, c.host)

        stage(c.core / 'complete', [c.core / 'fceumm.a'], build)

    def scanner(self) -> None:
        c = self.context
        directory = c.wayland_work / 'scanner-build'
        self.run(['pkg-config', '--exists', 'expat'], c.cache, host=True)
        self.meson(['setup', '--reconfigure', directory, c.wayland_work / 'wayland-1.23.1',
                    f'--prefix={c.wayland_prefix}', '--libdir=lib', '--buildtype=release',
                    '-Dlibraries=false', '-Dtests=false', '-Ddocumentation=false', '-Ddtd_validation=false'], host=True)
        self.run(['ninja', '-C', directory, '-j', str(c.jobs)], c.cache, host=True)
        self.meson(['install', '-C', directory], host=True)

    def wayland_client(self, cross: Sequence[str | Path]) -> None:
        c = self.context
        directory = c.wayland_work / 'client-build'
        self.meson(['setup', '--reconfigure', directory, c.wayland_work / 'wayland-1.23.1', *cross,
                    '-Dscanner=false', '-Dtests=false', '-Ddocumentation=false', '-Ddtd_validation=false'], wayland=True)
        self.run(['ninja', '-C', directory, '-j', str(c.jobs)], c.cache, wayland=True)
        self.meson(['install', '-C', directory], wayland=True)

    def xkb(self, cross: Sequence[str | Path]) -> None:
        c = self.context
        source, directory = c.wayland_work / 'libxkbcommon-xkbcommon-1.7.0', c.wayland_work / 'xkb-build'
        self.meson(['setup', '--reconfigure', directory, source, *cross, '-Denable-tools=false',
                    '-Denable-x11=false', '-Denable-wayland=false', '-Denable-xkbregistry=false',
                    '-Denable-docs=false', '-Denable-bash-completion=false', '-Dxkb-config-root=/usr/share/X11/xkb',
                    '-Dxkb-config-extra-path=/etc/xkb', '-Dx-locale-root=/usr/share/X11/locale'], wayland=True)
        # Upstream's full install also builds its tests.
        self.run(['ninja', '-C', directory, '-j', str(c.jobs), 'libxkbcommon.a'], c.cache, wayland=True)
        copy_files(source / 'include/xkbcommon', '*.h', c.wayland_prefix / 'include/xkbcommon')
        shutil.copy2(directory / 'libxkbcommon.a', c.wayland_prefix / 'lib/libxkbcommon.a')

    def wayland(self) -> None:
        c = self.context
        p, w = c.wayland_prefix, c.wayland_work
        outputs = [p / 'lib' / f'lib{name}.a' for name in ('ffi', 'wayland-client', 'xkbcommon')]
        outputs += [p / 'protocol/xdg-shell.h', p / 'protocol/xdg-shell.c']

        def build() -> None:
            self.sources('wayland.lock', w)
            stage(w / 'scanner.done', [p / 'bin/wayland-scanner'], self.scanner)
            # Wayland uses ffi_call; executable closure trampolines are unnecessary.
            stage(w / 'ffi.done', [p / 'lib/libffi.a'], lambda: self.autotools(w / 'libffi-3.4.6', p,
                  ('--host=x86_64-linux-musl', '--disable-docs', '--disable-exec-static-tramp'), wayland=True))
            (w / 'cross.ini').write_text(
                f"[binaries]\nc = {str(c.cc)!r}\nar = 'ar'\nstrip = 'strip'\npkg-config = 'pkg-config'\n"
                "[host_machine]\nsystem = 'linux'\ncpu_family = 'x86_64'\ncpu = 'x86_64'\nendian = 'little'\n")
            cross: list[str | Path] = ['--cross-file', w / 'cross.ini', f'--prefix={p}', '--libdir=lib',
                                       '--default-library=static', '--buildtype=release', '--wrap-mode=nodownload']
            stage(w / 'client.done', [p / 'lib/libwayland-client.a'], lambda: self.wayland_client(cross))
            stage(w / 'xkb.done', [p / 'lib/libxkbcommon.a'], lambda: self.xkb(cross))
            (p / 'protocol').mkdir(parents=True, exist_ok=True)
            protocol = w / 'wayland-protocols-1.36/stable/xdg-shell/xdg-shell.xml'
            for mode, filename in (('client-header', 'xdg-shell.h'), ('private-code', 'xdg-shell.c')):
                self.run([p / 'bin/wayland-scanner', mode, protocol, p / 'protocol' / filename], c.cache, host=True)

        stage(p / 'complete', outputs, build)


def bootstrap_locked(context: BuildContext) -> None:
    with build_log(context.logs / 'bootstrap.log') as runner:
        recipes = Recipes(context, runner)
        recipes.dependencies()
        recipes.core()
        recipes.wayland()
    print(f'Dependencies ready: {context.cache}')


def bootstrap(root: Path) -> None:
    context = BuildContext.create(root)
    with locked(context.cache / 'setup.lock'):
        bootstrap_locked(context)


def licenses(context: BuildContext, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('libXau-1.0.12', 'libxcb-1.17.0', 'xcb-proto-1.17.0', 'libsndfile-1.2.2'):
        shutil.copy2(context.work / name / 'COPYING', destination / f'{name}.txt')
    for source, name in (
        (context.core / 'Copying', 'fceumm-GPL-2.0.txt'),
        (context.work / 'pulseaudio-17.0/LICENSE', 'pulseaudio-17.0.txt'),
        (context.work / 'pulseaudio-17.0/LGPL', 'LGPL-2.1.txt'),
        (context.work / 'musl-1.2.5/COPYRIGHT', 'musl.txt'),
        (context.wayland_work / 'wayland-1.23.1/COPYING', 'wayland-1.23.1.txt'),
        (context.wayland_work / 'libffi-3.4.6/LICENSE', 'libffi-3.4.6.txt'),
        (context.wayland_work / 'libxkbcommon-xkbcommon-1.7.0/LICENSE', 'libxkbcommon-1.7.0.txt'),
        (context.wayland_work / 'wayland-protocols-1.36/COPYING', 'wayland-protocols-1.36.txt'),
    ):
        shutil.copy2(source, destination / name)


def build_game(root: Path) -> Path:
    initial_inputs = inputs(root)
    game = LoadedGame.load(root)
    c = BuildContext.create(root)
    binary = root / 'dist' / game.manifest.id
    with locked(c.cache / 'setup.lock'):
        bootstrap_locked(c)
        with tempfile.TemporaryDirectory(prefix='game-', dir=c.cache) as temporary:
            staging = Path(temporary)
            game.headers(staging, c.revision)
            sources = [root / 'static' / name for name in ('game.c', 'platform.c', 'platform-x11.c', 'platform-wayland.c')]
            build_id = fingerprint([*sources, root / 'static/platform.h', Path(__file__),
                                    staging / 'game-data.h', staging / 'game-identity.h'], c.core_key + c.wayland_key)
            marker = binary.with_suffix('.build-id')
            if not (binary.is_file() and os.access(binary, os.X_OK) and marker.is_file()
                    and marker.read_text().strip() == build_id):
                with build_log(c.logs / 'build.log') as runner:
                    includes = [c.prefix / 'include', c.wayland_prefix / 'include', c.wayland_prefix / 'protocol',
                                staging, c.core / 'src/drivers/libretro/libretro-common/include']
                    runner.run([c.cc, '-std=c11', '-O2', '-g0', '-static', '-s', '-Wall', '-Wextra', '-Werror',
                                f'-DBUILD_ID="{build_id[:16]}"', *(f'-I{path}' for path in includes), *sources,
                                c.wayland_prefix / 'protocol/xdg-shell.c', c.core / 'fceumm.a',
                                f'-L{c.prefix}/lib', f'-L{c.wayland_prefix}/lib', '-Wl,--start-group',
                                '-lwayland-client', '-lffi', '-lxkbcommon', '-lxcb-shm', '-lxcb-xkb', '-lxcb',
                                '-lXau', '-lpulse', '-lpulsecommon-17.0', '-lsndfile', '-Wl,--end-group',
                                '-lm', '-pthread', '-o', staging / 'game'], root, c.target_environment(wayland=True))
                    for flag, dependency in (('-lW', 'INTERP'), ('-dW', 'NEEDED')):
                        if dependency in subprocess.check_output(['readelf', flag, staging / 'game'], text=True):
                            raise Error('Game unexpectedly depends on the dynamic loader.')
                    runner.run([staging / 'game', '--version'], root, c.host)
                if inputs(root) != initial_inputs:
                    raise Error('Sources changed during build; rerun make build.')
                publish(staging / 'game', binary)
                atomic_write(marker, (build_id + '\n').encode())
            if inputs(root) != initial_inputs:
                raise Error('Sources changed during build; rerun make build.')
            licenses(c, root / 'dist/licenses')
            for path in [root / 'game.json', *(root / 'static').glob('*.lock')]:
                shutil.copy2(path, root / 'dist' / path.name)
            atomic_write(binary.with_suffix('.inputs-id'), (initial_inputs + '\n').encode())
    print(f'Game ready: {binary} ({binary.stat().st_size} bytes)')
    return binary


def run_game(root: Path, arguments: Sequence[str]) -> None:
    game = LoadedGame.load(root)
    binary = root / 'dist' / game.manifest.id
    marker = binary.with_suffix('.inputs-id')
    if not (binary.is_file() and os.access(binary, os.X_OK) and marker.is_file() and marker.read_text().strip() == inputs(root)):
        binary = build_game(root)
    environment = dict(os.environ)
    environment['NES_STATE_DIR'] = environment.get('NES_STATE_DIR') or str(root / 'saves')
    os.chdir(root)
    os.execve(binary, [str(binary), *arguments], environment)
