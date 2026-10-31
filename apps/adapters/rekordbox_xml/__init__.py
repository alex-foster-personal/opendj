"""rekordbox XML library reader (DJIMPORT-06).

The rekordbox XML export (``DJ_PLAYLISTS``) is the format DJ apps hand each
other: rekordbox writes it, and Lexicon, Engine DJ and VirtualDJ can too. See
:mod:`apps.adapters.rekordbox_xml.reader`.
"""

from __future__ import annotations

from apps.adapters.rekordbox_xml.reader import RekordboxXml, read_rekordbox_xml

__all__ = ["RekordboxXml", "read_rekordbox_xml"]
