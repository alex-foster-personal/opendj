import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";

import type { FrecentHit } from "../types";

/** Load top-200 frecent tracks from Rust when the palette becomes visible. */
export function useFrecency(paletteVisible: boolean): FrecentHit[] {
  const [top, setTop] = useState<FrecentHit[]>([]);
  useEffect(() => {
    if (!paletteVisible) return;
    invoke<FrecentHit[]>("get_frecent_top", { limit: 200 })
      .then(setTop)
      .catch(() => setTop([]));
  }, [paletteVisible]);
  return top;
}
