"""Title-fix DJ project-file rules and the JEV project judge.

Split out of `apps/tags/title_fix.py` (#9686); the mini-PRD for these rules lives in that module's docstring.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .title_fix_model import CFG, Track
from .title_fix_openrouter import (
    Api,
    _parse_billed,
    _post,
    _probability,
    _required_cost,
)

# ----------------------------------------------------------------- DJ project files
#: Ported from the round-5 prototype (strong rules AND JEV), measured on the private projects fixture set (scripts/title_fix_eval.py).


class Proj:
    SAMPLER_FOLDER = re.compile(r"/(?:PioneerDJ|rekordbox)/Sampler/", re.I)
    RECORDING_FOLDER = re.compile(r"/(?:PioneerDJ|rekordbox)/Recording/", re.I)
    SAMPLER_FILE = re.compile(r"MergeFX Sample Sound", re.I)
    CAPTURE_FILE = re.compile(r"_capture(?:\(\d+\))?\.(?:wav|aiff?)$", re.I)
    REC_FILE = re.compile(r"(?:^|\s)REC-\d{4}-\d{2}-\d{2}")
    MASTERING_SERVICE = re.compile(
        r"^LANDR-|(?<![A-Za-z])e-?mastered|-(?:Balanced|Warm|Open)-(?:Low|Medium|High)\b", re.I
    )
    DAW_EXPORT_CS = re.compile(r"(?<![A-Za-z])MASTER(?:ED)?(?![A-Za-z])")  # case-sensitive: "AA 1644 MASTER"
    DAW_EXPORT_CI = re.compile(r"_mastered|_mixdown\d*|_bounce\d*|(?<![a-z])(?:16|24|32)[ -]?bit(?![a-z])", re.I)
    GIG_WORDS = re.compile(r"\b(?:intro|outro|dj tool|sting|jingle|birthday|drop)\b", re.I)
    ALIAS_PREFIX = re.compile(r"^([A-Z][A-Z0-9]{2,})_")
    PROMO_OR_STORE = re.compile(r"_pn\.(?:wav|aiff?)$|^\d{6,}_", re.I)
    LOSSLESS = (".wav", ".aif", ".aiff")


def dj_aliases() -> tuple[str, ...]:
    """The library owner's own artist names, from per-user config only. Empty when unset."""
    return tuple(a.strip() for a in os.environ.get(CFG.DJ_ALIASES_ENV, "").split(",") if a.strip())


def _known_absent(value: str | int | None) -> bool:
    return value is not None and not value


def _is_daw_export(track: Track, name: str, *, released: bool, promo: bool) -> bool:
    if released or promo:
        return False
    return bool(Proj.DAW_EXPORT_CS.search(name) or Proj.DAW_EXPORT_CI.search(track.filename))


def _is_gig_intro_or_sting(track: Track, name: str, *, sampler: bool, bare: bool, promo: bool) -> bool:
    """A lossless, artist-less, metadata-less file named like a gig intro, or a clip too short to be a song.
    A sampler one-shot is a sample-pack sound, not a gig intro (round 7, both judges; 25 library rows)."""
    lossless = track.filename.lower().endswith(Proj.LOSSLESS)
    if sampler or not bare or promo or not lossless or track.artist.strip():
        return False
    short_clip = track.length_s is not None and 0 < track.length_s < CFG.STING_MAX_S
    return bool(Proj.GIG_WORDS.search(name)) or short_clip


def _is_own_alias(track: Track, *, released: bool) -> bool:
    aliases = {a.casefold() for a in dj_aliases()}
    if released or not aliases:
        return False
    prefix = Proj.ALIAS_PREFIX.match(track.filename)
    return track.artist.strip().casefold() in aliases or bool(prefix and prefix.group(1).casefold() in aliases)


def _weak_project_rules(track: Track, strong: tuple[str, ...], *, bare: bool, promo: bool) -> tuple[str, ...]:
    lossless = track.filename.lower().endswith(Proj.LOSSLESS)
    no_artist = not track.artist.strip()
    return ("lossless-no-metadata",) if not strong and bare and lossless and no_artist and not promo else ()


