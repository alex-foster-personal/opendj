"""Round 3 hardening: the digest fence (6b/N7) and the generation token/
retention (N6), as permanent tests.

Split out of :mod:`tests.cloudsync.test_hub_sync_round3` (round 4
quality-gate ratchet: that file crossed 600 lines). N4/A4, N5 and 4a's hub
blast radius stayed there; nothing here is imported back from it.

Every test here is one reproduction sketch from
``.planning/cloudsync-round2-adversarial.md``. The mapping, so a failure
names the defect it just let back in:

- 6b / N7  -> the digest compare was unfenced in both directions: a third
  machine pushing during the round trip, or a local write landing mid-sync,
  raised the alarm ADR 04 c6 reserves for corruption.
- N6       -> restore detection read the hub's ``MAX(seq)`` going backwards,
  which a routine changelog prune also does, so maintenance cost every spoke
  a full library re-offer. Neither changelog had any retention at all.

Acceptance criteria, one test each:
- if a third machine's push, or a local write, DURING the round trip raises
  ``SyncDigestMismatch``, the corruption alarm fires on ordinary concurrency
  -- broken. And if a real divergence stops raising, the settling round has
  become a repair pass -- also broken.
- if a changelog prune makes a spoke re-offer its library, restore detection
  is still reading ``MAX(seq)`` rather than the generation token -- broken.
- if a rotated token does not produce exactly one re-offer, the restore
  runbook has no signal -- broken.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client, engine, generation, maintenance, service
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _T1,
    _T2,
    _T3,
    _insert_track,
    _open,
    _seed_common_track,
    _set_track_title,
    _sync,
    _TestClientTransport,
    _track_title,
)

pytestmark = pytest.mark.requirement("CAT-04")


# ----- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MDT_IS_HUB`` from the developer's shell must not steer these tests."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


# ----- 6b / N7: the digest compare is fenced ---------------------------------


class _InterceptingTransport:
    """A transport that runs ``during`` once, before a chosen request.

    The only way to land a write "in the gap" deterministically. It does not
    fake anything about the protocol: the wrapped transport is the real one,
    and the callback drives real code against the real DBs.
    """

    def __init__(
        self,
        inner: _TestClientTransport,
        *,
        before: str,
        during: Callable[[], object],
    ) -> None:
        self._inner = inner
        self._before = before
        self._during = during
        self.fired = False

    def _maybe_fire(self, path: str) -> None:
        if self.fired or not path.endswith(self._before):
            return
        self.fired = True
        self._during()

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._maybe_fire(path)
        return self._inner.post(path, payload)

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        self._maybe_fire(path)
        return self._inner.get(path, params)


def test_a_third_machine_pushing_in_the_gap_is_not_a_divergence(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """6b's sketch: A did nothing wrong and halted anyway.

    Round 2 observed ``f6b A FAILED despite doing nothing wrong:
    SyncDigestMismatch: tables ['tracks'] differ``. Only the intra-snapshot
    half of ADR 08 point 6b was done; the digest still answered as of
    request time with no way for the spoke to tell "someone else pushed"
    from "we have diverged". It reports its seq now, and the spoke settles
    with one more round.
    """
    _seed_common_track((spoke_a,), "trk-a")
    _sync(spoke_a, hub, "spoke-a")

    conn_b = _open(spoke_b)
    try:
        _insert_track(conn_b, "trk-b", title="from B", updated_at=_T2, origin=_DEV_B)
    finally:
        conn_b.close()

    intercepted = _InterceptingTransport(
        hub,
        before="/digest",
        during=lambda: _sync(spoke_b, hub, "spoke-b"),
    )
    result = client.run_sync(
        spoke_a, "http://hub.invalid", transport=intercepted, name="spoke-a"
    )

    assert intercepted.fired, "the third machine never pushed; probe is void"
    assert result.rounds == 2, "the spoke did not settle before judging"
    conn_a = _open(spoke_a)
    try:
        assert _track_title(conn_a, "trk-b") == "from B", (
            "the settling round did not pull what the third machine pushed"
        )
    finally:
        conn_a.close()


def test_a_local_write_during_the_round_trip_is_not_a_divergence(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """N7's sketch: the daemon serves the webui throughout a sync.

    Round 2 observed ``n2b mid-sync write -> post-sync digest mismatch ...
    tables ['tracks'] differ`` followed by ``next sync pushed=1 accepted=1``:
    no row was lost, but an ordinary concurrent edit raised the one alarm
    ADR 04 c6 reserves for corruption, and then cleared itself.
    """
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    def _write_mid_sync() -> None:
        conn = _open(spoke_a)
        try:
            _insert_track(
                conn, "trk-mid", title="typed mid-sync", updated_at=_T2, origin=_DEV_A
            )
        finally:
            conn.close()

    # Before the PULL: the push has already fenced at the old local seq, so
    # this write is exactly the one the daemon takes from the webui while a
    # sync is in flight.
    intercepted = _InterceptingTransport(hub, before="/pull", during=_write_mid_sync)
    result = client.run_sync(
        spoke_a, "http://hub.invalid", transport=intercepted, name="spoke-a"
    )

    assert intercepted.fired, "the mid-sync write never happened; probe is void"
    assert result.rounds == 2
    assert result.accepted >= 1, "the settling round did not offer the new row"

    hub_conn = _open(hub_dir)
    try:
        assert _track_title(hub_conn, "trk-mid") == "typed mid-sync"
    finally:
        hub_conn.close()


def test_a_real_divergence_still_raises_after_settling(
    hub: _TestClientTransport, spoke_a: Path
) -> None:
    """The settle must not become a repair pass that hides a real bug.

    A raw write that bypasses the stamp helper is invisible to the fence
    (ADR 08 consequence 2), so nothing about it settles: the digest has to
    keep saying so.
    """
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")

    def _bypass_the_writer() -> None:
        conn = _open(spoke_a)
        try:
            conn.execute(
                "UPDATE tracks SET title = ?, updated_at = ? WHERE stable_id = ?",
                ("bypassed the writer", _T3, "trk-1"),
            )
        finally:
            conn.close()

    intercepted = _InterceptingTransport(
        hub, before="/pull", during=_bypass_the_writer
    )
    with pytest.raises(client.SyncDigestMismatch) as excinfo:
        client.run_sync(
            spoke_a, "http://hub.invalid", transport=intercepted, name="spoke-a"
        )
    assert "round(s)" in str(excinfo.value)


# ----- N6: the generation token and changelog retention ---------------------


def _changelog(conn: sqlite3.Connection, table: str) -> list[tuple[int, str, str]]:
    return [
        (int(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            f"SELECT seq, table_name, row_pk FROM {table} ORDER BY seq"
        )
    ]


def test_a_changelog_prune_is_not_mistaken_for_a_restore(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """N6's sketch: maintenance must not cost a fleet-wide re-offer.

    Round 2 observed ``n5b changelog-only prune -> restore_detected=True
    pushed=1`` -- any prune, vacuum or table-scoped repair made every spoke
    log ``the hub went BACKWARDS`` at ERROR and re-offer its whole library
    (minutes per 8k tracks, ADR 08 consequence 3). The token does not move
    when entries are pruned, and the sanctioned prune cannot lower
    ``MAX(seq)`` because it never drops the newest entry for a row.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn_a, "trk-2", title="other", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    # A second edit in a second sync: one row, two hub_changelog entries, the
    # older of which is superseded and therefore prunable.
    conn_a = _open(spoke_a)
    try:
        _set_track_title(conn_a, "trk-1", title="three", updated_at=_T2, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    _sync(spoke_b, hub, "spoke-b")

    hub_conn = _open(hub_dir)
    try:
        before = _changelog(hub_conn, "hub_changelog")
        deleted = engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=0)
        after = _changelog(hub_conn, "hub_changelog")
    finally:
        hub_conn.close()

    assert deleted >= 1, f"nothing was prunable out of {before}"
    assert max(seq for seq, _, _ in after) == max(seq for seq, _, _ in before), (
        "the prune lowered MAX(seq); that is indistinguishable from a restore"
    )
    assert {(table, pk) for _, table, pk in after} == {
        (table, pk) for _, table, pk in before
    }, "the prune dropped a row's ONLY entry; that row can never be pulled again"

    quiet = _sync(spoke_a, hub, "spoke-a")
    assert quiet.hub_restore_detected is False, "a prune was read as a restore"
    assert quiet.pushed == 0, "the spoke re-offered its library after a prune"

    # And a spoke starting from scratch still gets everything the hub holds.
    fresh = _sync(spoke_b, hub, "spoke-b")
    assert fresh.hub_restore_detected is False
    conn_b = _open(spoke_b)
    try:
        assert _track_title(conn_b, "trk-1") == "three"
        assert _track_title(conn_b, "trk-2") == "other"
    finally:
        conn_b.close()


