"""Every Playwright config in the repo turns off git info capture.

On GitHub Actions, Playwright's git-commit-info plugin runs BEFORE any
webServer starts. On a `pull_request` event it does a network
`git fetch origin <PR base sha>`, and although Playwright kills git after
3 s it then waits for git's children (the https transport, a credential
helper) to close their pipes. A stalled fetch therefore blocks webServer
startup for as long as the stall lasts (root cause in PR #4419). A config
opts out with a top-level `captureGitInfo: { commit: false, diff: false }`;
there is no environment variable or CLI switch, and no shared base config,
so every config must say it. Nothing in the repo reads the gitCommit or
gitDiff report metadata this drops; `metadata.ci` (commit and PR links) is
still recorded, because it comes from environment variables, not git.

The file set is discovered from `git ls-files`, never listed by hand: a file
counts as a Playwright config when Playwright's own naming picks it up
(`playwright[.name].config.<ext>`) or when it imports from
`@playwright/test` and calls `defineConfig(` / types `PlaywrightTestConfig`.
The check reads the TOP level of the exported object literal, skipping
strings and comments, so a commented-out or `projects`-nested setting does
not count. A config the reader cannot parse fails, it is never skipped.

Configs written at test time (the one inside tests/scripts/test_orphan_cleanup.py)
are not tracked config files and are owned by that test.

Regression lines:
  - [if] a tracked Playwright config has no top-level captureGitInfo [then] broken, [else stop].
  - [if] captureGitInfo commit or diff is not the literal false [then] broken, [else stop].
  - [if] the setting is only in a comment or nested in projects [then] broken, [else stop].
  - [if] a config's exported object cannot be found or parsed [then] broken, [else stop].
  - [if] discovery finds 0 configs or misses playwright.config.ts [then] broken, [else stop].
  - [if] discovery misses a defineConfig file or claims vite's [then] broken, [else stop].
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_CONFIG = "apps/webui/frontend/playwright.config.ts"

CONFIG_EXTENSIONS = (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs")
PLAYWRIGHT_CONFIG_NAME = re.compile(r"(?:^|/)playwright(?:\.[\w-]+)*\.config\.[mc]?[jt]s$")
PLAYWRIGHT_IMPORT = re.compile(r"""from\s+['"](?:@playwright/test|playwright/test)['"]""")
CONFIG_MARKER = re.compile(r"\bdefineConfig\s*\(|\bPlaywrightTestConfig\b")
EXPORT = re.compile(r"\bexport\s+default\b")
EXPORT_START = re.compile(r"export\s+default\s+(?:defineConfig\s*\(\s*)?\{")
REQUIRED_CAPTURE = {"commit": "false", "diff": "false"}


# ----------------------------------------------------------------- discovery


