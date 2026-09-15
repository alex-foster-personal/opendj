"""``python -m apps.stems build-index`` / ``bulk-hydrate`` CLI dispatch (ADR-0024).

Covers the CLI surface itself (argument parsing, journal-path defaulting,
credential refusal, output) -- the library functions underneath
(``apps.cloud.stem_index``, ``apps.cloud.stem_hydration``) already have
their own dedicated tests in ``tests/cloudsync/``.
"""
from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import pytest

from apps.cloud.asset_store import AssetHead, asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_index import INDEX_OBJECT_KEY, fetch_index, load_cached_index, publish_index
from apps.cloud.stem_source import DirectR2Source
from apps.stems.cli import _default_journal_path, build_parser, main


def _wav_bytes(*, frames: int = 8, sample_rate: int = 44_100, channels: int = 2) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(b"\x00\x00" * frames * channels)
    return buf.getvalue()


def _manifest_bytes(stable_id: str) -> bytes:
    manifest = {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": "/music/x.wav", "sha256": "a" * 64},
        "files": {
            "vocals": "vocals.wav", "drums": "drums.wav",
            "bass": "bass.wav", "other": "other.wav",
        },
    }
    return (json.dumps(manifest) + "\n").encode("utf-8")


def _journal_line(*, stable_id: str, files: dict[str, str]) -> str:
    objects = [
        {
            "legacy_key": f"stems/4.0.1/{stable_id}/{filename}",
            "key": f"assets/{digest[:2]}/{digest}",
        }
        for filename, digest in files.items()
    ]
    return json.dumps({"at": "2026-09-14T00:00:00Z", "objects": objects})


