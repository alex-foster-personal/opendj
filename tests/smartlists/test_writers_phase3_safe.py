"""SafePlaylistWriter six-rail cases for the Phase 3 wrappers.

[if] a SafePlaylistWriter rail is exercised [then] it dry-runs, confirms, backs up, verifies, or reverses as specified, [else stop].

Split from ``test_writers_phase3.py``: typed confirm, dry-run, live
guards, pgrep, backup, verify, reversal, and vendor passthrough.
"""

from __future__ import annotations

import pytest

from apps.smartlists.writers import (
    DEFAULT_CONFIRM_PHRASE,
    FakeWriter,
    SafePlaylistWriter,
    require_typed_confirm_phrase,
    safe_writer_session,
)
from apps.sync.safety import SafetyAbort
from tests.smartlists.writers_phase3_support import _make_safe_writer

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SMART-02"), pytest.mark.rekordbox_writeback]


# -------------------------------------------------------- SafePlaylistWriter
#
# Six-rail coverage for apps.smartlists.writers.SafePlaylistWriter.
# Each test exercises one or more of:
#   rail 1: pgrep process gate
#   rail 2: timestamped DB backup
#   rail 3: typed confirmation (flag_ok + prompt helper)
#   rail 4: dry-run default
#   rail 5: post-write verify (verifier callback)
#   rail 6: reversal script emitted into (private path)/<ts>
# All tests use a fixture DB under tmp_path -- the live master.db /
# MediaLibrary.db is never touched.


class TestRequireTypedConfirmPhrase:
    """Rail 3: typed confirmation prompt helper."""

    def test_accepts_exact_phrase(self) -> None:
        assert (
            require_typed_confirm_phrase(
                input_fn=lambda _prompt: DEFAULT_CONFIRM_PHRASE,
            )
            is True
        )

    def test_rejects_wrong_phrase(self) -> None:
        assert (
            require_typed_confirm_phrase(
                input_fn=lambda _prompt: "yes",
            )
            is False
        )

    def test_rejects_case_mismatch(self) -> None:
        assert (
            require_typed_confirm_phrase(
                input_fn=lambda _prompt: "apply smartlist",
            )
            is False
        )

    def test_strips_trailing_whitespace(self) -> None:
        assert (
            require_typed_confirm_phrase(
                input_fn=lambda _prompt: DEFAULT_CONFIRM_PHRASE + "\n",
            )
            is True
        )

    def test_returns_false_on_eof(self) -> None:
        def raise_eof(_prompt: str) -> str:
            raise EOFError()

        assert require_typed_confirm_phrase(input_fn=raise_eof) is False

    def test_returns_false_on_keyboard_interrupt(self) -> None:
        def raise_kbi(_prompt: str) -> str:
            raise KeyboardInterrupt()

        assert require_typed_confirm_phrase(input_fn=raise_kbi) is False

    def test_custom_phrase(self) -> None:
        assert (
            require_typed_confirm_phrase(
                "NUCLEAR",
                input_fn=lambda _p: "NUCLEAR",
            )
            is True
        )


class TestSafePlaylistWriterDryRun:
    """Rail 4: dry-run default blocks ALL writes until opted out."""

    def test_default_dry_run_is_true(self, tmp_path) -> None:
        safe, _raw, _db = _make_safe_writer(tmp_path)
        assert safe.dry_run is True
        assert safe.flag_ok is False

    def test_dry_run_create_is_noop(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(tmp_path)
        safe.create_playlist("[SL] DR", ["sid-1", "sid-2"])
        assert raw.calls == []  # inner never called
        assert raw.playlists == {}

    def test_dry_run_apply_diff_is_noop(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(tmp_path)
        safe.apply_diff("[SL] DR", added=["sid-1"], removed=[])
        assert raw.calls == []

    def test_dry_run_session_yields_self_without_opening_live(
        self,
        tmp_path,
    ) -> None:
        """safe_writer_session in dry-run must NOT open LiveWriteSession.

        This is important: LiveWriteSession would pgrep and backup the DB,
        neither of which is safe (or possible) in a test without mocks.
        """
        safe, _raw, _db = _make_safe_writer(tmp_path)
        with safe_writer_session(safe) as s:
            assert s is safe
            # The session helper must NOT have set _session.
            assert safe._session is None

    def test_playlist_exists_passthrough_even_in_dry_run(
        self,
        tmp_path,
    ) -> None:
        """Read-only playlist_exists bypasses all rails."""
        inner = FakeWriter(vendor="rekordbox", playlists={"[SL] X": []})
        safe, _raw, _db = _make_safe_writer(tmp_path, inner=inner)
        assert safe.playlist_exists("[SL] X") is True
        assert safe.playlist_exists("[SL] Y") is False


class TestSafePlaylistWriterLiveGuards:
    """Rail 3 + 4 guards: live writes require flag + active session."""

    def test_live_without_flag_refuses(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=False,
        )
        with pytest.raises(RuntimeError, match="typed-confirm flag not set"):
            safe.create_playlist("[SL] A", ["sid-1"])
        assert raw.calls == []

    def test_live_without_session_refuses(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )
        # Called outside safe_writer_session() -> _session is None.
        with pytest.raises(RuntimeError, match="no active LiveWriteSession"):
            safe.create_playlist("[SL] B", ["sid-1"])
        assert raw.calls == []

    def test_apply_diff_live_without_flag_refuses(self, tmp_path) -> None:
        safe, raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=False,
        )
        with pytest.raises(RuntimeError, match="typed-confirm flag not set"):
            safe.apply_diff("[SL] A", added=["sid-1"], removed=[])
        assert raw.calls == []


class TestSafePlaylistWriterPgrepRail:
    """Rail 1: pgrep gate (via LiveWriteSession.process_gate)."""

    def test_pgrep_abort_blocks_create(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        # Force the process gate to report "running" -> SafetyAbort.
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: True,
        )
        safe, raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )
        with pytest.raises(SafetyAbort, match="Rekordbox is running"), safe_writer_session(safe):
            pass  # entry should raise before yielding
        assert raw.calls == []


