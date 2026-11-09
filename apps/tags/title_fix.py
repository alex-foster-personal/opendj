"""Track title auto-fixer: regex proposes, JEV judges, DeepSeek breaks ties.

Mini-PRD
--------
✔︎ R1 regex_propose strips download junk, MIK key/energy prefixes, key/bpm
   suffixes and track numbers, and lifts an embedded artist out of the title.
   [if] "9A - 7 - Lumenfall" then title "Lumenfall", key 9A, energy 7 [else ⛔️]
   [if] "Video Gardens" by Ida Verrin then unchanged ("video" is not junk) [else ⛔️]
   [if] artist blank, title "Vessel Choir - Ember Tide [Official Music Video]" then
        artist "Vessel Choir", title "Ember Tide" [else ⛔️]
✔︎ R2 judge_with_jev asks JEV (one call) whether the row is music and whether
   the regex proposal is right; a failed call is UNKNOWN, never a pass.
   [if] JEV HTTP error then Verdict.status == "unknown" and nothing applied [else ⛔️]
✔︎ R3 fix_track applies the regex proposal only when JEV agrees (p >= CFG
   threshold); otherwise DeepSeek extracts artist/title and the row is marked
   for review unless DeepSeek independently matches the regex.
   [if] JEV p_ok 0.3 and DeepSeek == regex then source "regex+deepseek" [else ⛔️]
✔︎ R5 version_tags derives kebab-case Track.tags from the cleaned title; the title itself
   keeps its version text ("Original Mix" stays, and also tags original-mix).
   [if] "Neon Harbor (Original Mix)" then tags ("original-mix",) and title unchanged [else ⛔️]
   [if] "Opal - Marrow Remix" then tags ("remix",) [else ⛔️]
   [if] "Remixed Feelings" then no tags (whole-word match only) [else ⛔️]
✔︎ R6 (round 4) leading numbers and junk: a bare "404 - " is a track number only with a filled
   artist or another split after it; a lone "3 - " is MIK energy only with a key or a filled
   artist; cue times, **NEW**, [NCS Release], "- YouTube", Y2Mate tails and full-width
   brackets are junk; "Gloam-404-All The Quiet Things" splits, "Re-Wander" does not; a
   version-only right side ("Slow Harbor - 2003 Remastered") never becomes a split.
   [if] "404 - Cinder", blank artist, then artist "404", title "Cinder" [else ⛔️]
   [if] "3 - Vantor Quill - Glasswork", blank artist, then energy "" and artist "Vantor Quill" [else ⛔️]
   [if] "Slow Harbor - 2003 Remastered", blank artist, then artist "" (review) [else ⛔️]
✔︎ R7 (round 4) a MIK-strip-only change on a filled artist auto-applies at JEV p >= 0.5; a
   DeepSeek artist that appears nowhere in the row is never used (review with the regex artist).
   [if] JEV 0.69 on "6 - Evergreen - ..." by a filled artist then regex+jev, no DeepSeek [else ⛔️]
   [if] DeepSeek says "The 1980" for "1980 - Velvet" then review with artist "1980" [else ⛔️]
✔︎ R8 (round 5) a filled artist field stays verbatim, EXCEPT when it holds the title or a site
   watermark: then a cleaned artist is proposed and the row ALWAYS goes to review.
   [if] artist "Dun Harlow - Say Less (Original Mix) DANCEDJ.CLUB" then artist "Dun Harlow", review [else ⛔️]
   [if] artist "Tallow,Marrow" on "Opal - Marrow Remix" then artist unchanged [else ⛔️]
✔︎ R9 (round 5) soundtrack and unreleased context stays in the title AND is tagged.
   [if] "Pale Hare (Sunset Drive VA)" then title unchanged, tags ("soundtrack",) [else ⛔️]
   [if] "X (Remix) [Unreleased]" then tags ("remix", "unreleased") [else ⛔️]
✔︎ R10 (round 5) explicit covers: performer is the artist, original_artist is filled, a
   "(Guitar Cover)"-style marker stays in the title, tags cover (+ instrument) or karaoke.
   ALWAYS review, never auto-applied.
   [if] 'Iron Fern "Quiet Engine" - guitar cover by fretmoth' then fretmoth / "Quiet Engine
        (Guitar Cover)", original Iron Fern, review [else ⛔️]
   [if] "Salute" by Pine Ogre or "Recovery" then no cover [else ⛔️]
   [if] karaoke then "Originally Performed by" stays in the title [else ⛔️]
✔︎ R11 (round 5) DJ project files: strong code rules AND JEV P(project) >= 0.7 give status
   "project" with tags dj-project + a rule sub-tag; the title is never rewritten. Strong rules
   that JEV disputes go to review. The weak lossless-no-metadata band needs JEV >= 0.8. Owner
   aliases come from $OPENDJ_DJ_ALIASES only (default empty, never committed).
   [if] "Low Ember_capture.wav" then rule "capture" fires [else ⛔️]
   [if] a "_pn.wav" promo with no artist then no rule fires [else ⛔️]
   [if] OPENDJ_DJ_ALIASES is unset then the own-alias rule never fires [else ⛔️]
✔︎ R12 (round 6) provenance tags from the raw row: mik-analyzed, low-bitrate, streaming-only,
   youtube-rip, beatport-sourced, duplicate-copy (tag only; merging belongs to apps/dedup).
   [if] "8A - 6 - X" by a filled artist then mik-analyzed and Proposal.mik_analyzed [else ⛔️]
   [if] a 128 kbps mp3 then low-bitrate; a 128 kbps reading on a .wav or no bitrate then not [else ⛔️]
   [if] filename "Matinee(1).mp3" then duplicate-copy; "Marlowe Dune (Pt. 1).mp3" then not [else ⛔️]
✔︎ R13 (round 6) version family and DJ utility tags: re-edit, rerub, clean, explicit, intro,
   outro, dj-tool, video-source; live-set / dj-mix for blank-artist rows over 20 minutes.
   [if] "Rather Stay - Clean Orchard Remix" then no clean tag [else ⛔️]
   [if] album track "Intro" then no intro tag; "Song (Intro Edit)" then intro [else ⛔️]
   [if] a 22-minute album track with a filled artist then no dj-mix tag [else ⛔️]
✔︎ R14 (round 6) featured artists from feat./ft./featuring/(with X) in the title AND artist field
   go to Proposal.featured_artists; the title text is kept as-is.
   [if] "Love Song (feat. Glasses)" then featured ("Glasses",) and title unchanged [else ⛔️]
   [if] "Featuring Love" by A then no featured artists [else ⛔️]
✔︎ R15 (round 6) "- prod. X" / "(prod. X)" moves to Proposal.producer; a trailing "- A Vs B" tail
   that only repeats the artist field is stripped.
   [if] "Freed From Doubt - prod. Marrow Vell, Pip Jay" then title "Freed From Doubt" [else ⛔️]
   [if] "Dust - Remy Calder Vs. Cotonwell Remix" then unchanged [else ⛔️]
✔︎ R16 (round 6) ALL CAPS goes to review only with 3+ words AND junk/credit words; mojibake is
   re-decoded only when the round trip is clean; a file extension in the title is stripped; a
   known Windows-illegal substitution is undone and an unknown one goes to review; an exactly
   30-char title extended by the filename is proposed for review.
   [if] "RIOT", "MODEST.", "S.W.A.Y.", "I WON'T GO" then no review [else ⛔️]
   [if] "Der B\u00c3\u00a4r Groovt" then "Der B\u00e4r Groovt"; "\u00c3me" then unchanged [else ⛔️]
   [if] artist "u_v" then unchanged [else ⛔️]
✔︎ R17 (round 6) mashups: " x " / " vs " candidates get a JEV mashup question; collaborations
   ("Kestrel Fold x Hollow Parade") must not tag. Candidates never take the safe tier.
   [if] "Tangerine X Crowns Will Roll" then JEV is asked and p >= CFG then tag mashup [else ⛔️]
✔︎ R18 (round 6) safe tier: when every change is in SAFE_TRANSFORMS (each audited on the full
   library and against AcoustID/MusicBrainz), the row auto-applies with source "safe-tier" and
   NO JEV or DeepSeek call. Sampler rows are project with no JEV.
   [if] "8A - 6 - Ember Tide" by Vessel Choir then fixed, source safe-tier, cost 0 [else ⛔️]
   [if] "6 - Ember Tide [Official Video]" by Vessel Choir then JEV is called [else ⛔️]
✔︎ R19 (round 7) every OpenRouter call has a HARD wall-clock deadline (CFG.CALL_WALL_S, not only httpx's
   per-read timeout) and fix_tracks takes an optional whole-run wall budget; a call past either deadline
   makes the row UNKNOWN with the reason in Verdict.error. The key is an argument (api_key); the
   environment is only the default for the CLI and the eval.
   [if] a call outlives CFG.CALL_WALL_S then the row is unknown, error names the wall deadline [else ⛔️]
   [if] the run budget is spent then later rows make no call and are unknown [else ⛔️]
   [if] fix_tracks(rows, api_key="k") with no environment key then the calls carry "k" [else ⛔️]
✔︎ R20 (round 7) youtube-rip needs a converter marker (YT2mp3, y2mate, onlymp3, SnapSave, a YouTube id
   tail, "- YouTube"); a bare "(320kbps)" is not one. Sampler rows never take dj-project-gig-intro-or-sting.
   Lyric and translation notes ("(Lyrics)", "(Lyric Video)", "(English lyrics translation)") stay in the
   title and tag video-source.
   [if] filename "Song (320kbps).mp3" then no youtube-rip [else ⛔️]
   [if] "Maple Hollow (English lyrics translation)" then title kept, tag video-source [else ⛔️]
✔︎ R21 (round 7) a regex+deepseek agreement that drops bracketed text outside the strip list
   (Rx.STRIPPABLE_BRACKET) goes to review instead of auto-applying.
   [if] raw "Song (Rare Mix)" and both say "Song" then review [else ⛔️]
   [if] raw "Song (Official Video)" and both say "Song" then fixed [else ⛔️]
→  R4 writeback to rekordbox/file tags (goes through apps.tags.apply later).
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from typing import Any

import httpx

from .title_fix_model import (  # re-exported: the public surface callers import from apps.tags.title_fix
    CFG,
    Proposal,
    Status,
    Track,
    Verdict,
)
from .title_fix_openrouter import (  # noqa: F401  # re-exported: the public surface callers import from apps.tags.title_fix
    JEV_QUESTIONS,
    LLM_SCHEMA,
    LLM_SYSTEM,
    MASHUP_QUESTION,
    SPEND_CAP_ERROR,
    Api,
    CallTimeout,
    JevAnswer,
    PaidCallError,
    SpendBudget,
    SpendCapReached,
    T,
    UnknownCostError,
    _api_key,
    charged_usd,
    extract_with_llm,
    judge_with_jev,
    mashup_candidate,
)
from .title_fix_project import (  # noqa: F401  # re-exported: the public surface callers import from apps.tags.title_fix
    PROJECT_INSTRUCTIONS,
    Proj,
    _describe_project,
    dj_aliases,
    judge_project_with_jev,
    project_decision,
    project_rules,
    project_tags,
)
from .title_fix_provenance import (  # noqa: F401  # re-exported: the public surface callers import from apps.tags.title_fix
    Prov,
    featured_artists,
    provenance_tags,
)
from .title_fix_regex import (  # noqa: F401  # re-exported: the public surface callers import from apps.tags.title_fix
    Cov,
    Cover,
    _fold,
    _merge_tags,
    _names_a_version,
    regex_propose,
)
from .title_fix_vocab import (  # noqa: F401  # re-exported: the public surface callers import from apps.tags.title_fix
    MIX_DESCRIPTORS,
    VERSION_TAGS,
    Rx,
    version_tags,
)

# ----------------------------------------------------------------- pipeline


def _same(a: Proposal, b: Proposal) -> bool:
    """Punctuation-blind, case-SENSITIVE: 'CANT STOP IT' is not 'Can't Stop It'."""
    norm = lambda s: re.sub(r"\W+", "", s)  # noqa: E731
    return norm(a.artist) == norm(b.artist) and norm(a.title) == norm(b.title)


