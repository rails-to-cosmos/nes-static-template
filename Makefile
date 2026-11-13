.DEFAULT_GOAL := help
export ROM GAME_ID TITLE REGION DEST
.PHONY: help init new bootstrap build launch run configure controls controls-wasd check test typecheck test-runtime
help:
	@./tools/nes-static --help
init new bootstrap build controls-wasd check test typecheck test-runtime:
	@./tools/nes-static $@
launch run:
	@./tools/nes-static run $(GAME_ARGS)
configure controls:
	@./tools/nes-static controls $(GAME_ARGS)
