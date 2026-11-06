"""The root Playwright footprint map: built from a run's two logs, published, fetched
(DEVOPS-28, scripts/e2e_footprints_refresh.py).

[if] a full root run writes its two logs [then] the map names the src files each spec fetched, [else stop].

Regression lines:
  - [if] a request during a spec's tests lands in another spec's footprint, or a request in
    the gap between two specs is dropped from both [then] fail.
  - [if] a failed or timed-out spec reads as measured, or a skipped one as unmeasured [then] fail.
  - [if] a spec on disk the run never collected is missing from `ignored` [then] the
    selector runs it on every head, so fail.
  - [if] an empty root request or test log builds a map [then] fail.
  - [if] a named gate has a tests log and a missing or empty request log, or an empty tests log,
    and the map build aborts or records that gate as measured [then] fail.
  - [if] `publish` writes anything but one parentless commit moved by force [then] fail.
  - [if] `fetch` writes a file on a failed exchange [then] the scope job selects over
    garbage, so fail.
  - [if] `ledger` reads an R2 error as a missing artifact, or keeps a map over 7 days old
    green [then] fail.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import e2e_footprints_refresh as refresh
from scripts.e2e_footprints_refresh import MapError, build, fetch, ledger, publish, validate
from scripts.r2_artifact import R2Error

pytestmark = pytest.mark.requirement("DEVOPS-28")

SHA = "f" * 40
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
A, B, C = "tests/e2e/a.spec.ts", "tests/e2e/b.spec.ts", "tests/e2e/c.spec.ts"


def _run_dir(
    tmp_path: Path,
    *,
    tests: list[dict] | None = None,
    requests: list[dict] | None = None,
    collected: list[str] | None = None,
) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    tests = (
        [
            {"spec": A, "start": 1000, "end": 2000, "status": "passed"},
            {"spec": A, "start": 2100, "end": 3000, "status": "skipped"},
            {"spec": B, "start": 4000, "end": 5000, "status": "failed"},
            {"spec": C, "start": 6000, "end": 6500, "status": "skipped"},
        ]
        if tests is None
        else tests
    )
    requests = (
        [
            {"t": 900, "path": "src/before-a.ts"},
            {"t": 1500, "path": "src/a.ts"},
            {"t": 2050, "path": "src/a-between-tests.ts"},
            {"t": 3500, "path": "src/gap-a-b.ts"},
            {"t": 4500, "path": "src/b.svelte"},
            {"t": 4600, "path": "src/b.svelte"},
            {"t": 5500, "path": "src/gap-b-c.ts"},
        ]
        if requests is None
        else requests
    )
    (run / "tests.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tests), encoding="utf-8", newline="\n")
    (run / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in requests), encoding="utf-8", newline="\n")
    (run / "collected.json").write_text(
        json.dumps([A, B, C, "tests/e2e/never-ran.spec.ts"] if collected is None else collected),
        encoding="utf-8",
        newline="\n",
    )
    return run


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    e2e = repo / "apps/webui/frontend/tests/e2e"
    e2e.mkdir(parents=True, exist_ok=True)
    for name in ("a", "b", "c", "never-ran", "ignored-by-config"):
        (e2e / f"{name}.spec.ts").write_text("// spec\n", encoding="utf-8", newline="\n")
    return repo


def test_build_cuts_the_request_log_into_one_footprint_per_spec_with_gaps_shared(tmp_path: Path) -> None:
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    assert footprints["schema"] == 1 and footprints["sha"] == SHA and footprints["built"] == "2026-10-30T12:00:00+00:00"
    paths = footprints["paths"]

    def files(spec: str) -> set[str]:
        return {paths[i] for i in footprints["specs"][spec]["files"]}

    assert files(A) == {"src/before-a.ts", "src/a.ts", "src/a-between-tests.ts", "src/gap-a-b.ts"}
    assert files(B) == {"src/gap-a-b.ts", "src/b.svelte", "src/gap-b-c.ts"}
    assert files(C) == {"src/gap-b-c.ts"}
    assert "src/b.svelte" not in files(A), "control: a request during B's tests is not A's"


def test_build_records_status_seconds_tests_and_the_uncollected_specs(tmp_path: Path) -> None:
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    specs = footprints["specs"]
    assert specs[A]["status"] == "passed" and specs[A]["seconds"] == 1.9 and specs[A]["tests"] == 2
    assert specs[B]["status"] == "unmeasured"
    assert specs[C]["status"] == "skipped"
    assert specs["tests/e2e/never-ran.spec.ts"] == {"status": "unmeasured", "seconds": 0.0, "tests": 0, "files": []}
    assert footprints["ignored"] == ["tests/e2e/ignored-by-config.spec.ts"]
    assert list(specs) == sorted(specs)
    assert footprints["gates"] == {}


@pytest.mark.parametrize("empty", ["requests.jsonl", "tests.jsonl"])
def test_build_refuses_an_empty_log(tmp_path: Path, empty: str) -> None:
    run = _run_dir(tmp_path)
    (run / empty).write_text("", encoding="utf-8", newline="\n")
    with pytest.raises(MapError, match="recorded nothing"):
        build(run, SHA, _repo(tmp_path), NOW)


def test_build_records_a_named_gate_from_its_own_directory(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    gate = run / "gates" / "rekordbox"
    gate.mkdir(parents=True)
    spec = "tests/e2e/rekordbox-writeback-disabled.spec.ts"
    (gate / "tests.jsonl").write_text(
        json.dumps({"spec": spec, "start": 10, "end": 2000, "status": "passed"}) + "\n", encoding="utf-8", newline="\n"
    )
    (gate / "requests.jsonl").write_text(
        json.dumps({"t": 500, "path": "src/a.ts"}) + "\n" + json.dumps({"t": 600, "path": "src/gate-only.ts"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (gate / "collected.json").write_text(
        json.dumps([spec, "tests/e2e/never-ran-gate.spec.ts"]), encoding="utf-8", newline="\n"
    )
    footprints = build(run, SHA, _repo(tmp_path), NOW)
    entry = footprints["gates"]["rekordbox"]
    assert entry["status"] == "unmeasured", "a collected spec that never ran leaves the gate unmeasured"
    assert entry["seconds"] == 2.0 and entry["specs"] == ["tests/e2e/never-ran-gate.spec.ts", spec]
    assert {footprints["paths"][i] for i in entry["files"]} == {"src/a.ts", "src/gate-only.ts"}
    assert "src/gate-only.ts" in footprints["paths"], "a gate's request paths join the shared table"
    assert spec not in footprints["specs"], "a gate's specs are not root specs"
    assert footprints["gates"].keys() == {"rekordbox"}
    (gate / "collected.json").write_text(json.dumps([spec]), encoding="utf-8", newline="\n")
    assert build(run, SHA, _repo(tmp_path), NOW)["gates"]["rekordbox"]["status"] == "passed"


_UNMEASURED_GATE = {"status": "unmeasured", "seconds": 0.0, "specs": [], "files": []}


@pytest.mark.parametrize("write_requests", [False, True])
def test_named_gate_missing_or_empty_request_log_is_unmeasured(tmp_path: Path, write_requests: bool) -> None:
    run = _run_dir(tmp_path)
    gate = run / "gates" / "preflight"
    gate.mkdir(parents=True)
    spec = "tests/e2e/preflight-smoke.spec.ts"
    (gate / "tests.jsonl").write_text(
        json.dumps({"spec": spec, "start": 10, "end": 2000, "status": "passed"}) + "\n", encoding="utf-8", newline="\n"
    )
    if write_requests:
        (gate / "requests.jsonl").write_text("", encoding="utf-8", newline="\n")
    footprints = build(run, SHA, _repo(tmp_path), NOW)
    assert footprints["gates"]["preflight"] == _UNMEASURED_GATE
    assert len(footprints["specs"]) == 4


def test_named_gate_empty_tests_log_is_unmeasured_and_paths_exclude_gate_requests(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    gate = run / "gates" / "preflight"
    gate.mkdir(parents=True)
    (gate / "tests.jsonl").write_text("", encoding="utf-8", newline="\n")
    (gate / "requests.jsonl").write_text(
        json.dumps({"t": 500, "path": "src/gate-empty.ts"}) + "\n", encoding="utf-8", newline="\n"
    )
    footprints = build(run, SHA, _repo(tmp_path), NOW)
    assert footprints["gates"]["preflight"] == _UNMEASURED_GATE
    assert "src/gate-empty.ts" not in footprints["paths"]


def test_named_gate_requests_path_as_directory_raises_unreadable(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    gate = run / "gates" / "preflight"
    gate.mkdir(parents=True)
    (gate / "tests.jsonl").write_text(
        json.dumps({"spec": "tests/e2e/x.spec.ts", "start": 10, "end": 20, "status": "passed"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (gate / "requests.jsonl").mkdir()
    with pytest.raises(MapError, match="unreadable"):
        build(run, SHA, _repo(tmp_path), NOW)


def test_a_passed_spec_that_fetched_nothing_is_unmeasured(tmp_path: Path) -> None:
    """A harness-bundled spec (esbuild, `page.addScriptTag`) fetches no source through Vite."""
    tests = [
        {"spec": A, "start": 1000, "end": 2000, "status": "passed"},
        {"spec": B, "start": 4000, "end": 5000, "status": "passed"},
    ]
    requests = [{"t": 1500, "path": "src/a.ts"}]
    footprints = build(_run_dir(tmp_path, tests=tests, requests=requests, collected=[A, B]), SHA, _repo(tmp_path), NOW)
    assert footprints["specs"][A]["status"] == "passed"
    assert footprints["specs"][B]["status"] == "unmeasured" and footprints["specs"][B]["seconds"] == 1.0


def test_build_refuses_a_bad_sha(tmp_path: Path) -> None:
    with pytest.raises(MapError, match="40-hex"):
        build(_run_dir(tmp_path), "main", _repo(tmp_path), NOW)


def test_the_built_map_validates_for_the_selector(tmp_path: Path) -> None:
    from scripts.ci_e2e_selection import validate_map

    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    rendered = json.loads(refresh.render(footprints))
    assert validate_map(rendered, "built").sha == SHA
    assert validate(rendered, "built") is rendered


# ----- publish and fetch ----------------------------------------------------------


def _built_map(tmp_path: Path) -> Path:
    path = tmp_path / ".spec_footprints.json"
    path.write_text(
        refresh.render(build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)), encoding="utf-8", newline="\n"
    )
    return path


def test_publish_creates_then_moves_a_parentless_commit(tmp_path: Path) -> None:
    sent: list[tuple[str, str, dict]] = []

    def send(method: str, path: str, body: dict) -> bytes:
        sent.append((method, path, body))
        if path.endswith("/git/blobs"):
            return b'{"sha": "blob1"}'
        if path.endswith("/git/trees"):
            return b'{"sha": "tree1"}'
        if path.endswith("/git/commits"):
            return b'{"sha": "' + b"1" * 40 + b'"}'
        if method == "PATCH":
            raise MapError("Reference does not exist")
        return b"{}"

    line = publish("o/r", _built_map(tmp_path), send)
    assert line.endswith("ci-data-e2e at " + "1" * 40)
    methods = [(m, p.split("/git/")[1]) for m, p, _ in sent]
    assert methods == [
        ("POST", "blobs"),
        ("POST", "trees"),
        ("POST", "commits"),
        ("PATCH", "refs/heads/ci-data-e2e"),
        ("POST", "refs"),
    ]
    assert sent[2][2]["parents"] == [] and sent[3][2]["force"] is True
    assert sent[1][2]["tree"][0]["path"] == ".spec_footprints.json"


def test_publish_refuses_an_invalid_map_before_sending(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": 1}', encoding="utf-8", newline="\n")
    with pytest.raises(MapError):
        publish("o/r", bad, lambda *_: pytest.fail("nothing may be sent"))


def _api_for(footprints: dict, committed: str = "2026-10-30T06:00:00Z"):
    content = base64.b64encode(refresh.render(footprints).encode()).decode()

    def api(path: str) -> bytes:
        if path.endswith("/git/ref/heads/ci-data-e2e"):
            return json.dumps({"object": {"sha": "2" * 40}}).encode()
        if "/git/commits/" in path:
            return json.dumps({"tree": {"sha": "t"}, "committer": {"date": committed}}).encode()
        if "/git/trees/" in path:
            return json.dumps({"tree": [{"path": ".spec_footprints.json", "sha": "b"}]}).encode()
        if "/git/blobs/" in path:
            return json.dumps({"content": content}).encode()
        if "/actions/workflows/e2e.yml/runs" in path:
            return json.dumps({"workflow_runs": []}).encode()
        raise AssertionError(path)

    return api


def test_fetch_writes_the_published_map_and_names_it(tmp_path: Path) -> None:
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    dest = tmp_path / "published" / "map.json"
    line = fetch("o/r", dest, _api_for(footprints))
    assert json.loads(dest.read_text(encoding="utf-8")) == json.loads(refresh.render(footprints))
    assert f"on {SHA[:9]} from ci-data-e2e 222222222" in line


def test_fetch_writes_nothing_on_a_failed_exchange(tmp_path: Path) -> None:
    def api(path: str) -> bytes:
        raise MapError("gh api exploded")

    dest = tmp_path / "published" / "map.json"
    with pytest.raises(MapError):
        fetch("o/r", dest, api)
    assert not dest.exists()


# ----- ledger --------------------------------------------------------------------


class Store:
    def __init__(self, objects: dict[str, bytes | Exception]) -> None:
        self.objects = objects

    def exists(self, key: str) -> bool:
        value = self.objects.get(key)
        if isinstance(value, Exception):
            raise value
        return value is not None

    def read_member(self, key: str, member: str) -> bytes:
        assert member == ".spec_footprints.json"
        value = self.objects[key]
        assert isinstance(value, bytes)
        return value


def _runs_api(footprints: dict, run_ids: list[int], committed: str = "2026-10-30T06:00:00Z"):
    base = _api_for(footprints, committed)

    def api(path: str) -> bytes:
        if "/actions/workflows/e2e.yml/runs" in path:
            assert "branch=main&event=push&status=success" in path
            return json.dumps({"workflow_runs": [{"id": i, "head_sha": SHA} for i in run_ids]}).encode()
        return base(path)

    return api


def test_ledger_writes_the_newest_run_with_an_artifact(tmp_path: Path) -> None:
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    key_old = refresh.r2_artifact.object_key("o/r", "e2e-spec-footprints", "10")
    store = Store({key_old: refresh.render(footprints).encode()})
    out = tmp_path / "out" / "map.json"
    line = ledger("o/r", out, NOW, _runs_api(footprints, [11, 10]), store)
    assert "run 10" in line and json.loads(out.read_text(encoding="utf-8"))["sha"] == SHA


def test_ledger_fails_on_an_r2_error_and_never_reads_it_as_missing(tmp_path: Path) -> None:
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    key = refresh.r2_artifact.object_key("o/r", "e2e-spec-footprints", "11")
    with pytest.raises(MapError, match="R2 lookup"):
        ledger("o/r", tmp_path / "map.json", NOW, _runs_api(footprints, [11]), Store({key: R2Error("503")}))


def _built(footprints: dict, days_ago: float) -> dict:
    return {**footprints, "built": (NOW - timedelta(days=days_ago)).isoformat()}


def test_ledger_keeps_a_young_published_map_and_goes_red_past_seven_days(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    out = tmp_path / "map.json"
    line = ledger("o/r", out, NOW, _runs_api(_built(footprints, 2), []), Store({}))
    assert line.startswith("[e2e-footprints] kept ci-data-e2e") and not out.exists()
    assert "::warning title=e2e footprint map is not refreshing" in capsys.readouterr().err
    # A fresh publication commit does not renew an old map: the age is the map's own `built`.
    renewed = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(MapError, match=r"built 8\.0 days ago, over the 7-day limit"):
        ledger("o/r", out, NOW, _runs_api(_built(footprints, 8), [], renewed), Store({}))


def test_ledger_skips_an_artifact_whose_map_is_already_over_the_age_limit(tmp_path: Path) -> None:
    """[if] the newest artifact's map is over 7 days old [then] republishing it would renew its
    publication date while the selector rejects it by `built`, so it is skipped by name."""
    footprints = build(_run_dir(tmp_path), SHA, _repo(tmp_path), NOW)
    key = refresh.r2_artifact.object_key("o/r", "e2e-spec-footprints", "11")
    store = Store({key: refresh.render(_built(footprints, 7.5)).encode()})
    out = tmp_path / "map.json"
    with pytest.raises(MapError, match=r"run 11: map built 7\.5 days ago, over the 7-day limit") as info:
        ledger("o/r", out, NOW, _runs_api(_built(footprints, 9), [11]), store)
    assert "built 9.0 days ago" in str(info.value) and not out.exists()


def test_the_cli_exits_1_with_an_annotation_on_any_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = refresh.main(
        ["build", "--dir", str(tmp_path / "missing"), "--sha", SHA, "--out", str(tmp_path / "m.json")], now=NOW
    )
    assert rc == 1 and "::error title=e2e footprint map::" in capsys.readouterr().err
