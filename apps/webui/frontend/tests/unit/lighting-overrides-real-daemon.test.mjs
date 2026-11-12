/**
 * Real-path contract test for GET/POST /api/v1/lighting/overrides (PRRT_kwDOSEvNd861MduS).
 *
 * Runs the production overrides client against the production daemon app
 * (fixtures/real-daemon.mjs). This is the check the synthesized `fakeServer` double in
 * lighting-overrides-client.test.mjs answers to. Until the daemon mounts the route
 * (#9894 / #9898), the test reports UNAVAILABLE and proves the route is absent. Once the
 * route lands, the full contract runs with no edit needed.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { startRealDaemon } from './fixtures/real-daemon.mjs';

const UNAVAILABLE_MSG =
	'UNAVAILABLE: the daemon does not mount GET/POST /api/v1/lighting/overrides yet (backend dependency #9894/#9898); the overrides client is unchecked against the real endpoint until it lands';

const BACKFILL_PROGRESS_PATH = '/api/v1/analysis/backfill/progress';

let daemon;
let mod;

before(async () => {
	daemon = await startRealDaemon();
	mod = await loadTypeScriptModule('src/lib/lighting/overrides-client.ts');
});

after(async () => {
	if (daemon) await daemon.stop();
});

async function overridesRoute() {
	const res = await fetch(`${daemon.apiBase}/openapi.json`);
	assert.equal(res.status, 200, 'if the daemon does not serve openapi.json then broken');
	const doc = await res.json();
	assert.ok(
		Object.keys(doc.paths ?? {}).length > 0,
		'if openapi has no paths then a broken schema could pass as route absent'
	);
	assert.ok(
		doc.paths[BACKFILL_PROGRESS_PATH],
		'if the known backfill path is missing then openapi is not a trustworthy positive control'
	);
	return doc.paths[mod.OVERRIDES_PATH];
}

async function reportUnavailable(t) {
	t.diagnostic(UNAVAILABLE_MSG);
	await assert.rejects(mod.getOverrides(fetch, daemon.apiBase), (e) => {
		assert.ok(
			e instanceof mod.LightingOverridesHttpError,
			'if getOverrides does not throw LightingOverridesHttpError then broken'
		);
		assert.equal(e.status, 404, 'if the missing route does not 404 then broken');
		return true;
	});
}

test('the daemon serves the overrides contract the panel and the CLI share, or reports UNAVAILABLE', async (t) => {
	const route = await overridesRoute();
	if (!route) {
		await reportUnavailable(t);
		return;
	}
	assert.ok(
		route.get,
		'if the daemon mounts only POST then the panel cannot read state, so broken'
	);
	assert.ok(route.post, 'if the daemon mounts only GET then the panel cannot write, so broken');

	const rawRes = await fetch(`${daemon.apiBase}${mod.OVERRIDES_PATH}`);
	assert.equal(rawRes.status, 200, 'if GET overrides is not 200 when mounted then broken');
	const raw = await rawRes.json();
	assert.deepEqual(
		Object.keys(raw).sort(),
		[...mod.OVERRIDE_NAMES].sort(),
		"if the daemon's body has a different key set than the client assumes then the fake is wrong, so broken"
	);

	const initial = await mod.getOverrides(fetch, daemon.apiBase);
	try {
		const posted = await mod.postOverrides(fetch, daemon.apiBase, { dimmer: 0.5, mood: 'low' });
		assert.equal(posted.dimmer, 0.5, 'if POST does not apply dimmer then broken');
		assert.equal(posted.mood, 'low', 'if POST does not apply mood then broken');
		assert.deepEqual(
			await mod.getOverrides(fetch, daemon.apiBase),
			posted,
			'if GET does not reflect POST then broken'
		);
		await assert.rejects(mod.postOverrides(fetch, daemon.apiBase, { mood: 'loud' }), (e) => {
			assert.ok(
				e instanceof mod.LightingOverridesHttpError,
				'if invalid mood is not an HttpError then broken'
			);
			assert.equal(e.status, 422, 'if invalid mood is not 422 then broken');
			assert.notEqual(e.detail, undefined, 'if 422 drops detail then broken');
			return true;
		});
	} finally {
		await mod.postOverrides(fetch, daemon.apiBase, initial);
	}
});

function realTimerClock() {
	return {
		setTimeout(fn, ms) {
			return setTimeout(fn, ms);
		},
		clearTimeout(id) {
			clearTimeout(id);
		}
	};
}

function alternateMood(current) {
	if (current === 'low') return 'mid';
	if (current === 'mid') return 'high';
	if (current === 'high') return 'auto';
	return 'low';
}

async function waitForMood(states, target, deadlineMs) {
	while (Date.now() < deadlineMs) {
		const latest = states.at(-1);
		if (latest?.mood === target) return true;
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	return false;
}

test('a change made outside the panel reaches the poller within 1 s on the real daemon', async (t) => {
	const route = await overridesRoute();
	if (!route) {
		await reportUnavailable(t);
		return;
	}

	const initial = await mod.getOverrides(fetch, daemon.apiBase);
	const newMood = alternateMood(initial.mood);
	const states = [];
	const errors = [];
	const poller = mod.createOverridesPoller({
		fetchFn: fetch,
		baseUrl: daemon.apiBase,
		intervalMs: mod.OVERRIDES_POLL_MS,
		clock: realTimerClock(),
		onState: (s) => states.push(s),
		onError: (e) => errors.push(e)
	});
	try {
		poller.start();
		const firstDeadline = Date.now() + 5000;
		while (states.length === 0 && Date.now() < firstDeadline) {
			await new Promise((resolve) => setTimeout(resolve, 25));
		}
		assert.ok(states.length > 0, 'if the poller never delivers initial state then broken');

		const res = await fetch(`${daemon.apiBase}${mod.OVERRIDES_PATH}`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ mood: newMood })
		});
		assert.equal(res.status, 200, 'if the CLI-style POST fails then broken');
		const changedAt = Date.now();
		const saw = await waitForMood(states, newMood, changedAt + 1000);
		assert.ok(saw, 'if a CLI change is not shown within 1 s then broken');
		assert.equal(errors.length, 0, 'if polling errors during the wait then broken');
	} finally {
		poller.stop();
		await mod.postOverrides(fetch, daemon.apiBase, initial);
	}
});
