/**
 * The engine's own service time, read off `Server-Timing: app;dur=<ms>`
 * (PERF-DIAG-9990). A deck-load fetch member's client duration mixes queueing
 * behind the single-worker engine with service; this header is the service
 * half, so `scripts/perf/amdahl_report.py` can split a contested fetch group.
 *
 * Pure and import-free: a missing header or malformed value reads as null,
 * never 0, because a fake 0 ms service time would fabricate a ceiling.
 */

/** The deck-load fetch members whose service time the row carries. */
export const DECK_LOAD_SERVICE_MEMBERS = [
	'getTrack',
	'fetchAnlz',
	'fetchHotCues',
	'fetchAudio'
] as const;
export type DeckLoadServiceMember = (typeof DECK_LOAD_SERVICE_MEMBERS)[number];

/** Anything with a `get(name)` header lookup, e.g. a fetch `Headers`. */
export interface HeaderSource {
	get(name: string): string | null;
}

/** The `dur` of metric `name` in a Server-Timing header, or null if absent or malformed. */
export function parseServerTiming(header: string | null | undefined, name = 'app'): number | null {
	if (typeof header !== 'string') return null;
	for (const metric of header.split(',')) {
		const [token, ...params] = metric.split(';').map((part) => part.trim());
		if (token !== name) continue;
		for (const param of params) {
			const match = /^dur=(.*)$/i.exec(param);
			if (match === null) continue;
			const raw = match[1].trim();
			if (!/^\d+(\.\d+)?$/.test(raw)) return null;
			const ms = Number(raw);
			return Number.isFinite(ms) ? ms : null;
		}
		return null;
	}
	return null;
}

/**
 * The `service_ms` map for one deck-load row: one entry per member whose
 * response carried a parseable `app;dur=`, every other member left absent.
 */
export function deckLoadServiceMs(
	responses: Partial<Record<DeckLoadServiceMember, { headers: HeaderSource } | null | undefined>>
): Partial<Record<DeckLoadServiceMember, number>> {
	const serviceMs: Partial<Record<DeckLoadServiceMember, number>> = {};
	for (const member of DECK_LOAD_SERVICE_MEMBERS) {
		const ms = parseServerTiming(responses[member]?.headers.get('Server-Timing'));
		if (ms !== null) serviceMs[member] = ms;
	}
	return serviceMs;
}
