/**
 * PREFLIGHT-02 (issue #2589): a dismissed-empty library must not lock a
 * brand-new user behind a dead boot gate. The server-side half is
 * apps/webui/server/preflight_checks.py; this is the frontend half --
 * PreflightCheckRow (the per-check row PreflightScreen renders) must show a
 * real, clickable "Run setup" control for the `library-attached` row when
 * (and only when) it comes back `pending` because setup was dismissed with
 * an empty library.
 *
 * Mounted through the REAL Svelte compiler + SSR renderer
 * (mount-svelte.mjs), not source-grepping: a grep proves the string exists
 * in the file, never that the component actually renders the branch for a
 * given prop. PreflightScreen.svelte itself cannot be SSR-mounted this way
 * (it registers onDestroy for its polling loop, which svelte/server's
 * one-shot render() has nothing to destroy and throws on), which is why the
 * row markup lives in its own lifecycle-hook-free PreflightCheckRow.svelte
 * -- see that file's doc comment.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const RUN_SETUP_TESTID = 'data-testid="preflight-run-setup"';

function check(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		...overrides
	};
}

/** The exact PreflightCheckOut row apps/webui/server/preflight_checks.py
 * produces for a fresh, dismissed-empty install's library-attached check. */
function dismissedEmptyLibraryRow() {
	return check({
		id: 'library-attached',
		label: 'Library attached',
		status: 'pending',
		detail: 'no state.db at /data/state/state.db yet; setup was dismissed with an empty library',
		remediation:
			'Setup was dismissed with an empty library. Click Run setup to import one, or keep using the app empty.'
	});
}

/** The true first-boot fail state: not dismissed, nothing imported yet. */
function freshFailLibraryRow() {
	return check({
		id: 'library-attached',
		label: 'Library attached',
		status: 'fail',
		detail: 'no state.db at /data/state/state.db',
		remediation: 'Run setup to import a library (Cmd+, > Run setup).'
	});
}

/** A fully attached, healthy library: nothing to run setup for. */
function attachedLibraryRow() {
	return check({ id: 'library-attached', label: 'Library attached', detail: '812 tracks' });
}

/** A `pending` row that is NOT library-attached (e.g. audio-access with
 * nothing sampleable) -- must never offer Run setup either. */
function pendingAudioAccessRow() {
	return check({
		id: 'audio-access',
		label: 'Audio access',
		status: 'pending',
		detail: 'no state.db yet; nothing to sample'
	});
}

let bundle;

before(async () => {
	bundle = await bundleSvelteEntry(`
		export { default as PreflightCheckRowComponent } from '$lib/components/preflight/PreflightCheckRow.svelte';
	`);
});

test('dismissed-empty library-attached row renders a clickable, enabled Run setup button', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: dismissedEmptyLibraryRow(),
		navigate: () => {}
	});

	assert.ok(html.includes(RUN_SETUP_TESTID), 'expected a preflight-run-setup control in the markup');
	const buttonMatch = html.match(/<button[^>]*data-testid="preflight-run-setup"[^>]*>/);
	assert.ok(buttonMatch, 'expected a <button> element carrying the testid');
	// A real, live control: not disabled, and not the app's own "dead
	// button" pattern (CLAUDE.md's inert-control house rule).
	assert.doesNotMatch(buttonMatch[0], /disabled/, 'the control must not be disabled in this state');
	assert.doesNotMatch(html, /not implemented - see PARITY-TODO/);
	assert.match(html, />\s*Run setup\s*</);
	assert.ok(html.includes('Setup was dismissed with an empty library'));
});

test('the true fresh-install fail row renders a clickable Run setup control (issue #2722)', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: freshFailLibraryRow(),
		navigate: () => {}
	});

	assert.ok(html.includes(RUN_SETUP_TESTID), 'expected a preflight-run-setup control in the markup');
	const buttonMatch = html.match(/<button[^>]*data-testid="preflight-run-setup"[^>]*>/);
	assert.ok(buttonMatch, 'expected a <button> element carrying the testid');
	assert.doesNotMatch(buttonMatch[0], /disabled/);
});

test('an attached, healthy library row renders no Run setup control', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: attachedLibraryRow(),
		navigate: () => {}
	});

	assert.ok(!html.includes(RUN_SETUP_TESTID));
});

test('a pending row that is not library-attached renders no Run setup control', async () => {
	const html = await renderToHtml(bundle.PreflightCheckRowComponent, {
		check: pendingAudioAccessRow(),
		navigate: () => {}
	});

	assert.ok(!html.includes(RUN_SETUP_TESTID));
});
