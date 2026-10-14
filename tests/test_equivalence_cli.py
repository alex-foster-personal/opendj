"""CLI-level coverage for the equivalence gate's publish timing (PR #383 review).

The canonical verdict file feeds ``apps.shared.equivalence.EquivalenceGate`` on
the NEXT MIK load. Two failure modes were possible before this suite existed:

1. A run that later fails its own known-answer check (exit 3) had ALREADY
   replaced the gate file with the unproven verdicts from this run.
2. ``run --fields X`` only computes verdicts for ``X``, and a wholesale write
   erased every OTHER field's already-proven verdict from the gate.

Both are exercised end to end through ``apps.equivalence.__main__.main`` with
``read_rekordbox``/``read_mik``/``read_mik_energy_segments``/
``run_known_answers`` monkeypatched, so no real rekordbox or MIK database is
needed -- empty source rows are enough to reach the write-gating logic
without tripping any mapping-bug cluster.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.equivalence import __main__ as cli
from apps.equivalence.compare import Agreement
from apps.equivalence.config import SINGLE_SOURCE_FIELDS
from apps.equivalence.known import write_manifest
from apps.equivalence.probe import Probe
from apps.equivalence.verdict import PASSED, SUITE_PASSED, PairVerdict, build_document


def _probe(**kw) -> Probe:
    base: dict[str, Any] = dict(
        source="rekordbox",
        locator="djmdContent.BPM",
        kind="bpm",
        declared_unit="centi_bpm",
        rows=200,
        present=200,
        null=0,
        distinct=50,
        numeric=True,
    )
    base.update(kw)
    return Probe(**base)


def _passing_bpm_verdict() -> PairVerdict:
    agreement = Agreement(
        pairs=200,
        comparable=200,
        agree=200,
        disagree=0,
        left_missing=0,
        right_missing=0,
        left_unmapped=0,
        right_unmapped=0,
        tolerance=1.0,
        tolerance_note="1.0 BPM",
    )
    return PairVerdict(
        field_name="bpm",
        status=PASSED,
        suite_status=SUITE_PASSED,
        reasons=["200/200 agree (100.0%)"],
        left=_probe(),
        right=_probe(source="mik", locator="ZSONG.ZTEMPO", declared_unit="bpm"),
        agreement=agreement,
        clusters=[],
        mismatches=[],
        normaliser="bpm:centi_bpm->bpm | bpm:bpm->bpm",
    )


class _FakeKnownReport:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def as_dict(self) -> dict:
        return self._payload


def _patch_empty_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "read_rekordbox", lambda _path: [])
    monkeypatch.setattr(cli, "read_mik", lambda _path: [])
    monkeypatch.setattr(cli, "read_mik_energy_segments", lambda _path: [])


def _patch_known_answers(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> None:
    payload = {
        "fixture": "fake-fixture.json",
        "tracks": 0,
        "passed": 0,
        "failed": 0 if ok else 1,
        "pending_human_verification": 0,
        "unresolved_tracks": 0,
        "unresolved_checks": 0,
        "ok": ok,
        "results": [],
        "note": "fake report for CLI publish-timing tests",
    }
    monkeypatch.setattr(cli, "run_known_answers", lambda *a, **k: _FakeKnownReport(payload))


def test_a_corrupt_source_exits_source_unreadable_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that exists but is corrupt/locked/schema-drifted raises
    ``sqlite3.DatabaseError`` (or its ``OperationalError`` subclass), not
    ``FileNotFoundError`` (P2 review, PR #383). Before the fix, ``main`` only
    caught ``FileNotFoundError`` here, so this class of failure fell through
    to an unhandled traceback and Python's default exit code 1 -- masking the
    documented ``EXIT_SOURCE_UNREADABLE`` (4) contract callers rely on to
    distinguish an environment/source problem from an unspecified crash."""

    def _raise_corrupt(_path):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(cli, "read_rekordbox", _raise_corrupt)
    monkeypatch.setattr(cli, "read_mik", lambda _path: [])

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json"])

    assert exit_code == cli.EXIT_SOURCE_UNREADABLE


