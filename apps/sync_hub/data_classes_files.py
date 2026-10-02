"""Data classes that are not database tables, plus the ``.gitignore`` map.

``<data>`` is the data dir (``MDT_DATA_DIR``: the repo's ``data/`` or the
packaged app's support dir); ``<repo>`` is the git checkout. Every
``.gitignore`` pattern is classified in :data:`IGNORED_PATHS`, and a test
parses the real ``.gitignore`` so a new pattern cannot land unclassified.
"""

from __future__ import annotations

from apps.shared.state.migrations_v10 import ASSET_KIND_CHECK_VALUES
from apps.sync_hub.data_classes_types import (
    POLICY_MODES,
    DataClass,
    Dependency,
    IgnoredKind,
    IgnoredPath,
    Location,
    Mechanism,
    files,
    fixed,
    logical,
)

_R2_KEY = Location("r2", "assets/<sha256[:2]>/<sha256>")
_POLICY_INERT = (
    "The mode is stored and synced, but no production producer or reader "
    "applies it yet: resolve_playback_source, apply_policy_after_produce and "
    "evict_cache are reached only from tests."
)


def _asset(
    asset_kind: str,
    title: str,
    storage: tuple[Location, ...],
    reason: str,
    depends_on: tuple[Dependency, ...],
) -> DataClass:
    """A per-machine configurable R2 asset-tier class keyed by ``asset_kind``."""
    if asset_kind not in ASSET_KIND_CHECK_VALUES:
        raise ValueError(f"{asset_kind!r} is not a sync_policies.asset_kind CHECK value")
    return DataClass(
        id=asset_kind.replace("_", "-"),
        title=title,
        storage=storage,
        mechanism="r2_asset_tier",
        configurable_per_machine=True,
        allowed_modes=POLICY_MODES,
        depends_on=depends_on,
        reason=reason,
        asset_kind=asset_kind,
    )


ASSET_CLASSES: tuple[DataClass, ...] = (
    _asset(
        "audio",
        "Audio files",
        (
            Location("file", "user music roots (track_locations.file_path)"),
            Location("file", "<data>/state/audio-cache/<sha256[:2]>/<sha256>"),
            _R2_KEY,
        ),
        "The largest asset; each machine picks pinned for gigs or stream to "
        "save disk. Outside the policy, bytes also move by optional Syncthing "
        f"and crate_sync rsync to agentbox. {_POLICY_INERT}",
        (logical("track-locations"),),
    ),
    _asset(
        "stem_bundle",
        "Stem bundles",
        (Location("file", "<data>/state/stems/<stable_id>/"), _R2_KEY),
        "Expensive to compute (GPU minutes per track), so at least one durable "
        "copy must exist. legacy_stems_migration.py writes R2 outside the policy "
        f"layer. {_POLICY_INERT}",
        (logical("library-tracks"),),
    ),
    _asset(
        "anlz_cache",
        "Rekordbox ANLZ analysis cache",
        (Location("file", "<data>/state/anlz-cache/<stable_id>.json"), _R2_KEY),
        "Parsed from the local rekordbox analysis; regenerable (refresh = "
        f"rm -rf the dir). {_POLICY_INERT}",
        (logical("rekordbox-plain-db"),),
    ),
    _asset(
        "vocal_cache",
        "Vocal presence cache",
        (Location("file", "<data>/state/vocal-cache/<stable_id>.json"), _R2_KEY),
        f"Derived from stem bundles (python -m apps.vocals from-stems). {_POLICY_INERT}",
        (logical("stem-bundle"),),
    ),
    _asset(
        "lyrics_cache",
        "Fetched lyrics cache",
        (Location("file", "<data>/state/lyrics-cache/<stable_id>.json"), _R2_KEY),
        f"Line-synced lyrics fetched per track (LYRICS-01). {_POLICY_INERT}",
        (logical("library-tracks"),),
    ),
    _asset(
        "karaoke_words",
        "Karaoke word timings",
        (Location("file", "<data>/state/karaoke-cache/<stable_id>.json"), _R2_KEY),
        "The one asset kind whose producer is wired: apps/lyrics/artifacts.py "
        "pushes it through push_asset under the policy.",
        (logical("lyric-verdicts"), logical("lyrics-cache")),
    ),
)