def discover_playwright_configs(repo_root: Path) -> list[str]:
    """Tracked files Playwright would load as a config, by name or by content."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    configs: list[str] = []
    for rel in filter(None, listed):
        if "node_modules/" in rel or not rel.endswith(CONFIG_EXTENSIONS):
            continue
        if PLAYWRIGHT_CONFIG_NAME.search(rel):
            configs.append(rel)
            continue
        source = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        if all(marker.search(source) for marker in (PLAYWRIGHT_IMPORT, CONFIG_MARKER, EXPORT)):
            configs.append(rel)
    return sorted(configs)


# ------------------------------------------------------------------- reading


def _skip_string_or_comment(source: str, i: int) -> int | None:
    """Index just past the string/comment starting at i, or None if none starts there."""
    ch, nxt = source[i], source[i + 1 : i + 2]
    if ch == "/" and nxt == "/":
        end = source.find("\n", i)
        return len(source) if end == -1 else end
    if ch == "/" and nxt == "*":
        end = source.find("*/", i + 2)
        if end == -1:
            raise ValueError("unterminated block comment")
        return end + 2
    if ch in "'\"`":
        j = i + 1
        while j < len(source) and source[j] != ch:
            j += 2 if source[j] == "\\" else 1
        if j >= len(source):
            raise ValueError(f"unterminated string starting at offset {i}")
        return j + 1
    return None


def top_level_text(source: str, open_brace: int) -> str:
    """The characters at depth 1 of the object literal opening at open_brace.

    Strings and comments are dropped, and every nested bracket body is dropped,
    so only the object's own keys and scalar values remain.
    """
    depth, i, kept = 0, open_brace, []
    while i < len(source):
        skipped = _skip_string_or_comment(source, i)
        if skipped is not None:
            if depth == 1:
                kept.append(" ")
            i = skipped
            continue
        ch = source[i]
        if ch in "{[(":
            depth += 1
            if depth == 2:
                kept.append(ch)
        elif ch in "}])":
            depth -= 1
            if depth == 0:
                return "".join(kept)
            if depth == 1:
                kept.append(ch)
        elif depth == 1:
            kept.append(ch)
        i += 1
    raise ValueError("exported object literal is never closed")


def _find_top_level_key(source: str, open_brace: int, key: str) -> int | None:
    """Offset of `key:` at depth 1 of the object at open_brace, skipping strings/comments."""
    pattern = re.compile(rf"(?<![\w$]){key}\s*:")
    depth, i = 0, open_brace
    while i < len(source):
        skipped = _skip_string_or_comment(source, i)
        if skipped is not None:
            i = skipped
            continue
        ch = source[i]
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            if depth == 0:
                return None
        elif depth == 1 and pattern.match(source, i):
            return i
        i += 1
    return None


def capture_violation(source: str) -> str | None:
    """Why this config leaves git info capture on, or None when it is off."""
    start = EXPORT_START.search(source)
    if start is None:
        return "no `export default defineConfig({` / `export default {` object literal found"
    open_brace = start.end() - 1
    try:
        top_level_text(source, open_brace)  # the whole exported object must parse
        key = _find_top_level_key(source, open_brace, "captureGitInfo")
        if key is None:
            return "no top-level `captureGitInfo: { commit: false, diff: false }`"
        value = re.compile(r"captureGitInfo\s*:\s*\{").match(source, key)
        if value is None:
            return "captureGitInfo is not an object literal"
        body = top_level_text(source, value.end() - 1)
    except ValueError as exc:
        return f"cannot parse the exported config: {exc}"
    parts = [part.strip() for part in body.split(",") if part.strip()]
    if not all(re.fullmatch(r"\w+\s*:\s*\S.*", part, re.DOTALL) for part in parts):
        return f"captureGitInfo has an entry that is not `key: value`: {parts}"
    pairs = (part.split(":", 1) for part in parts)
    entries = {name.strip(): value.strip() for name, value in pairs}
    if entries != REQUIRED_CAPTURE:
        return f"captureGitInfo is {entries}, expected {REQUIRED_CAPTURE}"
    return None


# ---------------------------------------------------------------------- tests


def test_every_playwright_config_turns_git_info_capture_off() -> None:
    configs = discover_playwright_configs(REPO_ROOT)
    print(f"discovered {len(configs)} Playwright configs")
    assert len(configs) > 0, "discovery found no Playwright configs, so this guard measured nothing"
    assert ROOT_CONFIG in configs, f"discovery missed {ROOT_CONFIG}, so it is not finding configs"
    violations = {
        rel: reason
        for rel in configs
        if (reason := capture_violation((REPO_ROOT / rel).read_text(encoding="utf-8"))) is not None
    }
    assert not violations, (
        "Playwright configs that leave git info capture on (a network git fetch runs before "
        "the webServer on every pull_request CI run, see PR #4419):\n"
        + "\n".join(f"  {rel}: {reason}" for rel, reason in sorted(violations.items()))
    )


_OFF = "captureGitInfo: { commit: false, diff: false },"


def _config(body: str) -> str:
    """A defineConfig file whose object literal holds body."""
    return f"export default defineConfig({{\n\t{body}\n}});\n"


@pytest.mark.parametrize(
    ("label", "config"),
    [
        ("missing", _config("testDir: '.',")),
        ("commit on", _config("captureGitInfo: { commit: true, diff: false },")),
        ("diff unset", _config("captureGitInfo: { commit: false },")),
        ("expression", _config("captureGitInfo: { commit: false, diff: !!process.env.X },")),
        ("spread after", _config("captureGitInfo: { commit: false, diff: false, ...base },")),
        ("not a literal", _config("captureGitInfo: NO_CAPTURE,")),
        ("commented out", _config(f"// {_OFF}\n\ttestDir: '.',")),
        ("block comment", _config(f"/* {_OFF} */\n\ttestDir: '.',")),
        ("in a string", _config(f"testDir: '{_OFF}',")),
        ("nested in projects", _config(f"projects: [{{ name: 'a', {_OFF} }}],")),
        ("prefixed key", _config(f"x{_OFF}")),
        ("unterminated", _config(f"{_OFF}\n\ttestDir: '.,")),
        ("no export", f"const config = {{ {_OFF} }};\n"),
    ],
)
def test_positive_control_capture_left_on_is_reported(label: str, config: str) -> None:
    assert capture_violation(config) is not None, f"{label}: capture on passed the check"


@pytest.mark.parametrize(
    "config",
    [
        _config(f"{_OFF}\n\ttestDir: '.',"),
        _config("use: { baseURL: 'http://x' }, captureGitInfo: {diff:false,commit:false},"),
        f"export default {{\n\tprojects: [{{ name: 'a' }}],\n\t{_OFF}\n}} satisfies Config;\n",
    ],
)
def test_negative_control_capture_off_passes(config: str) -> None:
    assert capture_violation(config) is None


def test_discovery_finds_configs_by_name_and_content_but_not_vite(tmp_path: Path) -> None:
    define = "export default defineConfig({});\n"
    files = {
        "app/playwright.config.ts": "export default {};\n",
        "e2e/playwright.gate.config.mjs": "export default {};\n",
        "e2e/gate-harness.mts": f"import {{ defineConfig }} from '@playwright/test';\n{define}",
        "app/vite.config.ts": f"import {{ defineConfig }} from 'vite';\n{define}",
        "e2e/thing.spec.ts": "import { test } from '@playwright/test';\ntest('x', () => {});\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    assert discover_playwright_configs(tmp_path) == [
        "app/playwright.config.ts",
        "e2e/gate-harness.mts",
        "e2e/playwright.gate.config.mjs",
    ]