def test_gate_not_written_when_known_answers_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_empty_sources(monkeypatch)
    _patch_known_answers(monkeypatch, ok=False)

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json"])

    assert exit_code == cli.EXIT_KNOWN_ANSWER_FAILED
    assert not (tmp_path / "state" / "equivalence-verdicts.json").exists()


def test_gate_written_when_run_is_trustworthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_empty_sources(monkeypatch)
    _patch_known_answers(monkeypatch, ok=True)

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json"])

    assert exit_code == cli.EXIT_OK
    verdicts_path = tmp_path / "state" / "equivalence-verdicts.json"
    assert verdicts_path.exists()
    written = json.loads(verdicts_path.read_text(encoding="utf-8"))
    assert set(written["fields"]) == {p.field_name for p in cli.FIELD_PAIRS} | {
        s.field_name for s in SINGLE_SOURCE_FIELDS
    }


def test_partial_fields_run_merges_onto_existing_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``--fields key`` run must not demote ``bpm``'s prior proven verdict."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    verdicts_path = state_dir / "equivalence-verdicts.json"

    prior = build_document(
        [_passing_bpm_verdict()],
        match_report={},
        known_answers={"ok": True},
        single_source=[],
    )
    verdicts_path.write_text(json.dumps(prior, indent=2), encoding="utf-8")

    _patch_empty_sources(monkeypatch)
    _patch_known_answers(monkeypatch, ok=True)

    exit_code = cli.main(["run", "--data-dir", str(tmp_path), "--json", "--fields", "key"])

    assert exit_code == cli.EXIT_OK
    written = json.loads(verdicts_path.read_text(encoding="utf-8"))
    assert written["fields"]["bpm"]["status"] == "passed"  # untouched by this run
    assert "key" in written["fields"]  # freshly recomputed by this run


def test_merge_with_existing_gate_ignores_a_corrupt_prior_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "equivalence-verdicts.json"
    path.write_text("{not json", encoding="utf-8")
    document = {"fields": {"key": {"status": "passed"}}}

    merged = cli._merge_with_existing_gate(document, path)

    assert merged is document
    assert merged["fields"] == {"key": {"status": "passed"}}


def test_merge_with_existing_gate_no_prior_file_is_a_no_op(tmp_path: Path) -> None:
    document = {"fields": {"key": {"status": "passed"}}}

    merged = cli._merge_with_existing_gate(document, tmp_path / "nope.json")

    assert merged is document


# ------------------------------------------------ per-field hold, real path
#
# Real path end to end: a real rekordbox-shaped sqlite file, a real Mixed In
# Key-shaped sqlite file and a real checksummed fixture go through the
# production readers, matcher, known-answer harness and verdict builders by
# way of ``python -m apps.equivalence run``. Nothing is replaced.
#
# [if] a tempo check awaits a human [then] bpm is published untested and every other field keeps its verdict, [else stop].

SONG_COUNT = 150
CAMELOT = [f"{number}{letter}" for number in range(1, 13) for letter in "AB"]
RB_SCALE = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "Dbm",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}  # fmt: skip


def _song(index: int) -> dict[str, Any]:
    return {
        "title": f"Track {index:03d}",
        "artist": f"Artist {index % 17}",
        "key": CAMELOT[index % len(CAMELOT)],
        "energy": 1 + index % 9,
        "tempo": 100.0 + index % 40,
        "volume": -14.0 + (index % 50) / 10,
        "seconds": 180 + index % 240,
        "clipped": index % 7,
    }


