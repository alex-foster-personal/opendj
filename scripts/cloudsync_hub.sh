#!/usr/bin/env bash
# CloudSync hub lifecycle:
#   start [--serve-r2-off] [--backup-r2-off] [--allowed-hosts-clear]
#   stop | status | backup | restore <file> [data-dir]
#
# Driven by the `just cloudsync-hub-*` recipes. The command a unit runs, its
# environment, and the probe are all owned by apps/sync_hub/hub_deploy.py;
# this script only installs the rendered units and drives the host's service
# manager: systemd --user on Linux, launchd on macOS. Nothing here binds a
# wider interface: tailnet exposure is a separate, manual `tailscale serve`
# step ((private path)).
#
# Overrides (each resolved value is printed before use):
#   OPENDJ_HUB_DATA_DIR    default: hub_deploy default-data-dir
#   OPENDJ_HUB_PORT        default: hub_deploy CFG.HUB_PORT
#   OPENDJ_HUB_BACKUP_DIR  default: <data dir>-backups
#   OPENDJ_HUB_BACKUP_KEEP default: hub_deploy CFG.BACKUP_KEEP
#   OPENDJ_HUB_BACKUP_R2   1 = upload each backup to R2 via doppler (general/dev_personal)
#   OPENDJ_HUB_SERVE_R2    1 = wrap the serve unit in doppler for R2 stem presign creds
#   OPENDJ_HUB_SERVE_DOPPLER_CONFIG  Doppler config name (required when serve R2 is on)
#   OPENDJ_HUB_ALLOWED_HOSTS  comma-separated MagicDNS hostname(s) for tailnet spokes
#   OPENDJ_HUB_SERVICE_MANAGER  systemd | launchd; default: by uname (Linux | Darwin)
#   OPENDJ_HUB_CONFIG_DIR  default: ${XDG_CONFIG_HOME:-~/.config}/opendj-hub
#
# The four options above the service manager are REMEMBERED (CLOUDSYNC-4686).
# `start` writes them to <config dir>/serve.env after a successful render, and
# every later start, status and backup reuses a saved value whenever its
# variable is unset. So an upgrade that re-runs a bare `start` re-renders the
# same units, where before it silently rendered R2 serving away. With no saved
# file yet, the values are adopted from the units already installed. Turning a
# saved option off is a deliberate flag on start (--serve-r2-off,
# --backup-r2-off, --allowed-hosts-clear): an exported `=0` against a saved
# `1` is refused, because a wrapper that defaults a variable to 0 is the same
# forgetting in a different place.
#
# Must run under bash 3.2 (stock macOS /bin/bash): under `set -u` it treats an
# empty "${array[@]}" as unbound, so arrays that can be empty expand as
# ${array[@]+"${array[@]}"}. (private path) runs /bin/bash.
set -euo pipefail
unset VIRTUAL_ENV

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
  echo "[ERROR] uv is not on PATH" >&2
  exit 1
fi

hub_py() { "${UV_BIN}" run --no-sync python -m apps.sync_hub.hub_deploy "$@"; }
cfg_value() { "${UV_BIN}" run --no-sync python -c "from apps.sync_hub.hub_deploy import CFG; print(CFG.$1)"; }

HUB_DATA_DIR="${OPENDJ_HUB_DATA_DIR:-$(hub_py default-data-dir)}"
HUB_PORT="${OPENDJ_HUB_PORT:-$(cfg_value HUB_PORT)}"
HUB_BACKUP_DIR="${OPENDJ_HUB_BACKUP_DIR:-${HUB_DATA_DIR}-backups}"
HUB_BACKUP_KEEP="${OPENDJ_HUB_BACKUP_KEEP:-$(cfg_value BACKUP_KEEP)}"
HUB_CONFIG_DIR="${OPENDJ_HUB_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/opendj-hub}"
SERVE_ENV_FILE="${HUB_CONFIG_DIR}/serve.env"
SAVED_KEYS="OPENDJ_HUB_SERVE_R2 OPENDJ_HUB_SERVE_DOPPLER_CONFIG OPENDJ_HUB_BACKUP_R2 OPENDJ_HUB_ALLOWED_HOSTS"
HUB_URL="http://127.0.0.1:${HUB_PORT}"
DOPPLER_PROJECT="general"
DOPPLER_CONFIG="dev_personal"
START_WAIT_S=90

