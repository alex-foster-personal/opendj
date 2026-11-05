"""Publish a built Electron release to the public updater host, then prove the public feeds serve it.

Spec: ``(private path)`` "Delivery plan" step 5 (parity
matrix rows 60 and 62). Input is the ``--out`` directory of a finished
``scripts/electron_release.py`` run; this never builds or signs.

One run uploads the six assets that build wrote (``OpenDJ-<v>-arm64.dmg``,
``OpenDJ-<v>-arm64-mac.zip``, ``latest-mac.yml``, ``OpenDJ-<v>-aarch64.app.tar.gz``
and its ``.sig``, ``latest.json``) to ONE new release ``app-v<v>`` on
``alex-foster-personal/issue-assets``, the host ``scripts/release.sh`` publishes
Tauri builds to. It then downloads ``releases/latest/download/latest-mac.yml``
and ``latest.json`` with no credentials and requires them to be byte-identical
to the local files, and downloads the zip and the bridge archive they name and
requires their sha512 and size to match. GitHub's CDN can lag a fresh upload,
so the public check retries until a deadline, then fails naming what differed.

Immutability, as in ``release.sh``: a tag that already exists is never
replaced. A retry on an existing tag uploads nothing and only re-runs the
public check, so it passes only when the published bytes are these bytes.

Acceptance:

- [if] the build is a proof, or any asset's sha512 or size differs from
  release-evidence.json [then] publishing refuses before any upload [else stop].
- [if] latest.json does not point at the immutable app-v<v> tag, or its
  signature is not the .sig file [then] publishing refuses [else stop].
- [if] the version is not above the latest published release [then]
  publishing refuses before any upload [else stop].
- [if] the release tag already exists [then] nothing is uploaded and only the
  public check runs [else stop].
- [if] the public feeds or the files they name still differ after the retry
  deadline [then] the run fails naming every difference [else stop].

Usage::

    uv run --no-sync python -m scripts.electron_publish --from <electron_release --out dir>

-Claude
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from apps.shared.semver import parse_semver
from scripts import ship_electron
from scripts.electron_release import (
    PUBLIC_REPO,
    UPDATE_FEED_URL,
    Artifacts,
    ReleaseError,
    check_feed_matches,
    release_notes,
    sha512_b64,
)
from scripts.release_identity import release_asset_url, release_tag
from scripts.release_semver import latest_published_version, require_semver_bump

#: The public check retries this many times, this many seconds apart (CDN lag on a fresh upload).
PUBLIC_CHECK_ATTEMPTS = 20
PUBLIC_CHECK_DELAY_S = 15.0
FETCH_TIMEOUT_S = 120.0

Fetch = Callable[[str], bytes]
Gh = Callable[[Sequence[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class BuiltRelease:
    version: str
    git_sha: str
    notes: str
    art: Artifacts

    @property
    def tag(self) -> str:
        return release_tag(self.version)

    @property
    def assets(self) -> list[Path]:
        a = self.art
        return [a.dmg, a.zip, a.feed, a.archive, a.signature, a.bridge]


# ----- local checks ---------------------------------------------------------------


def check_bridge_matches(bridge_text: str, art: Artifacts) -> None:
    """Raise unless latest.json names this version, the immutable tag's archive and the .sig bytes."""
    bridge = json.loads(bridge_text)
    entry = (bridge.get("platforms") or {}).get("darwin-aarch64") or {}
    if bridge.get("version") != art.version:
        raise ReleaseError(f"latest.json version is {bridge.get('version')!r}, expected {art.version}")
    expected_url = release_asset_url(PUBLIC_REPO, art.version, art.archive.name)
    if entry.get("url") != expected_url:
        raise ReleaseError(f"latest.json url is {entry.get('url')!r}, expected {expected_url}")
    if entry.get("signature") != art.signature.read_text(encoding="utf-8").strip():
        raise ReleaseError(f"latest.json signature is not the contents of {art.signature.name}")


