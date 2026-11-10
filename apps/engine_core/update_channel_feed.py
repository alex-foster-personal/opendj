"""Platform keys, semver comparison, manifest fetch and update-check resolution.

Part of the update channel split out of one oversized module (#9688). Import
from ``apps.engine_core.update_channel``, the public facade, not from here.
"""

from __future__ import annotations

import os
import platform

import httpx
import yaml

from apps.engine_core.build_info import (
    BuildIdentity,
)
from apps.engine_core.update_channel_contract import (
    _ARCH_KEYS,
    _OS_KEYS,
    CHECKED_FEED_FILE,
    ELECTRON_FEED_FILE,
    FEED_URL_ENV,
    REQUEST_TIMEOUT_S,
    UPDATE_FEED_URL,
    UpdateCheckError,
    UpdateCheckOut,
    UpdateStatus,
    facade,
)
from apps.shared.semver import Semver as _Version
from apps.shared.semver import parse_semver as _parse_semver


# ----- platform -----------------------------------------------------------
def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """This machine's Tauri platform key, or a fault naming what it saw.

    Refuses rather than guessing: a wrong key would read a manifest entry
    built for a different architecture, and on macOS that is the difference
    between an arm64 and an x86_64 bundle.
    """
    system_name = platform.system() if system is None else system
    machine_name = platform.machine() if machine is None else machine
    os_key = _OS_KEYS.get(system_name)
    arch_key = _ARCH_KEYS.get(machine_name.lower())
    if os_key is None or arch_key is None:
        raise UpdateCheckError(
            "platform-unsupported",
            f"this machine reports system={system_name!r} machine="
            f"{machine_name!r}, which does not map to a Tauri platform key",
        )
    return f"{os_key}-{arch_key}"


# ----- semver -------------------------------------------------------------
def parse_version(raw: str, source: str) -> _Version:
    """Strict SemVer precedence, or the existing named malformed-version fault."""
    try:
        return _parse_semver(raw)
    except ValueError as exc:
        raise UpdateCheckError(
            "manifest-malformed",
            f"{source} is {raw!r}, which is not a semver version, so this build cannot be ordered against the channel",
        ) from exc


def compare_versions(current: str, available: str) -> UpdateStatus:
    """Which way round the running build and the channel sit.

    Mirrors what ``tauri-plugin-updater`` will do with the same two strings:
    strictly greater means an update. Equal is up to date. A running build
    NEWER than the channel is its own answer -- that is a developer build, and
    calling it "up to date" would hide the fact that the channel is behind.
    """
    here = parse_version(current, "the running app's version")
    there = parse_version(available, "the channel manifest's 'version'")
    if there > here:
        return "update-available"
    if there == here:
        return "up-to-date"
    return "ahead-of-channel"


# ----- manifest -----------------------------------------------------------
def update_endpoint(env: dict[str, str] | None = None) -> str:
    """The feed this engine reads: ``OPENDJ_UPDATE_FEED_URL`` when set, else the public one."""
    override = (os.environ if env is None else env).get(FEED_URL_ENV, "").strip()
    base = override.rstrip("/") if override else UPDATE_FEED_URL
    return f"{base}/{CHECKED_FEED_FILE}"


def _is_electron_feed(endpoint: str) -> bool:
    return endpoint.split("?", 1)[0].endswith((".yml", ".yaml"))


