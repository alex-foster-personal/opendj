import { useEffect, useState } from "react";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

// Event-name constants mirror commands/drag.rs -> TauriHostBridge::emit_event.
// Keep these in sync with DragEvent::name values produced by drag-core.
export const DRAG_EVENTS = [
  "drag-started",
  "drag-success",
  "drag-fallback",
  "drag-failed",
] as const;

export type DragEventName = (typeof DRAG_EVENTS)[number];

/** Matches the JSON payload emitted by TauriHostBridge::emit_event:
 *   { vendor: "djay" | "rekordbox" | "serato" | "traktor" | ...,
 *     outcome: "started" | "fallback1" | "fallback2" | "unsupported" | ...,
 *     detail: string }
 * All fields are best-effort strings because drag-core may evolve the shape. */
export interface DragEventPayload {
  vendor?: string | null;
  outcome?: string | null;
  detail?: string | null;
}

export interface DragToast {
  /** Monotonic id so React can key / dismiss individual toasts. */
  id: number;
  /** Event name that produced this toast (drives the visual tone). */
  kind: DragEventName;
  /** Short user-facing sentence ("Dragging to djay", "Dropped via fallback", ...). */
  message: string;
  /** Timestamp the toast was created (ms since epoch). */
  createdAt: number;
}

/**
 * Subscribe to Rust-side drag-lifecycle events and surface them as toasts.
 *
 * Returns `[toasts, dismiss]`:
 * - `toasts` is the currently-live toast list (auto-expires after `ttlMs`).
 * - `dismiss(id)` removes a toast by id (wire to an X button if desired).
 *
 * The hook owns a monotonic counter so two events arriving in the same
 * millisecond still get distinct keys. Listeners are torn down on unmount.
 */
export function useDragEvents(
  ttlMs: number = 3000,
): [DragToast[], (id: number) => void] {
  const [toasts, setToasts] = useState<DragToast[]>([]);

  useEffect(() => {
    let nextId = 1;
    const unlisteners: Array<Promise<UnlistenFn>> = [];
    let cancelled = false;

    for (const name of DRAG_EVENTS) {
      const p = listen<DragEventPayload>(name, (ev) => {
        if (cancelled) return;
        const payload = ev.payload ?? {};
        const message = formatMessage(name, payload);
        const toast: DragToast = {
          id: nextId++,
          kind: name,
          message,
          createdAt: Date.now(),
        };
        setToasts((prev) => [...prev, toast]);
        if (ttlMs > 0) {
          setTimeout(() => {
            setToasts((prev) => prev.filter((t) => t.id !== toast.id));
          }, ttlMs);
        }
      });
      unlisteners.push(p);
    }

    return () => {
      cancelled = true;
      unlisteners.forEach((p) => {
        p.then((un) => un()).catch(() => undefined);
      });
    };
  }, [ttlMs]);

  const dismiss = (id: number) =>
    setToasts((prev) => prev.filter((t) => t.id !== id));

  return [toasts, dismiss];
}

/** Pure helper (exported for unit tests) that turns a raw drag event into a
 * short user-facing sentence. Kept deterministic so snapshot tests are trivial. */
export function formatMessage(
  kind: DragEventName,
  payload: DragEventPayload,
): string {
  const vendor = (payload.vendor ?? "").trim();
  const detail = (payload.detail ?? "").trim();
  const vendorSuffix = vendor ? ` to ${vendor}` : "";
  switch (kind) {
    case "drag-started":
      return `Dragging${vendorSuffix}`;
    case "drag-success":
      return `Dropped${vendorSuffix}`;
    case "drag-fallback":
      return detail
        ? `Fallback${vendorSuffix}: ${detail}`
        : `Fallback${vendorSuffix}`;
    case "drag-failed":
      return detail ? `Drag failed: ${detail}` : "Drag failed";
  }
}
