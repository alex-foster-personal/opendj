"""Both Modal routes refuse what the farm cannot encode BEFORE a GPU is bought (STEM-75).

[if] a Modal route asks for another codec than route_stem_codec(tier) [then] fail, [else stop].
[if] a by-source tier is handed a source with no output-format policy [then]
  the route refuses it before ``.remote()``/``.starmap()`` [⛔️ if dispatched].
[if] a route dispatches without running the farm's mirror guard [then] fail.
[if] the farm CLI's own batch holds a source with no policy under by-source [then]
  it is dropped and named before ``app.run()``; a FLAC run keeps every source, [else stop].
[if] a bundle is republished in another codec [then] only the new manifest's part
  files remain, and files that are not stem parts are kept, [else stop].

Everything here runs the REAL code: ``apps.stems.bundle_publish``, the real
``scripts.stems_relay_app.modal_backend``, the real
``scripts.stems_modal_worker.modal_separator`` and the real
``scripts.modal_vocal_farm``. Nothing is replaced, patched or recorded.

Where the routes stop. The route tests run each route in a CHILD process
whose environment holds no Modal credential (no ``MODAL_TOKEN_*``, ``HOME``
and ``MODAL_CONFIG_PATH`` pointed at an empty directory), because modal reads
``~/.modal.toml`` once at import. That makes the first network step fail
closed at the true vendor boundary:

  - relay: ``farm.app.run()`` raises modal's ``AuthError`` ("Token missing")
    without opening a connection, so no GPU can ever be bought, even if a
    mutation removed every guard in front of it;
  - direct: ``assert_modal_credentials()`` exits 2, the last step before
    ``farm.app.run()``.

Reaching that boundary is the PROOF that a supported source is dispatched;
being refused before it is the proof that an unsupported one is not. What
these tests do NOT cover: the container side (``_analyse``'s own defensive
``resolve_stem_codec``) and a real separation, which need Modal credentials
and a GPU; ``tests/stems/test_farm_mp3_ladder.py`` covers the in-container
resolver as a real function.

The audio handed to each route is a real fixture of the extension it is named
for (``tests/fixtures/phase7-dedup``, ``tests/fixtures/adts``). Nothing
decodes it before the boundary; it is real so that nothing here is fabricated.

-Claude
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.stems import bundle_publish, stem_size_policy
from apps.stems.stem_size_policy import UnknownSourceFormatError
from apps.stems.tiers import get_tier, modal_tiers

pytestmark = pytest.mark.requirement("STEM-75")

REPO = Path(__file__).resolve().parents[2]
FARM = REPO / "scripts" / "modal_vocal_farm.py"
FIXTURES = {
    ".mp3": REPO / "tests/fixtures/phase7-dedup/src-128.mp3",
    ".m4a": REPO / "tests/fixtures/phase7-dedup/src.m4a",
    ".flac": REPO / "tests/fixtures/phase7-dedup/src.flac",
    ".wav": REPO / "tests/fixtures/phase7-dedup/src.wav",
    ".aac": REPO / "tests/fixtures/adts/src.aac",  # no output-format policy
}
SUPPORTED = sorted(stem_size_policy.LOSSLESS_SOURCE_EXTS | stem_size_policy.LOSSY_SOURCE_EXTS)
UNSUPPORTED = [".ogg", ".aac", ".opus", ".wma", ""]
BY_SOURCE_TIERS = [t.key for t in modal_tiers() if t.codec != "flac"]
FLAC_TIERS = [t.key for t in modal_tiers() if t.codec == "flac"]
SID = "a" * 40


def test_the_tier_split_is_not_empty() -> None:
    """Control: every parametrization below needs both kinds of tier."""
    assert BY_SOURCE_TIERS, "no by-source Modal tier: the refusal tests test nothing"
    assert FLAC_TIERS, "no FLAC Modal tier: the pass-through tests test nothing"
    assert ".aac" not in SUPPORTED and ".mp3" in SUPPORTED


# ----- the shared pre-dispatch helper (modal-free) ---------------------------


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
@pytest.mark.parametrize("ext", SUPPORTED)
def test_a_by_source_tier_accepts_every_source_with_a_policy(tier: str, ext: str) -> None:
    assert bundle_publish.route_stem_codec_for_source(tier, f"Track{ext.upper()}") == (
        bundle_publish.STEM_CODEC_BY_SOURCE
    )


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
@pytest.mark.parametrize("ext", UNSUPPORTED)
def test_a_by_source_tier_refuses_a_source_with_no_policy(tier: str, ext: str) -> None:
    with pytest.raises(UnknownSourceFormatError, match="no output-format policy"):
        bundle_publish.route_stem_codec_for_source(tier, f"track{ext}")


@pytest.mark.parametrize("tier", FLAC_TIERS)
@pytest.mark.parametrize("ext", [*SUPPORTED, *UNSUPPORTED])
def test_a_flac_tier_takes_any_source(tier: str, ext: str) -> None:
    """The overshoot control: FLAC is asked for explicitly, so no extension
    policy applies and refusing here would block a tier that works."""
    assert bundle_publish.route_stem_codec_for_source(tier, f"track{ext}") == "flac"


# ----- the arguments both routes hand separate_track (modal-free) -------------


def _farm_separate_track_params() -> list[str]:
    tree = ast.parse(FARM.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "separate_track":
            return [arg.arg for arg in node.args.args]
    raise AssertionError(f"separate_track not found in {FARM}")


def test_the_argument_mirror_names_the_farm_signature() -> None:
    assert tuple(_farm_separate_track_params()) == bundle_publish.SEPARATE_TRACK_PARAMS


@pytest.mark.parametrize("tier", [t.key for t in modal_tiers()])
def test_the_route_arguments_carry_the_tier_rung_and_codec(tier: str) -> None:
    preset = bundle_publish.preset_for_tier(tier)
    codec = bundle_publish.route_stem_codec_for_source(tier, "a.mp3")
    args = bundle_publish.separate_track_args(
        audio_bytes=FIXTURES[".mp3"].read_bytes(),
        stable_id=SID,
        name="a.mp3",
        source_path="/music/a.mp3",
        preset=preset,
        stem_codec=codec,
    )
    bound = dict(zip(_farm_separate_track_params(), args, strict=True))

    assert bound["stem_codec"] == ("flac" if get_tier(tier).codec == "flac" else "by-source")
    assert (bound["model_name"], bound["overlap"], bound["shifts"]) == (
        get_tier(tier).model,
        get_tier(tier).overlap,
        get_tier(tier).shifts,
    )
    assert bound["preset_stamp"] == preset.stamp()
    assert bound["stems_dest"] == "local"
    assert (bound["stable_id"], bound["name"], bound["source_path"]) == (
        SID,
        "a.mp3",
        "/music/a.mp3",
    )


def _expected_codec(tier: str) -> str:
    return "flac" if get_tier(tier).codec == "flac" else "by-source"


@pytest.mark.parametrize("tier", [t.key for t in modal_tiers()])
@pytest.mark.parametrize("ext", [".mp3", ".flac"])
def test_the_relay_route_arguments(tier: str, ext: str) -> None:
    from scripts.stems_relay_app import relay_route_args

    audio = FIXTURES[ext].read_bytes()
    preset = bundle_publish.preset_for_tier(tier)
    args = relay_route_args(audio, SID, f"track{ext}", tier, preset)
    bound = dict(zip(_farm_separate_track_params(), args, strict=True))

    assert bound["stem_codec"] == _expected_codec(tier)
    assert bound["audio_bytes"] == audio
    assert (bound["name"], bound["source_path"]) == (f"track{ext}", f"track{ext}")
    assert bound["model_name"] == get_tier(tier).model


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
def test_the_relay_route_arguments_refuse_a_source_with_no_policy(tier: str) -> None:
    from scripts.stems_relay_app import relay_route_args

    with pytest.raises(UnknownSourceFormatError, match=r"'\.aac'"):
        relay_route_args(
            FIXTURES[".aac"].read_bytes(),
            SID,
            "track.aac",
            tier,
            bundle_publish.preset_for_tier(tier),
        )


@pytest.mark.parametrize("tier", [t.key for t in modal_tiers()])
def test_the_direct_route_arguments(tier: str) -> None:
    from scripts.stems_modal_worker import direct_route_args, split_by_source_policy

    job = _job(".mp3")
    [(track, codec)], [] = split_by_source_policy([job], tier)
    args = direct_route_args(track, bundle_publish.preset_for_tier(tier), codec)
    bound = dict(zip(_farm_separate_track_params(), args, strict=True))

    assert bound["stem_codec"] == _expected_codec(tier)
    assert bound["audio_bytes"] == job.audio_path.read_bytes()
    assert (bound["name"], bound["source_path"]) == (job.audio_path.name, str(job.audio_path))
    assert bound["model_name"] == get_tier(tier).model


# ----- the direct worker's split (modal-free) ---------------------------------


def _job(ext: str, n: int = 0) -> Any:
    from scripts.stems_modal_worker import TrackJob

    path = FIXTURES[ext]
    return TrackJob(
        stable_id=f"{ext.strip('.')}{n}".ljust(40, "0"),
        audio_path=path,
        duration_ms=0,
        size_bytes=path.stat().st_size,
    )


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
def test_the_direct_worker_splits_off_a_source_with_no_policy(tier: str) -> None:
    from scripts.stems_modal_worker import split_by_source_policy

    jobs = [_job(".mp3"), _job(".aac"), _job(".flac"), _job(".wav"), _job(".m4a")]
    dispatchable, rejected = split_by_source_policy(jobs, tier)

    assert [t.stable_id for t, _ in dispatchable] == [j.stable_id for j in jobs if j.audio_path.suffix != ".aac"]
    assert {codec for _, codec in dispatchable} == {"by-source"}
    assert [r["stable_id"] for r in rejected] == [jobs[1].stable_id]
    assert "refused before dispatch" in rejected[0]["error"]
    assert "'.aac'" in rejected[0]["error"]


@pytest.mark.parametrize("tier", FLAC_TIERS)
def test_the_direct_worker_refuses_nothing_on_a_flac_tier(tier: str) -> None:
    from scripts.stems_modal_worker import split_by_source_policy

    jobs = [_job(".aac"), _job(".mp3")]
    dispatchable, rejected = split_by_source_policy(jobs, tier)

    assert rejected == []
    assert [(t.stable_id, c) for t, c in dispatchable] == [(j.stable_id, "flac") for j in jobs]


# ----- the real routes, up to the Modal boundary (needs modal) -----------------

_CHILD = r"""
import json, sys
from pathlib import Path

