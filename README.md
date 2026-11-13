# NES static executable template

Build a standalone Linux x86-64 executable from a supplied `.nes` cartridge.
It embeds the original ROM, a pinned FCEUmm core, X11 and native Wayland
renderers, and a PulseAudio client. The template contains no commercial ROMs.

## Create a game

```sh
make new ROM=/path/to/original-game.nes DEST="$HOME/games/nes-my-game" TITLE="My Game"
cd "$HOME/games/nes-my-game"
make build
make run
```

The destination must be new. It receives an independent copy of the template,
the unchanged ROM with its original filename in `roms/`, a generated
`game.json`, and `launch-nes-my-game`. The destination directory supplies the
default game ID; `GAME_ID=nes-other-name` overrides it. Copies retain their own
source code, so future template changes require deliberate propagation.

Alternatively, copy this repository and run `make init ROM=/path/game.nes`.
Initialization is repeatable for identical inputs and refuses to replace a
different cartridge or configuration. `make check` verifies the ROM checksum,
header and manifest. `TITLE` controls the window title; `REGION=auto|ntsc|pal|dendy`
selects the core region. Defaults are derived from the cartridge and filename.
Changing the game ID changes its state directory; keep it stable after playing.

`make bootstrap` builds dependencies without needing a ROM. `make build` embeds
the configured cartridge into `dist/<game id>`. `make run` and `make launch`
build missing or stale binaries, using content hashes rather than timestamps.
The generated launcher accepts executable arguments directly. Make accepts
them as `GAME_ARGS`, for example `make run GAME_ARGS='--mute --seconds 10'`.
`make help` lists the interface.

Copy `dist/<game id>` to run it from any directory, even after renaming it.
The finished executable needs no Python, compiler, emulator installation,
external ROM or shared libraries. Keep the generated notices and matching
source/build files when packaging it for distribution.

## Build prerequisites

Use Linux x86-64 with GCC/G++, binutils, Make, CMake, Ninja, pkg-config, Bison,
Expat development files, curl, tar, xz, patch, coreutils, util-linux, Bash and
`uv`. On Arch/CachyOS:

```sh
sudo pacman -S --needed gcc binutils make cmake ninja pkgconf bison expat curl tar xz patch coreutils util-linux bash uv
```

Bootstrap checks prerequisites and builds as the regular user. It does not
install system packages. GCC compiles pinned musl and the runtime libraries;
Zig is not used. `pyproject.toml` declares the supported Python range and Python
dependencies; `uv.lock` records their resolution. There are currently no
third-party Python dependencies. `.python-version` pins the exact interpreter.
`tools/python-env` uses `uv sync --locked` with managed Python and supplies that
environment to upstream generators too. Environments live under
`${XDG_CACHE_HOME:-$HOME/.cache}/nes-static/python/<checkout hash>/`, keeping
their symlinks on a Linux filesystem when a game directory is on exFAT.
The system Python is unchanged. Python tools are executable `uv` scripts:
`./tools/manifest.py`, `./tools/new-game.py`, `./tools/prepare-build.py`,
`./tools/test.py` and `./tools/check-runtime.py`. Their shebangs require managed
Python, and their inline metadata declares the Python range and standalone
dependencies. Make invokes them directly. Bootstrap exports the pinned Python
version and runs upstream Meson through `uv run --locked`; Autoconf receives
the managed environment's interpreter through `PYTHON` and `PATH`.

After changing project Python dependencies, run `uv lock` and commit the
manifest and lockfile together. Standalone scripts declare any dependencies
in their inline metadata. The first bootstrap needs network access; verified
downloads and completed builds are reused.

Source versions and SHA-256 checksums live in `static/*.lock`. Musl 1.2.5,
Meson 1.7.2 and the runtime libraries are built from those archives. Host GCC,
binutils, CMake and Ninja remain system tools; builds across toolchain versions
are not claimed to be byte-identical. GCC/binutils versions participate in the
dependency cache key. The executable targets baseline x86-64.

| Variable | Default and purpose |
| --- | --- |
| `NES_BUILD_DIR` | `${XDG_CACHE_HOME:-$HOME/.cache}/nes-static`; source and dependency cache |
| `NES_BUILD_JOBS` | `4`; compilation parallelism |
| `NES_LOG_DIR` | `${XDG_STATE_HOME:-$HOME/.local/state}/nes-static-build`; build logs |
| `NES_STATE_DIR` | `<game directory>/saves` through launchers; `<executable directory>/saves` for a direct binary |
| `NES_DISPLAY` | `auto`; choose `x11` or `wayland` explicitly |

The build cache must have an absolute path without whitespace on a Linux
filesystem. Generated game directories may live on exFAT. A shared setup lock
serializes dependency and executable builds; completed stages are reused.
Failure output identifies the relevant log under `NES_LOG_DIR`.

## Controls and saves

| Action | Default |
| --- | --- |
| D-pad | W / A / S / D |
| A / B | J / K |
| Select / Start | Tab / Enter |
| Pause / resume | Space |
| Reset | F2 |
| Save / load selected slot | F5 / F8 |
| Previous / next slot | `[` / `]` |
| Fullscreen / quit | F11 / Escape |
| Show controls in window title | F1 |