host_service_manager() {
  case "$(uname -s)" in
    Linux) echo "systemd" ;;
    Darwin) echo "launchd" ;;
    *) echo "[ERROR] unsupported OS $(uname -s); the hub runs on Linux or macOS" >&2; exit 1 ;;
  esac
}

case "${OPENDJ_HUB_SERVICE_MANAGER:-}" in
  "") SERVICE_MANAGER="$(host_service_manager)" ;;
  systemd | launchd) SERVICE_MANAGER="${OPENDJ_HUB_SERVICE_MANAGER}" ;;
  *) echo "[ERROR] OPENDJ_HUB_SERVICE_MANAGER=${OPENDJ_HUB_SERVICE_MANAGER} is not understood; use systemd or launchd" >&2; exit 1 ;;
esac

unit_dir() {
  if [[ "${SERVICE_MANAGER}" == "systemd" ]]; then
    echo "${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
  elif [[ "${SERVICE_MANAGER}" == "launchd" ]]; then
    echo "${HOME}/Library/LaunchAgents"
  fi
}

#----- remembered options (serve.env) ---------------------------------------------

# KEY=VALUE lines: serve.env when it exists, else what the installed units
# express, else nothing. SAVED_SOURCE names which, for the printed config.
load_saved_options() {
  if [[ -f "${SERVE_ENV_FILE}" ]]; then
    SAVED_SOURCE="saved"
    SAVED_TEXT="$(grep -v -e '^#' -e '^$' "${SERVE_ENV_FILE}" || true)"
  else
    SAVED_SOURCE="installed unit"
    SAVED_TEXT="$(hub_py installed-serve-options --kind "${SERVICE_MANAGER}" --unit-dir "$(unit_dir)")"
  fi
  local line key
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    key="${line%%=*}"
    if [[ "${line}" != *=* || " ${SAVED_KEYS} " != *" ${key} "* ]]; then
      echo "[ERROR] ${SERVE_ENV_FILE} (${SAVED_SOURCE}): line '${line}' is not one of: ${SAVED_KEYS}" >&2
      exit 1
    fi
  done <<< "${SAVED_TEXT}"
}

saved_option() {
  local key="$1" line
  while IFS= read -r line; do
    if [[ "${line}" == "${key}="* ]]; then
      printf '%s' "${line#*=}"
    fi
  done <<< "${SAVED_TEXT}"
}

# resolve_toggle <name> <env value> <saved value> <off flag given: 0|1> <off flag>
# Prints "<value> <source>". Runs in a command substitution, so `exit` here
# fails the assignment and `set -e` stops the script.
resolve_toggle() {
  local name="$1" from_env="$2" saved="$3" off_given="$4" off_flag="$5"
  if [[ "${off_given}" == "1" ]]; then
    if [[ "${from_env}" == "1" ]]; then
      echo "[ERROR] ${name}=1 and ${off_flag} contradict each other" >&2
      exit 1
    fi
    echo "0 ${off_flag}"
  elif [[ -n "${from_env}" ]]; then
    if [[ "${COMMAND}" == "start" && "${from_env}" == "0" && "${saved}" == "1" ]]; then
      echo "[ERROR] ${name}=0 would turn off an option this hub has on (${SAVED_SOURCE}); unset it to keep it on, or pass ${off_flag} to turn it off deliberately" >&2
      exit 1
    fi
    echo "${from_env} env"
  elif [[ -n "${saved}" ]]; then
    echo "${saved} ${SAVED_SOURCE}"
  else
    echo "0 default"
  fi
}

