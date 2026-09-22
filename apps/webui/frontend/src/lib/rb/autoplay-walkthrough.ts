/**
 * Pure timeline for the AutoPlay explainer's mini-library walkthrough
 * animation. No DOM, no Svelte, no wall-clock reads - frameAt is a lookup
 * function of "how far in" to "what's on screen," same shape as
 * autoplay-curve.ts's buildCurveSegments. Fake rows, real compatibility
 * rules: pass/fail, chain order, and stranding are all computed via the
 * actual picker predicates (camelotKeysAreCompatible, bpmWithinPhaseLockRange,
 * simulateAutoPlayChain), never hand-typed, so the demo can never silently
 * disagree with what AutoPlay really does.
 */

import { camelotKeysAreCompatible, parseCamelotKey } from '$lib/player/key/camelot';
import {
	bpmWithinPhaseLockRange,
	simulateAutoPlayChain,
	tempoBoundsFromPitchRange,
	type AutoPlayTrackRow
} from '$lib/rb/auto-play';

export type WalkthroughMode = 'greedy' | 'reach' | 'enforce';

export interface WalkthroughRow {
	id: string;
	/** Fake Camelot label, e.g. "8A". */
	key: string;
	/** Fake BPM. */
	bpm: number;
}

// ----- DEMO fixture (SA5 4.1, hand-verified against the real predicates) --

/** Fixed fake fixture - never real track data. Referenced by id from the
 * timeline below, so row list and timeline can never drift out of sync. */
export const DEMO_ANCHOR: WalkthroughRow = { id: 'demo-anchor', key: '8A', bpm: 128 };
export const DEMO_ROWS: readonly WalkthroughRow[] = [
	{ id: 'demo-1', key: '8A', bpm: 124 }, // reachable in every mode - greedy's first pick
	{ id: 'demo-2', key: '9A', bpm: 137 }, // reachable in every mode - reach's first pick instead
	{ id: 'demo-3', key: '2A', bpm: 128 }, // key-only reject: dist(2,8)=6, bpm matches anchor exactly
	{ id: 'demo-4', key: '8A', bpm: 220 }, // BPM-only reject: same key as anchor, ratio always > 1.16
	{ id: 'demo-5', key: '7A', bpm: 145 }, // stranded under greedy; reached under reach (visited last)
	{ id: 'demo-6', key: '7A', bpm: 143 } // stranded under greedy; reached under reach
];

const DEMO_PITCH_RANGE_PCT = 16; // same default DeckHeader/beatSyncBullets use

function _asTrackRow(row: WalkthroughRow): AutoPlayTrackRow {
	return { stable_id: row.id, key: row.key, bpm: row.bpm, file_exists: true };
}

/** True fixture-wide compatibility with the anchor, computed via the real
 * predicates - not a hand-typed flag. */
export function isDemoRowCompatible(row: WalkthroughRow): boolean {
	if (!camelotKeysAreCompatible(row.key, DEMO_ANCHOR.key)) return false;
	const bounds = tempoBoundsFromPitchRange(DEMO_PITCH_RANGE_PCT);
	return bpmWithinPhaseLockRange(row.bpm, DEMO_ANCHOR.bpm, bounds.min, bounds.max);
}

/** Camelot number (1-12) to a 0-360 hue; compatible keys (circular distance
 * <= 1) are always adjacent on this wheel, since that distance IS the real
 * compatibility check (audio-engine.svelte.ts's camelotKeysAreCompatible) -
 * no separate metric. */
export function camelotHue(key: string): number | null {
	const parsed = parseCamelotKey(key);
	return parsed === null ? null : ((parsed.number - 1) / 12) * 360;
}

// ----- real chain results (module load, never re-derived per frame) -------

const _PLAYLIST: readonly AutoPlayTrackRow[] = [DEMO_ANCHOR, ...DEMO_ROWS].map(_asTrackRow);
const _BOUNDS = tempoBoundsFromPitchRange(DEMO_PITCH_RANGE_PCT);

