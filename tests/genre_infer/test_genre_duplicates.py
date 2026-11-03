"""Duplicates of a track share one genre result (GENRE-08).

- [if] two copies of a track are inferred together [then] the providers are called once and both show the result, [else stop].
- [if] a copy has no file and was never asked about [then] it shows its duplicate's findings, vector and genre, [else stop].
- [if] copies hold disagreeing findings [then] ok, then confidence, then newest, then the lowest id wins, for every copy, [else stop].
- [if] a finding is attached to or removed from one copy [then] every copy shows the change, [else stop].
- [if] a track only shares a title, or only an artist [then] it is NOT a duplicate and shares nothing, [else stop].
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.genre_infer import findings, groups, pipeline, providers, service, tag_write, taxonomy
from apps.shared.song_identity import artists_from_json, song_key, title_artists_key
from apps.shared.tag_writer import read_tags
from tests.genre_infer.pipeline_support import (
    ASKED_ID,
    COPY_ID,
    PLACEHOLDER_ID,
    SAME_ARTIST_ID,
    ProviderServer,
    library,
)

pytestmark = pytest.mark.requirement("GENRE-08")

SOURCES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"
ASKED, OTHER = ASKED_ID, SAME_ARTIST_ID


def _add(conn: sqlite3.Connection, *rows: tuple[str, str, str, str | None]) -> None:
    conn.executemany(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, created_at, updated_at) "
        "VALUES (?, 'inferred', ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        rows,
    )
    conn.commit()


def _copies(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    """Rows derived from the library's asked track: two copies of it, and a same-title track by someone else.

    ``t-twin`` is tagged as another tool would (other case, other spacing);
    ``t-twin-nofile`` has no file; ``t-cover`` shares only the title.
    """
    title, artists_json = conn.execute(
        "SELECT title, artists_json FROM tracks WHERE stable_id = ?", (ASKED,)
    ).fetchone()
    shouted = json.dumps([name.upper() for name in json.loads(artists_json)])
    return (
        ("t-twin", title.upper().replace(" ", "  ", 1), shouted, "/m/backup/twin.mp3"),
        ("t-twin-nofile", title, artists_json, None),
        ("t-cover", title, '["Someone Else"]', "/m/cover.mp3"),
    )


def _conn(tmp_path: Path) -> sqlite3.Connection:
    _data, state = library(tmp_path)
    conn = sqlite3.connect(str(state))
    findings.ensure_schema(conn)
    # These tests are about group sharing, not precedence: pin the pre-GENRE-09
    # order so a fixture's library tag never masks the finding under test.
    findings.update_config(conn, {"precedence": ["user", "beatport", "odj-fused", "library-tag"]})
    _add(conn, *_copies(conn))
    return conn


def _put(conn: sqlite3.Connection, sid: str, provider: str, genre: str | None, conf: float | None, at: str) -> None:
    status = findings.STATUS_OK if genre else findings.STATUS_UNKNOWN
    findings.put_finding(conn, findings.Finding(sid, provider, status, genre, conf, created_at=at))


def _tracks(conn: sqlite3.Connection, *ids: str) -> dict[str, Any]:
    return service.genres_for(conn, list(ids))["tracks"]


# ----------------------------------------------------------------- identity
def test_the_group_key_is_the_song_key_without_its_length() -> None:
    """[if] two rows share title and artists but not length [then] one genre group, two songs, [else stop]."""
    a = title_artists_key("Night Bus", ["Tamsin Quell", "\uff24\uff2a X"])
    assert a == title_artists_key(" NIGHT  bus ", ["dj x", "TAMSIN QUELL"]), (
        "case, spacing, width and artist order fold"
    )
    assert a != title_artists_key("Night Bus", ["Tamsin Quell"])
    assert a != title_artists_key("Night Bus (Edit)", ["Tamsin Quell", "DJ X"])
    assert title_artists_key("", ["x"]) is None and title_artists_key("t", []) is None
    assert song_key("Night Bus", ["Tamsin Quell"], 200_000) != song_key("Night Bus", ["Tamsin Quell"], 300_000)
    assert song_key("Night Bus", ["Tamsin Quell"], 200_400) == song_key("night bus", ["TAMSIN QUELL"], 199_600)


def test_both_keys_read_the_artists_the_same_way() -> None:
    """[if] a tag repeats an artist or holds the key's own separator [then] both keys treat it alike, [else stop]."""
    # An artist listed twice is one artist, for the genre group AND for the enrich card's song.
    assert title_artists_key("Night Bus", ["Tamsin Quell", "tamsin quell"]) == title_artists_key(
        "Night Bus", ["Tamsin Quell"]
    )
    assert song_key("Night Bus", ["Tamsin Quell", "tamsin quell"], 200_000) == song_key(
        "Night Bus", ["Tamsin Quell"], 200_000
    )
    # No character in a tag can move text between the title and the artists.
    for joiner in ("|", ",", '"', '","', "\x1f"):
        split = (f"A{joiner}B", ["C"]), ("A", [f"B{joiner}C"]), ("A", ["B", "C"])
        assert len({title_artists_key(*parts) for parts in split}) == 3, joiner
        assert len({song_key(*parts, 200_000) for parts in split}) == 3, joiner
    # Control: the same parts still give the same key.
    assert title_artists_key("A|B", ["C"]) == title_artists_key("a|b", ["c"])
    # Only a JSON list names artists; anything else names none, so the row joins nothing.
    assert artists_from_json('["A", "B"]') == ["A", "B"]
    assert [artists_from_json(raw) for raw in ("not json", '"Solo"', '{"a": 1}', "", None)] == [[]] * 5


