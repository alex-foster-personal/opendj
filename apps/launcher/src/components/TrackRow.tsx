import { Command } from "cmdk";
import { invoke } from "@tauri-apps/api/core";

import type { TrackHit } from "../types";

interface Props {
  track: TrackHit;
}

export default function TrackRow({ track }: Props) {
  const dispatchDrag = () => {
    invoke("start_track_drag", { path: track.path, stableId: track.stable_id })
      .then(() => invoke("record_drag", { stableId: track.stable_id }).catch(() => undefined))
      .catch((e) => {
        // eslint-disable-next-line no-console
        console.error("start_track_drag failed:", e);
      });
  };

  return (
    <Command.Item
      value={`${track.title ?? ""} ${track.artist ?? ""}`}
      onSelect={dispatchDrag}
      onMouseDown={(e) => {
        e.preventDefault();
        dispatchDrag();
      }}
      className="palette-item"
    >
      <div className="palette-row">
        <strong className="palette-title">{track.title ?? track.path}</strong>
        <span className="palette-artist"> {track.artist ? `- ${track.artist}` : ""}</span>
        <span className="palette-meta">
          {track.bpm != null ? `${track.bpm.toFixed(0)} BPM` : ""}
          {track.bpm != null && track.key ? " - " : ""}
          {track.key ?? ""}
        </span>
      </div>
    </Command.Item>
  );
}
