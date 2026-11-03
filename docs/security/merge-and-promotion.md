# Merge fast in private, promote to public only when all CI and security pass

Part of `(private path)`. Status: **partly built** (see "What
exists today" below): the mirror build and its gate exist and are run by hand;
the scheduled promotion job does not. Decision record: ADR-0043.
Requirement: SEC-03.

## What exists today (re-checked Thu 15 Oct 2026)

- **The public repo** is `alex-foster-personal/opendj`, a filtered mirror of
  this repo's `main`. It is public (read from the GitHub API, Thu 15 Oct
  2026). Agents never change its visibility.
- **The build** is a private publish toolkit, not in this repo. It runs one
  deterministic `git filter-repo` sequence over a fresh clone of a pinned
  `main` SHA: a path allowlist, a denylist, path renames, text and message
  rewrites, and a mailmap. Re-running it with unchanged inputs reproduces the
  same SHAs, so the public repo only fast-forwards.
- **The gate** runs before any push: a harness that replays every leak class
  already hit (with controls), a verify pass over the built repo, and a judge
  over every published text file and every commit message. A stage that
  cannot measure reports UNKNOWN, which blocks the push.
- **Not built yet:** the scheduled job that picks the newest fully green
  private SHA, and the contributor-PR import script (hard rule 3).


## Problem

Agents merge very fast. Security checks cannot block those merges without
slowing the fleet, and today they do not (ADR-0043). The repo is going
public (OSSPUB). Unvetted fast merges must not reach the public repo.

## Decision: private upstream, public downstream

This is the established pattern for companies that develop internally and
publish open source. Google's Copybara tool exists for exactly this move
between a private and a public repository.

```text
agents -> PRs -> [fast merge, security reports only] -> PRIVATE repo main
                                                         |
                            promotion job (every N hours, or on demand):
                            pick newest private main SHA where ALL required
                            CI + security runs COMPLETED with SUCCESS
                                                         |
                            promotion-only checks: oss_tip_audit, secret scan
                            of the delta, license check
                                                         |
                            fast-forward push of that SHA -> PUBLIC repo main
```

- **Private repo** = today's `music-dj-tools`. Keeps the self-hosted
  runners, the IDD fleet, fast merges.
- **Public repo** = a new repo. Receives only promoted SHAs, through a
  deploy key that can write to the public repo and nothing else.
- **The gate proves presence, not absence.** A SHA qualifies only when every
  required job EXISTS in its runs and concluded `success`. Cancelled, skipped,
  queued or missing jobs disqualify it. This matters: main CI runs are often
  cancelled today, so "no failures" would promote untested code.
- **Fast-forward only.** If private history is rewritten (it was on
  Mon 14 Sep 2026), promotion fails loudly instead of force-pushing public
  history.

## What going public adds for free

These are paid on a private repo and free on a public one:

- CodeQL code scanning
- secret scanning with push protection
- rulesets with required status checks
- private vulnerability reporting
- free GitHub-hosted runner minutes

Turn them all on in the public repo on day one. Branch protection there means
required checks are finally enforceable, at the promotion boundary rather
than on every fast merge.

## Hard rules

1. **Never attach self-hosted runners to the public repo.** Fork PRs could
   run attacker code on agentbox or nucbox machines. The public repo uses
   GitHub-hosted runners only.
2. **Publish rewritten history, never raw history.** Publishing the private
   history as-is would expose every secret and identity ever committed, and
   the OSSPUB audit covers only the current tree. The first design here was a
   squashed root commit. The owner chose instead (Mon 5 Oct 2026) to keep
   attribution: the mirror publishes the filtered, rewritten history, and
   every commit message and every historical blob of a kept path passes the
   gate's history and message checks before a push. Only the v1.0 line of
   tags is published.
3. **Contributor PRs land in private first.** A public PR is imported into
   the private repo (patch applied to a private branch, full CI there),
   merged there, and carried out by the next promotion. The public PR is
   closed with a link. v1 does this with a small script; Copybara automates
   it if volume grows.

## Alternatives considered

| Option | Why not (for v1) |
|---|---|
| One public repo, fast `integration` branch, protected `main` | Every fast merge is public the moment it is pushed. A secret an agent leaks is exposed immediately |
| Block merges on security in the current repo | Slows the fleet; branch protection is not even available on a private Free repo |
| GitHub merge queue | Needs branch protection (unavailable on private Free) and does not separate public from private |

## SWOT (the design)

| Strengths | Weaknesses |
|---|---|
| Fleet speed unchanged; public only sees commits that passed everything; public repo gets free GitHub security features | Two repos to run; contributor PR import is a new flow; promotion lags private main |
| **Opportunities** | **Threats** |
| Required checks become enforceable at the boundary; a clean public history for v1 | A gate that trusts a missing or cancelled run would promote untested code; a leaked deploy key can write the public repo |

## Acceptance criteria (for the build)

- [if] a private SHA with any required job cancelled, skipped, queued or
  absent is promoted [then] broken.
- [if] promotion would need a non-fast-forward push [then] it must fail
  with an explicit error, not force.
- [if] the public repo has any self-hosted runner registered [then] broken.
- [if] `oss_tip_audit` or the delta secret scan finds anything [then] no
  promotion.

## Before building

The owner must approve creating the public repo. Publication cannot be undone.
