"""Build a small, synthetic, SQLCipher-encrypted OneLibrary for tests.

The real ``rb-usb-export`` fixtures live on an external host (they hold a
real library and label artwork), so CI never had a OneLibrary to write
against. This builder makes one from scratch: the Rekordbox 7
``exportLibrary.db`` table and column names (as in pyrekordbox's MIT
``devicelib_plus`` models), encrypted with the export key, holding five
invented tracks and a small playlist tree. No real library data.
"""
from __future__ import annotations

from pathlib import Path

from apps.sync.usb.pioneer.onelibrary import onelibrary_key

_CONTENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("title", "TEXT"), ("titleForSearch", "TEXT"), ("subtitle", "TEXT"),
    ("bpmx100", "INTEGER"), ("length", "INTEGER"), ("trackNo", "INTEGER"),
    ("discNo", "INTEGER"), ("artist_id_artist", "INTEGER"),
    ("artist_id_remixer", "INTEGER"), ("artist_id_originalArtist", "INTEGER"),
    ("artist_id_composer", "INTEGER"), ("artist_id_lyricist", "INTEGER"),
    ("album_id", "INTEGER"), ("genre_id", "INTEGER"), ("label_id", "INTEGER"),
    ("key_id", "INTEGER"), ("color_id", "INTEGER"), ("image_id", "INTEGER"),
    ("djComment", "TEXT"), ("rating", "INTEGER"), ("releaseYear", "INTEGER"),
    ("releaseDate", "TEXT"), ("dateCreated", "TEXT"), ("dateAdded", "TEXT"),
    ("path", "TEXT"), ("fileName", "TEXT"), ("fileSize", "INTEGER"),
    ("fileType", "INTEGER"), ("bitrate", "INTEGER"), ("bitDepth", "INTEGER"),
    ("samplingRate", "INTEGER"), ("isrc", "TEXT"), ("djPlayCount", "INTEGER"),
    ("isHotCueAutoLoadOn", "INTEGER"), ("isKuvoDeliverStatusOn", "INTEGER"),
    ("kuvoDeliveryComment", "TEXT"), ("masterDbId", "INTEGER"),
    ("masterContentId", "INTEGER"), ("analysisDataFilePath", "TEXT"),
    ("analysedBits", "INTEGER"), ("contentLink", "INTEGER"),
    ("hasModified", "INTEGER"), ("cueUpdateCount", "INTEGER"),
    ("analysisDataUpdateCount", "INTEGER"), ("informationUpdateCount", "INTEGER"),
)

_SCHEMA = f"""
CREATE TABLE property ("deviceName" VARCHAR(255) NOT NULL PRIMARY KEY, "dbVersion" INTEGER,
  "numberOfContents" INTEGER, "createdDate" TEXT, "backGroundColorType" INTEGER,
  "myTagMasterDBID" INTEGER);
CREATE TABLE image (image_id INTEGER PRIMARY KEY AUTOINCREMENT, path VARCHAR(255) UNIQUE);
CREATE TABLE artist (artist_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) UNIQUE,
  "nameForSearch" VARCHAR(255));
CREATE TABLE album (album_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) UNIQUE,
  artist_id INTEGER, image_id INTEGER, "isComplation" INTEGER, "nameForSearch" VARCHAR(255));
CREATE TABLE genre (genre_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) UNIQUE);
CREATE TABLE key (key_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) UNIQUE);
CREATE TABLE label (label_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) UNIQUE);
CREATE TABLE color (color_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) UNIQUE);
CREATE TABLE content (content_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {", ".join(f'"{name}" {typ}' for name, typ in _CONTENT_COLUMNS)});
CREATE TABLE playlist (playlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
  "sequenceNo" INTEGER NOT NULL, name VARCHAR(255) NOT NULL, image_id INTEGER,
  attribute INTEGER, playlist_id_parent INTEGER);
CREATE TABLE playlist_content (playlist_id INTEGER NOT NULL, content_id INTEGER NOT NULL,
  "sequenceNo" INTEGER NOT NULL, PRIMARY KEY (playlist_id, content_id));
"""

#: (title, bpmx100, rating, fileName) for content ids 1..5.
SYNTH_TRACKS: tuple[tuple[str, int, int, str], ...] = (
    ("Synth One", 12000, 0, "one.mp3"),
    ("Synth Two ü", 12450, 3, "two.mp3"),
    ("Synth Three 日本", 12800, 5, "three.mp3"),
    ("Synth Four", 17400, 1, "four.mp3"),
    ("Synth Five", 9000, 2, "five.mp3"),
)


def build_synthetic_onelibrary(path: Path) -> Path:
    """Write the synthetic export to ``path`` and return it.

    Playlist tree: ``Existing`` (id 1, top-level seq 1, tracks 3 then 1),
    folder ``Folder`` (id 2, top-level seq 2) holding ``Child`` (id 3).
    """
    from sqlcipher3 import dbapi2 as sqlcipher

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlcipher.connect(str(path))
    try:
        escaped = onelibrary_key().replace("'", "''")
        conn.execute(f"PRAGMA key = '{escaped}'")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO property VALUES ('SYNTH', 1000, ?, '2026-10-01', 0, 1)",
            (len(SYNTH_TRACKS),),
        )
        conn.execute("INSERT INTO artist (name, \"nameForSearch\") VALUES ('Synth Artist', 'synth artist')")
        conn.execute("INSERT INTO genre (name) VALUES ('House')")
        conn.execute("INSERT INTO key (name) VALUES ('8A')")
        conn.execute("INSERT INTO color (name) VALUES ('Pink')")
        names = [name for name, _ in _CONTENT_COLUMNS]
        for idx, (title, bpm, rating, fname) in enumerate(SYNTH_TRACKS, start=1):
            values: dict[str, object] = {
                name: ("" if typ == "TEXT" else 0) for name, typ in _CONTENT_COLUMNS
            }
            values.update(
                title=title, titleForSearch=title.lower(), bpmx100=bpm,
                rating=rating, fileName=fname, path=f"/Contents/Synth/{fname}",
                artist_id_artist=1, genre_id=1, key_id=1, length=300 + idx,
                releaseDate="2020-01-01", dateCreated="2026-10-01",
                dateAdded="2026-10-01", fileType=1, bitrate=320,
                bitDepth=16, samplingRate=44100,
            )
            conn.execute(
                f"INSERT INTO content (content_id, {', '.join(chr(34) + n + chr(34) for n in names)}) "
                f"VALUES (?, {', '.join('?' * len(names))})",
                [idx, *(values[n] for n in names)],
            )
        conn.executemany(
            'INSERT INTO playlist ("sequenceNo", name, image_id, attribute, playlist_id_parent) '
            "VALUES (?, ?, 0, ?, ?)",
            [(1, "Existing", 0, 0), (2, "Folder", 1, 0), (1, "Child", 0, 2)],
        )
        conn.executemany(
            'INSERT INTO playlist_content VALUES (?, ?, ?)', [(1, 3, 1), (1, 1, 2)]
        )
        conn.commit()
    finally:
        conn.close()
    return path
