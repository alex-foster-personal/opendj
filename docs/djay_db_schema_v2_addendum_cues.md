# djay TSAF schema v2 addendum: cue points, loop regions, start point

**Status:** Plan 1 authored. Byte layout here matches what
``apps/sync/djay_writer.py`` emits and what ``apps/shared/djay_db.py``
decodes. Live-djay byte-identical fidelity has not been verified against
a committed hex fixture corpus; see `scripts/harvest_djay_cues.py` for
the runbook that pulls live blobs once the user harvests them.

**Scope:** `cuePoints`, `loopRegions`, `startPoint`, plus supporting
bool marker for `isStraightGrid`. All writes are preserve-then-patch
(D3): we never reserialize the whole blob; we splice-replace the named
field's bytes.

## Token reference (schema v2 intro)

| Token | Bytes | Purpose |
|---|---|---|
| `0x08 <utf8> 0x00` | var | String (UTF-8, NUL-terminated). Used for field names and scalar string values. |
| `0x13 0x00 0x00 0x00 <float32 LE>` | 8 | Float32 value. TSAF always emits 3 padding bytes between the tag and the value bytes. |
| `0x0f <uint8>` | 2 | Small unsigned-int value (ratings, indices, colours, key). |
| `0x0b <count uint32 LE>` | 5 | Array / object header. Followed by `count` nested-object elements. |
| `0x2b 0x08 <classname> 0x00` | var | Nested-object marker (start of a TSAF class instance, e.g. `ADCMediaItemCuePoint`). |
| `0x0d` | 1 | Boolean-false marker. Placed immediately before the key name for `isStraightGrid` when false. |
| `0x00` | 1 | Null / inner terminator. Marks end of nested-object elements and end of the top-level object. |

Schema v2 convention: **values precede keys** (e.g. `0x13 <float> 0x08 "bpm" 0x00`).

## `cuePoints` array

Located anywhere in a `mediaItemUserData` TSAF blob, preceded by the
field name marker:

```
0x08 "cuePoints" 0x00
0x0b <count uint32 LE>
<element>*          # count nested ADCMediaItemCuePoint objects
```

When the track has no cues, the field may either:
* be absent entirely, or
* be present as `0x0b 0x00 0x00 0x00 0x00` (zero-count array).

Both cases decode to an empty cue list. The writer emits the zero-count
form whenever a previous non-empty array is being cleared.

## `ADCMediaItemCuePoint` element

```
0x2b 0x08 "ADCMediaItemCuePoint" 0x00
<float32 seconds> 0x08 "position" 0x00
[0x0f <uint8 0..7>  0x08 "index" 0x00]       # hot cues only
[0x0f <uint8 1..8>  0x08 "color" 0x00]       # palette index; omit if 0
[0x08 <utf8> 0x00 0x08 "name" 0x00]          # optional label
0x00                                          # element terminator
```

`position` is stored in **seconds** (float32) -- `NormalisedCue.position_msec` is
`round(position * 1000)`. Float32 precision is ±1 ms at typical song
lengths.

`index` presence distinguishes hot cues (`kind="hot"`) from memory cues
(`kind="memory"`); the former store `0..7`, the latter omit the field.

`color` is a palette index into the Rekordbox 8-colour palette stored in
`apps/shared/rb_color_palette.py`; `0` means "no colour" and is omitted
from the encoding.

### Puzzling bytes (TODO(O1))

1. Algoriddim may or may not include additional per-cue flags we have
   not reverse-engineered (e.g. "call-out" / "auto-loop enabled"). If
   a live-harvested blob carries unknown tokens between the known
   fields and the `0x00` terminator, extend the parser to preserve
   them via a generic "unknown token passthrough" list.
2. Whether `index` is implicit by array position instead of stored
   explicitly. The writer stores it explicitly.
3. Rounding direction for `position`: we use `round()`; djay may use
   `floor()`. 1 ms is below audible.

## `ADCMediaItemLoopRegion` element

Same shape as a cue element but adds a `length` float32 (seconds):

```
0x2b 0x08 "ADCMediaItemLoopRegion" 0x00
<float32 seconds> 0x08 "position" 0x00
<float32 seconds> 0x08 "length" 0x00
[0x0f <uint8>  0x08 "color" 0x00]
[0x08 <utf8> 0x00 0x08 "name" 0x00]
0x00
```

`loopRegions` lives in its own named array adjacent to `cuePoints`.
`NormalisedCue.kind="loop"` maps here; `loop_length_msec` is the
length field converted to milliseconds.

## `startPoint` (scalar)

```
<float32 seconds> 0x08 "startPoint" 0x00
```

Plain scalar field with the standard TSAF value-before-key layout.

## `isStraightGrid` (scalar bool)

```
<false marker 0x0d> 0x08 "isStraightGrid" 0x00
```

When the marker byte before the key name is `0x0d` the value is `False`;
any other byte (or field absent) is treated as `True` / unset. `True`
is the overwhelmingly common case (straight-grid tracks).

## Write strategy: preserve-then-patch

`apps/sync/djay_writer.py` implements:

* `patch_cue_points(blob, new_cues)` -- locate the existing `cuePoints`
  array via `_tsaf_scan_array(blob, b"cuePoints")` and splice-replace
  its `0x0b <count> <elements>` bytes. When the field is absent we
  insert the full `0x08 "cuePoints" 0x00 0x0b <count> <elements>`
  sequence before the tail terminator block (`0x0b 0x05 0x?? 0x00`).
* `patch_loop_regions(blob, new_loops)` -- same for `loopRegions`.
* `patch_start_point(blob, seconds)` -- single-field scalar splice.
* `patch_manual_bpm` / `patch_key_signature_index` / `patch_color_index`
  / `patch_tags` -- same pattern for the analysis scalars.

Byte-diff vs the original is minimal: only the affected region changes.
All other fields, including anything we have not reverse-engineered,
survive intact.

## Round-trip guarantee

The parser (`parse_cues_from_blob`) and the writer (`encode_cue_point`)
are round-trip consistent: `parse(encode(cues)) == cues` for any list
of `NormalisedCue`. This is tested in `tests/test_tsaf_cues.py`
(17 tests).

## When live-harvest reveals drift

Once `scripts/harvest_djay_cues.py` produces a real committed fixture
corpus, compare the writer's output against the first 3 harvested
blobs byte-by-byte. If deviations appear:

1. Update this addendum with the observed layout.
2. Update `encode_cue_point` + `_tsaf_parse_cue_element` together so
   reader and writer stay in lock-step.
3. Re-run `tests/test_tsaf_cues.py` + `tests/test_djay_writer.py`
   (Plan 3).

Any unknown bytes captured during harvest that we cannot decode but
must preserve on write go through a per-element passthrough byte buffer
-- see the Plan 3 TODO.
