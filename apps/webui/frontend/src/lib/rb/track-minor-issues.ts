/** Minor track problems for the error-square indicator (CHROME-03). */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';

export type MinorIssueId =
	| 'not-on-cloud'
	| 'low-quality'
	| 'missing-genre'
	| 'stems-failed'
	| 'stems-not-run-remote'
	| 'no-lyrics';

export interface MinorIssue {
	id: MinorIssueId;
	label: string;
}

/** Bottom venue rungs on the quality ladder (rank 0-1). */
const LOW_QUALITY_VENUES = new Set(['naughty_step', 'lounge']);

/** kbps below lounge ceiling when venue is unknown. */
const LOW_KBPS_THRESHOLD = 128;

function _isStreaming(row: BrowserRow): boolean {
	return row.is_streaming === true || row.rb_meta?.is_streaming === true;
}

export function minorIssuesFor(row: BrowserRow): MinorIssue[] {
	const issues: MinorIssue[] = [];

	if (row.has_remote_copy !== true && !_isStreaming(row)) {
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

	if (row.has_remote_copy === true && row.stems?.status === 'none') {
		issues.push({ id: 'stems-not-run-remote', label: 'Stems not run remotely yet' });
	}

	const lyrics = row.lyrics;
	if (lyrics !== null) {
		if (lyrics.effective === 'no-lyrics') {
			issues.push({ id: 'no-lyrics', label: 'No lyrics' });
		} else if (lyrics.has_words === false && lyrics.n_lines !== null) {
			issues.push({ id: 'no-lyrics', label: 'No lyrics' });
		}
	}

	return issues;
}
