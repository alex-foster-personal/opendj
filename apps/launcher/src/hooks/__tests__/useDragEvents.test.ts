import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";

// Capture one listener per event name keyed by the subscribed event so the
// test can fire them synchronously without touching the real Tauri IPC.
const listeners = new Map<string, (ev: { payload: unknown }) => void>();
const unlistenSpy = vi.fn();

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn((name: string, cb: (ev: { payload: unknown }) => void) => {
    listeners.set(name, cb);
    return Promise.resolve(unlistenSpy);
  }),
}));

import { formatMessage, useDragEvents } from "../useDragEvents";

describe("formatMessage", () => {
  it("formats drag-started with vendor", () => {
    expect(formatMessage("drag-started", { vendor: "djay" })).toBe(
      "Dragging to djay",
    );
  });

  it("formats drag-success with vendor", () => {
    expect(formatMessage("drag-success", { vendor: "serato" })).toBe(
      "Dropped to serato",
    );
  });

  it("formats drag-fallback with detail", () => {
    expect(
      formatMessage("drag-fallback", { vendor: "rekordbox", detail: "xml" }),
    ).toBe("Fallback to rekordbox: xml");
  });

  it("formats drag-failed with no vendor but with detail", () => {
    expect(formatMessage("drag-failed", { detail: "perm denied" })).toBe(
      "Drag failed: perm denied",
    );
  });

  it("falls back to bare labels when payload is empty", () => {
    expect(formatMessage("drag-started", {})).toBe("Dragging");
    expect(formatMessage("drag-fallback", {})).toBe("Fallback");
    expect(formatMessage("drag-failed", {})).toBe("Drag failed");
  });
});

describe("useDragEvents", () => {
  beforeEach(() => {
    listeners.clear();
    unlistenSpy.mockClear();
  });

  it("registers listeners for all four drag lifecycle events", async () => {
    renderHook(() => useDragEvents(0));
    // The hook subscribes inside a useEffect; the mocked listen resolves
    // synchronously, so by the next microtask all four listeners are in
    // the map.
    await Promise.resolve();
    expect([...listeners.keys()].sort()).toEqual([
      "drag-failed",
      "drag-fallback",
      "drag-started",
      "drag-success",
    ]);
  });

  it("surfaces incoming events as toasts", async () => {
    const { result } = renderHook(() => useDragEvents(0));
    await Promise.resolve();

    act(() => {
      listeners.get("drag-success")!({
        payload: { vendor: "djay", outcome: "started", detail: "" },
      });
    });

    expect(result.current[0]).toHaveLength(1);
    expect(result.current[0][0].kind).toBe("drag-success");
    expect(result.current[0][0].message).toBe("Dropped to djay");
  });

  it("dismiss() removes a toast by id", async () => {
    const { result } = renderHook(() => useDragEvents(0));
    await Promise.resolve();

    act(() => {
      listeners.get("drag-failed")!({
        payload: { vendor: "rekordbox", detail: "xml locked" },
      });
    });
    const id = result.current[0][0].id;
    act(() => {
      result.current[1](id);
    });
    expect(result.current[0]).toHaveLength(0);
  });
});
