"""The ``stems.separate`` job kind: payload, argv, events, reconcile.

Unit-level. The end-to-end run through the real engine pipeline lives in
``test_stems_job_pipeline.py``; this file pins the parts that must hold before
a job is ever spawned, because every one of them is a guard against spending
money on the wrong thing.

Single-line intent:
  - if an empty or malformed payload is accepted then a job spawns with
    nothing to do, or with a traversal id as a directory name
  - if a non-Modal tier is accepted then this kind silently runs a rung it
    has no worker for
  - if duplicate ids are not collapsed then a track is separated twice and
    billed twice
  - if the argv stops overlaying modal then the worker dies on import, since
    modal is deliberately not a repo dependency
  - if a progress line without a track id still publishes library.changed
    then the browser refetches the whole library on every heartbeat
  - if reconcile answers 'succeeded' for a missing bundle then a lost job is
    marked done with no artifact behind it

-Claude
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.shared import events
from apps.stems.job import (
    JOB_KIND,
    LIBRARY_KIND,
    MODAL_TIER_KEYS,
    PROGRESS_TRACK_KEY,
    SCOPE_PENDING,
    StemsJobPayloadError,
    build_argv,
    on_progress,
    parse_payload,
    reconcile_from_disk,
)

SID_A = "a" * 40
SID_B = "b" * 40


class _RecordingHub:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


@pytest.fixture
def hub() -> Iterator[_RecordingHub]:
    recorder = _RecordingHub()
    events.set_hub(recorder)
    yield recorder
    events.set_hub(None)


# ----- payload ---------------------------------------------------------------


def test_ids_are_deduplicated_and_order_preserved() -> None:
    """if a repeated id is separated twice then the GPU is billed twice"""
    ids, tier, data_dir = parse_payload(
        {"stable_ids": [SID_B, SID_A, SID_B], "tier": "M"}
    )
    assert ids == [SID_B, SID_A]
    assert tier == "M"
    assert data_dir is None


def test_scope_defers_the_track_set_to_run_time() -> None:
    """if a scope job froze its ids at enqueue then a scan finishing in the
    gap would leave those tracks unseparated forever"""
    ids, _tier, _data_dir = parse_payload({"scope": SCOPE_PENDING})
    assert ids is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"stable_ids": []},
        {"stable_ids": "not-a-list"},
        {"stable_ids": [""]},
        {"stable_ids": [123]},
        {"stable_ids": ["../escape"]},
        {"stable_ids": ["a/b"]},
        {"stable_ids": [".."]},
        {"stable_ids": [SID_A], "tier": "LOCAL"},
        {"stable_ids": [SID_A], "tier": "nonsense"},
        {"stable_ids": [SID_A], "data_dir": "relative/path"},
        {"stable_ids": [SID_A], "data_dir": ""},
        {"scope": "everything"},
        {"scope": SCOPE_PENDING, "stable_ids": [SID_A]},
    ],
)
def test_bad_payloads_are_refused_before_anything_spawns(
    payload: dict[str, Any],
) -> None:
    """if any of these is accepted then a job spawns that should not have"""
    with pytest.raises(StemsJobPayloadError):
        parse_payload(payload)


def test_local_tier_is_refused_because_this_kind_is_the_modal_path() -> None:
    """if LOCAL were accepted then naming a free rung would start a paid one"""
    assert "LOCAL" not in MODAL_TIER_KEYS
    with pytest.raises(StemsJobPayloadError, match="not a Modal tier"):
        parse_payload({"stable_ids": [SID_A], "tier": "LOCAL"})


# ----- argv ------------------------------------------------------------------


def test_argv_overlays_modal_rather_than_using_the_repo_venv() -> None:
    """if the overlay is dropped then the worker dies at `import modal`,
    because modal is deliberately absent from the repo venv"""
    argv = build_argv({"stable_ids": [SID_A], "tier": "M"})
    assert argv[:5] == ["uv", "run", "--with", "modal", "python"]
    assert argv[5].endswith("stems_modal_worker.py")
    assert "--stable-id" in argv and SID_A in argv


def test_argv_passes_a_scope_instead_of_ids() -> None:
    """if a scope job passed ids then the run-time resolution is bypassed"""
    argv = build_argv({"scope": SCOPE_PENDING, "tier": "S"})
    assert argv[-2:] == ["--scope", SCOPE_PENDING]
    assert "--stable-id" not in argv
    assert argv[argv.index("--tier") + 1] == "S"


def test_argv_carries_an_explicit_data_dir(tmp_path: Path) -> None:
    """if the data dir is dropped then a test job writes into the real one"""
    argv = build_argv({"stable_ids": [SID_A], "data_dir": str(tmp_path)})
    assert argv[argv.index("--data-dir") + 1] == str(tmp_path)


# ----- the progress observer -------------------------------------------------


def test_a_completed_track_publishes_one_library_event(hub: _RecordingHub) -> None:
    """if this stops firing then a finished track shows no stems until the
    browser happens to refetch for some other reason"""
    on_progress(
        {"kind": JOB_KIND, "id": "job-1"},
        {"progress": 0.25, "message": "1/4", PROGRESS_TRACK_KEY: SID_A},
    )
    assert hub.published == [
        ("library.changed", {"kind": LIBRARY_KIND, "ids": [SID_A]})
    ]


@pytest.mark.parametrize(
    "line",
    [
        {"progress": 0.5, "message": "still going"},
        {"progress": 0.5, PROGRESS_TRACK_KEY: ""},
        {"progress": 0.5, PROGRESS_TRACK_KEY: 42},
        {"progress": 0.5, PROGRESS_TRACK_KEY: None},
    ],
)
def test_a_line_naming_no_track_publishes_nothing(
    hub: _RecordingHub, line: dict[str, Any]
) -> None:
    """if a bare heartbeat published then every line would refetch the library"""
    on_progress({"kind": JOB_KIND, "id": "job-1"}, line)
    assert hub.published == []


def test_the_event_kind_is_one_the_browser_already_consumes() -> None:
    """if this drifts to a kind events-bus.ts does not know then the frame is
    logged as contract drift and dropped, and nothing refreshes"""
    bus = (
        Path(__file__).resolve().parents[2]
        / "apps/webui/frontend/src/lib/api/events-bus.ts"
    ).read_text(encoding="utf-8")
    assert f"'{LIBRARY_KIND}'" in bus


# ----- reconcile -------------------------------------------------------------


def _write_bundle(root: Path, stable_id: str) -> None:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps({"stable_id": stable_id}))


def test_reconcile_reads_the_artifacts_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """if a missing bundle reconciled as succeeded then a lost job would be
    marked done with nothing behind it"""
    root = tmp_path / "state" / "stems"
    root.mkdir(parents=True)
    job = {
        "kind": JOB_KIND,
        "payload": {"stable_ids": [SID_A, SID_B], "data_dir": str(tmp_path)},
    }
    assert reconcile_from_disk(job) == "failed"

    _write_bundle(root, SID_A)
    assert reconcile_from_disk(job) == "failed"

    _write_bundle(root, SID_B)
    assert reconcile_from_disk(job) == "succeeded"


def test_a_scope_job_stays_unknown_rather_than_claiming_success(
    tmp_path: Path,
) -> None:
    """if a scope job reconciled as succeeded then 'the scope was empty' and
    'every track finished' would be the same answer"""
    job = {
        "kind": JOB_KIND,
        "payload": {"scope": SCOPE_PENDING, "data_dir": str(tmp_path)},
    }
    assert reconcile_from_disk(job) == "unknown"


def test_an_unparseable_payload_reconciles_to_unknown() -> None:
    """if a malformed row reconciled either way then re-enqueue would act on
    a verdict nothing supports"""
    assert reconcile_from_disk({"kind": JOB_KIND, "payload": {}}) == "unknown"
