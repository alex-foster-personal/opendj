/**
 * The empty-state DECISION, exhaustively, with nothing stubbed.
 *
 * `emptyMessage` and `searchFailureMessage` take plain values and return
 * strings, so these tests need no IPC, no renderer and no jsdom capability
 * that jsdom lacks. That is deliberate: it is the half of issue #1542 that
 * can be tested against the production code path with no test double
 * anywhere, and it is where the acceptance criteria live.
 *
 * Regression lines:
 *   - if two distinguishable situations produce the same sentence then broken
 *     (that identity IS the defect: a fresh install read like a broken one)
 *   - if a pending search reports no matches then broken
 *   - if a failed search reports no matches then broken
 *   - if the empty-recents sentence names an action that does not populate
 *     recents then broken (nothing writes plays/last_played_at)
 */
import { describe, it, expect } from "vitest";

import { emptyMessage, searchNoticeMessage } from "../emptyState";
import type { FrecencyState } from "../../hooks/useFrecency";
import type { TrackHit } from "../../types";

const LOADING: FrecencyState = { status: "loading" };
const READY: FrecencyState = { status: "ready", top: [] };
const BROKEN: FrecencyState = { status: "failed", error: "database is locked" };

describe("emptyMessage", () => {
  it("says LOADING while the recents store has not answered", () => {
    expect(emptyMessage(LOADING, "")).toMatch(/loading/i);
  });

  it("names the error when the recents store could not be read", () => {
    expect(emptyMessage(BROKEN, "")).toContain("database is locked");
  });

  it("says nothing has been played yet when the store is genuinely empty", () => {
    expect(emptyMessage(READY, "")).toMatch(/no recent tracks yet/i);
  });

  it("points at DRAGGING, the only action that populates recents", () => {
    // `record_drag` in src-tauri/src/commands/frecency.rs is the sole writer of
    // tracks_frecency; nothing writes plays or last_played_at. An instruction
    // to play something would leave the user in this same state forever.
    const message = emptyMessage(READY, "");
    expect(message).toMatch(/drag/i);
    expect(message).not.toMatch(/play something/i);
  });

  it("distinguishes a short-query cache miss from a whole-library miss", () => {
    const short = emptyMessage(READY, "abc");
    const long = emptyMessage(READY, "abcd");
    expect(short).not.toEqual(long);
    // The short one must SAY that it only looked at recents, or the user reads
    // it as proof the library holds nothing.
    expect(short).toMatch(/recent/i);
    expect(long).toMatch(/no tracks match "abcd"/i);
  });

  it("prefers the store error over a no-match claim while filtering a dead cache", () => {
    // 1-3 chars filter the recents cache in memory. If that cache failed to
    // load, "no match" is a claim about a list that was never read.
    expect(emptyMessage(BROKEN, "abc")).toContain("database is locked");
  });

  it("gives every distinguishable situation its own sentence", () => {
    const sentences = [
      emptyMessage(LOADING, ""),
      emptyMessage(BROKEN, ""),
      emptyMessage(READY, ""),
      emptyMessage(READY, "abc"),
      emptyMessage(READY, "abcd"),
    ];
    expect(new Set(sentences).size).toBe(sentences.length);
  });
});

const HIT: TrackHit = {
  stable_id: "s1", path: "/a.mp3", title: "Neon Orchard", artist: "Mira Valen",
  album: null, genre: null, key: "11A", bpm: 103,
};

// LAUNCH-04 (issue #2759): the requirement names five palette states --
// loading / error / searching / no-drags-yet / no-match -- and the acceptance
// criterion is that they are five DIFFERENT sentences, not five call sites
// that happen to share one. `emptyMessage` alone only ever reaches four of
// them; `searching` comes out of `searchNoticeMessage`, so this test spans
// both functions the way the palette actually renders them side by side.
describe("LAUNCH-04 five empty states", () => {
  it("gives each of the five states its own non-empty sentence", () => {
    const states: Record<string, string | null> = {
      loading: emptyMessage(LOADING, ""),
      error: emptyMessage(BROKEN, ""),
      searching: searchNoticeMessage({ status: "searching", hits: [] }, "blinding"),
      "no-drags-yet": emptyMessage(READY, ""),
      "no-match": emptyMessage(READY, "abcd"),
    };

    for (const [name, message] of Object.entries(states)) {
      expect(message, `${name} must not be empty`).toBeTruthy();
      expect((message ?? "").length, `${name} must not be empty`).toBeGreaterThan(0);
    }

    const sentences = Object.values(states);
    expect(new Set(sentences).size, "all five states must read differently").toBe(
      sentences.length,
    );
  });
});

describe("searchNoticeMessage", () => {
  it("is null only when the search is settled", () => {
    expect(searchNoticeMessage({ status: "ready", hits: [] }, "blinding")).toBeNull();
    expect(searchNoticeMessage({ status: "ready", hits: [HIT] }, "blinding")).toBeNull();
  });

  it("says SEARCHING while a query is in flight, with rows still showing", () => {
    // The case Codex flagged: `useSearch` retains the previous hits, so a
    // notice gated on an empty list is silent exactly when stale draggable
    // rows are on screen looking like current matches.
    const message = searchNoticeMessage({ status: "searching", hits: [HIT] }, "blinding");
    expect(message).toMatch(/searching/i);
    expect(message).not.toMatch(/no tracks match/i);
  });

  it("says SEARCHING with no rows showing too", () => {
    expect(searchNoticeMessage({ status: "searching", hits: [] }, "blinding")).toMatch(
      /searching/i,
    );
  });

  it("names the error when the fallback found nothing", () => {
    const message = searchNoticeMessage(
      { status: "failed", error: "no such table: tracks_fts", hits: [] },
      "blinding",
    );
    expect(message).toContain("no such table: tracks_fts");
  });

  it("still names the error when the fallback DID find rows", () => {
    // The silent fallback is what made a backend failure invisible; showing
    // rows must not be allowed to swallow the reason they are cached ones.
    const message = searchNoticeMessage(
      { status: "failed", error: "database is locked", hits: [HIT] },
      "blinding",
    );
    expect(message).toContain("database is locked");
    expect(message).toMatch(/recent/i);
  });
});
