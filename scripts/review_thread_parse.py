"""Pure parsing and classification for bot review threads.

The functional core of the triage gate: everything here is a pure function of
a GraphQL thread node, with no network and no filesystem. `review_thread_triage`
is the shell that fetches, reports and exits. Split out when the pair crossed
the 600-line file ceiling, along the seam that was already there.

Four reviewers, four vocabularies, one model. Copilot states a bare P-level
and verdict as plain text leading its headline. Codex states a P-level in a
shields badge and (since Mon 31 Aug 2026) an explicit BLOCKING verdict. Devin
states neither: it ships a JSON envelope in an HTML comment plus a headline
colour. CodeRabbit is governed but has emitted no threads here yet.

Acceptance tests live in tests/scripts/test_review_thread_parse.py and run
against real captured payloads pinned by checksum.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from scripts.review_claude import CLAUDE, is_claude_thread
from scripts.review_sol import SOL, is_sol_thread

# GitHub Copilot's reviewer has THREE spellings for one author: GraphQL
# reviewThreads (what this gate reads) says `copilot-pull-request-reviewer`
# with no [bot] suffix, REST pulls/<n>/reviews adds the suffix, and REST
# pulls/<n>/comments says `Copilot`. Missing it hid six Copilot threads, five
# of them "P1 BLOCKING", from the gate on PR #4240 (Tue 29 Sep 2026).
BOT_LOGINS = frozenset(
    {
        "chatgpt-codex-connector",
        "coderabbitai",
        "copilot",
        "copilot-pull-request-reviewer",
        "devin-ai-integration",
    }
)

# Codex renders severity as a shields.io badge, e.g.
# ![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)
_SEVERITY_BADGE = re.compile(r"badge/(P[0-9])-")
_SEVERITY_BARE = re.compile(r"\b(P[0-3])\b")
# The verdict is the FIRST TOKEN on the headline, which is how reviewers
# actually emit it ("BLOCKING Validate ...", "[NON-BLOCKING] Restrict ...").
# Anchoring matters: a headline may legitimately describe the problem using
# the word, as in "Move blocking upload analysis off the event loop", and
# reading that as an explicit verdict mis-tiers an ordinary P2.
#
# What is pinned is POSITION, not punctuation. Codex changed its own markup
# between PR #1600 and PR #1658 -- a bare "BLOCKING Close ..." became a
# bracketed "[BLOCKING] Close ..." -- and an anchor requiring the marker at
# literal offset zero read every bracketed verdict as `unmarked`. An unmarked
# P2 is eligible for the debt-log path under MERGE WITH P2s OPEN, so a thread
# the reviewer had explicitly marked BLOCKING could be waved through a merge:
# exactly the one shortcut the tiering exists to refuse. Pinning the new markup
# would rot the same way on the bot's next rendering change.
#
# So a WRAPPER is optional and unenumerated -- `[`, `(`, U+3010, whatever the
# renderer emits next -- but it must ABUT the marker, with no space between.
# That is what makes it a wrapper rather than a word of its own, and it is the
# discriminator Codex asked for on PR #1671: Devin opens its headlines with a
# SEMANTIC emoji, `🟡 **Blocking I/O stalls the event loop**`, and
# swallowing every leading non-word character exposed ordinary prose at offset
# zero and invented a verdict from it. An emoji is followed by a space; a
# wrapper is not.
#
# NON- is matched inside the SAME anchored alternation and is never tested as a
# separate substring. "BLOCKING" occurs inside "NON-BLOCKING" at offset 4, so an
# unanchored search reads every non-blocking nit as a merge blocker -- the
# opposite error, and one this gate has already made once. Anchoring is what
# keeps both directions correct at the same time: at offset zero the optional
# NON- group consumes the prefix before BLOCKING is ever reached.
#
# Every class here is Unicode-aware (`\w`, not `A-Za-z`): an ASCII-only class
# calls every accented letter decoration, so "πBLOCKING calculation is wrong"
# would have its leading letter stripped and become an explicit blocker
# (Sol P2, PR #1671).
#
# A bare P-level MAY precede the verdict, because Copilot writes its severity
# as plain text leading the headline ("P1 BLOCKING - ...", "P2 NON-BLOCKING:
# ..."), PR #4240. Without that allowance every Copilot verdict read unmarked.
_BLOCKING = re.compile(r"^\s*(?:P[0-3]\s+)?(?:[^\w\s]+)?(NON\W?)?BLOCKING", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")

# Devin carries no P-level. It ships a machine-readable envelope instead:
#   <!-- devin-review-comment {"id": "BUG_...", "kind": "bug"} -->
#   🟡 **Dirty checkouts are modified**
# Measured over this repo's whole review corpus (107 Devin threads, Mon 31 Aug
# 2026): kind is exactly "bug" (40) or "analysis" (67); every bug carries 🔴
# (7) or 🟡 (33), and no analysis carries either. So the mapping below is a
# reading of Devin's own vocabulary, not an invented ranking:
#   bug + red    -> P1   a defect claim Devin rates high
#   bug + yellow -> P2   a defect claim Devin rates moderate
#   analysis     -> INFO an observation, never a defect claim
# The debt-log reply has a fixed opening by policy, which is what makes the
# "no blocker may be debt-logged" rule mechanically checkable.
# Anchored to the start of ONE REPLY, because the policy defines this as the
# reply's opening. Unanchored it matched any mention of the phrase, so a reply
# that merely QUOTED the convention -- as the replies on this PR do -- read as
# a write-off. Line-anchored was still wrong: a FIXED reply that goes on to
# describe a separate follow-up debt entry would flip the whole thread.
_DEBT_LOG_REPLY = re.compile(r"^\s*logged as tech debt\s*:", re.IGNORECASE)

# A terminal reply must say WHICH of the three dispositions it is, otherwise
# "investigating" plus a resolve would clear the gate with nothing recorded.
# Patterns are deliberately generous: the goal is to catch a reply that names
# no disposition at all, not to impose one phrasing on reviewers.
_DISPOSITIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DEBT-LOGGED", _DEBT_LOG_REPLY),
    (
        "REBUTTED",
        re.compile(
            r"\b(rebut\w*|disput\w+|declin\w+|not doing this|won'?t fix|wontfix"
            r"|by design|working as intended|disagree\w*|leaving as[- ]is)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "FIXED",
        re.compile(
            # "fixes" as a bare alternative is omitted on purpose: it is a
            # noun as often as a verb ("there are two possible fixes;
            # investigating which is safer"), and it needs neither negation
            # nor future wording to read as done. A false PASS lets a silent
            # finding through; a false FAIL costs an author one reword.
            r"\b(fixed|addressed|implemented|resolved in|done in"
            r"|corrected|landed in)\b",
            re.IGNORECASE,
        ),
    ),
)

# A disposition word is only a disposition when it is both affirmative and
# COMPLETE. Three ways it is neither: negated ("not fixed yet"), still ahead of
# the author ("will fix", "about to fix"), or merely PRESCRIBED rather than
# done ("should be fixed", "needs to be fixed before merge"). The third is the
# sneakiest, because it reads as agreement with the reviewer while recording
# that nothing has happened. All three describe a thread that has not reached a
# terminal state, and all three would otherwise clear the gate.
# The window is short on purpose: it should catch "has not been fixed" without
# reaching back across a sentence boundary to negate an unrelated clause.
_NEGATION_WINDOW = 40
_NEGATOR = re.compile(
    r"\b(not|isn\'?t|aren\'?t|wasn\'?t|hasn\'?t|haven\'?t|don\'?t|doesn\'?t|didn\'?t"
    r"|never|cannot|unable to|nothing"
    r"|will|going to|about to|planning to|intend to|i\'?ll|we\'?ll|shall"
    r"|should|must|needs? to|need to|ought to|has to|have to|remains? to|yet to)"
    r"\b[^.!?\n]{0,20}$",
    re.IGNORECASE,
)

# The ledger is the other half of a DEBT-LOGGED claim. A reply naming an anchor
# that was never appended loses the finding just as silently as saying nothing.
_DEVIN_ENVELOPE = re.compile(r"<!--\s*devin-review-comment\s*(\{.*?\})\s*-->", re.DOTALL)
_DEVIN_RED = "\U0001f534"
_DEVIN_YELLOW = "\U0001f7e1"

# -----------------------------------------------------------------------------
# model
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Thread:
    """One bot-opened review thread and its triage state."""

    node_id: str
    path: str
    line: int | None
    resolved: bool
    outdated: bool
    bot: str
    severity: str
    blocking: str
    summary: str
    permalink: str
    created_at: str
    human_replies: int
    disposition: str | None
    ledger_indexed: bool
    ledger_checked: bool = True

    @property
    def triaged(self) -> bool:
        """A NAMED disposition AND a resolve. Neither half alone will do.

        This is what the policy table says: FIXED is "the change plus a reply
        saying where", REBUTTED is "a reply with the reasoning, thread
        resolved", DEBT-LOGGED is "reply naming the ledger anchor, thread
        resolved". Every row needs both halves, and the reply half has to say
        WHICH of the three it is.

        Three earlier cuts were each weaker than that text. The first accepted
        any reply, so "investigating" cleared the gate. The second accepted a
        bare resolve, so clicking Resolve silently cleared it. The third
        counted reply bodies without reading them, so "investigating" plus a
        resolve cleared it again.
        """
        return self.resolved and self.disposition is not None

    @property
    def state(self) -> str:
        if self.resolved and self.disposition is not None:
            return "RESOLVED"
        if self.resolved and self.human_replies > 0:
            return "RESOLVED-UNCLEAR"
        if self.resolved:
            return "RESOLVED-SILENT"
        if self.human_replies > 0:
            return "IN-PROGRESS"
        return "UNTRIAGED"

    @property
    def illegal_debt_log(self) -> bool:
        """A blocker written off as debt: forbidden however it was resolved.

        P0/P1 and anything marked BLOCKING must be FIXED or REBUTTED. Catching
        this is mechanical because the debt-log reply has a fixed opening, so
        the gate can refuse the one shortcut the tiering exists to prevent.
        """
        return self.disposition == "DEBT-LOGGED" and (
            self.severity in {"P0", "P1"} or self.blocking == "BLOCKING"
        )

    @property
    def debt_not_in_ledger(self) -> bool:
        """A debt-log claim whose finding was never actually indexed.

        The reply and the ledger append are one action in two places. A typo'd
        anchor, or a reply nobody followed through on, loses the finding just
        as silently as saying nothing at all.

        Gated on `ledger_checked`: #805 round 8 (review_thread_triage.py:360).
        A thread built on the deferred-read path never had its permalink
        checked against anything -- `ledger_indexed` defaults False there, not
        because the claim was absent, but because nobody looked. Asserting
        NOT IN LEDGER from a default is exactly the accusation-from-an-
        unmeasured-read #792 banned, just one layer up: the same class of bug
        `LedgerReadError` exists to keep out of a rendered verdict, but for a
        read that was skipped by design rather than one that failed outright.
        """
        return self.disposition == "DEBT-LOGGED" and self.ledger_checked and not self.ledger_indexed

    @property
    def ledger_relevant(self) -> bool:
        """A DEBT-LOGGED claim whose place in `PullRequest.failing` actually
        hinges on ledger membership. #805 round 8 (review_thread_triage.py:347):
        `illegal_debt_log` already fails a P0/P1 or BLOCKING debt-log
        regardless of `ledger_indexed`, and an unresolved thread already fails
        via `triaged` regardless too -- in both cases a ledger read can only
        downgrade an already-known verdict to COULD NOT MEASURE if the read
        happens to fail (a deleted historical base, a predates-the-ledger
        head). Only a RESOLVED, non-blocking DEBT-LOGGED thread's outcome is
        actually undetermined without `ledger_indexed`; that is the one shape
        `debt_not_in_ledger` can flip from passing to failing.
        """
        return (
            self.disposition == "DEBT-LOGGED"
            and self.resolved
            and self.severity not in {"P0", "P1"}
            and self.blocking != "BLOCKING"
        )


@dataclass(frozen=True)
class PullRequest:
    """A PR plus every bot review thread found on it."""

    number: int
    title: str
    state: str
    merged: bool
    url: str
    # The head the threads were fetched at. A gate that certified coverage at
    # one SHA must refuse to render a verdict off threads read at another.
    head_sha: str
    threads: tuple[Thread, ...]
    # The branch this PR merges into. A DEBT-LOGGED claim may be indexed here
    # rather than at the head, because `.planning/TECH-DEBT.md` sanctions
    # appends pushed straight to main; the default is what every caller before
    # #805 was implicitly assuming.
    base_ref: str = "main"

    @property
    def unterminated(self) -> tuple[Thread, ...]:
        """Every thread that has not reached a terminal state. Fails the gate."""
        return tuple(t for t in self.threads if not t.triaged)

    @property
    def failing(self) -> tuple[Thread, ...]:
        """Everything the gate refuses: unterminated, plus illegal debt-logs."""
        return tuple(
            t for t in self.threads if not t.triaged or t.illegal_debt_log or t.debt_not_in_ledger
        )

    @property
    def silent(self) -> tuple[Thread, ...]:
        """Never answered at all: the failure the maintainer spotted."""
        return tuple(t for t in self.threads if t.state == "UNTRIAGED")

    @property
    def in_progress(self) -> tuple[Thread, ...]:
        """Answered but never dispositioned. Resolve it, or say why not."""
        return tuple(t for t in self.threads if t.state == "IN-PROGRESS")

    @property
    def resolved_silent(self) -> tuple[Thread, ...]:
        """Closed with no reply: no record of WHICH disposition was chosen."""
        return tuple(t for t in self.threads if t.state == "RESOLVED-SILENT")

    @property
    def resolved_unclear(self) -> tuple[Thread, ...]:
        """Closed with a reply that names none of the three dispositions."""
        return tuple(t for t in self.threads if t.state == "RESOLVED-UNCLEAR")

    @property
    def violations(self) -> tuple[Thread, ...]:
        """Blockers written off as debt. Never legal, however they were closed."""
        return tuple(t for t in self.threads if t.illegal_debt_log)

    @property
    def unindexed_debt(self) -> tuple[Thread, ...]:
        """Debt-log claims with no matching ledger entry."""
        return tuple(t for t in self.threads if t.debt_not_in_ledger)


# -----------------------------------------------------------------------------
# identity
# -----------------------------------------------------------------------------


def _normalize_login(login: str) -> str:
    return login.removesuffix("[bot]").lower()


def _is_bot(login: str) -> bool:
    """A review bot whose threads the three-state rule governs."""
    return _normalize_login(login) in BOT_LOGINS


def _reviewer_lane(login: str, body: str) -> str:
    """Which CLI review lane wrote this opening comment, if any.

    Sol and Claude both post through `gh` as the maintainer (issues #1211 and the Sun 6
    Sep 2026 Claude lane), so neither has a bot login to recognize and each is
    identified by the marker its own lane writes. The marker is checked at ANY
    head, not the current one: an outdated finding still has to reach a
    disposition.
    """
    if is_sol_thread(login, body):
        return SOL
    if is_claude_thread(login, body):
        return CLAUDE
    return ""


def _is_reviewer_thread(login: str, body: str) -> bool:
    """Is this opening comment a REVIEW that the three-state rule governs?

    Bot login is the usual answer; the CLI lanes are the exception. Without
    the lane branch a Sol or Claude finding would be an ordinary human
    comment, and the silence detector would let a P0 sit unanswered through a
    merge -- the exact gap this module exists to close, just wearing a
    different login.
    """
    return _is_bot(login) or bool(_reviewer_lane(login, body))


def _is_human(login: str) -> bool:
    """Any real person. Deliberately wider than "not a review bot".

    Only a human can supply a disposition, so ANY automation account is
    excluded here -- github-actions[bot], dependabot, a CI integration -- not
    just the three reviewers. Otherwise an unrelated bot commenting on a
    thread would read as author triage.
    """
    return bool(login) and not login.endswith("[bot]") and not _is_bot(login)


# -----------------------------------------------------------------------------
# parsing
# -----------------------------------------------------------------------------


def _devin_severity(body: str) -> str | None:
    """Map Devin's kind + headline colour onto a P-level, or None if not Devin."""
    envelope = _DEVIN_ENVELOPE.search(body)
    if not envelope:
        return None
    kind = json.loads(envelope.group(1)).get("kind")
    if kind != "bug":
        return "INFO"
    visible = _HTML_COMMENT.sub("", body)
    if _DEVIN_RED in visible:
        return "P1"
    if _DEVIN_YELLOW in visible:
        return "P2"
    return "P?"


