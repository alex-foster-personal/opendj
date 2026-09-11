# Licensing of open-dj artefacts

| Artefact                                             | Licence            | Why                                                    |
|------------------------------------------------------|--------------------|--------------------------------------------------------|
| Spec prose (`spec/`, this directory's `*.md`)        | CC-BY-4.0          | [`LICENSE-SPEC.md`](LICENSE-SPEC.md); encourages cite + reuse. |
| JSON Schema (`schema/`)                              | CC-BY-4.0          | Same as prose; schema is normative text.               |
| Reference implementation (`apps/open_dj/`)           | Apache-2.0         | Repo-root `LICENSE`.                                   |
| Vendor adapters (`apps/adapters/<name>/`)            | Apache-2.0         | Repo-root `LICENSE`, file-level.                       |
| Conformance corpus (`tests/fixtures/conformance/`)   | CC-BY-4.0          | Seed docs are treated as spec artefacts.               |
| Test code (`tests/`)                                 | Apache-2.0         | Repo-root `LICENSE`.                                   |

## Third-party reference material used for the Serato adapter

All clean-room; no code copied in.

| Source                                                        | Licence        | Use                                      |
|---------------------------------------------------------------|----------------|------------------------------------------|
| [triseratops](https://github.com/HoldenVR/triseratops)        | MPL-2          | README docs only, format reference.      |
| [serato-tags](https://github.com/Holzhaus/serato-tags)        | CC-BY-SA-4.0   | Kaitai schema docs only.                 |
| [pyserato](https://github.com/GuillaumeDua/pyserato)          | MIT            | Reference only (output cross-check).     |
| [python-serato-crates](https://github.com/example/gpl-serato) | GPL-3          | **Not used.** Excluded by ROADMAP C9.    |

If a future phase wants to embed triseratops code directly, it must live
under `apps/adapters/serato/_triseratops_port/` with every file carrying an
MPL-2 header. The carve-out is explicit + auditable.

## Nominative trademark use

"Serato", "Traktor", "Rekordbox", "djay", and related marks are property of
their respective owners. open-dj references them only in the nominative
sense -- describing the formats these applications produce. No endorsement
or affiliation is implied.
