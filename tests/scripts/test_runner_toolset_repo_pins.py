"""Every `repo:`-sourced pin in ci/runner-toolset.yml matches the file it names.

Split out of tests/scripts/test_runner_toolset_complete.py: that file checks
the manifest declares every tool CI uses; this one checks the declared versions
of repo-backed entries (packageManager, action inputs, lockfiles, a pinned
`cargo install`) track their source, so a bump in the source goes red here
before a host is provisioned against a stale pin.

Regression lines:
  - if a `repo:` pin moves in its source file and the manifest keeps the old
    version then broken
  - if one source glob holds two different pins and the test stays green then broken
  - if a pin written equivalently in its source (a `v` prefix, a pnpm integrity
    suffix) goes red then broken
  - if the cargo-audit install is unpinned, or pinned through an unset variable,
    and the test stays green then broken
  - if a `.yaml` workflow pins another version than the manifest and the test
    stays green, or a workflow-backed source names `*.yml` without `*.yaml`,
    then broken
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts import runner_toolset_scan as scan


@pytest.fixture(scope="module")
def manifest() -> dict:
    return scan.load_manifest()


# ----- repo-backed pins match their source -------------------------------------------------


def _workflow_steps(paths: list[Path]) -> list[dict]:
    steps: list[dict] = []
    for path in paths:
        jobs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}
        for job in jobs.values():
            steps += job.get("steps") or []
    return steps


def _action_input(action: str, key: str) -> Callable[[list[Path]], set[str]]:
    """Every `with: <key>` of a workflow step that uses `<action>@...`."""

    def derive(paths: list[Path]) -> set[str]:
        values: set[str] = set()
        for step in _workflow_steps(paths):
            if str(step.get("uses", "")).split("@")[0] == action:
                value = (step.get("with") or {}).get(key)
                values |= {str(value).removeprefix("v")} if value is not None else set()
        return values

    return derive


CARGO_INSTALL_VERSION_RE = re.compile(r'--version[= ]"?\$\{?([A-Za-z_]\w*)\}?"?')


def _cargo_install(crate: str) -> Callable[[list[Path]], set[str]]:
    """The version every `cargo install <crate>` line in a workflow step installs:
    its `--version "$VAR"`, read from that step's `env`. An install without a
    `--version` yields an `unpinned:` value, which can never equal a pin."""

    def derive(paths: list[Path]) -> set[str]:
        values: set[str] = set()
        for step in _workflow_steps(paths):
            for line in re.findall(
                rf"cargo install {re.escape(crate)}\b.*", str(step.get("run", ""))
            ):
                pinned = CARGO_INSTALL_VERSION_RE.search(line)
                value = (step.get("env") or {}).get(pinned.group(1)) if pinned else None
                values.add(str(value) if value is not None else f"unpinned: {line.strip()}")
        return values

    return derive


def _package_manager(tool: str) -> Callable[[list[Path]], set[str]]:
    def derive(paths: list[Path]) -> set[str]:
        found = [json.loads(p.read_text(encoding="utf-8")).get("packageManager", "") for p in paths]
        return {pm.split("@", 1)[1].split("+")[0] for pm in found if pm.startswith(f"{tool}@")}

    return derive


def _uv_lock(package: str) -> Callable[[list[Path]], set[str]]:
    def derive(paths: list[Path]) -> set[str]:
        return {
            pkg["version"]
            for p in paths
            for pkg in tomllib.loads(p.read_text(encoding="utf-8")).get("package", [])
            if pkg.get("name") == package
        }

    return derive


def _pnpm_lock(package: str) -> Callable[[list[Path]], set[str]]:
    def derive(paths: list[Path]) -> set[str]:
        return {
            key.rsplit("@", 1)[1].split("(")[0]
            for p in paths
            for key in (yaml.safe_load(p.read_text(encoding="utf-8")).get("packages") or {})
            if key.rsplit("@", 1)[0] == package
        }

    return derive


# Entry name -> how to read the pin out of the file its `source: repo:` names.
REPO_PINS: dict[str, Callable[[list[Path]], set[str]]] = {
    "pnpm": _package_manager("pnpm"),
    "cargo-audit": _cargo_install("cargo-audit"),
    "rust-cargo": _action_input("dtolnay/rust-toolchain", "toolchain"),
    "rust-rustc": _action_input("dtolnay/rust-toolchain", "toolchain"),
    "toolcache-python": _action_input("actions/setup-python", "python-version"),
    "toolcache-node": _action_input("actions/setup-node", "node-version"),
    "toolcache-sccache": _action_input("mozilla-actions/sccache-action", "version"),
    "toolcache-python-pytest": _uv_lock("pytest"),
    **{
        f"playwright-{browser}": _pnpm_lock("playwright")
        for browser in ("chromium", "chromium-headless-shell", "webkit", "ffmpeg")
    },
}


def _expected_pin(entry: dict) -> str | None:
    """The value the source must pin. A Playwright entry's version is a browser
    revision that the Playwright release fixes, so its source names that release."""
    if entry["kind"] != "playwright":
        return str(entry["version"])
    stated = re.search(r"\(playwright ([\w.-]+)\)", entry["source"])
    return stated.group(1) if stated else None


def repo_pin_mismatches(entries: list[dict], root: Path) -> list[str]:
    """Every `repo:`-sourced entry whose source file pins something else."""
    problems = []
    for entry in entries:
        if not str(entry["source"]).startswith("repo:"):
            continue
        name, pattern = entry["name"], entry["source"].removeprefix("repo:").split(" ")[0]
        paths, derive, expected = (
            sorted({path for glob in pattern.split(",") for path in root.glob(glob)}),
            REPO_PINS.get(name),
            _expected_pin(entry),
        )
        if derive is None or expected is None or not paths:
            problems.append(f"{name}: no deriver, stated pin or file for {entry['source']!r}")
        elif (found := derive(paths)) != {expected}:
            problems.append(f"{name}: manifest says {expected}, {pattern} pins {sorted(found)}")
    return problems


def test_repo_backed_versions_match_their_sources(manifest: dict) -> None:
    problems = repo_pin_mismatches(manifest["entries"], scan.REPO_ROOT)
    assert not problems, "\n  ".join(["repo pins drifted from the manifest:", *problems])


def test_every_repo_pin_deriver_has_an_entry(manifest: dict) -> None:
    repo_sourced = {e["name"] for e in manifest["entries"] if str(e["source"]).startswith("repo:")}
    assert set(REPO_PINS) == repo_sourced, set(REPO_PINS) ^ repo_sourced


def _entry_named(manifest: dict, name: str) -> dict:
    return next(e for e in manifest["entries"] if e["name"] == name)


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def _workflow(uses: str, key: str, value: str) -> str:
    return yaml.safe_dump(
        {"jobs": {"j": {"steps": [{"uses": f"{uses}@abc", "with": {key: value}}]}}}
    )


def _cargo_audit_step(version: str, install: str) -> str:
    step = {"name": "Ensure cargo-audit", "env": {"CARGO_AUDIT_VERSION": version}, "run": install}
    return yaml.safe_dump({"jobs": {"j": {"steps": [step]}}})


PINNED_INSTALL = 'cargo install cargo-audit --locked --force --version "$CARGO_AUDIT_VERSION"\n'


@pytest.mark.parametrize(
    ("version", "install", "green"),
    [
        ("0.22.2", PINNED_INSTALL, True),
        ("0.22.3", PINNED_INSTALL, False),
        (
            "0.22.2",
            "command -v cargo-audit >/dev/null || cargo install cargo-audit --locked\n",
            False,
        ),
        ("0.22.2", 'cargo install cargo-audit --locked --version "$OTHER_VERSION"\n', False),
    ],
)
def test_the_cargo_audit_pin_is_read_from_its_install_line(
    manifest: dict, tmp_path: Path, version: str, install: str, green: bool
) -> None:
    """Mutation controls both ways: the workflow installing another version, not
    pinning at all, or pinning through an unset variable goes red; the same
    version installed through its step env stays green."""
    _write(tmp_path, ".github/workflows/periodic-checks.yml", _cargo_audit_step(version, install))
    problems = repo_pin_mismatches([_entry_named(manifest, "cargo-audit")], tmp_path)
    assert (problems == []) is green, problems


@pytest.mark.parametrize(
    ("name", "rel", "text"),
    [
        ("pnpm", "apps/webui/frontend/package.json", '{"packageManager": "pnpm@11.10.0"}'),
        (
            "rust-rustc",
            ".github/workflows/a.yml",
            _workflow("dtolnay/rust-toolchain", "toolchain", "1.97.0"),
        ),
        (
            "toolcache-sccache",
            ".github/workflows/ci.yml",
            _workflow("mozilla-actions/sccache-action", "version", "v0.18.0"),
        ),
        ("toolcache-python-pytest", "uv.lock", '[[package]]\nname = "pytest"\nversion = "9.2.0"\n'),
        (
            "playwright-webkit",
            "apps/webui/frontend/pnpm-lock.yaml",
            "packages:\n  playwright@1.62.0:\n    resolution: {}\n",
        ),
    ],
)
def test_a_bumped_repo_pin_goes_red(
    manifest: dict, tmp_path: Path, name: str, rel: str, text: str
) -> None:
    """Mutation control: the source moves, the manifest does not."""
    _write(tmp_path, rel, text)
    problems = repo_pin_mismatches([_entry_named(manifest, name)], tmp_path)
    assert len(problems) == 1 and name in problems[0], problems


def test_two_different_pins_in_one_source_glob_go_red(manifest: dict, tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/a.yml",
        _workflow("actions/setup-node", "node-version", "22.14.0"),
    )
    _write(
        tmp_path,
        ".github/workflows/b.yml",
        _workflow("actions/setup-node", "node-version", "24.1.0"),
    )
    problems = repo_pin_mismatches([_entry_named(manifest, "toolcache-node")], tmp_path)
    assert problems and "24.1.0" in problems[0], problems


@pytest.mark.parametrize(
    ("name", "rel", "text"),
    [
        (
            "pnpm",
            "apps/webui/frontend/package.json",
            '{"packageManager": "pnpm@11.9.0+sha512.abc"}',
        ),
        (
            "toolcache-sccache",
            ".github/workflows/ci.yml",
            _workflow("mozilla-actions/sccache-action", "version", "v0.17.0"),
        ),
        (
            "playwright-chromium",
            "apps/webui/frontend/pnpm-lock.yaml",
            "packages:\n  playwright@1.61.1:\n    resolution: {}\n"
            "  playwright-core@1.62.0:\n    resolution: {}\n",
        ),
    ],
)
def test_an_equivalent_repo_pin_stays_green(
    manifest: dict, tmp_path: Path, name: str, rel: str, text: str
) -> None:
    """Overshoot control: an integrity hash, a `v` prefix or a sibling package
    with a different version is the same pin, not drift."""
    _write(tmp_path, rel, text)
    assert repo_pin_mismatches([_entry_named(manifest, name)], tmp_path) == []


WORKFLOW_ACTIONS = {
    "rust-cargo": ("dtolnay/rust-toolchain", "toolchain", "1.97.0"),
    "rust-rustc": ("dtolnay/rust-toolchain", "toolchain", "1.97.0"),
    "toolcache-python": ("actions/setup-python", "python-version", "3.12"),
    "toolcache-node": ("actions/setup-node", "node-version", "24.1.0"),
}


def _workflow_globbed(entry: dict) -> list[str]:
    """The source's globs that pick workflows by wildcard (not one named file)."""
    pattern = str(entry["source"]).removeprefix("repo:").split(" ")[0]
    return [glob for glob in pattern.split(",") if glob.startswith(".github/workflows/*")]


