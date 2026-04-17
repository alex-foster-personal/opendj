"""NML parser + canonical writer tests (OPEN-02d)."""

from __future__ import annotations

import pytest

from apps.adapters.traktor.nml import NMLDocument


SAMPLE_NML = b"""<?xml version='1.0' encoding='utf-8'?>
<NML VERSION="20">
  <HEAD></HEAD>
  <MUSICFOLDERS></MUSICFOLDERS>
  <COLLECTION ENTRIES="1">
    <ENTRY TITLE="Test Track" ARTIST="Alice">
      <LOCATION DIR="/:Music/:" FILE="a.mp3" VOLUME=""></LOCATION>
      <TEMPO BPM="128.000"></TEMPO>
      <MUSICAL_KEY VALUE="7"></MUSICAL_KEY>
    </ENTRY>
  </COLLECTION>
  <PLAYLISTS></PLAYLISTS>
</NML>
"""


@pytest.mark.requirement("OPEN-02d")
def test_empty_roundtrip(tmp_path) -> None:
    doc = NMLDocument.empty()
    doc.write(tmp_path / "collection.nml")
    readback = NMLDocument.read(tmp_path / "collection.nml")
    assert readback.root.tag == "NML"
    assert len(readback.entries()) == 0


@pytest.mark.requirement("OPEN-02d")
def test_read_real_sample() -> None:
    doc = NMLDocument.read(SAMPLE_NML)
    entries = doc.entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.title == "Test Track"
    assert entry.artist == "Alice"
    assert entry.get_subchild_attr("TEMPO", "BPM") == "128.000"
    assert entry.location_path().endswith("a.mp3")


@pytest.mark.requirement("OPEN-02d")
def test_roundtrip_is_byte_stable(tmp_path) -> None:
    doc = NMLDocument.read(SAMPLE_NML)
    a = tmp_path / "a.nml"
    b = tmp_path / "b.nml"
    doc.write(a)
    # Read + re-write; should produce identical bytes.
    doc2 = NMLDocument.read(a)
    doc2.write(b)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.requirement("OPEN-02d")
def test_unknown_attributes_preserved(tmp_path) -> None:
    raw = (
        b"<?xml version='1.0' encoding='utf-8'?>\n"
        b"<NML VERSION=\"20\">\n"
        b"  <HEAD><SOMETHING_UNKNOWN FOO=\"BAR\"></SOMETHING_UNKNOWN></HEAD>\n"
        b"  <COLLECTION ENTRIES=\"0\"></COLLECTION>\n"
        b"</NML>\n"
    )
    doc = NMLDocument.read(raw)
    out = tmp_path / "out.nml"
    doc.write(out)
    readback = NMLDocument.read(out)
    head = readback.root.find("HEAD")
    assert head is not None
    unknown = head.find("SOMETHING_UNKNOWN")
    assert unknown is not None
    assert unknown.get("FOO") == "BAR"


@pytest.mark.requirement("OPEN-02d")
def test_non_nml_root_raises() -> None:
    with pytest.raises(ValueError):
        NMLDocument.read(b"<NOT_NML></NOT_NML>")


@pytest.mark.requirement("OPEN-02d")
def test_add_entry_increments_count() -> None:
    doc = NMLDocument.empty()
    doc.add_entry()
    doc.add_entry()
    data = doc.to_bytes()
    assert b'ENTRIES="2"' in data
