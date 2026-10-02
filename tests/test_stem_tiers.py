"""Regression tests for the S/M/L separation tiers.

One-line intent per test, in the house format:
  if a tier names a preset the farm cannot run, config is broken
  if a tier's model is not baked into the farm image, config is broken
  if an unbenchmarked (tier, gpu) pair returns a number, the estimator is broken
  if the estimator interpolates from a neighbouring card, the estimator is broken
  if wall and gpu clocks are conflated, the cost maths is broken
  if the farm's mirrored constants drift from tiers.py, the mirror is broken
  if the batch estimate ignores the concurrency cap, the packer is broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.stems import tiers as tiercfg


# ----- helpers ---------------------------------------------------------------
def _fake_throughput(tier_key: str, gpu: str) -> tiercfg.Throughput:
    return tiercfg.Throughput(
        tier_key=tier_key,
        gpu=gpu,
        wall_fixed_s=20.0,
        wall_s_per_audio_minute=10.0,
        gpu_fixed_s=8.0,
        gpu_s_per_audio_minute=6.0,
        n_tracks=3,
        duration_span_s=[150.0, 330.0, 600.0],
        r_squared=0.99,
        measured_by="test",
        measured_at="2026-07-24T00:00:00+00:00",
    )


@pytest.fixture
def measured(monkeypatch):
    """Install one measured pair, M@H100, and nothing else."""
    monkeypatch.setattr(
        tiercfg, "THROUGHPUT", {"M@H100": _fake_throughput("M", "H100")}
    )
    return tiercfg


# ----- config coherence ------------------------------------------------------
def test_ladder_has_four_distinct_rungs():
    """if the ladder loses a rung or two rungs share a preset, config broken"""
    assert set(tiercfg.TIERS) == {"LOCAL", "S", "M", "L"}
    tags = [t.preset_tag for t in tiercfg.TIERS.values()]
    assert len(set(tags)) == 4, f"rungs share a preset tag: {tags}"


def test_tier_order_covers_every_rung():
    """if a rung is missing from TIER_ORDER it is invisible in every UI"""
    assert set(tiercfg.TIER_ORDER) == set(tiercfg.TIERS)
    assert [t.key for t in tiercfg.ladder()] == list(tiercfg.TIER_ORDER)


def test_quick_rung_is_not_applicable_with_a_stated_reason():
    """if an empty rung ships with no reason, it invites re-proposing itself"""
    quick = tiercfg.get_tier("S")
    assert quick.availability == "NOT_APPLICABLE"
    assert quick.unavailable_because.strip()


def test_local_rung_names_no_gpu_and_costs_nothing():
    """if the local rung is priced as cloud GPU time, the ladder lies"""
    local = tiercfg.get_tier("LOCAL")
    assert local.where == "local"
    assert local.gpu == ""
    assert local.key not in {t.key for t in tiercfg.modal_tiers()}


def test_default_tier_is_a_real_tier():
    """if the default names a tier that does not exist, config broken"""
    assert tiercfg.DEFAULT_TIER in tiercfg.TIERS


# The farm is READ, NOT IMPORTED. ``scripts/modal_vocal_farm.py`` does a bare
# ``import modal`` at module scope, and modal is deliberately not a repo
# dependency -- every call site is `uv run --with modal`, and CI installs from
# requirements.txt. Importing it here made three tests die with
# ModuleNotFoundError in CI while passing locally.
#
# The obvious patch, pytest.importorskip("modal"), is NOT used: it turns the
# suite green by silently switching off the only check that the tier ladder and
# the farm still agree, which is a fallback masking a failure. Parsing the
# literals out of the source proves the same property against the real file, in
# any environment, and fails loudly if the constants stop being literals.
def _farm_literals() -> dict:
    """Top-level literal constants of the farm, via ast. No import, no modal."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "scripts/modal_vocal_farm.py"
    tree = ast.parse(src.read_text())
    out: dict = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if not isinstance(target, ast.Name) or node.value is None:
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except ValueError:
            continue  # computed, not a literal -- not our business
    return out


