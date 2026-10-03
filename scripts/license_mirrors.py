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
    "bzip2",
    "expat",
    "libedit",
    "libffi",
    "liblzma",
    "libuuid",
    "mpdecimal",
    "ncurses",
    "openssl-3",
    "sqlite",
    "tcl",
    "tix",
    "zlib",
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


_FETCHED = "Sat 3 Oct 2026"
_ISSUE_5143 = "https://github.com/maintainer/music-dj-tools/issues/5143"
_ISSUE_5189 = "https://github.com/maintainer/music-dj-tools/pull/5189"


def _pin(
    ecosystem: str,
    name: str,
    version: str,
    text_relative: str,
    source_url: str,
    sha256: str,
    reason: str,
    issue: str = _ISSUE_5189,
) -> ReviewedLicenseText:
    return ReviewedLicenseText(
        ecosystem=ecosystem,
        name=name,
        version=version,
        text_relative=text_relative,
        source_url=source_url,
        fetched=_FETCHED,
        sha256=sha256,
        reason=reason,
        issue=issue,
    )


def _each(
    ecosystem: str,
    names_versions: tuple[tuple[str, str], ...],
    *,
    text_relative: str,
    source_url: str,
    sha256: str,
    reason: str,
) -> tuple[ReviewedLicenseText, ...]:
    return tuple(
        _pin(ecosystem, name, version, text_relative, source_url, sha256, reason) for name, version in names_versions
    )


_OBJC2_MIT = (
    ("block2", "0.6.2"),
    ("objc2", "0.6.4"),
    ("objc2-encode", "4.1.0"),
    ("objc2-foundation", "0.3.2"),
)
_OBJC2_TRIO = (
    ("dispatch2", "0.3.1"),
    ("objc2-app-kit", "0.3.2"),
    ("objc2-audio-toolbox", "0.3.2"),
    ("objc2-cloud-kit", "0.3.2"),
    ("objc2-core-audio", "0.3.2"),
    ("objc2-core-audio-types", "0.3.2"),
    ("objc2-core-data", "0.3.2"),
    ("objc2-core-foundation", "0.3.2"),
    ("objc2-core-graphics", "0.3.2"),
    ("objc2-core-image", "0.3.2"),
    ("objc2-core-text", "0.3.2"),
    ("objc2-core-video", "0.3.2"),
    ("objc2-exception-helper", "0.1.1"),
    ("objc2-io-surface", "0.3.2"),
    ("objc2-javascript-core", "0.3.2"),
    ("objc2-osa-kit", "0.3.2"),
    ("objc2-quartz-core", "0.3.2"),
    ("objc2-security", "0.3.2"),
    ("objc2-web-kit", "0.3.2"),
)
_DASP_DASP = (
    ("dasp", "0.11.0"),
    ("dasp-envelope", "0.11.0"),
    ("dasp-frame", "0.11.0"),
    ("dasp-interpolate", "0.11.0"),
    ("dasp-peak", "0.11.0"),
    ("dasp-rms", "0.11.0"),
    ("dasp-signal", "0.11.0"),
    ("dasp-slice", "0.11.0"),
    ("dasp-window", "0.11.1"),
)
_DASP_SAMPLE = (
    ("dasp-ring-buffer", "0.11.0"),
    ("dasp-sample", "0.11.0"),
)
_UNIC = (
    ("unic-char-property", "0.9.0"),
    ("unic-char-range", "0.9.0"),
    ("unic-common", "0.9.0"),
    ("unic-ucd-version", "0.9.0"),
)

_OBJC2_REASON = (
    "the crates.io package at this lock version ships no LICENSE file; madsmtm/objc2 LICENSE.md "
    "(identical at git SHAs 8852b424, 7b1abfd7, 8d214f54, b4167b58), workspace authors from "
    "Cargo.toml at 8852b424, and the MIT, Apache-2.0 and Zlib texts LICENSE.md cites are staged "
    "as one combined file (SPDX MIT template copyright line omitted; authors field is the "
    "locked-revision attribution)"
)
_DASP_REASON = (
    "the crates.io package ships no LICENSE file; RustAudio LICENSE-MIT at the crate git SHA is "
    "staged (bytes identical across dasp 221b810, sample 97c3bb9 and dasp window e4d5353)"
)

