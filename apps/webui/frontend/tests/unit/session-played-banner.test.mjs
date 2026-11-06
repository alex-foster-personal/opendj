/**
 * SESSION-PLAYED-01: the live session control consumes no track-table row.
 *
 * [if] a live session has played tracks [then] New session is one click away, [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const componentSource = readFileSync(
	new URL('../../src/lib/components/rb/browser/SessionPlayedBanner.svelte', import.meta.url),
	'utf8'
);

let bundle;

before(async () => {
	bundle = await bundleSvelteEntry(`
		export { default as SessionPlayedBanner } from '$lib/components/rb/browser/SessionPlayedBanner.svelte';
		export { sessionPlayed } from '$lib/rb/session-played.svelte';
	`);
});

function setSession(phase, ids) {
	bundle.sessionPlayed.phase = phase;
	bundle.sessionPlayed.visible = new Set(ids);
	bundle.sessionPlayed.doc = {
		session_id: 'session-1',
		started_at_ms: 1_799_942_400_000,
		last_activity_ms: 1_799_946_000_000,
		expired: phase === 'expired',
		played_ids: [...ids],
		count: ids.length
	};
}

function cssRule(selector) {
	const match = componentSource.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`));
	assert.ok(match, `if ${selector} has no CSS rule then its layout contract is missing`);
	return match[1];
}

function buttonTag(html, testId) {
	const match = html.match(new RegExp(`<button[^>]*data-testid="${testId}"[^>]*>`));
	assert.ok(match, `if ${testId} is not rendered then the session action is unreachable`);
	return match[0];
}

test('live played count overlays the table without reserving a row', async () => {
	setSession('live', ['track-a', 'track-b']);
	const html = await renderToHtml(bundle.SessionPlayedBanner);

	assert.match(html, /data-testid="session-played-line"/, 'the live played count is missing');
	assert.match(html, />2 played this session</, 'the live played count is wrong');
	assert.match(
		componentSource,
		/<div class="session-live-anchor">\s*<div class="session-line"/,
		'the live root steals a track-table row instead of using its zero-height anchor'
	);
	assert.match(cssRule('.session-live-anchor'), /position:\s*relative;/);
	assert.match(cssRule('.session-live-anchor'), /flex:\s*0 0 0;/);
	assert.match(cssRule('.session-live-anchor'), /height:\s*0;/);
	assert.match(cssRule('.session-live-anchor'), /overflow:\s*visible;/);
	assert.match(
		cssRule('.session-line'),
		/position:\s*absolute;/,
		'the live control must leave normal flow at 1280x800'
	);
});

test('live New session remains an enabled one-click action', async () => {
	setSession('live', ['track-a']);
	const html = await renderToHtml(bundle.SessionPlayedBanner);
	const newSession = buttonTag(html, 'session-new');

	assert.doesNotMatch(newSession, /\sdisabled(?:[= >])/, 'New session is disabled while idle');
	assert.match(
		componentSource,
		/onclick=\{run\(startNewSession\)\}[^>]*data-testid="session-new"/,
		'New session requires more than one click or bypasses the existing action'
	);
});

test('expired session keeps the in-flow Restore and New session choices', async () => {
	setSession('expired', ['track-a', 'track-b']);
	const html = await renderToHtml(bundle.SessionPlayedBanner);

	assert.match(html, /data-testid="session-restore-banner"/, 'the recovery prompt is obscured');
	assert.match(html, /data-testid="session-restore"/, 'Restore is missing from recovery');
	assert.match(html, /data-testid="session-new"/, 'New session is missing from recovery');
	assert.doesNotMatch(
		html,
		/session-live-anchor/,
		'the expired recovery choice must stay in flow instead of using the zero-height anchor'
	);
});
