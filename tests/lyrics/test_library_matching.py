"""Regression tests for the fetch fork's matching normalizers (8fbe4c6a).

The bug: the spike queried providers with RAW titles/artists, so 62.5% of the
library (MIK-prefixed "9A - 7 - Title") missed lyrics that exist. The fix
routed queries through normalize_title/primary_artist - these tests pin that
behaviour.

- if normalize_title stops stripping MIK key/energy prefixes then MIK-titled
  tracks stop matching providers -- broken
- if normalize_title starts stripping version/remix suffixes then remixes
  fetch the WRONG (original) lyrics as truth -- broken
- if primary_artist stops reducing collab strings then multi-artist tracks
  miss provider matches -- broken
- if collapse_duplicates stops preferring the audio-resolved copy then a
  broken path hides a working one (Fri 28 Aug 2026 regression) -- broken
"""

from apps.lyrics.sources.base import Track
from apps.lyrics.sources.library import collapse_duplicates, normalize_title, primary_artist

#-----------------------------------------------------------------------------


def test_normalize_title_strips_single_mik_key_prefix() -> None:
    assert normalize_title("9A - Meridian") == "Meridian"


def test_normalize_title_strips_key_and_energy_prefix_pair() -> None:
    assert normalize_title("5A - 8 - Quiet Curriculum") == "Quiet Curriculum"


def test_normalize_title_keeps_remix_suffix() -> None:
    # Version suffixes change WHICH lyrics are correct - never stripped.
    assert normalize_title("9A - 7 - Meridian - Kestrel Hale Remix") == "Meridian - Kestrel Hale Remix"
    assert normalize_title("Um (Radio Edit)") == "Um (Radio Edit)"


def test_normalize_title_leaves_numeric_titles_alone() -> None:
    assert normalize_title("99 Luftballons") == "99 Luftballons"
    assert normalize_title("1998 - Remastered") == "1998 - Remastered"  # 4 digits != MIK key


def test_primary_artist_reduces_collab_strings() -> None:
    assert primary_artist("Tomas Rye, Pell") == "Tomas Rye"
    assert primary_artist("Artist feat. Guest") == "Artist"
    assert primary_artist("Artist ft Guest") == "Artist"
    assert primary_artist("A x B") == "A"
    assert primary_artist("Solo Artist") == "Solo Artist"


def _track(audio_path: str | None) -> Track:
    return Track(
        artist="Tomas Rye",
        title="Quiet Curriculum",
        norm_title="Quiet Curriculum",
        primary_artist="Tomas Rye",
        duration_s=213,
        audio_path=audio_path,
    )


def test_collapse_duplicates_prefers_resolved_copy_either_order() -> None:
    broken, resolved = _track(None), _track("/audio/quiet-curriculum.mp3")
    assert collapse_duplicates([broken, resolved]) == [resolved]
    assert collapse_duplicates([resolved, broken]) == [resolved]