Close the game and run `make controls-wasd` to apply all eight default bindings
to existing settings, preserving volume and fullscreen preferences. Use
`make controls` or `make configure` for the terminal editor, or
`make controls GAME_ARGS='--set a=x --set b=z --set volume=70'` for explicit
edits. The command prints the `settings.conf` path. Existing settings survive
rebuilds; malformed files are preserved and reported. Focus loss pauses play
and clears held keys; press Space to resume.

Launchers and Make commands default to `saves/` in the game directory. A copied
executable defaults to `saves/` beside itself, regardless of the current working
directory. Both accept `NES_STATE_DIR` as an absolute override:

```sh
make run NES_STATE_DIR="$HOME/games/saves"
NES_STATE_DIR="$HOME/games/saves" ./dist/nes-my-game
```

Keep saves on writable storage. Existing home-directory saves remain untouched;
when updating an older game checkout, retain their location by setting
`NES_STATE_DIR="$HOME/.local/state/nes-static"`. Generated game copies exclude
`saves/`, and Git ignores it. Settings and logs share the selected state root.
State is isolated under `NES_STATE_DIR/<game id>/<ROM SHA-256>/`:

- `settings.conf`, `game.log` and `run.lock`.
- `slot-0.state` through `slot-9.state`, written only when requested.
- `recovery.state`, attempted after a detected fatal error during play.
- `battery.ram`, for supported battery-backed cartridges.

One process at a time may use a state directory. Snapshots validate their ROM,
exact core revision, length and CRC before loading. Writes use a temporary
file, fsync and atomic rename. `--load-slot N` loads and selects a slot;
`--save-slot N` saves on exit. `--recover` loads the recovery snapshot.
Recovery requires writable storage and cannot cover SIGKILL or power loss.

Battery RAM is loaded automatically and saved when changed every five seconds,
on normal exit and during detected fatal-error recovery. A failed periodic
save pauses play and retries; an invalid existing file prevents startup and is
preserved. Its header validates ROM identity, size and CRC. Loading an old
snapshot also restores its battery RAM, which will subsequently replace the
newer battery file. Back up the whole state directory before rolling back.
Existing FCEUX/Mario state locations remain separate; this template does not
import or overwrite another emulator's saves.

## Display and compatibility

`NES_DISPLAY=auto` prefers native Wayland when `WAYLAND_DISPLAY` or
`WAYLAND_SOCKET` is set, otherwise X11. Both use software rendering with a 4:3
letterboxed image. Wayland uses xdg-shell and compositor-supplied keymaps;
it has no client-drawn titlebar or native high-DPI buffer scaling. X11 uses
MIT-SHM when available; `NES_NO_SHM=1` selects image transfer instead.

Audio uses a local PulseAudio-protocol server, including `pipewire-pulse`.
`PULSE_SERVER=unix:/absolute/socket` overrides the default local socket.
Audio loss pauses play; F5 still saves and Space resumes silently. Restart
after restoring audio to regain sound. `--mute` skips audio initialization.

The frontend supports one keyboard controller. Standard iNES and NES 2.0
cartridges are validated before compilation. FCEUmm supports many mappers, but
each new ROM still needs gameplay testing. Battery persistence is limited to
standard mapper 0, 1 and 4 cartridges. Other battery layouts, persistent CHR,
VS/PlayChoice consoles, miscellaneous ROM payloads and special peripherals
are rejected. FDS, UNIF, gamepads, a second player, light guns and external BIOS
files are outside this template's scope. HD packs and NTSC filtering are off.

## Development

`static/game.c` handles the libretro core, input, configuration and persistence.
`static/platform*.c` supplies shared audio and the display backends.
`tools/manifest.py` owns cartridge validation and generated identity headers;
game-specific values stay in `game.json`. The bootstrap scripts pin and build
libraries independently of any cartridge. These are the reusable parts of the
original Mario integration.

Run `make test` for cartridge and initialization checks. Runtime validation
requires a graphical session; use isolated state directories and private
Xvfb/nested Weston sessions to protect real saves and desktop focus.
`DISPLAY=:97 NES_DISPLAY=x11 make test-runtime` builds a synthetic battery
cartridge and checks configuration, snapshots, periodic battery saves,
restart, rollback, corruption and failed writes. Point `WAYLAND_DISPLAY` at a
private compositor and set `NES_DISPLAY=wayland` to run the same checks there.
Evidence is retained under `NES_BUILD_DIR/runtime-check-*`; no commercial ROM
is needed. Do not run graphical tests on a desktop while using it.
`--version` records the ROM/core/build identity; `game.log` reports timing,
rendering, input and save outcomes. Visual gameplay, physical input/audio and
each target compositor remain necessary release checks.

Verified on 2026-11-13 with CachyOS/GCC 16: a fresh GCC/musl bootstrap and a
repeat bootstrap, eight cartridge/setup tests, and synthetic cartridge runtime
checks on Xvfb and native Wayland through nested Weston. These covered static
ELF validation, the WASD preset, snapshots, battery restart/rollback, corrupt
data rejection and failed writes; periodic battery saving was also checked
before process exit on X11. Contra Force (mapper 4) built and reached its title
and character selection screens, with input and snapshot saving checked.
Dragon Warrior (mapper 1, battery) compiled successfully; its gameplay has not
been validated. These local cartridge copies are outside the template.
Physical Wayland desktops, additional compositors and full-game compatibility
remain unverified.
