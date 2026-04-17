.PHONY: test cov reqs reqs-check fixture ci clean audit-cues audit-sync integration

VENV ?= .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest

test:
	$(PYTEST) -q

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
	rm -rf .pytest_cache htmlcov .coverage coverage-matrix.md

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
	$(VENV)/bin/uvicorn apps.webui.server.app:app --reload --port 8585

webui.prod:
	doppler run -p music-dj-tools -c prod -- $(VENV)/bin/uvicorn \
		apps.webui.server.app:app --host $${MUSIC_DJ_BIND_HOST:-127.0.0.1} --port 8585

webui.openapi:
	$(PY) -m apps.webui.server --dump-openapi apps/webui/openapi.json

cloud.replicate:
	doppler run -p music-dj-tools -c prod -- $(PY) -m apps.cloud.replicate

cloud.self-check:
	$(PY) -m apps.cloud.replicate --self-check