resolve_options() {
  load_saved_options
  local resolved
  resolved="$(resolve_toggle OPENDJ_HUB_SERVE_R2 "${OPENDJ_HUB_SERVE_R2:-}" \
    "$(saved_option OPENDJ_HUB_SERVE_R2)" "${SERVE_R2_OFF}" --serve-r2-off)"
  HUB_SERVE_R2="${resolved%% *}" HUB_SERVE_R2_SOURCE="${resolved#* }"
  resolved="$(resolve_toggle OPENDJ_HUB_BACKUP_R2 "${OPENDJ_HUB_BACKUP_R2:-}" \
    "$(saved_option OPENDJ_HUB_BACKUP_R2)" "${BACKUP_R2_OFF}" --backup-r2-off)"
  HUB_BACKUP_R2="${resolved%% *}" HUB_BACKUP_R2_SOURCE="${resolved#* }"
  HUB_SERVE_DOPPLER_CONFIG="${OPENDJ_HUB_SERVE_DOPPLER_CONFIG:-$(saved_option OPENDJ_HUB_SERVE_DOPPLER_CONFIG)}"
  if [[ "${ALLOWED_HOSTS_CLEAR}" == "1" ]]; then
    if [[ -n "${OPENDJ_HUB_ALLOWED_HOSTS:-}" ]]; then
      echo "[ERROR] OPENDJ_HUB_ALLOWED_HOSTS and --allowed-hosts-clear contradict each other" >&2
      exit 1
    fi
    HUB_ALLOWED_HOSTS=""
  else
    HUB_ALLOWED_HOSTS="${OPENDJ_HUB_ALLOWED_HOSTS:-$(saved_option OPENDJ_HUB_ALLOWED_HOSTS)}"
  fi
}

# Written only after a render succeeded: the file records what is installed.
save_options() {
  mkdir -p "${HUB_CONFIG_DIR}"
  local partial="${SERVE_ENV_FILE}.partial.$$"
  {
    echo "# Written by scripts/cloudsync_hub.sh start; reused by every later start."
    echo "# Turn an option off with a start flag, not by editing: see the script header."
    echo "OPENDJ_HUB_SERVE_R2=${HUB_SERVE_R2}"
    echo "OPENDJ_HUB_SERVE_DOPPLER_CONFIG=${HUB_SERVE_DOPPLER_CONFIG}"
    echo "OPENDJ_HUB_BACKUP_R2=${HUB_BACKUP_R2}"
    echo "OPENDJ_HUB_ALLOWED_HOSTS=${HUB_ALLOWED_HOSTS}"
  } > "${partial}"
  mv "${partial}" "${SERVE_ENV_FILE}"
  echo "[INFO] saved serve options to ${SERVE_ENV_FILE}"
}

print_config() {
  echo "[INFO] manager=${SERVICE_MANAGER} data_dir=${HUB_DATA_DIR} url=${HUB_URL}"
  echo "[INFO] backups=${HUB_BACKUP_DIR} keep=${HUB_BACKUP_KEEP} r2=${HUB_BACKUP_R2} (${HUB_BACKUP_R2_SOURCE})"
  echo "[INFO] serve_r2=${HUB_SERVE_R2} (${HUB_SERVE_R2_SOURCE}) serve_doppler_config=${HUB_SERVE_DOPPLER_CONFIG}"
  echo "[INFO] allowed_hosts=${HUB_ALLOWED_HOSTS:-<unset>} serve_options_file=${SERVE_ENV_FILE}"
}

#----- units ------------------------------------------------------------------------

validate_serve_r2() {
  if [[ "${HUB_SERVE_R2}" == "1" ]]; then
    if [[ -z "${HUB_SERVE_DOPPLER_CONFIG}" ]]; then
      echo "[ERROR] OPENDJ_HUB_SERVE_R2=1 needs OPENDJ_HUB_SERVE_DOPPLER_CONFIG" >&2
      exit 1
    fi
    if ! command -v doppler > /dev/null 2>&1; then
      echo "[ERROR] OPENDJ_HUB_SERVE_R2=1 needs the doppler CLI on PATH" >&2
      exit 1
    fi
  elif [[ "${HUB_SERVE_R2}" != "0" ]]; then
    echo "[ERROR] OPENDJ_HUB_SERVE_R2=${HUB_SERVE_R2} is not understood; use 1 or 0" >&2
    exit 1
  fi
}