function _simulateChain(maximizeReach: boolean, enforcePlayOrder: boolean): readonly string[] {
	return simulateAutoPlayChain({
		playlist: _PLAYLIST,
		start_stable_id: DEMO_ANCHOR.id,
		enforce_play_order: enforcePlayOrder,
		maximize_reach: maximizeReach,
		min_tempo_ratio: _BOUNDS.min,
		max_tempo_ratio: _BOUNDS.max
	});
}

/** Every row individually anchor-compatible, in fixture order (demo-1, demo-2, demo-5, demo-6). */
const _COMPATIBLE_IDS: readonly string[] = DEMO_ROWS.filter(isDemoRowCompatible).map((r) => r.id);
/** Never compatible with the anchor in this fixture, in fixture order (demo-3, demo-4). */
const _SKIPPED_IDS: readonly string[] = DEMO_ROWS.filter((r) => !isDemoRowCompatible(r)).map(
	(r) => r.id
);
/** anchor, demo-1, demo-2 - greedy stops here (real simulateAutoPlayChain replay). */
const _GREEDY_CHAIN = _simulateChain(false, false);
/** anchor, demo-2, demo-1, demo-6, demo-5 - reach resolves the strand (real replay). */
const _REACH_CHAIN = _simulateChain(true, false);
/** All 7 rows in strict membership order (real replay; enforce ignores key/BPM). */
const _ENFORCE_CHAIN = _simulateChain(false, true);

const _GREEDY_ACCEPTED_IDS: readonly string[] = _GREEDY_CHAIN.slice(1);
const _GREEDY_STRANDED_IDS: readonly string[] = _COMPATIBLE_IDS.filter(
	(id) => !_GREEDY_ACCEPTED_IDS.includes(id)
);
const _REACH_ACCEPTED_IDS: readonly string[] = _REACH_CHAIN.slice(1);
const _ENFORCE_HANDOFF_IDS: readonly string[] = _ENFORCE_CHAIN.slice(1);

function _rowById(id: string): WalkthroughRow {
	const row = DEMO_ROWS.find((r) => r.id === id);
	if (row === undefined) throw new Error(`autoplay-walkthrough: unknown demo row id ${id}`);
	return row;
}

/** Which of the two universal rejects fails on key vs BPM - drives caption text. */
function _rejectCaption(id: string): string {
	const row = _rowById(id);
	return camelotKeysAreCompatible(row.key, DEMO_ANCHOR.key)
		? "BPM too far off - can't follow this."
		: "Wrong key family - can't follow this.";
}

// ----- phases / frames (SA5 4.2, 4.5) --------------------------------------

export type WalkthroughPhaseKind =
	| 'anchor'
	| 'reject'
	| 'strand'
	| 'simulate'
	| 'accept'
	| 'handoff'
	| 'summary';

export interface WalkthroughFrame {
	phase: WalkthroughPhaseKind;
	/** Row id(s) actively spotlighted this frame - the reject/simulate flash
	 * target, or the row(s) just accepted/handed off. Empty for anchor and
	 * most strand/summary frames. */
	focusIds: readonly string[];
	/** Rows confirmed compatible and in the chain as of this frame - cumulative, renders green. */
	acceptedIds: readonly string[];
	/** Rows confirmed permanently incompatible as of this frame - cumulative, renders a quiet grey. */
	skippedIds: readonly string[];
	/** Rows reachable earlier that are not reachable now - cumulative until `simulate` resolves them. */
	strandedIds: readonly string[];
	/** Ms this frame holds before the next one starts. */
	holdMs: number;
	/** Caption line rendered under the animation - mode-specific narration. */
	caption: string;
}

