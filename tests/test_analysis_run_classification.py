"""Which analyzer failures are about the FILE, and which are about the box.

``apps.analysis.run`` hands the caller an exit status, and the caller - the
chunked drain in ``apps/webui/server/routes/ingest.py`` - decides from that
status alone whether to try the next chunk. So the classification IS the
contract: a machine-wide fault wearing EXIT_TRACK_FAILURES gets met once per
chunk across the whole library.

Driven through the SHIPPED backend and its production config loader. An
earlier version of this file registered a small backend of its own that read
the config the way LibrosaBackend does; that stayed green whatever the real
setup path did, which is the failure mode this module exists to catch.

Kept out of ``tests/analysis/``, whose conftest skips the whole package
without ``soundfile``: skipping is right, but it has to be a per-test
capability report, not a silently absent module.

Regression lines:
  - if a backend setup failure is reported as a failed track then broken
  - if that failure reaches the caller as exit 1 rather than 5 then broken
  - if a read the process is denied is blamed on the file then broken
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apps.analysis import config as analysis_config
from apps.analysis import run as run_mod
from apps.analysis.backends import DEFAULT_BACKEND


@pytest.fixture()
def analyzer_config_was_never_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """Point the production config loader at a config that is not there.

    ``LibrosaBackend.analyze`` calls ``config.load_config()`` on every track,
    so a config the install never shipped is a fact about the machine that
    surfaces once per file. That is the shape this module is about, and it
    is a real deployment failure: the packaged engine builds its closure from
    ``uv export``, and a data file left out of that closure produces exactly
    this.

    Nothing about the loader is replaced. It reads a path, and the path does
    not exist, so it raises what a missing file really raises.
    """
    missing = tmp_path / "analysis" / "config.yaml"
    monkeypatch.setattr(analysis_config, "CONFIG_PATH", missing)
    analysis_config.load_config.cache_clear()
    yield missing
    analysis_config.load_config.cache_clear()


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_a_backend_setup_failure_is_not_reported_as_a_failed_track(
    analyzer_config_was_never_installed, tmp_path: Path
) -> None:
    """Only a failure ABOUT THE FILE may wear the continue-capable status.

    A broad catch around ``backend.analyze`` turns every machine-wide fault -
    an analyzer config that was never installed, a backend that could not
    initialize - into a per-track error. The CLI then exits
    EXIT_TRACK_FAILURES, which is the one status a chunking caller may
    continue past, so it meets the identical fault once per chunk across the
    whole library.

    The backend is the shipped ``librosa`` one and the loader is the shipped
    one. Neither is replaced, so this cannot stay green while the real setup
    path starts swallowing the same failure.
    """
    audio = tmp_path / "fine.mp3"
    audio.write_bytes(b"\x00" * 32)

    with pytest.raises(FileNotFoundError):
        run_mod._analyze_one("librosa", "sid00001", str(audio))


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_the_cli_exits_internal_error_when_the_backend_cannot_be_set_up(
    analyzer_config_was_never_installed, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the same fault must reach the caller as status 5, not 1.

    1 is EXIT_TRACK_FAILURES and would be continued past; 5 stops the drain.
    """
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    audio = tmp_path / "fine.mp3"
    audio.write_bytes(b"\x00" * 32)

    code = run_mod.main([
        "--backend", "librosa", "--all", "--files", str(audio),
    ])

    assert code == run_mod.EXIT_INTERNAL_ERROR, (
        f"a machine-wide setup failure exited {code}; "
        f"{run_mod.EXIT_TRACK_FAILURES} would invite a retry per chunk"
    )


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_bytes_every_installed_decoder_rejects_are_blamed_on_the_file(
    tmp_path: Path,
) -> None:
    """One unplayable file must not stop the queue behind it.

    Measured Tue 2 Sep 2026: garbage named ``.mp3`` comes out of
    ``librosa.load`` as ``audioread.NoBackendError``, a name that reads
    systemic. It is reached only after libsndfile has already rejected the
    bytes, so it must still arrive as ``TrackUnreadable`` and let the drain
    move on - on the ffmpeg-less CI runner as much as on a full box, which is
    the case that caught a first attempt at this classification.
    """
    from apps.analysis.backends.base import TrackUnreadable
    from apps.analysis.backends.librosa import LibrosaBackend

    junk = tmp_path / "notaudio.mp3"
    junk.write_bytes(b"this is not audio" * 40)

    with pytest.raises(TrackUnreadable):
        LibrosaBackend.analyze(junk, "sid00002")


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_a_misconfigured_sample_rate_is_not_blamed_on_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bad config value fails INSIDE the decode, and is still not per-file.

    This is the case the allow-list exists for. ``analyzer.sample_rate_hz``
    is read from the shipped config and handed to ``librosa.load``, so a zero
    there raises ``ValueError`` from inside the one call the backend is
    allowed to blame on the input - on a file that is perfectly good audio,
    and identically on every other file in the library. A catch that keys on
    WHERE the exception came from rather than WHAT it is reports the whole
    library as corrupt, one chunk at a time.

    Real end to end: the production config file with one value replaced, the
    production loader, a real wav written by soundfile, and the real
    ``librosa.load``. Nothing is patched into the backend.
    """
    import numpy as np
    import soundfile as sf
    import yaml

    from apps.analysis.backends.base import TrackUnreadable
    from apps.analysis.backends.librosa import LibrosaBackend

    shipped = yaml.safe_load(
        (Path(analysis_config.__file__).parent / "config.yaml").read_text()
    )
    shipped["analyzer"]["sample_rate_hz"] = 0
    broken = tmp_path / "config.yaml"
    broken.write_text(yaml.safe_dump(shipped))
    monkeypatch.setattr(analysis_config, "CONFIG_PATH", broken)
    analysis_config.load_config.cache_clear()

    good_audio = tmp_path / "real.wav"
    sf.write(good_audio, np.full(44100, 0.1, dtype="float32"), 44100)

    try:
        with pytest.raises(ValueError) as caught:
            LibrosaBackend.analyze(good_audio, "sid00004")
    finally:
        analysis_config.load_config.cache_clear()

    assert not isinstance(caught.value, TrackUnreadable), (
        "a machine-wide config fault was relabeled as an unreadable file, so "
        "the drain would meet it once per chunk across the whole library"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_lost_target_outranks_a_backend_that_could_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two true things at once, and only one of them clears the booking.

    A chunk can both lose a target before it is queued AND find the analysis
    install broken for the targets it did admit - a manual drain over a
    library being reorganized on a machine missing the analyzer is exactly
    that. The ingest worker clears `queue_signature` for
    EXIT_MISSING_TARGETS and for nothing else, so reporting the backend
    verdict first books a snapshot containing a target no process ever
    attempted. If that file comes back with the same content token, the
    queue signature never moves and the watcher answers `unchanged`
    indefinitely.

    Both facts are real. The missing target is a path that is genuinely not
    there, and `mik` is genuinely unavailable: it shells out to the paid
    `mixed-in-key-cli`, which the test asserts is absent before relying on
    it, so `BackendNotAvailable` comes from the production probe.
    """
    import shutil

    if shutil.which("mixed-in-key-cli") is not None:
        pytest.skip(
            "UNAVAILABLE: mixed-in-key-cli IS installed here, so the backend "
            "cannot be made genuinely unavailable without faking it. This is "
            "a capability report, not a pass."
        )
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    present = tmp_path / "here.mp3"
    present.write_bytes(b"\x00" * 32)
    handoff = tmp_path / "chunk.pairs.json"
    handoff.write_text(json.dumps([
        ["sid00010", str(present)],
        ["sid00011", str(tmp_path / "gone.mp3")],
    ]))

    code = run_mod.main([
        "--backend", "mik", "--all", "--pairs-json", str(handoff),
    ])

    assert code == run_mod.EXIT_MISSING_TARGETS, (
        f"exited {code}; only {run_mod.EXIT_MISSING_TARGETS} makes the ingest "
        "worker clear the booking, so the lost target would never be retried"
    )


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_a_file_lost_after_admission_is_missing_rather_than_unreadable(
    tmp_path: Path,
) -> None:
    """Admission is a check against a filesystem that keeps moving.

    ``build_queue_from_pairs`` verifies every target exists, and the backend
    opens it some chunks later. A library sync, a rename or an unmount in
    between leaves a target this process accepted and never attempted. Read
    as ``TrackUnreadable`` that is exit 1, which books the queue as tried -
    and a file restored byte-identically keeps its content token, so the
    backlog signature never moves and the watcher answers ``unchanged`` for
    a track nothing ever analyzed.

    Real from end to end: a real wav passes the real admission check, is
    really deleted, and the real ``librosa.load`` meets a real missing path.
    """
    import numpy as np
    import soundfile as sf

    from apps.analysis.backends.base import TrackUnreadable, TrackVanished
    from apps.analysis.backends.librosa import LibrosaBackend

    doomed = tmp_path / "was-here.wav"
    sf.write(doomed, np.full(22050, 0.1, dtype="float32"), 22050)

    admitted = run_mod.build_queue_from_pairs([("sid00020", str(doomed))])
    assert [r.stable_id for r in admitted] == ["sid00020"], (
        "fixture precondition: the target has to be ADMITTED first, or this "
        "is the pre-admission case that already exits 3"
    )

    doomed.unlink()

    with pytest.raises(TrackVanished) as caught:
        LibrosaBackend.analyze(doomed, "sid00020")

    assert not isinstance(caught.value, TrackUnreadable), (
        "a file that is gone was reported as a file that is bad, so the CLI "
        "exits 1 and the drain books a queue it never attempted"
    )


