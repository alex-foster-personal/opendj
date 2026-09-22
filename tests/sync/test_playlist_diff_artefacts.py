"""SYNC-03: artefact writers (markdown + CSV + JSON) + CLI smoke."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from apps.sync.playlist_diff import (
    _path_under_repo,
    write_diff_md,
    write_patch_csv,
    write_plan_json,
)
from apps.sync.playlist_diff import (
    main as playlist_diff_main,
)
from apps.sync.playlist_plan import (
    DjayPlaylistRead,
    FlatPlaylist,
    MatchSet,
    PlannedMember,
    PlaylistOp,
    PlaylistPlan,
    build_plan,
)


def _fixture_plan() -> PlaylistPlan:
    """Deterministic plan used by every artefact test in this module."""
    rb = [
        FlatPlaylist(
            rb_id="r1",
            flat_name="Events / 2024 / Berlin",
            parent_path=("Events", "2024"),
            track_ids_ordered=("t1", "t2", "t_missing"),
        ),
        FlatPlaylist(
            rb_id="r2",
            flat_name="Warmup",
            parent_path=(),
            track_ids_ordered=("t1", "t3"),
        ),
    ]
    djay = [
        DjayPlaylistRead(
            uuid="pl_warm",
            name="warmup",
            member_uuids_ordered=("u1", "u_old"),
        ),
        DjayPlaylistRead(
            uuid="pl_djay_only",
            name="User's Drafts",
            member_uuids_ordered=("u_x",),
        ),
    ]
    matches = MatchSet(
        rb_to_djay={"t1": "u1", "t2": "u2", "t3": "u3"},
        djay_to_rb={"u1": "t1", "u2": "t2", "u3": "t3"},
        source_sha256="deadbeef",
        source_path="fake/matches.csv",
    )
    titles = {
        "t1": ("Track One", "A"),
        "t2": ("Track Two", "B"),
        "t_missing": ("Ghost", "Unknown"),
        "t3": ("Track Three", "C"),
    }
    return build_plan(
        rb, djay, matches,
        rb_track_titles=titles,
        generated_at="2026-04-17T00:00:00Z",
    )


# ----- JSON writer -----------------------------------------------------


@pytest.mark.requirement("SYNC-03")
def test_plan_json_has_stable_schema(tmp_path: Path) -> None:
    out = tmp_path / "plan.json"
    write_plan_json(_fixture_plan(), out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert set(doc.keys()) == {"generated_at", "match_set_sha256", "playlists", "djay_only"}
    assert doc["generated_at"] == "2026-04-17T00:00:00Z"
    assert doc["match_set_sha256"] == "deadbeef"

    # Each playlist entry has the documented fields.
    required = {
        "rb_id", "rb_name", "op", "djay_uuid", "djay_name_current",
        "target_members", "djay_current_members", "adds", "removes",
        "reordered", "unmatched_rb",
    }
    for pl in doc["playlists"]:
        assert required <= set(pl.keys())
    # target_members rows have track_no + rb_id + djay_uuid.
    for pl in doc["playlists"]:
        for m in pl["target_members"]:
            assert set(m.keys()) == {"track_no", "rb_id", "djay_uuid"}
    # djay_only entries have djay_uuid + name + member_count.
    for d in doc["djay_only"]:
        assert set(d.keys()) == {"djay_uuid", "name", "member_count"}


@pytest.mark.requirement("SYNC-03")
def test_plan_json_reflects_decisions(tmp_path: Path) -> None:
    out = tmp_path / "plan.json"
    write_plan_json(_fixture_plan(), out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    # Berlin -> create, because djay has no colliding name.
    berlin = next(p for p in doc["playlists"] if p["rb_id"] == "r1")
    assert berlin["op"] == "create"
    assert berlin["djay_uuid"] is None
    # Unmatched Ghost surfaces as metadata.
    assert [u["rb_id"] for u in berlin["unmatched_rb"]] == ["t_missing"]

    # Warmup -> update, case-insensitive match.
    warm = next(p for p in doc["playlists"] if p["rb_id"] == "r2")
    assert warm["op"] == "update"
    assert warm["djay_uuid"] == "pl_warm"
    assert "u_old" in warm["removes"]
    assert any(a["djay_uuid"] == "u3" for a in warm["adds"])

    # djay-only is reported, not mutated.
    assert [d["djay_uuid"] for d in doc["djay_only"]] == ["pl_djay_only"]


@pytest.mark.requirement("SYNC-03")
def test_plan_json_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    out1 = tmp_path / "a.json"
    out2 = tmp_path / "b.json"
    write_plan_json(_fixture_plan(), out1)
    write_plan_json(_fixture_plan(), out2)
    assert out1.read_bytes() == out2.read_bytes()


# ----- CSV writer -------------------------------------------------------


@pytest.mark.requirement("SYNC-03")
def test_patch_csv_columns_are_stable(tmp_path: Path) -> None:
    out = tmp_path / "patch.csv"
    write_patch_csv(_fixture_plan(), out)
    with out.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        expected = [
            "op", "playlist_name", "playlist_rb_id", "playlist_djay_uuid",
            "track_rb_id", "track_djay_uuid", "new_track_no", "reason",
        ]
        assert reader.fieldnames == expected


@pytest.mark.requirement("SYNC-03")
def test_patch_csv_rows_reflect_ops(tmp_path: Path) -> None:
    out = tmp_path / "patch.csv"
    write_patch_csv(_fixture_plan(), out)
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    ops = [r["op"] for r in rows]
    # create op + add_member for each matched track in Berlin
    assert "create_playlist" in ops
    assert ops.count("add_member") >= 2
    # update op adds + removes
    assert "remove_member" in ops
    # djay-only reported as noop
    djay_only_rows = [r for r in rows if r["playlist_djay_uuid"] == "pl_djay_only"]
    assert djay_only_rows and djay_only_rows[0]["op"] == "noop"


# ----- Markdown writer --------------------------------------------------


@pytest.mark.requirement("SYNC-03")
def test_diff_md_contains_summary_and_sections(tmp_path: Path) -> None:
    out = tmp_path / "diff.md"
    write_diff_md(_fixture_plan(), out)
    text = out.read_text(encoding="utf-8")
    assert "# Playlist sync dry-run (SYNC-03)" in text
    assert "Match-set sha256: `deadbeef`" in text
    # Each affected playlist appears as an H2.
    assert "## Events / 2024 / Berlin" in text
    assert "## Warmup" in text
    # djay-only section present.
    assert "## djay-only playlists" in text
    assert "pl_djay_only" in text
    # Unmatched table present with the ghost row.
    assert "Unmatched RB tracks" in text
    assert "Ghost" in text


@pytest.mark.requirement("SYNC-03")
def test_diff_md_deterministic(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    write_diff_md(_fixture_plan(), a)
    write_diff_md(_fixture_plan(), b)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.requirement("SYNC-03")
def test_diff_md_pipes_in_names_are_escaped(tmp_path: Path) -> None:
    plan = PlaylistPlan(
        generated_at="2026-04-17T00:00:00Z",
        match_set_sha256="",
        playlists=[
            PlaylistOp(
                rb_id="r1",
                rb_name="Weird|Name",
                op="create",
                djay_uuid=None,
                djay_name_current=None,
                target_members=[PlannedMember(1, "t1", "u1")],
                adds=[PlannedMember(1, "t1", "u1")],
            )
        ],
    )
    out = tmp_path / "diff.md"
    write_diff_md(plan, out)
    text = out.read_text(encoding="utf-8")
    assert "Weird\\|Name" in text


# ----- Safety: out-dir escape + CLI flags ------------------------------


@pytest.mark.requirement("SYNC-03")
def test_out_dir_outside_repo_is_rejected(tmp_path: Path) -> None:
    rc = playlist_diff_main(["--out-dir", str(tmp_path), "--matches", str(tmp_path / "m.csv")])
    assert rc == 2  # refused before any DB access


@pytest.mark.requirement("SYNC-03")
def test_path_under_repo_guard_trusts_project_root(tmp_path: Path) -> None:
    from apps.shared import paths
    assert _path_under_repo(paths.DATA_DIR / "sync")
    assert not _path_under_repo(Path("/etc"))


@pytest.mark.requirement("SYNC-03")
def test_missing_matches_returns_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no matches.csv the CLI must exit 2 with a hint on stderr."""
    from apps.shared import paths
    # Provide fake DB paths that never get opened because matches check fails first.
    # Force out-dir to repo-local to pass the safety rail.
    repo_out = paths.DATA_DIR / "sync" / "_test_scratch"
    repo_out.mkdir(parents=True, exist_ok=True)
    # Skip past DB loading by stubbing the loader.
    from apps.sync import playlist_diff as pd

    class _Stub:
        pass

    def _fake_rb_inputs(_):
        return [], {}, {}

    def _fake_djay_iter(_):
        return iter([])

    monkeypatch.setattr(pd, "_load_rb_inputs", _fake_rb_inputs)
    monkeypatch.setattr(pd, "djay_iter_playlists", _fake_djay_iter)
    # Point matches at a non-existent file.
    rc = pd.main(
        [
            "--out-dir", str(repo_out),
            "--matches", str(tmp_path / "nope.csv"),
            "--djay-db", str(tmp_path / "fake.db"),
        ]
    )
    assert rc == 2