/** Precedence, first match wins - reused by AutoPlayWalkthrough.svelte per row per frame. */
export function rowTreatment(
	rowId: string,
	frame: WalkthroughFrame
): 'accepted' | 'skipped' | 'stranded' | 'flash' | 'neutral' {
	if (frame.acceptedIds.includes(rowId)) return 'accepted';
	if (frame.skippedIds.includes(rowId)) return 'skipped';
	if (frame.strandedIds.includes(rowId)) return 'stranded';
	if (frame.focusIds.includes(rowId) && (frame.phase === 'reject' || frame.phase === 'simulate')) {
		return 'flash';
	}
	return 'neutral';
}

const _EMPTY: readonly string[] = [];
const _ANCHOR_FRAME: WalkthroughFrame = {
	phase: 'anchor',
	focusIds: _EMPTY,
	acceptedIds: _EMPTY,
	skippedIds: _EMPTY,
	strandedIds: _EMPTY,
	holdMs: 800,
	caption: "Here's what's queued next."
};

/** Two reject beats shared verbatim by reach and greedy (SA5 4.5 frames 2-5). */
function _rejectBeats(): WalkthroughFrame[] {
	const [firstSkip, secondSkip] = _SKIPPED_IDS;
	return [
		{
			phase: 'reject',
			focusIds: [firstSkip],
			acceptedIds: _EMPTY,
			skippedIds: _EMPTY,
			strandedIds: _EMPTY,
			holdMs: 1000,
			caption: _rejectCaption(firstSkip)
		},
		{
			phase: 'reject',
			focusIds: _EMPTY,
			acceptedIds: _EMPTY,
			skippedIds: [firstSkip],
			strandedIds: _EMPTY,
			holdMs: 400,
			caption: 'Skipping it.'
		},
		{
			phase: 'reject',
			focusIds: [secondSkip],
			acceptedIds: _EMPTY,
			skippedIds: [firstSkip],
			strandedIds: _EMPTY,
			holdMs: 1000,
			caption: _rejectCaption(secondSkip)
		},
		{
			phase: 'reject',
			focusIds: _EMPTY,
			acceptedIds: _EMPTY,
			skippedIds: [firstSkip, secondSkip],
			strandedIds: _EMPTY,
			holdMs: 400,
			caption: 'Skipping it.'
		}
	];
}

function _acceptBeat(): WalkthroughFrame {
	const firstTwo = _COMPATIBLE_IDS.slice(0, 2);
	return {
		phase: 'accept',
		focusIds: firstTwo,
		acceptedIds: firstTwo,
		skippedIds: _SKIPPED_IDS,
		strandedIds: _EMPTY,
		holdMs: 700,
		caption: 'These two are compatible.'
	};
}

function _reachTimeline(): readonly WalkthroughFrame[] {
	const stranded = _GREEDY_STRANDED_IDS; // the two rows the "strand" beat depicts
	const flicker: WalkthroughFrame[] = [];
	for (let i = 0; i < 8; i += 1) {
		flicker.push({
			phase: 'simulate',
			focusIds: [stranded[i % stranded.length]],
			acceptedIds: _COMPATIBLE_IDS.slice(0, 2),
			skippedIds: _SKIPPED_IDS,
			strandedIds: stranded,
			holdMs: 200,
			caption: 'Trying a smarter order...'
		});
	}
	return [
		_ANCHOR_FRAME,
		..._rejectBeats(),
		_acceptBeat(),
		{
			phase: 'strand',
			focusIds: stranded,
			acceptedIds: _COMPATIBLE_IDS.slice(0, 2),
			skippedIds: _SKIPPED_IDS,
			strandedIds: stranded,
			holdMs: 1200,
			caption: 'These two look stuck.'
		},
		...flicker,
		{
			phase: 'simulate',
			focusIds: stranded,
			acceptedIds: _REACH_ACCEPTED_IDS,
			skippedIds: _SKIPPED_IDS,
			strandedIds: _EMPTY,
			holdMs: 400,
			caption: 'Found it - fewest options go first.'
		},
		{
			phase: 'summary',
			focusIds: _EMPTY,
			acceptedIds: _REACH_ACCEPTED_IDS,
			skippedIds: _SKIPPED_IDS,
			strandedIds: _EMPTY,
			holdMs: 1500,
			caption: 'Every reachable track gets played.'
		}
	];
}