def _farm_presets() -> dict:
    """PRESETS as {tag: (model, overlap, shifts)}, read from the source.

    PRESETS is a dict of Preset(...) CALLS, so literal_eval cannot take it;
    the positional args are read directly instead.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "scripts/modal_vocal_farm.py"
    tree = ast.parse(src.read_text())
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1
            else None
        )
        if not isinstance(target, ast.Name) or target.id != "PRESETS":
            continue
        presets = {}
        for key, val in zip(node.value.keys, node.value.values, strict=False):
            args = [ast.literal_eval(a) for a in val.args]
            presets[ast.literal_eval(key)] = (args[1], args[2], args[3])
        return presets
    raise AssertionError("PRESETS not found in scripts/modal_vocal_farm.py")


def test_every_modal_rung_is_runnable_by_the_farm():
    """if a modal rung names a preset the farm cannot run, config broken

    LOCAL is excluded on purpose: it never touches the farm, so its preset tag
    is not one of the farm's and requiring it to be would be a false contract.
    """
    presets = _farm_presets()
    for tier in tiercfg.modal_tiers():
        assert tier.preset_tag in presets, (
            f"tier {tier.key} names {tier.preset_tag!r}, not in PRESETS"
        )
        assert presets[tier.preset_tag] == (
            tier.model, tier.overlap, tier.shifts
        ), f"tier {tier.key} disagrees with the farm preset of the same name"


def test_every_modal_rung_model_is_baked_into_the_image():
    """if a modal rung's model is not baked, every cold container re-downloads"""
    baked = _farm_literals()["BAKED_MODELS"]
    for tier in tiercfg.modal_tiers():
        assert tier.model in baked, (
            f"tier {tier.key} needs {tier.model!r}, baked: {baked}"
        )


def test_farm_mirror_matches_tier_config():
    """if the farm's mirrored constants drift from tiers.py, mirror broken"""
    lits = _farm_literals()
    assert lits["GPU_USD_PER_S"] == tiercfg.GPU_USD_PER_S
    assert lits["DEFAULT_GPU_KIND"] == tiercfg.DEFAULT_GPU
    assert lits["DEFAULT_MAX_CONTAINERS"] == tiercfg.MAX_CONCURRENT_GPUS


def test_blackwell_cards_are_not_selectable():
    """if B200/B300 appear as options, the pinned torch would fail to launch"""
    assert "B200" not in tiercfg.SUPPORTED_GPUS
    assert "B300" not in tiercfg.SUPPORTED_GPUS
    assert tiercfg.DEFAULT_GPU in tiercfg.SUPPORTED_GPUS


# ----- estimator refuses to guess --------------------------------------------
def test_unmeasured_pair_raises_rather_than_guessing(measured):
    """if an unbenchmarked pair returns a number, the estimator is broken"""
    with pytest.raises(tiercfg.ThroughputNotMeasured) as exc:
        tiercfg.estimate_seconds(240.0, "L", "H100")
    assert "L on H100" in str(exc.value)
    assert "tier_throughput" in str(exc.value), "error must name the fix"


def test_does_not_interpolate_from_a_neighbouring_card(measured):
    """if a measurement on one card answers for another, estimator is broken"""
    assert tiercfg.estimate_seconds(240.0, "M", "H100") > 0  # measured pair works
    with pytest.raises(tiercfg.ThroughputNotMeasured):
        tiercfg.estimate_seconds(240.0, "M", "L4")


def test_unknown_tier_key_raises():
    """if a typo'd tier silently resolves, config lookup is broken"""
    with pytest.raises(KeyError):
        tiercfg.get_tier("XL")


# ----- estimator arithmetic --------------------------------------------------
def test_estimate_is_fixed_plus_linear(measured):
    """if the estimate is not fixed + minutes * slope, the fit is misapplied"""
    # 240s = 4 audio-minutes -> 20 + 4 * 10 = 60
    assert tiercfg.estimate_seconds(240.0, "M", "H100") == pytest.approx(60.0)
    # 600s = 10 audio-minutes -> 20 + 10 * 10 = 120
    assert tiercfg.estimate_seconds(600.0, "M", "H100") == pytest.approx(120.0)


def test_cost_uses_billed_seconds_not_wall(measured):
    """if wall clock is billed, cost is overstated by the cold start"""
    # billed = 8 + 4 * 6 = 32s; wall would be 60s. They must differ.
    expected = 32.0 * tiercfg.GPU_USD_PER_S["H100"]
    assert tiercfg.estimate_usd(240.0, "M", "H100") == pytest.approx(expected)
    wall_priced = 60.0 * tiercfg.GPU_USD_PER_S["H100"]
    assert tiercfg.estimate_usd(240.0, "M", "H100") < wall_priced


