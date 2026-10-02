"""What the going-public audit LOOKS FOR, separate from how it walks a tree.

Split out of scripts/oss_tip_audit.py at the 600-line file limit, along a seam
that was already there: this module is the rule set and its exceptions, that one
is the git plumbing, the line accounting and the CLI. They change for different
reasons -- a new identity shape edits this file, a new source of bytes edits that
one -- and the history audit under `odj-private` consumes THIS half alone.

Every exception here is enumerated with its reason rather than shape-matched, so
the whole exception surface is reviewable on one screen. Shape rules were tried
repeatedly for the mailbox cases and were wrong in one direction or the other
every time (#1440).

NOTE: this file is scanned by the gate it defines. Write patterns and describe
shapes; never spell out an example address, home path or CGNAT address here. Four
review round-trips were spent relearning that.
"""

from __future__ import annotations

import re

# Binary sniff window. A NUL in the first 8 KiB marks a blob; size is NOT a skip
# criterion, because "too big to read" is how a real identifier stays green forever
# (Devin, #1440). 29 tracked files used to leave the scan unread on size alone.
BINARY_SNIFF_BYTES = 8192
PLACEHOLDER_USERS = frozenset(
    {
        # Synthetic logins the docs and tests use on purpose. EXACT matches only: a
        # prefix rule ("test", "example") also exempts `tester` and `example-person`,
        # which are shapes a real login has (Devin, #1440).
        "user",
        "test",
        "test-owner",
        "test-dead-home",
        "test-old",
        "someone-else",
        "somebody-else",
        "dj",
        # The repo's own placeholder for "the developer's own machine". It is not a
        # login on any machine this project runs on (the macOS account is the
        # author's first name, the Linux accounts are elsewhere), and it is what
        # #910/#1326 already substituted into the launchd plists, the autoreposync
        # skill's per-machine path table and the platform-path tests. Left out of
        # the original list, which is why 105 tracked references read as findings
        # (#1808).
        "dev",
        # The second macOS machine's placeholder login, so the autoreposync
        # per-machine path table and its profiles can keep three DISTINCT homes
        # (Air, silver, agentbox) after the real silver login was scrubbed. Keyed
        # to the fleet's own machine names, not to a shape (#1808).
        "silver",
        "old",
        "older",
        "oldname",
        "gone",
        "mapped",
        "example",
        "someone",
        "name",
        "you",
        "YOU",
        "me",
        "x",
        "whoever",
        "foo",
        "alice",
        "bob",
        "USER",
        "runner",
        # The self-hosted CI runner's service account, the local counterpart to
        # GitHub's hosted `runner` above. Not a person: it is referenced in
        # workflows, ownership tests and the analysis warmup lock, and the line
        # that first tripped this rule quotes a verbatim uv lock-timeout message,
        # so scrubbing it would damage a diagnostic record to hide a username
        # that identifies nobody.
        "ghrunner",
        # The project author's public first name; never a login on any live machine.
        "maintainer",
        # Login on the GCP VM decommissioned Tue 28 Jul 2026. The host no longer exists,
        # so the path names nothing reachable; scrubbing 120 planning docs buys nothing.
        "dev3",
        # De-identified logins minted by scripts/fixtures/deidentify_store_names.py for
        # tests/fixtures/provenance/cc-store-names.tsv.
        "vlx",
        "uwcyb",
    }
)
# A path segment that is a template, not a login: `<name>`, `$USER`, `${HOME}`, `%s`.
#
# `(` is deliberately NOT in that class. A bare leading parenthesis is legal in
# a real Windows profile name, so exempting every segment that starts with one
# let a parenthesized profile under the Windows users root through the
# publication gate at findings=0. The example is described rather than
# written out: spelling it here makes this very file a finding
# (Codex P1, #1440). Only the regex-group forms `(?...)` and an alternation
# containing `|` are templates; a parenthesized NAME is a name. For a gate
# whose false negative is a published identity and whose false positive is one
# reviewed line, this is the direction to err in.
_TEMPLATE_SEGMENT = re.compile(
    # `<name>`, `%s`, `{user}`, `*`, a regex group `(?...)` or an alternation
    # `(a|b)`; a `(` that opens a REGEX (`(?`, `([`, `(\\`, `(^`), which is how
    # this module's own pattern source reads; and `$NAME` ONLY in the env-var
    # shape, braced or all caps, so a profile literally named with a leading
    # `$` is still reported.
    r"^(?:[<%{*]|\$\{|\((?:\?|\[|\\|\^)|\([^)]*\|)"
)

