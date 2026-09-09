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
 * [if] a dismiss control is added [then] this reds - an acknowledgement that
 *   leaves the room quiet and the screen blank is the failure being removed ⛔️
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

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

test('the rendered banner offers no way to dismiss it', () => {
	const html = renderFor(stallWith(2));
	const buttons = [...html.matchAll(/<button[\s\S]*?<\/button>/g)].map((match) => match[0]);
	// Control: the assertion is only meaningful if there IS a rendered button
	// to misclassify. The show-tracks toggle is that button.
	assert.equal(buttons.length, 1, `expected exactly the tracks toggle, rendered ${buttons.length}`);
	assert.match(buttons[0], /Show the 2 tracks/);
	assert.equal(
		/dismiss|close|acknowledge|hide banner|×/i.test(buttons[0]),
		false,
		'a dismissable stop banner reintroduces the five-second toast this replaces'
	);
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
