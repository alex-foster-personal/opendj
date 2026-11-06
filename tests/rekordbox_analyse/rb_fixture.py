"""Build a small plain-SQLite Rekordbox 6 library pyrekordbox can open and commit to.

Real schema (pyrekordbox's own table models), plain rather than SQLCipher so the
tests need no Rekordbox key. Rows: the registry counters ``commit`` bumps, the
TRACK menu item and one device ``add_content`` reads, then the caller's tracks
and playlists.

-Claude
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pyrekordbox.db6 import tables
from sqlalchemy import MetaData, create_engine


def _nullable_metadata() -> MetaData:
    """pyrekordbox's models, with non-key columns nullable as in a real Rekordbox master.db."""
    metadata = MetaData()
    for table in tables.Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for column in copy.columns:
            column.nullable = not column.primary_key
    return metadata


def _insert(conn, metadata: MetaData, name: str, **values: object) -> None:
    conn.execute(metadata.tables[name].insert().values(**values))


@dataclass(frozen=True)
class FixtureTrack:
    content_id: str
    folder_path: str
    analysed: bool
    bpm: float | None = None


@dataclass(frozen=True)
class FixturePlaylist:
    playlist_id: str
    name: str
    member_ids: tuple[str, ...] = ()
    is_folder: bool = False


@dataclass
class FixtureLibrary:
    rb_dir: Path
    tracks: list[FixtureTrack] = field(default_factory=list)
    playlists: list[FixturePlaylist] = field(default_factory=list)

    @property
    def master_db(self) -> Path:
        return self.rb_dir / "master.db"


def build_library(rb_dir: Path, tracks: list[FixtureTrack], playlists: list[FixturePlaylist]) -> FixtureLibrary:
    rb_dir.mkdir(parents=True, exist_ok=True)
    db = rb_dir / "master.db"
    engine = create_engine(f"sqlite:///{db}")
    metadata = _nullable_metadata()
    metadata.create_all(engine)
    with engine.begin() as conn:
        _insert(conn, metadata, "agentRegistry", registry_id="localUpdateCount", int_1=100)
        _insert(conn, metadata, "djmdMenuItems", ID="1", Class=1, Name="TRACK", UUID="menu-track", rb_local_usn=1)
        _insert(conn, metadata, "djmdDevice", ID="1", MasterDBID="master-1", Name="fixture-mac", UUID="device-1")
        for track in tracks:
            _insert(
                conn,
                metadata,
                "djmdContent",
                ID=track.content_id,
                UUID=f"uuid-{track.content_id}",
                FolderPath=track.folder_path,
                FileNameL=Path(track.folder_path).name,
                Analysed=105 if track.analysed else 0,
                AnalysisDataPath=f"/PIONEER/USBANLZ/{track.content_id}/ANLZ0000.DAT" if track.analysed else "",
                BPM=int(track.bpm * 100) if track.bpm else None,
            )
        for seq, playlist in enumerate(playlists, start=1):
            _insert(
                conn,
                metadata,
                "djmdPlaylist",
                ID=playlist.playlist_id,
                Seq=seq,
                Name=playlist.name,
                Attribute=1 if playlist.is_folder else 0,
                ParentID="root",
                UUID=f"pl-{playlist.playlist_id}",
            )
            for track_no, content_id in enumerate(playlist.member_ids, start=1):
                _insert(
                    conn,
                    metadata,
                    "djmdSongPlaylist",
                    ID=f"{playlist.playlist_id}-{track_no}",
                    PlaylistID=playlist.playlist_id,
                    ContentID=content_id,
                    TrackNo=track_no,
                    UUID=f"sp-{playlist.playlist_id}-{track_no}",
                )
    with engine.connect() as conn:
        # Rekordbox keeps master.db in WAL mode; the fixture must too.
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    engine.dispose()
    nodes = "".join(
        f'<NODE Id="{int(pl.playlist_id):X}" ParentId="0" Attribute="{1 if pl.is_folder else 0}" '
        f'Timestamp="1793682000000" Lib_Type="0" CheckType="0"/>'
        for pl in playlists
    )
    (rb_dir / "masterPlaylists6.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<MASTER_PLAYLIST Version="1.0.0" AutomaticSync="0">'
        '<PRODUCT Name="rekordbox" Version="6.8.5" Company="AlphaTheta"/>'
        f"<PLAYLISTS>{nodes}</PLAYLISTS></MASTER_PLAYLIST>\n",
        encoding="utf-8",
    )
    return FixtureLibrary(rb_dir=rb_dir, tracks=list(tracks), playlists=list(playlists))
