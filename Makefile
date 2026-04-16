.PHONY: test cov reqs reqs-check fixture ci clean

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

clean:
	rm -rf .pytest_cache htmlcov .coverage coverage-matrix.md
