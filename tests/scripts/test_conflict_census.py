"""PATH fake-gh tests for scripts/conflict_census.py open-PR listing.

Regression lines:
  - if listing uses `gh pr list --limit 200` then broken (GitHub 502s 100-PR
    mergeStateStatus pages; a 200 cap silently samples)
  - if GraphQL page size is not hardcoded at 25 then broken
  - if a 5xx page is not retried then broken
  - if listed count disagrees with totalCount and the run still exits 0 then broken
  - if an explicit --limit sample is treated as a full-census mismatch then broken
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from scripts import conflict_census as mod
from tests.scripts._hermetic_path import path_hiding

SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "conflict_census.py"

_FAKE_GH = r"""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\n")

if len(argv) < 2 or argv[0] != "api" or argv[1] != "graphql":
    sys.stderr.write("fake gh: unhandled invocation: " + json.dumps(argv) + "\n")
    sys.exit(4)

with open(os.environ["FAKE_GH_PAGES"], encoding="utf-8") as fh:
    pages = json.load(fh)
cursor = None
for i, arg in enumerate(argv):
    if arg.startswith("cursor="):
        cursor = arg.split("=", 1)[1]
    elif arg == "-f" and i + 1 < len(argv) and argv[i + 1].startswith("cursor="):
        cursor = argv[i + 1].split("=", 1)[1]

if cursor is None:
    page_index = 1
else:
    page_index = None
    for i, body in enumerate(pages):
        end = body["data"]["repository"]["pullRequests"]["pageInfo"].get("endCursor")
        if end == cursor:
            page_index = i + 2
            break
    if page_index is None:
        sys.stderr.write("fake gh: unhandled invocation: " + json.dumps(argv) + "\n")
        sys.exit(4)

if page_index > len(pages):
    sys.stderr.write("fake gh: unhandled invocation: " + json.dumps(argv) + "\n")
    sys.exit(4)

fail_page = int(os.environ.get("FAKE_GH_FAIL_PAGE") or "0")
fail_times = int(os.environ.get("FAKE_GH_FAIL_TIMES") or "0")
if fail_page == page_index and fail_times > 0:
    hits_path = os.path.join(os.environ["FAKE_GH_STATE"], "fail_hits")
    hits = 0
    if os.path.exists(hits_path):
        with open(hits_path, encoding="utf-8") as fh:
            hits = int(fh.read() or "0")
    if hits < fail_times:
        with open(hits_path, "w", encoding="utf-8") as fh:
            fh.write(str(hits + 1))
        sys.stderr.write("gh: HTTP 502\n")
        sys.exit(1)

sys.stdout.write(json.dumps(pages[page_index - 1]))
"""


def _node(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"title-{number}",
        "mergeStateStatus": "DIRTY" if number % 3 == 0 else "CLEAN",
        "headRefOid": f"{number:040x}",
    }


def _graphql_page(
    nodes: list[dict[str, object]],
    total_count: int,
    *,
    has_next: bool,
    cursor: str,
) -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "totalCount": total_count,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


def thirty_pr_pages() -> list[dict[str, object]]:
    return [
        _graphql_page([_node(n) for n in range(1, 26)], 30, has_next=True, cursor="cursor-p1"),
        _graphql_page([_node(n) for n in range(26, 31)], 30, has_next=False, cursor="cursor-p2"),
    ]


def expected_dirty(numbers: range) -> list[int]:
    return [n for n in numbers if n % 3 == 0]


def _install_fake_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pages: list[dict[str, object]],
    *,
    fail_page: int = 0,
    fail_times: int = 0,
) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(f"#!{sys.executable}\n{_FAKE_GH}", encoding="utf-8")
    gh.chmod(0o755)
    pages_path = tmp_path / "pages.json"
    pages_path.write_text(json.dumps(pages), encoding="utf-8")
    log_path = tmp_path / "gh.log"
    log_path.write_text("", encoding="utf-8")
    state_dir = tmp_path / "gh-state"
    state_dir.mkdir()
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + path_hiding(tmp_path, "gh"))
    monkeypatch.setenv("FAKE_GH_LOG", str(log_path))
    monkeypatch.setenv("FAKE_GH_PAGES", str(pages_path))
    monkeypatch.setenv("FAKE_GH_STATE", str(state_dir))
    monkeypatch.setenv("FAKE_GH_FAIL_PAGE", str(fail_page))
    monkeypatch.setenv("FAKE_GH_FAIL_TIMES", str(fail_times))
    return log_path


def _logged_argv(log_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def _assert_graphql_pages(calls: list[list[str]]) -> None:
    assert calls, "expected gh api graphql invocations"
    for argv in calls:
        assert argv[:2] == ["api", "graphql"]
        assert argv[:2] != ["pr", "list"]
        assert "pr" not in argv
        assert "list" not in argv
        blob = " ".join(argv)
        assert "first: 25" in blob or "first:25" in blob


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gh is a shebang script")
def test_multi_page_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = _install_fake_gh(tmp_path, monkeypatch, thirty_pr_pages())
    dirty, listed, total_count = mod._dirty_prs(None)
    assert listed == 30
    assert total_count == 30
    assert [int(p["number"]) for p in dirty] == expected_dirty(range(1, 31))
    calls = _logged_argv(log_path)
    assert len(calls) == 2
    _assert_graphql_pages(calls)
    assert not any(a.startswith("cursor=") for a in calls[0])
    assert any(a.startswith("cursor=") for a in calls[1])


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gh is a shebang script")
def test_5xx_page_retried_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = _install_fake_gh(tmp_path, monkeypatch, thirty_pr_pages(), fail_page=2, fail_times=1)
    dirty, listed, total_count = mod._dirty_prs(None)
    assert listed == 30
    assert total_count == 30
    assert [int(p["number"]) for p in dirty] == expected_dirty(range(1, 31))
    calls = _logged_argv(log_path)
    assert len(calls) == 3
    _assert_graphql_pages(calls)
    assert not any(a.startswith("cursor=") for a in calls[0])
    assert any(a.startswith("cursor=") for a in calls[1])
    assert any(a.startswith("cursor=") for a in calls[2])


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gh is a shebang script")
def test_count_mismatch_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [_graphql_page([_node(n) for n in range(1, 26)], 40, has_next=False, cursor="cursor-p1")]
    _install_fake_gh(tmp_path, monkeypatch, pages)
    with pytest.raises(SystemExit) as raised:
        mod._dirty_prs(None)
    assert raised.value.code not in (0, None)


def test_mutation_control_source_has_no_pr_list_limit_200() -> None:
    src = SOURCE.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", src)
    assert "default=200" not in compact
    assert '"pr","list"' not in compact
    assert "'pr','list'" not in compact
    assert '"--limit","200"' not in compact
    assert "'--limit','200'" not in compact


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gh is a shebang script")
def test_explicit_sample_prints_sample_and_does_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_gh(tmp_path, monkeypatch, thirty_pr_pages())
    monkeypatch.setattr(mod, "_pinned_main", lambda: "cafef00d1" + "0" * 31)
    monkeypatch.setattr(mod, "_conflicting_paths", lambda main_sha, head_sha: ([], ""))
    rc = mod.main(["--limit", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sample" in out
    assert "--limit" in out
    assert "totalCount" in out
