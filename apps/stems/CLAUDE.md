# apps/stems -- agent notes

CLI: `python -m apps.stems` (see module docstring / `cli.py`). Bundles land in
`data/state/stems/<stable_id>/` for webui mute/solo. Torch stays out of the
repo venv (`uv run scripts/stem_bundle_worker.py`).

## Docs agents should open first

| Doc | Why |
| --- | --- |
| [ops/stem-farm/README.md](../../ops/stem-farm/README.md) | Farm ops, GCE policy, playlist scope |
| [ops/stem-farm/DATASETS.md](../../ops/stem-farm/DATASETS.md) | Public MSS corpora MOC (sizes, licenses, limitations) |
| [.planning/stems-handoff/HANDOFF.md](../../.planning/stems-handoff/HANDOFF.md) | Eval ladder, MUSDB status, bifrost2 paths |

## Large corpora (~20 GB+) -- do not predownload / do not git-lfs wholesale

Canonical public HQ eval set: **MUSDB18-HQ** (~22.7 GB).

- Public entry: https://zenodo.org/records/3338373 (access request; academic / NC framing)
- Overview: https://sigsep.github.io/datasets/musdb.html
- Our copy (do not pull to Mac by default): bifrost2
  `D:/asset-store/datasets/musdb18hq.zip` (22,656,664,047 bytes; zip CD intact, not fully CRC-verified)

Prefer a **git-tracked MOC + progressive disclosure** (metadata / small clips /
named splits) over git-lfs for the full zip. Score on bifrost2 or Modal; leave
the zip where it is unless a task explicitly needs a local extract.

Agent rule of thumb: read DATASETS.md + handoff first; download only the
smallest subset that answers the question.
