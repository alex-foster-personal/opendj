// requirement: LIBWS-04
// requirement: LIBWS-06
// [if] Play is pressed in Library mode [then] the track plays through a private context, never the Gig engine
// [if] the preview stops, ends or sits paused past the idle limit [then] its context is closed and the audio dropped
// [if] a slower decode lands after a newer request [then] it never plays
// [if] a load to deck N is asked for in Library mode [then] Gig opens with the track in deck N's URL slot and loads it
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let player;
let session;
let snapshot;
let deeplink;
let preview;

before(async () => {
	player = await loadTypeScriptModule('src/lib/library/library-preview-player.ts');
	({ session, snapshot } = await loadTypeScriptModule('tests/unit/fixtures/performance-session-restore-entry.ts'));
	deeplink = await loadTypeScriptModule('src/lib/rb/performance-deeplink.ts');
	preview = await loadTypeScriptModule('src/lib/library/library-preview.svelte.ts');
});

/** A fake AudioContext whose clock the test moves by hand. */
function fakeContext(log, durationS = 200) {
	const ctx = {
		currentTime: 0,
		destination: 'out',
		closed: false,
		sources: [],
		async decodeAudioData(bytes) {
			log.push(['decode', bytes.byteLength]);
			return { duration: durationS };
		},
		createBufferSource() {
			const s = {
				buffer: null,
				onended: null,
				started: null,
				stopped: false,
				connect: () => {},
				start(when, offset) {
					s.started = offset;
				},
				stop() {
					s.stopped = true;
				},
				disconnect: () => {}
			};
			ctx.sources.push(s);
			return s;
		},
		async resume() {},
		async close() {
			ctx.closed = true;
		}
	};
	return ctx;
}

function harness({ fetchAudio, durationS, patchContext } = {}) {
	const log = [];
	const contexts = [];
	const timers = new Map();
	let nextTimer = 1;
	const snapshots = [];
	const p = player.createLibraryPreviewPlayer({
		fetchAudio: fetchAudio ?? (async () => new ArrayBuffer(8)),
		createContext: () => {
			const c = fakeContext(log, durationS);
			patchContext?.(c);
			contexts.push(c);
			return c;
		},
		onchange: (s) => snapshots.push(s),
		setInterval: (fn) => {
			const id = nextTimer++;
			timers.set(id, { fn, kind: 'interval' });
			return id;
		},
		clearInterval: (id) => timers.delete(id),
		setTimeout: (fn) => {
			const id = nextTimer++;
			timers.set(id, { fn, kind: 'timeout' });
			return id;
		},
		clearTimeout: (id) => timers.delete(id)
	});
	const fire = (kind) => {
		for (const [id, t] of [...timers]) {
			if (t.kind !== kind) continue;
			if (kind === 'timeout') timers.delete(id);
			t.fn();
		}
	};
	return { p, contexts, snapshots, timers, fire, log };
}

test('Play decodes once into its own context and plays from the start', async () => {
	const h = harness();
	await h.p.play('t1');
	assert.equal(h.contexts.length, 1);
	const s = h.p.snapshot();
	assert.equal(s.status, 'playing');
	assert.equal(s.stable_id, 't1');
	assert.equal(s.duration_ms, 200_000);
	assert.equal(h.contexts[0].sources[0].started, 0);
	assert.equal(h.snapshots[0].status, 'loading', 'loading is published before the bytes arrive');
});

test('a strip click plays from that point, and the playhead follows the context clock', async () => {
	const h = harness();
	await h.p.play('t1', 0.5);
	assert.equal(h.contexts[0].sources[0].started, 100);
	h.contexts[0].currentTime = 2;
	h.fire('interval');
	assert.equal(h.p.snapshot().position_ms, 102_000);
});

test('pause keeps the decode, resume restarts from the paused point without a refetch', async () => {
	let fetches = 0;
	const h = harness({ fetchAudio: async () => (fetches++, new ArrayBuffer(8)) });
	await h.p.play('t1');
	h.contexts[0].currentTime = 10;
	await h.p.toggle('t1');
	assert.equal(h.p.snapshot().status, 'paused');
	assert.equal(h.p.snapshot().position_ms, 10_000);
	assert.equal(h.contexts[0].sources[0].stopped, true);
	await h.p.toggle('t1');
	assert.equal(h.p.snapshot().status, 'playing');
	assert.equal(fetches, 1, 'no second fetch');
	assert.equal(h.contexts.length, 1, 'no second context');
	assert.equal(h.contexts[0].sources[1].started, 10);
});

