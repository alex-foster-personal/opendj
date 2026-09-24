/**
 * FB-16 criterion 5: comment-pin explainer opens to the right, not over I/O modal.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const WIDGET = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/FeedbackWidget.svelte', import.meta.url)),
	'utf8'
);

test('performance feedback ControlExplainer uses right placement beside the comment icon', () => {
	const explainer = WIDGET.slice(WIDGET.indexOf('<ControlExplainer'));
	const end = explainer.indexOf('>');
	assert.ok(end > 0);
	const tag = explainer.slice(0, end + 1);
	assert.match(tag, /placement="right"/);
	assert.doesNotMatch(tag, /placement="auto"/);
});
