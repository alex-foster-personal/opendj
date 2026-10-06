# Security Policy

Thanks for helping keep music-dj-tools and its operators safe. This project
writes to working DJ libraries (Rekordbox, djay Pro, Serato, Traktor) and
wraps live credentials for Spotify, cloud replicate, Picovoice, and Groq. A
sloppy bug here can corrupt a library or leak a token, so we take reports
seriously.

## Supported versions

| Version      | Status                | Security updates |
|--------------|-----------------------|------------------|
| v1.x         | Current (v1.0-rc1)    | Yes              |
| master       | Rolling dev branch    | Yes, best effort |
| < v1.0-rc1   | Pre-release snapshots | No               |

Only the `master` branch and the latest tagged `v1.x` release receive fixes.
Older pre-v1 snapshots are not patched; please upgrade.

## Reporting a vulnerability

Please do not open a public GitHub issue, discussion, or pull request for
security reports. Public disclosure before a fix puts every operator at risk.

Preferred channels, in order:

1. Open a private report through this repository's **Security** tab ->
   **Report a vulnerability** (GitHub private vulnerability reporting). It is
   visible only to the maintainers, and it is the fastest route.
2. If you need an encrypted channel or a live credential handoff, say so in
   that first report. We route sensitive material through Doppler-gated
   contact details rather than plaintext email.

Please include, at minimum:

- A clear description of the issue and the impact you observed.
- Repro steps or a minimal proof-of-concept.
- The commit SHA or version you tested against.
- Whether you believe the issue is already public.

We will acknowledge receipt within 5 business days.

## Disclosure policy

We follow a 90-day coordinated disclosure window by default, measured from
the day we acknowledge your report:

- Day 0: acknowledgement, triage, assign severity.
- Day 0 to 30: investigation, fix, regression test, internal review.
- Day 30 to 90: release the fix, publish an advisory, credit the reporter
  (with their consent).
- Day 90+: if a fix is not yet shipped, we will publish a short public
  advisory describing the issue and any available workaround.

We will negotiate a shorter or longer window with you when the situation
clearly calls for it (active exploitation, upstream dependency timing, or
library-corruption risk). No bug bounty is offered; this is an open-source
hobby project.

## Scope

In scope for this policy:

- Any code in `apps/`, `open-dj/`, `scripts/`, or the top-level Python
  package that writes to a DJ library. This includes the Rekordbox and djay
  mutation rails, Serato and Traktor writers, the shared state DB, and the
  USB / Pioneer export paths.
- The six-rail safety pattern itself (see
  [`apps/sync/safety.py`](apps/sync/safety.py) and the `Safety` section of
  [`README.md`](README.md)). A bypass of any rail on a destructive path is
  in scope.
- Handling of live credentials: Spotify, cloud replicate (S3 / R2),
  Picovoice, Groq, and anything else wired through Doppler. Token leakage,
  logging of secrets, or reading secrets from the wrong source are all in
  scope.
- The FastAPI daemon, SvelteKit web UI, and Tauri launcher when they are
  bound to the default loopback address. Issues that require a non-default
  bind address should say so in the report.
- Supply-chain issues in code we vendor into the repo (none today, but this
  is in scope if it changes).

## Out of scope

- Upstream dependencies. Please report to the upstream project and link the
  upstream advisory in a report to us. Our current runtime deps of note are
  pyrekordbox (MIT), mutagen (GPL-2.0-or-later), madmom, librosa,
  chromaprint via pyacoustid, FastAPI, and Tauri. See
  [`NOTICE`](NOTICE) for the full list we track.
- Hardening requests that are not vulnerabilities (for example, "please add
  a rate limit" with no exploit). Open a normal issue or PR.
- Findings that require an attacker who already has shell or filesystem
  access to the operator's Mac. We assume local-root is game-over and
  design the safety rails accordingly; see
  [`.planning/MAINTAINER-REVIEW-QUEUE.md`](.planning/MAINTAINER-REVIEW-QUEUE.md) for
  the full threat-model posture.
- UX issues in commercial apps we integrate with (Rekordbox, djay Pro,
  Serato, Traktor). Those belong with the vendor.

## Related documents

- [`README.md`](README.md) Safety section for the six-rail pattern summary.
- [`docs/operator-setup.md`](docs/operator-setup.md) for the Doppler-based
  secret handling operators are expected to use.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) for the rules new code is held to,
  including the no-dotenv and fixture-first conventions.
- [`.planning/MAINTAINER-REVIEW-QUEUE.md`](.planning/MAINTAINER-REVIEW-QUEUE.md) for the
  ongoing list of decisions the maintainer still needs to confirm before v1
  is stamped.
