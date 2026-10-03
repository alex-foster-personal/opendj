/**
 * HEALTH-01: Library health counts only audio expected on this machine.
 * The shared reconcile API supplies the production availability predicate.
 * Counts from a failed or malformed measurement are never a verdict.
 * Adapted from Preview source 574f6add3; coverage policy remains separate.
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
	availabilityError: string | null
): LibraryHealthDot {
	const label = 'Library health' as const;
	if (libraryHealthError !== null) {
		return unknownDot(label, libraryHealthError);
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

