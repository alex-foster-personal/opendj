"""Configuration, shared types, and link-classification regexes for skill_tree_lint."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar


@dataclass(frozen=True)
class _Cfg:
    # Anthropic's published ceiling for a SKILL.md body (see module docstring).
    # Both limits are exclusive: the rules say "under", so a file AT one is over.
    SKILL_MAX_LINES: int = 500
    # A rule file loads on every matching read, so it stays far below a skill.
    # 200 is the next round number above the longest rule on main (161 lines,
    # verification.md, measured Wed 23 Sep 2026).
    RULE_MAX_LINES: int = 200
    RULES_DIR: str = "(private path)"
    # main held 25 skills on Wed 23 Sep 2026; a scan finding under 10 is broken.
    MIN_SKILLS: int = 10


CFG = _Cfg()

N = TypeVar("N", str, Path)

# Ends at the extension: `guide.md.bak` and `guide.md-old` are other files.
# A query or fragment right after a file name with an extension (`guide.md#x`,
# `notes.txt?y`), up to the next whitespace.
LOCAL_SUFFIX_RE = re.compile(r"([\w-]\.[A-Za-z0-9]+)[?#]\S*")
MD_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+\.(?:markdown|mdx|mdc|md)(?![\w-]|\.\w)", re.IGNORECASE)
# A URL in prose (an autolink's text, or a bare one) is remote for the same reason.
# A scheme-bearing URL, `mailto:`, or a protocol-relative `//host/...` URL: each
# names a remote document, never a local skill, so its path is not searched.
# So does a root-relative `/docs/x` path of two or more segments (a site path);
# a one-segment `/name` is a slash-command invocation and stays. Any other
# absolute URI (`urn:x`, `https:host/x`) is remote too, except `skill:name`,
# which is how a skill is invoked (a scheme in any case, `SKILL:name` too, as
# RFC 3986 makes schemes case-insensitive); a scheme names at least two characters, so a
# drive letter is not one, and it cannot follow a path character.
# A bare host (`www.example.com/...`) is remote too: a first segment of dotted
# labels ending in letters, `localhost`, a dotted-quad IPv4 address or a
# bracketed IPv6 one (`[::1]`); `v1.20/` ends in digits and has two parts, so
# it stays a path. A label may be internationalized (`münich.example`), and
# so may the last one, written in Unicode or as punycode (`docs.xn--p1ai`).
# A query or fragment may follow the host directly (`example.com?x`,
# `example.com#x`), except after a markdown file name (`guide.md#usage`).
# A bare email address (`target-skill@example.com`) names a mailbox, not a
# skill, whether or not `mailto:` precedes it; so does one whose domain is a
# single label (`target-skill@localhost`) or a bracketed literal (`x@[::1]`).
# An SCP-style remote (`git@github.com:org/skills/x`) is taken whole too, so
# its host is every dotted label, whatever it ends in (`git@10.0.0.2:x`,
# `git@build.host1:x`), not only a name a bare host would need. The user is
# optional (`[user@]host:path`), so an IP-literal host takes a path after its
# colon too (`10.0.0.2:org/x`, `[::1]:x`); a named one already reads as a
# scheme. A host must be a whole dotted quad, so `step 2:skills/x` stays.
# A drive-qualified path (`C:/docs/x`, `C:\docs\x`) is machine-absolute, so
# like a root-relative path it never names a skill beside this one; a lone
# `A:` label is not one. So is a home-qualified path (`~/docs/x`, `~ops/x`)
# and one rooted at an environment variable (`$HOME/x`, `${HOME}/x`), which
# is upper case; a lower-case `$name` is a skill invocation and stays. So is
# a UNC path (`\\server\share\x`) or a Windows rooted one (`\docs\x`), the
# backslash twin of `//host/x` and `/docs/x`.
_DOMAIN = r"(?:(?:[^\W_]|-)+\.)+(?:[^\W\d_]{2,}|xn--[A-Za-z0-9-]+)"
# A scheme then `//`, or a bare `//`: the destination carries an authority (a host),
# which is exactly the part urlsplit validates and the part that makes it remote.
AUTHORITY_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//")
URL_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://\S*|\bmailto:\S*|(?<![\w:/.-])//[^\s/]+\S*"
    r"|(?<![\w:/.~-])/[^\s/]+/\S*"
    r"|(?<![\w:/.~$-])(?!(?i:skill):)[A-Za-z][A-Za-z0-9+.-]+:(?=[^\s:])\S*"
    r"|(?<![\w:/.~$-])(?:" + _DOMAIN + r"|localhost|\d{1,3}(?:\.\d{1,3}){3}"
    r"|\[[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*\])"
    r"(?::\d+)?(?:/|(?<!(?i:\.md))(?<!(?i:\.mdx|\.mdc))(?<!(?i:\.markdown))[?#])\S*"
    r"|(?<![\w:/.~$-])(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*\]):\S*"
    r"|(?<![\w.+-])[\w.+-]+@(?:[\w-]+(?:\.[\w-]+)*|\[[^\]\s]*\])(?::\S+)?"
    r"|(?<![\w:/.~$\\-])[A-Za-z]:[\\/]\S*"
    r"|(?<![\w:/.~$-])(?:~[\w.-]*|\$[A-Z_][A-Z0-9_]*|\$\{[A-Z_][A-Z0-9_]*\})/\S*"
    r"|(?<![\w:/.~$\\-])\\{1,2}[^\s\\/]+[\\/]\S*"
)


# Links are read by a CommonMark parser rather than a pattern: five review
# rounds on #3846 each found one more legal destination form (spaces, titles,
# titles on the next line, nested parentheses) that a regex dropped. It is
# imported on first use, not at module scope: scripts/quality_gate.py imports
# this module eagerly and must stay stdlib-only for recipes such as
# `just quality-arch`, while the `skills` evaluator runs the measurement in the
# pinned ops/quality/requirements.txt env, which carries markdown-it-py.


@dataclass(frozen=True)
class Skill:
    name: str
    root: Path

    @property
    def skill_md(self) -> Path:
        return self.root / "SKILL.md"


@dataclass(frozen=True)
class Report:
    skills_scanned: int
    over_line_limit: list[str]
    over_line_excess: int
    file_ref_depth_max: int
    file_ref_worst: list[str]
    skill_ref_depth_max: int
    skill_ref_worst: list[str]
    file_ref_deep_edges: int
    skill_ref_deep_edges: int
    skill_ref_cycles: list[str]
    cyclic_skill_refs: int
    rules_over_limit: list[str]
    rules_over_excess: int


class UnparsableDestinationError(ValueError):
    """A link destination this scan cannot classify as local or remote."""
