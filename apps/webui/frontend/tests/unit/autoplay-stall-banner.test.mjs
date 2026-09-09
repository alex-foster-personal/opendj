/**
 * PLAY-08: the properties of the stall banner that carry the fix.
 *
 * There is no component-mount harness in this suite, so these are source-shape
 * guards. They pin only the three things whose loss would restore issue #1640,
 * and each one is checked against a control so an unlocatable file fails rather
 * than passing vacuously.
 *
 * [if] the banner gains a dismiss control [then] this reds - an acknowledgement
 *   that leaves the room quiet and the screen blank is the exact failure the
 *   banner replaces [⛔️ if "AutoPlay stopped" can be clicked away while
 *   AutoPlay is still stopped].
 * [if] the banner stops reading the reactive stall [then] it can no longer
 *   appear at all.
 * [if] the performance route stops rendering it [then] the state exists and
 *   nobody sees it, which is the original bug with extra code.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(relative) {
	const source = readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');
	assert.ok(source.length > 0, `${relative} is empty: this guard would assert nothing`);
	return source;
}

const BANNER = read('../../src/lib/components/rb/AutoPlayStallBanner.svelte');
const PERF_PAGE = read('../../src/routes/performance/+page.svelte');

test('the banner renders from the reactive stall and announces itself', () => {
	assert.match(BANNER, /from '\$lib\/rb\/autoplay-stall\.svelte'/);
	assert.match(BANNER, /autoPlayStall\.current/);
	assert.match(BANNER, /role="alert"/);
	assert.match(BANNER, /\{#if stall !== null\}/, 'the banner must be conditional on a live stall');
});

test('the banner names the cause, the way out, and the blocked tracks', () => {
	assert.match(BANNER, /\{stall\.headline\}/);
	assert.match(BANNER, /\{stall\.resume\}/);
	assert.match(BANNER, /describeStallTrack\(track\)/);
	assert.match(BANNER, /\{stall\.blocked_total\}/, 'a capped list must never read as the whole set');
});

test('the banner has no dismiss control', () => {
	const buttons = [...BANNER.matchAll(/<button[\s\S]*?<\/button>/g)].map((match) => match[0]);
	// Control: the assertion below is only meaningful if there IS a button to
	// misclassify. The show/hide-tracks toggle is that button.
	assert.equal(buttons.length, 1, `expected exactly the tracks toggle, found ${buttons.length}`);
	assert.match(buttons[0], /showTracks = !showTracks/);
	assert.equal(
		/dismiss|close|acknowledge|×/i.test(buttons[0]),
		false,
		'a dismissable stop banner reintroduces the five-second toast this replaces'
	);
});

test('the performance route renders it', () => {
	assert.match(PERF_PAGE, /import AutoPlayStallBanner from '\$lib\/components\/rb\/AutoPlayStallBanner\.svelte'/);
	assert.match(PERF_PAGE, /<AutoPlayStallBanner \/>/);
});

test('the ui-mirror publishes the stall for agents', () => {
	const mirror = read('../../src/lib/rb/ui-mirror.ts');
	assert.match(mirror, /autoplay_stall: readAutoPlayStall\(\)/);
});