def load_built_release(out: Path) -> BuiltRelease:
    """Read release-evidence.json and prove every asset on disk is the one the build verified."""
    art_probe = Artifacts(out, "0")
    if not art_probe.evidence.is_file():
        raise ReleaseError(f"{art_probe.evidence} is missing; run scripts.electron_release --out {out} first")
    evidence = json.loads(art_probe.evidence.read_text(encoding="utf-8"))
    if evidence.get("proof") is not False:
        raise ReleaseError(f"{out} is a proof build (proof={evidence.get('proof')!r}); only a release is published")
    version, git_sha = evidence.get("version"), evidence.get("git_sha")
    if not isinstance(version, str) or not isinstance(git_sha, str) or len(git_sha) != 40:
        raise ReleaseError(f"release-evidence.json lacks a version or a full git sha: {version!r} {git_sha!r}")
    parse_semver(version)
    built = BuiltRelease(version, git_sha, str(evidence.get("notes") or ""), Artifacts(out, version))
    recorded = evidence.get("artifacts") or {}
    for path in built.assets:
        if not path.is_file():
            raise ReleaseError(f"{path.name} is missing from {out}")
        want = recorded.get(path.name)
        have = {"sha512": sha512_b64(path), "size": path.stat().st_size}
        if want != have:
            raise ReleaseError(f"{path.name} is not the verified build: evidence {want}, on disk {have}")
    check_feed_matches(built.art.feed.read_text(encoding="utf-8"), built.art.zip, version)
    check_bridge_matches(built.art.bridge.read_text(encoding="utf-8"), built.art)
    return built


# ----- GitHub ---------------------------------------------------------------------


def run_gh(argv: Sequence[str]) -> subprocess.CompletedProcess:
    """gh by resolved path (AGENTS.md "Launch programs by resolved path"); a missing gh is a named ReleaseError."""
    try:
        gh = ship_electron.resolve_tool("gh")
    except ship_electron.ShipError as exc:
        raise ReleaseError(str(exc)) from exc
    return subprocess.run([gh, *argv], capture_output=True, text=True, check=False)


def release_exists(tag: str, gh: Gh = run_gh) -> bool:
    """True for a published release. A DRAFT under the tag is an interrupted earlier upload: it is refused by
    name, never resumed or replaced, because its asset set is unknown (Sentry review on #8789)."""
    done = gh(["release", "view", tag, "--repo", PUBLIC_REPO, "--json", "tagName,isDraft"])
    if done.returncode == 0:
        if json.loads(done.stdout).get("isDraft") is not False:
            raise ReleaseError(
                f"{tag} on {PUBLIC_REPO} is a draft, an interrupted upload; inspect it with "
                f"`gh release view {tag} --repo {PUBLIC_REPO}` and delete it before publishing again"
            )
        return True
    if "release not found" in (done.stderr + done.stdout).lower():
        return False
    raise ReleaseError(f"could not read release {tag} on {PUBLIC_REPO}: {(done.stderr or done.stdout).strip()}")


def create_release(built: BuiltRelease, gh: Gh = run_gh) -> None:
    """One release, every asset, marked latest so ``releases/latest/download`` serves it."""
    argv = [
        "release",
        "create",
        built.tag,
        *(str(p) for p in built.assets),
        "--repo",
        PUBLIC_REPO,
        "--title",
        f"Open DJ {built.version}",
        "--notes",
        release_notes(built.notes, built.git_sha),
        "--latest",
    ]
    print(f"[RUN] gh release create {built.tag} ({len(built.assets)} assets) --repo {PUBLIC_REPO}", flush=True)
    done = gh(argv)
    if done.returncode != 0:
        raise ReleaseError(f"gh release create {built.tag} failed: {(done.stderr or done.stdout).strip()}")


# ----- public check -------------------------------------------------------------