function _greedyTimeline(): readonly WalkthroughFrame[] {
	const stranded = _GREEDY_STRANDED_IDS;
	return [
		_ANCHOR_FRAME,
		..._rejectBeats(),
		_acceptBeat(),
		{
			phase: 'strand',
			focusIds: stranded,
			acceptedIds: _GREEDY_ACCEPTED_IDS,
			skippedIds: _SKIPPED_IDS,
			strandedIds: stranded,
			holdMs: 1800,
			caption: 'These two never get a turn.'
		},
		{
			phase: 'summary',
			focusIds: _EMPTY,
			acceptedIds: _GREEDY_ACCEPTED_IDS,
			skippedIds: _SKIPPED_IDS,
			strandedIds: stranded, // held, not resolved - greedy genuinely does not fix this
			holdMs: 1200,
			caption: 'Greedy stops here - Reach mode plays every track.'
		}
	];
}

function _enforceTimeline(): readonly WalkthroughFrame[] {
	const handoffs: WalkthroughFrame[] = _ENFORCE_HANDOFF_IDS.map((id, i) => ({
		phase: 'handoff',
		focusIds: [id],
		acceptedIds: _ENFORCE_HANDOFF_IDS.slice(0, i + 1),
		skippedIds: _EMPTY,
		strandedIds: _EMPTY,
		holdMs: 300,
		caption: 'Enforce order: just plays them in order.'
	}));
	return [
		_ANCHOR_FRAME,
		...handoffs,
		{
			phase: 'summary',
			focusIds: _EMPTY,
			acceptedIds: _ENFORCE_HANDOFF_IDS,
			skippedIds: _EMPTY,
			strandedIds: _EMPTY,
			holdMs: 1500,
			caption: 'Every track plays, compatible or not.'
		}
	];
}

/** Builds the phase sequence for one AutoPlay mode. Branches structurally,
 * not just cosmetically: 'enforce' has no reject/strand/simulate/accept
 * phases at all, matching the real rule that enforce-order skips
 * compatibility checks entirely. */
export function buildWalkthroughTimeline(mode: WalkthroughMode): readonly WalkthroughFrame[] {
	if (mode === 'reach') return _reachTimeline();
	if (mode === 'greedy') return _greedyTimeline();
	return _enforceTimeline();
}

export interface WalkthroughPlayerState {
	frameIndex: number;
	frame: WalkthroughFrame;
	/** True once elapsedMs has reached or passed the timeline's total duration. */
	done: boolean;
}

export function totalMs(frames: readonly WalkthroughFrame[]): number {
	return frames.reduce((sum, f) => sum + f.holdMs, 0);
}

/** Pure lookup: given a full timeline and an elapsed offset, which frame is
 * active right now. elapsedMs <= 0 clamps to frame 0; elapsedMs >= totalMs
 * clamps to the final (summary) frame with done=true - the same call used
 * for the prefers-reduced-motion static-frame path. */
export function frameAt(
	frames: readonly WalkthroughFrame[],
	elapsedMs: number
): WalkthroughPlayerState {
	if (frames.length === 0) throw new Error('frameAt: frames must be non-empty');
	const clamped = Math.max(0, elapsedMs);
	const total = totalMs(frames);
	let acc = 0;
	for (let i = 0; i < frames.length; i += 1) {
		const frame = frames[i];
		const frameEnd = acc + frame.holdMs;
		if (clamped < frameEnd || i === frames.length - 1) {
			return { frameIndex: i, frame, done: clamped >= total };
		}
		acc = frameEnd;
	}
	const last = frames.length - 1;
	return { frameIndex: last, frame: frames[last], done: true };
}
