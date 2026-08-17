.PHONY: test cov reqs reqs-check fixture ci clean audit-cues audit-sync integration lint quality quality-baseline build-dist release-check rb-parity-check rb-parity-final

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
