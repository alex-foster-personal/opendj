"""Build the webkit performance-suite fixture library. No mocked data.

The webkit suite drives the PRODUCTION build served by the engine daemon, so
it needs a library the engine can actually serve. It cannot use the lane data
dir (one engine holds a singleton lock on it) and it must never touch the
primary checkout's ``data/``. This module builds a throwaway one instead:

1. Real audio is GENERATED here (stdlib ``wave`` only): a stereo pulse train
   at a known BPM under a strong sustained tone, so decode, playback,
   position measurement AND spectral measurement all have real signal to
   work on. See "spectral landmark" below for why the tone is where it is.
2. The library rows are written by the REAL folder ingest
   (``python -m apps.shared.state.cli ingest-folder``), not by hand. That
   adapter reads tags and writes NO bpm, NO key and NO beatgrid, and says so.
   The engine has a first-class path for exactly those rows
   (``local_anlz_payload``: "a deck can load and play with no grid").

What that honestly leaves OUT is named rather than faked: these tracks have no
rekordbox vendor mapping, so ``/anlz`` serves a payload whose beatgrid, cues
and phrases are all empty. Since Tue 1 Sep 2026 its WAVEFORM is not: those
peaks come from our own ffmpeg decode of the fixture audio (PARITY-08), so a
lane drawn here describes the real tone. The performance rescue path
(``--seed-rescue-playback``) runs the production ``apps.analysis`` librosa
backend over the generated accented audio and serves measured downbeats through
``/beatgrid-fallback``; it never writes a synthetic grid.

SPECTRAL LANDMARK. The tempo, key and master-tempo tests assert the effect on
the AUDIO, read back through the app's own post-processor AnalyserNode. That
analyser is 4096-point at 44.1 kHz, so its bins are ~10.8 Hz wide. The tone
therefore sits at 2 kHz: a +-16% tempo move shifts it ~30 bins and a
one-semitone key nudge ~11 bins, which is an unambiguous read. The original
220 Hz bed moved only ~1.6 bins under the same change, which is too fragile to
assert on honestly, so it was raised rather than asserted loosely.

Idempotent: re-running with an existing, verified fixture is a no-op, because
Playwright evaluates its config (and this builder) once per process. The
generated audio is versioned by ``FIXTURE_REVISION``: bumping it wipes and
regenerates a stale fixture dir instead of silently measuring old audio.

``feedback/`` is different: it holds RUN-scoped mutations the server writes in
response to real `feedback_mark`/pin/archive commands the specs dispatch, not
cached content keyed by revision, so every call here wipes it unconditionally
rather than gating it on `FIXTURE_REVISION`. Every call happens at config
import time in the Playwright MAIN process, before webServer boots the engine
and before any spec's test body runs, so this can never discard a mutation a
still-running test wrote. Workers also import the config, but only after the
engine is live on this data dir, so a config must not call this builder from a
worker (`TEST_WORKER_INDEX` set): a write here beside the live engine's own
commits fails with "database is locked" (job 109106840542).
Left alone, an interrupted `performance-feedback-card-dismiss.spec.ts` run
(killed after it seeds a pin, before its `finally` archives it) would leave
that pin behind forever, and the next run's `button.fb-pin[title^=...]`
locator would match it plus the new one, breaking Playwright's strict-mode
single-element assumption (Codex P2 BLOCKING, #1628).

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.deckload_fixture \
        --data-dir /abs/path/to/tests/e2e/fixtures/deckload-data --seed-playlists

``--manifest <abs path>`` writes a machine-readable JSON manifest (revision +
one row per track) alongside the human-readable stdout `main()` already
printed. Purely additive: no existing caller passes it, so nothing about the
5 other e2e gates sharing this builder changes shape.

``--seed-autoplay-chain`` is a SEPARATE opt-in, used only by the performance
suite. It layers a 3rd real track onto the base 2-track library (own
function, own playlist) and tags all three tracks with the SAME Camelot key
and BPM through the real ``StateWriter.set_field`` path (source="manual",
exactly how a manual tag edit lands in production) so AutoPlay's real
key/BPM compatibility gate has real, non-null data to walk instead of the
folder-ingest rows (which the module docstring above already documents as
carrying no bpm/key/beatgrid). It is deliberately its own function rather
than a change to ``build()``: the 2-track shape ``build()`` produces is
shared with playwright.webkit-deckload/preflight-gate/boot-burst/
hotcue-mapping-gate/comment-hotkey-gate configs, none of which pass this
flag, so their fixture is byte-for-byte unchanged.

``--seed-autoplay-hunt`` is another SEPARATE opt-in, used only by the
AutoPlay/mixing error hunt (#1853). It builds a 6-track library (own
function, own two playlists, own filenames) and is mutually exclusive with
``--seed-autoplay-chain`` so a caller cannot accidentally enlarge the
performance suite. Hunt files are new; ``FIXTURE_TRACKS`` and the chain
audio are unchanged, and ``FIXTURE_REVISION`` is not bumped.

Acceptance tests:

- [if] the ingest writes zero tracks [then] the builder exits non-zero.
- [if] a generated wav is header-only (no PCM frames) [then] it exits non-zero.
- [if] --data-dir is relative [then] it exits non-zero before writing anything.
- [if] the dir was built by an older FIXTURE_REVISION [then] the audio and the
  state db are wiped and rebuilt, never reused.
- [if] --seed-playlists is set [then] one populated and one empty playlist are
  written through StateWriter, never direct SQLite inserts.
- [if] --seed-autoplay-chain is set [then] a 3rd track is ingested and all
  three tracks carry the same real, non-null Camelot key, while each keeps its
  own declared BPM (128 / 124 / 128) so the distinct-BPM pair survives.
- [if] --seed-autoplay-hunt is set [then] 6 tracks are ingested into two
  playlists (A in declared order, B the reverse), all carrying Camelot key 8A
  and alternating 128 / 124 BPM, and ``FIXTURE_TRACKS`` stays a 2-track tuple.
- [if] --manifest is set [then] the written JSON's revision and track rows
  match what was actually ingested, read back from the manifest file itself.
- [if] ``feedback/`` holds a stale file from a prior (or interrupted) run
  [then] the next build call removes it, matching FIXTURE_REVISION or not.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.webui.frontend.tests.e2e.support.deckload_autoplay import (
    build_autoplay_chain,
    build_autoplay_hunt,
)
from apps.webui.frontend.tests.e2e.support.deckload_fixture_analysis import (
    OWN_BEATGRID_BACKEND,
    OWN_BEATGRID_WEIGHTS_ENV,
    _assert_fixture_bpms,
    _run_own_beatgrid_analysis,
    _sid_for_filename,
)
from apps.webui.frontend.tests.e2e.support.deckload_fixture_artwork import wav_id3_pictures
from apps.webui.frontend.tests.e2e.support.deckload_fixture_audio import (  # noqa: F401
    _backbeat_value,
    _noise,
    ensure_audio,
)
from apps.webui.frontend.tests.e2e.support.deckload_fixture_core import (
    _discard_stale_revision,
    _ingest_and_verify,
    _reset_feedback_dir,
    _seed_playlists,
    _state_cli,
    _track_rows,
)
from apps.webui.frontend.tests.e2e.support.deckload_fixture_model import (  # noqa: F401
    AUDIO_SUBDIR,
    ARTWORK_PNG_SHA256,
    ARTWORK_SOURCE_PNG,
    AUTOPLAY_CHAIN_CAMELOT_KEY,
    AUTOPLAY_CHAIN_PLAYLIST_ID,
    AUTOPLAY_CHAIN_PLAYLIST_NAME,
    AUTOPLAY_CHAIN_TRACK,
    EMPTY_PLAYLIST_ID,
    FEEDBACK_SUBDIR,
    FIXTURE_REVISION,
    FIXTURE_TRACKS,
    HUNT_CAMELOT_KEY,
    HUNT_PLAYLIST_A_ID,
    HUNT_PLAYLIST_A_NAME,
    HUNT_PLAYLIST_B_ID,
    HUNT_PLAYLIST_B_NAME,
    HUNT_TRACKS,
    KICK_MS,
    MANIFEST_FILENAME,
    PAIRING_ARROW_TRACKS,
    PATTERN_BACKBEAT,
    PERFORMANCE_FOLD_TRACK,
    POPULATED_PLAYLIST_ID,
    SAMPLE_RATE_HZ,
    REPOSITORY_ROOT,
    RESCUE_PLAYBACK_ARTWORK_TRACK,
    RESCUE_PLAYBACK_LIBRARY_TRACKS,
    RESCUE_PLAYBACK_TRACKS,
    FixtureTrack,
)
from apps.webui.frontend.tests.e2e.support.deckload_rescue import build_rescue_playback

def write_manifest(
    manifest_path: Path,
    rows: list[tuple[str, str | None, str | None]],
    *,
    autoplay_chain_playlist_id: str | None = None,
    autoplay_chain_playlist_name: str | None = None,
    autoplay_hunt_playlist_a_id: str | None = None,
    autoplay_hunt_playlist_a_name: str | None = None,
    autoplay_hunt_playlist_b_id: str | None = None,
    autoplay_hunt_playlist_b_name: str | None = None,
) -> None:
    """Write the machine-readable sibling of ``main()``'s stdout listing.

    Consumed by ``tests/e2e/support/fixture-manifest.ts``. Fails loudly
    rather than writing a partial file: an empty ``rows`` here would make a
    reader believe the fixture built successfully with zero tracks.
    """
    if not rows:
        raise SystemExit("[ERROR] refusing to write a manifest with zero tracks")
    payload = {
        "fixture_revision": FIXTURE_REVISION,
        "populated_playlist_id": POPULATED_PLAYLIST_ID,
        "empty_playlist_id": EMPTY_PLAYLIST_ID,
        "autoplay_chain_playlist_id": autoplay_chain_playlist_id,
        "autoplay_chain_playlist_name": autoplay_chain_playlist_name,
        "autoplay_hunt_playlist_a_id": autoplay_hunt_playlist_a_id,
        "autoplay_hunt_playlist_a_name": autoplay_hunt_playlist_a_name,
        "autoplay_hunt_playlist_b_id": autoplay_hunt_playlist_b_id,
        "autoplay_hunt_playlist_b_name": autoplay_hunt_playlist_b_name,
        "tracks": [
            {"stable_id": stable_id, "title": title, "file_path": file_path}
            for stable_id, title, file_path in rows
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_pairing_arrow(
    data_dir: Path,
) -> list[tuple[str, str | None, str | None]]:
    """Build the pairing-arrow library: the 2 deck tracks plus a long browse list.

    Own function, like ``build_autoplay_hunt``: ``build()`` and its 2-track shape
    stay untouched for the five other gates. Playwright re-imports its config
    more than once per run, so this tolerates being called against its own
    finished output.
    """
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir, PAIRING_ARROW_TRACKS)
    rows = _ingest_and_verify(data_dir, audio_dir, files, "pairing-arrow")
    # Raises StopIteration if the master file was not ingested.
    _sid_for_filename(rows, FIXTURE_TRACKS[0].filename)
    return rows


def build(
    data_dir: Path, *, seed_playlists: bool = False
) -> list[tuple[str, str | None, str | None]]:
    """Generate the audio, run the real ingest, verify the result."""
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir)
    rows = _ingest_and_verify(data_dir, audio_dir, files, "fixture")
    if seed_playlists:
        _seed_playlists(data_dir / "state" / "state.db", rows)
    return rows
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckload_fixture",
        description="Build the webkit performance-suite fixture library.",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="absolute path of the throwaway fixture data dir",
    )
    parser.add_argument(
        "--seed-playlists",
        action="store_true",
        help="seed browser-addressable populated and empty playlists",
    )
    parser.add_argument(
        "--seed-autoplay-chain",
        action="store_true",
        help=(
            "add a 3rd, key/BPM-tagged track and its own AutoPlay-chain "
            "playlist (performance suite only; implies --seed-playlists)"
        ),
    )
    parser.add_argument(
        "--seed-autoplay-hunt",
        action="store_true",
        help=(
            "build a 6-track hunt library with two playlists (error-hunt "
            "suite only; implies --seed-playlists; exclusive with "
            "--seed-autoplay-chain)"
        ),
    )
    parser.add_argument(
        "--seed-rescue-playback",
        action="store_true",
        help=(
            "autoplay-chain library plus a 64 bpm fold track, analyzed through "
            "the production librosa backend for rescue playback e2e "
            "(performance suite only)"
        ),
    )
    parser.add_argument(
        "--seed-own-beatgrid",
        action="store_true",
        help=(
            "with --seed-rescue-playback: also run the production own beatgrid "
            "producer (Beat This!), so /anlz serves a measured own grid; needs "
            f"{OWN_BEATGRID_WEIGHTS_ENV} and ffmpeg"
        ),
    )
    parser.add_argument(
        "--seed-pairing-arrow",
        action="store_true",
        help=(
            "2 deck tracks plus a 24-track browse list, so a paired partner can "
            "sit off screen (pairing-arrow e2e only; exclusive with the other "
            "--seed-* library shapes)"
        ),
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="absolute path to write a machine-readable JSON manifest",
    )
    args = parser.parse_args(argv)
    if sum(
        int(flag)
        for flag in (
            args.seed_autoplay_chain,
            args.seed_autoplay_hunt,
            args.seed_rescue_playback,
            args.seed_pairing_arrow,
        )
    ) > 1:
        raise SystemExit(
            "[ERROR] --seed-autoplay-chain, --seed-autoplay-hunt, "
            "--seed-rescue-playback, and --seed-pairing-arrow are mutually exclusive"
        )
    if args.seed_own_beatgrid and not args.seed_rescue_playback:
        raise SystemExit("[ERROR] --seed-own-beatgrid requires --seed-rescue-playback")
    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}")
    data_dir.mkdir(parents=True, exist_ok=True)
    autoplay_chain_playlist_id: str | None = None
    autoplay_chain_playlist_name: str | None = None
    autoplay_hunt_playlist_a_id: str | None = None
    autoplay_hunt_playlist_a_name: str | None = None
    autoplay_hunt_playlist_b_id: str | None = None
    autoplay_hunt_playlist_b_name: str | None = None
    if args.seed_rescue_playback:
        rows = build_rescue_playback(data_dir, own_beatgrid=args.seed_own_beatgrid)
        autoplay_chain_playlist_id = AUTOPLAY_CHAIN_PLAYLIST_ID
        autoplay_chain_playlist_name = AUTOPLAY_CHAIN_PLAYLIST_NAME
    elif args.seed_autoplay_hunt:
        rows = build_autoplay_hunt(data_dir)
        autoplay_hunt_playlist_a_id = HUNT_PLAYLIST_A_ID
        autoplay_hunt_playlist_a_name = HUNT_PLAYLIST_A_NAME
        autoplay_hunt_playlist_b_id = HUNT_PLAYLIST_B_ID
        autoplay_hunt_playlist_b_name = HUNT_PLAYLIST_B_NAME
    elif args.seed_pairing_arrow:
        rows = build_pairing_arrow(data_dir)
    elif args.seed_autoplay_chain:
        rows = build_autoplay_chain(data_dir)
        autoplay_chain_playlist_id = AUTOPLAY_CHAIN_PLAYLIST_ID
        autoplay_chain_playlist_name = AUTOPLAY_CHAIN_PLAYLIST_NAME
    else:
        rows = build(data_dir, seed_playlists=args.seed_playlists)
    print(f"[OK] fixture library at {data_dir} with {len(rows)} track(s):")
    for stable_id, title, file_path in rows:
        print(f"  {stable_id}  {title!r}  {file_path}")
    if args.manifest is not None:
        manifest_path = Path(args.manifest).expanduser()
        if not manifest_path.is_absolute():
            raise SystemExit(f"[ERROR] --manifest must be absolute, got {args.manifest!r}")
        write_manifest(
            manifest_path,
            rows,
            autoplay_chain_playlist_id=autoplay_chain_playlist_id,
            autoplay_chain_playlist_name=autoplay_chain_playlist_name,
            autoplay_hunt_playlist_a_id=autoplay_hunt_playlist_a_id,
            autoplay_hunt_playlist_a_name=autoplay_hunt_playlist_a_name,
            autoplay_hunt_playlist_b_id=autoplay_hunt_playlist_b_id,
            autoplay_hunt_playlist_b_name=autoplay_hunt_playlist_b_name,
        )
        print(f"[OK] manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
