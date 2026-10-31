/**
 * CMDK-07 / CMDK-08: the bar arithmetic behind the Cmd K headphone preview.
 *
 * ← and → move the cue preview on a FIXED grid of bars: every 4 bars (Shift: 8)
 * counted from the track's first downbeat, plus the start of every drop, break
 * or vocal, so a jump never sails past one. The grid is a set of bars, not an
 * offset from wherever the preview has drifted to, so → then ← lands on the
 * bar it started from and replays the same passage. A landing starts at the
 * same fraction of its bar as the master deck is into its own bar, which is
 * what keeps the preview in phase with the master.
 *
 * Pure: no runes, no DOM, no audio. The bar reads beat grids and analysis the
 * shared caches already hold.
 */
// The bar's one route to these types: anlz-types is at the importer limit.
export type { AnlzBeat, AnlzData } from '$lib/rb/anlz-types';

/** Bars per jump, and per Shift jump. */
export const PREVIEW_STEP_BARS = 4;
export const PREVIEW_BIG_STEP_BARS = 8;

/** What a snap stop is the start of. */
export type SnapKind = 'drop' | 'break' | 'vocal';

export interface SnapStop {
	/** Bar index, 0 being the first downbeat. */
	bar: number;
	kind: SnapKind;
	/** Section label as analyzed ("Drop", "Breakdown"), or "Vocal". */
	label: string;
}

/** A section as `sectionSpans` returns it; only these fields are read. */
export interface SnapSection {
	start_s: number;
	label: string;
	family: string;
}

/** A vocal region as `/anlz` and row assets return it. */
export interface SnapVocal {
	start_s: number;
	end_s: number;
}

/** Downbeat times in seconds (beats numbered 1), ascending. */
// Structural, not AnlzBeat: a direct import would add one more importer of anlz-types, the
// quality gate's top fan-in module, which main already holds at its 57 ceiling.
export function downbeatTimes(beats: readonly { n: number; t: number }[]): number[] {
	const out: number[] = [];
	for (const beat of beats) if (beat.n === 1 && Number.isFinite(beat.t)) out.push(beat.t);
	out.sort((a, b) => a - b);
	return out;
}

/** Index of the last downbeat at or before `sec`, or -1 before the first one. */
function _barFloor(downbeats: readonly number[], sec: number): number {
	let lo = 0;
	let hi = downbeats.length - 1;
	let found = -1;
	while (lo <= hi) {
		const mid = (lo + hi) >> 1;
		if (downbeats[mid] <= sec) {
			found = mid;
			lo = mid + 1;
		} else hi = mid - 1;
	}
	return found;
}

/** The bar whose downbeat is nearest `sec` (sections and vocals start near one, rarely on it). */
export function nearestBar(downbeats: readonly number[], sec: number): number {
	if (downbeats.length === 0) throw new RangeError('nearestBar: the beat grid has no downbeats');
	const floor = _barFloor(downbeats, sec);
	if (floor < 0) return 0;
	if (floor + 1 >= downbeats.length) return floor;
	return sec - downbeats[floor] <= downbeats[floor + 1] - sec ? floor : floor + 1;
}

/** Median bar length in seconds, the unit vocal gaps are measured in. */
function _typicalBarSec(downbeats: readonly number[]): number {
	const gaps: number[] = [];
	for (let i = 1; i < downbeats.length; i++) gaps.push(downbeats[i] - downbeats[i - 1]);
	if (gaps.length === 0) return 0;
	gaps.sort((a, b) => a - b);
	return gaps[gaps.length >> 1];
}

/**
 * Bars where a drop, a break or a vocal starts. A vocal counts only where it
 * starts after at least a bar of no vocals, so the gaps between the words of
 * one verse do not each become a stop. A section and a vocal on the same bar
 * keep the section's name. Bar 0 is left out: the grid starts there anyway.
 */
export function snapStops(
	downbeats: readonly number[],
	sections: readonly SnapSection[],
	vocals: readonly SnapVocal[]
): SnapStop[] {
	if (downbeats.length === 0) return [];
	const byBar = new Map<number, SnapStop>();
	for (const section of sections) {
		const kind: SnapKind | null = section.family === 'drop' ? 'drop' : section.family === 'calm' ? 'break' : null;
		if (kind === null) continue;
		const bar = nearestBar(downbeats, section.start_s);
		if (bar > 0 && !byBar.has(bar)) byBar.set(bar, { bar, kind, label: section.label });
	}
	const barSec = _typicalBarSec(downbeats);
	const sorted = [...vocals].filter((v) => v.end_s > v.start_s).sort((a, b) => a.start_s - b.start_s);
	// The track start counts as the end of silence before it, so a first vocal needs a full bar too.
	let lastEnd = 0;
	for (const vocal of sorted) {
		if (vocal.start_s - lastEnd >= barSec) {
			const bar = nearestBar(downbeats, vocal.start_s);
			if (bar > 0 && !byBar.has(bar)) byBar.set(bar, { bar, kind: 'vocal', label: 'Vocal' });
		}
		lastEnd = Math.max(lastEnd, vocal.end_s);
	}
	return [...byBar.values()].sort((a, b) => a.bar - b.bar);
}

