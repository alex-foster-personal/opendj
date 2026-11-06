"""Core acceptance tests for the fail-closed Trunk merge-path audit.

Requirements (DEVOPS-4215):
- [if] Trunk identity and an authenticated draft tree match [then] TESTED, [else stop].
- [if] a one-parent SHA has no associated PR [then] UNTESTED direct-push, [else stop].
- [if] provenance cannot be proved [then] UNKNOWN and exit 10, [else stop].
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import trunk_merge_path_audit as mod

pytestmark = pytest.mark.requirement("DEVOPS-4215")

HUMAN = ("Dev", "dev@example.com")
TRUNK = ("trunk-io[bot]", mod.TRUNK_BOT_EMAIL)
MERGIFY = ("mergify[bot]", mod.MERGIFY_BOT_EMAIL)
WINDOW = ("--since", "2026-10-02T00:00:00Z", "--until", "2026-10-03T00:00:00Z")


def _git(repo: Path, *args: str, who: tuple[str, str] = HUMAN, when: str = "") -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0],
        "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": "GitHub",
        "GIT_COMMITTER_EMAIL": "committer@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _commit(
    repo: Path,
    name: str,
    subject: str,
    *,
    who: tuple[str, str] = HUMAN,
    when: str = "2026-10-02T10:00:00Z",
) -> str:
    (repo / name).write_text(subject, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject, who=who, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo, _commit(repo, "root.txt", "root", when="2026-10-01T10:00:00Z")


def _draft(commit: mod.LocalCommit, **changes: object) -> mod.DraftEvidence:
    values: dict[str, object] = {
        "pr_number": 44,
        "head_ref": "trunk-merge/pr-42/uuid",
        "head_commit_sha": "d" * 40,
        "head_tree_sha": commit.tree_sha,
        "author_login": mod.TRUNK_GRAPHQL_LOGIN,
        "head_repository": mod.DEFAULT_REPOSITORY,
        "created_at": "2026-10-02T09:00:00Z",
        "closed_at": "2026-10-02T11:00:00Z",
    }
    values.update(changes)
    return mod.DraftEvidence(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    drafts: tuple[mod.DraftEvidence, ...] = (),
    pulls: dict[str, tuple[int, ...]] | None = None,
    errors: dict[str, str] | None = None,
) -> mod.EvidenceSnapshot:
    return mod.EvidenceSnapshot(drafts, pulls or {}, errors or {})


def test_exact_trunk_identity_and_authenticated_tree_are_tested(tmp_path: Path) -> None:
    """[if] identity and draft tree both match [then] TESTED with evidence, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "queued.txt", "queued", who=TRUNK)
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits([local], _snapshot(drafts=(_draft(local),)), frozenset())
    assert (row.verdict, row.reason) == (mod.Verdict.TESTED, mod.Reason.TRUNK_DRAFT_TREE)
    assert row.draft and row.draft.pr_number == 44
    assert mod.exit_code([row]) == mod.EXIT_OK


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"author_login": "human"}, "human"),
        ({"author_login": "app/trunk-io"}, "app-prefix"),
        ({"head_repository": "fork/music-dj-tools"}, "fork"),
        ({"head_ref": "trunk-merge/not-canonical"}, "malformed"),
        ({"head_ref": "feature/pr-42/uuid"}, "ordinary"),
    ],
)
def test_spoofed_or_malformed_draft_never_certifies(
    tmp_path: Path, changes: dict[str, object], label: str
) -> None:
    """[if] draft authentication is incomplete [then] the commit is UNKNOWN, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, f"{label}.txt", label, who=TRUNK)
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits(
        [local], _snapshot(drafts=(_draft(local, **changes),)), frozenset()
    )
    assert (row.verdict, row.reason) == (mod.Verdict.UNKNOWN, mod.Reason.NO_DRAFT_TREE_MATCH)


def test_trunk_display_name_without_email_is_not_trunk(tmp_path: Path) -> None:
    """[if] only Trunk's display name matches [then] the commit is not TESTED, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "lookalike.txt", "lookalike", who=("trunk-io[bot]", HUMAN[1]))
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits(
        [local], _snapshot(drafts=(_draft(local),), pulls={sha: (7,)}), frozenset()
    )
    assert row.verdict is mod.Verdict.UNKNOWN


