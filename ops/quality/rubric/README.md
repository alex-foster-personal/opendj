# Scored code-quality rubric (issue #389)

This directory holds a **versioned, evidence-anchored rubric** that scores four
surfaces of the repo independently. It is an **instrument**, not a ratchet: low
scores exit 0. It does not affect `ops/quality/baseline.json` or `just quality`.

Seed defects that motivated the rubric are tracked in issue #388 (pre-publish
review of `open-dj/`). The vendored fixture at
`tests/quality/fixtures/open-dj-a4adf880` pins those cases at commit `a4adf880`.

## Surfaces

| id | root | kind |
| --- | --- | --- |
| `spec` | `open-dj` | interchange spec |
| `python_cli` | `apps/open_dj` | Python CLI / library |
| `vendor_adapter` | `apps/adapters` | reverse-engineered vendor adapters |
| `svelte_ui` | `apps/webui/frontend` | Svelte UI |

## Run

```bash
uv run --no-sync python -m scripts.quality_rubric validate-rubric
uv run --no-sync python -m scripts.quality_rubric score --surface spec --json
uv run --no-sync python -m scripts.quality_rubric score --all --json
just quality-rubric score --all --json
```

Recorded baselines live in `baselines/v1.json`. Regenerate with:

```bash
uv run --no-sync python -m scripts.quality_rubric score --all --json --out ops/quality/rubric/baselines/v1.json
```

## Versioning

- Rubric version `1` is `v1.yaml` (`version: 1` inside the file).
- Adding a dimension requires role-model evidence and a probe in the same change.
- JSON Schema at `schema.json` enforces evidence on every dimension.
- A later version is `v2.yaml`; do not silently change v1 meaning.

## Inter-rater rule

v1 has no free-form judgment. Two agents scoring the same surface both run:

```bash
uv run --no-sync python -m scripts.quality_rubric score --surface <id> --json
```

and compare per-dimension `score` fields. They must match exactly (delta 0, which
is within one point). If they do not, the probe is buggy, not the agents. A later
judgment dimension must add a probe that counts binary observations; disagreement
on that checklist means the item is underspecified and the dimension does not ship.

## Role-model evidence

Every dimension cites a role-model repo and the artefact that earns full marks.
The shortlist source is `afmac/code-quality-role-models/README.md`; evidence is
copied into `v1.yaml` so this repo is self-contained.
