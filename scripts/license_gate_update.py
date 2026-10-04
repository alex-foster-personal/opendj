"""Network half of scripts/license_gate.py: fetch licenses for new name@version entries.

``python -m scripts.license_gate update`` calls :func:`run_update`. It reads PyPI JSON,
the npm registry, and ``cargo metadata`` (crates.io as a fallback), normalizes each
license to an SPDX-style expression, and rewrites licenses/register.json one entry per
line. Nothing here runs in the CI gate.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.license_gate import (
    ECOSYSTEMS,
    EXIT_OK,
    POLICY,
    REGISTER,
    UNKNOWN_LICENSE,
    GateError,
    _tokens,
    load_json,
    locked_packages,
)

# ----- update (network) -------------------------------------------------------

_CLASSIFIER_SPDX = {
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "Apache Software License": "Apache-2.0",
    # The classifier does not say which BSD; record that honestly.
    "BSD License": "LicenseRef-BSD",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "zlib/libpng License": "Zlib",
    "Boost Software License 1.0 (BSL-1.0)": "BSL-1.0",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0-only",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "GNU Affero General Public License v3": "AGPL-3.0-only",
    "GNU Affero General Public License v3 or later (AGPLv3+)": "AGPL-3.0-or-later",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
}

_TEXT_SPDX = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit-cmu": "MIT-CMU",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "bsd": "LicenseRef-BSD",
    "bsd license": "LicenseRef-BSD",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "modified bsd": "BSD-3-Clause",
    "simplified bsd": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "isc license": "ISC",
    "psf": "PSF-2.0",
    "psf license": "PSF-2.0",
    "mpl 2.0": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "public domain": "LicenseRef-Public-Domain",
    "unlicense license": "Unlicense",
    "zlib/libpng": "Zlib",
}

_SPDXISH = re.compile(r"^[A-Za-z0-9.+\-() ]+$")


def _spdxish(text: str) -> str | None:
    text = text.strip()
    if not text or "\n" in text or len(text) > 120:
        return None
    mapped = _TEXT_SPDX.get(text.lower().rstrip("."))
    if mapped:
        return mapped
    if not _SPDXISH.match(text):
        return None
    atoms = [t for t in _tokens(text) if t not in ("(", ")") and t.upper() not in ("AND", "OR")]
    if any(" " in re.split(r"\s+WITH\s+", a, flags=re.IGNORECASE)[0].strip() for a in atoms):
        return None
    return text


def normalize_pypi(info: dict) -> str:
    expr = (info.get("license_expression") or "").strip()
    if expr:
        return expr
    found = []
    for c in info.get("classifiers") or []:
        if c.startswith("License :: "):
            tail = c.split(" :: ")[-1]
            if tail in ("OSI Approved", "Other/Proprietary License"):
                if tail == "Other/Proprietary License":
                    found.append("LicenseRef-Proprietary")
                continue
            found.append(_CLASSIFIER_SPDX.get(tail, f"LicenseRef-{re.sub(r'[^A-Za-z0-9.]+', '-', tail).strip('-')}"))
    if found:
        return " OR ".join(dict.fromkeys(found))
    return _spdxish(info.get("license") or "") or UNKNOWN_LICENSE


def normalize_npm(meta: dict) -> str:
    lic = meta.get("license")
    if isinstance(lic, dict):
        lic = lic.get("type")
    if not lic and isinstance(meta.get("licenses"), list):
        types = [x.get("type") if isinstance(x, dict) else x for x in meta["licenses"]]
        lic = " OR ".join(t for t in types if t)
    if not lic or not isinstance(lic, str) or lic.upper().startswith("SEE LICENSE"):
        return UNKNOWN_LICENSE
    lic = lic.strip()
    if lic.startswith("(") and lic.endswith(")") and lic.count("(") == 1:
        lic = lic[1:-1]
    return lic


def normalize_cargo(license_field: str | None) -> str:
    if not license_field:
        return UNKNOWN_LICENSE
    return re.sub(r"\s*/\s*", " OR ", license_field.strip())


def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "music-dj-tools-license-gate", "Accept": "application/json"}
    )
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
    return None


def fetch_pypi(name: str, version: str) -> str:
    # A local version (torch 2.14.0+cpu) lives on the vendor index, not PyPI;
    # its license is the public release's.
    data = _get_json(f"https://pypi.org/pypi/{name}/{version.split('+', 1)[0]}/json")
    return normalize_pypi(data["info"]) if data else UNKNOWN_LICENSE


def fetch_npm(name: str, version: str) -> str:
    data = _get_json(f"https://registry.npmjs.org/{name.replace('/', '%2F')}/{version}")
    return normalize_npm(data) if data else UNKNOWN_LICENSE


def cargo_metadata_licenses(root: Path, lockfiles: Iterable[str]) -> dict[str, str]:
    """name@version -> license via `cargo metadata` (offline first, then online)."""
    out: dict[str, str] = {}
    for rel in lockfiles:
        manifest = (root / rel).parent / "Cargo.toml"
        if not manifest.is_file():
            continue
        cargo = shutil.which("cargo")
        if cargo is None:
            raise GateError(f"cargo not found on PATH; cannot read licenses for {rel}")
        for extra in (["--offline"], []):
            proc = subprocess.run(
                [cargo, "metadata", "--format-version", "1", "--locked", *extra, "--manifest-path", str(manifest)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                for pkg in json.loads(proc.stdout)["packages"]:
                    if pkg.get("source"):
                        out[f"{pkg['name']}@{pkg['version']}"] = normalize_cargo(pkg.get("license"))
                break
    return out


def fetch_crates_io(name: str, version: str) -> str:
    data = _get_json(f"https://crates.io/api/v1/crates/{name}/{version}")
    return normalize_cargo(data["version"].get("license")) if data else UNKNOWN_LICENSE


def write_register(root: Path, register: dict) -> None:
    """One entry per line, sorted, so concurrent PRs conflict only on the lines they touch."""
    parts = ["{"]
    ecos = [e for e in ECOSYSTEMS if e in register]
    for i, eco in enumerate(ecos):
        parts.append(f'  "{eco}": {{')
        items = sorted(register[eco].items())
        for j, (key, lic) in enumerate(items):
            comma = "," if j < len(items) - 1 else ""
            parts.append(f"    {json.dumps(key)}: {json.dumps(lic)}{comma}")
        parts.append("  }" + ("," if i < len(ecos) - 1 else ""))
    parts.append("}")
    (root / REGISTER).write_text("\n".join(parts) + "\n", encoding="utf-8")


def run_update(root: Path, refresh: bool) -> int:
    policy = load_json(root, POLICY)
    old = {} if refresh or not (root / REGISTER).is_file() else load_json(root, REGISTER)
    locked = locked_packages(root, policy)
    wanted: dict[str, set[tuple[str, str]]] = {eco: set() for eco in ECOSYSTEMS}
    for pkg in locked:
        wanted[pkg.ecosystem].add((pkg.name, pkg.version))

    new: dict[str, dict[str, str]] = {eco: {} for eco in ECOSYSTEMS}
    todo: dict[str, list[tuple[str, str]]] = {eco: [] for eco in ECOSYSTEMS}
    for eco, items in wanted.items():
        for name, version in sorted(items):
            key = f"{name}@{version}"
            if key in old.get(eco, {}):
                new[eco][key] = old[eco][key]
            else:
                todo[eco].append((name, version))

    if todo["cargo"]:
        cargo_files = [e["path"] for e in policy["lockfiles"] if e["format"] == "cargo"]
        meta = cargo_metadata_licenses(root, cargo_files)
        remaining = []
        for name, version in todo["cargo"]:
            key = f"{name}@{version}"
            if key in meta:
                new["cargo"][key] = meta[key]
            else:
                remaining.append((name, version))
        todo["cargo"] = remaining

    fetchers = {"pypi": fetch_pypi, "npm": fetch_npm, "cargo": fetch_crates_io}
    for eco in ECOSYSTEMS:
        if not todo[eco]:
            continue
        workers = 1 if eco == "cargo" else 16  # crates.io asks for one request per second
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda nv, f=fetchers[eco]: f(*nv), todo[eco]))
        for (name, version), lic in zip(todo[eco], results, strict=True):
            new[eco][f"{name}@{version}"] = lic
        print(f"[update] {eco}: fetched {len(todo[eco])}")

    write_register(root, {eco: v for eco, v in new.items() if v})
    overridden = {(o["ecosystem"], o["name"]) for o in policy.get("overrides", [])}
    unknown = sorted(
        f"{eco} {k}"
        for eco, v in new.items()
        for k, lic in v.items()
        if lic == UNKNOWN_LICENSE and (eco, k.rsplit("@", 1)[0]) not in overridden
    )
    for item in unknown:
        print(f"[update] no license metadata for {item}: add a reasoned override to {POLICY}")
    print(f"[update] wrote {REGISTER}; now run `python -m scripts.license_gate check`")
    return EXIT_OK