# A bare `$NAME` is only a template when NAME is an interpolation this tree
# actually performs. The rule used to be "all caps after a `$`", which is a
# SHAPE, and a shape cannot separate an env var from a Windows profile whose
# directory name legally begins with a `$`, nor from a mailbox whose local part
# does. Both were exempted and left the publication audit green (Codex
# P1/BLOCKING, #1440, discussion_r3972967004). The forms are DESCRIBED rather
# than written out: this file is scanned by its own gate, and my first draft of
# this very comment spelled the example address and became a finding about the
# repository. Enumerated instead, for the same reason
# ALLOWED_NON_ADDRESSES is enumerated: every exception is reviewable on one
# screen, and an unlisted name is reported rather than assumed harmless.
# `${ANYTHING}` stays exempt on FORM above, because the braces are syntax a
# directory name cannot carry silently.
_BARE_ENV_VARS = frozenset(
    {
        "$HOME",
        "$USER",
        "$USERNAME",
        "$USERPROFILE",
        "$LOGNAME",
        "$PWD",
        "$TMPDIR",
        "$RUNNER_TEMP",
        "$GITHUB_WORKSPACE",
    }
)

# Reserved / non-routable mail domains: RFC 2606 (.invalid, .test, and the
# second-level example.com/net/org), RFC 6761 (.localhost) and mDNS (.local),
# plus the `.example` special-use suffix. Nobody receives mail at one, so an
# address there is a fixture by construction rather than because someone
# remembered to list it.
#
# What is NOT reserved is a domain that merely CONTAINS an `example` label:
# `sub.example.fr` is registrable and can carry a real mailbox, and treating
# the label as proof of reservation exempted it (Codex P1, #1440).
_RESERVED_MAIL_TLDS = frozenset({"invalid", "test", "localhost", "local", "example"})
_RESERVED_MAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
# Exact addresses that are service constants, not a person's mailbox. Enumerated
# rather than shape-matched so every exception is reviewable on one screen; a new
# address at any of these domains still fails.
ALLOWED_MAILBOXES = frozenset(
    {
        "git@github.com",  # VCS clone-URL constant
        "git@gitlab.com",  # VCS clone-URL constant
        "ghs_redacted@github.com",  # redacted-token fixture
        "noreply@anthropic.com",  # commit trailer constant
        "ci-eval@users.noreply.github.com",  # CI bot commit identity
        # The repository owner's GitHub noreply alias, used as the reporting
        # address in SECURITY.md and CODE_OF_CONDUCT.md. GitHub puts this exact
        # address on every commit, issue and review the account makes, and the
        # account handle is already in the repository's own URL, so withholding
        # it in two policy files removes nothing that is not already published.
        "15217094+owner@example.com",
        "public@o0.ingest.de.sentry.io",  # Sentry DSN public key, not a mailbox
        "public@o0.ingest.us.sentry.io",  # placeholder key, US ingest host (OBS-06)
        "public@o0.ingest.sentry.io",  # placeholder key, global ingest host (OBS-06)
        "support@sourcery.ai",  # vendor support address quoted in a planning doc
        "i@izs.me",  # third-party maintainer metadata inside a pnpm lockfile
    }
)
# Asset and version specs with an address's exact shape. Enumerated, like
# ALLOWED_MAILBOXES, so each one is a reviewed line.
#
# The first attempt at this was structural -- suppress a match sitting inside a
# single-slash path segment -- and that is the overshoot the report warned about
# in the other direction: a real address under a docs directory is an address,
# and placement is not proof of anything (Codex P1, #1440). A gate whose failure
# mode is a reviewed extra line is the right way round. The counter-example is
# spelled in the test, not here: this file is scanned by its own gate.
# A shape rule stood here first and kept being wrong in one direction or the
# other: "any numeric label" exempted `mail.123reg.co.uk`, and "every label before
# the suffix is numeric" still exempted `123.com`, which is registrable and can
# carry a real mailbox (Codex P1, #1440, twice). There is no shape that separates
# a version spec from a numeric domain, because there is no such difference --
# only the intent behind the string. So they are enumerated rather than shaped.
# The one shape that IS safe here is the retina-asset domain below, and it is
# safe for a reason none of those attempts had: it is anchored on a suffix no
# mailbox can sit behind. Read its comment before adding a second shape.
ALLOWED_NON_ADDRESSES = frozenset(
    {
        "signalsmith-stretch@1.3.2.patch",  # pnpm patch spec, package.json
    }
)

