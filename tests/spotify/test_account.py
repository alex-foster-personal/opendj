"""Live account playlist listing (SPOT-6555): all pages, not just imported ones."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from spotipy import SpotifyException
from spotipy.exceptions import SpotifyOauthError

from apps.spotify import account
from tests.spotify.spotify_loopback import (
    DROP,
    TokenEndpoint,
    WebApi,
    closed_port_url,
    playlist,
    spotify_schema_errors,
    write_token,
)


def _client(tmp_path: Path, api: WebApi) -> Any:
    """spotipy's real client on a cached sign-in, aimed at the loopback Web API."""
    token = tmp_path / "token.json"
    write_token(token)
    return account.open_account_client(
        env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"},
        token_path=token,
        client_id_path=tmp_path / "cid.json",
        api_base=api.api_base,
    )


def test_loopback_bodies_are_held_to_spotifys_published_schema() -> None:
    """The loopback's playlist rows fit Spotify's schema, and the check bites."""
    page = {"href": "h", "items": [playlist(1), playlist(2, key="tracks")], "limit": 50,
            "next": None, "offset": 0, "previous": None, "total": 2}
    assert spotify_schema_errors("PagingPlaylistObject", page) == []
    # Off-contract bodies are refused: a missing required total, a null total,
    # a numeric name, and an owner of the wrong type.
    assert spotify_schema_errors("PagingPlaylistObject", {k: v for k, v in page.items() if k != "total"})
    assert spotify_schema_errors("PagingPlaylistObject", {**page, "total": None})
    assert spotify_schema_errors("PagingPlaylistObject", {**page, "items": [{**playlist(1), "name": 7}]})
    owner = {**playlist(1)["owner"], "type": "artist"}
    assert spotify_schema_errors("PagingPlaylistObject", {**page, "items": [{**playlist(1), "owner": owner}]})


def test_an_off_schema_body_fails_the_test_that_served_it(tmp_path: Path) -> None:
    row = {**playlist(1), "public": "yes"}
    with (
        pytest.raises(AssertionError, match="outside Spotify's schema"),
        WebApi([row]) as api,
        pytest.raises(SpotifyException),  # spotipy sees the loopback's 500
    ):
        account.list_account_playlists(_client(tmp_path, api))


@pytest.mark.requirement("SPOT-6555")
def test_lists_every_page_not_just_the_first(tmp_path: Path) -> None:
    """[if] the account has 123 playlists [then] all 123 come back over 3 pages, [else stop]."""
    with WebApi([playlist(i) for i in range(123)]) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    assert len(listing.playlists) == 123
    assert listing.total_reported == 123
    # Three pages of 50, walked once by offset, each with the cached bearer token.
    assert api.calls == [(50, 0), (50, 50), (50, 100)]
    assert set(api.auth_headers) == {"Bearer BQcached-access"}
    assert [p.spotify_id for p in listing.playlists][:2] == ["pl0000", "pl0001"]


@pytest.mark.requirement("SPOT-6555")
def test_short_pages_still_reach_the_end(tmp_path: Path) -> None:
    """[if] Spotify returns short pages [then] the offset follows them to the end, [else stop]."""
    with WebApi([playlist(i) for i in range(70)], page_cap=20) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    assert len(listing.playlists) == 70
    assert [offset for _, offset in api.calls] == [0, 20, 40, 60]


def test_owned_followed_and_null_rows(tmp_path: Path) -> None:
    rows: list[Any] = [playlist(1), playlist(2, owner="someone-else"), None, playlist(3, key="tracks")]
    with WebApi(rows, allow_null_items=True) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    ids = [p.spotify_id for p in listing.playlists]
    assert ids == ["pl0001", "pl0002", "pl0003"]
    by_id = {p.spotify_id: p for p in listing.playlists}
    assert by_id["pl0001"].owned is True
    assert by_id["pl0002"].owned is False
    assert by_id["pl0002"].owner_name == "SOMEONE-ELSE"
    # Old "tracks" and new "items" shapes both give a count.
    assert by_id["pl0003"].track_count == 3
    assert by_id["pl0001"].open_url == "https://open.spotify.com/playlist/pl0001"


@pytest.mark.requirement("SPOT-6555")
def test_a_playlist_listed_twice_is_listed_once(tmp_path: Path) -> None:
    """[if] a playlist id repeats across the walk [then] it is listed once, in its first place, [else stop].

    A repeat means the list moved under the offsets; the next refresh sees it whole."""
    rows: list[Any] = [playlist(1), playlist(2), playlist(1)]
    with WebApi(rows) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    assert [p.spotify_id for p in listing.playlists] == ["pl0001", "pl0002"]
    assert listing.total_reported == 3


def _swap_first_for_new(api: WebApi, call: int) -> None:
    """Before page 2: unfollow the first playlist and follow a new one, so the count stays the same."""
    if call == 2:
        api.playlists = api.playlists[1:] + [playlist(9000 + call)]


def test_a_same_count_change_mid_walk_is_one_walk(tmp_path: Path) -> None:
    """A change made during the walk is not chased: one walk, and the next refresh lists the shifted row.

    The change shifts every later offset by one, so this walk skips pl0020."""
    with WebApi([playlist(i) for i in range(70)], page_cap=20, on_page=_swap_first_for_new) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
        assert [offset for _, offset in api.calls] == [0, 20, 40, 60]
        ids = [p.spotify_id for p in listing.playlists]
        assert "pl0020" not in ids and ids[-1] == "pl9002"
        refreshed = [p.spotify_id for p in account.list_account_playlists(_client(tmp_path, api)).playlists]
    assert refreshed == [f"pl{i:04d}" for i in range(1, 70)] + ["pl9002"]


