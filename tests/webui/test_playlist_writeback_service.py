"""Tests for :mod:`apps.webui.server.playlist_writeback` (service layer,
no HTTP client -- see ``test_playlist_writeback_route.py`` for the FastAPI
surface). Node ``write-back-rekordbox-djay``, LANE playlists-router.

Regression one-liners:
  * if plan() does not diff against the LIVE target then broken (no
    persisted materialisation record backs this feature -- see module
    docstring)
  * if apply() ever mutates a target with unresolved stable_ids then broken
  * if apply() silently overwrites a pre-existing target without
    force_adopt then broken
  * if dry_run=True (the default) ever calls create_playlist/apply_diff
    then broken
  * if capability() does not surface a specific reason on an unreachable
    vendor then broken
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pytest

from apps.webui.server.playlist_writeback import (
    VENDORS,
    WritebackService,
    WritebackUnavailable,
)


@dataclass
class _FakeVendorWriter:
    """In-memory stand-in for RBPlaylistWriter/DjayPlaylistWriter."""

    vendor: str
    state_conn: sqlite3.Connection
    playlists: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    raise_on_apply: bool = False

    def playlist_exists(self, name: str) -> bool:
        self.calls.append(("exists", name))
        return name in self.playlists

    def read_members(self, name: str) -> list[str]:
        self.calls.append(("read", name))
        return list(self.playlists[name])

    def create_playlist(self, name: str, track_ids: list[str]) -> None:
        self.calls.append(("create", name, list(track_ids)))
        if self.raise_on_apply:
            raise RuntimeError(f"{self.vendor} refused create")
        self.playlists[name] = list(track_ids)

    def apply_diff(self, name: str, added: list[str], removed: list[str]) -> None:
        self.calls.append(("diff", name, list(added), list(removed)))
        if self.raise_on_apply:
            raise RuntimeError(f"{self.vendor} refused diff")
        current = self.playlists.setdefault(name, [])
        for rid in removed:
            if rid in current:
                current.remove(rid)
        for aid in added:
            if aid not in current:
                current.append(aid)


@pytest.fixture
def state_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE track_vendor_ids ("
        "stable_id TEXT, vendor TEXT, vendor_id TEXT, "
        "PRIMARY KEY (stable_id, vendor))"
    )
    rows = [
        ("a", "rekordbox", "rb-a"), ("b", "rekordbox", "rb-b"),
        ("c", "rekordbox", "rb-c"),
    ]
    conn.executemany(
        "INSERT INTO track_vendor_ids VALUES (?, ?, ?)", rows,
    )
    yield conn
    conn.close()


@pytest.fixture
def rb_writer(state_conn) -> _FakeVendorWriter:
    return _FakeVendorWriter(vendor="rekordbox", state_conn=state_conn)


@pytest.fixture
def service(rb_writer) -> WritebackService:
    return WritebackService(writer_factory=lambda vendor: rb_writer if vendor == "rekordbox" else None)


# --------------------------------------------------------------- capability


class TestCapability:
    def test_available_when_writer_builds(self, service) -> None:
        cap = service.capability("rekordbox")
        assert cap.available is True
        assert cap.reason is None

    def test_unavailable_with_reason_when_factory_returns_none(self, service) -> None:
        cap = service.capability("djay")
        assert cap.available is False
        assert cap.reason and "djay" in cap.reason

    def test_all_vendors_enumerable(self) -> None:
        assert set(VENDORS) == {"rekordbox", "djay"}


# --------------------------------------------------------------------- plan


class TestPlan:
    def test_plan_against_missing_target_is_all_additions(self, service) -> None:
        plan = service.plan(vendor="rekordbox", playlist_name="New Set", desired_ids=["a", "b"])
        assert plan.target_exists is False
        assert plan.added == ["a", "b"]
        assert plan.removed == []
        assert plan.unresolved == []
        assert plan.is_noop is False

    def test_plan_diffs_against_live_target_not_a_cache(self, service, rb_writer) -> None:
        rb_writer.playlists["Existing"] = ["a", "c"]
        plan = service.plan(vendor="rekordbox", playlist_name="Existing", desired_ids=["a", "b"])
        assert plan.target_exists is True
        assert plan.added == ["b"]
        assert plan.removed == ["c"]

    def test_plan_noop_when_target_already_matches(self, service, rb_writer) -> None:
        rb_writer.playlists["Same"] = ["a", "b"]
        plan = service.plan(vendor="rekordbox", playlist_name="Same", desired_ids=["a", "b"])
        assert plan.is_noop is True

    def test_plan_surfaces_unresolved_stable_ids(self, service) -> None:
        plan = service.plan(vendor="rekordbox", playlist_name="New Set", desired_ids=["a", "no-mapping"])
        assert plan.unresolved == ["no-mapping"]
        assert "no-mapping" not in plan.added

    def test_plan_raises_unavailable_for_unreachable_vendor(self, service) -> None:
        with pytest.raises(WritebackUnavailable):
            service.plan(vendor="djay", playlist_name="X", desired_ids=[])


# -------------------------------------------------------------------- apply


class TestApplyDryRun:
    def test_default_dry_run_never_calls_writer_mutations(self, service, rb_writer) -> None:
        result = service.apply(vendor="rekordbox", playlist_name="New Set", desired_ids=["a"])
        assert result.dry_run is True
        assert result.applied is False
        assert not any(c[0] in ("create", "diff") for c in rb_writer.calls)

    def test_dry_run_still_reports_the_diff(self, service, rb_writer) -> None:
        rb_writer.playlists["Existing"] = ["a"]
        result = service.apply(
            vendor="rekordbox", playlist_name="Existing",
            desired_ids=["a", "b"], dry_run=True,
        )
        assert result.added == ["b"]
        assert result.removed == []


class TestApplyLiveCreate:
    def test_creates_new_target(self, service, rb_writer) -> None:
        result = service.apply(
            vendor="rekordbox", playlist_name="New Set",
            desired_ids=["a", "b"], dry_run=False,
        )
        assert result.applied is True
        assert result.ok is True
        assert rb_writer.playlists["New Set"] == ["a", "b"]

    def test_unresolved_ids_block_apply_before_any_write(self, service, rb_writer) -> None:
        result = service.apply(
            vendor="rekordbox", playlist_name="New Set",
            desired_ids=["a", "unmapped"], dry_run=False,
        )
        assert result.applied is False
        assert result.error and "unmapped" in result.error
        assert rb_writer.calls == []


class TestApplyCollisionGuard:
    def test_existing_target_blocks_without_force_adopt(self, service, rb_writer) -> None:
        rb_writer.playlists["Existing"] = ["z"]
        result = service.apply(
            vendor="rekordbox", playlist_name="Existing",
            desired_ids=["a"], dry_run=False, force_adopt=False,
        )
        assert result.applied is False
        assert "force_adopt" in (result.error or "")
        assert not any(c[0] in ("create", "diff") for c in rb_writer.calls)

    def test_force_adopt_applies_diff_against_existing_target(self, service, rb_writer) -> None:
        rb_writer.playlists["Existing"] = ["z"]
        result = service.apply(
            vendor="rekordbox", playlist_name="Existing",
            desired_ids=["a"], dry_run=False, force_adopt=True,
        )
        assert result.applied is True
        assert set(rb_writer.playlists["Existing"]) == {"a"}


class TestApplyErrorIsolation:
    def test_writer_exception_is_captured_not_raised(self, state_conn) -> None:
        writer = _FakeVendorWriter(vendor="rekordbox", state_conn=state_conn, raise_on_apply=True)
        svc = WritebackService(writer_factory=lambda v: writer)
        result = svc.apply(vendor="rekordbox", playlist_name="New Set", desired_ids=["a"], dry_run=False)
        assert result.applied is False
        assert result.ok is False
        assert "refused create" in (result.error or "")


class TestIdempotentReapply:
    def test_second_apply_after_success_is_a_true_noop(self, service, rb_writer) -> None:
        first = service.apply(vendor="rekordbox", playlist_name="Set", desired_ids=["a", "b"], dry_run=False)
        assert first.applied is True
        second = service.apply(
            vendor="rekordbox", playlist_name="Set", desired_ids=["a", "b"],
            dry_run=False, force_adopt=True,
        )
        assert second.added == [] and second.removed == []
