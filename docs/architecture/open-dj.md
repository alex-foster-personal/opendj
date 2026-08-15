# open-dj: the vendor-neutral library format

*Deep-dive. Overview: [`../architecture.md`](../architecture.md). Terms:
[`../glossary.md`](../glossary.md).*

Spec `open-dj/spec/v0.2/open-dj.md` (CC-BY-4.0), schema `open-dj/schema/v0.2/`, reference
implementation `apps/open_dj/`, adapters `apps/adapters/`.

open-dj is not a module of this project. It is a **published standard with its own licence,
schema, conformance corpus and versioning policy**, which this project happens to be the
first implementation of. Given the thesis is opening the DJ-software moats, it is the most
strategically loaded artifact in the repo.

## The problem it exists to solve

Four vendors, four incompatible storage models, and no provenance anywhere:

| Vendor | Storage |
|:------------|:-------------------------------------------------|
| Rekordbox | SQLite (encrypted, SQLCipher) |
| djay Pro | proprietary binary blob (TSAF) |
| Serato | undocumented ID3 `GEOB` frames |
| Traktor | XML dialect, fixed five-colour cue palette |

Moving a library between them is destructive: Serato has no rating field at all, cue colours
collapse to whatever palette survives, memory cues get demoted to hot cues or the reverse,
and playlist hierarchy flattens.

But the failure the format actually targets is subtler, and the spec states it directly:

> a BPM is just a number, with no record of who analysed it, when, or with what confidence

So a **human-corrected BPM is silently clobbered by the next app's auto-analysis.** The
expensive thing in a DJ library is not the audio, it is the accumulated human judgement
layered on top of it: corrected grids, cue points, ratings, keys. Every vendor migration
quietly destroys some of it, and nothing records that it happened.

open-dj's answer is to make every analysed value carry where it came from, so disagreement
between sources is *representable* rather than resolved by whoever wrote last.

## Why a new format

The spec names and rejects alternatives explicitly (section 3.6), which is unusual and
worth preserving:

| Rejected | Stated reason |
|:---------------------|:--------------------------------------------------|
| TOML | "weak for nested arrays of objects" |
| YAML as canonical | "anchor/alias ambiguity, weaker hash-stability" |
| Protobuf / FlatBuffers | "tooling burden... not diffable" |
| SQLite as the file | "not text-diffable; worse for git" |

The common thread is that **diffability and hash stability are treated as primary
requirements**, not conveniences. That choice only makes sense once you accept the sync
model below.

It also positions against the industry's own answer. **OneLibrary** (AlphaTheta, Algoriddim
and Native Instruments, Oct 2025) is the vendor consortium format. The spec's objection:

> OneLibrary is proprietary... There is no provenance layer: a BPM is still just a number

open-dj positions as the archival complement OneLibrary is not: open, attributable, and
provenance-first.

## The provenance model

Analysed fields are wrapped:

```
ProvenanceValue<T> = { value, source, confidence?, modified_at }
```

Identity fields (`title`, `artists`, `isrc`, `track_id`) are deliberately **not** wrapped.
The spec's reasoning:

> they are observed facts... if they change, the track_id changes too

That is the load-bearing distinction. Identity is definitional; everything else is a claim
by some source at some time. Conformance case 10 shows the intended shape: a manually locked
BPM carried alongside four vendor priors (`x_prior_rekordbox`, `x_prior_djay`,
`x_prior_mik`, `x_prior_serato`), so the disagreement survives the round-trip instead of
being flattened.

Section 3.2 frames provenance as "the ETag for a future sync protocol", which is the real
ambition: enough information to merge two libraries without a human adjudicating every field.

## Canonicalization, and why it is load-bearing

`apps/open_dj/canon.py` serialises via RFC 8785 JCS (sorted keys, no whitespace,
shortest-round-trip numbers), and the SHA-256 of those bytes is the `open-dj-hash`.

Without canonicalization, four things break at once:

| Property | Failure without JCS |
|:-------------------|:--------------------------------------------------------|
| Diffing | key-order and whitespace churn read as semantic changes |
| Content identity | the hash stops being stable, so sync cannot answer "nothing to do" |
| Signing | two semantically identical documents produce different signatures |
| Equality | degrades from a byte compare to a deep structural compare |

The spec's target property (section 3.4) is that two independent tools writing the same
library **produce byte-identical output**. The corpus tests enforce exactly this: every
committed file must already be canonical (`to_canonical_bytes(doc) == raw`) and
canonicalization must be idempotent (`canon(parse(canon(x))) == canon(x)`).

## Conformance: two distinct mechanisms

Easy to conflate; they test different things.

**Document-level** (`open-dj/conformance/corpus-0.2/`, 10 files). Checks canonical stability,
idempotency, schema validity, and a pinned `track_id` regression. Never touches an adapter.

