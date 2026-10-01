"""The refresh job and the drain agree on which stems are left to make (HEALTH-13).

Regression lines:
  - if a refresh targets a bundle that R2 holds and this machine can fetch then broken
  - if a refresh skips a bundle that is in neither place then broken
  - if a refresh treats anything as in cloud when the index is off or unreadable then broken
  - if the stems CLI is free to pick tracks the job did not target then broken
  - if a refresh runs the stems CLI when every missing bundle is in the cloud then broken

[if] a library refresh targets a stem bundle the cloud index holds and this machine can fetch [then] fail, [else stop].

Found on the preview library (Thu 1 Oct 2026): coverage read 744 stem bundles
missing locally, 735 of them in R2 and 0 pending, so the drain had nothing to
make while a library refresh would have re-rendered five evicted bundles per
run and logged "744 missing".
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from apps.stems import cli as stems_cli
from apps.webui.server import coverage_cloud
from apps.webui.server.routes import ingest as ingest_mod
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("HEALTH-13")


@pytest.fixture
def s3() -> InMemoryAssetS3:
    return InMemoryAssetS3()


@pytest.fixture
def cfg():
    from apps.cloud.config import CloudConfig

    return CloudConfig(
        r2_account_id="acct1234", r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key", state_bucket="test-state",
        audio_bucket="test-audio", hostname="test-host", bind_host="127.0.0.1",
    )


def _present(library: Library, stable_id: str, *, stems: bool = False) -> None:
    audio = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))
    if stems:
        fx.write_stem_bundle(library.stems, stable_id)


def _three_tracks(library: Library, s3, cfg, tmp_path: Path) -> dict[str, dict[str, str]]:
    """local: bundle on disk. cloud: evicted, in the index. neither: nowhere."""
    _present(library, "local", stems=True)
    _present(library, "cloud")
    _present(library, "neither")
    return {"cloud": fx.put_in_cloud(s3, cfg, tmp_path / "scratch", "cloud")}


def _job(library: Library, steps: list[str] | None = None) -> ingest_mod._RefreshJob:
    return ingest_mod._RefreshJob(
        0.0, steps or [], stem_cloud=ingest_mod.stem_cloud_for(library.app)
    )


def _refresh_stem_ids(library: Library) -> list[str]:
    targets = ingest_mod._library_targets(_job(library), ingest_mod._stem_roots(library.app))
    return [stable_id for stable_id, _path in targets["stems"]]


def _drain_stem_ids(library: Library) -> list[str]:
    snapshot = ingest_mod.build_snapshot(library.app)
    return [stable_id for stable_id, _path in snapshot.pending["stems"]]


def test_refresh_leaves_a_bundle_in_the_cloud_alone_and_agrees_with_the_drain(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, _three_tracks(library, s3, cfg, tmp_path))

    # Overshoot control: the track in NEITHER place is still a target. A fix
    # that dropped every missing bundle would read [] here.
    assert _refresh_stem_ids(library) == ["neither"]
    assert _refresh_stem_ids(library) == _drain_stem_ids(library)


def test_with_no_cloud_on_this_machine_every_missing_bundle_is_a_target(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    from apps.cloud import stem_index

    index = _three_tracks(library, s3, cfg, tmp_path)
    stem_index.save_cached_index(library.data_dir, index)   # present, but no source armed

    assert sorted(_refresh_stem_ids(library)) == ["cloud", "neither"]
    assert sorted(_refresh_stem_ids(library)) == sorted(_drain_stem_ids(library))


def test_an_unreadable_index_proves_nothing_is_in_the_cloud(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    from apps.cloud import stem_index

    fx.arm_cloud(library.app, library.data_dir, s3, cfg, _three_tracks(library, s3, cfg, tmp_path))
    stem_index.local_index_cache_path(library.data_dir).write_text("{ not json", encoding="utf-8")

    assert ingest_mod.stem_cloud_for(library.app).state == "unknown"
    assert sorted(_refresh_stem_ids(library)) == ["cloud", "neither"]
    assert sorted(_refresh_stem_ids(library)) == sorted(_drain_stem_ids(library))


def test_the_job_log_says_how_many_missing_bundles_are_in_the_cloud(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, _three_tracks(library, s3, cfg, tmp_path))
    job = _job(library)

    ingest_mod._library_targets(job, ingest_mod._stem_roots(library.app))

    assert any(
        "stems: 1 of 2 with no local bundle are in the cloud" in line for line in job.log
    ), list(job.log)


def test_the_stems_cli_is_handed_exactly_the_jobs_targets(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run_cli(_job: ingest_mod._RefreshJob, argv: list[str]) -> None:
        ids_file = Path(argv[argv.index("--ids-file") + 1])
        seen["argv"] = argv
        seen["path"] = ids_file
        seen["ids"] = ids_file.read_text(encoding="utf-8").split()

    monkeypatch.setattr(ingest_mod, "_run_cli", fake_run_cli)
    job = _job(library, ["stems"])

    ingest_mod._step_stems(job, [("neither", "/music/neither.mp3"), ("other", "/music/other.mp3")])

    assert seen["ids"] == ["neither", "other"]
    assert "trickle" in seen["argv"] and "--live" in seen["argv"]
    assert not seen["path"].exists(), "the id list is a scratch file and must not be left behind"


def _wait_refresh(library: Library) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = library.client.get("/api/v1/ingest/refresh/status").json()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("the refresh job did not finish within 30 s")


def test_a_refresh_over_the_route_renders_nothing_when_the_rest_is_in_the_cloud(
    library: Library, s3, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _present(library, "local", stems=True)
    _present(library, "cloud")
    index = {"cloud": fx.put_in_cloud(s3, cfg, tmp_path / "scratch", "cloud")}
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, index)
    ran: list[list[str]] = []
    monkeypatch.setattr(ingest_mod, "_run_cli", lambda _job, argv: ran.append(argv))
    library.client.put(
        "/api/v1/ingest/config",
        json={"enabled": {"analysis": False, "stems": True, "vocals": False}},
    )

    response = library.client.post("/api/v1/ingest/refresh", json={"scope": "library"})
    assert response.status_code == 202, response.text
    status = _wait_refresh(library)

    assert status["phase"] == "done", status["log_tail"]
    assert ran == [], "a bundle R2 holds was handed to the stems renderer"
    assert any("stems: 0 missing" in line for line in status["log_tail"]), status["log_tail"]


def test_a_refresh_over_the_route_still_renders_what_is_in_neither_place(
    library: Library, s3, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: same route, one bundle genuinely
    missing, and the renderer IS run, with that track and only that track."""
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, _three_tracks(library, s3, cfg, tmp_path))
    handed: list[list[str]] = []

    def fake_run_cli(_job: ingest_mod._RefreshJob, argv: list[str]) -> None:
        handed.append(Path(argv[argv.index("--ids-file") + 1]).read_text("utf-8").split())

    monkeypatch.setattr(ingest_mod, "_run_cli", fake_run_cli)
    library.client.put(
        "/api/v1/ingest/config",
        json={"enabled": {"analysis": False, "stems": True, "vocals": False}},
    )

    assert library.client.post("/api/v1/ingest/refresh", json={"scope": "library"}).status_code == 202
    status = _wait_refresh(library)

    assert status["phase"] == "done", status["log_tail"]
    assert handed == [["neither"]]


