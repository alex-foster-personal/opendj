/**
 * pin 4860c440: "comment pins should WORK everywhere and be conditional to
 * which page you're on (pin knows)". The global pin layer drew every pin on
 * every route, so a pin placed on /performance floated over an unrelated page
 * at coordinates that mean nothing there.
 *
 * - if a pin placed on another route is drawn then it points at the wrong UI
 *   -> broken
 * - if a trailing slash on either side makes a pin vanish from its own page
 *   (the router ignores trailing slashes) -> broken
 * - if a pin with no recorded page is hidden everywhere then feedback is lost
 *   silently -> broken (the overshoot)
 * - if `/performance` matches `/performance-old` by prefix -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let board;
before(async () => {
	board = await loadTypeScriptModule('src/lib/rb/feedback-pin-board.ts');
});

test('a pin is drawn only on the route it was placed on', () => {
	const pin = { id: 'a', page: '/performance' };
	assert.equal(board.isPinOnPage(pin, '/performance'), true);
	assert.equal(board.isPinOnPage(pin, '/'), false);
	assert.equal(board.isPinOnPage(pin, '/library'), false);
	assert.equal(board.isPinOnPage({ id: 'b', page: '/' }, '/performance'), false);
	assert.equal(board.isPinOnPage({ id: 'b', page: '/' }, '/'), true);
});

test('route comparison is exact, not a prefix or substring match', () => {
	const pin = { id: 'a', page: '/performance' };
	assert.equal(board.isPinOnPage(pin, '/performance-old'), false);
	assert.equal(board.isPinOnPage(pin, '/performance/sub'), false);
	assert.equal(board.isPinOnPage({ id: 'c', page: '/performance/sub' }, '/performance'), false);
});

test('a trailing slash on either side does not hide a pin from its own page', () => {
	assert.equal(board.isPinOnPage({ id: 'a', page: '/performance/' }, '/performance'), true);
	assert.equal(board.isPinOnPage({ id: 'a', page: '/performance' }, '/performance/'), true);
});

test('a pin with no recorded page is drawn everywhere rather than lost', () => {
	assert.equal(board.isPinOnPage({ id: 'a' }, '/performance'), true);
	assert.equal(board.isPinOnPage({ id: 'a', page: '' }, '/'), true);
});

test('the global pin layer filters its pins to the current route', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/FeedbackPinLayer.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		src,
		/const pagePins = \$derived\([\s\S]*?if \(!isPinOnPage\(p, pathname\)\) return false;[\s\S]*?\);/,
		'pagePins must drop pins placed on another route'
	);
});
