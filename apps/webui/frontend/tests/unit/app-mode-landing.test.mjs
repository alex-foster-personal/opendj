/**
 * PERFMODE-11: boot landing route from app_mode.last_gig_at.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let landing;

const NOW = Date.parse('2026-09-15T12:00:00.000Z');

before(async () => {
	landing = await loadTypeScriptModule('src/lib/rb/app-mode-landing.ts');
});

test('stamp 1 h ago lands on Gig', () => {
	const stamp = new Date(NOW - 60 * 60 * 1000).toISOString();
	assert.equal(landing.resolveBootLandingRoute(stamp, NOW), landing.GIG_LANDING_ROUTE);
});

test('stamp 25 h ago still lands on Gig in v1', () => {
	const stamp = new Date(NOW - 25 * 60 * 60 * 1000).toISOString();
	assert.equal(landing.resolveBootLandingRoute(stamp, NOW), landing.GIG_LANDING_ROUTE);
});

test('stamp 25 h ago lands on Library when library mode shipped', () => {
	const stamp = new Date(NOW - 25 * 60 * 60 * 1000).toISOString();
	assert.equal(
		landing.resolveBootLandingRoute(stamp, NOW, { libraryModeShipped: true }),
		landing.LIBRARY_LANDING_ROUTE
	);
});

test('missing or null stamp lands on Gig in v1', () => {
	assert.equal(landing.resolveBootLandingRoute(null, NOW), landing.GIG_LANDING_ROUTE);
	assert.equal(landing.resolveBootLandingRoute(undefined, NOW), landing.GIG_LANDING_ROUTE);
});

test('malformed stamp parses null and lands on Gig in v1', () => {
	assert.equal(landing.parseLastGigAt('not-a-date'), null);
	assert.equal(landing.resolveBootLandingRoute('not-a-date', NOW), landing.GIG_LANDING_ROUTE);
});

test('24 h boundary is exclusive', () => {
	const inside = new Date(NOW - landing.GIG_RETURN_WINDOW_MS + 1).toISOString();
	const outside = new Date(NOW - landing.GIG_RETURN_WINDOW_MS).toISOString();
	assert.equal(landing.resolveBootLandingRoute(inside, NOW), landing.GIG_LANDING_ROUTE);
	assert.equal(
		landing.resolveBootLandingRoute(outside, NOW, { libraryModeShipped: true }),
		landing.LIBRARY_LANDING_ROUTE
	);
});
