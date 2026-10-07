# Open DJ

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release: 1.0 alpha](https://img.shields.io/badge/release-1.0%20alpha-d97757.svg)](https://github.com/alex-foster-personal/opendj/releases/tag/app-v1.0.0-alpha.1)
![Platform: macOS 14+, Apple silicon](https://img.shields.io/badge/platform-macOS%2014%2B%20%C2%B7%20Apple%20silicon-lightgrey.svg)

Open DJ is a free, local-first DJ app for the Mac. It has a four-deck screen laid out like
rekordbox, imports your rekordbox library without changing it, and mixes the next track in for
you with AutoPlay and Beat Sync. Scripts and AI agents can drive most of its deck and mixer
controls through the same commands as the screen.

Website: [open-dj.com](https://open-dj.com)

> **Alpha.** Open DJ 1.0 alpha is for trying Open DJ, not for a gig you cannot afford to
> lose. Features, screens and stored data formats can change between releases. Keep a backup
> of your DJ library.

## Download

**[Open DJ 1.0 alpha for macOS](https://github.com/alex-foster-personal/opendj/releases/tag/app-v1.0.0-alpha.1)**
(`OpenDJ-1.0.0-alpha.1-aarch64.dmg`). Open the disk image and drag Open DJ to Applications.

- Apple silicon Macs only, macOS 14 (Sonoma) or newer.
- Signed with an Apple Developer ID and notarized by Apple.
- The release page lists the known issues for this alpha and the file's SHA-256.

## What it does

- **Four decks on one screen.** EQ, filter, trim and a crossfader on every deck, running on a
  Web Audio engine. LESS view keeps it to two decks.
- **Brings your rekordbox library.** First run reads your tracks, playlists, beatgrids, cues,
  keys and waveforms. Open DJ never writes to rekordbox, and your library stays on your Mac.
- **AutoPlay.** Open DJ mixes the next track in for you, beat-matched with Beat Sync. Take over
  at any point.
- **Moves several knobs at once.** Shift+click dials to select them, then scroll or drag to move
  them together.
- **Headphone cue on a single output.** With one audio device, split cue puts the master in one
  ear and your cue in the other.
- **Shows the vocals.** A vocal lane shows where the singing sits, and synced lyrics run along
  the waveform for tracks that have them.
- **Records your set.** REC captures the master mix to a file.
- **Scriptable.** Every control has a local HTTP API and an `opendj` command-line tool.

## The rule of the build

> **Nothing is mocked, nothing is invented.** A control with no real data source is visible
> but inert, and its tooltip says so. Open DJ never shows invented data.

## About this repository

This repository is the published source of Open DJ, licensed Apache-2.0. Development happens
in a private repository, and each release is published here with its history.

- **Bugs and feature requests:** open an issue here using the forms. Each one is copied into
  the development tracker and the outcome is posted back on your issue.
- **Questions and ideas:** use Discussions.
- **Pull requests** are welcome. They are applied in the development repository with you
  credited as co-author, then arrive here with the next release. See
  [CONTRIBUTING.md](CONTRIBUTING.md).
- **Security problems:** report them privately from the **Security** tab, never in an issue.

## Build from source

You need macOS, [git](https://git-scm.com/), [uv](https://docs.astral.sh/uv/getting-started/installation/),
[just](https://github.com/casey/just) and Node.js 22.14 or newer.

```bash
git clone https://github.com/alex-foster-personal/opendj.git
cd opendj

# Python environment (.venv), reproducible from uv.lock
uv sync --locked --extra dev --python 3.11.15

# Frontend (pnpm only)
corepack enable
(cd apps/webui/frontend && pnpm install --frozen-lockfile)

# Claim this checkout's ports, then run the backend and the frontend in two terminals
just webui-ports-claim
just webui-backend
just webui-frontend
```

Open `/performance` on the frontend address printed by `just webui-ports`. The
[developer guide](docs/developer-guide.md) covers tests, rekordbox library data, the
command-line tools and the safety rules for anything that writes to a DJ library.

## Documentation

| Document | What it is for |
|:-------------------------------------------------|:-------------------------------------------|
| [`docs/developer-guide.md`](docs/developer-guide.md) | The long-form tour: goals, setup, tests, command-line tools, repository layout. |
| [`docs/architecture.md`](docs/architecture.md) | The shape of the system, and why. |
| [`docs/glossary.md`](docs/glossary.md) | Terms a newcomer cannot look up (PQTZ, PVDI, stable_id). |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Conventions, tests and the pull request flow. |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each release. |
| [`open-dj/`](open-dj/) | The open-dj library format: spec, schema and conformance corpus (CC BY 4.0). |

## Thanks

Open DJ builds on the reverse-engineering and open-source work of several projects and
communities, including pyrekordbox and the Pioneer database research community.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) for the full text and [`NOTICE`](NOTICE) for
attribution of third-party runtime dependencies. Third-party components retain their own
licenses. The open-dj spec is published under CC BY 4.0; see
[`open-dj/LICENSE-SPEC.md`](open-dj/LICENSE-SPEC.md).

Open DJ is not affiliated with, endorsed by or sponsored by AlphaTheta (Pioneer DJ), Serato,
Native Instruments, Algoriddim, or the OpenDJ LDAP directory server project. rekordbox, Serato
DJ, Traktor and djay are trademarks of their respective owners.

## Security and conduct

- Security reports: [`SECURITY.md`](SECURITY.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
