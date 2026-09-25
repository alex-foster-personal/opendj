.PHONY: test cov reqs reqs-check fixture vocal-kpi-live-check ci clean audit-cues audit-sync integration lint quality quality-baseline build-dist release-check rb-parity-check rb-parity-final waveform-native-wheel waveform-native-verify waveform-native-release-check

VENV ?= .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
WAVEFORM_CONSUMER_VENV := dist/waveform-consumer
WAVEFORM_CONSUMER_PY := $(WAVEFORM_CONSUMER_VENV)/bin/python
FRONTEND_NODE ?= node

# PYTEST_JOBS feeds `-n`: `auto` lets xdist size itself to the box. Override for
# a serial run (`make test PYTEST_JOBS=0`), which is what you want when reading
# a traceback rather than a pass/fail, and what ci.yml's own pytest step does
# because `-n` measured SLOWER on a 4-vCPU runner (ops/test-latency/README.md).
# PYTEST_COLLECT_FLOOR is the guard that
# keeps `-n` honest: parallelism redistributes tests, it never removes them, so
# collecting fewer than the floor means the suite lost tests. It only ratchets
# up. Keep both in sync with scripts/savepoint_gate.py and .github/workflows/ci.yml.
PYTEST_JOBS ?= auto
PYTEST_COLLECT_FLOOR ?= 3700

# PYTEST_BASETEMP is unset for a normal dev-machine run (pytest falls back to
# its own default under the system temp dir). CI workflows pass it set to a
# path under $RUNNER_TEMP, which GitHub wipes per job, so a shard killed by
# its wall budget leaves no lock file behind to outlive the job (see
# docs/decisions for the ADR). The flag is a single directory under
# PYTEST_BASETEMP (hyphenated with $@), not a nested path: pytest creates the
# basetemp with parents=False, so the parent must already exist. $@ still
# namespaces per target, so a composite target (release-check: test +
# waveform-native-verify) never has the second pytest invocation wipe the
# first one's basetemp out from under it.
pytest_basetemp_flag = $(if $(PYTEST_BASETEMP),--basetemp=$(PYTEST_BASETEMP)-$@,)

test:
	$(PYTEST) -q -n $(PYTEST_JOBS) --dist loadgroup --collect-floor $(PYTEST_COLLECT_FLOOR) $(pytest_basetemp_flag)