def test_a_single_page_listing_is_read_once(tmp_path: Path) -> None:
    """One page is one request."""
    with WebApi([playlist(i) for i in range(3)]) as api:
        assert len(account.list_account_playlists(_client(tmp_path, api)).playlists) == 3
    assert api.calls == [(50, 0)]


@pytest.mark.parametrize(
    "sent",
    [
        "https://evil.example/x",
        # Spotify's own host, but the wrong page or the wrong playlist.
        "https://open.spotify.com/album/pl0005",
        "https://open.spotify.com/user/me",
        "https://open.spotify.com/playlist/pl0009",
    ],
)
def test_open_url_is_built_from_the_playlist_id(tmp_path: Path, sent: str) -> None:
    """[if] external_urls names another page [then] the row still opens its own playlist, [else stop]."""
    row = playlist(5)
    row["external_urls"] = {"spotify": sent}
    with WebApi([row]) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    assert listing.playlists[0].open_url == "https://open.spotify.com/playlist/pl0005"


def test_absent_or_null_optional_fields_still_list(tmp_path: Path) -> None:
    """The control for the mistyped cases: absence and null are Spotify's own "none".

    Absent fields fit the published schema; the nulls do not, but Spotify has
    sent ``images: null`` for coverless playlists, so this page is served
    without schema validation on purpose."""
    bare = {k: v for k, v in playlist(1).items() if k not in ("images", "snapshot_id", "items")}
    nulls = {**playlist(2), "images": None, "snapshot_id": None, "items": None,
             "owner": {**playlist(2)["owner"], "display_name": None}}
    empty = {**playlist(3), "images": []}
    with WebApi([bare, nulls, empty], malformed_page={}) as api:
        rows = account.list_account_playlists(_client(tmp_path, api)).playlists
    assert [(p.image_url, p.snapshot_id, p.track_count) for p in rows[:2]] == [(None, None, None)] * 2
    assert rows[1].owner_name is None
    assert rows[2].image_url is None
    assert rows[0].open_url == "https://open.spotify.com/playlist/pl0001"


@pytest.mark.requirement("SPOT-6555")
def test_an_account_past_ten_thousand_playlists_is_listed_whole(tmp_path: Path) -> None:
    """[if] the account has 10,050 playlists [then] all come back over 201 pages, [else stop].

    Otherwise a page cap drops the account's list."""
    with WebApi([playlist(i) for i in range(10_050)]) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    assert len(listing.playlists) == 10_050
    assert len(api.calls) == 201  # one walk of 201 pages
    assert listing.playlists[-1].spotify_id == "pl10049"


def test_a_next_page_after_an_empty_one_fails_loudly(tmp_path: Path) -> None:
    with WebApi([], empty_next=True) as api, pytest.raises(RuntimeError, match="after an empty one"):
        account.list_account_playlists(_client(tmp_path, api))
    assert len(api.calls) == 1


def test_a_chain_past_the_reported_total_fails_loudly(tmp_path: Path) -> None:
    with WebApi([], endless_total=120) as api, pytest.raises(RuntimeError, match="reported total of 120"):
        account.list_account_playlists(_client(tmp_path, api))
    # One row a page: the walk stops on the first page that passes 120.
    assert len(api.calls) == 121


@pytest.mark.parametrize(
    ("patch", "problem"),
    [
        ({"items": DROP}, "items is NoneType"),
        ({"items": None}, "items is NoneType"),
        ({"items": {}}, "items is dict"),
        ({"next": DROP}, "no next field"),
        ({"next": ""}, "next is ''"),
        ({"next": 7}, "next is 7"),
        ({"total": DROP}, "total is None"),
        ({"total": "3"}, "total is '3'"),
        ({"total": -1}, "total is -1"),
        ({"offset": DROP}, "offset is None, not 0"),
        ({"offset": "0"}, "offset is '0', not 0"),
        ({"offset": False}, "offset is False, not 0"),
        ({"offset": 5}, "offset is 5, not 0"),
    ],
)
def test_a_malformed_playlists_page_fails_instead_of_ending_the_list(
    tmp_path: Path, patch: dict[str, Any], problem: str
) -> None:
    """[if] a page lacks or mistypes items, next or total [then] the listing fails, [else stop].

    Otherwise a partial response reads as a complete, shorter list."""
    with WebApi([playlist(i) for i in range(3)], malformed_page=patch) as api, pytest.raises(
        RuntimeError, match=f"malformed page at offset 0: {re.escape(problem)}"
    ):
        account.list_account_playlists(_client(tmp_path, api))


@pytest.mark.parametrize(("skip", "points_at"), [(5, 25), (-5, 15)])
def test_a_next_link_that_skips_or_repeats_rows_fails(tmp_path: Path, skip: int, points_at: int) -> None:
    """[if] a next link does not start where the page ended [then] the listing fails, [else stop].

    Otherwise rows are silently skipped or repeated and the list returned as complete."""
    with (
        WebApi([playlist(i) for i in range(70)], page_cap=20, next_skip=skip) as api,
        pytest.raises(RuntimeError, match=f"points at offset {points_at}, not 20 where the page ended"),
    ):
        account.list_account_playlists(_client(tmp_path, api))
    # The link is read, not assumed: the walk stopped on the first page.
    assert [offset for _, offset in api.calls] == [0]


