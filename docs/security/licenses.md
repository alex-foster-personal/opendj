# Dependency licenses

Part of [security.md](security.md). Status: **wired** (Wed 1 Oct 2026),
`.github/workflows/license-gate.yml`, decision in
[ADR-NEW-dependency-license-gate](../decisions/ADR-NEW-dependency-license-gate.md).

## What it covers

Every package locked by every lockfile in the repo, transitive ones included, and
every model weights file the payload builders ship. It answers one question: did a
license we do not want come in, directly, through a transitive dependency, or
through a version bump that relicensed a package we already had?

Why it exists: on Wed 1 Oct 2026 the shipped desktop payload already carried two GPL
packages nobody had chosen. `rbox` went from MIT/Apache to GPL-3.0-only at 0.1.6, and
`jsonschema[format]` pulled in `rfc3987` (GPL-3.0+). The second is fixed here
(`jsonschema[format-nongpl]`); the first is a recorded exception while the
Apache-versus-GPL question is open (PR #4770).

## The three files

| File | Written by | Holds |
|---|---|---|
| `licenses/policy.json` | hand | lockfiles scanned, `allow` and `deny` regexes, reasoned `exceptions`, reasoned `overrides` |
| `licenses/register.json` | `python -m scripts.license_gate update` | one line per locked `name@version` per ecosystem (pypi, npm, cargo) with its license expression |
| `licenses/models.json` | hand | model weights: code license, weights license, whether it ships, evidence |

## How a license is judged

The expression is parsed as SPDX: `OR` passes if any branch is allowed, `AND` needs
every term allowed, `WITH <exception>` is judged on its base license. Each id is
matched against `deny` first (GPL, AGPL, "General Public License", non-commercial,
SSPL, BUSL, proprietary), then `allow` (MIT, BSD, Apache-2.0, ISC, MPL-2.0, Zlib, CC0,
Unicode and similar). An id matching neither, LGPL and `UNKNOWN` included, is
**unknown** and fails like a denied one. Either way the fix is to drop the
dependency or add an exception with a reason.

An exception names the ecosystem, the package, and the **exact** license string. If
the package is relicensed, the exception stops matching and the gate fails again, so
an exception is a decision about one license, never a blanket pass for a package.
Exceptions, overrides and register entries that no longer match anything locked
fail as stale, so the files cannot rot.

## Keying on name@version

The register keys every entry by version, not by name. That is deliberate: `rbox`
relicensed in a patch-level bump, and the only way to catch that is to re-read the
license on every bump. The cost is that every lockfile change, including Renovate's
weekly lock maintenance, needs one `update` run on the branch:

```bash
python -m scripts.license_gate update   # network: PyPI JSON, npm registry, cargo metadata / crates.io
git diff licenses/register.json          # review: new licenses show up here
python -m scripts.license_gate check    # the CI gate, offline
```

`update` only fetches entries that are new, so a typical bump is seconds. A package
with no license metadata comes back `UNKNOWN`; read its upstream LICENSE and add an
`override` with the evidence in its reason.

## What it cannot see

- Code vendored into the tree without a lockfile entry. Review catches that, not this.
- Models downloaded at runtime by name (Hugging Face ids, `torch.hub`). Those are
  declared in `licenses/models.json` by hand; the gate only discovers weights files
  that the payload builders reference.
- Whether an exception's reasoning is still true. `ships` and `status` fields record
  what was believed on the `recorded` date.
- Registry metadata that is itself wrong. PyPI `license` fields are free text; the
  normalizer maps the common ones and leaves the rest as `UNKNOWN` or
  `LicenseRef-...`, which fail until a human looks.
