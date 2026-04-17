import { invoke } from "@tauri-apps/api/core";
import { matchSorter } from "match-sorter";
import { useEffect, useState } from "react";

import type { TrackHit } from "../types";

/**
 * Two-tier cache:
 * - empty query: return the frecency cache as-is
 * - 1-3 chars: filter the in-memory cache with match-sorter (no IPC)
 * - >=4 chars: debounce 80ms, then call Rust `search_tracks`
 */
export function useSearch<T extends TrackHit>(query: string, frecencyTop: T[]): T[] {
  const [results, setResults] = useState<T[]>(frecencyTop);

  useEffect(() => {
    const q = query.trim();
    if (q.length === 0) {
      setResults(frecencyTop);
      return;
    }
    if (q.length < 4) {
      setResults(matchSorter(frecencyTop, q, { keys: ["title", "artist"] }));
      return;
    }
    const id = setTimeout(async () => {
      try {
        const hits = await invoke<T[]>("search_tracks", { query: q, limit: 20 });
        setResults(hits);
      } catch {
        setResults(matchSorter(frecencyTop, q, { keys: ["title", "artist"] }));
      }
    }, 80);
    return () => clearTimeout(id);
  }, [query, frecencyTop]);

  return results;
}