def test_contiguous_short_pages_walk_every_row(tmp_path: Path) -> None:
    with WebApi([playlist(i) for i in range(70)], page_cap=20) as api:
        assert len(account.list_account_playlists(_client(tmp_path, api)).playlists) == 70
    assert [offset for _, offset in api.calls] == [0, 20, 40, 60]


def test_a_last_page_short_of_the_reported_total_fails(tmp_path: Path) -> None:
    """[if] the last page ends before the reported total [then] the listing fails, [else stop].

    Otherwise a truncated account is returned as complete."""
    with (
        WebApi([playlist(i) for i in range(3)], malformed_page={"total": 5}) as api,
        pytest.raises(RuntimeError, match="stopped at 3 rows of its reported total of 5"),
    ):
        account.list_account_playlists(_client(tmp_path, api))


@pytest.mark.parametrize(
    ("link", "problem"),
    [
        ("https://api.spotify.com/v1/me/playlists?limit=50", "has no offset"),
        ("https://api.spotify.com/v1/me/playlists?offset=x&limit=50", "has no offset"),
        ("https://api.spotify.com/v1/me/playlists?offset=0&limit=50", "points at offset 0, not 3"),
    ],
)
def test_a_next_link_without_a_usable_offset_fails(tmp_path: Path, link: str, problem: str) -> None:
    with (
        WebApi([playlist(i) for i in range(3)], malformed_page={"next": link}) as api,
        pytest.raises(RuntimeError, match=re.escape(problem)),
    ):
        account.list_account_playlists(_client(tmp_path, api))


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("id", None, "id is None"),
        ("name", None, "pl0001 name is None"),
        ("collaborative", "yes", "pl0001 collaborative is 'yes'"),
        ("public", "true", "pl0001 public is 'true'"),
        ("public", DROP, "pl0001 has no public field"),
        ("owner", DROP, "pl0001 owner is None"),
        ("owner", "me", "pl0001 owner is 'me'"),
        ("owner", {"display_name": "ME"}, "pl0001 owner id is None"),
        ("owner", {"id": ""}, "pl0001 owner id is ''"),
        ("owner", {"id": 7}, "pl0001 owner id is 7"),
        ("items", {"href": "x", "total": True}, "pl0001 items total is True"),
        ("items", {"href": "x", "total": -1}, "pl0001 items total is -1"),
        ("items", {"href": "x", "total": "3"}, "pl0001 items total is '3'"),
        ("items", {"href": "x", "total": None}, "pl0001 items total is None"),
        # Mistyped optional fields fail too, rather than reading as absent.
        ("items", "3", "pl0001 items is '3'"),
        ("images", "cover.jpg", "pl0001 images is 'cover.jpg'"),
        ("images", [{"url": 7}], "pl0001 image entry is {'url': 7}"),
        ("images", ["https://i.example/1.jpg"], "pl0001 image entry is 'https://i.example/1.jpg'"),
        ("snapshot_id", 7, "pl0001 snapshot_id is 7"),
        ("owner", {"id": "me", "display_name": 7}, "pl0001 owner display_name is 7"),
        # The side player only opens base-62 ids, so no other id gets a row.
        ("id", "spotify:playlist:abc", "id is 'spotify:playlist:abc'"),
        ("id", "abc/def", "id is 'abc/def'"),
    ],
)
def test_a_malformed_playlist_row_fails_instead_of_getting_defaults(
    tmp_path: Path, field: str, value: object, problem: str
) -> None:
    """[if] a playlist row lacks or mistypes a field [then] the listing fails, [else stop].

    Otherwise an empty name or a coerced flag is shown as Spotify's."""
    row = {key: v for key, v in {**playlist(1), field: value}.items() if v is not DROP}
    rows = [playlist(0), row]
    with WebApi(rows, malformed_page={}) as api, pytest.raises(RuntimeError, match=re.escape(problem)):
        account.list_account_playlists(_client(tmp_path, api))


def test_an_explicit_null_public_field_is_kept(tmp_path: Path) -> None:
    """[if] Spotify sends public as an explicit null [then] the row lists with public None, [else stop].

    The control for the missing-field case: null is Spotify's own value, absence is not."""
    rows = [playlist(0), {**playlist(1), "public": None}]
    with WebApi(rows, malformed_page={}) as api:
        listing = account.list_account_playlists(_client(tmp_path, api))
    assert [p.public for p in listing.playlists][1] is None


@pytest.mark.parametrize("entry", ["pl0001", 7, [], True])
def test_a_non_object_playlist_entry_fails(tmp_path: Path, entry: object) -> None:
    """[if] a playlist entry is neither an object nor Spotify's deleted-playlist null [then] the listing fails, [else stop].

    Otherwise the entry is dropped and a shorter listing passes the total check."""
    rows = [playlist(0), entry]
    with (
        WebApi(rows, malformed_page={}) as api,
        pytest.raises(RuntimeError, match=re.escape(f"entry {entry!r} is not an object")),
    ):
        account.list_account_playlists(_client(tmp_path, api))


