"""Shared harness for ops/dmg-smoke/run.sh Linux contract tests (DEVOPS-04)."""

from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "ops" / "dmg-smoke" / "run.sh"
INSTALLER = REPO_ROOT / "scripts" / "install_dmg_smoke_launchd.sh"

HEAD_SHA = "a" * 40
OLD_ROW7_SHA = "b" * 40
ROW1_SHA = "c" * 40


def _write(path: Path, content: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _gh_shim(fixture_dir: Path, comment_file: Path) -> str:
    return textwrap.dedent(
        rf"""#!/usr/bin/env bash
set -euo pipefail
comment_file="{comment_file}"
fixture_dir="{fixture_dir}"
_apply_jq() {{
  local payload="$1"
  if [[ -n "$jq_filter" ]]; then
    if [[ "$paginate" == "1" ]]; then
      # Real gh applies --jq per page (not to a slurped array of pages), so
      # the fixture's outer page-array is unwrapped one level here before
      # the caller's filter runs against each page's own array of records.
      printf '%s' "$payload" | jq -r ".[] as \$__page | (\$__page | ${{jq_filter}})"
    else
      printf '%s' "$payload" | jq -r "$jq_filter"
    fi
  else
    printf '%s' "$payload"
  fi
}}
if [[ "$1" == "api" ]]; then
  shift
  paginate=0 slurp=0 jq_filter="" template=""
  while (($#)); do
    case "$1" in
      --paginate) paginate=1; shift ;;
      --slurp) slurp=1; shift ;;
      --jq) jq_filter="$2"; shift 2 ;;
      --template) template="$2"; shift 2 ;;
      *) endpoint="$1"; shift ;;
    esac
  done
  if [[ "$slurp" == "1" ]] && {{ [[ -n "$jq_filter" ]] || [[ -n "$template" ]]; }}; then
    echo "gh: the \`--slurp\` option is not supported with \`--jq\` or \`--template\`" >&2
    exit 1
  fi
  if [[ "$endpoint" == *"/commits/main" ]]; then
    _apply_jq "$(cat "$fixture_dir/commits_main.json")"
    exit 0
  fi
  if [[ "$endpoint" == *"/compare/"* ]]; then
    if [[ "${{DMG_SMOKE_GH_COMPARE_UNRESOLVABLE:-0}}" == "1" ]]; then
      echo "gh: HTTP 404: No commit found for SHA" >&2
      exit 1
    fi
    if [[ "${{DMG_SMOKE_GH_COMPARE_FAIL:-0}}" == "1" ]]; then
      echo "gh: HTTP 503: service unavailable" >&2
      exit 1
    fi
    _apply_jq "$(cat "$fixture_dir/compare.json")"
    exit 0
  fi
  if [[ "$endpoint" == *"/issues/"*"/comments" ]]; then
    if [[ "${{DMG_SMOKE_GH_COMMENTS_FAIL:-0}}" == "1" ]]; then
      echo "gh: HTTP 401: Bad credentials" >&2
      exit 1
    fi
    _apply_jq "$(cat "$fixture_dir/comments.json")"
    exit 0
  fi
  echo "[shim-gh] unknown api endpoint: $endpoint" >&2
  exit 1
fi
if [[ "$1" == "issue" && "$2" == "comment" ]]; then
  shift 2
  body_file=""
  while (($#)); do
    case "$1" in
      --body-file) body_file="$2"; shift 2 ;;
      --repo) shift 2 ;;
      *) shift ;;
    esac
  done
  cp "$body_file" "$comment_file"
  exit 0
fi
echo "[shim-gh] unknown gh invocation: $*" >&2
exit 1
"""
    )


def _headless_shim(build_root: Path) -> str:
    bundle_dir = build_root / "apps/desktop/src-tauri/target/release/bundle/dmg"
    log_dir = build_root / "ops/logs"
    return textwrap.dedent(
        f"""#!/usr/bin/env bash
set -euo pipefail
build_root="{build_root}"
bundle_dir="{bundle_dir}"
log_dir="{log_dir}"
prepare_only=0
while (($#)); do
  case "$1" in
    --prepare-only) prepare_only=1; shift ;;
    --sha|--host-label) shift 2 ;;
    *) shift ;;
  esac
done
if [[ "$prepare_only" == "1" ]]; then
  exit 0
fi
if [[ -f "$build_root/DMG_SMOKE_BUILD_FAIL" ]]; then
  exit 1
fi
mkdir -p "$bundle_dir" "$log_dir"
dmg="$bundle_dir/Open DJ_test.dmg"
printf 'fake dmg bytes\\n' > "$dmg"
if command -v shasum >/dev/null 2>&1; then
  digest="$(shasum -a 256 "$dmg" | awk '{{print $1}}')"
else
  digest="$(sha256sum "$dmg" | awk '{{print $1}}')"
fi
printf 'origin/main 2026-01-01T00:00:00Z\\n%s\\n' "$digest" > "$dmg.complete"
if [[ -f "$build_root/DMG_SMOKE_TIMING_LOG" ]]; then
  cat "$build_root/DMG_SMOKE_TIMING_LOG" > "$log_dir/ship-dmg.log"
else
  echo '[TIMING] total=60s rc=0 verdict=OK' > "$log_dir/ship-dmg.log"
fi
exit 0
"""
    )


def _hdiutil_shim() -> str:
    return textwrap.dedent(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "attach" ]]; then
  mountpoint=""
  shift
  while (($#)); do
    case "$1" in
      -mountpoint) mountpoint="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  mkdir -p "$mountpoint"
  if [[ "${DMG_SMOKE_MOUNT_APP:-1}" != "0" ]]; then
    app="$mountpoint/Open DJ.app"
    mkdir -p "$app/Contents/MacOS"
    printf '#!/bin/sh\\n' > "$app/Contents/MacOS/opendj-desktop"
    chmod +x "$app/Contents/MacOS/opendj-desktop"
    cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.opendj.desktop</string></dict></plist>
PLIST
  fi
  exit 0
fi
if [[ "$1" == "detach" ]]; then
  exit 0
fi
exit 0
"""
    )


def _curl_shim() -> str:
    return textwrap.dedent(
        """#!/usr/bin/env bash
set -euo pipefail
url="${@: -1}"
if [[ "$url" == *"/api/v1/preflight" ]]; then
  attempt=1
  if [[ -n "${DMG_SMOKE_PREFLIGHT_ATTEMPTS_FILE:-}" ]]; then
    prev="$(cat "$DMG_SMOKE_PREFLIGHT_ATTEMPTS_FILE" 2>/dev/null || echo 0)"
    attempt=$((prev + 1))
    printf '%s' "$attempt" > "$DMG_SMOKE_PREFLIGHT_ATTEMPTS_FILE"
  fi
  # Simulates the packaged app's engine port accepting the TCP connection but
  # not answering yet (the TCC/Gatekeeper negotiation window): curl returns
  # an empty body, exactly like the real `-m` timeout does after `|| true`
  # discards its non-zero exit.
  if [[ "${DMG_SMOKE_PREFLIGHT_NEVER_ANSWER:-0}" == "1" ]]; then
    exit 0
  fi
  ready_after="${DMG_SMOKE_PREFLIGHT_READY_AFTER_ATTEMPTS:-0}"
  if [[ "$ready_after" -gt 0 && "$attempt" -lt "$ready_after" ]]; then
    exit 0
  fi
  printf '%s' "${DMG_SMOKE_PREFLIGHT_JSON:?}"
  exit 0
fi
if [[ "$url" == *"/api/v1/health" ]]; then
  printf '%s' "${DMG_SMOKE_HEALTH_JSON:?}"
  exit 0
fi
if [[ "$url" == "http://127.0.0.1:"*"/" || "$url" == *"://127.0.0.1:"* ]]; then
  echo ok
  exit 0
fi
echo "[shim-curl] unhandled url: $url" >&2
exit 1
"""
    )


def _port_shims() -> dict[str, str]:
    return {
        "pmset": textwrap.dedent(
            """#!/usr/bin/env bash
if [[ "$1" == "-g" && "$2" == "batt" ]]; then
  echo "${DMG_SMOKE_PMSET_OUTPUT:-Now drawing from 'AC Power'}"
  exit 0
fi
exit 1
"""
        ),
        "pgrep": textwrap.dedent(
            """#!/usr/bin/env bash
pattern="${2:-}"
if [[ "$1" == "-x" && "$2" == "opendj-desktop" ]]; then
  if [[ -n "${DMG_SMOKE_LIVE_APP_PID:-}" ]]; then
    echo "$DMG_SMOKE_LIVE_APP_PID"
    exit 0
  fi
  exit 1
fi
if [[ "$1" == "-f" ]]; then
  if [[ "$pattern" == *"runtime/bin/python"* ]] && [[ -n "${DMG_SMOKE_ENGINE_PID:-}" ]]; then
    echo "$DMG_SMOKE_ENGINE_PID"
    exit 0
  fi
  if [[ "$pattern" == *"Open DJ.app"* ]] && [[ -f "${DMG_SMOKE_SCRATCH_STATE:-}" ]]; then
    echo "${DMG_SMOKE_SCRATCH_PID:-4242}"
    exit 0
  fi
  exit 1
fi
exit 1
"""
        ),
        "lsof": textwrap.dedent(
            """#!/usr/bin/env bash
if [[ -n "${DMG_SMOKE_ENGINE_PORT:-}" ]]; then
  echo "n*:${DMG_SMOKE_ENGINE_PORT}"
  exit 0
fi
exit 1
"""
        ),
        "open": textwrap.dedent(
            """#!/usr/bin/env bash
if [[ "${DMG_SMOKE_OPEN_FAIL:-0}" == "1" ]]; then
  exit 1
fi
printf '%s\\n' "$@" > "${DMG_SMOKE_OPEN_LOG:?}"
touch "${DMG_SMOKE_SCRATCH_STATE:?}"
touch "${DMG_SMOKE_OPEN_CALLED:?}"
exit 0
"""
        ),
        "osascript": textwrap.dedent(
            """#!/usr/bin/env bash
rm -f "${DMG_SMOKE_SCRATCH_STATE:-}"
exit 0
"""
        ),
        "ditto": textwrap.dedent(
            """#!/usr/bin/env bash
set -euo pipefail
cp -R "$1" "$2"
"""
        ),
        "lockf": textwrap.dedent(
            """#!/usr/bin/env bash
if [[ "$1" == "-t" && "$2" == "0" ]]; then
  if [[ "${DMG_SMOKE_LOCK_HELD:-0}" == "1" ]]; then
    exit 75
  fi
  exit 0
fi
exit 0
"""
        ),
        "doppler": textwrap.dedent(
            """#!/usr/bin/env bash
echo "SECRETKEY"
"""
        ),
        "just": textwrap.dedent(
            """#!/usr/bin/env bash
if [[ "$1" == "dmg-preflight" ]]; then
  if [[ "${DMG_SMOKE_PREFLIGHT_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  exit 0
fi
exit 1
"""
        ),
        "ship_dmg.sh": textwrap.dedent(
            """#!/usr/bin/env bash
echo "ship_dmg.sh must not be called" >&2
exit 99
"""
        ),
    }


def write_gh_fixtures(
    fixture_dir: Path,
    *,
    merge_count: int = 60,
    comments: list[dict] | None = None,
) -> None:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir.joinpath("commits_main.json").write_text(
        json.dumps({"sha": HEAD_SHA}), encoding="utf-8"
    )
    fixture_dir.joinpath("compare.json").write_text(
        json.dumps({"total_commits": merge_count}), encoding="utf-8"
    )
    pages = [comments or []]
    fixture_dir.joinpath("comments.json").write_text(
        json.dumps(pages), encoding="utf-8"
    )


def setup_layout(home: Path) -> dict[str, Path]:
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    fixture_dir = home / "gh-fixtures"
    comment_file = home / "issue_comment.md"
    build_root = home / "build-worktree"
    scratch = home / "scratch"
    headless = home / "headless_dmg_build.sh"

    _write(bin_dir / "gh", _gh_shim(fixture_dir, comment_file), executable=True)
    _write(headless, _headless_shim(build_root), executable=True)
    _write(bin_dir / "hdiutil", _hdiutil_shim(), executable=True)
    _write(bin_dir / "curl", _curl_shim(), executable=True)
    for name, body in _port_shims().items():
        _write(bin_dir / name, body, executable=True)

    open_called = home / "open_called"
    open_called.write_text("pending", encoding="utf-8")
    open_log = home / "open_log.txt"
    scratch_state = home / "scratch_running"
    build_root.mkdir(parents=True, exist_ok=True)
    preflight_attempts_file = home / "preflight_attempts"

    return {
        "bin_dir": bin_dir,
        "fixture_dir": fixture_dir,
        "comment_file": comment_file,
        "build_root": build_root,
        "scratch": scratch,
        "headless": headless,
        "open_called": open_called,
        "open_log": open_log,
        "scratch_state": scratch_state,
        "preflight_attempts_file": preflight_attempts_file,
    }


def run_smoke(
    home: Path,
    paths: dict[str, Path],
    *,
    extra_env: dict[str, str] | None = None,
    force: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{paths['bin_dir']}:/usr/bin:/bin",
        "HOME": str(home),
        "MDT_DMG_BUILD_WORKTREE": str(paths["build_root"]),
        "MDT_HEADLESS_DMG_BUILD": str(paths["headless"]),
        "MDT_DMG_SMOKE_SCRATCH": str(paths["scratch"]),
        # Required by run.sh (no hidden "air" default); tests that need a
        # different label, or none, override it via extra_env.
        "MDT_DMG_SMOKE_HOST_LABEL": "test-host",
        "DMG_SMOKE_OPEN_CALLED": str(paths["open_called"]),
        "DMG_SMOKE_OPEN_LOG": str(paths["open_log"]),
        "DMG_SMOKE_SCRATCH_STATE": str(paths["scratch_state"]),
    }
    if extra_env:
        env.update(extra_env)
    cmd = ["bash", str(RUN_SH)]
    if force:
        env["FORCE"] = "1"
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=REPO_ROOT,
    )


def ok_preflight_json() -> str:
    return json.dumps(
        {
            "status": "pass",
            "checks": [{"id": "library-attached", "status": "pass"}],
        }
    )


def ok_health_json(tracks: int = 5, playlists: int = 3) -> str:
    return json.dumps({"status": "ok", "state_db": {"tracks": tracks, "playlists": playlists}})


def zero_health_json() -> str:
    return ok_health_json(tracks=0, playlists=0)