def test_every_workflow_backed_source_reads_both_workflow_extensions(manifest: dict) -> None:
    """GitHub runs `.github/workflows/*.yml` and `*.yaml`, so a source that globs
    workflows must read both, or a `.yaml` workflow's pin is never compared."""
    globbed = {e["name"]: _workflow_globbed(e) for e in manifest["entries"]}
    globbed = {name: globs for name, globs in globbed.items() if globs}
    assert set(globbed) == set(WORKFLOW_ACTIONS), set(globbed) ^ set(WORKFLOW_ACTIONS)
    wanted = [".github/workflows/*.yml", ".github/workflows/*.yaml"]
    short = {name: globs for name, globs in globbed.items() if sorted(globs) != sorted(wanted)}
    assert not short, short


@pytest.mark.parametrize("name", sorted(WORKFLOW_ACTIONS))
def test_a_yaml_workflow_pinning_another_version_goes_red(
    manifest: dict, tmp_path: Path, name: str
) -> None:
    """A `.yml` workflow agreeing with the manifest does not vouch for a `.yaml`
    one that pins something else."""
    action, key, bumped = WORKFLOW_ACTIONS[name]
    entry = _entry_named(manifest, name)
    _write(tmp_path, ".github/workflows/a.yml", _workflow(action, key, str(entry["version"])))
    assert repo_pin_mismatches([entry], tmp_path) == []
    _write(tmp_path, ".github/workflows/b.yaml", _workflow(action, key, bumped))
    problems = repo_pin_mismatches([entry], tmp_path)
    assert problems and bumped in problems[0], problems