def test_an_edited_schema_fixture_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the schema fixture's bytes drift from its MANIFEST [then] loading it fails, [else stop].

    Otherwise a weakened schema lets invented loopback bodies pass."""
    from tests.spotify import spotify_loopback

    real = spotify_loopback._SCHEMA_PATH
    copy = tmp_path / real.name
    (tmp_path / "MANIFEST.json").write_bytes((real.parent / "MANIFEST.json").read_bytes())
    copy.write_bytes(real.read_bytes())
    monkeypatch.setattr(spotify_loopback, "_SCHEMA_PATH", copy)
    # Control: the unedited bytes load.
    assert spotify_loopback._verified_schema_bytes() == real.read_bytes()
    copy.write_bytes(real.read_bytes().replace(b'"required"', b'"optional"', 1))
    with pytest.raises(RuntimeError, match="does not match its MANIFEST"):
        spotify_loopback._verified_schema_bytes()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source_version", "openapi 3.0.3, info.version 2.0.0"),
        ("source_sha256", "0" * 64),
        ("source", "https://example.invalid/open-api-schema.yaml"),
        ("source_version", None),
    ],
)
def test_a_schema_fixture_from_another_release_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str | None
) -> None:
    """[if] the schema MANIFEST names a release other than the pinned one [then] loading it fails, [else stop].

    Otherwise a refresh from the wrong Spotify schema release tests an unintended contract."""
    from tests.spotify import spotify_loopback

    real = spotify_loopback._SCHEMA_PATH
    copy = tmp_path / real.name
    copy.write_bytes(real.read_bytes())
    manifest = json.loads((real.parent / "MANIFEST.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(spotify_loopback, "_SCHEMA_PATH", copy)
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    # Control: the real MANIFEST loads.
    assert spotify_loopback._verified_schema_bytes() == real.read_bytes()
    if value is None:
        del manifest[key]
    else:
        manifest[key] = value
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    with pytest.raises(RuntimeError, match=f"MANIFEST {key} .* is not the pinned"):
        spotify_loopback._verified_schema_bytes()


def test_client_id_env_wins_then_saved_file(tmp_path: Path) -> None:
    saved = tmp_path / "spotify-client.json"
    assert account.resolve_client_id({}, saved) is None
    account.save_client_id("0123456789abcdef0123456789abcdef", saved)
    assert account.resolve_client_id({}, saved) == "0123456789abcdef0123456789abcdef"
    assert account.resolve_client_id({"SPOTIFY_CLIENT_ID": " FEDCBA9876543210FEDCBA9876543210 "}, saved) == "fedcba9876543210fedcba9876543210"


@pytest.mark.parametrize("value", ["x", "envid", "0123456789abcdef0123456789abcde", "spotify:client"])
def test_an_env_client_id_that_is_not_one_is_refused_before_sign_in(tmp_path: Path, value: str) -> None:
    """[if] SPOTIFY_CLIENT_ID is set to something that is not a Client ID [then] Connect and listing refuse it, [else stop].

    Otherwise Connect opens a sign-in Spotify rejects and waits out its deadline."""
    env = {"SPOTIFY_CLIENT_ID": value}
    with pytest.raises(account.EnvClientIdInvalid, match="SPOTIFY_CLIENT_ID in the environment"):
        account.resolve_client_id(env, tmp_path / "c.json")
    with pytest.raises(account.ClientIdUnreadable):
        account.open_account_client(env=env, token_path=tmp_path / "t.json", client_id_path=tmp_path / "c.json")
    flow = account.ConnectFlow(token_path=tmp_path / "t.json", client_id_path=tmp_path / "c.json")
    with pytest.raises(account.ClientIdUnreadable):
        flow.start(env=env)
    assert flow.status()["phase"] == "idle"


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("{not json", "not JSON"),
        ('["cid"]', 'no "client_id"'),
        ('{"client_id": 7}', 'no "client_id"'),
        ('{"client_id": " "}', 'no "client_id"'),
        ('{"client_id": "bad"}', "not a Spotify Client ID"),
    ],
)
def test_a_damaged_client_id_file_is_reported_not_treated_as_absent(tmp_path: Path, body: str, problem: str) -> None:
    """[if] the saved client id file is damaged [then] it is reported as unreadable, [else stop].

    Otherwise setup runs again and overwrites the evidence."""
    saved = tmp_path / "spotify-client.json"
    saved.write_text(body, encoding="utf-8", newline="\n")
    with pytest.raises(account.ClientIdUnreadable, match=problem):
        account.resolve_client_id({}, saved)
    with pytest.raises(account.ClientIdUnreadable):
        account.open_account_client(env={}, token_path=tmp_path / "t.json", client_id_path=saved)
    # The environment still wins without reading the file.
    assert account.resolve_client_id({"SPOTIFY_CLIENT_ID": " FEDCBA9876543210FEDCBA9876543210 "}, saved) == "fedcba9876543210fedcba9876543210"


@pytest.mark.parametrize("fails", ["fsync", "replace"])
def test_a_failed_client_id_save_keeps_the_old_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    """[if] saving a new client id fails mid-write [then] the old id still reads back and no temp file is left, [else stop].

    Otherwise a full disk or a kill leaves a damaged file and SPOTIFY_CLIENT_ID_UNREADABLE."""
    saved = tmp_path / "spotify-client.json"
    old, new = "0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"
    account.save_client_id(old, saved)
    # The same bytes on every platform: no newline translation.
    assert saved.read_bytes() == b'{"client_id": "0123456789abcdef0123456789abcdef"}\n'
    # Control: a save that succeeds replaces the id.
    account.save_client_id(new, saved)
    assert account.resolve_client_id({}, saved) == new
    account.save_client_id(old, saved)

    def boom(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(account.os, fails, boom)
    with pytest.raises(OSError, match="No space left"):
        account.save_client_id(new, saved)
    monkeypatch.undo()
    assert account.resolve_client_id({}, saved) == old
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spotify-client.json"]


def test_a_client_id_file_that_is_not_utf8_is_reported(tmp_path: Path) -> None:
    """[if] the saved client id file holds bytes that are not UTF-8 [then] it is reported as unreadable, [else stop].

    Otherwise the decode error escapes as a generic API error or a traceback."""
    saved = tmp_path / "spotify-client.json"
    saved.write_bytes(b'{"client_id": "\xff\xfe"}')
    with pytest.raises(account.ClientIdUnreadable, match="not UTF-8"):
        account.resolve_client_id({}, saved)
    with pytest.raises(account.ClientIdUnreadable, match="not UTF-8"):
        account.open_account_client(env={}, token_path=tmp_path / "t.json", client_id_path=saved)


def test_an_unreadable_client_id_path_is_reported(tmp_path: Path) -> None:
    # A directory where the file should be: a real read error, not absence.
    saved = tmp_path / "spotify-client.json"
    saved.mkdir()
    with pytest.raises(account.ClientIdUnreadable, match="cannot be read"):
        account.resolve_client_id({}, saved)


def test_open_client_reasons(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    cid = tmp_path / "cid.json"
    with pytest.raises(account.NotConnected) as exc:
        account.open_account_client(env={}, token_path=token, client_id_path=cid)
    assert exc.value.reason == "no_client_id"

    env = {"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"}
    with pytest.raises(account.NotConnected) as exc:
        account.open_account_client(env=env, token_path=token, client_id_path=cid)
    assert exc.value.reason == "not_signed_in"

    write_token(token, scope="playlist-read-collaborative")
    with pytest.raises(account.NotConnected) as exc:
        account.open_account_client(env=env, token_path=token, client_id_path=cid)
    assert exc.value.reason == "scope_missing"

    write_token(token)
    client = account.open_account_client(env=env, token_path=token, client_id_path=cid)
    assert hasattr(client, "current_user_playlists")


def _ephemeral_factory(bound: dict[str, int]):
    def factory(_addr: tuple[str, int], handler: type) -> HTTPServer:
        server = HTTPServer(("127.0.0.1", 0), handler)
        bound["port"] = server.server_address[1]
        return server

    return factory


def _wait_phase(flow: account.ConnectFlow, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = flow.status()
        if status["phase"] != "waiting":
            return status
        time.sleep(0.05)
    return flow.status()


def test_connect_flow_ignores_a_callback_with_another_state(tmp_path: Path) -> None:
    """[if] a stale or forged callback arrives [then] it is answered and ignored, [else stop].

    Otherwise it ends the sign-in the user is still finishing."""
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    bound: dict[str, int] = {}
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=10,
        token_url=endpoint.url,
    )
    try:
        url = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
        assert url.startswith("https://accounts.spotify.com/authorize?")
        assert "code_challenge=" in url and "state=" in url
        state = url.split("state=")[1].split("&")[0]
        port = bound["port"]
        for query in ("code=c&state=forged", "error=access_denied&state=forged", "code=c"):
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}").read()
            assert b"replaced by a newer one" in body
        assert flow.status() == {"phase": "waiting", "detail": None}
        assert endpoint.forms == []
        assert not (tmp_path / "t.json").exists()
        # Control: the callback carrying this flow's state still completes it.
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=good&state={state}").read()
        assert body == account.CALLBACK_RECEIVED_TEXT.encode()
        assert _wait_phase(flow) == {"phase": "connected", "detail": None}
    finally:
        endpoint.close()
    assert [form["code"] for form in endpoint.forms] == [["good"]]


def test_a_callback_with_no_code_fails_without_an_exchange(tmp_path: Path) -> None:
    """A callback with this flow's state but neither code nor error ends the sign-in as failed.

    Exchanging an empty code would send spotipy off to prompt for one on stdin."""
    bound: dict[str, int] = {}
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=5,
    )
    url = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    state = url.split("state=")[1].split("&")[0]
    body = urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?state={state}").read()
    assert b"did not complete" in body
    assert _wait_phase(flow) == {"phase": "failed", "detail": "Spotify sent no sign-in code"}
    assert not (tmp_path / "c.json").exists()


def test_connect_flow_reports_spotifys_refusal_for_its_own_state(tmp_path: Path) -> None:
    """The opposite direction: an error carrying THIS flow's state still ends it."""
    bound: dict[str, int] = {}
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=5,
    )
    url = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    state = url.split("state=")[1].split("&")[0]
    body = urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?error=access_denied&state={state}").read()
    assert b"did not complete" in body
    assert _wait_phase(flow) == {"phase": "failed", "detail": "Spotify said: access_denied"}
    assert not (tmp_path / "c.json").exists()


