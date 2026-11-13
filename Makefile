.DEFAULT_GOAL := help
export ROM GAME_ID TITLE REGION DEST
.PHONY: new controls-wasd help init bootstrap build launch run configure controls check test test-runtime
help:
	@printf '%s\n' 'make init ROM=/path/game.nes [GAME_ID=nes-example TITLE="Example" REGION=auto]' 'make new ROM=/path/game.nes DEST=/path/nes-game  Create an independent game directory' 'make bootstrap  Build pinned dependencies and the emulator core' 'make build      Embed the ROM and produce dist/<game id>' 'make run        Build if needed, then play (GAME_ARGS passes executable options)' 'make launch     Alias for run' 'make controls-wasd  Apply WASD movement, J/K buttons, Tab Select, Enter Start' 'make controls   Configure bindings (GAME_ARGS="--set a=x --set b=z")' 'make configure  Alias for controls' 'make check      Validate the manifest and original ROM checksum' 'make test       Run template/manifest regression tests without a ROM' 'make test-runtime  Build a synthetic cartridge and test saves in a private display session' 'NES_DISPLAY=auto|x11|wayland selects the display backend.' 'NES_STATE_DIR overrides ./saves with an absolute path; each game/hash has its own subdirectory.' 'NES_BUILD_DIR overrides ~/.cache/nes-static; builds require a Linux filesystem.' 'NES_LOG_DIR overrides ~/.local/state/nes-static-build for build logs.'
init:
	@./tools/manifest.py init
bootstrap:
	@bash tools/bootstrap-core
	@bash tools/bootstrap-wayland
build:
	@bash tools/build-game
launch run:
	@bash tools/run-game $(GAME_ARGS)
configure controls:
	@bash tools/run-game --configure $(GAME_ARGS)
check:
	@./tools/manifest.py check
test:
	@./tools/test.py
test-runtime:
	@./tools/check-runtime.py

controls-wasd:
	@bash tools/controls-wasd

new:
	@./tools/new-game.py
