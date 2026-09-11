"""The hosted-hub config switch: strict env, a real standings file, fail-fast startup.

Every hub DB here is a real migrated sqlite DB with owners enrolled through
the real /enroll door; the webui is the real ``create_app``.

- [if] anything but "1" turns hosted mode on [then] broken, [else stop].
- [if] hosted mode starts with no source, or a malformed one [then] broken, [else stop].
- [if] an unset env configures anything but the inert self-hosted hub [then] broken, [else stop].
- [if] a hosted hub starts on a DB two owners share [then] broken, [else stop].
- [if] the CLI cannot report what startup would decide [then] broken, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.entitlements import Standing
from apps.sync_hub import client, hosted_config, maintenance
from apps.webui.server.app import create_app

from .conftest import ENROLL_OWNER_SUB, seed_user
from .enrollment_helpers import http_enroll, mint_grant
from .enrollment_transport import TestClientTransport
from .test_hosted_entitlement_gate import _hub_app

pytestmark = pytest.mark.requirement("CAT-04")

FEATURE: str = "cloudsync.hosted_hub"
PROVIDER: str = "zz-test-provider"
SECOND_SUB: str = "zz-test-second-owner-sub"
SECOND_EMAIL: str = "zz-test-second-owner@example.invalid"


def _standings_file(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "standings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_payload() -> dict[str, Any]:
    entry = {
        "subject": ENROLL_OWNER_SUB,
        "feature_id": FEATURE,
        "state": "read_only",
        "quota": None,
    }
    return {"provider": PROVIDER, "standings": [entry]}


def _hosted_env(tmp_path: Path) -> dict[str, str]:
    return {
        hosted_config.HOSTED_ENV: "1",
        hosted_config.ENTITLEMENTS_FILE_ENV: str(_standings_file(tmp_path, _valid_payload())),
    }


def _enroll_owners(hub_dir: Path, spoke_dirs: list[tuple[Path, str | None]]) -> None:
    """Enroll each spoke through the real /enroll door; email None means the seeded owner."""
    with TestClient(_hub_app(hub_dir, hosted=False)) as http:
        for spoke_dir, email in spoke_dirs:
            token = mint_grant(hub_dir) if email is None else mint_grant(hub_dir, email=email)
            # machines.name is UNIQUE: name each spoke after its own dir.
            http_enroll(TestClientTransport(http), spoke_dir, name=spoke_dir.name, token=token)


# ----- the switch -----------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("", False), ("0", False), ("1", True)])
def test_only_the_string_1_turns_hosted_on(raw: str, expected: bool) -> None:
    """If unset, "" or "0" is hosted, or "1" is not, then billing enforcement is a coin toss."""
    assert hosted_config.parse_hosted({hosted_config.HOSTED_ENV: raw}) is expected
    assert hosted_config.parse_hosted({}) is False


@pytest.mark.parametrize("raw", ["true", "yes", " 1", "2", "on"])
def test_a_lookalike_hosted_value_is_refused(raw: str) -> None:
    """If a truthy-looking value is guessed either way then a typo switches billing on or off."""
    with pytest.raises(hosted_config.HostedConfigError, match="must be unset, '0' or '1'"):
        hosted_config.parse_hosted({hosted_config.HOSTED_ENV: raw})


def test_hosted_without_an_entitlements_file_fails_fast() -> None:
    """If hosted mode starts with no source then it answers who may sync by guessing."""
    with pytest.raises(hosted_config.HostedConfigError, match=hosted_config.ENTITLEMENTS_FILE_ENV):
        hosted_config.from_env({hosted_config.HOSTED_ENV: "1"})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(extra=1),
        lambda p: p["standings"][0].update(state="lapsed"),
        lambda p: p["standings"][0].update(quota=True),
        lambda p: p["standings"][0].update(subject=" "),
        lambda p: p["standings"].append(dict(p["standings"][0])),
    ],
    ids=["extra-key", "unknown-state", "bool-quota", "blank-subject", "duplicate-pair"],
)
def test_a_malformed_standings_file_fails_fast(tmp_path: Path, mutate: Any) -> None:
    """If a malformed standings file is partly served then some owner gets a guessed answer."""
    payload = _valid_payload()
    mutate(payload)
    with pytest.raises(hosted_config.HostedConfigError):
        hosted_config.load_static_source(_standings_file(tmp_path, payload))


def test_a_valid_file_serves_exactly_its_table(tmp_path: Path) -> None:
    """If the loaded source answers differently from the file, or invents a record, then broken."""
    config = hosted_config.from_env(_hosted_env(tmp_path))
    assert config.hosted is True and config.source is not None
    assert config.source.provider == PROVIDER
    assert config.source.standing(ENROLL_OWNER_SUB, FEATURE) == Standing("read_only", None)
    assert config.source.standing("zz-test-absent-sub", FEATURE) is None


# ----- startup --------------------------------------------------------------


def test_an_unset_env_configures_the_inert_self_hosted_hub(tmp_path: Path) -> None:
    """If an unset env attaches a source or touches the DB then self-hosted behavior changed."""
    app = FastAPI()
    db_path = tmp_path / "state" / "state.db"
    config = hosted_config.configure(app, {}, db_path=db_path)
    assert (app.state.sync_hub_hosted, app.state.entitlement_source) == (False, None)
    assert config.describe() == {"hosted": False, "entitlement_provider": None}
    assert not db_path.exists()


def test_webui_startup_fails_fast_when_hosted_has_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the webui boots hosted with no source then only the first request finds out."""
    monkeypatch.setenv(hosted_config.HOSTED_ENV, "1")
    monkeypatch.delenv(hosted_config.ENTITLEMENTS_FILE_ENV, raising=False)
    with pytest.raises(hosted_config.HostedConfigError):
        create_app(state_db_path=str(tmp_path / "state" / "state.db"), mount_frontend=False)


