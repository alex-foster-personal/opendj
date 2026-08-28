/**
 * JobsDrawer.svelte contract.
 *
 * SOURCE-SHAPE ON PURPOSE, and only where unavoidable. This harness is
 * node:test + esbuild with no component mount infra (no jsdom, no
 * @testing-library), so a .svelte file cannot be rendered and queried here.
 * Every behaviour that CAN be tested by execution was pushed into
 * jobs-store.svelte.ts and is covered by jobs-store.test.mjs; what is left is
 * markup-only facts, and the established precedent for pinning those is
 * inert-controls.test.mjs, which greps the .svelte sources the same way.
 *
 * Regression lines:
 * - if the empty state stops saying "no jobs yet" then the drawer has started
 *   filling silence with something that is not a real job
 * - if a numeric readout loses its title then the house rule that every number
 *   explains itself has been broken
 * - if an action button stops sourcing its title from the refusal helpers then
 *   a disabled button no longer says why the engine refuses it
 * - if either file grows a raw fetch( then the typed-client rule is broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const DRAWER = fileURLToPath(
	new URL('../../src/lib/components/rb/JobsDrawer.svelte', import.meta.url)
);
const STORE = fileURLToPath(new URL('../../src/lib/rb/jobs-store.svelte.ts', import.meta.url));
const TOPBAR = fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url));

const drawer = readFileSync(DRAWER, 'utf8');
const store = readFileSync(STORE, 'utf8');
const topbar = readFileSync(TOPBAR, 'utf8');

/** The character class matters: a plain 'fetch(' also matches prefetch(. */
const RAW_FETCH = /(^|[^A-Za-z0-9_])fetch\(/;

test('an empty list says so instead of rendering a placeholder job', () => {
	assert.match(drawer, /no jobs yet/);
});

test('neither the drawer nor the store hand-rolls a fetch', () => {
	assert.equal(RAW_FETCH.test(drawer), false, 'JobsDrawer.svelte must go through the store');
	assert.equal(RAW_FETCH.test(store), false, 'jobs-store must go through the generated client');
});

test('the store calls the engine through the generated typed client', () => {
	assert.match(store, /from '\.\.\/api\/client'/);
	for (const path of [
		"api.GET('/api/v1/jobs'",
		"api.POST('/api/v1/jobs/{job_id}/cancel'",
		"api.POST('/api/v1/jobs/{job_id}/reenqueue'"
	]) {
		assert.ok(store.includes(path), `expected the store to call ${path}`);
	}
});

test('the store subscribes to the jobs.updated topic and to resync', () => {
	assert.match(store, /TOPIC_JOBS_UPDATED/);
	assert.match(store, /subscribeResync/);
	assert.match(store, /from '\.\.\/api\/events-bus'/);
});

test('every numeric readout in the drawer carries a title', () => {
	// The count, the percent label and the bar itself are the three numbers on
	// screen. Each is asserted by the title it must carry.
	assert.match(drawer, /class="jobs-count"[\s\S]{0,200}?title="Number of job rows/);
	assert.match(drawer, /class="jobs-pct" title=\{progressTitle\(job\)\}/);
	assert.match(drawer, /class="jobs-bar"[\s\S]{0,400}?title=\{progressTitle\(job\)\}/);
	// progressTitle spells out both the percent and the raw 0..1 server value.
	assert.match(drawer, /progressPct\(job\.progress\)\}% complete/);
	assert.match(drawer, /on a 0\.\.1 scale/);
});

test('the progress bar is driven by the clamped helper, not raw arithmetic', () => {
	assert.match(drawer, /width: \$\{progressPct\(job\.progress\)\}%/);
	assert.equal(
		/job\.progress\s*\*\s*100/.test(drawer),
		false,
		'clamping must not be re-implemented in the template'
	);
});

test('a disabled action button explains the engine refusal in its title', () => {
	assert.match(drawer, /title=\{cancelRefusal\(job\) \?\?/);
	assert.match(drawer, /title=\{reenqueueRefusal\(job\) \?\?/);
	assert.match(drawer, /disabled=\{!canCancel\(job\) \|\| jobsStore\.busyId === job\.id\}/);
	assert.match(drawer, /disabled=\{!canReenqueue\(job\) \|\| jobsStore\.busyId === job\.id\}/);
});

test('a refused action prints the server message next to its own row', () => {
	assert.match(drawer, /jobsStore\.actionError\.id === job\.id/);
	assert.match(drawer, /\{jobsStore\.actionError\.message\}/);
});

test('a failed job shows its error tail, with the whole error in the title', () => {
	assert.match(drawer, /class="jobs-tail" title=\{job\.error\}>\{errorTail\(job\.error\)\}/);
});

test('the drawer never imports the fake-progress store', () => {
	assert.equal(
		drawer.includes('job-progress'),
		false,
		'the engine jobs drawer must not borrow the simulated per-track bar'
	);
});

test('TopBar mounts the drawer behind exactly one toggle', () => {
	assert.match(topbar, /import JobsDrawer from '\$lib\/components\/rb\/JobsDrawer\.svelte'/);
	assert.match(topbar, /import \{ jobsStore, toggleJobsDrawer \} from '\$lib\/rb\/jobs-store\.svelte'/);
	assert.match(topbar, /onclick=\{toggleJobsDrawer\}/);
	assert.match(topbar, /aria-expanded=\{jobsStore\.drawerOpen\}/);
	assert.equal(topbar.match(/<JobsDrawer \/>/g)?.length, 1, 'mounted exactly once');
	assert.equal(
		topbar.match(/toggleJobsDrawer/g)?.length,
		2,
		'one import plus one call site, no second toggle'
	);
});
