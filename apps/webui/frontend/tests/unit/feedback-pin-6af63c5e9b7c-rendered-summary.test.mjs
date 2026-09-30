/**
 * Pin 6af63c5e9b7c / FB-20, RENDERED: the operator total + breakdown from
 * GET /api/v1/feedback/comments/summary reaches the markup of BOTH comment-pin
 * controls (the /performance FeedbackWidget and the app-shell
 * FeedbackPinShellButton), not merely the source text of a helper.
 *
 * SSR (load-svelte-ssr.mjs) runs the real components against the real store
 * module in one bundle, so what is asserted is the markup produced for a given
 * store state. It cannot show the ControlExplainer popover opening on focus
 * (no DOM); the shell button's focus path is asserted as the ControlExplainer
 * wrapper plus an aria-describedby target carrying the same summary.
 */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Shell } from '$lib/components/rb/FeedbackPinShellButton.svelte';",
	"export { default as Widget } from '$lib/components/rb/FeedbackWidget.svelte';",
	"export { feedbackState, commentPinSummaryBullets } from '$lib/rb/feedback-store.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

const OPERATOR_LINE =
	'Active comment pins: 7 total - 2 sent to queue, 3 in-progress, 1 delegated, 0 fixed, 1 merged';

function summary(fleet_correlation = 'ok') {
	return {
		operator: {
			total: 7,
			sent_to_queue: 2,
			in_progress: 3,
			delegated: 1,
			fixed: 0,
			merged: 1,
			blocked: 0,
			harvested: 0
		},
		lifecycle: {
			total: 7,
			untriaged: 2,
			open: 3,
			issued: 1,
			blocked: 0,
			fixed: 0,
			merged: 1,
			harvested: 0
		},
		fleet_correlation
	};
}

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

afterEach(() => {
	mod.feedbackState.availability = 'unknown';
	mod.feedbackState.pins = [];
	mod.feedbackState.pinSummary = null;
});

function decode(html) {
	return html.replaceAll('&amp;', '&').replaceAll('&quot;', '"').replaceAll('&#39;', "'");
}

function titleOfCommentButton(html) {
	const button = decode(html).match(/<button[^>]*aria-label="Drop a comment pin"[^>]*>/);
	assert.ok(button, 'the comment-pin button must render');
	return button[0].match(/title="([^"]*)"/)?.[1] ?? null;
}

test('pin 6af63c5e9b7c the shell button title carries the server operator breakdown', () => {
	mod.feedbackState.availability = 'ok';
	mod.feedbackState.pinSummary = summary();
	const html = mod.render(mod.Shell, { props: {} }).body;
	assert.equal(
		titleOfCommentButton(html),
		`${OPERATOR_LINE}. Click to drop a comment pin anywhere on the UI`
	);
});

test('pin 6af63c5e9b7c the shell button exposes the breakdown to focus and screen readers', () => {
	mod.feedbackState.availability = 'ok';
	mod.feedbackState.pinSummary = summary();
	const html = decode(mod.render(mod.Shell, { props: {} }).body);
	assert.match(html, /<span class="explainer[^"]*"/, 'wrapped in ControlExplainer (focusin opens it)');
	const describedBy = html.match(/aria-describedby="([^"]+)"/)?.[1];
	assert.ok(describedBy, 'the button must name a description element');
	const target = html.match(new RegExp(`<span id="${describedBy}"[^>]*>([^<]*)</span>`));
	assert.ok(target, 'aria-describedby must point at an element that is actually rendered');
	assert.match(target[1], new RegExp(`^${OPERATOR_LINE}`));
});

test('pin 6af63c5e9b7c the /performance widget button title carries the same breakdown', () => {
	mod.feedbackState.availability = 'ok';
	mod.feedbackState.pinSummary = summary();
	const html = mod.render(mod.Widget, { props: {} }).body;
	assert.equal(
		titleOfCommentButton(html),
		`${OPERATOR_LINE}. Click to drop a comment pin anywhere on the UI`
	);
});

test('pin 6af63c5e9b7c without the summary route both controls keep the lifecycle line', () => {
	// Opposite direction: a daemon that never served /comments/summary must not
	// be shown operator buckets it never measured.
	mod.feedbackState.availability = 'ok';
	mod.feedbackState.pins = [
		{ id: 'a', status: null },
		{ id: 'b', status: 'issued' }
	];
	for (const component of [mod.Shell, mod.Widget]) {
		const title = titleOfCommentButton(mod.render(component, { props: {} }).body);
		assert.match(title, /^Active comment pins: 2 total - 1 untriaged, 0 open, 1 issued/);
		assert.doesNotMatch(title, /sent to queue/);
	}
});

test('pin 6af63c5e9b7c explainer bullets: untracked stub only without the route, fleet gap stated', () => {
	mod.feedbackState.availability = 'ok';
	const noRoute = mod.commentPinSummaryBullets([]);
	assert.ok(noRoute.some((b) => /not tracked by the comment API yet/.test(b)));

	mod.feedbackState.pinSummary = summary('ok');
	const measured = mod.commentPinSummaryBullets([]);
	assert.equal(measured[0], OPERATOR_LINE);
	assert.ok(!measured.some((b) => /not tracked|excludes fleet work/.test(b)), measured.join(' | '));

	mod.feedbackState.pinSummary = summary('ledger_missing');
	const noLedger = mod.commentPinSummaryBullets([]);
	assert.ok(
		noLedger.some((b) => /In-progress excludes fleet work: this daemon has no progress ledger/.test(b)),
		'an unmeasured fleet correlation must be said, never shown as zero fleet work'
	);
});
