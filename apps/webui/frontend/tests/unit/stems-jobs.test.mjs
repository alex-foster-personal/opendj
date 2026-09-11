/**
 * The stems-separation UI surface: the TopBar bar's arithmetic, the install
 * prompt's numbers, and the two calls behind both.
 *
 * Runes modules are exercised by execution; the two `.svelte` files are
 * pinned by reading their source, which is the harness this repo has (node
 * --test, no DOM, components cannot be mounted).
 *
 * Regression lines:
 *  - if stemsProgress counts terminal jobs then the bar never goes away
 *  - if a queued job is skipped then the bar jumps backwards when it starts
 *  - if the bar's title loses its numbers then a live readout has no
 *    explanation, against the house rule
 *  - if enqueue stops sending scope=pending then the install prompt freezes
 *    the track set at click time and misses whatever the scan found after
 *  - if attach() stops reference counting then closing the JOBS drawer
 *    detaches the bus under the TopBar and the bar freezes
 *  - if StemsProgress stops gating on jobsRefusal then a legacy daemon shows
 *    a bar for jobs it cannot run
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://stems.example.test';
const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;
let originalFetch;

function job(overrides = {}) {
	return {
		id: 'job-1',
		kind: 'stems.separate',
		payload: {},
		status: 'running',
		progress: 0.5,
		message: null,
		error: null,
		attempt: 1,
		created_at: '2026-08-19T10:00:00.000Z',
		started_at: '2026-08-19T10:00:01.000Z',
		finished_at: null,
		owner_pid: 4242,
		owner_boot_id: 'boot-1',
		worker_pid: null,
		worker_pgid: null,
		worker_argv: null,
		worker_started_at: null,
		external_ref: null,
		...overrides
	};
}

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function engineHealth() {
	return {
		status: 'ok',
		state_db: {
			path: 'data/state/state.db',
			tracks: 1,
			playlists: 1,
			pairings: 0,
			last_writer_hostname: null,
			last_writer_at: null
		},
		cloud: { lock_holder: null },
		syncthing: null,
		bind_host: '127.0.0.1',
		version: '0.1.0',
		contract_rev: 'sha256:2f6c',
		engine_version: '0.1.0',
		boot_id: 'boot-1'
	};
}

function plan(overrides = {}) {
	return {
		tier: 'M',
		tier_name: 'optimal',
		total: 1000,
		pending: 120,
		ready: 800,
		unavailable: 80,
		estimate_seconds: 600,
		estimate_usd: 0.64,
		...overrides
	};
}

function makeFakeBus() {
	const topicListeners = new Map();
	const resyncListeners = new Set();
	return {
		bus: {
			subscribe(topic, listener) {
				let bucket = topicListeners.get(topic);
				if (bucket === undefined) {
					bucket = new Set();
					topicListeners.set(topic, bucket);
				}
				bucket.add(listener);
				return () => bucket.delete(listener);
			},
			subscribeResync(listener) {
				resyncListeners.add(listener);
				return () => resyncListeners.delete(listener);
			}
		},
		topicCount(topic) {
			return topicListeners.get(topic)?.size ?? 0;
		}
	};
}

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/stems-jobs-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => jsonResponse(engineHealth());
	assert.equal(await mod.capabilities.probe(), 'engine');
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	mod.jobsStore.detach();
	mod.jobsStore.jobs = [];
	globalThis.fetch = async () => jsonResponse(engineHealth());
});

// ---------------------------------------------------------------- the bar

test('no stems job means no bar at all', () => {
	const state = mod.stemsProgress([]);
	assert.equal(state.active, false);
	assert.equal(state.progress, 0);
});

test('jobs of other kinds are not counted as stems work', () => {
	const state = mod.stemsProgress([job({ kind: 'analysis.beatgrid', status: 'running' })]);
	assert.equal(state.active, false);
	assert.equal(state.running, 0);
});

test('a finished stems job retires the bar rather than pinning it at 100%', () => {
	const state = mod.stemsProgress([job({ status: 'succeeded', progress: 1 })]);
	assert.equal(state.active, false);
	assert.equal(state.succeeded, 1);
});

test('two active jobs average, and a queued one counts as zero not as absent', () => {
	// Skipping the queued job would read 80%; when it starts the bar would
	// drop to 40% and look like it went backwards.
	const state = mod.stemsProgress([
		job({ id: 'a', status: 'running', progress: 0.8 }),
		job({ id: 'b', status: 'queued', progress: 0 })
	]);
	assert.equal(state.active, true);
	assert.equal(state.progress, 0.4);
	assert.equal(state.running, 1);
	assert.equal(state.queued, 1);
});

test('a non-finite or out-of-range progress is clamped, never rendered raw', () => {
	const state = mod.stemsProgress([
		job({ id: 'a', progress: Number.NaN }),
		job({ id: 'b', progress: 12 })
	]);
	assert.equal(state.progress, 0.5);
});

test('the newest active message is surfaced for the hover title', () => {
	const state = mod.stemsProgress([job({ message: '3/10 stems separated' })]);
	assert.equal(state.message, '3/10 stems separated');
});

test('the bar title names the numbers and says the work costs money', () => {
	const jobs = [
		job({ id: 'a', status: 'running', progress: 0.5, message: '5/10 stems separated' }),
		job({ id: 'b', status: 'queued', progress: 0 })
	];
	const title = mod.stemsProgressTitle(mod.stemsProgress(jobs), jobs);
	assert.match(title, /25%/);
	assert.match(title, /1 running/);
	assert.match(title, /1 queued/);
	assert.match(title, /5\/10 stems separated/);
	assert.match(title, /costs real money/i);
});

test('local stems jobs say generating locally in the ribbon title', () => {
	const jobs = [
		job({
			status: 'running',
			progress: 0.4,
			payload: { tier: 'LOCAL', executor: 'local', stable_ids: ['abc'] }
		})
	];
	const title = mod.stemsProgressTitle(mod.stemsProgress(jobs), jobs);
	assert.match(title, /Generating stems locally/i);
	assert.match(title, /On-device separation/i);
});

test('isLocalStemsJob detects executor field and worker argv', () => {
	assert.ok(
		mod.isLocalStemsJob(
			job({ payload: { executor: 'local', tier: 'M' }, worker_argv: null })
		)
	);
	assert.ok(
		mod.isLocalStemsJob(
			job({
				payload: { tier: 'M' },
				worker_argv: ['uv', 'run', 'python', 'scripts/stems_local_worker.py']
			})
		)
	);
	assert.equal(mod.isLocalStemsJob(job({ payload: { tier: 'M' } })), false);
});

test('an idle title still explains a past failure instead of going silent', () => {
	const title = mod.stemsProgressTitle(mod.stemsProgress([job({ status: 'failed' })]));
	assert.match(title, /1 failed/);
	assert.match(title, /JOBS/);
});

// ------------------------------------------------------------- the prompt

test('the plan summary names every bucket, so the denominator is honest', () => {
	const summary = mod.stemsPlanSummary(plan());
	assert.match(summary, /120 of 1000/);
	assert.match(summary, /800 already done/);
	assert.match(summary, /80 have no audio/);
	assert.match(summary, /\$0\.64/);
	assert.match(summary, /10 min/);
});

test('a sub-cent estimate says so rather than rounding to $0.00', () => {
	const summary = mod.stemsPlanSummary(plan({ estimate_usd: 0.004 }));
	assert.match(summary, /under \$0\.01/);
	assert.doesNotMatch(summary, /\$0\.00/);
});

test('fetchStemsPlan asks the engine for the tier it was given', async () => {
	let seen = null;
	globalThis.fetch = async (request) => {
		seen = typeof request === 'string' ? request : request.url;
		return jsonResponse(plan({ tier: 'L' }));
	};
	const result = await mod.fetchStemsPlan('L');
	assert.match(seen, /\/api\/v1\/stems\/plan\?tier=L$/);
	assert.equal(result.tier, 'L');
});

// ------------------------------------------------------------- the enqueue

test('enqueueStemsForPending sends a scope, never a frozen list of ids', async () => {
	let body = null;
	globalThis.fetch = async (request) => {
		body = JSON.parse(await request.text());
		return jsonResponse(job({ id: 'job-new', status: 'queued', progress: 0 }), 201);
	};
	const created = await mod.enqueueStemsForPending('M');
	assert.equal(body.kind, 'stems.separate');
	assert.deepEqual(body.payload, { scope: 'pending', tier: 'M' });
	assert.equal(created.id, 'job-new');
	// The new row is in the store immediately, so the bar appears on click
	// rather than on the next frame from the bus.
	assert.equal(mod.jobsStore.jobs.some((row) => row.id === 'job-new'), true);
});

test('enqueueStemsForTracks refuses an empty selection instead of enqueuing a no-op', async () => {
	await assert.rejects(() => mod.enqueueStemsForTracks([]), /at least one stable id/);
});

test('enqueueStemsForTracks sends the ids it was given', async () => {
	let body = null;
	globalThis.fetch = async (request) => {
		body = JSON.parse(await request.text());
		return jsonResponse(job({ id: 'job-ids' }), 201);
	};
	await mod.enqueueStemsForTracks(['a'.repeat(40)], 'S');
	assert.deepEqual(body.payload, { stable_ids: ['a'.repeat(40)], tier: 'S' });
});

// --------------------------------------------------------- the shared bus

test('two holders share one subscription and the first release does not tear it down', () => {
	const fake = makeFakeBus();
	globalThis.fetch = async () => jsonResponse([]);
	const releaseBar = mod.jobsStore.attach(fake.bus);
	const releaseDrawer = mod.jobsStore.attach(fake.bus);
	assert.equal(fake.topicCount('jobs.updated'), 1, 'attach must not double-subscribe');

	releaseDrawer();
	assert.equal(
		fake.topicCount('jobs.updated'),
		1,
		'closing the drawer must not detach the bus under the TopBar bar'
	);

	releaseBar();
	assert.equal(fake.topicCount('jobs.updated'), 0, 'the last holder tears it down');
});

test('a holder releasing twice does not drop another holder subscription', () => {
	const fake = makeFakeBus();
	globalThis.fetch = async () => jsonResponse([]);
	const releaseBar = mod.jobsStore.attach(fake.bus);
	const releaseDrawer = mod.jobsStore.attach(fake.bus);
	releaseDrawer();
	releaseDrawer();
	assert.equal(fake.topicCount('jobs.updated'), 1);
	releaseBar();
	assert.equal(fake.topicCount('jobs.updated'), 0);
});

// ------------------------------------------------------------- the markup

test('the TopBar bar is gated on the daemon actually offering jobs', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/StemsProgress.svelte`, 'utf8');
	assert.match(source, /jobsRefusal\(\)/);
	assert.match(source, /refusal === null && state\.active/);
});

test('every numeric readout in the bar carries a hover title', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/StemsProgress.svelte`, 'utf8');
	assert.match(source, /role="progressbar"/);
	assert.match(source, /aria-valuenow=\{pct\}/);
	// the percentage span and the bar itself both explain themselves
	assert.equal((source.match(/\{title\}/g) ?? []).length >= 3, true);
});

test('the bar holds the jobs subscription itself rather than relying on the drawer', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/StemsProgress.svelte`, 'utf8');
	assert.match(source, /jobsStore\.attach\(\)/);
});

test('TopBar mounts the stems bar', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/TopBar.svelte`, 'utf8');
	assert.match(source, /import StemsProgress from '\.\/StemsProgress\.svelte'/);
	assert.match(source, /<StemsProgress \/>/);
});

test('the install prompt refuses to ask until it can state the cost', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/StemsPrompt.svelte`, 'utf8');
	// no plan -> no button. The accept button lives inside the branch that
	// has a plan, so a tester is never asked to spend an unknown amount.
	assert.match(source, /\{:else if plan === null\}/);
	assert.match(source, /stemsPlanSummary\(plan\)/);
	assert.match(source, /enqueueStemsForPending/);
});

test('the install prompt exposes the props a wizard step needs to mount it', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/StemsPrompt.svelte`, 'utf8');
	for (const prop of ['tier?', 'onenqueued?', 'onskip?']) {
		assert.ok(source.includes(prop), `StemsPrompt must accept ${prop}`);
	}
});
