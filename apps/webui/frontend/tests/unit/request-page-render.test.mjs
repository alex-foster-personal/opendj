/**
 * Crowd request page render (issue #10446, CROWDREQ-10725).
 *
 * Pure HTML from request-render.js: closed session, cooldown countdown,
 * free-text box visibility, and client-side catalog filter.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	CLOSED_MESSAGE,
	filterCatalog,
	formatCountdown,
	renderPage
} from '../../static/request/request-render.js';

function catalogRow(index, extras = {}) {
	return {
		stable_id: extras.stable_id ?? `track-${index}`,
		title: extras.title ?? `Track ${index}`,
		artist: extras.artist ?? `Artist ${index}`,
		artwork: extras.artwork ?? `/request/api/artwork/track-${index}`
	};
}

function openState(overrides = {}) {
	return {
		guestName: 'the maintainer',
		session: {
			open: true,
			catalog_name: 'Tonight',
			free_text_allowed: true,
			cooldown_s: 180
		},
		catalog: [catalogRow(1, { title: 'One', artist: 'Someone' })],
		filter: '',
		requests: [
			{
				id: 'req-1',
				kind: 'track',
				title: 'One',
				artist: 'Someone',
				text: '',
				requester_name: 'Sam',
				votes: 2
			}
		],
		votedIds: [],
		retryAfterS: null,
		inlineReason: null,
		...overrides
	};
}

test('closed session shows the closed message and no Request or vote buttons', () => {
	const html = renderPage(
		openState({
			session: {
				open: false,
				catalog_name: 'Tonight',
				free_text_allowed: true,
				cooldown_s: 180
			}
		})
	);
	assert.match(html, new RegExp(CLOSED_MESSAGE));
	assert.equal(html.includes('The DJ is not taking requests right now'), true);
	assert.doesNotMatch(html, /class="[^"]*request-track/);
	assert.doesNotMatch(html, /class="[^"]*vote-btn/);
	assert.doesNotMatch(html, />Request</);
	assert.doesNotMatch(html, />Vote</);
});

test('429 retry_after_s 90 disables every Request button with a 1:30 countdown', () => {
	assert.equal(formatCountdown(90), '1:30');
	const catalog = [
		catalogRow(1, { title: 'Alpha', artist: 'A' }),
		catalogRow(2, { title: 'Beta', artist: 'B' })
	];
	const html = renderPage(openState({ catalog, retryAfterS: 90 }));
	const buttons = [...html.matchAll(/<button\b[^>]*class="[^"]*request-track[^"]*"[^>]*>/g)];
	assert.equal(buttons.length, 2);
	for (const match of buttons) {
		assert.match(match[0], /\bdisabled\b/);
	}
	assert.equal((html.match(/1:30/g) || []).length >= 2, true);
});

test('free_text_allowed false renders no Say something box', () => {
	const html = renderPage(
		openState({
			session: {
				open: true,
				catalog_name: 'Tonight',
				free_text_allowed: false,
				cooldown_s: 180
			}
		})
	);
	assert.doesNotMatch(html, /Say something/i);
	assert.doesNotMatch(html, /<textarea\b/);
});

test('catalog filter "dua" keeps matching rows only and does not fetch', () => {
	const catalog = [];
	for (let i = 0; i < 200; i += 1) {
		if (i === 7) {
			catalog.push(catalogRow(i, { title: 'Levitating', artist: 'Dua Lipa', stable_id: 'dua-1' }));
		} else if (i === 42) {
			catalog.push(
				catalogRow(i, { title: 'Dua for the night', artist: 'Other', stable_id: 'dua-2' })
			);
		} else {
			catalog.push(catalogRow(i));
		}
	}
	assert.equal(catalog.length, 200);
	assert.equal(filterCatalog(catalog, 'dua').length, 2);

	let fetches = 0;
	const previousFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		fetches += 1;
		throw new Error('filter must not fetch');
	};
	try {
		const html = renderPage(openState({ catalog, filter: 'dua' }));
		assert.match(html, /Levitating/);
		assert.match(html, /Dua Lipa/);
		assert.match(html, /Dua for the night/);
		assert.doesNotMatch(html, /Track 1</);
		assert.doesNotMatch(html, />Artist 1</);
		assert.equal(fetches, 0);
	} finally {
		globalThis.fetch = previousFetch;
	}
});
