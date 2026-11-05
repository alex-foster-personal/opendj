"""[if] a frontend format commit is not exactly the pinned prettier's output of its parent [then] prove fails it or reports UNKNOWN, [else stop].

A frontend format commit must be exactly the pinned prettier's output of its parent (issue #4456, part 8).

`prove` hands frontend sources to `scripts/format_proof_prettier.py`, which formats each base blob with
the frontend's installed prettier under the head commit's `.prettierrc` and requires byte equality with
head plus a stable second pass. Every test builds its own throwaway repository; the prettier that runs
is this checkout's pinned one, so the tests skip where `pnpm install` has not run.

Regression lines:
  - if prove fails a commit that is exactly prettier's output of its parent (ts, mjs or svelte) then broken
  - if prove passes a prettier-formatted file that also carries a hand edit then broken
  - if prove passes a frontend change when the installed prettier is not the version head pins then broken
  - if prove passes a frontend change when head has no .prettierrc, or one with overrides, then broken
  - if prove passes a frontend change when node_modules is missing then broken: it must be UNKNOWN
  - if prove passes a range that mixes a proven frontend file with a non-source file then broken
  - if ignore-revs fails a listed commit that is exactly prettier's output of its parent then broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import format_proof
from scripts.format_proof_prettier import CFG
from tests.quality.format_proof_repo import commit_files, init_repo

REAL_FRONTEND = format_proof.REPO / CFG.FRONTEND
pytestmark = [
    pytest.mark.requirement("OPS-8792"),
    pytest.mark.skipif(
        not (REAL_FRONTEND / "node_modules" / "prettier").exists(), reason="frontend node_modules not installed"
    ),
]

FE = CFG.FRONTEND
PIN = json.loads((REAL_FRONTEND / "package.json").read_text())["devDependencies"]["prettier"]
RATIFIED = json.dumps(
    {
        "useTabs": True,
        "singleQuote": True,
        "trailingComma": "none",
        "printWidth": 100,
        "plugins": ["prettier-plugin-svelte"],
    }
)
PACKAGE = json.dumps({"devDependencies": {"prettier": PIN}})
TS_BEFORE = 'export const limits = {soft: 1000, "hard": 1500,}\nexport function f(a:number,b:number){return a+b}\n'
TS_AFTER = (
    "export const limits = { soft: 1000, hard: 1500 };\nexport function f(a: number, b: number) {\n\treturn a + b;\n}\n"
)
SVELTE_BEFORE = '<script lang="ts">\nlet n = $state(0)\n</script>\n<button onclick={() => n++}>{n}</button>\n'
SVELTE_AFTER = '<script lang="ts">\n\tlet n = $state(0);\n</script>\n\n<button onclick={() => n++}>{n}</button>\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = init_repo(tmp_path / "repo")
    commit_files(path, {f"{FE}/.prettierrc": RATIFIED, f"{FE}/package.json": PACKAGE}, "config")
    return path


def _reformat(repo: Path, before: dict[str, str], after: dict[str, str]) -> tuple[str, str]:
    base = commit_files(repo, {f"{FE}/{k}": v for k, v in before.items()}, "init")
    head = commit_files(repo, {f"{FE}/{k}": v for k, v in after.items()}, f"style(format): prettier@{PIN} src")
    return base, head


def test_prove_passes_prettier_output_for_ts_mjs_and_svelte(repo: Path) -> None:
    base, head = _reformat(
        repo,
        {"src/a.ts": TS_BEFORE, "src/b.mjs": "const x = {a:1}\n", "src/C.svelte": SVELTE_BEFORE},
        {"src/a.ts": TS_AFTER, "src/b.mjs": "const x = { a: 1 };\n", "src/C.svelte": SVELTE_AFTER},
    )
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines
    assert any(f"OK 3 frontend files are prettier@{PIN}" in line for line in result.lines), result.lines


def test_prove_rejects_a_hand_edit_inside_formatted_output(repo: Path) -> None:
    base, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER.replace("1500", "1501")})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1
    assert any("src/a.ts" in line and "not prettier" in line for line in result.lines), result.lines


def test_prove_rejects_a_renamed_identifier_that_prettier_would_not_make(repo: Path) -> None:
    base, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER.replace("f(a", "g(a")})
    assert format_proof.prove(repo, base, head).exit_code == 1


def test_prove_is_unknown_when_head_pins_another_prettier(repo: Path) -> None:
    commit_files(repo, {f"{FE}/package.json": json.dumps({"devDependencies": {"prettier": "3.0.0"}})}, "pin")
    base, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2
    assert any("3.0.0" in line for line in result.lines), result.lines


def test_prove_is_unknown_when_the_pin_is_a_range(repo: Path) -> None:
    commit_files(repo, {f"{FE}/package.json": json.dumps({"devDependencies": {"prettier": f"^{PIN}"}})}, "pin")
    base, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER})
    assert format_proof.prove(repo, base, head).exit_code == 2


@pytest.mark.parametrize("config", ["", json.dumps({"useTabs": True, "overrides": []})])
def test_prove_is_unknown_without_a_flat_prettierrc(repo: Path, config: str) -> None:
    commit_files(repo, {f"{FE}/.prettierrc": config}, "config")
    base, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER})
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_prove_is_unknown_without_node_modules(repo: Path, tmp_path: Path) -> None:
    empty = tmp_path / "frontend-without-deps"
    (empty / "scripts").mkdir(parents=True)
    (empty / CFG.HELPER).write_text((REAL_FRONTEND / CFG.HELPER).read_text())
    base, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER})
    result = format_proof.prove(repo, base, head, frontend_dir=empty)
    assert result.exit_code == 2
    assert any("node_modules" in line for line in result.lines), result.lines


def test_prove_is_unknown_when_a_frontend_part_also_edits_markdown(repo: Path) -> None:
    base, head = _reformat(
        repo, {"src/a.ts": TS_BEFORE, "README.md": "# a\n"}, {"src/a.ts": TS_AFTER, "README.md": "# b\n"}
    )
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_prove_leaves_a_ts_file_outside_the_frontend_unproven(repo: Path) -> None:
    base = commit_files(repo, {"tools/a.ts": TS_BEFORE}, "init")
    head = commit_files(repo, {"tools/a.ts": TS_AFTER}, f"style(format): prettier@{PIN} tools")
    assert format_proof.prove(repo, base, head).exit_code == 2


def test_ignore_revs_accepts_a_listed_prettier_commit(repo: Path) -> None:
    _, head = _reformat(repo, {"src/a.ts": TS_BEFORE}, {"src/a.ts": TS_AFTER})
    ignore = repo / format_proof.CFG.IGNORE_REVS_FILE
    ignore.write_text(f"# prettier {PIN} src\n{head}\n")
    result = format_proof.check_ignore_revs(repo, ignore)
    assert result.exit_code == 0, result.lines