def _shouted(title: str, raw_title: str) -> bool:
    """ALL CAPS is artist styling (RIOT, MODEST., S.W.A.Y., I WON'T GO) unless the raw title is 3+ words
    AND carries junk/credit words (official, video, feat., deluxe...). A lookup tier may add disagreement later."""
    if sum(c.isupper() for c in title) <= 3 or any(c.islower() for c in title):
        return False
    raw = raw_title or title
    return len(raw.split()) >= 3 and bool(Rx.CAPS_JUNK.search(raw))


def _needs_human(proposal: Proposal, raw_title: str = "") -> str:
    """Script checks JEV is bad at: things that are MISSING or need counting."""
    if Rx.PLACEHOLDER_ARTIST.fullmatch(proposal.artist):
        return "no artist found anywhere"
    if not proposal.title.strip():
        return "empty title"
    if "\ufffd" in proposal.title:
        return "title has a replacement character (lost bytes)"
    if _shouted(proposal.title, raw_title):
        return "title is ALL CAPS with junk or credit words"
    return ""


def _mik_strip_only(track: Track, proposal: Proposal) -> bool:
    """The regex only removed a MIK key/energy prefix and the artist field was already filled."""
    if not (track.artist.strip() and proposal.energy and proposal.artist == track.artist.strip()):
        return False
    m = Rx.MIK_PREFIX.match(track.title.strip())
    return bool(m) and track.title.strip()[m.end() :].strip() == proposal.title