def fetch_public(url: str) -> bytes:
    """Unauthenticated GET, as an installed app makes it. An HTTP error is a mismatch to retry, not a crash."""
    request = urllib.request.Request(url, headers={"User-Agent": "opendj-electron-publish"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
        return response.read()


def _digest(data: bytes) -> dict:
    return {"sha512": base64.b64encode(hashlib.sha512(data).digest()).decode("ascii"), "size": len(data)}


def public_differences(built: BuiltRelease, fetch: Fetch) -> list[str]:
    """Every way the public feeds, or the files they name, differ from the local release. Empty means served."""
    art = built.art
    problems: list[str] = []
    feeds = {art.feed.name: art.feed, art.bridge.name: art.bridge}
    served: dict[str, bytes] = {}
    for name, local in feeds.items():
        url = f"{UPDATE_FEED_URL}/{name}"
        try:
            served[name] = fetch(url)
        except (OSError, http.client.HTTPException) as exc:
            problems.append(f"{url}: {exc}")
            continue
        if served[name] != local.read_bytes():
            problems.append(f"{url} serves other bytes than {local} (a stale or different release)")
    if problems:
        return problems
    mac_feed = yaml.safe_load(served[art.feed.name])
    bridge = json.loads(served[art.bridge.name])
    payloads = {
        f"{UPDATE_FEED_URL}/{mac_feed['path']}": art.zip,
        bridge["platforms"]["darwin-aarch64"]["url"]: art.archive,
    }
    for url, local in payloads.items():
        try:
            have = _digest(fetch(url))
        except (OSError, http.client.HTTPException) as exc:
            problems.append(f"{url}: {exc}")
            continue
        want = {"sha512": sha512_b64(local), "size": local.stat().st_size}
        if have != want:
            problems.append(f"{url} is {have}, expected {want}")
    return problems


def wait_until_public(
    built: BuiltRelease,
    fetch: Fetch = fetch_public,
    attempts: int = PUBLIC_CHECK_ATTEMPTS,
    delay_s: float = PUBLIC_CHECK_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    problems: list[str] = []
    for attempt in range(1, attempts + 1):
        problems = public_differences(built, fetch)
        if not problems:
            print(f"[OK] public feeds serve {built.version} (attempt {attempt}/{attempts})", flush=True)
            return
        print(f"[WAIT] attempt {attempt}/{attempts}: {problems[0]}", flush=True)
        if attempt < attempts:
            sleep(delay_s)
    raise ReleaseError(
        f"the public feeds did not serve {built.version} after {attempts} attempts:\n" + "\n".join(problems)
    )


def public_summary(built: BuiltRelease) -> list[str]:
    mac_feed = yaml.safe_load(built.art.feed.read_text(encoding="utf-8"))
    bridge = json.loads(built.art.bridge.read_text(encoding="utf-8"))
    return [
        f"latest-mac.yml: version {mac_feed['version']}, {mac_feed['path']}, "
        f"sha512 {mac_feed['sha512'][:16]}..., size {mac_feed['files'][0]['size']}",
        f"latest.json: version {bridge['version']}, {bridge['platforms']['darwin-aarch64']['url']}, "
        f"archive sha512 {sha512_b64(built.art.archive)[:16]}..., size {built.art.archive.stat().st_size}",
    ]


# ----- CLI ------------------------------------------------------------------------


def publish(
    out: Path, gh: Gh = run_gh, fetch: Fetch = fetch_public, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Publish ``out`` once, or validate the existing release; return the release URL."""
    built = load_built_release(out)
    if release_exists(built.tag, gh):
        print(
            f"[OK] {built.tag} already exists on {PUBLIC_REPO}; uploading nothing, checking what it serves", flush=True
        )
    else:
        require_semver_bump(built.version, latest_published_version(PUBLIC_REPO))
        create_release(built, gh)
    wait_until_public(built, fetch, sleep=sleep)
    for line in public_summary(built):
        print(f"[OK] {line}", flush=True)
    return f"https://github.com/{PUBLIC_REPO}/releases/tag/{built.tag}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="out", type=Path, required=True, help="a finished electron_release --out dir")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        url = publish(args.out.resolve())
    except (ReleaseError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] published and verified: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
