"""Build lineage: which long-lived line a build carries, as a colour (INSTALL-44).

WHY THIS EXISTS

Tue 3 Nov 2026: an agent installed a genre-keys build over the running set-rc
build, and every set fix silently vanished. Both builds looked the same in the
badge: a short sha and an age. So every build is stamped, at build time, with
the line it carries as a colour name, and with whether it descends from the
build that was running when it was made. The badge shows the colour, and an
install that would drop commits says so loudly.

THE RULE

- Each long-lived line in ``build_lineage.json`` owns a fixed colour (main =
  white, af--set-rc = cerulean, af--b2b-link = yellow).
- A line's SHARE is the fraction of its own commits (those not on trunk) the
  build contains. It is CARRIED, and colours the build, at ``carry_threshold``
  (0.5) or more: a main-based branch that picked up a few set-rc commits is
  still main's white, and its ``line_shares`` say what it picked up.
- No line carried: the trunk's colour. One: that line's. Two or more: the
  share-weighted RGB blend, named by the nearest palette colour that is not one
  of the blended lines' own (cerulean + yellow = teal). A line that merged into
  trunk has no own commits left, so the blend ends when it merges.
- ``descends_from_previous`` asks git whether the previous build's commit is an
  ancestor of this one. When not, ``missing_commits`` counts what would be lost
  and ``missing_from`` names the line those commits belong to.

A configured line whose ref is not in this clone is listed in
``unresolved_lines``, never guessed.

Requirements (mini-PRD):

- ✔︎ ✅ 🎯 colour: trunk-only build -> trunk colour; one line -> its colour;
  two lines -> nearest palette blend excluding their own. -> :func:`lineage_color`
- ✔︎ ✅ 🎯 shares are read from real git history. -> :func:`line_shares`
- ✔︎ ✅ 🎯 descent names the missing commit count and their line. -> :func:`descent`

Acceptance tests (tests/engine_core/test_build_lineage.py):

- [if] a build merges af--set-rc and af--b2b-link [then] its colour is teal, else ⛔️
- [if] af--set-rc merges into main [then] a later build of main is white again, else ⛔️
- [if] a genre-keys build forked from main replaces a set-rc build [then]
  descends_from_previous is false and missing_from is af--set-rc, else ⛔️
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LINEAGE_CONFIG_PATH: Path = Path(__file__).with_name("build_lineage.json")
_HEX = re.compile(r"^#[0-9a-f]{6}$")
NOT_DESCENDED_PREFIX: str = "NOT DESCENDED FROM YOUR LAST BUILD"


class LineageError(RuntimeError):
    """Lineage cannot be stated: a bad config, or git refused. Always names why."""


@dataclass(frozen=True)
class LineSpec:
    name: str
    ref: str
    color: str


@dataclass(frozen=True)
class LineageConfig:
    trunk: str
    lines: tuple[LineSpec, ...]
    palette: dict[str, str]
    carry_threshold: float = 0.5

    def color_of(self, line: str) -> str:
        return next(spec.color for spec in self.lines if spec.name == line)


@dataclass(frozen=True)
class Descent:
    descends: bool
    missing_commits: int
    missing_from: str | None


# ----- config ---------------------------------------------------------------
def load_lineage_config(path: Path = LINEAGE_CONFIG_PATH) -> LineageConfig:
    """Read and validate the line colours. A bad file is an error, never a default."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LineageError(f"{path} could not be read as JSON: {error}") from error
    palette = {str(name): str(value).lower() for name, value in raw.get("palette", {}).items()}
    bad = sorted(name for name, value in palette.items() if not _HEX.match(value))
    if bad:
        raise LineageError(f"{path}: palette colours {bad} are not #rrggbb")
    lines = tuple(LineSpec(str(item["name"]), str(item["ref"]), str(item["color"])) for item in raw.get("lines", []))
    unknown = sorted({spec.color for spec in lines} - set(palette))
    if unknown:
        raise LineageError(f"{path}: line colours {unknown} are not in the palette")
    trunk = raw.get("trunk")
    if trunk not in {spec.name for spec in lines}:
        raise LineageError(f"{path}: trunk {trunk!r} is not one of the configured lines")
    threshold = raw.get("carry_threshold")
    if not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        raise LineageError(f"{path}: carry_threshold must be a number in (0, 1], got {threshold!r}")
    return LineageConfig(trunk=str(trunk), lines=lines, palette=palette, carry_threshold=float(threshold))


# ----- colour ---------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def carried_lines(config: LineageConfig, shares: dict[str, float]) -> dict[str, float]:
    """The lines that colour a build: share at or over ``carry_threshold``."""
    return {line: share for line, share in shares.items() if share >= config.carry_threshold}


def lineage_color(config: LineageConfig, shares: dict[str, float]) -> tuple[str, str]:
    """(colour name, #hex) for a build carrying ``shares`` of non-trunk lines."""
    carried = carried_lines(config, shares)
    if not carried:
        name = config.color_of(config.trunk)
        return name, config.palette[name]
    if len(carried) == 1:
        name = config.color_of(next(iter(carried)))
        return name, config.palette[name]
    total = sum(carried.values())
    blend = [
        sum(_rgb(config.palette[config.color_of(line)])[channel] * share for line, share in carried.items()) / total
        for channel in range(3)
    ]
    own = {config.color_of(line) for line in carried} | {config.color_of(config.trunk)}
    candidates = {name: value for name, value in config.palette.items() if name not in own}
    if not candidates:
        raise LineageError("the palette has no colour left to name a blend; add one to build_lineage.json")
    name = min(candidates, key=lambda key: sum((a - b) ** 2 for a, b in zip(_rgb(candidates[key]), blend, strict=True)))
    return name, candidates[name]


