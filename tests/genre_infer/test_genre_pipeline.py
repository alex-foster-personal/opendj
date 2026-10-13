"""Genre pipeline, findings store and precedence (GENRE-03, GENRE-04, GENRE-07).

- [if] infer_tracks runs with both keys [then] each track gets jev, deepseek and odj-fused findings plus JEV's full vector, [else stop].
- [if] the same track is inferred again with nothing changed [then] it is up_to_date and no provider is called again, [else stop].
- [if] a key is missing [then] ProviderUnavailable names the key and nothing is stored, [else stop].
- [if] a provider call fails [then] its finding is unknown with a reason, never a genre, [else stop].
- [if] an external provider in the precedence has a finding [then] it is chosen over odj-fused, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.genre_infer import findings, pipeline, providers, service, taxonomy
from tests.genre_infer.pipeline_support import ProviderServer, captured, library

pytestmark = pytest.mark.requirement("GENRE-03")


def _conn(state: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(state))
    findings.ensure_schema(conn)
    return conn


# ------------------------------------------------------------------ pipeline
def test_infer_stores_three_findings_and_the_full_vector(tmp_path: Path, providers_live: ProviderServer) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    summary = pipeline.infer_tracks(conn, ["t-fred"])
    assert summary.count(pipeline.OUTCOME_DONE) == 1, summary.as_dict()
    rows = {f.provider: f for f in findings.findings_for(conn, ["t-fred"])["t-fred"]}
    assert set(rows) == {"jev", "deepseek", "odj-fused"}
    jev_probs = captured("jev_decision")["response"]["answers"]["genre"]["probabilities"]
    assert rows["jev"].genre == max(jev_probs, key=lambda g: jev_probs[g])
    ds = rows["deepseek"]
    assert ds.genre == "Dance / Pop" and ds.confidence == 0.3
    # DeepSeek is not sure (< 0.9), so the fused genre is the equal-weight blend.
    spread = providers.answer_distribution(providers.Answer("deepseek", ds.genre, ds.confidence, None, None, None))
    blend = {g: 0.5 * jev_probs[g] + 0.5 * spread[g] for g in taxonomy.NAMES}
    fused = rows["odj-fused"]
    assert fused.genre == max(blend, key=lambda g: blend[g])
    assert fused.evidence["rule"] == "blend jev+deepseek"
    assert fused.request_digest and fused.request_digest == rows["jev"].request_digest
    vec = findings.vectors(conn)["t-fred"]
    assert set(vec) == set(taxonomy.NAMES) and len(vec) == 47
    # The text sent carries the artist's other library titles and the web snippets.
    sent = providers_live.bodies["/alpha/decisions"][0]["state"]
    assert "Fred again.. - Jungle" in sent and "Web search results for this track:" in sent
    assert providers_live.bodies["/search"][0] == {"q": "Fred again.. Delilah (pull me out of this) genre", "num": 10}
    assert providers_live.bodies["/v1/chat/completions"][0]["reasoning"] == {"enabled": False}
    # Cost: the models' billed cost from the replies, plus one search at the configured price.
    assert summary.paid_searches == 1
    assert summary.search_cost_usd == providers.serper_usd_per_search() == 0.001
    model_cost = summary.cost_usd - summary.search_cost_usd
    assert 0 < model_cost < 0.0005, "model cost must come from the replies' usage.cost"


def test_second_run_is_up_to_date_and_calls_nothing(tmp_path: Path, providers_live: ProviderServer) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    pipeline.infer_tracks(conn, ["t-fred"])
    before = dict(providers_live.hits)
    again = pipeline.infer_tracks(conn, ["t-fred"])
    assert again.count(pipeline.OUTCOME_UP_TO_DATE) == 1
    assert dict(providers_live.hits) == before, "a cached search or an unchanged request was paid for again"
    forced = pipeline.infer_tracks(conn, ["t-fred"], force=True)
    assert forced.count(pipeline.OUTCOME_DONE) == 1
    assert providers_live.hits["/search"] == before["/search"], "force re-asks the models but reuses the cached search"
    assert providers_live.hits["/alpha/decisions"] == before["/alpha/decisions"] + 1


def test_placeholder_titles_are_skipped_without_a_call(tmp_path: Path, providers_live: ProviderServer) -> None:
    _data, state = library(tmp_path)
    summary = pipeline.infer_tracks(_conn(state), ["t-id"])
    assert [r.reason for r in summary.results] == [pipeline.SKIP_NO_ARTIST_TITLE]
    assert sum(providers_live.hits.values()) == 0


@pytest.mark.parametrize("missing", [providers.OPENROUTER_KEY_ENV, providers.SERPER_KEY_ENV])
def test_missing_key_names_it_and_stores_nothing(
    tmp_path: Path, providers_live: ProviderServer, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _data, state = library(tmp_path)
    monkeypatch.setenv(missing, "  ")
    conn = _conn(state)
    with pytest.raises(providers.ProviderUnavailable) as err:
        pipeline.infer_tracks(conn, ["t-fred"])
    assert err.value.key_env == missing and missing in str(err.value)
    assert findings.findings_for(conn, ["t-fred"]) == {}
    assert sum(providers_live.hits.values()) == 0


def test_a_failed_model_call_is_unknown_never_a_genre(tmp_path: Path, providers_live: ProviderServer) -> None:
    _data, state = library(tmp_path)
    providers_live.fail.add("/v1/chat/completions")
    conn = _conn(state)
    pipeline.infer_tracks(conn, ["t-fred"])
    rows = {f.provider: f for f in findings.findings_for(conn, ["t-fred"])["t-fred"]}
    assert rows["deepseek"].status == "unknown" and rows["deepseek"].genre is None
    assert "HTTP 500" in (rows["deepseek"].reason or "")
    assert rows["odj-fused"].evidence["rule"] == "jev only (deepseek unknown)"
    # A failed call is not an answer: the next run asks again instead of reading it as up to date.
    providers_live.fail.clear()
    again = pipeline.infer_tracks(conn, ["t-fred"])
    assert again.count(pipeline.OUTCOME_DONE) == 1, again.as_dict()
    assert {f.provider: f for f in findings.findings_for(conn, ["t-fred"])["t-fred"]}["deepseek"].status == "ok"
    providers_live.fail.update({"/alpha/decisions", "/v1/chat/completions"})
    out = pipeline.infer_tracks(conn, ["t-fred"], force=True)
    assert out.count(pipeline.OUTCOME_FAILED) == 1
    fused = {f.provider: f for f in findings.findings_for(conn, ["t-fred"])["t-fred"]}["odj-fused"]
    assert fused.status == "unknown" and fused.genre is None


def test_a_failed_search_fails_the_track_and_caches_nothing(tmp_path: Path, providers_live: ProviderServer) -> None:
    _data, state = library(tmp_path)
    providers_live.fail.add("/search")
    conn = _conn(state)
    out = pipeline.infer_tracks(conn, ["t-fred"])
    assert out.count(pipeline.OUTCOME_FAILED) == 1 and "search failed" in (out.results[0].reason or "")
    assert conn.execute("SELECT COUNT(*) FROM genre_search_cache").fetchone()[0] == 0
    assert providers_live.hits["/alpha/decisions"] == 0


# -------------------------------------------------------------------- fusion
def _answer(genre: str | None, conf: float | None, probs: dict[str, float] | None = None) -> providers.Answer:
    return providers.Answer("x", genre, conf, probs, None, None)


def test_fusion_takes_deepseek_when_sure_and_blends_otherwise() -> None:
    jev = {g: 0.0 for g in taxonomy.NAMES} | {"House": 0.9, "Tech House": 0.1}
    sure = providers.fuse(_answer("House", 0.9, jev), _answer("Afro House", 0.9))
    assert (sure.genre, sure.rule) == ("Afro House", "deepseek>=0.9")
    unsure = providers.fuse(_answer("House", 0.9, jev), _answer("Afro House", 0.89))
    assert unsure.genre == "House" and unsure.rule == "blend jev+deepseek"
    # Control the other way: a confident JEV does NOT override a sure DeepSeek.
    assert providers.fuse(_answer("House", 1.0, jev), _answer("Techno (Peak Time / Driving)", 0.95)).genre != "House"
    assert providers.fuse(_answer(None, None), _answer(None, None)).genre is None


# ------------------------------------------------------------- precedence
def test_external_finding_outranks_the_fused_guess_only_when_listed(
    tmp_path: Path, providers_live: ProviderServer
) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    pipeline.infer_tracks(conn, ["t-fred"])
    fused = service.genres_for(conn, ["t-fred"])["tracks"]["t-fred"]["chosen"]
    assert fused["provider"] == "odj-fused"
    service.attach_finding(
        conn,
        stable_id="t-fred",
        provider="beatport",
        genre="melodic house & techno",
        confidence=1.0,
        source="https://example.test/track/1",
    )
    chosen = service.genres_for(conn, ["t-fred"])["tracks"]["t-fred"]["chosen"]
    assert chosen == {"genre": "Melodic House & Techno", "provider": "beatport", "confidence": 1.0}
    service.attach_finding(
        conn, stable_id="t-fred", provider="agent.discogs", genre="Electronic", confidence=0.6, source="discogs"
    )
    assert service.genres_for(conn, ["t-fred"])["tracks"]["t-fred"]["chosen"]["provider"] == "beatport"
    findings.update_config(conn, {"precedence": ["agent.discogs", "beatport"]})
    assert service.genres_for(conn, ["t-fred"])["tracks"]["t-fred"]["chosen"]["genre"] == "Electronic"


def test_library_tag_is_the_last_resort_and_user_edit_wins(tmp_path: Path) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    doc = service.genres_for(conn, ["t-fred2"])["tracks"]["t-fred2"]
    assert doc["chosen"] == {"genre": "Electronic", "provider": "library-tag", "confidence": None}
    conn.execute(
        "INSERT OR REPLACE INTO track_fields (stable_id, field_name, value_json, source, confidence, modified_at) "
        "VALUES ('t-fred2', 'genre', '\"Garage\"', 'webui', 1.0, '2026-10-12T00:00:00Z')"
    )
    assert service.genres_for(conn, ["t-fred2"])["tracks"]["t-fred2"]["chosen"]["provider"] == "user"


@pytest.mark.parametrize(
    ("provider", "message"),
    [("odj-fused", "reserved"), ("user", "reserved"), ("Bad Id", "must match")],
)
def test_attach_refuses_reserved_and_malformed_providers(tmp_path: Path, provider: str, message: str) -> None:
    _data, state = library(tmp_path)
    with pytest.raises(findings.GenreInputError, match=message):
        service.attach_finding(
            _conn(state), stable_id="t-fred", provider=provider, genre="House", confidence=1, source="x"
        )


def test_attach_refuses_unknown_tracks_and_missing_source(tmp_path: Path) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    with pytest.raises(service.UnknownTrack):
        service.attach_finding(conn, stable_id="nope", provider="beatport", genre="House", confidence=1, source="x")
    with pytest.raises(findings.GenreInputError, match="source is required"):
        service.attach_finding(conn, stable_id="t-fred", provider="beatport", genre="House", confidence=1, source=" ")


def test_config_rejects_unknown_keys_and_relative_roots(tmp_path: Path) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    with pytest.raises(findings.GenreInputError, match="unknown genre config keys"):
        findings.update_config(conn, {"precedense": ["user"]})
    with pytest.raises(findings.GenreInputError, match="absolute"):
        findings.update_config(conn, {"write_roots": ["relative/dir"]})
    with pytest.raises(findings.GenreInputError, match="distinct provider ids"):
        findings.update_config(conn, {"precedence": [["user"], "beatport"]})
    assert findings.read_config(conn) == findings.GenreConfig()


# --------------------------------------------------------------- similarity
def test_similar_ranks_by_cosine_and_refuses_unmeasured_tracks(tmp_path: Path) -> None:
    _data, state = library(tmp_path)
    conn = _conn(state)
    base = {g: 0.0 for g in taxonomy.NAMES}
    findings.put_vector(conn, "t-fred", "jev", base | {"House": 0.8, "Deep House": 0.2})
    findings.put_vector(conn, "t-fred2", "jev", base | {"House": 0.7, "Deep House": 0.3})
    findings.put_vector(conn, "t-copy", "jev", base | {"Drum & Bass": 1.0})
    ranked = findings.similar_tracks(conn, "t-fred")
    assert [sid for sid, _ in ranked] == ["t-fred2", "t-copy"]
    assert ranked[0][1] > 0.9 and ranked[1][1] == 0.0
    with pytest.raises(KeyError, match="no jev genre vector"):
        findings.similar_tracks(conn, "t-id")


def test_import_search_cache_makes_the_search_free(tmp_path: Path, providers_live: ProviderServer) -> None:
    _data, state = library(tmp_path)
    cache = tmp_path / "serper.json"
    cache.write_text(
        '{"Fred again.. Delilah (pull me out of this) genre": {"text": "Delilah: house"}}', encoding="utf-8"
    )
    conn = _conn(state)
    assert findings.import_search_cache(conn, cache, "q1") == 1
    assert findings.import_search_cache(conn, cache, "q1") == 0
    pipeline.infer_tracks(conn, ["t-fred"])
    assert providers_live.hits["/search"] == 0
    assert "Delilah: house" in providers_live.bodies["/alpha/decisions"][0]["state"]


def test_a_partial_jev_vector_is_unknown_but_its_cost_is_kept(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] JEV bills a reply whose vector is partial [then] the finding is unknown and the cost still counts, [else stop]."""

    def drop_one(reply: dict) -> dict:
        probs = dict(reply["answers"]["genre"]["probabilities"])
        probs.pop(next(iter(probs)))
        return {**reply, "answers": {"genre": {**reply["answers"]["genre"], "probabilities": probs}}}

    providers_live.transform["/alpha/decisions"] = drop_one
    _data, state = library(tmp_path)
    conn = _conn(state)
    pipeline.infer_tracks(conn, ["t-fred"])
    jev_row = {f.provider: f for f in findings.findings_for(conn, ["t-fred"])["t-fred"]}["jev"]
    assert jev_row.status == "unknown" and "does not cover the taxonomy" in (jev_row.reason or "")
    assert jev_row.cost_usd == captured("jev_decision")["response"]["usage"]["cost"]


