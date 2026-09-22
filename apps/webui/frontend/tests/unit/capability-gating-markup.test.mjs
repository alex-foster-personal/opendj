/**
 * The markup half of daemon capability gating.
 *
 * SOURCE-SHAPE ON PURPOSE, and only where unavoidable: this harness is
 * node:test + esbuild with no component mount infra, so a .svelte file cannot
 * be rendered and queried. Everything that CAN be tested by execution lives in
 * daemon-capabilities.test.mjs (the probe, and the two request gates that
 * matter). What is left is markup-only facts, pinned the same way
 * inert-controls.test.mjs and jobs-drawer.test.mjs pin theirs.
 *
 * Regression lines:
 * - if the layout connects the events bus without checking capabilities then a
 *   legacy boot reconnects forever against a socket route that is not there
 * - if the layout stops probing at mount then every surface stays inert forever
 * - if /progress-tree starts its refresh timer outside the capability gate then
 *   it is back to refetching a 404 every 30 seconds
 * - if the JOBS toggle or the drawer refresh loses its refusal title then a dead
 *   control stops saying why it is dead
 * - if a capability tooltip is reworded as PARITY-TODO then two different rules
 *   (not built / not served here) have been collapsed into one
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

const layout = read('src/routes/+layout.svelte');
const progressPage = read('src/routes/progress-tree/+page.svelte');
const drawer = read('src/lib/components/rb/JobsDrawer.svelte');
const topbar = read('src/lib/components/rb/TopBar.svelte');
const capabilities = read('src/lib/api/capabilities.svelte.ts');

const PARITY_TODO = 'not implemented - see PARITY-TODO';

// -------------------------------------------------------------- one probe

test('the layout probes once at mount and nobody else polls for the flavor', () => {
	assert.match(layout, /capabilities\.probe\(\)/);
	// setInterval in the layout belongs to refreshHealth, which predates this
	// and is a different concern. The probe must not acquire one of its own.
	assert.equal(
		/setInterval\([^)]*probe/.test(layout),
		false,
		'the capability probe is a probe, not a poll'
	);
	assert.equal(
		/setInterval|setTimeout/.test(capabilities),
		false,
		'the capability module must not schedule anything'
	);
});

test('the events bus connects only when the daemon offers it', () => {
	assert.match(layout, /if \(!capabilities\.events\) return;\s*\n[\s\S]{0,200}?connectEventsBus\(\)/);
	assert.equal(
		layout.match(/connectEventsBus\(\)/g)?.length,
		1,
		'exactly one connect call site, and it is the gated one'
	);
});

// -------------------------------------------------------- progress ledger

test('the progress route loads nothing and schedules nothing while inert', () => {
	assert.match(progressPage, /const ledgerRefusal = \$derived\(progressRefusal\(\)\)/);
	// The load + refresh timer + visibilitychange listener all sit behind the
	// gate, so an engine-served boot never starts any of them.
	assert.match(
		progressPage,
		/\$effect\(\(\) => \{\s*\n\s*if \(ledgerRefusal !== null\) return;\s*\n\s*void load\(\);\s*\n\s*const intervalId = setInterval\(/
	);
	assert.equal(
		progressPage.match(/setInterval\(/g)?.length,
		1,
		'one timer, inside the gate'
	);
	assert.equal(
		progressPage.match(/addEventListener\('visibilitychange'/g)?.length,
		1,
		'one visibility refetch, inside the gate'
	);
});

test('the inert progress page explains itself and shows no ledger data', () => {
	assert.match(progressPage, /\{#if ledgerRefusal !== null\}/);
	assert.match(progressPage, /class="inert-note" title=\{ledgerRefusal\}/);
	// The error banner is for a failed fetch. Nothing was fetched here, so it
	// must not fire and claim the ledger is broken.
	assert.match(progressPage, /\{#if ledgerRefusal === null && error\}/);
});

test('the sidebar link says why Progress leads nowhere', () => {
	assert.match(layout, /title=\{ledgerRefusal \?\? '[^']*\/api\/v1\/progress[^']*'\}/);
	assert.match(layout, /class:nav-unavailable=\{ledgerRefusal !== null\}/);
});

// ------------------------------------------------------------ jobs drawer

test('the drawer attaches only when the daemon offers the jobs API', () => {
	assert.match(drawer, /const refusal = \$derived\(jobsRefusal\(\)\)/);
	assert.match(
		drawer,
		/if \(!jobsStore\.drawerOpen\) return;\s*\n\s*if \(refusal !== null\) return;\s*\n\s*return jobsStore\.attach\(\)/
	);
});

test('the inert drawer disables refresh, states the reason, and lists nothing', () => {
	assert.match(drawer, /disabled=\{jobsStore\.loading \|\| refusal !== null\}/);
	assert.match(drawer, /title=\{refusal \?\? 'Refetch the list from the engine'\}/);
	assert.match(drawer, /class="jobs-inert" title=\{refusal\}/);
	// "no jobs yet" is a claim about what the daemon said. A daemon with no
	// jobs API never said it, so the empty state must sit behind the gate.
	assert.match(drawer, /\{#if refusal !== null\}[\s\S]{0,400}?\{:else if jobsStore\.jobs\.length === 0\}/);
});

test('the TopBar toggle goes inert with the reason in its title', () => {
	assert.match(topbar, /const jobsUnavailable = \$derived\(jobsRefusal\(\)\)/);
	assert.match(topbar, /disabled=\{jobsUnavailable !== null\}/);
	assert.match(topbar, /title=\{jobsUnavailable \?\?/);
});

// ------------------------------------------------------------- the wording

test('a capability refusal is never dressed up as a PARITY-TODO stub', () => {
	// Different rules: PARITY-TODO means "not built"; these features ARE built,
	// this daemon just does not serve them. Collapsing the two would make the
	// inert-controls rule unfalsifiable.
	// The module explains the distinction in prose; what it must never do is
	// DECLARE the parity literal as one of its refusal strings.
	assert.equal(capabilities.includes(`'${PARITY_TODO}'`), false);
	assert.equal(capabilities.includes(`"${PARITY_TODO}"`), false);
	for (const [name, source] of [
		['JobsDrawer', drawer],
		['progress-tree page', progressPage]
	]) {
		assert.equal(
			/PARITY[- ]?TODO/i.test(source),
			false,
			`${name} must not borrow the PARITY-TODO wording for a capability refusal`
		);
	}
});

test('every refusal names the surface AND the daemon, not just "unavailable"', () => {
	for (const phrase of [
		'jobs API not offered by this daemon',
		'event bus not offered by this daemon',
		'daemon not identified yet'
	]) {
		assert.ok(capabilities.includes(phrase), `expected the refusal wording ${phrase}`);
	}
});