def _severity(body: str) -> str:
    """Pull the P-level out of a bot comment body, or 'P?' when unmarked."""
    badge = _SEVERITY_BADGE.search(body)
    if badge:
        return badge.group(1)
    devin = _devin_severity(body)
    if devin is not None:
        return devin
    head = body.split("\n", 1)[0]
    bare = _SEVERITY_BARE.search(head)
    if bare:
        return bare.group(1)
    return "P?"


def _headline(body: str) -> str:
    """The finding's first visible line: its title and marker line.

    HTML comments are stripped FIRST and across newlines: Devin's envelope is
    an HTML comment carrying JSON, and a line-at-a-time strip would return
    that machine metadata as the headline.
    """
    for raw in _HTML_COMMENT.sub("", body).split("\n"):
        line = _HTML_TAG.sub("", _MD_IMAGE.sub("", raw)).replace("*", "").strip()
        if line:
            return line
    return ""


def _joins_into_a_longer_word(char: str) -> bool:
    """Does `char` make the BLOCKING before it part of a longer word?

    A word character continues it outright ("BLOCKINGS"). So does any Unicode
    DASH, because a hyphenated compound is one word for this purpose:
    "BLOCKING-ADJACENT work belongs elsewhere" is prose, not a verdict, and it
    stays prose when the hyphen is U+2011 or U+2013.

    The dash test asks `unicodedata` for the CATEGORY rather than listing the
    dashes, which is the same rule the wrapper follows one level up: an
    enumeration is a value that rots, and Unicode has nineteen of these.
    """
    return char.isalnum() or char == "_" or unicodedata.category(char) == "Pd"


