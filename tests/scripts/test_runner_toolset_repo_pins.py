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
  - if a setup action step that omits its version input stays green because
    another step pins the manifest version then broken
  - if a `repo:` source names one file where its pin can live in several (one
    workflow, one app), so a second file pinning another version stays green,
    then broken
  - if an app nested deeper under apps/ pinning another version stays green, or
    a package.json or lock under node_modules is read as a repo pin, then broken
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Callable
from fnmatch import fnmatch
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
    """Every `with: <key>` of a workflow step that uses `<action>@...`. A step that
    omits the key yields an `unpinned:` value, which can never equal a pin: the
    action then picks a PATH or default toolchain, whatever other steps pin."""

    def derive(paths: list[Path]) -> set[str]:
        values: set[str] = set()
        for step in _workflow_steps(paths):
            uses = str(step.get("uses", ""))
            if uses.split("@", maxsplit=1)[0] == action:
                value = (step.get("with") or {}).get(key)
                values.add(
                    str(value).removeprefix("v") if value is not None else f"unpinned: {uses}"
                )
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


def _workflow_env(var: str) -> Callable[[list[Path]], set[str]]:
    def derive(paths: list[Path]) -> set[str]:
        found: set[str] = set()
        for path in paths:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for job in (document.get("jobs") or {}).values():
                for step in job.get("steps") or []:
                    value = (step.get("env") or {}).get(var)
                    if value is not None:
                        found.add(str(value))
        return found

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
    "cargo-nextest": _workflow_env("CARGO_NEXTEST_VERSION"),
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


def _source_files(root: Path, glob: str) -> set[Path]:
    """The files one `repo:` glob names. `<dir>/**/<name>` walks <dir> at any depth
    but prunes every `node_modules` segment: an installed dependency's package.json
    or lockfile is a pin that dependency makes, not one this repo makes."""
    if "/**/" not in glob:
        return set(root.glob(glob))
    top, name = glob.split("/**/", 1)
    found: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root / top):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        found |= {Path(dirpath) / f for f in filenames if fnmatch(f, name)}
    return found


def repo_pin_mismatches(entries: list[dict], root: Path) -> list[str]:
    """Every `repo:`-sourced entry whose source file pins something else."""
    problems = []
    for entry in entries:
        if not str(entry["source"]).startswith("repo:"):
            continue
        name, pattern = entry["name"], entry["source"].removeprefix("repo:").split(" ")[0]
        paths, derive, expected = (
            sorted({path for glob in pattern.split(",") for path in _source_files(root, glob)}),
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
    "toolcache-sccache": ("mozilla-actions/sccache-action", "version", "0.18.0"),
}

WORKFLOW_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
# Deriver -> every file its pin can live in. GitHub runs any `*.yml` or `*.yaml`
# workflow; corepack and Playwright read the package.json / pnpm-lock of whichever
# app they run in, at any depth under apps/ (a spike nests three deep); the repo has
# one uv project, the root.
SOURCE_GLOBS: dict[str, tuple[str, ...]] = {
    "_action_input": WORKFLOW_GLOBS,
    "_cargo_install": WORKFLOW_GLOBS,
    "_package_manager": ("apps/**/package.json",),
    "_pnpm_lock": ("apps/**/pnpm-lock.yaml",),
    "_uv_lock": ("uv.lock",),
    "_workflow_env": WORKFLOW_GLOBS,
}


def _source_globs(entry: dict) -> list[str]:
    return str(entry["source"]).removeprefix("repo:").split(" ")[0].split(",")


def test_every_repo_pin_source_globs_every_file_its_pin_can_live_in(manifest: dict) -> None:
    """A source naming one file (a single workflow, one app's lockfile) lets a
    second file pin another version while this test compares only the first.
    Keyed on the deriver, so a new entry inherits the rule instead of a list."""
    kind = {name: derive.__qualname__.split(".")[0] for name, derive in REPO_PINS.items()}
    assert set(kind.values()) == set(SOURCE_GLOBS), kind
    narrow = {
        e["name"]: e["source"]
        for e in manifest["entries"]
        if e["name"] in kind and sorted(_source_globs(e)) != sorted(SOURCE_GLOBS[kind[e["name"]]])
    }
    assert not narrow, narrow


