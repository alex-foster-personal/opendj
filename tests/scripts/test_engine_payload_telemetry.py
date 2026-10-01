"""OBS-04: payload bake + fail-loud verify for the ship Sentry DSN.

[if] a payload ships without a bundled DSN [then] verify fails naming the env var, [else stop].
[if] the build host offers only SENTRY_DSN [then] bake refuses the preview key, [else stop].

Until Mon 21 Sep 2026 every dmg carried no DSN and no SDK, so no installed
engine ever reported an error. Bake writes telemetry.json at the payload
root; verify fails naming the env var when it is absent or malformed; the
launcher exports the file's path so the engine can find it.

Regression lines:
  - if verify passes with no bundled DSN, a silent payload ships again
  - if bake falls back to SENTRY_DSN, a dmg reports under the preview key
  - if the launcher does not export OPENDJ_BUNDLED_TELEMETRY, a payload that
    staged the file still boots with no bundle
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.payload_telemetry import (
    BUNDLED_RELATIVE,
    FRONTEND_DSN_ENV,
    SHIP_DSN_ENV,
    PayloadTelemetryError,
    bake_telemetry,
    verify_bundled_telemetry,
)

pytestmark = pytest.mark.requirement("OBS-04")

FAKE_DSN = "https://public@o0.ingest.de.sentry.io/42"
FAKE_FRONTEND_DSN = "https://public@o0.ingest.de.sentry.io/43"
PREVIEW_DSN = "https://public@o0.ingest.de.sentry.io/41"
BOTH = {SHIP_DSN_ENV: FAKE_DSN, FRONTEND_DSN_ENV: FAKE_FRONTEND_DSN}


def test_bake_writes_telemetry_json_from_the_ship_env(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    path = bake_telemetry(payload, env=BOTH)
    assert path == payload / BUNDLED_RELATIVE
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "dsn": FAKE_DSN,
        "frontend_dsn": FAKE_FRONTEND_DSN,
    }
    verify_bundled_telemetry(payload)


def test_bake_fails_naming_the_frontend_variable_when_only_the_engine_dsn_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBS-06: a build with no frontend DSN would ask for consent and then
    record no replay. Fail here, not on a tester's Mac."""
    monkeypatch.setenv("PATH", "")
    with pytest.raises(PayloadTelemetryError) as excinfo:
        bake_telemetry(tmp_path / "payload", env={SHIP_DSN_ENV: FAKE_DSN})
    assert FRONTEND_DSN_ENV in str(excinfo.value)
    assert not (tmp_path / "payload" / BUNDLED_RELATIVE).exists()


def test_bake_never_falls_back_to_the_preview_sentry_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build host's SENTRY_DSN is the preview-dev key. A dmg reporting
    under it would file every tester as a preview host; refuse instead."""
    monkeypatch.setenv("PATH", "")  # no doppler either
    with pytest.raises(PayloadTelemetryError) as excinfo:
        bake_telemetry(tmp_path / "payload", env={"SENTRY_DSN": PREVIEW_DSN})
    message = str(excinfo.value)
    assert SHIP_DSN_ENV in message
    assert PREVIEW_DSN not in message
    assert not (tmp_path / "payload" / BUNDLED_RELATIVE).exists()


def test_bake_fails_naming_the_variable_when_env_and_doppler_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(PayloadTelemetryError) as excinfo:
        bake_telemetry(tmp_path / "payload", env={})
    assert SHIP_DSN_ENV in str(excinfo.value)


def test_bake_refuses_a_value_that_is_not_dsn_shaped(tmp_path: Path) -> None:
    with pytest.raises(PayloadTelemetryError) as excinfo:
        bake_telemetry(
            tmp_path / "payload", env={**BOTH, SHIP_DSN_ENV: "not-a-dsn"}
        )
    assert SHIP_DSN_ENV in str(excinfo.value)
    assert "not-a-dsn" not in str(excinfo.value)


def test_verify_fails_loud_when_the_bundled_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PayloadTelemetryError) as excinfo:
        verify_bundled_telemetry(tmp_path / "payload")
    assert SHIP_DSN_ENV in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    ["{}", "{\"dsn\": \"  \"}", "[]", "not json", f'{{"dsn": "{FAKE_DSN}"}}'],
)
def test_verify_fails_loud_on_a_malformed_bundle(tmp_path: Path, body: str) -> None:
    path = tmp_path / "payload" / BUNDLED_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    with pytest.raises(PayloadTelemetryError) as excinfo:
        verify_bundled_telemetry(tmp_path / "payload")
    message = str(excinfo.value)
    # A bundle with only the engine DSN is refused naming the FRONTEND var;
    # every other malformation names the engine one.
    expected = FRONTEND_DSN_ENV if body.startswith('{"dsn": "https') else SHIP_DSN_ENV
    assert expected in message


def test_launcher_exports_the_bundled_telemetry_path() -> None:
    from scripts.build_engine_payload import CLI_LAUNCHER_TEMPLATE, LAUNCHER_TEMPLATE

    for template in (LAUNCHER_TEMPLATE, CLI_LAUNCHER_TEMPLATE):
        assert 'OPENDJ_BUNDLED_TELEMETRY="$payload/telemetry.json"' in template
        assert "export OPENDJ_BUNDLED_TELEMETRY" in template


def test_the_engine_loads_what_the_bake_wrote(tmp_path: Path) -> None:
    """Round trip through the runtime loader, so the writer and the reader
    cannot drift apart on the key name."""
    from apps.shared.telemetry.bundled import (
        BUNDLED_TELEMETRY_ENV,
        load_bundled_telemetry,
    )

    payload = tmp_path / "payload"
    path = bake_telemetry(payload, env=BOTH)
    bundled = load_bundled_telemetry({BUNDLED_TELEMETRY_ENV: str(path)})
    assert bundled is not None
    assert bundled.dsn == FAKE_DSN
    assert bundled.frontend_dsn == FAKE_FRONTEND_DSN