def _artist_in_row(artist: str, track: Track) -> bool:
    """Never invent artists: every credited name must appear somewhere in the raw row."""
    row = _fold(" ".join((track.title, track.artist, track.filename)))
    names = re.split(r"\s*(?:,|&|\+|\bx\b|\band\b|\bfeat\.?|\bft\.?)\s*", artist, flags=re.I)
    return all(_fold(name) in row for name in names if _fold(name))


def _artist_is_title_head(artist: str, proposal: Proposal) -> bool:
    """The regex found no artist separator, yet the LLM artist is the title's leading words: it took
    the song name for the artist (e.g. from a "Tumbleweed Road - ..." filename)."""
    return not proposal.artist and bool(_fold(artist)) and _fold(proposal.title).startswith(_fold(artist))


def _agree_threshold(track: Track, proposal: Proposal) -> float:
    return CFG.MIK_ONLY_THRESHOLD if _mik_strip_only(track, proposal) else CFG.AGREE_THRESHOLD


API_ERRORS = (httpx.HTTPError, RuntimeError, KeyError, ValueError, TypeError)


#: Deterministic edits that auto-apply with NO JEV/DeepSeek call when they are the ONLY changes to a row
#: whose artist field is filled (the maintainer, Fri 2 Oct). Entry needs (1) a full-library structural audit with every
#: firing row checked and (2) agreement with AcoustID/MusicBrainz truth. Audit: 10,138 rows, Fri 2 Oct 2026.
SAFE_TRANSFORMS: tuple[str, ...] = (
    # MIK "8A - 6 - " / "6 - " prefix on a filled artist. Fired 4315, 4292 safe-tier. Failure modes checked
    # on every row: empty remainder 0, a second number prefix 0, same prefix in the filename 0; 41 remainders
    # differ from the filename, all a different version or title in the file name, none a strip error.
    # MusicBrainz: 151/156 exact or base; the 5 differ are MB-side (2 fingerprint collisions, 1 typo, 2 spellings).
    "mik-prefix",
    # MIK "7A (6 - Title)" wrapper. Fired 28, 28 safe-tier. 4/28 lost a ")" before the round 6 balance fix;
    # 0 unbalanced after it. MusicBrainz: 9/9 exact or base.
    "mik-paren",
)
#: Audited and NOT admitted (round 6): key-bpm-suffix (56 fired, bpm matches the stored BPM on every row, but
#: 0 AcoustID/MB rows); bitrate-bracket (2 safe-eligible rows, both with a residual YouTube-style title);
#: whitespace (12 rows, one censored "Ni  as" collapses to a new word); site-prefix, pn-suffix, copy-suffix
#: (0 safe-eligible rows).


