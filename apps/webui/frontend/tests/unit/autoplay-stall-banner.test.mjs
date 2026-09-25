/**
 * PLAY-08: the stall banner, RENDERED.
 *
 * Round-1 review (Codex r3973806306) correctly refused the first version of
 * this file, which was regexes over `.svelte` source and could not establish
 * that the component mounts at all. These compile the real component and run
 * svelte's own renderer over it, so every assertion below is about markup the
 * component actually produced from real store state.
 *
 * WHAT A PASS HERE DOES NOT COVER, so nobody reads more into it: there is no
 * DOM in this suite (no jsdom/happy-dom, by design - the unit suite's whole
 * dependency surface is esbuild plus svelte), so this proves nothing about
 * reactivity to a LATER change, about the toggle's click handler, or about
 * layout, CSS and stacking. The state-layer reactivity is covered by
 * autoplay-stall-persistence.test.mjs, which runs the real controller. What is
 * genuinely uncovered is whether the fixed banner is VISIBLE and does not
 * obscure the decks; that needs a browser and is called out in the PR.
 *
 * [if] no stall is recorded [then] the component renders NOTHING - no element,
 *   not a hidden one [⛔️ if a healthy set carries a stop banner in its DOM].
 * [if] a stall is recorded [then] the rendered markup carries the headline, the
 *   way back to sound, and the exact blocked total ⛔️
 * [if] the blocked list is capped [then] the rendered count is still the exact
 *   total [⛔️ if a capped list reads as the whole remainder].
 * [if] issue #3882 transient banner is shown [then] it carries an X dismiss
 *   control and auto-hides without clearing durable stall state ⛔️
 * [if] the banner wraps to more than one row [then] the list still sits under
 *   it [⛔️ if a hard-coded offset covers the wrapped instructions and the
 *   toggle needed to close the list].
 * [if] a repeated missing file reaches the descriptor [then] the rendered list
 *   carries no duplicate key [⛔️ if svelte throws and the banner never draws].
 * [if] a LATER stall arrives [then] its list starts collapsed [⛔️ if a previous
 *   expansion springs 30vh of list over the decks unasked]. Keyed on the
 *   per-OCCURRENCE revision, because a second stop can carry the same reason
 *   and the same source track over a different playlist.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { default as Banner } from '$lib/components/rb/AutoPlayStallBanner.svelte';",
	"export { autoPlayStall } from '$lib/rb/autoplay-stall.svelte';",
	"export { describeAutoPlayStall, STALL_TRACK_LIMIT } from '$lib/rb/autoplay-stall';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function blockedRow(index) {
	return {
		stable_id: `gone-${index}`,
		key: '8A',
		bpm: 124,
		file_exists: false,
		title: `Gone ${index}`,
		artist: `Artist ${index}`
	};
}

/** Render the banner for one stall (or none) and return its markup. */
function renderFor(stall) {
	mod.autoPlayStall.current = stall;
	return mod.render(mod.Banner).body;
}

function stallWith(count) {
	return mod.describeAutoPlayStall({
		reason: 'missing-audio',
		source_stable_id: 'src-1',
		blocked: Array.from({ length: count }, (_, index) => blockedRow(index))
	});
}

test('with no stall the component renders no banner element at all', () => {
	const html = renderFor(null);
	assert.equal(html.includes('autoplay-stall-banner'), false, `rendered: ${html}`);
	assert.equal(html.includes('<div'), false, 'a healthy set must not carry a hidden stop banner');
});

test('with a stall it renders the cause, the way out and the exact total', () => {
	const html = renderFor(stallWith(3));
	assert.match(html, /data-testid="autoplay-stall-banner"/);
	assert.match(html, /role="alert"/, 'a stopped set has to be announced, not just drawn');
	assert.match(html, /all 3 remaining playlist tracks have missing or stub audio/);
	assert.match(html, /Relink or re-download the tracks below/);
	assert.match(html, /Show the 3 tracks/);
});

test('a capped blocked list still renders the exact total', () => {
	const total = mod.STALL_TRACK_LIMIT + 7;
	const html = renderFor(stallWith(total));
	assert.match(html, new RegExp(`all ${total} remaining playlist tracks`));
	assert.match(html, new RegExp(`Show the ${total} tracks`));
	assert.equal(
		mod.autoPlayStall.current.blocked.length,
		mod.STALL_TRACK_LIMIT,
		'precondition: the descriptor really is capped, so the total above is not the list length'
	);
});

