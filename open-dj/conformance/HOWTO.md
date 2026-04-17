# Running and extending the open-dj conformance corpus

Phase 16 (OPEN-03c) ships a seed corpus under
[`tests/fixtures/conformance/`](../../tests/fixtures/conformance/). Each
fixture is a self-contained triple:

- `expected.opendj.json` -- hand-authored canonical document.
- `capabilities.yaml` -- per-adapter drop mask (which fields the adapter is
  legitimately allowed to lose on round-trip).
- optional vendor-DB snapshot under `fixtures/` (empty in v0; adapters
  synthesise from the expected document).

## Running the suite

```
pytest -m conformance
```

A subset across adapters:

```
pytest tests/test_conformance.py -k "serato"
pytest tests/test_conformance.py -k "traktor"
```

Unimplemented adapters (rekordbox, djay in Phase 16 execution; potentially
others) emit `skip` at collection time rather than `fail`. A green CI run
with skips is expected while adapter phases are still in flight.

## Fixture format

Each `capabilities.yaml` looks like:

```yaml
source: manual                 # or "generated" for auto-built fixtures
applies:
  serato:
    drops: ["rating", "bpm"]   # fields Serato is allowed to lose
  traktor:
    drops: ["bpm"]
  rekordbox:
    drops: ["bpm"]
  djay:
    drops: ["bpm"]
```

The set of supported drop paths lives in
[`tests/test_conformance.py`](../../tests/test_conformance.py) `_mask()` --
currently `rating`, `cue_points.color`, `cue_points.memory`, `bpm`,
`duration_ms`, `album`, `play_count`, `color_rgb`, `cues`, and identity
masks for `track_id` and `file_path`.

## Adding a fixture

1. Pick the next unused number (`04-<slug>`).
2. Create `expected.opendj.json` -- must validate against
   `open-dj/schema/v0.2/open-dj.schema.json`.
3. Create `capabilities.yaml` with a `drops` list per adapter.
4. Run `pytest -m conformance` and adjust the mask until it passes for every
   adapter you expect to support.

## Adding an adapter

1. Land `apps/adapters/<name>/` implementing the `Adapter` Protocol in
   [`apps/adapters/_shim/__init__.py`](../../apps/adapters/_shim/__init__.py)
   (to be moved to `apps/open_dj/` once Phase 15 ships its typed
   dataclass layer).
2. Register the adapter in
   [`tests/test_conformance.py`](../../tests/test_conformance.py)
   `_load_adapter` with a short name.
3. Add a per-adapter entry to the `applies:` section of every fixture
   `capabilities.yaml` describing which fields it legitimately drops.
4. Re-run `pytest -m conformance`; a passing run proves round-trip.