def safe_tier(track: Track, proposal: Proposal) -> bool:
    """Every change is a SAFE_TRANSFORMS edit on a filled-artist row and nothing else needs judging."""
    artist = track.artist.strip()
    padded = (
        track.artist != artist or track.title != track.title.strip()
    )  # trimming is an unaudited edit: never auto-applied
    return (
        not padded
        and bool(proposal.transforms)
        and set(proposal.transforms) <= set(SAFE_TRANSFORMS)
        and not proposal.review_reasons
        and bool(artist)
        and proposal.artist == artist
        and not _needs_human(proposal, track.title)
        and not mashup_candidate(track, proposal)
        and "mashup" not in proposal.tags  # an explicit mashup is never judged, so it is never safe
    )


def fix_track(
    track: Track,
    api_key: str | None = None,
    *,
    deadline: float | None = None,
    budget: SpendBudget | None = None,
) -> Verdict:
    """api_key None reads $OPENROUTER_API_KEY; deadline is a time.monotonic() stamp (the run's wall budget);
    budget is the run's shared spend budget, checked before every paid call."""
    api = Api(api_key, deadline, budget)
    proposal = regex_propose(track)
    base = Verdict("unknown", track.artist, track.title, "none", proposal)
    strong, weak = project_rules(track)
    if (no_api := _no_api_verdict(track, proposal, base, strong, weak)) is not None:
        return no_api
    project_note = ""
    if strong or weak:
        try:
            p_project, cost = judge_project_with_jev(track, api)
        except API_ERRORS as exc:
            return replace(base, error=f"jev project: {exc}", cost_usd=base.cost_usd + charged_usd(exc))
        base = replace(base, cost_usd=cost)
        decision = project_decision(strong, weak, p_project)
        if decision == "project":
            note = f"project rules {', '.join(strong or weak)}, JEV p(project) {p_project:.2f}"
            return replace(base, status="project", source="rules+jev", tags=project_tags(strong or weak), notes=[note])
        if decision == "review":
            project_note = f"project rules {', '.join(strong)} fired but JEV p(project) {p_project:.2f}"
    return _demote_to_review(with_tags(_fix_title(track, proposal, base, api)), project_note)


