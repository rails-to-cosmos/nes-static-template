#!/usr/bin/env -S uv run --managed-python --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Build a synthetic cartridge and verify persistence in an isolated state tree."""
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parent.parent


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=300, **kwargs)


def check(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def limit_writes():
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))


def main():
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        raise SystemExit('Set DISPLAY to a private Xvfb server or WAYLAND_DISPLAY to a test compositor.')
    parent = Path(os.environ.get('NES_BUILD_DIR', Path.home() / '.cache/nes-static'))
    parent.mkdir(parents=True, exist_ok=True)
    report = Path(tempfile.mkdtemp(prefix='runtime-check-', dir=parent))
    print(f'Evidence: {report}', flush=True)
    # A mapper-1 cartridge increments SRAM[0] once, writes a marker, then loops.
    header = bytearray(b'NES\x1a' + bytes(12)); header[4:7] = bytes([1, 1, 0x12])
    prg = bytearray([0xea] * 16384)
    prg[:11] = bytes.fromhex('a9428d0160ee00604c0880')
    prg[-6:] = bytes.fromhex('088000800880')
    rom = report / 'Battery Counter.nes'; rom.write_bytes(header + prg + bytes(8192))
    project = report / 'nes-battery-counter'
    env = {**os.environ, 'ROM': str(rom), 'DEST': str(project), 'TITLE': 'Battery Counter',
           'GAME_ID': 'nes-battery-counter', 'REGION': 'ntsc', 'NES_STATE_DIR': str(report / 'state')}
    check(run(['make', '-C', str(ROOT), 'new'], env=env))
    build = run(['make', '-C', str(project), 'build'], env=env)
    (report / 'build.log').write_text(build.stdout + build.stderr); check(build)
    binary = project / 'dist/nes-battery-counter'
    for flag, forbidden in (('-lW', 'INTERP'), ('-dW', 'NEEDED')):
        assert forbidden not in check(run(['readelf', flag, str(binary)])).stdout
    game = json.loads((project / 'game.json').read_text())
    state = Path(env['NES_STATE_DIR']) / game['id'] / game['sha256']
    default_env = {key: value for key, value in env.items() if key != 'NES_STATE_DIR'}
    portable = report / 'portable'; portable.mkdir()
    copied = portable / 'renamed-game'; shutil.copy2(binary, copied)
    check(run([str(copied), '--configure'], env=default_env, cwd=report))
    assert (portable / 'saves' / game['id'] / game['sha256'] / 'settings.conf').is_file()
    check(run(['make', '-C', str(project), 'controls', 'GAME_ARGS=--set volume=63'], env=default_env))
    local_settings = project / 'saves' / game['id'] / game['sha256'] / 'settings.conf'
    assert 'volume=63' in local_settings.read_text(), 'Launcher did not use game-local saves'
    count = 0

    def execute(*args, **kwargs):
        nonlocal count
        count += 1
        result = run([str(binary), *args], env=env, **kwargs)
        (report / f'run-{count}.log').write_text(result.stdout + result.stderr)
        return result

    check(execute('--configure', '--set', 'volume=37', '--set', 'a=x'))
    check(run(['make', '-C', str(project), 'controls-wasd'], env=env))
    settings = dict(line.split('=', 1) for line in (state / 'settings.conf').read_text().splitlines()
                    if line and not line.startswith('#'))
    assert settings == dict(a='j', b='k', select='Tab', start='Return', up='w', down='s',
                            left='a', right='d', volume='37', fullscreen='0'), settings
    check(execute('--mute', '--seconds', '0.5', '--save-slot', '2'))
    battery = state / 'battery.ram'; first = battery.read_bytes()
    assert first[:8] == b'NESRAM01' and first[129] == 0x42
    assert struct.unpack_from('<I', first, 72)[0] == len(first) - 128
    assert struct.unpack_from('<I', first, 76)[0] == zlib.crc32(first[128:])
    check(execute('--mute', '--seconds', '0.5'))
    second = battery.read_bytes()
    assert second[128] == (first[128] + 1) % 256, 'Battery did not survive restart'
    snapshot = (state / 'slot-2.state').read_bytes()
    check(execute('--mute', '--paused', '--seconds', '0.2', '--load-slot', '2', '--save-slot', '3'))
    assert (state / 'slot-3.state').read_bytes() == snapshot, 'Snapshot roundtrip changed state'
    assert battery.read_bytes() == first, 'Loading a snapshot did not restore its SRAM'
    failed = execute('--mute', '--seconds', '0.5', preexec_fn=limit_writes)
    assert failed.returncode != 0 and battery.read_bytes() == first, 'Failed write replaced battery save'
    damaged = bytearray(first); damaged[-1] ^= 1; battery.write_bytes(damaged)
    failed = execute('--mute', '--seconds', '0.5')
    assert failed.returncode != 0 and 'Invalid battery.ram' in failed.stderr
    assert battery.read_bytes() == damaged, 'Corrupt battery save was overwritten'
    battery.write_bytes(first)
    with (report / 'periodic-save.log').open('w') as log:
        process = subprocess.Popen([str(binary), '--mute', '--seconds', '30'], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 12
            while battery.read_bytes() == first and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.1)
            assert process.poll() is None and battery.read_bytes()[128] == (first[128] + 1) % 256, 'Periodic save did not run'
        finally:
            if process.poll() is None: process.terminate()
            assert process.wait(timeout=5) == 0
    print('PASS: static ELF, local/configurable saves, WASD preset, battery persistence and snapshots.')


if __name__ == '__main__':
    main()
