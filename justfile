# just command runner: https://github.com/casey/just

set dotenv-load

# Reproduce issue #1461's local-stems research measurement on this exact
# machine. Candidate scripts select MPS, CUDA, or CPU themselves and record the
# selected device in the ledger. Run ``just local-stems-bench 2 120`` for two
# clips and a two-minute per-cell timeout. The MUSDB18 seven-second fixture
# means this is a clip-length benchmark only.
local-stems-bench N='2' TIMEOUT='600':
    uv run --with 'fast-bss-eval>=0.1.4' --with 'numpy<2' --with 'soundfile>=0.12' python -m scripts.bench.local.run_local_stems_benchmark --n "{{ N }}" --timeout-s "{{ TIMEOUT }}"

# The pytest selection the lane gate runs, named ONCE. It used to be spelled
# out twice inside the gate recipe (collect-only, then run), which is its own
# small trap: widen one and forget the other and the floor stops describing
# what actually ran. tests/quality/test_gate_scope.py parses this exact
# assignment, so it is also the machine-readable answer to "what does the gate
# see?".
#
# What is NOT here is the point. Six separate trains have now landed tests this
# gate did not run, so the guard test turns the seventh into a red gate rather
# than a discovery. Deliberately still out: tests/analysis, tests/stems and
# tests/vocals import soundfile/modal, which are kept out of the repo venv on
# purpose, so they ERROR rather than skip. Everything else on the guard's
# known-unscoped list is unexamined, not excused.
pytest_scope := "tests/webui tests/test_progress.py tests/engine_core tests/entitlements tests/database tests/test_conformance.py tests/open_dj tests/shared tests/scripts tests/quality tests/perf tests/loudness tests/analysis_loudness tests/cloudsync tests/dedup tests/reconcile tests/smartlists tests/sync tests/streaming_transfer tests/test_rekordbox_writeback_gate.py tests/test_rekordbox_writeback_recovery.py tests/test_rekordbox_writeback_surfaces.py tests/test_apply_analysis.py tests/test_apply_cues.py tests/test_apply_ratings.py tests/test_rb_writer.py tests/test_relocate_route.py tests/test_sync_safety.py tests/test_requirement_markers.py tests/test_requirement_intent_ratchet.py tests/test_paths_data_dir_override.py tests/test_skill_runners.py tests/test_dependency_contract.py tests/test_ci_cost_guard.py tests/test_ci_cost_ledger.py tests/test_ci_cost_ledger_cache.py tests/test_ci_cost_billing_ledger.py tests/test_feedback_harvest_discovery.py tests/test_schema_time_travel.py tests/test_security_status_doc.py tests/test_analysis_backlog.py tests/test_analysis_run_classification.py tests/test_analysis_backlog_token.py tests/test_ci_host_serialization.py tests/test_root_playwright_gate.py tests/lyrics tests/lyrics_alignment tests/lyrics_search tests/mik tests/test_equivalence_audio_probe.py tests/test_equivalence_cli.py tests/test_equivalence_contract.py tests/test_equivalence_detection.py tests/test_equivalence_known.py tests/test_equivalence_normalisers.py tests/test_equivalence_single_source.py tests/test_equivalence_sources.py tests/test_equivalence_suite.py tests/test_planning_merge_drivers.py tests/test_pairing_capture_route.py tests/test_uv_run_no_sync.py tests/analysis_beatgrid tests/analysis_waveform tests/analysis_contract tests/analysis_bench tests/analysis_key tests/analysis tests/test_analysis_source.py tests/opendj_cli tests/test_move_copy_between_playlists.py tests/test_playlist_write_persistence.py tests/test_playlist_add.py tests/test_playlist_add_member_reads.py tests/test_smartlists_delete.py tests/test_playlist_remove.py tests/agentic_testing tests/parity tests/controller_probe tests/playlist_sets tests/test_playlist_forbid_duplicates.py tests/test_playlist_memberships_scale.py tests/test_playlist_move.py tests/test_smartlists_duplicate.py tests/test_smartlists_include_counts.py tests/test_smartlists_rename.py tests/test_smartlists_soft_delete.py tests/ops tests/sync_hub tests/standalone tests/analysis_structure tests/genre_infer tests/health_lights"
# The engine OpenAPI dump, shared by openapi-dump and openapi-determinism.
# Inline rather than a script module so the schema dump adds no new Python
# source. Statement order is load bearing: apply_env_contract must set
# MDT_DATA_DIR BEFORE apps.engine_core.app is imported, because that import
# pulls in apps.webui, which resolves its paths at import time.
engine_openapi_dump := "import json, sys, tempfile; from pathlib import Path; from apps.engine_core.config import apply_env_contract, build_config, prepare_layout; cfg = build_config(tempfile.mkdtemp(prefix='opendj-openapi-'), '127.0.0.1', 8683); apply_env_contract(cfg); prepare_layout(cfg); from apps.engine_core.app import create_app; out = Path(sys.argv[1]); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(create_app(cfg).openapi(), indent=2, sort_keys=True) + '\\n', encoding='utf-8'); print('wrote OpenAPI schema to ' + str(out))"

# Boot the rebuilt engine on this worktree's claimed backend port over the
# lane data dir. Both vars come from .env; serve itself fails loud when the
# data dir is missing or relative.
engine-serve:
    uv run --no-sync python -m apps.engine_core serve --data-dir "$MDT_DATA_DIR" --host 127.0.0.1 --port "$MUSIC_DJ_BACKEND_PORT"

# Savepoint smoke against the rebuilt engine via the ENGINE_CMD seam,
# over this lane's own data dir (never the primary checkout's).
savepoint-smoke-engine: webui-ports-check venv-ready
    cd apps/webui/frontend && SAVEPOINT_SMOKE_DATA_DIR="$MDT_DATA_DIR" ENGINE_CMD='uv run --no-sync python -m apps.engine_core serve --data-dir {data_dir} --host {host} --port {port}' pnpm exec playwright test --config tests/e2e/playwright.savepoint.config.ts

vocal_cache_dir := env_var_or_default("VOCAL_CACHE_DIR", "data/state/vocal-cache")

# Print the one entitlement-selected, data-only CloudSync policy. Runtime
# callers import apps.cloud.policy.CFG; they do not choose a mode themselves.
cloudsync-policy:
    uv run --no-sync python -m apps.cloud.policy

# CloudSync FAST tier: everything except the two slow tiers. The collect floor
# fails a run where a test module silently stopped collecting; it only ratchets
# up (raise it when you add tests, never lower it to get green).
cloudsync_fast_min_selected := "375"

cloudsync-fast *args:
    uv run --no-sync python -m pytest tests/cloudsync tests/webui/test_cloudsync_routes.py -q \
        -p scripts.pytest_tier_floor -m "not live_r2 and not real_library" \
        --tier-min-selected={{ cloudsync_fast_min_selected }} {{ args }}

# CloudSync SLOW tiers: live R2 plus the real library. Exits nonzero unless
# BOTH tiers executed at least one test, so a skipped-everything run is never
# green. Real library: the packaged app's state.db (read-only snapshot) or
# MDT_REAL_LIBRARY_STATE_DB. Supply live R2 credentials through the configured
# operator credential environment before invoking this command.
cloudsync-slow *args:
    uv run --no-sync python -m pytest tests/cloudsync -q -rs \
        -p scripts.pytest_tier_floor -m "live_r2 or real_library" \
        --tier-require-executed=live_r2 --tier-require-executed=real_library {{ args }}

# CloudSync hub lifecycle (docs/cloudsync/hub-runbook.md). A headless
# engine_core with MDT_IS_HUB=1 on 127.0.0.1:8870 and a dedicated data dir,
# under systemd --user (Linux) or launchd (macOS); units are rendered from
# apps/sync_hub/hub_deploy.py. Never the packaged app. Tailnet exposure is a
# separate `tailscale serve` step. Overrides: OPENDJ_HUB_DATA_DIR,
# OPENDJ_HUB_PORT, OPENDJ_HUB_BACKUP_DIR, OPENDJ_HUB_BACKUP_KEEP,
# OPENDJ_HUB_BACKUP_R2=1 (upload using configured operator credentials),
# OPENDJ_HUB_SERVE_R2=1 and OPENDJ_HUB_SERVE_DOPPLER_CONFIG (stem presign creds),
# OPENDJ_HUB_ALLOWED_HOSTS (MagicDNS hostname for tailnet spokes).
cloudsync-hub-start:
    ./scripts/cloudsync_hub.sh start

cloudsync-hub-stop:
    ./scripts/cloudsync_hub.sh stop

# Exit 0 OK / 2 CRITICAL / 3 UNKNOWN: is_hub true, id matches the data dir,
# schema matches this checkout.
cloudsync-hub-status:
    ./scripts/cloudsync_hub.sh status

cloudsync-hub-backup:
    ./scripts/cloudsync_hub.sh backup

# Per-machine authoritative state.db backup (issue #2498). Override dest for external volumes.
state-authoritative-backup data_dir="data" dest="" keep="14":
    uv run --no-sync python -m apps.shared.state_authoritative_backup backup \
      --data-dir {{data_dir}} \
      {{ if dest != "" { "--dest " + quote(dest) } else { "" } }} \
      --keep {{keep}}

# Selective restore of ADR-0022 authoritative tables. Stop the engine first.
state-authoritative-restore backup table data_dir="data":
    uv run --no-sync python -m apps.shared.state_authoritative_backup restore \
      --backup {{quote(backup)}} \
      --data-dir {{data_dir}} \
      --table {{table}}

# Restore into an EMPTY data dir (default: the hub's). Refuses a live DB.
cloudsync-hub-restore backup data_dir="":
    ./scripts/cloudsync_hub.sh restore {{quote(backup)}} {{ if data_dir == "" { "" } else { quote(data_dir) } }}

# Validate and print KPIs from the current real vocal cache. Machine-local
# acceptance gate with Make parity (make vocal-kpi-live-check); portable tests
# use the committed sanitized fixture in tests/fixtures/vocal-kpi/ instead.
vocal-kpi-live-check:
    uv run scripts/bench/kpi_derive.py --cache-dir "{{ vocal_cache_dir }}" --window latest --json

# Fail when the interpreter running here violates pyproject's requires-python.
# `uv run <cmd>` silently falls back to a PATH command when that command is
# missing from the project environment, and the PATH command brings its own
# interpreter (homebrew `pytest` carries Python 3.10). The same check runs at
# conftest load for every pytest invocation; this is the standalone probe.
interpreter-check:
    uv run --no-sync python -m scripts.interpreter_contract

# The CI fast tier, locally: every test the committed ledger (.test_durations)
# recorded under 0.5 s, plus tests it has never seen, over the same scope the
# ci.yml `fast` job runs (SMARTEST-CI round 6, specs/ci-fail-fast.md). Refuses
# a ledger that names under 95% of the collection, exactly as CI does. Pass
# `just fast-tier slow` for the complement. ~745 s of serial test time on
# main 5cca5d21f (Wed 16 Sep 2026), so `-n auto` if pytest-xdist is installed.
# Every parameter after a defaulted one carries a default too: the runners'
# just 1.21 rejects the whole justfile otherwise ("Non-default parameter
# follows default parameter"), which broke every `just` call in CI on Wed 16
# Sep 2026 while just 1.58 on the Macs accepted `TIER="fast" *ARGS`. This
# shape parses on 1.21 (probed on agentbox). Pinned by
# tests/scripts/test_justfile_parameter_order.py.
fast-tier TIER="fast" *ARGS="":
    env -u MDT_DATA_DIR uv run --no-sync python -m pytest -q \
        --fast-tier {{TIER}} --fast-tier-max-seconds 0.5 --ledger-coverage-min 0.95 --tier-min-selected 2000 \
        --ignore=tests/analysis --ignore=tests/vocals --ignore=tests/sync/usb --ignore=tests/sets \
        --ignore=tests/quality/test_frontend_typing_gate.py --ignore=tests/quality/test_frontend_typing_directives.py \
        --ignore=tests/quality/test_mypy_cache.py {{ARGS}}

# Re-verify a cloud agent environment with no installs: pnpm pin, then
# tests/test_cloud_agent_env.py (deps, frontend, and a real Doppler round
# trip when DOPPLER_TOKEN is set). See docs/cloud-agent-environments.md.
cloud-env-doctor:
    ./scripts/cloud_agent_setup.sh --check

# Claim or restore this worktree's pair in the shared Git-common-dir registry.
webui-ports-claim:
    uv run --no-sync python -m apps.webui.port_config claim

# Regenerate `.claude/launch.json` from `.claude/dev-servers.json`.
# Default profile writes the committed 8585/5173 pair for CI and pre-push.
# Worktree profile reads this checkout's claimed `.env` pair for preview_start.
launch-json PROFILE="default":
    uv run --no-sync python -m scripts.dev_server_registry --write --profile {{ PROFILE }}

# Fail when the committed launch file drifted from the registry default profile.
launch-json-check:
    uv run --no-sync python -m scripts.dev_server_registry --check --profile default

# Populate this worktree's .venv so `uv run --no-sync` can actually import.
#
# Every backend recipe runs `uv run --no-sync` for speed, which does NOT install
# anything -- so a FRESH worktree has an empty .venv and the daemon dies on
# `ModuleNotFoundError: No module named 'fastapi'`. Playwright reports that as
# "Process from config.webServer was not able to start", which reads like a
# port or config fault and sent at least one agent hunting the wrong bug.
# A warm `uv sync` is ~0.08s, so this is close to free on every run after the
# first, and it makes a fresh worktree work with no manual setup step.
#
# `--extra dev` is load-bearing, not tidiness. A plain `uv sync` PRUNES the
# env to the default dependency set, and pytest lives in the `dev` extra
# (pyproject.toml), so running any recipe that depends on this one DELETES
# the test runner:
#     just savepoint-smoke && uv run pytest
#     error: Failed to spawn: `pytest`
# pyproject.toml already carries a comment about "the first `uv sync` that
# pruned the env deleted the test runner" (Fri 24 Jul 2026). Declaring pytest
# fixed half of it -- declaring it in an EXTRA that this recipe did not
# install left the prune in place. `observability` is kept for the same reason:
# a prune drops sentry-sdk, and the Sentry e2e tests import it.
venv-ready:
    uv sync --extra dev --extra observability --quiet

# Show the worktree's isolated web UI endpoints.
# After claiming a new pair, refresh preview configs with `just launch-json PROFILE=worktree`.
webui-ports: webui-ports-claim
    uv run --no-sync python -m apps.webui.port_config show

# Release this worktree's reservation before removing the worktree.
webui-ports-release:
    uv run --no-sync python -m apps.webui.port_config release

# ---------------------------------------------------------------------------
# Worktree lifecycle (OPS-21, OPS-22). Policy + derivation:
# .planning/FANOUT-CONVENTIONS.md -> "Worktree lifecycle". The tools live in
# fleet-af `agents_worktree_tools/` (Fri 2 Oct 2026); scripts/worktree_tools.py
# runs that copy against this checkout and exits 2 when it is absent.
# ---------------------------------------------------------------------------

# Refuse to create another worktree past the cap or under the disk floor.
# Cheap by construction (a path count and a statvfs), so it is safe to put in
# front of every `git worktree add`. ENOSPC stopped threads twice on the
# weekend of Sat 5 Sep 2026; this is the thing that turns that into one loud
# refusal instead.
#
# Refuse a new worktree past the cap or under the disk floor.
wt-guard:
    uv run --no-sync python -m scripts.worktree_tools lifecycle guard

# Sparse linked worktrees (OPS-45): a NEW worktree leaves blog and docs/landscape
# images out of the checkout. Policy: .planning/FANOUT-CONVENTIONS.md -> "Worktrees".
#
# Install the post-checkout hook once per clone (by copy; never clobbers another hook).
wt-sparse-hook-install:
    uv run --no-sync python -m scripts.sparse_worktree install-hook

# SPARSE or FULL for this worktree, and how many tracked paths it leaves out.
wt-sparse-status:
    uv run --no-sync python -m scripts.sparse_worktree status

# Restore a full checkout in this worktree (same as `git sparse-checkout disable`).
wt-full:
    uv run --no-sync python -m scripts.sparse_worktree full

# Refuse worker branches from a dirty or unpublished primary checkout. The
# caller supplies the exact base and worker path so no branch source is hidden.
worker-preflight REPO BASE:
    uv run --no-sync python -m scripts.worker_worktree_guard preflight --repo {{ REPO }} --base {{ BASE }}

# Create a worker worktree from the explicit base after the source preflight.
worker-worktree REPO TARGET BRANCH BASE:
    uv run --no-sync python -m scripts.worktree_tools worker_guard create --repo {{ REPO }} --target {{ TARGET }} --branch {{ BRANCH }} --base {{ BASE }}

# Report and enforce a worker branch's declared issue scope before review.
worker-scope REPO BASE MAX_COMMITS MAX_FILES:
    uv run --no-sync python -m scripts.worker_worktree_guard scope --repo {{ REPO }} --base {{ BASE }} --max-commits {{ MAX_COMMITS }} --max-files {{ MAX_FILES }}

# Scan every worktree and write the registry into the shared git common dir.
# There is no worktree-creating helper in this repo to hook, so registration is
# a scan: owner is inferred from the branch prefix, PR state from one gh call.
#
# Scan every worktree into the shared registry.
wt-register:
    uv run --no-sync python -m scripts.worktree_tools lifecycle register

# Table: path, branch, PR, size, last touch, dirty, verdict. `du` over ~140
# trees is the slow half; add --no-size when you only want the verdicts.
#
# Table of every worktree with its keep-or-reap verdict.
wt-status *ARGS:
    uv run --no-sync python -m scripts.worktree_tools lifecycle status {{ ARGS }}