def test_a_second_connect_replaces_an_abandoned_one(tmp_path: Path) -> None:
    """Closing the browser tab must not lock Connect out until the old listener times out."""
    bound: dict[str, int] = {}
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=60,
    )
    first = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    first_port = bound["port"]
    second = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    assert second != first
    assert flow.status() == {"phase": "waiting", "detail": None}
    # The abandoned listener is gone, so its port no longer answers.
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{first_port}/callback?code=c&state=s", timeout=2)
    # The first tab coming back late cannot end the replacement.
    first_state = first.split("state=")[1].split("&")[0]
    urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?code=c&state={first_state}").read()
    assert flow.status() == {"phase": "waiting", "detail": None}
    second_state = second.split("state=")[1].split("&")[0]
    urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?error=access_denied&state={second_state}").read()
    assert _wait_phase(flow) == {"phase": "failed", "detail": "Spotify said: access_denied"}


def test_a_stalled_callback_connection_cannot_wedge_connect(tmp_path: Path) -> None:
    """A client that connects to the redirect port and never sends a request
    must not hold the listener past a replacing Connect."""
    bound: dict[str, int] = {}
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=60,
    )
    flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    stalled = socket.create_connection(("127.0.0.1", bound["port"]))
    try:
        time.sleep(0.3)  # the listener has accepted it and is reading
        second = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})  # raises if the old listener never stops
        assert flow.status() == {"phase": "waiting", "detail": None}
        # Control: a stalled socket on the live listener does not stop it
        # serving the real callback that follows.
        again = socket.create_connection(("127.0.0.1", bound["port"]))
        try:
            state = second.split("state=")[1].split("&")[0]
            urllib.request.urlopen(
                f"http://127.0.0.1:{bound['port']}/callback?error=access_denied&state={state}",
                timeout=account.CALLBACK_READ_TIMEOUT_S + 3,
            ).read()
        finally:
            again.close()
        assert _wait_phase(flow) == {"phase": "failed", "detail": "Spotify said: access_denied"}
    finally:
        stalled.close()


