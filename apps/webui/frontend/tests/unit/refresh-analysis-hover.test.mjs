/**
 * requirement: CHROME-09, CHROME-10
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const refresh = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/RefreshAnalysisButton.svelte', import.meta.url)),
	'utf8'
);

test('refresh analysis button does not set native title alongside popover hover', () => {
	const buttonStart = refresh.indexOf('data-testid="refresh-analysis"');
	const buttonChunk = refresh.slice(refresh.lastIndexOf('<button', buttonStart), buttonStart + 200);
	assert.doesNotMatch(buttonChunk, /\btitle=/);
	assert.match(refresh, /data-testid="refresh-analysis-pop"/);
});

test('refresh click surfaces WIP or running feedback in the popover', () => {
	assert.match(refresh, /clickFeedback/);
	assert.match(refresh, /WIP - not working/);
	assert.match(refresh, /data-testid="refresh-click-feedback"/);
});

// Codex P2 on PR #3896: the click result rendered only when fetchError was
// null AND the polled status was idle, so a failed click after a finished run
// (or after a hover fetch error) showed stale state instead of its outcome.
const popover = refresh.slice(refresh.indexOf('data-testid="refresh-analysis-pop"'));

test('the latest click result renders ahead of the hover error and the polled status', () => {
	const feedbackAt = popover.indexOf('{#if clickFeedback !== null}');
	const errorAt = popover.indexOf('{#if fetchError !== null}');
	const statusAt = popover.indexOf("{#if status !== null && status.phase !== 'idle'}");
	assert.ok(feedbackAt >= 0, 'click feedback must be its own top-level block');
	assert.ok(feedbackAt < errorAt && feedbackAt < statusAt, 'it must render before the stale fields');
	const block = popover.slice(feedbackAt, popover.indexOf('{/if}', feedbackAt));
	assert.match(block, /data-testid="refresh-click-feedback">\{clickFeedback\}/);
	assert.equal((refresh.match(/data-testid="refresh-click-feedback"/g) ?? []).length, 1);
});

test('every failed click records its own outcome, and each click starts from a clean slate', () => {
	const click = refresh.slice(refresh.indexOf('async function onClick()'), refresh.indexOf('onDestroy('));
	assert.match(click, /^async function onClick\(\): Promise<void> \{\s*clickFeedback = null;/);
	const failure = click.slice(click.indexOf('} catch (e) {'));
	for (const outcome of ["'Already running'", "'WIP - not working: no ingestion steps enabled'", '`WIP - not working: ${e.message}`']) {
		assert.ok(failure.includes(`clickFeedback = ${outcome}`), outcome);
	}
	assert.doesNotMatch(click, /hovered = true;/, 'opening without onEnter starts no polling');
});

// Codex P2 4130152643 and 4130407830: every click, started (202) or refused,
// opens the popover the way a hover does (polling included), and only when
// not already open. 4130581613: it opens BEFORE the POST is awaited, never
// after it, so a dismissal while the POST is pending is not undone when it
// settles. A started run still polls while it lasts (_syncTimer after the
// POST), which is how its badges and done toast arrive. Driven for real in
// tests/e2e/refresh-analysis-keyboard-409.spec.ts.
test('every click opens the popover once, before the POST, whatever the outcome', () => {
	const click = refresh.slice(refresh.indexOf('async function onClick()'), refresh.indexOf('onDestroy('));
	assert.equal((click.match(/void onEnter\(\)/g) ?? []).length, 1);
	const openAt = click.indexOf('if (!hovered) void onEnter();');
	assert.ok(openAt > 0 && openAt < click.indexOf('await startIngestRefresh()'), 'opened before the POST');
	const started = click.slice(click.indexOf('await startIngestRefresh()'), click.indexOf('} catch (e) {'));
	assert.match(started, /_syncTimer\(\);/, 'a started run polls while it lasts');
});

test('a keyboard-opened popover closes without a mouse: blur and Escape', () => {
	const buttonStart = refresh.lastIndexOf('<button', refresh.indexOf('data-testid="refresh-analysis"'));
	const button = refresh.slice(buttonStart, refresh.indexOf('>', refresh.indexOf('data-testid="refresh-analysis"')));
	assert.match(button, /onblur=\{onBlur\}/);
	assert.match(button, /onkeydown=\{onKeydown\}/);
	assert.match(refresh, /function onBlur\(\): void \{\s*if \(hovered && !wrapEl\?\.matches\(':hover'\)\) onLeave\(\);/);
	assert.match(refresh, /if \(e\.key === 'Escape' && hovered\) onLeave\(\);/);
});

test('a real running status still renders its progress bar alongside any click result (control)', () => {
	const statusAt = popover.indexOf("{#if status !== null && status.phase !== 'idle'}");
	const progress = popover.slice(statusAt, popover.indexOf('{:else', statusAt));
	assert.match(progress, /class="bar"/);
	assert.match(progress, /status\.step_done/);
	// The status block is not gated on clickFeedback, so a 409 'Already
	// running' click shows the live progress too.
	assert.doesNotMatch(popover.slice(0, statusAt), /\{#if clickFeedback === null\}/);
});

// Codex P2 4130868861: a status GET sent before the POST and answered after it
// put the idle snapshot back. The engine's own start stamp orders snapshots:
// null (no run) coerces to 0 and a first-ever status (undefined) to NaN, which
// compares false, so only an older run or no run is dropped. Driven end to end
// in tests/e2e/refresh-analysis-keyboard-409.spec.ts.
test('a polled snapshot older than the one shown is dropped before it is applied', () => {
	const poll = refresh.slice(refresh.indexOf('async function _poll()'), refresh.indexOf('function _syncTimer()'));
	assert.match(poll, /const s = await getIngestRefreshStatus\(\);[\s\S]*?if \(s\.started_at! < status\?\.started_at!\) return;\s*const wasRunning = status\?\.running === true;\s*status = s;/);
});
