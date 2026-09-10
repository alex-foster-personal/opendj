import { Command } from "cmdk";
import { useState } from "react";

import DragToasts from "./DragToasts";
import { emptyMessage, searchNoticeMessage } from "./emptyState";
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
  // Null only when the search is settled, which is the one state with nothing
  // to report about it.
  const notice = searchNoticeMessage(search, query);

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
        <Command.List
          className={
            search.status === "searching" ? "palette-list palette-list-stale" : "palette-list"
          }
        >
          {notice !== null && (
            // Rendered independently of `results.length`. Both non-ready states
            // retain the previous rows, so an empty-list-only notice is silent
            // in exactly the case where stale rows look like current matches.
            <div
              className={failed ? "palette-empty palette-empty-failed" : "palette-empty"}
              role="status"
            >
              {notice}
            </div>
          )}
          {results.length === 0 && notice === null && (
            <Command.Empty
              className={failed ? "palette-empty palette-empty-failed" : "palette-empty"}
            >
              {emptyMessage(frecency, query)}
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