/**
 * Every bar a jump can land on: 0, then every `step` bars, plus each stop,
 * all below `barCount`. Sorted and unique.
 */
export function jumpGrid(barCount: number, stops: readonly number[], step: number): number[] {
	if (!(step > 0) || !Number.isInteger(step)) throw new RangeError(`jumpGrid: step must be a positive integer, got ${step}`);
	if (barCount <= 0) return [];
	const points = new Set<number>();
	for (let bar = 0; bar < barCount; bar += step) points.add(bar);
	for (const stop of stops) if (stop > 0 && stop < barCount) points.add(stop);
	return [...points].sort((a, b) => a - b);
}

/** The next grid bar after `from` (dir 1) or before it (dir -1); `from` itself at either end. */
export function jumpTarget(grid: readonly number[], from: number, dir: 1 | -1): number {
	if (dir > 0) {
		for (const bar of grid) if (bar > from) return bar;
		return from;
	}
	for (let i = grid.length - 1; i >= 0; i--) if (grid[i] < from) return grid[i];
	return from;
}

/**
 * How far into its bar `sec` is (0 up to 1), or null before the first downbeat or past the last bar.
 * The last bar has no next downbeat, so it borrows the bar before's length, as `inPhaseStartSec`
 * does: a master playing its final bar still gives its phase (Sol r4278366227).
 */
export function barPhase(downbeats: readonly number[], sec: number): number | null {
	const bar = _barFloor(downbeats, sec);
	if (bar < 0) return null;
	const len =
		bar + 1 < downbeats.length
			? downbeats[bar + 1] - downbeats[bar]
			: bar > 0
				? downbeats[bar] - downbeats[bar - 1]
				: 0;
	if (!(len > 0)) return null;
	const phase = (sec - downbeats[bar]) / len;
	return phase < 1 ? phase : null;
}

/** Seconds into the track where a landing on `bar` starts, `phase` of the way into that bar. */
export function inPhaseStartSec(downbeats: readonly number[], bar: number, phase: number): number {
	if (bar < 0 || bar >= downbeats.length) throw new RangeError(`inPhaseStartSec: bar ${bar} is outside the grid`);
	const next = bar + 1 < downbeats.length ? downbeats[bar + 1] : null;
	const len = next !== null ? next - downbeats[bar] : bar > 0 ? downbeats[bar] - downbeats[bar - 1] : 0;
	return downbeats[bar] + Math.max(0, Math.min(1, phase)) * len;
}

/**
 * Runs one task at a time. A task asked for while one runs waits, and a newer
 * ask replaces an older waiting one, so a held → never queues a backlog of
 * seeks and the last press is the one that sounds.
 */
export function latestOnly<T>(run: (task: T) => Promise<void>): (task: T) => Promise<void> {
	let running: Promise<void> | null = null;
	let waiting: { task: T; done: Array<{ resolve: () => void; reject: (error: unknown) => void }> } | null = null;
	const drain = async (task: T): Promise<void> => {
		try {
			await run(task);
		} finally {
			const next = waiting;
			waiting = null;
			if (next === null) running = null;
			else {
				running = drain(next.task);
				// The waiters share the run that replaced their asks: its failure is
				// theirs too, never a success (Sol r4279823893).
				running.then(
					() => next.done.forEach((waiter) => waiter.resolve()),
					(error: unknown) => next.done.forEach((waiter) => waiter.reject(error))
				);
			}
		}
	};
	return (task: T): Promise<void> => {
		if (running === null) {
			running = drain(task);
			return running;
		}
		const done = waiting?.done ?? [];
		return new Promise<void>((resolve, reject) => {
			done.push({ resolve, reject });
			waiting = { task, done };
		});
	};
}

/**
 * The strip's hot cues: every cue-bank entry in slots A to F, sorted by slot. A slot is
 * selected by its letter, not its kind, because a hot loop in the bank is stored as
 * `kind: 'loop'` with its slot set; the strip draws those as loops (Sol r4280385244).
 */
export function stripHotCues<C extends { slot: string | null }>(cues: readonly C[]): C[] {
	return cues
		.filter((cue) => cue.slot !== null && 'ABCDEF'.includes(cue.slot))
		.sort((a, b) => (a.slot as string).localeCompare(b.slot as string));
}
