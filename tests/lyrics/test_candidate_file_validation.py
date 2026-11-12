"""Pin validate_candidate_file rejections and acceptance (issue #9338)."""

from __future__ import annotations

import pytest

from apps.lyrics.sources.candidates import (
    CANDIDATE_REQUIRED_KEYS,
    SCHEMA_VERSION,
    TRACK_REQUIRED_KEYS,
    validate_candidate_file,
)


def _track(**overrides: object) -> dict:
    base = {
        "key": "abc123",
        "artist": "Artist",
        "title": "Title",
        "norm_title": "title",
        "duration_s": 200,
        "audio_path": "/audio/track.mp3",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def _candidate(**overrides: object) -> dict:
    base = {
        "id": "lrclib-exact-get-0",
        "source": "lrclib",
        "method": "search",
        "rank": 0,
        "plain_text": "hello",
        "synced_lrc": None,
        "source_track": {"artist": "Artist", "title": "Title"},
        "duration_delta_s": None,
        "retrieved_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def _payload(**overrides: object) -> dict:
    base = {
        "schema_version": SCHEMA_VERSION,
        "track": _track(),
        "generated_at": "2026-01-01T00:00:00+00:00",
        "candidates": [],
        "instrumental_evidence": [],
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def test_track_required_keys_tuple_unchanged() -> None:
    assert TRACK_REQUIRED_KEYS == (
        "key",
        "artist",
        "title",
        "norm_title",
        "duration_s",
        "audio_path",
    )


def test_candidate_required_keys_tuple_unchanged() -> None:
    assert CANDIDATE_REQUIRED_KEYS == (
        "id",
        "source",
        "method",
        "rank",
        "plain_text",
        "synced_lrc",
        "source_track",
        "duration_delta_s",
        "retrieved_at",
    )


def test_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValueError, match=r"schema_version 99 != 1"):
        validate_candidate_file(_payload(schema_version=99))
    with pytest.raises(ValueError, match=r"schema_version None != 1"):
        validate_candidate_file({**_payload(), "schema_version": None})


def test_schema_mismatch_wins_over_missing_track() -> None:
    p = _payload(schema_version=99)
    del p["track"]
    with pytest.raises(ValueError, match=r"schema_version 99 != 1"):
        validate_candidate_file(p)


def test_track_missing_key_wins_over_candidates_omitted() -> None:
    t = _track()
    del t["key"]
    p = _payload(track=t)
    del p["candidates"]
    with pytest.raises(ValueError, match=r"^track missing 'key'$"):
        validate_candidate_file(p)


@pytest.mark.parametrize(
    "track_value",
    [None, ["not", "a", "dict"], "not-a-dict"],
)
def test_rejects_missing_or_non_dict_track(track_value: object) -> None:
    p = _payload()
    if track_value is None:
        del p["track"]
    else:
        p["track"] = track_value  # type: ignore[assignment]
    with pytest.raises(ValueError, match=r"^missing track object$"):
        validate_candidate_file(p)


@pytest.mark.parametrize("missing_key", list(TRACK_REQUIRED_KEYS))
def test_rejects_track_missing_required_key(missing_key: str) -> None:
    t = _track()
    del t[missing_key]
    with pytest.raises(ValueError, match=rf"^track missing {missing_key!r}$"):
        validate_candidate_file(_payload(track=t))


def test_rejects_track_key_mismatch_expected_key() -> None:
    with pytest.raises(
        ValueError,
        match=r"^track.key 'abc123' != filename key 'other'$",
    ):
        validate_candidate_file(_payload(), expected_key="other")


@pytest.mark.parametrize(
    "candidates_value",
    [None, {"not": "list"}, "not-a-list"],
)
def test_rejects_candidates_not_a_list(candidates_value: object) -> None:
    p = _payload()
    if candidates_value is None:
        del p["candidates"]
    else:
        p["candidates"] = candidates_value  # type: ignore[assignment]
    with pytest.raises(ValueError, match=r"^candidates must be a list$"):
        validate_candidate_file(p)


@pytest.mark.parametrize("missing_key", list(CANDIDATE_REQUIRED_KEYS))
def test_rejects_candidate_missing_required_key(missing_key: str) -> None:
    c = _candidate()
    del c[missing_key]
    with pytest.raises(ValueError, match=rf"^candidates\[0\] missing {missing_key!r}$"):
        validate_candidate_file(_payload(candidates=[c]))


def test_rejects_second_candidate_missing_key() -> None:
    c0 = _candidate()
    c1 = _candidate(id="other", rank=1)
    del c1["retrieved_at"]
    with pytest.raises(ValueError, match=r"^candidates\[1\] missing 'retrieved_at'$"):
        validate_candidate_file(_payload(candidates=[c0, c1]))


def test_rejects_candidate_with_no_text() -> None:
    c = _candidate(plain_text=None, synced_lrc=None)
    with pytest.raises(ValueError, match=r"^candidates\[0\] carries no text at all$"):
        validate_candidate_file(_payload(candidates=[c]))


def test_accepts_candidate_with_only_plain_text() -> None:
    c = _candidate(plain_text="only", synced_lrc=None)
    validate_candidate_file(_payload(candidates=[c]))


def test_accepts_candidate_with_only_synced_lrc() -> None:
    c = _candidate(plain_text=None, synced_lrc="[00:00.00]line")
    validate_candidate_file(_payload(candidates=[c]))


@pytest.mark.parametrize(
    "evidence_value",
    ["string-not-list", {"not": "list"}],
)
def test_rejects_instrumental_evidence_not_a_list(evidence_value: object) -> None:
    with pytest.raises(ValueError, match=r"^instrumental_evidence must be a list$"):
        validate_candidate_file(_payload(instrumental_evidence=evidence_value))


def test_accepts_missing_instrumental_evidence_key() -> None:
    p = _payload()
    del p["instrumental_evidence"]
    validate_candidate_file(p)


def test_rejects_exact_get_instrumental_and_text_clash() -> None:
    ev = {"source": "lrclib", "method": "exact-get", "source_id": 1}
    c = _candidate(method="exact-get", plain_text="x")
    with pytest.raises(
        ValueError,
        match=r"^exact-get both declared instrumental AND supplied text$",
    ):
        validate_candidate_file(_payload(candidates=[c], instrumental_evidence=[ev]))


def test_no_clash_when_evidence_exact_get_but_candidates_not_exact_get() -> None:
    ev = {"method": "exact-get"}
    c = _candidate(method="search")
    validate_candidate_file(_payload(candidates=[c], instrumental_evidence=[ev]))


def test_no_clash_when_candidate_exact_get_but_evidence_not() -> None:
    ev = {"method": "matcher-track-get"}
    c = _candidate(method="exact-get")
    validate_candidate_file(_payload(candidates=[c], instrumental_evidence=[ev]))


def test_no_clash_when_evidence_exact_get_but_candidates_empty() -> None:
    ev = {"method": "exact-get"}
    validate_candidate_file(_payload(candidates=[], instrumental_evidence=[ev]))


def test_accepts_empty_payload_no_expected_key() -> None:
    validate_candidate_file(_payload())


def test_accepts_matching_expected_key() -> None:
    validate_candidate_file(_payload(), expected_key="abc123")