# ----- the stems CLI honors the list ----------------------------------------
def test_trickle_accepts_an_ids_file() -> None:
    args = stems_cli.build_parser().parse_args(
        ["trickle", "--live", "--ids-file", "/tmp/ids.txt"]
    )
    assert args.ids_file == Path("/tmp/ids.txt")
    assert stems_cli.build_parser().parse_args(["trickle", "--live"]).ids_file is None


def test_read_ids_file_returns_the_listed_ids(tmp_path: Path) -> None:
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("b\na\n\n", encoding="utf-8")
    assert stems_cli.read_ids_file(ids_file) == {"a", "b"}


def test_read_ids_file_fails_loudly_on_a_missing_or_empty_list(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot read"):
        stems_cli.read_ids_file(tmp_path / "absent.txt")
    empty = tmp_path / "empty.txt"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="lists no stable ids"):
        stems_cli.read_ids_file(empty)


class _Track:
    def __init__(self, stable_id: str) -> None:
        self.stable_id = stable_id


def test_restricting_the_batch_keeps_only_listed_tracks_in_their_order() -> None:
    todo = [_Track("c"), _Track("a"), _Track("b")]
    kept = stems_cli.restrict_to_ids(todo, {"b", "c", "not-in-todo"})
    assert [track.stable_id for track in kept] == ["c", "b"]
    assert stems_cli.restrict_to_ids(todo, None) == todo


def test_stem_cloud_default_on_a_job_is_off_not_ok() -> None:
    """A job built without a cloud view must not count anything as in cloud."""
    assert ingest_mod._RefreshJob(0.0, []).stem_cloud is coverage_cloud.OFF