# What would be backed up and how much disk comes back. Changes nothing.
#
# Dry run: what the reaper WOULD remove.
wt-reap *ARGS:
    uv run --no-sync python -m scripts.worktree_tools lifecycle reap --dry-run {{ ARGS }}

# Back up every uncommitted path, verify the archive holds them, THEN remove.
# Backups are stdlib tar+gzip (<branch>-<utc>.tar.gz, no host binary needed),
# land in ~/.cache/mdt-worktree-backups; remote backup availability depends
# on the configured operator endpoint. Run just wt-reap first.
#
# Back up uncommitted work, verify it, then remove the stale worktrees.
wt-reap-apply *ARGS:
    uv run --no-sync python -m scripts.worktree_tools lifecycle reap --apply {{ ARGS }}

# Recreate a reaped worktree and untar its saved files, verifying every
# restored file against the manifest's sha256 before reporting success.
# Reads .tar.gz with the stdlib; a legacy .tar.zst archive still needs the
# `zstd` binary on PATH and says so by name if it is missing.
#
# Recreate a reaped worktree and restore its saved files.
wt-restore BRANCH *ARGS:
    uv run --no-sync python -m scripts.worktree_tools lifecycle restore {{ BRANCH }} {{ ARGS }}

# Fail if either reserved worktree port is already bound.
#
# `env -u` is load bearing on all three of these. `set dotenv-load` reads .env
# into every recipe's environment when JUST STARTS, which is before
# webui-ports-claim has written this worktree's pair; resolve_ports then ranks
# process env above .env and validates the pair just replaced. In a fresh
# worktree, whose .env arrives copied from the primary checkout via
# .worktreeinclude, that is the primary's 8585/5173 -- so the check failed with
# "not reserved by this worktree; currently owned by ..." on the first run and
# passed on the second, once .env and the environment agreed again. Unsetting
# the two vars makes the check read the file the claim just wrote. The
# documented CLI > env > .env precedence in port_config is untouched.
webui-ports-check: webui-ports-claim
    env -u MUSIC_DJ_BACKEND_PORT -u MUSIC_DJ_FRONTEND_PORT uv run --no-sync python -m apps.webui.port_config check --service all

# Check only the backend port, so a running frontend does not block startup.
webui-backend-port-check: webui-ports-claim
    env -u MUSIC_DJ_BACKEND_PORT -u MUSIC_DJ_FRONTEND_PORT uv run --no-sync python -m apps.webui.port_config check --service backend

# Check only the frontend port, so a running backend does not block startup.
webui-frontend-port-check: webui-ports-claim
    env -u MUSIC_DJ_BACKEND_PORT -u MUSIC_DJ_FRONTEND_PORT uv run --no-sync python -m apps.webui.port_config check --service frontend

# Track (g) local HTTP API security probes (issue #1861 / plan #1778).
# One command. Prints PASS/FAIL/UNKNOWN per probe with its control.
# Does not patch the daemon. File FAIL findings with:
#   uv run --no-sync python -m scripts.redteam_local_api --file
# which invokes scripts.redteam_filing --label red-team.
redteam-local-api: webui-ports-claim venv-ready
    uv run --no-sync python -m scripts.redteam_local_api

# Start the reloadable FastAPI daemon; appends today's host log.
webui-backend: webui-backend-port-check venv-ready
    #!/usr/bin/env bash
    set -euo pipefail
    log=$(uv run --no-sync python -m apps.webui.run_agentbox --prepare-log webui-backend)
    uv run --no-sync python -m apps.webui.run_agentbox --prepare-log webui-client-errors >/dev/null
    uv run --no-sync python -m apps.webui.run_agentbox --prepare-log webui-visitors >/dev/null
    "$PWD/.venv/bin/python" -m apps.webui.server --host 127.0.0.1 --reload 2>&1 | tee -a "$log"

# Start Vite on this worktree's frontend port and proxy API calls locally.
webui-frontend: webui-frontend-port-check
    #!/usr/bin/env bash
    set -euo pipefail
    log=$(uv run --no-sync python -m apps.webui.run_agentbox --prepare-log webui-frontend)
    cd apps/webui/frontend && pnpm exec vite dev --host 127.0.0.1 2>&1 | tee -a "$log"

# Run the WebKit-truthful UI loop. The watcher is a Vite production build, not
# a Vite server, so every save gets exactly the transforms that ship. The
# engine starts only after this watcher has produced its first bundle, and the
# explicit directory override makes the engine's static mount unambiguous.
webui-webkit-watch: webui-backend-port-check
    #!/usr/bin/env bash
    set -euo pipefail
    frontend_build_dir="$(pwd)/apps/webui/frontend/build"
    watch_log=$(mktemp)
    watcher_pid=""
    cleanup() {
        if [[ -n "$watcher_pid" ]] && kill -0 "$watcher_pid" 2>/dev/null; then
            kill "$watcher_pid"
        fi
        rm -f "$watch_log"
    }
    trap cleanup EXIT
    (
        cd apps/webui/frontend
        exec pnpm build --watch
    ) >"$watch_log" 2>&1 &
    watcher_pid=$!
    for _ in $(seq 1 60); do
        if grep -q "built in" "$watch_log"; then
            MDT_FRONTEND_BUILD_DIR="$frontend_build_dir" just webui-backend
            exit $?
        elif ! kill -0 "$watcher_pid" 2>/dev/null; then
            cat "$watch_log" >&2
            echo "[ERROR] production build watcher exited before its initial bundle" >&2
            exit 1
        fi
        sleep 0.2
    done
    cat "$watch_log" >&2
    echo "[ERROR] production build watcher did not finish its initial bundle within 12 seconds" >&2
    exit 1

# Build and launch the debug Tauri shell against this worktree's running engine.
# The WebDriver port is an explicit recipe argument so agents can give each
# concurrently running shell its own seam. The default matches the MCP runbook.
dev-attach webdriver_port="4456": webui-ports-claim
    #!/usr/bin/env bash
    set -euo pipefail
    webdriver_port="{{webdriver_port}}"
    command -v curl >/dev/null || { echo "[ERROR] dev-attach requires curl" >&2; exit 1; }
    command -v cargo >/dev/null || { echo "[ERROR] dev-attach requires Cargo on PATH" >&2; exit 1; }
    ports_json=$(env -u MUSIC_DJ_BACKEND_PORT -u MUSIC_DJ_FRONTEND_PORT uv run --no-sync python -m apps.webui.port_config show --json)
    engine_origin=$(printf '%s' "$ports_json" | uv run --no-sync python -c 'import json, sys; print(json.load(sys.stdin)["api_proxy_target"])')
    curl --connect-timeout 2 --max-time 5 --fail --silent --show-error "$engine_origin/api/v1/health" >/dev/null || {
        echo "[ERROR] no healthy worktree engine at $engine_origin; start it with: just webui-backend" >&2
        exit 1
    }
    spa_content_type=$(curl --connect-timeout 2 --max-time 5 --fail --silent --show-error --output /dev/null --write-out '%{content_type}' "$engine_origin/")
    if [[ "$spa_content_type" != text/html* ]]; then
        echo "[ERROR] $engine_origin is not serving the SPA; stop the engine, run pnpm build in apps/webui/frontend, then restart just webui-backend" >&2
        exit 1
    fi
    uv run --no-sync python -c 'import socket, sys; sock = socket.socket(); sock.bind(("127.0.0.1", int(sys.argv[1]))); sock.close()' "$webdriver_port"
    mkdir -p apps/desktop/src-tauri/payload
    desktop_binary=$(cargo build --manifest-path apps/desktop/src-tauri/Cargo.toml --message-format=json-render-diagnostics | uv run --no-sync python -c 'import json, sys; artifacts = [message["executable"] for line in sys.stdin if (message := json.loads(line)).get("reason") == "compiler-artifact" and message.get("executable") and message["target"]["name"] == "opendj-desktop" and "bin" in message["target"]["kind"]]; len(artifacts) == 1 or sys.exit(f"expected one opendj-desktop binary, found {len(artifacts)}"); print(artifacts[0])')
    echo "[OK] debug shell -> $engine_origin; webview MCP -> http://127.0.0.1:$webdriver_port"
    OPENDJ_ENGINE_ORIGIN="$engine_origin" TAURI_WEBDRIVER_PORT="$webdriver_port" exec "$desktop_binary"

# Savepoint gate: 6 real-library UI checks on one server boot (route, deck load,
# transport, waveform pixels, mixer IPC, /admin). Playwright owns the backend +
# Vite lifecycle on this worktree's claimed ports and reads the primary
# checkout's data/ via MDT_DATA_DIR, so it never touches 8585/5173.
# Measured Mon 17 Aug 2026 on this Mac across two runs: 20.6s and 32.6s wall for
# the whole recipe (Playwright reported "6 passed (18.4s)" then "(28.2s)"); the
# spread is other agents' load on the box, not the suite. Budget is 2 minutes;
# investigate anything over that rather than raising the timeouts.
savepoint-smoke: webui-ports-check venv-ready
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    {{justfile_directory()}}/scripts/iteration_metrics.sh savepoint-smoke \
        pnpm exec playwright test --config tests/e2e/playwright.savepoint.config.ts

# One-way rekordbox import gate, end to end: proves a sync-triggering control
# renders inert and fires nothing. Self-contained -- its own Vite on 127.0.0.1:5399
# with no backend and every /api/v1 call stubbed in the spec, so it needs no
# library, no daemon, and cannot reach a real rekordbox target. ~6s.
rekordbox-gate-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.rekordbox-gate.config.ts

# Hot-cue mapping gate, end to end (CUES-01, formerly #736): proves an unmapped
# deck saves hot cues into the own cue store, and an unloaded deck stays inert. Its own
# backend + Vite against a throwaway fixture library (support/deckload_fixture.py,
# real folder ingest, no rekordbox mapping by construction) on
# 127.0.0.1:8695/5320, so it needs no daemon and never touches a real library.
hotcue-mapping-gate-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.hotcue-mapping-gate.config.ts

# Karaoke words route + track-page LyricsPanel proof (#2079). Own backend +
# Vite on 127.0.0.1:8706/5328 against support/lyrics_words_fixture.py.
# Consume UI-queued lyric jobs with the batch driver (section-12 R3).
# Loops with a poll sleep; the testable unit is:
#   uv run --no-sync python -m apps.lyrics jobs work --once
lyrics-jobs-worker:
    uv run --no-sync python -m apps.lyrics jobs work

lyrics-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.lyrics-words.config.ts

# /cloudsync UI, end to end (plan W17): config form, Sync now against a REAL
# hub engine, the journal row, the Fleet tab, and the heartbeat-gated chip.
# Hub + spoke engines + Vite on 127.0.0.1:8711/8712/5331 over throwaway
# fixture data dirs, so it needs no daemon and never touches a real library.
cloudsync-ui-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.cloudsync-ui.config.ts

# Comment ('m') hotkey gate, end to end (r3918992947): proves the real
# availability probe and the real hotkey handler together against a real
# backend on 127.0.0.1:8696/5321, no fixture library needed (feedback routes
# only touch <data-dir>/feedback/*.json).
comment-hotkey-gate-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.comment-hotkey-gate.config.ts

# AutoPlay stall-banner gate, end to end (issue #1659 / PLAY-08): proves the
# banner is VISIBLE over /performance, survives the source deck stopping, and
# does not cover deck transport. Own backend + Vite against a throwaway
# fixture library on 127.0.0.1:8699/5324.
autoplay-stall-gate-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.autoplay-stall-gate.config.ts

# Full-reload gate, end to end (REFRESH-01, r3920753724): edits a real source
# file with no HMR-accepting boundary so Vite emits a genuine full-reload,
# then proves the countdown overlay holds it before the ensuing navigation.
# Own vite instance on 127.0.0.1:5323 (no backend, see vite.full-reload-gate.
# config.ts) so the broadcast cannot land mid-test in any other e2e spec.
full-reload-gate-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.full-reload-gate.config.ts

# Signalsmith worklet gate against the BUILT artifact, chromium AND webkit.
# Self-contained -- `pnpm build` then a static server on 127.0.0.1:5311, no
# backend, no library, no data dir. Deliberately NOT a dev-server suite: Vite
# serves the package untransformed, so a dev run is green on a bundle that
# cannot load a single track in the installed app. Proves the raw package asset
# is emitted, that a shipped chunk points at it, and that it registers a
# processor which reaches its ready handshake in both engines. ~30s incl. build.
stretch-artifact-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm exec playwright test --config tests/e2e/playwright.stretch-artifact.config.ts

# Full savepoint gate: unit + types + full pytest + e2e smoke, ~4-5 min.
# `just savepoint-gate --fast` proves units + types only in ~35s.
# The gate owns the exit code -- run it on its own line and read the status
# yourself before pushing. Never pipe it through echo/tee/head or join it to
# the push with `;`, both of which mask a failing exit:
#     just savepoint-gate
#     GATE_EXIT=$?
#     echo "GATE_EXIT=$GATE_EXIT"
savepoint-gate *args:
    ./scripts/iteration_metrics.sh savepoint-gate python3 scripts/savepoint_gate.py {{args}}

# ----- Tests --------------------------------------------------------------
# Workers and the collect floor are defined once in scripts/savepoint_gate.py
# and mirrored here, in the Makefile and in .github/workflows/ci.yml.
# `-n auto` measured 117.3s -> 43.4s median on the full suite (Thu 20 Aug 2026,
# this 10-core Mac, warm, serial and parallel runs interleaved, 3782 passed /
# 80 skipped both ways). --collect-floor is what makes that safe: -n
# redistributes tests, it never removes them, so a run collecting under the
# floor has lost tests and fails instead of reporting a smaller green suite.

# Full suite in parallel. Same tests as `just test-serial`, ~2.7x faster.
test *args:
    uv run --no-sync --with modal pytest -q -n auto --dist loadgroup --collect-floor 3700 {{args}}

# Full suite single-process. Reach for this when reading a traceback: xdist
# interleaves worker output, and a real ordering bug reproduces here.
test-serial *args:
    uv run --no-sync --with modal pytest -q {{args}}

# One file or directory, single-process. No floor -- a scoped run is meant to
# collect less, and a floor that fires on every inner-loop run teaches nothing.
test-scope target:
    uv run --no-sync --with modal pytest -q {{target}}

# CloudSync property simulation, deep profile: 5000 random examples against
# the LWW oracle (plan W18). The ci profile (50 derandomized) runs in the
# normal suite; this one is on demand. Prints hypothesis statistics.
cloudsync-sim-deep:
    MDT_CLOUDSYNC_SIM_PROFILE=deep uv run --no-sync pytest -q -p no:cacheprovider --no-coverage-matrix --hypothesis-show-statistics tests/cloudsync/test_sim_property.py

# Bot review-thread triage gate. Every Codex/CodeRabbit/Devin thread must be
# FIXED, REBUTTED or DEBT-LOGGED before merge (rule: .planning/DEBT-POLICY.md).
# Nonzero exit lists the threads still sitting in silence.
review-triage pr *args:
    uv run --no-sync python -m scripts.review_thread_triage {{pr}} {{args}}

# Classify a PR's Actions jobs so a 0-step infrastructure refusal (a re-run
# resurrecting a pre-migration runner config, issue #1166) reads as NO-RUNNER
# rather than a code defect. Prints one line per job plus a tally; never
# touches runs-on or the CI_RUNS_ON_* variables.
ci-zero-step-triage pr:
    uv run --no-sync python -m scripts.ci_zero_step_triage {{pr}}

# Fail-closed CI-wait primitive (issue #1608). Binds to the PR's CURRENT head
# SHA, derives expected Actions check-run names from that PR's own previous
# push, and polls until every expected check-run exists and is completed.
# Exit 0 SUCCESS or MERGED, 1 TIMEOUT, 2 NO_BASELINE (no prior push to read an
# expectation from), 3 could-not-measure, 4 FAILURE, 5 CLOSED_UNMERGED. Replaces bare `gh pr checks
# <PR> --watch`, which exits 0 within seconds of a push when zero check-runs
# have registered yet -- see scripts/ci_wait.py's module docstring.
ci-wait pr *args:
    uv run --no-sync python -m scripts.ci_wait {{pr}} {{args}}

# Fail-fast CI watcher (specs/ci-fail-fast.md). Polls the PR's pinned head every 30 s,
# prints each newly failed check with the test ids that decided it, and stops on the
# first failure that is not also red on main. Exit 0 GREEN, 1 GENUINE, 3 UNKNOWN,
# 4 KNOWN_RED_ONLY (only main's red, flakes or ratchet debt), 5 CLOSED.
ci-watch pr *args:
    uv run --no-sync python -m scripts.ci_watch {{pr}} {{args}}

# SMARTEST-CI round 7: the miss audit. Would the planner have run the tests each failed
# pull request actually broke, and are those tests in the fast tier? Reads the newest
# failed ci.yml runs' job logs (cached under ~/.cache/opendj/ci-miss-audit), subtracts
# trunk red measured on main in the same window, and prints both recalls with an honest
# denominator. Exit 0 measured, 3 UNKNOWN. Reports only; nothing gates on it yet.
ci-miss-audit *args="":
    uv run --no-sync python -m scripts.ci_miss_audit {{args}}

# Enforcement half of issue #1605 (DEVOPS-07's periodic report is the other
# half): fails when this PR cites a pending v1 id it neither flips to
# shipped nor marks `reqs: no-flip <id> <reason>`.
reqs-flip-gate pr:
    uv run --no-sync python -m scripts.reqs_flip_gate --pr {{pr}}

# AGT-07: revert one merged agentic-testing PR as a unit.
at-revert pr:
    ./ops/agentic_testing/at_revert.sh {{pr}}

# AGT-07: fail if a merged PR labeled agentic-testing has no ledger row.
at-check-ledger:
    uv run --no-sync python -m ops.agentic_testing.check_ledger