test('stop closes the context and drops the track', async () => {
	const h = harness();
	await h.p.play('t1');
	h.p.stop();
	assert.equal(h.contexts[0].closed, true);
	assert.equal(h.p.snapshot().stable_id, null);
	assert.equal(h.p.snapshot().status, 'idle');
	assert.equal(h.timers.size, 0, 'no tick left running');
});

test('stop while the fetch is still pending closes the context at once (Codex P2 on PR #6189)', async () => {
	// The fetch never settles: the context made in the click must still close.
	const h = harness({ fetchAudio: () => new Promise(() => {}) });
	void h.p.play('t1');
	assert.equal(h.contexts.length, 1);
	assert.equal(h.contexts[0].closed, false, 'control: open while loading');
	h.p.stop();
	assert.equal(h.contexts[0].closed, true);
	assert.equal(h.p.snapshot().status, 'idle');
});

test('stop aborts an in-flight preview fetch (Codex P2 on PR #6189)', async () => {
	let sawSignal = false;
	const h = harness({
		fetchAudio: (_id, signal) =>
			new Promise((resolve, reject) => {
				if (signal === undefined) {
					reject(new Error('expected abort signal'));
					return;
				}
				sawSignal = true;
				signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
			})
	});
	void h.p.play('t1');
	assert.equal(sawSignal, true);
	h.p.stop();
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(h.p.snapshot().status, 'idle');
	assert.equal(h.p.snapshot().error, null, 'abort must not surface as a preview error');
});

test('a natural end closes the context', async () => {
	const h = harness();
	await h.p.play('t1');
	h.contexts[0].sources[0].onended();
	assert.equal(h.contexts[0].closed, true);
	assert.equal(h.p.snapshot().status, 'idle');
	assert.equal(h.timers.size, 0);
});

test('a long pause releases the audio but keeps the playhead, so Play decodes again from there', async () => {
	const h = harness();
	await h.p.play('t1');
	h.contexts[0].currentTime = 30;
	h.p.pause();
	h.fire('timeout');
	assert.equal(h.contexts[0].closed, true, 'released after the idle limit');
	assert.equal(h.p.snapshot().status, 'paused');
	await h.p.toggle('t1');
	assert.equal(h.contexts.length, 2);
	assert.equal(h.contexts[1].sources[0].started, 30);
});

test('mutation control: a pause shorter than the idle limit keeps the context open', async () => {
	const h = harness();
	await h.p.play('t1');
	h.p.pause();
	assert.equal(h.contexts[0].closed, false);
	assert.equal(player.PREVIEW_IDLE_RELEASE_MS, 60_000);
});

test('a slower decode for an older click never plays over a newer one', async () => {
	let releaseFirst;
	const first = new Promise((resolve) => (releaseFirst = resolve));
	const h = harness({
		fetchAudio: async (id) => {
			if (id === 'slow') await first;
			return new ArrayBuffer(8);
		}
	});
	const slow = h.p.play('slow');
	await h.p.play('fast');
	releaseFirst();
	await slow;
	assert.equal(h.p.snapshot().stable_id, 'fast');
	assert.equal(h.contexts[0].sources.length, 0, 'the stale request never played');
	assert.equal(h.contexts[0].closed, true, 'and its context was closed');
	assert.equal(h.contexts[1].closed, false);
});

test('a failed fetch reports the reason and leaves no context', async () => {
	const h = harness({
		fetchAudio: async () => {
			throw new Error('404 audio file not found');
		}
	});
	await h.p.play('t1');
	assert.equal(h.p.snapshot().status, 'error');
	assert.match(h.p.snapshot().error, /preview failed: 404 audio file not found/);
	assert.ok(h.contexts.every((c) => c.closed), 'no context left open');
});

test('a browser refusing the context ends in error, not stuck loading (Codex P2 on PR #6189)', async () => {
	const h = harness({
		patchContext: () => {
			throw new Error('AudioContext not allowed');
		}
	});
	await h.p.play('t1');
	assert.equal(h.p.snapshot().status, 'error');
	assert.match(h.p.snapshot().error, /preview failed: AudioContext not allowed/);
});

