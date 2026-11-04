# Vendored fleet-af runner inventory (schema v1)

Committed snapshot of fleet-af `inventory/runners.yaml`. This repository is the
consumer; fleet-af ADR-0001 defines the producer/consumer boundary.

## Pin

| Field | Value |
| --- | --- |
| Tag | `inventory-runners-v1.0.0` |
| Merge commit | `946dbfd49d65c62e9a723ae0170fd85ed7a824a9` (fleet-af#15) |
| sha256 | `7a4c577252b09cbf8447d6bec4d5985ffb437e720903d3b1aa1932d2426df337` |
| git blob | `ec470200b9` (full `ec470200b96227961a13dc3370a3f162c3292b78`) |
| Schema | v1, documented in fleet-af `ci--hosts/scripts/runners_inventory.py` |

## Consumers (read `runners.v1.yaml`, never a copy of it)

- `tests/scripts/test_vendored_runners_pin.py` — sha256 pin and coverage-gap claims.
- `tests/scripts/vendored_runners.py` — loader used by the pool tests.
- `(private path)` — `mq` pool (fully covered by v1).
- `tests/scripts/test_ci_main_fix_runner_reserve.py` — `main-fix` routing labels (v1 seats partial).
- `tests/scripts/runner_canary_workflow_checks.py` — vendor labels must not collide with vendored pool labels.

## Coverage gap (never silent)

v1 is the 46 Ansible runners on agentbox, agbox2, and agbox3. **Outside v1:**
nucbox-wsl x30, bifrost1 x9, megamac-vm x4, and the retired agentbox. Tests naming
those hosts keep their current source until a v2 inventory covers them.

## Refresh

Extract a new fleet-af tag or commit into `runners.v1.yaml`, update the sha256
constant in `tests/scripts/vendored_runners.py` and `tests/scripts/test_vendored_runners_pin.py`,
and update this README. Do not hand-edit the YAML.