def test_enqueue_missing_retries_an_unknown_fused_finding(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] both calls failed for a track [then] infer-everything-untagged picks it up again, [else stop]."""
    _data, state = library(tmp_path)
    conn = _conn(state)
    providers_live.fail.update({"/alpha/decisions", "/v1/chat/completions"})
    pipeline.infer_tracks(conn, ["t-fred"])
    assert pipeline.missing_fused_ids(conn, ["t-fred", "t-fred2"]) == ["t-fred", "t-fred2"]
    providers_live.fail.clear()
    pipeline.infer_tracks(conn, ["t-fred"])
    assert pipeline.missing_fused_ids(conn, ["t-fred", "t-fred2"]) == ["t-fred2"]


def test_similar_leaves_out_deleted_tracks(tmp_path: Path) -> None:
    """[if] a track with a genre vector is deleted [then] it is no longer anyone's neighbor, [else stop]."""
    _data, state = library(tmp_path)
    conn = _conn(state)
    base = {g: 0.0 for g in taxonomy.NAMES}
    for sid in ("t-fred", "t-fred2", "t-copy"):
        findings.put_vector(conn, sid, "jev", base | {"House": 1.0})
    conn.execute("UPDATE tracks SET deleted_at = '2026-10-12T00:00:00Z' WHERE stable_id = 't-fred2'")
    assert [sid for sid, _ in findings.similar_tracks(conn, "t-fred")] == ["t-copy"]


