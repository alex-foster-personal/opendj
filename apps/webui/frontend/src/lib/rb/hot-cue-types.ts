/**
 * Hot-cue bank contract: the A..H slot letters and the per-slot read model
 * the Deck renders. djmdCue Kind 1..8 maps to A..H (COMPONENT-MAP 2.3).
 *
 * Split out of the former lib/rb/types.ts god module.
 */

/** Hot-cue bank slot letter. djmdCue Kind 1..8 maps to A..H (COMPONENT-MAP 2.3). */
export type HotCueSlot = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H';

/** A hot-cue bank slot as the Deck component consumes it (derived from
 * AnlzCue rows with kind 'hot_cue' or 'loop' and slot != null). */
export interface HotCue {
	/** Bank letter A..H. */
	slot: HotCueSlot;
	/** Jump target in ms; clicking a populated slot seeks here (real). */
	in_ms: number;
	/** Loop-out ms when the slot stores a loop; null otherwise. */
	out_ms: number | null;
	/** True when the slot stores a loop (renders loop glyph + time chips). */
	is_loop: boolean;
	/** Beat length when beat-quantised (djmdCue BeatLoopSize / decoded); null otherwise. */
	beat_loop_size: number | null;
	/** rekordbox ColorTableIndex (0..62); null = default colour. */
	color_table_index: number | null;
	/** User comment; null when unset. */
	comment: string | null;
}
