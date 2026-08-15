.PHONY: test cov reqs reqs-check fixture ci clean audit-cues audit-sync integration lint build-dist release-check rb-parity-check rb-parity-final

VENV ?= .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
FRONTEND_NODE ?= node
RB_PARITY_PYTEST_PATHS := \
	tests/reconcile/test_prefix_dead_playlists.py \
	tests/shared/test_rekordbox_db.py \
	tests/test_codex_followups_c.py \
	tests/test_progress.py \
	tests/test_rb_assets.py \
	tests/webui

test:
	$(PYTEST) -q

# Fast iteration gates for the Rekordbox parity stack. These deliberately omit
# untouched analysis backends and Pioneer actuator suites, which require
# optional madmom, Quartz, or cliclick dependencies. Keep `release-check` as
# the repository-wide release gate and run the dedicated performance
# Playwright suite once real DB fixtures are available (tracked in #155).
rb-parity-check:
	@echo "[rb-parity-check] focused Python, frontend unit, and type gates"
	$(PYTEST) -q $(RB_PARITY_PYTEST_PATHS)
	cd apps/webui/frontend && $(FRONTEND_NODE) --test --test-concurrency=1 tests/unit/*.test.mjs
	cd apps/webui/frontend && pnpm check

rb-parity-final: rb-parity-check
	@echo "[rb-parity-final] production frontend build"
	cd apps/webui/frontend && pnpm build

cov:
	$(PYTEST) --cov=apps --cov-report=term-missing --cov-report=html

reqs:
	$(PY) -m scripts.build_reqs_json

reqs-check:
	$(PY) -m scripts.build_reqs_json --check

fixture:
	$(PY) -m scripts.make_rb_fixture --force

# CI composite: canonical ordering = reqs-check first (fast fail on drift),
# then tests + coverage.
ci: reqs-check cov

# ----- Phase 4 audit + integration targets -------------------------------

audit-cues:
	$(PY) -m apps.audit.cue_comparison

audit-sync:
	$(PY) -m apps.audit.sync_diff

integration:
	$(PYTEST) -m integration -q

clean:
	rm -rf .pytest_cache htmlcov .coverage coverage-matrix.md dist build *.egg-info

# ----- Pre-release checks -----------------------------------------------
# `release-check` is the single command CI and humans run before cutting a
# release tag. It runs the standard gates in sequence:
#   1. test        — full pytest suite
#   2. lint        — ruff check across apps/tests/scripts
#   3. build-dist  — python -m build (wheel + sdist)
#   4. reqs-check  — verify reqs.json is fresh vs REQUIREMENTS.md
#   5. prior-tag   — best-effort `gh release view v1.0.1` sanity check (non-fatal)
# Keep this target serial; failures should halt the pipeline immediately.

lint:
	@# Prefer the venv-local ruff (CI installs it there); fall back to a
	@# system-wide `ruff` on PATH for local dev machines that manage linters
	@# outside the project venv.
	@#
	@# LINT_PATHS defaults to the top-level source trees. To replicate the
	@# pre-commit experience (lint only files changed vs master) set
	@# LINT_PATHS to the output of `git diff --name-only master... -- '*.py'`
	@# when calling `make lint` / `make release-check`.
	@ruff_bin="ruff"; \
	if [ -x "$(VENV)/bin/ruff" ]; then ruff_bin="$(VENV)/bin/ruff"; fi; \
	paths="$(LINT_PATHS)"; \
	if [ -z "$$paths" ]; then paths="apps tests scripts"; fi; \
	echo "$$ruff_bin check $$paths"; \
	$$ruff_bin check $$paths

build-dist:
	rm -rf dist build
	$(PY) -m build

release-check: test lint build-dist reqs-check
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

# ----- Phase 11: cloud sync + web UI (CAT-04, CAT-05) --------------------
.PHONY: webui.dev webui.prod webui.openapi cloud.replicate cloud.self-check

webui.dev:
	just webui-backend

webui.prod:
	doppler run -p music-dj-tools -c prod -- $(PY) -m apps.webui.server --prod

webui.openapi:
	$(PY) -m apps.webui.server --dump-openapi apps/webui/openapi.json

cloud.replicate:
	doppler run -p music-dj-tools -c prod -- $(PY) -m apps.cloud.replicate

cloud.self-check:
	$(PY) -m apps.cloud.replicate --self-check