def _blocking(body: str) -> str:
    """Read the BLOCKING / NON-BLOCKING marker, or 'unmarked' when absent.

    Headline only, deliberately. The marker is defined as an explicit verdict
    on the finding's first line; scanning the whole body would read a P2 whose
    prose happens to mention "blocking I/O" as an explicit BLOCKING verdict and
    strand it outside the debt-log path.

    Two boundaries, both invariants rather than punctuation lists: the marker
    leads the headline (bare, or abutting an unenumerated wrapper), and it is a
    COMPLETE TOKEN rather than the start of a longer one.
    """
    headline = _headline(body)
    match = _BLOCKING.match(headline)
    if not match:
        return "unmarked"
    tail = headline[match.end() :]
    if tail and _joins_into_a_longer_word(tail[0]):
        return "unmarked"
    return "NON-BLOCKING" if match.group(1) else "BLOCKING"


def _summary(body: str, limit: int = 90) -> str:
    """Reduce a bot comment to its one-line headline."""
    return _headline(body)[:limit] or "(no summary)"


def _affirmative(text: str, start: int) -> bool:
    """False when a negator sits just before the disposition word.

    "Not fixed yet; still investigating" contains "fixed" and records the
    opposite of a decision. Matching the positive word alone would clear the
    gate on a reply that explicitly says no decision was reached.
    """
    return not _NEGATOR.search(text[max(0, start - _NEGATION_WINDOW) : start])