def test_network_error_is_not_reported_as_an_expired_sign_in(tmp_path: Path) -> None:
    token = tmp_path / "token.json"
    write_token(token, expires_in=-60)  # expired, so opening must refresh
    env = {"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"}
    cid = tmp_path / "cid.json"
    # Nothing listens on the token URL: a real connection failure.
    # requests' errors are OSErrors; matched by name so the quality gate's
    # stub-less mypy run does not count an untyped import.
    with pytest.raises(OSError) as refused:
        account.open_account_client(env=env, token_path=token, client_id_path=cid, token_url=closed_port_url())
    assert (type(refused.value).__module__, type(refused.value).__name__) == ("requests.exceptions", "ConnectionError")

    # Spotify itself refusing the refresh token is a dead sign-in.
    endpoint = TokenEndpoint(400, TokenEndpoint.INVALID_GRANT)
    try:
        with pytest.raises(account.NotConnected) as exc:
            account.open_account_client(env=env, token_path=token, client_id_path=cid, token_url=endpoint.url)
    finally:
        endpoint.close()
    assert exc.value.reason == "token_refresh_failed"
    assert endpoint.forms[0]["grant_type"] == ["refresh_token"]

    # Spotify failing for any other reason is not a dead sign-in: it surfaces.
    endpoint = TokenEndpoint(503, {"error": "temporarily_unavailable"})
    try:
        with pytest.raises(SpotifyOauthError) as unavailable:
            account.open_account_client(env=env, token_path=token, client_id_path=cid, token_url=endpoint.url)
    finally:
        endpoint.close()
    assert unavailable.value.error == "temporarily_unavailable"

    # And a good refresh opens a client and rewrites the cache.
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    try:
        client = account.open_account_client(env=env, token_path=token, client_id_path=cid, token_url=endpoint.url)
    finally:
        endpoint.close()
    assert hasattr(client, "current_user_playlists")
    assert json.loads(token.read_text(encoding="utf-8"))["access_token"] == "BQinvented-access"


def test_a_stalled_token_endpoint_fails_instead_of_hanging(tmp_path: Path) -> None:
    """[if] Spotify's token endpoint never answers [then] the refresh fails after TOKEN_REQUEST_TIMEOUT_S, [else stop].

    Otherwise Connect and listing hang."""
    token = tmp_path / "token.json"
    write_token(token, expires_in=-60)  # expired, so opening must refresh
    endpoint = TokenEndpoint(200, TokenEndpoint.OK, stall=True)
    started = time.monotonic()
    try:
        with pytest.raises(OSError) as stalled:
            account.open_account_client(
                env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"},
                token_path=token,
                client_id_path=tmp_path / "c.json",
                token_url=endpoint.url,
            )
    finally:
        endpoint.close()
    elapsed = time.monotonic() - started
    assert type(stalled.value).__name__ == "ReadTimeout"
    assert account.TOKEN_REQUEST_TIMEOUT_S <= elapsed < account.TOKEN_REQUEST_TIMEOUT_S + 5
    assert endpoint.forms[0]["grant_type"] == ["refresh_token"]


def test_connect_flow_saves_token_and_client_id(tmp_path: Path) -> None:
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    bound: dict[str, int] = {}
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=5,
        token_url=endpoint.url,
    )
    try:
        url = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789ABCDEF0123456789ABCDEF"})
        state = url.split("state=")[1].split("&")[0]
        body = urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?code=thecode&state={state}").read()
        assert body == account.CALLBACK_RECEIVED_TEXT.encode()
        assert _wait_phase(flow) == {"phase": "connected", "detail": None}
    finally:
        endpoint.close()
    form = endpoint.forms[0]
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["thecode"]
    # Normalized before the sign-in, so Spotify sees the id the file stores.
    assert form["client_id"] == ["0123456789abcdef0123456789abcdef"]
    assert form["code_verifier"][0]  # PKCE: the verifier matching the URL's challenge
    assert json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["access_token"] == "BQinvented-access"
    assert account.resolve_client_id({}, tmp_path / "c.json") == "0123456789abcdef0123456789abcdef"


def test_connect_flow_fails_when_the_client_id_cannot_be_saved(tmp_path: Path) -> None:
    """[if] the client-id file cannot be written after sign-in [then] the flow fails with the reason, [else stop].

    Otherwise it stays waiting and the panel polls forever."""
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    bound: dict[str, int] = {}
    blocked = tmp_path / "c.json"
    blocked.mkdir()  # a directory where the file should go: a real write error
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=blocked,
        server_factory=_ephemeral_factory(bound),
        timeout_s=5,
        token_url=endpoint.url,
    )
    try:
        url = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
        state = url.split("state=")[1].split("&")[0]
        body = urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?code=thecode&state={state}").read()
        status = _wait_phase(flow)
    finally:
        endpoint.close()
    assert status["phase"] == "failed"
    assert "client id could not be saved" in (status["detail"] or "")
    # The tab never claimed success for a sign-in that then failed.
    assert b"connected to Spotify" not in body and body == account.CALLBACK_RECEIVED_TEXT.encode()


