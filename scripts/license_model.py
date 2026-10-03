"""Shared inventory model and filesystem helpers for third-party license generation.

Split out of scripts/third_party_licenses.py (file_size.over_limit_python ratchet,
the same seam as scripts/license_classify.py and scripts/license_mirrors.py): the
Component record, inventory errors, and small helpers for reading license files
from disk. Ecosystem collectors stay in scripts/third_party_licenses.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from scripts.license_classify import Cat, classify_license

LICENSE_FILE_PATTERN = re.compile(r"(licen[sc]e|copying|notice|copyright|unlicense)", re.IGNORECASE)
NOTICE_FILE_PATTERN = re.compile(r"notice", re.IGNORECASE)


@dataclass
class Component:
    ecosystem: str
    name: str
    version: str
    license: str
    homepage: str = ""
    texts: list[tuple[str, str]] = field(default_factory=list)  # (file name, text)
    note: str = ""

    @property
    def category(self) -> str:
        return classify_license(self.license) if self.license else Cat.UNKNOWN

    @property
    def notices(self) -> list[tuple[str, str]]:
        return [(n, t) for n, t in self.texts if NOTICE_FILE_PATTERN.search(n)]

    @property
    def license_texts(self) -> list[tuple[str, str]]:
        return [(n, t) for n, t in self.texts if not NOTICE_FILE_PATTERN.search(n)]


class LicenseInventoryError(RuntimeError):
    """The inventory could not be measured. Never rendered as a result."""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _license_files_in(directory: Path) -> list[tuple[str, str]]:
    if not directory.is_dir():
        return []
    return [
        (path.name, _read_text(path))
        for path in sorted(directory.iterdir())
        if path.is_file() and LICENSE_FILE_PATTERN.search(path.name) and path.suffix not in {".py", ".js", ".json"}
    ]
