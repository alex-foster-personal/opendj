import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

/**
 * UXR-02 (adversarial UX round 1, UX-R1-05): the track detail page's Actions row.
 *
 * TrackActions is compiled by the real Svelte compiler and rendered by Svelte's
 * own SSR renderer (mount-svelte.mjs), so these assertions read what a user is
 * served, not what the source file happens to contain.
 *
 * Regression lines:
 * - if roadmap wording ("coming in Phase 17") comes back then a DJ meets an
 *   internal planning note where the actions should be
 * - if Show in Finder renders disabled or loses the reveal route then the one
 *   action that exists is a dead end
 * - if Open in Rekordbox or djay renders enabled then an unbuilt control looks live
 */

const INERT_TITLE = 'not implemented - see PARITY-TODO';
let html;
let revealTitle;

before(async () => {
	const mod = await bundleSvelteEntry(
		"export { default as TrackActions } from '$lib/components/TrackActions.svelte';\n" +
			"export { REVEAL_TRACK_TITLE } from '$lib/components/rb/browser/track-context-menu';"
	);
	revealTitle = mod.REVEAL_TRACK_TITLE;
	html = await renderToHtml(mod.TrackActions, { stableId: 'rb:42' });
});

function buttonTag(label) {
	const match = html.match(new RegExp(`<button[^>]*>\\s*${label}\\s*</button>`));
	assert.ok(match, `if no rendered button reads ${label} then the action is missing from the page`);
	return match[0];
}

test('Show in Finder renders enabled and names the reveal route it calls', () => {
	const tag = buttonTag('Show in Finder');
	assert.doesNotMatch(tag, /\sdisabled/);
	assert.equal(revealTitle, 'POST /api/v1/tracks/{stable_id}:reveal');
	assert.ok(tag.includes(`title="${revealTitle}"`), tag);
});

test('Open in Rekordbox and Open in djay render disabled with the PARITY-TODO tooltip', () => {
	for (const label of ['Open in Rekordbox', 'Open in djay']) {
		const tag = buttonTag(label);
		assert.match(tag, /\sdisabled/, `if ${label} is enabled then an unbuilt control looks live`);
		assert.ok(tag.includes(`title="${INERT_TITLE}"`), tag);
		assert.ok(tag.includes(`aria-label="${label} - ${INERT_TITLE}"`), tag);
	}
});

test('the rendered actions carry no roadmap phase wording', () => {
	assert.doesNotMatch(html, /Phase/);
});

test('the track route renders TrackActions for its track and keeps no phase wording of its own', () => {
	// The route itself loads its track in onMount, which SSR never runs, so the
	// wiring from route to component is the one part pinned as source text.
	const page = readFileSync(new URL('../../src/routes/track/[stable_id]/+page.svelte', import.meta.url), 'utf8');
	assert.match(page, /<TrackActions stableId=\{track\.stable_id\} \/>/);
	assert.doesNotMatch(page, /coming in Phase/);
});