@contextmanager
def _losing_one_target_after_admission(doomed: Path) -> Iterator[None]:
    """Unlink ``doomed`` at the entry to :func:`run`, and not before.

    That is the window this case is about: ``build_queue_from_pairs`` has
    already admitted the file, so deleting it any earlier produces the
    pre-admission shortfall, which is a different code path with the same
    exit status. ``sys.settrace`` is restored the moment the deletion lands,
    so the analyzer itself runs untraced and nothing is injected into it.
    """
    previous = sys.gettrace()

    def evict_once(frame, event, arg):
        if frame.f_code is run_mod.run.__code__ and doomed.exists():
            doomed.unlink()
            sys.settrace(previous)
        # Returning None declines a local trace, so only `call` events fire.

    sys.settrace(evict_once)
    try:
        yield
    finally:
        sys.settrace(previous)


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_the_cli_reports_a_post_admission_loss_as_a_missing_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The status is what the drain acts on, so the race has to reach it.

    Only ``EXIT_MISSING_TARGETS`` makes the ingest worker clear
    ``queue_signature``; every other non-zero status leaves the snapshot
    booked. A target lost after admission was never analyzed, so it has to
    arrive as that status and not as the per-track exit 1 its decode failure
    would otherwise produce.
    """
    import numpy as np
    import soundfile as sf

    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    doomed = tmp_path / "vanishes.wav"
    sf.write(doomed, np.full(22050, 0.1, dtype="float32"), 22050)
    handoff = tmp_path / "chunk.pairs.json"
    handoff.write_text(json.dumps([["sid00021", str(doomed)]]))

    with _losing_one_target_after_admission(doomed):
        code = run_mod.main([
            "--backend", DEFAULT_BACKEND, "--all", "--pairs-json", str(handoff),
        ])

    assert not doomed.exists(), (
        "fixture precondition: the eviction never fired, so this asserts "
        "nothing about the race"
    )
    assert code == run_mod.EXIT_MISSING_TARGETS, (
        f"exited {code}; only {run_mod.EXIT_MISSING_TARGETS} makes the ingest "
        "worker clear the booking, so this target would never be retried"
    )


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_a_read_the_process_is_denied_is_not_blamed_on_the_file(
    tmp_path: Path,
) -> None:
    """A denial is about this process's access, not this file's contents.

    Almost nothing denies exactly one file. A share mounted without
    credentials, a parent-directory ACL, or a macOS privacy grant that permits
    ``stat`` and denies ``open`` denies EVERY track the same way - and the
    admission check passes, because ``exists()`` only needs the stat. Admitted
    as ``TrackUnreadable`` that is the continue-capable exit 1, so the drain
    launches every remaining chunk into the identical denial and then books
    the queue as attempted.

    Real: a genuine wav, a genuine ``chmod 000``, and the real
    ``librosa.load`` meeting a real EACCES. Reported as a capability report
    rather than a pass on a box where the mode bits do not bite.
    """
    import numpy as np
    import soundfile as sf

    from apps.analysis.backends.base import TrackUnreadable
    from apps.analysis.backends.librosa import LibrosaBackend

    denied = tmp_path / "denied.wav"
    sf.write(denied, np.full(22050, 0.1, dtype="float32"), 22050)
    denied.chmod(0o000)
    if os.access(denied, os.R_OK):
        pytest.skip(
            "UNAVAILABLE: this process can read a mode-000 file (root, or a "
            "filesystem that ignores the mode bits), so the denial cannot be "
            "made real here. This is a capability report, not a pass."
        )
    assert denied.exists(), (
        "fixture precondition: stat must still succeed, or this is the "
        "missing-target case rather than the denied-read one"
    )

    with pytest.raises(PermissionError) as caught:
        LibrosaBackend.analyze(denied, "sid00030")

    assert not isinstance(caught.value, TrackUnreadable), (
        "a denial that will repeat on every track in the library was "
        "reported as one bad file, so the drain works through the whole "
        "library meeting it once per chunk"
    )


@pytest.mark.requires_audio_stack
@pytest.mark.requirement("PARITY-06")
def test_the_cli_reports_a_denied_read_with_the_systemic_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The drain acts on the status, so the denial has to reach it as one.

    ``EXIT_TRACK_FAILURES`` is the only status the chunk loop may continue
    past. A denial that arrives wearing it costs one subprocess per chunk
    across the entire library before anyone is told what is wrong.
    """
    import numpy as np
    import soundfile as sf

    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    denied = tmp_path / "denied.wav"
    sf.write(denied, np.full(22050, 0.1, dtype="float32"), 22050)
    denied.chmod(0o000)
    if os.access(denied, os.R_OK):
        pytest.skip(
            "UNAVAILABLE: this process can read a mode-000 file, so the "
            "denial cannot be made real here."
        )
    handoff = tmp_path / "chunk.pairs.json"
    handoff.write_text(json.dumps([["sid00031", str(denied)]]))

    code = run_mod.main([
        "--backend", DEFAULT_BACKEND, "--all", "--pairs-json", str(handoff),
    ])

    assert code == run_mod.EXIT_INTERNAL_ERROR, (
        f"exited {code}; only a systemic status stops the chunk loop, and "
        f"{run_mod.EXIT_TRACK_FAILURES} is the one it may continue past"
    )