# Fast iteration gates for the Rekordbox parity stack. These deliberately omit
# untouched analysis backends and Pioneer actuator suites, which require
# optional madmom, Quartz, or cliclick dependencies. Keep `release-check` as
# the repository-wide release gate and run the dedicated performance
# Playwright suite once real DB fixtures are available (tracked in #155).
rb-parity-check:
	@echo "[rb-parity-check] focused Python, frontend unit, and type gates"
	$(PYTEST) -q -m rb_parity $(pytest_basetemp_flag)
	cd apps/webui/frontend && $(FRONTEND_NODE) --experimental-strip-types --test --test-reporter=tap --test-concurrency=4 tests/unit/*.test.mjs
	cd apps/webui/frontend && pnpm check

rb-parity-final: rb-parity-check
	@echo "[rb-parity-final] production frontend build"
	cd apps/webui/frontend && pnpm build

cov:
	$(PYTEST) --cov=apps --cov-report=term-missing --cov-report=html $(pytest_basetemp_flag)

reqs:
	$(PY) -m scripts.build_reqs_json

reqs-check:
	$(PY) -m scripts.build_reqs_json --check

fixture:
	$(PY) -m scripts.make_rb_fixture --force

# Explicit machine-local acceptance gate. This is intentionally not a
# dependency of any portable release gate: portable tests use the committed
# sanitized fixture in tests/fixtures/vocal-kpi/, while this command validates
# and prints the current real cache.
VOCAL_CACHE_DIR ?= data/state/vocal-cache
vocal-kpi-live-check:
	uv run scripts/bench/kpi_derive.py --cache-dir "$(VOCAL_CACHE_DIR)" --window latest --json

# CI composite: canonical ordering = reqs-check first (fast fail on drift),
# then tests + coverage.
ci: reqs-check cov

# ----- Phase 4 audit + integration targets -------------------------------

audit-cues:
	$(PY) -m apps.audit.cue_comparison

audit-sync:
	$(PY) -m apps.audit.sync_diff

integration:
	$(PYTEST) -m integration -q $(pytest_basetemp_flag)

clean:
	rm -rf .pytest_cache htmlcov .coverage coverage-matrix.md dist build *.egg-info

# ----- Pre-release checks -----------------------------------------------
# `release-check` is the single command CI and humans run before cutting a
# release tag. It runs the standard gates in sequence:
#   1. test        -- full pytest suite
#   2. lint        -- ruff check across apps/tests/scripts
#   3. build-dist  -- python -m build (wheel + sdist)
#   4. reqs-check  -- verify reqs.json is fresh vs REQUIREMENTS.md
#   5. prior-tag   -- best-effort `gh release view v1.0.1` sanity check (non-fatal)
# Keep this target serial; failures should halt the pipeline immediately.

lint:
	@# Runs the ruff pinned in ops/quality/requirements.txt against the rule
	@# set pinned in pyproject.toml's [tool.ruff]. Both pins matter: before
	@# they existed this target ran whatever ruff happened to be on PATH with
	@# whatever that version's built-in defaults were, so "the tree is clean"
	@# meant something different on every machine.
	@#
	@# This target reports the ABSOLUTE violation count, which is currently
	@# large. The enforced gate is `make quality`, which fails only when a
	@# count grows. Use this one to see what to fix next.
	@#
	@# LINT_PATHS defaults to the top-level source trees. To replicate the
	@# pre-commit experience (lint only files changed vs master) set
	@# LINT_PATHS to the output of `git diff --name-only master... -- '*.py'`
	@# when calling `make lint` / `make release-check`.
	@paths="$(LINT_PATHS)"; \
	if [ -z "$$paths" ]; then paths="apps tests scripts"; fi; \
	uv run --no-project --quiet --with-requirements ops/quality/requirements.txt \
		ruff check $$paths

# ----- Code quality ratchet ----------------------------------------------
# `quality` scores lint debt, complexity, architecture contracts, frontend
# coupling, dead code, dependency defects, file bloat and duplication, then
# fails if any of them got worse than ops/quality/baseline.json allows.
# See ops/quality/README.md.

quality:
	uv run --no-project --quiet python -m scripts.quality_gate --report ops/quality/report.md

# Re-record the baseline after a cleanup. Allowances only ever shrink; this
# refuses to run on a partial (--only) measurement.
quality-baseline:
	uv run --no-project --quiet python -m scripts.quality_gate \
		--update-baseline --report ops/quality/report.md

build-dist:
	rm -rf dist build
	$(PY) -m build

# The native extension is part of the normal server distribution. Keep this
# target wheel-based so the gate tests what consumers install, not a source-tree
# maturin development shim.
waveform-native-wheel:
	rm -f dist/music_dj_tools-*.whl
	mkdir -p dist
	uv build --wheel --python $(PY) --out-dir dist

waveform-native-verify:
	$(PY) scripts/check_waveform_native_wheel.py dist/music_dj_tools-*.whl
	rm -rf $(WAVEFORM_CONSUMER_VENV)
	uv venv --python $(PY) $(WAVEFORM_CONSUMER_VENV)
	uv pip install --python $(WAVEFORM_CONSUMER_PY) dist/music_dj_tools-*.whl
	FIXTURE_RB_USB_EXPORT=$$($(PY) -c "from tests.fixtures._resolver import fixture_path; print(fixture_path('rb-usb-export'))") && \
	cd $(WAVEFORM_CONSUMER_VENV) && \
		MDT_WAVEFORM_BACKEND=native MDT_REQUIRE_WAVEFORM_NATIVE=1 \
		$(CURDIR)/$(WAVEFORM_CONSUMER_PY) \
			$(CURDIR)/scripts/check_waveform_installed_consumer.py \
			--source-root $(CURDIR) \
			--fixture "$$FIXTURE_RB_USB_EXPORT/PIONEER/USBANLZ/P000/00029138" \
			--manifest $(CURDIR)/tests/fixtures/rb-usb-export.manifest.json
	uv pip install --python $(PY) --reinstall --no-deps dist/music_dj_tools-*.whl
	MDT_WAVEFORM_BACKEND=native MDT_REQUIRE_WAVEFORM_NATIVE=1 \
		$(PYTEST) -q tests/webui/test_waveform_native.py $(pytest_basetemp_flag) \
			-k 'native_request_selects or collision_resistant or canonical_fixture or release_acceptance or native_exactly or native_matches_empty or production_dispatch or dispatch_reports or native_rejects'

waveform-native-release-check: waveform-native-wheel waveform-native-verify

release-check: test lint build-dist waveform-native-verify reqs-check
	@echo "[release-check] verifying prior release tag (non-fatal)..."
	@gh release view v1.0.1 >/dev/null 2>&1 \
		&& echo "[release-check] prior release v1.0.1 found." \
		|| echo "[release-check] note: prior release v1.0.1 not visible (skipped)."
	@echo "[release-check] OK"

# ----- Phase 9: Spotify importer (CAT-01) --------------------------------
# Wrap with doppler so SPOTIFY_CLIENT_ID flows in without being committed.
# Dry-run is the default inside the CLI; add --live + --i-understand-the-risks
# and type the confirmation prompt to flip to a live write.
spotify-import:
	doppler run -p construct -c dev_af -- $(PY) -m apps.spotify import $(URL)

spotify-rematch:
	doppler run -p construct -c dev_af -- $(PY) -m apps.spotify rematch \
		--playlist-id $(PLAYLIST_ID)

spotify-watched:
	$(PY) -m apps.spotify watched --ensure

# Local spotDL bulk download for watched playlists. Runner lives in odj-private
# (gitignored shim: scripts/spotdl_watched.py; pointer: scripts/spotdl_watched.md).
spotdl-watched:
	@if [ ! -f scripts/spotdl_watched.py ]; then \
		echo "missing scripts/spotdl_watched.py -- see scripts/spotdl_watched.md"; exit 2; \
	fi
	$(PY) scripts/spotdl_watched.py $(SPOTDL_WATCHED_ARGS)

# ----- Phase 11: cloud sync + web UI (CAT-04, CAT-05) --------------------
.PHONY: spotify-import spotify-rematch spotify-watched spotdl-watched webui.dev webui.prod webui.openapi cloud.replicate cloud.self-check

webui.dev:
	just webui-backend

webui.prod:
	doppler run -p general -c dev_personal -- $(PY) -m apps.webui.server --prod

webui.openapi:
	# The engine dump in the justfile is the ONLY writer of openapi.json. The old
	# apps.webui.server dump produced a 210-path contract (no /account, /flags,
	# /setup, /jobs); PR #2326 committed it and every pytest shard on main failed
	# at collection (test_flag_capture_parity) until #2346 restored the file.
	just openapi-dump

cloud.replicate:
	doppler run -p general -c dev_personal -- $(PY) -m apps.cloud.replicate

cloud.self-check:
	$(PY) -m apps.cloud.replicate --self-check
