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
    canonical_payload,
    on_progress,
    parse_payload,
    reconcile_from_disk,
)

SID_A = "a" * 40
SID_B = "b" * 40


@pytest.fixture(autouse=True)
def _default_modal_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most unit tests assume the Modal farm path unless they override."""
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "modal",
    )


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
    ids, tier, data_dir, _executor = parse_payload(
        {"stable_ids": [SID_B, SID_A, SID_B], "tier": "M"}
    )
    assert ids == [SID_B, SID_A]
    assert tier == "M"
    assert data_dir is None


def test_scope_defers_the_track_set_to_run_time() -> None:
    """if a scope job froze its ids at enqueue then a scan finishing in the
    gap would leave those tracks unseparated forever"""
    ids, _tier, _data_dir, _executor = parse_payload({"scope": SCOPE_PENDING})
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


def test_local_tier_is_refused_when_modal_executor_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if LOCAL were accepted on the Modal path then a free rung would start a paid one"""
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "modal",
    )
    assert "LOCAL" not in MODAL_TIER_KEYS
    with pytest.raises(StemsJobPayloadError, match="not a Modal tier"):
        parse_payload({"stable_ids": [SID_A], "tier": "LOCAL"})


def test_local_payload_accepted_when_local_executor_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "local",
    )
    ids, tier, _data_dir, executor = parse_payload(
        {"stable_ids": [SID_A], "tier": "M"}
    )
    assert ids == [SID_A]
    assert tier == "LOCAL"
    assert executor == "local"


def test_canonical_payload_stores_executor_and_local_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "local",
    )
    out = canonical_payload({"stable_ids": [SID_A, SID_A], "tier": "M"})
    assert out["tier"] == "LOCAL"
    assert out["executor"] == "local"
    assert out["stable_ids"] == [SID_A]


def test_local_argv_uses_local_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.stems.routing.resolve_stems_executor",
        lambda **_: "local",
    )
    monkeypatch.setattr("apps.stems.local_gate.local_stems_gate", lambda: None)
    argv = build_argv({"stable_ids": [SID_A], "tier": "M"})
    joined = " ".join(argv)
    assert "stems_local_worker.py" in joined
    assert "--with" not in argv


# ----- argv ------------------------------------------------------------------


def test_relay_argv_needs_no_modal_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """if the relay argv asked for modal then the installed app, which ships
    no modal and no uv, could never separate through the relay (#3421)"""
    monkeypatch.delenv("MDT_STEMS_TRANSPORT", raising=False)
    argv = build_argv({"stable_ids": [SID_A], "tier": "M"})
    assert argv[:4] == ["uv", "run", "--no-sync", "python"]
    assert "--with" not in argv
    assert argv[4].endswith("stems_modal_worker.py")
    assert Path(argv[4]).is_absolute()
    assert argv[argv.index("--transport") + 1] == "relay"
    assert "--stable-id" in argv and SID_A in argv


def test_direct_argv_overlays_modal_rather_than_using_the_repo_venv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if the overlay is dropped then a direct run dies at `import modal`,
    because modal is deliberately absent from the repo venv"""
    monkeypatch.setenv("MDT_STEMS_TRANSPORT", "direct")
    argv = build_argv({"stable_ids": [SID_A], "tier": "M"})
    assert argv[:6] == ["uv", "run", "--no-sync", "--with", "modal", "python"]
    assert argv[6].endswith("stems_modal_worker.py")


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


# ----- the local worker script has to BE somewhere -------------------------
# Measured on the shipped build on the test Mac Wed 16 Sep 2026: the wizard
# enqueued separation, the job died 198 ms later, and the wizard had already
# advanced so nobody saw it. The error was
#   can't open file '//scripts/stems_local_worker.py': No such file or directory
# because the argv named a RELATIVE path, the engine's cwd in an installed app
# is /, and the payload ships no scripts/ directory at all. The gate that is
# supposed to make the button inert never looked at whether the worker exists.
@pytest.mark.requirement("STEM-36")
def test_the_local_worker_argv_names_an_absolute_path() -> None:
    """[if] the local worker argv is a relative path [then] fail, [else stop].

    A relative path is a bet on the cwd of whoever spawns the job.
    """
    from apps.stems import job as stems_job

    assert Path(stems_job.local_worker_script()).is_absolute()


@pytest.mark.requirement("STEM-36")
def test_the_gate_refuses_when_the_local_worker_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the gate stays open with no worker script on disk [then] fail, [else stop]."""
    from apps.stems import job as stems_job

    monkeypatch.setattr("apps.shared.platform_paths.PROJECT_ROOT", tmp_path)
    refusal = stems_job.local_worker_refusal()
    assert refusal is not None
    assert "stems_local_worker.py" in refusal


@pytest.mark.requirement("STEM-36")
def test_the_gate_allows_it_when_the_local_worker_IS_installed() -> None:
    """[if] the gate refuses in a checkout that ships the worker [then] fail, [else stop].

    The opposite mutation. A refusal that fires in a real checkout would
    turn local stems off for every developer, which no bug report would
    mention because the feature would simply be gone."""
    from apps.stems import job as stems_job

    assert stems_job.local_worker_refusal() is None


@pytest.mark.requirement("STEM-36")
def test_build_argv_refuses_rather_than_naming_a_script_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] build_argv names a worker script that is absent [then] fail, [else stop].

    The last line of defence: even if the gate is bypassed, the job must
    not be queued to die in a subprocess nobody is watching."""
    from apps.stems import job as stems_job

    # Routed exactly as test_local_argv_uses_local_worker routes it. Without
    # the executor patch this payload resolves to the MODAL branch and "did
    # not raise" would be a true statement about the wrong code path.
    monkeypatch.setattr("apps.stems.routing.resolve_stems_executor", lambda **_: "local")
    monkeypatch.setattr("apps.stems.local_gate.local_stems_gate", lambda: None)
    monkeypatch.setattr("apps.shared.platform_paths.PROJECT_ROOT", tmp_path)
    with pytest.raises(stems_job.StemsJobPayloadError, match=r"stems_local_worker\.py"):
        stems_job.build_argv({"stable_ids": [SID_A], "tier": "M"})


@pytest.mark.requirement("STEM-36")
def test_the_plan_gate_reports_a_missing_worker_as_its_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the plan gate omits a missing worker from its refusal [then] fail, [else stop].

    GET /stems/plan reads local_stems_gate() for `local_refusal`, and the
    wizard's StemsPrompt renders that inert with the sentence as its reason.
    So the worker check has to be IN the gate, not only in build_argv, or the
    button stays live and the tester presses it into a job that cannot run."""
    from apps.stems import local_gate

    class _FlagOn:
        def enabled(self, _flag: str) -> bool:
            return True

    monkeypatch.setattr(local_gate, "resolve_stems_executor", lambda: "local")
    monkeypatch.setattr(local_gate, "local_stems_tier_refusal", lambda: None)
    monkeypatch.setattr("apps.shared.platform_paths.PROJECT_ROOT", tmp_path)
    refusal = local_gate.local_stems_gate(flag_store=_FlagOn())
    assert refusal is not None
    assert "not installed in this build" in refusal
