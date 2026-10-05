"""Generate THIRD-PARTY-LICENSES for the shipped Open DJ desktop app.

WHAT THIS PRODUCES

``THIRD-PARTY-LICENSES.txt`` (every shipped component, its license identifier and the verbatim
license / NOTICE texts its distribution carries) and a markdown flag report (every component whose
license is copyleft, non-commercial or unidentified). The dmg build stages both inside the payload, so
they land in ``Open DJ.app/Contents/Resources/payload/``. Reviewed exceptions: scripts/license_mirrors.py;
shared model and bundled inventory: scripts/license_model.py, scripts/license_bundled.py.

WHERE EACH ECOSYSTEM'S INVENTORY COMES FROM (the shipped artifact, not the declared intent)

- Python: the ``*.dist-info`` directories of the STAGED payload sites
  (``pylib`` and the beat-grid runner site), read with ``importlib.metadata``.
  That is what the interpreter will import, so it cannot drift from the lock.
- JavaScript: the SPA bundles its runtime dependencies, so the inventory is
  the production dependency closure from ``pnpm-lock.yaml`` plus the
  framework runtime that vite bundles from devDependencies (svelte,
  @sveltejs/kit), with texts read from ``node_modules`` (needs a prior
  ``pnpm install``, which the SPA build already requires). Over-inclusion is
  the safe direction for attribution.
- Rust: ``cargo metadata --locked`` for the Tauri shell, the ``odj-audio``
  engine (feature ``device``, as staged) and the waveform PyO3 extension,
  following normal dependency edges only (build and dev edges never ship).
- Bundled data and runtimes that are not package-manager managed: the relocatable CPython, the Beat
  This! weights notice, the Anybody font, the native codecs compiled into the JS audio decoders,
  and vendored source under ``apps/**/_vendor``.

Industry-standard equivalents (pip-licenses, license-checker, cargo-about) were considered; this stays
one stdlib-only module because the build already stages every ecosystem and a second tool per ecosystem
would be three more things to pin. See docs/third-party-licenses.md.

Requirements:

- ✔︎ ✅ 🎯 The payload carries THIRD-PARTY-LICENSES.txt and NOTICE.
  -> :func:`write_payload_license_files`, called from
  ``scripts.build_engine_payload.build`` (OSSPUB-05).
- ✔︎ ✅ 🎯 Every component is classified; copyleft, non-commercial and unknown
  licenses are listed in the flag report, never silently passed.
  -> :func:`classify_license`, :func:`flag_report`.
- ✔︎ ✅ 🎯 A tool that cannot measure fails loudly (missing site, missing
  node_modules, cargo failure), it never emits an empty inventory.

Acceptance tests:

- [if] the payload site has no dist-info [then] generation raises, [else ⛔️].
- [if] a component's license is "GPL-3.0-or-later" [then] it appears in the
  flag report as strong-copyleft, [else ⛔️].
- [if] a component is "MIT OR GPL-2.0" [then] it is permissive (a permissive
  option exists), [else ⛔️].
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as importlib_metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from scripts.license_bundled import supplement_components, vendored_source_components
from scripts.license_classify import FLAGGED, RANK, Cat, classify_license
from scripts.license_mirrors import KNOWN_TEXTLESS, REVIEWED_LICENSE_TEXTS
from scripts.license_model import (
    Component,
    LICENSE_FILE_PATTERN,
    LicenseInventoryError,
    _license_files_in,
    _read_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

LICENSES_FILE_NAME = "THIRD-PARTY-LICENSES.txt"
REPORT_FILE_NAME = "THIRD-PARTY-LICENSES-FLAGS.md"
NOTICE_FILE_NAME = "NOTICE"
ROOT_LICENSE_FILE_NAME = "LICENSE"

PYTHON_SITES_RELATIVE: tuple[str, ...] = ("pylib", "runners/beatgrid/site")
FRONTEND_RELATIVE = Path("apps/webui/frontend")
# vite bundles the framework runtime out of devDependencies, so the
# production-only closure would under-attribute the shipped SPA.
JS_BUNDLED_FROM_DEV: tuple[str, ...] = ("svelte", "@sveltejs/kit")
# (crate dir, extra cargo args): the three Rust artifacts a dmg carries.
RUST_CRATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apps/desktop/src-tauri", ()),
    ("apps/audio-engine", ("--features", "device")),
    ("apps/webui/server/native/waveform", ()),
)
RUST_TARGET_TRIPLE = "aarch64-apple-darwin"

NON_TEXT_SUFFIXES: frozenset[str] = frozenset({".py", ".pyc", ".pyi", ".so", ".dylib", ".dll", ".h", ".c", ".js", ".json"})

#: A real inventory renders hundreds of license texts; a stub cannot pass.
MIN_LICENSES_FILE_CHARS = 100_000

#: Our own packages are not third party.
FIRST_PARTY_NAMES: frozenset[str] = frozenset(
    {
        "music-dj-tools",
        "music-dj-tools-webui",
        "music-dj-tools-waveform-native",
        "music_dj_tools_waveform_native",
        "odj-audio",
        "open-dj-desktop",
        "open-dj",
    }
)


def _meta_get(metadata: importlib_metadata.PackageMetadata, key: str) -> str | None:
    """`PackageMetadata.get`, typed via `__getitem__` (the stub omits `.get`)."""
    try:
        return metadata[key]
    except KeyError:
        return None


# ----- python -------------------------------------------------------------
def _python_license_string(metadata: importlib_metadata.PackageMetadata) -> str:
    expression = _meta_get(metadata, "License-Expression")
    if expression:
        return expression.strip()
    free_text = (_meta_get(metadata, "License") or "").strip()
    free_text_usable = free_text and "\n" not in free_text and len(free_text) < 100 and free_text.upper() != "UNKNOWN"
    if free_text_usable and classify_license(free_text) != Cat.UNKNOWN:
        return free_text
    classifiers = [
        c.split("::")[-1].strip()
        for c in (metadata.get_all("Classifier") or [])
        if c.startswith("License ::") and c.split("::")[-1].strip() != "OSI Approved"
    ]
    if classifiers:
        return " / ".join(classifiers)
    return free_text if free_text_usable else ""


def python_components(payload_dir: Path) -> list[Component]:
    """Every configured Python site, required individually.

    `PYTHON_SITES_RELATIVE` names every site this inventory is responsible for (the main app's `pylib` AND
    the beatgrid runner's own venv). Accepting whichever sites happened to exist let a missing or empty site
    silently drop that runtime's ENTIRE dependency set (Sol P1, PR #4853), so each must exist and hold at
    least one measurable distribution, or the build fails naming it. A license file vendored outside
    .dist-info also becomes its own unidentified component, so the flag report lists it for a human
    (soundfile's BSD wrapper ships LGPL libsndfile, Codex P1, PR #4853).
    """
    components: dict[tuple[str, str], Component] = {}
    sites = [payload_dir / relative for relative in PYTHON_SITES_RELATIVE]
    if missing := [site for site in sites if not site.is_dir()]:
        raise LicenseInventoryError(f"Python site(s) missing under {payload_dir}: {missing}; expected all of {PYTHON_SITES_RELATIVE}")
    for site in sites:
        site_distributions = list(importlib_metadata.distributions(path=[str(site)]))
        if not site_distributions:
            raise LicenseInventoryError(f"Python site {site} has no dist-info: unmeasurable, not empty by design")
        for dist in site_distributions:
            name = dist.metadata["Name"]
            if name is None or name.lower() in FIRST_PARTY_NAMES:
                continue
            key = (re.sub(r"[-_.]+", "-", name).lower(), dist.version)
            if key in components:
                continue
            texts: list[tuple[str, str]] = []
            for file in dist.files or []:
                if LICENSE_FILE_PATTERN.search(file.name) and file.suffix not in NON_TEXT_SUFFIXES:
                    located = Path(str(dist.locate_file(file)))
                    if located.is_file():
                        texts.append((file.name if ".dist-info" in str(file) else str(file), _read_text(located)))
            components[key] = Component(
                ecosystem="python",
                name=name,
                version=dist.version,
                license=_python_license_string(dist.metadata),
                homepage=_meta_get(dist.metadata, "Home-page") or "",
                texts=texts,
            )
            for path, text in (t for t in texts if "/" in t[0]):  # vendored: not the wrapper's license
                components[(f"{key[0]}:{path}", dist.version)] = Component(
                    "python", f"{name} vendored {path}", dist.version, "", texts=[(path, text)],
                    note=f"Vendored inside {name}; its license is not identified from metadata, read the text.")
    if not components:
        raise LicenseInventoryError(f"no dist-info found in {sites}")
    return sorted(components.values(), key=lambda c: c.name.lower())


# ----- javascript ---------------------------------------------------------
def _lock_package_key(snapshot_key: str) -> str:
    """``'@scope/name@1.2.3(peer@x)'`` -> ``@scope/name@1.2.3``."""
    return snapshot_key.split("(", 1)[0]


def js_closure(frontend_dir: Path) -> tuple[list[str], set[str]]:
    """(closure keys, keys reached ONLY through optionalDependencies).

    Optional edges are per-platform binaries (esbuild, rollup): the ones not
    installed on the build host are not in the bundle either.
    """
    lock = yaml.safe_load((frontend_dir / "pnpm-lock.yaml").read_text(encoding="utf-8"))
    importer = lock["importers"]["."]
    snapshots = lock["snapshots"]
    by_key = {_lock_package_key(key): value for key, value in snapshots.items()}
    roots: list[str] = []
    for name, spec in importer.get("dependencies", {}).items():
        roots.append(f"{name}@{_lock_package_key(str(spec['version']))}")
    dev = importer.get("devDependencies", {})
    for name in JS_BUNDLED_FROM_DEV:
        if name not in dev:
            raise LicenseInventoryError(f"{name} is no longer a devDependency of the SPA; update JS_BUNDLED_FROM_DEV")
        roots.append(f"{name}@{_lock_package_key(str(dev[name]['version']))}")
    seen: set[str] = set()
    required: set[str] = set(roots)
    stack = list(roots)
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        if key not in by_key:
            raise LicenseInventoryError(f"{key} is missing from pnpm-lock.yaml snapshots")
        for field_name in ("dependencies", "optionalDependencies"):
            for dep_name, dep_version in (by_key[key].get(field_name) or {}).items():
                dep_key = f"{dep_name}@{_lock_package_key(str(dep_version))}"
                if field_name == "dependencies":
                    required.add(dep_key)
                stack.append(dep_key)
    return sorted(seen), seen - required


def _pnpm_bin() -> str:
    """Absolute path to pnpm; see scripts/quality_gate.py:_pnpm_bin for why a bare
    ``pnpm`` never launches on Windows (CreateProcess ignores PATHEXT)."""
    path = shutil.which("pnpm")
    if path is None:
        raise LicenseInventoryError("pnpm is not on PATH; required to inventory JS licenses")
    return path


def js_components(frontend_dir: Path) -> list[Component]:
    if not (frontend_dir / "node_modules").is_dir():
        raise LicenseInventoryError(f"{frontend_dir}/node_modules missing: run `pnpm install --frozen-lockfile` first")
    listing = subprocess.run(
        [_pnpm_bin(), "licenses", "list", "--json", "--long"],
        cwd=frontend_dir, capture_output=True, text=True, check=False,
    )
    if listing.returncode != 0:
        raise LicenseInventoryError(f"pnpm licenses list failed:\n{listing.stderr}")
    installed: dict[str, tuple[str, str, Path]] = {}
    for license_name, packages in json.loads(listing.stdout).items():
        for package in packages:
            for version, path in zip(package["versions"], package["paths"], strict=True):
                installed[f"{package['name']}@{version}"] = (license_name, package.get("homepage", ""), Path(path))
    components: list[Component] = []
    closure, optional_only = js_closure(frontend_dir)
    for key in closure:
        if key not in installed and key in optional_only:
            continue
        if key not in installed:
            raise LicenseInventoryError(f"{key} is in the lock closure but not installed in node_modules")
        license_name, homepage, path = installed[key]
        name, _, version = key.rpartition("@")
        components.append(Component("javascript", name, version, license_name, homepage, _license_files_in(path)))
    return sorted(components, key=lambda c: c.name.lower())


# ----- rust ---------------------------------------------------------------
def rust_components(repo_root: Path) -> list[Component]:
    cargo_bin = shutil.which("cargo")
    if cargo_bin is None:
        raise LicenseInventoryError("cargo is not on PATH")
    components: dict[tuple[str, str], Component] = {}
    for crate_relative, extra_args in RUST_CRATES:
        crate = repo_root / crate_relative
        result = subprocess.run(
            [cargo_bin, "metadata", "--locked", "--format-version", "1",
             "--filter-platform", RUST_TARGET_TRIPLE, *extra_args],
            cwd=crate, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise LicenseInventoryError(f"cargo metadata failed in {crate}:\n{result.stderr}")
        metadata = json.loads(result.stdout)
        packages = {p["id"]: p for p in metadata["packages"]}
        nodes = {n["id"]: n for n in metadata["resolve"]["nodes"]}
        workspace = set(metadata["workspace_members"])
        seen: set[str] = set()
        stack = list(workspace)
        while stack:
            package_id = stack.pop()
            if package_id in seen:
                continue
            seen.add(package_id)
            # kind None is a normal edge; "build" and "dev" never ship.
            stack.extend(
                dep["pkg"]
                for dep in nodes[package_id]["deps"]
                if any(kind["kind"] is None for kind in dep["dep_kinds"])
            )
        for package_id in seen - workspace:
            package = packages[package_id]
            key = (package["name"], package["version"])
            if key in components:
                continue
            directory = Path(package["manifest_path"]).parent
            texts = _license_files_in(directory)
            if package.get("license_file"):
                declared = directory / package["license_file"]
                if declared.is_file() and declared.name not in {name for name, _ in texts}:
                    texts.append((declared.name, _read_text(declared)))
            components[key] = Component(
                "rust", package["name"], package["version"],
                package.get("license") or "", package.get("homepage") or package.get("repository") or "",
                texts,
            )
    return sorted(components.values(), key=lambda c: c.name.lower())


def _reviewed_name(name: str) -> str:
    """PEP 503 / cargo hyphen-normalization so `dasp_envelope` matches `dasp-envelope`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def attach_reviewed_license_texts(repo_root: Path, components: list[Component]) -> list[Component]:
    """Stage each REVIEWED_LICENSE_TEXTS entry on its exact (ecosystem, name, version), if textless."""
    reviewed = {(e.ecosystem, _reviewed_name(e.name), e.version): e for e in REVIEWED_LICENSE_TEXTS}
    for c in components:
        entry = reviewed.get((c.ecosystem, _reviewed_name(c.name), c.version))
        if entry is None or any(text.strip() for _, text in c.license_texts):
            continue
        text_path = repo_root / entry.text_relative
        if not text_path.is_file() or hashlib.sha256(text_path.read_bytes()).hexdigest() != entry.sha256:
            raise LicenseInventoryError(f"{text_path} is missing or not the reviewed text (sha256 {entry.sha256})")
        c.texts.append((text_path.name, _read_text(text_path)))
        c.note = f"Canonical text from {entry.source_url} (fetched {entry.fetched}): {entry.reason}. {entry.issue}"
    return components


# ----- rendering ----------------------------------------------------------
def collect_all(repo_root: Path, payload_dir: Path) -> list[Component]:
    return attach_reviewed_license_texts(
        repo_root,
        [
            *python_components(payload_dir),
            *js_components(repo_root / FRONTEND_RELATIVE),
            *rust_components(repo_root),
            *supplement_components(repo_root, payload_dir),
        ],
    )


def _digest(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()


def flag_report(components: list[Component]) -> str:
    """Two tables: risky licenses, then components that ship with no license text."""
    flagged = sorted(
        (c for c in components if c.category in FLAGGED),
        key=lambda c: (-RANK[c.category], c.ecosystem, c.name.lower()),
    )
    lines = [
        "## Copyleft, non-commercial and unidentified licenses",
        "",
        "| Component | Ecosystem | Version | License | Category | Note |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {c.name} | {c.ecosystem} | {c.version} | {c.license or 'UNKNOWN'} | {c.category} | {c.note} |"
        for c in flagged
    ]
    textless = sorted(
        (c for c in components if not c.license_texts and c.ecosystem != "bundled"),
        key=lambda c: (c.ecosystem, c.name.lower()),
    )
    lines += [
        "",
        "## License identified but no license text file in the distribution",
        "",
        "| Component | Ecosystem | Version | License |",
        "|---|---|---|---|",
    ]
    lines += [f"| {c.name} | {c.ecosystem} | {c.version} | {c.license or 'UNKNOWN'} |" for c in textless]
    return "\n".join(lines) + "\n"


def render_licenses(components: list[Component]) -> str:
    out: list[str] = [
        "THIRD-PARTY SOFTWARE LICENSES FOR OPEN DJ",
        "=" * 41,
        "",
        "Open DJ is licensed under the Apache License 2.0 (see LICENSE). The app bundles the",
        "third-party components below. Each keeps its own license; the identifier and the",
        "license texts its distribution carries follow. Generated by",
        "scripts/third_party_licenses.py from the shipped payload; do not edit by hand.",
        "",
        f"Components: {len(components)}",
        "",
        "COMPONENTS",
        "-" * 10,
    ]
    for c in components:
        out.append(f"{c.name} {c.version} [{c.ecosystem}] -- {c.license or 'UNKNOWN (see texts)'}")
        if c.note:
            out.append(f"    note: {c.note}")
    out += ["", "LICENSE TEXTS", "-" * 13, ""]
    grouped: dict[str, tuple[str, list[str]]] = {}
    for c in components:
        for _name, text in c.license_texts:
            if not text:
                continue
            _, members = grouped.setdefault(_digest(text), (text, []))
            label = f"{c.name} {c.version}"
            if label not in members:
                members.append(label)
    for text, members in sorted(grouped.values(), key=lambda pair: pair[1][0].lower()):
        out += ["=" * 78, "Applies to: " + ", ".join(members), "=" * 78, text, ""]
    out += ["NOTICES REQUIRED BY APACHE-2.0 SECTION 4(d) AND SIMILAR", "-" * 52, ""]
    notices: dict[str, tuple[str, list[str]]] = {}
    for c in components:
        for _name, text in c.notices:
            if text:
                _, members = notices.setdefault(_digest(text), (text, []))
                members.append(f"{c.name} {c.version}")
    for text, members in sorted(notices.values(), key=lambda pair: pair[1][0].lower()):
        out += ["=" * 78, "From: " + ", ".join(members), "=" * 78, text, ""]
    return "\n".join(out) + "\n"


def unreviewed_textless_components(components: list[Component]) -> list[Component]:
    """Components with no non-empty license text, excluding `KNOWN_TEXTLESS`.

    A NOTICE is attribution, not the license text itself (Apache-2.0 section 4(d) notices are additive,
    not a substitute for the license terms), so it does not excuse a missing license text (Sol P1,
    PR #4853 r4167612998). An empty-string text entry is likewise not real content: `any(... .strip()
    ...)` catches it even though `c.license_texts` itself is a non-empty list. Pulled out of
    `write_payload_license_files` so the guard is testable without staging a full fake payload.
    """
    return sorted(
        (
            c
            for c in components
            if not any(text.strip() for _, text in c.license_texts)
            and (c.ecosystem, c.name) not in KNOWN_TEXTLESS
        ),
        key=lambda c: (c.ecosystem, c.name.lower()),
    )


def require_license_texts(components: list[Component]) -> None:
    """Raise unless every component carries a license text or a reviewed exception."""
    if unreviewed := unreviewed_textless_components(components):
        names = ", ".join(f"{c.ecosystem}:{c.name} {c.version}" for c in unreviewed)
        raise LicenseInventoryError(
            f"{len(unreviewed)} component(s) have no non-empty license text: {names}. Stage the missing text, "
            "or add a human-reviewed entry (scripts/license_mirrors.py) explaining why none ships."
        )


def write_payload_license_files(repo_root: Path, payload_dir: Path) -> dict[str, object]:
    """Stage THIRD-PARTY-LICENSES.txt, the flag report, LICENSE and NOTICE.

    Raises (never returns a partial result) when any inventory cannot be
    measured, so the dmg build fails rather than shipping without attribution.
    """
    components = collect_all(repo_root, payload_dir)
    require_license_texts(components)
    (payload_dir / LICENSES_FILE_NAME).write_text(render_licenses(components), encoding="utf-8", newline="\n")
    (payload_dir / REPORT_FILE_NAME).write_text(flag_report(components), encoding="utf-8", newline="\n")
    for name in (ROOT_LICENSE_FILE_NAME, NOTICE_FILE_NAME):
        shutil.copyfile(repo_root / name, payload_dir / name)
    return {
        "file": LICENSES_FILE_NAME,
        "components": len(components),
        "by_ecosystem": {e: sum(1 for c in components if c.ecosystem == e) for e in sorted({c.ecosystem for c in components})},
        "flagged": sum(1 for c in components if c.category in FLAGGED),
        "sha256": hashlib.sha256((payload_dir / LICENSES_FILE_NAME).read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", type=Path, required=True, help="a staged payload dir (or one with pylib/, runtime/, models/)")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--flags", action="store_true", help="print the copyleft/non-commercial/unknown table and exit")
    args = parser.parse_args(argv)
    if args.flags:
        sys.stdout.write(flag_report(collect_all(args.repo_root, args.payload)))
        return 0
    print(json.dumps(write_payload_license_files(args.repo_root, args.payload), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
