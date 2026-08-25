VENV ?= $(HOME)/.cache/localflow-venv
PY   := $(VENV)/bin/python
REPO := $(shell pwd)
AGENT := com.cscmsg.localflow.flowd
AGENT_PLIST := $(HOME)/Library/LaunchAgents/$(AGENT).plist

.PHONY: bootstrap test app install daemon install-daemon uninstall-daemon status logs clean

## First-time setup: venv + model download (~4.5 GB).
bootstrap:
	./scripts/bootstrap.sh

test:
	$(PY) -m pytest tests/ -q

app:
	./scripts/make_app.sh

## Replace the installed copy in ~/Applications and relaunch it.
## (~/Applications, not /Applications: the latter needs admin rights here.)
install: app
	pkill -x LocalFlow 2>/dev/null || true
	mkdir -p $(HOME)/Applications
	rm -rf $(HOME)/Applications/LocalFlow.app
	ditto .dist/LocalFlow.app $(HOME)/Applications/LocalFlow.app
	@# Delete the staging copy. Excluding it from Spotlight is not enough --
	@# dot-directories ARE indexed (verified with mdfind), and a marker file only
	@# affects future indexing, not an entry already held. Two identical bundles
	@# with the same name and bundle id are indistinguishable in Spotlight
	@# results, and the staging one goes stale the moment you install without
	@# rebuilding. The only reliable fix is for it not to exist.
	rm -rf .dist
	@echo ""
	@echo "Installed. Launch it from Finder or Spotlight -- NOT from a terminal."
	@echo "macOS evaluates microphone access against the process responsible for"
	@echo "the launch. Started from a shell, LocalFlow inherits that shell's"
	@echo "responsibility and the request is denied with no prompt and no entry"
	@echo "in System Settings, which looks like a broken app."

## Run the daemon in the foreground -- the way to watch what it is doing.
daemon:
	$(PY) daemon/flowd.py --verbose

## Install the daemon as a LaunchAgent so it survives logout and reboot.
install-daemon:
	mkdir -p $(HOME)/Library/LaunchAgents
	sed -e 's|@PYTHON@|$(PY)|g' -e 's|@REPO@|$(REPO)|g' -e 's|@HOME@|$(HOME)|g' \
		scripts/launchagent.plist.in > $(AGENT_PLIST)
	launchctl bootout gui/$(shell id -u)/$(AGENT) 2>/dev/null || true
	launchctl bootstrap gui/$(shell id -u) $(AGENT_PLIST)
	@echo "daemon installed; models take ~25s to load on first start"

uninstall-daemon:
	launchctl bootout gui/$(shell id -u)/$(AGENT) 2>/dev/null || true
	rm -f $(AGENT_PLIST)

status:
	@$(PY) daemon/flowctl.py status

logs:
	@tail -f "$(HOME)/Library/Application Support/LocalFlow/flowd.log"

clean:
	rm -rf app/.build build .dist