def test_groups_join_copies_and_leave_everything_else_alone(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    grp = groups.load(conn)
    assert grp.members_of("t-twin-nofile") == (ASKED, "t-twin", "t-twin-nofile")
    assert grp.siblings_of(ASKED) == ("t-twin", "t-twin-nofile")
    # Controls: same title by another artist, another title by the same artist, a deleted copy, an unknown id.
    assert grp.members_of("t-cover") == ("t-cover",)
    assert grp.members_of(OTHER) == (OTHER,)
    assert grp.members_of("nope") == ("nope",)
    # Two rows whose artists cannot be read are never joined, however alike the unreadable text is.
    _add(conn, ("t-odd-1", "Same Title", "Tamsin Quell", None), ("t-odd-2", "Same Title", "Tamsin Quell", None))
    assert groups.load(conn).members_of("t-odd-1") == ("t-odd-1",)
    grp = groups.load(conn)
    assert grp.first_per_group([ASKED, "t-twin", "t-cover", "t-twin-nofile", OTHER]) == [ASKED, "t-cover", OTHER]
    conn.execute("UPDATE tracks SET deleted_at = '2026-10-12T00:00:00Z' WHERE stable_id = 't-twin'")
    assert groups.load(conn).members_of(ASKED) == (ASKED, "t-twin-nofile"), "the tracks write token did not move"
    conn.execute("UPDATE tracks SET title = 'Something else' WHERE stable_id = 't-twin-nofile'")
    assert groups.load(conn).members_of(ASKED) == (ASKED,), "a retag must regroup at the next read"


# ---------------------------------------------------------------- inference
def test_copies_inferred_together_are_asked_once(tmp_path: Path, providers_live: ProviderServer) -> None:
    conn = _conn(tmp_path)
    summary = pipeline.infer_tracks(conn, [ASKED, "t-twin", "t-cover"])
    # Two works (the asked track's and the cover), three tracks: two searches, two asks of each model.
    assert dict(providers_live.hits) == {"/search": 2, "/alpha/decisions": 2, "/v1/chat/completions": 2}
    by_id = {r.stable_id: r for r in summary.results}
    assert set(by_id) == {ASKED, "t-twin", "t-cover"}
    assert all(r.outcome == pipeline.OUTCOME_DONE for r in by_id.values()), summary.as_dict()
    assert by_id[ASKED].shared_from is None and by_id[ASKED].cost_usd > 0
    shared = by_id["t-twin"]
    assert (shared.shared_from, shared.cost_usd, shared.paid_search) == (ASKED, 0.0, False)
    assert shared.genre == by_id[ASKED].genre
    doc = summary.as_dict()
    assert (doc["asked"], doc["shared_with_duplicates"], doc["done"]) == (2, 1, 3)
    assert doc["cost_per_1k_usd"] == round(1000 * summary.cost_usd / 2, 4), "a shared duplicate is not an asked track"
    # One stored result for the group, on the copy that was asked.
    assert sorted(findings.findings_for(conn)) == ["t-cover", ASKED]


def test_every_copy_shows_the_groups_findings_vector_and_genre(tmp_path: Path, providers_live: ProviderServer) -> None:
    conn = _conn(tmp_path)
    pipeline.infer_tracks(conn, [ASKED])
    docs = _tracks(conn, ASKED, "t-twin", "t-twin-nofile", OTHER)
    asked = docs[ASKED]
    assert asked["chosen"]["provider"] == "odj-fused"
    for sid in ("t-twin", "t-twin-nofile"):
        assert docs[sid]["chosen"] == asked["chosen"]
        assert docs[sid]["findings"] == asked["findings"]
        assert {f["provider"] for f in docs[sid]["findings"]} == {"jev", "deepseek", "odj-fused"}
        assert {f["stable_id"] for f in docs[sid]["findings"]} == {ASKED}, "a finding names the copy that holds it"
        assert docs[sid]["vector"] == {"provider": "jev", "stable_id": ASKED}
        assert sid in asked["duplicates"] and ASKED in docs[sid]["duplicates"]
    # Control: another track by the same artist shares nothing.
    assert docs[OTHER]["findings"] == [] and docs[OTHER]["vector"] is None
    assert docs[OTHER]["duplicates"] == []
    # Similarity: a copy with no vector of its own answers from the group's, and never lists its own copies.
    findings.put_vector(conn, "t-copy", "jev", {g: 0.0 for g in taxonomy.NAMES} | {"House": 1.0})
    assert [s["stable_id"] for s in service.similar(conn, "t-twin-nofile", 5)["similar"]] == ["t-copy"]
    with pytest.raises(KeyError, match="no jev genre vector"):
        service.similar(conn, OTHER, 5)


def test_a_copy_requested_later_costs_nothing_and_is_not_queued(tmp_path: Path, providers_live: ProviderServer) -> None:
    conn = _conn(tmp_path)
    everything = [ASKED, "t-twin", "t-twin-nofile", "t-cover", OTHER]
    assert pipeline.missing_fused_ids(conn, everything) == [ASKED, "t-cover", OTHER], "one candidate per group"
    pipeline.infer_tracks(conn, [ASKED])
    before = dict(providers_live.hits)
    again = pipeline.infer_tracks(conn, ["t-twin", "t-twin-nofile"])
    assert [r.outcome for r in again.results] == [pipeline.OUTCOME_UP_TO_DATE] * 2
    assert dict(providers_live.hits) == before, "a duplicate of an inferred track was paid for again"
    assert again.cost_usd == 0.0
    assert pipeline.missing_fused_ids(conn, everything) == ["t-cover", OTHER]


def test_a_forced_ask_leaves_one_result_for_the_group(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] copies hold old results and one is asked again [then] the group keeps only the new one, [else stop]."""
    conn = _conn(tmp_path)
    for sid, genre in (("t-twin", "Trance (Main Floor)"), ("t-twin-nofile", "Hard Techno")):
        _put(conn, sid, "odj-fused", genre, 0.99, "2026-10-01T00:00:00+00:00")
        _put(conn, sid, "jev", genre, 0.99, "2026-10-01T00:00:00+00:00")
        findings.put_vector(conn, sid, "jev", {g: 0.0 for g in taxonomy.NAMES} | {genre: 1.0})
    # A provider the pipeline does not own survives the collapse.
    _put(conn, "t-twin-nofile", "beatport", "House", 1.0, "2026-10-01T00:00:00+00:00")
    out = pipeline.infer_tracks(conn, [ASKED], force=True)
    assert out.count(pipeline.OUTCOME_DONE) == 1
    holders = {
        (sid, provider)
        for sid, provider in conn.execute("SELECT stable_id, provider FROM genre_findings ORDER BY 1, 2")
    }
    # The copy that held the group's result (highest confidence, lowest id) is the one asked again.
    assert holders == {
        ("t-twin", "deepseek"),
        ("t-twin", "jev"),
        ("t-twin", "odj-fused"),
        ("t-twin-nofile", "beatport"),
    }
    assert [r[0] for r in conn.execute("SELECT stable_id FROM genre_vectors")] == ["t-twin"]
    fused = {f["provider"]: f for f in _tracks(conn, ASKED)[ASKED]["findings"]}["odj-fused"]
    assert fused["genre"] == out.results[0].genre != "Trance (Main Floor)"


def _old_result(conn: sqlite3.Connection, sid: str, genre: str, confidence: float) -> None:
    _put(conn, sid, "odj-fused", genre, confidence, "2026-10-01T00:00:00+00:00")
    findings.put_vector(conn, sid, "jev", {g: 0.0 for g in taxonomy.NAMES} | {"House": 1.0})
    conn.commit()


RETAG_TWIN = ("UPDATE tracks SET title = 'Something else entirely' WHERE stable_id = 't-twin'", ())
# The asked track becomes a copy of ``t-cover`` (same title, now the same artist).
RETAG_ASKED_INTO_THE_COVERS_GROUP = (
    "UPDATE tracks SET artists_json = '[\"Someone Else\"]' WHERE stable_id = ?",
    (ASKED,),
)
OLD = {ASKED: "Hard Techno", "t-twin": "Trance (Main Floor)", "t-cover": "Dubstep"}
DONE, FAILED = pipeline.OUTCOME_DONE, pipeline.OUTCOME_FAILED


@pytest.mark.parametrize(
    ("retag", "outcomes", "untouched"),
    [
        # The other copy is retagged while the models are being asked: it is no longer a copy,
        # keeps its own result, and (requested too) is told to be asked under its new name.
        (RETAG_TWIN, {ASKED: DONE, "t-twin": FAILED}, {"t-twin", "t-cover"}),
        # The asked track itself moves into another group: the answer is about its old name,
        # so nothing is stored and nothing is replaced, there or in the group it left.
        (RETAG_ASKED_INTO_THE_COVERS_GROUP, {ASKED: FAILED, "t-twin": FAILED}, {ASKED, "t-twin", "t-cover"}),
        # Control: nothing moves; the asked track's group collapses to the new result.
        (None, {ASKED: DONE, "t-twin": DONE}, {"t-cover"}),
    ],
)
def test_a_track_retagged_while_the_models_are_asked_is_not_given_the_answer(
    tmp_path: Path,
    providers_live: ProviderServer,
    retag: tuple[str, tuple[str, ...]] | None,
    outcomes: dict[str, str],
    untouched: set[str],
) -> None:
    """[if] a track changes group while the calls are in flight [then] the answer is neither stored on it nor deletes its findings, [else stop]."""
    conn = _conn(tmp_path)
    state = conn.execute("PRAGMA database_list").fetchone()[2]
    _old_result(conn, ASKED, OLD[ASKED], 0.9)  # the holder, so the copy that is asked again
    _old_result(conn, "t-twin", OLD["t-twin"], 0.5)
    _old_result(conn, "t-cover", OLD["t-cover"], 0.5)

    def retag_in_flight(reply: dict[str, Any]) -> dict[str, Any]:
        # Runs on the provider server's thread while infer_tracks waits on the call:
        # another writer (an import, a sync) commits a retag through its own connection.
        if retag is not None:
            other = sqlite3.connect(state)
            other.execute(*retag)
            other.commit()
            other.close()
        return reply

    providers_live.transform["/alpha/decisions"] = retag_in_flight
    out = pipeline.infer_tracks(conn, [ASKED, "t-twin"], force=True)
    by_id = {r.stable_id: r for r in out.results}
    assert {sid: r.outcome for sid, r in by_id.items()} == outcomes, out.as_dict()
    assert {r.reason for r in by_id.values() if r.outcome == FAILED} <= {pipeline.FAIL_RETAGGED}
    assert out.cost_usd > 0, "the calls were made and billed, whatever was kept"
    fused = dict(conn.execute("SELECT stable_id, genre FROM genre_findings WHERE provider = 'odj-fused'"))
    vectors = {r[0] for r in conn.execute("SELECT stable_id FROM genre_vectors")}
    assert {sid: fused.get(sid) for sid in untouched} == {sid: OLD[sid] for sid in untouched}, fused
    assert untouched <= vectors, vectors
    if outcomes[ASKED] == DONE:
        assert fused[ASKED] == by_id[ASKED].genre != OLD[ASKED]
        # Its group now holds one computed result: a copy still in the group lost its old one.
        assert ("t-twin" in fused) == ("t-twin" in untouched)
    assert dict(providers_live.hits) == {"/search": 1, "/alpha/decisions": 1, "/v1/chat/completions": 1}


def test_an_up_to_date_copy_fails_when_the_holder_of_its_result_is_retagged_meanwhile(
    tmp_path: Path, providers_live: ProviderServer
) -> None:
    """[if] the copy holding a group's result is retagged while another group is asked [then] its copies are not reported up to date, [else stop]."""
    conn = _conn(tmp_path)
    state = conn.execute("PRAGMA database_list").fetchone()[2]
    pipeline.infer_tracks(conn, [ASKED])  # ASKED holds the group's result; t-twin is up to date through it
    control = pipeline.infer_tracks(conn, ["t-twin"])
    assert [r.outcome for r in control.results] == [pipeline.OUTCOME_UP_TO_DATE]

    def retag_the_holder(reply: dict[str, Any]) -> dict[str, Any]:
        other = sqlite3.connect(state)
        other.execute("UPDATE tracks SET title = 'Something else entirely' WHERE stable_id = ?", (ASKED,))
        other.commit()
        other.close()
        return reply

    # t-cover is another group and has to be asked; the holder is retagged during that call.
    providers_live.transform["/alpha/decisions"] = retag_the_holder
    out = pipeline.infer_tracks(conn, ["t-twin", "t-cover"])
    by_id = {r.stable_id: r for r in out.results}
    assert (by_id["t-twin"].outcome, by_id["t-twin"].reason) == (FAILED, pipeline.FAIL_RETAGGED)
    assert by_id["t-cover"].outcome == DONE
    assert _tracks(conn, "t-twin")["t-twin"]["chosen"] is None, "the group has no result any more"
    assert pipeline.missing_fused_ids(conn, ["t-twin"]) == ["t-twin"]


# ------------------------------------------- findings made for an old name
def _made_by_inference(conn: sqlite3.Connection) -> None:
    pipeline.infer_tracks(conn, [OTHER])


def _made_before_identity_was_recorded(conn: sqlite3.Connection) -> None:
    """A computed finding as main stored it: the search query in its evidence, no identity."""
    title, artists_json = conn.execute(
        "SELECT title, artists_json FROM tracks WHERE stable_id = ?", (OTHER,)
    ).fetchone()
    query = providers.search_query(", ".join(json.loads(artists_json)), title)
    for provider in ("odj-fused", "jev"):
        findings.put_finding(
            conn,
            findings.Finding(
                OTHER, provider, "ok", "House", 0.8, evidence={"search_query": query}, request_digest="d" * 64
            ),
        )
    findings.put_vector(conn, OTHER, "jev", {g: 0.0 for g in taxonomy.NAMES} | {"House": 1.0})


@pytest.mark.parametrize("make", [_made_by_inference, _made_before_identity_was_recorded])
def test_a_finding_made_for_an_old_name_is_not_shared_after_a_retag(
    tmp_path: Path, providers_live: ProviderServer, make: Any
) -> None:
    """[if] a track with findings is retagged into another group [then] that group's copies do not read them, [else stop]."""
    conn = _conn(tmp_path)
    make(conn)
    conn.commit()
    before = _tracks(conn, OTHER)[OTHER]
    assert before["chosen"]["provider"] == "odj-fused" and before["vector"]["stable_id"] == OTHER
    assert pipeline.missing_fused_ids(conn, [ASKED, "t-twin", OTHER]) == [ASKED]
    # Retag: the track is now named as the asked track is, so it is one of its copies.
    conn.execute(
        "UPDATE tracks SET title = (SELECT title FROM tracks WHERE stable_id = ?) WHERE stable_id = ?", (ASKED, OTHER)
    )
    conn.commit()
    assert OTHER in groups.load(conn).members_of(ASKED)
    docs = _tracks(conn, ASKED, "t-twin", "t-twin-nofile", OTHER)
    for sid in (ASKED, "t-twin", "t-twin-nofile"):
        assert docs[sid]["findings"] == [] and docs[sid]["chosen"] is None and docs[sid]["vector"] is None, sid
    # The retagged track itself still reads its own rows, exactly as before sharing existed.
    assert docs[OTHER]["chosen"] == before["chosen"] and docs[OTHER]["vector"] == before["vector"]
    # Its old answer does not count as the group's result: the group is still to be asked.
    assert pipeline.missing_fused_ids(conn, [ASKED, "t-twin", OTHER]) == [ASKED]
    with pytest.raises(KeyError, match="no jev genre vector"):
        service.similar(conn, "t-twin", 5)
    assert service.similar(conn, OTHER, 5)["stable_id"] == OTHER
    # Asking the group stores one result and clears the old answer from the retagged copy.
    hits = dict(providers_live.hits)
    out = pipeline.infer_tracks(conn, [ASKED])
    assert (
        out.count(pipeline.OUTCOME_DONE) == 1
        and providers_live.hits["/alpha/decisions"] == hits.get("/alpha/decisions", 0) + 1
    )
    docs = _tracks(conn, ASKED, "t-twin", OTHER)
    assert {d["chosen"]["genre"] for d in docs.values()} == {out.results[0].genre}
    assert {f["stable_id"] for d in docs.values() for f in d["findings"]} == {ASKED}


def test_a_copys_own_old_vector_is_never_its_groups_neighbor(tmp_path: Path) -> None:
    """[if] a copy still holds a vector made for its old name [then] no copy of its group lists it as a neighbor, [else stop]."""
    conn = _conn(tmp_path)
    flat = {g: 0.0 for g in taxonomy.NAMES}
    findings.put_vector(conn, ASKED, "jev", flat | {"House": 1.0})
    findings.put_vector(conn, COPY_ID, "jev", flat | {"House": 0.9, "Deep House": 0.1})
    # OTHER was measured under its own name, then retagged into the asked track's group.
    identity = {groups.EVIDENCE_IDENTITY: groups.load(conn).key(OTHER)}
    findings.put_finding(
        conn, findings.Finding(OTHER, "jev", "ok", "Techno (Peak Time / Driving)", 0.9, evidence=identity)
    )
    findings.put_vector(conn, OTHER, "jev", flat | {"House": 0.5, "Techno (Peak Time / Driving)": 0.5})
    assert [s["stable_id"] for s in service.similar(conn, OTHER, 5)["similar"]] == [ASKED, COPY_ID], (
        "control: before the retag"
    )
    conn.execute(
        "UPDATE tracks SET title = (SELECT title FROM tracks WHERE stable_id = ?) WHERE stable_id = ?", (ASKED, OTHER)
    )
    for sid in (OTHER, ASKED, "t-twin"):
        assert [s["stable_id"] for s in service.similar(conn, sid, 5)["similar"]] == [COPY_ID], sid
    # A track outside the group still sees that vector, as it did on main.
    assert [s["stable_id"] for s in service.similar(conn, COPY_ID, 5)["similar"]] == [ASKED, OTHER]


def test_an_attached_finding_is_not_shared_after_its_track_is_retagged(tmp_path: Path) -> None:
    """[if] a track with an attached finding is retagged into another group [then] only it still reads the finding, [else stop]."""
    conn = _conn(tmp_path)
    service.attach_finding(conn, stable_id=OTHER, provider="beatport", genre="Afro House", confidence=1.0, source="s")
    conn.execute(
        "UPDATE tracks SET title = (SELECT title FROM tracks WHERE stable_id = ?) WHERE stable_id = ?", (ASKED, OTHER)
    )
    docs = _tracks(conn, ASKED, "t-twin", OTHER)
    assert docs[ASKED]["chosen"] is None and docs["t-twin"]["chosen"] is None
    assert docs[OTHER]["chosen"] == {"genre": "Afro House", "provider": "beatport", "confidence": 1.0}
    # Control: a finding that recorded nothing about what it was made for cannot be checked, and is shared.
    _put(conn, OTHER, "agent.old", "Hard Techno", 0.4, "2026-10-01")
    findings.update_config(conn, {"precedence": ["agent.old"]})
    assert {d["chosen"]["genre"] for d in _tracks(conn, ASKED, "t-twin", OTHER).values()} == {"Hard Techno"}


def test_a_retag_that_keeps_the_name_keeps_the_sharing(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] a holder's tags change in spelling or artist order only [then] its copies still read its findings, [else stop]."""
    conn = _conn(tmp_path)
    _add(conn, ("t-duo", "Night Bus", '["Tamsin Quell", "Odile Marsh"]', "/m/duo.mp3"))
    _add(conn, ("t-duo-2", "night bus", '["Odile Marsh", "Tamsin Quell"]', None))
    pipeline.infer_tracks(conn, ["t-duo"])
    conn.commit()
    # Same work, other spelling, the artists the other way round and one of them listed twice: the search
    # query differs even in its words, the group does not, and the recorded identity is what says so.
    conn.execute(
        "UPDATE tracks SET title = 'NIGHT  BUS', artists_json = ? WHERE stable_id = 't-duo'",
        ('["odile marsh", "TAMSIN QUELL", "Tamsin Quell"]',),
    )
    conn.commit()
    docs = _tracks(conn, "t-duo", "t-duo-2")
    assert docs["t-duo-2"]["chosen"] == docs["t-duo"]["chosen"] and docs["t-duo"]["chosen"]["provider"] == "odj-fused"
    assert {f["stable_id"] for f in docs["t-duo-2"]["findings"]} == {"t-duo"}
    assert pipeline.missing_fused_ids(conn, ["t-duo", "t-duo-2"]) == []
    # A finding stored before identities were recorded has only its search query to say what it was for.
    original = ("Night Bus", '["Tamsin Quell", "Odile Marsh"]')
    reordered = ("NIGHT  BUS", '["odile marsh", "TAMSIN QUELL"]')
    retag = "UPDATE tracks SET title = ?, artists_json = ? WHERE stable_id = 't-duo'"
    strip = (
        "UPDATE genre_findings SET evidence_json = json_remove(evidence_json, '$.identity') WHERE stable_id = 't-duo'"
    )
    conn.execute(retag, original)
    conn.execute(strip)
    conn.commit()
    assert "identity" not in findings.findings_for(conn, ["t-duo"])["t-duo"][0].evidence
    assert _tracks(conn, "t-duo-2")["t-duo-2"]["chosen"] == docs["t-duo"]["chosen"], "an exact query match is shared"
    # Until its identity is recorded, the query is compared as text: the other artist order is not a match,
    # so the finding is its holder's alone and the group reads as not yet asked (the safe direction).
    conn.execute(retag, reordered)
    conn.commit()
    assert _tracks(conn, "t-duo-2")["t-duo-2"]["chosen"] is None
    # Any write path records the identity while the query still matches exactly ...
    conn.execute(retag, original)
    conn.commit()
    assert pipeline.missing_fused_ids(conn, ["t-duo", "t-duo-2"]) == []
    assert "identity" in findings.findings_for(conn, ["t-duo"])["t-duo"][0].evidence
    # ... and from then on the same reorder changes nothing.
    conn.execute(retag, reordered)
    conn.commit()
    docs = _tracks(conn, "t-duo", "t-duo-2")
    assert docs["t-duo-2"]["chosen"] == docs["t-duo"]["chosen"] and docs["t-duo-2"]["vector"] == docs["t-duo"]["vector"]
    assert pipeline.missing_fused_ids(conn, ["t-duo", "t-duo-2"]) == []
    # Control: a real rename makes that same finding its holder's alone.
    conn.execute("UPDATE tracks SET title = 'Day Train' WHERE stable_id = 't-duo'")
    conn.commit()
    assert _tracks(conn, "t-duo-2")["t-duo-2"]["chosen"] is None


@pytest.mark.parametrize("bound_before_the_retag", [False, True])
def test_a_legacy_finding_is_never_bound_to_another_work_made_of_the_same_words(
    tmp_path: Path, bound_before_the_retag: bool
) -> None:
    """[if] a holder is retagged to another work spelled with the same words [then] its old finding is not shared, [else stop]."""
    conn = _conn(tmp_path)
    _add(conn, ("t-old", "Harbor Lights", '["Quell"]', "/m/a.mp3"), ("t-new", "Quell Lights", '["Harbor"]', "/m/b.mp3"))
    for provider in ("odj-fused", "jev"):
        evidence = {"search_query": providers.search_query("Quell", "Harbor Lights")}
        findings.put_finding(
            conn, findings.Finding("t-old", provider, "ok", "House", 0.8, evidence=evidence, request_digest="d" * 64)
        )
    conn.commit()
    if bound_before_the_retag:
        assert pipeline.missing_fused_ids(conn, ["t-old", "t-new"]) == ["t-new"]
        assert "identity" in findings.findings_for(conn, ["t-old"])["t-old"][0].evidence
    # Artist "Quell", title "Harbor Lights" becomes artist "Harbor", title "Quell Lights": the same words, another work.
    conn.execute("UPDATE tracks SET title = 'Quell Lights', artists_json = '[\"Harbor\"]' WHERE stable_id = 't-old'")
    conn.commit()
    assert groups.load(conn).members_of("t-new") == ("t-new", "t-old")
    assert _tracks(conn, "t-new")["t-new"]["chosen"] is None
    # The old answer covers nothing, before and after the identities are recorded, and is never bound to the new name.
    assert pipeline.missing_fused_ids(conn, ["t-new", "t-old"]) == ["t-new"]
    assert _tracks(conn, "t-new")["t-new"]["chosen"] is None
    recorded = findings.findings_for(conn, ["t-old"])["t-old"][0].evidence.get("identity")
    assert recorded != groups.load(conn).key("t-old"), "an answer about the old name was bound to the new one"
    assert (recorded is not None) == bound_before_the_retag


def _writers_locked_out_at_each_group_read(conn: sqlite3.Connection, state: str, run: Any) -> list[bool]:
    """Run ``run`` and report, for each read of the groups it makes, whether another writer was locked out.

    The groups are read through the ``tracks`` write token (``groups.load``). At each such
    read a second connection tries to take the write lock without waiting: it can only
    fail when ``conn`` already holds it, which is what makes the read and the following
    write one snapshot.
    """
    locked: list[bool] = []

    def on_statement(sql: str) -> None:
        if "FROM sync_write_tokens WHERE table_name = 'tracks'" not in sql:
            return
        other = sqlite3.connect(state, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            locked.append(False)
        except sqlite3.OperationalError:
            locked.append(True)
        finally:
            other.close()

    conn.set_trace_callback(on_statement)
    try:
        run()
    finally:
        conn.set_trace_callback(None)
    return locked


def test_the_group_is_read_under_the_write_lock_when_a_finding_is_stored(
    tmp_path: Path, providers_live: ProviderServer
) -> None:
    """[if] a finding is stored, attached or removed [then] the group it is checked against was read with the write lock held, [else stop]."""
    conn = _conn(tmp_path)
    state = conn.execute("PRAGMA database_list").fetchone()[2]
    pipeline.infer_tracks(conn, [ASKED])  # the search is cached now: a second ask writes nothing before the group read
    planned_then_stored = _writers_locked_out_at_each_group_read(
        conn, state, lambda: pipeline.infer_tracks(conn, [ASKED], force=True)
    )
    # First the identities of older findings are recorded (locked). Planning then reads the groups with no
    # lock (the calls follow, and may take seconds). Storing reads them twice, to check the asked track and
    # then each requested copy, and both of those reads are under the lock.
    assert planned_then_stored == [True, False, True, True]
    attach = lambda: service.attach_finding(  # noqa: E731
        conn, stable_id="t-twin", provider="beatport", genre="House", confidence=1.0, source="s"
    )
    assert _writers_locked_out_at_each_group_read(conn, state, attach) == [True]
    remove = lambda: service.remove_finding(conn, stable_id=ASKED, provider="beatport")  # noqa: E731
    assert _writers_locked_out_at_each_group_read(conn, state, remove) == [True]
    # Each of them committed: nothing is left locked.
    other = sqlite3.connect(state, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    other.close()


# --------------------------------------------------------------- resolution
@pytest.mark.parametrize(
    ("rows", "winner"),
    [
        # Highest confidence wins, whichever copy and however old.
        (
            [
                (ASKED, "House", 0.6, "2026-10-05"),
                ("t-twin", "Techno (Raw / Deep / Hypnotic)", 0.9, "2026-09-01"),
            ],
            1,
        ),
        (
            [
                (ASKED, "House", 0.9, "2026-09-01"),
                ("t-twin", "Techno (Raw / Deep / Hypnotic)", 0.6, "2026-10-05"),
            ],
            0,
        ),
        # Equal confidence: the newest.
        ([(ASKED, "House", 0.8, "2026-09-01"), ("t-twin-nofile", "Hard Techno", 0.8, "2026-10-05")], 1),
        ([("t-twin-nofile", "House", 0.8, "2026-10-05"), (ASKED, "Hard Techno", 0.8, "2026-09-01")], 0),
        # Equal confidence and time: the lowest stable_id, whatever the insert order.
        ([("t-twin-nofile", "House", 0.8, "2026-10-05"), ("t-twin", "Hard Techno", 0.8, "2026-10-05")], 1),
        # An answer beats "unknown", even a newer one, even at confidence zero.
        ([(ASKED, None, None, "2026-10-09"), ("t-twin", "House", 0.0, "2026-09-01")], 1),
    ],
)
def test_disagreeing_copies_resolve_to_one_genre_for_every_copy(
    tmp_path: Path, rows: list[tuple[str, str | None, float | None, str]], winner: int
) -> None:
    conn = _conn(tmp_path)
    for sid, genre, conf, at in rows:
        _put(conn, sid, "odj-fused", genre, conf, at)
    docs = _tracks(conn, ASKED, "t-twin", "t-twin-nofile")
    expected = {"genre": rows[winner][1], "provider": "odj-fused", "confidence": rows[winner][2]}
    assert [d["chosen"] for d in docs.values()] == [expected] * 3
    assert all(len(d["findings"]) == 1 and d["findings"][0]["stable_id"] == rows[winner][0] for d in docs.values())


def test_provider_precedence_is_applied_before_confidence(tmp_path: Path) -> None:
    """[if] one copy has a sure fused guess and another a beatport finding [then] beatport is shown on all, [else stop]."""
    conn = _conn(tmp_path)
    _put(conn, ASKED, "odj-fused", "House", 1.0, "2026-10-09")
    _put(conn, "t-twin-nofile", "beatport", "Afro House", 0.2, "2026-09-01")
    docs = _tracks(conn, ASKED, "t-twin", "t-twin-nofile")
    assert {d["chosen"]["genre"] for d in docs.values()} == {"Afro House"}
    findings.update_config(conn, {"precedence": ["odj-fused", "beatport"]})
    assert {d["chosen"]["genre"] for d in _tracks(conn, ASKED, "t-twin", "t-twin-nofile").values()} == {"House"}


# ------------------------------------------------------- external findings
def test_attach_and_remove_on_one_copy_apply_to_the_group(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    service.attach_finding(
        conn, stable_id="t-twin-nofile", provider="beatport", genre="Afro House", confidence=1.0, source="a"
    )
    docs = _tracks(conn, ASKED, "t-twin", "t-cover")
    assert docs[ASKED]["chosen"] == docs["t-twin"]["chosen"]
    assert docs[ASKED]["chosen"] == {"genre": "Afro House", "provider": "beatport", "confidence": 1.0}
    assert docs["t-cover"]["chosen"] is None, "another artist's track of the same title is not a duplicate"
    # A later attach on another copy replaces it, even with a lower confidence.
    service.attach_finding(conn, stable_id=ASKED, provider="beatport", genre="Tech House", confidence=0.5, source="b")
    assert conn.execute("SELECT stable_id FROM genre_findings WHERE provider = 'beatport'").fetchall() == [(ASKED,)]
    assert _tracks(conn, "t-twin")["t-twin"]["chosen"]["genre"] == "Tech House"
    # Removing through a copy that does not hold the row still removes it.
    assert service.remove_finding(conn, stable_id="t-twin", provider="beatport") is True
    assert _tracks(conn, ASKED)[ASKED]["chosen"] is None
    assert service.remove_finding(conn, stable_id="t-twin", provider="beatport") is False


def test_a_genre_typed_on_one_copy_is_the_user_genre_of_all(tmp_path: Path) -> None:
    """[if] a genre is typed on one copy [then] every copy shows it as user; the library tag stays per row, [else stop]."""
    conn = _conn(tmp_path)
    insert = (
        "INSERT OR REPLACE INTO track_fields (stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES (?, 'genre', ?, ?, 1.0, ?)"
    )
    conn.execute(insert, ("t-twin", '"File Tag"', "inferred", "2026-10-01T00:00:00Z"))
    docs = _tracks(conn, ASKED, "t-twin")
    assert docs[ASKED]["chosen"] is None, "a copy's own file tag is not shared"
    assert docs["t-twin"]["chosen"]["provider"] == "library-tag"
    conn.execute(insert, ("t-twin-nofile", '"Garage"', "webui", "2026-10-02T00:00:00Z"))
    docs = _tracks(conn, ASKED, "t-twin", "t-twin-nofile", "t-cover")
    for sid in (ASKED, "t-twin", "t-twin-nofile"):
        assert docs[sid]["chosen"] == {"genre": "Garage", "provider": "user", "confidence": 1.0}
    assert docs["t-cover"]["chosen"] is None
    # Asked for alone, a copy still reads what was typed on another one.
    assert _tracks(conn, ASKED)[ASKED]["chosen"]["provider"] == "user"
    # Two copies typed: the newest edit is the group's.
    conn.execute(insert, (ASKED, '"Dubstep"', "webui", "2026-10-03T00:00:00Z"))
    assert {d["chosen"]["genre"] for d in _tracks(conn, ASKED, "t-twin-nofile").values()} == {"Dubstep"}
    assert _tracks(conn, "t-twin")["t-twin"]["chosen"]["genre"] == "Dubstep"
    # A typed value that is not JSON stays on its own row: newest of all, and still not shared.
    conn.execute(insert, ("t-twin", "Not JSON", "webui", "2026-10-09T00:00:00Z"))
    docs = _tracks(conn, ASKED, "t-twin", "t-twin-nofile")
    assert docs["t-twin"]["virtual"]["user"] == "Dubstep", "the group's readable edit outranks an unreadable own value"
    assert {d["chosen"]["genre"] for d in docs.values()} == {"Dubstep"}
    conn.execute("DELETE FROM track_fields WHERE field_name = 'genre' AND stable_id != 't-twin'")
    docs = _tracks(conn, ASKED, "t-twin")
    assert docs["t-twin"]["virtual"]["user"] == "Not JSON" and docs[ASKED]["virtual"]["user"] is None


def test_a_whole_library_read_answers_like_a_read_by_id(tmp_path: Path) -> None:
    """[if] the listing reads more ids than the one-query threshold [then] each row gets what a read by id gives, [else stop]."""
    conn = _conn(tmp_path)
    _add(conn, *((f"t-filler-{i:04d}", f"Filler {i}", '["Nobody"]', None) for i in range(groups._READ_ALL_OVER)))
    _put(conn, "t-twin", "odj-fused", "House", 0.7, "2026-10-01")
    _put(conn, ASKED, "beatport", "Afro House", 1.0, "2026-10-02")
    _put(conn, "t-cover", "odj-fused", "Techno (Peak Time / Driving)", 0.9, "2026-10-01")
    _put(conn, "t-filler-0007", "odj-fused", "Dubstep", 0.9, "2026-10-01")
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES ('t-twin-nofile', 'genre', '\"Garage\"', 'webui', 1.0, '2026-10-02T00:00:00Z')"
    )
    grp = groups.load(conn)
    every = [
        r[0] for r in conn.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id != 't-cover'")
    ]
    few = [ASKED, "t-twin-nofile", OTHER, "t-filler-0007"]  # t-cover holds a finding and is never asked for
    assert len(few) <= groups._READ_ALL_OVER < len(every), "one read by id chunks, one in a single query"
    by_id = (groups.findings_for(conn, grp, few), groups.user_genres(conn, grp, few))
    assert set(by_id[0]) == {ASKED, "t-twin-nofile", "t-filler-0007"}
    assert by_id[1] == {ASKED: "Garage", "t-twin-nofile": "Garage"}
    assert {f.provider for f in by_id[0]["t-twin-nofile"]} == {"odj-fused", "beatport"}
    whole = (groups.findings_for(conn, grp, every), groups.user_genres(conn, grp, every))
    assert ({s: whole[0][s] for s in few if s in whole[0]}, {s: whole[1][s] for s in few if s in whole[1]}) == by_id
    assert set(whole[0]) == {ASKED, "t-twin", "t-twin-nofile", "t-filler-0007"}, "t-cover was not asked for"
    assert set(whole[1]) == {ASKED, "t-twin", "t-twin-nofile"}
    only = groups.findings_for(conn, grp, every, providers=["beatport"])
    assert {f.provider for rows in only.values() for f in rows} == {"beatport"} and len(only) == 3


def test_placeholder_tracks_are_never_duplicates_of_each_other(tmp_path: Path) -> None:
    """[if] two tracks are both ``ID`` by ``ID`` [then] a genre given to one is not shown on the other, [else stop]."""
    conn = _conn(tmp_path)
    _add(conn, ("t-id-2", "id", '["ID"]', "/m/id2.mp3"), ("t-untitled", "Untitled", '["Someone"]', None))
    _add(conn, ("t-untitled-2", "untitled", '["someone"]', None))
    # Full-width letters and an artist listed twice normalize to the same placeholder; two rows of each,
    # so judging the raw text would join each pair.
    wide = "\uff29\uff24"
    _add(conn, ("t-id-wide", wide, json.dumps([wide]), None), ("t-id-wide-2", wide, json.dumps([wide]), None))
    _add(conn, ("t-id-twice", "ID", '["ID", "id"]', None), ("t-id-twice-2", "ID", '["ID", "id"]', None))
    grp = groups.load(conn)
    for sid in (PLACEHOLDER_ID, "t-id-2", "t-untitled", "t-id-wide", "t-id-wide-2", "t-id-twice", "t-id-twice-2"):
        assert grp.members_of(sid) == (sid,), sid
    service.attach_finding(conn, stable_id="t-id-2", provider="beatport", genre="House", confidence=1.0, source="x")
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES ('t-untitled', 'genre', '\"Garage\"', 'webui', 1.0, '2026-10-02T00:00:00Z')"
    )
    docs = _tracks(conn, PLACEHOLDER_ID, "t-id-2", "t-untitled", "t-untitled-2")
    assert docs["t-id-2"]["chosen"]["genre"] == "House" and docs[PLACEHOLDER_ID]["chosen"] is None
    assert docs["t-untitled"]["chosen"]["genre"] == "Garage" and docs["t-untitled-2"]["chosen"] is None
    # Control: real names with the same spelling difference DO group.
    assert grp.members_of("t-twin") == (ASKED, "t-twin", "t-twin-nofile")


# ------------------------------------------------------------------ file tags
def test_file_tags_are_written_per_file_and_only_where_a_file_exists(tmp_path: Path) -> None:
    """[if] a group's genre is written [then] each present copy's own file gets it and a copy with no file is refused, [else stop]."""
    music = tmp_path / "music"
    music.mkdir()
    one, two = music / "one.mp3", music / "two.mp3"
    for path in (one, two):
        shutil.copy2(SOURCES / "src-320.mp3", path)
    data, state = library(tmp_path, audio=one)
    conn = sqlite3.connect(str(state))
    findings.ensure_schema(conn)
    _add(conn, ("t-copy-2", "COPY", '["someone"]', str(two)), ("t-copy-gone", "Copy", '["Someone"]', None))
    service.attach_finding(
        conn, stable_id="t-copy-gone", provider="beatport", genre="Tech House", confidence=1, source="x"
    )
    findings.update_config(conn, {"write_file_tags": True, "write_roots": [str(music)]})
    service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")
    assert read_tags(one).genre == "Tech House" and read_tags(two).genre != "Tech House", "one write, one file"
    service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy-2")
    assert read_tags(two).genre == "Tech House"
    with pytest.raises(tag_write.TagWriteRefused, match="no audio file on this machine"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy-gone")
    assert sorted(w["stable_id"] for w in tag_write.journal(conn)) == ["t-copy", "t-copy-2"]
