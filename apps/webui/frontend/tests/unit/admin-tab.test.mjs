// requirement: BACKSTAGE-6529d
// Direct-route coverage for the query-addressed /admin panels (issue #12531).
//
// Regression lines:
// - if `/admin?p=jobs` stops resolving to the jobs panel then a Backstage deep
//   link lands on the wrong page
// - if a panel id has no render branch (or two) then a query renders zero or
//   several panels instead of exactly one
// - if an unknown `p` falls back to a default then a typo silently shows a
//   different panel instead of saying so
// - if `/admin` without `p` stops being the old tabbed page then the legacy
//   view changes under every existing link
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PAGE = readFileSync(new URL('../../src/routes/admin/+page.svelte', import.meta.url), 'utf8');
const u = (q) => new URL(`http://x/admin${q}`);
/** The `{#if panel.kind === 'panel'}` block, up to the unknown branch. */
const panelBlock = () => {
	const start = PAGE.indexOf("{#if panel.kind === 'panel'}");
	const end = PAGE.indexOf("{:else if panel.kind === 'unknown'}");
	assert.ok(start >= 0 && end > start, 'panel and unknown branches exist in order');
	return PAGE.slice(start, end);
};

let tab;
before(async () => {
	tab = await loadTypeScriptModule('src/routes/admin/admin-tab.ts');
});

test('/admin?p=jobs with backstage on picks the jobs panel and nothing else', () => {
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=jobs')), { kind: 'panel', id: 'jobs' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=jobs&tab=lyrics')), { kind: 'panel', id: 'jobs' });
	assert.match(
		PAGE,
		/backstageEnabled\(\)\s*\?\s*adminPanelFromUrl\(\$page\.url\)\s*:\s*\(\{ kind: 'none' \} as const\)/
	);
	assert.match(
		panelBlock(),
		/panel\.id === 'jobs'\}\s*<section class="panel"><h3>Lyric jobs<\/h3><LyricJobs \/>/
	);
});

test('every known panel id has exactly one render branch, so a query renders one panel', () => {
	const block = panelBlock();
	for (const id of tab.ADMIN_PANEL_IDS) {
		const hits = block.match(new RegExp(`panel\\.id === '${id}'`, 'g')) ?? [];
		assert.equal(hits.length, 1, `panel ${id} render branches`);
	}
	assert.doesNotMatch(block, /\{:else\}/, 'no catch-all panel branch');
});

test('/admin?p=unknown is refused and lists the known ids, never a default panel', () => {
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=unknown')), { kind: 'unknown', id: 'unknown' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=JOBS')), { kind: 'unknown', id: 'JOBS' });
	const end = PAGE.indexOf('{:else}', PAGE.indexOf("{:else if panel.kind === 'unknown'}"));
	const unknown = PAGE.slice(PAGE.indexOf("{:else if panel.kind === 'unknown'}"), end);
	assert.match(
		unknown,
		/UNKNOWN PANEL \{panel\.id\} - known panels: \{ADMIN_PANEL_IDS\.join\(', '\)\}/
	);
});

test('/admin without p is the old tabbed page, tab selection unchanged', () => {
	assert.deepEqual(tab.adminPanelFromUrl(u('')), { kind: 'none' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=')), { kind: 'none' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?tab=lyrics')), { kind: 'none' });
	assert.equal(tab.adminTabFromUrl(u('')), 'kpi');
	assert.equal(tab.adminTabFromUrl(u('?tab=title-fix')), 'title-fix');
	assert.equal(tab.adminTabFromUrl(u('?tab=spotify')), 'spotify');
	const legacy = PAGE.slice(
		PAGE.indexOf('{:else}', PAGE.indexOf("{:else if panel.kind === 'unknown'}"))
	);
	assert.match(legacy, /^\{:else\}\s*<h2>Admin<\/h2>/);
	assert.match(legacy, /role="tablist" aria-label="admin sections"/);
});