test('a resume that throws at once ends in error and closes that context (Codex P2 on PR #6189)', async () => {
	const h = harness({
		patchContext: (c) => {
			c.resume = () => {
				throw new Error('resume refused');
			};
		}
	});
	await h.p.play('t1');
	assert.equal(h.p.snapshot().status, 'error');
	assert.match(h.p.snapshot().error, /resume refused/);
	assert.ok(h.contexts.every((c) => c.closed), 'no context left open');
	assert.ok(!h.log.some(([kind]) => kind === 'decode'), 'nothing decoded after the refusal');
});

test('a resume that rejects later still ends in error and closes the context', async () => {
	const h = harness({
		patchContext: (c) => {
			c.resume = async () => {
				throw new Error('resume rejected');
			};
		}
	});
	await h.p.play('t1');
	assert.equal(h.p.snapshot().status, 'error');
	assert.ok(h.contexts.every((c) => c.closed));
});

test('a refused resume of a paused preview ends in error, not a stray rejection', async () => {
	const h = harness();
	await h.p.play('t1');
	await h.p.toggle('t1');
	h.contexts[0].resume = async () => {
		throw new Error('resume refused');
	};
	await h.p.toggle('t1');
	assert.equal(h.p.snapshot().status, 'error');
	assert.ok(h.contexts[0].closed);
});

test('the context is made before the fetch, inside the click (WebKit gesture rule)', async () => {
	let contextsAtFetch = -1;
	const h = harness({
		fetchAudio: async () => {
			contextsAtFetch = h.contexts.length;
			return new ArrayBuffer(8);
		}
	});
	await h.p.play('t1');
	assert.equal(contextsAtFetch, 1);
});

test('a second click on a track still decoding joins it instead of fetching again', async () => {
	let fetches = 0;
	let release;
	const gate = new Promise((resolve) => (release = resolve));
	const h = harness({ fetchAudio: async () => (fetches++, await gate, new ArrayBuffer(8)) });
	const first = h.p.play('t1');
	const second = h.p.play('t1', 0.25);
	release();
	await Promise.all([first, second]);
	assert.equal(fetches, 1);
	assert.equal(h.contexts.length, 1);
	assert.equal(h.contexts[0].sources[0].started, 50, 'the later click picks the start point');
});

test('an idle release landing during resume decodes again instead of silently staying paused', async () => {
	const h = harness();
	await h.p.play('t1');
	h.contexts[0].currentTime = 20;
	h.p.pause();
	h.contexts[0].resume = async () => h.fire('timeout');
	await h.p.toggle('t1');
	assert.equal(h.p.snapshot().status, 'playing');
	assert.equal(h.contexts.length, 2);
	assert.equal(h.contexts[1].sources[0].started, 20);
});

test('the deck hand-off URL is the Gig deep link for that deck', () => {
	const href = preview.gigDeckLoadHref('sid/with space', 3);
	const url = new URL(href, 'http://127.0.0.1');
	assert.equal(url.pathname, '/performance');
	assert.deepEqual(deeplink.parseLv2Ids(url.search), { 3: 'sid/with space' });
});

test('a row with no local audio is refused before any fetch', () => {
	assert.equal(preview.libraryAudioRefusal({ stable_id: 'x', file_exists: true }), null);
	assert.match(
		preview.libraryAudioRefusal({ stable_id: 'x', file_exists: false }),
		/audio file missing on disk/
	);
});