def test_connect_flow_fails_when_the_token_cannot_be_saved(tmp_path: Path) -> None:
    """[if] the token cache cannot be written after the exchange [then] the flow fails, [else stop].

    spotipy only logs the write error, so otherwise Connect reports success
    and the next listing says not signed in."""
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    bound: dict[str, int] = {}
    blocked = tmp_path / "t.json"
    blocked.mkdir()  # a directory where the token file should go: a real write error
    flow = account.ConnectFlow(
        token_path=blocked,
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory(bound),
        timeout_s=5,
        token_url=endpoint.url,
    )
    try:
        url = flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
        state = url.split("state=")[1].split("&")[0]
        body = urllib.request.urlopen(f"http://127.0.0.1:{bound['port']}/callback?code=thecode&state={state}").read()
        status = _wait_phase(flow)
    finally:
        endpoint.close()
    assert len(endpoint.forms) == 1  # the exchange itself succeeded
    assert status == {"phase": "failed", "detail": f"the Spotify sign-in could not be saved to {blocked}"}
    assert b"connected to Spotify" not in body and body == account.CALLBACK_RECEIVED_TEXT.encode()
    assert not (tmp_path / "c.json").exists()


def test_an_ephemeral_redirect_listens_where_the_authorize_url_says(tmp_path: Path) -> None:
    """[if] a test harness asks for an ephemeral redirect [then] the URL names the bound port, [else stop].

    Suites sharing one host each get their own listener instead of colliding
    on 8888; the code exchange sends that same redirect URI."""
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    bound: list[tuple[str, int]] = []

    def recording_factory(addr: tuple[str, int], handler: type) -> HTTPServer:
        bound.append(addr)
        return HTTPServer(addr, handler)

    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=recording_factory,
        timeout_s=5,
        token_url=endpoint.url,
        env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef", account.EPHEMERAL_REDIRECT_ENV: "1"},
    )
    try:
        url = flow.start()
        assert bound == [("127.0.0.1", 0)]
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/callback", redirect)
        assert match is not None and int(match.group(1)) not in (0, 8888)
        state = url.split("state=")[1].split("&")[0]
        body = urllib.request.urlopen(f"{redirect}?code=thecode&state={state}").read()
        assert body == account.CALLBACK_RECEIVED_TEXT.encode()
        assert _wait_phase(flow) == {"phase": "connected", "detail": None}
    finally:
        endpoint.close()
    assert endpoint.forms[0]["redirect_uri"] == [redirect]


@pytest.mark.parametrize(("value", "port"), [(None, 8888), ("", 8888)])
def test_a_real_sign_in_keeps_the_registered_redirect(tmp_path: Path, value: str | None, port: int) -> None:
    """Control: without the harness flag the listener and URL stay on 8888, the registered URI."""
    bound: list[tuple[str, int]] = []

    def recording_factory(addr: tuple[str, int], handler: type) -> HTTPServer:
        bound.append(addr)
        return HTTPServer(("127.0.0.1", 0), handler)

    env = {"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"}
    if value is not None:
        env[account.EPHEMERAL_REDIRECT_ENV] = value
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json", client_id_path=tmp_path / "c.json",
        server_factory=recording_factory, timeout_s=1, env=env,
    )
    url = flow.start()
    assert bound == [("127.0.0.1", port)]
    assert urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"] == [f"http://127.0.0.1:{port}/callback"]
    flow._cancel_waiting()


def test_a_bad_ephemeral_redirect_value_fails_before_listening(tmp_path: Path) -> None:
    bound: list[tuple[str, int]] = []

    def recording_factory(addr: tuple[str, int], handler: type) -> HTTPServer:
        bound.append(addr)
        return HTTPServer(addr, handler)

    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json", client_id_path=tmp_path / "c.json",
        server_factory=recording_factory,
        env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef", account.EPHEMERAL_REDIRECT_ENV: "true"},
    )
    with pytest.raises(RuntimeError, match="OPENDJ_SPOTIFY_EPHEMERAL_REDIRECT is 'true'"):
        flow.start()
    assert bound == []


