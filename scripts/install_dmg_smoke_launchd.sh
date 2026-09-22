#!/usr/bin/env bash
# Render ops/dmg-smoke/com.af.dmg-smoke.plist.template and optionally install
# the Air dmg-smoke launchd agent.
#
# Loading the job needs the maintainer's explicit say-so plus --install. Default and
# --render-to stop before launchctl.
#
# USAGE
#   MDT_MACOS_SIGNING_IDENTITY=... MDT_MACOS_NOTARY_KEYCHAIN_PROFILE=... \
#     scripts/install_dmg_smoke_launchd.sh
#   scripts/install_dmg_smoke_launchd.sh --render-to FILE
#   scripts/install_dmg_smoke_launchd.sh --install
set -euo pipefail

shopt -u patsub_replacement 2>/dev/null || true

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/ops/dmg-smoke/com.af.dmg-smoke.plist.template"
RUNNER_SRC="$REPO_ROOT/ops/dmg-smoke/run.sh"
LABEL="com.af.dmg-smoke"

INSTALL=0
RENDER_ONLY=""
HOST_LABEL="air"
while (($#)); do
  case "$1" in
    --install) INSTALL=1; shift ;;
    --render-to)
      RENDER_ONLY="${2:-}"
      [ -n "$RENDER_ONLY" ] || { echo "[ERROR] --render-to needs a path" >&2; exit 2; }
      shift 2
      ;;
    --host-label)
      HOST_LABEL="${2:-}"
      [ -n "$HOST_LABEL" ] || { echo "[ERROR] --host-label needs a value" >&2; exit 2; }
      shift 2
      ;;
    *)
      echo "[ERROR] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ "$INSTALL" = 1 ] && [ "$(uname -s)" != "Darwin" ]; then
  echo "[ERROR] --install is Darwin-only (this host is $(uname -s))" >&2
  exit 2
fi

[ -f "$TEMPLATE" ] || { echo "[ERROR] no template at $TEMPLATE" >&2; exit 1; }
[ -f "$RUNNER_SRC" ] || { echo "[ERROR] no runner at $RUNNER_SRC" >&2; exit 1; }

_resolve_signing_env() {
  local name="$1"
  if [ -n "${!name:-}" ]; then
    return 0
  fi
  local from_shell
  from_shell="$(zsh -c "source ~/.zshenv 2>/dev/null; printenv $name" 2>/dev/null || true)"
  if [ -n "$from_shell" ]; then
    export "$name=$from_shell"
    return 0
  fi
  return 1
}

if ! _resolve_signing_env MDT_MACOS_SIGNING_IDENTITY \
  || ! _resolve_signing_env MDT_MACOS_NOTARY_KEYCHAIN_PROFILE; then
  echo "[ERROR] MDT_MACOS_SIGNING_IDENTITY and MDT_MACOS_NOTARY_KEYCHAIN_PROFILE are required" >&2
  echo "        Export both or define them in ~/.zshenv" >&2
  exit 2
fi

xml_escape() {
  local s=$1
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  printf '%s' "$s"
}

rendered="$(cat "$TEMPLATE")"
rendered=${rendered//__ABS_HOME__/$(xml_escape "$HOME")}
rendered=${rendered//__MDT_MACOS_SIGNING_IDENTITY__/$(xml_escape "$MDT_MACOS_SIGNING_IDENTITY")}
rendered=${rendered//__MDT_MACOS_NOTARY_KEYCHAIN_PROFILE__/$(xml_escape "$MDT_MACOS_NOTARY_KEYCHAIN_PROFILE")}
rendered=${rendered//__MDT_DMG_SMOKE_HOST_LABEL__/$(xml_escape "$HOST_LABEL")}

TARGET="${RENDER_ONLY:-$HOME/Library/LaunchAgents/com.af.dmg-smoke.plist}"
mkdir -p "$(dirname "$TARGET")"
rm -f "$TARGET"
staged="$(mktemp "${TARGET}.XXXXXX")"
trap 'rm -f "$staged"' EXIT
printf '%s\n' "$rendered" > "$staged"

if grep -q '__[A-Z_]*__' "$staged"; then
  echo "[ERROR] unrendered placeholder left in the rendered plist:" >&2
  grep -n '__[A-Z_]*__' "$staged" >&2
  exit 1
fi

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$staged" >/dev/null || { echo "[ERROR] rendered plist is not valid" >&2; exit 1; }
elif command -v python3 >/dev/null 2>&1; then
  python3 -c 'import plistlib, pathlib, sys; plistlib.loads(pathlib.Path(sys.argv[1]).read_bytes())' "$staged" \
    || { echo "[ERROR] rendered plist is not valid" >&2; exit 1; }
else
  echo "[ERROR] rendered plist is not valid (neither plutil nor python3 on PATH)" >&2
  exit 1
fi

mv -f "$staged" "$TARGET"
trap - EXIT

if [ "$INSTALL" = 1 ]; then
  runtime_dir="$HOME/code/afmac/dmg-smoke"
  mkdir -p "$runtime_dir" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  rm -f "$runtime_dir/run.sh"
  cp "$RUNNER_SRC" "$runtime_dir/run.sh"
  chmod +x "$runtime_dir/run.sh"
fi

if [ -n "$RENDER_ONLY" ]; then
  echo "[OK] rendered $LABEL plist to $TARGET (launchctl not touched)"
  exit 0
fi

if [ "$INSTALL" = 1 ]; then
  uid="$(id -u)"
  launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$TARGET"
  launchctl print "gui/$uid/$LABEL" | grep -E "state = " || {
    echo "[ERROR] job did not register" >&2
    exit 1
  }
  echo "[OK] $LABEL installed; runner at $HOME/code/afmac/dmg-smoke/run.sh"
  exit 0
fi

echo "[OK] rendered $LABEL plist to $TARGET (launchctl not touched; use --install after the maintainer says so)"
