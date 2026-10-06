"""Hermetic tests for scripts/sink_triage.py (issue #2673).

Regression lines:
  - if two messages differ only by path or integer then they share a fingerprint
  - if window count is below 100 and three-run sum is below 10 then no issue
  - if window count is 100+ then threshold fires
  - if dry-run fixture has one new flood and one existing issue then one create
    and one comment command print
  - if one ssh host is unreachable then the other sources still yield records,
    the skip is reported with host and path, and the skipped offsets are unchanged
  - if every source is unreachable then the run raises and writes no kpi file
  - if a source was reached and every other source failed then the run finishes
    (skips alone never mean a total outage) and each dead host is logged
  (the kpi.sh sink-triage card tests moved to fleet-af tests/kpi_rails/ with kpi.sh)

[if] sink fingerprints and thresholds fire [then] triage creates or comments on issues, [else stop].
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.sink_triage import (
    AllSourcesUnreachable,
    FingerprintStats,
    HostSource,
    SinkRecord,
    TriageState,
    aggregate,
    collect_host_records,
    main,
    marker_for,
    parse_record,
    read_delta_bytes,
    run_triage,
    threshold_met,
    triage_fingerprint,
)

pytestmark = pytest.mark.requirement("OPS-34")


def _record(
    site: str,
    message: str,
    host: str = "silver",
    *,
    kind: str | None = None,
) -> SinkRecord:
    raw: dict[str, str] = {
        "source_site": site,
        "message": message,
        "host": host,
        "build_sha": "abc123",
    }
    if kind is not None:
        raw["kind"] = kind
    return SinkRecord(
        host=host,
        source_site=site,
        message=message,
        build_sha="abc123",
        error_id="eid-test",
        raw=raw,
    )


_CI_FAILURE_MESSAGE = (
    "CI failed workflow=CI run=34705584177 conclusion=failure "
    "url=https://github.com/private_owner/music-dj-tools/actions/runs/34705584177 "
    "sha=98159af7b5fb6d8ef7fd255a43a5d4386a4530b8"
)


def _build_e2e_message(run_id: int = 34705843143) -> str:
    return (
        f"CI failed workflow=E2E run={run_id} conclusion=failure "
        f"url=https://github.com/private_owner/music-dj-tools/actions/runs/{run_id} "
        f"sha=921b0489fa060e5945b8302285b2a2a872b4ab40"
    )


def _build_e2e_record(host: str = "nucbox-wsl-8", run_id: int = 34705843143) -> SinkRecord:
    message = _build_e2e_message(run_id)
    return SinkRecord(
        host=host,
        source_site="build:E2E",
        message=message,
        build_sha="921b0489fa060e5945b8302285b2a2a872b4ab40",
        error_id="eid-21f55ba9a63a",
        raw={
            "build_sha": "921b0489fa060e5945b8302285b2a2a872b4ab40",
            "error_id": "eid-21f55ba9a63a",
            "host": host,
            "kind": "build",
            "message": message,
            "source_site": "build:E2E",
        },
    )


def test_parse_record_skips_kind_build() -> None:
    line = json.dumps(
        {
            "kind": "build",
            "source_site": "build:E2E",
            "message": _build_e2e_message(),
            "host": "nucbox-wsl-8",
        }
    )
    assert parse_record(line, "nucbox") is None


def test_parse_record_skips_build_source_site_without_kind() -> None:
    line = json.dumps(
        {
            "source_site": "build:E2E",
            "message": _build_e2e_message(),
            "host": "nucbox-wsl-8",
        }
    )
    assert parse_record(line, "nucbox") is None


def test_build_e2e_fingerprint_anchor() -> None:
    assert triage_fingerprint("build:E2E", _build_e2e_message()) == "878d8ed91bfc"


def _collect_from_jsonl(
    tmp_path: Path, lines: list[dict[str, object]], name: str = "nucbox"
) -> list[SinkRecord]:
    sink = tmp_path / f"{name}-sink.jsonl"
    sink.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    state = TriageState(offsets={}, run_history={}, last_comment={})
    source = HostSource(name=name, mode="local", sink_path=str(sink))
    return collect_host_records(source, state)[0]


def _gh_no_open_issues(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, "", "")


def test_build_e2e_flood_does_not_file_issues(tmp_path: Path) -> None:
    lines = [
        {
            "build_sha": "921b0489fa060e5945b8302285b2a2a872b4ab40",
            "error_id": "eid-21f55ba9a63a",
            "host": f"nucbox-wsl-{i % 30}",
            "kind": "build",
            "message": _build_e2e_message(34705843143 + i),
            "source_site": "build:E2E",
        }
        for i in range(225)
    ]
    records = _collect_from_jsonl(tmp_path, lines)
    assert records == []
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="private_owner/music-dj-tools",
        dry_run=True,
        records_in=records,
        gh_run=_gh_no_open_issues,
    )
    assert result.new_issues == 0
    assert result.comments == 0
    assert result.commands == []


def test_engine_flood_still_files_when_build_rows_present(tmp_path: Path) -> None:
    engine_lines = [
        {
            "source_site": "engine:flood",
            "message": "disk write failed on path /tmp/x",
            "host": "silver",
            "build_sha": "abc123",
        }
    ] * 100
    build_lines = [
        {
            "build_sha": "921b0489fa060e5945b8302285b2a2a872b4ab40",
            "error_id": "eid-21f55ba9a63a",
            "host": f"nucbox-wsl-{i % 30}",
            "kind": "build",
            "message": _build_e2e_message(34705843143 + i),
            "source_site": "build:E2E",
        }
        for i in range(225)
    ]
    records = _collect_from_jsonl(tmp_path, engine_lines + build_lines)
    assert len(records) == 100
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="private_owner/music-dj-tools",
        dry_run=True,
        records_in=records,
        gh_run=_gh_no_open_issues,
        sb_run=lambda _args: "- [ ] stop the flood\n",
    )
    assert result.new_issues == 1
    assert result.comments == 0
    assert len(result.commands) == 1
    assert "issue create" in result.commands[0]


def test_paths_and_numbers_share_a_fingerprint() -> None:
    a = triage_fingerprint("engine:runner:run", "open failed /home/foo/a/17")
    b = triage_fingerprint("engine:runner:run", "open failed /var/tmp/b/99")
    assert a == b


def test_different_source_sites_differ() -> None:
    message = "job 17 failed"
    a = triage_fingerprint("engine:a", message)
    b = triage_fingerprint("engine:b", message)
    assert a != b


def test_threshold_window_count() -> None:
    stats = FingerprintStats(fingerprint="fp1", count=100)
    assert threshold_met(stats, []) is True
    stats.count = 1
    assert threshold_met(stats, [1, 1]) is False


def test_threshold_three_run_sum() -> None:
    stats = FingerprintStats(fingerprint="fp1", count=4)
    assert threshold_met(stats, [3, 3]) is True
    stats.count = 2
    assert threshold_met(stats, [3, 3]) is False


def test_dry_run_fixture_emits_one_create_and_one_comment(tmp_path: Path) -> None:
    fp_new = triage_fingerprint("engine:flood", "disk write failed on path /tmp/x")
    fp_low = triage_fingerprint("engine:quiet", "one-off")
    fp_existing = triage_fingerprint("engine:known", "repeat failure")
    records = [_record("engine:flood", "disk write failed on path /tmp/x")] * 100
    records.append(_record("engine:quiet", "one-off"))
    records.extend([_record("engine:known", "repeat failure")] * 100)

    def gh_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if args[:2] == ["issue", "list"] and marker_for(fp_existing) in joined:
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="private_owner/music-dj-tools",
        dry_run=True,
        records_in=records,
        gh_run=gh_run,
        sb_run=lambda _args: "- [ ] stop the flood\n",
    )
    assert result.new_issues == 1
    assert result.comments == 1
    assert len(result.commands) == 2
    assert any("issue create" in cmd for cmd in result.commands)
    assert any("issue comment 4242" in cmd for cmd in result.commands)
    assert fp_low not in " ".join(result.commands)


def test_build_kind_records_are_excluded_from_triage(tmp_path: Path) -> None:
    assert triage_fingerprint("build:CI", _CI_FAILURE_MESSAGE) == "9e91f071611f"
    records = [
        _record("build:CI", _CI_FAILURE_MESSAGE, host=f"nucbox-wsl-{i}", kind="build")
        for i in range(100)
    ]
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="private_owner/music-dj-tools",
        dry_run=True,
        records_in=records,
    )
    assert result.new_issues == 0
    assert result.comments == 0
    assert result.fingerprints_seen == 0


def test_build_source_site_without_kind_is_excluded(tmp_path: Path) -> None:
    records = [_record("build:CI", _CI_FAILURE_MESSAGE, host=f"nucbox-wsl-{i}") for i in range(100)]
    result = run_triage(
        sources=[],
        state_path=tmp_path / "state.json",
        kpi_path=tmp_path / "kpi.json",
        repo="private_owner/music-dj-tools",
        dry_run=True,
        records_in=records,
    )
    assert result.new_issues == 0
    assert result.comments == 0
    assert result.fingerprints_seen == 0


def test_aggregate_counts_hosts_and_examples() -> None:
    records = [
        _record("engine:a", "boom 1", "silver"),
        _record("engine:a", "boom 2", "air"),
    ]
    grouped = aggregate(records)
    assert len(grouped) == 1
    stats = next(iter(grouped.values()))
    assert stats.count == 2
    assert stats.hosts == {"silver", "air"}
    assert len(stats.examples) == 2


# ----- per-host isolation (issue #3431)


_SSH_SINK = "~/Library/Logs/opendj/error-sink.jsonl"
_SSH_TRUNCATIONS = "~/.cache/opendj-host-disk-mac/truncations.jsonl"

_FLOOD_LINE = (
    json.dumps({"source_site": "engine:flood", "message": "disk write failed", "host": "nucbox"})
    + "\n"
)


def _unreachable_ssh(
    host: str, remote_path: str, offset: int, tail_bytes: int
) -> tuple[bytes, int]:
    """The real air-host failure of 2026-09-17T05:00:10Z: ssh itself exits 255.

    The remote command carries its own ``|| echo 0`` fallback, which only runs
    once ssh has connected, so an unreachable host raises instead of returning 0.
    """
    raise subprocess.CalledProcessError(
        255,
        ["ssh", "-o", "BatchMode=yes", host, f"wc -c < '{remote_path}' 2>/dev/null || echo 0"],
    )


def _boom_local(path: Path, offset: int, tail_bytes: int) -> tuple[bytes, int]:
    raise OSError(f"cannot read local sink {path}")


@pytest.mark.requirement("OPS-40")
def test_unreachable_ssh_source_is_skipped_and_offsets_unchanged(tmp_path: Path) -> None:
    source = HostSource(
        name="air", mode="ssh", sink_path=_SSH_SINK, truncations_path=_SSH_TRUNCATIONS
    )
    stored = {f"air:{_SSH_SINK}": 4096, f"air:{_SSH_TRUNCATIONS}": 512}
    state = TriageState(offsets=dict(stored), run_history={}, last_comment={})

    records, skip, reached = collect_host_records(source, state, fetch_ssh=_unreachable_ssh)

    assert records == []
    assert reached is False
    assert skip is not None
    assert skip.source == "air"
    assert skip.mode == "ssh"
    assert skip.path == _SSH_SINK
    assert "255" in skip.error
    # Nothing was consumed, so the next run must resume from where this one left off.
    assert state.offsets == stored


def test_reachable_source_still_yields_records_beside_an_unreachable_one(tmp_path: Path) -> None:
    sink = tmp_path / "nucbox-sink.jsonl"
    sink.write_text(_FLOOD_LINE, encoding="utf-8")
    state = TriageState(offsets={}, run_history={}, last_comment={})
    good = HostSource(name="nucbox", mode="local", sink_path=str(sink))
    bad = HostSource(name="air", mode="ssh", sink_path=_SSH_SINK)

    good_records, good_skip, good_reached = collect_host_records(good, state)
    bad_records, bad_skip, bad_reached = collect_host_records(
        bad, state, fetch_ssh=_unreachable_ssh
    )

    assert len(good_records) == 1
    assert good_skip is None
    assert good_reached is True
    assert bad_records == []
    assert bad_reached is False
    assert bad_skip is not None and bad_skip.source == "air"
    # The healthy host's offset advanced; the dead host's key was never created.
    assert state.offsets[f"nucbox:{sink}"] == sink.stat().st_size
    assert not [key for key in state.offsets if key.startswith("air:")]


@pytest.mark.requirement("OPS-40")
def test_run_triage_triages_reachable_sources_and_records_the_skip_in_kpi(
    tmp_path: Path,
) -> None:
    sink = tmp_path / "logs" / "opendj-error-sink.jsonl"
    sink.parent.mkdir(parents=True)
    sink.write_text(_FLOOD_LINE, encoding="utf-8")
    kpi_path = tmp_path / "state" / "sink-triage-kpi.json"
    sources = [
        HostSource(name="nucbox", mode="local", sink_path=str(sink)),
        HostSource(name="air", mode="ssh", sink_path=_SSH_SINK),
    ]

    result = run_triage(
        sources=sources,
        state_path=tmp_path / "state" / "sink-triage.json",
        kpi_path=kpi_path,
        repo="private_owner/music-dj-tools",
        dry_run=False,
        fetch_ssh=_unreachable_ssh,
    )

    assert result.fingerprints_seen == 1
    assert [skip.source for skip in result.skips] == ["air"]

    kpi = json.loads(kpi_path.read_text(encoding="utf-8"))
    assert kpi["fingerprints"] == 1
    assert len(kpi["skipped_sources"]) == 1
    assert kpi["skipped_sources"][0]["source"] == "air"
    assert kpi["skipped_sources"][0]["path"] == _SSH_SINK
    assert "255" in kpi["skipped_sources"][0]["error"]


@pytest.mark.requirement("OPS-40")
def test_run_triage_raises_and_writes_no_kpi_when_every_source_is_unreachable(
    tmp_path: Path,
) -> None:
    kpi_path = tmp_path / "state" / "sink-triage-kpi.json"
    sources = [
        HostSource(name="silver", mode="ssh", sink_path=_SSH_SINK),
        HostSource(name="air", mode="ssh", sink_path=_SSH_SINK),
    ]

    with pytest.raises(AllSourcesUnreachable) as excinfo:
        run_triage(
            sources=sources,
            state_path=tmp_path / "state" / "sink-triage.json",
            kpi_path=kpi_path,
            repo="private_owner/music-dj-tools",
            dry_run=False,
            fetch_ssh=_unreachable_ssh,
        )

    message = str(excinfo.value)
    assert "silver" in message
    assert "air" in message
    assert _SSH_SINK in message
    assert "255" in message
    # A run that triaged nothing must not advance last_run, or the staleness
    # health line reads fresh on a lane that reached no host at all.
    assert not kpi_path.exists()


def _boom_local_after_first_path(boom_path: Path) -> Callable[[Path, int, int], tuple[bytes, int]]:
    """A local reader that serves every path except ``boom_path``."""

    def _fetch(path: Path, offset: int, tail_bytes: int) -> tuple[bytes, int]:
        if path == boom_path:
            raise OSError(f"cannot read local sink {path}")
        return read_delta_bytes(path, offset, tail_bytes)

    return _fetch


def test_run_triage_continues_when_one_source_was_reached_and_all_others_failed(
    tmp_path: Path,
) -> None:
    """As many skips as sources is not proof that no host was reached.

    nucbox's sink reads fine, its truncations path then fails, and both Macs are
    offline: three skips over three sources, but the run did reach a host, so it
    must finish and keep the nucbox rows instead of discarding them (issue #3431).
    """
    sink = tmp_path / "opendj-error-sink.jsonl"
    sink.write_text(_FLOOD_LINE, encoding="utf-8")
    truncations = tmp_path / "truncations.jsonl"
    dead_mac = {"sink_path": _SSH_SINK, "truncations_path": _SSH_TRUNCATIONS}
    sources = [
        HostSource(
            name="nucbox",
            mode="local",
            sink_path=str(sink),
            truncations_path=str(truncations),
        ),
        HostSource(name="silver", mode="ssh", **dead_mac),
        HostSource(name="air", mode="ssh", **dead_mac),
    ]
    kpi_path = tmp_path / "state" / "sink-triage-kpi.json"

    result = run_triage(
        sources=sources,
        state_path=tmp_path / "state" / "sink-triage.json",
        kpi_path=kpi_path,
        repo="private_owner/music-dj-tools",
        dry_run=False,
        fetch_local=_boom_local_after_first_path(truncations),
        fetch_ssh=_unreachable_ssh,
    )

    assert result.fingerprints_seen == 1
    assert sorted(skip.source for skip in result.skips) == ["air", "nucbox", "silver"]
    kpi = json.loads(kpi_path.read_text(encoding="utf-8"))
    assert kpi["fingerprints"] == 1
    assert len(kpi["skipped_sources"]) == 3
    assert kpi["skipped_sources"][0]["path"] == str(truncations)


def test_main_logs_skipped_hosts_and_exits_zero_on_a_partial_run(tmp_path: Path) -> None:
    sink = tmp_path / "logs" / "opendj-error-sink.jsonl"
    sink.parent.mkdir(parents=True)
    sink.write_text(_FLOOD_LINE, encoding="utf-8")

    rc = main(["--jobs-dir", str(tmp_path)], fetch_ssh=_unreachable_ssh)

    assert rc == 0
    log = (tmp_path / "logs" / "sink-triage.log").read_text(encoding="utf-8")
    assert "sink-triage SKIPPED host=air" in log
    assert "sink-triage SKIPPED host=silver" in log
    assert _SSH_SINK in log
    assert "255" in log
    assert "skipped=2/3" in log


@pytest.mark.requirement("OPS-40")
def test_main_exits_nonzero_and_names_every_host_when_all_sources_fail(tmp_path: Path) -> None:
    rc = main(["--jobs-dir", str(tmp_path)], fetch_local=_boom_local, fetch_ssh=_unreachable_ssh)

    assert rc == 1
    log = (tmp_path / "logs" / "sink-triage.log").read_text(encoding="utf-8")
    # Each dead host is named on its own line BEFORE the aggregate failure: the
    # abort must not swallow the evidence of what could not be reached.
    for host in ("nucbox", "silver", "air"):
        assert f"sink-triage SKIPPED host={host}" in log
    assert "255" in log
    assert log.index("sink-triage ERROR") > log.rindex("sink-triage SKIPPED host=")
    assert not (tmp_path / "state" / "sink-triage-kpi.json").exists()