def test_prune_keeps_entries_inside_the_retention_window(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """Superseded is necessary but not sufficient: the bounds bind too."""
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")
    conn_a = _open(spoke_a)
    try:
        _set_track_title(conn_a, "trk-1", title="two", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    hub_conn = _open(hub_dir)
    try:
        assert len(_changelog(hub_conn, "hub_changelog")) == 2, (
            "the probe needs one superseded entry to be about anything"
        )
        assert (
            engine.prune_changelog(hub_conn, keep_days=3650.0, keep_rows=0) == 0
        ), "an entry inside the day window was pruned"
        assert (
            engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=10_000) == 0
        ), "an entry inside the row window was pruned"
        assert (
            engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=0, now=_T3) == 0
        ), "an entry newer than the supplied cutoff was pruned"
        # Not vacuous: with both bounds open, the superseded entry does go.
        assert engine.prune_changelog(hub_conn, keep_days=0.0, keep_rows=0) == 1
        with pytest.raises(engine.SyncApplyError):
            engine.prune_changelog(hub_conn, changelog="tracks")
    finally:
        hub_conn.close()


def test_a_rotated_generation_is_what_triggers_the_re_offer(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """The restore runbook's own signal, for a restore the anchor cannot see.

    A whole-machine restore rolls the data dir back with the DB, so the
    anchor agrees with the DB and nothing looks wrong. Rotating by hand is
    then the signal, and it must produce the same recovery as an automatic
    rotation.
    """
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
    finally:
        conn_a.close()
    first = _sync(spoke_a, hub, "spoke-a")
    assert first.hub_restore_detected is False

    before = maintenance.show_generation(hub_dir)
    after = maintenance.rotate(hub_dir)
    assert after != before

    recovered = _sync(spoke_a, hub, "spoke-a")
    assert recovered.hub_restore_detected is True
    assert recovered.pushed >= 1, "the re-offer did not happen"
    assert _sync(spoke_a, hub, "spoke-a").hub_restore_detected is False, (
        "the rotation was detected twice; the spoke did not store the new token"
    )


def test_the_generation_anchor_survives_an_ordinary_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path
) -> None:
    """The token is stable across syncs, and the spoke stores what it saw."""
    _seed_common_track((spoke_a,), "trk-1")
    _sync(spoke_a, hub, "spoke-a")
    minted = maintenance.show_generation(hub_dir)
    result = _sync(spoke_a, hub, "spoke-a")

    assert maintenance.show_generation(hub_dir) == minted
    conn_a = _open(spoke_a)
    try:
        watermark = engine.read_watermark(conn_a, result.hub_machine_id)
    finally:
        conn_a.close()
    assert watermark.peer_generation == minted
    assert generation.anchor_path(hub_dir).exists()


def test_the_maintenance_cli_prunes_and_rotates(
    hub: _TestClientTransport,
    hub_dir: Path,
    spoke_a: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Agent-native parity: the operator actions are drivable without a UI."""
    conn_a = _open(spoke_a)
    try:
        _insert_track(conn_a, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _set_track_title(conn_a, "trk-1", title="two", updated_at=_T1, origin=_DEV_A)
    finally:
        conn_a.close()
    _sync(spoke_a, hub, "spoke-a")

    assert (
        maintenance.main(
            [
                "prune",
                "--data-dir",
                str(hub_dir),
                "--keep-days",
                "0",
                "--keep-rows",
                "0",
            ]
        )
        == 0
    )
    assert "pruned" in capsys.readouterr().out

    assert maintenance.main(["generation", "--data-dir", str(hub_dir)]) == 0
    shown = capsys.readouterr().out.strip()
    assert maintenance.main(["rotate", "--data-dir", str(hub_dir)]) == 0
    assert capsys.readouterr().out.strip() != shown
