"""Remote versus local link destination classification.

Sibling of ``test_skill_tree_lint.py`` for ``scripts/skill_tree_lint.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import SplitResult

import pytest

from scripts import skill_tree_lint
from scripts.skill_tree_lint import (
    UnparsableDestinationError,
    discover,
    file_ref_depth,
    main,
    measure,
    skill_graph,
)

from tests.scripts.skill_tree_lint_support import CFG, REPO, _git, _repo, _skill


def test_a_remote_url_is_not_a_skill_reference(tmp_path: Path) -> None:
    # Codex P2 BLOCKING on #3846: https://example.com/skills/target-skill
    # reduced to a local-looking path and matched the skill's name.
    repo = _repo(tmp_path)
    _skill(repo, "target-skill", "Does the work.")
    for body in (
        "Upstream [docs](https://example.com/skills/target-skill).",
        "Upstream: <https://example.com/skills/target-skill>.",
        "Upstream: https://example.com/.agents/skills/target-skill/SKILL.md",
        "Upstream [docs][u].\n\n[u]: https://example.com/skills/target-skill/SKILL.md",
        # Codex P2 BLOCKING on #3846: a protocol-relative URL is remote too.
        "Upstream: //example.com/skills/target-skill",
        "Upstream [docs](//example.com/skills/target-skill).",
        # Codex P2 BLOCKING on #3846: a root-relative link is a web path.
        "Upstream [docs](/docs/skills/target-skill).",
        "Upstream [docs](/.agents/skills/target-skill/SKILL.md).",
        # Codex P2 BLOCKING on #3846: a bare root-relative path in prose too.
        "Upstream docs live at /docs/skills/target-skill on the site.",
        # Codex P2 BLOCKING on #3846: an absolute URI without `//` is remote too.
        "Upstream: urn:docs:skills/target-skill",
        "Upstream: https:example.com/skills/target-skill",
        # Codex P2 BLOCKING on #3846: a bare host with no scheme is remote too.
        "Upstream: www.example.com/skills/target-skill",
        # Codex P2 BLOCKING on #3846: an internationalized host is remote too.
        "Upstream: docs.xn--p1ai/skills/target-skill",
        "Upstream: münich.example/skills/target-skill",
        "Upstream: docs.пример.рф/skills/target-skill",
        "Questions to target-skill@münich.example.",
        # Codex P2 BLOCKING on #3846: an SCP-style remote is taken whole.
        "Clone git@github.com:org/skills/target-skill/SKILL.md first.",
        "Clone git@host:../target-skill first.",
        # Codex P2 BLOCKING on #3846: whatever the SCP host ends in.
        "Clone git@192.168.1.2:org/skills/target-skill/SKILL.md first.",
        "Clone git@build.host1:org/skills/target-skill/SKILL.md first.",
        # Codex P2 BLOCKING on #3846: the SCP user is optional.
        "Clone 192.168.1.2:org/skills/target-skill/SKILL.md first.",
        "Clone [::1]:org/skills/target-skill first.",
        "Clone [2001:db8::1]:../target-skill first.",
        # Codex P2 BLOCKING on #3846: a local file's fragment or query is a place
        # in that file, not a handoff.
        "See guide.md#skill:target-skill for it.",
        "See guide.md#skills/target-skill for it.",
        "See notes.txt?next=/target-skill for it.",
        # Codex P2 BLOCKING on #3846: a home- or variable-rooted path is machine-local.
        "See ~/docs/skills/target-skill/SKILL.md.",
        "See ~ops/skills/target-skill.",
        "See $HOME/.agents/skills/target-skill.",
        "See ${HOME}/skills/target-skill.",
        # Codex P2 BLOCKING on #3846: so is a UNC or Windows rooted path.
        "See \\\\server\\share\\target-skill\\SKILL.md.",
        "See `\\\\server\\share\\target-skill\\SKILL.md`.",
        "See `\\docs\\skills\\target-skill`.",
        "Upstream: docs.example.org:8080/.agents/skills/target-skill/SKILL.md",
        # Codex P2 BLOCKING on #3846: localhost and an IPv4 address are hosts too.
        "Upstream: localhost/skills/target-skill",
        "Upstream: 127.0.0.1:8000/skills/target-skill",
        # Codex P2 BLOCKING on #3846: so is a bracketed IPv6 address.
        "Upstream: [::1]:8000/skills/target-skill",
        "Upstream: [2001:db8::1]/skills/target-skill",
        # Codex P2 BLOCKING on #3846: a link destination that resolves to no
        # discovered skill is not one by its spelling either.
        "See [external](../../other-repo/skills/target-skill/SKILL.md).",
        "See [external][x].\n\n[x]: ../../../../other-repo/.agents/skills/target-skill",
        '<p>See <a href="../../other-repo/skills/target-skill/SKILL.md">it</a>.</p>',
        # Codex P2 BLOCKING on #3846: so is an anchored path in prose.
        "See ../../other-repo/skills/target-skill/SKILL.md.",
        'See "../../other repo/skills/target-skill/SKILL.md".',
        "See `../../../../other-repo/.agents/skills/target-skill`.",
        "See ./skills/target-skill.",
        "See `../../other repo/skills/target-skill/SKILL.md`.",
        # Control for the anchored-command fix below: only a call after the
        # path is kept, so a name in its arguments still names nothing.
        "Run `../runner --into vendor/skills/target-skill`.",
        "Run `../runner skill:target-skill.md`.",
        # Codex P2 BLOCKING on #3846: only a word after the path is a call, not
        # the path's own fragment or a segment of a spaced pathname.
        "See `../guide.md#skill:target-skill`.",
        'See "../guide.md#skill:target-skill".',
        "See `../some folder/skill:target-skill/SKILL.md`.",
        # A path argument is read only by where it resolves, never by the
        # `skills/x` shorthand: it reads exactly like a spaced pathname's segment.
        "Run `../runner --delegate skills/target-skill`.",
        "See `../my skills/target-skill/SKILL.md`.",
        # Codex P2 BLOCKING on #3846: a whole delimited path with any parent
        # but the `skills/` shorthand says where it points, unanchored or not.
        "See `other-repo/skills/target-skill/SKILL.md`.",
        'See "other-repo/.agents/skills/target-skill".',
        "See [it](guide.md) and `vendor/skills/target-skill`.",
        # Codex P2 BLOCKING on #3846: a bare host may take a query or fragment.
        "Upstream: example.com?next=skill:target-skill",
        "Upstream: example.com#skills/target-skill",
        "Upstream: localhost:8000?next=/target-skill",
        # Codex P2 BLOCKING on #3846: a drive-qualified path is machine-local.
        "Built from C:/docs/skills/target-skill/README.md on the PC.",
        "Built from C:\\docs\\target-skill\\SKILL.md on the PC.",
        # Codex P2 BLOCKING on #3846: tag attributes are not shown text.
        'See <span title="skills/target-skill">help</span>.',
        '<img alt="target-skill" src="d.png">',
        # Codex P2 BLOCKING on #3846: a quoted attribute may hold a `>`.
        'See <span title="see > skill:target-skill">help</span>.',
        "<p>See <span title='a > /target-skill'>help</span>.</p>",
        # Codex P2 BLOCKING on #3846: only an href attribute is a link.
        '<a data-href="../target-skill/SKILL.md">specialist</a>',
        '<p>See <a aria-href="../target-skill/SKILL.md">specialist</a>.</p>',
        # Codex P2 BLOCKING on #3846: a script, style or template body is not shown.
        '<script>const x = "skill:target-skill"</script>',
        "<style>\n/* skill:target-skill */\n</style>",
        "<template><p>Then run /target-skill.</p></template>",
        'Inline <script>const x = "skill:target-skill"</script> code.',
        # Codex P2 BLOCKING on #3846: an inner close does not end the outer element.
        "Inline <template><script>x</script> skill:target-skill</template> done.",
        "<template><template>x</template> skill:target-skill</template>",
        # Codex P2 BLOCKING on #3846: a link inside a hidden element is not followed.
        'Inline <template><a href="../target-skill/SKILL.md">x</a></template> done.',
        "Inline <template>[x](../target-skill/SKILL.md)</template> done.",
        # Codex P2 BLOCKING on #3846: an element carrying `hidden` is not shown,
        # up to its own end tag, nesting included.
        "<div hidden>skill:target-skill</div>",
        "<div hidden><div>x</div> skill:target-skill</div>",
        'Inline <span hidden="hidden">skill:target-skill</span> done.',
        'Inline <a hidden href="../target-skill/SKILL.md">x</a> done.',
        '<p>See <a class="x" hidden href="../target-skill/SKILL.md">x</a>.</p>',
        "<div hidden>\n\n[x](../target-skill/SKILL.md)\n\n</div>",
        "<p><textarea hidden>skill:target-skill</textarea></p>",
        # Sol P1 BLOCKING on #3846: an element nested in a list inside a hidden
        # item stays hidden; the list scopes its items' optional end tags.
        "<ul><li hidden>x<ul><li>skill:target-skill</li></ul></li></ul>",
        "<dl><dd hidden>x<dl><dt>skill:target-skill</dt></dl></dd></dl>",
        "<div hidden><p>x<p>skill:target-skill</div>",
        "<table><tr><td hidden>x<table><tr><td>skill:target-skill</td></tr></table></td></tr></table>",
        "<table><tbody hidden><tr><td>x<tr><td>skill:target-skill</table>",
        "<select><optgroup hidden><option>x<option>skill:target-skill</select>",
        "<ruby>x<rt hidden>y<ruby>z<rt>skill:target-skill</ruby></ruby>",
        "<ruby>x<rp hidden>y<ruby>z<rp>skill:target-skill</ruby></ruby>",
        "<table><caption hidden>x<table><tr><td>skill:target-skill</table></caption></table>",
        # Codex P2 BLOCKING on #3846: a raw `<pre>` block is a code example.
        "<pre><code>skill:target-skill</code></pre>",
        "Inline <pre>skill:target-skill</pre> done.",
        # Codex P2 BLOCKING on #3846: a script body is raw text, not markup.
        '<template><script>const x="</template>";</script> skill:target-skill</template>',
        # Control: a comment still hides what it holds, a script tag included.
        "<p><!-- <script> --> x <!-- skill:target-skill --></p>",
        # Control for the entity fix below: code shows an entity literally.
        "Then `skill:target&#45;skill` in code.",
        # Codex P2 BLOCKING on #3846: an unclosed comment hides the rest of the file.
        "Intro.\n\n<!-- retired: skill:target-skill\n\nAlso skill:target-skill.",
        # Control for the formatting fix below: a block tag still parts words.
        "<p>Then run skill:target-</p><p>skill.</p>",
        "Then run skill:target-<br>skill.",
        "<ul><li>Then run skill:target-</li><li>skill.</li></ul>",
        # Codex P2 BLOCKING on #3846: a bare email address is not a handoff.
        "Questions to target-skill@example.com.",
        "<p>Mail <b>target-skill@docs.example.org</b> for access.</p>",
        # Codex P2 BLOCKING on #3846: so is one on a single-label or literal domain.
        "Questions to target-skill@localhost.",
        "Questions to target-skill@[::1].",
        # A tag inside a textarea is shown as its characters, so it joins nothing.
        "<p><textarea>skill:target-<b></b>skill</textarea></p>",
        # An xmp body is shown exactly as written, entities included.
        "<p><xmp>skill:target&#45;skill</xmp></p>",
        # Codex P2 BLOCKING on #3846: document metadata is never rendered.
        "<title>skill:target-skill</title>",
        "<head><meta charset=utf-8>\nskill:target-skill\n</head>",
        "Inline <title><b>skill:target-skill</b></title> done.",
        # Codex P2 BLOCKING on #3846: a quoted `>` in an end tag does not end it.
        '<script>x</script title="> skill:target-skill">',
        "Inline <style>p{}</style data-x='> skill:target-skill'> done.",
        # Codex P2 BLOCKING on #3846: an iframe's (or any embed's) fallback
        # content is never rendered.
        '<iframe src="preview.html">skill:target-skill</iframe>',
        "Inline <iframe>skill:target-skill</iframe> done.",
        "<video src=a.mp4><p>Then run skill:target-skill.</p></video>",
        "Inline <noembed>skill:target-skill</noembed> done.",
        # Codex P2 BLOCKING on #3846: a noscript body is not shown while
        # scripting is on.
        "<noscript>skill:target-skill</noscript>",
        "Inline <noscript><b>skill:target-skill</b></noscript> done.",
        # Control: an embed is a box, so it still parts the words around it.
        "<p>Then run skill:target-<iframe></iframe>skill.</p>",
        "Then run skill:target-<pre>x</pre>skill.",
        # Control for the remote-anchor fix above: its href is still not a reference.
        '<p><a href="https://example.com/skills/target-skill">docs</a></p>',
        '<p><a href="https://example.com/skills/target-skill">docs</p>',
        # Control for the missing-file fix below: a missing file beside this
        # skill's own SKILL.md is still this skill's.
        "Then [see](notes/missing.md).",
    ):
        _skill(repo, "docs-skill", body)
        assert measure(repo).skill_ref_depth_max == 0, body

    # Control: the same skill reached by a local link is a reference.
    _skill(repo, "docs-skill", "Hand off to the [specialist](../target-skill/SKILL.md).")
    assert measure(repo).skill_ref_depth_max == 1
    # Control: a local path, even one with a doubled slash, is not a URL, and
    # neither is a `skill:` invocation or a label followed by a space.
    for body in (
        "Then see ../target-skill/SKILL.md.",
        "Then see (private path).",
        "Then see .agents//skills/target-skill.",
        "Then run skill:target-skill.",
        # Codex P2 BLOCKING on #3846: a call written after an anchored path in
        # one span is still a handoff.
        "Run `../runner --delegate skill:target-skill`.",
        'Run "./run.sh --then /target-skill".',
        "Run `../tools/go $target-skill`.",
        # Codex P2 BLOCKING on #3846: so is any handoff after it, read as prose.
        "Run `../runner --delegate [[target-skill]]`.",
        "Run `../runner --then target-skill`.",
        # Codex P2 BLOCKING on #3846: so is a path argument that resolves to it.
        "Run `../runner ../target-skill/SKILL.md`.",
        "Run `../runner --into (private path)`.",
        "Note: see skills/target-skill.",
        "Then see (private path):42.",
        # Sol P1 BLOCKING on #3846: a missing file in another skill is still a handoff.
        "Then [see]((private path)).",
        '<p>Then <a href="(private path)">see</a>.</p>',
        "Then see v1.20/skills/target-skill.",
        "Then see 1.2.3/skills/target-skill.",
        # Control for the SCP fix: a colon that ends a sentence is not a path.
        "Ping 10.0.0.2: then run skill:target-skill.",
        "Ping [::1]: then see skills/target-skill.",
        "Step 2:skills/target-skill next.",
        "See 1.2.3:skills/target-skill next.",
        # Control for the email fix: a nearby address does not swallow a handoff.
        "Then run skill:target-skill, or mail ops@example.com.",
        "Then run target-skill @ the end.",
        # Control for the home-path fix: a `$name` invocation is not a variable.
        "Then run $target-skill.",
        # Codex P2 BLOCKING on #3846: a `skill:` link destination is an invocation.
        "Then [run it](skill:target-skill).",
        '<p>Then <a href="skill:target-skill">run it</a>.</p>',
        "Then [run it][r].\n\n[r]: skill:target-skill",
        "Then see ../target-skill, or ~/notes.",
        # Control for the code-span path fix: an unanchored span is still read.
        "Then run `skill:target-skill`.",
        "Then see `skills/target-skill`.",
        "See `\\\\server\\share\\notes` then run skill:target-skill.",
        "Then run skill:target-skill, or mail ops@localhost.",
        "Mail ops@localhost: then run skill:target-skill.",
        "Mail ops@example.com,skill:target-skill handles it.",
        'Run "skill:target-skill" next, then "../target-skill/SKILL.md".',
        'See "skills/target-skill" for it.',
        "Then see `skills/target-skill/references/guide.md`.",
        # An apostrophe quotes nothing: `s' ... team's` is not a quoted span.
        "The agents' ../notes/x.md then skill:target-skill is the team's step.",
        "Read guide.md#usage, then skill:target-skill.",
        # Control for the drive-path fix: a one-letter label is not a drive.
        "A: see skills/target-skill.",
        "Step B:skills/target-skill next.",
        # Control for the IPv6 fix: a wiki link is not an address.
        "Then [[target-skill]].",
        "<!-- note -->\n\nThen run skill:target-skill.",
        "<!-- note --> Then run skill:target-skill.",
        # Codex P2 BLOCKING on #3846: inline formatting does not split a word.
        "Then run skill:target-<em>skill</em>.",
        "<p>Then run skill:target-<em>skill</em>.</p>",
        # Codex P2 BLOCKING on #3846: a zero-width break and an element no list
        # names render inline too.
        "Then run skill:target-<wbr>skill.",
        "<p>Then run skill:target-<x-note>skill</x-note>.</p>",
        # Codex P2 BLOCKING on #3846: a comment has no width, so it does not
        # part the words around it.
        "<p>skill:target-<!-- note -->skill</p>",
        "Then run skill:target-<!-- note -->skill.",
        "<p>Then run skill:target-<script>x</script>skill.</p>",
        # Control: text after a closed embed is shown again.
        "<p><iframe>x</iframe> then skill:target-skill.</p>",
        "<noscript>x</noscript> then skill:target-skill.",
        # Codex P2 BLOCKING on #3846: a textarea body is text, so a tag in it
        # opens nothing, and the text after it is shown.
        "<p><textarea><script></textarea> then skill:target-skill</p>",
        "Inline <textarea><script></textarea> then skill:target-skill.",
        "<textarea><!--</textarea> then skill:target-skill",
        # Codex P2 BLOCKING on #3846: so is an xmp or plaintext body.
        "<p><xmp><script></xmp> then skill:target-skill</p>",
        "<xmp>skill:target-skill</xmp>",
        # Nothing ends a plaintext body, its own end tag included.
        "<plaintext></plaintext><script> skill:target-skill",
        # Control: a textarea's own text is shown, entities decoded.
        "<p><textarea>skill:target&#45;skill</textarea></p>",
        # Codex P2 BLOCKING on #3846: an escaped or entity-encoded comment or
        # tag is shown as text, so what it holds is read.
        "\\<!-- run skill:target-skill -->",
        "&lt;!-- run skill:target-skill --&gt;",
        'See \\<span title="skill:target-skill"> here.',
        "Then `<!-- skill:target-skill -->` in code.",
        # Control: a custom element whose name starts like a hidden one is shown.
        "<p>Then run skill:<pre-note>target-skill</pre-note>.</p>",
        # Codex P2 BLOCKING on #3846: a remote block anchor's words are shown prose.
        '<p><a href="https://example.com">run skill:target-skill</a></p>',
        "<p><a href='https://example.com/x'>see</a> then <a href=\"https://e.com\">skill:target-skill</a></p>",
        # Codex P2 BLOCKING on #3846: a block anchor left open, or closed in a
        # later block, is still a link.
        '<p>Hand off to the <a href="../target-skill/SKILL.md">specialist</p>',
        '<div><a href="../target-skill/SKILL.md">\n\nspecialist</a> here.</div>',
        '<div><a href="https://example.com">\n\nrun skill:target-skill</a></div>',
    ):
        _skill(repo, "docs-skill", body)
        assert measure(repo).skill_ref_depth_max == 1, body
    # Control: the visible text of raw HTML and a one-segment /name invocation
    # are still read.
    for body in (
        "<details>Then run /target-skill.</details>",
        "<b>Then [[target-skill]].</b>",
        # Codex P2 BLOCKING on #3846: a raw HTML anchor's href is a link.
        'Hand off to the <a href="../target-skill/SKILL.md">specialist</a>.',
        "<p>Hand off to the <a class=x href='../target-skill/SKILL.md'>specialist</a>.</p>",
        # Codex P2 BLOCKING on #3846: an href's HTML entities are decoded.
        'Hand off to the <a href="../target&#45;skill/SKILL.md">specialist</a>.',
        '<p>Hand off to the <a href="../target&#x2d;skill/SKILL.md">specialist</a>.</p>',
        # Codex P2 BLOCKING on #3846: shown HTML text is entity-decoded too.
        '<p>See <a href="notes.md">then skill:target&#45;skill</a>.</p>',
        "<p>Then skill:target&#45;skill.</p>",
        # Control: text after a closed title or head is shown again.
        "<title>Notes</title>\n\nThen skill:target-skill.",
        "Inline <head></head> then skill:target-skill.",
        "<header>Then skill:target-skill.</header>",
        # Control: text after a hidden element's end tag is shown again, and
        # `hidden` only hides as an attribute of its own.
        "<div hidden>x</div>\n\nThen skill:target-skill.",
        "Inline <span hidden><span>x</span></span> then skill:target-skill.",
        '<p><span data-hidden="1">skill:target-skill</span></p>',
        '<p><span title="hidden">skill:target-skill</span></p>',
        "<p><input hidden> then skill:target-skill.</p>",
        # Sol P1 BLOCKING on #3846: a start tag ends an open hidden element it
        # cannot sit inside, as HTML's optional end tags do.
        "<ul><li hidden>old<li>skill:target-skill</ul>",
        "<dl><dt hidden>old<dd>Then skill:target-skill.</dl>",
        "<p hidden>old<p>Then skill:target-skill.",
        "<p hidden>old<div>Then skill:target-skill.</div>",
        # Sol P1 BLOCKING on #3846: the same holds for options, table parts and ruby text.
        "<select><option hidden>old<option>skill:target-skill</select>",
        "<select><optgroup hidden><option>old<optgroup><option>skill:target-skill</select>",
        "<table><tr><td hidden>old<td>skill:target-skill</table>",
        "<table><tr><th hidden>old<th>skill:target-skill</table>",
        "<table><tr hidden><td>old<tr><td>skill:target-skill</table>",
        "<table><thead hidden><tr><td>old<tbody><tr><td>skill:target-skill</table>",
        "<ruby>x<rt hidden>old<rt>skill:target-skill</ruby>",
        "<ruby>x<rp hidden>old<rp>skill:target-skill</ruby>",
        # Sol P1 BLOCKING on #3846: a hidden caption or column group ends at the next table part.
        "<table><caption hidden>old<tbody><tr><td>skill:target-skill</table>",
        "<table><caption hidden>old<tr><td>skill:target-skill</table>",
        "<table><caption hidden>old<td>skill:target-skill</table>",
        "<table><caption hidden>old<colgroup><col><tr><td>skill:target-skill</table>",
        "<table><colgroup hidden><col><tbody><tr><td>skill:target-skill</table>",
        "<table><tbody hidden><tr><td>old<caption>skill:target-skill</table>",
        # Control: text after a closed script is shown again.
        "<script>x()</script>\n\nThen skill:target-skill.",
        "Inline <script>x()</script> then skill:target-skill.",
        "Inline <template><script>x</script></template> then skill:target-skill.",
        'Inline <template>x</template> then <a href="../target-skill/SKILL.md">go</a>.',
        "<p><pre>x</pre> then skill:target-skill.</p>",
        '<script>const x="<template>";</script>\n\nThen skill:target-skill.',
        '<p>A <style>p::after{content:"<template>"}</style> then skill:target-skill.</p>',
        # Codex P2 BLOCKING on #3846: a script ends at its end tag even inside
        # comment-looking text.
        "<script><!-- </script> --> Then skill:target-skill",
        # Control: text after such an end tag is shown again.
        '<script>x</script title="a > b">\n\nThen skill:target-skill.',
        "Inline <script><!-- </script> --> then skill:target-skill.",
        # Codex P2 BLOCKING on #3846: a `>` in an earlier quoted attribute
        # does not end the anchor's tag.
        '<p>Hand off to the <a title="a > b" href="../target-skill/SKILL.md">specialist</a>.</p>',
        'Hand off to the <a title="a > b" href="../target-skill/SKILL.md">specialist</a>.',
    ):
        _skill(repo, "docs-skill", body)
        assert measure(repo).skill_ref_depth_max == 1, body