# Sol review lane: use the configured Codex subscription CLI to post real
# review threads with the same P-level and BLOCKING/NON-BLOCKING markers.
# Availability depends on the authenticated operator subscription seat.
# --seat auto follows the configured seat route. --dry-run prints without
# posting. Idempotent per head SHA; missing capability is a refusal.
sol-review pr *args:
    uv run --no-sync python -m scripts.sol_review {{pr}} {{args}}

# Claude review lane: use the configured Claude subscription CLI to post
# real review threads with P-level and BLOCKING/NON-BLOCKING markers.
# Run once per head; the command is idempotent per SHA (--force to redo).
# Exit 3 means the review could not be performed (quota, CLI error or oversized
# diff). Missing capability must never read as a completed review.
# --dry-run reviews and prints without posting.
claude-review pr *args:
    uv run --no-sync python -m scripts.claude_review {{pr}} {{args}}

# Grok and Cursor review lanes (the maintainer, Thu 1 Oct 2026): Grok 4.6 on SuperGrok
# Heavy and Composer 2.5 on Cursor Ultra, through the af-sub-broker `sb` CLI,
# subscription only (never metered, never an API key). Same prompt, cap,
# markers and exit 3 contract as sol-review. --dry-run prints, posts nothing.
grok-review pr *args:
    uv run --no-sync python -m scripts.subscription_review grok {{pr}} {{args}}

cursor-review pr *args:
    uv run --no-sync python -m scripts.subscription_review cursor {{pr}} {{args}}

# Reviewer failover chain: THE command to run when coverage reports MISS
# (REVIEW-15; also delivers REVIEW-12, the automated fallback from #4546).
# Exits 0 at once if the head is already covered (Codex app included); there is
# no Codex grace (removed Fri 2 Oct 2026, fleet-af ADR-0010 phase 2b), so
# otherwise it tries Sol, Grok, Cursor, then Claude (the maintainer's
# own allowance, last, once per head), moving on only when a lane could not
# review, and stopping at the first lane whose review coverage then
# recognizes. Exit 75: the head moved mid-run. Exit 3 names every lane's failure.
review pr:
    uv run --no-sync python -m scripts.review_chain {{pr}}

# Claim ports, start BE+FE (Mac Terminal windows or Linux tmux), open loopback.
run:
    uv run --no-sync python -m apps.webui.run_local

# Restart agentbox behind tailnet-only HTTPS Serve (SSH hop from Mac), then open it.
run-agentbox:
    #!/usr/bin/env bash
    set -euo pipefail
    # The origin is READ BACK from the launcher, never spelled here. The MagicDNS
    # name is deployment config, and a URL guessed on this side is how a restart
    # that never came up behind HTTPS looks successful.
    log="$(mktemp)"
    trap 'rm -f "$log"' EXIT
    uv run --no-sync python -m apps.webui.run_agentbox | tee "$log"
    url="$(uv run --no-sync python -c 'import sys; from apps.webui.run_agentbox import serve_url_from_launcher_output as f; print(f(open(sys.argv[1]).read()))' "$log")"
    if command -v open >/dev/null 2>&1; then
        open "$url"
    else
        echo "open $url on a tailnet device (Claude Code's hosted browser cannot reach it)"
    fi

# Prove agentbox short/FQDN HTTP redirects plus secure browser/API startup.
agentbox-check:
    uv run --no-sync python -m apps.webui.run_agentbox --check

# Load one real agentbox track and prove its decoded stem graph plus mute control.
agentbox-stems-check stable_id:
    uv run --no-sync python -m apps.webui.run_agentbox --check --stem-track {{stable_id}}

# Dry-run a crate copy (count + bytes). Spike: `just crate-du --preload1`
crate-du *args:
    uv run --no-sync python -m apps.webui.crate_sync --dry-run {{args}}

# Reconcile from either machine: Mac pushes, agentbox pulls, then audits both books.
crate-sync *args:
    uv run --no-sync python -m apps.webui.crate_sync --live {{args}}

# Live crate push from the Air, explicitly selecting the owner side.
crate-push *args:
    uv run --no-sync python -m apps.webui.crate_sync --live --local {{args}}

# Live crate pull from agentbox, explicitly selecting the replica side.
crate-pull *args:
    uv run --no-sync python -m apps.webui.crate_sync --live --remote {{args}}

# Box-side crate mode, path-map sample, manifest scope, resolve counts.
crate-status *args:
    uv run --no-sync python -m apps.webui.crate_sync --status {{args}}

# Rebuild the replica book from disk and compare it to the owner manifest.
crate-audit *args:
    uv run --no-sync python -m apps.webui.crate_sync --audit --remote {{args}}

# Print the forced-command Mac authorized_keys entry for agentbox crate pulls.
crate-owner-key key_path:
    @uv run --no-sync python -m apps.agentbox.crate_owner_ssh_gate --authorized-key {{quote(key_path)}}

# Prove preload1 audio, ANLZ, and route via Vite/Serve; records the spike gate.
crate-verify *args:
    uv run --no-sync python -m apps.webui.crate_sync --verify-spike --preload1 {{args}}

# ----- Security status (SEC-02) --------------------------------------------
# Regenerate the exact-status table in docs/security/security.md from repo files.
# If the table changed, re-read the SWOT there, then: `just security-status --ack-swot`.
security-status *args="--write":
    uv run --no-project --quiet python -m scripts.security.security_status {{args}}

# Run the security scanners with their positive controls and print a summary table.
# area: all, or a comma list of deps,secrets,sast,workflows. mode: pr (new findings
# versus origin/main) or full. Exit 0 clean, 1 findings, 2 UNKNOWN. Report-only (ADR-0043).
security-scan area="all" mode="pr":
    scripts/security/install_scanners.sh
    scripts/security/scan.sh {{area}} {{mode}}

# ----- Code quality ratchet ----------------------------------------------
# Full detail in ops/quality/README.md.

# Score the tree and fail if anything got worse than the recorded baseline.
quality *args:
    ./scripts/iteration_metrics.sh quality \
        uv run --no-project --quiet python -m scripts.quality_gate --report ops/quality/report.md {{args}}

# Just the fast Python evaluators, no node toolchain needed.
quality-py:
    ./scripts/iteration_metrics.sh quality-py \
        uv run --no-project --quiet python -m scripts.quality_gate --only ruff,complexity,arch,deps

# Hard architecture gate on its own: contracts in .importlinter, no ratchet.
quality-arch:
    uv run --no-project --quiet python -m scripts.quality_gate --only arch

# Per-surface scored rubric (issue #389). Instrument, not a ratchet.
# Low scores exit 0. JSON: just quality-rubric score --all --json
quality-rubric *args:
    uv run --no-sync python -m scripts.quality_rubric {{args}}

# Hard shell gate on its own: three constructs that silently return a plausible
# wrong answer ($? after a pipeline, gh api --arg, unbraced "$VAR:modifier").
# No ratchet. Runs inside `just quality`; this target is the fast standalone.
shell-lint *args:
    uv run --no-project --quiet python -m scripts.shell_construct_lint {{args}}

# Hard sync-schema gate on its own: eight OMISSIONS that raise nothing, ever.
# Forget to bump SCHEMA_VERSION and apply_migrations short-circuits, so the new
# DDL never runs on any database. Forget to add a table to SYNC_TABLES and it
# simply never syncs: no error, no digest entry, no divergence report. Add a
# second schema authority and nothing compares its tables to any inventory --
# that is how apply_pairing_capture_migrations came to create three tables in
# every live state.db that no declaration and no generated document had heard
# of, and how the launcher's Rust module came to create a fourth (both found
# by this recipe, Wed 9 Sep 2026). Declare DEFAULT CURRENT_TIMESTAMP
# and every row it mints carries a naive stamp that quarantines itself out of
# CloudSync forever.
#
# The RUNTIME status surface does NOT cover any of this, and cannot.
# `python -m apps.sync_hub status` (and the /cloudsync page reading the same
# object) reports convergence over the tables the protocol ALREADY KNOWS
# ABOUT, read from a live fleet: it answers "do my machines agree about the
# sync set?". A table missing from that set is outside the question -- two
# machines agree perfectly about data neither of them is sending, so the
# omission reads as health. This recipe asks the other question, against a
# freshly provisioned throwaway DB, so it needs no fleet, no hub and no data
# dir: "is the sync set the same shape as the schema?".
#
# No ratchet. Runs inside `just quality`; this target is the fast standalone.
#
# `--isolated` is safe here and pinned by
# tests/quality/test_sync_drift_imports.py: the linter's whole import graph is
# stdlib plus this repo, so it runs on a fresh clone that has never synced a
# venv -- the same property that lets `just quality` measure it from an
# environment holding only ops/quality/requirements.txt.
sync-drift-lint *args:
    uv run --no-project --isolated --quiet python -m scripts.sync_drift_lint {{args}}

# Type-check the Python interior with the exact mypy the ratchet measures with.
#
# NOT `uv run mypy`, and note the `--isolated`. Without it uv layers these
# packages on top of whatever `.venv` is in the working directory, mypy finds
# real fastapi / pydantic / pytest signatures, and the count changes: measured
# 550 with a venv in scope against 1356 without, on one identical commit. That
# is the difference between a number a developer sees and the one CI records.
# Only this invocation is comparable to ops/quality/baseline.json. Scope comes
# from [tool.mypy] files in pyproject.toml, so no paths are passed here.
#
# Exits non-zero while any type debt remains, which is today and for a while
# yet. That is mypy reporting, not the recipe breaking; the thing that gates is
# `just quality-types`, which compares the counts to their recorded allowance.
# MYPYPATH/PYTHONPATH and a UV_ENV_FILE-supplied .env would otherwise widen
# mypy's import search path outside this pinned env too, the same escape
# scripts/quality_gate.py's _uv(isolated=True) closes for the gated run --
# this recipe is a separate direct invocation and needs the same protections.
typecheck *args:
    env -u MYPYPATH -u PYTHONPATH \
        uv run --isolated --no-project --no-env-file --quiet \
        --with-requirements ops/quality/mypy-requirements.txt mypy {{ args }}

# Just the mypy half of the ratchet, compared against the recorded allowance.
quality-types:
    uv run --no-project --quiet python -m scripts.quality_gate --only mypy

# Re-record allowances after a cleanup. They only ever shrink.
quality-baseline:
    uv run --no-project --quiet python -m scripts.quality_gate --update-baseline --report ops/quality/report.md

# Named trunkio-* because bare trunk-* recipes here mean the main branch.
# Not a gate: `just quality` stays authoritative. Needs `npm ci` once at the
# root; --no-install stops npx from downloading whatever npm package is named trunk.
# Optional Trunk linters (.trunk/trunk.yaml): new issues on changed lines only.
trunkio-check *args:
    npx --no-install trunk check {{args}}

# Trunk formatters on changed files. Only dotenv-linter runs until issue #4456's
# formatting pass lands; the ratified formatters are listed in .trunk/trunk.yaml.
trunkio-fmt *args:
    npx --no-install trunk fmt {{args}}

# Deliberately a flag, not `git config blame.ignoreRevsFile`: that config is shared by
# every worktree of a clone, and git exits 128 in any checkout where the file is absent.
# Blame that skips the format-only commits in .git-blame-ignore-revs (issue #4456).
blame file *args:
    if [ -f .git-blame-ignore-revs ]; then git blame --ignore-revs-file .git-blame-ignore-revs {{args}} -- "{{file}}"; \
    else git blame {{args}} -- "{{file}}"; fi

# Prove base..head changed Python layout only (AST-equal modulo docstring whitespace).
fmt-proof base head="HEAD":
    uv run --no-project --quiet python -m scripts.format_proof prove --base {{base}} --head {{head}}

# Every SHA in .git-blame-ignore-revs is an ancestor of HEAD with a style(format): subject, proven format-only.
fmt-ignore-revs-check:
    uv run --no-project --quiet python -m scripts.format_proof ignore-revs

# ----- Stretch-quality measurement ---------------------------------------
# The table that gates the STRETCH_BLOCK_MS flip (latency round 2 step 2).
# Binding spec .planning/QUALITY-METHODOLOGY-RECONCILED.md; full detail and the
# open questions in ops/quality/stretch/README.md.
#
# Roughly 70 minutes for the 6 x 9 x 4 grid: every cell is rendered TWICE and
# the two sha256s compared, because determinism is enforced rather than
# recorded. The manifest is written after every fixture and merged on re-run,
# so an interrupted grid resumes instead of starting over.
#
# The daemon is needed ONLY to extract fixtures (--base-url, the lane's own
# port). The render half serves the vendored signalsmith module to a synthetic
# origin through Playwright request interception, so it needs no dev server, no
# port and no data dir.
stretch-quality *args:
    uv run --no-sync python -m scripts.quality.run {{args}}

# Analysis primitives only. Numpy-only by design: decoding happens in Chromium,
# so librosa/soundfile/torch never enter the repo venv.
stretch-quality-tests:
    env -u MDT_DATA_DIR uv run --no-sync python -m pytest tests/quality -q

# The CI-runnable render smoke: one fixture, one condition, two arms, no daemon.
stretch-quality-smoke:
    cd apps/webui/frontend && pnpm exec playwright test --config tests/e2e/playwright.stretch-quality.config.ts

# PERF-R6 boot-burst bench: what a deck load costs when it is fired while the
# app is still starting. A MEASUREMENT lane, not a gate -- it prints medians
# and writes its samples, it never fails on a threshold. Production build,
# real single-worker engine, chromium (the six-connections-per-origin pool is
# half the mechanism), its own throwaway fixture on port 8692.
#
# BOOT_BURST_LATENCY_MS puts every request in BOTH arms of a comparison into a
# regime where per-request cost is the binding constraint, which is what a real
# 8000-row library produces and a two-track fixture does not. 0 (default)
# measures raw loopback, where the burst is ~86ms and the effect is under the
# machine's own noise. Read the spec's header before quoting any number.
boot-burst-bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm build
    pnpm exec playwright test --config tests/e2e/playwright.boot-burst.config.ts {{args}}

# Stem decode perf bench (#1509 row 6): four-way FLAC decode on the production
# browser path. MEASUREMENT lane only; prints medians to stem-decode-bench.json.
stem-decode-bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm build
    pnpm exec playwright test --config tests/e2e/playwright.stem-decode-bench.config.ts {{args}}

# PERF-UI-05 playlist tree / switch latency gate (issue #3530). Production
# build, real 1k-track fixture on port 8713, asserts p50 caps. Default e2e
# runs write to gitignored test-results/; this recipe opts in to refresh the
# committed library-playlist-switch-bench.json snapshot for pytest.
playlist-switch-bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm build
    PLAYLIST_SWITCH_BENCH_UPDATE_FIXTURE=1 \
    PLAYLIST_SWITCH_BENCH_OUT=tests/fixtures/library-playlist-switch-bench.json \
    pnpm exec playwright test --config tests/e2e/playwright.playlist-switch-latency.config.ts {{args}}

# Waveform render perf bench (#1509 row 6): native _rb_waveform_native
# materialization. MEASUREMENT lane only; requires the release waveform wheel.
waveform-render-bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run --no-sync maturin develop --release \
        --manifest-path apps/webui/server/native/waveform/Cargo.toml
    uv run --no-sync python scripts/bench/waveform_render_bench.py {{args}}

# WebKit performance controls against the PRODUCTION build served by the engine.
# The ONLY gate that reproduces WKWebView (the installed app), where every
# chromium/dev gate stayed green while no deck could load a track. Builds first
# on purpose: this suite refuses to run against vite or a stale build. Its
# library is a throwaway fixture on port 8690 -- never the lane data dir, whose
# engine lock is singleton. ~1 min including the build.
webkit-deckload-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm build
    pnpm exec playwright test --config tests/e2e/playwright.webkit-deckload.config.ts

# ENDURANCE SOAK for the audio lifecycle. Plays real audio through the SHIPPED
# rebind + context-watchdog modules for `minutes` (default 10) while a modelled
# output device is flapped, stranded, suspended and crashed under it, and fails
# when the measured signal stays at silence longer than two rebind cooldowns.
# The assertion is the PRESENCE of sound, so a silent run with no errors is RED.
# Its own tier because it is measured in minutes -- never on the PR gate.
# Run it before shipping anything that touches the audio lifecycle.
#   just test-audio-soak        # 10 minutes
#   just test-audio-soak 2      # quick "does it still bite" pass
test-audio-soak minutes="10":
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    MDT_AUDIO_SOAK_MINUTES={{minutes}} pnpm exec playwright test \
        --config tests/e2e/playwright.audio-soak.config.ts

