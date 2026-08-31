"""Pure parsing and classification for bot review threads.

The functional core of the triage gate: everything here is a pure function of
a GraphQL thread node, with no network and no filesystem. `review_thread_triage`
is the shell that fetches, reports and exits. Split out when the pair crossed
the 600-line file ceiling, along the seam that was already there.

Three reviewers, three vocabularies, one model. Codex states a P-level in a
shields badge and (since Mon 31 Aug 2026) an explicit BLOCKING verdict. Devin
states neither: it ships a JSON envelope in an HTML comment plus a headline
colour. CodeRabbit is governed but has emitted no threads here yet.

Acceptance tests live in tests/scripts/test_review_thread_parse.py and run
against real captured payloads pinned by checksum.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

BOT_LOGINS = frozenset(
    {
        "chatgpt-codex-connector",
        "coderabbitai",
        "devin-ai-integration",
    }
)

# Codex renders severity as a shields.io badge, e.g.
# ![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)
_SEVERITY_BADGE = re.compile(r"badge/(P[0-9])-")
_SEVERITY_BARE = re.compile(r"\b(P[0-3])\b")
# The verdict is a LEADING token on the headline, which is how reviewers
# actually emit it ("BLOCKING Validate ...", "NON-BLOCKING: Restrict ...").
# Anchoring matters: a headline may legitimately describe the problem using
# the word, as in "Move blocking upload analysis off the event loop", and
# reading that as an explicit verdict mis-tiers an ordinary P2.
_BLOCKING = re.compile(r"^\s*(NON[-\s]?)?BLOCKING\b\s*[:.-]?\s", re.IGNORECASE)
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
        """
        return self.disposition == "DEBT-LOGGED" and not self.ledger_indexed


@dataclass(frozen=True)
class PullRequest:
    """A PR plus every bot review thread found on it."""

    number: int
    title: str
    state: str
    merged: bool
    url: str
    threads: tuple[Thread, ...]

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


def _blocking(body: str) -> str:
    """Read the BLOCKING / NON-BLOCKING marker, or 'unmarked' when absent.

    Headline only, deliberately. The marker is defined as an explicit verdict
    on the finding's first line; scanning the whole body would read a P2 whose
    prose happens to mention "blocking I/O" as an explicit BLOCKING verdict and
    strand it outside the debt-log path.
    """
    match = _BLOCKING.match(_headline(body))
    if not match:
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


def build_thread(node: dict, ledger: frozenset[str] = frozenset()) -> Thread | None:
    """Turn one GraphQL reviewThread node into a Thread, or None if not a bot's."""
    comments = node["comments"]["nodes"]
    if not comments:
        return None
    first = comments[0]
    author = (first.get("author") or {}).get("login") or ""
    if not _is_bot(author):
        return None
    replies = [
        comment
        for comment in comments[1:]
        if _is_human((comment.get("author") or {}).get("login") or "")
    ]
    body = first["body"]
    return Thread(
        node_id=node["id"],
        path=node["path"] or "(file gone)",
        line=node["line"],
        resolved=node["isResolved"],
        outdated=node["isOutdated"],
        bot=author,
        severity=_severity(body),
        blocking=_blocking(body),
        summary=_summary(body),
        permalink=first["url"],
        created_at=first["createdAt"],
        human_replies=len(replies),
        disposition=_disposition(replies),
        ledger_indexed=first["url"] in ledger,
    )
