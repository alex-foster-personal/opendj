"""Response and request shapes for the karaoke words routes (LYR-03).

Split out of :mod:`apps.webui.server.routes.lyrics_words` only to keep both
files under the 600-line quality gate; the router is the only importer. Names
are prefixed Karaoke/Coverage rather than Lyric because
``apps.webui.server.models`` already owns a ``LyricLineOut`` for main's
LINE-level LRCLIB payload, and two same-named models one import apart is how
the wrong schema ends up in openapi.json.

Every non-obvious field carries a ``description``: FastAPI puts it in
openapi.json, openapi-typescript carries it into api-types.ts as a doc
comment, and the UI renders numeric readouts with it as hover text (house
rule: a number on screen explains itself).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


#-----------------------------------------------------------------------------
# models: words, lines and the verdict envelope
#-----------------------------------------------------------------------------
class KaraokeWordOut(BaseModel):
    """One aligned word, exactly as the artifact stores it."""

    model_config = ConfigDict(frozen=True)

    idx: int = Field(description="position in the track's flat word list, from 0")
    word: str
    start_s: float | None = Field(default=None, description="sung onset in seconds")
    end_s: float | None = Field(default=None, description="sung offset in seconds")
    score: float | None = Field(
        default=None, description="aligner confidence for this word, 0..1"
    )
    witness: str | None = Field(
        default=None,
        description="ASR-witness verdict: agree/drift/unheard/unmatchable are "
                    "shown normally; contradict/lost are the suspect classes.",
    )
    line_final: bool = Field(
        default=False, description="this word closes its line (the grouping flag)"
    )


class KaraokeLineOut(BaseModel):
    """One derived line (:mod:`apps.lyrics.lines`, the canonical grouping).

    Served rather than client-derived so every surface agrees and an agent can
    curl exactly what the deck renders.
    """

    model_config = ConfigDict(frozen=True)

    first_idx: int
    last_idx: int
    text: str
    start_s: float | None = Field(description="first sung onset in the line, seconds")
    end_s: float | None = Field(description="last sung offset in the line, seconds")
    n_words: int
    n_red: int = Field(description="words the witness put in a red class")
    n_judged: int = Field(description="words the witness judged at all")
    quality: float | None = Field(
        description="share of witness-judged words NOT in the red classes; "
                    "null when no word in the line was judged",
    )
    band: str = Field(description="good | uncertain | bad | unjudged")
    para_final: bool = Field(
        description="gap-derived paragraph break: a blank line follows"
    )


class CoverageVerdictOut(BaseModel):
    """One ``lyric_verdict`` row as the API states it."""

    model_config = ConfigDict(frozen=True)

    stable_id: str
    verdict: str = Field(description="computed verdict from stem vocal coverage")
    effective_verdict: str = Field(
        description="what to act on: the human override if set, else verdict"
    )
    effective: str = Field(
        description="same as effective_verdict; kept for clients that read .effective"
    )
    coverage_pct: float | None = Field(
        default=None, description="share of the track the vocal stem carries energy"
    )
    source: str | None = Field(default=None, description="lyric provider + match method")
    language_iso3: str | None = None
    n_words: int | None = Field(default=None, description="words in the karaoke artifact")
    n_lines: int | None = Field(default=None, description="derived lines in the artifact")
    pct_witness_red: float | None = Field(
        default=None, description="share of words the independent ASR witness distrusts"
    )
    has_words: bool = Field(
        description="a words artifact exists for this track (the row records a "
                    "words_content_hash); false means the words route will 404"
    )
    override: str | None = None
    override_note: str | None = None
    pipeline_version: str = Field(
        description="the code version that produced this row's hash"
    )
    computed_at: str = Field(description="when the pipeline judged the track")
    updated_at: str = Field(description="sync stamp: when a machine last wrote the row")
    # Track metadata, populated ONLY by the listing (one bulk join) so the
    # triage table can show names instead of ids. Absent everywhere else.
    title: str | None = None
    artist: str | None = None


class KaraokeTrackOut(BaseModel):
    """The words payload. ``verdict`` is ALWAYS set: see the 404 rule."""

    model_config = ConfigDict(frozen=True)

    verdict: CoverageVerdictOut
    words: list[KaraokeWordOut]
    lines: list[KaraokeLineOut] | None = Field(
        default=None, description="present only when requested with ?include=lines"
    )


class KaraokeSummaryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    counts: dict[str, int] = Field(description="effective-verdict histogram")
    total: int = Field(description="live verdict rows, the histogram's denominator")
    no_lyrics_max_coverage: float = Field(
        description="calibrated band: at or below this stem vocal coverage a track "
                    "is auto-stamped no-lyrics (novox round 0, pinned by test)"
    )
    sparse_max_coverage: float = Field(
        description="above this coverage a track is treated as vocal; between the "
                    "two, other evidence decides"
    )


#-----------------------------------------------------------------------------
# models: the operator levers
#-----------------------------------------------------------------------------
class OverrideIn(BaseModel):
    override: str | None = Field(
        default=None, description="vocal|sparse|no-lyrics, or null to clear"
    )
    note: str | None = Field(default=None, description="why the human disagreed")


class LyricsConfigOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_order: list[str] = Field(
        description="lyric sources, strongest first; the pipeline's tie-break"
    )
    source_titles: dict[str, str] = Field(
        description="hover text per source (what the method actually does)"
    )
    is_default: bool = Field(
        description="true while no operator has persisted a custom order"
    )


class LyricsConfigIn(BaseModel):
    source_order: list[str] = Field(
        description="every known source, ranked; a partial list is rejected"
    )


class LyricJobOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    ts: str = Field(description="when the job was queued, UTC")
    kind: str
    stable_ids: list[str]
    status: str = Field(description="queued | done | failed")
    note: str | None = Field(default=None, description="the runner's stage progress")


class LyricJobIn(BaseModel):
    kind: str = Field(description="analyze | lyricsync | stems")
    stable_ids: list[str] = Field(description="tracks to process; each must exist")
    note: str | None = None


class LyricsPurgeIn(BaseModel):
    source_prefix: str = Field(description="source prefix, matched LIKE prefix%")
    dry_run: bool = Field(
        description="NO default on purpose: a purge deletes licensed text from "
                    "disk and R2, so the caller states which one it wants"
    )


class LyricsPurgeOut(BaseModel):
    """:class:`apps.lyrics.purge.PurgeReport`. Every count is observed."""

    model_config = ConfigDict(frozen=True)

    source_prefix: str
    dry_run: bool
    rows_matched: int = Field(description="live verdict rows whose source matched")
    rows_tombstoned: int = Field(description="rows soft-deleted; 0 on a dry run")
    files_removed: int = Field(description="local karaoke artifacts deleted")
    files_absent: int = Field(description="matched rows with no local artifact")
    objects_deleted: int = Field(description="R2 objects deleted")
    objects_absent: int = Field(description="R2 keys already gone")
    r2_skipped_reason: str | None = Field(
        default=None, description="why R2 was left alone; null means it was visited"
    )
    stable_ids: list[str]


class LyricsVerdictBackfillIn(BaseModel):
    """Parity for ``python -m apps.lyrics verdicts backfill``."""

    dry_run: bool = Field(
        description="NO default on purpose, same reasoning as the purge lever: "
                    "the caller states which one it wants"
    )
    limit: int | None = Field(
        default=None, ge=0, description="max tracks to compute this call (null = no cap)"
    )
    include_reserved: bool = Field(
        default=False,
        description="also process the 100 stable_ids reserved for in-app "
                    "ordering QA (refused by default)",
    )


class LyricsVerdictBackfillOut(BaseModel):
    """:class:`apps.lyrics.library_verdicts.VerdictBackfillReport`."""

    model_config = ConfigDict(frozen=True)

    dry_run: bool
    data_dir: str
    candidates: int = Field(description="stable_ids with a bundle directory in either root")
    processed: list[str] = Field(description="written (or, on a dry run, would be written)")
    reused_cache: list[str] = Field(
        description="subset of processed whose coverage came from an existing "
                    "from-stems vocal-cache entry instead of a fresh decode"
    )
    skipped: dict[str, list[str]] = Field(description="reason -> stable_ids")
    failed: dict[str, str] = Field(description="stable_id -> failure reason")


class LyricsKpiDefOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    unit: str
    direction: str = Field(description="lower_better | higher_better")
    title: str = Field(description="hover text: what the number actually measures")


class LyricsKpiSnapshotOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: str
    label: str = Field(description="the experiment round or shipped milestone")
    values: dict[str, float | None] = Field(
        description="one entry per KPI; null is a gap, never a carried-forward number"
    )
    provenance: str = Field(
        description="'measured' at append time, or 'hand' transcribed from the "
                    "experiment log. House rule: never quote a value without its ts."
    )
    notes: str


class LyricsKpiOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    kpis: dict[str, LyricsKpiDefOut]
    snapshots: list[LyricsKpiSnapshotOut] = Field(description="oldest first")