def test_a_refresh_that_cannot_be_saved_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a refreshed token is not saved [then] opening the client fails, [else stop].

    spotipy only logs a failed cache write; a client on unsaved credentials
    would refresh on every call. The write is made to fail the way spotipy
    handles a real OSError: logged, not raised."""
    from spotipy.cache_handler import CacheFileHandler

    token = tmp_path / "token.json"
    write_token(token, expires_in=-60)  # expired, so opening must refresh
    monkeypatch.setattr(CacheFileHandler, "save_token_to_cache", lambda self, info: None)
    endpoint = TokenEndpoint(200, TokenEndpoint.OK)
    try:
        with pytest.raises(RuntimeError, match="refreshed Spotify sign-in could not be saved"):
            account.open_account_client(
                env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"}, token_path=token, client_id_path=tmp_path / "cid.json",
                token_url=endpoint.url,
            )
    finally:
        endpoint.close()
    assert len(endpoint.forms) == 1  # the refresh itself succeeded


def test_concurrent_connect_starts_leave_one_listener(tmp_path: Path) -> None:
    """[if] two Connects start at once [then] the later one replaces the earlier, leaving one listener, [else stop].

    Otherwise the second finds no thread to stop and two listeners outlive it."""
    servers: list[HTTPServer] = []
    closed: list[HTTPServer] = []
    gate = threading.Event()

    def factory(_addr: tuple[str, int], handler: type) -> HTTPServer:
        server = HTTPServer(("127.0.0.1", 0), handler)
        original_close = server.server_close

        def close() -> None:
            closed.append(server)
            original_close()

        server.server_close = close  # type: ignore[method-assign]
        servers.append(server)
        if len(servers) == 1:
            gate.wait(timeout=5)  # the first start stalls mid-bind
        return server

    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json", client_id_path=tmp_path / "c.json", server_factory=factory, timeout_s=30
    )
    env = {"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"}
    first = threading.Thread(target=flow.start, kwargs={"env": env})
    second = threading.Thread(target=flow.start, kwargs={"env": env})
    first.start()
    time.sleep(0.2)
    second.start()
    time.sleep(0.3)
    gate.set()
    first.join(timeout=10)
    second.join(timeout=10)
    try:
        time.sleep(1.5)  # a cancelled listener stops within one handle_request timeout
        open_servers = [server for server in servers if server not in closed]
        assert len(servers) == 2
        assert len(open_servers) == 1
        assert flow.status()["phase"] == "waiting"
    finally:
        flow._cancel_waiting()


@pytest.mark.requirement("SPOTIFY-11967")
def test_wait_pkce_connect_refocuses_authorize_url_after_10s() -> None:
    """[if] login waits and Connect is still waiting at 10s [then] the authorize URL opens again, [else stop]."""
    clock = iter([0.0, 5.0, 10.0, 10.5])
    opens: list[str] = []
    waits = {"n": 0}

    class _Flow:
        def status(self) -> dict[str, Any]:
            waits["n"] += 1
            if waits["n"] >= 4:
                return {"phase": "connected", "detail": None}
            return {"phase": "waiting", "detail": None}

    status = account.wait_pkce_connect(
        _Flow(),  # type: ignore[arg-type]
        "https://accounts.spotify.com/authorize?client_id=x",
        open_browser=True,
        poll_s=0.01,
        monotonic=lambda: next(clock),
        sleep=lambda _s: None,
        opener=lambda url: opens.append(url) or True,
    )
    assert status["phase"] == "connected"
    assert len(opens) == 1
    assert opens[0].startswith("https://accounts.spotify.com/")


@pytest.mark.requirement("SPOTIFY-11967")
def test_wait_pkce_connect_with_no_browser_never_opens_url() -> None:
    """[if] login uses --no-browser [then] wait_pkce_connect never opens the authorize URL, [else stop]."""
    opens: list[str] = []
    waits = {"n": 0}

    class _Flow:
        def status(self) -> dict[str, Any]:
            waits["n"] += 1
            return {"phase": "connected", "detail": None} if waits["n"] > 1 else {"phase": "waiting", "detail": None}

    account.wait_pkce_connect(
        _Flow(),  # type: ignore[arg-type]
        "https://accounts.spotify.com/authorize?client_id=x",
        open_browser=False,
        poll_s=0.01,
        sleep=lambda _s: None,
        opener=lambda url: opens.append(url) or True,
    )
    assert opens == []


def test_wait_pkce_connect_returns_connect_flow_terminal_phase(tmp_path: Path) -> None:
    """[if] ConnectFlow times out waiting [then] wait_pkce_connect returns failed with its detail, [else stop]."""
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory({}),
        timeout_s=0.2,
    )
    flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    status = account.wait_pkce_connect(
        flow,
        "https://accounts.spotify.com/authorize?client_id=x",
        open_browser=False,
        poll_s=0.05,
        sleep=time.sleep,
    )
    assert status["phase"] == "failed"
    assert status["detail"] == "timed out waiting for the browser"


def test_connect_flow_times_out(tmp_path: Path) -> None:
    flow = account.ConnectFlow(
        token_path=tmp_path / "t.json",
        client_id_path=tmp_path / "c.json",
        server_factory=_ephemeral_factory({}),
        timeout_s=0.2,
    )
    flow.start(env={"SPOTIFY_CLIENT_ID": "0123456789abcdef0123456789abcdef"})
    assert _wait_phase(flow)["detail"] == "timed out waiting for the browser"


def test_threads_do_not_leak(tmp_path: Path) -> None:
    before = {t.name for t in threading.enumerate()}
    test_connect_flow_times_out(tmp_path)
    time.sleep(1.2)
    assert "spotify-connect" not in {t.name for t in threading.enumerate()} - before


@pytest.mark.parametrize("value", [7, {"text": "DJ"}, ["DJ"], True])
def test_a_mistyped_display_name_fails_the_listing(tmp_path: Path, value: object) -> None:
    """[if] /me sends a display_name that is not a string or null [then] the listing fails, [else stop].

    Otherwise drift reads as a connected account with no name."""
    with (
        WebApi([playlist(1)], me_fields={"display_name": value}) as api,
        pytest.raises(RuntimeError, match=re.escape(f"display_name of {value!r}")),
    ):
        account.list_account_playlists(_client(tmp_path, api))


@pytest.mark.parametrize(("fields", "expected"), [({"display_name": None}, None), ({"display_name": DROP}, None), ({}, "Test DJ")])
def test_an_absent_or_null_display_name_is_none(tmp_path: Path, fields: dict[str, object], expected: str | None) -> None:
    """The control: absence and null are Spotify's own "no name", and a string is kept."""
    with WebApi([playlist(1)], me_fields=fields) as api:
        assert account.list_account_playlists(_client(tmp_path, api)).display_name == expected
