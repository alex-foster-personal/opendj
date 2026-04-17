// Shared TS types for the palette. Mirrors the Rust `TrackHit` /
// `FrecentHit` shapes from src-tauri/src/commands/{search,frecency}.rs.

export interface TrackHit {
  stable_id: string;
  path: string;
  title: string | null;
  artist: string | null;
  album: string | null;
  genre: string | null;
  key: string | null;
  bpm: number | null;
}

export interface FrecentHit extends TrackHit {
  score: number;
  plays: number;
  drags: number;
}
