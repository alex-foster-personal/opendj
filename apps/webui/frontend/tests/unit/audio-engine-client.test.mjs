import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/audio-engine/client.ts');
});

const RUNNING = {
	state: 'running',
	clock: 'wall',
	ws_url: 'ws://127.0.0.1:4555/api/v1/ws',
	token: 'tok/+=',
	protocol: 1,
	generation: 3,
	error: null
};

function fetchOf(body, status = 200) {
	const calls = [];
	const fn = async (url) => {
		calls.push(url);
		return { ok: status < 400, status, json: async () => body };
	};
	fn.calls = calls;
	return fn;
}

/** A socket the test plays the engine's side of. */
function fakeSocket() {
	const s = {
		sent: [],
		closed: false,
		onopen: null,
		onmessage: null,
		onclose: null,
		onerror: null,
		send(data) {
			s.sent.push(JSON.parse(data));
		},
		close() {
			s.closed = true;
		},
		engine(msg) {
			s.onmessage({ data: JSON.stringify(msg) });
		},
		drop() {
			s.onclose({});
		}
	};
	return s;
}

function client(overrides = {}) {
	let clock = 1000;
	const sockets = [];
	const timers = [];
	const c = new mod.AudioEngineClient({
		fetch: fetchOf(RUNNING),
		openSocket: (url) => {
			const s = fakeSocket();
			s.url = url;
			sockets.push(s);
			return s;
		},
		now: () => clock,
		setTimer: (fn, ms) => {
			timers.push({ fn, ms });
			return timers.length - 1;
		},
		clearTimer: (h) => {
			timers[h] = null;
		},
		...overrides
	});
	return { c, sockets, timers, tick: (ms) => (clock += ms) };
}

async function connected(overrides) {
	const t = client(overrides);
	const p = t.c.connect();
	await new Promise((r) => setImmediate(r));
	t.sockets[0].engine({ type: 'hello', protocol: 1, clock: 'wall' });
	await p;
	return t;
}

test('the origin route gives the socket URL with its token, encoded', async () => {
	const f = fetchOf(RUNNING);
	const r = await mod.resolveEngineSocket(f, 'http://127.0.0.1:8585');
	assert.deepEqual(f.calls, ['http://127.0.0.1:8585/api/v1/audio-engine']);
	assert.equal(r.url, 'ws://127.0.0.1:4555/api/v1/ws?token=tok%2F%2B%3D');
	assert.equal(r.generation, 3);
});

test('an engine that is not running is a named refusal, not a URL', async () => {
	await assert.rejects(
		mod.resolveEngineSocket(fetchOf({ ...RUNNING, state: 'unavailable', ws_url: null, token: null, error: 'no build' })),
		(e) => e.state === 'unavailable' && /no build/.test(e.message)
	);
	await assert.rejects(mod.resolveEngineSocket(fetchOf({}, 500)), (e) => e.state === 'http_error');
	await assert.rejects(mod.resolveEngineSocket(fetchOf({ ...RUNNING, protocol: 2 })), /protocol 2/);
});

test('a command resolves on its own result and rejects on a refusal', async () => {
	const { c, sockets } = await connected();
	const s = sockets[0];
	assert.equal(c.connected, true);
	const ok = c.send({ type: 'crossfader', value: 0.25 });
	const bad = c.send({ type: 'stem_mute', deck: 1, stem: 'vocals', muted: true });
	assert.deepEqual(s.sent.map((m) => m.cmd.type), ['crossfader', 'stem_mute']);
	const [idOk, idBad] = s.sent.map((m) => m.id);
	assert.notEqual(idOk, idBad);
	// Results can arrive out of order; each settles its own command.
	s.engine({ type: 'result', id: idBad, ok: false, error: { code: 'not_implemented', message: 'plan 20-04' } });
	s.engine({ type: 'result', id: idOk, ok: true });
	assert.equal((await ok).ok, true);
	await assert.rejects(bad, (e) => e instanceof mod.EngineCommandError && e.code === 'not_implemented');
});

test('a dropped socket rejects every waiting command and a reconnect re-reads the route', async () => {
	const fetch = fetchOf(RUNNING);
	const { c, sockets } = await connected({ fetch });
	const waiting = c.send({ type: 'play', deck: 1, playing: true });
	sockets[0].drop();
	await assert.rejects(waiting, (e) => e.state === 'closed');
	assert.equal(c.connected, false);
	await assert.rejects(c.send({ type: 'play', deck: 1, playing: false }), /not connected/);
	const again = c.connect();
	await new Promise((r) => setImmediate(r));
	sockets[1].engine({ type: 'hello', protocol: 1 });
	await again;
	assert.equal(fetch.calls.length, 2, 'a restarted engine has a new token, so the route is read again');
});

test('a command with no result times out rather than hanging', async () => {
	const { c, timers } = await connected();
	const p = c.send({ type: 'crossfader', value: 0.5 });
	timers.at(-1).fn();
	await assert.rejects(p, /no result for crossfader/);
});

test('a first message that is not a v1 hello fails the connect', async () => {
	const t = client();
	const p = t.c.connect();
	await new Promise((r) => setImmediate(r));
	t.sockets[0].engine({ type: 'hello', protocol: 2 });
	await assert.rejects(p, /v1 hello/);
	assert.equal(t.sockets[0].closed, true);
});

test('the playhead extrapolates from the last acknowledged state only', async () => {
	const { c, sockets, tick } = await connected();
	assert.equal(c.positionMs(1), null);
	const seen = [];
	c.onState((st) => seen.push(st.frame));
	sockets[0].engine({
		type: 'state',
		frame: 48000,
		sample_rate: 48000,
		engine_time_ns: 1e9,
		decks: [{ deck: 1, loaded: true, playing: true, position_ms: 1000, duration_ms: 1100, rate: 1.1, tempo: 1.1 }],
		mixer: { crossfader: 0.5, master_volume: 1 },
		master: { muted: false }
	});
	assert.deepEqual(seen, [48000]);
	assert.equal(c.positionMs(1), 1000);
	tick(50);
	assert.equal(c.positionMs(1), 1055);
	// Sending play or stop does not move the prediction; only state does.
	c.send({ type: 'play', deck: 1, playing: false });
	assert.equal(c.positionMs(1), 1055);
	tick(1000);
	assert.equal(c.positionMs(1), 1100, 'clamped to the track');
	const stopped = { deck: 1, loaded: true, playing: false, position_ms: 500, duration_ms: 1100, rate: 0, tempo: 1 };
	assert.equal(mod.predictPositionMs(stopped, 0, 10_000), 500);
	assert.equal(mod.predictPositionMs({ ...stopped, loaded: false }, 0, 0), 0);
});
