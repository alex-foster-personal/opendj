import { Command } from "cmdk";
import { useState } from "react";

import DragToasts from "./DragToasts";
import TrackRow from "./TrackRow";
import { useDragEvents } from "../hooks/useDragEvents";
import { useFrecency } from "../hooks/useFrecency";
import { useSearch } from "../hooks/useSearch";
import type { TrackHit } from "../types";

const NO_TRACKS: TrackHit[] = [];

/** What to tell the user when the list is empty, and why it is empty.
 *
 * There used to be three hard-coded sample tracks here, rendered whenever the
 * real list came back empty (issue #1542). That is mocked data on the render
 * path, which the house rule forbids outright, and it made a fresh install
 * indistinguishable from a launcher whose store failed to open. Every empty
 * state now says which one it is in words the user can read.
 */
function emptyMessage(
  status: "loading" | "ready" | "failed",
  error: string | null,
  query: string,
): string {
  if (status === "loading") return "Loading your recent tracks...";
  if (status === "failed") return `Could not read your track history: ${error}`;
  if (query.trim().length > 0) return `No tracks match "${query.trim()}".`;
  return "No tracks played yet. Play something in Open DJ and your recents appear here.";
}

export default function Palette() {
  const [query, setQuery] = useState("");
  const paletteVisible = true;
  const frecency = useFrecency(paletteVisible);
  const frecent = frecency.status === "ready" ? frecency.top : NO_TRACKS;
  const results = useSearch(query, frecent);
  // Subscribe to drag-lifecycle events from commands/drag.rs so the React
  // layer can surface start / success / fallback / failure feedback instead
  // of leaving the user guessing (UI-REVIEW-2026-04-17 launcher gap).
  const [toasts, dismissToast] = useDragEvents();

  return (
    <div className="palette-shell">
      <Command shouldFilter={false} label="Hyper-K Launcher" className="palette-command">
        <Command.Input
          value={query}
          onValueChange={setQuery}
          placeholder="Search tracks..."
          autoFocus
          className="palette-input"
        />
        <Command.List className="palette-list">
          {results.length === 0 && (
            <Command.Empty
              className={
                frecency.status === "failed" ? "palette-empty palette-empty-failed" : "palette-empty"
              }
            >
              {emptyMessage(
                frecency.status,
                frecency.status === "failed" ? frecency.error : null,
                query,
              )}
            </Command.Empty>
          )}
          {results.map((t) => (
            <TrackRow key={t.stable_id} track={t} />
          ))}
        </Command.List>
      </Command>
      <DragToasts toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