def test_a_rekordbox_genre_is_the_library_tag_for_choice_and_for_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] library-tag outranks the fused guess and rekordbox has a genre [then] it is chosen and no file is written, [else stop]."""
    from apps.adapters.rekordbox import config as rb_config
    from apps.genre_infer import tag_write

    data, state = library(tmp_path)
    master = data / "master.plain.db"
    m = sqlite3.connect(str(master))
    m.executescript(
        "CREATE TABLE djmdGenre (ID TEXT, Name TEXT, rb_local_deleted INTEGER);"
        "CREATE TABLE djmdContent (ID TEXT, GenreID TEXT, rb_local_deleted INTEGER);"
        "INSERT INTO djmdGenre VALUES ('g1', 'Garage', 0);"
        "INSERT INTO djmdContent VALUES ('rb1', 'g1', 0);"
    )
    m.commit()
    m.close()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master)
    conn = _conn(state)
    conn.execute("INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES ('t-copy', 'rekordbox', 'rb1')")
    service.attach_finding(conn, stable_id="t-copy", provider="beatport", genre="House", confidence=1.0, source="x")
    findings.update_config(
        conn, {"precedence": ["library-tag", "beatport"], "write_file_tags": True, "write_roots": [str(tmp_path)]}
    )
    chosen = service.genres_for(conn, ["t-copy"])["tracks"]["t-copy"]["chosen"]
    assert chosen == {"genre": "Garage", "provider": "library-tag", "confidence": None}
    with pytest.raises(tag_write.TagWriteRefused, match="already the library's own tag"):
        service.write_chosen_tag(conn, data_dir=data, stable_id="t-copy")


def test_a_billed_search_with_an_unusable_body_is_still_counted(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] the search answers with no results list [then] the track fails and the search is still costed, [else stop]."""
    providers_live.transform["/search"] = lambda reply: {**reply, "organic": None}
    _data, state = library(tmp_path)
    out = pipeline.infer_tracks(_conn(state), ["t-fred"])
    assert out.count(pipeline.OUTCOME_FAILED) == 1
    assert out.paid_searches == 1 and out.search_cost_usd == providers.serper_usd_per_search()


