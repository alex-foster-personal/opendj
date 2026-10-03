/**
 * Health-light policy. Pure, and the ONLY place the browser panel's dot
 * states and wording are decided.
 *
 * `BrowserPanel.svelte` measures (playlists loaded, the reconcile summary,
 * the ingest coverage response, any request error) and decides nothing;
 * every threshold and wording choice lives here, where it can be unit tested
 * without a component, an AudioContext, or a network mock. The tests import
 * and call these exact functions.
 *
 * HEALTH-01: Library health counts only audio expected on this machine.
 * Supersedes: the legacy total_tracks - total_broken dot verdict.
 * The shared reconcile API supplies the production availability predicate.
 * Counts from a failed or malformed measurement are never a verdict.
 * Coverage policy stays in this module, separate from that predicate.
 *
 * The rules (HEALTH-01, HEALTH-03, HEALTH-04):
 *
 * - Library health is green when every track whose audio is EXPECTED ON THIS
 *   MACHINE resolves. Rows that live on another machine, wait for an
 *   unmounted volume, stream, or have no path are reported in the detail and
 *   never make the dot amber. Amber is only for a link this machine recorded
 *   as working that no longer resolves. The availability buckets must sum to
 *   the live row total or the dot stays grey.
 * - A coverage dot is green when nothing is pending or failed over `present`
 *   tracks. "Nothing to make" (no lyrics available, no stems source) is a
 *   finished state, counted on its own so it is never mistaken for done.
 * - A stem bundle evicted to the cloud is DONE (HEALTH-07): the R2 index
 *   holds it and a deck fetches it back in seconds. The hover splits done
 *   into local and in cloud. When the index cannot be read, tracks with no
 *   local bundle cannot be told apart from evicted ones, so the Stems dot
 *   is grey rather than amber or green.
 * - An endpoint that cannot answer is grey "unknown". A failed or malformed
 *   measurement is not a verdict in either direction.
 * - The coverage dots show the engine's LAST measurement at once and say how
 *   old it is (HEALTH-12). An old measurement keeps its verdict; a refresh
 *   that failed does not, because the counts are then known to be unchecked.
 *
 * Every count names its denominator: `present` is the set of tracks whose
 * audio resolves on this machine right now.
 */

export type LibraryHealthDot = {
	label:
		| 'Frontend'
		| 'Backend'
		| 'Library health'
		| 'Vocals completion'
		| 'Stems completion'
		| 'Lyrics completion';
	state: 'loading' | 'complete' | 'incomplete' | 'unavailable' | 'error';
	detail: string;
};

/** Where every live row's audio stands on this machine (reconcile summary). */
export type LibraryAvailability = {
	total: number;
	present: number;
	broken_here: number;
	off_machine: number;
	awaiting_volume: number;
	streaming: number;
	pathless: number;
};

const AVAILABILITY_KEYS = [
	'total',
	'present',
	'broken_here',
	'off_machine',
	'awaiting_volume',
	'streaming',
	'pathless'
] as const;

export type CoverageStep = 'vocals' | 'stems' | 'lyrics';

/** The fields of GET /ingest/coverage the dots read. */
export type CoverageCounts = {
	on_disk: number;
	done: Record<string, number>;
	terminal: Record<string, number>;
	failed: Record<string, number>;
	pending: Record<string, number>;
	corrupt: Record<string, number>;
	waiting_on_stems: number;
	stems_source_refusal: string | null;
	/** Stem bundles on this disk. `local.stems + in_cloud.stems === done.stems`. */
	local: Record<string, number>;
	/** Stem bundles evicted to R2 that this machine can fetch back. */
	in_cloud: Record<string, number>;
	/** Vocals pending whose stem bundle is in the cloud: the drain fetches one at a time. */
	awaiting_stem_download: number;
	/** `ok`: index read. `off`: no cloud on this machine. `unknown`: it could not be read. */
	stems_index: { state: 'ok' | 'off' | 'unknown'; reason: string | null };
	/** Seconds since these counts were measured (0 for a fresh measurement). */
	age_s: number;
	/** A newer measurement is being taken; ask again shortly. */
	refreshing: boolean;
	/** Why the latest background refresh failed, or null. */
	refresh_error: string | null;
};

const STEMS_INDEX_STATES = ['ok', 'off', 'unknown'];

const TERMINAL_WORDING: Record<CoverageStep, string> = {
	vocals: 'no stems source',
	stems: 'no stems source',
	lyrics: 'no lyrics available'
};