def project_rules(track: Track) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Returns (strong, weak) rule names. Unknown metadata (None) never counts as absent."""
    path, name = f"{track.folder}/{track.filename}", f"{track.title} | {track.filename}"
    released = bool(track.album) or bool(track.label)
    bare = _known_absent(track.album) and _known_absent(track.label) and _known_absent(track.year)
    promo = bool(Proj.PROMO_OR_STORE.search(track.filename))
    sampler = bool(Proj.SAMPLER_FOLDER.search(path) or Proj.SAMPLER_FILE.search(track.filename))
    checks = (
        ("sampler", sampler),
        ("capture", bool(Proj.CAPTURE_FILE.search(track.filename))),
        ("recording", bool(Proj.RECORDING_FOLDER.search(path) or Proj.REC_FILE.search(track.filename))),
        ("mastering-service", bool(Proj.MASTERING_SERVICE.search(track.filename))),
        ("daw-export", _is_daw_export(track, name, released=released, promo=promo)),
        ("gig-intro-or-sting", _is_gig_intro_or_sting(track, name, sampler=sampler, bare=bare, promo=promo)),
        ("own-alias", _is_own_alias(track, released=released)),
    )
    strong = tuple(rule for rule, fired in checks if fired)
    return strong, _weak_project_rules(track, strong, bare=bare, promo=promo)


def project_decision(strong: tuple[str, ...], weak: tuple[str, ...], p_project: float) -> str:
    """ "project", "review" (strong rules that JEV disputes) or "" (not a project file)."""
    if strong and p_project >= CFG.PROJECT_THRESHOLD:
        return "project"
    if strong:
        return "review"
    if weak and p_project >= CFG.PROJECT_MAYBE_THRESHOLD:
        return "project"
    return ""


def project_tags(rules: tuple[str, ...]) -> tuple[str, ...]:
    return ("dj-project", *(f"dj-project-{rule}" for rule in rules))


PROJECT_INSTRUCTIONS = (
    "Is this library row a DJ's OWN PROJECT FILE rather than a released song? Answer YES for: "
    "(1) anything in a Sampler, Capture or Recording folder, including one-shots (CLAP, HORN, LIFTER) "
    "even when a sample-pack name like Loopmasters fills the artist field, and '_capture' grabs of released "
    "songs; (2) the DJ's own masters, bounces, mixdowns and renders ('MASTER', '_mastered', '_mixdown', "
    "'LANDR-', 'eMastered', '24bit', version tags like 'v5-Balanced-Medium'); (3) short intros, outros, "
    "stings, DJ tools or edits the DJ made for a specific gig, venue, party or person, usually a lossless "
    "file with no artist, album or label (a clip under 20 s with no metadata is a sting); (4) any file "
    "whose artist or filename prefix is one of the library owner's own DJ names. Answer NO for songs "
    "released by other artists, INCLUDING remixes, edits, bootlegs, mashups, white labels, unreleased IDs, "
    "promo files ('_pn', Beatport numeric ids), YouTube rips, downloaded DJ sets by named artists, and "
    "album tracks titled 'Intro', 'Outro', 'Untitled', 'Remastered' or with 'Project' or 'Master' inside a "
    "band or remix name. Missing metadata alone is not enough for YES."
)


def _describe_project(track: Track) -> str:
    def known(value: str | int | None) -> str:
        return "unknown" if value is None else ("present" if value else "ABSENT")

    def num(value: int | float | None, unit: str) -> str:
        return "unknown" if value is None else f"{value}{unit}"

    ext = Path(track.filename).suffix.lower() or "(none)"
    return (
        f"title: {track.title}\nartist: {track.artist or '(blank)'}\nfilename: {track.filename}\n"
        f"folder: {track.folder or 'unknown'}\nformat: {ext}, {num(track.bit_depth, '-bit')}, "
        f"{num(track.sample_rate, ' Hz')}, {num(track.length_s, ' s')}\n"
        f"library owner's own DJ names: {', '.join(dj_aliases()) or '(none configured)'}\n"
        f"metadata: album {known(track.album)}, label {known(track.label)}, release year {known(track.year)}"
    )


def judge_project_with_jev(track: Track, api: Api) -> tuple[float, float]:
    """Returns (p_project, cost). Raises on any failed call."""
    data = _post(
        CFG.JEV_URL,
        {
            "model": CFG.JEV_MODEL,
            "state": _describe_project(track),
            "questions": {"project": {"type": "noul", "instructions": PROJECT_INSTRUCTIONS}},
        },
        api,
    )
    return _parse_billed(
        data, lambda: (_probability(data["answers"]["project"]["noul"], "project.noul"), _required_cost(data))
    )
