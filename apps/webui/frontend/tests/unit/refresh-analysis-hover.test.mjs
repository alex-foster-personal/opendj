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
