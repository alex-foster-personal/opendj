"""NML (collection.nml) parser + canonical writer -- stdlib only.

Reads a Traktor collection.nml into a light-weight Python data model that
preserves every attribute + child element we do not explicitly understand.
Writes back canonical XML (lexicographic attribute order, 2-space indent,
UTF-8 + XML declaration) so two consecutive writes of the same doc are
byte-identical.

Design rules:

  * No third-party dependencies. ``xml.etree.ElementTree`` only.
  * Unknown children + attributes round-trip verbatim (open-dj section 9).
  * XML escaping is handled by ``ElementTree``'s writer; attacker-controlled
    ``<EXTENDEDDATA>`` payloads stay escaped and are never re-parsed as XML.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# ----------------------------------------------------------- data model


@dataclass
class NMLEntry:
    """One ``<ENTRY>`` child -- a track row in Traktor's collection."""

    element: ET.Element

    # ------ accessors

    @property
    def artist(self) -> str:
        return self.element.get("ARTIST", "")

    @artist.setter
    def artist(self, value: str) -> None:
        if value:
            self.element.set("ARTIST", value)
        elif "ARTIST" in self.element.attrib:
            del self.element.attrib["ARTIST"]

    @property
    def title(self) -> str:
        return self.element.get("TITLE", "")

    @title.setter
    def title(self, value: str) -> None:
        if value:
            self.element.set("TITLE", value)
        elif "TITLE" in self.element.attrib:
            del self.element.attrib["TITLE"]

    def get_subchild(self, tag: str) -> ET.Element | None:
        return self.element.find(tag)

    def get_subchild_attr(self, tag: str, attr: str) -> str | None:
        el = self.element.find(tag)
        if el is None:
            return None
        return el.get(attr)

    def ensure_subchild(self, tag: str) -> ET.Element:
        el = self.element.find(tag)
        if el is None:
            el = ET.SubElement(self.element, tag)
        return el

    def location_path(self) -> str:
        loc = self.element.find("LOCATION")
        if loc is None:
            return ""
        volume = loc.get("VOLUME", "")
        directory = loc.get("DIR", "").replace("/:", "/")
        filename = loc.get("FILE", "")
        base = f"{volume}{directory}{filename}"
        return base


@dataclass
class NMLDocument:
    """Parsed ``collection.nml`` document wrapping an ``ElementTree``."""

    tree: ET.ElementTree
    root: ET.Element

    # ----------------------------------------------------------------- load

    @classmethod
    def read(cls, source: Path | bytes | str) -> "NMLDocument":
        if isinstance(source, (str, Path)):
            tree = ET.parse(Path(source))
        else:
            tree = ET.ElementTree(ET.fromstring(source))
        root = tree.getroot()
        if root.tag != "NML":
            raise ValueError(f"expected <NML> root, got <{root.tag}>")
        return cls(tree=tree, root=root)

    @classmethod
    def empty(cls, version: str = "20") -> "NMLDocument":
        root = ET.Element("NML", {"VERSION": version})
        ET.SubElement(root, "HEAD")
        ET.SubElement(root, "MUSICFOLDERS")
        ET.SubElement(root, "COLLECTION", {"ENTRIES": "0"})
        ET.SubElement(
            root,
            "PLAYLISTS",
            {},
        )
        return cls(tree=ET.ElementTree(root), root=root)

    # ----------------------------------------------------------------- dump

    def write(self, target: Path) -> None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.to_bytes())

    def to_bytes(self) -> bytes:
        # Refresh <COLLECTION ENTRIES="N"> before serialising.
        collection = self.root.find("COLLECTION")
        if collection is not None:
            collection.set("ENTRIES", str(len(collection.findall("ENTRY"))))
        canonicalise(self.root)
        xml_string = ET.tostring(
            self.root,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )
        return xml_string + b"\n"

    # -------------------------------------------------------------- entries

    def entries(self) -> list[NMLEntry]:
        collection = self.root.find("COLLECTION")
        if collection is None:
            return []
        return [NMLEntry(element=el) for el in collection.findall("ENTRY")]

    def add_entry(self) -> NMLEntry:
        collection = self.root.find("COLLECTION")
        if collection is None:
            collection = ET.SubElement(self.root, "COLLECTION", {"ENTRIES": "0"})
        el = ET.SubElement(collection, "ENTRY")
        return NMLEntry(element=el)


# ----------------------------------------------------------- canonicaliser


def canonicalise(element: ET.Element) -> None:
    """In-place: sort attributes lexicographically + indent with 2 spaces.

    This is a small stable normaliser -- not a full XML canonicalisation
    spec -- but enough to make byte-diff round-trips deterministic.
    """
    _sort_attributes(element)
    _indent(element, level=0)


def _sort_attributes(element: ET.Element) -> None:
    """Sort ``element.attrib`` keys lexicographically, in-place, recursively."""
    if element.attrib:
        sorted_items = sorted(element.attrib.items())
        element.attrib.clear()
        for k, v in sorted_items:
            element.set(k, v)
    for child in element:
        _sort_attributes(child)


def _indent(element: ET.Element, level: int, step: str = "  ") -> None:
    """Pretty-indent a tree in-place (Python 3.9+ has ET.indent; we replicate
    it explicitly to keep a byte-stable output across minor Python versions)."""
    indent_outer = "\n" + step * level
    indent_inner = "\n" + step * (level + 1)
    if len(element):
        if not element.text or not element.text.strip():
            element.text = indent_inner
        for i, child in enumerate(element):
            _indent(child, level + 1, step)
            if not child.tail or not child.tail.strip():
                is_last = i == len(element) - 1
                child.tail = indent_outer if is_last else indent_inner
    else:
        # Leaf -- no text mutation.
        if level > 0 and (not element.tail or not element.tail.strip()):
            element.tail = indent_outer
