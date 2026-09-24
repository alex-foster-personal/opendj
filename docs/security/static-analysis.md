# Static analysis (SAST) and AI security review

Part of [security.md](security.md). Status: **spec, not wired** (Mon 14 Sep 2026).

## What it covers

Insecure patterns in our own code: `subprocess` misuse, path traversal, unsafe
deserialization, weak crypto, SQL built from strings, parsers fed untrusted bytes. Most
code here is agent-written, so volume is high, and pattern checks are cheap to run on
every PR.

It does NOT find design flaws such as "the daemon trusts every loopback caller". Those
belong to the AI review layer below and the manual areas in
[security.md](security.md#what-scanners-cannot-see).

## Layers

| Layer | Tool | When | Blocks? |
|---|---|---|---|
| Python patterns | **ruff `S` rules** (flake8-bandit) | pre-commit + existing quality ratchet | yes, via ratchet |
| Python + TS patterns | **Semgrep CE** registry packs + custom rules | PR (diff-aware) | new findings only |
| Cross-file taint, platform rules | **Semgrep AppSec Platform** (`semgrep ci`) | same-repo PR (diff-aware) + weekly full of `main` (unproven: never completed inside its timeout as of Tue 22 Sep 2026) | Block-mode rules only; report-only |
| Design / logic | Claude Code **`/security-review`** on high-risk paths | weekly + on PRs touching those paths | advisory |

### ruff `S`

Zero new tools: ruff already runs. Add `"S"` to `[tool.ruff.lint] select` in
`pyproject.toml` and:

```toml
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "S105", "S106", "S108"]   # asserts and fixture strings are fine in tests
```

Land it through the existing quality ratchet: measure the count first
(`ruff check --select S --statistics apps scripts`), baseline it, and allow it only to
go down. Suppress a true exception inline with the reason
(`# noqa: S603 -- argv list, no shell, fixed binary`).

### Semgrep CE

```bash
semgrep scan --config p/python --config p/typescript --config p/rust \
  --config tools/semgrep/ --baseline-commit origin/main --error
```

- No login needed for registry packs. `--error` is required, because Semgrep exits 0 on
  findings by default.
- **Known gap:** Semgrep does not parse `.svelte` files. Logic in `src/lib/**/*.ts` is
  covered, but `<script>` blocks inside components are not. Accepted for v1.
- `tools/semgrep/` holds a few repo-specific rules, each with a positive test file.
  First rules:
  - `Image.open` without `formats=` in server code (see the Pillow item in the patch list)
  - `subprocess` with `shell=True`
  - FastAPI routes writing to a request-supplied path without resolving it against an
    allowed root

Semgrep's free cloud tier (up to 10 contributors) adds cross-file taint analysis, which
is its biggest lead over CE (research: CE detected 44-48% vs Pro 72-75% on Semgrep's own
benchmark). It requires uploading code to Semgrep. **Adopted Tue 15 Sep 2026:** the
`semgrep` job in `security.yml` runs `semgrep ci` against the Semgrep AppSec Platform on
same-repo PRs and weekly (Monday) on `main`; the full scan was daily until ADR-NEW-semgrep-full-scan-weekly. Token handling and the reason CE still runs are in
[routine-scanning.md](routine-scanning.md#semgrep-appsec-platform-semgrep-ci).

### AI security review

Run `/security-review` (Claude Code) weekly, scoped to the paths where a bug is a
security bug:

- `apps/webui/server/share_gate.py`, `app_wiring.py` (CORS), `routes/**`
- `apps/desktop/src-tauri/tauri.conf.json`, `capabilities/**`
- `.github/workflows/**`, `scripts/sign_macos_developer_id.sh`

Findings go into the weekly security issue as advisory items. A human or agent confirms
each one before fixing. The `anthropics/claude-code-security-review` action states it is
not hardened against prompt injection: if used in CI, run it only on trusted
same-repo branches, with a read-only token.

## Acceptance criteria

- [if] `tests/fixtures/security/sast-control/` (a file with one known-bad pattern per
  custom rule, excluded from normal runs) produces zero Semgrep findings in the control
  step [then] fail: the rules or the parser are broken.
- [if] a PR adds `subprocess.run(cmd, shell=True)` under `apps/` [then] the PR fails.
- [if] the ruff `S` count on a PR exceeds the ratchet baseline [then] the PR fails.
