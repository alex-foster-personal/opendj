/**
 * PREPWS-01 - Prep mode (/prep) is a hub of working prep pages, not a stub.
 *
 * [if] a hub entry points at a route with no page [then] Prep links to a 404 - broken
 * [if] a hub entry is a developer-only page [then] testers land on admin views - broken
 * [if] /prep still renders the PERFMODE-14 stub paragraph [then] broken
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { PREP_HUB_ENTRIES } = await loadTypeScriptModule('src/lib/shell/prep-hub.ts');
const { SIDEBAR_NAV_LINKS } = await loadTypeScriptModule('src/lib/shell/sidebar-nav.ts');

const routesDir = fileURLToPath(new URL('../../src/routes/', import.meta.url));
const prepPage = readFileSync(`${routesDir}prep/+page.svelte`, 'utf8');

/** Whether a static href has a SvelteKit page file. */
function hasPage(href) {
	const rel = href === '/' ? '' : `${href.replace(/^\//, '')}/`;
	return existsSync(`${routesDir}${rel}+page.svelte`);
}

test('the route probe reports a known-absent route as absent (negative control)', () => {
	assert.equal(hasPage('/prep'), true);
	assert.equal(hasPage('/no-such-prep-page'), false);
});

test('the hub lists working prep pages, each with a page file and a description', () => {
	assert.ok(PREP_HUB_ENTRIES.length >= 4, 'hub should offer several prep pages');
	const hrefs = PREP_HUB_ENTRIES.map((e) => e.href);
	assert.equal(new Set(hrefs).size, hrefs.length, 'duplicate hub entries');
	for (const entry of PREP_HUB_ENTRIES) {
		assert.ok(hasPage(entry.href), `${entry.href} has no +page.svelte`);
		assert.notEqual(entry.label.trim(), '');
		assert.ok(entry.does.trim().length > 10, `${entry.href} needs a real description`);
		assert.notEqual(entry.href, '/prep', 'the hub must not link to itself');
	}
	for (const expected of ['/', '/smartlists', '/dedup', '/reconcile']) {
		assert.ok(hrefs.includes(expected), `hub is missing ${expected}`);
	}
});

test('the Analysis queue entry promises planning and watching, not running', () => {
	// /analysis-backfill enqueues and monitors a batch; BackfillQueuePanel never
	// drains it and prints the queue_cli command instead.
	const entry = PREP_HUB_ENTRIES.find((e) => e.href === '/analysis-backfill');
	assert.ok(entry, 'hub should still offer the analysis queue');
	assert.doesNotMatch(entry.does, /\band run\b|\bruns? (the )?(background )?analysis\b/i);
	assert.match(entry.does, /command line/);
});

test('the hub never links a developer-only page', () => {
	const devHrefs = SIDEBAR_NAV_LINKS.filter((l) => l.dev === true).map((l) => l.href);
	assert.ok(devHrefs.length > 0, 'sidebar should still mark some pages dev-only');
	for (const entry of PREP_HUB_ENTRIES) {
		assert.ok(!devHrefs.includes(entry.href), `${entry.href} is a developer page`);
	}
});

test('/prep renders the hub, not the PERFMODE-14 stub', () => {
	assert.match(prepPage, /PREP_HUB_ENTRIES/);
	assert.match(prepPage, /\{#each PREP_HUB_ENTRIES as entry/);
	assert.match(prepPage, /href=\{entry\.href\}/);
	assert.doesNotMatch(prepPage, /lands in PERFMODE-14/);
	assert.doesNotMatch(prepPage, /without a 404/);
	assert.doesNotMatch(prepPage, /rb-inert|disabled/);
});