REVIEWED_LICENSE_TEXTS: tuple[ReviewedLicenseText, ...] = (
    _pin(
        "python",
        "rbox",
        "0.1.7",
        "docs/legal/GPL-3.0.txt",
        "https://www.gnu.org/licenses/gpl-3.0.txt",
        "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
        "the wheel declares GPLv3 only by PyPI classifier, ships no license file, and its declared "
        "source repo (github.com/dylanljones/rbox) 404s; GPLv3 section 4 requires giving every recipient "
        "a copy of the License, so the canonical text ships in its place",
        issue=_ISSUE_5143,
    ),
    *_each(
        "python",
        (("antlr4-python3-runtime", "4.9.3"),),
        text_relative="docs/legal/antlr4-4.9.3-LICENSE.txt",
        source_url="https://raw.githubusercontent.com/antlr/antlr4/4.9.3/LICENSE.txt",
        sha256="b1b379fcaf3219593a4c433feb1b35c780bed23fafaae440b1ae2771a9521e3a",
        reason="the 4.9.3 wheel/sdist ships no license file; antlr/antlr4 tag 4.9.3 LICENSE.txt is staged",
    ),
    *_each(
        "python",
        (("pyobjc-core", "12.2.2"),),
        text_relative="docs/legal/pyobjc-core-12.2.2-License.txt",
        source_url="https://raw.githubusercontent.com/ronaldoussoren/pyobjc/v12.2.2/pyobjc-core/License.txt",
        sha256="0ca04b07928d4872b9d9bb22187ca0426dd8bfab08f26eada0999a71dc81aaff",
        reason="the 12.2.2 wheel ships no License.txt (payload pruning also drops PyObjCTest copying.yml "
        "false positives); ronaldoussoren/pyobjc v12.2.2 pyobjc-core/License.txt is staged",
    ),
    *_each(
        "javascript",
        (("@esbuild/darwin-arm64", "0.25.12"),),
        text_relative="docs/legal/esbuild-LICENSE.md",
        source_url="https://raw.githubusercontent.com/evanw/esbuild/208f539945b145e7c9d6d844290f81c3fe5af320/LICENSE.md",
        sha256="b40ec5baec7bb34fa5b1c09521fa3cd52d5fad7adafed74932a2010d3612a681",
        reason="the darwin-arm64 native package ships no LICENSE; parent esbuild 0.25.12 LICENSE.md "
        "(gitHead 208f5399) covers this binary and is staged",
    ),
    *_each(
        "javascript",
        (("@rollup/rollup-darwin-arm64", "4.62.2"),),
        text_relative="docs/legal/rollup-LICENSE.md",
        source_url="https://raw.githubusercontent.com/rollup/rollup/8faa18777374582bb813d54ce3623f4acf1f9e0b/LICENSE.md",
        sha256="fa1bd040c5bdeefe65b3821cebf474f2733ce65df13089bd151dda1778e62fe8",
        reason="the darwin-arm64 native package ships no LICENSE; parent rollup 4.62.2 LICENSE.md "
        "(gitHead 8faa1877) covers this binary and is staged",
    ),
    *_each(
        "javascript",
        (("sirv", "3.0.2"),),
        text_relative="docs/legal/lukeed-MIT.txt",
        source_url="https://raw.githubusercontent.com/lukeed/sirv/1135207e92c40354543cbd15c763c7a61d79d432/license",
        sha256="ba573393f24555ac0528612ad39665fab5bdcc80330a61096024bbf5f736526d",
        reason="sirv 3.0.2 ships no license file; lukeed/sirv gitHead 1135207e license is staged",
    ),
    *_each(
        "javascript",
        (("@polka/url", "1.0.0-next.29"),),
        text_relative="docs/legal/lukeed-MIT.txt",
        source_url="https://raw.githubusercontent.com/lukeed/polka/02cbdb529ddca0a9f3d225e2abb2931924219cc3/license",
        sha256="ba573393f24555ac0528612ad39665fab5bdcc80330a61096024bbf5f736526d",
        reason="@polka/url 1.0.0-next.29 ships no license file; lukeed/polka gitHead 02cbdb52 license is staged",
    ),
    *_each(
        "javascript",
        (("signalsmith-stretch", "1.3.2"),),
        text_relative="docs/legal/signalsmith-stretch-LICENSE.txt",
        source_url="https://signalsmith-audio.co.uk/code/stretch.git",
        sha256="ee2ef82481ffb445ecdd4b3a4c1f82c0cddb2da8fe39d8e8dc384fffb3e7f06f",
        reason="npm 1.3.2 ships no LICENSE; LICENSE.txt from stretch.git commit "
        "83f32d337bb4604d878c532167072ab10a078b07 (unchanged since 2022-11-25) is staged",
    ),
    *_each(
        "javascript",
        (("is-reference", "3.0.3"),),
        text_relative="docs/legal/is-reference-3.0.3-LICENSE.txt",
        source_url="https://raw.githubusercontent.com/Rich-Harris/is-reference/8bb053129bfabe2f6a7d7ed050159d67ebe82829/package.json",
        sha256="c5e20d6bf1bbed90e7f08a2c64ce86131bba2c3eca5c08d1d6d73f1531841cf5",
        reason="npm 3.0.3 and gitHead 8bb053129b declare MIT and ship no LICENSE in the tarball or "
        "at that revision; package.json author/license and README License from that gitHead are "
        "staged with the MIT permission notice (SPDX template copyright line omitted)",
    ),
    *_each(
        "javascript",
        (("locate-character", "3.0.0"),),
        text_relative="docs/legal/locate-character-3.0.0-LICENSE.txt",
        source_url="https://raw.githubusercontent.com/Rich-Harris/locate-character/4f08a59ec248121f7002abd02ee7b94e8eda06bc/package.json",
        sha256="a397185c0bd097bb68329bbf21323ebf29123928ccabe14c26ef11c38662ef6c",
        reason="npm 3.0.0 and gitHead 4f08a59ec2 declare MIT and ship no LICENSE in the tarball or "
        "at that revision (npm repository field is GitLab; the gitHead is on GitHub); "
        "package.json author/license and README License from that gitHead are staged with the "
        "MIT permission notice (SPDX template copyright line omitted)",
    ),
    *_each(
        "rust",
        _OBJC2_MIT + _OBJC2_TRIO,
        text_relative="docs/legal/objc2-licenses.txt",
        source_url="https://raw.githubusercontent.com/madsmtm/objc2/8852b424193ca41602281b3d7540d7c8ed51e49a/LICENSE.md",
        sha256="2001f1ac74823ea95c52652785873026e36246088d72908175eeb4a3075e015e",
        reason=_OBJC2_REASON,
    ),
    *_each(
        "rust",
        _DASP_DASP,
        text_relative="docs/legal/dasp-LICENSE-MIT.txt",
        source_url="https://raw.githubusercontent.com/RustAudio/dasp/221b81038c528bf3fc364a8ab5cc4b2e52f7dbfc/LICENSE-MIT",
        sha256="b1d6df41ed3aa96806e74c729444d7c121d90e6660a6aed01d298e03fde475a0",
        reason=_DASP_REASON,
    ),
    *_each(
        "rust",
        _DASP_SAMPLE,
        text_relative="docs/legal/dasp-LICENSE-MIT.txt",
        source_url="https://raw.githubusercontent.com/RustAudio/sample/97c3bb9b2363c0b46ac1633858bf1054fd02a980/LICENSE-MIT",
        sha256="b1d6df41ed3aa96806e74c729444d7c121d90e6660a6aed01d298e03fde475a0",
        reason=_DASP_REASON,
    ),
    *_each(
        "rust",
        _UNIC,
        text_relative="docs/legal/unic-LICENSE-MIT.txt",
        source_url="https://raw.githubusercontent.com/open-i18n/rust-unic/5878605364af97a3358368a6eaef02104af2e016/LICENSE-MIT",
        sha256="23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3",
        reason="the crates.io package ships no LICENSE; open-i18n/rust-unic LICENSE-MIT at git SHA 58786053 is staged",
    ),
    *_each(
        "rust",
        (("unic-ucd-ident", "0.9.0"),),
        text_relative="docs/legal/unic-LICENSE-MIT.txt",
        source_url="https://raw.githubusercontent.com/open-i18n/rust-unic/8a6ce83063d90b91ae2ce59eddb803edd393fca9/LICENSE-MIT",
        sha256="23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3",
        reason="the crates.io package ships no LICENSE; open-i18n/rust-unic LICENSE-MIT at git SHA 8a6ce830 is staged",
    ),
    *_each(
        "rust",
        (("defmt-parser", "1.0.0"),),
        text_relative="docs/legal/defmt-LICENSE-MIT.txt",
        source_url="https://raw.githubusercontent.com/knurling-rs/defmt/4a8cdb44891ed57b8ff5a023b6bec7137c48708f/LICENSE-MIT",
        sha256="2710a622a896bba67356913d4d0492cab5465f61b2ecce6d880aeb483834fb50",
        reason="the crates.io package ships no LICENSE; knurling-rs/defmt LICENSE-MIT at git SHA 4a8cdb44 is staged",
    ),
    *_each(
        "rust",
        (("alloc-stdlib", "0.2.4"),),
        text_relative="docs/legal/alloc-stdlib-LICENSE.txt",
        source_url="https://raw.githubusercontent.com/dropbox/rust-alloc-no-stdlib/ae42d22078b98549e987d2f03d12df7b984fde47/LICENSE",
        sha256="c0c56f26d9c051cac4d200c34c84e7ae9aaa853e01a982a1df08b09931e518ae",
        reason="the crates.io package ships no LICENSE; dropbox/rust-alloc-no-stdlib LICENSE at git SHA ae42d220 is staged",
    ),
    *_each(
        "rust",
        (("selectors", "0.36.1"),),
        text_relative="docs/legal/selectors-0.36.1-LICENSE.txt",
        source_url="https://raw.githubusercontent.com/servo/stylo/635e1a19d02960588a00e189bd4bd5bdb150ec3d/selectors/lib.rs",
        sha256="8fb842f37e6e40c174b850d5c0816fd64efc2347d50bc4b6d7a68031b9b2b192",
        reason="the crates.io package at 0.36.1 (git SHA 635e1a19, path_in_vcs selectors) ships no "
        "LICENSE; MPL Exhibit A from lib.rs, Cargo.toml authors/license, and the canonical "
        "Mozilla MPL-2.0 text are staged",
    ),
    *_each(
        "rust",
        (("realfft", "3.5.0"),),
        text_relative="docs/legal/realfft-3.5.0-LICENSE.txt",
        source_url="https://raw.githubusercontent.com/HEnquist/realfft/d0d4eee0525fd27c96c8a046d6d107acd5ed84a6/Cargo.toml",
        sha256="8eb17835ae38101a31dca0aa580fefded3fa604c9b6a3f761faa8b84fb63b061",
        reason="HEnquist/realfft 3.5.0 (git SHA d0d4eee052) declares MIT in Cargo.toml and ships no "
        "LICENSE in the crate or at that revision; Cargo.toml authors/license and README "
        "License from that SHA are staged with the MIT permission notice (SPDX template "
        "copyright line omitted)",
    ),
)