class _InMemoryAssetS3:
    """Same minimal fake used by the webui hydration route tests -- this
    file should not reach into tests/cloudsync/'s fixtures, since the CLI
    surface is exercised standalone."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}
        self.get_calls: list[tuple[str, str]] = []

    def head_object(self, bucket, key):
        body = self.store.get((bucket, key))
        return None if body is None else AssetHead(size=len(body), etag="x")

    def get_object(self, bucket, key):
        self.get_calls.append((bucket, key))
        body = self.store.get((bucket, key))
        return None if body is None else (body, "x")

    def put_object_if_none_match(self, bucket, key, body):
        if not isinstance(body, bytes):
            body = body.read()
        if (bucket, key) in self.store:
            return False, None
        self.store[(bucket, key)] = body
        return True, "x"

    def put_object_if_match(self, bucket, key, body, *, etag):
        if not isinstance(body, bytes):
            body = body.read()
        self.store[(bucket, key)] = body
        return True, "y"

    def delete_object(self, bucket, key):
        return self.store.pop((bucket, key), None) is not None


def _cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct", r2_access_key_id="id", r2_secret_access_key="secret",
        state_bucket="test-state", audio_bucket="test-audio",
        hostname="host", bind_host="127.0.0.1",
    )


def _run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


# --- build-index ---------------------------------------------------------


def test_default_journal_path_matches_push_rail_convention(tmp_path: Path) -> None:
    """The CLI's default journal path must agree with the fixed path
    scripts/local_stems_to_r2.py journals to -- a drift here silently
    starves build-index of everything the push rail wrote."""
    assert _default_journal_path(tmp_path) == tmp_path / "state" / "stem-r2-migration.jsonl"


def test_build_index_reads_default_journal_and_caches_locally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    journal = _default_journal_path(data_dir)
    journal.parent.mkdir(parents=True)
    journal.write_text(
        _journal_line(stable_id="track-a", files={"manifest.json": "a" * 64}) + "\n",
        encoding="utf-8",
    )

    rc = _run(["build-index", "--data-dir", str(data_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 bundles" in out
    assert load_cached_index(data_dir) == {"track-a": {"manifest.json": "a" * 64}}


def test_build_index_honors_explicit_journal_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """MUTATION TARGET: if ``--journal`` stopped being threaded through to
    ``build_index_from_journal`` (falling back to the default path always),
    this test goes red because the default-path journal (left empty) would
    produce zero bundles instead of one."""
    data_dir = tmp_path / "data"
    custom_journal = tmp_path / "elsewhere" / "custom.jsonl"
    custom_journal.parent.mkdir(parents=True)
    custom_journal.write_text(
        _journal_line(stable_id="track-b", files={"manifest.json": "b" * 64}) + "\n",
        encoding="utf-8",
    )
    # The default-path journal is deliberately absent, so a fall-through to
    # it would produce an empty index instead of raising.

    rc = _run(["build-index", "--data-dir", str(data_dir), "--journal", str(custom_journal)])
    assert rc == 0
    assert load_cached_index(data_dir) == {"track-b": {"manifest.json": "b" * 64}}


def test_build_index_publish_writes_to_r2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    journal = _default_journal_path(data_dir)
    journal.parent.mkdir(parents=True)
    journal.write_text(
        _journal_line(stable_id="track-c", files={"manifest.json": "c" * 64}) + "\n",
        encoding="utf-8",
    )

    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    monkeypatch.setattr("apps.cloud.config.CloudConfig.from_env", staticmethod(lambda: cfg))
    monkeypatch.setattr("apps.cloud.replicate.boto3_s3_client", lambda _cfg: s3)

    rc = _run(["build-index", "--data-dir", str(data_dir), "--publish"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "published to r2://" in out
    assert fetch_index(cfg, s3) == {"track-c": {"manifest.json": "c" * 64}}


# --- bulk-hydrate ----------------------------------------------------------


def test_bulk_hydrate_requires_ids_or_playlist(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["bulk-hydrate", "--data-dir", str(tmp_path), "--budget-bytes", "1000"]
        )


def test_bulk_hydrate_rejects_both_ids_and_playlist(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "bulk-hydrate", "--data-dir", str(tmp_path), "--budget-bytes", "1000",
                "--ids", "a,b", "--playlist", "Some Playlist",
            ]
        )


def test_bulk_hydrate_without_credentials_refuses_loudly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source", lambda _data_dir: None
    )
    with pytest.raises(SystemExit, match="credentials"):
        _run(
            [
                "bulk-hydrate", "--data-dir", str(tmp_path / "data"),
                "--ids", "nowhere", "--budget-bytes", "1000",
            ]
        )


def test_bulk_hydrate_ids_fetches_and_reports(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()

    files = {
        "manifest.json": _manifest_bytes("cli-track"),
        "vocals.wav": _wav_bytes(), "drums.wav": _wav_bytes(),
        "bass.wav": _wav_bytes(), "other.wav": _wav_bytes(),
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[filename] = digest
    from apps.cloud.stem_index import save_cached_index

    save_cached_index(data_dir, {"cli-track": entry})

    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    rc = _run(
        [
            "bulk-hydrate", "--data-dir", str(data_dir),
            "--ids", "cli-track", "--budget-bytes", str(10**9),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "fetched cli-track" in out
    assert "1 fetched, 0 skipped" in out
    assert (data_dir / "state" / "stems" / "cli-track" / "manifest.json").exists()


def test_bulk_hydrate_json_output_reports_skip_reason(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stable_id with nothing in the index is a reported skip, not a
    silent no-op -- 'prints what it fetched/skipped with reasons' (D4)."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()
    from apps.cloud.stem_index import save_cached_index

    # Warm cache: skip the auto-refresh-on-empty path so an absent id is a
    # reported skip rather than a "no index in R2" refusal.
    save_cached_index(data_dir, {"other-track": {"manifest.json": "a" * 64}})
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    rc = _run(
        [
            "bulk-hydrate", "--data-dir", str(data_dir),
            "--ids", "nowhere-track", "--budget-bytes", str(10**9), "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fetched"] == []
    assert payload["skipped"][0]["stable_id"] == "nowhere-track"
    assert payload["skipped"][0]["reason"]


def test_bulk_hydrate_refresh_index_fetches_from_r2_on_empty_cache(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh machine with no local cache must pull the published R2 index
    before hydrating, not silently treat the cache as empty."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()

    files = {
        "manifest.json": _manifest_bytes("fresh-track"),
        "vocals.wav": _wav_bytes(), "drums.wav": _wav_bytes(),
        "bass.wav": _wav_bytes(), "other.wav": _wav_bytes(),
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[filename] = digest
    publish_index(cfg, s3, {"fresh-track": entry})

    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    rc = _run(
        [
            "bulk-hydrate", "--data-dir", str(data_dir),
            "--ids", "fresh-track", "--budget-bytes", str(10**9),
            "--refresh-index",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "fetched fresh-track" in out
    assert (data_dir / "state" / "stems" / "fresh-track" / "manifest.json").exists()
    assert (cfg.audio_bucket, INDEX_OBJECT_KEY) in s3.get_calls


def test_bulk_hydrate_with_warm_cache_does_not_refetch_index(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid non-empty local cache must not hit R2 for the index on every run."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _InMemoryAssetS3()

    files = {
        "manifest.json": _manifest_bytes("cached-track"),
        "vocals.wav": _wav_bytes(), "drums.wav": _wav_bytes(),
        "bass.wav": _wav_bytes(), "other.wav": _wav_bytes(),
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[filename] = digest
    from apps.cloud.stem_index import save_cached_index

    save_cached_index(data_dir, {"cached-track": entry})
    publish_index(cfg, s3, {"other-track": {"manifest.json": "z" * 64}})

    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source",
        lambda _data_dir: DirectR2Source(cfg=cfg, s3=s3),
    )

    # publish_index's own read-merge-CAS just touched INDEX_OBJECT_KEY as
    # part of THIS SETUP, not the bulk-hydrate run under test; only calls
    # made by the CLI invocation below are what this assertion is about.
    s3.get_calls.clear()

    rc = _run(
        [
            "bulk-hydrate", "--data-dir", str(data_dir),
            "--ids", "cached-track", "--budget-bytes", str(10**9),
        ]
    )
    assert rc == 0
    assert (cfg.audio_bucket, INDEX_OBJECT_KEY) not in s3.get_calls


@pytest.mark.requirement("STEM-34")
def test_bulk_hydrate_unknown_playlist_exits_2(tmp_path: Path) -> None:
    """[if] bulk-hydrate names an unknown playlist [then] exit 2 names it, [else stop]."""
    from tests.webui.test_stems_route_survival import _seed_playlist_data_dir

    data_dir = _seed_playlist_data_dir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "bulk-hydrate",
                "--data-dir",
                str(data_dir),
                "--playlist",
                "all",
                "--budget-bytes",
                "1000",
            ]
        )
    assert exc_info.value.args[0] == 2
    assert "unknown playlist" in str(exc_info.value)