render_units() {
  validate_serve_r2
  local r2_args=() serve_r2_args=()
  if [[ "${HUB_BACKUP_R2}" == "1" ]]; then
    local doppler_bin
    doppler_bin="$(command -v doppler || true)"
    if [[ -z "${doppler_bin}" ]]; then
      echo "[ERROR] OPENDJ_HUB_BACKUP_R2=1 needs the doppler CLI on PATH" >&2
      exit 1
    fi
    r2_args=(--upload-r2 --doppler "${doppler_bin}")
  elif [[ "${HUB_BACKUP_R2}" != "0" ]]; then
    echo "[ERROR] OPENDJ_HUB_BACKUP_R2=${HUB_BACKUP_R2} is not understood; use 1 or 0" >&2
    exit 1
  fi
  if [[ "${HUB_SERVE_R2}" == "1" ]]; then
    serve_r2_args=(--serve-r2 --doppler "$(command -v doppler)" --serve-doppler-config "${HUB_SERVE_DOPPLER_CONFIG}")
  fi
  local allowed_hosts_args=()
  if [[ -n "${HUB_ALLOWED_HOSTS}" ]]; then
    allowed_hosts_args=(--allowed-hosts "${HUB_ALLOWED_HOSTS}")
  fi
  hub_py render --kind "${SERVICE_MANAGER}" --out-dir "$(unit_dir)" --repo-root "${REPO_ROOT}" \
    --uv "${UV_BIN}" --data-dir "${HUB_DATA_DIR}" --port "${HUB_PORT}" \
    --backup-dest "${HUB_BACKUP_DIR}" --keep "${HUB_BACKUP_KEEP}" \
    ${r2_args[@]+"${r2_args[@]}"} ${serve_r2_args[@]+"${serve_r2_args[@]}"} \
    ${allowed_hosts_args[@]+"${allowed_hosts_args[@]}"}
}

wait_for_hub() {
  local waited=0
  local started_at=$SECONDS
  # Identity only: "is the right hub answering yet". The full verdict, R2
  # presign included, is cmd_status right after, and it decides start's exit.
  until hub_py status --identity-only --data-dir "${HUB_DATA_DIR}" --url "${HUB_URL}" > /dev/null; do
    if (( waited >= START_WAIT_S )); then
      echo "[ERROR] hub did not answer as a hub within ${START_WAIT_S}s" >&2
      return 1
    fi
    sleep 3
    waited=$(( waited + 3 ))
  done
  echo "[INFO] hub answered as a hub after $((SECONDS - started_at))s (slept ${waited}s of budget ${START_WAIT_S}s)"
}

cmd_start() {
  print_config
  if [[ -z "${HUB_ALLOWED_HOSTS}" ]]; then
    echo "[WARN] OPENDJ_HUB_ALLOWED_HOSTS is unset: tailnet spokes via tailscale serve will get 403 HOST_NOT_ALLOWED until you set it to the hub MagicDNS name and re-run start"
  fi
  validate_serve_r2
  mkdir -p "${HUB_DATA_DIR}"
  MDT_IS_HUB=1 hub_py init --data-dir "${HUB_DATA_DIR}"
  render_units
  save_options
  if [[ "${SERVICE_MANAGER}" == "systemd" ]]; then
    systemctl --user daemon-reload
    systemctl --user enable --now opendj-hub.service opendj-hub-backup.timer
    if [[ "$(loginctl show-user "${USER}" -p Linger --value)" != "yes" ]]; then
      echo "[WARN] linger is off: the hub stops at logout. Run: loginctl enable-linger ${USER}"
    fi
  elif [[ "${SERVICE_MANAGER}" == "launchd" ]]; then
    local label
    for label in com.opendj.hub com.opendj.hub-backup; do
      if launchctl print "gui/$(id -u)/${label}" > /dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/${label}"
      fi
      launchctl bootstrap "gui/$(id -u)" "$(unit_dir)/${label}.plist"
    done
  fi
  wait_for_hub
  cmd_status
  echo "[INFO] loopback only. Expose to the tailnet (after the ACL step) with:"
  echo "       tailscale serve --bg --https=${HUB_PORT} ${HUB_URL}"
}

