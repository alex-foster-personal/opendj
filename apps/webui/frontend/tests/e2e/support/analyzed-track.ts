/**
 * Finding a track with a real beatgrid, whatever kind of library is mounted.
 *
 * A beatgrid reaches the app by two different real routes and the specs must
 * not care which one a given library uses:
 *
 * - a rekordbox-mapped track carries its authoritative grid on `/anlz`;
 * - a locally ingested track (including every generated fixture track) has an
 *   EMPTY `/anlz` beatgrid by design - `local_anlz_payload` leaves everything
 *   rekordbox owns empty - and its `apps.analysis` grid on
 *   `/beatgrid-fallback`, in the identical shape (`routes/analysis.py`).
 *
 * Reading only one of them silently excludes a whole kind of library:
 * `/beatgrid-fallback` alone 404s on every row of a rekordbox library, and
 * `/anlz` alone returns an empty grid for every row of a generated fixture.
 * So this asks for the authoritative one first and falls back, which is the
 * order the route's own 404 message tells clients to use.
 */
import { expect, type APIRequestContext } from '@playwright/test';

export interface BeatWire {
	n: number;
	bpm: number;
	t: number;
	extrapolated?: boolean;
}

export interface AnlzWire {
	beatgrid: { beats: BeatWire[] };
}

export async function beatgridFor(
	request: APIRequestContext,
	apiBase: string,
	stableId: string,
	discoveryErrors: string[]
): Promise<AnlzWire | null> {
	const id = encodeURIComponent(stableId);
	const anlz = await request.get(`${apiBase}/api/v1/tracks/${id}/anlz?points=100`);
	if (anlz.ok()) {
		const payload = (await anlz.json()) as AnlzWire;
		if (payload.beatgrid.beats.length > 0) return payload;
	} else if (anlz.status() !== 404) {
		discoveryErrors.push(`${stableId}: ANLZ HTTP ${anlz.status()}`);
	}
	const derived = await request.get(`${apiBase}/api/v1/tracks/${id}/beatgrid-fallback`);
	if (derived.status() === 404) return null;
	if (!derived.ok()) {
		discoveryErrors.push(`${stableId}: analyzed beatgrid HTTP ${derived.status()}`);
		return null;
	}
	return (await derived.json()) as AnlzWire;
}

/**
 * The first available, BPM-tagged track whose beatgrid carries at least
 * `minBeats` beats, or null. Scans the FULL candidate set rather than a
 * fixed-size prefix: a library can carry more BPM-tagged candidates than any
 * fixed cutoff, with the first usable beatgrid appearing later in the
 * listing, and a cutoff there returns null - "no analyzed track" - while an
 * analyzed track really exists (Codex P2 BLOCKING, #1628). The scan still
 * stops as soon as a match is found, the same shape as
 * performance-controls.spec.ts's `_fetchAnalyzedTracks`, which caps on
 * matches wanted rather than candidates scanned.
 *
 * "Full" means every PAGE too, not just every row of the first one:
 * `/api/v1/tracks` caps `limit` at 1000 and is cursor-paginated
 * (`routes/tracks.py`), and `available` filtering runs AFTER pagination on
 * each page rather than on the whole set, so a library over 1000 rows can
 * have zero qualifying candidates on page 1 while a later page has one. This
 * follows `next_cursor` until either a match is found or it comes back null
 * (Codex P2 BLOCKING, #1628 fix-round 2) - the same false "no analyzed
 * track" shape as the fixed-prefix cutoff, one level up.
 */
export async function firstAnalyzedStableId(
	request: APIRequestContext,
	apiBase: string,
	{ minBeats = 32 }: { minBeats?: number } = {}
): Promise<string | null> {
	const discoveryErrors: string[] = [];
	let scanned = 0;
	// The match is recorded rather than returned from inside the loop, so the
	// error check below is on the path for BOTH outcomes. Returning early would
	// jump over it: candidate 1 answering HTTP 500 while candidate 2 has a good
	// grid is exactly the regression this is here to catch, and it is the case
	// an early return hides (blinded review, PR #1628).
	let match: string | null = null;
	let cursor: string | null = null;
	do {
		const cursorParam = cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`;
		const listing = await request.get(
			`${apiBase}/api/v1/tracks?limit=1000&available=true${cursorParam}`
		);
		expect(listing.ok(), 'real available-track listing must succeed').toBeTruthy();
		const payload = (await listing.json()) as {
			items: { stable_id: string; bpm: number | null; file_exists: boolean }[];
			next_cursor: string | null;
		};
		const candidates = payload.items.filter(
			(track) => track.file_exists && typeof track.bpm === 'number' && track.bpm > 0
		);
		for (const track of candidates) {
			scanned += 1;
			const grid = await beatgridFor(request, apiBase, track.stable_id, discoveryErrors);
			if (grid !== null && grid.beatgrid.beats.length >= minBeats) {
				match = track.stable_id;
				break;
			}
		}
		cursor = payload.next_cursor;
	} while (match === null && cursor !== null);
	// Callers treat null as "this library has no analyzed track" and SKIP. That
	// reading is only honest when discovery itself completed - if any request
	// errored, a backend or ANLZ-parser regression would otherwise be laundered
	// into a green suite (Codex P1, #1628). Non-404 errors only: a 404 is the
	// route saying this track has no grid, which is data, not a failure.
	if (discoveryErrors.length > 0) {
		throw new Error(
			`beatgrid discovery failed while scanning ${scanned} track(s), so neither a match ` +
				`nor "no analyzed track" can be trusted: ${discoveryErrors.join('; ')}`
		);
	}
	return match;
}