function isCount(value: unknown): value is number {
	return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function plural(count: number, one: string, many: string): string {
	return `${count} ${count === 1 ? one : many}`;
}

/** Grey: the measurement could not be taken. Never a verdict. */
export function unknownDot(label: LibraryHealthDot['label'], why: string): LibraryHealthDot {
	return { label, state: 'unavailable', detail: `unknown - ${why}` };
}

export function libraryHealthDot(
	libraryHealthError: string | null,
	allTracksCount: number | null,
	playlistCount: number,
	availability: unknown,
	availabilityError: string | null,
	/** The first pane's paged listing while it is in flight (HEALTH-14). */
	loadProgress: { loaded: number; total: number | null } | null = null
): LibraryHealthDot {
	const label = 'Library health' as const;
	if (libraryHealthError !== null) {
		return unknownDot(label, libraryHealthError);
	}
	// HEALTH-14: the availability check runs after the listing, so while rows
	// are still arriving the light says how far the listing has got instead
	// of an unexplained "checking". It stays a loading state, never a verdict.
	const listing =
		loadProgress === null
			? null
			: loadProgress.total === null
				? `loading ${loadProgress.loaded} tracks, total not yet known`
				: `loading ${loadProgress.loaded} of ${loadProgress.total} tracks`;
	if (allTracksCount === null) {
		return { label, state: 'loading', detail: listing ?? 'checking library health' };
	}
	// Checked BEFORE any settled counts: a refresh that fails AFTER an
	// earlier one succeeded keeps the prior counts in the caller, and the
	// dot must not go on quoting them once the measurement has failed.
	if (availabilityError !== null) {
		return unknownDot(label, availabilityError);
	}
	if (availability === null) {
		return {
			label,
			state: 'loading',
			detail:
				listing === null
					? 'checking which tracks are on this machine'
					: `${listing}; which are on this machine is checked next`
		};
	}
	const counts = availability as Record<string, unknown>;
	if (typeof availability !== 'object' || !AVAILABILITY_KEYS.every((key) => isCount(counts[key]))) {
		return unknownDot(label, 'the engine returned no availability breakdown for this library');
	}
	const here = availability as LibraryAvailability;
	if (AVAILABILITY_KEYS.filter((key) => key !== 'total').reduce((sum, key) => sum + here[key], 0) !== here.total) {
		return unknownDot(label, 'the availability buckets do not sum to the live row total');
	}
	const playlistPart =
		playlistCount > 0 ? `${plural(playlistCount, 'playlist', 'playlists')} found` : 'no playlists found';
	const elsewhere =
		`Not counted: ${here.off_machine} on other machines, ${here.awaiting_volume} awaiting a volume, ` +
		`${here.streaming} streaming, ${here.pathless} without a path.`;
	const expected = here.present + here.broken_here;
	if (expected === 0) {
		return {
			label,
			state: 'unavailable',
			detail: `${playlistPart}; no tracks are expected on this machine. ${elsewhere}`
		};
	}
	const brokenPart =
		here.broken_here > 0
			? `${plural(here.broken_here, 'broken link', 'broken links')} here`
			: 'no broken links here';
	return {
		label,
		state: here.broken_here > 0 ? 'incomplete' : 'complete',
		detail:
			`${playlistPart}; ${here.present} of ${expected} tracks expected on this machine resolve ` +
			`(denominator: present + broken here), ${brokenPart}. ${elsewhere}`
	};
}

/** "just now", "12 s ago", "4 min ago", "2 h ago". */
export function coverageAgeText(ageS: number): string {
	if (ageS < 1) return 'just now';
	if (ageS < 60) return `${Math.floor(ageS)} s ago`;
	if (ageS < 3600) return `${Math.floor(ageS / 60)} min ago`;
	return `${Math.floor(ageS / 3600)} h ago`;
}

/**
 * How soon to ask for coverage again, or null to wait for the regular
 * refetch. A refresh running behind a cached read, a refresh that failed and
 * a request that failed are each worth asking again soon; `rechecks` counts
 * the early re-asks already made, and the delays back off and then stop so a
 * measurement that never settles is not polled in a tight loop.
 */
export const COVERAGE_RECHECK_DELAYS_MS: readonly number[] = [3000, 3000, 6000, 12000, 24000];

export function coverageRecheckDelayMs(
	outcome: { ok: true; refreshing: boolean; refresh_error: string | null } | { ok: false },
	rechecks: number
): number | null {
	const settled = outcome.ok && !outcome.refreshing && outcome.refresh_error === null;
	if (settled) return null;
	return COVERAGE_RECHECK_DELAYS_MS[rechecks] ?? null;
}

export function coverageDot(
	label: LibraryHealthDot['label'],
	coverage: CoverageCounts,
	step: CoverageStep
): LibraryHealthDot {
	const age: unknown = coverage.age_s;
	const refreshError: unknown = coverage.refresh_error;
	if (
		typeof age !== 'number' ||
		!Number.isFinite(age) ||
		age < 0 ||
		typeof coverage.refreshing !== 'boolean' ||
		(refreshError !== null && typeof refreshError !== 'string')
	) {
		return unknownDot(label, 'the coverage response does not say how old its counts are');
	}
	if (refreshError !== null) {
		// The counts on hand are the last good measurement, and the attempt to
		// check them again failed: they are not shown as a verdict.
		return unknownDot(
			label,
			`the last coverage refresh failed (${refreshError}); the counts on hand were measured ${coverageAgeText(age)}`
		);
	}
	const dot = _coverageVerdict(label, coverage, step);
	const refreshing = coverage.refreshing ? '; a fresh count is being taken' : '';
	return { ...dot, detail: `${dot.detail} Measured ${coverageAgeText(age)}${refreshing}.` };
}

function _coverageVerdict(
	label: LibraryHealthDot['label'],
	coverage: CoverageCounts,
	step: CoverageStep
): LibraryHealthDot {
	const done: unknown = coverage.done?.[step];
	const terminal: unknown = coverage.terminal?.[step];
	const failed: unknown = coverage.failed?.[step];
	const pending: unknown = coverage.pending?.[step];
	const corrupt: unknown = coverage.corrupt?.[step];
	const present: unknown = coverage.on_disk;
	if (
		!isCount(done) ||
		!isCount(terminal) ||
		!isCount(failed) ||
		!isCount(pending) ||
		!isCount(corrupt) ||
		!isCount(present)
	) {
		return unknownDot(label, `the ${step} coverage counts are missing or not whole numbers`);
	}
	if (done + terminal + failed + pending !== present) {
		return unknownDot(label, `the ${step} coverage states do not sum to the present tracks`);
	}
	if (present === 0) {
		return { label, state: 'unavailable', detail: 'no present tracks to measure on this machine' };
	}
	let doneWording = `${done} done`;
	let indexNote = '';
	let waiting = '';
	if (step !== 'lyrics') {
		const index = coverage.stems_index;
		if (!index || !STEMS_INDEX_STATES.includes(index.state)) {
			return unknownDot(label, 'the coverage response does not say whether the cloud stem index was read');
		}
		const indexUnknown = index.state === 'unknown';
		const why = index.reason ?? 'no reason given';
		if (step === 'stems') {
			const local: unknown = coverage.local?.stems;
			const inCloud: unknown = coverage.in_cloud?.stems;
			if (!isCount(local) || !isCount(inCloud) || local + inCloud !== done) {
				return unknownDot(label, 'the local and in-cloud stem counts are missing or do not sum to done');
			}
			if (indexUnknown && pending > 0) {
				// Not amber: some of these are probably safe in the cloud. Not green:
				// that is unproven. The measurement could not be taken.
				return unknownDot(
					label,
					`${plural(pending, 'track has', 'tracks have')} no local stems and the cloud stem index could not be read (${why}), ` +
						`so stems in the cloud cannot be told apart from stems still to make. ${local} local of ${present} present tracks.`
				);
			}
			doneWording = `${local} local, ${inCloud} in cloud (fetched back when loaded on a deck)`;
		} else {
			const fetching: unknown = coverage.awaiting_stem_download;
			if (!isCount(fetching) || !isCount(coverage.waiting_on_stems)) {
				return unknownDot(label, 'the vocals stem-dependency counts are missing or not whole numbers');
			}
			const parts = [
				fetching > 0 ? `${fetching} with stems in cloud, fetched one at a time` : '',
				coverage.waiting_on_stems > 0 ? `${coverage.waiting_on_stems} waiting on stems` : ''
			].filter((part) => part !== '');
			waiting = parts.length > 0 ? ` (${parts.join('; ')})` : '';
			if (indexUnknown && coverage.waiting_on_stems > 0) {
				indexNote = ` The cloud stem index could not be read (${why}), so some of those waiting may only need a download.`;
			}
		}
	}
	const refusal =
		step !== 'lyrics' && terminal > 0 && coverage.stems_source_refusal
			? ` Stems cannot be made here: ${coverage.stems_source_refusal}.`
			: '';
	const counts =
		`${doneWording}, ${terminal} ${TERMINAL_WORDING[step]}, ${pending} pending${waiting}` +
		`${failed > 0 ? `, ${failed} failed` : ''} of ${present} present tracks ` +
		`(denominator: present = audio resolves on this machine).${refusal}${indexNote}`;
	// Corruption is a DISTINCT, always-surfaced state: a subset of the work
	// still to do, never folded into a quiet amber.
	if (corrupt > 0) {
		return { label, state: 'error', detail: `${plural(corrupt, 'corrupt entry', 'corrupt entries')} - ${counts}` };
	}
	return { label, state: pending === 0 && failed === 0 ? 'complete' : 'incomplete', detail: counts };
}
