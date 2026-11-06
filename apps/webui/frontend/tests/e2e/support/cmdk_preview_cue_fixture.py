"""Build the throwaway library for the Cmd K cue-preview browser gate (CMDK-07..08).

No mocked data, and no hand-written grid. The Cmd K cue preview jumps by bars,
so it needs REAL measured downbeats for the highlighted track. A locally
ingested track has an empty ``/anlz`` grid (``local_anlz_payload`` leaves the
rekordbox-owned blocks empty), while the production librosa analysis is served
through ``/beatgrid-fallback``. This builder does what a real local library gets:

1. Real audio is GENERATED with the same accented pulse the deckload fixture
   uses (``deckload_fixture.write_tone_and_pulse_wav``): a loud downbeat on
   every fourth pulse, at 128 and 124 BPM, the two tempos that producer is
   measured to grid (deckload_fixture.py, "PATTERN_BACKBEAT").
2. The rows are written by the REAL folder ingest
   (``python -m apps.shared.state.cli ingest-folder``), through the same
   helper the deckload fixture uses.
3. The PRODUCTION librosa analyzer runs over that audio
   (``apps.analysis.run --backend librosa``), and every track must come back
   with measured downbeats at its generated tempo, or the build fails naming it.
4. ``cmdk.v2`` and ``cmdk.v2.preview`` are turned on in the data dir's own
   ``feature-flags.json``.

The data dir is wiped and rebuilt on every run, and only after
:func:`checked_data_dir` has accepted it (Sol r4278366173 on #6689).

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.cmdk_preview_cue_fixture \\
        --data-dir /abs/path/to/tests/e2e/fixtures/cmdk-preview-cue-data
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from apps.webui.frontend.tests.e2e.support import deckload_fixture as deckload
from apps.webui.frontend.tests.e2e.support.deckload_fixture import FixtureTrack

#: Two tracks, so the highlighted row is a choice among real rows, not the only one.
TRACKS: tuple[FixtureTrack, ...] = (
    FixtureTrack(filename="cmdk-preview-cue-a-128bpm.wav", bpm=128.0, seconds=60.0),
    FixtureTrack(filename="cmdk-preview-cue-b-124bpm.wav", bpm=124.0, seconds=60.0),
)
#: What the spec types to list them: the folder ingest titles a tagless wav by its file name.
SEARCH_QUERY: str = "cmdk-preview-cue"
AUDIO_SUBDIR: str = deckload.AUDIO_SUBDIR
#: The flags this gate needs on, written as the data dir's own override file.
FLAGS: dict[str, bool] = {"cmdk.v2": True, "cmdk.v2.preview": True}
#: The one directory this builder may wipe: the e2e fixtures dir's own cue-preview data dir,
#: which playwright.cmdk-preview-cue.config.ts passes as --data-dir.
FIXTURES_ROOT: Path = Path(__file__).resolve().parents[1] / "fixtures"
DATA_DIR_NAME: str = "cmdk-preview-cue-data"


def checked_data_dir(data_dir: Path) -> Path:
    """The data dir to rebuild, or SystemExit for any other path.

    The builder wipes it with ``shutil.rmtree``, so only the named fixture dir
    under the e2e fixtures root is accepted: a typo, ``/``, a repository or a
    home directory is refused before anything is removed.
    """
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {data_dir}")
    resolved = data_dir.resolve()
    expected = FIXTURES_ROOT / DATA_DIR_NAME
    if resolved != expected:
        raise SystemExit(f"[ERROR] --data-dir must be {expected}, got {data_dir}; refusing to wipe it")
    return resolved


def build(data_dir: Path) -> list[tuple[str, str | None, str | None]]:
    data_dir = checked_data_dir(data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    (data_dir / "feature-flags.json").write_text(json.dumps(FLAGS), encoding="utf-8")
    audio_dir = data_dir / AUDIO_SUBDIR
    files = deckload.ensure_audio(audio_dir, TRACKS)
    rows = deckload._ingest_and_verify(data_dir, audio_dir, files, "cmdk-preview-cue")
    deckload._run_librosa_analysis(data_dir, rows, TRACKS, "cmdk-preview-cue")
    for stable_id, title, _file_path in rows:
        if title is None or SEARCH_QUERY not in title:
            raise SystemExit(
                f"[ERROR] row {stable_id} is titled {title!r}, which the spec's query {SEARCH_QUERY!r} cannot find"
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cmdk_preview_cue_fixture", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True, help="absolute path of the throwaway data dir")
    parser.add_argument("--home", help="sandbox HOME the engine runs with, created here so the config needs no shell")
    args = parser.parse_args(argv)
    # The guard runs before anything else, so a refused path cannot be removed.
    checked_data_dir(Path(args.data_dir))
    if args.home:
        Path(args.home).mkdir(parents=True, exist_ok=True)
    rows = build(Path(args.data_dir))
    print(
        f"[OK] cmdk cue-preview fixture: {len(rows)} tracks with librosa fallback downbeats, flags {FLAGS}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
