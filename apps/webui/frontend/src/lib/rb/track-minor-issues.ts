/** Minor track problems for the error-square indicator (CHROME-03). */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';

export type MinorIssueId =
	| 'not-on-cloud'
	| 'low-quality'
	| 'missing-genre'
	| 'stems-failed'
	| 'no-lyrics';

export interface MinorIssue {
	id: MinorIssueId;
	label: string;
}

/** Bottom venue rungs on the quality ladder (rank 0-1). */
const LOW_QUALITY_VENUES = new Set(['naughty_step', 'lounge']);

/** kbps below lounge ceiling when venue is unknown. */
const LOW_KBPS_THRESHOLD = 128;

/** Same streaming facts trackCloudView() uses: an unmatched Spotify
 * placeholder (spotify_pending) streams too, though it carries no rekordbox
 * path and is_streaming stays false. */
function _isStreaming(row: BrowserRow): boolean {
	return (
		row.is_streaming === true || row.rb_meta?.is_streaming === true || row.spotify_pending === true
	);
}

export function minorIssuesFor(row: BrowserRow): MinorIssue[] {
	const issues: MinorIssue[] = [];

	// Only an explicit false is "not on CloudSync". Missing Tracks rows carry no
	// remote-copy fact at all (BrokenTrackOut has none), and unknown is not no.
	if (row.has_remote_copy === false && !_isStreaming(row)) {
		issues.push({ id: 'not-on-cloud', label: 'Not on CloudSync' });
	}

	const q = row.quality;
	if (q !== null) {
		if (q.venue !== null && LOW_QUALITY_VENUES.has(q.venue)) {
			issues.push({ id: 'low-quality', label: `Low quality (${q.label})` });
		} else if (q.venue === null && q.kbps !== null && q.kbps < LOW_KBPS_THRESHOLD) {
			issues.push({
				id: 'low-quality',
				label: `Low quality (${q.kbps} kbps effective)`
			});
		}
	}

	const genre = row.genre ?? row.rb_meta?.genre ?? null;
	if ((genre === null || genre.trim() === '') && row.genre_reason) {
		issues.push({ id: 'missing-genre', label: 'Missing genre' });
	}

	if (row.stems?.status === 'invalid') {
		issues.push({
			id: 'stems-failed',
			label: row.stems.error ? `Stems failed: ${row.stems.error}` : 'Stems failed'
		});
	}

	// "Stems not run remotely" is deliberately NOT emitted: no row field
	// carries a remote stem job or artifact status. has_remote_copy is the
	// original audio's cloud presence and row.stems is a LOCAL stems-dir
	// scan, so combining them would fabricate the warning. Missing data
	// source: a per-row remote stem status on the listing wire (see
	// PARITY-TODO); add the issue back only when that field exists.

	// Only the lyric verdict establishes "no lyrics"; has_words === false
	// with n_lines set is line-synced lyrics, not an absence of lyrics.
	if (row.lyrics?.effective === 'no-lyrics') {
		issues.push({ id: 'no-lyrics', label: 'No lyrics' });
	}

	return issues;
}
