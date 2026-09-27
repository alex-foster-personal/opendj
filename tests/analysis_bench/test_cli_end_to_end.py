"""The whole path on one synthetic bundle: seal, push, pull, run, score, post.

This is the regression test for the thing the issue asks to be proved -- a host
with no music library can pull a bundle by checksum, run a candidate, score it
and post the round. It uses a synthetic click grid because the real beatgrid
fixtures need a Mac's rekordbox ANLZ; that limit is stated in the bundle's own
manifest (`synthetic`) so nobody can mistake a run over it for a measurement of
an analyzer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_bench import bundles, cli, lanes
from tests.analysis_bench import synthetic


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    staged = tmp_path / "beatgrid-v1"
    fixtures = synthetic.build(staged)
    bundles.seal_bundle(
        staged, lane="beatgrid", version="v1", manifest_extra=synthetic.manifest_extra(fixtures)
    )
    return staged


# REQ: NATIVE-11
@pytest.mark.requirement("NATIVE-11")
def test_seal_push_pull_run_and_post(bundle: Path, tmp_path: Path) -> None:
    """[if] a bundle is pushed, pulled and run with --post [then] a round is logged, [else stop]."""
    store_root = tmp_path / "asset-store"
    assert cli.main(["fixtures", "push", "--lane", "beatgrid", "--version", "v1",
                     "--bundle-dir", str(bundle), "--store", str(store_root)]) == 0
    pulled = tmp_path / "pulled"
    assert cli.main(["fixtures", "pull", "--lane", "beatgrid", "--version", "v1",
                     "--dest", str(pulled), "--store", str(store_root)]) == 0

    # The round goes to a scratch log through the production `--log-path` input,
    # not by replacing the lane registry in-process: a test that swaps the
    # registry is not exercising the configuration `--post` actually reads.
    log = tmp_path / "spec.md"
    log.write_text("# spec\n\n## Experiment log\n")
    report_path = tmp_path / "report.json"
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(pulled), "--workdir", str(tmp_path / "arms"),
                     "--out", str(report_path), "--post", "--log-path", str(log)]) == 0

    report = json.loads(report_path.read_text())
    assert report["scorer_version"] == "1.1.0"
    assert set(report["arms"]) == {"constant_128", "truth_offset_25ms"}
    assert report["bundle"]["bundle_id"] == bundles.verify_bundle(bundle)["bundle_id"]

    # The positive control must land every beat and carry its injected shift as
    # a measured one: 25 ms of raw nearest-beat error that the median-shift
    # correction removes. If the truth join, the windowing or the shift
    # correction broke, this is the assertion that notices. F itself cannot see
    # the shift, because 25 ms is inside the +/-70 ms tolerance -- which is
    # exactly why scorer v1.1.0 reports the position percentiles beside it.
    positive = report["arms"]["truth_offset_25ms"]["fixed"]
    assert positive["f_measure_mean"] == 1.0
    assert positive["f_measure_shifted_mean"] == 1.0
    assert abs(positive["raw_p50_ms"] - 25.0) < 1.0
    assert positive["shifted_p50_ms"] < 1.0
    assert positive["bpm_exact_0_01_pct"] == 100.0
    # The negative control is the floor the candidate row is read against.
    assert report["arms"]["constant_128"]["fixed"]["f_measure_mean"] < 1.0

    text = log.read_text()
    assert "### beatgrid bench round 2" in text
    assert "scorer 1.1.0" in text
    assert "constant_128 (negative_control)" in text


def test_run_without_controls_cannot_post(bundle: Path, tmp_path: Path, capsys) -> None:
    """--no-controls is allowed for a quick look and must never reach the log."""
    log = tmp_path / "spec.md"
    log.write_text("# spec\n")
    code = cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(tmp_path / "arms"),
                     "--no-controls", "--post", "--log-path", str(log)])
    assert code == 2
    assert "cannot be posted" in capsys.readouterr().err
    assert log.read_text() == "# spec\n"


def test_scoring_a_tampered_bundle_refuses(bundle: Path, tmp_path: Path, capsys) -> None:
    (bundle / "wav" / "synthetic-0.wav").write_bytes(b"not the audio it was sealed with")
    code = cli.main(["score", "--lane", "beatgrid", "--bundle-dir", str(bundle),
                     "--arm", "constant_128=/dev/null"])
    assert code == 2
    assert "wav/synthetic-0.wav" in capsys.readouterr().err


def test_an_arm_measured_on_another_bundle_is_refused(bundle: Path, tmp_path: Path) -> None:
    """A rebuild that keeps the stable ids must not be able to reuse old results."""
    arms = tmp_path / "arms"
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(arms)]) == 0
    stamped = json.loads((arms / "constant_128.json").read_text())
    assert stamped["bundle_id"] == bundles.verify_bundle(bundle)["bundle_id"]

    other = tmp_path / "other"
    fixtures = synthetic.build(other, bpms=(90.0, 128.0, 174.0))
    bundles.seal_bundle(other, lane="beatgrid", version="v1",
                        manifest_extra=synthetic.manifest_extra(fixtures))
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["score", "--lane", "beatgrid", "--bundle-dir", str(other),
                  "--arm", f"constant_128={arms / 'constant_128.json'}"])
    assert "another bundle's numbers" in str(excinfo.value)


def test_a_candidate_that_writes_nothing_is_refused(tmp_path: Path) -> None:
    """`true` exits 0 and writes no results. The stale file must not stand in."""
    arms = tmp_path / "arms"
    arms.mkdir()
    stale = arms / "noop.json"
    stale.write_text('{"schema": 1, "candidate": "a previous run", "results": {}}')
    noop = lanes.Candidate(name="noop", role="candidate", argv=("true",), note="writes nothing")
    with pytest.raises(SystemExit) as excinfo:
        cli.run_arm(noop, "unused-manifest.json", arms, "some-bundle-id")
    assert "wrote no" in str(excinfo.value)
    assert not stale.exists()


def test_a_stale_results_file_is_replaced_not_reused(bundle: Path, tmp_path: Path) -> None:
    """The same path from a previous round must not survive into this one."""
    arms = tmp_path / "arms"
    arms.mkdir()
    (arms / "constant_128.json").write_text('{"schema": 1, "candidate": "STALE", "results": {}}')
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(arms),
                     "--no-controls"]) == 0
    assert json.loads((arms / "constant_128.json").read_text())["candidate"] == "constant_128"


def test_a_partial_candidate_keeps_the_bundle_denominator(bundle: Path, tmp_path: Path) -> None:
    """An arm that answers only its easy tracks is scored over all of them.

    Dropping the unanswered fixtures would aggregate the candidate over a
    smaller, friendlier denominator while the report went on advertising the
    bundle's full fixture count.
    """
    arms = tmp_path / "arms"
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(arms)]) == 0
    path = arms / "constant_128.json"
    payload = json.loads(path.read_text())
    kept = sorted(payload["results"])[0]
    payload["results"] = {kept: payload["results"][kept]}
    path.write_text(json.dumps(payload))

    report_path = tmp_path / "partial.json"
    assert cli.main(["score", "--lane", "beatgrid", "--bundle-dir", str(bundle),
                     "--arm", f"constant_128={path}", "--out", str(report_path)]) == 0
    report = json.loads(report_path.read_text())
    arm = report["arms"]["constant_128"]
    assert arm["n_omitted"] == 2
    assert arm["fixed"]["n"] == 3
    assert arm["fixed"]["n_emitted_nothing"] == 2


def test_an_arm_answering_a_foreign_fixture_is_refused(bundle: Path, tmp_path: Path) -> None:
    """The other direction: results for a track this bundle does not contain."""
    arms = tmp_path / "arms"
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(arms)]) == 0
    path = arms / "constant_128.json"
    payload = json.loads(path.read_text())
    payload["results"]["not-in-this-bundle"] = {"beats": [], "downbeats": None,
                                                "native_bpm": None, "error": None}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="does not contain"):
        cli.main(["score", "--lane", "beatgrid", "--bundle-dir", str(bundle),
                  "--arm", f"constant_128={path}"])


def test_a_bundle_sealed_for_another_lane_is_refused(tmp_path: Path) -> None:
    """A key bundle scored under --lane beatgrid would misfile the measurement."""
    staged = tmp_path / "key-v1"
    fixtures = synthetic.build(staged)
    bundles.seal_bundle(
        staged, lane="key", version="v1", manifest_extra=synthetic.manifest_extra(fixtures)
    )
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                  "--bundle-dir", str(staged), "--workdir", str(tmp_path / "arms"),
                  "--no-controls"])
    assert "sealed as key/v1" in str(excinfo.value)
    assert not (tmp_path / "arms" / "constant_128.json").exists(), "refused before launching"


def test_a_bundle_of_another_version_is_refused(bundle: Path, tmp_path: Path) -> None:
    """The round line quotes the version, so a mismatch would mislabel the round."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "--lane", "beatgrid", "--version", "v2", "--candidate", "constant_128",
                  "--bundle-dir", str(bundle), "--workdir", str(tmp_path / "arms"),
                  "--no-controls"])
    assert "beatgrid/v1" in str(excinfo.value)
    assert "beatgrid/v2" in str(excinfo.value)


