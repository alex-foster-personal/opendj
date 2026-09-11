/**
 * Deck karaoke: shared shapes.
 *
 * Ported from the af--karaoke-ui-deck spike (specs/features/karaoke-deck-panel/
 * REQUIREMENTS.md R4, R5, R6). The clock/cursor discipline is kept verbatim;
 * the data source changed from a fixture catalog to the daemon's
 * /api/v1/tracks/{id}/lyrics payload, so per-line trust now comes from the
 * server's calibrated witness `band` instead of the spike's local score
 * heuristics (see build-track.ts).
 *
 * THE CLOCK CONTRACT (do not reinvent):
 *  - word timings are in immutable SOURCE-TRACK seconds;
 *  - the active word is chosen from the source position of the sample the
 *    output has ACTUALLY PRESENTED (in production: the engine presented clock,
 *    DeckState.position_ms). Never wall time, never a nominal-latency
 *    subtraction;
 *  - intervals are half-open [start_s, end_s). The inter-word gap is a REAL
 *    state with NO active word - a word is never stretched to cover the gap
 *    that follows it.
 */

/** How much of the lyric we are willing to CLAIM. */
export type LyricsFidelity = 'word' | 'line' | 'region' | 'none';

/** The rungs a single LINE can sit on. A line either has word onsets we trust
 * or it does not; "region" and "none" are whole-track verdicts, not per-line
 * ones, because withholding the text is a decision about the whole display. */
export type LineFidelity = 'word' | 'line';

/** Server-calibrated witness verdict for one line (api.ts LyricLine.band).
 * Mirrors the API strings exactly so the adapter is a pass-through. */
export type LyricLineBand = 'good' | 'uncertain' | 'bad' | 'unjudged';

/** Where the playhead sits relative to the vocal, as a named state. */
export type LyricsPlaybackState = 'preroll' | 'line' | 'gap' | 'outro';

export interface LyricsWord {
	readonly word: string;
	readonly start_s: number;
	readonly end_s: number;
	/** Aligner log-probability (closer to 0 = more confident), or null when
	 * the pipeline stored no score for this word. Kept for tooltips only:
	 * per-line TRUST comes from the server band, never re-derived here. */
	readonly score: number | null;
	readonly line_final: boolean;
	/** The word's idx in the API payload, for traceability across the filter
	 * that drops untimed words (see build-track.ts). */
	readonly src_idx: number;
}

export interface LyricsLine {
	readonly index: number;
	/** Server-canonical line text - includes words that carry no timing. */
	readonly text: string;
	/** Range into LyricsTrack.words (TIMED words only), inclusive. */
	readonly first_word: number;
	readonly last_word: number;
	readonly start_s: number;
	readonly end_s: number;
	/** Whether THIS line renders per-word timing: band 'good' earns the word
	 * wipe, every other band falls back to whole-line lighting. */
	readonly fidelity: LineFidelity;
	/** The server's calibrated witness band, verbatim, for styling + titles. */
	readonly band: LyricLineBand;
	/** Share of witness-JUDGED words not in the red classes; null when the
	 * witness judged nothing in this line. */
	readonly quality: number | null;
	readonly n_red: number;
	readonly n_judged: number;
}

export interface LyricsTrack {
	readonly id: string;
	readonly artist: string;
	readonly title: string;
	readonly language_iso: string;
	readonly duration_s: number;
	/** Timed words only, source order. Untimed words are dropped from the
	 * cursor's domain (they cannot be placed on the clock) but survive in
	 * each line's server text. */
	readonly words: readonly LyricsWord[];
	readonly lines: readonly LyricsLine[];
	/** The track's effective verdict (human override applied server-side). */
	readonly verdict: 'vocal' | 'sparse' | 'no-lyrics' | 'unknown';
	/** How many lines earned per-word timing. The mix is per-LINE, not one
	 * verdict per track. */
	readonly word_fidelity_lines: number;
	/** Lines per band, for the summary tooltip. */
	readonly band_counts: Readonly<Record<LyricLineBand, number>>;
}

/** Listing-row lyric summary (bulk-joined server-side, one SELECT per page).
 * `has_words` gates word-level UI: a verdict can exist from stem coverage
 * alone before any alignment ran. */
export interface LyricsRowSummary {
	effective: 'vocal' | 'sparse' | 'no-lyrics' | 'unknown';
	n_words: number | null;
	has_words: boolean;
	/** Line count from derive_lines semantics; null until alignment ran. */
	n_lines: number | null;
	pct_witness_red: number | null;
	source: string | null;
	language_iso3: string | null;
	override: string | null;
}

/**
 * The semantic payload emitted on every active-word / state change.
 *
 * STRUCTURE, NOT PIXELS: the deck strip, a crowd display and an LED bar all
 * consume this same object, so it carries indices, text and normalised
 * progress - never colours, sizes or DOM.
 */
export interface LyricsFrame {
	readonly schema: 'lyrics.frame/1';
	/** Presented source-track position, milliseconds. */
	readonly source_ms: number;
	/** The EFFECTIVE rung for the line in this frame, after per-line
	 * degradation. Downstream consumers inherit the same honesty rather than
	 * re-deriving it from bands. */
	readonly fidelity: LyricsFidelity;
	readonly state: LyricsPlaybackState;
	readonly line_index: number | null;
	/** null during an inter-word gap - an honest "nothing is being sung". */
	readonly word_index: number | null;
	readonly lines: {
		readonly prev: string | null;
		readonly current: string | null;
		readonly next: string | null;
	};
	/** Span of the active word in source ms, or null in a gap. */
	readonly word_span_ms: { readonly start: number; readonly end: number } | null;
	readonly progress: {
		/** 0..1 through the current line, by source time. */
		readonly line: number;
		/** 0..1 through the active word, null in a gap. */
		readonly word: number | null;
	};
	/** ms until the next vocal onset, or null when a word is sounding. */
	readonly next_vocal_in_ms: number | null;
	readonly track: {
		readonly id: string;
		readonly language_iso: string;
		readonly word_count: number;
		readonly line_count: number;
		/** Lines that earned per-word timing, so a downstream consumer can see
		 * this frame's rung is part of a MIX rather than a whole-track verdict. */
		readonly word_fidelity_lines: number;
	};
}