# The retina-asset naming convention: a name, an at-sign, a pixel multiplier,
# and an image extension. Apple iconsets, the Tauri icon manifest and every web
# asset pipeline write it, and it parses as a local part at a two-label domain.
#
# This one IS a shape, against the rule stated above, and the reason it is safe
# is the reason the numeric-domain shapes were not: the trailing label is an
# image extension, and none of the extensions below is a delegated top-level
# domain, so no mailbox can exist at one. That is a property of the DNS root
# rather than of this repository, so it cannot rot the way a value does.
#
# The extensions are ENUMERATED rather than matched as "any short suffix",
# because several plausible asset words ARE delegated and do carry real mail --
# the photo-related ones are the obvious trap. An unlisted extension is reported
# rather than assumed harmless, which is the same direction to err in as
# everything else in this module.
#
# Enumerating the FILENAMES instead is what this replaces, and it cost a red
# build every time an asset was added: #2528 enumerated the six names then in
# the tree, and the same commit had to add five more when the iconset slot set
# in scripts/desktop_icons.py was scanned. Do NOT rename asset files to dodge
# the gate: iconutil's iconset slot names are a fixed Apple contract, not
# something this repo controls.
_RETINA_ASSET_EXTENSIONS = (
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "svg",
    "avif",
    "ico",
    "icns",
    "bmp",
    "tif",
    "tiff",
    "pdf",
)
_RETINA_ASSET_DOMAIN = re.compile(
    r"^\d+x\.(?:" + "|".join(_RETINA_ASSET_EXTENSIONS) + r")$", re.IGNORECASE
)

# A systemd UNIT TYPE is not a mail TLD. `systemctl list-timers` renders the unit it
# activates as `<unit>@<instance>.<type>`, so recording one verbatim reads as an address
# at `<instance>.<type>` (`idd-lane@5.service`) -- a real shape, which is why it matched.
# Every label was checked against the IANA root zone on Thu 17 Sep 2026 and is NOT a
# delegated TLD, so no mailbox can exist behind one. `.target` IS delegated and is absent
# here for that reason, so a mailbox there is registrable and is still reported.
_SYSTEMD_UNIT_TYPE_LABELS = frozenset(
    ("service", "timer", "socket", "mount", "automount", "path", "slice", "scope", "swap", "device")
)