def _no_api_verdict(
    track: Track, proposal: Proposal, base: Verdict, strong: tuple[str, ...], weak: tuple[str, ...]
) -> Verdict | None:
    """The two certain paths that need no JEV/DeepSeek call; None sends the row on to the judges."""
    if "sampler" in strong:  # certain on its own: no JEV (round 6, sampler one-shots sat at JEV 0.73-0.82)
        note = f"project rules {', '.join(strong)} (sampler is certain: no JEV)"
        return replace(base, status="project", source="safe-tier", tags=project_tags(strong), notes=[note])
    if not (strong or weak) and safe_tier(track, proposal):
        status: Status = "clean" if _same(proposal, Proposal(track.artist, track.title)) else "fixed"
        note = f"safe tier: {', '.join(proposal.transforms)}"
        return with_tags(
            replace(base, status=status, artist=proposal.artist, title=proposal.title, source="safe-tier", notes=[note])
        )
    return None


def _demote_to_review(verdict: Verdict, project_note: str) -> Verdict:
    """An auto-applicable verdict goes to review when JEV disputed the project rules or tagged a mashup."""
    if project_note and verdict.status in (
        "fixed",
        "clean",
        "not_music",
    ):  # every usable verdict, unknown stays unknown
        return replace(verdict, status="review", notes=[*verdict.notes, project_note])
    if CFG.MASHUP_REVIEW and _is_mashup(verdict) and verdict.status in ("fixed", "clean"):
        return replace(verdict, status="review", notes=[*verdict.notes, "mashup: review"])
    return verdict