class TestSafePlaylistWriterBackupRail:
    """Rail 2: timestamped backup before any write."""

    def test_backup_created_on_session_entry(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: False,
        )
        safe, _raw, db_path = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )
        with safe_writer_session(safe, reversal_root=tmp_path / "reversal"):
            # A .bak.<ISO> sibling of db_path must exist.
            backups = list(db_path.parent.glob(f"{db_path.name}.bak.*"))
            assert len(backups) == 1
            assert backups[0].read_bytes() == db_path.read_bytes()

    def test_backup_missing_source_aborts(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """If the target DB doesn't exist we abort (no backup possible)."""
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: False,
        )
        safe, _raw, db_path = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )
        db_path.unlink()
        with (
            pytest.raises(SafetyAbort, match="DB to back up is missing"),
            safe_writer_session(safe, reversal_root=tmp_path / "reversal"),
        ):
            pass


class TestSafePlaylistWriterVerifyRail:
    """Rail 5: post-write verify via injected verifier."""

    def test_verify_ok_writes_reverse_snippet(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: False,
        )
        safe, raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )
        verifier_calls: list[tuple[str, list[str]]] = []

        def verifier(name: str, expected: list[str]) -> bool:
            verifier_calls.append((name, list(expected)))
            return True

        reversal_root = tmp_path / "reversal"
        with safe_writer_session(
            safe,
            verifier=verifier,
            reversal_root=reversal_root,
        ) as s:
            s.create_playlist("[SL] V", ["sid-1", "sid-2"])
        # Verifier was called with the expected membership.
        assert verifier_calls == [("[SL] V", ["sid-1", "sid-2"])]
        # Reverse snippet was emitted.
        scripts = list(reversal_root.rglob("reverse.sh"))
        assert len(scripts) == 1
        body = scripts[0].read_text()
        assert "# revert smartlist create" in body
        assert "[SL] V" in body
        # Underlying writer DID fire (verify passed).
        assert raw.playlists == {"[SL] V": ["sid-1", "sid-2"]}

    def test_verify_fail_aborts_session(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: False,
        )
        safe, raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )

        # Verifier returns False -> verify_readback fails -> session
        # aborts the batch via LiveWriteSession's verify-failure path.
        # Non-tty stdin -> default action is "abort".
        def bad_verifier(_name: str, _expected: list[str]) -> bool:
            return False

        reversal_root = tmp_path / "reversal"
        with (
            pytest.raises(SafetyAbort, match="verify_readback failed"),
            safe_writer_session(
                safe,
                verifier=bad_verifier,
                reversal_root=reversal_root,
            ) as s,
        ):
            s.create_playlist("[SL] Bad", ["sid-1"])
        # The inner writer DID run (the verify happens AFTER the write).
        assert raw.playlists == {"[SL] Bad": ["sid-1"]}


class TestSafePlaylistWriterReversalRail:
    """Rail 6: reversal script content + path layout."""

    def test_reversal_script_header_and_shebang(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: False,
        )
        safe, _raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
        )
        reversal_root = tmp_path / "reversal"
        with safe_writer_session(safe, reversal_root=reversal_root):
            pass
        scripts = list(reversal_root.rglob("reverse.sh"))
        assert len(scripts) == 1
        content = scripts[0].read_text()
        assert content.startswith("#!/usr/bin/env bash")
        assert "target=rekordbox" in content
        # Last-resort restore is always appended on clean exit.
        assert "Last-resort full restore" in content

    def test_apply_diff_appends_reverse_snippet(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda _name: False,
        )
        inner = FakeWriter(
            vendor="rekordbox",
            playlists={"[SL] R": ["sid-1", "sid-2"]},
        )
        safe, _raw, _db = _make_safe_writer(
            tmp_path,
            dry_run=False,
            flag_ok=True,
            inner=inner,
        )
        reversal_root = tmp_path / "reversal"
        with safe_writer_session(safe, reversal_root=reversal_root) as s:
            s.apply_diff("[SL] R", added=["sid-3"], removed=["sid-1"])
        scripts = list(reversal_root.rglob("reverse.sh"))
        body = scripts[0].read_text()
        assert "# revert smartlist diff on '[SL] R'" in body
        assert "added=['sid-3']" in body
        assert "removed=['sid-1']" in body


class TestSafePlaylistWriterVendorPassthrough:
    """The adapter is a drop-in PlaylistWriter (vendor preserved)."""

    def test_vendor_matches_inner(self, tmp_path) -> None:
        safe_rb, _r1, _d1 = _make_safe_writer(tmp_path, target="rekordbox")
        assert safe_rb.vendor == "rekordbox"

        # Make the inner writer a djay-flavoured fake; the safe adapter
        # should surface the inner.vendor regardless of `target`.
        inner = FakeWriter(vendor="djay")
        safe_djay = SafePlaylistWriter(
            inner=inner,
            db_path=tmp_path / "djay.db",
            target="djay",
        )
        assert safe_djay.vendor == "djay"

    def test_target_validation_rejects_bogus_target(self, tmp_path) -> None:
        """Only rekordbox|djay are accepted when opening a live session."""
        (tmp_path / "f.db").write_bytes(b"x")
        safe = SafePlaylistWriter(
            inner=FakeWriter(),
            db_path=tmp_path / "f.db",
            target="bogus",
            dry_run=False,
            flag_ok=True,
        )
        with pytest.raises(ValueError, match="target must be"), safe_writer_session(safe):
            pass
