"""Discover Mixxx stable releases from the official downloads archive index."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from html.parser import HTMLParser

from apps.compatint.coverage import build_discovery_coverage
from apps.compatint.schema import (
    MIXXX_ARCHIVE_BASE_URL,
    MIXXX_VENDOR,
    DiscoveryContext,
    GapKind,
    MixxxDiscoveryResult,
    ReleaseGap,
    StableReleaseRecord,
)

_PRE_DIR = re.compile(r"^\.\./$")
_PRERELEASE_MARKERS = (
    "-beta",
    "-alpha",
    "-rc",
    "-svn",
    "beta",
    "alpha",
    "rc",
    "snapshot",
)


def _discovery_context(ctx: DiscoveryContext | None) -> DiscoveryContext:
    return ctx if ctx is not None else DiscoveryContext()


def _is_prerelease_identifier(identifier: str) -> bool:
    lowered = identifier.lower()
    return any(marker in lowered for marker in _PRERELEASE_MARKERS)


def _stable_folder_names(rows: Iterable[tuple[str, str | None]]) -> set[str]:
    return {folder for folder, _ in rows if is_stable_archive_entry(folder)}


def _looks_like_semver_release(identifier: str) -> bool:
    return bool(re.fullmatch(r"\d+\.\d+(?:\.\d+)?(?:[.-][\w]+)?", identifier))


def is_stable_archive_entry(folder_name: str) -> bool:
    name = folder_name.rstrip("/")
    if not _looks_like_semver_release(name):
        return False
    return not _is_prerelease_identifier(name)


def parse_archive_index_date(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    try:
        parsed = datetime.strptime(raw, "%d-%b-%Y")
    except ValueError:
        return None
    return parsed.date().isoformat()


class _ApacheIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_pre = False
        self._pre_chunks: list[str] = []
        self.rows: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "pre":
            self._in_pre = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "pre" and self._in_pre:
            self._in_pre = False
            self._parse_pre("".join(self._pre_chunks))
            self._pre_chunks.clear()

    def handle_data(self, data: str) -> None:
        if self._in_pre:
            self._pre_chunks.append(data)

    def _parse_pre(self, block: str) -> None:
        for line in block.splitlines():
            html_match = re.search(
                r'<a href="([^"]+)/">[^<]*</a>\s+(\d{2}-[A-Za-z]{3}-\d{4})',
                line,
            )
            if html_match:
                href = html_match.group(1)
                if _PRE_DIR.match(href):
                    continue
                self.rows.append((href.rstrip("/"), parse_archive_index_date(html_match.group(2))))
                continue
            text_match = re.match(
                r"^([\w][\w.-]+)/\s+(\d{2}-[A-Za-z]{3}-\d{4})",
                line.strip(),
            )
            if not text_match:
                continue
            folder = text_match.group(1)
            if folder == "..":
                continue
            self.rows.append((folder, parse_archive_index_date(text_match.group(2))))


def parse_archive_index(html: str) -> list[tuple[str, str | None]]:
    parser = _ApacheIndexParser()
    parser.feed(html)
    return parser.rows


def discover_stable_releases(
    archive_rows: Iterable[tuple[str, str | None]],
    *,
    ctx: DiscoveryContext | None = None,
) -> list[StableReleaseRecord]:
    context = _discovery_context(ctx)
    base = context.source.rstrip("/") + "/"
    out: list[StableReleaseRecord] = []
    seen: set[str] = set()
    for folder, released_on in archive_rows:
        if not is_stable_archive_entry(folder):
            continue
        if folder in seen:
            continue
        seen.add(folder)
        out.append(
            StableReleaseRecord(
                vendor=context.vendor,
                identifier=folder,
                platforms=list(context.platforms),
                released_on=released_on,
                primary_url=f"{base}{folder}/",
            )
        )
    out.sort(key=lambda row: row.identifier)
    return out


def _normalize_tag_identifier(tag: str) -> str:
    tag = tag.strip()
    if tag.startswith("release-"):
        return tag.removeprefix("release-")
    return tag


def gap_tags_without_archive(
    tags: Iterable[str],
    stable_releases: Iterable[StableReleaseRecord],
    *,
    vendor: str = MIXXX_VENDOR,
) -> list[ReleaseGap]:
    archived = {row.identifier for row in stable_releases}
    gaps: list[ReleaseGap] = []
    seen: set[str] = set()
    for raw_tag in tags:
        ident = _normalize_tag_identifier(raw_tag)
        if not _looks_like_semver_release(ident):
            continue
        if _is_prerelease_identifier(ident):
            continue
        if ident in archived or ident in seen:
            continue
        seen.add(ident)
        kind: GapKind = "older" if ident.startswith("1.") else "tag_only"
        gaps.append(
            ReleaseGap(
                vendor=vendor,
                identifier=ident,
                kind=kind,
                reason="Git tag has no matching stable archive folder",
                tag_url=f"https://github.com/mixxxdj/mixxx/releases/tag/{raw_tag.strip()}",
            )
        )
    gaps.sort(key=lambda row: row.identifier)
    return gaps


def discover_mixxx_from_archive_index(
    html: str,
    *,
    tags: Iterable[str] | None = None,
    ctx: DiscoveryContext | None = None,
) -> MixxxDiscoveryResult:
    context = _discovery_context(ctx)
    rows = parse_archive_index(html)
    stable = discover_stable_releases(rows, ctx=context)
    denominator = len(_stable_folder_names(rows))
    acquired = len(stable)
    gaps = gap_tags_without_archive(tags or (), stable, vendor=context.vendor)
    coverage = build_discovery_coverage(acquired=acquired, denominator=denominator)
    return MixxxDiscoveryResult(
        schema_version=context.schema_version,
        source=context.source,
        list_kind=context.list_kind,
        exhaustive=context.exhaustive,
        vendor=context.vendor,
        stable_releases=stable,
        gaps=gaps,
        discovery_coverage=coverage,
    )


def fetch_official_archive_index(url: str = MIXXX_ARCHIVE_BASE_URL) -> str:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "OpenDJ-compatint/1"})
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310: caller selects URL
            payload = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Mixxx archive fetch failed for {url}: {exc}") from exc
    return payload.decode("utf-8", errors="replace")
