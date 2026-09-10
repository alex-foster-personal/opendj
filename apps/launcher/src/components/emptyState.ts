import type { FrecencyState } from "../hooks/useFrecency";
import { FTS_MIN_QUERY_CHARS, type SearchState } from "../hooks/useSearch";
import type { TrackHit } from "../types";

/** What to tell the user when the list is empty, and why it is empty.
 *
 * There used to be three hard-coded sample tracks in the palette, rendered
 * whenever the real list came back empty (issue #1542). That is mocked data on
 * the render path, which the house rule forbids outright, and it made a fresh
 * install indistinguishable from a launcher whose store failed to open. Every
 * empty state now says which one it is in words the user can read.
 *
 * PURE ON PURPOSE. This is the decision the whole issue is about, and it takes
 * plain values in and returns a string, so its tests need no IPC, no renderer
 * and nothing stubbed. Whether the palette WIRES it to the right states is a
 * separate, smaller question that the component test asks.
 */
export function emptyMessage(
  frecency: FrecencyState,
  search: SearchState<TrackHit>,
  query: string,
): string {
  const q = query.trim();
  if (search.status === "searching") return `Searching for "${q}"...`;
  if (q.length < FTS_MIN_QUERY_CHARS) {
    // Short queries never reach FTS5; they filter the recents cache in memory,
    // so the state of THAT cache is what an empty list means here.
    if (frecency.status === "loading") return "Loading your recent tracks...";
    if (frecency.status === "failed") {
      return `Could not read your track history: ${frecency.error}`;
    }
    if (q.length === 0) {
      // Not "play something in Open DJ": repo-wide, nothing writes
      // `tracks_frecency.plays` or `last_played_at`. `record_drag` is the only
      // writer, so a drag is the only action that actually populates this list
      // and telling the user otherwise leaves them in the same empty state
      // (Codex P2, PR #1633).
      return "No recent tracks yet. Search for a track and drag it out; dragged tracks appear here.";
    }
    return (
      `No recent track matches "${q}". Type ${FTS_MIN_QUERY_CHARS} or more ` +
      "characters to search the whole library."
    );
  }
  return `No tracks match "${q}".`;
}

/** The sentence shown when `search_tracks` itself failed.
 *
 * Separate from `emptyMessage` because a search failure must be visible even
 * when the recents-cache fallback has rows to show: a silent fallback is how
 * the failure became indistinguishable from a successful empty answer.
 */
export function searchFailureMessage(error: string, cachedHits: number): string {
  if (cachedHits > 0) {
    return `Track search failed (${error}). Showing matching recent tracks instead.`;
  }
  return `Track search failed: ${error}`;
}
