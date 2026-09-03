/**
 * Map ANLZ cue rows to the deck's hot-cue bank: slotted cues only, sorted by
 * slot. Pure, so it lives outside the engine (convention D5) and is shared by
 * deck load and the slot-state path without either importing the other.
 */
import type { AnlzCue } from "$lib/rb/anlz-types";
import type { HotCue } from "$lib/rb/hot-cue-types";

export function hotCuesFromAnlz(cues: AnlzCue[]): HotCue[] {
  return cues
    .filter(
      (c): c is AnlzCue & { slot: NonNullable<AnlzCue["slot"]> } =>
        c.slot !== null,
    )
    .map((c) => ({
      slot: c.slot,
      in_ms: c.in_ms,
      out_ms: c.out_ms,
      is_loop: c.is_loop,
      beat_loop_size: c.beat_loop_size,
      color_table_index: c.color_table_index,
      comment: c.comment,
    }))
    .sort((a, b) => a.slot.localeCompare(b.slot));
}
