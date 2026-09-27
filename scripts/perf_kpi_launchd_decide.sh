#!/usr/bin/env bash
# Decision + cleanup helpers for scripts/install_perf_kpi_launchd.sh's --nightly-only
# health-agent teardown. Sourced, never executed directly.

# health_agent_action <print_rc> echoes exactly one of:
#   bootout  -- print_rc == 0:   the agent is loaded, bootout then remove the plist.
#   absent   -- print_rc == 113: launchctl's "service not found", nothing to unload.
#   unknown  -- any other rc:    loaded state cannot be determined, leave the plist.
# and nothing else. Pure: no I/O, no launchctl, so it is testable by sourcing this
# file and asserting stdout for a given rc, with no PATH tricks or fakes.
health_agent_action() {
  local print_rc="$1"
  if [[ "$print_rc" -eq 0 ]]; then
    echo bootout
  elif [[ "$print_rc" -eq 113 ]]; then
    echo absent
  else
    echo unknown
  fi
}

# cleanup_stale_health_agent <uid> <label> <plist_path>
# Real I/O: calls the real launchctl (print, and bootout when loaded) plus rm,
# driven by health_agent_action's decision. This is the exact code
# install_perf_kpi_launchd.sh's --nightly-only path runs, factored out so a
# macOS test can exercise it against a uniquely-labelled throwaway agent
# without ever invoking the installer's main bootstrap loop (which touches
# the real com.af.perf-kpi-nightly label unconditionally, --nightly-only or
# not, and so must never run against a fake/throwaway target).
# Returns 1 (with a message on stderr) when the agent is loaded and refuses
# to unload, or when its loaded state cannot be determined.
cleanup_stale_health_agent() {
  local uid="$1" label="$2" plist_path="$3"
  local print_rc=0
  launchctl print "gui/$uid/$label" >/dev/null 2>&1 || print_rc=$?
  case "$(health_agent_action "$print_rc")" in
    bootout)
      if ! launchctl bootout "gui/$uid/$label"; then
        echo "[ERROR] $label is loaded and would not unload; run" \
          "launchctl bootout gui/$uid/$label, then re-run" >&2
        return 1
      fi
      ;;
    absent)
      : # nothing loaded, nothing to unload
      ;;
    unknown)
      echo "[ERROR] launchctl print gui/$uid/$label exited $print_rc, so" \
        "whether the health agent is loaded is unknown; plist left in place" >&2
      return 1
      ;;
  esac
  if [[ -e "$plist_path" ]]; then
    rm -f "$plist_path"
    echo "[OK] removed the existing $label agent (--nightly-only)"
  fi
}