def _reply_disposition(body: str) -> str | None:
    """The disposition ONE reply records, if any.

    DEBT-LOGGED is tested against this reply's OPENING, per the policy. Testing
    it per line would let a FIXED reply that mentions a separate follow-up debt
    entry reclassify the whole thread, and on a P1 that would then fail as an
    illegal debt-log despite the real disposition being a fix.
    """
    if _DEBT_LOG_REPLY.match(body):
        return "DEBT-LOGGED"
    for name, pattern in _DISPOSITIONS:
        if name == "DEBT-LOGGED":
            continue
        for match in pattern.finditer(body):
            if _affirmative(body, match.start()):
                return name
    return None


def _disposition(replies: list[dict]) -> str | None:
    """The state the LATEST recorded disposition puts the thread in.

    Order matters, and category order would get it wrong. A thread that was
    debt-logged and then actually fixed is FIXED; scanning by category would
    return DEBT-LOGGED forever, leaving a blocker permanently failing as an
    illegal debt-log and an unindexed debt entry permanently failing a thread
    that has since been closed properly.

    Within one reply, every occurrence is considered, so "not fixed in round 5,
    but fixed in round 6" still reads FIXED on the affirmative clause.
    """
    latest: str | None = None
    for reply in replies:
        recorded = _reply_disposition(reply["body"])
        if recorded is not None:
            latest = recorded
    return latest