function engineState() {
	const channel = { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' };
	const deck = () => ({
		stable_id: null,
		playing: false,
		position_ms: 0,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: false,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		stems: {
			available_controls: ['vocal', 'instrumental', 'drums'],
			controls: {
				vocal: { muted: false, solo: false, gain: 0.5 },
				instrumental: { muted: false, solo: false, gain: 0.5 },
				drums: { muted: false, solo: false, gain: 0.5 }
			}
		}
	});
	return {
		browser: { active_playlist: null },
		master_deck: null,
		mixer: { crossfader: 0.5, master: 0.5, channels: { 1: channel, 2: channel, 3: channel, 4: channel } },
		decks: { 1: deck(), 2: deck(), 3: deck(), 4: deck() }
	};
}

/** Mount the Gig restore as an arrival from Library mode with `search` in the URL. */
async function arriveFromLibrary(search, { fromLibrary = true, flags: initial = null } = {}) {
	const flags = new Map(initial ?? (fromLibrary ? [['mdt.library_mode_exit', '1']] : []));
	globalThis.window = {};
	globalThis.sessionStorage = {
		getItem: (k) => flags.get(k) ?? null,
		setItem: (k, v) => flags.set(k, v),
		removeItem: (k) => flags.delete(k)
	};
	const state = engineState();
	const dispatched = [];
	// A real, recent Gig session (decks 1 and 3, crossfader hard left) that
	// Library mode must NOT bring back.
	const before = engineState();
	before.decks[1].stable_id = 'old-one';
	before.decks[3].stable_id = 'old-three';
	before.mixer.crossfader = 0;
	const old = session.buildPerformanceSessionSnapshot(before, Date.now() - 1000, null);
	assert.ok(snapshot.parsePerformanceSession(old), 'control: the stored session is a valid one');
	try {
		const dispose = session.installPerformanceSessionRestore({
			location: { pathname: '/performance', search, href: `http://127.0.0.1/performance${search}` },
			storage: { getItem: () => old, setItem: () => {} },
			replaceState: () => {},
			dispatch: async (command) => {
				dispatched.push(command);
				if (command.type === 'load') state.decks[command.deck].stable_id = command.stable_id;
				return state;
			},
			query: () => state,
			document: { hidden: false, addEventListener: () => {}, removeEventListener: () => {} },
			window: { addEventListener: () => {}, removeEventListener: () => {} },
			setInterval: () => 1,
			clearInterval: () => {},
			commandSession: () => 1,
			operatorMaster: () => null
		});
		for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setImmediate(resolve));
		dispose();
	} finally {
		delete globalThis.window;
		delete globalThis.sessionStorage;
	}
	return {
		loads: dispatched.filter((c) => c.type === 'load'),
		mixer: dispatched.filter((c) => c.type === 'crossfader' || c.type === 'master_volume'),
		flagLeft: flags.has('mdt.library_mode_exit')
	};
}

test('arriving from Library with a deck in the URL loads exactly that deck', async () => {
	const out = await arriveFromLibrary('?d2=sid-picked');
	assert.deepEqual(
		out.loads.map((c) => ({ deck: c.deck, stable_id: c.stable_id })),
		[{ deck: 2, stable_id: 'sid-picked' }]
	);
	assert.deepEqual(out.mixer, [], 'the old mixer is not replayed');
	assert.equal(out.flagLeft, false, 'the Library exit flag is consumed');
});

test('control: the same arrival NOT from Library restores the old session around the URL deck', async () => {
	const out = await arriveFromLibrary('?d2=sid-picked', { fromLibrary: false });
	assert.deepEqual(
		out.loads.map((c) => c.deck).sort(),
		[1, 2, 3],
		'proves the stored session is one the restore would replay'
	);
});

test('mutation control: arriving from Library with no deck in the URL still loads nothing (PERFMODE-14)', async () => {
	const out = await arriveFromLibrary('');
	assert.deepEqual(out.loads, []);
});

test('a remount that finds only the hold is still an arrival from Library (Codex P1 on PR #6189)', async () => {
	// The first mount consumed the exit marker and was remounted before its deck
	// restore settled, so the rescue hold is all that is left.
	const withDeck = await arriveFromLibrary('?d2=sid-picked', { flags: [['mdt.library_rescue_hold', '1']] });
	assert.deepEqual(
		withDeck.loads.map((c) => ({ deck: c.deck, stable_id: c.stable_id })),
		[{ deck: 2, stable_id: 'sid-picked' }]
	);
	assert.deepEqual(withDeck.mixer, [], 'the old mixer is not replayed');
	const noDeck = await arriveFromLibrary('', { flags: [['mdt.library_rescue_hold', '1']] });
	assert.deepEqual(noDeck.loads, [], 'no deck in the URL loads nothing');
});

/** An Error carrying a DOMException-style name, as the browser throws. */
function namedError(name, message) {
	const e = new Error(message);
	e.name = name;
	return e;
}

test('a context that fails to close is reported, not swallowed (Sol P1 on PR #6189)', async () => {
	const h = harness({
		patchContext: (c) => {
			c.close = async () => {
				throw new Error('close failed');
			};
		}
	});
	await h.p.play('t1');
	h.p.stop();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(h.p.snapshot().status, 'error');
	assert.match(h.p.snapshot().error, /preview cleanup failed: close failed/);
});