def test_webui_startup_hosted_with_a_source_wires_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a correctly configured hosted webui does not reach app.state then the gate never runs."""
    for key, value in _hosted_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    app = create_app(state_db_path=str(tmp_path / "state" / "state.db"), mount_frontend=False)
    assert app.state.sync_hub_hosted is True
    assert app.state.entitlement_source.provider == PROVIDER


def test_a_hosted_hub_refuses_to_start_on_a_db_two_owners_share(
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    tmp_path: Path,
) -> None:
    """If hosted mode starts on a shared DB then pull hands each owner the other's library."""
    seed_user(client.state_db_path(enroll_hub_dir), sub=SECOND_SUB, email=SECOND_EMAIL)
    _enroll_owners(enroll_hub_dir, [(enroll_spoke_dir, None)])
    db_path = client.state_db_path(enroll_hub_dir)
    hosted_config.configure(FastAPI(), _hosted_env(tmp_path), db_path=db_path)  # one owner: starts

    _enroll_owners(enroll_hub_dir, [(enroll_other_spoke_dir, SECOND_EMAIL)])
    with pytest.raises(hosted_config.HostedConfigError, match="holds 2 owners"):
        hosted_config.configure(FastAPI(), _hosted_env(tmp_path), db_path=db_path)


# ----- the CLI twin ---------------------------------------------------------


def test_the_hosted_cli_prints_what_startup_would_decide(
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If an agent cannot read hosted, provider and owners from the CLI then parity breaks."""
    _enroll_owners(enroll_hub_dir, [(enroll_spoke_dir, None)])
    for key, value in _hosted_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    assert maintenance.main(["hosted", "--data-dir", str(enroll_hub_dir)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "hosted": True,
        "entitlement_provider": PROVIDER,
        "owners": 1,
    }

    monkeypatch.delenv(hosted_config.HOSTED_ENV)
    assert maintenance.main(["hosted", "--data-dir", str(enroll_hub_dir)]) == 0
    readout = json.loads(capsys.readouterr().out)
    assert (readout["hosted"], readout["entitlement_provider"]) == (False, None)