def _write_sources(data_dir: Path) -> Path:
    songs = [_song(index) for index in range(1, SONG_COUNT + 1)]
    rekordbox = sqlite3.connect(data_dir / "master.plain.db")
    rekordbox.executescript(
        """
        CREATE TABLE djmdKey (ID TEXT PRIMARY KEY, ScaleName TEXT);
        CREATE TABLE djmdArtist (ID TEXT PRIMARY KEY, Name TEXT);
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY, FolderPath TEXT, FileNameL TEXT, Title TEXT,
            BPM INTEGER, Length INTEGER, Rating INTEGER, KeyID TEXT, ArtistID TEXT
        );
        """
    )
    store_path = data_dir / "Collection10.mikdb"
    store = sqlite3.connect(store_path)
    store.executescript(
        """
        CREATE TABLE ZSONG (
            Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZARTIST TEXT, ZKEY TEXT,
            ZENERGY REAL, ZTEMPO REAL, ZVOLUME REAL, ZRATING REAL,
            ZCLIPPEDPEAKCOUNT INTEGER, ZBOOKMARKDATA BLOB
        );
        CREATE TABLE ZENERGYSEGMENT (
            Z_PK INTEGER PRIMARY KEY, ZSONG INTEGER, ZENERGY REAL,
            ZLENGTH REAL, ZSTARTTIME REAL
        );
        CREATE TABLE ZKEYSEGMENT (
            Z_PK INTEGER PRIMARY KEY, ZSONG INTEGER, ZCONFIDENCE REAL, ZKEY TEXT
        );
        """
    )
    for key in CAMELOT:
        rekordbox.execute("INSERT INTO djmdKey VALUES (?, ?)", (key, RB_SCALE[key]))
    for index, song in enumerate(songs, start=1):
        rekordbox.execute("INSERT OR IGNORE INTO djmdArtist VALUES (?, ?)", (song["artist"], song["artist"]))
        rekordbox.execute(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                str(index),
                f"/Users/dev/Music/{song['title']}.mp3",
                f"{song['title']}.mp3",
                song["title"],
                int(song["tempo"] * 100),
                song["seconds"],
                song["key"],
                song["artist"],
            ),
        )
        store.execute(
            "INSERT INTO ZSONG VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)",
            (
                index,
                song["title"],
                song["artist"],
                song["key"],
                float(song["energy"]),
                song["tempo"],
                song["volume"],
                song["clipped"],
            ),
        )
        half = song["seconds"] / 2
        for start, energy in ((0.0, song["energy"]), (half, 1 + (song["energy"] % 10))):
            store.execute(
                "INSERT INTO ZENERGYSEGMENT (ZSONG, ZENERGY, ZLENGTH, ZSTARTTIME) VALUES (?, ?, ?, ?)",
                (index, float(energy), half, start),
            )
        store.execute(
            "INSERT INTO ZKEYSEGMENT (ZSONG, ZCONFIDENCE, ZKEY) VALUES (?, 0.95, ?)",
            (index, song["key"]),
        )
    rekordbox.commit()
    rekordbox.close()
    store.commit()
    store.close()
    return store_path


def _write_fixture(path: Path, pending_targets: list[str]) -> Path:
    song = _song(1)
    checks: list[dict[str, Any]] = [
        {"target": "mik.energy", "raw": float(song["energy"]), "verified_by": "source-read"},
        {"target": "mik.volume", "raw": song["volume"], "verified_by": "source-read"},
    ]
    checks.extend({"target": target, "verified_by": "needs-maintainer", "why": "needs an ear"} for target in pending_targets)
    answered = {check["target"] for check in checks}
    if "mik.tempo" not in answered:
        checks.append({"target": "mik.tempo", "raw": song["tempo"], "verified_by": "source-read"})
    path.write_text(
        json.dumps(
            {
                "schema": "equivalence-known-answers/v1",
                "tracks": [{"id": "track-001", "select": {"mik_title": song["title"]}, "expect": checks}],
            }
        ),
        encoding="utf-8",
    )
    write_manifest(path, path.with_suffix(".manifest.json"))
    return path


