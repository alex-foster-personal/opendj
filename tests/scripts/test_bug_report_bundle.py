"""Privacy-safe diagnostics bundle (issue #11173)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.telemetry.scrub import (
    BUG_REPORT_ALLOWED_KEYS,
    BUG_REPORT_TEXT_MAX_BYTES,
    REDACTED,
    assert_bug_report_text_safe,
    scrub_bug_report_mapping,
    scrub_string,
    truncate_bug_report_text,
)
from scripts import bug_report_bundle as mod

_SPOTIFY_TOKEN = "BQC0super-secret-spotify-access-token-value"
_ENV_SECRET = "sk-test-super-secret-env-value-should-never-appear"


@pytest.mark.requirement("OBS-12015")
def test_env_and_spotify_tokens_absent_from_bundle_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] .env holds a token and prefs hold Spotify [then] bundle text has neither, [else stop]."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"MUSIC_DJ_BACKEND_PORT=8599\nOPENDJ_API_KEY={_ENV_SECRET}\n",
        encoding="utf-8",
    )
    prefs_dir = tmp_path / "state"
    prefs_dir.mkdir(parents=True)
    prefs_dir.joinpath("ui-prefs.json").write_text(
        json.dumps({"spotify_access_token": _SPOTIFY_TOKEN, "midi_enabled": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    probe = mod.BackendProbe(
        base="http://127.0.0.1:8599",
        reachable=False,
        decks_live=False,
        build_sha="abc123",
        health_summary=None,
        client_errors=[],
        conn_dots={"conn_dot_fe": "offline", "conn_dot_be": "offline", "conn_dot_lib": "empty"},
    )
    payload = mod._build_payload(
        ports=mod._read_env_ports(env_file),
        git_sha="deadbeef",
        probe=probe,
        prefs_booleans=mod._read_redacted_prefs(tmp_path),
    )
    text = json.dumps(payload)
    assert _ENV_SECRET not in text
    assert _SPOTIFY_TOKEN not in text
    assert payload["prefs_booleans"].get("midi_enabled") is True
    assert "spotify_access_token" not in text


@pytest.mark.requirement("OBS-12015")
def test_live_decks_refuse_shareable_bundle(tmp_path: Path) -> None:
    """[if] any deck is playing or audible [then] refusal file only, under 256KiB text, [else stop]."""
    path = mod._write_refusal(tmp_path, "decks_playing_or_audible")
    body = path.read_text(encoding="utf-8")
    assert len(body.encode("utf-8")) <= BUG_REPORT_TEXT_MAX_BYTES
    assert "decks_playing_or_audible" in body
    assert _ENV_SECRET not in body


@pytest.mark.requirement("OBS-12015")
def test_forbidden_diagnostics_classes_absent_from_scrubbed_errors() -> None:
    """[if] output is checked for ANLZ, audio paths, DB dumps [then] they are absent, [else stop]."""
    raw = {
        "message": "failed on /home/USER/Music/Artist - Title.flac with ANLZ parse",
        "stack": "USB_EXPORT file list: track1.mp3 track2.flac",
        "context": {"deck_id": "A", "track_title": "Secret"},
    }
    scrubbed = scrub_bug_report_mapping(
        {
            "client_errors": [
                {
                    "message": raw["message"],
                    "stack": raw["stack"],
                    "context": raw["context"],
                }
            ]
        }
    )
    text = json.dumps(scrubbed)
    assert "Artist" not in text
    assert ".flac" not in text or "<path" in text
    assert "ANLZ" not in text
    assert "USB_EXPORT" not in text
    assert "Secret" not in text
    assert_bug_report_text_safe(text)


@pytest.mark.requirement("OBS-12015")
def test_redaction_tests_fail_closed_on_secrets_and_allowlist() -> None:
    """[if] CI runs redaction tests [then] missing allowlist keys redact secrets, [else stop]."""
    assert "track_title" not in BUG_REPORT_ALLOWED_KEYS
    payload = scrub_bug_report_mapping({"track_title": "Nina Vale", "deck_id": "A", "password": "hunter2"})
    assert payload["track_title"] == REDACTED
    assert payload["password"] == REDACTED
    assert payload["deck_id"] == "A"
    with pytest.raises(ValueError, match="secret-shaped"):
        assert_bug_report_text_safe("api_key=supersecretvalue123456")
    long_text = "x" * (BUG_REPORT_TEXT_MAX_BYTES + 50)
    clipped = truncate_bug_report_text(long_text)
    assert len(clipped.encode("utf-8")) <= BUG_REPORT_TEXT_MAX_BYTES
    assert "[filtered]" in scrub_string(f"bearer {_ENV_SECRET}")


@pytest.mark.requirement("OBS-12015")
def test_main_refuses_when_probe_reports_live_decks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] any deck is playing or audible [then] main exits 2 and writes refusal only, [else stop]."""
    monkeypatch.setattr(mod, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(
        mod,
        "_probe_backend",
        lambda _port: mod.BackendProbe(
            base="http://127.0.0.1:1",
            reachable=True,
            decks_live=True,
            build_sha="abc",
            health_summary=None,
            client_errors=[],
            conn_dots={},
        ),
    )
    assert mod.main(["--output-dir", str(tmp_path)]) == 2
    files = list(tmp_path.glob("bug-report-refused-*.json"))
    assert len(files) == 1
    assert not list(tmp_path.glob("bug-report-2*.json"))


@pytest.mark.requirement("OBS-12015")
def test_git_sha_launches_resolved_git_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] git resolves on PATH [then] _git_sha launches the resolved absolute path, [else stop]."""
    calls: list[list[str]] = []

    class _Done:
        stdout = "deadbeef\n"

    def _fake_run(argv: list[str], **_kw: object) -> _Done:
        """Record argv and return a fake completed process."""
        calls.append(argv)
        return _Done()

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    assert mod._git_sha() == "deadbeef"
    assert calls == [["/usr/bin/git", "rev-parse", "HEAD"]]


@pytest.mark.requirement("OBS-12015")
def test_git_sha_falls_back_when_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] git is not on PATH [then] _git_sha returns the build identity without spawning, [else stop]."""

    def _boom(*_a: object, **_kw: object) -> None:
        """Fail the test if a subprocess is spawned."""
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mod.subprocess, "run", _boom)
    monkeypatch.setattr(mod, "current_build_sha", lambda: "build-identity")
    assert mod._git_sha() == "build-identity"
