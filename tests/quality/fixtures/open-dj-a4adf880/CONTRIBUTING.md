# Contributing to open-dj

open-dj is pre-1.0 and actively evolving. Contributions are welcome;
everything here is governed by the repo-level `LICENSE` (Apache-2.0 for
code) and `open-dj/LICENSE-SPEC.md` (CC-BY-4.0 for the spec prose).

## Proposing a spec change

The normative spec lives at
[`spec/v0.2/open-dj.md`](spec/v0.2/open-dj.md) with the JSON Schema at
[`schema/v0.2/open-dj.schema.json`](schema/v0.2/open-dj.schema.json). To
change either:

1. Open an issue describing the problem and the proposed change.
2. For additive changes (new optional field, new `x_*` extension): bump
   the spec minor version in a dedicated PR and ship a migration note in
   `CHANGELOG.md`.
3. For breaking changes (field rename, removed field): we accept breaking
   changes freely before 1.0, but every breaking change needs an updated
   migration note + a conformance-corpus update.

## Extension namespace

Any `x_<adapter>_<field>` key is reserved for adapter-specific data that
cannot be modelled losslessly in the core schema. Rules:

- Keys MUST match `^x_[a-z0-9][a-z0-9_]*$`.
- Keys SHOULD carry the adapter prefix so two adapters cannot collide on
  the same name.
- Values MUST be JSON-serialisable (no bytes, no binary blobs; base64 as
  string is fine).

## Adding a vendor adapter

See [`conformance/HOWTO.md`](conformance/HOWTO.md) for the adapter + fixture
protocol. In short:

1. Implement the `Adapter` Protocol under `apps/adapters/<name>/`.
2. Ship a `capabilities()` descriptor.
3. Add a snapshot test at `tests/adapters/<name>/test_capabilities.py`.
4. Wire the adapter into the conformance harness + add entries to every
   fixture's `capabilities.yaml`.
5. Land a docs page under `open-dj/adapters/<name>.md` summarising the
   field table and any safety rails.

## Licensing gotchas

- **Apache-2.0** for reference code (repo-root `LICENSE`).
- **CC-BY-4.0** for spec prose + schema (`LICENSE-SPEC.md`).
- **MPL-2 carve-out** is permitted for a single adapter sub-tree when
  derived from an MPL-2 reference (e.g. Serato triseratops) -- file-level
  headers required, and the carve-out may not leak into other adapters.
- **GPL / AGPL** code is not accepted in-tree (see ROADMAP C9).

Run the Phase 16 suite before opening a PR:

```
pytest tests/adapters/ tests/test_conformance.py
```
