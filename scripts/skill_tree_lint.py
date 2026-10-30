"""
skill_tree_lint.py -- keep agent skills short and their reference trees flat.

A skill is read by an agent in the middle of real work, so every line and every
hop costs context. Anthropic's skill authoring guidance
(platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices, read
Wed 23 Sep 2026) sets two numbers this file measures:

  * "Keep SKILL.md body under 500 lines for optimal performance. Split content
    into separate files when approaching this limit."
  * "Keep references one level deep from SKILL.md." A file reached through
    another referenced file may only be previewed (`head -100`), not read.

the maintainer asked for the same thing for skills that call other skills (Wed 23 Sep
2026): subskills are fine, a deep chain of them is not. So this measures three
trees, all from the repo's own skill directories:

Requirements (status key: -> out-of-scope, ? todo, OK done, RUN done+ran,
REG done+ran+regression-tested):

REG  K-01 Count SKILL.md files at or over CFG.SKILL_MAX_LINES lines (the
          rule is "under 500", so a 500-line file already breaks it).
          [if a SKILL.md grows to 500 lines then skills.over_line_limit rises]
          [if it is split into SKILL.md plus a referenced file then it falls]
          Also total the lines past the last allowed one across those files,
          so one already-long skill growing longer still moves a gated number.
          [if a 612-line SKILL.md grows to 700 then skills.over_line_excess
           rises by 88 while skills.over_line_limit stays put]
REG  K-02 Measure the deepest chain of markdown files one skill reaches inside
          its own directory, starting at SKILL.md (Anthropic's "one level").
          [if SKILL.md -> a.md -> b.md then skills.file_ref_depth_max is 2]
          [if SKILL.md links a.md and b.md directly then it is 1]
          A link destination is read whole, angle-bracketed or percent-encoded.
          [if SKILL.md links <reference guide.md> then that file is reached]
          Also count every reference made below the first level (each link
          out of a file SKILL.md reaches, other than SKILL.md itself), so a
          second deep chain moves a gated number even while the deepest one
          is unchanged, and so does a deep route to a file a shorter route
          already reaches.
          [if a skill already 3 deep adds SKILL.md -> c.md -> d.md then
           skills.file_ref_deep_edges rises by 1 while the max stays put]
          [if SKILL.md links a.md and c.md and a.md now links c.md too then
           skills.file_ref_deep_edges rises by 1]
          Every Markdown suffix the exporter recognizes (.md, .mdx, .mdc,
          .markdown, any case) is followed; a root-relative destination
          (/docs/x) is a web path, never a local file or skill.
          [if SKILL.md links guide.MDX then that file is reached]
          [if SKILL.md links /docs/skills/other-skill then it is not a ref]
REG  K-03 Measure the deepest chain of skills naming other skills (skill A
          names B, B names C = depth 2), and find the groups of skills that
          can reach one another (cycles of any length, not only pairs), which
          make "depth" unbounded for a reader. The gate ratchets the number of
          references on a cycle, which never falls when a reference is added
          (merging two cycles into one would lower a count of groups).
          [if A names B, B names C and C names A then one cycle is reported]
          A skill's own name turns up in its frontmatter, headings and paths to
          its own files, so only an explicit invocation of itself ([[name]],
          $name, /name) counts as a self-reference, which is a one-skill loop.
          [if a skill writes [[its-own-name]] then cyclic_skill_refs rises]
          A path reference resolves to the one skill directory it names; only a
          bare name reaches every tree's copy of a shared name.
          [if a skill links ../dup/SKILL.md then only that tree's dup is a ref]
          Only SKILL.md and the files it reaches (as K-02 walks them) are read,
          since an agent is never led to an unlinked note.
          [if an unlinked note names a skill then it is not a ref]
          [if a reference joins two cycles into one then the count rises]
          Depth collapses each cycle first, so a dense ring of skills costs
          polynomial time, not the factorial cost of enumerating simple paths.
          [if A names B and B names C then skills.skill_ref_depth_max is 2]
          Also count every handoff made below the first level from each
          skill, as K-02 does for files.
          [if a new chain D -> E -> F appears beside a deeper one then
           skills.skill_ref_deep_edges rises while the max stays put]
          [if a name appears only inside a fenced example then it is not a ref]
REG  K-04 Count agent rule files ((private path)/**/*.md) at or over CFG.RULE_MAX_LINES.
          Rules are meant to be small and single-topic; a long one is a doc.
          Total their lines past the last allowed one too, as K-01 does.
          [if a 250-line rule grows to 900 then skills.rules_over_excess
           rises by 650 while skills.rules_over_limit stays put]
REG  K-05 Refuse to report a clean scan over an empty skill set: zero is both
          a value and an error signature ((private path)).
          [if fewer than CFG.MIN_SKILLS skills are found then the run aborts]
          [if a SKILL.md git tracks under a skill tree is not discovered then
           the run aborts, so the scanned set cannot shrink unnoticed]

Skill references reuse scripts/export_global_skills.py's documentation filter
(fenced code, `ref-examples` blocks, `<!-- not-a-ref -->` lines) so the two
tools agree about what counts as a reference.

Usage (markdown-it-py comes from the pinned quality tool manifest, not the
project venv, so run it the way the quality gate does):
    UV="uv run --isolated --no-project --with-requirements ops/quality/requirements.txt"
    $UV python -m scripts.skill_tree_lint           # report every measurement
    $UV python -m scripts.skill_tree_lint --json    # the same, machine readable
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import unquote, urlsplit

from scripts.export_global_skills import (
    BARE_NAME_MIN_LEN,
    LOCAL_SKILL_TREES,
    WIKI_RE,
    doc_line_mask,
    is_markdown,
)

if TYPE_CHECKING:
    # markdown-it-py is in the repo venv (uv.lock), not in the pinned mypy env.
    from markdown_it import MarkdownIt  # type: ignore[import-not-found]
    from markdown_it.token import Token  # type: ignore[import-not-found]


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
@cache
def _md_parser() -> MarkdownIt:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark")


# Leaf tokens whose text is prose, where a bare `.md` path still counts. Code
# blocks, fenced or indented, are examples: the parser finds them wherever they
# sit (a block quote, a list item), which the line filter cannot.
# A raw HTML tag is markup: its attributes (a title, an alt) are never shown as
# instructions, so only the text between tags is read. A quoted attribute
# value may hold a `>`, so quotes are consumed whole rather than ending the tag.
_ATTRS = r"""(?:[^>"']|"[^"]*"|'[^']*')"""
HTML_TAG_RE = re.compile(rf"<[A-Za-z/!?]{_ATTRS}*>")
# Inline content renders flush against its neighbors (`target-<em>skill</em>`
# and `target-<wbr>skill` both read `target-skill`), and so do unknown and
# custom elements, which a browser lays out inline. So only the tags listed
# here part words: block boxes, line breaks and embedded content. Listing the
# gap tags rather than the inline ones means a tag nobody listed can never
# split a real handoff out of the ratchet.
_GAP_TAGS = frozenset(
    {
        *("address", "article", "aside", "blockquote", "br", "caption", "dd", "details", "dialog", "div"),
        *("dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5"),
        *("h6", "header", "hgroup", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary"),
        *("table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "audio", "button", "canvas"),
        *("embed", "iframe", "img", "input", "object", "select", "svg", "textarea", "video", "xmp"),
        "plaintext",
    }
)


def _untag(text: str) -> str:
    """Some HTML with its tags removed, as it reads when rendered."""

    def gap(tag: re.Match[str]) -> str:
        name = re.match(r"</?([A-Za-z][A-Za-z0-9-]*)", tag.group(0))
        return " " if name is None or name.group(1).lower() in _GAP_TAGS else ""

    return HTML_TAG_RE.sub(gap, text)


# A script, style or template body is never rendered, so it is never read, and
# neither is document metadata (`<head>`, `<title>`), the fallback content of
# embedded content a browser renders itself (`<iframe>`, `<video>`, `<audio>`,
# `<canvas>`, `<noembed>`, `<noframes>`), the fallback a browser shows only
# with scripting off (`<noscript>`), or a `<datalist>`; a `<pre>` block is a
# code example, skipped like a fenced one.
# Templates nest, so open elements are kept on a stack; a script, style,
# iframe, noembed, noframes, noscript or title body is raw text, so only its
# own end tag ends it.
# A `<textarea>` body is escapable raw text too: no tag inside it opens
# anything, but unlike the others its text is shown, as text, so a tag written
# in it is read as the characters it shows rather than as markup. An `<xmp>`
# or `<plaintext>` body is shown the same way, as raw text.
# Comments are scanned in the same left-to-right pass: a script or style body
# ends at its end tag even inside comment-looking text, which a comment
# stripped first would delete.
# A comment with no `-->` runs to the end of its HTML block, which CommonMark
# extends to the end of the document.
# Any element carrying the global `hidden` attribute is not rendered either
# (`<div hidden>`, `<a hidden href>`), up to its own end tag: an element of the
# same name inside it nests, so its end tag does not end the hidden one. Such
# an element is kept on the stack as `<name> hidden`. A void element
# (`<input hidden>`) has no body to hide.
_HIDDEN_NAMES = frozenset(
    {
        *("script", "style", "template", "pre", "title", "head", "iframe", "noembed", "noframes"),
        *("noscript", "video", "audio", "canvas", "datalist", "textarea", "xmp", "plaintext"),
    }
)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
HTML_HIDDEN_TAG_RE = re.compile(
    rf"<!--.*?(?:-->|\Z)|<(/?)([A-Za-z][A-Za-z0-9-]*)(?=[\s/>])({_ATTRS}*)>", re.IGNORECASE | re.DOTALL
)
_HIDDEN_ATTR_RE = re.compile(r"(?<![^\s])hidden(?=[\s=/]|\Z)", re.IGNORECASE)
_RAW_TEXT = frozenset(
    {"script", "style", "title", "iframe", "noembed", "noframes", "noscript", "textarea", "xmp", "plaintext"}
)
# Raw-text elements whose body is shown. Only a textarea's decodes entities;
# an `<xmp>` or `<plaintext>` body is shown exactly as written, and nothing
# ends a `<plaintext>`, so it runs to the end of the document.
_SHOWN_RAW = frozenset({"textarea", "xmp", "plaintext"})
_ESCAPABLE_RAW = frozenset({"textarea"})


# HTML's optional end tags: a start tag ends an open element it cannot sit
# inside (`<li hidden>a<li>b` shows `b`), unless an element between them
# scopes it (`<li hidden><ul><li>b` nests). While anything is open, those
# scoping elements are kept on the stack so that can be told.
_DEFAULT_SCOPE = frozenset({"applet", "caption", "html", "marquee", "object", "table", "td", "template", "th"})
_LIST_SCOPE = _DEFAULT_SCOPE | {"menu", "ol", "ul"}
_CLOSES_P = frozenset(
    {
        *("address", "article", "aside", "blockquote", "center", "dd", "details", "dialog", "dir", "div"),
        *("dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5"),
        *("h6", "header", "hgroup", "hr", "li", "listing", "main", "menu", "nav", "ol", "p", "plaintext"),
        *("pre", "search", "section", "summary", "table", "ul", "xmp"),
    }
)
_CELL = frozenset({"td", "th"})
_ROW_GROUP = frozenset({"tbody", "tfoot", "thead"})
_TABLE_SCOPE = frozenset({"html", "table", "template"})
_SELECT_SCOPE = _DEFAULT_SCOPE | {"datalist", "select"}
_ROW_SCOPE = _TABLE_SCOPE | _ROW_GROUP | {"tr"}
_End = tuple[tuple[frozenset[str], frozenset[str]], ...]
# start tag -> ((elements it ends, elements that scope them), ...), applied in order
_IMPLIED_END: dict[str, _End] = {name: ((frozenset({"p"}), _DEFAULT_SCOPE | {"button"}),) for name in _CLOSES_P}
_IMPLIED_END["li"] += ((frozenset({"li"}), _LIST_SCOPE),)
for _name in ("dd", "dt"):
    _IMPLIED_END[_name] += ((frozenset({"dd", "dt"}), _DEFAULT_SCOPE | {"dl"}),)
_IMPLIED_END.update(
    {
        "option": ((frozenset({"option"}), _SELECT_SCOPE | {"optgroup"}),),
        "optgroup": ((frozenset({"option"}), _SELECT_SCOPE | {"optgroup"}), (frozenset({"optgroup"}), _SELECT_SCOPE)),
        "td": ((_CELL, _ROW_SCOPE),),
        "th": ((_CELL, _ROW_SCOPE),),
        "tr": ((_CELL, _ROW_SCOPE), (frozenset({"tr"}), _TABLE_SCOPE | _ROW_GROUP)),
        **{
            g: ((_CELL, _ROW_SCOPE), (frozenset({"tr"}), _TABLE_SCOPE | _ROW_GROUP), (_ROW_GROUP, _TABLE_SCOPE))
            for g in _ROW_GROUP
        },
        "rp": ((frozenset({"rp", "rt"}), _DEFAULT_SCOPE | {"ruby"}),),
        "rt": ((frozenset({"rp", "rt"}), _DEFAULT_SCOPE | {"ruby"}),),
    }
)
# A caption or column group ends at the next table part, and a new caption or
# column group also ends any open rows, cells and row groups before it.
_TABLE_PART: _End = ((frozenset({"caption", "colgroup"}), _TABLE_SCOPE),)
for _name in ("td", "th", "tr", *_ROW_GROUP):
    _IMPLIED_END[_name] = _TABLE_PART + _IMPLIED_END[_name]
for _name in ("caption", "colgroup"):
    _IMPLIED_END[_name] = _IMPLIED_END.get(_name, ()) + _TABLE_PART + _IMPLIED_END["tbody"][1:]
_IMPLIED_END["col"] = ((frozenset({"caption"}), _TABLE_SCOPE),)
_SCOPING = _LIST_SCOPE | _SELECT_SCOPE | _ROW_SCOPE | {"button", "dl", "optgroup", "ruby"}


def _end_implied(name: str, open_tags: list[str], shown: list[str]) -> None:
    """End, before start tag `name`, each open element it implicitly ends."""
    for ends, scope in _IMPLIED_END.get(name, ()):
        names = [_tag_name(e) for e in open_tags]
        for i in range(len(names) - 1, -1, -1):
            if names[i] in ends:
                del open_tags[i:]
                if names[i] in _GAP_TAGS and not open_tags:
                    shown.append(" ")
                break
            if names[i] in scope:
                break


def _has_hidden_attr(attrs: str) -> bool:
    """Whether a start tag's attributes include `hidden`, outside any quoted value."""
    return bool(_HIDDEN_ATTR_RE.search(re.sub(r"\"[^\"]*\"|'[^']*'", '""', attrs)))


def _tag_name(entry: str) -> str:
    """The element name of an open-element stack entry (`div hidden` -> `div`)."""
    return entry.split(" ", 1)[0]


def _visible(open_tags: list[str]) -> bool:
    """Whether text at this point is rendered: no element hides it."""
    return all(name in _SHOWN_RAW for name in open_tags)


# The one attribute that does lead somewhere: a raw `<a href>` is a link.
# Its value is HTML, so entities are decoded before it is read as a path.
_HREF = rf"""<a\s{_ATTRS}*?(?<=\s)href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)){_ATTRS}*>"""
HTML_ANCHOR_OPEN_RE = re.compile(rf"^{_HREF}$", re.IGNORECASE)
HTML_ANCHOR_CLOSE_RE = re.compile(r"^</a\s*>$", re.IGNORECASE)
# An anchor's start or end tag inside a raw HTML block. They are matched one at
# a time, not as pairs: an anchor may open in one block and close in a later
# one (across a blank line), or never close at all, and is a link either way.
# A quoted span on one line, double or single, straight or curly. A single
# quote is also an apostrophe, so it only opens after a non-word character and
# only closes before one (`don't` and `it's` quote nothing).
QUOTED_RE = re.compile(
    r'"([^"\n]+)"|\u201c([^\u201d\n]+)\u201d'
    r"|(?<!\w)'([^'\n]+)'(?!\w)|(?<!\w)\u2018([^\u2019\n]+)\u2019(?!\w)"
)
HTML_ANCHOR_TAG_RE = re.compile(rf"{_HREF}|</a\s*>", re.IGNORECASE)
_PROSE_TOKENS = frozenset({"text", "code_inline", "html_inline", "html_block"})


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


def discover(repo: Path) -> list[Skill]:
    """Every skill directory, once, even when one tree symlinks another."""
    seen: dict[Path, Skill] = {}
    for tree in LOCAL_SKILL_TREES:
        base = repo / tree
        if not base.is_dir():
            continue
        for skill_md in sorted(base.rglob("SKILL.md")):
            real = skill_md.parent.resolve()
            seen.setdefault(real, Skill(skill_md.parent.name, real))
    return sorted(seen.values(), key=lambda s: s.name)


def _read(path: Path) -> str:
    """A file's text, decoded strictly: a replaced byte could alter a
    reference or a line and let the ratchet undercount while reporting
    success, so an undecodable file aborts the scan and names itself."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"skill_tree_lint: {path} is not valid UTF-8 ({exc})") from exc


def _ref_lines(path: Path) -> list[str]:
    """Lines that can carry a reference, with documentation filtered out."""
    lines = _read(path).splitlines()
    return [
        line for line, is_doc in zip(lines, doc_line_mask(lines, markdown=is_markdown(path)), strict=True) if not is_doc
    ]


def _line_count(path: Path) -> int:
    return len(_read(path).splitlines())


# ----- K-02: files inside one skill -----------------------------------------


class UnparsableDestinationError(ValueError):
    """A link destination this scan cannot classify as local or remote."""


def _is_local(href: str) -> bool:
    # A root-relative destination (/docs/x) is a path on a web site, not a
    # file beside this one.
    try:
        parts = urlsplit(href)
    except ValueError as exc:
        # urlsplit raises only while splitting an authority (`http://[bad`), and
        # an authority is what makes a destination remote, so that is a
        # classification, not a guess. Anything else fails the scan by name.
        if AUTHORITY_RE.match(href):
            return False
        raise UnparsableDestinationError(f"cannot classify link destination {href!r}: {exc}") from exc
    return not parts.scheme and not parts.netloc and not parts.path.startswith("/")


def _parse_file(path: Path) -> tuple[set[str], str, list[tuple[str, str]], list[tuple[str | None, str]]]:
    """_parse_md over a file's reference lines, naming the file on a failure."""
    try:
        return _parse_md("\n".join(_ref_lines(path)))
    except UnparsableDestinationError as exc:
        raise UnparsableDestinationError(f"{path}: {exc}") from exc


def _code_path(code: str) -> bool:
    """Whether a code span's content could be a local relative path."""
    return "/" in code and _is_local(code) and "\n" not in code


def _link_event(token: Token) -> tuple[str, str] | None:
    """("open", href) or ("close", "") when a token starts or ends a link,
    written in Markdown or as a raw inline HTML anchor; None otherwise."""
    if token.type == "link_open":
        return "open", str(token.attrGet("href") or "")
    if token.type == "link_close":
        return "close", ""
    if token.type == "html_inline":
        if anchor := HTML_ANCHOR_OPEN_RE.match(token.content):
            # A raw `<a href>` opens a link exactly as `[label](href)` does.
            return "open", html.unescape("".join(g or "" for g in anchor.groups()))
        if HTML_ANCHOR_CLOSE_RE.match(token.content):
            return "close", ""
    return None


def _opens_hidden(token: Token) -> bool:
    """Whether a raw inline tag opens an element its `hidden` attribute hides."""
    if token.type != "html_inline":
        return False
    tag = HTML_HIDDEN_TAG_RE.match(token.content)
    return tag is not None and _has_hidden_attr(tag.group(3) or "") and not tag.group(1)


def _raw_text_body(text: str, pos: int, open_tags: list[str], shown: list[str]) -> int | None:
    """Read the raw-text body of `open_tags[-1]` from `pos` into `shown`;
    return where its end tag ends, or None when this text ends first.

    Nothing in the body is a tag or a comment, and only the element's own end
    tag ends it. That end tag may carry (ignored) attributes, whose quoted
    values can hold a `>`."""
    name = _tag_name(open_tags[-1])
    end = None if name == "plaintext" else re.compile(rf"</{name}(?=[\s/>]){_ATTRS}*>", re.IGNORECASE).search(text, pos)
    body = text[pos : len(text) if end is None else end.start()]
    if _visible(open_tags):
        # Shown as the characters it holds (entities decoded where the
        # element decodes them), then a `<` kept from reading as a tag
        # downstream.
        shown.append(html.escape(html.unescape(body) if name in _ESCAPABLE_RAW else body, quote=False))
    if end is None:
        return None
    if _tag_name(open_tags.pop()) in _GAP_TAGS and not open_tags:
        shown.append(" ")
    return end.end()


def _on_tag(tag: re.Match[str], open_tags: list[str], shown: list[str]) -> None:
    """Apply one `HTML_HIDDEN_TAG_RE` match to the open-element stack. A
    comment is dropped; any tag that neither hides nor ends a hidden element is
    kept, where shown, as markup the caller still reads (an anchor, a gap)."""
    closing, name = tag.group(1), (tag.group(2) or "").lower()
    if name and not closing:
        _end_implied(name, open_tags, shown)
    names = [_tag_name(e) for e in open_tags]
    hides = bool(name) and not closing and name not in _VOID and _has_hidden_attr(tag.group(3))
    if name in _HIDDEN_NAMES and not closing:
        if name in _GAP_TAGS and not open_tags:
            shown.append(" ")
        open_tags.append(f"{name} hidden" if hides else name)
    elif hides or (name and not closing and (name in names or (open_tags and name in _SCOPING))):
        # A hidden element, one nested in a hidden element of its name, or one
        # that scopes the optional end tags of what is open.
        open_tags.append(f"{name} hidden" if hides else name)
    elif name and closing and name in names:
        del open_tags[len(names) - 1 - names[::-1].index(name) :]
        if name in _GAP_TAGS and not open_tags:
            shown.append(" ")
    elif name and _visible(open_tags):
        shown.append(tag.group(0))


def _strip_hidden(text: str, open_tags: list[str]) -> str:
    """The parts of some raw HTML outside any comment or unrendered element
    (see `HTML_HIDDEN_TAG_RE`). `open_tags` holds the hidden elements still
    open, and is updated in place.

    What is removed takes up no width, so the text on either side of it stays
    joined (`target-<!-- x -->skill` reads `target-skill`), unless the outermost
    removed element is itself a box, like a `<pre>` or an `<iframe>`."""
    shown: list[str] = []
    pos = 0
    while True:
        if open_tags and _tag_name(open_tags[-1]) in _RAW_TEXT:
            end = _raw_text_body(text, pos, open_tags, shown)
            if end is None:
                return "".join(shown)
            pos = end
            continue
        tag = HTML_HIDDEN_TAG_RE.search(text, pos)
        if tag is None:
            break
        if _visible(open_tags):
            shown.append(text[pos : tag.start()])
        _on_tag(tag, open_tags, shown)
        pos = tag.end()
    if _visible(open_tags):
        shown.append(text[pos:])
    return "".join(shown)


class _LinkWalker:
    """Walks markdown-it tokens, splitting link destinations, prose, and each
    local link's label (see `_parse_md`)."""

    def __init__(self) -> None:
        self.hrefs: list[str] = []
        self.code_paths: list[str] = []
        # Each unanchored delimited path, with the destination of the link
        # whose label holds it, if any.
        self.spans: list[tuple[str | None, str]] = []
        self.prose: list[str] = []
        self.labels: list[tuple[str, list[str]]] = []
        self.label: list[str] | None = None
        self.hidden: list[str] = []

    def open_link(self, href: str) -> None:
        self.hrefs.append(href)
        # A `skill:<name>` destination is an explicit invocation, whatever
        # the label says, so it is read as one; any other scheme is remote.
        self.prose.append(f" {unquote(href)} " if href[:6].lower() == "skill:" else " ")
        if _is_local(href):
            self.label = []
            self.labels.append((unquote(urlsplit(href).path), self.label))

    def block_anchors(self, visible: str) -> None:
        """Route a raw HTML block's shown text, anchor by anchor.

        A local anchor's words become its label; a remote anchor's words stay
        in the prose, as a Markdown link's would. An anchor left open stays open
        into the following tokens, exactly as an inline `<a href>` does."""
        pos = 0
        for tag in HTML_ANCHOR_TAG_RE.finditer(visible):
            self._shown(visible[pos : tag.start()])
            if tag.group(0).startswith("</"):
                self.label = None
                self.prose.append(" ")
            else:
                self.open_link(html.unescape("".join(g or "" for g in tag.groups())))
            pos = tag.end()
        self._shown(visible[pos:])

    def _shown(self, raw: str) -> None:
        out = self.prose if self.label is None else self.label
        out.append(html.unescape(_untag(raw)))

    def _path_readings(self, path: str) -> None:
        self.code_paths += [path, unquote(path), unquote(urlsplit(path).path)]

    def _delimited(self, path: str) -> str:
        """Take a delimited path out of the prose, keeping its readings.

        Literally, decoded, and as a link destination would be read, without
        its query or fragment (`./guide.md#setup` leads to `guide.md`). An
        anchored path is read only that way; any other is also kept apart
        as a span, read by name only as a shorthand (see `_read_by_name`)."""
        self._path_readings(path)
        if not path.startswith(("./", "../")):
            self.spans.append((None if self.label is None else self.labels[-1][0], path))
            return " "
        # A command can follow an anchored path in one span
        # (`../runner --delegate skill:x`). The whole span is still read as a
        # path, and so is each later word that could be one, by itself and
        # only by where it resolves (`../runner ../x/SKILL.md`). It is not
        # read by name: it cannot be told apart from a segment of a spaced
        # pathname (`../a b/skills/x/SKILL.md`). Every other word, a `/x` call
        # included, stays in the prose and is read as prose is.
        prose: list[str] = []
        for word in path.split()[1:]:
            if word.startswith("/") or not _code_path(word):
                prose.append(word)
            else:
                self._path_readings(word)
        return " " + " ".join(prose) + " "

    def _quoted(self, m: re.Match[str]) -> str:
        path = next(g for g in m.groups() if g is not None).strip()
        return self._delimited(path) if _code_path(path) else m.group(0)

    def text(self, token: Token) -> str:
        if not _visible(self.hidden):
            # Inside an unrendered element only its raw HTML is read, for
            # the tag that ends it.
            if not token.type.startswith("html_"):
                return " "
        elif token.type == "code_inline" and _code_path(token.content.strip()):
            # A code span is delimited, so a local path in one is taken whole,
            # spaces and all (`some folder/x/SKILL.md`), and also read by
            # where it resolves, like a link destination. Code is literal, so
            # `%20` may be part of a real name; both readings are kept, and
            # only one that resolves counts. A span is taken whole, so where
            # it points is all it says: one that resolves nowhere names a
            # skill only as a shorthand (`skills/x`); `/x` is not a path.
            return self._delimited(token.content.strip())
        elif token.type == "text":
            # A quoted path is delimited too (`"some folder/x/SKILL.md"`), so
            # it is read like a code span's. Markdown text arrives as shown:
            # an escaped or entity-encoded `<!-- ... -->` or `<b>` is visible
            # text, so nothing in it is stripped as markup.
            return QUOTED_RE.sub(self._quoted, token.content)
        elif not token.type.startswith("html_"):
            # Code already arrives as shown, like text.
            return token.content
        # An HTML comment is invisible: a commented-out handoff is not one.
        visible = _strip_hidden(token.content, self.hidden)
        if token.type == "html_block":
            self.block_anchors(visible)
            return " "
        # Raw HTML is shown with its entities decoded.
        return html.unescape(_untag(visible))

    def visit(self, tokens: list[Token]) -> None:
        for token in tokens:
            out = self.prose if self.label is None else self.label
            # A link inside an unrendered element is never followed either.
            event = None if self.hidden or _opens_hidden(token) else _link_event(token)
            if event is not None and event[0] == "open":
                self.open_link(event[1])
            elif event is not None:
                self.label = None
                self.prose.append(" ")
            elif token.type in ("image", "softbreak", "hardbreak"):
                # A picture is shown, not opened and followed: neither its
                # source nor its alt text is a reference.
                out.append(" ")
            elif token.type in _PROSE_TOKENS:
                out.append(self.text(token))
            if token.children and token.type != "image":
                self.visit(token.children)
        self.prose.append(" ")


def _parse_md(
    text: str,
) -> tuple[set[str], str, list[tuple[str, str]], list[tuple[str | None, str]]]:
    """Every local link destination in some markdown, decoded; its prose;
    each local link's label paired with that link's destination; and each
    unanchored delimited path, paired with the destination of the link whose
    label holds it, if any.

    Given a whole file rather than one line, so a link spanning lines is still
    one construct. A destination is taken whole, however CommonMark lets it be
    written (angle brackets, spaces, escapes, nested parentheses, percent
    encoding), and its title is never read as a reference. A local link's
    label is kept apart from the prose so a caller can tell a label that only
    restates where its link points from a label that names something else.
    """
    walker = _LinkWalker()
    walker.visit(_md_parser().parse(text, {}))
    hrefs, prose, labels = walker.hrefs, walker.prose, walker.labels
    # Reference definitions are read only through the links that use them: the
    # parser resolves `[x][ref]` onto its link_open, and an unused `[ref]: x.md`
    # leads nowhere.
    # Only a local destination can name a file or a skill here: a remote URL
    # whose path happens to end in `skills/<name>` is a document, not a handoff.
    local = {unquote(urlsplit(href).path) for href in hrefs if _is_local(href)}
    local |= set(walker.code_paths)
    paired = [(dest, _unlinked("".join(words))) for dest, words in labels]
    spans = [(dest, _unlinked(span)) for dest, span in walker.spans]
    return local - {""}, _unlinked("".join(prose)), paired, spans


def _unlinked(text: str) -> str:
    """Prose with remote references removed and each local file name's query
    or fragment dropped: `guide.md#skill:x` is a place in `guide.md`, not a
    handoff, while the file name itself is still read."""
    return LOCAL_SUFFIX_RE.sub(r"\1", URL_RE.sub(" ", text))


def _md_tokens(path: Path) -> set[str]:
    """Markdown file paths in a file: link destinations in full, plus bare paths
    named in prose or a link label (a bare `docs/x.md` still counts)."""
    destinations, prose, labels, spans = _parse_file(path)
    words = " ".join([prose, *(label for _, label in labels), *(span for _, span in spans)])
    return {d for d in destinations if is_markdown(Path(d))} | set(MD_TOKEN_RE.findall(words))


def _in_skill(target: Path, skill: Skill) -> bool:
    """Whether a path is one of this skill's own files: inside its directory
    and not inside a skill nested there. A nested skill's files are its own,
    and a link to it is a handoff, measured by K-03 rather than as a file.

    Containment is checked first, so nothing outside the skill is stat()ed."""
    if not target.is_relative_to(skill.root):
        return False
    return not any(
        (d / "SKILL.md").is_file() for d in target.parents if d != skill.root and d.is_relative_to(skill.root)
    )


def _file_edges(path: Path, skill: Skill, repo: Path) -> set[Path]:
    edges: set[Path] = set()
    for token in _md_tokens(path):
        for base in (path.parent, repo):
            target = (base / token).resolve()
            # Containment first: a quoted absolute path (a home directory)
            # must never be stat()ed, or an unreadable one aborts the scan.
            inside = target != path and _in_skill(target, skill)
            if inside and target.is_file():
                edges.add(target)
                break
    return edges


def _reachable(start: N, graph: dict[N, set[N]]) -> set[N]:
    seen: set[N] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return seen


def _components(graph: dict[N, set[N]]) -> dict[N, frozenset[N]]:
    """Each node's strongly connected group: the nodes it reaches that reach it back."""
    nodes = set(graph) | {t for targets in graph.values() for t in targets}
    reach: dict[N, set[N]] = {n: _reachable(n, graph) for n in nodes}
    groups: dict[N, frozenset[N]] = {}
    for node in nodes:
        mutual = {m for m in reach[node] if node in reach[m]}
        mutual.add(node)
        groups[node] = frozenset(mutual)
    return groups


def _deepest(graph: dict[N, set[N]], starts: Iterable[N]) -> list[N]:
    """Deepest chain from any start, with each cycle counted once at its full size.

    Cycles are collapsed first and the longest path is taken over the acyclic
    graph that leaves, a cycle contributing all its members (an upper bound on
    any simple path through it). Enumerating simple paths instead is factorial
    on a dense cycle, which a ring of cross-referencing skills would hit.
    """
    comp = _components(graph)
    memo: dict[frozenset[N], list[N]] = {}

    def best(group: frozenset[N]) -> list[N]:
        if group not in memo:
            after = {comp[t] for n in group for t in graph.get(n, ()) if comp[t] != group}
            tails = [best(g) for g in sorted(after, key=lambda g: sorted(map(str, g)))]
            memo[group] = sorted(group, key=str) + max(tails, key=len, default=[])
        return memo[group]

    chains: list[list[N]] = []
    for start in starts:
        chain: list[N] = best(comp[start])
        if start in chain:
            chain = [start] + [n for n in chain if n != start]
        chains.append(chain)
    return max(chains, key=len)


def _deep_edges(graph: dict[N, set[N]], start: N) -> int:
    """References made below the first level: every edge leaving a node that
    start reaches, other than start itself.

    A flat tree has none. Each reference added past the first level adds one,
    including one that ends at a node a shorter route already reaches, so a
    new deep route moves this even when no depth or node count changes.
    """
    return sum(len(graph.get(node, ())) for node in _reachable(start, graph) - {start})


def _file_graph(skill: Skill, repo: Path) -> dict[Path, set[Path]]:
    """The Markdown files SKILL.md reaches, each mapped to the files it references.

    Built outward from SKILL.md, so a note nothing links to is never read: it
    cannot be loaded, and an undecodable one must not abort the scan."""
    start = skill.skill_md.resolve()
    graph: dict[Path, set[Path]] = {}
    stack = [start]
    while stack:
        md = stack.pop()
        if md not in graph:
            graph[md] = _file_edges(md, skill, repo)
            stack.extend(t for t in graph[md] if is_markdown(t) and t not in graph)
    return graph


def file_ref_depth(skill: Skill, repo: Path) -> list[Path]:
    return _deepest(_file_graph(skill, repo), [skill.skill_md.resolve()])


def loaded_files(skill: Skill, repo: Path) -> list[Path]:
    """SKILL.md and every file it reaches: what an agent can actually be led to read.

    An unlinked note in the skill directory is never loaded, so it must not
    add skill references (and through them depth or cycles) either.
    """
    start = skill.skill_md.resolve()
    return sorted({start} | _reachable(start, _file_graph(skill, repo)))


# ----- K-03: skills naming skills -------------------------------------------


# A skill name ends where the name does: `x.md` and `x-old` are other things,
# while sentence punctuation (`see x.`) and a path separator are not.
_NAME_END = r"(?![\w-]|\.\w)"


def _invocation_pattern(name: str) -> re.Pattern[str]:
    """A `/name`, `$name` or `skill:name` call: how a skill is invoked by name."""
    return re.compile(rf"(?:(?<![\w/.:-])[/$]|(?<![\w-])(?i:skill):){re.escape(name)}{_NAME_END}")


def _name_patterns(name: str) -> list[re.Pattern[str]]:
    escaped = re.escape(name)
    patterns = [
        re.compile(rf"skills/{escaped}{_NAME_END}"),
        # Includes `skill:html`, the explicit form the exporter also reads.
        _invocation_pattern(name),
        # A sibling link such as [specialist](../html/SKILL.md): the label
        # need not name the skill, and the path has no skills/ segment.
        re.compile(rf"(?<![\w.])\.\./{escaped}{_NAME_END}"),
    ]
    if "-" in name and len(name) >= BARE_NAME_MIN_LEN:
        patterns.append(re.compile(rf"(?<![\w/$-]){escaped}(?![\w/-]|\.\w)"))
    return patterns


def _path_pattern(name: str) -> re.Pattern[str]:
    """A path token with this name as a directory: `(private path)`, `../x`,
    `../_vendored/x`, or a direct child `x/SKILL.md`. Any parent will do,
    because `_path_target` keeps only a token that resolves to a discovered
    skill directory. A bare word is not a path, so `x` alone needs a `/`."""
    escaped = re.escape(name)
    return re.compile(rf"(?<![\w./-])(?:[\w./-]*?/{escaped}{_NAME_END}|{escaped}(?=/))")


def _read_by_name(token: str, name: str) -> bool:
    """Whether a whole path token that resolves nowhere still names its skill.

    Only the shorthands do: `skills/x`, and `x/...` with no parent. Any other
    parent says where the skill lives, so `other-repo/skills/x` names
    nothing when it is not there."""
    return token[: -len(name)].rstrip("/") in ("", "skills")


def _path_target(token: str, md: Path, repo: Path, roots: list[Path]) -> Path | None:
    """The one skill directory a path token points at, or None if it is not one."""
    for base in (md.parent, repo):
        target = (base / token).resolve()
        if target in roots:
            return target
    return None


def _dest_skill(dest: str, md: Path, repo: Path, roots: set[Path]) -> Path | None:
    """The skill directory a whole link destination lies in, or None.

    The destination is resolved as written, whatever characters its parents
    hold (`<../some folder/x/SKILL.md>`), so a link to a skill's `SKILL.md`,
    to the directory itself, or to a file inside it all lead to that skill.
    A base under which the destination exists wins: `(private path)` is
    read from the repo root, not as a missing file in this skill's own
    directory. Where it exists under neither, another skill wins over this
    file's own, so a link to a missing file in another skill is still a
    handoff rather than a self-reference. Otherwise this file's directory is
    tried first, as a link is."""
    own = next((p for p in md.resolve().parents if p in roots), None)
    found: list[Path] = []
    for base in (md.parent, repo):
        target = (base / dest).resolve()
        root = next((p for p in (target, *target.parents) if p in roots), None)
        if root is not None and target.exists():
            return root
        if root is not None:
            found.append(root)
    return next((r for r in found if r != own), found[0] if found else None)


def _resolved_names(dest: str, md: Path, repo: Path, targets: dict[str, _Target]) -> set[str]:
    """The skill names whose directory a link destination points at."""
    whole = _dest_skill(dest, md, repo, {root for t in targets.values() for root in t.roots})
    # Only where the destination resolves, so a segment it climbs back out
    # of (`../a/../b/SKILL.md`) is not mistaken for where it points.
    return {name for name, t in targets.items() if whole in t.roots}


@dataclass(frozen=True)
class _Target:
    """Everything needed to find references to one skill name."""

    patterns: list[re.Pattern[str]]
    path: re.Pattern[str]
    roots: list[Path]


def _skill_edges(skill: Skill, targets: dict[str, _Target], repo: Path) -> set[Path]:
    """The skill directories one skill refers to.

    A path that resolves to one directory refers to that copy only. A bare
    name, wiki link or command cannot say which copy it means when two trees
    hold the same name, so it refers to every copy of that name.
    """
    by_path: set[Path] = set()
    by_name: set[str] = set()
    invokes_self = False
    invocation = _invocation_pattern(skill.name)
    all_roots = {root for t in targets.values() for root in t.roots}
    for md in loaded_files(skill, repo):
        # The parsed prose and decoded destinations, never the raw text: a path
        # written `../target%2Dskill/SKILL.md` still names `target-skill`, and a
        # name inside a link title is metadata, not a handoff.
        destinations, prose, labels, spans = _parse_file(md)
        # A label naming the skill its link resolves to only restates that path,
        # so it must not also name every copy: `[dup](../dup/SKILL.md)`. The rest
        # of the label is still read: `[a, then /b](../a/SKILL.md)` names b.
        # A link destination is a path whatever it looks like, so one that
        # resolves into a skill directory is a reference to it, with no `/` in
        # it (`[specialist](pdf)`, `[specialist](pdf#usage)`) or with parents
        # no path pattern spells (`<../some folder/pdf/SKILL.md>`).
        # So a destination is read only by where it resolves, never by its
        # spelling: `../../other-repo/skills/x/SKILL.md` names no local skill.
        by_path |= {r for d in destinations if (r := _dest_skill(d, md, repo, all_roots)) is not None}
        lines = [(prose, set[str](), False)]
        lines += [(label, _resolved_names(dest, md, repo, targets), False) for dest, label in labels]
        # A delimited path is taken whole, so it says where it points.
        lines += [(span, _resolved_names(dest, md, repo, targets) if dest else set(), True) for dest, span in spans]
        for line, restated, whole in lines:
            wiki = {m.group(1) for m in WIKI_RE.finditer(line)}
            invokes_self = invokes_self or skill.name in wiki or bool(invocation.search(line))
            for name, target in targets.items():
                rest = line
                # A path anchored at this file (`./x`, `../x`) says exactly where
                # it points, so one that resolves nowhere names nothing:
                # `../../other-repo/skills/x`. So does a whole delimited one
                # with any parent but the `skills/` shorthand. An unanchored
                # `skills/x` or a `/x` command is still read by name, and so
                # is any path in prose, where nothing says where it starts.
                for m in reversed(list(target.path.finditer(line))):
                    root = _path_target(m.group(0), md, repo, target.roots)
                    if root is not None:
                        by_path.add(root)
                    if (
                        root is not None
                        or m.group(0).startswith(("./", "../"))
                        or (whole and not _read_by_name(m.group(0), name))
                    ):
                        rest = rest[: m.start()] + " " + rest[m.end() :]
                if name not in restated and (name in wiki or any(p.search(rest) for p in target.patterns)):
                    by_name.add(name)
    # A skill names itself in its frontmatter, headings and paths to its own
    # files. Only an explicit invocation of itself is a handoff, a loop.
    if not invokes_self:
        by_name.discard(skill.name)
        by_path.discard(skill.root)
    return by_path | {root for name in by_name for root in targets[name].roots}


def _labels(skills: list[Skill], repo: Path) -> dict[Path, str]:
    """One graph node per skill DIRECTORY: a name shared by two trees keeps both."""
    counts = Counter(s.name for s in skills)
    return {s.root: s.name if counts[s.name] == 1 else s.root.relative_to(repo.resolve()).as_posix() for s in skills}


def skill_graph(skills: Iterable[Skill], repo: Path) -> dict[str, set[str]]:
    skills = list(skills)
    labels = _labels(skills, repo)
    roots: dict[str, list[Path]] = defaultdict(list)
    for s in skills:
        roots[s.name].append(s.root)
    targets = {name: _Target(_name_patterns(name), _path_pattern(name), rs) for name, rs in roots.items()}
    return {labels[s.root]: {labels[r] for r in _skill_edges(s, targets, repo)} for s in skills}


def _cycles(graph: dict[str, set[str]]) -> list[str]:
    """Each group of skills that can reach one another, at any cycle length."""
    groups = set(_components(graph).values())
    looped = {g for g in groups if len(g) > 1 or next(iter(g)) in graph.get(next(iter(g)), ())}
    return sorted(" <-> ".join(sorted(g)) if len(g) > 1 else f"{next(iter(g))} -> itself" for g in looped)


def _cyclic_refs(graph: dict[str, set[str]]) -> int:
    """References that sit on a cycle: both ends in one group that reaches itself.

    A skill invoking itself is a cycle of one, so its self-reference counts.

    This, not the number of groups, is what the gate ratchets. Adding a
    reference can merge two groups into one, so the group count can FALL
    while a new recursive handoff appears. Groups only ever merge as edges
    are added, so a reference inside one stays inside one: this count never
    drops when a reference is added, and rises when the new one closes a cycle.
    """
    comp = _components(graph)
    return sum(1 for src, targets in graph.items() for dst in targets if dst == src or comp[dst] == comp[src])


# ----- report ----------------------------------------------------------------


def _over_limit(files: Iterable[tuple[str, Path]], limit: int) -> list[str]:
    """Files at or over an exclusive limit: "under 500" makes 500 a violation."""
    return [f"{label} ({n} lines)" for label, path in files if (n := _line_count(path)) >= limit]


def _excess_lines(files: Iterable[Path], limit: int) -> int:
    """Lines past the last allowed one (limit - 1), summed across files."""
    return sum(max(0, _line_count(path) - (limit - 1)) for path in files)


def _tracked_skill_dirs(repo: Path) -> set[Path]:
    """The resolved directory of every SKILL.md git tracks under a skill tree.

    The expected population, read from the index rather than the disk, so a
    skill the scan skips (a missing tree, a symlinked skill directory rglob
    does not enter) is a named failure instead of a smaller denominator."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH, so the expected skill set cannot be read")
    out = subprocess.run(
        [git, "-C", str(repo), "ls-files", "-z", "--", *LOCAL_SKILL_TREES],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return {(repo / rel).parent.resolve() for rel in out.split("\0") if Path(rel).name == "SKILL.md"}


def measure(repo: Path) -> Report:
    skills = discover(repo)
    if len(skills) < CFG.MIN_SKILLS:
        raise RuntimeError(
            f"found {len(skills)} skills under {', '.join(LOCAL_SKILL_TREES)} in {repo}, "
            f"expected at least {CFG.MIN_SKILLS}. That is a broken scan, not a clean tree."
        )
    if missed := sorted(_tracked_skill_dirs(repo) - {s.root for s in skills}):
        raise RuntimeError(
            f"git tracks {len(missed)} SKILL.md the scan did not discover: "
            f"{', '.join(p.as_posix() for p in missed)}. That is a shrunken scan, not a clean tree."
        )
    worst_file = max((file_ref_depth(s, repo) for s in skills), key=len)
    deep_edges = sum(_deep_edges(_file_graph(s, repo), s.skill_md.resolve()) for s in skills)
    graph = skill_graph(skills, repo)
    worst_skill = _deepest(graph, sorted(graph))
    rules_dir = repo / CFG.RULES_DIR
    # Rules may sit in subdirectories ((private path)); every one loads.
    if not rules_dir.is_dir():
        # A missing directory would read as zero rules, a clean result the scan never measured.
        raise RuntimeError(f"skill_tree_lint: {rules_dir} is not a directory. That is a broken scan, not a clean tree.")
    rules = sorted(rules_dir.rglob("*.md"))
    return Report(
        skills_scanned=len(skills),
        over_line_limit=_over_limit(((s.name, s.skill_md) for s in skills), CFG.SKILL_MAX_LINES),
        over_line_excess=_excess_lines((s.skill_md for s in skills), CFG.SKILL_MAX_LINES),
        file_ref_depth_max=len(worst_file) - 1,
        file_ref_worst=[p.relative_to(repo.resolve()).as_posix() for p in worst_file],
        skill_ref_depth_max=len(worst_skill) - 1,
        skill_ref_worst=worst_skill,
        file_ref_deep_edges=deep_edges,
        skill_ref_deep_edges=sum(_deep_edges(graph, name) for name in graph),
        skill_ref_cycles=_cycles(graph),
        cyclic_skill_refs=_cyclic_refs(graph),
        rules_over_limit=_over_limit(((p.relative_to(rules_dir).as_posix(), p) for p in rules), CFG.RULE_MAX_LINES),
        rules_over_excess=_excess_lines(rules, CFG.RULE_MAX_LINES),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = measure(args.repo)
    if args.json:
        print(json.dumps(report.__dict__, indent=2))
        return 0
    print(f"skills scanned: {report.skills_scanned}")
    print(f"SKILL.md at or over {CFG.SKILL_MAX_LINES} lines: {report.over_line_limit or 'none'}")
    file_chain = " -> ".join(report.file_ref_worst)
    skill_chain = " -> ".join(report.skill_ref_worst)
    print(f"deepest file chain inside a skill: {report.file_ref_depth_max} ({file_chain})")
    print(f"deepest skill chain: {report.skill_ref_depth_max} ({skill_chain})")
    print(f"file references below the first level: {report.file_ref_deep_edges}")
    print(f"skill handoffs below the first level: {report.skill_ref_deep_edges}")
    print(f"skill cycles: {report.skill_ref_cycles or 'none'} ({report.cyclic_skill_refs} references on them)")
    print(f"rule files at or over {CFG.RULE_MAX_LINES} lines: {report.rules_over_limit or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