def build_thread(
    node: dict, ledger: frozenset[str] = frozenset(), *, ledger_checked: bool = True
) -> Thread | None:
    """Turn one GraphQL reviewThread node into a Thread, or None if not a bot's.

    `ledger_checked` defaults True because every direct caller (production's
    normal read path, and every test in this suite that hand-feeds a `ledger`
    set) genuinely attempted the read. `fetch_pull_request`'s deferred-read
    path is the one caller that passes `ledger_checked=False`, for threads it
    built without ever attempting a read at all.
    """
    comments = node["comments"]["nodes"]
    if not comments:
        return None
    first = comments[0]
    author = (first.get("author") or {}).get("login") or ""
    body = first["body"]
    if not _is_reviewer_thread(author, body):
        return None
    replies = [
        comment
        for comment in comments[1:]
        if _is_human((comment.get("author") or {}).get("login") or "")
    ]
    return Thread(
        node_id=node["id"],
        path=node["path"] or "(file gone)",
        line=node["line"],
        resolved=node["isResolved"],
        outdated=node["isOutdated"],
        bot=_reviewer_lane(author, body) or author,
        severity=_severity(body),
        blocking=_blocking(body),
        summary=_summary(body),
        permalink=first["url"],
        created_at=first["createdAt"],
        human_replies=len(replies),
        disposition=_disposition(replies),
        ledger_indexed=first["url"] in ledger,
        ledger_checked=ledger_checked,
    )