_LOCAL: Mechanism = "machine_local"

FILE_CLASSES: tuple[DataClass, ...] = (
    fixed(
        "data-dir",
        "The data dir as a whole",
        files("<data>/", "<data>/state/state.db", "<data>/state/cache.db"),
        _LOCAL,
        "Container for the classes in this doc; it is never copied as a whole. "
        "Its state.db rows move table by table (see above).",
        (),
    ),
    fixed(
        "rekordbox-plain-db",
        "Decrypted rekordbox working copy",
        files("<data>/master.plain.db", "<data>/master.db.copy"),
        _LOCAL,
        "Regenerate locally by re-decrypting each machine's own rekordbox "
        "(specs/cloudsync-spec.md); never sync a vendor DB copy.",
        (),
    ),
    fixed(
        "cue-points",
        "Hot cues, memory cues and loops",
        files("<data>/state/state.db (track_fields row 'cue_points')"),
        "sync_hub_changelog",
        "Open DJ's own cue store (CUES-01): one provenance-wrapped track_fields "
        "row per track, so cues move with the track-fields class. Rekordbox "
        "cues are copied in at import; djmdCue itself stays machine-local.",
        (logical("track-fields"), logical("library-tracks")),
    ),
    fixed(
        "vendor-working-copies",
        "Other vendor DB copies and XML exports",
        files("<data>/djay_MediaLibrary.db.copy", "<repo>/**/*.xml"),
        _LOCAL,
        "Point-in-time copies of vendor libraries; each machine reads its own vendor.",
        (),
    ),
    fixed(
        "waveform-cache",
        "Waveform render cache",
        files("<data>/state/local-waveform-cache/"),
        _LOCAL,
        "Regenerable render cache with no asset kind (rekordbox/config.py).",
        (logical("audio"),),
    ),
    fixed(
        "analysis-file-caches",
        "Analysis and index file caches",
        files(
            "<data>/state/beatgrid-issue-cache/",
            "<data>/analysis/",
            "<data>/state/disk-audio-index.json",
            "<data>/state/lyrics-index.db",
            "<data>/sync/fingerprints.sqlite",
            "<data>/launcher-bootstrap.sqlite",
        ),
        _LOCAL,
        "Derived from local files and cheap to rebuild; no asset kind.",
        (logical("audio"),),
    ),
    fixed(
        "structure-and-genre-caches",
        "Song structure sidecars and genre suggestions",
        files(
            "<data>/state/structure-cache/<stable_id>.json",
            "<data>/state/genre/",
        ),
        _LOCAL,
        "Model output derived from local audio plus the library's own tags "
        "(apps/analysis_structure, apps/genre_infer); regenerable, no asset kind "
        "yet. Structure is GPU-minutes per track on a CPU box, so an asset kind "
        "is the follow-up once the lane is promoted (ADR-NEW-structure-and-genre-sidecars).",
        (logical("audio"), logical("engine-analysis")),
    ),
    fixed(
        "stem-experiments",
        "Stem separation experiments",
        files(
            "<data>/state/stems-roformer-spike*/",
            "<data>/state/stems-demucs-ab/",
            "<repo>/scripts/bench/clips*/",
        ),
        _LOCAL,
        "Spike and benchmark output; durable copies live on Modal volumes.",
        (),
    ),
    fixed(
        "model-weights",
        "Downloaded model weights and reference data",
        files(
            "<data>/weights/",
            "<data>/reference/",
            "<repo>/apps/voice/models/",
            "<repo>/vendor/whisper.cpp/",
        ),
        _LOCAL,
        "Re-downloadable per machine; not user-authored.",
        (),
    ),
    fixed(
        "settings-and-preferences",
        "UI, feature and ingest preferences",
        (
            *files(
                "<data>/state/ui-prefs.json",
                "<data>/feature-flags.json",
                "<data>/state/ingest-config.json",
                "<data>/state/lyrics-config.json",
                "<data>/path-map.json",
                "<repo>/data/voice/settings.sqlite",
            ),
            Location("external", "browser localStorage (frontend)"),
        ),
        _LOCAL,
        "Machine-local on purpose (specs/cloudsync-spec.md): screen size, paths "
        "and devices differ per machine. The localStorage keys are not inventoried.",
        (),
    ),
    fixed(
        "cloudsync-file-policy",
        "File-based CloudSync entitlement mode",
        files("<data>/state/cloudsync-policy.json"),
        _LOCAL,
        "apps/cloud/policy.py cloud|local mode (MUSIC_DJ_CLOUDSYNC_MODE "
        "overrides). Its machine-class defaults become seed input only under "
        "the pending authority decision (see the doc).",
        (),
    ),
    fixed(
        "external-oauth-tokens",
        "Third-party OAuth tokens",
        files("~/.music-dj-tools/spotify-token.json"),
        _LOCAL,
        "A credential: never leaves the machine. Outside the repo on purpose.",
        (),
    ),
    fixed(
        "machine-id",
        "Machine identity",
        files("<data>/machine-id"),
        _LOCAL,
        "Minted once, stored outside the DB (0600) so a restore onto new "
        "hardware cannot inherit the old identity (machine_identity.py).",
        (),
    ),
    fixed(
        "secrets",
        "Secrets and credentials",
        (Location("external", "Doppler (agent-secrets workplace)"), *files("<repo>/.env")),
        _LOCAL,
        "openDJ never syncs a secret. Doppler is the secrets authority; .env is "
        "the only file .worktreeinclude copies into a new worktree.",
        (),
    ),
    fixed(
        "logs-and-journals",
        "Logs and status journals",
        files(
            "<repo>/ops/logs/",
            "<data>/logs/",
            "<repo>/data/voice/*.log",
            "<repo>/data/voice/*.jsonl",
            "<data>/cloudsync-status.json",
            "<data>/hub-generation.json",
            "<data>/state/health*.jsonl",
            "<data>/state/history.jsonl",
        ),
        _LOCAL,
        "Per-machine diagnostics; each describes the machine that wrote it.",
        (),
    ),
    fixed(
        "engine-runtime",
        "Engine lock and job queue",
        files("<data>/.engine.lock", "<data>/state/jobs.db"),
        _LOCAL,
        "Singleton lock and in-flight work of the engine running HERE.",
        (),
    ),
    fixed(
        "feedback-board",
        "In-app feedback pins and attachments",
        files("<data>/feedback/"),
        _LOCAL,
        "Harvested per machine by `just` feedback harvest, which reads each "
        "machine over its API rather than syncing the files.",
        (),
    ),
    fixed(
        "usb-export-state",
        "USB export profiles, hashes and traces",
        files(
            "<data>/usb/",
            "<repo>/usb-profiles/local/",
            "<repo>/apps/sync/usb/pioneer/traces/",
        ),
        _LOCAL,
        "Volume labels, UUIDs and screen traces of drives attached here.",
        (),
    ),
    fixed(
        "operation-journals-and-backups",
        "Import, reconcile and writeback journals",
        files(
            "<data>/spotify/",
            "<data>/dedup/",
            "<data>/tags/",
            "<data>/reconcile/",
            "<data>/writeback-backups/",
            "<data>/bench/",
            "<data>/backup/",
        ),
        _LOCAL,
        "Reversal scripts and backups of operations run on THIS machine's "
        "vendor libraries; they only replay against that machine.",
        (),
    ),
    fixed(
        "vendor-documentation",
        "Vendor manuals kept locally",
        files(
            "<repo>/docs/controller/reference/*.pdf",
            "<repo>/tools/deck-diagrams/devices/*/source/",
        ),
        _LOCAL,
        "Manufacturer copyright: may be kept locally, never redistributed.",
        (),
    ),
    fixed(
        "git-repo",
        "The code repository",
        (Location("git", "<repo>"),),
        "git",
        "autoreposync fast-forwards origin/main on every enabled machine every "
        "120s. All user data is gitignored, so git moves code only.",
        (),
    ),
    fixed(
        "litestream-replica",
        "Whole state.db replica to R2",
        (Location("r2", "apps/cloud/litestream.yml target"),),
        "not_yet_built",
        "Configured but not running; hard-codes data/state/state.db (ignores "
        "MDT_DATA_DIR) and would ship auth_sessions tokens. Owner decision D4f.",
        (logical("data-dir"),),
    ),
)

