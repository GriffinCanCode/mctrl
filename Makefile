# Building, installing and updating MindControl.app.
#
#   make app       first build, and after changing dependencies (minutes)
#   make update    push this project into the installed app (seconds)
#   make dmg       cut the installer
#
# A certificate on the keychain is used if there is one, which is what keeps the
# Camera, Accessibility and Menu Bar grants across builds; SIGN_IDENTITY overrides
# it, with "-" forcing ad-hoc. See packaging/identity.sh for why that matters.

APP       := build/MindControl.app
INSTALLED := /Applications/MindControl.app
PY        := $(APP)/Contents/Resources/python/bin/python3
BRIDGE    := native/.build/release/mindcontrol-bridge
LOG       := $(HOME)/.local/state/mindcontrol/app.log
SIGN      := $(shell packaging/identity.sh)

.DEFAULT_GOAL := help
.PHONY: help app icon refresh install update dmg run stop restart logs permissions lint test clean uninstall

help: ## list what this can do
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  \033[1;35m%-12s\033[0m %s\n", $$1, $$2}'

# Anything that edits the bundle needs one to exist first.
$(APP):
	packaging/build_app.sh

app: ## build the bundle from scratch: its own CPython, every pinned dependency
	packaging/build_app.sh

refresh: | $(APP) ## rebuild only this project and the native helper into the bundle
	@$(PY) -m pip install --quiet --no-input --disable-pip-version-check \
		--no-deps --force-reinstall .
	@if [ -f native/Package.swift ] && swift build -c release --package-path native >/dev/null; then \
		cp $(BRIDGE) $(APP)/Contents/MacOS/mindcontrol-bridge; \
	else \
		echo "  helper unchanged: nothing new built"; \
	fi
	@packaging/launcher.sh $(APP)
	@codesign --force --deep --sign '$(SIGN)' $(APP) 2>/dev/null \
		|| echo "  could not sign as '$(SIGN)'"
	@echo "refreshed $(APP)"

icon: | $(APP) ## re-render the icon into the bundle
	@PYTHON=$(PY) packaging/icns.sh $(APP)/Contents/Resources/MindControl.icns
	@codesign --force --deep --sign '$(SIGN)' $(APP) 2>/dev/null || true

install: stop | $(APP) ## copy what changed into /Applications
	@rsync -a --delete $(APP)/ $(INSTALLED)/
	@touch $(INSTALLED)
	@echo "installed $(INSTALLED)"

update: refresh install run ## refresh, reinstall and relaunch — the everyday one

dmg: | $(APP) ## cut dist/MindControl-<version>.dmg
	packaging/make_dmg.sh

run: ## launch the installed app
	@open -a $(INSTALLED)

stop: ## quit it, if it is running
	@osascript -e 'quit app "MindControl"' >/dev/null 2>&1 || true
	@pkill -f '$(INSTALLED)/Contents' >/dev/null 2>&1 || true
	@sleep 1

restart: stop run ## bounce it

logs: ## follow what the app would have printed to a terminal
	@tail -f -n 40 $(LOG)

permissions: ## open the Privacy and Menu Bar panes the app needs
	@open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Camera'
	@sleep 1
	@open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
	@sleep 1
	@open 'x-apple.systempreferences:com.apple.ControlCenter-Settings.extension'

lint: ## ruff and shellcheck
	uvx ruff check .
	uvx ruff format --check .
	@command -v shellcheck >/dev/null && shellcheck packaging/*.sh || echo "shellcheck not installed"

test: ## run the test suite
	uv run pytest

uninstall: stop ## remove the installed app (permissions have to be revoked by hand)
	@rm -rf $(INSTALLED)
	@echo "removed $(INSTALLED)"

clean: ## drop build products
	@rm -rf build dist
