import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const invokeMock = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({ invoke: (...a: unknown[]) => invokeMock(...a) }));

import { useSearch } from "../useSearch";
import type { TrackHit } from "../../types";

const FRECENCY: TrackHit[] = [
  { stable_id: "s1", path: "/a.mp3", title: "Neon Orchard",      artist: "Mira Valen",   album: null, genre: null, key: "11A", bpm: 103 },
  { stable_id: "s2", path: "/b.mp3", title: "Velvet Static", artist: "The Lowlands", album: null, genre: null, key: "11B", bpm: 171 },
];

// Hoisted, NOT an inline `[]`: `frecencyTop` is a useEffect dependency, so a
// fresh array literal on every render re-runs the effect forever. Written
// inline first, and the whole vitest worker died of heap exhaustion.
const NO_FRECENCY: TrackHit[] = [];

describe("useSearch", () => {
  beforeEach(() => {
    invokeMock.mockReset();
  });

  it("returns frecency as-is for empty query", () => {
    const { result } = renderHook(() => useSearch("", FRECENCY));
    expect(result.current).toEqual({ status: "ready", hits: FRECENCY });
  });

  it("uses match-sorter (no IPC) for 1-3 char queries", () => {
    const { result } = renderHook(() => useSearch("mir", FRECENCY));
    // match-sorter should find "Mira Valen"
    expect(result.current.hits.map((t) => t.stable_id)).toContain("s1");
    expect(result.current.status).toBe("ready");
    expect(invokeMock).not.toHaveBeenCalled();
  });

  it("debounces + calls FTS5 for 4+ char queries", async () => {
    invokeMock.mockResolvedValue([FRECENCY[1]]);
    const { result } = renderHook(() => useSearch("blin", FRECENCY));
    await waitFor(
      () => {
        expect(invokeMock).toHaveBeenCalledWith(
          "search_tracks",
          expect.objectContaining({ query: "blin", limit: 20 }),
        );
      },
      { timeout: 500 },
    );
    await waitFor(() => {
      expect(result.current).toEqual({ status: "ready", hits: [FRECENCY[1]] });
    });
  });

  // The two regressions below are the reason this hook returns a state rather
  // than an array. Both used to arrive as `[]` with status "ready", which the
  // palette rendered as an authoritative "no tracks" (Codex P1, PR #1633).

  it("says SEARCHING before the answer arrives, never a ready empty list", async () => {
    // The deferred is built BEFORE the render, not inside the mock body: the
    // 80 ms debounce means invoke has not been called yet when the assertion
    // on "searching" passes, so a resolver captured in the body is still the
    // no-op placeholder at the moment the test tries to release it.
    let release: (hits: TrackHit[]) => void = () => {};
    const pending = new Promise<TrackHit[]>((resolve) => {
      release = resolve;
    });
    invokeMock.mockReturnValue(pending);
    // An EMPTY cache is the case that used to lie: there is nothing to keep on
    // screen, so the pending window rendered as a definitive empty answer.
    const { result } = renderHook(() => useSearch("blin", NO_FRECENCY));
    await waitFor(() => expect(result.current.status).toBe("searching"));
    expect(result.current.hits).toEqual([]);
    release([FRECENCY[1]]);
    await waitFor(() => expect(result.current.status).toBe("ready"));
  });

  it("says FAILED, and carries the error, when search_tracks rejects", async () => {
    invokeMock.mockRejectedValue(new Error("no such table: tracks_fts"));
    const { result } = renderHook(() => useSearch("blin", NO_FRECENCY));
    await waitFor(() => expect(result.current.status).toBe("failed"));
    expect(result.current).toMatchObject({
      status: "failed",
      error: "no such table: tracks_fts",
      hits: [],
    });
  });

  it("keeps the cache fallback on failure, and still reports FAILED", async () => {
    invokeMock.mockRejectedValue(new Error("database is locked"));
    const { result } = renderHook(() => useSearch("velvet", FRECENCY));
    await waitFor(() => expect(result.current.status).toBe("failed"));
    // The fallback is the point of contention: it must not be what hides the
    // failure, so the rows come back AND the status still says failed.
    expect(result.current.hits.map((t) => t.stable_id)).toContain("s2");
  });
});