NON_TABLE_CLASSES: tuple[DataClass, ...] = (*ASSET_CLASSES, *FILE_CLASSES)


# ----- .gitignore classification ---------------------------------------------


def _ignored(kind: IgnoredKind, data_class: str | None, *patterns: str) -> tuple[IgnoredPath, ...]:
    """Rows for ``patterns`` sharing one classification."""
    return tuple(IgnoredPath(p, kind, data_class) for p in patterns)


IGNORED_PATHS: tuple[IgnoredPath, ...] = (
    *_ignored(
        "user_data",
        "data-dir",
        "data/*",
        "*.db",
        "*.db-wal",
        "*.db-shm",
        "*.sqlite",
        "*.sqlite-wal",
        "*.sqlite-shm",
    ),
    *_ignored("user_data", "vendor-working-copies", "*.xml"),
    *_ignored("user_data", "operation-journals-and-backups", "data/spotify/"),
    *_ignored(
        "user_data", "logs-and-journals", "data/voice/*.log", "data/voice/*.jsonl", "ops/logs/"
    ),
    *_ignored("user_data", "settings-and-preferences", "data/voice/settings.sqlite*"),
    *_ignored(
        "user_data",
        "usb-export-state",
        "usb-profiles/local/",
        "data/usb/",
        "apps/sync/usb/pioneer/traces/",
    ),
    *_ignored(
        "user_data",
        "secrets",
        ".env",
        "apps/shared/bundled_google_oauth.json",
        "*.pem",
        "*.key",
        "*.p12",
        "*.p8",
        "*.mobileprovision",
        "credentials.json",
        "service-account*.json",
    ),
    *_ignored(
        "user_data",
        "stem-experiments",
        "scripts/bench/clips/",
        "scripts/bench/clips-musdb/",
        "scripts/bench/clips-4stem/",
        "scripts/bench/clips-roformer-spike/",
    ),
    *_ignored(
        "user_data",
        "model-weights",
        "apps/voice/models/*.bin",
        "apps/voice/models/*.onnx",
        "apps/voice/models/*.mlmodelc/",
        "apps/voice/models/*.ppn",
        "vendor/whisper.cpp/",
    ),
    *_ignored(
        "user_data",
        "vendor-documentation",
        "docs/controller/reference/*.pdf",
        "docs/controller/reference/*_MIDI_Message_List_*.txt",
        "tools/deck-diagrams/devices/*/source/*.pdf",
        "tools/deck-diagrams/devices/*/source/*.txt",
        "tools/deck-diagrams/devices/*/source/midi-list-page-*.png",
    ),
    *_ignored(
        "user_data",
        "rekordbox-plain-db",
        "tests/fixtures/rekordbox/_tmp_plain.db",
        "tests/fixtures/rekordbox/master.plain.db",
    ),
    *_ignored(
        "user_data",
        "usb-export-state",
        "tests/fixtures/rb-usb-export/Contents/",
        "tests/fixtures/rb-usb-export/PIONEER/extracted/",
        "tests/fixtures/rb-usb-export/",
        "tests/fixtures/rb-usb-export-onetera-20260805/",
    ),
    *_ignored(
        "user_data",
        "data-dir",
        "/Music/Recovered/",
        "/odj-private",
        "scripts/spotdl_watched.py",
        "maintainer/",
        "/refs/",
        "/blog/",
    ),
    *_ignored(
        "build_output",
        None,
        "__pycache__/",
        "*.pyc",
        ".mypy_cache/",
        ".pnpm-store/",
        ".venv/",
        "*.egg-info/",
        "/dist/",
        "/build/",
        ".pytest_cache/",
        ".coverage",
        ".coverage.*",
        "junit-shard-*.xml",  # per-shard JUnit the fast lane uploads to Trunk Flaky Tests
        "htmlcov/",
        "coverage-matrix.md",
        "/node_modules/",  # root npm install: @trunkio/launcher (npm run lint / fmt)
        "node_modules/",  # any depth: the CI runners keep every node_modules (ci_clean_untracked.sh)
        "target/",  # any depth: cargo output, which the CI runners keep as well
        "apps/webui/frontend/node_modules/",
        "apps/webui/frontend/build/",
        "apps/webui/frontend/.svelte-kit/",
        "apps/webui/frontend/.vite/",
        "/ds-bundle/",
        "/.ds-sync/",
        "apps/launcher/src-tauri/Cargo.lock",
        "ops/quality/report.md",
        "ops/quality/metrics.json",
        "ops/quality/stretch/work/",
        "ops/stable/*.json",
        "apps/webui/frontend/tests/e2e/fixtures/deckload-data/",
        "apps/webui/frontend/tests/e2e/fixtures/boot-burst-data/",
        "apps/webui/frontend/tests/e2e/fixtures/boot-burst*.json",
        "apps/webui/frontend/tests/e2e/fixtures/hotcue-mapping-gate-data/",
        "apps/webui/frontend/tests/e2e/fixtures/preflight-gate-healthy-data/",
        "apps/webui/frontend/tests/e2e/fixtures/preflight-gate-broken-data/",
        "apps/webui/frontend/tests/e2e/fixtures/root-playwright-data/",
        "apps/webui/frontend/tests/e2e/fixtures/performance-playwright-data/",
        "apps/webui/frontend/tests/e2e/fixtures/playlist-switch-latency-data/",
        "apps/webui/frontend/tests/e2e/fixtures/comment-hotkey-gate-data/",
        "apps/webui/frontend/tests/e2e/fixtures/autoplay-stall-gate-data/",
        "apps/webui/frontend/tests/e2e/fixtures/autoplay-error-hunt-data/",
        "apps/webui/frontend/tests/e2e/fixtures/stem-decode-data/",
        "apps/webui/frontend/tests/e2e/fixtures/stem-decode-bench.json",
        "apps/webui/frontend/tests/e2e/fixtures/waveform-render.json",
        "apps/webui/frontend/tests/e2e/fixtures/cloudsync-ui-hub-data/",
        "apps/webui/frontend/tests/e2e/fixtures/cloudsync-ui-spoke-data/",
        "apps/webui/frontend/tests/e2e/fixtures/lyrics-words-data/",
        "apps/webui/frontend/tests/e2e/fixtures/lyrics-words-manifest.json",
        "apps/webui/frontend/test-results/",
        "apps/desktop/mcp/.smoke-data/",
        "apps/webui/frontend/tests/unit/fixtures/machine-id",
        "/spikes/aalink-js-link/node_modules/",
        "/spikes/carabiner-link/.tools/",
        "/spikes/carabiner-link/build/",
        "/spikes/carabiner-link/upstream/",
        "/spikes/rusty-link/.cargo-home/",
        "/spikes/rusty-link/.rustup-home/",
        "/spikes/rusty-link/.tool-venv/",
        "/spikes/rusty-link/target/",
        "/_rb_waveform_native*.so",
        ".wrangler/",
    ),
    *_ignored(
        "runtime_state",
        None,
        ".tmp/",
        ".DS_Store",
        ".vscode/",
        ".idea/",
        ".planning/ci-watcher-*.txt",
        ".planning/ci-watcher-*.log",
        ".planning/.tmp-*",
        ".planning/.lock-*",
        ".planning/heartbeats/",
        ".planning/ci-fixer/",
        ".claude/worktrees/",
        ".claude/checkpoints/",
        ".claude/mailbox/",
        ".claude/routines/.state/",
        ".claude/scheduled_tasks.lock",
        ".claude/scheduled_tasks.json",
        ".claude/agent-registry.json",
        ".claude/agent-memory-local",
        ".claude/first-run",
        ".claude/assistant-daemon-state.json",
        ".claude/settings.local.json",
        ".codex/**/cache/",
        ".codex/**/*.log",
        ".cursor/**/cache/",
        ".cursor/**/*.log",
        "/runs/",
        "/.sweep-*",
        "/wt/",
        "/zTasks.md",
        "/.cccron/",
        "docs/threads/.provenance-watermark.json",
        "docs/threads/.provenance-lock",
        "docs/threads/*.tmp",
        "ops/autoreposync/",
        "ops/autoreposync/com.af.autoreposync.plist",
        ".planning/bifrost2-handoff/",
        "**/sync-kit/",
        ".codex/skills/",
        "adws/adw_data/sessions/",
        "adws/adw_data/sssf.db*",
    ),
)
"""Every non-negated ``.gitignore`` pattern, verbatim, with what it holds.

``runtime_state`` is a third bucket beside the requested user-data/build
split: agent bookkeeping, locks and editor state are neither user data nor
build output, and filing them under either would mislead a reader."""
