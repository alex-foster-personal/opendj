"""Construct the shipping app tag and immutable updater asset identity.

Supersedes: duplicated v-version tag and URL spelling in scripts/release.sh.
"""

from __future__ import annotations

import argparse
from urllib.parse import quote

from scripts.release_semver import parse_semver, read_configured_version


def release_tag(version: str) -> str:
    """Use the independent app namespace with the configured SemVer intact."""
    parse_semver(version, source="shipping app version")
    if version != version.strip() or version.startswith("v"):
        raise ValueError("shipping app version must be an unprefixed SemVer")
    return f"app-v{version}"


def release_asset_url(repository: str, version: str, asset: str) -> str:
    """Name the same immutable tag used to upload the signed updater asset."""
    tag = quote(release_tag(version), safe="")
    return f"https://github.com/{repository}/releases/download/{tag}/{quote(asset, safe='')}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    print(release_tag(read_configured_version(args.config)))


if __name__ == "__main__":
    main()
