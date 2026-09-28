# Dependency vulnerability scanning (SCA)

Part of [security.md](security.md). Status: **spec, not wired** (Mon 14 Sep 2026).

## What it covers

Known, published vulnerabilities (CVE / GHSA / PYSEC / RUSTSEC / MAL-) in the exact
versions our lockfiles resolve. It does NOT cover freshly poisoned releases with no
advisory yet: that is [malicious-packages.md](malicious-packages.md).

This is the one security area that needs a **daily** scan: the lockfiles can sit
unchanged while new advisories land against them.

## Inventory (what must be scanned)

| Manifest | Ecosystem | Why it matters |
|---|---|---|
| `uv.lock` | PyPI | Daemon runtime + dev tools |
| `requirements.txt` | PyPI | Source of `pylock.ci.toml` and `pylock.release-check.toml` (via `requirements-*.in`, compiled by `scripts/ci_lock.py`), which self-hosted CI syncs. It is a second source of truth that can drift from `uv.lock` |
| `ops/quality/requirements.txt`, `requirements-docs.txt` | PyPI | CI tooling, docs build |
| `pylock.ci.toml`, `pylock.release-check.toml`, `pylock.docs.toml`, `ops/fleet/pylock.duplicate-writer.toml` | PyPI | The exact, hash-pinned packages self-hosted CI syncs (`scripts/ci_lock.py`), transitive versions included |
| `apps/webui/frontend/pnpm-lock.yaml` | npm | UI (prod) + Vite/Storybook toolchain (dev) |
| `apps/desktop/pnpm-lock.yaml` | npm | Desktop test tooling (wdio) |
| `apps/desktop/src-tauri/Cargo.lock` | crates.io | Tauri shell, updater TLS |
| `apps/webui/server/native/waveform/Cargo.lock` | crates.io | pyo3 native waveform module |

Adding a lockfile means adding it here. The scan asserts the parsed-lockfile count
(see acceptance criteria), so a lockfile the scanner silently skips fails loudly.

## Tool choice

**osv-scanner v2** (Google, Apache-2.0, v2.6.0 on Mon 14 Sep 2026). One binary reads
all three ecosystems (`uv.lock`, `requirements.txt`, `pnpm-lock.yaml`, `Cargo.lock`)
against OSV.dev, which aggregates GHSA, PyPA, RustSec and OSSF malicious-package feeds.

Rejected as primary scanners (overlapping databases, more moving parts):
`pip-audit` (needs a `uv export` step), `pnpm audit` (npm only; fine as a local quick
check), `cargo-audit` / `cargo-deny` (Rust only; `cargo-deny` is worth adding later for
license and duplicate-crate bans, not for advisories).

## Setup spec

```bash
# local, whole repo, all ecosystems
osv-scanner scan source -r --config=osv-scanner.toml .
```

- Install: `brew install osv-scanner` locally. In CI, download the release binary and
  verify its checksum (see [ci-supply-chain.md](ci-supply-chain.md) for why a pinned
  binary beats a marketplace action).
- PR mode: scan base and head and fail only on vulnerabilities **new** in the PR
  (`osv-scanner-reusable-pr.yml` does this; if used, pin it by SHA and set
  `upload-sarif: false`, because SARIF upload needs GitHub Code Security, which a private
  repo on GitHub Free does not have).
- Config `osv-scanner.toml` at repo root. Per-directory configs do not propagate to
  subdirectories, so pass `--config` explicitly to apply the root file everywhere.

```toml
# osv-scanner.toml -- every entry needs a reason AND an expiry (max 90 days)
[[IgnoredVulns]]
id = "RUSTSEC-2025-0081"          # unic-char-property: unmaintained, transitive via Tauri
ignoreUntil = 2026-12-14
reason = "Unmaintained notice, no exploit; recheck on next Tauri upgrade"
```

## Failure policy

| osv-scanner exit | Meaning | CI result |
|---|---|---|
| 0 | No findings | pass |
| 1 | Findings | fail on PR if new; daily run files/updates the issue |
| 127 | Scanner error | **fail as UNKNOWN**, never as clean |
| 128 | No packages found | **fail as UNKNOWN**: the scan measured nothing |

## Acceptance criteria

- [if] the daily run parses fewer than the 7 manifests above [then] fail (count parsed
  lockfiles from JSON output; do not trust exit 0 alone).
- [if] a control scan of `tests/fixtures/security/osv-control/requirements.txt`
  (pins a known-vulnerable version, e.g. `jinja2==2.10`) exits 0 [then] fail: the
  scanner cannot see.
- [if] a PR adds a dependency version with a published advisory [then] the PR check fails
  naming the advisory ID.
- [if] an `IgnoredVulns` entry lacks `reason` or `ignoreUntil` [then] a lint step fails.

## Baseline (Mon 14 Sep 2026, main `8842bbf50`)

Measured with pip-audit on a `uv.lock` export (109 packages), `pnpm audit`, and the OSV
API over both `Cargo.lock` files (503 + 28 crates, with a positive control):

- PyPI: 6 packages flagged (starlette, pillow, msgpack, httpx2, httpcore2, pytest).
- npm: production deps clean in both projects; dev toolchain 7 high (webui) and 10 high
  (desktop).
- crates.io: rustls, pyo3, glib, proc-macro-error, 5x `unic-*`.

Prioritized fixes live in [security.md](security.md#patch-priority).