MAILBOX_EXEMPT_PATHS = frozenset(
    {
        ".mailmap",
        "docs/git-author-convention.md",
        # Verbatim upstream license mirrors (docs/legal/python-build-standalone/README.md):
        # bzip2's and zlib's own license texts name their authors by email. That is part
        # of the license, not the maintainer's identity. Enumerated exactly, not prefix-matched
        # (Codex P1, PR #4853 r4170573408): a directory-wide exemption would also cover
        # any FUTURE file added to this dir, including one with a real mailbox in it.
        # A new mirror needs an explicit line here, same as any other exemption.
        "docs/legal/python-build-standalone/LICENSE.bzip2.txt",
        "docs/legal/python-build-standalone/LICENSE.expat.txt",
        "docs/legal/python-build-standalone/LICENSE.libedit.txt",
        "docs/legal/python-build-standalone/LICENSE.libffi.txt",
        "docs/legal/python-build-standalone/LICENSE.liblzma.txt",
        "docs/legal/python-build-standalone/LICENSE.libuuid.txt",
        "docs/legal/python-build-standalone/LICENSE.mpdecimal.txt",
        "docs/legal/python-build-standalone/LICENSE.ncurses.txt",
        "docs/legal/python-build-standalone/LICENSE.openssl-3.txt",
        "docs/legal/python-build-standalone/LICENSE.sqlite.txt",
        "docs/legal/python-build-standalone/LICENSE.tcl.txt",
        "docs/legal/python-build-standalone/LICENSE.tix.txt",
        "docs/legal/python-build-standalone/LICENSE.zlib.txt",
    }
)
GENERATED_TEST_ID_PATHS = frozenset({".test_durations"})  # pytest-split cache, not a mailbox
# Tailnet labels that are fixtures by construction. `example-tailnet` is the
# synthetic label this repo standardized on for MagicDNS fixtures in tests, the
# agentbox README and `.env.sample`; no Tailscale network carries it. Enumerated,
# not prefix-matched: `example-tailnet-prod` would be a different network and is
# reported (#1808).
_ALLOWED_TAILNETS = frozenset(
    {
        "example",
        "example-tailnet",
        # Negative-example label `tests/scripts/test_oss_tip_audit.py` builds to
        # prove the tailnet-name rule fires; not a network anyone operates.
        "not-a-real-tailnet",
        # Negative-example label `tests/webui/test_crate_sync.py` builds to prove
        # a push destination on the WRONG tailnet is rejected; not a network
        # anyone operates.
        "not-this-deployments-tailnet",
    }
)

# A URL AUTHORITY immediately before a match means the `/Users/` (or `/home/`,
# or `C:\Users\`) segment is a URL ROUTE, not a home directory. The GitHub REST
# API's shape is `https://api.github.com/users/<login>`, and one recorded
# check-runs fixture carries 130 of them (#1808). The macOS rule is
# case-INSENSITIVE precisely so a lowercase spelling of a real home is caught,
# and case-folding is exactly what makes that URL read as one.
#
# Anchored at the end so it only fires when the authority runs straight into the
# match, which is the only position where the segment after it is a URL path.
_URL_AUTHORITY_BEFORE = re.compile(r"[a-z][a-z0-9+.\-]*://[^\s/]+$", re.IGNORECASE)

# Write-once session records. Their CONTENT is not scanned, because rewriting a
# dated record of what was actually run is worse than leaving it: the record is
# the evidence, and a scrub that edits it destroys the thing of value while
# changing nothing that is still reachable. Every other directory IS scanned.
#
# The scope-out is enumerated and the walker COUNTS these paths and prints the
# count, so the exemption cannot hide a new value quietly: `python -m
# scripts.oss_tip_audit --include-records` scans them too and is the number the
# next full audit should start from. The pathname of every record is still
# audited, so a record FILENAME cannot smuggle an identity through.
#
# The trade this makes explicit: a real identity typed into a NEW record will not
# fail CI. That is accepted, and is the reason this list is four directories
# rather than "anything dated".
RECORD_PATHS = ("app_docs/", "docs/threads/", ".planning/", "specs/")


def is_record_path(rel: str) -> bool:
    """True when `rel` is a path inside one of the write-once record directories."""
    return rel.startswith(RECORD_PATHS)


