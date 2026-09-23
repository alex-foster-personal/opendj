/**
 * Pure helpers bridging a beatgrid-fallback response into the existing
 * wavestack painter (build unit: wavestack). No runes, no DOM - unit-
 * testable in isolation. ANLZ always preferred, never invented.
 */
import { hasRealBeatGrid } from '$lib/player/grid-features';
import type { BeatgridFallbackOut } from './beatgrid-fallback-api';
import type { AnlzBeatgrid, AnlzData } from './anlz-types';
import type { RbMeta } from './library-types';

/** Everything the gate below is allowed to reason about. Every field is
 * required: a caller that does not know one yet passes the explicit null
 * for it rather than omitting it, because "unknown" and "absent" decide
 * this gate differently. */
export interface BeatgridFallbackGate {
	/** The /anlz error code; null when /anlz answered 200 or has not been
	 * asked yet (which `anlz` then distinguishes). */
	anlzErrorCode: string | null;
	/** The /anlz 200 payload; null when /anlz errored or is still in flight. */
	anlz: AnlzData | null;
	/** RbMetaOut.vendor for this track (apps/webui/server/routes/rb_assets.py);
	 * null while /rb-meta has not answered - a vendor mapping is never guessed. */
	vendor: RbMeta['vendor'] | null;
	/** PARITY-02's effective 'beatgrid' analysis-source selection
	 * (analysis-source.svelte.ts's `analysisSourceState.features.beatgrid`).
	 * `undefined` means the client has not yet polled the daemon for it and
	 * is treated the same as an explicit 'rekordbox' selection - this gate
	 * never substitutes an own-derived grid the daemon has not confirmed
	 * selecting (discussion_r3972682719 P1 BLOCKING). Callers with an
	 * in-flight fetch must re-read this live and re-run the gate right
	 * before publishing, not trust the value captured when the fetch began:
	 * a switch back to rekordbox mid-fetch must not land an own grid after
	 * the fact. */
	effectiveSource: 'rekordbox' | 'own' | undefined;
}

/** Whether this track should go looking for an analysis-derived beatgrid.
 * ANLZ is ALWAYS preferred, so this is the single gate a caller checks
 * before ever fetching the fallback grid. Two ways in:
 *
 * 1. `ANALYSIS_NOT_FOUND` - a rekordbox-MAPPED track whose ANLZ file is
 *    gone, so there is no payload at all to read.
 * 2. A 200 /anlz payload with an EMPTY grid on a track with NO vendor
 *    mapping. rb_vendor.empty_anlz_payload serves exactly this for a
 *    locally imported file, and it is the whole parity gap: without
 *    rekordbox there is nothing to prefer, so apps.analysis is the only
 *    grid there will ever be (PARITY-TODO "Deck consumes the fallback for
 *    unmapped tracks").
 *
 * A rekordbox-mapped track with no PQTZ is rekordbox's own answer and is
 * left alone, and an unknown vendor is never guessed at.
 *
 * An empty OWN block (source: 'own', status: 'missing' or 'failed') is also
 * left alone, never patched over by this fallback: /beatgrid-fallback reads
 * the LEGACY librosa/MIK analysis row (own_% rows are explicitly excluded
 * server-side, apps/webui/server/routes/analysis.py's `_load_latest_record`)
 * but always labels its response `source: "own"` regardless, so accepting
 * it here would silently replace an own lane's authoritative terminal
 * answer - missing (the backfill queue has not reached this track) or
 * failed (the analyzer ran and could not measure) - with a legacy grid
 * dressed up as if own had produced it, re-enabling quantize and Beat Sync
 * against a measurement own already rejected or has not reached (Codex P1
 * BLOCKING, PR #1587). */
export function hasNoVendorAnlzPayload(anlz: AnlzData): boolean {
	return anlz.local_waveform !== undefined;
}

export function shouldUseBeatgridFallback(gate: BeatgridFallbackGate): boolean {
	if (gate.anlzErrorCode === 'ANALYSIS_NOT_FOUND') return gate.effectiveSource === 'own';
	if (gate.anlzErrorCode !== null) return false; // another lane's failure
	if (gate.anlz === null) return false; // /anlz has not answered yet
	if (hasAnlzBeatgrid(gate.anlz)) return false; // real grid always wins
	if (gate.anlz.beatgrid.source === 'own') {
		const status = gate.anlz.beatgrid.status;
		if (status === 'missing' || status === 'failed') return false;
	}
	if (hasNoVendorAnlzPayload(gate.anlz)) return true;
	// PARITY-02: an empty grid from a rekordbox-selected lane means "no grid
	// FROM REKORDBOX", not "no grid at all, so show whatever we have" - a DJ
	// who explicitly selected rekordbox must not be quietly handed an
	// own-analysis grid while the toggle and IPC still report rekordbox.
	if (gate.effectiveSource !== 'own') return false;
	return gate.vendor === 'local';
}

/** Whether an /anlz payload carries a grid the deck can actually use.
 *
 * Delegates to grid-features' hasRealBeatGrid, which is the SAME predicate
 * quantize and Beat Sync are gated on, so "has a grid" cannot come to mean
 * one thing in this lane and another inside the beat math. `beats` is the
 * authority, not `beat_count`: every consumer reads the array, so a count
 * that disagrees with it must not lock a track out of the fallback. */
