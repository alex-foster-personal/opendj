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
	assert.match(failure, /hovered = true;/, 'a failed click opens the popover that shows it');
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
