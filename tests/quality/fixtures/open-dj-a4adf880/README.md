# open-dj

Vendor-neutral, provenance-aware metadata format for DJ libraries.

## Layout

- [`spec/v0.2/open-dj.md`](spec/v0.2/open-dj.md) -- normative specification (CC-BY-4.0).
- [`schema/v0.2/open-dj.schema.json`](schema/v0.2/open-dj.schema.json) -- JSON Schema draft 2020-12 for the spec.
- [`conformance/`](conformance/) -- seed corpus used by round-trip tests.
- [`CHANGELOG.md`](CHANGELOG.md) -- history of spec bumps.
- [`LICENSE-SPEC.md`](LICENSE-SPEC.md) -- CC-BY-4.0 licence that covers the spec prose and schemas.

## Reference implementation

Lives at [`apps/open_dj/`](../apps/open_dj/). Entry points:

- `python -m apps.open_dj.cli validate <file>` -- validate an open-dj JSON doc.
- `python -m apps.open_dj.cli canon <file>` -- rewrite to canonical form (RFC 8785 JCS).
- `python -m apps.open_dj.cli diff <a> <b>` -- structural diff on `track_id` + provenance.

Reference code is Apache-2.0 (see repo-root [`LICENSE`](../LICENSE)).

## Status

- **v0.2 (2026-04-17)** -- first in-repo cut, Rekordbox + djay Pro adapters shipped.
- **v0.3** -- adds Serato + Traktor adapters (Phase 16).

Submit issues via the repo; the spec is expected to break prior to 1.0.
