import { Command } from "cmdk";
import { useState } from "react";

import DragToasts from "./DragToasts";
import { emptyMessage, searchFailureMessage } from "./emptyState";
import TrackRow from "./TrackRow";
import { useDragEvents } from "../hooks/useDragEvents";
import { useFrecency } from "../hooks/useFrecency";
import { useSearch } from "../hooks/useSearch";
import type { TrackHit } from "../types";

const NO_TRACKS: TrackHit[] = [];

export default function Palette() {
  const [query, setQuery] = useState("");
  const paletteVisible = true;
  const frecency = useFrecency(paletteVisible);
  const frecent = frecency.status === "ready" ? frecency.top : NO_TRACKS;
  const search = useSearch(query, frecent);
  const results = search.hits;
  // Subscribe to drag-lifecycle events from commands/drag.rs so the React
  // layer can surface start / success / fallback / failure feedback instead
  // of leaving the user guessing (UI-REVIEW-2026-04-17 launcher gap).
  const [toasts, dismissToast] = useDragEvents();
  const failed = search.status === "failed" || frecency.status === "failed";

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
          {search.status === "failed" && (
            // Rendered whether or not the cache fallback found anything, so a
            // failed search is never mistaken for a successful empty one.
            <div className="palette-empty palette-empty-failed" role="status">
              {searchFailureMessage(search.error, results.length)}
            </div>
          )}
          {results.length === 0 && search.status !== "failed" && (
            <Command.Empty
              className={failed ? "palette-empty palette-empty-failed" : "palette-empty"}
            >
              {emptyMessage(frecency, search, query)}
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
