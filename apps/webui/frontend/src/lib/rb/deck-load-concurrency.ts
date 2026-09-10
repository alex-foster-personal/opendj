/**
 * Was this deck load measured ALONE, or was it racing another one?
 *
 * WHY THIS EXISTS. Every load latency this program has banked is a number
 * without its conditions attached, and the register (docs/perf/performance-
 * register.md) records what that costs: the same waveform decode measured
 * 0.73 s and 7.53 s in one evening, and the 10x was the machine's load
 * average, not the change under test. The Amdahl harness now refuses to print
 * a speedup ceiling on any ring on this machine because every deck load in
 * every ring "has a contested fetch group" -- and it cannot say WHICH loads
 * were contested and which were clean, because the rows never carried it.
 *
 * So a load row states, at write time, whether anything else was loading
 * while it ran. `solo=1` is the row an optimization proposal may be checked
 * against a ceiling; `solo=0` is the row that must be thrown away.
 *
 * THIS MODULE IS PURE, and deliberately so. It holds no timers, no module
 * state, no imports, and never reads a clock: every function takes the spans
 * and the instant as arguments. That is what lets the `solo=0` case be
 * exercised as directly as the `solo=1` case in a unit test -- a suite in
 * which every case comes back solo could not tell a working instrument from
 * one wired to a constant. The registry that accumulates spans over a real
 * page's lifetime lives in deck-load-context.ts, which is where the clock is.
 *
 * INTERVALS ARE CLOSED. Two loads that merely touch at an instant (one ends
 * exactly as the next begins) count as overlapping. That is the conservative
 * direction: it can only ever downgrade a row from solo to contested, never
 * promote a contested row to solo, and a measurement wrongly discarded is a
 * cheaper mistake than a measurement wrongly trusted.
 */

/**
 * One deck load's occupancy of the wall clock.
 *
 * `endMs === null` means STILL RUNNING. That state is load-bearing rather
 * than a placeholder: the load that finishes FIRST is exactly the one whose
 * row would otherwise claim solo, because the load it is racing has not
 * written its own row yet and so would be invisible to any scheme that only
 * looked at completed loads. An in-flight span is treated as occupying the
 * clock right up to the instant of the query.
 */
export interface DeckLoadSpan {
	/** Monotonically increasing per page load; identifies the subject. */
	readonly id: number;
	/** Deck the load targeted, for reading a span list by eye. */
	readonly deck: 1 | 2 | 3 | 4 | null;
	/** `performance.now()`-domain start. */
	readonly startMs: number;
	/** `performance.now()`-domain end, or null while the load is in flight. */
	readonly endMs: number | null;
}

/** Where a span's occupancy of the clock ends, as of `nowMs`. */
function _effectiveEndMs(span: DeckLoadSpan, nowMs: number): number {
	if (span.endMs !== null) return span.endMs;
	// An in-flight span cannot end before it started even if a caller passes a
	// stale `nowMs`; clamping keeps a degenerate input from producing an
	// inverted interval that silently intersects nothing.
	return Math.max(span.startMs, nowMs);
}

/**
 * Do two spans share any instant of the wall clock, as of `nowMs`?
 *
 * Closed-interval intersection: `[aStart, aEnd]` meets `[bStart, bEnd]` when
 * neither ends strictly before the other begins.
 */
export function spansOverlap(a: DeckLoadSpan, b: DeckLoadSpan, nowMs: number): boolean {
	const aEnd = _effectiveEndMs(a, nowMs);
	const bEnd = _effectiveEndMs(b, nowMs);
	return a.startMs <= bEnd && b.startMs <= aEnd;
}

/**
 * The highest number of deck loads in flight at any single instant DURING the
 * subject's span, the subject itself included.
 *
 * Never less than 1: the subject occupies its own span by definition, so a
 * genuinely alone load reports 1 rather than 0. A zero here would be
 * indistinguishable from "nothing was measured", which is the reading this
 * whole change exists to stop rows from making.
 *
 * The sweep clips every other span to the subject's own window first, because
 * the question is what the SUBJECT experienced, not what the machine was
 * doing generally: a load that began long before the subject and ran long
 * after it contributes exactly one unit of contention to the subject's
 * window, not a wider count earned outside it.
 */
export function peakConcurrentLoads(
	subject: DeckLoadSpan,
	others: readonly DeckLoadSpan[],
	nowMs: number
): number {
	const subjectEnd = _effectiveEndMs(subject, nowMs);
	// +1 opens an interval, -1 closes it. Sorted by instant, with OPENS taken
	// before CLOSES at the same instant, so two spans that merely touch are
	// counted as coincident -- the same closed-interval convention as
	// spansOverlap, applied to the count rather than the boolean.
	const edges: Array<readonly [number, number]> = [[subject.startMs, 1], [subjectEnd, -1]];
	for (const other of others) {
		if (other.id === subject.id) continue;
		if (!spansOverlap(subject, other, nowMs)) continue;
		const start = Math.max(other.startMs, subject.startMs);
		const end = Math.min(_effectiveEndMs(other, nowMs), subjectEnd);
		edges.push([start, 1], [end, -1]);
	}
	edges.sort((left, right) => (left[0] === right[0] ? right[1] - left[1] : left[0] - right[0]));
	let live = 0;
	let peak = 0;
	for (const [, delta] of edges) {
		live += delta;
		if (live > peak) peak = live;
	}
	return peak;
}

/** The contention labels a deck-load row carries. Strings, because
 * `PerfEvent.labels` is a string map: `stages` is a ms-per-stage map and
 * anything summing or charting it must never meet a value that is not a
 * duration. */
export interface ConcurrencyLabels {
	/** '1' when nothing else was loading during ANY part of this load. */
	readonly solo: '0' | '1';
	/** Peak simultaneous in-flight loads during this one, subject included. */
	readonly concurrent_loads: string;
}

/**
 * Read the contention facts for one span out of the spans around it.
 *
 * `solo` is derived from the overlap test rather than from `peak === 1` even
 * though the two agree today. They answer different questions -- "did anything
 * touch me" versus "how many at once" -- and deriving one from the other would
 * make a future change to either silently change both.
 */
export function concurrencyLabels(
	subject: DeckLoadSpan,
	others: readonly DeckLoadSpan[],
	nowMs: number
): ConcurrencyLabels {
	const contended = others.some(
		(other) => other.id !== subject.id && spansOverlap(subject, other, nowMs)
	);
	return {
		solo: contended ? '0' : '1',
		concurrent_loads: String(peakConcurrentLoads(subject, others, nowMs))
	};
}
