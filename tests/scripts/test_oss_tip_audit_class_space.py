"""oss_tip_audit: every spelling of an identity string the gate claims to cover.

Split out of test_oss_tip_audit.py, which crossed the 600-line file limit. The
separation is real and not just arithmetic: the sibling module tests one rule at
a time against a case that motivated it, and this one asks the whole class
question at once.

Regression lines:
- if any enumerated spelling stops being detected then broken
- if the Linux home rule starts folding case then broken (see the test for why)
- if an exemption stops where its reason stops, in EITHER direction, then broken
- if a URL authority before a home path stops exempting a URL route then broken
- if the trailing-delimiter strip stops covering a JSON comma, a plist angle
  bracket or a semicolon then broken
- if the trailing-delimiter strip starts exempting a real login then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.oss_tip_audit import audit_paths


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# Values are assembled from fragments because the gate scans its own test suite:
# a literal here would make the tracked-tree assertion in the sibling module
# unsatisfiable.
# Every spelling of an identity string this gate claims to cover, enumerated.
# Seven of the twenty review findings on #1440 were the same shape -- a rule
# narrower than the class it names -- each found one at a time by a reviewer.
# A table is the cheap way to ask the whole question at once, and it found one
# more (an unfolded Linux rule) before a reviewer did. Values are assembled from
# fragments because this file is scanned by the gate it tests.
_L = "jdoe"
_USERS = "/" + "Users" + "/"
_HOME = "/" + "home" + "/"
_CLASS_SPACE: tuple[tuple[str, str], ...] = (
    ("mac lower", "/" + "users" + "/" + _L),
    ("mac upper", "/" + "USERS" + "/" + _L),
    ("mac mixed", "/" + "Users" + "/" + _L),
    ("mac double slash", "/" + "/" + "Users" + "/" + _L),
    ("win drive backslash", "C:" + "\\" + "Users" + "\\" + _L),
    ("win drive slash", "C:" + "/" + "Users" + "/" + _L),
    ("win lowercase drive", "c:" + "\\" + "users" + "\\" + _L),
    ("win drive-less", "\\" + "Users" + "\\" + _L),
    ("win UNC", "\\" + "\\" + "server" + "\\" + "Users" + "\\" + _L),
    ("linux", "/" + "home" + "/" + _L),
    ("mail bare", "someone" + "@" + "real-host" + ".com"),
    ("mail after a path slash", "docs/" + "someone" + "@" + "real-host" + ".com"),
    ("mail after a URL authority", "https://" + "someone" + "@" + "real-host" + ".com"),
    ("mail after a backslash", "C:" + "\\" + "x" + "\\" + "someone" + "@" + "real-host" + ".com"),
    ("mail quoted", '"' + "someone" + "@" + "real-host" + ".com" + '"'),
    ("mail parenthesized", "(" + "someone" + "@" + "real-host" + ".com" + ")"),
    ("mail angle-bracketed", "<" + "someone" + "@" + "real-host" + ".com" + ">"),
    ("mail after mailto:", "mailto:" + "someone" + "@" + "real-host" + ".com"),
    ("mail plus-addressed", "someone" + "+tag" + "@" + "real-host" + ".com"),
    ("mail dotted local part", "first.last" + "@" + "real-host" + ".com"),
    ("mail uppercase", "SOMEONE" + "@" + "REAL-HOST" + ".COM"),
    ("mail subdomain", "someone" + "@" + "mail.real-host" + ".co.uk"),
    ("mail numeric domain", "someone" + "@" + "123" + ".com"),
    ("mail IDN", "person" + "@" + "b\u00fccher" + ".de"),
    ("mail non-latin script", "person" + "@" + "\u4f8b\u3048" + "." + "\u65e5\u672c"),
    ("tailnet", "box." + "tail1234" + ".ts.net"),
    ("tailnet uppercase", "BOX." + "TAIL1234" + ".TS.NET"),
    ("tailnet in a URL", "https://" + "box." + "tail1234" + ".ts.net" + "/x"),
    ("cgnat low", "100." + "64.0.1"),
    ("cgnat mid", "100." + "96.3.9"),
    ("cgnat high", "100." + "127.255.254"),
)


@pytest.mark.parametrize("label,probe", _CLASS_SPACE, ids=[c[0] for c in _CLASS_SPACE])
def test_every_spelling_in_the_class_space_is_detected(
    tmp_path: Path, label: str, probe: str
) -> None:
    """One spelling per case, so a regression names the spelling that broke."""
    path = _write(tmp_path, "probe.md", probe + "\n")
    result = audit_paths(tmp_path, [path])
    assert result.findings, f"{label}: {probe!r} is not detected by any rule"


def test_the_linux_rule_is_case_sensitive_on_purpose(tmp_path: Path) -> None:
    """The one asymmetry in the home rules, pinned so it is not "fixed".

    macOS and Windows fold case because their filesystems do. Linux paths are
    case-sensitive, so an uppercase spelling names a different directory, and
    folding it made `/Home/End` -- the keyboard keys, in a UI document -- a
    finding. Nobody writes a Linux home in caps, so the fold bought nothing and
    cost noise. This test is the record of that measurement.
    """
    keyboard_keys = "/" + "Home" + "/" + "End"
    real_linux_home = "/" + "home" + "/" + _L
    path = _write(tmp_path, "keys.md", f"{keyboard_keys}\n{real_linux_home}\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == [real_linux_home], [
        f.render() for f in result.findings
    ]


# An exemption is a hole with a reason. These two arrived as exemptions that
# widened past their reason and let a real identity through the publication
# gate, so each case below names both directions: what must still be exempt,
# and the neighbouring spelling that must NOT be.
_EXEMPTION_EDGES = [
    # (label, probe, must_be_reported)
    #
    # A TRAILING `=` is assignment syntax the capture ran into. An `=` in the
    # MIDDLE is part of a Windows profile name, and judging only the prefix
    # exempted the whole of `the maintainer=DJ` on the strength of `the maintainer`
    # (Codex P1/BLOCKING, #1440, discussion_r3970584434).
    ("mapping syntax, trailing equals", "--map /" + "Users" + "/foo=/data/x", False),
    ("equals INSIDE a profile name", "C:\\" + "Users" + "\\the maintainer=DJ\\Music", True),
    # `$NAME` is a template only when NAME is an interpolation this tree
    # performs. All-caps is a SHAPE, and a shape cannot separate `$HOME` from a
    # profile literally named `$JANE`
    # (Codex P1/BLOCKING, #1440, discussion_r3972967004).
    ("an env var this tree interpolates", "/" + "Users" + "/$HOME/x", False),
    ("a braced interpolation", "/" + "Users" + "/${ANYTHING}/x", False),
    ("an unlisted all-caps segment", "/" + "Users" + "/$JANE/x", True),
    ("an unlisted all-caps profile", "C:\\" + "Users" + "\\$JANE\\Music", True),
    ("an unlisted all-caps local part", "$JANE@private-" + "domain.com", True),
    # #1808 added one more exemption and widened an existing one. Each gets the
    # same two-directional treatment as the pair above, because the exempt side
    # alone is the direction that cannot fail: an exemption that has swallowed
    # the whole class passes it.
    #
    # A URL AUTHORITY running straight into the match makes the segment a URL
    # ROUTE rather than a home. The home rule is case-insensitive precisely so a
    # lowercase spelling of a real home is caught, and that fold is what made
    # the GitHub REST API's own `/users/<login>` shape read as a home directory
    # (#1808: 130 matches in one recorded check-runs fixture, 3582 on the tree).
    ("a url route, not a home", "https://api.github.com" + "/" + "users" + "/" + _L, False),
    # The overshoot control, and the reason the exemption is ANCHORED at the
    # end: an authority that does not run into the match is just a URL, so the
    # segment after it is an ordinary path and must still be reported.
    ("a url path with a segment before the home", "https://example.com/x" + _HOME + _L, True),
    # The trailing-delimiter strip covers the punctuation a capture runs into at
    # a boundary. A JSON list ends a segment at the comma, a plist element at
    # the angle bracket, and a shell or SQL fragment at the semicolon.
    ("a json list, trailing comma", "[" + '"' + _USERS + "dev" + '",', False),
    ("a plist element, trailing angle bracket", "<string>" + _USERS + "dev" + "</string>", False),
    ("a semicolon-terminated segment", "ran on " + _USERS + "dev" + "; then rebuilt", False),
    # The direction that makes it a STRIP rather than a shape rule: the check
    # after the strip is still the exact-match list, so a real login carrying
    # the same punctuation is reported rather than exempted.
    ("a real login, trailing comma", "ran on " + _USERS + _L + ", then rebuilt", True),
    ("a real login, trailing angle bracket", "<string>" + _USERS + _L + "</string>", True),
]


@pytest.mark.parametrize(
    ("label", "probe", "must_be_reported"),
    [(label, probe, reported) for label, probe, reported in _EXEMPTION_EDGES],
    ids=[label for label, _, _ in _EXEMPTION_EDGES],
)
def test_each_exemption_stops_exactly_where_its_reason_stops(
    tmp_path: Path, label: str, probe: str, must_be_reported: bool
) -> None:
    """Both directions per exemption, because only one of them is a hole.

    A test that only checks the exempt side passes for an exemption that has
    swallowed the whole class, which is how both of these shipped.
    """
    path = _write(tmp_path, "probe.md", probe + "\n")
    result = audit_paths(tmp_path, [path])
    if must_be_reported:
        assert result.findings, f"{label}: {probe!r} is exempted but names an identity"
    else:
        assert not result.findings, (
            f"{label}: {probe!r} must stay exempt; got {[f.render() for f in result.findings]}"
        )
