"""Checked-in license texts the dmg inventory attaches, and the human-reviewed exceptions.

Split out of scripts/third_party_licenses.py (file_size.over_limit_python ratchet, the same seam as
scripts/license_classify.py): this module is DATA a reviewer signs off on -- which mirrored files must
exist, which components may ship textless, which canonical text stands in for a missing one -- while
that module is the inventory machinery. Every entry is enumerated, pinned and carries its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Checked-in mirror of python-build-standalone's per-library LICENSE files for the CPython runtime's
#: statically-linked native C libs; see that dir's README.md for provenance.
PBS_NATIVE_LICENSES_RELATIVE = Path("docs/legal/python-build-standalone")
#: Required BY NAME, not globbed (Codex P2, PR #4853 r4170677873): a nonempty-glob check let a refresh
#: that lost LICENSE.openssl-3.txt still ship and verify a dmg without OpenSSL's license.
PBS_NATIVE_LIBRARIES: tuple[str, ...] = (
    "bzip2", "expat", "libedit", "libffi", "liblzma", "libuuid", "mpdecimal",
    "ncurses", "openssl-3", "sqlite", "tcl", "tix", "zlib",
)

#: (ecosystem, name) pairs verified textless at BOTH the installed package
#: AND its upstream repo root -- the ONLY components `write_payload_license_files`
#: may stage without a license text or notice (Sol P1, PR #4853: the flag
#: report used to exclude "bundled" wholesale from its textless table, which
#: let a NEW textless component of any ecosystem ship unnoticed). mpg123 was
#: wrongly listed here once on the premise that "the package carries no
#: license text" -- true of its npm wrapper, false of mpg123 itself, whose
#: COPYING (LGPL-2.1) is now mirrored at docs/legal/mpg123-COPYING.txt and
#: staged as its license text; that single-file check was not enough on its
#: own, hence the BOTH above. eshaz/wasm-audio-decoders below IS verified at
#: both: every package declares MIT but ships no LICENSE file, and the
#: monorepo's GitHub root has none either (commit 3c74930e67, Fri 2 Oct 2026).
#: The wrappers are textless, but the native code they compile in is not: mpg123 and libFLAC
#: ship as bundled components with their own mirrored texts (scripts/third_party_licenses.py).
KNOWN_TEXTLESS: frozenset[tuple[str, str]] = frozenset(
    {
        ("javascript", "mpg123-decoder"),
        ("javascript", "@wasm-audio-decoders/common"),
        ("javascript", "@wasm-audio-decoders/flac"),
    }
)


@dataclass(frozen=True)
class ReviewedLicenseText:
    """A canonical license text staged for ONE exact release whose distribution ships none.

    Pinned to name AND version, so an upgrade fails the build again instead of silently
    inheriting the exception, and to the committed text's sha256, so the text cannot drift.
    """

    ecosystem: str
    name: str
    version: str
    text_relative: str
    source_url: str
    fetched: str
    sha256: str
    reason: str
    issue: str


REVIEWED_LICENSE_TEXTS: tuple[ReviewedLicenseText, ...] = (
    ReviewedLicenseText(
        ecosystem="python",
        name="rbox",
        version="0.1.7",
        text_relative="docs/legal/GPL-3.0.txt",
        source_url="https://www.gnu.org/licenses/gpl-3.0.txt",
        fetched="Sat 3 Oct 2026",
        sha256="3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
        reason="the wheel declares GPLv3 only by PyPI classifier, ships no license file, and its declared "
        "source repo (github.com/dylanljones/rbox) 404s; GPLv3 section 4 requires giving every recipient "
        "a copy of the License, so the canonical text ships in its place",
        issue="https://github.com/maintainer/music-dj-tools/issues/5143",
    ),
)
