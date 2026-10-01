"""Logins the published tree itself reveals, and the rule that catches them written bare.

Why this exists (Thu 1 Oct 2026): every identity rule in scripts/oss_tip_rules.py is
POSITIONAL. A home-directory rule recognizes a login only where it sits after a home
root, so the same login written anywhere else -- a backticked username in prose, a
`sudo` refusal quoted in an ADR, a launchd label, the dash-encoded project directory a
Claude session writes (the home path with every slash turned into a dash) -- has no
shape the rules know, and the gate reported the tip clean at findings=0 while four
tracked documents named the nucbox WSL account outright. A bare login has no shape of
its own, so no new pattern can catch it.

What CAN catch it is knowledge the tree already holds. The write-once records keep their
content out of the scan, but they still record real paths, so the logins in them are
known to the audit even though the records themselves are not judged. This module
harvests every login that appears in a home-directory position ANYWHERE in the
published tree (records included), drops the placeholders, and then reports any scanned
file that writes one of those logins as a whole word. It is an invariant rather than a
list: a login that turns up in a new record is protected from that commit on, with no
value here to maintain, and nothing in this file spells one out.

What it cannot catch, stated so a clean run is not over-read: a login that has never
appeared in a home-directory position anywhere in the tree. The audit has no way to
know that string is a login, and that case still needs a human.

NOTE: this file is scanned by the gate it extends, like oss_tip_rules.py. Describe
logins; never write a real one here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from scripts.oss_tip_rules import PATTERNS, RULE_PREFILTERS, _is_placeholder_home, _rule_accepts

RULE = "known-login"
_HOME_RULES = frozenset({"home-path", "windows-home-path", "linux-home-path"})

# The login as a TOKEN: the leading run of characters a login is made of. A capture
# that ran into a separator, an escape or prose (a `\` before a newline escape, a `:`
# or a trailing `...` elision) reduces to the login it started with, and the
# placeholder check then judges that login rather than the noise after it.
_LOGIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Shorter than this is too common as an ordinary word to report as a login by itself.
# Measured on Thu 1 Oct 2026: every harvested real login is longer than this.
MIN_LOGIN_LEN = 3

# Revealed logins that are ALSO ordinary words in this tree's prose, so a whole-word
# match on them reports a sentence rather than an account. Enumerated, like every other
# exception in this gate, so the list is reviewable on one screen. The home-path rules
# still report each of them in a home-directory position; only the BARE match is waived.
BARE_MATCH_EXEMPT = frozenset(
    {
        # A Windows profile that is a person's first name. The person is named in
        # published prose already, so a bare match reports that prose, not a login.
        "ben",
        # A Windows profile that is also the fleet's nickname for the silver laptop.
        "steve",
        # The fixture login the audit's own tests build to prove the home rules fire.
        "jdoe",
    }
)

# A match whose preceding text ends at a home root is already reported by the
# home-path rule that owns that position; reporting it twice adds a line, not a leak.
_HOME_ROOT_BEFORE = re.compile(r"(?:/users/|/home/|users\\+)$", re.IGNORECASE)


def _login_from_segment(segment: str) -> str | None:
    token = _LOGIN_TOKEN.match(segment)
    if token is None:
        return None
    login = token.group(0).rstrip("._-").lower()
    return login if len(login) >= MIN_LOGIN_LEN else None


def logins_in_text(text: str) -> set[str]:
    """Non-placeholder logins that `text` puts in a home-directory position."""
    lowered = text.lower()
    found: set[str] = set()
    for rule, pattern in PATTERNS:
        if rule not in _HOME_RULES:
            continue
        if not any(token in lowered for token in RULE_PREFILTERS[rule]):
            continue
        for match in pattern.finditer(text):
            preceding = text[max(0, match.start() - 256) : match.start()]
            if not _rule_accepts(rule, match, preceding):
                continue
            login = _login_from_segment(match.group(1))
            if login is not None and not _is_placeholder_home(login):
                found.add(login)
    return found


def revealed_logins(texts: Iterable[str]) -> frozenset[str]:
    """Every login the given texts reveal, minus the reviewed bare-match exemptions."""
    found: set[str] = set()
    for text in texts:
        found |= logins_in_text(text)
    return frozenset(found - BARE_MATCH_EXEMPT)


def known_login_pattern(logins: frozenset[str]) -> re.Pattern[str] | None:
    """One alternation over the revealed logins, matched as whole words, any case.

    Whole word means not flanked by a letter, digit or underscore. A dash or a dot is a
    boundary, which is the point: the dash-encoded session path and a reverse-DNS
    launchd label both carry the login between dashes or dots. None when there is
    nothing to look for, so a tree that reveals no login costs nothing.
    """
    if not logins:
        return None
    alternation = "|".join(re.escape(login) for login in sorted(logins, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{alternation})(?![A-Za-z0-9_])", re.IGNORECASE)


def bare_login_matches(
    text: str, lowered: str, pattern: re.Pattern[str] | None, logins: frozenset[str]
) -> Iterator[re.Match[str]]:
    """Whole-word occurrences of a revealed login outside a home-directory position."""
    if pattern is None or not any(login in lowered for login in logins):
        return
    for match in pattern.finditer(text):
        if _HOME_ROOT_BEFORE.search(text[max(0, match.start() - 16) : match.start()]):
            continue
        yield match
