"""Bundled (non package-manager) components for the third-party license inventory.

Split out of scripts/third_party_licenses.py (file_size.over_limit_python ratchet,
the same seam as scripts/license_classify.py and scripts/license_mirrors.py): the
relocatable runtime, model weights, fonts, mirrored codec texts, and vendored
``apps/**/_vendor`` sources. Collectors for Python, JS and Rust stay in
scripts/third_party_licenses.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.license_mirrors import PBS_NATIVE_LIBRARIES, PBS_NATIVE_LICENSES_RELATIVE
from scripts.license_model import Component, LicenseInventoryError, _read_text

_FRONTEND_RELATIVE = Path("apps/webui/frontend")

_UPSTREAM_LICENSE_HEADER = re.compile(
    r"Upstream license:\s+.*?\((?P<spdx>[A-Za-z0-9.+-]+)\)"
)
_GITHUB_REPO_URL = re.compile(r"https://github.com/(?P<org>[^/\s]+)/(?P<repo>[^/\s]+)/")


def vendored_source_components(repo_root: Path) -> list[Component]:
    """Every ``apps/**/_vendor/*.py`` source file the dmg redistributes, with its license text.

    Header contract (first ~30 lines): ``Upstream license: ... (<SPDX-id>)`` and a
    ``https://github.com/<org>/<repo>/`` URL. ``docs/legal/<SPDX>.txt`` must exist.
    """
    apps_root = (repo_root / "apps").resolve()
    if not apps_root.is_dir():
        return []
    components: list[Component] = []
    for vendor_dir in sorted(apps_root.rglob("_vendor")):
        if not vendor_dir.is_dir():
            continue
        if "node_modules" in vendor_dir.parts:
            continue
        try:
            vendor_dir.resolve().relative_to(apps_root)
        except ValueError:
            continue
        for source in sorted(vendor_dir.glob("*.py")):
            if source.name.startswith("__init__"):
                continue
            header = "\n".join(source.read_text(encoding="utf-8", errors="replace").splitlines()[:30])
            license_match = _UPSTREAM_LICENSE_HEADER.search(header)
            if license_match is None:
                raise LicenseInventoryError(
                    f"{source.relative_to(repo_root)} has no parseable "
                    "'Upstream license: ... (<SPDX-id>)' header"
                )
            github_match = _GITHUB_REPO_URL.search(header)
            if github_match is None:
                raise LicenseInventoryError(
                    f"{source.relative_to(repo_root)} has no https://github.com/<org>/<repo>/ URL in its header"
                )
            spdx = license_match.group("spdx")
            project = github_match.group("repo")
            homepage = f"https://github.com/{github_match.group('org')}/{project}/"
            text_path = repo_root / "docs" / "legal" / f"{spdx}.txt"
            if not text_path.is_file():
                raise LicenseInventoryError(
                    f"{text_path} missing: vendored {source.relative_to(repo_root)} is {spdx} with no license text"
                )
            rel = source.relative_to(repo_root).as_posix()
            components.append(
                Component(
                    "bundled",
                    f"{source.name} (vendored {project} Kaitai code)",
                    "",
                    spdx,
                    homepage,
                    [(f"{spdx}.txt", _read_text(text_path))],
                    note=f"Vendored generated source at {rel}.",
                )
            )
    return components


def supplement_components(repo_root: Path, payload_dir: Path) -> list[Component]:
    runtime_licenses = sorted((payload_dir / "runtime/lib").glob("python3*/LICENSE.txt"))
    if not runtime_licenses:
        raise LicenseInventoryError(f"no CPython LICENSE.txt under {payload_dir}/runtime/lib")
    beat_this_notice = payload_dir / "models/beatgrid/LICENSE-beat_this.txt"
    if not beat_this_notice.is_file():
        raise LicenseInventoryError(f"{beat_this_notice} missing: the weights ship without their notice")
    font_license = repo_root / _FRONTEND_RELATIVE / "static/fonts/Anybody-OFL.txt"
    staged_fonts = list((payload_dir / "app" / _FRONTEND_RELATIVE / "build/fonts").glob("anybody-*.woff2"))
    if not font_license.is_file() or not staged_fonts:  # attribute only a font the payload ships (Sol P1)
        raise LicenseInventoryError(f"{font_license} missing, or no Anybody font in the staged SPA: {staged_fonts}")
    mpg123_copying = repo_root / "docs/legal/mpg123-COPYING.txt"
    if not mpg123_copying.is_file():
        raise LicenseInventoryError(f"{mpg123_copying} missing: mpg123 (LGPL-2.1) ships with no license text")
    flac_copying = repo_root / "docs/legal/libFLAC-COPYING.Xiph.txt"
    if not flac_copying.is_file():
        raise LicenseInventoryError(f"{flac_copying} missing: libFLAC (BSD-3-Clause) ships with no license text")
    native_lib_dir = repo_root / PBS_NATIVE_LICENSES_RELATIVE
    native_lib_licenses = [native_lib_dir / f"LICENSE.{lib}.txt" for lib in PBS_NATIVE_LIBRARIES]
    if missing_native := [path.name for path in native_lib_licenses if not path.is_file()]:
        raise LicenseInventoryError(
            f"{missing_native} missing under {native_lib_dir}: the CPython runtime statically links "
            "these native libraries, whose notices must ship with the binary (Sol P1, PR #4853). "
            f"See {native_lib_dir}/README.md to refresh this mirror."
        )
    return [
        Component(
            "bundled", "CPython (python-build-standalone)", runtime_licenses[0].parent.name, "PSF-2.0",
            "https://github.com/astral-sh/python-build-standalone",
            [("LICENSE.txt", _read_text(runtime_licenses[0]))]
            + [(path.name, _read_text(path)) for path in native_lib_licenses],
            note="The relocatable interpreter statically links third-party C libraries; their "
                 f"license texts are mirrored from {PBS_NATIVE_LICENSES_RELATIVE}/ (see its README "
                 "for provenance) rather than merely referenced.",
        ),
        Component(
            "bundled", "Beat This! final0 checkpoint", "final0", "MIT",
            "https://github.com/CPJKU/beat_this",
            [("LICENSE-beat_this.txt", _read_text(beat_this_notice))],
            note="Weights; upstream states they are MIT. Training-data terms are upstream's to assess.",
        ),
        Component(
            "bundled", "Anybody (variable font, wordmark subset)", "", "OFL-1.1",
            "https://github.com/etunni/anybody",
            [("Anybody-OFL.txt", _read_text(font_license))],
        ),
        Component(
            "bundled", "mpg123 (compiled to WebAssembly inside mpg123-decoder)", "", "LGPL-2.1-only",
            "https://www.mpg123.de/",
            [("mpg123-COPYING.txt", _read_text(mpg123_copying))],
            note="The npm wrapper declares MIT but is a WebAssembly build of mpg123 itself, which is "
                 "LGPL-2.1; the wrapper package ships no license text, so mpg123's own COPYING is "
                 "mirrored from libsdl-org/mpg123 (an upstream mirror) instead. A human must still "
                 "confirm the compiled .wasm's relinking and source-offer obligations under LGPL-2.1.",
        ),
        Component(
            "bundled", "libFLAC (compiled to WebAssembly inside @wasm-audio-decoders/flac)", "", "BSD-3-Clause",
            "https://xiph.org/flac/",
            [("libFLAC-COPYING.Xiph.txt", _read_text(flac_copying))],
            note="The npm wrapper ships no license text but compiles libFLAC into its WebAssembly (Codex P1, "
                 "PR #4853); COPYING.Xiph is mirrored from xiph/flac at 1507800de4b, the wasm-audio-decoders "
                 "modules/flac submodule pin at 3c74930e67, fetched Sat 3 Oct 2026.",
        ),
        *vendored_source_components(repo_root),
    ]
