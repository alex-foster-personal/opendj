"""JSON-serializable shapes for compatint release-discovery output."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from apps.compatint import CORPUS_SCHEMA_VERSION

MixxxListKind = Literal["discovered", "seed"]
GapKind = Literal["tag_only", "older"]

MIXXX_VENDOR = "Mixxx"
MIXXX_STABLE_PLATFORMS: tuple[str, ...] = ("linux", "macos", "windows")
MIXXX_ARCHIVE_BASE_URL = "https://downloads.mixxx.org/releases/"


@dataclass(frozen=True, slots=True)
class StableReleaseRecord:
    vendor: str
    identifier: str
    platforms: list[str]
    released_on: str | None
    primary_url: str


@dataclass(frozen=True, slots=True)
class ReleaseGap:
    vendor: str
    identifier: str
    kind: GapKind
    reason: str
    tag_url: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryCoverageAxis:
    acquired: int
    denominator: int
    status: Literal["partial", "complete"]


@dataclass(frozen=True, slots=True)
class MixxxDiscoveryResult:
    schema_version: int
    source: str
    list_kind: MixxxListKind
    exhaustive: bool
    vendor: str
    stable_releases: list[StableReleaseRecord]
    gaps: list[ReleaseGap]
    discovery_coverage: DiscoveryCoverageAxis

    def to_corpus_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "vendor": self.vendor,
            "list_kind": self.list_kind,
            "exhaustive": self.exhaustive,
            "discovery_source": self.source,
            "stable_releases": [asdict(row) for row in self.stable_releases],
            "gaps": [asdict(row) for row in self.gaps],
            "discovery_coverage": asdict(self.discovery_coverage),
        }


@dataclass
class DiscoveryContext:
    schema_version: int = CORPUS_SCHEMA_VERSION
    source: str = MIXXX_ARCHIVE_BASE_URL
    list_kind: MixxxListKind = "discovered"
    exhaustive: bool = False
    vendor: str = MIXXX_VENDOR
    platforms: list[str] = field(default_factory=lambda: list(MIXXX_STABLE_PLATFORMS))
