import { invoke } from "@tauri-apps/api/core";
import { matchSorter } from "match-sorter";
import { useEffect, useState } from "react";

import type { TrackHit } from "../types";

/** Below this many characters the palette filters the in-memory recents cache
 * and never touches FTS5. The palette says so in its empty state, because
 * "no tracks match" means something different on each side of this line. */
export const FTS_MIN_QUERY_CHARS = 4;

/** What the result list is, and what an EMPTY one means.
 *
 * A bare `TrackHit[]` collapses three different situations into the same value:
 * the query is still in flight, the backend rejected, or the library genuinely
 * holds no match. The palette rendered all three as "No tracks.", so a pending
 * FTS5 call read as an authoritative "nothing here" and a `search_tracks`
 * failure read as one permanently (Codex P1 BLOCKING, PR #1633). That is the
 * same defect `FrecencyState` fixes one hook over, found by asking what the
 * CLASS was rather than fixing the one instance reported.
 *
 * `hits` is always what to render. `status` is what an empty `hits` means, and
 * on failure it also keeps the error, so a fallback to the recents cache is
 * visible rather than silent.
 */
export type SearchState<T extends TrackHit> =
  | { status: "ready"; hits: T[] }
  | { status: "searching"; hits: T[] }
  | { status: "failed"; error: string; hits: T[] };

/**
 * Two-tier cache:
 * - empty query: return the frecency cache as-is
 * - 1-3 chars: filter the in-memory cache with match-sorter (no IPC)
 * - >=4 chars: debounce 80ms, then call Rust `search_tracks`
 */
export function useSearch<T extends TrackHit>(
  query: string,
  frecencyTop: T[],
): SearchState<T> {
  const [state, setState] = useState<SearchState<T>>({ status: "ready", hits: frecencyTop });

  useEffect(() => {
    const q = query.trim();
    if (q.length === 0) {
      setState({ status: "ready", hits: frecencyTop });
      return;
    }
    if (q.length < FTS_MIN_QUERY_CHARS) {
      setState({
        status: "ready",
        hits: matchSorter(frecencyTop, q, { keys: ["title", "artist"] }),
      });
      return;
    }
    // Announced BEFORE the debounce, not after it: the whole point is that the
    // window between the keystroke and the answer must not read as an answer.
    setState((prev) => ({ status: "searching", hits: prev.hits }));
    const id = setTimeout(async () => {
      try {
        const hits = await invoke<T[]>("search_tracks", { query: q, limit: 20 });
        setState({ status: "ready", hits });
      } catch (err) {
        // P17-03: surface backend search failures to the devtools console.
        // eslint-disable-next-line no-console
        console.error("[launcher] search_tracks failed; falling back to cache", err);
        // The UX fallback stays so the user can keep typing, but `failed` now
        // travels with it: the palette says the search failed whether or not
        // the cache happens to have something to show.
        setState({
          status: "failed",
          error: err instanceof Error ? err.message : String(err),
          hits: matchSorter(frecencyTop, q, { keys: ["title", "artist"] }),
        });
      }
    }, 80);
    return () => clearTimeout(id);
  }, [query, frecencyTop]);

  return state;
}
