"""oss_tip_audit: the mailbox rule, which took the most rounds to get right.

Split out of test_oss_tip_audit.py at the 600-line file limit. The seam is the
one the module itself has: `scripts/oss_tip_rules.py` holds the rules, and the
mailbox rule alone accounts for a third of the review findings on #1440 -- a
provider list, reserved domains, path and URL contexts, quoted local parts,
numeric domains, IDNs, atext punctuation, and delimiters absorbed into a token.

Regression lines:
- if the mailbox rule only fires for an enumerated list of providers then broken
- if a reserved-domain fixture or a version spec is reported then broken
- if a retina asset name (a pixel multiplier and an image extension for a
  domain) is reported, or one at a DELEGATED suffix is not, then broken
- if a systemd template instance (`name@instance.service`) is reported, or one
  at the DELEGATED `.target` suffix is not, then broken
- if a path-exempt mailbox is dropped instead of counted as exempt then broken
- if a mailbox after a slash, in a URL, quoted, at a numeric domain, at a Unicode
  domain, or ending in atext punctuation slips through then broken
- if a delimiter (quote, =, backtick, tab) is absorbed into a token then broken
- if a path-exempt mailbox stops being raised for ANY entry in the exempt set,
  rather than only the one that sorts first, then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.oss_tip_audit import MAILBOX_EXEMPT_PATHS, audit_paths

# Assembled from fragments: the gate scans its own test suite, so a literal here
# would make the clean-tree assertion in the sibling module unsatisfiable.
_USERS = "/" + "Users" + "/"
_LOGIN = "jdoe"
_REAL_HOME = _USERS + _LOGIN + "/Music/x.mp3"
_MAILBOX = "someone" + "@" + "not-a-real-provider" + ".com"
_CGNAT = "100." + "64.3.9"
_URL_MAILBOX = "https://" + _MAILBOX + "/path"
_PATH_MAILBOX = "docs/" + _MAILBOX


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_mailbox_rule_is_not_a_provider_list(tmp_path: Path) -> None:
    """Devin P1: an enumerated provider list is a value that rots, so a mailbox
    at any registrable domain must fire, in any case."""
    addresses = [
        "someone" + "@" + "proton" + ".me",
        "someone" + "@" + "a-regional-host" + ".co.uk",
        "Someone.Else" + "@" + "MixedCase-Host" + ".COM",
        "someone" + "@" + "a.deeply.nested.host" + ".org.au",
        "someone" + "@" + "gmail" + ".com",
    ]
    path = _write(tmp_path, "mail.md", "\n".join(addresses) + "\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == addresses, [f.render() for f in result.findings]


def test_reserved_domains_and_non_addresses_are_not_mailboxes(tmp_path: Path) -> None:
    """The generic rule must not turn every `@` in the tree into a finding: a
    reserved-domain fixture receives no mail, and a version or file spec is not
    an address at all."""
    path = _write(
        tmp_path,
        "fixtures.md",
        "a " + "user" + "@" + "example.org" + "\n"
        "b " + "user" + "@" + "example.com" + "\n"
        "c " + "user" + "@" + "host.invalid" + "\n"
        "d " + "user" + "@" + "host.test" + "\n"
        "e " + "agent" + "@" + "mux.local" + "\n"
        "f " + "git" + "@" + "github.com" + "\n"
        "g signalsmith-stretch" + "@" + "1.3.2.patch" + "\n"
        "h icons/128x128" + "@" + "2x.png" + "\n",
    )
    result = audit_paths(tmp_path, [path])
    assert result.findings == (), [f.render() for f in result.findings]


@pytest.mark.parametrize("exempt_name", sorted(MAILBOX_EXEMPT_PATHS))
def test_exempt_path_mailboxes_are_counted_not_hidden(tmp_path: Path, exempt_name: str) -> None:
    """A path exemption that dropped its matches could not be checked by the
    person running the gate; it reports them under `exempt` instead.

    One case per entry in the set, not the first one: #1808 grew the set from
    one file to two, and a test that reads `sorted(...)[0]` covers whichever
    path sorts first, so the next entry would arrive untested and the coverage
    would look unchanged.
    """
    path = _write(tmp_path, exempt_name, f"{_MAILBOX}\n{_CGNAT}\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.exempt] == [_MAILBOX]
    # The exemption is mailbox-only: every other rule still fails the gate there.
    assert [f.match for f in result.findings] == [_CGNAT]
    assert "exempt=1" in result.summary()

    # Negative control: the SAME address in any other file is a finding, so the
    # exemption is the path and not the address.
    elsewhere = _write(tmp_path, "notes.md", f"{_MAILBOX}\n")
    other = audit_paths(tmp_path, [elsewhere])
    assert other.exempt == ()
    assert [f.match for f in other.findings] == [_MAILBOX]


def test_a_mailbox_after_a_slash_is_still_a_mailbox(tmp_path: Path) -> None:
    """Codex P1, twice. The cost lookbehind must not double as a filter, and
    the first fix for that overshot: it suppressed EVERY single-slash context,
    which is a real address in `docs/<name>@<domain>` as often as it is an
    asset name. Path placement proves nothing either way, so the asset forms
    are excluded on the domain they carry, and everything else fires.
    """
    path = _write(
        tmp_path,
        "urls.md",
        f"a {_URL_MAILBOX}\n"
        f"b {_PATH_MAILBOX}\n"
        "c icons/128x128" + "@" + "2x.png" + "\n"
        "d node_modules/.bin/pkg" + "@" + "1.2.3" + ".sh" + "\n",
    )
    result = audit_paths(tmp_path, [path])
    # Line d FIRES, and that is the intended trade. `.sh` is a real ccTLD, so no
    # shape separates a version spec from a domain; the enumerated exception list
    # holds the real one and everything else is a reviewed line (Codex P1,
    # #1440). Line c does not fire because its domain is a pixel multiplier at an
    # undelegated image extension, which no mailbox can sit behind.
    assert [(f.line, f.match) for f in result.findings] == [
        (1, _MAILBOX),
        (2, _MAILBOX),
        (4, "pkg" + "@" + "1.2.3" + ".sh"),
    ], [f.render() for f in result.findings]


def test_only_the_reserved_example_domains_are_reserved(tmp_path: Path) -> None:
    """Codex P1: RFC 2606 reserves example.com/net/org and the `.example`
    suffix. A registrable domain that merely CARRIES the label -- and can hold
    a real mailbox -- is not reserved, and treating it as such exempted it."""
    real = "person" + "@" + "sub.example" + ".fr"
    also_real = "person" + "@" + "example-host" + ".co.uk"
    fixtures = [
        "user" + "@" + "example.com",
        "user" + "@" + "mail.example.org",
        "user" + "@" + "anything" + ".example",
        # An ssh destination on an ALLOWED tailnet, not a mailbox.
        "user" + "@" + "agentbox." + "example" + ".ts.net",
    ]
    path = _write(tmp_path, "domains.md", "\n".join([real, also_real, *fixtures]) + "\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == [real, also_real], [
        f.render() for f in result.findings
    ]


def test_a_mailbox_at_a_unicode_domain_is_still_a_mailbox(tmp_path: Path) -> None:
    """Codex P1: an internationalized domain written in its human-facing form
    is a valid mailbox domain, and an ASCII-only character class failed at the
    domain's first character, so the gate certified it clean.

    The controls are the ASCII behaviour that must survive the widening: a
    reserved fixture domain is still exempt, and a version spec is still not an
    address. A Unicode class that swallowed those would trade one blind spot
    for a noisy gate.
    """
    idn = "person" + "@" + "b\u00fccher" + ".de"
    other_script = "person" + "@" + "\u4f8b\u3048" + "." + "\u65e5\u672c"
    ascii_real = "someone" + "@" + "real-host" + ".co.uk"
    reserved = "user" + "@" + "example" + ".com"
    version = "signalsmith-stretch" + "@" + "1.3.2" + ".patch"
    path = _write(
        tmp_path,
        "idn.md",
        "\n".join([idn, other_script, ascii_real, reserved, version]) + "\n",
    )
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == [idn, other_script, ascii_real], [
        f.render() for f in result.findings
    ]


def test_a_quoted_mailbox_local_part_is_still_a_mailbox(tmp_path: Path) -> None:
    """Codex P1: a quoted local part is a valid mailbox and names the person
    just as plainly, while an unquoted-only class returned nothing."""
    quoted = '"' + "john.doe" + '"' + "@" + "private-domain" + ".com"
    path = _write(tmp_path, "q.md", quoted + "\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == [quoted], [f.render() for f in result.findings]


def test_a_numeric_domain_is_not_assumed_to_be_a_version(tmp_path: Path) -> None:
    """Codex P1, twice, in both directions. "any numeric label" exempted
    `mail.123reg.co.uk`; "every label before the suffix is numeric" still
    exempted `123.com`. Both are registrable and can carry a real mailbox.

    There is no shape that separates a version spec from a numeric domain,
    because the difference is intent, not form. So the version specs are
    enumerated (the control on the last line) and everything else fires.
    """
    numeric_label = "person" + "@" + "mail.123reg" + ".co.uk"
    all_numeric = "person" + "@" + "123" + ".com"
    enumerated = "signalsmith-stretch" + "@" + "1.3.2" + ".patch"
    path = _write(tmp_path, "domains.md", f"{numeric_label}\n{all_numeric}\n{enumerated}\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == [numeric_label, all_numeric], [
        f.render() for f in result.findings
    ]


def test_a_local_part_of_atext_punctuation_is_still_a_mailbox(tmp_path: Path) -> None:
    """Codex P1: the unquoted branch was a convenience subset of RFC 5322
    `atext`, so an address whose local part ends in legal punctuation could not
    match at all -- the character immediately before the `@` had to be in the
    class, and there was no suffix to restart from."""
    bang = "user" + "!" + "@" + "private-domain" + ".com"
    tilde = "user" + "~" + "@" + "private-domain" + ".com"
    path = _write(tmp_path, "atext.md", f"{bang}\n{tilde}\n")
    result = audit_paths(tmp_path, [path])
    assert [f.match for f in result.findings] == [bang, tilde], [
        f.render() for f in result.findings
    ]


def test_delimiters_are_not_absorbed_into_a_token(tmp_path: Path) -> None:
    """The measured cost of widening to atext, pinned so it cannot come back.

    Every character here is legal in a local part AND a delimiter in the text
    this gate reads, so including it absorbed the delimiter and the exact-match
    allowlist stopped matching. The backtick alone produced 405 findings on the
    tracked tree, because markdown puts a doubled backtick directly before
    decorator text. A gate that flags 405 non-addresses is one nobody runs.
    """
    quoted_constant = "'" + "git" + "@" + "gitlab" + ".com" + "'"
    config_line = "user.email=" + "ci-eval" + "@" + "users.noreply.github" + ".com"
    markdown_decorator = "``" + "@" + "pytest.mark" + ".requirement"
    interpolated = "$" + "{TOKEN}" + "@" + "github" + ".com"
    tsv_row = "C:" + "\\" + "Users" + "\\" + "uwcyb" + "\t" + "C--Users-uwcyb"
    path = _write(
        tmp_path,
        "delims.md",
        "\n".join([quoted_constant, config_line, markdown_decorator, interpolated, tsv_row]) + "\n",
    )
    result = audit_paths(tmp_path, [path])
    assert result.findings == (), [f.render() for f in result.findings]


def test_a_dns_label_may_not_hold_an_underscore(tmp_path: Path) -> None:
    """Codex P2, #1440, discussion_r3972967015.

    An underscore is legal in the LOCAL part and illegal in a hostname label, so
    an address-shaped token like a package coordinate at `foo_bar.com` names a
    domain that cannot receive mail. The label boundaries already said
    `[^\\W_]`; only the interior said `[\\w-]`, which admits `_`, so the token
    matched and a mandatory gate blocked unrelated work over an identifier.

    Both directions, because narrowing a class is exactly the change that
    silently stops matching real addresses: the hyphenated, multi-label,
    Unicode and short-domain forms must all still be reported.
    """
    # Split BEFORE the `@`, never inside the domain. A fragment that ends part
    # way through a domain is still a complete address at a shorter one, so the
    # earlier split made the source line a finding about this repository -- and
    # so did the first version of THIS comment, which quoted the offending
    # fragment to explain the point. No fragment below can match on its own,
    # and none is written out.
    who = "person" + "@"
    not_a_mailbox = "package" + "@" + "foo_bar.com"
    real = [
        who + "foo-bar.com",
        who + "sub.foo-bar.co.uk",
        who + "x.com",
        who + "münchen.de",
    ]
    path = _write(tmp_path, "probe.md", "\n".join([not_a_mailbox, *real]) + "\n")

    matches = [f.match for f in audit_paths(tmp_path, [path]).findings]

    assert not_a_mailbox not in matches, f"an underscored host label was reported: {matches}"
    for address in real:
        assert address in matches, f"{address} stopped being reported; the class is too narrow"


def test_a_local_part_must_be_dot_separated_atoms(tmp_path: Path) -> None:
    """Codex P2, #1440, discussion_r3975241649.

    An unquoted local part is a DOT-ATOM: one or more non-empty atoms joined by
    single dots. It can neither begin nor end with a dot nor carry two in a
    row. The class allowed a dot at any position, so three shapes that cannot
    be addresses were reported as mailboxes, and on a mandatory publication
    gate a false positive blocks unrelated work exactly as a real leak does.

    Both directions again. Narrowing a class is the change that silently stops
    matching real addresses, so the multi-atom, single-atom and
    punctuation-carrying forms must all still be reported.
    """
    # Assembled the same way as above: split BEFORE the `@` so no fragment on
    # this line is a complete address at a shorter domain.
    at_host = "@" + "example-host.com"
    not_addresses = [
        "." + "leading" + at_host,
        "trailing" + "." + at_host,
        "double" + ".." + "dot" + at_host,
    ]
    real = [
        "one" + at_host,
        "two" + "." + "atoms" + at_host,
        "three" + "." + "small" + "." + "atoms" + at_host,
        "has" + "+" + "plus" + at_host,
    ]
    # A leading space on every line, so the lookbehind cannot be what rejects
    # the leading-dot case: without it that line begins the token at `leading`
    # with a `.` before it and is refused for the wrong reason.
    body = "\n".join(f" {line}" for line in [*not_addresses, *real]) + "\n"
    path = _write(tmp_path, "atoms.md", body)

    matches = [f.match for f in audit_paths(tmp_path, [path]).findings]

    for shape in not_addresses:
        assert shape not in matches, f"a non dot-atom local part was reported: {shape}"
    for address in real:
        assert address in matches, f"{address} stopped being reported; the class is too narrow"


def test_retina_asset_names_are_not_mailboxes(tmp_path: Path) -> None:
    """#1808, revisited. scripts/desktop_icons.py names its `@2x` iconset slots
    as quoted Python string literals, and a quote is not a word boundary, so
    `icon_` joins the local part and every slot parses as a mailbox. #2528 fixed
    that by enumerating the six filenames then in the tree, which is a VALUE:
    the build went red again the moment an asset was added, and this is the
    class fix that replaces it.

    THIS REVERSES the direction #1808 took, which was to enumerate and never
    match the shape. The shape is safe here for a reason that ruling did not
    weigh: the domain's last label is an image extension, none of the enumerated
    extensions is a delegated top-level domain, and a mailbox cannot exist at an
    undelegated one -- so no identity can hide in the exempted set.

    The guard that remains is the one that bites: a multiplier-shaped label at a
    DELEGATED suffix is a registrable domain that can carry real mail and must
    still fire. `.zip` and `.photos` are both delegated and both read like asset
    suffixes, which is exactly why they are the controls.
    """
    iconset_lines = "\n".join(
        f'    "icon_{size}@2x.png",' for size in ("16x16", "32x32", "128x128", "256x256", "512x512")
    )
    accepted = [
        "icon_64x64" + "@" + "2x.png",  # not an Apple slot; still not a mailbox
        "logo" + "@" + "3x.webp",  # a web asset pipeline, not an iconset
        "SPLASH" + "@" + "2X.PNG",  # the extension is matched case-insensitively
        "sheet" + "@" + "4x.svg",
    ]
    fires = [
        _MAILBOX,
        "someone" + "@" + "2x.com",  # a multiplier label at a registrable domain
        "gallery" + "@" + "2x.photos",  # .photos IS delegated and carries mail
        "bundle" + "@" + "2x.zip",  # .zip IS delegated; it only LOOKS like a file
        "name" + "@" + "x.png",  # no multiplier, so not the asset shape
    ]
    path = _write(
        tmp_path,
        "desktop_icons_fixture.py",
        "expected = {\n"
        + f"{iconset_lines}\n"
        + "}\n"
        + "".join(f"# {x}\n" for x in accepted + fires),
    )

    result = audit_paths(tmp_path, [path])
    matches = [f.match for f in result.findings]

    for size in ("16x16", "32x32", "128x128", "256x256", "512x512"):
        literal = f"icon_{size}@2x.png"
        assert literal not in matches, f"{literal} is an Apple iconset slot name, not a mailbox"
    for name in accepted:
        assert name not in matches, f"{name} is an asset filename, not a mailbox"
    for address in fires:
        assert address in matches, (
            f"{address} stopped firing -- the rule was loosened past the point "
            "where a real mailbox can still exist"
        )


def test_systemd_template_unit_instances_are_not_mailboxes(tmp_path: Path) -> None:
    """#2542's fleet process registry records every nucbox lane as
    `idd-lane@<n>.service`, and the mailbox rule read the four of them as
    addresses at the domain `<n>.service`, which turned the tracked-tree audit
    red on every PR (#3256 was the first to hit it). Same class fix as the
    retina assets: the shape is safe because the trailing label is a systemd
    unit type, none of the exempted types is a delegated top-level domain, and
    a mailbox cannot exist at an undelegated one.

    The control is `.target`, the one unit type that IS delegated: an instance
    at that suffix is a registrable domain that can carry mail, so it fires and
    stays a reviewed line, exactly as `.zip` does for the asset shape.
    """
    accepted = [
        "idd-lane" + "@" + "5.service",  # the registry's own line
        "systemd --user timer -> idd-lane" + "@" + "1.service",  # as the registry renders it
        "getty" + "@" + "tty1.service",
        "opendj-idd-lane" + "@" + "3.timer",
        "backup" + "@" + "home-maintainer.mount",
        "wrapper" + "@" + "nested.instance.socket",  # an instance may carry dots
        "ALERT" + "@" + "1.SERVICE",  # the type is matched case-insensitively
    ]
    fires = [
        _MAILBOX,
        "someone" + "@" + "5.target",  # .target IS delegated and carries mail
        "someone" + "@" + "5.services",  # .services IS delegated; not a unit type
        "someone" + "@" + "service.com",  # the type as a label, not the suffix
    ]
    path = _write(tmp_path, "process-registry.json", "".join(f"{x}\n" for x in accepted + fires))

    matches = [f.match for f in audit_paths(tmp_path, [path]).findings]

    for unit in accepted:
        assert unit.split(" ")[-1] not in matches, f"{unit} is a systemd unit name, not a mailbox"
    for address in fires:
        assert address in matches, (
            f"{address} stopped firing -- the rule was loosened past the point "
            "where a real mailbox can still exist"
        )