def test_batch_tip_does_not_certify_intermediate_tree(tmp_path: Path) -> None:
    """[if] only the batch-tip tree matches [then] an intermediate remains UNKNOWN, [else stop]."""
    repo, root = _init_repo(tmp_path)
    middle_sha = _commit(repo, "middle.txt", "middle", who=TRUNK, when="2026-10-02T10:00:00Z")
    tip_sha = _commit(repo, "tip.txt", "tip", who=TRUNK, when="2026-10-02T10:01:00Z")
    commits = mod.range_commits(repo, root, tip_sha)
    tip = next(commit for commit in commits if commit.sha == tip_sha)
    rows = mod.classify_commits(commits, _snapshot(drafts=(_draft(tip),)), frozenset())
    by_sha = {row.sha: row for row in rows}
    assert by_sha[tip_sha].verdict is mod.Verdict.TESTED
    assert by_sha[middle_sha].verdict is mod.Verdict.UNKNOWN


def test_complete_empty_pull_lookup_names_direct_push(tmp_path: Path) -> None:
    """[if] a one-parent SHA has zero associated PRs [then] name direct-push, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "direct.txt", "direct")
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits([local], _snapshot(pulls={sha: ()}), frozenset())
    assert (row.verdict, row.reason) == (mod.Verdict.UNTESTED, mod.Reason.DIRECT_PUSH)
    assert mod.exit_code([row]) == mod.EXIT_UNTESTED


@pytest.mark.parametrize(
    "snapshot",
    [_snapshot(pulls={"SHA": (7,)}), _snapshot(errors={"SHA": "boom"})],
)
def test_associated_or_unreadable_pull_lookup_is_unknown(
    tmp_path: Path, snapshot: mod.EvidenceSnapshot
) -> None:
    """[if] direct-push evidence is nonempty or unreadable [then] UNKNOWN, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "candidate.txt", "candidate")
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    pulls = {sha: snapshot.associated_pulls["SHA"]} if "SHA" in snapshot.associated_pulls else {}
    errors = {sha: snapshot.errors["SHA"]} if "SHA" in snapshot.errors else {}
    [row] = mod.classify_commits([local], _snapshot(pulls=pulls, errors=errors), frozenset())
    assert row.verdict is mod.Verdict.UNKNOWN
    assert mod.exit_code([row]) == mod.EXIT_UNKNOWN


def test_legacy_mergify_is_unknown_and_coverage_is_null(tmp_path: Path) -> None:
    """[if] a legacy Mergify commit is selected [then] UNKNOWN and no coverage, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "legacy.txt", "legacy", who=MERGIFY)
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits([local], _snapshot(), frozenset({sha}))
    assert (row.verdict, row.reason) == (mod.Verdict.UNKNOWN, mod.Reason.LEGACY_MERGIFY)
    assert mod.summarize([row])["tested_coverage_pct"] is None


def test_mixed_output_orders_untested_unknown_tested_and_exits_10(tmp_path: Path) -> None:
    """[if] verdicts are mixed [then] all print in risk order and exit 10, [else stop]."""
    repo, root = _init_repo(tmp_path)
    direct = _commit(repo, "direct.txt", "direct | push", when="2026-10-02T10:00:00Z")
    unknown = _commit(repo, "unknown.txt", "unknown", who=TRUNK, when="2026-10-02T10:01:00Z")
    tested = _commit(repo, "tested.txt", "tested", who=TRUNK, when="2026-10-02T10:02:00Z")
    commits = mod.range_commits(repo, root, tested)
    tested_local = next(commit for commit in commits if commit.sha == tested)
    rows = mod.classify_commits(
        commits,
        _snapshot(drafts=(_draft(tested_local),), pulls={direct: ()}),
        frozenset(),
    )
    markdown = mod.render_markdown(rows, culprit_mode=True)
    assert markdown.index(direct[:10]) < markdown.index(unknown[:10]) < markdown.index(tested[:10])
    assert "direct \\| push" in markdown
    assert mod.exit_code(rows) == mod.EXIT_UNKNOWN


def test_json_envelope_names_all_rows_and_provenance(tmp_path: Path) -> None:
    """[if] JSON is requested [then] every row and matching draft is serialized, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "queued.txt", "queued", who=TRUNK)
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits([local], _snapshot(drafts=(_draft(local),)), frozenset())
    payload = json.loads(mod.render_json([row], repository=mod.DEFAULT_REPOSITORY, selection="x"))
    assert payload["summary"]["tested"] == 1
    assert payload["commits"][0]["draft"]["head_tree_sha"] == local.tree_sha


