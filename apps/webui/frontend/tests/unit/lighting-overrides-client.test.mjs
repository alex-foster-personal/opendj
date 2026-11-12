import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/lighting/overrides-client.ts');
});

const STATE = {
	blackout: false,
	dimmer: 1,
	colour: 'auto',
	strobe: 'auto',
	mood: 'auto',
	ambient: false
};

/** Manual clock: advance(ms) fires due timers in order, flushing microtasks between them. */
function fakeClock() {
	let now = 0;
	let seq = 0;
	const timers = new Map();
	const flush = async () => {
		for (let i = 0; i < 20; i++) await Promise.resolve();
	};
	return {
		setTimeout(fn, ms) {
			const id = ++seq;
			timers.set(id, { at: now + ms, fn });
			return id;
		},
		clearTimeout(id) {
			timers.delete(id);
		},
		async advance(ms) {
			const end = now + ms;
			await flush();
			for (;;) {
				const due = [...timers.entries()]
					.filter(([, t]) => t.at <= end)
					.sort((a, b) => a[1].at - b[1].at)[0];
				if (!due) break;
				timers.delete(due[0]);
				now = due[1].at;
				due[1].fn();
				await flush();
			}
			now = end;
		}
	};
}

/**
 * Synthesized in-memory double of the daemon overrides endpoint (not a recorded payload).
 * Checked against the real daemon in lighting-overrides-real-daemon.test.mjs, which reports
 * UNAVAILABLE until the backend route (#9894/#9898) lands. If the two disagree, this fake is the bug.
 */
/** Recording fetch backed by a mutable server; `respond` can override per call. */
function fakeServer(initial = STATE) {
	const server = { state: { ...initial }, calls: [], respond: null };
	server.fetch = async (url, init = {}) => {
		server.calls.push({ url, method: init.method ?? 'GET', body: init.body });
		if (server.respond) return server.respond(url, init);
		if (init.method === 'POST') Object.assign(server.state, JSON.parse(init.body));
		return { ok: true, status: 200, json: async () => ({ ...server.state }) };
	};
	return server;
}

function blackoutGate(server, clock) {
	return mod.createHoldGate({
		holdMs: mod.BLACKOUT_HOLD_MS,
		clock,
		onConfirm: () => void mod.postOverrides(server.fetch, '', { blackout: true })
	});
}

test('holding blackout 300 ms sends exactly one POST with blackout true', async () => {
	const server = fakeServer();
	const clock = fakeClock();
	const gate = blackoutGate(server, clock);
	gate.press();
	await clock.advance(300);
	assert.equal(server.calls.length, 1, 'if a 300 ms hold does not POST once then broken');
	const [call] = server.calls;
	assert.equal(call.method, 'POST');
	assert.ok(
		call.url.endsWith('/api/v1/lighting/overrides'),
		'if the POST URL is wrong then broken'
	);
	assert.deepEqual(JSON.parse(call.body), { blackout: true });
	await clock.advance(1000);
	assert.equal(server.calls.length, 1, 'if holding longer sends a second POST then broken');
});

test('a 100 ms click or a pointer leaving the button sends nothing', async () => {
	for (const end of ['release', 'cancel']) {
		const server = fakeServer();
		const clock = fakeClock();
		const gate = blackoutGate(server, clock);
		gate.press();
		await clock.advance(100);
		gate[end]();
		await clock.advance(1000);
		assert.equal(server.calls.length, 0, `if ${end} at 100 ms sends anything then broken`);
		assert.equal(gate.holding, false);
	}
});

test('299 ms of hold confirms nothing', async () => {
	const server = fakeServer();
	const clock = fakeClock();
	const gate = blackoutGate(server, clock);
	gate.press();
	await clock.advance(299);
	assert.equal(server.calls.length, 0, 'if 299 ms confirms blackout then broken');
	assert.equal(gate.holding, true);
});

