"""The setup wire models -- one shape per endpoint, in one place.

Split out of :mod:`apps.engine_core.setup.api` so the router file is the
FLOW (what each step does, what it refuses) and this file is the CONTRACT
(what crosses the wire). They are generated into the frontend's typed
client, so a field renamed here is a compile error there rather than an
undefined at runtime.

Two conventions carry the house rules into the schema itself:

* ``LastImportOut`` and ``FolderLastImportOut`` are a DISCRIMINATED union on
  ``kind``. A rekordbox import and a folder import report different facts and
  a caller must not have to guess which one it is holding.
* every count that could be quoted against a bad denominator sits next to the
  denominator it belongs to (``analyses_linked`` with ``analyses_expected``,
  ``tracks`` with ``unreadable_music_roots``), so an honest sentence can be
  built without a second request.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from apps.shared.platform_paths import normalise_path_prefix


def normalize_setup_folder_path(raw: str) -> str:
    """Canonical folder path for setup detect/import: expanduser + strip trailing slash."""
    trimmed = raw.strip()
    expanded = str(Path(trimmed).expanduser())
    return normalise_path_prefix(expanded)


class FileProbeOut(BaseModel):
    """One real path and whether it is actually there."""

    path: str
    exists: bool
    size_bytes: int | None = None
    modified_at: str | None = None


class RekordboxDetectionOut(BaseModel):
    """The 'detect rekordbox' step, reported without touching the install."""

    installed: bool
    live_db: FileProbeOut
    share_dir: FileProbeOut
    working_copy: FileProbeOut
    plain_copy: FileProbeOut
    key_available: bool
    key_detail: str
    import_source: str | None = None
    import_source_encrypted: bool | None = None
    blockers: list[str] = Field(default_factory=list)
    rekordbox_running: bool


class AccessProbeOut(BaseModel):
    """One folder, and whether this process can actually read it.

    ``exists`` true with ``readable`` false and ``denied`` true is the macOS
    TCC case: the folder is there and full of music, and the listing is
    refused, so anything that counted files inside it would report zero.
    """

    path: str
    exists: bool
    readable: bool
    denied: bool
    detail: str


class PermissionsOut(BaseModel):
    """The folder-access answer, and what to do about a refusal."""

    all_readable: bool
    denied: list[str] = Field(default_factory=list)
    roots: list[AccessProbeOut] = Field(default_factory=list)
    how_to_grant: str


class FolderCandidatesOut(BaseModel):
    """Existing folders under the user's home worth offering as one-click
    setup suggestions, instead of making them type a path blind."""

    candidates: list[AccessProbeOut] = Field(default_factory=list)


class LastImportOut(BaseModel):
    """What the previous rekordbox import did. Mirrors ``ImportOutcome``.

    Typed rather than a free-form object: a caller reading a track count off
    an untyped dict has no contract, and the wizard's "done" screen is built
    entirely out of these numbers.
    """

    kind: Literal["rekordbox"] = "rekordbox"
    started_at: str
    finished_at: str
    source: str
    source_was_encrypted: bool
    ingested_from: str
    tracks: int
    playlists: int
    analyses_linked: int
    analyses_expected: int
    rekordbox_tracks: int
    share_root: str
    rekordbox_was_running: bool
    #: Present on records written by this engine version. Older records have
    #: no such field, and defaulting it to [] would claim "nothing was
    #: denied" about a run that never asked -- so the wizard checks
    #: `permissions` for the live answer and treats this as history only.
    unreadable_music_roots: list[str] = Field(default_factory=list)


class FolderLastImportOut(BaseModel):
    """What the previous FOLDER import did. Mirrors ``FolderImportOutcome``.

    A different model rather than optional fields on the rekordbox one,
    because the two describe different work: there is no decrypt here, no
    playlists, and -- the field that matters --
    ``tracks_without_analysis``, which equals the tracks written.
    """

    kind: Literal["folder"] = "folder"
    started_at: str
    finished_at: str
    roots: list[str] = Field(default_factory=list)
    unreadable_roots: list[str] = Field(default_factory=list)
    files_seen: int
    files_dataless: int
    files_without_tags: int
    files_rejected_unplayable: int = 0
    tracks: int
    tracks_written: int
    tracks_without_analysis: int
    analysis_available: bool = False
    analysis_detail: str


class FolderWatchOut(BaseModel):
    """LIBM-128: the continuous folder-rescan scheduler's own status.

    ``None`` on ``SetupStatusOut.folder_watch`` means the scheduler has not
    run in this process (no lifespan, or not yet its first cycle) -- a
    genuinely different fact from a scheduler that ran and found nothing,
    which is ``warning=None`` with a real ``last_cycle_at``.
    """

    running: bool
    interval_s: float
    last_cycle_at: str | None = None
    consecutive_failures: int = 0
    #: The one required "surfaced warning" channel for an unreadable
    #: configured folder or a refused mass-missing scan (LIBM-41). Never
    #: cleared silently: it re-populates every cycle the condition persists.
    warning: str | None = None
    unreadable_roots: list[str] = Field(default_factory=list)
    tracks_added_last_cycle: int = 0
    tracks_removed_last_cycle: int = 0


class SetupStatusOut(BaseModel):
    """Everything the wizard needs to decide whether to show itself."""

    library_empty: bool
    tracks: int
    playlists: int
    state_db: FileProbeOut
    data_dir: str
    dismissed: bool
    should_show_wizard: bool
    #: True when this engine is running out of a git checkout rather than a
    #: packaged build -- ``/api/v1/build-info`` reports ``source == "repo"``.
    #: A developer working with a fresh ``--data-dir`` has an empty library
    #: on every boot and must not be thrown at the wizard for it, so this
    #: forces ``should_show_wizard`` false.
    #:
    #: Reported rather than merely applied: without it, "no wizard on an
    #: empty library" is indistinguishable from a dismissal or a broken gate,
    #: and the bug report reads "setup never appears". /setup stays reachable
    #: by hand either way.
    dev_mode: bool
    #: The rekordbox import's stages, in order.
    stages: list[str]
    #: The folder import's stages. Shorter on purpose: no snapshot and no
    #: decrypt, because there is no rekordbox database in that path.
    folder_stages: list[str]
    last_import: LastImportOut | FolderLastImportOut | None = Field(
        default=None, discriminator="kind"
    )
    rekordbox: RekordboxDetectionOut
    #: Folder access, inlined so the wizard's first render already knows
    #: whether a count of zero means "empty" or "not allowed to look".
    permissions: PermissionsOut
    #: The continuous folder-rescan scheduler's own status (LIBM-128).
    #: ``None`` when this process has no such scheduler running yet.
    folder_watch: FolderWatchOut | None = None


class SetupImportIn(BaseModel):
    """Import options. Both are the same knobs the CLI exposes."""

    source: str | None = Field(
        default=None,
        description=(
            "explicit rekordbox database to read; omit to auto-detect"
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="ingest at most N tracks (smoke-test aid)",
    )
    refresh_decrypt: bool = Field(
        default=False,
        description=(
            "re-decrypt the encrypted snapshot instead of reusing an "
            "existing master.plain.db; the wizard's half of the ingest-rb "
            "CLI's --refresh-decrypt"
        ),
    )


class FolderImportIn(BaseModel):
    """Point at one or more folders of audio files. No rekordbox involved."""

    folders: list[str] = Field(
        min_length=1,
        description="absolute paths to walk; at least one",
    )
    limit: int | None = Field(
        default=None, ge=1, description="import at most N files"
    )

    @field_validator("folders")
    @classmethod
    def folders_are_absolute_paths(cls, value: list[str]) -> list[str]:
        # Rejected here, at the wire boundary, rather than left to fall through
        # to the access probe (wrong verdict: "not found") or the job-payload
        # builder (wrong layer: a 400 several calls deep). ``~`` is not
        # expanded on this path -- the import job walks the literal string --
        # so a leading ``~`` is refused too, not treated as a convenience.
        normalized: list[str] = []
        seen: set[str] = set()
        for entry in value:
            if not entry or not Path(entry).is_absolute():
                raise ValueError(f"folder must be an absolute path, got {entry!r}")
            canon = normalize_setup_folder_path(entry)
            if canon in seen:
                raise ValueError(
                    f"duplicate folder path after normalization: {canon!r}"
                )
            seen.add(canon)
            normalized.append(canon)
        return normalized


class FolderScanOut(BaseModel):
    """What a candidate folder actually holds, before anything is imported.

    ``audio_files`` counts what could be READ. When ``denied`` is true that
    number is not a count of the folder, it is a count of nothing, and
    ``detail`` says so -- which is the difference between "this folder is
    empty" and "macOS would not let me look".
    """

    path: str
    exists: bool
    readable: bool
    denied: bool
    detail: str
    audio_files: int
    icloud_placeholders: int
    how_to_grant: str
    sample: list[str] = Field(default_factory=list)


class SetupDismissIn(BaseModel):
    dismissed: bool = True


class StemTierOut(BaseModel):
    """One real separation rung, straight out of apps.stems.tiers."""

    key: str
    name: str
    where: str
    availability: str
    unavailable_because: str


class StemsSetupOut(BaseModel):
    """Whether a library-wide separation pass can be started here.

    The agent-facing half of the wizard's stems step. The browser mounts
    af--stems-modal's StemsPrompt component, which asks
    ``/api/v1/stems/plan`` itself; an agent asks this instead, gets the same
    verdict, and is told exactly which two calls drive the flow.

    ``available`` is EVIDENCE, not a constant: it reports whether the
    ``stems.separate`` job kind is actually registered in this engine. The
    ``tiers`` list is real data either way, never a placeholder.
    """

    available: bool
    reason: str
    job_kind: str
    plan_endpoint: str
    enqueue_endpoint: str
    per_track_endpoint: str
    tiers: list[StemTierOut]


__all__ = [
    "AccessProbeOut",
    "FileProbeOut",
    "FolderCandidatesOut",
    "FolderImportIn",
    "FolderLastImportOut",
    "FolderScanOut",
    "FolderWatchOut",
    "LastImportOut",
    "PermissionsOut",
    "RekordboxDetectionOut",
    "SetupDismissIn",
    "SetupImportIn",
    "SetupStatusOut",
    "StemTierOut",
    "StemsSetupOut",
    "normalize_setup_folder_path",
]
