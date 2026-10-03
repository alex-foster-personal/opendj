import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invokeMock(...a) }));
vi.mock("../../hooks/useDragEvents", () => ({
  useDragEvents: () => [[], () => {}],
}));

import Palette from "../Palette";
import type { FrecentHit } from "../../types";

const HIT: FrecentHit = {
  stable_id: "s1",
  path: "/tmp/real.mp3",
  title: "Neon Orchard",
  artist: "Mira Valen",
  album: null,
  genre: null,
  key: "11A",
  bpm: 103,
  score: 1,
  plays: 1,
  drags: 0,
};

/** Every invented title the launcher used to ship as a live fallback.
 *
 * Asserted by NAME rather than by counting rows: a count-based check passes
 * for any three items, including three real ones, so it cannot tell fake data
 * from a loaded library (issue #1542).
 */
const INVENTED = ["Neon Orchard", "Velvet Static", "Dancing In Your Head"];

function frecencyResolves(hits: FrecentHit[]) {
  invokeMock.mockImplementation((cmd: string) =>
    cmd === "get_frecent_top" ? Promise.resolve(hits) : Promise.resolve([]),
  );
}

describe("Palette empty states", () => {
  beforeEach(() => {
    invokeMock.mockReset();
  });

  it("says nothing has been dragged yet when the store is empty, and invents nothing", async () => {
    frecencyResolves([]);
    render(<Palette />);
    await screen.findByText(/No recent tracks yet/i);
    for (const title of INVENTED) {
      expect(screen.queryByText(title)).not.toBeInTheDocument();
    }
  });

  it("says the read FAILED, in different words, when the store cannot be opened", async () => {
    invokeMock.mockImplementation((cmd: string) =>
      cmd === "get_frecent_top"
        ? Promise.reject(new Error("database is locked"))
        : Promise.resolve([]),
    );
    render(<Palette />);
    const failed = await screen.findByText(/Could not read your track history/i);
    expect(failed).toHaveTextContent("database is locked");
    // The two empty states must not be the same sentence, or the user cannot
    // tell a fresh install from a broken one, which is the whole defect.
    expect(screen.queryByText(/No recent tracks yet/i)).not.toBeInTheDocument();
  });

  it("renders the real list once it loads, and no invented row beside it", async () => {
    frecencyResolves([HIT]);
    render(<Palette />);
    await screen.findByText("Neon Orchard");
    expect(screen.queryByText("Velvet Static")).not.toBeInTheDocument();
    expect(screen.queryByText("Dancing In Your Head")).not.toBeInTheDocument();
    expect(screen.queryByText(/No recent tracks yet/i)).not.toBeInTheDocument();
  });

  it("says SEARCHING while the previous rows are still on screen", async () => {
    // Codex P2 BLOCKING: the notice used to be gated on an empty list, so it
    // was silent in exactly the case where retained rows read as current
    // matches for the query being typed.
    let release: (hits: FrecentHit[]) => void = () => {};
    const pending = new Promise<FrecentHit[]>((resolve) => {
      release = resolve;
    });
    invokeMock.mockImplementation((cmd: string) =>
      cmd === "get_frecent_top" ? Promise.resolve([HIT]) : pending,
    );
    render(<Palette />);
    await screen.findByText("Neon Orchard");
    fireEvent.change(screen.getByPlaceholderText("Search tracks..."), {
      target: { value: "blinding" },
    });
    await screen.findByText(/Searching for "blinding"/i, {}, { timeout: 2000 });
    // The stale row is still there, which is why the notice has to be.
    expect(screen.getByText("Neon Orchard")).toBeInTheDocument();
    release([]);
  });

  it("says the SEARCH failed rather than reporting no matches", async () => {
    // The palette wiring, not the sentence: `emptyState.test.ts` owns the
    // wording with nothing stubbed. What this asserts is that a rejected
    // `search_tracks` reaches the render at all, which is the defect (a
    // rejection fell back to the cache and read as a successful empty answer).
    invokeMock.mockImplementation((cmd: string) =>
      cmd === "get_frecent_top"
        ? Promise.resolve([])
        : Promise.reject(new Error("no such table: tracks_fts")),
    );
    render(<Palette />);
    await screen.findByText(/No recent tracks yet/i);
    fireEvent.change(screen.getByPlaceholderText("Search tracks..."), {
      target: { value: "blinding" },
    });
    const failed = await screen.findByText(/Track search failed/i, {}, { timeout: 2000 });
    expect(failed).toHaveTextContent("no such table: tracks_fts");
    expect(screen.queryByText(/No tracks match/i)).not.toBeInTheDocument();
  });

  it("shows a LOADING state before the store answers, distinct from both", async () => {
    let release: (hits: FrecentHit[]) => void = () => {};
    invokeMock.mockImplementation((cmd: string) =>
      cmd === "get_frecent_top"
        ? new Promise<FrecentHit[]>((resolve) => {
            release = resolve;
          })
        : Promise.resolve([]),
    );
    render(<Palette />);
    expect(screen.getByText(/Loading your recent tracks/i)).toBeInTheDocument();
    expect(screen.queryByText(/No recent tracks yet/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not read/i)).not.toBeInTheDocument();
    release([]);
    await waitFor(() => expect(screen.getByText(/No recent tracks yet/i)).toBeInTheDocument());
  });
});
