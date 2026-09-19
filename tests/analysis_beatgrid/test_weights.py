"""Producer version agreement, and offline checkpoint provisioning.

The version test reads `beat_this_runner.py` as TEXT. That file imports torch
at module scope and lives in its own PEP 723 environment, so the repo venv
cannot import it; reading the literal is the only way this suite can hold the
two constants equal, and holding them equal is what stops a record being keyed
by a version that did not produce it.

-Claude
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.analysis_beatgrid import weights
from apps.analysis_beatgrid.version import PRODUCER_VERSION

RUNNER = (
    Path(__file__).resolve().parents[2]
    / "apps" / "analysis_beatgrid" / "beat_this_runner.py"
)


def _runner_literal(name: str) -> str:
    match = re.search(rf'^{name} = "([^"]+)"$', RUNNER.read_text(encoding="utf-8"), re.M)
    assert match is not None, f"{RUNNER} no longer declares {name} as a bare literal"
    return match.group(1)


def test_producer_version_matches_the_runner_literal() -> None:
    assert _runner_literal("PRODUCER_VERSION") == PRODUCER_VERSION


def test_runner_producer_name_matches_the_backend_name() -> None:
    from apps.analysis.backends.own_beatgrid import BACKEND_NAME

    assert _runner_literal("PRODUCER") == BACKEND_NAME


def test_version_literal_control_can_fail() -> None:
    """The reader is checked against a name the runner does NOT declare.

    Without this, a regex that silently matched nothing would make both tests
    above pass for a reason unrelated to the constants they compare.
    """
    with pytest.raises(AssertionError, match="no longer declares"):
        _runner_literal("NOT_A_CONSTANT_IN_THAT_FILE")


#-----------------------------------------------------------------------------
# weights
#-----------------------------------------------------------------------------

def _fixture_checkpoint(tmp_path: Path, body: bytes = b"beat-this weights") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / weights.CHECKPOINT_FILENAME
    path.write_bytes(body)
    return path


def test_env_override_is_searched_first_and_its_digest_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fixture_checkpoint(tmp_path)
    digest = weights.sha256_of(source)
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(source))
    resolved, reported = weights.resolve_checkpoint(expected_sha256=digest)
    assert resolved == source
    assert reported == digest


def test_absent_checkpoint_names_every_location_it_looked_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(tmp_path / "nowhere.ckpt"))
    with pytest.raises(weights.WeightsUnavailable) as exc:
        weights.resolve_checkpoint(app_dir=tmp_path / "app")
    message = str(exc.value)
    assert "nowhere.ckpt" in message
    assert str(tmp_path / "app") in message


def test_a_wrong_checkpoint_is_refused_rather_than_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fixture_checkpoint(tmp_path, b"some other model entirely")
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(source))
    with pytest.raises(weights.WeightsMismatch, match="pinned to"):
        weights.resolve_checkpoint(expected_sha256="0" * 64)


def test_an_unusable_override_never_falls_back_to_the_app_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this guard exists for (Sol P1, PR #1587): an explicit,
    unusable `MDT_BEATGRID_WEIGHTS` must not resolve to whatever the app
    weights directory happens to hold. That would run a DIFFERENT checkpoint
    than the one asked for, silently."""
    app_dir = tmp_path / "app"
    real = _fixture_checkpoint(app_dir)
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(tmp_path / "does-not-exist.ckpt"))
    with pytest.raises(weights.WeightsUnavailable, match="does not name a file"):
        weights.resolve_checkpoint(expected_sha256=weights.sha256_of(real), app_dir=app_dir)


def test_an_unset_override_still_resolves_from_the_app_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both-directions control: the ordinary case (no override at all) must
    still resolve location 2, unaffected by the override-specific guard."""
    app_dir = tmp_path / "app"
    real = _fixture_checkpoint(app_dir)
    monkeypatch.delenv(weights.WEIGHTS_PATH_ENV, raising=False)
    resolved, _reported = weights.resolve_checkpoint(
        expected_sha256=weights.sha256_of(real), app_dir=app_dir
    )
    assert resolved == real


def test_no_torch_hub_fallback_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one behavior this module exists to remove.

    A negative control: with both named locations empty, resolution must fail
    LOUDLY rather than resolve anything, on a machine whose Torch Hub cache
    holds a perfectly good `final0`. Reported as an absence claim with a probe
    that can fail: the assertion is on the raised message, not on a silence.
    """
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(tmp_path / "absent.ckpt"))
    with pytest.raises(weights.WeightsUnavailable, match="never falls back"):
        weights.resolve_checkpoint(app_dir=tmp_path / "empty-app-dir")

    source = Path(weights.__file__).read_text(encoding="utf-8")
    # The probe is shown able to FIND something first, so a rename of the file
    # or an unreadable read cannot make these three absences vacuously true.
    assert "WHY THE LANE OWNS THIS AT ALL" in source
    assert "import torch" not in source
    assert "torch.hub" not in source
    assert "hub.get_dir" not in source


def test_install_verifies_before_it_writes(tmp_path: Path) -> None:
    source = _fixture_checkpoint(tmp_path / "src", b"x")
    dest = tmp_path / "dest"
    with pytest.raises(weights.WeightsMismatch, match="refusing to install"):
        weights.install(source, dest_dir=dest, expected_sha256="0" * 64)
    assert not dest.exists() or not list(dest.iterdir())


def test_install_then_resolve_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fixture_checkpoint(tmp_path / "src")
    digest = weights.sha256_of(source)
    dest = tmp_path / "dest"
    installed = weights.install(source, dest_dir=dest, expected_sha256=digest)
    assert installed.is_file()
    assert not list(dest.glob("*.partial"))
    monkeypatch.delenv(weights.WEIGHTS_PATH_ENV, raising=False)
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(installed))
    resolved, reported = weights.resolve_checkpoint(expected_sha256=digest)
    assert (resolved, reported) == (installed, digest)


def test_install_from_a_missing_source_says_so(tmp_path: Path) -> None:
    with pytest.raises(weights.WeightsUnavailable, match="does not exist"):
        weights.install(tmp_path / "absent.ckpt", dest_dir=tmp_path / "dest")


def test_cli_path_subcommand_lists_both_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(weights.WEIGHTS_PATH_ENV, str(tmp_path / "override.ckpt"))
    assert weights.main(["path"]) == 0
    printed = capsys.readouterr().out
    assert "override.ckpt [absent]" in printed
    assert str(weights.app_weights_dir()) in printed