export function hasAnlzBeatgrid(anlz: AnlzData): boolean {
	return hasRealBeatGrid(anlz.beatgrid.beats);
}

/** Whether two beatgrids are the SAME measurement, beat for beat.
 *
 * Exact rather than heuristic (no beat-count-only or endpoint-only compare):
 * this decides whether the engine republishes a deck's grid and re-runs Beat
 * Sync reconciliation, and a false "same" silently pins the deck to the wrong
 * grid while a false "different" churns a locked follower's transport for no
 * reason. Grids are a few thousand beats at most and this is asked only when
 * the shared cache learns a real grid for a currently loaded track, so the
 * linear scan is not on any hot path. */
export function sameBeatgrid(left: AnlzBeatgrid, right: AnlzBeatgrid): boolean {
	if (left.beats.length !== right.beats.length) return false;
	return left.beats.every((beat, index) => {
		const other = right.beats[index];
		return (
			beat.n === other.n &&
			beat.t === other.t &&
			beat.bpm === other.bpm &&
			(beat.extrapolated === true) === (other.extrapolated === true)
		);
	});
}

/** The /anlz payload with the analysis-derived grid dropped into it, so a
 * deck loaded from a 200-but-empty payload gets real beat ticks without
 * losing the fields that payload genuinely carried (cues, waveform,
 * vocals). Returns a NEW object: the deck-snapshot beatgrid memo is keyed
 * on ANLZ identity, so mutating in place would serve a stale grid.
 *
 * Throws rather than overwrite a real ANLZ grid - that inversion would
 * silently demote rekordbox's own measurement to ours.
 *
 * Also overwrites `beatgrid_source`/`beatgrid_own_unavailable_reason`, not
 * just `beatgrid`: `anlz` can have been fetched while the server's effective
 * source was still 'rekordbox' (a stale deck load, or a switch to 'own'
 * that landed after /anlz answered), so its stamp says 'rekordbox' even
 * though the beats this function is about to install came from apps.analysis.
 * Spreading `anlz` unmodified would ship that stale stamp downstream -
 * anlz-cache.svelte.ts's grid/tempo pairing check and StripWaveform's
 * own-unavailable-reason display both read `beatgrid_source` as the
 * authority on where the grid came from, not `shouldUseBeatgridFallback`'s
 * gate (discussion_r3972682719 P1 BLOCKING). */
export function withFallbackBeatgrid<T extends AnlzData>(
	anlz: T,
	fallback: BeatgridFallbackOut
): T {
	if (hasAnlzBeatgrid(anlz)) {
		throw new Error(
			`withFallbackBeatgrid: track ${anlz.stable_id} already has a real ANLZ ` +
				`beatgrid (${anlz.beatgrid.beats.length} beats); ANLZ is always preferred`
		);
	}
	return {
		...anlz,
		beatgrid: fallback.beatgrid,
		beatgrid_source: 'own',
		beatgrid_own_unavailable_reason: null
	};
}

/** Wrap a beatgrid-fallback response in an AnlzData-shaped payload so
 * drawWaveRow / barsToNextCueLabel render it unmodified: real beat ticks
 * plus a bars-to-grid-end countdown. Waveform bands stay empty (no
 * fabricated audio), cues/phrases stay empty (this pipeline proposes
 * neither), and vocals is reported honestly as 'not_analyzed' (this
 * pipeline never touches PVDI).
 *
 * For the case where /anlz DID answer with a real (if gridless) payload,
 * use withFallbackBeatgrid instead - it keeps that payload's own fields. */
export function toSyntheticAnlzData(
	fallback: BeatgridFallbackOut
): AnlzData & { vocals: { status: 'not_analyzed' } } {
	return {
		stable_id: fallback.stable_id,
		points: 0,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: fallback.beatgrid,
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' },
		// This bridge always carries the apps.analysis grid (that's the whole
		// point of /beatgrid-fallback). Every caller reaches this only through
		// shouldUseBeatgridFallback, which now requires effectiveSource ===
		// 'own' (PARITY-02), so 'own' is always the correct stamp here, never
		// independent of the selector.
		beatgrid_source: 'own',
		beatgrid_own_unavailable_reason: null
	};
}

/** What the wavestack painter and bars-label consume (WaveRow.svelte): the
 * real ANLZ payload when present (its own grid, or the fallback grid merged
 * into it), else a synthesized beatgrid-only payload, else null.
 *
 * hasAnlzBeatgrid is re-asked rather than inferred from the fallback gate: a
 * stale cache entry can report ANALYSIS_NOT_FOUND while the deck still holds
 * a real grid, and withFallbackBeatgrid throws on that - which a Svelte
 * `$derived` must never do. A real ANLZ grid wins here exactly as it does in
 * the gate. */
export function resolvePaintAnlz(
	anlz: AnlzData | null,
	fallback: BeatgridFallbackOut | null
): AnlzData | null {
	if (anlz === null) return fallback !== null ? toSyntheticAnlzData(fallback) : null;
	if (fallback === null || hasAnlzBeatgrid(anlz)) return anlz;
	return withFallbackBeatgrid(anlz, fallback);
}