def test_the_cli_reads_the_rekordbox_genre_from_its_own_master(tmp_path: Path) -> None:
    """[if] the CLI is given --master-db [then] library-tag comes from that master, [else stop]."""
    from apps.genre_infer import __main__ as cli

    data, state = library(tmp_path)
    master = tmp_path / "elsewhere" / "master.db"
    master.parent.mkdir()
    m = sqlite3.connect(str(master))
    m.executescript(
        "CREATE TABLE djmdGenre (ID TEXT, Name TEXT, rb_local_deleted INTEGER);"
        "CREATE TABLE djmdContent (ID TEXT, GenreID TEXT, rb_local_deleted INTEGER);"
        "INSERT INTO djmdGenre VALUES ('g1', 'Garage', 0); INSERT INTO djmdContent VALUES ('rb1', 'g1', 0);"
    )
    m.commit()
    m.close()
    conn = _conn(state)
    conn.execute("INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES ('t-copy', 'rekordbox', 'rb1')")
    conn.commit()
    conn.close()
    out = tmp_path / "out.json"
    import contextlib
    import io
    import json as _json

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["--data-dir", str(data), "--master-db", str(master), "findings", "t-copy"])
    assert rc == 0
    doc = _json.loads(buf.getvalue())
    out.write_text(buf.getvalue(), encoding="utf-8")
    assert doc["tracks"]["t-copy"]["chosen"] == {"genre": "Garage", "provider": "library-tag", "confidence": None}


