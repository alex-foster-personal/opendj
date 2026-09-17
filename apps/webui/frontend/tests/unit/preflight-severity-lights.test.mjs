/**
 * Three light colours, rendered through the REAL Svelte compiler, not grepped.
 *
 * the maintainer, Wed 16 Sep 2026, on the first-run welcome screen: "maybe use green
 * orange red circles instead of red for everything, some checks aren't so
 * important", plus "what to do if X in hover".
 *
 * - if a failed ADVISORY row paints red then the colour still says stop for
 *   something the user can ignore -> broken.
 * - if a failed BLOCKING row paints orange then red has stopped meaning stop
 *   -> broken, and worse than the bug being fixed.
 * - if a row renders no hover explainer then the user has to break the app to
 *   learn what the row is -> broken.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const EXPLAINER = 'Open DJ runs a small engine on your Mac.';

// Svelte appends a scoped class (svelte-xxxxxxx) to every styled element, so
// the class attribute is a TOKEN LIST, never an exact string. Read it as one.
function lightClasses(html) {
	const found = [];
	for (const match of html.matchAll(/class="([^"]*)"/g)) {
		for (const token of match[1].split(/\s+/)) {
			if (token.startsWith('light-')) { found.push(token); }
		}
	}
	return found;
}

function row(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		severity: 'blocking',
		explainer: EXPLAINER,
		...overrides
	};
}

let bundle;

before(async () => {
	bundle = await bundleSvelteEntry(`
		export { default as PreflightCheckRowComponent } from '$lib/components/preflight/PreflightCheckRow.svelte';
	`);
});

test('control: the class reader finds exactly one light class per row', async () => {
	// Without this, a reader that silently matches nothing would make every
	// deepEqual below pass against an empty array.
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, { check: row() });
	assert.equal(lightClasses(html).length, 1, `expected one light-* class in: ${html}`);
});

test('a passing row is green', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, { check: row() });
	assert.deepEqual(lightClasses(html), ['light-pass']);
});

test('a failed BLOCKING row is red', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: row({ status: 'fail', severity: 'blocking', remediation: 'Restart Open DJ.' })
	});
	assert.deepEqual(lightClasses(html), ['light-fail']);
});

test('a failed ADVISORY row is orange, not red', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: row({
			id: 'library-attached',
			label: 'Library attached',
			status: 'fail',
			severity: 'advisory',
			detail: '0 tracks in the library',
			explainer: 'Orange just means no tracks yet.'
		})
	});
	assert.deepEqual(lightClasses(html), ['light-warn']);
});

test('an unexercised row is orange whatever its severity', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: row({ id: 'audio-access', status: 'pending', severity: 'blocking' })
	});
	assert.deepEqual(lightClasses(html), ['light-warn']);
});

test('a row with no severity at all reads as blocking', async () => {
	// Control: an older engine, or a check nobody classified, must not be
	// silently downgraded to a colour the user ignores.
	const legacy = row({ status: 'fail' });
	delete legacy.severity;
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, { check: legacy });
	assert.deepEqual(lightClasses(html), ['light-fail']);
});

test('the explainer is on the row and in the hover title', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, { check: row() });
	assert.ok(html.includes(EXPLAINER), 'explainer text must be visible on the row');
	assert.match(html, /title="[^"]*Working\./, 'the light must say what its colour means');
});

test('the hover says what a red light means, in plain words', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: row({ status: 'fail', severity: 'blocking' })
	});
	assert.match(html, /Open DJ needs this before it can run/);
});

test('the hover says an orange light can be carried past', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: row({ status: 'fail', severity: 'advisory' })
	});
	assert.match(html, /you can carry on/);
});