def _run(data_dir: Path, store: Path, fixture: Path, *extra: str) -> tuple[int, dict[str, Any] | None]:
    code = cli.main(
        ["run", "--data-dir", str(data_dir), "--mik-db", str(store), "--fixture", str(fixture), "--json", *extra]
    )
    gate = data_dir / "state" / "equivalence-verdicts.json"
    return code, json.loads(gate.read_text(encoding="utf-8")) if gate.exists() else None


def _statuses(document: dict[str, Any]) -> dict[str, str]:
    return {name: entry["status"] for name, entry in document["fields"].items()}


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "state").mkdir()
    return tmp_path, _write_sources(tmp_path)


def test_a_pending_tempo_check_holds_back_bpm_and_nothing_else(sources, capsys) -> None:
    data_dir, store = sources
    settled_code, settled = _run(data_dir, store, _write_fixture(data_dir / "settled.json", []))
    assert settled_code == cli.EXIT_OK
    assert settled is not None
    baseline = _statuses(settled)
    # The control: with nothing pending these fields really do pass, so a
    # later 'untested' can only come from the hold.
    assert baseline["bpm"] == "passed"
    assert baseline["energy"] == "passed"
    assert baseline["energy_segments"] == "passed"

    pending_code, pending = _run(data_dir, store, _write_fixture(data_dir / "pending.json", ["mik.tempo"]))
    capsys.readouterr()

    assert pending_code == cli.EXIT_KNOWN_ANSWER_FAILED
    assert pending is not None
    assert _statuses(pending) == {**baseline, "bpm": "untested"}
    assert pending["fields"]["bpm"]["held_by_pending_checks"] == ["mik.tempo"]
    assert pending["meta"]["held_by_pending_checks"] == {"bpm": ["mik.tempo"]}


def test_a_pending_energy_check_holds_the_scalar_and_leaves_the_series(sources, capsys) -> None:
    """The harness reads the scalar column for ``mik.energy``; it never reads the segment table."""
    data_dir, store = sources
    code, document = _run(data_dir, store, _write_fixture(data_dir / "pending.json", ["mik.energy"]))
    capsys.readouterr()

    assert code == cli.EXIT_KNOWN_ANSWER_FAILED
    assert document is not None
    statuses = _statuses(document)
    assert statuses["energy"] == "untested"
    assert statuses["energy_segments"] == "passed"
    assert document["meta"]["held_by_pending_checks"] == {"energy": ["mik.energy"]}


def test_a_failed_known_answer_beside_a_pending_one_withholds_the_whole_gate(sources, capsys) -> None:
    data_dir, store = sources
    fixture = data_dir / "drifted.json"
    _write_fixture(fixture, ["mik.tempo"])
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["tracks"][0]["expect"][0]["raw"] = 99.0  # not what the store holds
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    write_manifest(fixture, fixture.with_suffix(".manifest.json"))

    code, document = _run(data_dir, store, fixture)
    capsys.readouterr()

    assert code == cli.EXIT_KNOWN_ANSWER_FAILED
    assert document is None


def test_a_hold_never_softens_a_failed_verdict_already_in_the_gate(sources, capsys) -> None:
    """Under ``--fields`` a prior FAILED bpm verdict must survive a tempo hold as FAILED."""
    data_dir, store = sources
    gate = data_dir / "state" / "equivalence-verdicts.json"
    _run(data_dir, store, _write_fixture(data_dir / "settled.json", []))
    prior = json.loads(gate.read_text(encoding="utf-8"))
    prior["fields"]["bpm"]["status"] = "failed"
    gate.write_text(json.dumps(prior), encoding="utf-8")

    _code, document = _run(
        data_dir, store, _write_fixture(data_dir / "pending.json", ["mik.tempo"]), "--fields", "energy"
    )
    capsys.readouterr()

    assert document is not None
    assert document["fields"]["bpm"]["status"] == "failed"
    assert document["fields"]["bpm"]["held_by_pending_checks"] == ["mik.tempo"]
    assert document["fields"]["energy"]["status"] == "passed"
