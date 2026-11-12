"""perf-lab ledger: captures land in the KPI ledger, and compare exits 1 on a
regression or a Class A budget breach and 3 (UNKNOWN) when nothing comparable
exists. Also analyze.py's verdict on a whole run directory."""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.perf.perflab_support import (
    LAB,
    REPO,
    _chromium,
    _frontend_playwright,
    _load,
    _node,
    _node_json,
    pulse_server,
    set_default_sink,
)

# a stack manifest every capture here copies unless a test passes another
MANIFEST = {k: f"{k}-1" for k in _load("manifest").KEYS}
# the driver's workload options as a default `all` capture records them
OPTS = {
    "scenario": "all",
    "engine": "webaudio",
    "controls": False,
    "headless": False,
    "phaseS": 15,
    "leakCycles": 6,
    "latencyReps": 100,
    "width": 1728,
    "height": 1080,
}

HARNESS = "a" * 64
# driver.json started_at and finished_at of every _run_dir capture, as epoch seconds
DRIVER_T0 = 1791936000.0  # 2026-10-14T00:00:00Z
DRIVER_T1 = DRIVER_T0 + 300.0
SAMPLER_DEPS = {"python": "3.12.1", "psutil": "7.2.2", "instrument_env": {}}
ANALYSIS_DEPS = {"python": "3.12.1", "perfetto": "0.58.2", "psutil": "7.2.2", "instrument_env": {}}
DRIVER_DEPS = {"node": "v22.22.0", "playwright": "1.61.1", "playwright-core": "1.61.1", "instrument_env": {}}
GRAPHICS = {
    "devices": [{"vendor": "Apple (0x106b)", "device": "Apple M2 (0x0)", "driver": "Apple 1.0"}],
    "gl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    "gl_vendor": "Google Inc. (Apple)",
    "gl_version": "OpenGL ES 3.0 (ANGLE 2.1)",
    "gl_implementation": "(gl=egl-angle,angle=metal)",
    "skia_backend": "GaneshGL",
}
# a capture's audio output, as capture-meta.mjs audioOutputIdentity reports it
AUDIO_OUTPUT = {
    "sink_id": "",
    "sample_rate": 48000,
    "channels": 2,
    "base_latency_ms": 10.667,
    "device": "coreaudio:MacBook Pro Speakers | Apple Inc. | coreaudio_device_type_builtin | 48000",
    "device_unknown": None,
}


