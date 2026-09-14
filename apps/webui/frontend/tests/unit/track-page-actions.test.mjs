import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

/**
 * UXR-02 (adversarial UX round 1, UX-R1-05): the track detail page's Actions row.
 *
 * Regression lines:
 * - if roadmap wording ("coming in Phase 17") comes back then a DJ meets an
 *   internal planning note where the actions should be
 * - if Show in Finder is not wired to the reveal path then the one action that
 *   exists is a dead click
 * - if Open in Rekordbox or djay is enabled then an unbuilt control looks live
 */

const PAGE = readFileSync(new URL('../../src/routes/track/[stable_id]/+page.svelte', import.meta.url), 'utf8');

test('the track page carries no roadmap phase wording', () => {
	assert.doesNotMatch(PAGE, /coming in Phase|Phase \d+\)/);
});

test('Show in Finder calls the same reveal path as the track context menu', () => {
	assert.match(PAGE, /import \{ REVEAL_TRACK_TITLE, runRevealTracks \} from '\$lib\/components\/rb\/browser\/track-context-menu'/);
	assert.match(PAGE, /onclick=\{\(\) => track !== null && void runRevealTracks\(\[track\.stable_id\]\)\}>\s*Show in Finder/);
});

test('Open in Rekordbox and Open in djay render disabled with the PARITY-TODO tooltip', () => {
	assert.match(PAGE, /const INERT_TITLE = 'not implemented - see PARITY-TODO';/);
	assert.match(PAGE, /\{#each \['Open in Rekordbox', 'Open in djay'\] as label\}/);
	assert.match(PAGE, /<button type="button" disabled title=\{INERT_TITLE\} aria-label=\{`\$\{label\} - \$\{INERT_TITLE\}`\}>/);
});