# ERROR HUNT for AutoPlay + mixing on the real /performance UI. Boots the
# real engine against a 6-track throwaway fixture, starts AutoPlay, interleaves
# seeded mixing, and fails on every error signature not named in
# autoplay-error-hunt.allow.json. Own tier because it is measured in minutes --
# never on the PR gate. Feeds #1778 / #1853.
#   just test-autoplay-hunt            # 20 minutes, the default
#   just test-autoplay-hunt 2          # green check on the Linux chromium e2e host
test-autoplay-hunt minutes="20":
    #!/usr/bin/env bash
    set -euo pipefail
    repo_root="$PWD"
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    frontend="$PWD"
    data_dir="$frontend/tests/e2e/fixtures/autoplay-error-hunt-data"
    mkdir -p "$data_dir"
    cd "$repo_root"
    if ! uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.deckload_fixture \
        --data-dir "$data_dir" --seed-autoplay-hunt --manifest "$data_dir/fixture-manifest.json"; then
      echo "UNKNOWN: fixture library failed to build (deckload_fixture exited non-zero)" >&2
      exit 3
    fi
    if [ ! -f "$data_dir/fixture-manifest.json" ]; then
      echo "UNKNOWN: fixture library is missing (no fixture-manifest.json)" >&2
      exit 3
    fi
    # Track count from the manifest the builder just wrote. Do not grep builder stdout.
    python -c 'import json,sys; data=json.load(open(sys.argv[1])); sys.exit(0 if len(data.get("tracks") or [])>=6 else 1)' \
        "$data_dir/fixture-manifest.json" \
        || { echo "UNKNOWN: fixture library has fewer than 6 tracks" >&2; exit 3; }
    cd "$frontend"
    set +e
    MDT_AUTOPLAY_HUNT_MINUTES={{minutes}} pnpm exec playwright test \
        --config tests/e2e/playwright.autoplay-error-hunt.config.ts
    rc=$?
    set -e
    report="playwright-report/autoplay-error-hunt/error-hunt.json"
    results="test-results/autoplay-error-hunt/error-hunt.json"
    # The HTML reporter rewrites playwright-report/ after tests; the spec also
    # writes a copy under test-results/ so a missing HTML sibling is recoverable.
    if [ ! -f "$report" ] && [ -f "$results" ]; then
      mkdir -p "$(dirname "$report")"
      cp "$results" "$report"
    fi
    if [ ! -f "$report" ]; then
      echo "UNKNOWN: engine did not boot or the hunt never wrote a report ($report missing)" >&2
      exit 3
    fi
    exit "$rc"

# TIER 2 of the same fault class: the REAL Tauri window and its REAL WKWebView,
# driven by a WebDriver server compiled into the DEBUG binary only
# (tauri-plugin-wdio-webdriver is a cfg(debug_assertions) dependency, so it
# cannot reach a dmg). Covers the residue tier 1 structurally cannot see: the
# Rust initialization_script, the bootstrap page, and the navigation off
# tauri://localhost. macOS has no WKWebView WebDriver, so `tauri-driver` is not
# an option here. Own fixture library on port 8691, never a real data dir.
# ~2 min on a warm cargo cache, ~6 min cold.
real-shell-e2e:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/webui/frontend
    pnpm exec svelte-kit sync
    pnpm build
    cd ../../desktop/src-tauri
    cargo build
    cd ..
    pnpm install --frozen-lockfile
    pnpm exec wdio run wdio.conf.ts

# End-to-end smoke of the webview MCP (real fixture engine + real DEBUG shell +
# MCP stdio transport). Needs the debug binary: cd apps/desktop/src-tauri && cargo build.
# The MCP itself: apps/desktop/mcp/README.md.
webview-mcp-smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/desktop
    pnpm exec tsc --noEmit -p tsconfig.json
    pnpm exec tsx mcp/smoke.ts

# AGENT-11: real stdio MCP session against the opendj_cli test engine fixture.
opendj-mcp-smoke:
    uv run --no-sync pytest tests/opendj_cli/test_mcp_stdio.py -q

# ----- Fleet dispatch connector (AGENT-15) --------------------------------
# One MCP server, `dispatch`, over the queue, the nucbox dispatcher and the
# fan-out ledger. The server lives in fleet-af (`fleet--mcp/`, its README has
# the surface and rationale); scripts/dispatch_mcp.sh is this checkout's stub.
# Claude Code picks it up from .mcp.json with no setup; Codex needs the
# one-time registration below.

# Serve the dispatch MCP on stdio (what .mcp.json and Codex both launch).
dispatch-mcp:
    ./scripts/dispatch_mcp.sh

# Register the dispatch MCP with Codex for this checkout. Claude Code needs
# nothing: .mcp.json is committed and project-scoped.
dispatch-mcp-register:
    #!/usr/bin/env bash
    set -euo pipefail
    codex mcp add dispatch -- "$(pwd)/scripts/dispatch_mcp.sh"
    codex mcp list

# Prove this checkout's stub reaches the server. The server's own suite runs in
# fleet-af: `just fleet-mcp::test` there.
dispatch-mcp-smoke:
    uv run --no-sync pytest tests/scripts/test_dispatch_mcp_stub.py -q

# ----- Longitudinal diagnostics probe -------------------------------------
# The only per-machine longitudinal signal for the shell/engine/WebKit process
# family: the browser cannot see any of those footprints. Full runbook, including
# what each field means and how to reconcile a machine already running an
# unmanaged copy: docs/perf/diagnostics-probe.md.
#
# The launchd job runs a COPY under ~/Library/Application Support, never the
# repo checkout, so switching branches or deleting a worktree cannot silently
# kill sampling. `probe-install` is therefore also the upgrade command: it
# re-copies and re-bootstraps. Nothing here needs the repo venv (stdlib only,
# /usr/bin/python3), so the probe survives a broken venv.
#
# It is a PACKAGE of stdlib-only modules, not one script, so every entry point
# below runs it with `-m` from the directory holding the package: the repo root
# for the dev recipes, "$diag/bin" for the installed copy.
probe-diag-dir := "$HOME/Library/Application Support/OpenDJ Diagnostics"

# One deep sample to stdout. Writes nothing, installs nothing.
probe-once:
    /usr/bin/python3 -m scripts.diagnostics.opendj_performance_probe --once

