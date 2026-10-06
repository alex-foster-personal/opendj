/**
 * The /performance top bar must always offer a way to start Google sign-in
 * (issue #2357).
 *
 * Two defects, both in the bauble:
 *   1. `@media (max-width: 825px)` evicted `.bauble-root` outright, so at the
 *      harden-lane capture width (800x600) the ONLY sign-in affordance on the
 *      route was `display: none`. The rule was not wrong to evict something -
 *      the row is crowded at that width - but it evicted the one control with
 *      no substitute.
 *   2. Where it WAS shown the control was a bare circle: "Sign in with Google"
 *      lived only in `aria-label` / `title`, so it read as decoration.
 *
 * Regression lines:
 * - if the 825px tier hides `.bauble-root` again then /performance has no
 *   sign-in affordance at the capture width, which is the reported bug
 * - if the 825px tier is emptied instead of narrowed then the overflow it was
 *   added to solve comes back
 * - if the label is only an aria-label then it is decoration again
 * - if `google_oauth_configured` is not read then the control looks operable on
 *   a daemon that will fail it after the click
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const TOPBAR_PATH = fileURLToPath(
	new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)
);
const BAUBLE_PATH = fileURLToPath(
	new URL('../../src/lib/components/UserBauble.svelte', import.meta.url)
);
const topbar = readFileSync(TOPBAR_PATH, 'utf8');
const bauble = readFileSync(BAUBLE_PATH, 'utf8');

/** The declaration block of one `@media` rule, brace-matched. */
function mediaBlock(source, query) {
	const start = source.indexOf(`@media ${query} {`);
	assert.notEqual(start, -1, `@media ${query} not found`);
	const open = source.indexOf('{', start);
	let depth = 0;
	for (let i = open; i < source.length; i += 1) {
		if (source[i] === '{') depth += 1;
		else if (source[i] === '}') {
			depth -= 1;
			if (depth === 0) return source.slice(open + 1, i);
		}
	}
	throw new Error(`@media ${query} is not brace-balanced`);
}

// The read-only status tier. 1257px since Tue 6 Oct 2026: the pinned Settings and
// skin toggle (#5456) moved it up from 1160px (see the note on that tier).
const NARROW = mediaBlock(topbar, '(max-width: 1257px)');
const COMMAND = mediaBlock(topbar, '(max-width: 1023px)');

test('TopBar.svelte still compiles', () => {
	compile(topbar, { filename: TOPBAR_PATH, generate: 'client' });
});

test('UserBauble.svelte still compiles', () => {
	compile(bauble, { filename: BAUBLE_PATH, generate: 'client' });
});

// ------------------------------------------------------ defect 1: hidden

test('no tier evicts the sign-in control', () => {
	// `display: none` on `.bauble-root` is the reported bug. It must not come
	// back at ANY width: on /performance this is the only sign-in affordance.
	assert.doesNotMatch(
		topbar,
		/\.bauble-root[^}]*display:\s*none/,
		'a tier hides the only sign-in affordance on /performance'
	);
	assert.doesNotMatch(topbar, /\.bauble-root\s*[,{]/);
});

test('the narrow tier still evicts the chrome it always did', () => {
	// CHROME-07 (issue #3886) moved MIDI out of the top bar to settings, the
	// I/O view and the bottom tray, so there is no MIDI slot left to evict.
	assert.doesNotMatch(topbar, /topbar-slot-midi/, 'the top bar MIDI slot came back (CHROME-07)');
	for (const selector of ['refresh-analysis']) {
		assert.match(NARROW, new RegExp(selector.replace(/[[\]"]/g, '\\$&')), `${selector} left the narrow tier`);
	}
	// The free badge and the utility icons moved from 1400px to 1530px with the
	// rest of the ladder; they are still evicted, just later.
	const wide = mediaBlock(topbar, '(max-width: 1530px)');
	assert.match(wide, /free-badge/);
	assert.match(wide, /topbar-slot-utility/);
});


test('the row pays for the label with read-only status chrome', () => {
	// The label is ~100px of new permanent row width and the row had none to
	// give, so every status surface yields before any control does - the same
	// ranking the 1530px note already states. Each of these three REPORTS state
	// and operates nothing.
	assert.match(NARROW, /:global\(\.perf-meters-root\)/);
	assert.match(NARROW, /:global\(\.posture-chip\)/);
	assert.match(NARROW, /:global\(\.cloudsync-status\)/);
});

test('every other-component slot in the eviction lists is :global', () => {
	// Svelte scopes a bare `.foo` in this component's <style> to TopBar's own
	// hash, and AppPostureChip / PerfMeters / CloudSyncStatusChip carry
	// different hashes. A bare selector there is a rule that silently matches
	// nothing - measured: `.posture-chip` cost the 800px row 67px of eviction
	// that never happened.
	// Strip comments first: the tier notes quote selectors in prose, and a
	// match inside a comment is not a rule.
	const css = topbar.slice(topbar.indexOf('<style>')).replaceAll(/\/\*[\s\S]*?\*\//g, '');
	for (const foreign of ['perf-meters-root', 'posture-chip', 'cloudsync-status', 'cmd-entry']) {
		const bare = new RegExp(`\\.${foreign}\\s*[,{]`);
		assert.doesNotMatch(css, bare, `.${foreign} is another component's class and needs :global()`);
	}
});

test('the command entry yields only below 1024px, where the e2e gate stops asserting it', () => {
	assert.match(COMMAND, /\.cmd-entry/);
	assert.match(NARROW, /refresh-analysis/);
});


// ----------------------------------------------------- defect 2: unlabeled

test('the performance top bar asks the bauble for a visible label', () => {
	assert.match(topbar, /<UserBauble[^>]*\bshowLabel\b/);
});

test('UserBauble renders its label as text, not only as an aria-label', () => {
	assert.match(bauble, /showLabel\s*=\s*false/, 'showLabel prop missing or defaulted wrong');
	assert.match(bauble, /class="bauble-label"/);
	assert.match(bauble, /Sign in with Google/);
});

test('the signed-out label is the accessible name too, so both read the same', () => {
	// One string, two consumers: a button whose aria-label and visible text
	// disagree is the "two different sentences" defect.
	assert.match(bauble, /aria-label=\{label\}/);
});

// ------------------------------------------------- defect 3: unavailable

test('UserBauble reads the daemon OAuth bit rather than inventing one', () => {
	assert.match(bauble, /googleOAuthConfigured/);
	assert.doesNotMatch(bauble, /\/api\/v1\/health/, 'the bauble fetches health itself');
});

test('an unconfigured daemon disables the control instead of failing the click', () => {
	assert.match(bauble, /const signInUnavailable = \$derived\(/);
	assert.match(bauble, /disabled=\{[^}]*signInUnavailable/);
});