cmd_stop() {
  print_config
  if [[ "${SERVICE_MANAGER}" == "systemd" ]]; then
    systemctl --user disable --now opendj-hub.service opendj-hub-backup.timer
  elif [[ "${SERVICE_MANAGER}" == "launchd" ]]; then
    local label
    for label in com.opendj.hub com.opendj.hub-backup; do
      if launchctl print "gui/$(id -u)/${label}" > /dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/${label}"
      else
        echo "[INFO] ${label} was not loaded"
      fi
    done
  fi
  # Presence of the stopped state: the probe must now get no answer at all.
  local probe_rc=0
  hub_py status --identity-only --data-dir "${HUB_DATA_DIR}" --url "${HUB_URL}" > /dev/null || probe_rc=$?
  if [[ "${probe_rc}" == "0" ]]; then
    echo "[ERROR] ${HUB_URL} still answers as a hub after stop" >&2
    exit 1
  fi
  echo "[OK] hub stopped (${HUB_URL} no longer answers as a hub, probe rc=${probe_rc})"
}

cmd_status() {
  print_config
  if [[ "${SERVICE_MANAGER}" == "systemd" ]]; then
    echo "[INFO] systemd: $(systemctl --user is-active opendj-hub.service || true)"
  elif [[ "${SERVICE_MANAGER}" == "launchd" ]]; then
    local state
    state="$(launchctl print "gui/$(id -u)/com.opendj.hub" 2> /dev/null | awk '/^\tstate =/ {print $3}' || true)"
    echo "[INFO] launchd: ${state:-not loaded}"
  fi
  local status_args=(status --data-dir "${HUB_DATA_DIR}" --url "${HUB_URL}")
  if [[ -n "${HUB_ALLOWED_HOSTS}" ]]; then
    status_args+=(--allowed-hosts "${HUB_ALLOWED_HOSTS}")
  fi
  hub_py "${status_args[@]}"
}

cmd_backup() {
  print_config
  local backup_cmd=("${UV_BIN}" run --no-sync python -m apps.sync_hub.hub_backup backup
    --data-dir "${HUB_DATA_DIR}" --dest "${HUB_BACKUP_DIR}" --keep "${HUB_BACKUP_KEEP}")
  if [[ "${HUB_BACKUP_R2}" == "1" ]]; then
    doppler run -p "${DOPPLER_PROJECT}" -c "${DOPPLER_CONFIG}" -- "${backup_cmd[@]}" --upload-r2
  elif [[ "${HUB_BACKUP_R2}" == "0" ]]; then
    "${backup_cmd[@]}"
  else
    echo "[ERROR] OPENDJ_HUB_BACKUP_R2=${HUB_BACKUP_R2} is not understood; use 1 or 0" >&2
    exit 1
  fi
}

cmd_restore() {
  local backup_file="${1:-}"
  local target_dir="${2:-${HUB_DATA_DIR}}"
  if [[ -z "${backup_file}" ]]; then
    echo "[ERROR] usage: cloudsync_hub.sh restore <backup-file> [target-data-dir]" >&2
    exit 1
  fi
  print_config
  echo "[INFO] restore ${backup_file} -> ${target_dir}"
  "${UV_BIN}" run --no-sync python -m apps.sync_hub.hub_backup restore \
    --backup "${backup_file}" --data-dir "${target_dir}"
}

USAGE="usage: cloudsync_hub.sh start [--serve-r2-off] [--backup-r2-off] [--allowed-hosts-clear] | stop | status | backup | restore <file> [data-dir]"
COMMAND="${1:-}"
shift || true
SERVE_R2_OFF=0
BACKUP_R2_OFF=0
ALLOWED_HOSTS_CLEAR=0
if [[ "${COMMAND}" == "start" ]]; then
  for flag in "$@"; do
    case "${flag}" in
      --serve-r2-off) SERVE_R2_OFF=1 ;;
      --backup-r2-off) BACKUP_R2_OFF=1 ;;
      --allowed-hosts-clear) ALLOWED_HOSTS_CLEAR=1 ;;
      *) echo "[ERROR] start does not understand '${flag}'; ${USAGE}" >&2; exit 1 ;;
    esac
  done
fi
case "${COMMAND}" in
  start | stop | status | backup | restore) resolve_options ;;
  *) echo "[ERROR] ${USAGE}" >&2; exit 1 ;;
esac
case "${COMMAND}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  backup) cmd_backup ;;
  restore) cmd_restore "$@" ;;
esac
