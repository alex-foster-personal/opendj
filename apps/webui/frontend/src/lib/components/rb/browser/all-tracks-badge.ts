/**
 * The All Tracks badge (issue #3934, LIBM-184).
 *
 * Playlist badges count members with audio on this machine (`available_count`,
 * LIBM-169), so a streaming row never counts. All Tracks used to show
 * `total - broken`, which counts every streaming, off-machine and pathless row
 * too, so the top row of the tree disagreed with every row under it. It now
 * shows the reconcile summary's `availability.present`, the same one playable
 * predicate (`apps/webui/server/library_playable.py`), and its title names what
 * is left out. When the engine has nothing to measure (no state.db to scan),
 * it keeps the non-broken count and the title says so; a malformed answer is an
 * error state, never a count.
 */

const BUCKETS = [
	'total',
	'present',
	'broken_here',
	'off_machine',
	'awaiting_volume',
	'streaming',
	'pathless'
] as const;

type Availability = Record<(typeof BUCKETS)[number], number>;

/** What the reconcile summary said about audio on this machine. */
type AvailabilityRead =
	| { kind: 'measured'; counts: Availability }
	| { kind: 'unmeasured' }
	| { kind: 'malformed'; reason: string };

function readAvailability(value: unknown): AvailabilityRead {
	// null is the first read still in flight; 'unknown' is the engine saying it
	// had nothing to scan (reconcile-summary-refresh.ts). Neither is a fault.
	// undefined is neither: the engine always sends the field, so a missing one
	// (or a prop left unwired at runtime) is contract drift and falls through
	// to malformed below, never to the plausible non-broken fallback.
	if (value === null || value === 'unknown') return { kind: 'unmeasured' };
	if (typeof value !== 'object' || Array.isArray(value)) {
		const got = Array.isArray(value) ? 'an array' : typeof value;
		return { kind: 'malformed', reason: `expected bucket counts, got ${got}` };
	}
	const rec = value as Record<string, unknown>;
	for (const key of BUCKETS) {
		const n = rec[key];
		if (typeof n !== 'number' || !Number.isInteger(n) || n < 0) {
			return { kind: 'malformed', reason: `bucket ${key} is ${JSON.stringify(n) ?? 'missing'}` };
		}
	}
	// The six outcome buckets partition `total`, the same invariant the engine
	// enforces (`_require_buckets_cover_every_row`) and `libraryHealthDot`
	// checks; a payload that breaks it is malformed, not a measurement.
	const a = rec as Availability;
	const outcomes =
		a.present + a.broken_here + a.off_machine + a.awaiting_volume + a.streaming + a.pathless;
	if (outcomes !== a.total) {
		return { kind: 'malformed', reason: `buckets sum to ${outcomes}, not the total ${a.total}` };
	}
	return { kind: 'measured', counts: a };
}

/**
 * `error` is set when the engine's answer is malformed: the badge then shows
 * the error state, never a plausible count, and the title names the defect
 * (fail fast; a broken contract must not read as a measurement).
 */
export function allTracksBadge(
	nonBroken: number | null,
	broken: number | null,
	availability: unknown
): { count: number | null; title: string; error: string | null } {
	const read = readAvailability(availability);
	if (read.kind === 'malformed') {
		const error = `availability answer is malformed (${read.reason})`;
		return { count: null, title: `track count unavailable: ${error}`, error };
	}
	if (read.kind === 'measured') {
		const a = read.counts;
		return {
			count: a.present,
			title:
				`${a.present} tracks with audio on this machine. Not counted: ${a.streaming} streaming, ` +
				`${a.off_machine} on other machines, ${a.awaiting_volume} on unmounted drives, ` +
				`${a.broken_here} with a broken link here, ${a.pathless} with no file path`,
			error: null
		};
	}
	if (nonBroken === null || broken === null) {
		return { count: null, title: 'loading non-broken and broken track counts', error: null };
	}
	return {
		count: nonBroken,
		title: `${nonBroken} non-broken tracks, ${broken} broken (audio on this machine not measured)`,
		error: null
	};
}