route, tier, payload = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
out = {"yielded": []}
try:
    if route == "relay":
        from scripts.stems_relay_app import modal_backend

        name = payload[0]
        modal_backend(Path(payload[1]).read_bytes(), "a" * 40, name, tier)
        out["outcome"] = "returned"
    else:
        from scripts.stems_modal_worker import TrackJob, modal_separator

        jobs = [
            TrackJob(stable_id=sid, audio_path=Path(p), duration_ms=0,
                     size_bytes=Path(p).stat().st_size)
            for sid, p in payload
        ]
        for result in modal_separator(jobs, tier):
            out["yielded"].append(result)
        out["outcome"] = "exhausted"
except BaseException as exc:
    out["outcome"] = type(exc).__name__
    out["code"] = getattr(exc, "code", None)
    out["message"] = str(exc)[:300]
farm = sys.modules.get("scripts.modal_vocal_farm")
out["guard_runs"] = (
    farm.assert_route_contract.cache_info().misses if farm is not None else 0
)
print("CHILD-RESULT " + json.dumps(out, default=str))
"""


def _require_modal() -> None:
    """Fail, never skip, when modal is missing: these are the route acceptance tests.

    CI's test venv (requirements-ci.in) and ``just test`` (``uv run --with
    modal``) both provide it, so a missing modal is a broken environment, not
    a reason to pass with the dispatch boundary unchecked (Codex on #6821).
    """
    if importlib.util.find_spec("modal") is None:
        pytest.fail("modal is not installed: run through `just test` or `uv run --with modal pytest`")


def _run_route(route: str, tier: str, payload: Any, home: Path) -> dict[str, Any]:
    _require_modal()
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODAL_")}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["MODAL_CONFIG_PATH"] = str(home / "absent-modal.toml")
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, route, tier, json.dumps(payload)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("CHILD-RESULT ")]
    assert len(lines) == 1, f"child produced no result:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[0].removeprefix("CHILD-RESULT "))


def test_the_child_has_no_modal_credential(tmp_path: Path) -> None:
    """Control for every route test: the child must be unable to buy a GPU."""
    _require_modal()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODAL_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    env["MODAL_CONFIG_PATH"] = str(tmp_path / "absent-modal.toml")
    probe = "from modal.config import config; print(config.get('token_id'), config.get('token_secret'))"
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "None None"


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
def test_the_relay_refuses_an_unsupported_source_before_the_farm(tier: str, tmp_path: Path) -> None:
    out = _run_route("relay", tier, ["track.aac", str(FIXTURES[".aac"])], tmp_path / "h")

    assert out["outcome"] == "UnknownSourceFormatError", out
    assert "'.aac'" in out["message"]
    assert out["guard_runs"] == 0  # refused before the farm was even imported


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
@pytest.mark.parametrize("ext", [".mp3", ".m4a", ".flac", ".wav"])
def test_the_relay_dispatches_a_supported_source_after_the_guard(tier: str, ext: str, tmp_path: Path) -> None:
    out = _run_route("relay", tier, [f"track{ext}", str(FIXTURES[ext])], tmp_path / "h")

    assert out["outcome"] == "AuthError", out  # reached farm.app.run(): the boundary
    assert out["guard_runs"] == 1


@pytest.mark.parametrize("tier", FLAC_TIERS)
def test_the_relay_dispatches_any_source_on_a_flac_tier(tier: str, tmp_path: Path) -> None:
    out = _run_route("relay", tier, ["track.aac", str(FIXTURES[".aac"])], tmp_path / "h")

    assert out["outcome"] == "AuthError", out
    assert out["guard_runs"] == 1


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
def test_the_direct_worker_refuses_an_unsupported_batch_without_dispatch(tier: str, tmp_path: Path) -> None:
    job = _job(".aac")
    out = _run_route("direct", tier, [[job.stable_id, str(job.audio_path)]], tmp_path / "h")

    # Exhausted cleanly: no credential check, no app.run(), nothing bought.
    assert out["outcome"] == "exhausted", out
    assert [r["stable_id"] for r in out["yielded"]] == [job.stable_id]
    assert "refused before dispatch" in out["yielded"][0]["error"]
    assert out["guard_runs"] == 1


@pytest.mark.parametrize("tier", BY_SOURCE_TIERS)
def test_the_direct_worker_dispatches_the_supported_rest(tier: str, tmp_path: Path) -> None:
    bad, good = _job(".aac"), _job(".mp3")
    payload = [[j.stable_id, str(j.audio_path)] for j in (bad, good)]
    out = _run_route("direct", tier, payload, tmp_path / "h")

    assert [r["stable_id"] for r in out["yielded"]] == [bad.stable_id]
    # The supported track went on to the credential gate in front of app.run().
    assert (out["outcome"], out["code"]) == ("SystemExit", 2), out
    assert out["guard_runs"] == 1


@pytest.mark.parametrize("tier", [t.key for t in modal_tiers()])
@pytest.mark.parametrize("ext", [".mp3", ".flac", ".wav"])
def test_the_direct_worker_dispatches_a_supported_source(tier: str, ext: str, tmp_path: Path) -> None:
    job = _job(ext)
    out = _run_route("direct", tier, [[job.stable_id, str(job.audio_path)]], tmp_path / "h")

    assert out["yielded"] == []
    assert (out["outcome"], out["code"]) == ("SystemExit", 2), out
    assert out["guard_runs"] == 1


def test_the_farm_guard_passes_on_this_tree() -> None:
    """The guard the routes now call is the real one, and this tree satisfies it."""
    _require_modal()
    import scripts.modal_vocal_farm as farm

    farm.assert_route_contract.cache_clear()
    farm.assert_route_contract()
    assert farm.assert_route_contract.cache_info().misses == 1


# ----- the farm CLI's own batch (needs modal: the farm module imports it) ------


def _farm_cli_data(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A data dir whose gap holds one source of each kind, as real fixture files."""
    import shutil
    import sqlite3

    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    ids = {".mp3": "m" * 40, ".flac": "f" * 40, ".aac": "c" * 40}
    con = sqlite3.connect(data / "state" / "state.db")
    con.execute("CREATE TABLE tracks (stable_id TEXT, file_path TEXT, duration_ms INTEGER)")
    con.execute("CREATE TABLE playlist_memberships (stable_id TEXT)")
    for ext, sid in ids.items():
        audio = tmp_path / "music" / f"track{ext}"
        audio.parent.mkdir(exist_ok=True)
        shutil.copyfile(FIXTURES[ext], audio)
        con.execute("INSERT INTO tracks VALUES (?, ?, 1000)", (sid, str(audio)))
        con.execute("INSERT INTO playlist_memberships VALUES (?)", (sid,))
    con.commit()
    con.close()
    return data, ids


def _farm_cli_dry_run(data: Path, home: Path, *extra: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    _require_modal()
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODAL_")}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(REPO))
    env["MODAL_CONFIG_PATH"] = str(home / "absent-modal.toml")
    proc = subprocess.run(
        [sys.executable, str(FARM), "--data-dir", str(data), "--stems-dest", "local", "--dry-run", *extra],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == expect, f"{proc.stdout}\n{proc.stderr}"
    return proc


def _would_farm(stdout: str) -> set[str]:
    return {ln.split()[2] for ln in stdout.splitlines() if ln.strip().startswith("would farm ")}


def test_the_farm_cli_drops_an_unsupported_source_before_dispatch(tmp_path: Path) -> None:
    """The batch ``main`` hands ``run_farm`` (and ``--dry-run`` prints) holds only
    the sources by-source can encode; the dropped one is named (Codex P2 on #6821)."""
    data, ids = _farm_cli_data(tmp_path)

    proc = _farm_cli_dry_run(data, tmp_path / "h", expect=1)
    out = proc.stdout

    assert "stem codec: by-source" in out
    assert _would_farm(out) == {ids[".mp3"], ids[".flac"]}, out
    assert f"skip {ids['.aac']} track.aac: " in out and "'.aac'" in out
    assert "gap=2 batch=2" in out
    # The skipped source is a failure, not a silent omission (Codex P2 on #6821).
    assert f"error: 1 source(s) refused by the stem codec policy and not farmed: ['{ids['.aac']}']" in proc.stderr


def test_a_run_of_only_unsupported_sources_fails_rather_than_finding_nothing_to_do(tmp_path: Path) -> None:
    """``--only-stable-id`` naming just the refused source must not exit 0 through
    "nothing to do": the caller asked for that track and got nothing (Codex P2 on #6821)."""
    data, ids = _farm_cli_data(tmp_path)

    proc = _farm_cli_dry_run(data, tmp_path / "h", "--only-stable-id", ids[".aac"], expect=1)

    assert "gap=0 batch=0" in proc.stdout
    assert "nothing to do" not in proc.stdout
    assert f"not farmed: ['{ids['.aac']}']" in proc.stderr


def test_control_a_run_of_only_supported_sources_exits_zero(tmp_path: Path) -> None:
    """Control: the refusal count is per source, so a by-source run naming only
    encodable tracks still exits 0 and farms them."""
    data, ids = _farm_cli_data(tmp_path)

    proc = _farm_cli_dry_run(data, tmp_path / "h", "--only-stable-id", ids[".mp3"], "--only-stable-id", ids[".flac"])

    assert _would_farm(proc.stdout) == {ids[".mp3"], ids[".flac"]}, proc.stdout
    assert "refused" not in proc.stderr


def test_the_farm_cli_keeps_every_source_on_a_flac_run(tmp_path: Path) -> None:
    """Control: FLAC encodes any source the decoder reads, so nothing is dropped."""
    data, ids = _farm_cli_data(tmp_path)

    out = _farm_cli_dry_run(data, tmp_path / "h", "--stem-codec", "flac").stdout

    assert _would_farm(out) == set(ids.values()), out
    assert "skip " not in out


# ----- a republish in another codec (both writers, modal-free) ----------------


def _farm_publisher() -> Any:
    from tests.stems.test_bundle_publish_mirror import _farm_function, _farm_namespace, _farm_tree

    tree = _farm_tree()
    namespace = _farm_namespace(tree)
    _farm_function(tree, "_stem_manifest", namespace)
    return _farm_function(tree, "_publish_stems_local", namespace)


def _publish_result(ext: str) -> dict[str, Any]:
    return {
        "source_sha256": "0" * 64,
        "stem_ext": ext,
        "stem_codec": ext,
        "size_policy": None,
        "audio": {"sample_rate": 44100, "frame_count": 88200, "channels": 2},
        "stems": {part: f"{part}-{ext}".encode() for part in bundle_publish.STEM_PARTS},
    }


PUBLISHERS = {"mirror": lambda: bundle_publish.publish_stems_local, "farm": _farm_publisher}


@pytest.mark.parametrize("writer", sorted(PUBLISHERS))
def test_a_flac_bundle_republished_as_mp3_keeps_only_the_manifest_files(writer: str, tmp_path: Path) -> None:
    publish = PUBLISHERS[writer]()
    preset = bundle_publish.PRESETS["htdemucs-ov0.25"]
    bundle = tmp_path / "state" / "stems" / SID
    publish(SID, Path("/music/a.mp3"), _publish_result("flac"), preset, tmp_path)
    (bundle / "notes.txt").write_text("not a stem part\n")  # an unrelated file is kept
    (bundle / "extra").mkdir()
    (bundle / "extra" / "vocals.flac").write_bytes(b"nested, not a top-level part")

    publish(SID, Path("/music/a.mp3"), _publish_result("mp3"), preset, tmp_path)

    manifest = json.loads((bundle / "manifest.json").read_text())
    top = sorted(p.name for p in bundle.iterdir() if p.is_file())
    assert top == sorted(["manifest.json", "notes.txt", *manifest["files"].values()])
    assert all(name.endswith(".mp3") for name in manifest["files"].values())
    assert (bundle / "extra" / "vocals.flac").is_file()


@pytest.mark.parametrize("writer", sorted(PUBLISHERS))
def test_a_same_codec_republish_still_replaces_every_part(writer: str, tmp_path: Path) -> None:
    """Control: the removal cannot take the parts the new manifest names."""
    publish = PUBLISHERS[writer]()
    preset = bundle_publish.PRESETS["htdemucs-ov0.25"]
    bundle = tmp_path / "state" / "stems" / SID
    publish(SID, Path("/music/a.mp3"), _publish_result("mp3"), preset, tmp_path)
    again = _publish_result("mp3")
    again["stems"] = {part: b"second " + part.encode() for part in bundle_publish.STEM_PARTS}

    publish(SID, Path("/music/a.mp3"), again, preset, tmp_path)

    for part in bundle_publish.STEM_PARTS:
        assert (bundle / f"{part}.mp3").read_bytes() == b"second " + part.encode()
    assert sorted(p.name for p in bundle.iterdir()) == sorted(
        ["manifest.json", *(f"{part}.mp3" for part in bundle_publish.STEM_PARTS)]
    )
