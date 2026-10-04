# Rekordbox USB Export Format — Technical Research Brief

> **Generated:** 2026-04-17  
> **Scope:** `music-dj-tools` — USB export ingestion / OSS write path evaluation  
> **Status:** Research spike (read-only). No code written.  
> **Format template:** mirrors `docs/fingerprinting-options.md` / `docs/launcher-tech-survey.md`

---

## TL;DR

- **Format anatomy** is well-understood thanks to Deep-Symmetry's reverse-engineering. The USB stick is rooted at `PIONEER/` and contains two interlocking artifacts: a proprietary fixed-page relational database (`export.pdb`) and per-track analysis binary blobs (`ANLZxxxx.DAT` / `.EXT` / `.2EX`).
- **No OSS library can write a production-safe `export.pdb` as of April 2026.** `rekordcrate` (Rust) has the most complete write scaffolding and is the most promising write path. `rex` (Go) explicitly warns "do not use on a live gig."
- **pyrekordbox 0.4.4** — what we use — reads `master.db` (desktop SQLCipher) and can parse ANLZ files. It does **not** write `export.pdb`; USB export write is listed in the project's own roadmap as a future goal.
- **`rbox`** is a newer Python fork / replacement that claims read+write of ANLZ files; the PDB write side is unclear.
- **Reliability signals** are discouraging: corruption reports are common even for Rekordbox-generated files; third-party-written PDB files have a non-trivial failure rate on real CDJ-2000NXS2 / CDJ-3000 hardware.

---

## 1. Format Anatomy

### 1.1 Top-Level USB Layout

When Rekordbox runs "Export to Device" it populates the following directory tree on the target FAT32 / HFS+ volume:

```
<USB root>/
├── CONTENTS/                   # Audio files (copied verbatim from source)
│   └── .../<track>.mp3/.flac/etc.
└── PIONEER/                    # May be hidden in macOS Finder; access via Terminal
    ├── rekordbox/
    │   ├── export.pdb          # PRIMARY DATABASE — track metadata, playlists, etc.
    │   └── exportExt.pdb       # Extended export (newer devices; format partially understood)
    ├── USBANLZ/
    │   └── Pxxx/<hash>/
    │       ├── ANLZxxxx.DAT    # Beat grid, VBR seek index, waveform preview, cue pts
    │       └── ANLZxxxx.EXT    # Extended: color waveform, detailed waveform (nexus 2+)
    │       └── ANLZxxxx.2EX    # Further extensions (CDJ-3000 / high-res data)
    ├── ARTWORK/                # Embedded album art (JPEG blobs)
    ├── MYSETTING.DAT           # Per-user CDJ preferences (My Settings)
    ├── MYSETTING2.DAT
    ├── DEVSETTING.DAT
    ├── DJMMYSETTING.DAT        # Mixer (DJM) preferences
    ├── djprofile.nxs           # Kuvo profile
    └── playlists.sync          # Sync state metadata
```

> **Notes:**
> - The `PIONEER/` folder itself may be hidden on macOS (attribute `hidden`). Use `Terminal` or `ls -la` to see it. ([source: crate-digger README][crate-digger-gh])
> - Newer devices (OPUS-QUAD, OMNIS-DUO, XDJ-AZ, CDJ-3000X) additionally require **Device Library Plus / OneLibrary** (`exportLibrary.db`) — a SQLite database — which is a distinct, newer format. As of 2025 the CDJ-3000X **only** loads playlists from OneLibrary. ([source: rekordbox FAQ][rb-faq], [pyrekordbox README][pyrkb-gh])

### 1.2 `export.pdb` — the Pioneer DeviceSQL Database

**File:** `/PIONEER/rekordbox/export.pdb`

The format is called **DeviceSQL**, originally developed by a company called Encirq (later acquired by Ubiquitous Corp of Japan). No official public documentation exists. The entire reverse-engineering effort is community-driven. ([source: djl-analysis "missing" page][djl-missing])

#### Page Layout

The file is a relational database designed to be efficiently used by very low power devices (deployments on 16-bit devices with 32K of RAM were known). It consists of a series of fixed-size pages. The first page contains a file header that defines the page size and the locations of database tables by the index of their first page. The rest of the pages are data pages for the tables. Each table is a series of rows spread across any number of pages. Pages start with a header describing the page and linking to the next page.

Key header fields:
- Bytes `00–03`: always zero (padding)
- Byte `04–07`: `len_page` — fixed page size in bytes (typically 4096)
- `seqdb`: global sequence counter; each page has a `seqpage` updated to the global value every time any page is edited, then `seqdb` is incremented. Sequence numbers across pages reveal which pages were touched in which order during an export.
- All multi-byte numbers are stored in **little-endian** byte order.