def test_a_billed_reply_that_is_not_json_is_paid_once_and_not_retried(
    tmp_path: Path, providers_live: ProviderServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the search answers 200 with a body that is not JSON [then] it is costed once and not retried, [else stop]."""
    monkeypatch.setattr(providers, "MAX_ATTEMPTS", 4)
    providers_live.transform["/search"] = lambda _reply: b"<html>oops</html>"
    _data, state = library(tmp_path)
    out = pipeline.infer_tracks(_conn(state), ["t-fred"])
    assert providers_live.hits["/search"] == 1
    assert out.count(pipeline.OUTCOME_FAILED) == 1 and out.paid_searches == 1


def test_a_lost_claim_rolls_the_store_back_but_keeps_the_cost(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] settlement reports the job was cancelled [then] no finding is stored and the cost still counts, [else stop]."""
    _data, state = library(tmp_path)
    conn = sqlite3.connect(str(state), isolation_level=None)
    findings.ensure_schema(conn)
    out = pipeline.infer_tracks(conn, ["t-fred"], settle=lambda _result: False)
    assert out.count(pipeline.OUTCOME_CANCELLED) == 1
    assert findings.findings_for(conn, ["t-fred"]) == {}
    assert out.paid_searches == 1 and out.cost_usd > out.search_cost_usd > 0
    assert out.cost_per_1k_usd is not None


def test_a_sample_by_genre_tag_is_not_eligible(tmp_path: Path) -> None:
    """[if] a present track is tagged Loop Samples outside the sampler folder [then] it is not queued, [else stop]."""
    _data, state = library(tmp_path)
    conn = _conn(state)
    for sid in ("t-fred", "t-fred2"):
        f = tmp_path / f"{sid}.mp3"
        f.write_bytes(b"\0" * 1024)
        conn.execute("UPDATE tracks SET file_path = ? WHERE stable_id = ?", (str(f), sid))
    conn.execute(
        "UPDATE track_fields SET value_json = '\"Loop Samples\"' WHERE stable_id = 't-fred2' AND field_name = 'genre'"
    )
    conn.commit()
    assert pipeline.eligible_ids(conn) == ["t-fred"]


@pytest.mark.parametrize("limit", ["-1", "0", "501"])
def test_the_cli_refuses_an_unbounded_run(tmp_path: Path, limit: str) -> None:
    """[if] the CLI is given a limit outside 1 to 500 [then] it refuses before any call, [else stop]."""
    from apps.genre_infer import __main__ as cli

    data, _state = library(tmp_path)
    with pytest.raises(SystemExit) as err:
        cli.main(["--data-dir", str(data), "infer", "--all-missing", "--limit", limit])
    assert err.value.code == 2


def _add_duplicate_of_fred(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, created_at, updated_at) "
        "SELECT 't-fred-dup', stable_id_tier, title, artists_json, '/m/fred-copy.mp3', created_at, updated_at "
        "FROM tracks WHERE stable_id = 't-fred'"
    )
    conn.commit()


