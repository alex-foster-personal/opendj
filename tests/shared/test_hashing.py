"""Tests for :func:`apps.shared.hashing.sha256_audio_payload`.

Added for the waveform bundle identity fix (Codex P1 BLOCKING, PR #1536,
thread on ``scripts/build_waveform_bundle.py:105``): the bundle's
``compute_bundle_id`` now folds in each sampled track's AUDIO PAYLOAD hash,
tags stripped for mp3, so a repaired, relinked or re-encoded track changes
the identity even when its rekordbox row does not. This file pins the
ID3-stripping logic itself, independent of the bundle builder, against
synthetic mp3 bytes built the same way
``data/reference/mik/20260908/payload_hash.py`` (the reference
implementation this reuses) constructs its ID3v2/ID3v1 envelope.

  - [if] the same audio payload is wrapped in two DIFFERENT tag sets [then]
    the hash is identical [else] fail (a retag alone must not change
    identity).
  - [if] the audio payload itself differs [then] the hash differs [else]
    fail (a real content change must change identity).
  - [if] the mp3 carries nested leading ID3v2 tags [then] both are stripped
    [else] fail.
  - [if] the mp3 carries a trailing ID3v1 tag [then] it is stripped [else]
    fail.
  - [if] the file is not an mp3 [then] the whole file is hashed, matching
    :func:`apps.shared.hashing.sha256_file` [else] fail.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from apps.shared.hashing import sha256_audio_payload, sha256_file


def _sha256_of_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _id3v2_tag(body: bytes, *, footer: bool = False) -> bytes:
    """One ID3v2.4 tag: header + body (+ footer), synchsafe size."""
    size = len(body)
    synchsafe = bytes(
        [(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]
    )
    flags = 0x10 if footer else 0x00
    header = b"ID3" + bytes([4, 0, flags]) + synchsafe
    trailer = b"3DI" + bytes([4, 0, flags]) + synchsafe if footer else b""
    return header + body + trailer


def _id3v1_tag() -> bytes:
    return b"TAG" + b"\x00" * 125


def _write_mp3(
    path: Path, *, leading_tags: list[bytes], payload: bytes, trailing_tag: bytes
) -> None:
    path.write_bytes(b"".join(leading_tags) + payload + trailing_tag)


def test_a_retag_alone_does_not_change_the_payload_hash(tmp_path: Path) -> None:
    payload = b"\xff\xfb" + b"\x11" * 500  # stand-in MPEG frame bytes
    original = tmp_path / "original.mp3"
    retagged = tmp_path / "retagged.mp3"
    _write_mp3(
        original,
        leading_tags=[_id3v2_tag(b"old title")],
        payload=payload,
        trailing_tag=_id3v1_tag(),
    )
    _write_mp3(
        retagged,
        leading_tags=[_id3v2_tag(b"a completely different title, much longer")],
        payload=payload,
        trailing_tag=b"",  # no ID3v1 this time
    )
    assert sha256_audio_payload(original) == sha256_audio_payload(retagged)
    # And the raw file hash DOES differ, proving the tags really were different.
    assert sha256_file(original) != sha256_file(retagged)


def test_a_different_audio_payload_changes_the_hash(tmp_path: Path) -> None:
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    tag = [_id3v2_tag(b"tag")]
    _write_mp3(a, leading_tags=tag, payload=b"\xff\xfb" + b"\x11" * 500, trailing_tag=b"")
    _write_mp3(b, leading_tags=tag, payload=b"\xff\xfb" + b"\x22" * 500, trailing_tag=b"")
    assert sha256_audio_payload(a) != sha256_audio_payload(b)


def test_nested_leading_id3v2_tags_are_both_stripped(tmp_path: Path) -> None:
    """A second tagger writing after one already did leaves two ID3v2 tags
    back to back; the loop in sha256_audio_payload must walk past both."""
    payload = b"\xff\xfb" + b"\x33" * 500
    single = tmp_path / "single.mp3"
    nested = tmp_path / "nested.mp3"
    _write_mp3(single, leading_tags=[_id3v2_tag(b"one tag")], payload=payload, trailing_tag=b"")
    _write_mp3(
        nested,
        leading_tags=[_id3v2_tag(b"outer"), _id3v2_tag(b"inner, written first")],
        payload=payload,
        trailing_tag=b"",
    )
    assert sha256_audio_payload(single) == sha256_audio_payload(nested) == _sha256_of_bytes(payload)


def test_a_trailing_id3v1_tag_is_stripped(tmp_path: Path) -> None:
    payload = b"\xff\xfb" + b"\x44" * 500
    with_tag = tmp_path / "with_v1.mp3"
    without_tag = tmp_path / "without_v1.mp3"
    _write_mp3(with_tag, leading_tags=[], payload=payload, trailing_tag=_id3v1_tag())
    without_tag.write_bytes(payload)
    assert sha256_audio_payload(with_tag) == sha256_audio_payload(without_tag)


def test_an_id3v2_footer_is_accounted_for(tmp_path: Path) -> None:
    payload = b"\xff\xfb" + b"\x55" * 500
    path = tmp_path / "footer.mp3"
    _write_mp3(
        path, leading_tags=[_id3v2_tag(b"body", footer=True)], payload=payload, trailing_tag=b""
    )
    assert sha256_audio_payload(path) == _sha256_of_bytes(payload)


def test_a_non_mp3_file_is_hashed_whole_matching_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "clip.wav"
    path.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 200)
    assert sha256_audio_payload(path) == sha256_file(path)


def test_the_result_carries_the_shared_sha256_prefix(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"\xff\xfb" + b"\x66" * 50)
    assert sha256_audio_payload(path).startswith("sha256:")