test('control: closing an already closed context is the expected case and stays idle', async () => {
	const h = harness({
		patchContext: (c) => {
			c.close = async () => {
				throw namedError('InvalidStateError', 'already closed');
			};
		}
	});
	await h.p.play('t1');
	h.p.stop();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(h.p.snapshot().status, 'idle');
	assert.equal(h.p.snapshot().error, null);
});

test('a source that fails to stop is reported; one never started is not (Sol P1 on PR #6189)', async () => {
	for (const [error, expected] of [
		[new Error('stop failed'), 'error'],
		[namedError('InvalidStateError', 'not started'), 'idle']
	]) {
		const h = harness();
		await h.p.play('t1');
		for (const s of h.contexts[0].sources) {
			s.stop = () => {
				throw error;
			};
		}
		h.p.stop();
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(h.p.snapshot().status, expected, error.message);
	}
});

test('a stale cleanup failure never overwrites a newer preview', async () => {
	let closes = 0;
	const h = harness({
		patchContext: (c) => {
			c.close = async () => {
				closes += 1;
				throw new Error('close failed');
			};
		}
	});
	await h.p.play('t1');
	const from = h.snapshots.length;
	await h.p.play('t2');
	await new Promise((resolve) => setImmediate(resolve));
	assert.ok(closes >= 1, 'control: the first context was closed and failed');
	assert.deepEqual(
		h.snapshots.slice(from).filter((s) => s.status === 'error'),
		[],
		'the new preview never flashed the old cleanup error'
	);
	assert.equal(h.p.snapshot().stable_id, 't2');
	assert.equal(h.p.snapshot().status, 'playing');
});

test('a source that fails to stop on pause or seek fails the preview and closes its context (Codex P2 on PR #6189)', async () => {
	for (const act of ['pause', 'seek']) {
		const h = harness();
		await h.p.play('t1');
		for (const s of h.contexts[0].sources) {
			s.stop = () => {
				throw new Error('stop failed');
			};
		}
		if (act === 'pause') h.p.pause();
		else await h.p.play('t1', 0.5);
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(h.p.snapshot().status, 'error', act);
		assert.match(h.p.snapshot().error, /stop failed/, act);
		assert.equal(h.contexts[0].closed, true, `${act}: the context that may still sound is closed`);
	}
});

test('control: pause and seek with a source that stops cleanly keep the context', async () => {
	const h = harness();
	await h.p.play('t1');
	h.p.pause();
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(h.p.snapshot().status, 'paused');
	await h.p.play('t1', 0.5);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(h.p.snapshot().status, 'playing');
	assert.equal(h.contexts[0].closed, false);
});

test('a resume rejected while the fetch hangs fails at once and aborts the fetch (Codex P2 on PR #6189)', async () => {
	let aborted = false;
	const h = harness({
		fetchAudio: (_id, signal) =>
			new Promise((_resolve, reject) => {
				signal.addEventListener('abort', () => {
					aborted = true;
					reject(new DOMException('aborted', 'AbortError'));
				});
			}),
		patchContext: (c) => {
			c.resume = async () => {
				throw new Error('resume rejected');
			};
		}
	});
	void h.p.play('t1');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(h.p.snapshot().status, 'error');
	assert.match(h.p.snapshot().error, /resume rejected/);
	assert.equal(aborted, true, 'the hung fetch is aborted');
	assert.ok(h.contexts.every((c) => c.closed), 'the context is closed');
	assert.equal(h.snapshots.filter((s) => s.status === 'error').length, 1, 'reported once');
});

test('control: a resume that succeeds while the fetch hangs stays loading', async () => {
	let aborted = false;
	const h = harness({
		fetchAudio: (_id, signal) =>
			new Promise(() => {
				signal.addEventListener('abort', () => (aborted = true));
			})
	});
	void h.p.play('t1');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(h.p.snapshot().status, 'loading');
	assert.equal(aborted, false);
	assert.equal(h.contexts[0].closed, false);
});

test('control: a resume rejected after a newer click never fails the newer preview', async () => {
	let rejectFirst;
	let n = 0;
	const h = harness({
		patchContext: (c) => {
			if (n++ === 0) c.resume = () => new Promise((_r, rej) => (rejectFirst = rej));
		}
	});
	void h.p.play('t1');
	await h.p.play('t2');
	rejectFirst(new Error('late refusal'));
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(h.p.snapshot().stable_id, 't2');
	assert.equal(h.p.snapshot().status, 'playing');
	assert.equal(h.p.snapshot().error, null);
});