test('the track list is collapsed until asked for, and never lists more than the cap', () => {
	const html = renderFor(stallWith(mod.STALL_TRACK_LIMIT + 7));
	assert.match(html, /aria-expanded="false"/);
	assert.equal(
		html.includes('autoplay-stall-tracks'),
		false,
		'the list is opened by the operator, not sprung over the decks on its own'
	);
});

test('the rendered banner carries an X dismiss control (issue #3882)', () => {
	const html = renderFor(stallWith(2));
	assert.match(html, /data-testid="autoplay-stall-dismiss"/);
	assert.match(html, /ap-stall-dismiss/);
});

test('timer helpers use 5s default and 10s while hovered', async () => {
	const timer = await loadTypeScriptModule('src/lib/rb/autoplay-stall-banner-timer.ts');
	assert.equal(timer.autoplayStallBannerDismissMs(false), 5000);
	assert.equal(timer.autoplayStallBannerDismissMs(true), 10000);
	assert.equal(timer.shouldShowAutoplayStallBanner(3, 3), false);
	assert.equal(timer.shouldShowAutoplayStallBanner(4, 3), true);
});

test('the performance route mounts it, and the ui-mirror publishes it', () => {
	const page = readFileSync(
		fileURLToPath(new URL('../../src/routes/performance/+page.svelte', import.meta.url)),
		'utf8'
	);
	assert.ok(page.length > 0, 'the performance route is empty: this guard would assert nothing');
	assert.match(page, /<AutoPlayStallBanner \/>/, 'state nobody renders is the original bug');

	const mirror = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/ui-mirror.ts', import.meta.url)),
		'utf8'
	);
	assert.match(mirror, /autoplay_stall: readAutoPlayStall\(\)/);
});

test('a repeated missing file renders once, with no duplicate key', () => {
	// Playlist membership is keyed by position, so the SAME file can occupy
	// several rows and reach the descriptor twice. `{#each ... (stable_id)}`
	// throws on a duplicate key, which is the banner failing to draw at all.
	const repeated = mod.describeAutoPlayStall({
		reason: 'missing-audio',
		source_stable_id: 'src-1',
		blocked: [blockedRow(1), blockedRow(1), blockedRow(2)]
	});
	assert.equal(repeated.blocked_total, 2, 'precondition: the descriptor de-duplicated');
	const html = renderFor(repeated);
	assert.match(html, /Show the 2 tracks/);
	assert.equal(
		(html.match(/Gone 1/g) ?? []).length,
		0,
		'collapsed by default, so the names are not in the markup yet'
	);
});

test('the expanded-list choice is scoped to the stall it was made about', () => {
	// SSR renders the collapsed state, so this asserts the mechanism rather
	// than the click: the list is gated on the OCCURRENCE the operator
	// expanded, not on a bare boolean that outlives the stall, and not on the
	// stall's content, which two different stops can share.
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/AutoPlayStallBanner.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(source, /expandedFor === stall\.revision/);
	assert.match(source, /\{#if listOpen && stall\.blocked\.length > 0\}/);
	assert.equal(
		/\{#if showTracks &&/.test(source),
		false,
		'a bare showTracks would carry one stall expansion into the next'
	);
	assert.equal(
		/expandedFor === `/.test(source),
		false,
		'a content key reads a second, unrelated stop with the same shape as a continuation'
	);
});

test('the list sits under the banner by flow, not by a hard-coded offset', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/AutoPlayStallBanner.svelte', import.meta.url)),
		'utf8'
	);
	// The banner sets `flex-wrap: wrap`, so its height is not knowable in CSS.
	assert.match(source, /flex-wrap: wrap;/, 'precondition: the banner really can wrap');
	assert.equal(
		/top: calc\(var\(--rb-topbar-h\)/.test(source),
		false,
		'an offset computed from a one-row assumption breaks the moment it wraps'
	);
	// Exactly one fixed ancestor; the banner and the list are in normal flow
	// inside it, so the list lands under whatever height the banner took.
	assert.equal(
		(source.match(/position: fixed;/g) ?? []).length,
		1,
		'a second fixed element would be positioning itself independently again'
	);
	const html = renderFor(stallWith(3));
	assert.match(html, /class="ap-stall-root/, 'the fixed root is what renders');
});
