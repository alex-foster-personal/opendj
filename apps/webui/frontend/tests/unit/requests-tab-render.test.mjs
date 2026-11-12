/**
 * Phone remote Requests tab render (issue #10448, CROWDREQ-10761).
 *
 * Pure HTML from requests-tab-render.js: names, votes, age, dismiss, load,
 * mark loaded, and session open/close. requester_key never appears.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { REMOTE_TABS, renderTabBar } from '../../static/remote/remote-tabs.js';
import {
	formatAge,
	renderDeckPicker,
	renderRequestsTab,
	requestLabel,
	shouldPollRequests
} from '../../static/remote/requests-tab-render.js';

function openState(overrides = {}) {
	return {
		session: { open: true, catalog_playlist_id: 'P1', free_text_allowed: true },
		nowMs: Date.parse('2026-11-09T20:05:00.000Z'),
		requests: [
			{
				id: 'req-1',
				kind: 'track',
				stable_id: 'trk-1',
				title: 'Song One',
				artist: 'Artist A',
				text: null,
				requester_name: 'Sam',
				votes: 2,
				created_at: '2026-11-09T20:00:00.000Z',
				requester_key: 'should-never-render'
			},
			{
				id: 'req-2',
				kind: 'text',
				stable_id: null,
				title: null,
				artist: null,
				text: 'play something funky',
				requester_name: 'Kim',
				votes: 0,
				created_at: '2026-11-09T20:04:30.000Z'
			}
		],
		inlineReason: null,
		...overrides
	};
}

test('the phone remote tab list registers Requests', () => {
	const tab = REMOTE_TABS.find((row) => row.id === 'requests');
	assert.ok(tab);
	assert.equal(tab.label, 'Requests');
	const bar = renderTabBar('requests', { requests: 2 });
	assert.match(bar, /data-tab="requests"/);
	assert.match(bar, />Requests/);
	assert.match(bar, /aria-current="page"/);
	assert.match(bar, /tab-badge/);
	assert.match(bar, />2</);
});

test('the tab lists requester names, votes, age, dismiss, load and mark loaded', () => {
	const html = renderRequestsTab(openState());
	assert.match(html, /data-requests-tab/);
	assert.match(html, />Sam</);
	assert.match(html, />Kim</);
	assert.match(html, /2 votes/);
	assert.match(html, /0 votes/);
	assert.match(html, /5m ago/);
	assert.match(html, /30s ago/);
	assert.match(html, /data-dismiss/);
	assert.match(html, /data-loaded/);
	assert.match(html, /data-load/);
	assert.match(html, />Dismiss</);
	assert.match(html, />Mark loaded</);
	assert.match(html, />Load</);
	assert.match(html, /Song One - Artist A/);
	assert.match(html, /play something funky/);
	assert.equal(html.includes('should-never-render'), false);
	assert.equal(html.includes('requester_key'), false);
});

test('session controls follow open and closed state', () => {
	const openHtml = renderRequestsTab(openState());
	assert.match(openHtml, /Open for requests/);
	assert.match(openHtml, /data-session-open[^>]*disabled/);
	assert.match(openHtml, /data-session-close/);
	assert.doesNotMatch(openHtml, /data-session-close[^>]*disabled/);

	const closedHtml = renderRequestsTab(openState({ session: { open: false }, requests: [] }));
	assert.match(closedHtml, /Not taking requests/);
	assert.match(closedHtml, /No new requests/);
	assert.match(closedHtml, /data-session-close[^>]*disabled/);
	assert.doesNotMatch(closedHtml, /data-session-open[^>]*disabled/);
});

test('requestLabel uses text or title and artist and never the key', () => {
	assert.equal(requestLabel({ kind: 'text', text: 'hello' }), 'hello');
	assert.equal(requestLabel({ kind: 'track', title: 'A', artist: 'B' }), 'A - B');
	assert.equal(requestLabel({ kind: 'track', stable_id: 'trk-9' }), 'trk-9');
});

test('formatAge and polling follow the visible Requests tab', () => {
	assert.equal(
		formatAge('2026-11-09T20:00:00.000Z', Date.parse('2026-11-09T20:05:00.000Z')),
		'5m ago'
	);
	assert.equal(shouldPollRequests('requests', false), true);
	assert.equal(shouldPollRequests('requests', true), false);
	assert.equal(shouldPollRequests('decks', false), false);
	const picker = renderDeckPicker('req-1');
	assert.match(picker, /data-deck-picker/);
	assert.match(picker, /data-load-deck="2"/);
	assert.match(picker, />Deck 2</);
});
