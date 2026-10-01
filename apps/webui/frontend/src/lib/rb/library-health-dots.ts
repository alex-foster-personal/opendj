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
 * The rules (HEALTH-01, HEALTH-03, HEALTH-04):
 *
 * - Library health is green when every track whose audio is EXPECTED ON THIS
 *   MACHINE resolves. Rows that live on another machine, wait for an
 *   unmounted volume, stream, or have no path are reported in the detail and
 *   never make the dot amber. Amber is only for a link this machine recorded
 *   as working that no longer resolves.
 * - A coverage dot is green when nothing is pending or failed over `present`
 *   tracks. "Nothing to make" (no lyrics available, no stems source) is a
 *   finished state, counted on its own so it is never mistaken for done.
 * - An endpoint that cannot answer is grey "unknown". A failed or malformed
 *   measurement is not a verdict in either direction.
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
};

const TERMINAL_WORDING: Record<CoverageStep, string> = {
	vocals: 'no stems source',
	stems: 'no stems source',
	lyrics: 'no lyrics available'
};

function isCount(value: unknown): value is number {
	return typeof value === 'number' && Number.isInteger(value) && value >= 0;
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
	availabilityError: string | null
): LibraryHealthDot {
	const label = 'Library health' as const;
	if (libraryHealthError !== null) {
		return { label, state: 'error', detail: libraryHealthError };
	}
	if (allTracksCount === null) {
		return { label, state: 'loading', detail: 'checking library health' };
	}
	// Checked BEFORE any settled counts: a refresh that fails AFTER an
	// earlier one succeeded keeps the prior counts in the caller, and the
	// dot must not go on quoting them once the measurement has failed.
	if (availabilityError !== null) {
		return unknownDot(label, availabilityError);
	}
	if (availability === null) {
		return { label, state: 'loading', detail: 'checking which tracks are on this machine' };
	}
	const counts = availability as Record<string, unknown>;
	if (typeof availability !== 'object' || !AVAILABILITY_KEYS.every((key) => isCount(counts[key]))) {
		return unknownDot(label, 'the engine returned no availability breakdown for this library');
	}
	const here = availability as LibraryAvailability;
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

export function coverageDot(
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
	const waiting =
		step === 'vocals' && isCount(coverage.waiting_on_stems) && coverage.waiting_on_stems > 0
			? ` (${coverage.waiting_on_stems} waiting on stems)`
			: '';
	const refusal =
		step !== 'lyrics' && terminal > 0 && coverage.stems_source_refusal
			? ` Stems cannot be made here: ${coverage.stems_source_refusal}.`
			: '';
	const counts =
		`${done} done, ${terminal} ${TERMINAL_WORDING[step]}, ${pending} pending${waiting}` +
		`${failed > 0 ? `, ${failed} failed` : ''} of ${present} present tracks ` +
		`(denominator: present = audio resolves on this machine).${refusal}`;
	// Corruption is a DISTINCT, always-surfaced state: a subset of the work
	// still to do, never folded into a quiet amber.
	if (corrupt > 0) {
		return { label, state: 'error', detail: `${plural(corrupt, 'corrupt entry', 'corrupt entries')} - ${counts}` };
	}
	return { label, state: pending === 0 && failed === 0 ? 'complete' : 'incomplete', detail: counts };
}
