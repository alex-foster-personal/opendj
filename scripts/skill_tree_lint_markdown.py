"""Markdown and HTML reference extraction for skill_tree_lint."""

from __future__ import annotations

import html
import re
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote


def _urlsplit(href: str):
    """Split link destinations; delegates to scripts.skill_tree_lint.urlsplit for tests."""
    import scripts.skill_tree_lint as _stl

    return _stl.urlsplit(href)


from scripts.export_global_skills import doc_line_mask, is_markdown
from scripts.skill_tree_lint_config import (
    AUTHORITY_RE,
    LOCAL_SUFFIX_RE,
    MD_TOKEN_RE,
    URL_RE,
    UnparsableDestinationError,
)
from scripts.skill_tree_lint_io import _ref_lines

if TYPE_CHECKING:
    from markdown_it import MarkdownIt  # type: ignore[import-not-found]
    from markdown_it.token import Token  # type: ignore[import-not-found]


# Links are read by a CommonMark parser rather than a pattern: five review
# rounds on #3846 each found one more legal destination form (spaces, titles,
# titles on the next line, nested parentheses) that a regex dropped. It is
# imported on first use, not at module scope: scripts/quality_gate.py imports
# this module eagerly and must stay stdlib-only for recipes such as
# `just quality-arch`, while the `skills` evaluator runs the measurement in the
# pinned ops/quality/requirements.txt env, which carries markdown-it-py.
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


# ----- K-02: files inside one skill -----------------------------------------


def _is_local(href: str) -> bool:
    # A root-relative destination (/docs/x) is a path on a web site, not a
    # file beside this one.
    try:
        parts = _urlsplit(href)
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
            self.labels.append((unquote(_urlsplit(href).path), self.label))

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
        self.code_paths += [path, unquote(path), unquote(_urlsplit(path).path)]

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
    local = {unquote(_urlsplit(href).path) for href in hrefs if _is_local(href)}
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