test('a CLI change on the server shows in the panel within 1 s', async () => {
	assert.ok(mod.OVERRIDES_POLL_MS <= 900, 'if the poll interval cannot meet 1 s then broken');
	const server = fakeServer();
	const clock = fakeClock();
	const states = [];
	const poller = mod.createOverridesPoller({
		fetchFn: server.fetch,
		baseUrl: '',
		intervalMs: mod.OVERRIDES_POLL_MS,
		clock,
		onState: (s) => states.push(s),
		onError: (e) => assert.fail(String(e))
	});
	poller.start();
	await clock.advance(0);
	assert.equal(states.at(-1).blackout, false);
	Object.assign(server.state, { blackout: true, strobe: 'fast' });
	await clock.advance(1000);
	assert.equal(states.at(-1).blackout, true, 'if a CLI change is not shown within 1 s then broken');
	assert.equal(states.at(-1).strobe, 'fast');
	poller.stop();
});

test('a GET failure reaches onError and polling recovers', async () => {
	const server = fakeServer();
	server.respond = async () => ({
		ok: false,
		status: 404,
		json: async () => ({ detail: 'Not Found' })
	});
	const clock = fakeClock();
	const states = [];
	const errors = [];
	const poller = mod.createOverridesPoller({
		fetchFn: server.fetch,
		baseUrl: '',
		intervalMs: mod.OVERRIDES_POLL_MS,
		clock,
		onState: (s) => states.push(s),
		onError: (e) => errors.push(e)
	});
	poller.start();
	await clock.advance(0);
	assert.equal(states.length, 0, 'if a 404 produces state then broken');
	assert.ok(errors[0] instanceof mod.LightingOverridesHttpError, 'if a 404 is masked then broken');
	assert.equal(errors[0].status, 404);
	server.respond = null;
	await clock.advance(mod.OVERRIDES_POLL_MS);
	assert.equal(states.length, 1, 'if polling stops after an error then broken');
	poller.stop();
});

test('a 422 keeps the server detail', async () => {
	const detail = { allowed: [...mod.OVERRIDE_NAMES] };
	const server = fakeServer();
	server.respond = async () => ({ ok: false, status: 422, json: async () => ({ detail }) });
	await assert.rejects(mod.postOverrides(server.fetch, '', { mood: 'high' }), (err) => {
		assert.ok(err instanceof mod.LightingOverridesHttpError);
		assert.equal(err.status, 422);
		assert.deepEqual(err.detail, detail, 'if the 422 detail is dropped then broken');
		return true;
	});
});

test('parseOverrides is strict', () => {
	assert.deepEqual(mod.parseOverrides({ ...STATE, colour: '#ff0000' }), {
		...STATE,
		colour: '#ff0000'
	});
	const { ambient: _a, ...missing } = STATE;
	const bad = [
		[missing, /ambient/],
		[{ ...STATE, dimmer: 1.5 }, /dimmer/],
		[{ ...STATE, mood: 'loud' }, /mood/],
		[{ ...STATE, colour: 'red' }, /colour/],
		[{ ...STATE, strobe: 'FAST' }, /strobe/]
	];
	for (const [body, field] of bad)
		assert.throws(() => mod.parseOverrides(body), field, `if ${field} is accepted then broken`);
});

test('postOverrides refuses an empty or unknown patch before fetching', async () => {
	const server = fakeServer();
	await assert.rejects(mod.postOverrides(server.fetch, '', {}), /empty/);
	await assert.rejects(mod.postOverrides(server.fetch, '', { fog: true }), /fog/);
	assert.equal(server.calls.length, 0, 'if a bad patch reaches the server then broken');
});

test('stop() halts polling', async () => {
	const server = fakeServer();
	const clock = fakeClock();
	const poller = mod.createOverridesPoller({
		fetchFn: server.fetch,
		baseUrl: '',
		intervalMs: mod.OVERRIDES_POLL_MS,
		clock,
		onState: () => {},
		onError: () => {}
	});
	poller.start();
	await clock.advance(0);
	const n = server.calls.length;
	poller.stop();
	await clock.advance(5000);
	assert.equal(server.calls.length, n, 'if polling continues after stop then broken');
});