# ----- batch packing ---------------------------------------------------------
def test_batch_respects_the_concurrency_cap(measured):
    """if the batch estimate ignores the cap, it promises impossible throughput"""
    durations = [240.0] * 20  # 20 tracks, 60s each, cap 10 -> two waves
    total = tiercfg.estimate_batch_seconds(
        durations, "M", "H100", max_concurrent=10
    )
    assert total == pytest.approx(120.0)


def test_batch_of_one_equals_single_estimate(measured):
    """if a one-track batch differs from a single estimate, the packer is broken"""
    assert tiercfg.estimate_batch_seconds(
        [240.0], "M", "H100"
    ) == pytest.approx(tiercfg.estimate_seconds(240.0, "M", "H100"))


def test_empty_batch_is_zero(measured):
    """if an empty batch costs time, the packer is broken"""
    assert tiercfg.estimate_batch_seconds([], "M", "H100") == 0.0


# ----- API parity ------------------------------------------------------------
def test_api_reports_unmeasured_rather_than_a_number(measured):
    """if the API renders a guess as a number, an assumption becomes truth"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    client = TestClient(create_app())
    body = client.get("/api/v1/stems/estimate", params={"seconds": 240}).json()
    by_tier = {row["tier"]: row for row in body["tiers"]}
    assert by_tier["L"]["measured"] is False
    assert by_tier["L"]["seconds"] is None
    assert by_tier["L"]["unavailable_reason"]


def test_api_lists_every_tier_with_its_evidence():
    """if a tier ships without recorded evidence, the choice is unreviewable"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    client = TestClient(create_app())
    rows = client.get("/api/v1/stems/tiers").json()
    assert {r["key"] for r in rows} == {"LOCAL", "S", "M", "L"}
    assert [r["key"] for r in rows] == list(tiercfg.TIER_ORDER), "ladder order"
    for row in rows:
        assert row["evidence"].strip(), f"tier {row['key']} has no evidence"
        assert row["evidence_strength"] in {"MEASURED", "PARTIAL", "UNMEASURED"}


def test_local_rung_does_not_break_the_whole_estimate_response(monkeypatch):
    """if a rung with no GPU rate 400s the response, every tier disappears

    Regression: estimate_usd checked `card not in GPU_USD_PER_S` before it
    checked whether the rung even runs on a GPU, so once LOCAL had a measured
    row it raised KeyError, which the route turned into a 400 for ALL tiers.
    It looked like a frontend bug and was masked only by LOCAL being unmeasured.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    table = {
        "LOCAL@": _fake_throughput("LOCAL", ""),
        "M@H100": _fake_throughput("M", "H100"),
    }
    monkeypatch.setattr(tiercfg, "THROUGHPUT", table)
    assert tiercfg.estimate_usd(240.0, "LOCAL") == 0.0

    client = TestClient(create_app())
    r = client.get("/api/v1/stems/estimate", params={"seconds": 240})
    assert r.status_code == 200, r.text
    by_tier = {row["tier"]: row for row in r.json()["tiers"]}
    assert set(by_tier) == set(tiercfg.TIER_ORDER)
    assert by_tier["LOCAL"]["measured"] is True
    assert by_tier["LOCAL"]["usd"] == 0.0


def test_generate_refuses_a_not_applicable_rung():
    """if the API runs a rung the project retired, the ladder is decorative"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    client = TestClient(create_app())
    r = client.post("/api/v1/stems/generate", json={"stable_id": "x", "tier": "S"})
    assert r.status_code == 409, r.text
    assert "NOT_APPLICABLE" in r.json()["detail"]