def test_duplicates_in_one_batch_share_one_paid_search(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] two tracks in a batch have the same artist and title [then] one search is paid for both, [else stop]."""
    _data, state = library(tmp_path)
    conn = _conn(state)
    _add_duplicate_of_fred(conn)
    out = pipeline.infer_tracks(conn, ["t-fred", "t-fred-dup"])
    assert out.count(pipeline.OUTCOME_DONE) == 2
    assert providers_live.hits["/search"] == 1 and out.paid_searches == 1


def test_a_malformed_search_price_fails_before_any_call(
    tmp_path: Path, providers_live: ProviderServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the search price setting is malformed [then] the run fails before any provider is called, [else stop]."""
    monkeypatch.setenv(providers.SERPER_PRICE_ENV, "lots")
    _data, state = library(tmp_path)
    with pytest.raises(ValueError, match="lots"):
        pipeline.infer_tracks(_conn(state), ["t-fred"])
    assert sum(providers_live.hits.values()) == 0


def test_a_vector_that_does_not_sum_to_one_is_unknown(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] JEV returns a full but all-zero vector [then] its finding is unknown, never a genre, [else stop]."""

    def zero(reply: dict) -> dict:
        probs = {g: 0.0 for g in reply["answers"]["genre"]["probabilities"]}
        return {**reply, "answers": {"genre": {**reply["answers"]["genre"], "probabilities": probs}}}

    providers_live.transform["/alpha/decisions"] = zero
    providers_live.fail.add("/v1/chat/completions")
    _data, state = library(tmp_path)
    conn = _conn(state)
    pipeline.infer_tracks(conn, ["t-fred"])
    rows = {f.provider: f for f in findings.findings_for(conn, ["t-fred"])["t-fred"]}
    assert rows["jev"].status == "unknown" and rows["odj-fused"].genre is None


def test_a_cached_search_from_another_query_version_is_not_used(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] the only cached result was made by an older query version [then] the search is made again, [else stop]."""
    _data, state = library(tmp_path)
    conn = _conn(state)
    findings.put_search(conn, "Fred again.. Delilah (pull me out of this) genre", "q0", "stale text")
    pipeline.infer_tracks(conn, ["t-fred"])
    assert providers_live.hits["/search"] == 1
    assert "stale text" not in providers_live.bodies["/alpha/decisions"][0]["state"]


def test_the_cli_exits_3_when_every_attempted_call_failed(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] a CLI run skips one track and every attempted track fails [then] it exits 3, [else stop]."""
    from apps.genre_infer import __main__ as cli

    data, _state = library(tmp_path)
    providers_live.fail.add("/search")
    rc = cli.main(["--data-dir", str(data), "infer", "--stable-id", "t-id", "--stable-id", "t-fred"])
    assert rc == 3


def test_overlapping_runs_pay_for_one_search(tmp_path: Path, providers_live: ProviderServer) -> None:
    """[if] two runs for the same track overlap [then] the search and the model calls are paid for once, [else stop]."""
    import threading
    import time

    _data, state = library(tmp_path)
    _conn(state).close()

    def slow(reply: dict) -> dict:
        time.sleep(0.5)  # holds the first run inside its search, past the second run's start
        return reply

    providers_live.transform["/search"] = slow
    outcomes: list[str] = []

    def run() -> None:
        conn = sqlite3.connect(str(state), check_same_thread=False)
        try:
            outcomes.append(pipeline.infer_tracks(conn, ["t-fred"]).results[0].outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == [pipeline.OUTCOME_DONE, pipeline.OUTCOME_UP_TO_DATE]
    assert providers_live.hits["/search"] == 1
    assert providers_live.hits["/v1/chat/completions"] == 1