def test_all_tested_window_markdown_names_positive_provenance(tmp_path: Path) -> None:
    """[if] every window row is TESTED [then] Markdown names positive provenance, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "queued.txt", "queued", who=TRUNK)
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    [row] = mod.classify_commits([local], _snapshot(drafts=(_draft(local),)), frozenset())

    markdown = mod.render_markdown([row], culprit_mode=False)

    assert "Every selected tree has positive Trunk draft provenance." in markdown


def test_trunk_draft_transport_error_is_unknown(tmp_path: Path) -> None:
    """[if] draft transport fails [then] a Trunk commit is UNKNOWN, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    sha = _commit(repo, "queued.txt", "queued", who=TRUNK)
    [local] = mod.range_commits(repo, f"{sha}^", sha)
    evidence = mod.EvidenceSnapshot((), {}, {}, draft_error="transport failed")
    [row] = mod.classify_commits([local], evidence, frozenset())
    assert row.reason is mod.Reason.EVIDENCE_UNREADABLE
    assert mod.exit_code([row]) == mod.EXIT_UNKNOWN


def test_collect_evidence_preserves_draft_and_commit_errors() -> None:
    """[if] GitHub reads fail [then] preserve errors for affected rows, [else stop]."""
    trunk = mod.LocalCommit(
        "a" * 40,
        "b" * 40,
        "trunk",
        mod.TRUNK_BOT_EMAIL,
        "2026-10-02T00:00:00Z",
        ("c" * 40,),
        "trunk",
        None,
    )
    direct = mod.LocalCommit(
        "d" * 40,
        "e" * 40,
        "dev",
        HUMAN[1],
        "2026-10-02T00:01:00Z",
        ("f" * 40,),
        "direct",
        None,
    )

    class FailingReader(mod.GitHubEvidenceReader):
        def __init__(self) -> None:
            self.repository = mod.DEFAULT_REPOSITORY

        def fetch_drafts(self, committed_at: Sequence[str]) -> tuple[mod.DraftEvidence, ...]:
            raise mod.Unknown(f"draft failed for {len(committed_at)} rows")

        def fetch_associated_pulls(self, sha: str) -> tuple[int, ...]:
            raise mod.Unknown(f"pull lookup failed for {sha[:10]}")

    evidence = mod.collect_evidence([trunk, direct], frozenset(), FailingReader())
    assert evidence.draft_error == "draft failed for 1 rows"
    assert evidence.errors[direct.sha].startswith("pull lookup failed")


@pytest.mark.parametrize(
    ("since", "until", "message"),
    [
        ("not-a-date", "2026-10-03T00:00:00Z", "not an ISO 8601"),
        ("2026-10-02T00:00:00", "2026-10-03T00:00:00Z", "no UTC offset"),
        ("2026-10-03T00:00:00Z", "2026-10-02T00:00:00Z", "not before"),
    ],
)
def test_bad_window_bounds_are_unknown(
    tmp_path: Path, since: str, until: str, message: str
) -> None:
    """[if] window bounds are invalid [then] selection raises UNKNOWN, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    with pytest.raises(mod.Unknown, match=message):
        mod.window_commits(repo, "main", since, until)


def test_empty_or_unresolved_window_is_unknown(tmp_path: Path) -> None:
    """[if] a ref is unresolved or a window empty [then] selection is UNKNOWN, [else stop]."""
    repo, _ = _init_repo(tmp_path)
    with pytest.raises(mod.Unknown, match="does not resolve"):
        mod.window_commits(repo, "missing", *WINDOW[1::2])
    with pytest.raises(mod.Unknown, match="zero first-parent"):
        mod.window_commits(repo, "main", "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z")


def test_cli_names_direct_push_and_returns_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] CLI sees a known direct push [then] name it and return 1, [else stop]."""
    direct = mod.LocalCommit(
        "a" * 40,
        "b" * 40,
        "dev",
        HUMAN[1],
        "2026-10-02T00:00:00Z",
        ("c" * 40,),
        "direct",
        None,
    )

    class DirectReader(mod.GitHubEvidenceReader):
        def __init__(self, repository: str) -> None:
            self.repository = repository

        def fetch_associated_pulls(self, sha: str) -> tuple[int, ...]:
            assert sha == direct.sha
            return ()

    monkeypatch.setattr(mod, "_selection", lambda _args: ([direct], "GOOD..BAD"))
    monkeypatch.setattr(mod, "legacy_shas", lambda *_args: frozenset())
    monkeypatch.setattr(mod, "GitHubEvidenceReader", DirectReader)
    assert mod.main(["--range", "GOOD..BAD"]) == mod.EXIT_UNTESTED
    assert f"UNTESTED direct-push {direct.sha}" in capsys.readouterr().out
