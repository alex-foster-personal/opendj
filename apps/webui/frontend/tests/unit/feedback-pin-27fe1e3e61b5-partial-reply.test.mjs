/**
 * Pin 27fe1e3e61b5: partial agent work must still accept follow-up replies on
 * the same marker (not Close-only). Link/selectable halves are in
 * feedback-pin-card-note-links.test.mjs; the reply ROUTE appending to the same
 * pin is tests/webui/test_feedback_comments_summary.py::
 * test_pin_27fe1e3e61b5_reply_on_partial_open_pin.
 *
 * RENDERED, not source order: SSR runs the real FeedbackPinCard for a partial
 * pin (an open or issued pin whose agent note starts "PARTIAL:" and names the
 * remaining work) and asserts the follow-up composer is in its markup. SSR
 * cannot type or click (no DOM), so the submit itself is covered by the route
 * test above, not claimed here.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as Card } from '$lib/components/rb/FeedbackPinCard.svelte';",
	"export { pinVisualState } from '$lib/rb/feedback-pin-partial';",
	"export { render } from 'svelte/server';"
].join('\n');

function partialPin(status) {
	return {
		id: '27fe1e3e61b5',
		x_pct: 40,
		y_pct: 40,
		anchor: 'canvas[aria-label="deck 1 waveform seek"]',
		page: '/performance',
		text: 'agent replies are not selectable and partial pins only have close',
		created_at: '2026-09-03T00:00:00Z',
		build: { git_sha: 'abc', built_at_utc: '2026-09-03T00:00:00Z', source: 'test' },
		status,
		agent_note: 'PARTIAL: reply composer shipped; remaining work tracked in #4085',
		issue_url: 'https://github.com/private_owner/music-dj-tools/issues/4085',
		replies: [],
		attachment: null
	};
}

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

for (const status of ['open', 'issued']) {
	test(`pin 27fe1e3e61b5 a partial ${status} pin renders the follow-up composer, not Close only`, () => {
		const pin = partialPin(status);
		assert.equal(mod.pinVisualState(pin), 'partial', 'fixture must actually be a partial pin');
		const html = mod.render(mod.Card, {
			props: {
				pin,
				onclose() {},
				onarchive() {},
				onfollowon() {},
				onreply: async () => null
			}
		}).body;
		assert.match(html, /<textarea[^>]*class="fb-reply[ "][^>]*aria-label="Follow-up comment"/);
		assert.match(html, /<button[^>]*aria-label="Add follow-up comment"[^>]*>Reply<\/button>/);
		assert.match(html, /aria-label="Close comment pin"/);
	});
}
