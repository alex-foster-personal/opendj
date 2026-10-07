# Security Policy

Thanks for helping keep Open DJ and the people who use it safe. Open DJ reads working DJ
libraries (rekordbox, djay Pro, Serato, Traktor), can write to some of them behind explicit
safety rails, and can hold credentials for optional services. A careless bug here can corrupt
a library or leak a token, so we take reports seriously.

## Supported versions

| Version | Status | Security updates |
|:--------|:-------|:-----------------|
| 1.0 alpha (`app-v1.0.0-alpha.1`) | Current public release | Yes |
| `main` | Development | Yes, best effort |
| Anything older | Internal builds | No |

## Reporting a vulnerability

Please do not report security problems in public. Disclosure before a fix puts every user at
risk.

Report privately through this repository's **Security** tab, then **Report a vulnerability**
(GitHub private vulnerability reporting). Only the maintainers can see the report. If you need
an encrypted channel, say so in that first report and we will arrange one.

Please include, at minimum:

- A clear description of the issue and the impact you observed.
- Steps to reproduce, or a minimal proof of concept.
- The Open DJ version or commit SHA you tested.
- Whether you believe the issue is already public.

We will acknowledge receipt within 5 business days.

## Disclosure policy

We follow a 90-day coordinated disclosure window by default, measured from the day we
acknowledge your report:

- Day 0: acknowledgement, triage, assign severity.
- Day 0 to 30: investigation, fix, regression test, internal review.
- Day 30 to 90: release the fix, publish an advisory, credit the reporter (with their
  consent).
- Day 90+: if a fix is not yet shipped, we publish a short public advisory describing the
  issue and any available workaround.

We will agree a shorter or longer window with you when the situation clearly calls for it
(active exploitation, upstream dependency timing, or library-corruption risk). No bug bounty
is offered.

## Scope

In scope:

- The Mac app, its local engine, the web UI and the `opendj` command-line tool when bound to
  the default loopback address. Issues that need a non-default bind address should say so.
- Any code in `apps/`, `open-dj/` or `scripts/` that writes to a DJ library, including the
  rekordbox and djay write paths, the Serato and Traktor writers, the shared state database,
  and USB export.
- The six-rail safety pattern itself (see [`apps/sync/safety.py`](apps/sync/safety.py) and the
  safety section of the [developer guide](docs/developer-guide.md)). A bypass of any rail on a
  destructive path is in scope.
- Handling of credentials for optional services: token leakage, logging of secrets, or reading
  secrets from the wrong source.
- The update path: anything that could make the app install an update that was not signed by
  the project.

## Out of scope

- Upstream dependencies. Please report to the upstream project and link the upstream advisory
  in your report to us. See [`NOTICE`](NOTICE) for the dependencies we track.
- Hardening requests that are not vulnerabilities (for example, "please add a rate limit" with
  no exploit).
- Findings that need an attacker who already has shell or filesystem access to the user's Mac.
- Problems in the commercial apps Open DJ works with (rekordbox, djay Pro, Serato, Traktor).
  Those belong with the vendor.

## Related documents

- [`docs/developer-guide.md`](docs/developer-guide.md): the six-rail safety pattern and how
  the code is organized.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): the rules new code is held to.