# ----- git ------------------------------------------------------------------
@functools.cache
def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise LineageError("git is not on PATH, so lineage cannot be stated")
    return git


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run([_git_executable(), *args], cwd=repo_root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise LineageError(f"git {' '.join(args)} failed in {repo_root}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def _count(repo_root: Path, *revs: str) -> int:
    return int(_git(repo_root, "rev-list", "--count", *revs))


def _resolve(repo_root: Path, ref: str) -> str | None:
    result = subprocess.run(
        [_git_executable(), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def resolved_tips(repo_root: Path, config: LineageConfig) -> tuple[dict[str, str], list[str]]:
    """({line: tip sha}, [lines whose ref this clone does not have])."""
    tips: dict[str, str] = {}
    unresolved: list[str] = []
    for spec in config.lines:
        tip = _resolve(repo_root, spec.ref)
        if tip is None:
            unresolved.append(spec.name)
        else:
            tips[spec.name] = tip
    return tips, unresolved


def line_shares(repo_root: Path, config: LineageConfig, head: str, tips: dict[str, str]) -> dict[str, float]:
    """Fraction of each non-trunk line's own commits that ``head`` contains."""
    trunk_tip = tips.get(config.trunk)
    if trunk_tip is None:
        raise LineageError(f"the trunk {config.trunk!r} ref is not in this clone; lineage cannot be stated")
    shares: dict[str, float] = {}
    for line, tip in tips.items():
        if line == config.trunk:
            continue
        own = _count(repo_root, tip, "--not", trunk_tip)
        if own == 0:
            continue  # merged into trunk: nothing of its own left to carry
        missing = _count(repo_root, tip, "--not", trunk_tip, head)
        if own - missing > 0:
            shares[line] = round((own - missing) / own, 4)
    return shares


def descent(repo_root: Path, config: LineageConfig, previous: str, head: str, tips: dict[str, str]) -> Descent:
    """Does ``head`` contain ``previous``? If not, how much is lost, and from which line."""
    missing = _count(repo_root, previous, "--not", head)
    if missing == 0:
        return Descent(descends=True, missing_commits=0, missing_from=None)
    lost_by_line: dict[str, int] = {}
    for line, tip in tips.items():
        lost_by_line[line] = missing - _count(repo_root, previous, "--not", head, tip)
    non_trunk = {line: lost for line, lost in lost_by_line.items() if line != config.trunk and lost > 0}
    if non_trunk:
        missing_from: str = max(non_trunk, key=lambda line: non_trunk[line])
    elif lost_by_line.get(config.trunk, 0) > 0:
        missing_from = config.trunk
    else:
        missing_from = previous[:8]
    return Descent(descends=False, missing_commits=missing, missing_from=missing_from)


def not_descended_warning(missing_commits: int, missing_from: str | None) -> str:
    """The one loud sentence every surface shows."""
    return f"{NOT_DESCENDED_PREFIX}: missing {missing_commits} commits from {missing_from or 'it'}"


def compute_lineage(
    repo_root: Path,
    *,
    previous_git_sha_full: str | None,
    head: str = "HEAD",
    config: LineageConfig | None = None,
) -> dict[str, Any]:
    """The ``identity.lineage`` stamp for ``head``. ``previous`` None = nothing to compare."""
    config = load_lineage_config() if config is None else config
    head_sha = _git(repo_root, "rev-parse", f"{head}^{{commit}}")
    tips, unresolved = resolved_tips(repo_root, config)
    shares = line_shares(repo_root, config, head_sha, tips)
    color_name, color_hex = lineage_color(config, shares)
    carried = carried_lines(config, shares)
    lines = sorted(carried, key=lambda line: (-carried[line], line)) or [config.trunk]
    stamp: dict[str, Any] = {
        "color_name": color_name,
        "color_hex": color_hex,
        "lines": lines,
        "line_shares": shares,
        "unresolved_lines": unresolved,
        "previous_git_sha_full": previous_git_sha_full,
        "descends_from_previous": None,
        "missing_commits": None,
        "missing_from": None,
    }
    if previous_git_sha_full is not None:
        if _resolve(repo_root, previous_git_sha_full) is None:
            raise LineageError(
                f"the previous build {previous_git_sha_full} is not in this clone; fetch it so descent can be checked"
            )
        found = descent(repo_root, config, previous_git_sha_full, head_sha, tips)
        stamp.update(
            descends_from_previous=found.descends,
            missing_commits=found.missing_commits,
            missing_from=found.missing_from,
        )
    return stamp


__all__ = [
    "LINEAGE_CONFIG_PATH",
    "NOT_DESCENDED_PREFIX",
    "Descent",
    "LineSpec",
    "LineageConfig",
    "LineageError",
    "carried_lines",
    "compute_lineage",
    "descent",
    "line_shares",
    "lineage_color",
    "load_lineage_config",
    "not_descended_warning",
]