def _fetch_manifest(endpoint: str, client: httpx.Client) -> dict[str, object]:
    electron = _is_electron_feed(endpoint)
    try:
        response = client.get(
            endpoint,
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
            headers={"accept": "text/yaml" if electron else "application/json"},
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL is not an HTTPError: a malformed OPENDJ_UPDATE_FEED_URL
        # must still be a named fault, never an unhandled 500.
        raise UpdateCheckError(
            "endpoint-unreachable",
            f"{endpoint} could not be reached: {exc}",
        ) from exc
    if response.status_code != 200:
        hint = ""
        if response.status_code == 404:
            hint = " See docs/auto-update.md for the public release host."
        raise UpdateCheckError(
            "endpoint-refused",
            f"{endpoint} answered HTTP {response.status_code}.{hint}",
        )
    kind = "YAML" if electron else "JSON"
    try:
        manifest = yaml.safe_load(response.text) if electron else response.json()
    except (ValueError, yaml.YAMLError) as exc:
        raise UpdateCheckError(
            "manifest-malformed",
            f"{endpoint} answered 200 with a body that is not {kind}: {exc}",
        ) from exc
    if not isinstance(manifest, dict):
        raise UpdateCheckError(
            "manifest-malformed",
            f"{endpoint} answered 200 with a {kind} {type(manifest).__name__}, expected an object",
        )
    return manifest


#: electron-updater's macOS zip suffix per platform key (electron-builder's
#: ``${productName}-${version}-${arch}-mac.zip`` naming).
_ELECTRON_ZIP_SUFFIX: dict[str, str] = {
    "darwin-aarch64": "-arm64-mac.zip",
    "darwin-x86_64": "-x64-mac.zip",
}


def _electron_entry(manifest: dict[str, object], key: str) -> dict[str, object]:
    """The ``files`` entry this Mac would install, as a ``url``/``signature`` pair.

    electron-updater verifies the zip's sha512 from this entry, then
    Squirrel.Mac checks the bundle's code signature; an entry with no sha512
    would be refused at install time, so it is refused here.
    """
    suffix = _ELECTRON_ZIP_SUFFIX.get(key)
    if suffix is None:
        raise UpdateCheckError(
            "platform-unsupported",
            f"{ELECTRON_FEED_FILE} is the macOS feed and carries no build for '{key}'",
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise UpdateCheckError(
            "manifest-malformed",
            "the channel feed has no 'files' list, so it was not written for electron-updater",
        )
    urls = [f.get("url") for f in files if isinstance(f, dict)]
    entry = next(
        (f for f in files if isinstance(f, dict) and str(f.get("url", "")).endswith(suffix)),
        None,
    )
    if entry is None:
        raise UpdateCheckError(
            "platform-unsupported",
            f"the channel feed carries no '*{suffix}' build for '{key}'. It offers: {urls}",
        )
    if not isinstance(entry.get("sha512"), str) or entry["sha512"] == "":
        raise UpdateCheckError(
            "manifest-malformed",
            f"the '{entry['url']}' entry has no sha512. electron-updater "
            "would refuse it at install time, so it is refused here.",
        )
    return {"url": entry["url"], "signature": entry["sha512"]}


def _platform_entry(manifest: dict[str, object], key: str) -> dict[str, object]:
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict):
        raise UpdateCheckError(
            "manifest-malformed",
            "the channel manifest has no 'platforms' object, so it was not written by the Tauri bundler",
        )
    entry = platforms.get(key)
    if not isinstance(entry, dict):
        raise UpdateCheckError(
            "platform-unsupported",
            f"the channel manifest carries no '{key}' build. It offers: {sorted(platforms)}",
        )
    if not isinstance(entry.get("url"), str) or entry["url"] == "":
        raise UpdateCheckError(
            "manifest-malformed",
            f"the '{key}' entry has no download url",
        )
    if not isinstance(entry.get("signature"), str) or entry["signature"] == "":
        raise UpdateCheckError(
            "manifest-malformed",
            f"the '{key}' entry has no signature. An unsigned entry would be "
            "refused by the updater at install time, so it is refused here.",
        )
    return entry


def _manifest_version(manifest: dict[str, object]) -> str:
    raw_version = manifest.get("version")
    if not isinstance(raw_version, str):
        raise UpdateCheckError(
            "manifest-malformed",
            "the channel manifest has no string 'version' field",
        )
    return raw_version


# ----- resolution ---------------------------------------------------------
def resolve_update_check(
    identity: BuildIdentity,
    *,
    client: httpx.Client,
    endpoint: str | None = None,
    key: str | None = None,
) -> UpdateCheckOut:
    """Answer the question, or say precisely why it could not be answered.

    Takes the resolved build identity rather than re-reading it: the running
    build's version must be the one ``/build-info`` already published, or the
    two readouts on the same screen could disagree.
    """
    endpoint = update_endpoint() if endpoint is None else endpoint
    try:
        # read via the facade: tests patch it there
        resolved_key = facade().platform_key() if key is None else key
    except UpdateCheckError as exc:
        return UpdateCheckOut(
            status=exc.status,
            endpoint=endpoint,
            platform_key="?",
            detail=exc.message,
        )

    if identity.info is None:
        return UpdateCheckOut(
            status="identity-unavailable",
            endpoint=endpoint,
            platform_key=resolved_key,
            detail=(
                "this build cannot state its own identity, so it cannot be "
                f"compared against the channel: {identity.failure}"
            ),
        )

    info = identity.info
    # app_version is the number the updater compares. A payload built before
    # that field existed has None, and guessing it would mean comparing a
    # made-up version against a real one.
    current_version = info.app_version
    base = UpdateCheckOut(
        status="up-to-date",
        endpoint=endpoint,
        platform_key=resolved_key,
        current_version=current_version,
        current_git_sha=info.git_sha,
        current_built_at_utc=info.built_at_utc,
    )
    if current_version is None or current_version.strip() == "":
        return base.model_copy(
            update={
                "status": "identity-unavailable",
                "detail": (
                    "this build's manifest carries no app_version, so there is no number to compare against the channel"
                ),
            }
        )

    electron = _is_electron_feed(endpoint)
    try:
        manifest = _fetch_manifest(endpoint, client)
        entry = (_electron_entry if electron else _platform_entry)(manifest, resolved_key)
        raw_version = _manifest_version(manifest)
        verdict = compare_versions(current_version, raw_version)
    except UpdateCheckError as exc:
        return base.model_copy(update={"status": exc.status, "detail": exc.message})

    notes = manifest.get("releaseNotes" if electron else "notes")
    published = manifest.get("releaseDate" if electron else "pub_date")
    # The case the semver rule cannot see: same number, different build. The
    # updater will not act on it, and the reader is told rather than reassured.
    same_build_drift = verdict == "up-to-date" and _mentions_other_sha(notes, info.git_sha_full, info.git_sha)
    return base.model_copy(
        update={
            "status": verdict,
            "available_version": raw_version,
            "notes": notes if isinstance(notes, str) else None,
            "published_at": published if isinstance(published, str) else None,
            "same_version_different_build": same_build_drift,
            "detail": (
                f"the channel offers {raw_version} for {resolved_key} at {entry['url']}"
                if verdict == "update-available"
                else None
            ),
        }
    )


def _mentions_other_sha(notes: object, git_sha_full: str, git_sha: str) -> bool:
    """Does the release name a build that is not the one running?

    Deliberately conservative. Release notes are free text, so the only
    confident reading is "notes exist, and they do NOT contain this build's
    sha". Absent notes prove nothing and report nothing.
    """
    if not isinstance(notes, str) or notes.strip() == "":
        return False
    return git_sha_full not in notes and git_sha not in notes
