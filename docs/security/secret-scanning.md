# Secret scanning

Part of [security.md](security.md). Status: **spec, not wired** (Mon 14 Sep 2026).

## What it covers

Credentials committed to git: API keys, tokens, private keys, signing material. Secrets
are injected via Doppler at runtime (`doppler run -- ...`), so any secret literal in the
tree is a defect.

Availability of GitHub secret scanning and push protection depends on repository and
account configuration; verify it for the deployment. Everything below uses OSS tools
that run independently of those hosted capabilities.

## Tool choice

| Layer | Tool | Why |
|---|---|---|
| Pre-commit | **gitleaks** (v8.30.1, hook id `gitleaks`) | Fast regex scan of staged changes. Catches it before it exists in history |
| PR check | **gitleaks** CLI on the PR commit range | Agents can skip hooks with `--no-verify`; CI is the enforced layer |
| Weekly, full history | **trufflehog** `--only-verified` | Tests candidate secrets against the live provider, so findings are real, current exposures |

gitleaks-action is free for personal-account repos, but prefer the CLI binary
(checksum-verified) to keep third-party actions to a minimum.

## Setup spec

```yaml
# .pre-commit-config.yaml (add)
- repo: https://github.com/gitleaks/gitleaks
  rev: v8.30.1
  hooks:
    - id: gitleaks
```

```bash
# PR check: only the commits this PR adds
gitleaks git --log-opts="origin/main..HEAD" --redact --exit-code 1

# weekly: whole history, verified live secrets only
trufflehog git file://. --only-verified --fail
```

`.gitleaks.toml` extends the default rules; allowlist only proven false positives, by
path or regex, each with a comment saying why.

### First run

1. `gitleaks git --report-path .tmp/gitleaks-first-run.json` over full history.
2. Triage each hit. A real secret gets **rotated first** (in Doppler), then allowlisted
   as historical. Never "baseline away" a live credential.
3. Commit the reviewed baseline (`--baseline-path`) so later runs report only new hits.

### `.gitignore` hardening (same PR)

Today `.gitignore` covers `.env` but not key material. Add: `*.pem`, `*.key`, `*.p12`,
`*.p8` (Apple App Store Connect API keys), `*.mobileprovision`, `credentials.json`,
`service-account*.json`, and the Tauri updater private key file pattern (`*.key` covers
the default `tauri signer generate` output).

## Acceptance criteria

- [if] a temp-repo control commit containing a synthetic token in a known provider format
  does not make `gitleaks git` exit 1 [then] fail: the scanner is blind.
- [if] a PR adds a file matching a gitleaks rule [then] the PR check fails with the
  file and line (secret redacted).
- [if] trufflehog errors (network, auth) [then] the weekly job fails as UNKNOWN, not clean.
- [if] `git check-ignore` does not ignore a fixture `x.p8` and `x.pem` [then] fail.

## Not covered

Secrets pasted into issues, PR comments, chat, or agent transcripts. Secrets on
self-hosted runners: see [ci-supply-chain.md](ci-supply-chain.md).
