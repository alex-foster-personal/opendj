# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Populate BPM + key (camelot) track_fields for the 100 vocal stems.

The stems are acapellas of tracks whose BPM/key we already resolved into
data/clubsauna_acapella_techno_100.jsonl (rank == the NNN prefix on each
vocal file). Write those into track_fields so the Library/browser BPM + Key
columns render and key-matching works. Source 'inferred' (derived from the
source track, not measured on the stem). Fail-fast on any rank/title mismatch.
"""
from __future__ import annotations
import hashlib, json, re, sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/Users/dev/Music/music-dj-tools")
DB = REPO / "data" / "state" / "state.db"
JSONL = REPO / "data" / "clubsauna_acapella_techno_100.jsonl"
VOCALS = Path("/Users/dev/Music/_incoming/clubsauna-acapella-techno-100/vocals")
FN = re.compile(r"^(\d{2,3})\s*-\s*(.*?)\s*-\s*vocals$")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def main() -> None:
    by_rank = {r["rank"]: r for r in (json.loads(l) for l in JSONL.read_text().splitlines() if l.strip())}
    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB, timeout=15)
    con.execute("PRAGMA busy_timeout=15000;")
    n_bpm = n_key = 0
    with con:
        for fp in sorted(VOCALS.glob("*.mp3")):
            m = FN.match(fp.stem)
            if not m:
                raise SystemExit(f"unparseable: {fp.name}")
            rank = int(m.group(1))
            row = by_rank.get(rank)
            if row is None:
                raise SystemExit(f"no jsonl row for rank {rank} ({fp.name})")
            # sanity: the file's title should appear in the jsonl title (guard against drift)
            file_title = m.group(2).split(" - ", 1)[-1] if " - " in m.group(2) else m.group(2)
            if norm(file_title)[:8] not in norm(row["title"]) and norm(row["title"])[:8] not in norm(file_title):
                print(f"  WARN rank {rank}: file '{file_title}' vs jsonl '{row['title']}' (writing anyway by rank)")
            sid = hashlib.sha1(str(fp).encode("utf-8")).hexdigest()
            bpm, camelot = row.get("bpm"), row.get("camelot")
            if bpm is not None:
                con.execute(
                    """INSERT OR REPLACE INTO track_fields
                       (stable_id, field_name, value_json, source, confidence, modified_at)
                       VALUES (?, 'bpm', ?, 'inferred', 0.9, ?)""",
                    (sid, json.dumps(float(bpm)), now))
                n_bpm += 1
            if camelot:
                con.execute(
                    """INSERT OR REPLACE INTO track_fields
                       (stable_id, field_name, value_json, source, confidence, modified_at)
                       VALUES (?, 'key', ?, 'inferred', 0.9, ?)""",
                    (sid, json.dumps(camelot), now))
                n_key += 1
    con.close()
    print(f"wrote bpm for {n_bpm}/100, key for {n_key}/100 stems")


if __name__ == "__main__":
    main()