# A MagicDNS host: `<node>.<tailnet>.ts.net`. Used twice -- as the tailnet rule
# itself, and to recognize an ssh destination on an allowed tailnet as a fixture
# rather than a mailbox.
_TAILNET_HOST = re.compile(r"[a-z0-9-]+\.([a-z0-9-]+)\.ts\.net", re.IGNORECASE)
_TAILNET_HOST_INLINE = re.compile(r"\b[a-z0-9-]+\.([a-z0-9-]+)\.ts\.net\b", re.IGNORECASE)

# A lowercase literal that MUST be present for the rule to have any chance. The
# regex engine costs microseconds per start position, so on a multi-megabyte file
# it dominates; `in` on a str is a C memchr and settles the common case (no match
# anywhere) in microseconds total. Every prefilter is a strict substring of what
# its pattern can match, which is the property the test asserts -- a prefilter
# that is not would silently disable its rule.
RULE_PREFILTERS: dict[str, tuple[str, ...]] = {
    "home-path": ("/users/",),
    "windows-home-path": ("users",),
    "linux-home-path": ("/home/",),
    "consumer-mailbox": ("@",),
    "tailnet-name": (".ts.net",),
    "cgnat-address": ("100.",),
}

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # No trailing delimiter is required: a reference that ends at the login
    # (`/Users/name`, end of line or before a quote) names it just as plainly as
    # one that continues into a subdirectory.
    # Case-insensitive because the default macOS filesystem is: a lowercase
    # spelling opens the same home directory and names the same person.
    #
    # The Linux rule below is deliberately NOT folded, and the asymmetry is the
    # point rather than an oversight. Linux paths are case-sensitive, so an
    # uppercase spelling names a different directory, and folding it made
    # `/Home/End` -- the keyboard keys, in a UI document -- a finding. Nobody
    # writes a Linux home in caps, so the fold bought nothing and cost noise. A
    # "it is all just published text" rationale is tempting and would fold all
    # three; it was tried and this is what it produced.
    ("home-path", re.compile(r"/Users/([^/\s'\"`)]+)", re.IGNORECASE)),
    # Windows accepts BOTH separators: `C:/Users/name` is the form that appears in
    # anything that round-tripped through a URL, a JSON config, or Python's
    # pathlib, and a backslash-only rule read the tree clean while it sat there
    # (Codex P1, #1440). Components are case-insensitive there too.
    (
        "windows-home-path",
        # Either a drive letter or a bare backslash root. A DRIVE-LESS
        # `\Users\<login>` is how a WindowsPath renders a POSIX home, and it
        # names the account just as plainly, but a drive-required rule reported
        # the tree clean while one sat in a tracked docstring (Codex P1, #1440).
        #
        # The leading backslash is what separates this from the macOS form, which
        # the home-path rule already owns. Making the drive merely optional would
        # double-report every POSIX path instead.
        #
        # The drive letter must be a LONE letter: without the lookbehind, the `e`
        # of a `file:///Users/...` URL reads as drive `e:` and every such URL
        # becomes a Windows finding.
        # The profile segment may contain SPACES, and stopping at the first one
        # captured `the maintainer` out of a real name, which the placeholder list then
        # exempted -- the gate reporting clean on a full identity (Codex P1,
        # #1440). The segment therefore runs to a separator, quote or end of
        # line, with interior spaces allowed but never a trailing one.
        re.compile(
            r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]+|\\+)Users[\\/]+"
            # Spaces AND parentheses are legal in a Windows profile name, and
            # both were excluded, so the rule fell back to a partial capture of
            # the first token -- which the placeholder list then exempted, and a
            # full real name reported clean (Codex P1, #1440, twice).
            #
            # They are allowed in the alternative that ends at a separator, a
            # quote, or the END OF LINE. End-of-line had to join that list: a
            # path ending AT the profile name has no following separator, so the
            # rule fell back to the first token and the placeholder list exempted
            # it -- the same name was caught with a subdirectory appended and
            # missed at the home root (Codex P1, #1440).
            #
            # A trailing `)` counts only when it CLOSES one inside the same
            # segment. Otherwise a path wrapped in parentheses inside a sentence
            # reported the login with the sentence's own bracket stuck to it,
            # which is a wrong match string for whoever has to scrub it.
            #
            # A TAB ends the segment as firmly as a separator does: it is a
            # field delimiter, and without it one TSV row became a single
            # profile name spanning three columns.
            #
            # The remaining cost is real and accepted: trailing prose after a
            # path at end of line is captured too, because nothing distinguishes
            # it by shape from a spaced profile name. A gate whose failure mode is a
            # reviewed extra line is the right way round, and the tracked tree
            # was measured clean under it. That
            # lookahead is what keeps prose out: where a path is wrapped in
            # parentheses inside a sentence, the closing paren is followed by
            # end-of-line rather than a separator, so the first alternative
            # fails and the strict fallback captures just the login. The worked
            # example is in the test, not here: this file is scanned by its own
            # gate, and writing one out has now cost four round-trips.
            #
            # Allowing them unconditionally swallowed the prose
            # after a path, which is the overshoot of fixing the miss.
            r"((?:[^\\/\n\t'\"`]*\([^\\/\n\t'\"`)]*\)"
            r"|[^\\/\n\t'\"`]*[^\\/\s'\"`)])(?=[\\/\n\t\"'`]|$)"
            r"|[^\\/\s'\"`)]+)",
            re.IGNORECASE,
        ),
    ),
    # Linux homes too. The macOS and Windows rules reported the tree clean while a
    # real `/home/<login>` sat in four tracked files, so the gate certified a tip
    # that still named a live account (Codex P1, #1440).
    ("linux-home-path", re.compile(r"/home/([^/\s'\"`)]+)")),
    (
        # Any local part at any registrable domain, not a list of providers: a
        # provider list is a value that rots, and the rule exists to catch the
        # mailbox nobody thought to enumerate. Fixtures stay legible through the
        # reserved-domain rule above rather than through per-provider carve-outs.
        "consumer-mailbox",
        re.compile(
            # The lookbehind exists for COST, not for filtering: without it the
            # engine restarts the local-part scan at every character of a
            # minified bundle's single 3 MB line and goes quadratic. Only
            # characters that CONTINUE a local part belong in it. Neither `/`
            # nor `\` does, and excluding them suppressed real addresses after
            # a URL authority and after a Windows separator (Codex P1, #1440,
            # twice). Non-addresses are enumerated below instead, where the
            # reasoning is reviewable.
            # Unicode, not ASCII. An IDN domain written in its human-facing form
            # (a mailbox at a domain with non-ASCII letters) failed an ASCII-only
            # class at the first character, so the gate certified an
            # identity-bearing address as clean (Codex P1, #1440). `[^\W_]` is a
            # Unicode letter or digit; the TLD additionally excludes digits.
            # A quoted local part is a valid mailbox and carried the name just
            # as plainly, while an unquoted-only class returned nothing
            # (Codex P1, #1440).
            # The unquoted branch is RFC 5322 `atext`, not a convenience
            # subset: an address ending in `!` or `~` before the `@` could not
            # match at all, because the character immediately before the `@`
            # had to be in the class (Codex P1, #1440).
            #
            # Four atext characters are deliberately left out. Each is legal in
            # a local part and each is a DELIMITER in the text this gate reads,
            # so including it absorbs the delimiter into the token and the
            # allowlist stops matching. All four were measured, not guessed:
            #
            # `/` is legal in a local part, but including it makes every path
            # segment before an address part of the local part. The address is
            # reported either way, just with a longer match.
            #
            # The BACKTICK is legal too and cost 405 findings on the tracked
            # tree in one run: in markdown and docstrings a doubled backtick
            # sits directly before decorator text, so ``@pytest.mark.requirement
            # parses as a local part at a domain whose TLD is `requirement`.
            # A mailbox whose local part ends in a backtick is a shape nobody
            # writes; a gate that flags 405 non-addresses is one nobody runs.
            #
            # The APOSTROPHE and `=` cost the same way at smaller scale: a
            # source file quotes an address as `'git@gitlab.com'` and a config
            # line writes `user.email=ci-eval@...`, and both then failed the
            # exact-match allowlist because the delimiter had become part of the
            # address.
            # DOT-ATOM, not "atext with dots anywhere". An unquoted local part
            # is one or more non-empty atoms joined by single dots, so it can
            # neither begin nor end with a dot nor hold two in a row. The class
            # used to allow a dot at any position, which reported those three
            # shapes as mailboxes; on a MANDATORY gate a harmless
            # address-shaped identifier then blocks unrelated work (Codex P2,
            # #1440, discussion_r3975241649). The lookbehind already refused a
            # leading dot in the MIDDLE of a token, which is why this only
            # showed up at a token boundary.
            r"(?<![\w.-])(?:\"[^\"\n]{1,64}\"|"
            r"[\w!#$%&*+?^{|}~-]+(?:\.[\w!#$%&*+?^{|}~-]+)*)@"
            # A DNS label is letters, digits and hyphens. The interior class
            # used to be `[\w-]`, which includes `_`, so `package@foo_bar.com`
            # was reported as a mailbox at a domain that cannot host one; on a
            # MANDATORY gate a harmless identifier then blocks unrelated work
            # (Codex P2, #1440, discussion_r3972967015). The boundaries already
            # said `[^\W_]`; only the interior disagreed with them.
            r"[^\W_](?:(?:[^\W_]|-)*[^\W_])?"
            r"(?:\.[^\W_](?:(?:[^\W_]|-)*[^\W_])?)*"
            r"\.[^\W\d_]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    # DNS is case-insensitive, so a tailnet name in any case is the same name.
    ("tailnet-name", _TAILNET_HOST_INLINE),
    (
        "cgnat-address",
        # The carrier-grade NAT range exactly: leading octet 100, second octet
        # 64 through 127, and the last two real octets. A three-digit wildcard
        # also matched values above 255, which are not addresses at all (Codex
        # P2, #1440). Ranges are written as the pattern, not as an example
        # address: this file is scanned by its own gate, and an example here is
        # a finding about the repository.
        re.compile(
            # Bounded against adjacent digits AND dots: a word boundary alone
            # let a valid-looking middle substring of a longer dotted value (a
            # four-part version, say) report as an address, and this gate is
            # mandatory, so a false positive blocks unrelated work (Codex P2,
            # #1440).
            r"(?<![\d.])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])"
            r"(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){2}(?![\d.])"
        ),
    ),
)


def _is_placeholder_home(user: str) -> bool:
    # A TRAILING `=` is assignment syntax that the capture ran into, not part of
    # a name: `--map /Users/foo=/data/...` and `VAR=value` both end the segment
    # there. An `=` in the MIDDLE is part of the name, because Windows allows it
    # in a profile directory.
    #
    # Judging only the prefix, which is what this did before, exempted the whole
    # of `the maintainer=DJ` on the strength of `the maintainer` alone and published a real identity
    # (Codex P1/BLOCKING, #1440, discussion_r3970584434). Stripping only from the
    # END keeps `foo=` exempt and reports `the maintainer=DJ` whole. `foo=bar=` strips to
    # `foo=bar`, is not a placeholder, and is reported: one reviewed line in a
    # shape this tree does not use, against a published home directory.
    #
    # A trailing delimiter the capture ran into is the same case, and #1808 added
    # the rest of the set: `<string>/Users/dev</string>` in a plist snippet ends
    # the segment at the `<`, and a JSON list ends it at the `,`. Stripping is
    # safe in ONE direction only, which is why it is a strip and not a shape
    # rule: it can turn "placeholder plus punctuation" into "placeholder", and it
    # can never turn a real login into one, because the check after the strip is
    # still the exact-match list. `/Users/<real-login>,` strips to the login and
    # is still reported.
    stripped = user.rstrip("=,;<>")
    if stripped != user:
        return _is_placeholder_home(stripped)
    # Case-insensitively, because the home rules match case-insensitively: an
    # exemption that is case-SENSITIVE would fail to exempt the same placeholder
    # written in another case, which is noise, not safety.
    if user.lower() in {placeholder.lower() for placeholder in PLACEHOLDER_USERS}:
        return True
    # An ELISION is not a login: `/Users/...` in prose means "the rest is omitted".
    # This is narrow on purpose. The previous rule exempted any segment CONTAINING
    # a dot, which also exempted `first.last` -- the shape most likely to name a
    # real person (Codex P1, #1440). A segment of nothing but dots cannot be a
    # login on any of the three platforms.
    if user.strip(".") == "":
        return True
    # A template or a regex fragment, not a login: `<name>`, `$USER`, `dev3?`.
    #
    # Exempt on FORM, never on the mere presence of a character a real profile
    # may legally hold. The old test was `any(ch in user for ch in "?*[=|")`,
    # which exempted a bracketed or equals-bearing profile name outright, and
    # both characters are legal in a Windows directory name (Codex P1, #1440).
    # Only `? * |` survive as markers, because Windows forbids all three in a
    # name, so a segment holding one cannot be a real profile and must be a
    # glob or a regex. A genuine character class like `[a-z]+` is now reported,
    # which costs one reviewed line; the alternative cost a published identity.
    if user.upper() in _BARE_ENV_VARS:
        return True
    return _TEMPLATE_SEGMENT.match(user) is not None or any(ch in user for ch in "?*|")


def _is_reserved_mail_domain(domain: str) -> bool:
    lowered = domain.lower()
    # Both are final labels no mailbox can sit at: reserved by policy, or not delegated.
    if lowered.split(".")[-1] in _RESERVED_MAIL_TLDS | _SYSTEMD_UNIT_TYPE_LABELS:
        return True
    # A host on an ALLOWED tailnet is a fixture by the same construction the
    # tailnet rule uses, and `<user>@<host>` there is an ssh destination rather
    # than a mailbox. Reuses _ALLOWED_TAILNETS so there is one list, not two.
    tailnet = _TAILNET_HOST.fullmatch(lowered)
    if tailnet is not None and tailnet.group(1) in _ALLOWED_TAILNETS:
        return True
    # The reserved second-level names, and hosts under them, but NOT every
    # domain that happens to carry the label somewhere.
    return any(
        lowered == reserved or lowered.endswith(f".{reserved}")
        for reserved in _RESERVED_MAIL_DOMAINS
    )


def _is_reportable_mailbox(address: str) -> bool:
    local, _, domain = address.rpartition("@")
    if _is_reserved_mail_domain(domain):
        return False
    # A TEMPLATE is not an address, the same judgement `_is_placeholder_home`
    # already makes for a path segment. Widening the local part to RFC atext
    # brought `$`, `{` and `}` in with it, so a shell interpolation like a
    # token substituted into a clone URL parsed as a mailbox.
    if local.upper() in _BARE_ENV_VARS or _TEMPLATE_SEGMENT.match(local):
        return False
    # A retina asset is a filename, not a mailbox. Checked on the DOMAIN so the
    # whole family is covered, rather than on the full string, which needed a
    # new entry for every asset added to the tree.
    if _RETINA_ASSET_DOMAIN.match(domain):
        return False
    if address.lower() in ALLOWED_NON_ADDRESSES:
        return False
    return address.lower() not in ALLOWED_MAILBOXES


def _rule_accepts(rule: str, match: re.Match[str], preceding: str = "") -> bool:
    if rule in {"home-path", "windows-home-path", "linux-home-path"}:
        if _URL_AUTHORITY_BEFORE.search(preceding):
            return False
        return not _is_placeholder_home(match.group(1))
    if rule == "consumer-mailbox":
        return _is_reportable_mailbox(match.group(0))
    if rule == "tailnet-name":
        # DNS is case-insensitive; so is the allowlist it is compared against.
        return match.group(1).lower() not in _ALLOWED_TAILNETS
    return True