def _jev_mashup(verdict: Verdict) -> bool:
    return verdict.jev_p_mashup is not None and verdict.jev_p_mashup >= CFG.MASHUP_THRESHOLD


def _is_mashup(verdict: Verdict) -> bool:
    """JEV tagged it, or the title itself says mashup (the regex tag, which skips the JEV mashup question)."""
    return _jev_mashup(verdict) or "mashup" in verdict.regex.tags


def with_tags(verdict: Verdict) -> Verdict:
    """Tags follow the FINAL title; row-level tags (cover, provenance) ride along from the regex proposal."""
    if verdict.status in ("not_music", "unknown", "project"):
        return verdict
    regex = verdict.regex
    row_tags = tuple(t for t in regex.tags if t not in version_tags(regex.title))
    mashup = ("mashup",) if _jev_mashup(verdict) else ()
    return replace(
        verdict,
        tags=_merge_tags(version_tags(verdict.title), row_tags, mashup),
        original_artist=regex.original_artist,
        featured_artists=regex.featured_artists,
        producer=regex.producer,
    )


def _fix_title(track: Track, proposal: Proposal, base: Verdict, api: Api) -> Verdict:
    if proposal.review_reasons:
        return replace(
            base,
            status="review",
            artist=proposal.artist,
            title=proposal.title,
            source="regex",
            notes=list(proposal.review_reasons),
        )
    try:
        jev = judge_with_jev(track, proposal, api)
    except API_ERRORS as exc:
        return replace(base, error=f"jev: {exc}", cost_usd=base.cost_usd + charged_usd(exc))
    cost = base.cost_usd + jev.cost
    base = replace(base, jev_kind=jev.kind, jev_p_ok=jev.p_ok, jev_p_mashup=jev.p_mashup, cost_usd=cost)
    unchanged = _same(proposal, Proposal(track.artist, track.title))
    if jev.kind == "not_music":
        return replace(base, status="not_music")
    if jev.p_ok >= _agree_threshold(track, proposal) and not _needs_human(proposal, track.title):
        status: Status = "clean" if unchanged else "fixed"
        return replace(base, status=status, artist=proposal.artist, title=proposal.title, source="regex+jev")
    try:
        llm, llm_cost = extract_with_llm(track, api)
    except API_ERRORS as exc:
        return replace(base, error=f"llm: {exc}", cost_usd=cost + charged_usd(exc))
    return _settle_with_llm(replace(base, llm=llm, cost_usd=cost + llm_cost), track, proposal, llm, unchanged)


def _settle_with_llm(base: Verdict, track: Track, proposal: Proposal, llm: Proposal, unchanged: bool) -> Verdict:
    """DeepSeek alone never auto-applies: it can only confirm the regex or the raw row."""
    if not _artist_in_row(llm.artist, track):
        note = f"llm artist {llm.artist!r} is not in the row"
        return _llm_evidence_only(base, proposal, note)
    if _artist_is_title_head(llm.artist, proposal):
        note = f"llm artist {llm.artist!r} is the head of the song title"
        return _llm_evidence_only(base, proposal, note)
    if reason := _needs_human(llm, track.title):
        return _llm_evidence_only(base, proposal, f"{reason}: llm said {llm.artist!r} - {llm.title!r}")
    if _same(llm, proposal):
        if dropped := dropped_brackets(track.title, proposal.title):
            note = f"drops bracketed text {', '.join(dropped)} that is not on the strip list"
            return replace(
                base,
                status="review",
                artist=proposal.artist,
                title=proposal.title,
                source="regex+deepseek",
                notes=[note],
            )
        status: Status = "clean" if unchanged else "fixed"
        return replace(base, status=status, artist=proposal.artist, title=proposal.title, source="regex+deepseek")
    if _same(llm, Proposal(track.artist, track.title)):
        return replace(base, status="clean", source="deepseek(raw ok)")
    return _llm_evidence_only(base, proposal, f"llm disagrees: {llm.artist!r} - {llm.title!r}")


