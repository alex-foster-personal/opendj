#!/usr/bin/env bash
# Launch the `dispatch` MCP server for THIS checkout. The server lives in
# fleet-af (`fleet--mcp/`, moved there Thu 1 Oct 2026); this stub is the
# caller side, which .mcp.json and `just dispatch-mcp-register` launch.
#
# It names this checkout in DISPATCH_MCP_CHECKOUT, because only the launching
# checkout knows which webui backend (`just webui-ports`) holds the fan-out
# ledger the server's ledger tools read and claim.
#
# fleet-af is found at $FLEET_AF_HOME, else the pinned, pull-only LIVE clone
# $HOME/.local/share/fleet-af-live (Thu 1 Oct 2026). Never the shared dev
# checkout ~/code/fleet-af: agents switch its branch, so a server launched from
# it runs whatever branch is checked out. If the launcher is not there, this
# exits 2 and says so rather than starting a server that cannot measure
# anything; there is no fallback to another checkout.
set -euo pipefail

checkout="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fleet_af="${FLEET_AF_HOME:-$HOME/.local/share/fleet-af-live}"
launcher="$fleet_af/fleet--mcp/dispatch_mcp.sh"
if [[ ! -x "$launcher" ]]; then
  echo "[ERROR] dispatch MCP: no fleet-af launcher at $launcher." \
    "Create the fleet-af live clone (bash <fleet-af>/deploy--live-clone/install.sh," \
    "or 'just live-clone::install' in fleet-af) or set FLEET_AF_HOME." >&2
  exit 2
fi
DISPATCH_MCP_CHECKOUT="$checkout" exec "$launcher" "$@"
