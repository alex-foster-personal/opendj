"""Regression: every tag READ callsite survives a missing tinytag install.

tinytag (MIT) is a core dependency since Thu 1 Oct 2026, replacing GPL
mutagen for reads (``apps/shared/_tagreader.py``). It is still guarded, so a
broken environment degrades to an explicit "reader unavailable" rather than
an import crash. The contract:

* Modules still import cleanly with the reader flagged absent.
* :func:`apps.shared._tagreader.require` raises :class:`ImportError` with a
  reinstall hint.
* Best-effort read paths (metadata scan, artwork probe, fingerprint bitrate,
  matcher title/artist probe, reconcile index) degrade to ``None`` /
  ``ok=False`` without exploding.
* No read path reaches for mutagen: the reads work with mutagen made
  unimportable (the positive control below).

Absence is simulated by flipping ``HAS_TAG_READER`` (module-level imports
copy it by value, so each dependent module's own binding is patched too);
the HTTP-level 503s are proven with a real import block in their own tests.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-128.mp3"

_DEPENDENTS = (
    "apps.shared.audio_files",
    "apps.shared.audio_playable",
    "apps.reconcile.index_disk",
)


@pytest.fixture
def no_reader(monkeypatch):
    import apps.shared._tagreader as gate

    # Import every dependent BEFORE flipping the gate: a first import after the
    # flip would copy False by value, and teardown would then "restore" False.
    dependents = [importlib.import_module(name) for name in _DEPENDENTS]
    monkeypatch.setattr(gate, "HAS_TAG_READER", False)
    for module in dependents:
        monkeypatch.setattr(module, "HAS_TAG_READER", False)
    return gate


def test_require_raises_with_reinstall_hint(no_reader):
    with pytest.raises(ImportError) as excinfo:
        no_reader.require()
    msg = str(excinfo.value)
    assert "tinytag" in msg
    assert "uv sync" in msg


def test_modules_still_importable_without_reader(no_reader):
    for name in (*_DEPENDENTS, "apps.shared.fingerprints", "apps.sync.matcher"):
        assert importlib.import_module(name) is not None


def test_read_paths_degrade_without_reader(tmp_path, no_reader):
    from apps.reconcile import index_disk
    from apps.shared import audio_files, fingerprints
    from apps.sync import matcher

    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    assert audio_files.read_metadata(track) is None
    assert audio_files.read_embedded_artwork(track) is None
    assert audio_files.embedded_artwork_available(track) is False
    assert fingerprints._safe_bitrate(track) is None
    assert matcher._read_id3(track) is None
    assert index_disk.read_tags(track).ok is False


def test_read_paths_measure_with_reader(tmp_path):
    """Positive control for the test above: the same file DOES read."""
    from apps.reconcile import index_disk
    from apps.shared import audio_files, fingerprints

    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    meta = audio_files.read_metadata(track)
    assert meta is not None and meta.duration_s == pytest.approx(3.056, abs=0.01)
    assert fingerprints._safe_bitrate(track) == 127
    read = index_disk.read_tags(track)
    assert read.ok is True and read.duration_s == pytest.approx(3.056, abs=0.01)


def test_read_paths_never_import_mutagen(tmp_path):
    """Every read works in a process where ``import mutagen`` fails.

    Positive control first: the probe proves mutagen really is blocked, else
    a passing read could be mutagen's.
    """
    track = tmp_path / "x.mp3"
    shutil.copyfile(FIXTURE, track)
    # The matcher ignores untagged files, so give its read a tagged copy.
    # Written here, in the parent, because the probe process blocks mutagen.
    tagged = tmp_path / "tagged.mp3"
    shutil.copyfile(FIXTURE, tagged)
    mutagen_id3 = pytest.importorskip("mutagen.id3")
    tags = mutagen_id3.ID3()
    tags.add(mutagen_id3.TIT2(encoding=3, text="Probe Title"))
    tags.add(mutagen_id3.TPE1(encoding=3, text="Probe Artist"))
    tags.save(tagged)
    probe = textwrap.dedent(
        f"""
        import sys
        sys.modules["mutagen"] = None
        try:
            import mutagen  # noqa: F401
        except ImportError:
            pass
        else:
            raise SystemExit("control failed: mutagen still importable")
        from pathlib import Path
        from apps.reconcile import index_disk
        from apps.shared import audio_files, audio_playable, fingerprints
        from apps.sync import matcher
        p = Path({str(track)!r})
        meta = audio_files.read_metadata(p)
        assert meta is not None and meta.duration_s > 3, meta
        assert fingerprints._safe_bitrate(p) == 127
        assert index_disk.read_tags(p).ok
        assert matcher._read_id3(Path({str(tagged)!r})) is not None
        audio_playable.probe_playable_audio(p)
        assert "mutagen" not in {{k for k, v in sys.modules.items() if v is not None}}
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("OK")