def _llm_evidence_only(base: Verdict, proposal: Proposal, note: str) -> Verdict:
    """A DeepSeek answer that does not confirm the regex is review EVIDENCE: the proposal a person can select and
    apply stays the regex one, never text the model produced."""
    return replace(base, status="review", artist=proposal.artist, title=proposal.title, source="deepseek", notes=[note])


def dropped_brackets(raw_title: str, title: str) -> list[str]:
    """Bracketed groups of the raw title that are gone from `title` and are not on the strip list (R21)."""
    kept = _fold(title)
    return [
        group
        for group in Rx.BRACKET.findall(raw_title.translate(Rx.FULL_WIDTH))
        if _fold(group) and _fold(group) not in kept and not _is_junk_bracket(group)
    ]


def _is_junk_bracket(group: str) -> bool:
    """Strippable junk, unless the group names a version: "(Official Remix)" is a remix, not a promo label."""
    return bool(Rx.STRIPPABLE_BRACKET.search(group)) and not _names_a_version(group)


def fix_tracks(
    tracks: list[Track],
    api_key: str | None = None,
    *,
    wall_budget_s: float | None = None,
    spend_budget_usd: float | None = None,
    spend_budget: SpendBudget | None = None,
) -> list[Verdict]:
    """api_key None reads $OPENROUTER_API_KEY (CLI/eval). wall_budget_s caps the whole run: once it is spent no
    new OpenRouter call starts and in-flight calls stop at it, so those rows come back unknown with the reason.
    spend_budget_usd caps the run's OpenRouter cost the same way: once it is spent no new call starts.
    spend_budget is a caller-owned SpendBudget shared across several calls of this function (a chunked scan)."""
    key = _api_key(Api(api_key))  # fail before fanning out
    deadline = None if wall_budget_s is None else time.monotonic() + wall_budget_s
    if spend_budget is not None and spend_budget_usd is not None:
        raise ValueError("pass spend_budget_usd or spend_budget, not both")
    budget = (
        spend_budget
        if spend_budget is not None
        else (None if spend_budget_usd is None else SpendBudget(spend_budget_usd))
    )
    with ThreadPoolExecutor(CFG.WORKERS) as pool:
        return list(pool.map(lambda track: fix_track(track, key, deadline=deadline, budget=budget), tracks))


def verdict_row(track: Track, verdict: Verdict) -> dict[str, Any]:
    llm = verdict.llm
    return {
        **asdict(track),
        "regex_artist": verdict.regex.artist,
        "regex_title": verdict.regex.title,
        "mik": " ".join(x for x in (verdict.regex.key, verdict.regex.energy, verdict.regex.bpm) if x),
        "jev_kind": verdict.jev_kind,
        "jev_p_ok": "" if verdict.jev_p_ok is None else round(verdict.jev_p_ok, 2),
        "llm_artist": llm.artist if llm else "",
        "llm_title": llm.title if llm else "",
        "status": verdict.status,
        "source": verdict.source,
        "final_artist": verdict.artist,
        "final_title": verdict.title,
        "original_artist": verdict.original_artist,
        "featured_artists": "; ".join(verdict.featured_artists),
        "producer": verdict.producer,
        "transforms": " ".join(verdict.regex.transforms),
        "jev_p_mashup": "" if verdict.jev_p_mashup is None else round(verdict.jev_p_mashup, 2),
        "llm_called": "Y" if llm else "",
        "tags": " ".join(verdict.tags),
        "notes": "; ".join(verdict.notes),
        "error": verdict.error,
    }
