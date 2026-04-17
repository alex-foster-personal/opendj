import { Command } from "cmdk";
import { useState } from "react";

import DragToasts from "./DragToasts";
import TrackRow from "./TrackRow";
import { useDragEvents } from "../hooks/useDragEvents";
import { useFrecency } from "../hooks/useFrecency";
import { useSearch } from "../hooks/useSearch";
import type { TrackHit } from "../types";

// Hardcoded sample data for Plan 17-02. Plan 17-03 wires this to FTS5 via
// useSearch/useFrecency. Samples stay as a fallback when the DB is missing.
const SAMPLE: TrackHit[] = [
  { stable_id: "s1", path: "/Users/dev3/Music/sample-dua.mp3",      title: "Levitating",           artist: "Dua Lipa",       album: null, genre: null, bpm: 103, key: "11A" },
  { stable_id: "s2", path: "/Users/dev3/Music/sample-weekend.mp3",  title: "Blinding Lights",      artist: "The Weeknd",     album: null, genre: null, bpm: 171, key: "11B" },
  { stable_id: "s3", path: "/Users/dev3/Music/sample-omulu.mp3",    title: "Dancing In Your Head", artist: "Omulu",          album: null, genre: null, bpm: 124, key: "8A"  },
];

export default function Palette() {
  const [query, setQuery] = useState("");
  const paletteVisible = true;
  const frecent = useFrecency(paletteVisible);
  const frecentOrSample = frecent.length > 0 ? frecent : SAMPLE;
  const results = useSearch(query, frecentOrSample);
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
            <Command.Empty className="palette-empty">No tracks.</Command.Empty>
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
