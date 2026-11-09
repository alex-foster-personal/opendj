"""requirement: SKIN-8871D, SKIN-8871E, SKIN-9565

if GET /api/v1/practice/devices does not list ddj-flx4 then broken
if GET /api/v1/practice/devices/ddj-flx4 does not return all 77 panel controls then broken
if an unknown or path-traversal device id is not a 404 then broken
if opendj practice builds a URL other than <ui>/practice/<device>[?target=] then broken
if opendj practice accepts zero or two device arguments then broken
if opendj practice --compare builds a URL other than <ui>/practice/<device>/compare[?ref=] then broken
if a --ref path that does not exist reaches the browser instead of a usage error then broken
if the compare fixture image lives outside tests/ then broken
if opendj practice --compare --trials/--answer derives trials or a report that differ from the page then broken
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.practice_cli import (
    CompareArgs,
    PracticeUsage,
    parse_compare_args,
    parse_practice_args,
    practice_url,
    resolve_ref,
    run,
)
from apps.opendj_cli.practice_compare import blind_trials, build_compare_report, mulberry32, record_answer
from apps.shared.engine_origin import EngineOrigin
from apps.webui.server.routes import practice

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(practice.router, prefix="/api/v1")
    return TestClient(app)


def test_lists_flx4(client: TestClient) -> None:
    body = client.get("/api/v1/practice/devices").json()
    assert "ddj-flx4" in body["devices"]


def test_serves_flx4_panel(client: TestClient) -> None:
    r = client.get("/api/v1/practice/devices/ddj-flx4")
    assert r.status_code == 200
    doc = r.json()
    assert doc["format"] == "opendj-panel-layout/1"
    assert len(doc["controls"]) == 77


@pytest.mark.parametrize("device", ["no-such-device", "..", "ddj-400"])
def test_unknown_device_404(client: TestClient, device: str) -> None:
    # ddj-400 has a fig layout but no panel.json: a negative control for the glob.
    assert client.get(f"/api/v1/practice/devices/{device}").status_code == 404


def test_practice_url() -> None:
    assert practice_url("http://127.0.0.1:9400/", "ddj-flx4", None) == "http://127.0.0.1:9400/practice/ddj-flx4"
    assert practice_url("http://h", "ddj-flx4", "d1-play-pause") == "http://h/practice/ddj-flx4?target=d1-play-pause"


def test_parse_args() -> None:
    assert parse_practice_args(["ddj-flx4", "--target", "d2-cue"]) == ("ddj-flx4", "d2-cue")
    for bad in ([], ["a", "b"], ["ddj-flx4", "--target"]):
        with pytest.raises(PracticeUsage):
            parse_practice_args(bad)


def test_unknown_lock_is_no_engine_not_a_crash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from apps.opendj_cli import EXIT_NO_ENGINE
    from apps.opendj_cli.practice_cli import run

    assert run(["ddj-flx4", "--json"], as_json=False, lock=tmp_path / "missing.lock") == EXIT_NO_ENGINE


def test_payload_stages_flx4_panel(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # requirement: SKIN-8871D -- the installed app serves the same layouts as a checkout.
    from scripts.build_engine_payload import PayloadBuildError, _stage_practice_panels

    root = Path(__file__).resolve().parents[2]
    staged = _stage_practice_panels(root, tmp_path / "app")
    assert "tools/deck-diagrams/devices/ddj-flx4/panel.json" in staged
    assert (tmp_path / "app" / "tools/deck-diagrams/devices/ddj-flx4/panel.json").is_file()
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    with pytest.raises(PayloadBuildError, match="no practice panel layouts"):
        _stage_practice_panels(empty, tmp_path / "app2")


def test_compare_url() -> None:
    """[if] compare URL misses /compare path or ref query [then] broken."""
    assert practice_url("http://h/", "ddj-flx4", None, compare=True) == "http://h/practice/ddj-flx4/compare"
    assert (
        practice_url("http://h/", "ddj-flx4", None, compare=True, ref="https://e/x.jpg")
        == "http://h/practice/ddj-flx4/compare?ref=https%3A%2F%2Fe%2Fx.jpg"
    )
    assert (
        practice_url("http://h/", "ddj-flx4", None, compare=True, seed=42, scale=2)
        == "http://h/practice/ddj-flx4/compare?seed=42&scale=2"
    )
    assert (
        practice_url(
            "http://h/",
            "ddj-flx4",
            None,
            compare=True,
            ref="https://e/x.jpg",
            seed=42,
            scale=2,
        )
        == "http://h/practice/ddj-flx4/compare?ref=https%3A%2F%2Fe%2Fx.jpg&seed=42&scale=2"
    )


def test_parse_compare_args() -> None:
    """[if] compare flag parsing keeps wrong tokens or misses errors [then] broken."""
    rest, args = parse_compare_args(["ddj-flx4", "--compare", "--ref", "https://x/y"])
    assert rest == ["ddj-flx4"]
    assert args == CompareArgs(compare=True, ref="https://x/y", seed=None, scale=None, trials=False, answers=())
    r2, a2 = parse_compare_args(["ddj-flx4", "--compare", "--seed", "42", "--scale", "1.5", "--trials"])
    assert r2 == ["ddj-flx4"]
    assert a2.seed == 42
    assert a2.scale == 1.5
    assert a2.trials is True
    r3, a3 = parse_compare_args(
        [
            "ddj-flx4",
            "--compare",
            "--seed",
            "7",
            "--answer",
            "t1=A:4",
        ]
    )
    assert r3 == ["ddj-flx4"]
    assert a3.answers == (("t1", "A", 4),)
    with pytest.raises(PracticeUsage, match="--ref requires --compare"):
        parse_compare_args(["ddj-flx4", "--ref", "x"])
    with pytest.raises(PracticeUsage, match="--ref needs"):
        parse_compare_args(["ddj-flx4", "--compare", "--ref"])
    with pytest.raises(PracticeUsage, match="--compare and --target"):
        parse_compare_args(["ddj-flx4", "--compare", "--target", "x", "--ref", "y"])
    with pytest.raises(PracticeUsage, match="--trials cannot be combined"):
        parse_compare_args(["ddj-flx4", "--compare", "--seed", "1", "--trials", "--answer", "t1=A:4"])
    with pytest.raises(PracticeUsage, match="--trials/--answer need --seed"):
        parse_compare_args(["ddj-flx4", "--compare", "--trials"])
    with pytest.raises(PracticeUsage, match="--trials/--answer need --seed"):
        parse_compare_args(["ddj-flx4", "--compare", "--answer", "t1=A:4"])
    with pytest.raises(PracticeUsage, match="--seed requires --compare"):
        parse_compare_args(["ddj-flx4", "--seed", "1"])
    with pytest.raises(PracticeUsage, match="rating"):
        parse_compare_args(["ddj-flx4", "--compare", "--seed", "1", "--answer", "t1=C:4"])
    with pytest.raises(PracticeUsage, match="rating"):
        parse_compare_args(["ddj-flx4", "--compare", "--seed", "1", "--answer", "t1=A:9"])


def test_resolve_ref(tmp_path: Path) -> None:
    """[if] resolve_ref mishandles URL, file, or missing path [then] broken."""
    assert resolve_ref("https://a/b") == "https://a/b"
    f = tmp_path / "snap.png"
    f.write_bytes(b"x")
    uri = resolve_ref(str(f))
    assert uri.startswith("file://") and "snap.png" in uri
    with pytest.raises(PracticeUsage):
        resolve_ref(str(tmp_path / "nope.png"))


def test_cli_prints_compare_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """[if] practice --compare does not print the compare URL with ref [then] broken."""
    from apps.opendj_cli import practice_cli

    ref_file = tmp_path / "ref.png"
    ref_file.write_bytes(b"x")

    class FakeClient:
        def practice_devices(self) -> list[str]:
            return ["ddj-flx4"]

        def ui_address(self) -> dict:
            return {"ui_url": "http://127.0.0.1:9400"}

    dummy = EngineOrigin("127.0.0.1", 9, tmp_path / "l.lock", None, None, None)
    monkeypatch.setattr(practice_cli, "resolve_verified_origin", lambda lock: dummy)
    monkeypatch.setattr(practice_cli, "EngineClient", lambda origin: FakeClient())
    monkeypatch.setattr(practice_cli.webbrowser, "open", lambda url: True)

    code = run(["ddj-flx4", "--compare", "--ref", str(ref_file)], as_json=False, lock=None)
    assert code == EXIT_CONFIRMED
    out = capsys.readouterr().out
    line = out.strip().removeprefix("opened ").strip()
    parsed = urllib.parse.urlparse(line)
    qs = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/practice/ddj-flx4/compare"
    assert urllib.parse.unquote(qs["ref"][0]) == ref_file.resolve().as_uri()

    code_j = run(["ddj-flx4", "--compare", "--ref", str(ref_file), "--json"], as_json=False, lock=None)
    assert code_j == EXIT_CONFIRMED
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc["opened"]
    parsed_j = urllib.parse.urlparse(doc["url"])
    qs_j = urllib.parse.parse_qs(parsed_j.query)
    assert urllib.parse.unquote(qs_j["ref"][0]) == ref_file.resolve().as_uri()


def test_python_twin_matches_vectors() -> None:
    vec_path = ROOT / "tests" / "skins" / "fixtures" / "practice-compare-vectors.json"
    vec = json.loads(vec_path.read_text(encoding="utf8"))
    layout = json.loads((ROOT / "tools" / "deck-diagrams" / "devices" / "ddj-flx4" / "panel.json").read_text())
    panel = layout["panel"]
    for seed_str, expected in vec["mulberry32"].items():
        seed = int(seed_str) & 0xFFFFFFFF
        rand = mulberry32(seed)
        got = [rand() for _ in range(8)]
        assert got == expected
    for seed_str, expected in vec["blind_trials"].items():
        seed = int(seed_str)
        assert blind_trials(panel, seed) == expected
    tr = blind_trials(panel, 42)
    tr = record_answer(tr, "t1", "A", 4)
    tr = record_answer(tr, "t2", "B", 2)
    got = build_compare_report(
        device="ddj-flx4",
        ref="https://example.com/x.jpg",
        seed=42,
        px_per_mm=2.0,
        trials=tr,
        recorded_at="2026-11-06T12:00:00.000Z",
    )
    assert got == vec["report"]


def test_cli_trials_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from apps.opendj_cli import practice_cli

    vec = json.loads((ROOT / "tests" / "skins" / "fixtures" / "practice-compare-vectors.json").read_text())
    layout = json.loads((ROOT / "tools" / "deck-diagrams" / "devices" / "ddj-flx4" / "panel.json").read_text())

    class FakeClient:
        def practice_devices(self) -> list[str]:
            return ["ddj-flx4"]

        def ui_address(self) -> dict:
            return {"ui_url": "http://127.0.0.1:9400"}

        def practice_layout(self, device: str) -> dict:
            assert device == "ddj-flx4"
            return layout

    dummy = EngineOrigin("127.0.0.1", 9, tmp_path / "l.lock", None, None, None)
    monkeypatch.setattr(practice_cli, "resolve_verified_origin", lambda lock: dummy)
    monkeypatch.setattr(practice_cli, "EngineClient", lambda origin: FakeClient())

    def _no_browser(_url: str) -> bool:
        raise RuntimeError("no browser")

    monkeypatch.setattr(practice_cli.webbrowser, "open", _no_browser)

    assert (
        practice_cli.run(["ddj-flx4", "--compare", "--trials", "--seed", "42"], as_json=False, lock=None)
        == EXIT_CONFIRMED
    )
    out = json.loads(capsys.readouterr().out.strip())
    assert out["device"] == "ddj-flx4"
    assert out["seed"] == 42
    assert out["trials"] == vec["blind_trials"]["42"]

    assert (
        practice_cli.run(
            [
                "ddj-flx4",
                "--compare",
                "--ref",
                "https://example.com/x.jpg",
                "--seed",
                "42",
                "--answer",
                "t1=A:4",
                "--answer",
                "t2=B:2",
            ],
            as_json=False,
            lock=None,
        )
        == EXIT_CONFIRMED
    )
    rep = json.loads(capsys.readouterr().out.strip())
    expected = dict(vec["report"])
    assert rep.keys() == expected.keys()
    for k, v in expected.items():
        if k == "recorded_at":
            assert k in rep and rep[k] != v
        else:
            assert rep[k] == v

    assert (
        practice_cli.run(
            ["ddj-flx4", "--compare", "--seed", "42", "--answer", "t9=A:4"],
            as_json=False,
            lock=None,
        )
        == EXIT_FAILED
    )


def test_compare_fixture_lives_under_tests() -> None:
    """[if] compare PNG is absent, wrong size, or under apps/ routes [then] broken."""
    fixture = ROOT / "tests" / "skins" / "fixtures" / "practice-compare-965x545.png"
    assert fixture.is_file()
    assert (fixture.relative_to(ROOT / "tests")).parts[0] == "skins"
    assert Image.open(fixture).size == (965, 545)
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    r = subprocess.run(
        [
            git,
            "ls-files",
            "apps/webui/frontend/src/routes/practice",
            "apps/webui/frontend/src/lib/components/rb/practice",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    bad = [line for line in r.stdout.splitlines() if line.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    assert bad == [], bad


def test_practice_verb_only_at_command_position() -> None:
    # requirement: SKIN-8871E -- a later "practice" token (a title, a comment) is not the verb.
    from apps.opendj_cli.__main__ import _command_position

    assert _command_position(["practice", "ddj-flx4"]) == 0
    assert _command_position(["--lock", "/tmp/x.lock", "practice", "ddj-flx4"]) == 2
    assert _command_position(["--json", "practice", "ddj-flx4"]) == 1
    tokens = ["track", "comment", "abc", "practice"]
    assert tokens[_command_position(tokens)] == "track"
