# open-dj schema (v0.2)

JSON Schema draft 2020-12 for the open-dj v0.2 spec.

## Files

- `open-dj.schema.json` -- root schema covering Library / Track / CuePoint /
  BeatGrid / Playlist / PlayOrder / Pairing / Session / Transition /
  ProvenanceValue via `$defs`.

## `$id` note

Each schema declares a provisional `$id` under `https://open-dj.org/schema/0.2/`.
**The domain is not yet registered.** Phase 16 (OPEN-03 publish step) will
reassign `$id`s if the final hosting URL differs.

`schema_version` inside documents is independent of the `$id` URL, so a domain
switch does not break existing corpus files.

## Validation

From the repo root:

```bash
python -m apps.open_dj.cli validate <file>
```

or programmatically:

```python
from apps.open_dj.validate import validate_library
from apps.open_dj.schema_loader import load_schema

schema = load_schema("v0.2")
errors = validate_library(doc, schema=schema)
```
