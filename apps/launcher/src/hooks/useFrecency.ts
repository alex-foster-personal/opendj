import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";

import type { FrecentHit } from "../types";

/** What the frecency store is doing right now.
 *
 * A bare `FrecentHit[]` cannot say the difference between "nothing played yet"
 * and "the store could not be read", because both arrive as an empty array.
 * The launcher used to paper over that with three invented sample tracks, so a
 * fresh install and a broken one looked identical, and both looked like a
 * library (issue #1542). A discriminated state is what lets the palette tell
 * the user which one they are actually in.
 */
export type FrecencyState =
  | { status: "loading" }
  | { status: "ready"; top: FrecentHit[] }
  | { status: "failed"; error: string };

/** Load top-200 frecent tracks from Rust when the palette becomes visible. */
export function useFrecency(paletteVisible: boolean): FrecencyState {
  const [state, setState] = useState<FrecencyState>({ status: "loading" });
  useEffect(() => {
    if (!paletteVisible) return;
    let cancelled = false;
    setState({ status: "loading" });
    invoke<FrecentHit[]>("get_frecent_top", { limit: 200 })
      .then((top) => {
        if (!cancelled) setState({ status: "ready", top });
      })
      .catch((err: unknown) => {
        // Surfaced, never swallowed: the old `.catch(() => setTop([]))` turned
        // a failed read into an indistinguishable empty library.
        if (!cancelled) {
          setState({ status: "failed", error: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [paletteVisible]);
  return state;
}
