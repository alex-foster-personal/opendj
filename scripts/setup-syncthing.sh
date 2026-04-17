#!/usr/bin/env bash
# scripts/setup-syncthing.sh -- idempotent Syncthing folder-share config for
# ~/Music/DJ Library/. Phase 11, plan 11-01 step 6.
#
# Device-ID pairing is manual -- this script does NOT add remote devices.
# It prints instructions for the user to complete pairing on their other
# Mac. Safety rails:
#   * aborts if invoked with sudo (Syncthing must run as the user)
#   * aborts if Syncthing is not installed
#   * idempotent on re-run (no-op if folder already configured)
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    echo "error: do not run this script with sudo; Syncthing must run as the user" >&2
    exit 1
fi

SYNCTHING_BIN="/opt/homebrew/bin/syncthing"
if [[ ! -x "${SYNCTHING_BIN}" ]]; then
    # fall back to PATH lookup
    if command -v syncthing >/dev/null 2>&1; then
        SYNCTHING_BIN="$(command -v syncthing)"
    else
        cat <<EOF >&2
error: Syncthing is not installed.
Install via:
    brew install syncthing
Then re-run this script.
EOF
        exit 1
    fi
fi

CONFIG_XML="${HOME}/Library/Application Support/Syncthing/config.xml"
if [[ ! -f "${CONFIG_XML}" ]]; then
    echo "note: Syncthing config not yet initialised; starting it once to bootstrap..."
    "${SYNCTHING_BIN}" --generate="${HOME}/Library/Application Support/Syncthing" \
        >/dev/null 2>&1 || true
fi

DJ_LIBRARY="${HOME}/Music/DJ Library"
mkdir -p "${DJ_LIBRARY}"

FOLDER_ID="music-dj-library"
FOLDER_LABEL="Music DJ Library"

# Edit config.xml via python (xmltodict is optional; fall back to xmllint +
# awk-ish detection if missing). Idempotent: if the folder ID already exists,
# do nothing.
python3 - <<PYEOF "${CONFIG_XML}" "${FOLDER_ID}" "${FOLDER_LABEL}" "${DJ_LIBRARY}"
import sys
import xml.etree.ElementTree as ET

config_path, folder_id, label, path = sys.argv[1:]

tree = ET.parse(config_path)
root = tree.getroot()

for folder in root.findall("folder"):
    if folder.get("id") == folder_id:
        print(f"Folder '{folder_id}' already configured at {folder.get('path')}; no changes.")
        sys.exit(0)

folder_el = ET.SubElement(root, "folder", attrib={
    "id": folder_id,
    "label": label,
    "path": path,
    "type": "sendreceive",
    "rescanIntervalS": "3600",
    "fsWatcherEnabled": "true",
    "ignorePerms": "false",
    "autoNormalize": "true",
})
ET.SubElement(folder_el, "filesystemType").text = "basic"
ET.SubElement(folder_el, "minDiskFree", attrib={"unit": "%"}).text = "1"
ET.SubElement(folder_el, "versioning")
ET.SubElement(folder_el, "copiers").text = "0"
ET.SubElement(folder_el, "pullerMaxPendingKiB").text = "0"
ET.SubElement(folder_el, "hashers").text = "0"
ET.SubElement(folder_el, "order").text = "random"
ET.SubElement(folder_el, "ignoreDelete").text = "false"
ET.SubElement(folder_el, "scanProgressIntervalS").text = "0"
ET.SubElement(folder_el, "pullerPauseS").text = "0"
ET.SubElement(folder_el, "maxConflicts").text = "10"
ET.SubElement(folder_el, "disableSparseFiles").text = "false"
ET.SubElement(folder_el, "disableTempIndexes").text = "false"
ET.SubElement(folder_el, "paused").text = "false"
ET.SubElement(folder_el, "weakHashThresholdPct").text = "25"
ET.SubElement(folder_el, "markerName").text = ".stfolder"
ET.SubElement(folder_el, "copyOwnershipFromParent").text = "false"
ET.SubElement(folder_el, "modTimeWindowS").text = "0"
ET.SubElement(folder_el, "maxConcurrentWrites").text = "2"

tree.write(config_path, encoding="utf-8", xml_declaration=True)
print(f"Added folder '{folder_id}' at {path}")
PYEOF

# Surface the device-ID so the user can pair a second laptop.
DEVICE_ID=""
if "${SYNCTHING_BIN}" cli --help >/dev/null 2>&1; then
    DEVICE_ID="$("${SYNCTHING_BIN}" cli show system 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("myID",""))' 2>/dev/null || true)"
fi

cat <<EOF

Syncthing folder '${FOLDER_ID}' configured at ${DJ_LIBRARY}.

Next steps (manual):
  1. Start Syncthing:     brew services start syncthing
  2. Open the admin UI:   http://127.0.0.1:8384
  3. On your other Mac, Add Folder -> ID '${FOLDER_ID}', path ${DJ_LIBRARY}
EOF

if [[ -n "${DEVICE_ID}" ]]; then
    echo "  4. Add this device ID on the other machine:"
    echo "        ${DEVICE_ID}"
else
    echo "  4. Device ID will appear in the admin UI under Settings -> General."
fi
echo ""
echo "You are done when both Macs show 'Up to Date' for '${FOLDER_ID}'."
