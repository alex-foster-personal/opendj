"""Safety contracts for the live playlist-health rename utility."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.reconcile import prefix_dead_playlists as prefixer

# This module exercises live-write MECHANICS against tmp fixtures, so it runs
# with the one-way rekordbox import gate ON (root conftest reads the marker).
# It never touches a real rekordbox target.
pytestmark = [pytest.mark.rekordbox_writeback, pytest.mark.rb_parity]


def _plan() -> list[prefixer.PlaylistHealth]:
    return [
        prefixer.PlaylistHealth(
            pid="playlist-1",
            name="Broken links",
            is_folder=False,
            have=0,
            missing=4,
        )
    ]


def test_rekordbox_check_fails_closed_without_pgrep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_pgrep(*args, **kwargs):
        raise FileNotFoundError("pgrep not found")

    monkeypatch.setattr(prefixer.subprocess, "run", missing_pgrep)

    with pytest.raises(prefixer.SafetyCheckError, match="pgrep is unavailable"):
        prefixer._rekordbox_running()


def test_rekordbox_check_fails_closed_on_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(returncode=2, stdout="", stderr="probe failed")
    monkeypatch.setattr(prefixer.subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(prefixer.SafetyCheckError, match="exit 2"):
        prefixer._rekordbox_running()


def test_apply_rolls_back_before_commit_when_readback_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _install_fake_db(monkeypatch, readback_override="unexpected")

    with pytest.raises(RuntimeError, match="readback mismatch"):
        prefixer._apply(_plan())

    assert fake_db.events == ["flush", "expire", "rollback", "close"]


def test_apply_commits_only_after_verified_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _install_fake_db(monkeypatch)

    assert prefixer._apply(_plan()) == 1
    assert fake_db.events == ["flush", "expire", "commit", "close"]


def _install_fake_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    readback_override: str | None = None,
):
    class Session:
        def __init__(self, db) -> None:
            self.db = db

        def flush(self) -> None:
            self.db.persisted = {
                playlist_id: row.Name
                for playlist_id, row in self.db.rows.items()
            }
            self.db.events.append("flush")

        def expire_all(self) -> None:
            self.db.expired = True
            self.db.events.append("expire")

    class FakeDb:
        instance = None

        def __init__(self, *, path: str) -> None:
            self.path = path
            self.rows = {"playlist-1": SimpleNamespace(Name="Broken links")}
            self.persisted: dict[str, str] = {}
            self.expired = False
            self.events: list[str] = []
            self.session = Session(self)
            FakeDb.instance = self

        def get_playlist(self, *, ID: str):
            if not self.expired:
                return self.rows.get(ID)
            name = readback_override or self.persisted.get(ID)
            return None if name is None else SimpleNamespace(Name=name)

        def commit(self) -> None:
            self.events.append("commit")

        def rollback(self) -> None:
            self.events.append("rollback")

        def close(self) -> None:
            self.events.append("close")

    monkeypatch.setattr(prefixer, "Rekordbox6Database", FakeDb)
    fake_db = FakeDb(path="unused")
    FakeDb.instance = fake_db

    def factory(*, path: str):
        assert path
        return fake_db

    monkeypatch.setattr(prefixer, "Rekordbox6Database", factory)
    return fake_db