def test_an_arm_labeled_as_another_candidate_is_refused(bundle: Path, tmp_path: Path) -> None:
    """`--arm beat_this=constant_128.json` would print the control's numbers as Beat This."""
    arms = tmp_path / "arms"
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(arms), "--no-controls"]) == 0
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["score", "--lane", "beatgrid", "--bundle-dir", str(bundle),
                  "--arm", f"truth_offset_25ms={arms / 'constant_128.json'}"])
    assert "emitted by 'constant_128'" in str(excinfo.value)
    assert "'truth_offset_25ms'" in str(excinfo.value)


def test_a_correctly_labeled_arm_still_scores(bundle: Path, tmp_path: Path) -> None:
    """The control: the refusal above would pass against a check that always refused."""
    arms = tmp_path / "arms"
    assert cli.main(["run", "--lane", "beatgrid", "--candidate", "constant_128",
                     "--bundle-dir", str(bundle), "--workdir", str(arms), "--no-controls"]) == 0
    out = tmp_path / "report.json"
    assert cli.main(["score", "--lane", "beatgrid", "--bundle-dir", str(bundle),
                     "--arm", f"constant_128={arms / 'constant_128.json'}",
                     "--out", str(out)]) == 0
    assert set(json.loads(out.read_text())["arms"]) == {"constant_128"}