def _run_dir(
    tmp: Path,
    name: str,
    idle_cpu: float,
    *,
    ids: tuple[str, ...] = ("a",),
    audible_p99: float = 20.0,
    tails: dict[str, float] | None = None,
    visual: dict[str, float] | None = None,
    unbudgeted: list[str] | None = None,
    manifest: dict[str, Any] | None = MANIFEST,
    opts: dict[str, Any] | None = None,
    interval_s: float | None = 0.5,
    harness: dict[str, str | None] | None = None,
    media: dict[str, str] | None = None,
    deps: dict[str, dict[str, str] | None] | None = None,
    sampler_roots: list[int] | None = None,
    graphics: dict[str, Any] | None = GRAPHICS,
    audio_output: dict[str, Any] | None = AUDIO_OUTPUT,
    sampler_extra: dict[str, Any] | None = None,
    driver_extra: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> Path:
    run = tmp / name
    run.mkdir()
    phase = {
        "os": {"roles": {"chrome-renderer": {"cpu_pct": idle_cpu, "pss_mb": 200.0}, "engine-python": {"cpu_pct": 3.0}}}
    }
    t, v = tails or {}, visual or {}
    summary = {
        "play": {"presented_ms_p50": 160.0, "presented_ms_p99": audible_p99, "next_frame_ms_p99": v.get("play", 10.0)},
        "fader_cut": {
            "reflected_ms_p50": 5.0,
            "presented_ms_p99": t.get("fader_cut", 25.0),
            "next_frame_ms_p99": v.get("fader_cut", 11.0),
        },
        "fader_open": {"presented_ms_p99": t.get("fader_open", 26.0), "next_frame_ms_p99": v.get("fader_open", 12.0)},
        "eq_low_kill": {
            "presented_ms_p99": t.get("eq_low_kill", 28.0),
            "next_frame_ms_p99": v.get("eq_low_kill", 13.0),
        },
    }
    analysis = {
        "phases": {"idle-empty": phase, "loaded-stopped": phase, "four-playing": phase},
        "latency": {"summary": summary},
        "heap": {"snapshot_total_mb": 300.0},
        "class_a": {"budgeted": ["play", "eq", "fader"], "unbudgeted": unbudgeted or []},
        "coverage": {"PCI": 0.6, "control_factor": 0.9},
    }
    stamp = {"driver": HARNESS, "sampler": HARNESS, "analysis": HARNESS, **(harness or {})}
    libs = {"driver": DRIVER_DEPS, "sampler": SAMPLER_DEPS, "analysis": ANALYSIS_DEPS, **(deps or {})}
    analysis["harness"] = stamp["analysis"]
    analysis["measurement_deps"] = libs["analysis"]
    sampler = {
        "interval_s": interval_s,
        "harness": stamp["sampler"],
        "measurement_deps": libs["sampler"],
        "scope": "process-family",
        "roots": [101, 102, 103] if sampler_roots is None else sampler_roots,
        # ticks from a little before the driver started to a little after it finished
        "first_tick_t": DRIVER_T0 - 0.2,
        "last_tick_t": DRIVER_T1 + 0.3,
        "host": "lab-host",
        **(sampler_extra or {}),
    }
    sampler = {k: v for k, v in sampler.items() if f"sampler.{k}" not in drop}
    (run / "sampler.json").write_text(json.dumps(sampler))
    (run / "analysis.json").write_text(json.dumps(analysis))
    driver = {
        "browser_version": "131.0",
        "started_at": "2026-10-14T00:00:00.000Z",
        "finished_at": "2026-10-14T00:05:00.000Z",
        "host": "lab-host",
        "run_name": name,  # two real captures never share a driver.json; keeps the digests apart
        "opts": {**OPTS, **(opts or {}), "ids": list(ids), "commit": "0" * 40},
        "manifest": manifest,
        "harness": stamp["driver"],
        "measurement_deps": libs["driver"],
        "roots": {"engine": 101, "vite": 102, "driver": 103},
        "graphics": graphics,
        "audio_output": audio_output,
        "media": (
            {f"/api/v1/tracks/{i}/{a}": f"{i}-{a}" for i in ids for a in ("audio", "anlz")} if media is None else media
        ),
    }
    driver.update(driver_extra or {})
    driver = {k: v for k, v in driver.items() if f"driver.{k}" not in drop}
    (run / "driver.json").write_text(json.dumps(driver))
    return run


class _Ledger:
    def __init__(self, tmp: Path) -> None:
        self.path = tmp / "kpi-ledger.json"
        self.path.write_text('{\n "entries": []\n}\n')
        self.tmp = tmp

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, str(LAB / "ledger.py"), *args, "--ledger", str(self.path), "--host-class", "test-box"]
        return subprocess.run(argv, capture_output=True, text=True, cwd=REPO, check=False)

    def add(self, name: str, idle_cpu: float = 50.0, **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.cli("add", str(_run_dir(self.tmp, name, idle_cpu, **kw)))

    def compare(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.cli("compare", *extra)


def test_compare_is_unknown_until_two_captures_then_flags_a_regression(tmp_path: Path) -> None:
    led = _Ledger(tmp_path)
    assert led.compare().returncode == 3
    assert led.add("r1").returncode == 0
    mine = json.loads(led.path.read_text())["entries"]
    assert {e["kpi"] for e in mine} >= {"perflab_pci", "perflab_renderer_cpu_pct__idle_empty"}
    assert len({e["capture_id"] for e in mine}) == 1
    units = {e["kpi"]: e["unit"] for e in mine}
    assert units["perflab_renderer_pss_mb__loaded_stopped"] == "MB"
    assert units["perflab_renderer_cpu_pct__idle_empty"] == "pct_of_core"
    assert units["perflab_latency_play_audible_p50_ms"] == "ms"
    assert all(e["sha"] == "0" * 40 for e in mine)
    assert led.compare().returncode == 3  # one capture: nothing to compare with
    assert led.add("r2").returncode == 0
    assert led.compare().returncode == 0  # control: an identical capture is not a regression
    assert led.add("r3", 80.0).returncode == 0
    out = led.compare()
    assert out.returncode == 1 and "REGRESSED renderer_cpu_pct.idle-empty" in out.stdout, out.stdout


def test_compare_across_fixture_sets_is_unknown(tmp_path: Path) -> None:
    led = _Ledger(tmp_path)
    assert led.add("r1").returncode == 0
    assert led.add("r2", 80.0, ids=("b",)).returncode == 0  # other tracks: the delta says nothing about the code
    out = led.compare()
    assert out.returncode == 3 and "fixture tracks differ" in out.stdout, out.stdout


def test_compare_across_capture_settings_is_unknown(tmp_path: Path) -> None:
    """--controls plants a CPU burner and a leak, so a capture with it and one
    without measure different work; any workload option differing, or one
    missing, is UNKNOWN rather than a regression."""
    led = _Ledger(tmp_path)
    assert led.add("r1").returncode == 0
    assert led.add("r2", 80.0, opts={"controls": True}).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "capture settings differ in controls" in out.stdout, out.stdout
    assert led.add("r3", 80.0, opts={"controls": True}).returncode == 0
    out = led.compare()  # control: equal settings compare, and the delta is judged
    assert out.returncode == 0, out.stdout
    old = {k: v for k, v in OPTS.items() if k != "latencyReps"}
    d = tmp_path / "missing"
    d.mkdir()
    led = _Ledger(d)
    assert led.add("r1").returncode == 0
    run = _run_dir(d, "r2", 50.0)
    drv = json.loads((run / "driver.json").read_text())
    (run / "driver.json").write_text(json.dumps({**drv, "opts": {**old, "ids": ["a"], "commit": "0" * 40}}))
    assert led.cli("add", str(run)).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "second capture records no capture settings" in out.stdout, out.stdout


def test_compare_across_sampler_intervals_is_unknown(tmp_path: Path) -> None:
    """The sampler's tick interval is a capture setting: a coarser interval
    misses short spikes and costs less, so it changes the numbers."""
    led = _Ledger(tmp_path)
    assert led.add("r1").returncode == 0
    assert led.add("r2", 80.0, interval_s=1.0).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "capture settings differ in sampler_interval_s" in out.stdout, out.stdout
    assert led.add("r3", 80.0, interval_s=1.0).returncode == 0
    out = led.compare()  # control: equal intervals compare
    assert out.returncode == 0, out.stdout
    # a sampler with no interval has no tick to bound its span by (sampler_span),
    # so it never reaches the ledger to compare as a capture without settings
    out = led.add("r4", interval_s=None)
    assert out.returncode == 2 and "sampler.json records no interval_s" in out.stderr, out.stderr


def test_compare_across_different_fixture_inputs_is_unknown(tmp_path: Path) -> None:
    """A fixture id that resolves to a re-encoded library file, or to replaced
    Rekordbox analysis or artwork, outside the fixture dir measures different
    inputs under an unchanged manifest; the driver's digests of every track
    asset served make that UNKNOWN, naming the asset, as does a capture that
    records none."""
    led = _Ledger(tmp_path)
    base = {"/api/v1/tracks/a/audio": "a-audio", "/api/v1/tracks/a/anlz": "a-anlz"}
    reencoded = {**base, "/api/v1/tracks/a/audio": "re-encoded"}
    assert led.add("r1").returncode == 0
    assert led.add("r2", 80.0, media=reencoded).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "fixture inputs differ (/api/v1/tracks/a/audio)" in out.stdout, out.stdout
    assert led.add("r3", 80.0, media=reencoded).returncode == 0
    out = led.compare()  # control: the same inputs compare, and the delta is judged
    assert out.returncode == 0, out.stdout
    art = {**reencoded, "/api/v1/tracks/a/artwork?size=orig&online=true": "cover"}
    assert led.add("r4", 80.0, media=art).returncode == 0
    out = led.compare()  # an asset the page loaded in one capture only: different inputs
    assert out.returncode == 3, out.stdout
    assert "fixture inputs differ (/api/v1/tracks/a/artwork?size=orig&online=true)" in out.stdout, out.stdout
    assert led.add("r5", 80.0, media={}).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "second capture records no input digests" in out.stdout, out.stdout


def test_compare_across_harness_code_is_unknown(tmp_path: Path) -> None:
    """Captures measured by different lab code (driver, sampler or analyzer)
    are UNKNOWN: a delta could be the instrument changing, not the app."""
    led = _Ledger(tmp_path)
    other = "b" * 64
    assert led.add("r1").returncode == 0
    assert led.add("r2", 80.0, harness={"driver": other, "sampler": other, "analysis": other}).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "measured by different harness code" in out.stdout, out.stdout
    assert led.add("r3", 80.0, harness={"driver": other, "sampler": other, "analysis": other}).returncode == 0
    out = led.compare()  # control: one harness compares, and the delta is judged
    assert out.returncode == 0, out.stdout


def test_compare_across_playwright_versions_is_unknown(tmp_path: Path) -> None:
    """Same lab source, a different Playwright driving the browser: the
    driver library is part of the instrument, so the delta is UNKNOWN."""
    led = _Ledger(tmp_path)
    other = {**DRIVER_DEPS, "playwright": "1.56.1", "playwright-core": "1.56.1"}
    assert led.add("r1").returncode == 0
    assert led.add("r2", 80.0, deps={"driver": other}).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "measured by different harness code or libraries" in out.stdout, out.stdout
    assert led.add("r3", 80.0, deps={"driver": other}).returncode == 0
    out = led.compare()  # control: one Playwright compares, and the delta is judged
    assert out.returncode == 0, out.stdout


_JS_DEPS = """
import path from 'node:path';
import { createRequire } from 'node:module';
const [url, frontend] = process.argv.slice(1);
const { jsMeasurementDeps } = await import(url);
const req = createRequire(path.join(frontend, 'package.json'));
const core = createRequire(req.resolve('playwright/package.json'))('playwright-core/package.json').version;
console.log(JSON.stringify({ deps: jsMeasurementDeps(req), playwright: req('playwright/package.json').version, core }));
"""


def test_js_measurement_deps_are_the_playwright_the_driver_loads() -> None:
    """Real resolution from the frontend's node_modules, the copy driver.mjs
    loads, and the playwright-core that copy itself resolves."""
    _frontend_playwright()
    out = _node_json(_JS_DEPS, (LAB / "harness.mjs").as_uri(), str(REPO / "apps/webui/frontend"))
    deps = out["deps"]
    assert deps["playwright"] == out["playwright"] and deps["playwright-core"] == out["core"], out
    assert deps["playwright"][0].isdigit() and deps["node"].startswith("v"), deps
    # the executable that really ran, hashed as the stack manifest hashes its node
    assert deps["node_sha256"] == _load("manifest").sha256_file(Path(_node()).resolve()), deps


_NODE_ID = """
const { jsMeasurementDeps } = await import(process.argv[1]);
import { createRequire } from 'node:module';
const deps = jsMeasurementDeps(createRequire(process.argv[2]));
console.log(JSON.stringify({ node: deps.node, node_sha256: deps.node_sha256 }));
"""


@pytest.mark.skipif(os.name == "nt", reason="runs a copied node through a symlink and POSIX exec bits")
def test_two_node_binaries_of_one_version_are_two_driver_identities(tmp_path: Path) -> None:
    """Codex's case: the driver runs under another Node binary that reports
    the same process.version. The real node and a byte-different copy of it,
    each running jsMeasurementDeps, must record one version and two hashes,
    each the sha256 of the binary that ran it; launched through a symlink,
    the hash is the resolved target's."""
    manifest = _load("manifest")
    real = Path(_node()).resolve()
    other = tmp_path / "bin" / "node"
    other.parent.mkdir()
    if sys.platform == "darwin":
        # macOS kills a Mach-O whose bytes no longer match its signature, so the
        # other build is the same bytes re-signed ad hoc under another identifier;
        # a dylib-linked node (Homebrew's) finds its libnode beside bin/ via @rpath
        shutil.copyfile(real, other)
        other.chmod(0o755)
        if (real.parent.parent / "lib").is_dir():
            (tmp_path / "lib").symlink_to(real.parent.parent / "lib")
        sign = ["/usr/bin/codesign", "--force", "--sign", "-", "-i", "perflab.another.build", str(other)]
        subprocess.run(sign, check=True, capture_output=True, timeout=120)
    else:
        other.write_bytes(real.read_bytes() + b"\0another build")
        other.chmod(0o755)
    link = tmp_path / "node-link"
    link.symlink_to(other)
    args = [str(LAB / "harness.mjs"), str(REPO / "apps/webui/frontend/package.json")]

    def ident(exe: Path) -> dict[str, str]:
        r = subprocess.run(
            [str(exe), "--input-type=module", "-e", _NODE_ID, *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return dict(json.loads(r.stdout.strip().splitlines()[-1]))

    a, b, via_link = ident(real), ident(other), ident(link)
    assert a["node"] == b["node"] == via_link["node"], (a, b)
    assert a["node_sha256"] == manifest.sha256_file(real)
    assert b["node_sha256"] == via_link["node_sha256"] == manifest.sha256_file(other) != a["node_sha256"]


@pytest.mark.parametrize(
    ("stamps", "why"),
    [
        ({"sampler": "b" * 64}, "ran different harness code"),
        ({"analysis": None}, "analysis.json records no harness digest"),
        ({"driver": None}, "driver.json records no harness digest"),
        ({"deps": {"sampler": None}}, "sampler.json records no measurement_deps"),
        ({"deps": {"analysis": {}}}, "analysis.json records no measurement_deps"),
        ({"deps": {"driver": None}}, "driver.json records no measurement_deps"),
        (
            {"deps": {"driver": {k: v for k, v in DRIVER_DEPS.items() if k != "instrument_env"}}},
            "driver.json records no instrument_env",
        ),
        ({"deps": {"sampler": {**SAMPLER_DEPS, "instrument_env": None}}}, "sampler.json records no instrument_env"),
    ],
)
def test_add_refuses_a_capture_without_one_harness(tmp_path: Path, stamps: dict[str, Any], why: str) -> None:
    led = _Ledger(tmp_path)
    deps = stamps.pop("deps", None)
    out = led.add("r1", harness=stamps, deps=deps)
    assert out.returncode == 2 and why in out.stderr, out.stderr
    assert led.add("r2").returncode == 0  # control: one shared stamp is ledgered


@pytest.mark.parametrize(
    ("roots", "why"),
    [
        ([101, 103], "not this capture's engine, vite and driver: missing vite"),
        ([101, 102, 103, 999], "not this capture's engine, vite and driver: foreign pid(s) [999]"),
        ([101, 999, 103], "missing vite; foreign pid(s) [999]"),
    ],
)
def test_add_refuses_a_sampler_not_rooted_at_the_capture(tmp_path: Path, roots: list[int], why: str) -> None:
    """A sampler that omits the vite root or names an unrelated pid still says
    process-family, and its CPU totals would be partial or someone else's."""
    led = _Ledger(tmp_path)
    out = led.add("r1", sampler_roots=roots)
    assert out.returncode == 2 and why in out.stderr, out.stderr
    assert led.add("r2", sampler_roots=[103, 102, 101]).returncode == 0  # control: exactly its own, any order
    ledger = _load("ledger")
    assert ledger.sampler_scope({}, {"scope": "process-family", "roots": [1]}).startswith("driver.json records no")
    assert "not process-family" in ledger.sampler_scope({}, {"scope": "unscoped"})


# _run_dir's sampler ticks every 0.5 s; one tick is the slack sampler_span allows at each end
TICK = 0.5


@pytest.mark.parametrize(
    ("sampler", "why"),
    [
        # stopped more than one tick before the driver finished (a short --duration)
        ({"last_tick_t": DRIVER_T1 - TICK - 0.1}, "before the driver finished"),
        ({"last_tick_t": DRIVER_T1 - 120.0}, "the sampler stopped before the capture ended"),
        # first tick more than one tick after the driver started
        ({"first_tick_t": DRIVER_T0 + TICK + 0.1}, "after the driver started"),
        ({"first_tick_t": DRIVER_T0 + 60.0}, "the start of the capture was not sampled"),
        # the slack is one tick of THIS sampler: 0.3 s late is inside a 0.5 s tick, not a 0.1 s one
        ({"first_tick_t": DRIVER_T0 + 0.3, "interval_s": 0.1}, "after the driver started"),
    ],
)
def test_add_refuses_a_sampler_that_does_not_span_the_driver(tmp_path: Path, sampler: dict, why: str) -> None:
    """A sampler that stops before the driver ends (or starts after it began)
    still populates the later metrics, but one capture ran them under
    concurrent process-tree sampling and the other did not."""
    led = _Ledger(tmp_path)
    out = led.add("r1", sampler_extra=sampler)
    assert out.returncode == 2 and why in out.stderr, out.stderr
    assert json.loads(led.path.read_text())["entries"] == []
    assert led.add("r2").returncode == 0  # control: the same capture spanning the driver is ledgered


@pytest.mark.parametrize(
    "sampler",
    [
        # starts a few ms early and stops a few ms late: the ordinary case
        {"first_tick_t": DRIVER_T0 - 0.005, "last_tick_t": DRIVER_T1 + 0.005},
        # started minutes before and stopped minutes after: still covers it
        {"first_tick_t": DRIVER_T0 - 600.0, "last_tick_t": DRIVER_T1 + 600.0},
        # within one tick at each end: discrete ticks cannot place coverage finer
        {"first_tick_t": DRIVER_T0 + TICK - 0.01, "last_tick_t": DRIVER_T1 - TICK + 0.01},
    ],
)
def test_add_accepts_a_sampler_that_spans_the_driver(tmp_path: Path, sampler: dict) -> None:
    """The overshoot control: the span check must not demand the sampler start
    and stop at the driver's own instants."""
    led = _Ledger(tmp_path)
    out = led.add("r1", sampler_extra=sampler)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize(
    ("drop", "extra", "why"),
    [
        (("sampler.first_tick_t",), {}, "sampler.json records no first_tick_t"),
        (("sampler.last_tick_t",), {}, "sampler.json records no last_tick_t"),
        ((), {"sampler": {"first_tick_t": None}}, "sampler.json records no first_tick_t"),
        (("driver.started_at",), {}, "driver.json has no UTC started_at"),
        # a stamp with no zone is not read as local time
        ((), {"driver": {"started_at": "2026-10-14T00:00:00"}}, "driver.json has no UTC started_at"),
        (("sampler.host",), {}, "records no host"),
        (("driver.host",), {}, "records no host"),
        ((), {"sampler": {"host": "other-box"}}, "driver ran on lab-host and sampler on other-box"),
    ],
)
def test_a_sampler_span_that_cannot_be_measured_is_unknown(
    tmp_path: Path, drop: tuple[str, ...], extra: dict, why: str
) -> None:
    """An older capture, or one whose files lack the stamps or come from two
    clocks, is UNKNOWN and refused, never a pass."""
    led = _Ledger(tmp_path)
    out = led.add("r1", drop=drop, sampler_extra=extra.get("sampler"), driver_extra=extra.get("driver"))
    assert out.returncode == 2 and "UNKNOWN" in out.stderr and why in out.stderr, out.stderr
    assert json.loads(led.path.read_text())["entries"] == []


def _iso(t: float) -> str:
    """driver.mjs's stamp format (Date.toISOString): UTC, milliseconds."""
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Family:
    """Three real processes standing in for the engine, vite and the driver."""

    def __enter__(self) -> list[int]:
        argv = [sys.executable, "-c", "import time; time.sleep(120)"]
        self.procs = [subprocess.Popen(argv) for _ in range(3)]
        return [p.pid for p in self.procs]

    def __exit__(self, *_: object) -> None:
        for p in self.procs:
            p.kill()
            p.wait(timeout=10)


def _start_sampler(out: Path, roots: list[int], *args: str, lab: Path = LAB) -> subprocess.Popen[str]:
    """The real sampler.py (lab's copy), sampling the real family every 0.1 s;
    returns once its first tick is on disk."""
    argv = [sys.executable, str(lab / "sampler.py"), "--out", str(out), "--interval", "0.1"]
    argv += [a for pid in roots for a in ("--root-pid", str(pid))] + list(args)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not ((out / "samples.jsonl").exists() and (out / "samples.jsonl").read_text().strip()):
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "sampler wrote no tick in 30 s"
        time.sleep(0.02)
    return proc


def _ledger_real_sampler(led: _Ledger, name: str, sampled: Path, roots: list[int], t0: float, t1: float) -> Path:
    """A capture whose sampler.json is the real sampler's own output, and whose
    driver.json brackets the given wall-clock interval on this host."""
    real = _load("manifest").harness_digest()
    run = _run_dir(
        led.tmp,
        name,
        50.0,
        harness={"driver": real, "analysis": real},
        driver_extra={
            "started_at": _iso(t0),
            "finished_at": _iso(t1),
            "host": platform.node(),
            "roots": dict(zip(("engine", "vite", "driver"), roots, strict=True)),
        },
    )
    shutil.copyfile(sampled / "sampler.json", run / "sampler.json")
    return run


def test_real_sampler_span_is_checked_against_the_driver(tmp_path: Path) -> None:
    """Real sampler.py runs: one stopped by --duration before the driver
    finished, one started after the driver began, and the control that spans it."""
    led = _Ledger(tmp_path)
    with _Family() as roots:
        # control: the driver's interval lies inside the sampler's
        smp = tmp_path / "s-span"
        proc = _start_sampler(smp, roots, "--duration", "1.0")
        t0 = time.time()
        time.sleep(0.3)
        t1 = time.time()
        assert proc.wait(timeout=30) == 0, proc.communicate()
        out = led.cli("add", str(_ledger_real_sampler(led, "span", smp, roots, t0, t1)))
        assert out.returncode == 0, out.stderr
        rec = json.loads((smp / "sampler.json").read_text())
        assert rec["first_tick_t"] <= t0 and rec["last_tick_t"] >= t1 and rec["host"] == platform.node(), rec

        # Codex's case: --duration ends the sampler while the driver still runs
        smp = tmp_path / "s-early-stop"
        proc = _start_sampler(smp, roots, "--duration", "0.3")
        t0 = time.time()
        assert proc.wait(timeout=30) == 0, proc.communicate()
        time.sleep(0.5)  # the driver's latency and leak work, unsampled
        t1 = time.time()
        out = led.cli("add", str(_ledger_real_sampler(led, "early-stop", smp, roots, t0, t1)))
        assert out.returncode == 2 and "the sampler stopped before the capture ended" in out.stderr, out.stderr

        # the sampler started after the driver did
        smp = tmp_path / "s-late-start"
        t0 = time.time()
        time.sleep(0.5)
        proc = _start_sampler(smp, roots, "--duration", "0.3")
        t1 = time.time()
        assert proc.wait(timeout=30) == 0, proc.communicate()
        out = led.cli("add", str(_ledger_real_sampler(led, "late-start", smp, roots, t0, t1)))
        assert out.returncode == 2 and "the start of the capture was not sampled" in out.stderr, out.stderr
    assert len({e["capture_id"] for e in json.loads(led.path.read_text())["entries"]}) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Windows delivers no SIGTERM to a Python handler")
def test_real_sampler_takes_a_last_tick_after_it_is_signalled(tmp_path: Path) -> None:
    """A SIGTERM lands mid-sleep; the sampler still ticks once more after it,
    so its span reaches the moment it was told to stop."""
    with _Family() as roots:
        proc = _start_sampler(tmp_path / "s", roots, "--interval", "1.0")
        time.sleep(0.2)
        signalled = time.time()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0, proc.communicate()
    rec = json.loads((tmp_path / "s" / "sampler.json").read_text())
    assert rec["last_tick_t"] >= signalled and rec["ticks"] >= 2, rec


def test_a_sampler_whose_harness_changed_while_it_sampled_records_none(tmp_path: Path) -> None:
    """Codex's case: the real sampler.py runs from a measurement checkout (a
    copy of the lab), and its own source is replaced after its first tick.
    The samples were taken by the old code, so stamping the new digest would
    let it compare with captures that new code made: the record carries no
    harness, names why, the sampler exits 3, and the ledger refuses it. The
    control, the same copy left alone, stamps the digest taken before its
    first tick and is ledgered."""
    manifest = _load("manifest")
    led = _Ledger(tmp_path)
    lab = tmp_path / "harness-checkout"
    shutil.copytree(LAB, lab, ignore=shutil.ignore_patterns("__pycache__"))
    started = manifest.harness_digest(lab)
    assert started == manifest.harness_digest(LAB)
    with _Family() as roots:
        smp = tmp_path / "s-same"
        proc = _start_sampler(smp, roots, "--duration", "1.0", lab=lab)
        t0 = time.time()
        time.sleep(0.2)
        t1 = time.time()
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        rec = json.loads((smp / "sampler.json").read_text())
        assert rec["harness"] == started and rec["harness_fault"] is None and rec["measurement_deps"], rec
        assert led.cli("add", str(_ledger_real_sampler(led, "same", smp, roots, t0, t1))).returncode == 0

        smp = tmp_path / "s-moved"
        proc = _start_sampler(smp, roots, "--duration", "1.0", lab=lab)
        t0 = time.time()
        (lab / "sampler.py").write_text((lab / "sampler.py").read_text() + "\n# replaced mid-run\n")
        moved = manifest.harness_digest(lab)
        assert moved != started
        t1 = time.time()
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 3, (out, err)
        rec = json.loads((smp / "sampler.json").read_text())
        why = f"the lab code changed while the sampler ran ({started[:12]} -> {moved[:12]})"
        assert rec["harness"] is None and rec["measurement_deps"] is None and rec["harness_fault"] == why, rec
        assert why in err and rec["ticks"] >= 1
        refused = led.cli("add", str(_ledger_real_sampler(led, "moved", smp, roots, t0, t1)))
        assert refused.returncode == 2 and "sampler.json records no harness digest" in refused.stderr, refused.stderr


def test_harness_digest_agrees_across_python_and_node_and_tracks_the_lab_code(tmp_path: Path) -> None:
    """manifest.harness_digest (sampler, analyzer) and harness.mjs (driver)
    must agree on the same code, or no capture is ever ledgerable; and the
    digest must move when any lab script changes, and only then."""
    manifest = _load("manifest")
    lab = tmp_path / "lab"
    lab.mkdir()
    for name in ("driver.mjs", "sampler.py", "notes.md"):
        (lab / name).write_text(f"// {name}\n")
    (lab / "sub").mkdir()
    node = _node_json(_HARNESS, (LAB / "harness.mjs").as_uri(), str(lab))
    first = manifest.harness_digest(lab)
    assert node["digest"] == first and len(first) == 64
    assert manifest.harness_digest(LAB) == _node_json(_HARNESS, (LAB / "harness.mjs").as_uri(), str(LAB))["digest"]
    (lab / "notes.md").write_text("changed\n")  # not measurement code
    assert manifest.harness_digest(lab) == first
    (lab / "sampler.py").write_text("# changed\n")
    assert manifest.harness_digest(lab) != first
    second = manifest.harness_digest(lab)
    # a script's own lockfile pins its transitive dependencies: it is measurement code too
    (lab / "analyze.py.lock").write_text("version = 1\n")
    assert manifest.harness_digest(lab) != second
    assert _node_json(_HARNESS, (LAB / "harness.mjs").as_uri(), str(lab))["digest"] == manifest.harness_digest(lab)


def test_measurement_deps_record_the_realized_transitive_tree() -> None:
    """The whole environment, not only the named packages: pluggy is no
    package anyone names, only pytest's dependency, and it must be recorded
    at the version really installed; a named package not installed is absent."""
    from importlib.metadata import version

    manifest = _load("manifest")
    deps = manifest.measurement_deps(("perflab-no-such-package",))
    assert deps["pluggy"] == version("pluggy") and deps["pytest"] == version("pytest"), deps
    assert deps["perflab-no-such-package"] == "absent"


_HARNESS = """
const [url, lab] = process.argv.slice(1);
const { harnessDigest } = await import(url);
console.log(JSON.stringify({ digest: harnessDigest(lab) }));
"""


def test_a_move_from_a_zero_baseline_is_judged_by_direction(tmp_path: Path) -> None:
    led = _Ledger(tmp_path)
    assert led.add("r1", 0.0).returncode == 0
    assert led.add("r2", 5.0).returncode == 0
    out = led.compare()
    assert out.returncode == 1 and "REGRESSED renderer_cpu_pct.idle-empty" in out.stdout, out.stdout


def test_a_tail_regression_under_an_unchanged_median_is_flagged(tmp_path: Path) -> None:
    led = _Ledger(tmp_path)
    assert led.add("r1", audible_p99=25.0).returncode == 0
    assert led.add("r2", audible_p99=29.0).returncode == 0  # +16%, still inside the budget
    out = led.compare()
    assert out.returncode == 1 and "REGRESSED latency_play_audible_p99_ms" in out.stdout, out.stdout
    assert "ok        latency_play_audible_p50_ms" in out.stdout  # control: the median did not move


@pytest.mark.parametrize(("kind", "value", "ceiling"), [("audible", 32.0, 30.0), ("visual", 17.0, 16.0)])
def test_a_p99_over_the_class_a_budget_fails_compare(tmp_path: Path, kind: str, value: float, ceiling: float) -> None:
    """A move inside the relative tolerance still fails when it crosses the
    absolute Class A ceiling; at the ceiling is within it (control)."""
    led = _Ledger(tmp_path)
    key = "tails" if kind == "audible" else "visual"

    def p99(v: float) -> dict[str, Any]:
        return {key: {"fader_open": v}}

    assert led.add("r1", **p99(ceiling - 1)).returncode == 0
    assert led.add("r2", **p99(ceiling)).returncode == 0
    out = led.compare()
    assert out.returncode == 0 and "OVER-BUDGET" not in out.stdout, out.stdout
    assert led.add("r3", **p99(value)).returncode == 0
    out = led.compare()
    assert out.returncode == 1 and f"OVER-BUDGET latency_fader_open_{kind}_p99_ms" in out.stdout, out.stdout


@pytest.mark.parametrize(("visual", "rc"), [(15.0, 3), (17.0, 1)])
def test_a_first_capture_over_a_budget_fails_without_history(tmp_path: Path, visual: float, rc: int) -> None:
    led = _Ledger(tmp_path)
    assert led.add("r1", visual={"fader_cut": visual}).returncode == 0
    assert led.compare().returncode == rc


def test_unbudgeted_class_a_paths_make_compare_unknown_unless_scoped(tmp_path: Path) -> None:
    led = _Ledger(tmp_path)
    assert led.add("r1").returncode == 0
    assert led.add("r2").returncode == 0
    assert led.compare().returncode == 0  # control: every path budgeted
    assert led.add("r3", unbudgeted=["pause", "crossfader"]).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "2 Class A path(s) not budgeted (pause,crossfader)" in out.stdout, out.stdout
    out = led.compare("--class-a-scope", "budgeted")
    assert out.returncode == 0 and "[SCOPE]" in out.stdout, out.stdout


def test_add_refuses_an_invalid_capture_and_a_duplicate(tmp_path: Path) -> None:
    """A driver error, an invalid mark or an unfinished driver voids a capture;
    console errors (the app's own logging) do not (control). One run added
    twice would become compare's newest pair and read as no movement."""
    led = _Ledger(tmp_path)
    changes: list[dict[str, Any]] = [
        {"errors": ["memory dump B failed"]},
        {"invalid": "audio broke"},
        {"finished_at": None},
    ]
    for i, change in enumerate(changes):
        run = _run_dir(tmp_path, f"bad{i}", 20.0)
        drv = json.loads((run / "driver.json").read_text())
        (run / "driver.json").write_text(json.dumps({**drv, **change}))
        out = led.cli("add", str(run))
        assert out.returncode == 2 and "capture is invalid" in out.stderr, out.stderr
    assert json.loads(led.path.read_text())["entries"] == []
    run = _run_dir(tmp_path, "ok", 20.0)
    drv = json.loads((run / "driver.json").read_text())
    (run / "driver.json").write_text(json.dumps({**drv, "console_errors": ["favicon.ico 404"]}))
    assert led.cli("add", str(run)).returncode == 0
    dup = led.cli("add", str(run))
    assert dup.returncode == 2 and "already in the ledger" in dup.stderr


def _sampled_run(tmp: Path, scope: str, driver: dict | None = None) -> Path:
    """A run dir with three sampler ticks over one 2 s phase, no trace."""
    run = tmp / f"run-{scope}"
    run.mkdir()
    (run / "phases.json").write_text(
        json.dumps([{"name": "idle-empty", "wall_start": "2026-10-14T00:00:00Z", "wall_s": 2.0}])
    )
    t0 = 1791936000.0  # 2026-10-14T00:00:00Z
    proc = lambda cpu: {"role": "chrome-renderer", "cpu_s": cpu, "child_cpu_s": 0.0, "threads": {}, "pss": 1 << 20}  # noqa: E731
    ticks = [{"t": t0 + i, "procs": {"10": proc(0.5 * i)}} for i in range(3)]
    (run / "samples.jsonl").write_text("".join(json.dumps(t) + "\n" for t in ticks))
    (run / "sampler.json").write_text(json.dumps({"scope": scope, "thread_names": {}, "interval_s": 1.0}))
    (run / "driver.json").write_text(json.dumps({"finished_at": "2026-10-14T00:00:02Z", **(driver or {})}))
    return run


def _analyze(run: Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(LAB / "analyze.py"), str(run)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, cwd=REPO)


@pytest.mark.parametrize(("scope", "measured"), [("unscoped", False), ("process-family", True)])
def test_analysis_publishes_no_os_numbers_from_an_unscoped_sampler(tmp_path: Path, scope: str, measured: bool) -> None:
    run = _sampled_run(tmp_path, scope)
    r = _analyze(run)
    assert r.returncode == 0 and "[OK] PCI" in r.stdout, r.stderr
    os_ = json.loads((run / "analysis.json").read_text())["phases"]["idle-empty"]["os"]
    if measured:  # control: a process-family sampler is published
        assert abs(os_["roles"]["chrome-renderer"]["cpu_pct"] - 50.0) < 1e-9
    else:
        assert os_["status"] == "UNKNOWN" and "not process-family" in os_["reason"], os_


def test_an_invalid_capture_is_analyzed_for_diagnosis_but_never_ok(tmp_path: Path) -> None:
    run = _sampled_run(tmp_path, "process-family", {"errors": ["audio dropout at 3.2 s"]})
    r = _analyze(run)
    assert r.returncode == 1 and "[OK]" not in r.stdout and "audio dropout" in r.stderr, (r.stdout, r.stderr)
    assert "audio dropout" in json.loads((run / "analysis.json").read_text())["invalid"]
    assert (run / "report.md").read_text().startswith("# INVALID CAPTURE: driver recorded 1 error(s)")
    assert _load("ledger").capture_invalid({}) is not None  # no driver.json is no capture


_AUDIO_OUTPUT = """
import path from 'node:path';
import { createRequire } from 'node:module';
const [lab, chrome, ...args] = process.argv.slice(1);
const { audioOutputIdentity } = await import(path.join(lab, 'capture-meta.mjs'));
const { chromium } = createRequire(path.join(lab, '../../../apps/webui/frontend/package.json'))('playwright');
const browser = await chromium.launch({ headless: true, executablePath: chrome, args: ['--autoplay-policy=no-user-gesture-required', ...args] });
try {
  console.log(JSON.stringify({ first: await audioOutputIdentity(browser), again: await audioOutputIdentity(browser) }));
} finally {
  await browser.close();
}
"""


def _real_audio_output(*args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """audioOutputIdentity of a real Chromium launched with args under env
    (the sound server it reaches), read twice."""
    out = _node_json(_AUDIO_OUTPUT, str(LAB), _chromium(), *args, env=env)
    assert out["again"] == out["first"], out  # stable fields only: two reads of one output agree
    return dict(out["first"])


def test_compare_across_audio_outputs_is_unknown(tmp_path: Path) -> None:
    """Codex's cases, on a real PulseAudio server with two null sinks of one
    format, which real Chromium plays through. Two launches on the default
    sink have one identity, device included (control). Switching the OS
    default to the other sink changes nothing Chromium's AudioContext reports
    (same rate, channels and latencies, sinkId still ''), and the device
    field names it: the ledger refuses the pair as UNKNOWN, and two captures
    on the new sink compare and the delta is judged (control). Another
    output format on one device is named too, and a capture that records no
    output is UNKNOWN."""
    with pulse_server() as env:
        on_a, again = _real_audio_output(env=env), _real_audio_output(env=env)
        assert on_a == again and on_a["device"].startswith("pulse:perflab_a "), on_a
        assert on_a["device_unknown"] is None
        set_default_sink(env, "perflab_b")
        on_b = _real_audio_output(env=env)
    assert {k for k in on_a if on_a[k] != on_b[k]} == {"device"}, (on_a, on_b)
    assert on_b["device"].startswith("pulse:perflab_b ")
    assert set(on_a) == {
        "sink_id",
        "sample_rate",
        "channels",
        "base_latency_ms",
        "device",
        "device_unknown",
        "device_reader",
    }
    led = _Ledger(tmp_path)
    assert led.add("r1", audio_output=on_a).returncode == 0
    assert led.add("r2", 80.0, audio_output=on_b).returncode == 0
    out = led.compare()
    assert out.returncode == 3, out.stdout
    assert "audio outputs differ in device (device pulse:perflab_a" in out.stdout, out.stdout
    assert "not by the app" in out.stdout
    assert led.add("r3", 80.0, audio_output=on_b).returncode == 0
    out = led.compare()  # control: one output identity compares, and the delta is judged
    assert out.returncode == 0, out.stdout
    # another ALSA output renegotiates rate and latencies (real values from hw:9 in this container)
    other = {**on_b, "sample_rate": 48000, "base_latency_ms": 42.667}
    assert led.add("r4", 80.0, audio_output=other).returncode == 0
    out = led.compare()
    assert out.returncode == 3, out.stdout
    assert "audio outputs differ in base_latency_ms, sample_rate" in out.stdout, out.stdout
    d = tmp_path / "nooutput"
    d.mkdir()
    led = _Ledger(d)
    assert led.add("r1", audio_output=on_a).returncode == 0
    assert led.add("r2", audio_output=None).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "second capture records no audio output identity" in out.stdout, out.stdout


def test_two_undeterminable_output_devices_never_compare(tmp_path: Path) -> None:
    """Negative control, fail closed: real Chromium with no sound server that
    it or pactl can reach (XDG_RUNTIME_DIR pointing at an empty directory)
    still plays through its fallback output, but the device behind it cannot
    be read. Two such captures have identical identities and still compare
    UNKNOWN, naming why: two unknowns are never one output."""
    empty = tmp_path / "no-sound-server"
    empty.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k != "PULSE_SERVER"}
    env["XDG_RUNTIME_DIR"] = str(empty)
    first, second = _real_audio_output(env=env), _real_audio_output(env=env)
    assert first == second and first["device"] is None and first["device_unknown"], first
    led = _Ledger(tmp_path)
    assert led.add("r1", audio_output=first).returncode == 0
    assert led.add("r2", 80.0, audio_output=second).returncode == 0
    out = led.compare()
    assert out.returncode == 3, out.stdout
    assert "first capture's audio output device is undeterminable (" in out.stdout, out.stdout


def test_the_driver_reads_its_identity_at_launch_and_again_at_the_end() -> None:
    """Source pin (the driver needs a live stack to run): graphics and audio
    output are read before the measured context exists, and both are read
    again through the shared identityDrift before the browser closes; any
    drift voids the capture. identityDrift itself is tested on real
    Chromium in test_perflab_stack_ab.py."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    assert (
        src.index("const graphics = await graphicsIdentity(browser);")
        < src.index("const audioOutput = await audioOutputIdentity(browser);")
        < src.index("const context = await browser.newContext(")
    )
    tail = src[src.index("meta.audio_output = audioOutput;") :]
    assert "const launchDrift = await identityDrift(browser, { graphics, audio_output: audioOutput });" in tail
    assert "meta.errors.push(`capture: ${launchDrift}`);" in tail and "meta.invalid ??= launchDrift;" in tail
    assert tail.index("identityDrift(browser") < tail.index("await browser.close()")


def test_compare_across_graphics_stacks_is_unknown(tmp_path: Path) -> None:
    """Two machines of one host class with another GPU, driver or GL backend
    (or a browser fallen back to software) paint at different cost, so a
    delta between them says nothing about the app: UNKNOWN, naming what
    differs. A capture that records no graphics identity is UNKNOWN too."""
    led = _Ledger(tmp_path)
    assert led.add("r1").returncode == 0
    swift = {
        **GRAPHICS,
        "gl_renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver-5.0.0)",
        "gl_implementation": "(gl=egl-angle,angle=swiftshader)",
    }
    assert led.add("r2", 80.0, graphics=swift).returncode == 0
    out = led.compare()
    assert out.returncode == 3, out.stdout
    assert "graphics stacks differ in gl_implementation, gl_renderer" in out.stdout and "SwiftShader" in out.stdout
    driver = {**GRAPHICS, "devices": [{**GRAPHICS["devices"][0], "driver": "Apple 2.0"}]}
    assert led.add("r3", 80.0, graphics=driver).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "graphics stacks differ in devices" in out.stdout, out.stdout
    assert led.add("r4", 80.0, graphics=driver).returncode == 0
    out = led.compare()  # control: one graphics identity compares, and the delta is judged
    assert out.returncode == 0, out.stdout
    d = tmp_path / "nographics"
    d.mkdir()
    led = _Ledger(d)
    assert led.add("r1").returncode == 0
    assert led.add("r2", graphics=None).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "second capture records no graphics identity" in out.stdout, out.stdout


_READER = """
const [url, decoyPath, ...platforms] = process.argv.slice(1);
const { defaultOutputDevice } = await import(url);
const env = { ...process.env };
const out = {};
for (const p of platforms) out[p] = defaultOutputDevice(env, p);
// the same process, with a decoy pactl now first on PATH
out.after_decoy = defaultOutputDevice({ ...env, PATH: `${decoyPath}:${env.PATH}` }, 'linux');
console.log(JSON.stringify(out));
"""


def _decoy_pactl(tmp_path: Path) -> Path:
    """A real executable named pactl that answers as no real server would."""
    bin_dir = tmp_path / "decoy-bin"
    bin_dir.mkdir()
    decoy = bin_dir / "pactl"
    decoy.write_text("#!/bin/sh\necho decoy-sink\n", encoding="utf-8", newline="\n")
    decoy.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(os.name == "nt", reason="the decoy pactl is a POSIX shell script")
def test_the_output_reader_is_launched_by_its_pinned_resolved_path(tmp_path: Path) -> None:
    """The real pactl, on a real PulseAudio server, is resolved to its real
    path, recorded as device_reader and launched by that path. Once pinned,
    a decoy pactl put first on PATH later in the same process does not run:
    the device and reader are unchanged. Control: a fresh process whose PATH
    has the decoy first resolves the decoy, which then really runs (its
    answer names a sink the server never had), so the pin, not PATH order,
    is what kept the real one."""
    decoy_dir = _decoy_pactl(tmp_path)
    real = shutil.which("pactl")
    with pulse_server() as env:
        pinned = _node_json(_READER, (LAB / "capture-meta.mjs").as_uri(), str(decoy_dir), "linux", env=env)
        fresh = _node_json(
            _READER,
            (LAB / "capture-meta.mjs").as_uri(),
            str(decoy_dir),
            "linux",
            env={**env, "PATH": f"{decoy_dir}{os.pathsep}{env['PATH']}"},
        )
    assert real is not None
    first = pinned["linux"]
    assert first["device"].startswith("pulse:perflab_a ") and first["device_reader"] == str(Path(real).resolve()), first
    assert pinned["after_decoy"] == first, pinned
    decoy = str((decoy_dir / "pactl").resolve())
    assert fresh["linux"] == {
        "device": None,
        "device_unknown": "pactl's default sink decoy-sink is not among its sinks",
        "device_reader": decoy,
    }, fresh


def test_the_windows_seam_is_selected_on_win32_and_fails_closed_without_powershell(tmp_path: Path) -> None:
    """The Windows reader cannot run in this Linux container, so this tests
    what can be tested for real: on win32 the seam looks for PowerShell (by
    PATHEXT, as Windows does) and, with none on PATH, records no device and
    names why, never a guess; the Linux arm under the same empty PATH names
    its own reader (control: the platform picks the arm). The C# it compiles
    asks Core Audio for the default render endpoint and its friendly name."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    out = _node_json(
        _READER, (LAB / "capture-meta.mjs").as_uri(), str(empty), "win32", "linux", "aix", env={"PATH": str(empty)}
    )
    assert out["win32"] == {
        "device": None,
        "device_unknown": "powershell is not on PATH as an executable file",
        "device_reader": None,
    }, out
    assert out["linux"]["device_unknown"] == "pactl is not on PATH as an executable file", out
    assert out["aix"]["device_unknown"] == "no default-output reader on aix", out
    found = _node_json(
        "const { resolveExecutable } = await import(process.argv[1]);"
        "console.log(JSON.stringify({ hit: resolveExecutable('powershell', { Path: process.argv[2], PATHEXT: '.COM;.EXE' }, 'win32'),"
        " bare: resolveExecutable('powershell', { PATH: process.argv[2] }, 'linux') }));",
        (LAB / "resolve-exe.mjs").as_uri(),
        str(empty),
    )
    assert found == {"hit": None, "bare": None}  # nothing there yet
    (empty / "powershell.EXE").write_bytes(b"MZ")
    found = _node_json(
        "const { resolveExecutable } = await import(process.argv[1]);"
        "console.log(JSON.stringify({ hit: resolveExecutable('powershell', { Path: process.argv[2], PATHEXT: '.COM;.EXE' }, 'win32') }));",
        (LAB / "resolve-exe.mjs").as_uri(),
        str(empty),
    )
    assert found == {"hit": str((empty / "powershell.EXE").resolve())}, found
    src = (LAB / "capture-meta.mjs").read_text(encoding="utf-8")
    win = src[src.index("if (platform === 'win32') {") :]
    win = win[: win.index("return known(")]
    assert "reader = pinnedExecutable('powershell', env, platform);" in win
    assert "'-EncodedCommand', encoded" in win and "Buffer.from(WINDOWS_DEFAULT_OUTPUT, 'utf16le')" in win
    script = src[src.index("const WINDOWS_DEFAULT_OUTPUT = `") : src.index("/** The concrete output device")]
    assert "GetDefaultAudioEndpoint(0, 0, out d)" in script  # eRender, eConsole
    assert 'new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), pid = 14' in script  # PKEY_Device_FriendlyName


# The Core Audio interfaces the Windows seam declares, each with the IID and
# the vtable slots (after IUnknown) that mmdeviceapi.h / propsys.h give it, up
# to the last one the script calls. A slot out of order calls the wrong method.
_WINDOWS_COM_INTERFACES = {
    "IOdjMMDeviceEnumerator": (
        "A95664D2-9614-4F35-A746-DE8DB63617E6",
        ["EnumAudioEndpoints", "GetDefaultAudioEndpoint"],
    ),
    "IOdjMMDevice": ("D666063F-1587-4E43-81F1-B948E807363F", ["Activate", "OpenPropertyStore", "GetId"]),
    "IOdjPropertyStore": ("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99", ["GetCount", "GetAt", "GetValue"]),
}


def test_every_windows_com_interface_is_imported_with_its_iid_and_sdk_slot_order() -> None:
    # Static: this container has no Windows to run the PowerShell against. A
    # C# interface without [ComImport] is a managed interface the COM object
    # does not implement, so the cast throws and the device is never read.
    import re

    src = (LAB / "capture-meta.mjs").read_text(encoding="utf-8")
    script = src[src.index("const WINDOWS_DEFAULT_OUTPUT = `") : src.index("/** The concrete output device")]
    assert '[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class OdjMMDeviceEnumerator {}' in script
    declared = re.findall(r"\[([^\]]*)\]\s*interface (\w+) \{([^}]*)\}", script)
    assert sorted(name for _, name, _ in declared) == sorted(_WINDOWS_COM_INTERFACES), declared
    for attrs, name, body in declared:
        iid, slots = _WINDOWS_COM_INTERFACES[name]
        parts = [p.strip() for p in re.split(r",\s*(?![^()]*\))", attrs)]
        assert parts == [
            "ComImport",
            f'Guid("{iid}")',
            "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)",
        ], (name, parts)
        assert re.findall(r"\bint (\w+)\(", body) == slots, (name, body)


_JS_ENV_DEPS = """
import path from 'node:path';
import { createRequire } from 'node:module';
const [url, frontend] = process.argv.slice(1);
const { jsMeasurementDeps } = await import(url);
console.log(JSON.stringify(jsMeasurementDeps(createRequire(path.join(frontend, 'package.json')))));
"""
_PY_ENV_DEPS = "import json, sys; sys.path.insert(0, sys.argv[1]); import manifest; print(json.dumps(manifest.measurement_deps(('psutil',))))"


def _instrument_deps(env: dict[str, str]) -> dict[str, dict]:
    """The measurement_deps a real node driver and a real Python sampler
    record when each is started under exactly env."""
    driver = _node_json(_JS_ENV_DEPS, (LAB / "harness.mjs").as_uri(), str(REPO / "apps/webui/frontend"), env=env)
    r = subprocess.run(
        [sys.executable, "-c", _PY_ENV_DEPS, str(LAB)], capture_output=True, text=True, check=True, timeout=120, env=env
    )
    sampler = json.loads(r.stdout.strip().splitlines()[-1])
    return {"driver": driver, "sampler": sampler, "analysis": sampler}


def test_instruments_record_their_behavior_env_so_another_node_options_never_compares(tmp_path: Path) -> None:
    """Codex's case: the driver, sampler and analyzer inherit the launching
    shell. Real node and Python processes started under explicit envs record
    the behavior-changing part (NODE_OPTIONS, MALLOC_*, PYTHON*), never the
    locators (PATH, HOME, PWD, TMPDIR); the ledger folds it into the harness,
    so another NODE_OPTIONS is UNKNOWN while another PWD and TMPDIR compare."""
    # LANG set: with no locale at all Python coerces C to C.UTF-8 (PEP 538) and
    # sets LC_CTYPE in its own environment, which it then records (as it should)
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PWD": "/one",
        "TMPDIR": str(tmp_path / "t1"),
        "LANG": "C.UTF-8",
    }
    if os.name == "nt":
        base.update({k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ})
    plain = _instrument_deps(base)
    tuned_env = {**base, "NODE_OPTIONS": "--max-semi-space-size=64", "MALLOC_ARENA_MAX": "2", "PYTHONHASHSEED": "7"}
    tuned = _instrument_deps(tuned_env)
    moved = _instrument_deps({**tuned_env, "PWD": "/two", "TMPDIR": str(tmp_path / "t2"), "HOME": str(tmp_path / "h")})
    want = {
        "LANG": "C.UTF-8",
        "MALLOC_ARENA_MAX": "2",
        "NODE_OPTIONS": "--max-semi-space-size=64",
        "PYTHONHASHSEED": "7",
    }
    # one rule in both languages, applied to the env the process really got
    assert tuned["driver"]["instrument_env"] == tuned["sampler"]["instrument_env"] == want, tuned
    assert plain["driver"]["instrument_env"] == plain["sampler"]["instrument_env"] == {"LANG": "C.UTF-8"}, plain
    assert moved["driver"]["instrument_env"] == want == moved["sampler"]["instrument_env"], moved
    led = _Ledger(tmp_path)
    assert led.add("r1", deps=plain).returncode == 0
    assert led.add("r2", 80.0, deps=tuned).returncode == 0
    out = led.compare()
    assert out.returncode == 3 and "measured by different harness code or libraries" in out.stdout, out.stdout
    # control, and the overshoot: only locators moved, so the captures compare and the delta is judged
    assert led.add("r3", 80.0, deps=moved).returncode == 0
    out = led.compare()
    assert out.returncode == 0, out.stdout


def test_abwin_launched_through_harness_env_records_no_runtime_setting() -> None:
    """ab.py launches abwin under envs.harness_env: a real node started under
    harness_env of a tuned shell records an empty instrument_env and still
    finds its programs; the same launch under the shell itself records the
    tuning (control)."""
    envs = _load("envs")
    shell = {**os.environ, "NODE_OPTIONS": "--max-semi-space-size=64", "LD_BIND_NOW": "1", "MALLOC_ARENA_MAX": "2"}
    env = envs.harness_env(shell)
    assert env["PATH"] == os.environ["PATH"] and not envs.instrument_env(env), env
    deps = _node_json(_JS_ENV_DEPS, (LAB / "harness.mjs").as_uri(), str(REPO / "apps/webui/frontend"), env=env)
    assert deps["instrument_env"] == {}, deps
    deps = _node_json(_JS_ENV_DEPS, (LAB / "harness.mjs").as_uri(), str(REPO / "apps/webui/frontend"), env=shell)
    assert deps["instrument_env"]["NODE_OPTIONS"] == "--max-semi-space-size=64", deps
    src = (LAB / "ab.py").read_text(encoding="utf-8")
    run = src[src.index('with open(a.out / "abwin.log"') :]
    run = run[: run.index(".returncode")]
    assert "env=harness_env(os.environ)" in run, run  # the launch that matters takes the builder


_JS_RULE = """
const h = await import(process.argv[1]);
console.log(JSON.stringify({ names: [...h.INSTRUMENT_ENV_NAMES], prefixes: h.INSTRUMENT_ENV_PREFIXES,
  skip: [...h.INSTRUMENT_ENV_NOT_RECORDED], probe: h.instrumentEnv(JSON.parse(process.argv[2])) }));
"""


def test_the_node_and_python_instruments_record_by_one_rule() -> None:
    """harness.mjs mirrors envs.py's recorded names, prefixes and exclusions
    exactly, and real node and Python agree on a probe holding one variable
    per name and prefix, every exclusion, and every builder's locator."""
    envs = _load("envs")
    probe = {n: "1" for n in envs.INSTRUMENT_RECORDED}
    probe |= {f"{p}X": "1" for p in envs.INSTRUMENT_RECORDED_PREFIXES}
    probe |= {n: "1" for n in envs.INSTRUMENT_NOT_RECORDED | envs.HARNESS_INHERITED | envs.INSTALL_INHERITED}
    probe |= {
        "PWD": "/x",
        "OLDPWD": "/y",
        "SHLVL": "2",
        "TERM": "xterm",
        "VIRTUAL_ENV": "/v",
        "UV_RUN_RECURSION_DEPTH": "1",
    }
    js = _node_json(_JS_RULE, (LAB / "harness.mjs").as_uri(), json.dumps(probe))
    assert set(js["names"]) == set(envs.INSTRUMENT_RECORDED), js["names"]
    assert set(js["prefixes"]) == set(envs.INSTRUMENT_RECORDED_PREFIXES), js["prefixes"]
    assert set(js["skip"]) == set(envs.INSTRUMENT_NOT_RECORDED), js["skip"]
    py = envs.instrument_env(probe)
    assert js["probe"] == py and list(py) == sorted(py), (js["probe"], py)
    assert {"NODE_OPTIONS", "PYTHONPATH", "LD_PRELOAD", "MALLOCX", "LANG"} <= set(py), py
    volatile = {"PWD", "OLDPWD", "SHLVL", "TERM", "VIRTUAL_ENV", "UV_RUN_RECURSION_DEPTH", "PATH", "HOME", "TMPDIR"}
    assert not volatile & set(py) and not envs.INSTRUMENT_NOT_RECORDED & set(py), py


def test_instrument_env_refuses_a_rule_that_records_a_locator(tmp_path: Path) -> None:
    """A rule edit that would record a locator some builder passes raises
    rather than making every capture incomparable: a real copy of envs.py
    with TMPDIR added to the recorded names, loaded from its file."""
    import importlib.util

    envs = _load("envs")
    assert envs.instrument_env({"TMPDIR": "/t", "NODE_OPTIONS": "x"}) == {"NODE_OPTIONS": "x"}  # control
    src = (LAB / "envs.py").read_text(encoding="utf-8")
    edited = src.replace('    "UV_THREADPOOL_SIZE",\n', '    "UV_THREADPOOL_SIZE",\n    "TMPDIR",\n', 1)
    assert edited != src
    (tmp_path / "envs_edited.py").write_text(edited, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("perflab_envs_edited", tmp_path / "envs_edited.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(ValueError, match=r"would record locators: \['TMPDIR'\]"):
        mod.instrument_env({})


def test_a_url_capture_records_the_roots_it_is_given_so_its_sampler_scope_is_checked(tmp_path: Path) -> None:
    """Codex's case: a plain --url --commit capture has no stack state, so it
    recorded null engine and vite roots and ledger add could never check its
    sampler. A real driver.mjs run (nothing serves the url, so it stops after
    writing driver.json) records --engine-root and --vite-root, and
    ledger.sampler_scope accepts a sampler rooted at exactly those; without
    them the roots stay null and the scope is refused, as before."""
    ledger = _load("ledger")
    chrome = _chromium()  # the run launches a real browser before the page fails to load

    def capture(out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        argv = [_node(), str(LAB / "driver.mjs"), "--url", "http://127.0.0.1:9", "--commit", "abcdef1"]
        argv += ["--ids", "a", "--out", str(out), "--scenario", "leak", "--headless", "--chrome", chrome, *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)

    capture(tmp_path / "rooted", "--engine-root", "11", "--vite-root", "12")
    drv = json.loads((tmp_path / "rooted/driver.json").read_text())
    assert drv["roots"]["engine"] == 11 and drv["roots"]["vite"] == 12, drv["roots"]
    sampler = {"scope": "process-family", "roots": [11, 12, drv["roots"]["driver"]]}
    assert ledger.sampler_scope(drv, sampler) is None
    assert "foreign pid(s) [13]" in str(
        ledger.sampler_scope(drv, {**sampler, "roots": [11, 13, drv["roots"]["driver"]]})
    )
    # control: the same capture without the flags keeps null roots, which the ledger refuses
    capture(tmp_path / "bare")
    bare = json.loads((tmp_path / "bare/driver.json").read_text())
    assert bare["roots"]["engine"] is None and "records no engine, vite and driver pids" in str(
        ledger.sampler_scope(bare, sampler)
    )
    r = capture(tmp_path / "bad", "--engine-root", "x")
    assert r.returncode != 0 and "--engine-root must be a pid, got NaN" in r.stderr, r.stderr
    assert not (tmp_path / "bad").exists()


def _lab_checkout(tmp: Path) -> Path:
    """A measurement checkout: the lab copied under its package path, so a
    copied analyze.py or heapdiff.py imports its own copy of the lab."""
    root = tmp / "harness-checkout"
    (root / "scripts/perf").mkdir(parents=True)
    for init in ("scripts/__init__.py", "scripts/perf/__init__.py"):
        shutil.copy(REPO / init, root / init)
    shutil.copytree(LAB, root / "scripts/perf/lab", ignore=shutil.ignore_patterns("__pycache__"))
    return root / "scripts/perf/lab"


def _held_open(fifo: Path, argv: list[str], edit: Any, content: str) -> subprocess.CompletedProcess[str]:
    """Run argv, whose first input read is the named pipe fifo. Opening the pipe
    for writing returns only once the instrument opened it for reading, i.e.
    after it stamped its identity and before it measured; edit runs there."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=REPO)
    with fifo.open("w", encoding="utf-8") as fh:
        edit()
        fh.write(content)
    out, err = proc.communicate(timeout=120)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


_NO_FIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="holds the instrument mid-run on a POSIX named pipe")


@_NO_FIFO
@pytest.mark.parametrize("edited", [False, True])
def test_an_analyzer_whose_harness_changed_after_its_stamp_publishes_none(tmp_path: Path, edited: bool) -> None:
    """Codex's case 4284322500: the analyzer stamps the harness before it loads
    the trace (open_trace can run 600 s) and verifies it before publishing.
    The real analyze.py, from a measurement checkout, is held on its first
    input read; a lab file rewritten there gives an analysis with no harness,
    a harness_fault, INVALID and exit 1, which the ledger refuses. Control: the
    same run left alone publishes [OK] under the digest taken first."""
    manifest = _load("manifest")
    lab = _lab_checkout(tmp_path)
    started = manifest.harness_digest(lab)
    run = _sampled_run(tmp_path, "process-family")
    phases = (run / "phases.json").read_text()
    (run / "phases.json").unlink()
    os.mkfifo(run / "phases.json")

    def edit() -> None:
        if edited:
            (lab / "abstats.mjs").write_text((lab / "abstats.mjs").read_text() + "\n// replaced mid-run\n")

    r = _held_open(run / "phases.json", [sys.executable, str(lab / "analyze.py"), str(run)], edit, phases)
    a = json.loads((run / "analysis.json").read_text())
    if not edited:
        assert r.returncode == 0 and "[OK] PCI" in r.stdout, r.stderr
        assert a["harness"] == started and a["measurement_deps"]["perfetto"] and "harness_fault" not in a, a
        return
    moved = manifest.harness_digest(lab)
    why = f"the lab code changed while the instrument ran ({started[:12]} -> {moved[:12]})"
    assert r.returncode == 1 and "[OK]" not in r.stdout and why in r.stderr, (r.stdout, r.stderr)
    assert a["harness"] is None and a["measurement_deps"] is None and a["harness_fault"] == why, a
    assert a["invalid"] == why
    ident, refused = _load("ledger").capture_harness({"harness": started}, {"harness": started}, a)
    assert ident is None and "analysis.json records no harness digest" in str(refused)


_SNAP = {
    "snapshot": {"meta": {"node_fields": ["type", "name", "id", "self_size"], "node_types": [["object"]]}},
    "nodes": [0, 0, 1, 64],
    "strings": ["PerflabPlantedLeak"],
}


@_NO_FIFO
@pytest.mark.parametrize("edited", [False, True])
def test_a_heap_diff_whose_harness_changed_after_its_stamp_is_not_written(tmp_path: Path, edited: bool) -> None:
    """The class, heapdiff.py: it stamps before reading the first snapshot and
    verifies before writing heapdiff.json; a lab file rewritten while it is
    held on snapshot A leaves no heapdiff.json (a stale one is removed too)
    and exits 3. Control: left alone it writes the diff with the first stamp,
    and the analyzer takes that diff only under its own harness."""
    manifest = _load("manifest")
    lab = _lab_checkout(tmp_path)
    started = manifest.harness_digest(lab)
    run = tmp_path / "run"
    run.mkdir()
    (run / "heapdiff.json").write_text('{"stale": true}')
    for n in "BC":
        (run / f"heap-{n}.heapsnapshot").write_text(json.dumps(_SNAP))
    os.mkfifo(run / "heap-A.heapsnapshot")

    def edit() -> None:
        if edited:
            (lab / "heapdiff.py").write_text((lab / "heapdiff.py").read_text() + "\n# replaced mid-run\n")

    r = _held_open(
        run / "heap-A.heapsnapshot", [sys.executable, str(lab / "heapdiff.py"), str(run)], edit, json.dumps(_SNAP)
    )
    if edited:
        assert r.returncode == 3 and "the lab code changed while the instrument ran" in r.stderr, (r.stdout, r.stderr)
        assert not (run / "heapdiff.json").exists()
        return
    assert r.returncode == 0, r.stderr
    heap = json.loads((run / "heapdiff.json").read_text())
    assert heap["harness"] == started and heap["measurement_deps"]["python"], heap
    analyze = _load("analyze")
    assert analyze.heap_diff(run, started) == heap
    other = analyze.heap_diff(run, "f" * 64)
    assert other["status"] == "UNKNOWN" and "other harness code" in other["reason"], other


_STAMP = """
import fs from 'node:fs';
import { createRequire } from 'node:module';
const { harnessMoved, harnessStamp } = await import(process.argv[1]);
const [lab, pkg, edit] = process.argv.slice(2);
const req = createRequire(pkg);
const first = harnessStamp(req, lab);
const { version } = req('playwright');  // loaded, and cached by require, before the edit
if (edit === 'lab') fs.appendFileSync(`${lab}/abstats.mjs`, '\\n// replaced mid-run\\n');
if (edit === 'playwright') fs.writeFileSync(req.resolve('playwright/package.json'), JSON.stringify({ name: 'playwright', version: '2.0.0' }));
console.log(JSON.stringify({ first: first.measurement_deps.playwright, loaded: version, moved: harnessMoved(first, harnessStamp(req, lab)) }));
"""


@pytest.mark.parametrize("edit", ["none", "lab", "playwright"])
def test_the_js_instruments_detect_a_harness_or_playwright_replaced_mid_run(tmp_path: Path, edit: str) -> None:
    """driver.mjs and abwin.mjs stamp with harness.mjs harnessStamp before they
    load Playwright and verify with harnessMoved before their final write. In
    a real node: a lab file or a Playwright package.json replaced after the
    first stamp is named, even though require already cached Playwright's
    package.json; control, nothing replaced reads null."""
    lab = _lab_checkout(tmp_path)
    fe = tmp_path / "frontend"
    for name, ver in (("playwright", "1.0.0"), ("playwright/node_modules/playwright-core", "1.0.0")):
        d = fe / "node_modules" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(
            json.dumps({"name": name.rsplit("/", 1)[-1], "version": ver, "main": "index.js"})
        )
        (d / "index.js").write_text("module.exports = { version: require('./package.json').version };\n")
    (fe / "package.json").write_text("{}")
    got = _node_json(_STAMP, (lab / "harness.mjs").as_uri(), str(lab), str(fe / "package.json"), edit)
    assert got["first"] == "1.0.0" and got["loaded"] == "1.0.0", got
    expect = {
        "none": None,
        "lab": "the lab code changed while the instrument ran",
        "playwright": "the instrument's libraries or environment changed while it ran: playwright",
    }[edit]
    if expect is None:
        assert got["moved"] is None, got
    else:
        assert str(got["moved"]).startswith(expect), got


@pytest.mark.parametrize(
    ("script", "playwright", "final_write"),
    [
        ("driver.mjs", "requireFromFrontend('playwright')", "writeJson('driver.json', meta)"),
        ("abwin.mjs", "require('playwright')", "result.finished_at = new Date().toISOString();"),
    ],
)
def test_each_js_instrument_stamps_before_playwright_and_verifies_before_its_final_write(
    script: str, playwright: str, final_write: str
) -> None:
    """Wiring the test above cannot reach without a browser: the stamp is taken
    before Playwright is required, the recorded identity is that stamp, and
    harnessMoved runs before the final write, which records none when it moved."""
    src = (LAB / script).read_text(encoding="utf-8")
    stamp = src.index("const startedAs = harnessStamp(")
    assert stamp < src.index(f"const {{ chromium }} = {playwright}")
    assert "harness: startedAs.harness, measurement_deps: startedAs.measurement_deps" in src
    assert "harnessDigest()" not in src and "jsMeasurementDeps(" not in src  # no second, unverified stamp
    verify = src.index("harnessMoved(startedAs, harnessStamp(")
    last = (
        src.rindex(final_write)
        if script == "driver.mjs"
        else src.rindex("fs.writeFileSync(path.join(opts.out, 'ab.json')")
    )
    assert verify < last, script
    assert "harness: null, measurement_deps: null" in src[verify:last]