SECOND_FILE_PINS: dict[str, tuple[str, str, Callable[[str], str], str]] = {
    "cargo-audit": (
        ".github/workflows/periodic-checks.yml",
        ".github/workflows/other.yaml",
        lambda version: _cargo_audit_step(version, PINNED_INSTALL),
        "0.22.3",
    ),
    "pnpm": (
        "apps/webui/frontend/package.json",
        "apps/launcher/package.json",
        lambda version: json.dumps({"packageManager": f"pnpm@{version}"}),
        "12.0.0",
    ),
    "playwright-chromium": (
        "apps/webui/frontend/pnpm-lock.yaml",
        "apps/desktop/pnpm-lock.yaml",
        lambda version: f"packages:\n  playwright@{version}:\n    resolution: {{}}\n",
        "1.62.0",
    ),
}


@pytest.mark.parametrize("name", sorted(SECOND_FILE_PINS))
def test_a_second_file_pinning_another_version_goes_red(
    manifest: dict, tmp_path: Path, name: str
) -> None:
    """The file that agrees with the manifest (green, the control) does not vouch
    for another file in the same scope that pins something else."""
    agreeing, other, text, bumped = SECOND_FILE_PINS[name]
    entry = _entry_named(manifest, name)
    pin = _expected_pin(entry)
    assert pin, entry
    _write(tmp_path, agreeing, text(pin))
    assert repo_pin_mismatches([entry], tmp_path) == []
    _write(tmp_path, other, text(bumped))
    problems = repo_pin_mismatches([entry], tmp_path)
    assert len(problems) == 1 and bumped in problems[0], problems


@pytest.mark.parametrize(
    ("other", "red"),
    [("apps/launcher/spikes/djay-drag", True), ("apps/webui/frontend/node_modules/dep", False)],
    ids=["deep-app", "node_modules-control"],
)
@pytest.mark.parametrize("name", ["pnpm", "playwright-chromium"])
def test_a_deep_app_pin_goes_red_and_node_modules_is_ignored(
    manifest: dict, tmp_path: Path, name: str, other: str, red: bool
) -> None:
    """An app nested at any depth pins what its tools run; a dependency installed
    under node_modules pins only itself, so its package.json or lock is not read."""
    agreeing, _, text, bumped = SECOND_FILE_PINS[name]
    entry = _entry_named(manifest, name)
    pin = _expected_pin(entry)
    assert pin, entry
    _write(tmp_path, agreeing, text(pin))
    _write(tmp_path, f"{other}/{Path(agreeing).name}", text(bumped))
    problems = repo_pin_mismatches([entry], tmp_path)
    assert bool(problems) is red and all(bumped in problem for problem in problems), problems


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


ACTION_INPUTS = {name: (action, key) for name, (action, key, _) in WORKFLOW_ACTIONS.items()}


@pytest.mark.parametrize("pinned_twice", [False, True], ids=["one-unpinned", "control"])
@pytest.mark.parametrize("name", sorted(ACTION_INPUTS))
def test_a_setup_step_without_its_version_input_goes_red(
    manifest: dict, tmp_path: Path, name: str, pinned_twice: bool
) -> None:
    """One step pinning the manifest version does not vouch for another step of the
    same action that omits the input and so resolves a default toolchain."""
    action, key = ACTION_INPUTS[name]
    entry = _entry_named(manifest, name)
    pin = _expected_pin(entry)
    assert pin, entry
    first_glob = _source_globs(entry)[0]
    pinned = {"uses": f"{action}@abc", "with": {key: pin}}
    second = pinned if pinned_twice else {"uses": f"{action}@abc"}
    steps = {"jobs": {"j": {"steps": [pinned, second]}}}
    _write(tmp_path, first_glob.replace("*", "a"), yaml.safe_dump(steps))
    problems = repo_pin_mismatches([entry], tmp_path)
    if pinned_twice:
        assert problems == [], problems
    else:
        assert len(problems) == 1 and "unpinned" in problems[0], problems
