"""Canonical real Mach-O fixtures, verified by version and checksum before use.

AGENTS.md forbids stubs and fabricated application state in tests, permits
captured real artifacts consumed through production paths, and requires
canonical fixtures to be verified "by version, manifest, and checksum before
use". These are the captured artifacts: four files emitted by Apple's own
linker, each a loadable Mach-O that otool -L reads and codesign would accept.
An earlier revision of these tests constructed 32-byte Mach-O headers by hand,
which was a stub however faithfully it matched the spec.

``fixture_dir`` is a parameter of the production API rather than a module
global to be swapped at runtime, so a test that needs a mutated fixture points
the real verifier at a disposable copy instead of monkeypatching this module.

See tests/fixtures/macho/README.md.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

FIXTURE_DIR: Path = Path(__file__).resolve().parents[1] / "fixtures/macho"
MANIFEST_NAME: str = "manifest.json"
# Bumped when the manifest's SHAPE changes, never when a fixture is recaptured.
# An unknown version fails closed: a verifier that reads a format it does not
# understand cannot honestly report that a fixture matched.
SUPPORTED_MANIFEST_VERSION: int = 1

# The four shapes a staged payload carries. Keyed by the role each plays, so a
# test names what it is exercising rather than a filename.
EXECUTABLE: str = "opendj-probe-executable"
DYLIB: str = "libopendj-probe.dylib"
BUNDLE_SO: str = "opendj-probe-bundle.so"
DYLIB_NAMED_SO: str = "opendj-probe-dylib-named.so"


def load_manifest(fixture_dir: Path = FIXTURE_DIR) -> dict:
    """The manifest, after its version is one this module understands."""
    path = fixture_dir / MANIFEST_NAME
    if not path.is_file():
        raise AssertionError(f"no fixture manifest at {path}")
    manifest = json.loads(path.read_text())
    version = manifest.get("version")
    if version != SUPPORTED_MANIFEST_VERSION:
        raise AssertionError(
            f"{path} declares manifest version {version!r}, this module supports "
            f"{SUPPORTED_MANIFEST_VERSION}. Failing closed rather than reading a "
            "format whose meaning is not known: a checksum compared against a "
            "field that may have moved is not a verification."
        )
    return manifest


def verified(name: str, fixture_dir: Path = FIXTURE_DIR) -> Path:
    """The canonical fixture, after version and checksum both match.

    Fails loudly rather than returning an unverified path: a fixture that
    silently drifted would make every assertion built on it meaningless.
    """
    files = load_manifest(fixture_dir)["files"]
    if name not in files:
        raise AssertionError(
            f"{name} is not in {fixture_dir / MANIFEST_NAME}; known: {sorted(files)}"
        )
    path = fixture_dir / name
    if not path.is_file():
        raise AssertionError(f"canonical fixture missing: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != files[name]["sha256"]:
        raise AssertionError(
            f"{name} does not match its manifest checksum. Canonical fixtures are "
            "immutable during feature work; regenerating one to make a test pass is "
            f"forbidden. expected {files[name]['sha256']}, got {digest}"
        )
    return path


def hydrate(
    name: str,
    destination: Path,
    *,
    mode: int = 0o755,
    fixture_dir: Path = FIXTURE_DIR,
) -> Path:
    """Copy a verified fixture to a disposable path the caller may mutate."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(verified(name, fixture_dir), destination)
    destination.chmod(mode)
    return destination


def verify_all(fixture_dir: Path = FIXTURE_DIR) -> Path:
    """Every file the manifest lists, verified, before the set is used as a set.

    ``verified`` establishes provenance one file at a time, which is the right
    grain when a caller names the fixture it wants. A caller that takes the
    WHOLE directory names none of them, so nothing would check the other three.
    Verifying every listed entry keeps the fail-closed contract at the same
    grain as the thing being handed out.
    """
    for name in load_manifest(fixture_dir)["files"]:
        verified(name, fixture_dir)
    return fixture_dir


def disposable_fixture_dir(
    destination: Path,
    *,
    fixture_dir: Path = FIXTURE_DIR,
) -> Path:
    """A writable copy of the whole canonical set, for tests about the verifier.

    Exists so a test can corrupt a fixture and watch the REAL verifier refuse
    it, without reassigning this module's globals. The canonical directory is
    never touched.

    The source is verified IN FULL before it is copied. A test that mutates a
    copy is asserting about evidence, so copying first would hand it evidence
    whose provenance was never established -- a drifted canonical fixture would
    ride into the copy unnoticed, which is the exact failure this module exists
    to make impossible.
    """
    shutil.copytree(verify_all(fixture_dir), destination)
    return destination
