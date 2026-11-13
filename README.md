# NES static template

Build standalone Linux x86-64 games with an embedded ROM, pinned FCEUmm core,
X11/native Wayland video and PulseAudio audio. No commercial ROMs are included.

## Start

On Arch/CachyOS, install build prerequisites once:

```sh
sudo pacman -S --needed gcc binutils make cmake ninja pkgconf bison expat curl coreutils bash uv
make new ROM=/path/game.nes DEST="$HOME/games/nes-my-game" TITLE="My Game"
cd "$HOME/games/nes-my-game"
make run
```

Each game gets an independent template copy, the original ROM filename/bytes,
`game.json`, and `launch-nes-my-game`. The destination must be new.
`GAME_ID=nes-example` overrides its directory-derived ID; `REGION=auto|ntsc|pal|dendy`
selects timing. Keep the game ID stable once you have saves.

Use `make init ROM=/path/game.nes` to initialize the current checkout instead.
`make bootstrap` builds dependencies, `make build` produces `dist/<game id>`,
and `make run`/`make launch` rebuild stale binaries before playing.
The executable runs independently of Python, uv, external ROMs and shared
libraries. Retain matching source and license notices when distributing it.

## Controls and saves

W/A/S/D move; J/K are A/B; Tab/Enter are Select/Start.
Space pauses, F2 resets, F5/F8 save/load, `[`/`]` select a slot,
F11 toggles fullscreen, Escape quits, and F1 shows controls.
Focus loss pauses play; press Space to resume.

```sh
make controls                         # Terminal settings editor
make controls GAME_ARGS='--set a=x --set b=z --set volume=70'
make controls-wasd                    # Restore default bindings; retain other settings
make run GAME_ARGS='--mute'
```

Saves default to `./saves` in the game directory, or beside a copied executable.
Set `NES_STATE_DIR=/absolute/path` to override this. Older home-directory saves
remain available with `NES_STATE_DIR="$HOME/.local/state/nes-static"`.
Each `<game id>/<ROM SHA-256>/` contains settings, `game.log`, snapshots and
supported battery RAM. Existing saves are preserved; generated copies exclude them.
Battery RAM saves every five seconds and on exit. Loading an old snapshot also
rolls back battery RAM. Invalid saves are reported and preserved.

## Configuration

| Variable | Purpose / default |
| --- | --- |
| `NES_DISPLAY` | `auto` prefers Wayland when available; also `x11` or `wayland` |
| `NES_STATE_DIR` | Absolute save/settings/log root; defaults to local `saves/` |
| `NES_BUILD_DIR` | `${XDG_CACHE_HOME:-$HOME/.cache}/nes-static`; Linux filesystem, absolute path without whitespace |
| `NES_BUILD_JOBS` | Parallel compilation jobs; `4` |
| `NES_LOG_DIR` | `${XDG_STATE_HOME:-$HOME/.local/state}/nes-static-build`; `bootstrap.log` and `build.log` |

Builds use system GCC and pinned musl/libraries from `static/*.lock`.
Verified downloads and completed stages are shared under a setup lock.
Generated game directories may live on exFAT; the build cache must support Unix
symlinks. The first build needs network access. No system packages are installed
by bootstrap.

Audio requires PulseAudio or `pipewire-pulse`; `--mute` disables it.
`PULSE_SERVER=unix:/absolute/socket` selects another local server.
Both display backends use software rendering. Wayland lacks a client titlebar
and native high-DPI scaling; `NES_NO_SHM=1` disables X11 shared-memory transfer.

The frontend supports one keyboard player and standard iNES/NES 2.0 cartridges.
Battery persistence supports standard mappers 0, 1 and 4. Special peripherals,
persistent CHR, FDS/UNIF and external BIOS files are unsupported. Each new game
needs gameplay testing; mapper support alone does not establish compatibility.

## Development

`nes_static/` is the typed Python CLI: cartridge/project handling, build recipes,
and shared file/locking operations. `static/` contains the C runtime.
`tools/nes-static` selects the checkout and runs `uv run python -m nes_static`.
Python, Meson and mypy are managed by `pyproject.toml`, `.python-version` and
`uv.lock`. Dependency changes belong in the project and lockfile together.

The shim keeps uv environments under the Linux cache, keyed by lockfile hash;
`UV_PROJECT_ENVIRONMENT` overrides the location. The project is intentionally
unpackaged so each game uses its own source while sharing dependencies.
On a Linux filesystem, `uv run python -m nes_static --help` also works directly.

Run `make check`, `make test` and `make typecheck` for manifest validation,
regression tests and strict typing. `make test-runtime` builds a synthetic battery
cartridge and tests snapshots, corruption handling and persistence. Run it on a
private Xvfb or nested Wayland session with `NES_DISPLAY` set appropriately;
evidence stays in `NES_BUILD_DIR/runtime-check-*`. Physical input/audio and
visual gameplay still need manual checks. `make help` lists all commands.