#### Tables in `export.pdb`

| Table type | Content |
|---|---|
| `Tracks` | Title, artist_id, album_id, genre_id, key_id, rating, BPM×100, played_count, file paths, analyze_path (→ .DAT) |
| `Artists` | Artist name + ID |
| `Albums` | Album name + artist association |
| `Genres` / `Labels` / `Keys` | Lookup tables |
| `Playlists` / `PlaylistEntries` | Hierarchical playlist tree |
| `HistoryPlaylists` / `HistoryEntries` | Auto-recorded playlists of tracks performed off a particular USB. These are named "HISTORY 001", "HISTORY 002", etc., and created each time the media is mounted in a player. |
| `Artwork` | Album art blob references |
| `Columns` | Defines the active menus on the CDJ |
| `Colors` | Hot-cue color palette lookup |

**Ratings** live in the `Tracks` table as a numeric field (0–5 stars).

**`exportExt.pdb`:** A second file with a similar structure, the purpose of which was only recently understood. Dominik Stolz (@voidc) figured out what was in `exportExt.pdb` files. Support in OSS parsers is still incomplete (tracked as [crate-digger issue #11][cd-issue11]).

### 1.3 ANLZ Files — Per-Track Analysis Blobs

**Paths:** `PIONEER/USBANLZ/Pxxx/<hash>/ANLZxxxx.DAT`, `.EXT`, `.2EX`  
The path to the ANLZ directory is embedded in the `analyze_path` field of the track's PDB row.

When rekordbox analyzes tracks, some data is too big to fit in the database itself. This analysis data is organized into "ANLZ" files, whose path is found in the DeviceSQL string pointed to by index 14 in the string offsets at the end of the track row. Files are named like `ANLZ0001.DAT`. They are "tagged type" files with an overall file header followed by typed sections, each with its own header identifying type and length.

**ANLZ file magic:** `PMAI` (bytes 0–3), followed by `len_header` (u32) and `len_file` (u32).

#### ANLZ Tagged Sections

| Four-char tag | Extension | Content |
|---|---|---|
| `PQTZ` | `.DAT` | **Beat grid** — beat positions + BPM per beat |
| `PVBR` | `.DAT` | VBR seek index (fast seek in variable-bitrate audio) |
| `PWV1` | `.DAT` | Monochrome waveform preview (1,500 px, legacy CDJs) |
| `PWV2` | `.DAT` | Monochrome waveform detail (scrolling, legacy) |
| `PCOB` / `PCO2` | `.DAT` | **Cue points** — hot cues, memory cues, loops |
| `PSSI` | `.DAT` | Song structure / phrase analysis |
| `PWV3` | `.EXT` | Color waveform preview (Nexus 2) |
| `PWV4` | `.EXT` | Color waveform preview strip displayed above touch strip on nexus 2 players, providing a birds-eye view of current playback position. Also used in rekordbox itself. Stored in the `.EXT` file. |
| `PWV5` | `.EXT` | Variable-width color waveform detail, introduced with the nexus 2 line (also used in rekordbox), scrolling along while the track plays. Stored in the `.EXT` file. |
| `PWV6`, `PWV7` | `.2EX` | Higher-resolution waveforms for CDJ-3000 |
| `PWVC` | `.2EX` | CDJ-3000 waveform color |

**Why DAT vs EXT?** To avoid issues with older hardware unable to handle additional data due to memory limitations, new sections were only added to a copy of the original file (.DAT) and saved with extension (.EXT).

**Hot cue colors:** Immediately after the cue comment, four bytes encode color information. `color_code` identifies the color rekordbox displays; value `0x00` means the default green (the only color supported by older CDJs), while values `0x01–0x3e` identify specific colors from the 4×4 hot-cue palette grids. The next three bytes (`color_red`, `color_green`, `color_blue`) form an RGB specification similar, but not identical, to the display color.

**Waveform detail rate:** Each PWV5 entry represents one half-frame of audio data; at 75 frames/second there are 150 waveform detail entries per second of audio.

**Beat grid** is in the `PQTZ` tag of the `.DAT` file. Memory cues and hot cues are in `PCOB`/`PCO2` of the `.DAT` file. Color waveforms and detailed waveforms are in `.EXT` only.

**Ratings** are in `export.pdb` only, not in the ANLZ files.

---

## 2. OSS Libraries

### 2.1 Catalog

| Library | Language | URL | License | Read PDB | Write PDB | Read ANLZ | Write ANLZ | Last activity | Notes |
|---|---|---|---|---|---|---|---|---|---|
| **crate-digger** | Java / Clojure | [GitHub][crate-digger-gh] | Eclipse Public 2.0 | ✅ | ❌ | ✅ | ❌ | Active 2024–25 (beat-link ecosystem) | Gold standard reader; generates Java from Kaitai `.ksy` files |
| **pyrekordbox** | Python | [GitHub][pyrkb-gh] | MIT | ✅ master.db | ❌ | ✅ parse | ⚠️ planned | v0.4.4 Aug 2025 | Reads ANLZ; **no USB PDB write**; roadmap item |
| **rbox** | Python | [PyPI][rbox-pypi] | MIT/Apache to 0.1.5, GPL-3.0-only from 0.1.6 (not used here since Thu 1 Oct 2026) | ✅ | ❓ unclear | ✅ | ✅ | ~2025 | Newer fork/sibling of pyrekordbox; claims ANLZ read+write |
| **rekordcrate** | Rust | [GitHub][rekordcrate-gh] | MIT | ✅ | ⚠️ structural (binrw) | ✅ | ✅ | Active 2023–24 | Uses `binrw` for both read & write; most promising write path |
| **rex** | Go | [GitHub][rex-gh] | MIT (assumed) | ✅ | ✅ (experimental) | ❌ | ❌ | 2022–23, sparse | Writes PDB from Mixxx DB; author warns ⚠️ **"do not use on a live gig"** |
| **python-prodj-link** | Python | [GitHub][prodj-gh] | MIT | ✅ pdb decode | ❌ | ✅ partial | ❌ | 2020–22, low activity | Fabian Lesniak's original PDB decode work; foundational research |
| **beat-link** | Java | [GitHub][beatlink-gh] | Eclipse Public 2.0 | via crate-digger | ❌ | via crate-digger | ❌ | Active 2024–25 | Uses crate-digger for NFS-fetched metadata; runtime library, not a file writer |
| **mixxx** (internal) | C++ | [GitHub][mixxx-gh] | GPL-2.0 | ✅ | ❌ | ✅ partial | ❌ | Active | Reads PDB for CDJ-export USB import; read-only |

### 2.2 pyrekordbox — Our Dependency — Does It Touch USB `export.pdb`?

**Confirmed: pyrekordbox does NOT write `export.pdb`. Our assumption is correct.**

Evidence:
1. Pyrekordbox can parse all three analysis files (`.DAT`, `.EXT`, `.2EX`), although not all information can be extracted yet. "Changing and creating the Rekordbox analysis files is **planned** as well, but for that the full structure of the analysis files has to be understood." — The present tense "planned" confirms write support is not yet implemented.
2. The earliest PyPI release notes list `Add USB export database support (.pdb)` as a **future roadmap item**, not a completed feature. ([source: pyrekordbox PyPI 0.0.0][pyrkb-pypi-early])
3. Pyrekordbox can read and write Rekordbox XML databases, and can unlock and read the master.db SQLite database. There is no corresponding statement about `export.pdb` write capability.
4. The latest pyrekordbox 0.4.4 was released August 17, 2025. The GitHub README still does not list PDB write as a completed feature.

**What pyrekordbox 0.4.4 CAN do with USB export data:**
- Parse ANLZ `.DAT` / `.EXT` / `.2EX` files (beat grid, cue points, waveform metadata) — read only
- Read MySettings `.DAT` files from a USB (`PIONEER/` directory)
- Read the desktop `master.db` (our primary use case) via SQLCipher

**What `rbox` adds (distinct PyPI package):**
- rbox can **parse and write** all three ANLZ analysis files. This is the strongest Python-ecosystem ANLZ write claim, but PDB write is not explicitly mentioned.

### 2.3 rekordcrate (Rust) — Best Write Candidate

rekordcrate includes a parser for the extended Pioneer DeviceSQL database exports (`exportExt.pdb`) and a module for reading and **writing** offset arrays. An offset array consists of an array of offsets followed by data; the inner type T must implement both `BinRead` and `BinWrite`.

The Rust `binrw` crate is used symmetrically for read/write, so the structural scaffolding for writing PDB pages exists. However, no public tool built on rekordcrate has demonstrated a round-trippable `export.pdb` that plays cleanly on hardware. It is read-primary in practice.

### 2.4 `rex` (Go) — Only Confirmed PDB Writer (Experimental)

**⚠️ Warning from author:** "Do not use files generated from this project on a live gig, it probably won't work and you'll be miserable." That said, it is possible to create PDB files that can be opened in Rekordbox. These files have also been tested on a few Pioneer devices and are usable **to varying degrees**. Trying to import them on a Denon Prime 4 results in something happening, but no library.

- Writes PDB from Mixxx SQLite library databases
- Read-then-write loop for PDB analysis built in (`cmd/analyze`)
- Sparse maintenance (2022–23)

### 2.5 No JavaScript / TypeScript OSS Library Found

No actively maintained JS/TS library for reading or writing Rekordbox export PDB was found. The `rekord-cloud` community blog covers internals but produces no public library.

---

## 3. Reliability Signals

### 3.1 Corruption Reports — Summary

USB failures can be caused by physical problems on the drive, rekordbox database corruption, and the use of incompatible volume names, volume formatting, or incompatible files. All are preventable through preparation and backups.

Reported failure patterns:

| Pattern | Hardware | Root cause | Frequency |
|---|---|---|---|
| "Device library is corrupted" in rekordbox PC, but USB plays fine on CDJ | CDJ-2000NXS2, XDJ-RX3 | `export.pdb` sequence numbers / page headers out of sync after non-rekordbox write | Very common (dozens of Pioneer forum threads) |
| CDJ ignores playlist structure, plays folders only | CDJ-2000NXS2 | PDB size or page layout exceeds undocumented CDJ firmware limit | Reported (Pioneer forum, 2017+) |
| `export.pdb` from third-party writer locks up CDJ-XZ | XDJ-XZ | Malformed ANLZ reference or corrupt page heap | One documented case (VirtualDJ forum, 2021) |
| `.EXT` missing → no color waveform on CDJ-3000 | CDJ-3000 | Third-party writer omits `.EXT` file entirely | Expected; not a corruption, but degraded UX |

Key community observations:

- The majority of USB read issues are associated with people using "raw" USB sticks rather than rekordbox. CDJ failures were not specific to the CDJ-3000.
- Issues and performance degradation were mostly associated with using USB storage without rekordbox. The CDJ-2000, NXS, and NXS2 could also encounter issues with drives that hadn't been prepared with rekordbox or otherwise had formatting or physical defects.
- Pioneer's official position: "In order to get the most from Pioneer DJ hardware, we advise users to prep their files with the rekordbox software." Using rekordbox means access to full feature set on CDJ.
- Pioneer support states there is currently no ability to re-create or restore a corrupted database; the recommendation is to always maintain a backup of the main collection and carry two copies of the export device.

### 3.2 OSS-Written PDB on Real Hardware — Status

No community member has publicly reported a **fully functional, production-quality** OSS-written `export.pdb` on CDJ-2000NXS2 or CDJ-3000. The closest is:

- **rex (Go):** Author's own report — "usable to varying degrees" on some Pioneer devices; no CDJ-3000 or CDJ-2000NXS2 confirmation.
- **rekordcrate round-trip:** The presence of `BinWrite` trait impls is promising but there are no GitHub issue reports confirming CDJ hardware success.
- **Mixxx's built-in PDB reader** (C++) is the most widely field-tested OSS reader; Mixxx itself does not write PDB.

**Practical conclusion for music-dj-tools:** Any code path that writes `export.pdb` to a USB intended for a live gig carries meaningful hardware risk. The safe workflow remains: generate metadata → import into rekordbox desktop → let rekordbox write the USB.

---

## 4. Canonical Reverse-Engineering Specification

### 4.1 Primary Sources

| Document | URL | Covers |
|---|---|---|
| **DJ Link Ecosystem Analysis — Rekordbox Export Analysis** | `https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/` | PDB page layout, all table types, DeviceSQL strings, history playlists |
| **Analysis: Database Exports** | `https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/exports.html` | Complete PDB byte-level spec |
| **Analysis: Analysis Files** | `https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/anlz.html` | All ANLZ tags: beat grid, cue points, waveforms, color specs |
| **Kaitai Struct spec — `rekordbox_pdb.ksy`** | `https://github.com/Deep-Symmetry/crate-digger/blob/main/src/main/kaitai/rekordbox_pdb.ksy` | Machine-readable PDB structure; use with Kaitai Struct Web IDE |
| **Kaitai Struct spec — `rekordbox_anlz.ksy`** | `https://github.com/Deep-Symmetry/crate-digger/blob/main/src/main/kaitai/rekordbox_anlz.ksy` | Machine-readable ANLZ structure |
| **DJ Link Ecosystem Analysis — Protocol** | `https://djl-analysis.deepsymmetry.org/djl-analysis/` | Pro DJ Link network protocol (separate from export format) |

### 4.2 What the Spec Covers

The Deep-Symmetry / dysentery export analysis is the **de facto canonical specification**. The majority of the reverse-engineering work was performed by Henry Betts and Fabian Lesniak; more recently Dominik Stolz (@voidc) figured out the contents of `exportExt.pdb` files. The site documents:

- Complete PDB file header and page layout at the byte level (little-endian, with field diagrams)
- All known table types with row structure for each (tracks, artists, albums, genres, keys, artwork, playlists, history, columns, colors)
- DeviceSQL string format (two variants: short length-prefixed and long length-prefixed, UTF-16)
- All known ANLZ tags: beat-grid, VBR seek index, waveform preview (mono), waveform detail (mono), color waveform (preview + scrolling), cue points, loops, phrase/song structure
- Hot-cue color encoding (palette index + RGB fallback)
- `exportExt.pdb` partial coverage (recently added)

**Still unknown / not fully documented:**
- The exact semantics of several `unknown` fields in page headers
- Full details of the `Columns` table (CDJ menu definition)
- Complete `exportExt.pdb` table types beyond what @voidc decoded
- CDJ-3000-specific tags in `.2EX` files (partially covered)
- How PDB sequence numbers (`seqdb` / `seqpage`) must be managed for a CDJ to accept an OSS-written file without flagging corruption
- The KUVO integration fields (`djprofile.nxs`)
- OneLibrary / Device Library Plus (`exportLibrary.db`) schema

There is no official documentation publicly available; James Elliott, Henry Betts, Fabian Lesniak and others reverse-engineered and documented it on djl-analysis.deepsymmetry.org.

---

## 5. Notes from `docs/open-dj-v0-strawman.md`

The strawman spec is USB-export-adjacent in several places:

- **§1.1 (Relationship to OneLibrary):** Explicitly discusses OneLibrary as a "USB-on-CDJ playback transport" and notes it is proprietary, not open, and does not replace open-dj. Cue points and beatgrids are "delivered best-effort" with no provenance layer.
- **§2 Non-goals, item 7:** "open-dj does not define a USB-on-CDJ bootable library" — confirming the strawman intentionally defers the USB write problem.
- **§3.1 (Read-first):** References Rekordbox XML cue-point semantics as "the hot/memory baseline" and `DjmdContent.BPM` (×100) as the vendor field to map.
- **§4.2 Track — Vendor mapping table:** Lists `DjmdContent.BPM` (×100), `DjmdKey.ScaleName` as rekordbox desktop fields — these are the `master.db` fields, not the PDB fields.
- **§4 — No ANLZ or PDB fields are referenced.** The strawman deliberately avoids waveform data ("waveform summary data is not in v0", §2 item 4).

**Implication:** If music-dj-tools ever adds a USB export write path, it would extend the strawman (not conflict with it) and would require a separate implementation document covering PDB page construction and ANLZ file generation.

---

## 6. Practical Recommendation Matrix

| Goal | Recommended approach | Risk |
|---|---|---|
| Read track metadata from USB export | `pyrekordbox` (`AnlzFile.parse_file`) or `crate-digger` | Low — both are well-tested readers |
| Read `export.pdb` tables | `pyrekordbox` (partial), `crate-digger` (complete), `rekordcrate` | Low |
| Write ANLZ files | `rbox` (Python, write support claimed) or `rekordcrate` (Rust) | Medium — limited CDJ hardware validation |
| Write `export.pdb` for production USB | None ready; closest is `rekordcrate` scaffolding | **High — do not use on live gig** |
| Construct a full USB export from scratch | No OSS tool does this reliably; use rekordbox itself | High |

---

## References

[crate-digger-gh]: https://github.com/Deep-Symmetry/crate-digger  
[pyrkb-gh]: https://github.com/dylanljones/pyrekordbox  
[pyrkb-pypi-early]: https://pypi.org/project/pyrekordbox/0.0.0/  
[rbox-pypi]: https://pypi.org/project/rbox/  
[rekordcrate-gh]: https://github.com/Holzhaus/rekordcrate  
[rex-gh]: https://github.com/kimtore/rex  
[prodj-gh]: https://github.com/flesniak/python-prodj-link  
[beatlink-gh]: https://github.com/Deep-Symmetry/beat-link  
[mixxx-gh]: https://github.com/mixxxdj/mixxx  
[djl-export]: https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/exports.html  
[djl-anlz]: https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/anlz.html  
[djl-missing]: https://djl-analysis.deepsymmetry.org/djl-analysis/missing.html  
[rb-faq]: https://rekordbox.com/en/support/faq/trouble-shooting-6/  
[cd-issue11]: https://github.com/Deep-Symmetry/crate-digger/issues/11