**Adapter-level** (`tests/fixtures/conformance/`, 15 fixtures, each with `expected.opendj.json`
plus `capabilities.yaml`). The round-trip property: mask the expected document by the fields
the adapter *declares* it will lose, write through the adapter, read back, compare canonical
bytes.

The second is the interesting one, because of what a failure means. It does not mean "the
adapter is lossy" - lossiness is expected and declared. It means the adapter performed an
**undeclared** lossy conversion. That is precisely the destructive, silent operation the whole
format exists to prevent, so the test is aimed directly at the thesis.

## The lossy edges

The most practically useful content in the spec, because it tells you what a migration will
actually cost. From the four adapter documents:

| Vendor | What does not survive |
|:------------|:-------------------------------------------------------------|
| **Rekordbox** | `artists[]` lossy (single string split on `,`); cue points and beatgrid are **read-only, no write-back**; smart playlists opaque |
| **djay** | `artists[]` lossy; `rating=0` dropped (djay's "unrated" sentinel is indistinguishable from a real 0); TSAF cue decoding "opportunistic", some variants stay opaque blobs; streaming-only tracks dropped with a warning by every other vendor |
| **Serato** | **rating unsupported entirely** ("Serato has no native rating. Drop-with-warning"); memory cues unsupported (opt-in `memory_as_hot`); `artists[]` joined on `" & "`; ISRC stashed in `x_serato_isrc` |
| **Traktor** | memory cues demoted to hot on write; **cue colour is not user-defined** (hot=blue, loop=green, grid=white), so `color` is discarded and reconstructed from `type`; track colour a 1..16 palette index; ISRC unsupported; folder hierarchy flattened |

Two cross-vendor facts worth internalising: **no vendor round-trips named play-orders** (every
cell in that row of section 7.4 is "no"), and rating scales diverge wildly (0..5, 0/51/102/.../255,
1..5) with Serato the sole hard-lossy case.

## Versioning

During `0.x`, breaking changes are permitted at any MINOR bump, documented in the CHANGELOG.
The 0.1 to 0.2 bump was itself breaking (the `schema_version` const changed).

Two forward-compatibility rules:
- Unknown non-`x_*` fields are ignorable at MINOR, redefinable at MAJOR.
- **`x_*` extension fields are ALWAYS preserved on round-trip, across all versions.** This is
  what lets vendor-specific data survive a trip through a tool that has never heard of it.

Freezing to 1.0 requires: two independent round-trip implementations, all four adapters at
100 percent on a frozen corpus, one dogfooded real-library round-trip, and a published
deprecation policy. **None of those gates is met today.**

## Current state, honestly

The spec is ahead of the implementation, and the roadmap does not match what was built.

The published roadmap sequences adapters as 0.2 Rekordbox (read/write), 0.3 djay,
0.4 Traktor, 0.5 Serato. What actually exists in `apps/adapters/` is **Serato and Traktor
only** - roadmap items 0.4 and 0.5 - while the 0.2 milestone the spec is *named after*,
a read/write Rekordbox adapter, does not exist. Rekordbox and djay are read-only "module
family" adapters under `apps/open_dj/adapters/` with no `write()`, and the adapter conformance
harness skips them with an explicit, documented message ("not available in Phase 16 scope").
The skip is honest; the roadmap claim above it is not.

Two further gaps, both verified:

- **The spec's own normative example is invalid.** Section 11 states `schema_version: "0.1"`
  appears at every document root, and the Appendix A canonical example declares
  `"schema_version":"0.1"`, while the schema enforces `const: "0.2"`. Validating Appendix A
  against `open-dj/schema/v0.2/open-dj.schema.json` fails with `'0.2' was expected`. Anyone
  starting from the canonical example gets a document that will not validate.
- **`content_hash` is often synthesized**, derived from `(size_bytes, mtime, file_path)` rather
  than hashing file content, flagged `x_content_hash_mode=inferred`. Since `content_hash` is the
  spec's integrity anchor, an inferred value does not provide the guarantee the field implies.

Tracked as R1-10 and R1-11 in [`../../refactor1.md`](../../refactor1.md).

## Why the licences are split

Spec and schema are CC-BY-4.0; the code is Apache-2.0. The spec explains the split, and it is
a deliberate adoption strategy rather than an oversight:

> Attribution is how a small spec gets adopted: every derived work or vendor-mode document
> must cite open-dj, which compounds into a signal other vendors can point at when asking
> internally "should we support this?". CC0 removes that signal for nothing in return.

Apache-2.0 on the code is chosen for its **patent grant**, which "matters for adapters that
touch reverse-engineered vendor formats" - cover for the legally riskiest part of the project.

Read together: force citation pressure on vendors at the spec layer, give implementers patent
cover at the code layer. That is a standards-adoption play, not an internal file format.