def test_generate_refuses_a_second_local_job(monkeypatch):
    """if two local jobs can run at once, one Mac gets torch twice over

    Each local job spawns a torch process on the maintainer's machine. Two is not twice
    the throughput, it is one slow laptop, and this endpoint cannot see what
    else is running.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app
    from apps.webui.server.routes import stem_tiers

    class _LiveProc:
        def poll(self):
            return None  # still running

    monkeypatch.setattr(
        stem_tiers, "_JOBS",
        {"existing": {"where": "local", "proc": _LiveProc(), "tier": "LOCAL"}},
    )
    # A REAL id: the endpoint validates the track before it checks capacity,
    # which is the right order -- a 429 for a track that does not exist would
    # send the caller looking at the wrong problem.
    monkeypatch.setattr(
        stem_tiers, "_generate_command", lambda *a, **k: ["true"]
    )
    import apps.stems.cli as stems_cli

    monkeypatch.setattr(
        stems_cli, "resolve_audio_path", lambda *a, **k: __import__("pathlib").Path("/tmp/x.mp3")
    )
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/stems/generate", json={"stable_id": "whatever", "tier": "LOCAL"}
    )
    assert r.status_code == 429, r.text
    assert "already running" in r.json()["detail"]


def test_finished_jobs_are_reaped_but_running_ones_are_never_dropped(monkeypatch):
    """if reaping drops a live job, its subprocess is orphaned and unknowable"""
    from apps.webui.server.routes import stem_tiers

    class _Proc:
        def __init__(self, rc):
            self._rc = rc

        def poll(self):
            return self._rc

    table = {f"done{i}": {"where": "modal", "proc": _Proc(0)} for i in range(250)}
    table["live"] = {"where": "modal", "proc": _Proc(None)}
    monkeypatch.setattr(stem_tiers, "_JOBS", table)
    stem_tiers._reap_jobs()
    assert "live" in stem_tiers._JOBS, "a running job was dropped"
    assert len(stem_tiers._JOBS) <= stem_tiers._MAX_JOB_HISTORY + 1


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.requirement("STEM-49")
def test_a_local_job_writes_its_log_to_the_data_dir_never_the_source_tree(
    monkeypatch, tmp_path
):
    """[if] a LOCAL job runs [then] its log is under the data dir, never the source tree, [else stop].

    In the installed app the source tree is ``Open DJ.app/Contents/Resources/
    payload/app``; one LOCAL job wrote ``.tmp/stem-jobs/<id>.log`` there and
    ``codesign --verify --deep --strict`` then failed (packaged check of
    316572f5, Fri 2 Oct 2026, finding 2). Here the source tree is a read-only
    stand-in for that payload, and the job must leave it byte-for-byte as it
    was while its log lands under the data dir's ``logs/``.
    """
    pytest.importorskip("fastapi")
    import os
    import stat
    import sys
    import time

    from fastapi.testclient import TestClient

    import apps.shared.paths as shared_paths
    import apps.stems.cli as stems_cli
    from apps.webui.server.app import create_app
    from apps.webui.server.routes import stem_tiers

    payload_app = tmp_path / "Open DJ.app" / "Contents" / "Resources" / "payload" / "app"
    (payload_app / "scripts").mkdir(parents=True)
    (payload_app / "scripts" / "stem_bundle_worker.py").write_text("# shipped\n")
    data_dir = tmp_path / "Application Support" / "com.opendj.desktop"
    data_dir.mkdir(parents=True)
    audio = tmp_path / "music" / "track.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"ID3")
    before = _tree(tmp_path / "Open DJ.app")
    read_only = stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP
    payload_app.chmod(read_only)
    try:
        monkeypatch.setattr(stem_tiers, "_repo_root", lambda: payload_app)
        monkeypatch.setattr(stem_tiers, "_JOBS", {})
        monkeypatch.setattr(shared_paths, "DATA_DIR", data_dir)
        monkeypatch.setattr(stems_cli, "resolve_audio_path", lambda *a, **k: audio)
        # A real child process that writes to stdout, standing in for the
        # torch worker: the log file is what is under test, not separation.
        monkeypatch.setattr(
            stem_tiers,
            "_generate_command",
            lambda *a, **k: [sys.executable, "-c", "print('stem job ran')"],
        )
        client = TestClient(create_app(stem_roots=[data_dir / "state" / "stems"]))

        r = client.post(
            "/api/v1/stems/generate", json={"stable_id": "abc123", "tier": "LOCAL"}
        )

        assert r.status_code == 200, r.text
        log = Path(r.json()["log"])
        assert log.parent == data_dir / "logs" / "stem-jobs"
        deadline = time.monotonic() + 30
        status = client.get(r.json()["poll"]).json()
        while status["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = client.get(r.json()["poll"]).json()
        assert status["state"] == "done", status
        assert "stem job ran" in log.read_text()
        assert "stem job ran" in status["log_tail"]
    finally:
        payload_app.chmod(read_only | stat.S_IWUSR)
    assert _tree(tmp_path / "Open DJ.app") == before, "the job wrote into the source tree"
    assert not os.path.exists(payload_app / ".tmp")