# Install or upgrade the launchd job, then show what it became.
probe-install:
    #!/usr/bin/env bash
    set -euo pipefail
    diag="{{probe-diag-dir}}"
    mkdir -p "$diag/bin/opendj_diagnostics" "$diag/performance"
    # Replace, never merge: a module deleted upstream must not linger and be
    # imported by the installed copy.
    rm -rf "$diag/bin/opendj_diagnostics"
    mkdir -p "$diag/bin/opendj_diagnostics"
    cp scripts/diagnostics/*.py "$diag/bin/opendj_diagnostics/"
    chmod 644 "$diag/bin/opendj_diagnostics"/*.py
    # Pre-package single-file installs left this behind; it would shadow nothing
    # but would mislead anyone reading the install dir.
    rm -f "$diag/bin/opendj_performance_probe.py"
    cp scripts/diagnostics/com.opendj.performance-probe.plist \
        "$HOME/Library/LaunchAgents/com.opendj.performance-probe.plist"
    launchctl bootout "gui/$(id -u)/com.opendj.performance-probe" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" \
        "$HOME/Library/LaunchAgents/com.opendj.performance-probe.plist"
    just probe-status

# Is it loaded, is it writing, and how old is the newest sample.
probe-status:
    #!/usr/bin/env bash
    set -euo pipefail
    diag="{{probe-diag-dir}}"
    launchctl print "gui/$(id -u)/com.opendj.performance-probe" 2>/dev/null \
        | grep -E "state|pid|last exit code" || echo "[WARN] launchd job not loaded"
    ls -l "$diag/performance"/opendj-performance-*.jsonl 2>/dev/null \
        || echo "[WARN] no JSONL written yet at $diag/performance"
    tail -c 4096 "$diag/probe-stderr.log" 2>/dev/null || true

# Trend summary over the installed log dir. --summary-hours defaults to 24.
probe-summary *args:
    /usr/bin/python3 -m scripts.diagnostics.opendj_performance_probe \
        --summary --output-dir "{{probe-diag-dir}}/performance" {{args}}

# Read an explicit trend window from the installed JSONL. Example:
# just probe-trend --since 2026-09-05T08:00:00Z
probe-trend *args:
    /usr/bin/python3 -m scripts.diagnostics.opendj_performance_probe \
        trend --output-dir "{{probe-diag-dir}}/performance" {{args}}

# Stop sampling. Leaves the collected JSONL in place on purpose.
probe-uninstall:
    #!/usr/bin/env bash
    set -euo pipefail
    launchctl bootout "gui/$(id -u)/com.opendj.performance-probe" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/com.opendj.performance-probe.plist"
    echo "[OK] probe unloaded; logs kept at {{probe-diag-dir}}/performance"

# ----- Allstack profiling harness -----------------------------------------
# PROFILE INSTEAD OF GUESSING. Which tool reaches which layer, what needs sudo
# and what is impossible on a Mac with no Xcode: docs/perf/profiling-harness.md.
# Every recipe writes under .tmp/perf/ (gitignored) and prints its output path.
#
# ROLE is one of the diagnostics probe's own role names -- desktop-shell,
# python-engine, engine-worker, webkit-webcontent, webkit-gpu,
# webkit-networking -- or `all`. A bare pid works too. Roles resolve through
# the probe's family association, NOT pgrep: this Mac runs ten WebContent
# processes and only one of them is Open DJ's.

# Native CPU sample of one role via /usr/bin/sample. No sudo. Prints hot symbols.
# Variadic like perf-py and perf-alloc, so `--shell-pid PID` reaches the
# script: with two Open DJ builds running, resolve_role refuses as ambiguous
# and this recipe was the only way in that could not pass the selector.
perf-cpu *args="python-engine 5":
    ./scripts/perf/perf_cpu_sample.sh {{args}}

# Python-level frames from the live engine via py-spy. NEEDS SUDO on macOS:
# unprivileged, this installs py-spy and prints the exact command to paste.
perf-py *args:
    ./scripts/perf/perf_py_attach.sh {{args}}

# Live memory triage: vmmap -summary, footprint, leaks. No sudo.
# `just perf-alloc --python-bench scripts/bench/x.py` runs memray instead.
perf-alloc *args="webkit-webcontent":
    ./scripts/perf/perf_alloc.sh {{args}}

# Merge the frontend perf ring and the native probe log into one Perfetto
# trace. With no args, lists the localStorage stores that hold a ring.
perf-trace *args="--find-localstorage":
    uv run --no-project python -m scripts.perf.export_chrome_trace {{args}}

# Score every UX scenario in specs/perf-latency-program.md against what has
# actually been measured. An UNMEASURED scenario is never a passing one, and a
# KPI that measures something adjacent scores nothing at all.
perf-kpis *args:
    uv run --no-project python -m scripts.perf.kpi_scorecard {{args}}

# Stems + lyrics library-processing wait-time KPIs (PERFBATCH-01).
# Withheld is a valid reading when state.db is absent; pass --require-measured
# only when the caller needs numeric wall/throughput on this host.
perf-library-wait *args:
    uv run --no-project python -m scripts.perf.library_wait {{args}}

# Capture S2 press-to-audible, S5 deck-load, S12 CloudSync, and S13 login KPIs against a RUNNING engine.
perf-capture *args:
    uv run --no-sync python -m scripts.perf.capture_kpis {{args}}

# PERFMODE-14: reference-Mac Gig vs Library footprint/CPU ratio capture (Darwin only).
perf-library-mode-capture *args:
    uv run --no-sync python -m scripts.perf.capture_library_mode {{args}}

# Headed Google sign-in that writes a Playwright storageState for S13 capture.
perf-s13-signin *args:
    uv run --no-sync python -m scripts.perf.s13_signin {{args}}

# What is the CEILING on deck load time, before anyone builds the optimization.
perf-amdahl *args="--find-localstorage":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{args}}" = "--find-localstorage" ]; then
        echo "[OK] amdahl_report needs a source; here are the rings on this machine:"
        uv run --no-project python -m scripts.perf.export_chrome_trace --find-localstorage
        echo
        echo "Then: just perf-amdahl --localstorage <path>"
        exit 0
    fi
    uv run --no-project python -m scripts.perf.amdahl_report {{args}}

# The harness's own unit tests (parsing, merge, Amdahl math).
perf-harness-tests:
    uv run --no-sync pytest tests/perf -q

# ----- Rebuild bake-off lane agentB gates (plan s15 K0.7; grows per tranche) --
# v0 (T0): dataless steps only. savepoint-smoke requires an independently
# verified snapshot (via savepoint-smoke-engine); contract-drift runs in CI.
# Private snapshot acceptance data is unavailable in this public copy.
# Selector note: vocals tests import modal and
# analysis tests need the analysis extra, so v0 scoped pytest to tests/webui +
# tests/test_progress.py + tests/engine_core (remap recorded per plan s15).
#
# T3b wave 3-4 added tests/test_conformance.py and tests/open_dj to that scope.
# They were a real blind spot, not a theoretical one: the wave-4 rb_vendor move
# broke 15 conformance tests and this gate stayed green, because a gate that
# cannot see a suite cannot defend it. Both cost 0.39s to run, so there is no
# speed argument for leaving them out. Floor ratcheted 480 -> 850 against 878
# collected, keeping the same 28-test headroom the 480 floor had against 508.
#
# Train 26 found the SAME blind spot a third time. The decrypt work put its 8
# new tests in tests/shared/state/, which this gate did not scope, so the gate
# went green without ever running the tests that proved the train's headline
# change. tests/shared is now in scope: +531 collected (878 -> 1409) for +14.2s,
# and it was already green at 512 passed / 19 skipped / 0 failed. Floor 850 ->
# 1381, same 28-test headroom as every ratchet before it.
#
# Train 29, and the blind spot AGAIN: apps/loudness put its 9 tests in
# tests/loudness/, unscoped, and tests/quality/test_gate_scope.py caught it in
# CI rather than a human noticing. tests/loudness is now in scope. Floor
# ratcheted 4616 -> 4625, by exactly the 9 tests added, which preserves the
# existing headroom instead of re-baselining against a local collect count CI
# may not reproduce. The scope guard, not the floor, is what actually defends a
# new suite: the floor is coarse by design.
#
# Train 28 found it a FOURTH time. The dmg train put all 41 of its tests in
# tests/scripts/, unscoped, so the gate blessed the packaging work without
# running a line of it. tests/scripts is now in scope: +175 collected
# (1409 -> 1584) for +2.0s, already green at 175 passed / 0 failed. Floor
# 1381 -> 1556, same 28-test headroom as every ratchet before it.
#
# Train 29 found it a FIFTH time, one train after the fourth. The stretch-quality
# harness put all 47 of its tests in tests/quality/, unscoped. tests/quality is
# now in scope: +47 collected (1691 -> 1738) for +1.3s, already green at 47
# passed / 0 failed. Floor 1556 -> 1710, same 28-test headroom.
#
# Five for five now says this is not bad luck, it is the DEFAULT: an unscoped
# suite is the normal outcome of adding a test directory, because nothing in the
# repo forces the gate to notice.
#
# Train 30 found it a SIXTH time, and that one was a security suite: the one-way
# rekordbox gate landed 91 tests across three tests/*.py modules that this gate
# did not name, so the gate would have blessed a write-refusal guard without
# executing a single refusal probe. That is where the counting stopped and the
# durable fix got written: tests/quality/test_gate_scope.py now fails when a
# test directory or root module exists that pytest_scope does not cover and the
# guard's known-unscoped list does not already name. A new suite can still be
# left out, but only ON PURPOSE and in writing.
#
# The same train scoped in its own footprint: dedup, reconcile, smartlists and
# sync, the six touched root modules, the three new security modules and the
# guard's own 23 tests. +1069 collected (1738 -> 2807) for +35s (47.6s ->
# 82.4s), already green at 2723 passed / 84 skipped / 0 failed. Floor
# 1710 -> 2779, the same 28-test headroom as every ratchet since the
# 480-against-508 precedent.
#
# The follow-up train scoped in tests/test_requirement_markers.py too, on the
# same principle one turn later: that suite is the guard proving every
# @pytest.mark.requirement ID resolves in reqs.json, and the train was ADDING
# four such markers. Landing markers while the test that validates them sits
# outside the gate is the blind spot wearing a different hat. +3 collected
# (2807 -> 2810), floor 2779 -> 2782.
#
# The recurring lesson, now six for six: a new suite defaults to INVISIBLE
# here, and every green gate over an unscoped suite is a green that means less
# than it looks. When a train adds tests, check they are collected before
# trusting the gate that blessed them.
#
# PARITY-03 scoped in tests/test_analysis_backlog.py on the same principle, Tue
# 1 Sep 2026: that suite is the guard over the SQL that decides which tracks the
# auto-analyzer drains, so landing the drain loop while its selection test sat
# outside the gate is the blind spot wearing a third hat. +15 collected, floor
# 3112 -> 3127. The webui half of the same train needed no scope change,
# tests/webui was already named.
#
# tests/test_analysis_run_classification.py scoped in Wed 2 Sep 2026, same
# principle again: it is the guard over WHICH analyzer failures are per-file,
# and that classification is what the chunked drain reads to decide whether to
# keep going, so an unscoped guard there is a green that means less than it
# looks. It lives at tests/ root rather than in tests/analysis/ precisely so it
# CAN be scoped - that package's conftest skips without soundfile, which is the
# heavy audio dep deliberately kept out of the repo venv. +2 collected, floor
# 3127 -> 3129. tests/test_analysis_backlog_token.py joined it the same day
# for the same reason, when the token tests outgrew their parent file's
# 600-line ceiling: +3 collected, floor 3129 -> 3132. Review round 19 grew the
# classification guard by the two cases that pin the decode-site allow-list from
# both sides - an undecodable file stays per-file even on an ffmpeg-less box, a
# misconfigured sample rate never does: +2 collected, floor 3132 -> 3134.
# Round 23 added the platform-selector case for the content token, the
# running-drain booking case and the survivor-restart case, and retired the
# hand-built missing-target case that the first of those now drives through the
# real route: net +2 collected, floor 3134 -> 3136. Round 25 added the case
# that pins a newer drain's deliberate signature clear against the watcher's
# own older one: +1 collected, floor 3136 -> 3137. Round 26 added the case
# where that clear has to reach an ALREADY-settled booking: +1 collected,
# floor 3137 -> 3138. Round 28 added the post-admission target-loss case, its CLI
# exit-status case and the case pinning the suite's forced auto-analyze off
# over an inherited `on`: +3 collected, floor 3138 -> 3141. Round 29 added
# the case where the slot is claimed one statement after the drain starts:
# +1 collected, floor 3141 -> 3142. Round 30 added the two cases where a
# library refresh takes the slot after a drain cleared its signature, one
# per reader: +2 collected, floor 3142 -> 3144. Round 31 added the two
# denied-read cases, backend and CLI: +2 collected, floor 3144 -> 3146.
# Round 32 added the manual-drain-inside-the-scan case: +1, 3146 -> 3147.
# Round 33 added the drain-at-the-slot-claim case: +1, 3147 -> 3148.
# Round 34 added the shutdown-at-the-slot-claim case: +1, 3148 -> 3149.
#
# tests/test_ci_host_serialization.py scoped in Thu 3 Sep 2026, landing with
# #1014's move to self-hosted runners: it is the guard over the fixed-port
# host lock and job ordering that PR #1014's own review flagged as P1, so an
# unscoped guard there is exactly the blind spot this file exists to close.
# +3 collected, floor 3149 -> 3152.
#
# tests/test_planning_merge_drivers.py scoped in Sun 6 Sep 2026 (issue #1174):
# it is the local-only driver proving the .gitattributes union merge on the
# two append-only planning ledgers actually retains both sides on a real git
# merge, plus the negative control that reqs.json still conflicts instead of
# silently merging invalid JSON. tests/quality/test_gate_scope.py caught its
# own PR landing unscoped, per this file's docstring. +3 collected, floor
# 5547 -> 5550.
#
# tests/test_uv_run_no_sync.py scoped in Tue 8 Sep 2026: it is the guard over
# every `uv run` command this repo launches -- the AST scan over argv lists in
# apps/ and scripts/, plus the text scan over the launchers that spell the
# command as a STRING (shell scripts, Playwright configs, workflow YAML, this
# file and the Makefile) -- proving each one carries --no-sync so a bare
# `uv run` cannot re-sync the environment and prune extras out from under the
# live preview again. tests/quality/test_gate_scope.py caught its own PR
# landing unscoped, same pattern as every suite before it. 3 test functions:
# +3 collected, floor 5564 -> 5567.
#
# Tue 8 Sep 2026, PR #1514 scoped in tests/analysis_beatgrid (the beat-grid
# producer's pure policy modules; the model tests inside it are opt-in behind
# MDT_BEATGRID_MODEL_TESTS and collect but skip): floor 5567 -> 5607. Its
# review round added tests/analysis_beatgrid/test_bar_phase.py, 11 test
# functions pinning the 1-to-4 bar cycle: +11 collected, floor 5607 -> 5618.
#
# tests/test_move_copy_between_playlists.py and
# tests/test_playlist_write_persistence.py scoped in Thu 11 Sep 2026 (issue
# #1847): both are FastAPI/PlaylistStore suites at tests/ root, same stack as
# tests/test_apply_cues.py, already in pytest_scope. The gate-scope enumerator
# only sees directories and root test_*.py, so they landed unregistered rather
# than as known blind spots. Neither imports soundfile or modal, so they can
# collect in the repo venv. Scope them in rather than listing them as
# unexamined. +17 collected, floor 6920 -> 6937.
#
# tests/streaming_transfer scoped in Fri 11 Sep 2026 with META-05 (#960): the
# suite locks captured Spotify/SoundCloud provider payloads and the CLI safety
# gate, so leaving it off pytest_scope is exactly the blind spot this guard
# exists to close. +4 collected, floor 6937 -> 6941.
#
# tests/parity scoped in Sat 12 Sep 2026 with PARITY-01 (#1520): the suite pins
# per-lane scorers against committed library-shaped fixtures, and
# tests/quality/test_gate_scope.py caught it unregistered even though it already
# ran in CI shards, so leaving it off pytest_scope is exactly the blind spot
# this guard exists to close. +60 collected, floor 6941 -> 7001.
#
# tests/test_playlist_add.py and tests/test_smartlists_delete.py scoped in Sat
# 12 Sep 2026 (LIBM-20 #1849, LIBM-83 #1850): FastAPI suites at tests/ root,
# same stack as tests/test_apply_cues.py. test_smartlists_delete.py loads
# tests/test_smartlists_route.py via pytest_plugins (+23 collected). +39
# collected, floor 7001 -> 7040.
#
# tests/test_playlist_remove.py scoped in Sat 12 Sep 2026 (LIBM-21 #2225):
# FastAPI suite at tests/ root, same stack as tests/test_playlist_add.py.
# +9 collected, floor 7040 -> 7049.
#
# tests/controller_probe, tests/playlist_sets, tests/test_playlist_forbid_duplicates.py,
# tests/test_playlist_memberships_scale.py, tests/test_playlist_move.py,
# tests/test_smartlists_duplicate.py, tests/test_smartlists_include_counts.py,
# tests/test_smartlists_rename.py and tests/test_smartlists_soft_delete.py scoped
# in Mon 14 Sep 2026 (issue #2410 item 6): test_gate_scope.py caught all nine
# unregistered. The floor itself was stale independent of this change, by a wide
# margin: main's own scope (unchanged) collects 11305 on a venv synced with
# `--extra dev --extra analysis`, far past the recorded 7049, from unrelated
# growth since that number was last taken. Re-measured the whole scope rather
# than trusting the old number plus a delta: main's scope alone collects 11305;
# adding these nine suites collects 11369, a +64 covering their own tests plus
# whatever cross-import they pull in. floor 7049 -> 11369.
#
# Both counts above need `uv sync --extra dev --extra analysis`: tests/analysis
# is already in scope and needs `soundfile` to COLLECT rather than abort the
# whole invocation (pytest treats a conftest's module-level `importorskip`
# failure during initial-conftest loading as fatal, not a per-directory skip).
# The recipe runs `--no-sync` on purpose (see the UV_NO_SYNC note on the CI job
# below), so it cannot sync that extra itself -- it can only fail loudly if it
# is missing, which the `python -c 'import soundfile'` line below does before
# the count means anything.
#
# tests/ops is included in the scope checked by test_gate_scope.py.
# It is the bash-syntax /
# fail-fast / line-format guard over ops/testmac/{setup.sh,verify.sh,
# agt-persona-loop.sh}, runs the real scripts through real bash with no live
# Mac and no soundfile/modal dep, so there is no technical reason to leave it
# in KNOWN_UNSCOPED. The floor itself was stale independent of this change,
# same recurring lesson as Mon 14 Sep's other nine-suite entry above: main's
# own scope (unchanged) now collects 11539, past the recorded 11369 from
# unrelated growth since that number was last taken. Re-measured the whole
# scope rather than trusting the old number plus a delta: main's scope alone
# collects 11539; adding tests/ops collects 11554, a +15 covering its own
# tests. floor 11369 -> 11554.
#
# tests/sync_hub scoped in Mon 14 Sep 2026 (14f0a845a, 34cdede8b): test_gate_scope.py
# caught it unregistered, same pattern as above. Ten quarantine-log tests, no
# soundfile/modal dependency. The floor itself was stale independent of this
# change: main's scope alone now collects 11742 (needs `--extra dev --extra
# analysis` to collect tests/analysis's soundfile-gated cases), past the
# recorded 11554 from unrelated growth. Re-measured the whole scope: main's
# scope alone collects 11742; adding tests/sync_hub collects 11752, a +10
# covering its own tests. floor 11554 -> 11752.
#
# tests/test_security_status_doc.py scoped in Tue 15 Sep 2026 (6f5036d1b, SEC-02):
# test_gate_scope.py caught it unregistered on main. Five stdlib-only tests over
# scripts/security/security_status.py. Re-measured the whole scope with
# `--extra dev --extra analysis`: main's scope alone collects 12127 (unrelated
# growth past the recorded 11752); adding this suite collects 12132, a +5
# covering its own tests. floor 11752 -> 12132.
#
# tests/fleet_mcp scoped in Mon 21 Sep 2026 with the dispatch MCP connector
# (AGENT-15, AGENT-16): it is the guard over a surface every agent in the
# checkout calls, and over the refusals that keep it from becoming a second
# ungated path onto nucbox, so an unscoped guard there is exactly the blind
# spot this file exists to close. Its own collection is +72 (47 for the stdio server, 25 for the
# remote endpoint's Access gate and bind guard); the floor is
# ratcheted by that delta rather than by a re-measured absolute, because the
# full scope cannot be collected without the audio extras. floor 12132 -> 12204.
# Codex round 1 on #3735 fixed two P1s and two P2s and added the tests that
# make each one bite: the claim contract against the REAL progress route (3,
# in test_ledger_contract.py, which is what caught the payload the always-200
# transport hid), the health aggregate refusing a FAILED probe and its
# no-server control (3), the done/open state split (2) and the priority query
# (1, replacing a post-filter test that no longer describes the code).
# +9 collected, floor 12204 -> 12213.
#
# What is STILL unscoped, measured Wed 19 Aug 2026, so the next reader does not
# have to rediscover it: adapters 102, agentbox 7, audit 36, cloud 31, dedup 24,
# dj_copilot 99, docs 32, fixtures 15, integration 6, launcher 12, pairings 10,
# play_analytics 11, reconcile 231, sets 149, smartlists 154, spotify 84,
# stems 30, sync 438, tags 35, vocals 156, voice 220. That is roughly 1,880
# tests this gate does not run. They were NOT scoped in here because it is a
# scope decision worth taking deliberately rather than inside a packaging train,
# and because tests/analysis, tests/stems and tests/vocals cannot be scoped as
# they stand: they need `soundfile`, which is a heavy audio dep deliberately
# kept out of the repo venv, so collecting them errors rather than skips.
#
# The floor counts COLLECTED tests on purpose, and that choice is load-bearing:
# collection does not depend on which extras are installed, but the pass/skip
# split does. The same tree reads 829 passed / 49 skipped in an unsynced venv
# and 844 passed / 34 skipped in a fully synced one, because
# tests/test_conformance.py skips flip to passes once the extras are present.
# Both readings are 878 collected. So a floor written against "passed" would
# move with the machine rather than with the code; this one does not. Any
# test-count figure quoted anywhere else must state its venv state to mean
# anything.
#
# Train 23 closed the sibling blind spot. This ran `just quality-py`, which is
# `--only ruff,complexity,arch,deps`: the frontend and size evaluators never
# ran, so five frontend allowances drifted past their baseline while every lane
# gate went green. It now runs the full `just quality`, all six evaluators, the
# same invocation CI uses, so the lane and CI can no longer disagree about what
# the tree scores. quality-py is deliberately left alone: it is documented as
# the node-free subset and this gate already needs node for pnpm. The report
# and metrics files it writes are gitignored, so the pristine check above is
# unaffected.
#
# TIER 1 IS PART OF THE GATE. Every step above this one runs under node, vite
# or chromium, and the lane A T1 parity finding is that all of them stay green
# while the installed app is broken: the production transform is where that
# class of fault lives, and no dev-server gate can see it. webkit-deckload-e2e
# is the cheapest reproduction of it, about 1 min including its own build, so
# it runs LAST and lets the fast checks fail first. It builds its own SPA and
# boots its own engine on 8690 over a throwaway fixture library, so it wants
# nothing from the lane data dir and cannot collide with the lane's daemon.
# Its build and fixture artifacts are gitignored, which is what lets a step
# that writes a build sit inside a recipe that opens by demanding a pristine
# tree.
#
# tests/standalone scoped in Sat 19 Sep 2026 (issue #3535, STANDALONE-01):
# tests/quality/test_gate_scope.py caught it unregistered -- the suite IS the
# instrument that proves the app runs with no rekordbox artifact, so leaving
# it outside the gate was the blind spot wearing its most on-the-nose hat yet.
# Re-measured the whole scope: without it, 14777 collected (unrelated growth
# past the recorded 12213); adding tests/standalone collects 14783, a +6
# covering its own tests. Floor 12213 -> 14783.
#
# tests/fleet_mcp left the scope Thu 1 Oct 2026 with the dispatch MCP server,
# which moved to fleet-af (`fleet--mcp/`, maintainer/fleet-af#9).
# Its 81 collected tests run there now; tests/scripts/test_dispatch_mcp_stub.py
# replaces them here with 4 tests over the caller-side stub. Ratcheted by that
# delta (-81 +4), floor 14783 -> 14706.
gate:
    test -z "$(git status --porcelain)" || { echo '[ERROR] lane not pristine'; exit 1; }
    just quality
    cd apps/webui/frontend && pnpm run check
    cd apps/webui/frontend && pnpm run test:unit
    env -u MDT_DATA_DIR uv run --no-sync python -c 'import soundfile' || { echo "[ERROR] soundfile missing: tests/analysis is in pytest_scope and needs it to collect rather than abort collection entirely. Run: uv sync --extra dev --extra analysis"; exit 1; }
    n=$(env -u MDT_DATA_DIR uv run --no-sync python -m pytest {{pytest_scope}} --collect-only -q | grep -c '::' || true); echo "collected $n tests"; [ "$n" -ge 14706 ] || { echo "[ERROR] pytest floor: $n below 14706"; exit 1; }
    env -u MDT_DATA_DIR uv run --no-sync python -m pytest {{pytest_scope}} -q
    bash scripts/lane_commit_lint.sh
    just webkit-deckload-e2e

# ----- Pre-push contract guard -------------------------------------------

# The six CI failures a builder can see BEFORE burning a red PR, in one pass.
#
# Written Fri 5 Sep 2026 against five reds in one night, all of them
# mechanical and all of them findable locally in under a minute. #1177, #1178
# and #1188 each changed a route or a model and pushed WITHOUT regenerating
# apps/webui/openapi.json and apps/webui/frontend/src/lib/api-types.ts, which
# are two separate artifacts of one contract change: regenerating either alone
# leaves the other stale and reds a different CI job. #1181 collided a new
# requirement id with one already on main. #1178 and #1181 also crossed a file
# size ratchet. Each one cost a fixer agent and 20+ minutes of CI to learn
# something this recipe reports in seconds.
#
# Step 6 (bundle budget) was added Sun 6 Sep 2026 for the same reason: #1288
# and #1291 (batch 9e, Sat 5 Sep 2026) both passed every step below AND
# `quality_gate`, then both reded on ci.yml's "Bundle budgets (per-surface
# gzip ceilings)" step, which runs `bash scripts/check-bundle-size.sh` against
# a production `pnpm build` from apps/webui/frontend. That check existed only
# in CI. See the `bundle-budget` recipe below for why it is gated rather than
# unconditional.
#
# This is deliberately NOT `just gate`. The gate demands a pristine tree, runs
# the whole pytest scope and a chromium build, and takes minutes; it answers
# "is this lane green?". This answers the narrower question "will the contract
# and hygiene jobs red on me?", which is the question that actually went
# unasked five times. Run BOTH before a PR you care about; run this one every
# time.
#
# Ordering is cheapest-first so the common failure reports fastest, and every
# step is the same invocation CI uses:
#   1. build_reqs_json --check        -> duplicate requirement ids, reqs.json drift
#   2. debt_index --check             -> a malformed debt file, or a .planning/TECH-DEBT.md
#      that is hand-edited or stale for the debt files it references (a PR's own
#      new debt file may stay unindexed - issue #1415)
#   3. openapi.json dump + diff       -> ci.yml "Contract drift - openapi.json"
#   4. api:gen + git diff             -> ci.yml "Contract drift - TS client"
#   5. quality_gate (full, no --only) -> ci.yml "Quality ratchet", every metric
#   6. bundle-budget (conditional)    -> ci.yml "Bundle budgets (per-surface gzip ceilings)"
#
# Steps 2 and 3 REGENERATE in place and then compare against COMMITTED
# state, so commit first: an uncommitted regeneration reads as drift here
# exactly as it would in CI. When they do fail they have already written the
# correct file, so the fix is a commit rather than another command.
#
# MDT_LIBRARY_MODE=local mirrors what CI dumps the baseline under, so the
# schema this compares is the schema CI compares.
#
# Step 5 used to run `quality_gate --only size`, which only checks the file
# size ratchets. That let #1238 pass pre-push locally twice and then red CI
# ~12 minutes later on frontend.import_cycles / frontend.max_fan_in /
# frontend.max_fan_out -- metrics `--only size` never touches. Fixed Sat 5 Sep
# 2026: this now runs the EXACT invocation ci.yml's "Quality ratchet" job
# runs (same flags, same throwaway --isolated --no-project toolchain, no
# --only), so a pass here means every gated metric, not one of them. It is
# measurably slower for that reason -- the run prints its own wall time so the
# cost stays visible rather than hidden.
#
# Step 6 (bundle budget) is gated on a real change, not unconditional. The
# check needs a production `pnpm build`, which is the single slowest thing in
# this recipe by a wide margin -- and `pre-push` exists to answer "will CI red
# on me" in well under a minute for the common case of a backend-only change,
# where the frontend bundle cannot possibly have moved. Rejected alternatives:
# always building (pays a full production build on every backend-only push,
# which is exactly the "materially slower for every backend-only change" trade
# this recipe exists to avoid) and reusing a stale build artifact keyed on
# nothing (silently checks yesterday's bundle, which is worse than not
# checking: it reports PASS on a budget that would fail against what actually
# ships).
#
# The gate is an EXCLUSION list, not an allowlist, and that direction is
# deliberate: diff HEAD against the merge-base with origin/main over the
# whole `apps/webui/frontend` tree, and skip only when every changed path
# falls inside a small set already proven not to reach the build (test
# specs under `tests/`, Storybook config, README, `knip.js`,
# `playwright.config.ts`, `.nvmrc`, `.gitignore`). An earlier version of this
# gate allowlisted `src/` plus a handful of named config files, and a
# blinded review caught the real gap in that direction: any OTHER tracked
# build input (this project has no `index.html`, but does have
# `tsconfig.json`, which sets `target`/`module` and can change transpiled
# bytes, and a pnpm patch file under `patches/` that changes the actual
# dependency code Vite bundles) would have silently skipped. Getting a new,
# unenumerated build input WRONG in an allowlist means a silent false
# negative -- the exact bug this whole task exists to close. Getting one
# wrong in an exclusion list means an unnecessary build, which only costs
# time. So the failure mode is downgraded from silent-miss to slow-false-
# positive, which is the direction to err in for a correctness gate.
review-lease-check pr *args:
    uv run --no-sync python -m scripts.review_lease check --pr {{pr}} {{args}}

review-lease-check-branch *args:
    uv run --no-sync python -m scripts.review_lease check-branch {{args}}

review-lease-handoff pr fleet *args:
    uv run --no-sync python -m scripts.review_lease handoff --pr {{pr}} --fleet {{fleet}} {{args}}

pre-push: venv-ready
    @echo '[pre-push] 0/9 reviewer lease (open PR on current branch only)'
    _current=$(git branch --show-current); \
    if [ -n "$_current" ] && [ "$_current" != "main" ] && [ "$_current" != "master" ]; then \
      uv run --no-sync python -m scripts.review_lease check-branch --branch "$_current" || { echo '[ERROR] pre-push: active reviewer lease blocks this branch. Run `just review-lease-check <pr>` for details, or complete handoff.'; exit 1; }; \
    else \
      echo '[pre-push] reviewer lease skipped: not a feature branch'; \
    fi
    @echo '[pre-push] 1/9 requirement ids and reqs.json drift'
    uv run --no-sync python -m scripts.build_reqs_json --check || { echo '[ERROR] pre-push: REQUIREMENTS.md has duplicate ids, or reqs.json is stale. Fix the ids, then regenerate: uv run --no-sync python -m scripts.build_reqs_json'; exit 1; }
    @echo '[pre-push] 1b/9 requirement-marker ratchets (every marked test scope carries one intent line)'
    uv run --no-sync pytest -q -p no:cacheprovider tests/test_requirement_markers.py tests/test_requirement_intent_ratchet.py tests/quality/test_gate_scope.py || { echo '[ERROR] pre-push: a @pytest.mark.requirement scope (module pytestmark or a decorated test) has no single-line intent docstring of the form [if] X [then] Y, [else stop]. (under 100 chars), or a gate suite is unregistered. The nucbox merge gate refuses the PR for the same reason (issue #3051).'; exit 1; }
    just launch-json-check || { echo '[ERROR] pre-push: .claude/launch.json is stale for the default profile. Regenerate from .claude/dev-servers.json with `just launch-json` or `just launch-json PROFILE=default` before committing; use `just launch-json PROFILE=worktree` only for local preview_start refresh.'; exit 1; }
    @echo '[pre-push] 3/9 tech debt index drift'
    uv run --no-sync python -m scripts.debt_index --check || { echo '[ERROR] pre-push: a debt file is malformed, or .planning/TECH-DEBT.md is hand-edited or stale for the debt files it references. Fix malformed debt files; regenerate the index only on main (uv run --no-sync python -m scripts.debt_index), never in a PR (issue #1415).'; exit 1; }
    @echo '[pre-push] 3b/9 blame ignore list names only format commits on this branch (issue #4456)'
    uv run --no-sync python -m scripts.format_proof ignore-revs || { echo '[ERROR] pre-push: the .git-blame-ignore-revs check failed or could not measure (reason on the line above). A squash or rebase merge rewrites a format commit SHA: list the SHA main actually contains.'; exit 1; }
    @echo '[pre-push] 4/9 ADR line on gated paths (issue #1489)'
    uv run --no-sync python -m scripts.adr_check --base origin/main || { echo '[ERROR] pre-push: a gated path (apps/cloud, apps/engine_core, webui server state, REQUIREMENTS.md, or a top-level lockfile) needs ADR: <id> or ADR: none, because <reason> in the PR body (commit messages stand in before a PR exists). See docs/decisions/README.md.'; exit 1; }
    @echo '[pre-push] 5/9 openapi.json contract drift'
    MDT_LIBRARY_MODE=local uv run --no-sync python -c "{{engine_openapi_dump}}" apps/webui/openapi.json
    git diff --exit-code apps/webui/openapi.json || { echo '[ERROR] pre-push: apps/webui/openapi.json was stale. The dump above has already rewritten it -- commit it, and commit the regenerated api-types.ts with it.'; exit 1; }
    @echo '[pre-push] 6/9 TS client contract drift'
    cd apps/webui/frontend && pnpm run api:gen && git diff --exit-code src/lib/api-types.ts || { echo '[ERROR] pre-push: src/lib/api-types.ts is stale. pnpm run api:gen rewrote it -- commit the result alongside openapi.json.'; exit 1; }
    @echo '[pre-push] 7/9 full quality gate (same invocation as ci.yml "Quality ratchet", no --only)'
    _pre_push_gate_start=$(date +%s); \
    uv run --isolated --no-project --python 3.11 --with-requirements ops/quality/requirements.txt python -m scripts.quality_gate --report ops/quality/report.md --json ops/quality/metrics.json; _pre_push_gate_status=$?; \
    echo "[pre-push] quality gate took $(( $(date +%s) - _pre_push_gate_start ))s"; \
    [ "$_pre_push_gate_status" -eq 0 ] || { echo '[ERROR] pre-push: the quality gate regressed. See ops/quality/report.md for which metric and file. Fix the regression -- never shave lines, comments, tests or strings to fit, and never edit ops/quality/baseline.json.'; exit 1; }
    @echo '[pre-push] 8/9 bundle budget (frontend only, run when a build-relevant frontend file changed since origin/main)'
    _bb_base=$(git merge-base HEAD origin/main) || { echo '[ERROR] pre-push: git merge-base HEAD origin/main failed -- fetch origin first.'; exit 1; }; \
    _bb_changed=$(git diff --name-only "$_bb_base" HEAD -- apps/webui/frontend); \
    _bb_relevant=$(printf '%s\n' "$_bb_changed" | grep -vE '^apps/webui/frontend/(tests/|\.storybook/|README\.md$|knip\.js$|playwright\.config\.ts$|\.nvmrc$|\.gitignore$)'); \
    if [ -z "$_bb_changed" ] || [ -z "$_bb_relevant" ]; then \
        echo '[pre-push] bundle budget skipped: no build-relevant apps/webui/frontend changes vs origin/main'; \
    else \
        _bb_start=$(date +%s); \
        just bundle-budget && _bb_status=0 || _bb_status=1; \
        echo "[pre-push] bundle budget took $(( $(date +%s) - _bb_start ))s"; \
        [ "$_bb_status" -eq 0 ] || { echo '[ERROR] pre-push: bundle budget check failed (build error, or a gzip budget exceeded). See output above for which budget, the measured bytes, the limit, and the exact command to reproduce.'; exit 1; }; \
    fi
    @echo '[pre-push] PASS: reviewer lease, reqs ids + reqs.json, launch.json registry check, tech debt index, ADR line, openapi.json, api-types.ts, full quality gate, bundle budget all clean.'

# Merge-lane ADR duplicate gate (issue #3076). Nucbox requirement-marker-ratchet-gate.sh
# should call this same command before every merge.
adr-merge-check:
    uv run --no-sync python -m scripts.adr_check --merge-base origin/main

# The production build + per-surface gzip budget check
# (apps/webui/frontend/scripts/bundle-budget.mjs via check-bundle-size.sh),
# the exact check ci.yml's "Bundle budgets (per-surface gzip ceilings)" step
# runs. Its own recipe because `pre-push` calls it CONDITIONALLY (only when
# the frontend diff vs origin/main could move a gzip byte -- see the comment
# above `pre-push`); call this directly to gate a frontend PR on every push
# regardless, or to reproduce a CI bundle-budget failure locally.
bundle-budget:
    cd apps/webui/frontend && pnpm build && bash scripts/check-bundle-size.sh

# ----- Desktop app packaging ---------------------------------------------

# Check EVERY dmg build precondition in one pass, change nothing (OPS-11).
#
# Run this before `just dmg` when you are not sure the machine is ready. It
# reports every unmet precondition at once (working tree, signing config, rust
# and node toolchains, built SPA) rather than the first one, which is what
# `just dmg` used to do one failed attempt at a time.
#
# Go through the recipe rather than the script directly: `set dotenv-load` at
# the top of this file means a signing identity kept in .env is visible to the
# recipe and invisible to a bare shell, so the two would disagree about the
# signing configuration and the bare shell would manufacture a false finding.
dmg-preflight:
    scripts/dmg_preflight.sh

# Print the copyleft / non-commercial / unidentified license table for a
# staged payload (OSSPUB-05). `just dmg` writes the real THIRD-PARTY-LICENSES.txt
# into the payload; this recipe is the read-only view. PAYLOAD defaults to the
# payload `just dmg` stages. Needs `pnpm install` in apps/webui/frontend.
third-party-licenses-flags payload='apps/desktop/src-tauri/payload':
    uv run --no-sync python -m scripts.third_party_licenses --payload {{payload}} --flags

# Build the macOS .dmg for the Open DJ shell, then prove the artifact.
#
# Build and verification are one recipe on purpose: a dmg that was produced
# but never mounted is not evidence of anything. The verify half mounts the
# image read-only, asserts the .app carries the things a tester needs (a
# bundle, an executable inside it, and the expected bundle identifier),
# then detaches. Any missing piece exits non-zero.
#
# THE APP CARRIES ITS OWN ENGINE. scripts/build_engine_payload.py stages a
# relocatable CPython, the LOCKED dependency closure, the engine source and
# the built SPA into apps/desktop/src-tauri/payload, which tauri.conf.json
# copies to Contents/Resources/payload. The shell then starts that engine on
# an OS-assigned port at launch. There is no MDT_DESKTOP_ENGINE_ORIGIN any
# more: a build that boots its own engine has no address to bake, and a baked
# one would point one lane at the other lane's daemon.
#
# The payload build is INSIDE this recipe on purpose. A dmg assembled around
# a payload from three commits ago is exactly the stale artifact the build
# stamp exists to expose, and the cheapest way to never ship one is to make
# the two steps inseparable.
#
# LANE LABEL: MDT_LANE_LABEL is OPTIONAL again (OPS-08: the bake-off is
# over and the product is plain "Open DJ" / com.opendj.desktop). Unset
# builds the real product; a set label suffixes the bundle identifier and
# productName for a future bake-off lane, and the identifier is what macOS
# derives the app's Application Support directory from, so two labelled
# lanes never share a library. scripts/desktop_lane_config.py derives the
# overlay and refuses a label it cannot turn into a safe identifier.
#
# BUILD IDENTITY is stamped into BOTH halves, from one `git` read, and shown
# side by side in the UI. The shell gets OPENDJ_BUILD_* at compile time; the
# engine gets the same fields in the payload manifest. They can drift (a shell
# pointed at a dev engine is exactly that), which is why both are on screen.
#
# SIGNING is parameterized, never committed. This is the DEVELOPER ID path,
# for a dmg a tester downloads and double-clicks. It is not the Mac App Store
# path: scripts/ship_appstore.sh uses a Mac App Distribution certificate, a
# provisioning profile and sandbox entitlements off the SAME Team ID. Same
# account, different certificates, and they are not interchangeable.
#
#   MDT_MACOS_SIGNING_IDENTITY      -> exported as APPLE_SIGNING_IDENTITY,
#       which tauri-cli reads as the override for bundle.macOS.signingIdentity
#       (verified in tauri-cli 2.11.4 interface/rust.rs).
#   MDT_MACOS_NOTARY_KEYCHAIN_PROFILE -> a `xcrun notarytool store-credentials`
#       profile name. Tauri's bundler CANNOT use one: it only accepts
#       APPLE_API_* or APPLE_ID/APPLE_PASSWORD/APPLE_TEAM_ID (verified in
#       tauri-bundler 2.9.4), so this recipe drives notarytool and stapler
#       itself after bundling.
#
# THE TWO ARE ALL-OR-NOTHING. Either both are set and the artifact is signed,
# hardened, notarized and stapled, or neither is and the build is REFUSED --
# unless MDT_SHIP_UNSIGNED=1 is set explicitly, which is the one deliberate
# way to get an unsigned dev image and which prints a loud warning. There is
# no silent unsigned build: shipping unsigned has to be something someone
# chose, not something that happened because a variable was missing.
#
# Signing is spread across three points in this recipe rather than one,
# because the app carries a relocatable CPython under Contents/Resources and
# the bundler does not walk into a resource directory:
#
#   1. after the payload is staged, BEFORE cargo tauri build seals it, every
#      Mach-O in the payload is signed with the hardened runtime. Skipping
#      this is what makes the notary service return Invalid.
#   2. after the image exists, the .app inside it is checked for a Developer
#      ID authority, the hardened runtime and a secure timestamp -- the three
#      things the notary service reports slowly and confusingly.
#   3. the image itself is signed, then submitted, stapled and assessed.
#
# scripts/sign_macos_developer_id.sh holds all three; see its header.
#
# WHEN UNSIGNED, THE ARTIFACT IS NOT INSTALLABLE WITHOUT A WORKAROUND. macOS
# ad-hoc signs the binary at link time (codesign reports "adhoc,
# linker-signed", Sealed Resources=none), which is not a Developer ID
# signature: `spctl -a` rejects the bundle, and a tester who DOWNLOADS the
# image gets com.apple.quarantine on top of that, so Finder refuses to open
# it at all. ship_dmg.sh strips quarantine on install for exactly that case,
# and only that case. Do not describe an unsigned artifact as installable
# without saying so.
#
# ARM64 ONLY (v1 decision). An Intel Mac cannot run the artifact.
dmg lane='':
    #!/usr/bin/env bash
    set -euo pipefail
    # FAIL BEFORE THE BUILD, not at the EXIT trap ten minutes later. A build
    # that cannot be measured is not one this repo ships evidence for, and the
    # trap alone would let `just dmg` exit 0 having recorded nothing, which is
    # the opposite of the fail-closed acceptance condition OPS-29 states.
    _root="$(git rev-parse --show-toplevel)"
    _budget="$_root/ops/build-budget.env"
    if [ ! -r "$_budget" ]; then
        echo "[ERROR] $_budget unreadable: refusing to run an unmeasurable build" >&2
        exit 1
    fi
    _t0=$(date +%s)
    echo "[TIMING] recipe_start $_t0"
    # OPS-29. Armed HERE, not at recipe_end, because a build that dies at cargo
    # or at the bundler is exactly the one worth a timing row, and the old
    # placement recorded nothing unless the whole recipe succeeded.
    _mdt_recorded=0
    _record_build_time() {
        [ "$_mdt_recorded" = 0 ] || return 0   # one row per run, never two
        _mdt_recorded=1
        _rc="${1:-0}"
        _elapsed=$(( $(date +%s) - _t0 ))
        # The actual write (budget read, verdict, printf, append) lives in
        # ops/dmg-smoke/record_build_time.sh -- the ONE executable path this
        # trap and the dmg-smoke test harness's headless build shim both run,
        # so the two cannot drift apart (PR #4481 review). The script's own
        # location is resolved fresh via git-toplevel rather than through
        # `$_root`, because tests/scripts/test_build_budget.py's
        # `_run_recorder` re-executes this function body verbatim with
        # `_root` reassigned to a throwaway tmp_path (and `_budget` passed
        # separately) to exercise the real recorder without a full checkout
        # under tmp_path; `$_root` and `$_budget` are passed straight through
        # to the script exactly as this function received them. Exiting
        # inside an EXIT trap does not re-enter it, so the script's own
        # "unreadable budget + rc=0" failure is re-raised here rather than
        # swallowed. MDT_DMG_SMOKE_RUN_ID, when ops/dmg-smoke/run.sh set it
        # before invoking this build, stamps the row so that script can prove
        # a row belongs to THIS build rather than trusting log position alone
        # on the shared BUILD_WORKTREE log (review round 4, P1: "correlate
        # the timing row with this build").
        "$(git rev-parse --show-toplevel)/ops/dmg-smoke/record_build_time.sh" \
            "$_budget" "$_root" "$_elapsed" "$_rc" "${MDT_DMG_SMOKE_RUN_ID:-}" || exit 1
    }
    trap '_record_build_time $?' EXIT
    label="${MDT_LANE_LABEL:-}"
    lane_arg="{{lane}}"
    # Double intent required for ANY lane build: MDT_LANE_LABEL (env) and
    # lane= (explicit recipe arg) must both be set and agree. This closes
    # the class of bug where a stray/forgotten env var alone silently
    # produced a mislabelled artifact ("Open DJ (B)" shipped to Air, Sun 31
    # Aug 2026 postmortem). A lone env var, or a lone lane= arg, is refused
    # rather than guessed at.
    if [ "$label" != "$lane_arg" ]; then
        echo "[ERROR] lane build requires double intent: MDT_LANE_LABEL='$label' (env) and lane='$lane_arg' (recipe arg) must both be set to the SAME label. Run 'just dmg' with neither set for the plain product, or export MDT_LANE_LABEL=X and run 'just dmg X' together." >&2
        exit 1
    fi
    if [ -n "$label" ]; then
        echo "[WARN] LANE BUILD: $label"
    fi
    # Only the identity is read here now. The notary profile is validated by
    # the preflight and consumed directly from the environment by
    # scripts/sign_macos_developer_id.sh, so a local copy would be a second
    # name for one value.
    identity="${MDT_MACOS_SIGNING_IDENTITY:-}"
    # ONE PREFLIGHT, EVERY PRECONDITION, ONE PASS. The signing configuration,
    # the working tree, the rust and node toolchains and the built SPA used to
    # be four guards in a row here, so an operator with three problems learned
    # about them one failed run at a time. scripts/dmg_preflight.sh checks all
    # of them and reports EVERY failure before refusing, and it is read-only,
    # so `scripts/ship_dmg.sh --dry-run` and `just dmg-preflight` can run the
    # same checks without building anything (OPS-11).
    #
    # It runs BEFORE anything else here on purpose: an invalid configuration is
    # refusable on any machine, cargo installed or not, and the tests for these
    # refusals (tests/scripts/test_macos_signing.py and
    # tests/scripts/test_desktop_lane_config.py) rely on exactly that.
    scripts/dmg_preflight.sh
    # Said BEFORE the build rather than after it, so nobody discovers what
    # they built once they already have an artifact in hand.
    if [ -n "$identity" ]; then
        export APPLE_SIGNING_IDENTITY="$identity"
        echo "[INFO] signing identity: $identity"
    else
        echo '[WARN] ============================================================'
        echo '[WARN] MDT_SHIP_UNSIGNED=1: building a DELIBERATELY UNSIGNED image.'
        echo '[WARN] Gatekeeper refuses this on any Mac that downloads it. A'
        echo '[WARN] tester has to clear com.apple.quarantine by hand, and'
        echo '[WARN] ship_dmg.sh does it for them on install. Do not hand this'
        echo '[WARN] artifact to anyone without saying it is unsigned.'
        echo '[WARN] ============================================================'
    fi
    conf=apps/desktop/src-tauri/tauri.conf.json
    overlay=$(uv run --no-project python -m scripts.desktop_lane_config overlay --config "$conf" --label "$label")
    echo "[INFO] lane label: ${label:-(none, plain Open DJ)}"
    # THE ENGINE. Rebuilt every time, into the exact path tauri.conf.json
    # names, so the shell and the payload inside it always come from one tree.
    # The builder fails loudly on a missing or stale apps/webui/frontend/build
    # rather than rebuilding the SPA behind your back.
    payload=apps/desktop/src-tauri/payload
    rm -rf "$payload"
    uv run --no-sync python -m scripts.build_engine_payload --out "$payload" --label "$label"
    echo "[TIMING] payload_staged $(date +%s)"
    # STAGE 1 OF 3. The payload's Mach-O files are signed HERE, while it is
    # still a directory, because cargo tauri build is about to seal it inside
    # Contents/Resources and the bundler never walks in there. Doing this
    # afterwards would mean rebuilding the image.
    if [ -n "$identity" ]; then
        scripts/sign_macos_developer_id.sh payload "$payload"
        echo "[TIMING] payload_signed $(date +%s)"
    fi
    # ONE git read, stamped into BOTH halves. The values come from the
    # manifest the payload builder just wrote rather than from a second `git`
    # call, so the shell physically cannot disagree with the engine about the
    # commit they were built from.
    stamp() { uv run --no-project python -m scripts.desktop_lane_config stamp --manifest "$payload/manifest.json" --field "$1"; }
    # OPS-18: bake channel + evidence timestamp into the payload identity
    # (empty string when unset, never null) so the shell stamp and the engine
    # manifest cannot disagree. just release exports the two env vars.
    uv run --no-project python -m scripts.stable_evidence stamp-payload \
        --manifest "$payload/manifest.json" \
        --channel "${OPENDJ_RELEASE_CHANNEL:-}" \
        --evidence-at "${OPENDJ_EVIDENCE_WRITTEN_AT_UTC:-}"
    export OPENDJ_BUILD_GIT_SHA=$(stamp git_sha)
    export OPENDJ_BUILD_GIT_SHA_FULL=$(stamp git_sha_full)
    export OPENDJ_BUILD_GIT_BRANCH=$(stamp git_branch)
    export OPENDJ_BUILD_GIT_DIRTY=$(stamp git_dirty)
    export OPENDJ_BUILD_AT_UTC=$(stamp built_at_utc)
    export OPENDJ_BUILD_LANE_LABEL=$(stamp lane_label)
    export OPENDJ_BUILD_CHANNEL=$(stamp release_channel)
    export OPENDJ_BUILD_EVIDENCE_AT_UTC=$(stamp evidence_written_at_utc)
    echo "[INFO] build stamp: $OPENDJ_BUILD_GIT_SHA on $OPENDJ_BUILD_GIT_BRANCH at $OPENDJ_BUILD_AT_UTC (dirty=$OPENDJ_BUILD_GIT_DIRTY)"
    # Clear prior artifacts first, so the verification below can only ever
    # examine something THIS run produced. Without it a build that silently
    # emitted nothing would leave the previous dmg to be "verified" and
    # reported as fresh.
    # The markers go WITH the images. A surviving *.dmg.complete from an
    # earlier successful build would otherwise vouch for whatever this run
    # leaves behind under the same stable filename.
    rm -f apps/desktop/src-tauri/target/release/bundle/dmg/*.dmg
    rm -f apps/desktop/src-tauri/target/release/bundle/dmg/*.dmg.complete
    # THE APP DIRECTORY TOO, and for the same reason. A lane overlay renames
    # the .app, so a bundle left by a differently labelled build sits BESIDE
    # this run's and the `find ... -print -quit` below can return it. That
    # used to be survivable: only the SIGNED path read this directory, and it
    # failed its bundle-id check before shipping. The image is now built from
    # this app on BOTH paths (#1711), so a stale one would be packaged and
    # only caught after the image already existed.
    rm -rf apps/desktop/src-tauri/target/release/bundle/macos
    # The bundler runs `xattr -cr` on the .app before codesign. A Homebrew /
    # pip `xattr` (Python) shadows Apple's and has no -r, which killed a SIGNED
    # build 8 minutes in with "failed to run xattr" (Wed 2 Sep 2026). Apple's
    # tools go first for the bundle step only.
    # OPS-30. sccache for THIS cargo invocation only. Sourced, never exec'd:
    # a missing or corrupt cache unsets the wrapper and cargo proceeds,
    # rather than failing the dmg. The wrapper is an absolute path because
    # the subshell prefixes PATH with /usr/bin, which would hide Homebrew.
    . "$_root/scripts/dmg_sccache_env.sh"
    ( cd apps/desktop/src-tauri && PATH="/usr/bin:$PATH" cargo tauri build --config "$overlay" )
    if [ "${MDT_SCCACHE_ACTIVE:-0}" = 1 ] && [ -n "${MDT_SCCACHE_BIN:-}" ]; then
        echo "[TIMING] sccache_stats $(date +%s)"
        "$MDT_SCCACHE_BIN" --show-stats || true
    fi
    unset RUSTC_WRAPPER || true
    unset MDT_SCCACHE_ACTIVE || true
    unset MDT_SCCACHE_BIN || true
    unset CARGO_INCREMENTAL || true
    echo "[TIMING] cargo_built $(date +%s)"
    # THE IMAGE, NAMED WITHOUT A BUNDLED ONE. tauri.conf.json asks for the
    # `app` target only, because bundling a dmg means running tauri's
    # bundle_dmg.sh, which lays the window out by driving Finder over
    # AppleScript. Finder does not answer Apple events from a
    # non-interactive context and can time out. This headless recipe avoids
    # requiring Finder window layout. The name used to be read off the dmg
    # tauri produced; with that
    # gone the architecture is stated here instead. ARM64 ONLY is the v1
    # decision (see the recipe header) and scripts/dmg_preflight.sh refuses
    # any other host before this point, so this is the one value it can be.
    source_app=$(find "$PWD/apps/desktop/src-tauri/target/release/bundle/macos" -maxdepth 1 -type d -name '*.app' -print -quit)
    [ -n "$source_app" ] || { echo '[ERROR] no app bundle produced'; exit 1; }
    # INSTALL-19/20 (issues #2566, #2633): macOS 26 auto-normalizes a legacy
    # icon.icns with no Icon Composer catalog onto its own gray Dock plate.
    # Compile or reuse the verified committed Assets.car here, then RE-SEAL
    # the bundle below: `cargo tauri build` already signed it, and that seal
    # covers Contents/Resources and Info.plist, both of which the icon install
    # changes. Notarizing without the re-seal returns status Invalid. The same helper
    # scripts/dmg_preflight.sh runs in read-only mode decides compile vs reuse;
    # actool requires full Xcode, while Command Line Tools hosts may reuse only
    # a committed catalog whose sidecar matches the current source digest.
    uv run --no-project python -m scripts.icon_composer_asset --app "$source_app"
    echo "[TIMING] icon_composer_asset_installed $(date +%s)"
    # Re-seal the OUTER bundle only (#2635; dropped by a later merge and
    # re-added with a regression test on Mon 14 Sep 2026 after the Air's
    # notary submission d96ae9e7 at b2ff18aeb came back Invalid): nested code
    # keeps its own signatures, so no --deep on the sign; same hardened
    # runtime and timestamp tauri used, or the notary service rejects it.
    if [ -n "$identity" ]; then
        # --preserve-metadata=entitlements keeps the audio-input entitlement
        # tauri signed in (Entitlements.app.plist); a plain --force drops it,
        # and set recording then hears silence (SET-10).
        # Refresh copied payload bytes after every inner mutation, before the outer seal.
        manifest_uv="$(command -v uv)" ||
            { echo '[ERROR] uv unavailable for final payload metrics'; exit 1; }
        "$manifest_uv" run --locked --no-sync python -m scripts.payload_signed_manifest \
            --payload "$source_app/Contents/Resources/payload"
        codesign --force --timestamp --options runtime --preserve-metadata=entitlements --sign "$identity" "$source_app" ||
            { echo '[ERROR] re-signing the app after the icon install failed'; exit 1; }
        codesign --verify --deep --strict "$source_app" ||
            { echo '[ERROR] the re-sealed app does not verify; refusing to notarize it'; exit 1; }
        app_entitlements=$(codesign -d --entitlements - --xml "$source_app" 2>/dev/null || true)
        printf '%s' "$app_entitlements" | grep -q 'com.apple.security.device.audio-input' ||
            { echo '[ERROR] the re-sealed app lost the audio-input entitlement (Entitlements.app.plist); set recording would hear silence'; exit 1; }
        echo "[TIMING] app_resealed_after_icon $(date +%s)"
    fi
    dmg_dir=apps/desktop/src-tauri/target/release/bundle/dmg
    dmg_arch=aarch64
    name=$(uv run --no-project python -m scripts.desktop_lane_config dmg-name --config "$conf" --label "$label" --arch "$dmg_arch")
    dmg="$dmg_dir/$name"
    # STAGES 2 AND 3. The updater archive must carry a STAPLED app, not merely
    # an app whose ticket Gatekeeper can find while online. Tauri creates its
    # archive before a ticket exists, so replace it from the stapled bundle
    # before signing.
    # It must stay a gzip tarball: the updater plugin gunzips the download, and
    # a ZIP under the same name is refused with "invalid gzip header" (v0.1.2
    # to v0.1.4, Tue 15 Sep 2026). scripts/pack_updater_archive.sh proves that.
    if [ -n "$identity" ]; then
        echo "[TIMING] app_notarize_start $(date +%s)"
        scripts/sign_macos_developer_id.sh notarize-app "$source_app"
        echo "[TIMING] app_notarized $(date +%s)"
        archive=$(find "$PWD/apps/desktop/src-tauri/target/release/bundle/macos" -maxdepth 1 -type f -name '*.app.tar.gz' -print -quit)
        [ -n "$archive" ] || { echo '[ERROR] no updater archive produced'; exit 1; }
        rm -f "$archive" "$archive.sig"
        scripts/pack_updater_archive.sh "$source_app" "$archive"
        ( cd apps/desktop/src-tauri && cargo tauri signer sign "$archive" )
        [ -s "$archive.sig" ] || { echo '[ERROR] updater archive was not signed'; exit 1; }
    fi
    # THE IMAGE IS BUILT HERE, ON BOTH PATHS. `hdiutil create` needs no Finder
    # and no logged-in user, so it replaces tauri's dmg target outright. On
    # the SIGNED path it packs the stapled bundle; on the UNSIGNED path
    # (MDT_SHIP_UNSIGNED=1, so $identity is empty) it packs the plain .app,
    # which IS the deliverable -- nothing else would produce one now that the
    # dmg target is gone. It must stay outside the `if` above for that reason.
    mkdir -p "$dmg_dir"
    hdiutil create -volname "$(basename "$source_app" .app)" -srcfolder "$source_app" -ov -format UDZO "$dmg"
    if [ -n "$identity" ]; then
        scripts/sign_macos_developer_id.sh dmg "$dmg"
        echo "[TIMING] dmg_notarize_start $(date +%s)"
        notary_out=$(scripts/sign_macos_developer_id.sh notarize "$dmg")
        printf '%s\n' "$notary_out"
        echo "[TIMING] dmg_notarized $(date +%s)"
        submission_id=$(printf '%s\n' "$notary_out" | uv run --no-project python -m scripts.stable_evidence parse-notary-id)
        uv run --no-project python -m scripts.stable_evidence append-signing \
            --sha "$OPENDJ_BUILD_GIT_SHA_FULL" \
            --identity "$identity" \
            --notarization-submission-id "$submission_id" \
            --written-by "just dmg"
        echo "[OK] notarization submission id: $submission_id"
    fi
    mount=$(mktemp -d /tmp/opendj-dmg-verify.XXXXXX)
    archive_mount=''
    # Chained, not replaced: bash keeps ONE EXIT trap, so overwriting the
    # timing trap here would silently lose the row for every failure past
    # this point.
    trap '_rc=$?; hdiutil detach "$mount" >/dev/null 2>&1 || true; rmdir "$mount" 2>/dev/null || true; [ -z "$archive_mount" ] || rm -rf "$archive_mount"; _record_build_time $_rc' EXIT
    hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$mount" >/dev/null
    app=$(ls -d "$mount"/*.app | head -1)
    [ -n "$app" ] || { echo '[ERROR] dmg holds no .app'; exit 1; }
    if [ -n "$identity" ]; then
        xcrun stapler validate "$app" || { echo "[ERROR] mounted app lacks a valid staple"; exit 1; }
        archive_mount=$(mktemp -d /tmp/opendj-updater-verify.XXXXXX)
        tar -xzf "$archive" -C "$archive_mount"
        archive_app=$(find "$archive_mount" -maxdepth 1 -type d -name '*.app' -print -quit)
        [ -n "$archive_app" ] || { echo '[ERROR] updater archive holds no .app'; exit 1; }
        xcrun stapler validate "$archive_app" || { echo "[ERROR] updater app lacks a valid staple"; exit 1; }
    fi
    bin="$app/Contents/MacOS/opendj-desktop"
    [ -x "$bin" ] || { echo "[ERROR] missing executable $bin"; exit 1; }
    # THE POINT OF THE WHOLE TRAIN: the engine must be INSIDE the image. A
    # shell without its payload is the artifact this replaced -- a window onto
    # a machine that has the repo.
    shipped="$app/Contents/Resources/payload"
    launcher="$shipped/bin/opendj-engine"
    [ -x "$launcher" ] || { echo "[ERROR] no bundled engine at $launcher"; exit 1; }
    opendj_cli="$shipped/bin/opendj"
    [ -x "$opendj_cli" ] || { echo "[ERROR] no bundled opendj CLI at $opendj_cli"; exit 1; }
    [ -x "$shipped/runtime/bin/python3" ] || { echo '[ERROR] bundled engine has no interpreter'; exit 1; }
    [ -f "$shipped/app/apps/webui/frontend/build/index.html" ] || { echo '[ERROR] bundled engine has no built interface'; exit 1; }
    # OSSPUB-05: the mounted app must carry its third-party attribution.
    for licensed in THIRD-PARTY-LICENSES.txt NOTICE LICENSE; do
        [ -s "$shipped/$licensed" ] || { echo "[ERROR] bundled app lacks $licensed at $shipped/$licensed"; exit 1; }
    done
    stretch=$(ls "$shipped"/app/apps/webui/frontend/build/_app/immutable/assets/SignalsmithStretch.*.mjs 2>/dev/null | wc -l | tr -d ' ')
    [ "$stretch" = "1" ] || { echo "[ERROR] expected exactly 1 SignalsmithStretch asset in the payload, found $stretch"; exit 1; }
    shipped_sha=$(uv run --no-project python -m scripts.desktop_lane_config stamp --manifest "$shipped/manifest.json" --field git_sha)
    [ "$shipped_sha" = "$OPENDJ_BUILD_GIT_SHA" ] || { echo "[ERROR] payload says $shipped_sha, shell was stamped $OPENDJ_BUILD_GIT_SHA"; exit 1; }
    got_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")
    want_id=$(uv run --no-project python -c "import json,sys; from scripts.desktop_lane_config import lane_identifier, validate_label; c=json.load(open(sys.argv[1])); print(lane_identifier(c['identifier'], validate_label(sys.argv[2] or None)))" "$conf" "$label")
    [ "$got_id" = "$want_id" ] || { echo "[ERROR] bundle id $got_id, expected $want_id"; exit 1; }
    # Report the signature that is actually on the artifact, not the one the
    # environment asked for. A missing cert must not silently look signed.
    authority=$(codesign -dv --verbose=2 "$app" 2>&1 | grep '^Authority=' | head -1 | cut -d= -f2- || true)
    arch="${name%.dmg}"
    arch="${arch##*-}"
    echo "[OK] $(basename "$app") verified inside the image"
    echo "[OK] bundle id: $got_id"
    echo "[OK] bundled engine: $(du -sh "$shipped" | cut -f1) at Contents/Resources/payload"
    echo "[OK] build stamp: $shipped_sha on $OPENDJ_BUILD_GIT_BRANCH, dirty=$OPENDJ_BUILD_GIT_DIRTY, built $OPENDJ_BUILD_AT_UTC"
    echo "[OK] artifact: $(pwd)/$dmg"
    echo "[OK] size: $(du -h "$dmg" | cut -f1) ($(stat -f%z "$dmg") bytes)"
    echo "[OK] architecture: $arch only (Intel Macs cannot run this)"
    if [ -n "$authority" ]; then
        echo "[OK] signed by: $authority"
    else
        echo '[WARN] UNSIGNED (ad-hoc, linker-signed only) and un-notarized'
        echo '[WARN] a downloaded copy is Gatekeeper-blocked; the tester must run'
        echo "[WARN]   xattr -dr com.apple.quarantine \"/Applications/$(basename "$app")\""
    fi
    # THE COMPLETION MARKER, WRITTEN LAST AND ONLY HERE. Everything above can
    # fail after the image already exists on disk: signing, notarization and
    # every verification in this block run AFTER `cargo tauri build` has
    # produced the dmg. A half-built image left in a worktree is a normal
    # outcome of a failed run, and ship_dmg.sh now scans sibling worktrees
    # automatically, so "a dmg exists" can no longer be allowed to mean "a
    # build succeeded". This file is the positive proof that it did.
    # BOUND TO THESE BYTES, not merely present. Line 2 is the image's own
    # digest, so a marker left over from an earlier build cannot vouch for a
    # different image that happens to reuse the filename. Existence alone is
    # the weaker check that this pairing exists to avoid.
    printf '%s %s\n%s\n' "$OPENDJ_BUILD_GIT_SHA_FULL" "$OPENDJ_BUILD_AT_UTC" \
        "$(shasum -a 256 "$dmg" | awk '{print $1}')" > "$dmg.complete"
    echo "[OK] build completion marker: $dmg.complete"
    echo "[TIMING] recipe_end $(date +%s)"

# Build the Hyper-K launcher (apps/launcher, issue #2759) in release mode via
# Tauri's own bundler. UNSIGNED on purpose: tauri.conf.json leaves
# macOS.signingIdentity null for this app (see apps/launcher/README.md,
# "Notarization / code-signing"), so there is no secret to wire in -- a
# distinct app from the Open DJ shell `dmg` builds above, with its own
# pnpm-workspace.yaml and its own lockfile.
# CI's macos-packaging.yml launcher job runs this exact recipe so the local
# and CI build paths cannot drift (the whole point of issue #2759 LAUNCH-01:
# a recipe nobody runs is the same packaging gap in a new place).
launcher-build:
    cd apps/launcher && pnpm install --frozen-lockfile && pnpm tauri build

# Publish one signed, notarized desktop release and its Tauri update manifest.
# The publisher refuses dev unsigned builds, refuses a release with no semver
# bump over the latest public release, and never changes bytes under an existing
# version tag. It is intentionally an Air-only operation: the Apple identity
# and notary keychain profile live in that machine's keychain.
# OPS-18: `just release --channel stable` refuses unless evidence for this
# sha is complete and green; `--channel nightly` (the default) publishes
# without it. Args after `--` are forwarded the same way.
release *args:
    scripts/release.sh {{args}}

# Record a red-team pass on a sha's stable-evidence file (OPS-18). The
# live-bugs thread owns the runner; this is the writer it calls.
stable-evidence-red-team *args:
    python3 -m scripts.stable_evidence append-red-team {{args}}

# Agent parity for the semver gate inside just release. Runs on any OS with gh.
release-check-semver:
    uv run --no-sync python -m scripts.release_semver check --config apps/desktop/src-tauri/tauri.conf.json --repo maintainer/issue-assets

# ----- Preview-branch drift (DEVOPS-05) -----------------------------------

preview_worktree := env_var_or_default("OPENDJ_PREVIEW_WORKTREE", "")

# Audit the configured live review worktree: the head actually being served.
# Exit 0 OK, 1 DRIFT, 2 FOSSIL, 3 UNKNOWN. Set OPENDJ_PREVIEW_WORKTREE or use
# just preview_worktree=/some/path preview-drift. Empty configuration refuses.
# --fetch refreshes origin/main before comparing the live head, so a stopped
# watcher cannot make a stale tracking ref look current.
preview-drift *args:
    @uv run --no-project --quiet python -c "import sys; sys.exit(0 if sys.argv[1].strip() else (print('[UNKNOWN] Set OPENDJ_PREVIEW_WORKTREE or preview_worktree explicitly') or 3))" {{quote(preview_worktree)}}
    uv run --no-project --quiet python -m scripts.preview_drift_check \
        --worktree {{quote(preview_worktree)}} --fetch {{args}}

# Audit origin's preview ref from any clone. Runs hourly on nucbox as
# preview-refresh.timer. It CANNOT see the live head; use `just preview-drift`
# on the Air for that.
preview-drift-remote *args:
    uv run --no-project --quiet python -m scripts.preview_drift_check \
        --remote --fetch {{args}}

# Attempt ff-only sync of the live preview worktree; file queue:ready on refuse.
preview-sync-ff *args:
    @uv run --no-project --quiet python -c "import sys; sys.exit(0 if sys.argv[1].strip() else (print('[UNKNOWN] Set OPENDJ_PREVIEW_WORKTREE or preview_worktree explicitly') or 3))" {{quote(preview_worktree)}}
    uv run --no-project --quiet python -m scripts.preview_ff_sync \
        --worktree {{quote(preview_worktree)}} --fetch {{args}}

# Dump the OpenAPI schema from the ENGINE app -- the app this lane ships.
#
# Was the legacy server (`python -m apps.webui.server --dump-openapi`) until
# T5. The engine document is the legacy one plus the jobs endpoints, the
# fan-out progress ledger routes, and the engine health fields. Dumping the
# legacy app would omit the engine-only routes and health handshake fields.
#
# The data dir is a throwaway: create_app only ever creates what the chassis
# owns (state/, jobs.db), so a temp dir yields the same schema as the real
# library and leaves nothing behind. Schema-only, no library data is read.
openapi-dump out="apps/webui/openapi.json":
    uv run --no-sync python -c "{{engine_openapi_dump}}" {{out}}

# Harvest configured in-app feedback (FB-06): print every reachable daemon's
# review todos / pins / general note, append harvestable items to zTasks.md
# with provenance, archive them server-side (never deletes). Targets: local
# dev port(s), the local installed app, and configured remote installed apps.
# `just feedback-harvest --dry-run` prints without touching anything.
# Same `-- ` stripping as `pin-merged` below (#T8 P2): a leading `-- ` is
# tolerated but no longer required.
feedback-harvest *args:
    uv run --no-sync python -m scripts.feedback_harvest {{trim_start_match(args, "-- ")}}

# Turn comment pins green when the PR that fixed them merges (#858). The merge
# lane calls this ONCE with the merged PR number; it reads `pin <id> -> <sha>`
# lines and closing keywords (Fixes/Closes/Resolves #n) out of the PR body and
# PATCHes every pin they name to `merged`. No poller: the merge lane already
# holds the number, and re-running is a no-op.
# `just pin-merged 890 --dry-run` prints without patching.
# A leading `-- ` (e.g. `just pin-merged 890 -- --dry-run`) is stripped before
# forwarding: `just`'s *args passes it through literally, and argparse then
# reads everything after a literal `--` as a positional, not a flag, so
# `--dry-run` was rejected as an unrecognized argument. Both forms now work.
pin-merged pr *args:
    uv run --no-sync python -m scripts.pin_mark_merged {{pr}} {{trim_start_match(args, "-- ")}}

# T0 determinism check: two consecutive dumps must be byte-identical.
openapi-determinism:
    uv run --no-sync python -c "{{engine_openapi_dump}}" /tmp/oapi.a.json
    uv run --no-sync python -c "{{engine_openapi_dump}}" /tmp/oapi.b.json
    cmp /tmp/oapi.a.json /tmp/oapi.b.json
    echo '[OK] openapi dump deterministic'

# ----- Beat-mapping benchmark (lane agentB, proposed BEATMAP-01) ------------

# Round 0 of the beat-mapping benchmark: how closely does each candidate beat
# tracker agree with rekordbox's OWN beat grids?
#
# rekordbox is the ground truth here, not an independent notion of musical
# correctness. The library already has grids the user has mixed on and trusts,
# so the KPI is agreement with those, and every figure is a distance from what
# rekordbox already believes.
#
# Reads the lane daemon for BOTH the ANLZ grids and the audio. Audio must come
# through the daemon because raw rekordbox FolderPaths are frequently stale and
# the path healing lives behind that endpoint. Nothing here writes anywhere
# near rekordbox.
#
# Heavy analyzer deps NEVER enter the repo venv: each candidate is a standalone
# PEP 723 script in its own throwaway environment. The two exported variables
# are what makes aubio 0.4.9 build on a current arm64 toolchain (clang 16
# promoted a warning to an error, and aubio's ffmpeg input targets a removed
# ffmpeg 4 API, so the build is pointed away from pkg-config).
#
# Runtime is measured twice on purpose. The serial pass is one candidate at a
# time with one worker, which is the honest per-track cost. The full pass uses
# several workers and is only good for planning batch wall-clock. Running all
# five at once understated librosa by roughly 85x through CPU contention alone.
#
# Excerpt WAVs land in a gitignored scratch dir; only the report and the
# machine-readable JSON are committed.
beatbench-r0 base="http://127.0.0.1:8685" scratch=".tmp/beatbench" out="ops/beatbench/round-0":
    #!/usr/bin/env bash
    set -euo pipefail
    export CFLAGS="-Wno-incompatible-function-pointer-types -Wno-int-conversion"
    export PKG_CONFIG_LIBDIR="/nonexistent"
    S="{{scratch}}"; O="{{out}}"
    mkdir -p "$S/serial" "$S/full" "$S/wav" "$O"
    CANDS="aubio essentia librosa beat_this madmom_reference_only"

    echo "[beatbench] survey: naming the denominator over the whole library"
    uv run --no-project --script scripts/beatbench/survey.py \
        --base "{{base}}" --out "$S/survey.json"

    echo "[beatbench] fixtures: every dynamic grid, stratified sample of fixed"
    uv run --no-project --script scripts/beatbench/fixtures.py \
        --base "{{base}}" --survey "$S/survey.json" --out "$O/fixtures.json" \
        --wav-dir "$S/wav"

    echo "[beatbench] serial calibration (workers=1): honest per-track runtime"
    for n in $CANDS; do
        uv run --no-project --script "scripts/beatbench/run_$n.py" \
            --fixtures "$O/fixtures.json" --out "$S/serial/$n.json" \
            --limit 20 --workers 1
    done

    echo "[beatbench] full pass over every fixture"
    for n in $CANDS; do
        W=6; [ "$n" = "beat_this" ] && W=4
        uv run --no-project --script "scripts/beatbench/run_$n.py" \
            --fixtures "$O/fixtures.json" --out "$O/candidate-$n.json" --workers "$W"
    done

    echo "[beatbench] scoring with the versioned scorer"
    uv run --no-sync python -m scripts.beatbench.report \
        --round 0 \
        --fixtures "$O/fixtures.json" \
        --full $(for n in $CANDS; do printf '%s ' "$O/candidate-$n.json"; done) \
        --serial $(for n in $CANDS; do printf '%s ' "$S/serial/$n.json"; done) \
        --out-md "$O/report.md" --out-json "$O/results.json"
    echo "[OK] beatbench round 0 -> $O/report.md"

# Acceptance tests for the beat-mapping scorer (synthetic grids, no audio).
beatbench-test:
    uv run --no-sync python -m pytest tests/beatbench -q

# ----- Lane-agnostic analysis bench (NATIVE-11, apps/analysis_bench) --------

# What each lane is: its scorer, truth, experiment log and controls. Lanes
# without a scorer say so here rather than failing later.
bench-lanes:
    uv run --no-sync python -m apps.analysis_bench lanes

# The NATIVE-11 entry point, and the whole fresh-clone contract: pull the
# bundle by checksum if this clone does not hold it, run the candidate plus the
# lane's controls, score, and append the numbered round to the experiment log.
# `data/` is untracked, so a recipe that only opened the local bundle would
# fail on a fresh clone instead of fetching one.
bench lane candidate version="v1":
    ./scripts/bench_run.sh "{{ lane }}" "{{ candidate }}" "{{ version }}"

# The same run WITHOUT appending a round: for a quick look, or a rerun you do
# not want in the counter.
bench-dry lane candidate version="v1":
    ./scripts/bench_run.sh "{{ lane }}" "{{ candidate }}" "{{ version }}" --no-post

# The explicit name for what `just bench` already does.
bench-post lane candidate version="v1":
    ./scripts/bench_post.sh "{{ lane }}" "{{ candidate }}" "{{ version }}"

bench-test:
    uv run --no-sync python -m pytest tests/analysis_bench tests/beatbench -q

# ----- Fleet dispatch KPIs (OPS-16) -----------------------------------------
# Moved to the fleet-af repository on Thu 1 Oct 2026 (fleet_kpi/): there,
# `just kpi-rails kpi [HOURS]` and `just kpi-rails sla-buckets DAY`.

# ----- CI health watchdog ------------------------------------------------
# Deliberately runs OUTSIDE GitHub Actions: a billing refusal kills runs before any step
# and trigger drift means runs never start, so both are invisible to an in-CI monitor.
#
# Scheduled every 4 hours by a machine-local launchd agent (NOT committed):
#   ~/Library/LaunchAgents/com.YOU.mdt-ci-health.plist
# Install it once with:
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.YOU.mdt-ci-health.plist
# Alerts land in ~/Library/Logs/mdt-ci-health.log plus notification, email and Pushcut.

# Check CI health now. Exit 2 billing, 3 trigger drift, 4 failure rate, 5 staleness,
# 6 iteration-speed regression.
ci-health *args:
    uv run --no-project python -m scripts.ci_health_check {{args}}

# Cancel superseded queued trunk CI and bookkeeping runs (issue #2922).
trunk-tip-only *args:
    uv run --no-sync python -m scripts.ci_trunk_tip_only {{args}}

# Run the full notify path, alerting on every channel only if the check fails.
ci-health-notify:
    ./scripts/ci_health_notify.sh

# Force one labelled test alert down every channel to prove the alert path works.
ci-health-test-alert:
    ./scripts/ci_health_notify.sh --test

# Switching CI back to GitHub-hosted runners is a human-only action. The recipe and instructions are archived at commit 3d588609a938609fbbb10e9a92e515da423465af (`git show 3d588609a938609fbbb10e9a92e515da423465af:justfile`); agents must never set or delete the repository variables CI_RUNS_ON_LINUX, CI_RUNS_ON_E2E or CI_HOSTED_OS_JOBS.

# ----- Standing perf health check ----------------------------------------
# Reads every performance sink that exists on THIS machine (client-error JSONLs in both
# default locations, the iteration-metrics store, the ci-health watchdog, the diagnostics
# probe) and hands each finding a root-cause pointer. Read-only: it installs nothing,
# writes nothing, and calls no network.
#
# Stdlib-only and 3.9-compatible on purpose, so a scheduled copy can run it with no repo
# checkout and no venv. Scheduling guidance (a launchd plist example that is documented,
# NOT installed) and the escalation contract into docs/perf/QUEUE.md live in
# docs/perf/health-checks.md.

# Read every perf sink on this machine. Exit 1 only when a sink exists but is unreadable.
perf-health *args:
    uv run --no-project python scripts/perf_health_check.py {{args}}

# Nightly deck-load KPI capture + 10-minute live engine health probe (issue #1506).
perf-kpi-nightly *args:
    uv run --no-sync python -m scripts.perf.perf_kpi_job nightly {{args}}

perf-kpi-health *args:
    uv run --no-sync python -m scripts.perf.perf_kpi_job health {{args}}

# PERFMODE-06: reproducible macOS pressure run with real WebKit/CoreAudio KPIs.
# Read docs/perf/acid-test.md before invoking. The capture command must emit real
# deck_load_ms, xruns, and ui_latency_ms JSON for baseline, during, and after.
perf-squeeze *args:
    scripts/perf_squeeze.sh {{args}}

# Same checked-out harness on the Air. Arguments, capture command, and output path are
# evaluated remotely, so the real app and capture integration must exist on the Air.
perf-squeeze-air *args:
    source_sha="$(git rev-parse HEAD)" && test -z "$(git status --porcelain)" && ssh air "PERF_SQUEEZE_SOURCE_SHA=${source_sha} PERF_SQUEEZE_HARNESS_DIRTY=false bash -s --" {{args}} < scripts/perf_squeeze.sh

# Compare two finished squeeze reports for shared schema and required KPIs.
perf-squeeze-compare a b:
    uv run --no-project python -m scripts.perf.squeeze_report compare {{a}} {{b}}

# ----- Iteration-speed metrics -------------------------------------------
# scripts/iteration_metrics.sh wraps a recipe, times it, and appends one JSON line to the
# machine-local store at ~/.local/share/mdt-iteration-metrics/metrics.jsonl. `just ci-health`
# writes per-job CI durations into the same file and check 5 reads it back, so a slow gate
# here and a slow gate on a GitHub runner sit on one axis.
#
# WRAPPED ON THIS BRANCH: savepoint-gate, savepoint-smoke, quality, quality-py.
# NOT WRAPPED: `gate` and `dmg` (merged here from the af--dmg-installer lane) are
# multi-line / shebang recipes the one-line shim does not wrap; time them via
# `just iteration-time <step> <command>` until they are converted.

# Time an arbitrary command into the store: `just iteration-time cargo-debug cargo build`.
iteration-time step +command:
    ./scripts/iteration_metrics.sh {{step}} {{command}}

# Private timing baseline seeding is unavailable in this public copy.
iteration-metrics-seed *args:
    @uv run --no-project --quiet python -c "import sys; print('UNKNOWN: private timing baseline seeding is unavailable in this public copy', file=sys.stderr); sys.exit(3)"

# Per-step count, median and newest timing from the store.
iteration-metrics-report:
    @uv run --no-sync python -m scripts.iteration_metrics_report
