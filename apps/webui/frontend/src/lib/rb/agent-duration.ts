/** Resolve agent ramp duration against real PQTZ and AnlzPhrase boundaries. */

export type DurationUnit = 'beats' | 'bars' | 'phrases' | 'ms';
export type DurationAnchor = 'next_beat' | 'next_downbeat' | 'next_phrase';

export interface Duration {
	unit: DurationUnit;
	n: number;
	anchor?: DurationAnchor;
	clock?: number | 'master';
}

export interface DurationClock {
	position_ms: number;
	beatgrid: readonly { n: number; time_ms: number }[];
	phrases: readonly { start_ms: number }[];
}

export interface ResolvedDuration {
	start_ms: number;
	target_ms: number;
}

/** Progress is recomputed from the live clock position on every ramp tick. */
export function durationProgress(plan: ResolvedDuration, positionMs: number): number {
	if (!Number.isFinite(positionMs)) throw new RangeError('clock position_ms must be finite');
	if (positionMs <= plan.start_ms) return 0;
	if (positionMs >= plan.target_ms) return 1;
	return (positionMs - plan.start_ms) / (plan.target_ms - plan.start_ms);
}

function _duration(value: unknown): Duration {
	if (value === null || typeof value !== 'object' || Array.isArray(value)) {
		throw new TypeError('ramp over must be a Duration object');
	}
	const duration = value as Record<string, unknown>;
	if (duration.unit !== 'beats' && duration.unit !== 'bars' && duration.unit !== 'phrases' && duration.unit !== 'ms') {
		throw new TypeError('Duration unit must be beats, bars, phrases, or ms');
	}
	if (typeof duration.n !== 'number' || !Number.isFinite(duration.n) || duration.n <= 0) {
		throw new RangeError('Duration n must be finite and > 0');
	}
	if (!Number.isInteger(duration.n) && duration.unit !== 'ms') {
		throw new RangeError(`Duration ${duration.unit} n must be an integer`);
	}
	if (duration.anchor !== undefined && duration.anchor !== 'next_beat' && duration.anchor !== 'next_downbeat' && duration.anchor !== 'next_phrase') {
		throw new TypeError('Duration anchor must be next_beat, next_downbeat, or next_phrase');
	}
	if (duration.clock !== undefined && duration.clock !== 'master' && ![1, 2, 3, 4].includes(duration.clock as number)) {
		throw new RangeError('Duration clock must be master or deck 1..4');
	}
	return {
		unit: duration.unit,
		n: duration.n,
		anchor: duration.anchor,
		clock: duration.clock
	} as Duration;
}

function _firstAtOrAfter(rows: readonly { time_ms: number }[], positionMs: number): number {
	return rows.findIndex((row) => row.time_ms >= positionMs);
}

function _requireGrid(clock: DurationClock): void {
	if (clock.beatgrid.length === 0) throw new Error('no_grid');
	for (let index = 1; index < clock.beatgrid.length; index += 1) {
		if (clock.beatgrid[index].time_ms <= clock.beatgrid[index - 1].time_ms) throw new Error('no_grid');
	}
}

function _downbeats(clock: DurationClock): readonly { n: number; time_ms: number }[] {
	const downbeats = clock.beatgrid.filter((beat) => beat.n === 1);
	if (downbeats.length === 0) throw new Error('no_grid');
	return downbeats;
}

function _anchorPosition(duration: Duration, clock: DurationClock): number {
	if (duration.anchor === undefined) return clock.position_ms;
	if (duration.anchor === 'next_phrase') {
		const phrase = clock.phrases.find((row) => row.start_ms >= clock.position_ms);
		if (phrase === undefined) throw new Error('no_phrase');
		return phrase.start_ms;
	}
	_requireGrid(clock);
	const rows = duration.anchor === 'next_downbeat' ? _downbeats(clock) : clock.beatgrid;
	const index = _firstAtOrAfter(rows, clock.position_ms);
	if (index < 0) throw new Error('no_grid');
	return rows[index].time_ms;
}

/** Resolve the immutable music-time boundary a live ramp must reach. */
export function resolveDuration(value: unknown, clock: DurationClock): ResolvedDuration {
	const duration = _duration(value);
	if (!Number.isFinite(clock.position_ms) || clock.position_ms < 0) {
		throw new RangeError('clock position_ms must be finite and >= 0');
	}
	if (duration.unit === 'ms') {
		const startMs = _anchorPosition(duration, clock);
		return { start_ms: startMs, target_ms: startMs + duration.n };
	}
	if (duration.unit === 'phrases') {
		const startIndex = clock.phrases.findIndex((phrase) => phrase.start_ms >= _anchorPosition(duration, clock));
		if (startIndex < 0 || startIndex + duration.n >= clock.phrases.length) throw new Error('no_phrase');
		return { start_ms: clock.phrases[startIndex].start_ms, target_ms: clock.phrases[startIndex + duration.n].start_ms };
	}
	_requireGrid(clock);
	if (duration.unit === 'bars') {
		const downbeats = _downbeats(clock);
		const startIndex = _firstAtOrAfter(downbeats, _anchorPosition(duration, clock));
		if (startIndex < 0 || startIndex + duration.n >= downbeats.length) throw new Error('no_grid');
		return { start_ms: downbeats[startIndex].time_ms, target_ms: downbeats[startIndex + duration.n].time_ms };
	}
	const startRows = clock.beatgrid;
	const startIndex = _firstAtOrAfter(startRows, _anchorPosition(duration, clock));
	if (startIndex < 0) throw new Error('no_grid');
	const startMs = startRows[startIndex].time_ms;
	const gridStartIndex = clock.beatgrid.findIndex((beat) => beat.time_ms === startMs);
	if (gridStartIndex < 0 || gridStartIndex + duration.n >= clock.beatgrid.length) throw new Error('no_grid');
	return { start_ms: startMs, target_ms: clock.beatgrid[gridStartIndex + duration.n].time_ms };
}
