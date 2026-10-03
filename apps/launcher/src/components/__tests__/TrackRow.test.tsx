import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Command } from "cmdk";

const invokeMock = vi.fn().mockResolvedValue(undefined);
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invokeMock(...a) }));

import TrackRow from "../TrackRow";
import type { TrackHit } from "../../types";

const TRACK: TrackHit = {
  stable_id: "s1",
  path: "/tmp/x.mp3",
  title: "Neon Orchard",
  artist: "Mira Valen",
  album: null, genre: null, key: "11A", bpm: 103,
};

function renderRow(track: TrackHit = TRACK) {
  return render(
    <Command label="t">
      <Command.List>
        <TrackRow track={track} />
      </Command.List>
    </Command>,
  );
}

describe("TrackRow", () => {
  beforeEach(() => {
    invokeMock.mockClear();
  });

  it("renders title and artist", () => {
    renderRow();
    expect(screen.getByText("Neon Orchard")).toBeInTheDocument();
    expect(screen.getByText(/Mira Valen/)).toBeInTheDocument();
  });

  it("invokes start_track_drag on mousedown", () => {
    renderRow();
    const row = screen.getByText("Neon Orchard").closest('[cmdk-item]') as HTMLElement;
    expect(row).not.toBeNull();
    fireEvent.mouseDown(row);
    expect(invokeMock).toHaveBeenCalledWith("start_track_drag", {
      path: "/tmp/x.mp3",
      stableId: "s1",
    });
  });

  it("invokes start_track_drag when the row is selected (Enter)", () => {
    renderRow();
    const row = screen.getByText("Neon Orchard").closest('[cmdk-item]') as HTMLElement;
    // cmdk fires onSelect via a custom event; simulate via click (cmdk maps
    // click to onSelect).
    fireEvent.click(row);
    expect(invokeMock).toHaveBeenCalledWith(
      "start_track_drag",
      expect.objectContaining({ path: "/tmp/x.mp3" }),
    );
  });
});
