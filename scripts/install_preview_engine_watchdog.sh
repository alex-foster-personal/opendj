#!/usr/bin/env bash
# Install the dedicated launchd agent that probes the live preview engine.
set -euo pipefail

INSTALL_DIR="$HOME/Library/Application Support/OpenDJ Preview"
PLIST="$HOME/Library/LaunchAgents/com.af.opendj-preview-engine-watchdog.plist"
PREVIEWCTL=""

while (($#)); do
    case "$1" in
        --previewctl) PREVIEWCTL="${2:?--previewctl needs a path}"; shift 2 ;;
        --install-dir) INSTALL_DIR="${2:?--install-dir needs a path}"; shift 2 ;;
        *) echo "[ERROR] unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$PREVIEWCTL" ]] || { echo "[ERROR] --previewctl is required" >&2; exit 2; }
[[ -x "$PREVIEWCTL" ]] || { echo "[ERROR] previewctl is not executable: $PREVIEWCTL" >&2; exit 2; }

mkdir -p "$INSTALL_DIR" "$(dirname "$PLIST")"
install -m 644 scripts/preview_engine_watchdog.py "$INSTALL_DIR/preview_engine_watchdog.py"
cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.af.opendj-preview-engine-watchdog</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$INSTALL_DIR/preview_engine_watchdog.py</string>
    <string>--health-url</string><string>http://127.0.0.1:8728/api/v1/health</string>
    <string>--state-file</string><string>$INSTALL_DIR/engine-watchdog-state.json</string>
    <string>--restart-command</string><string>$PREVIEWCTL restart engine</string>
    <string>--timeout-s</string><string>5</string><string>--failure-threshold</string><string>2</string>
    <string>--restart-cooldown-s</string><string>120</string><string>--restart-timeout-s</string><string>30</string>
  </array>
  <key>StartInterval</key><integer>60</integer><key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$INSTALL_DIR/engine-watchdog.log</string>
  <key>StandardErrorPath</key><string>$INSTALL_DIR/engine-watchdog.log</string>
</dict></plist>
EOF
plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)/com.af.opendj-preview-engine-watchdog" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl print "gui/$(id -u)/com.af.opendj-preview-engine-watchdog" >/dev/null
echo "[OK] preview engine watchdog installed: $PLIST"
