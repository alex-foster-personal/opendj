/**
 * KEY SYNC follows the master (DECKUX-34).
 *
 * the maintainer's invariant: current key must not be able to show incorrect
 * information about what is playing. Two defects broke it:
 *
 * 1. Session restore replays {type:'key_sync', enabled:true} right after a
 *    deck load, before any master is elected. setKeySync threw "KEY SYNC
 *    requires an elected loaded master deck" and the WHOLE deck restore
 *    failed (webui-client-errors 2026-10-05 14:29Z, "session restore deck 1
 *    failed"). Enabled-without-master is now an explicit ARMED state
 *    ('waiting-no-master') that applies as soon as a master appears.
 * 2. KEY SYNC was one-shot: the button stayed lit ("this deck follows the
 *    selected master key") after the master changed, loaded a new track or
 *    nudged its key. The follower now re-follows on every such change, and
 *    a deck that cannot follow (no master, is the master, no key) reports a
 *    waiting status instead of a lit one.
 *
 * These run the REAL engine (stretch adapter stubbed) on paused decks, so
 * every key shift lands on the immediate path with no presentation clock.
 *
 * Regression lines:
 * - if setKeySync(true) with no master throws then session restore fails the deck load
 * - if an armed follower ignores a newly elected master then it stays unsynced while armed
 * - if the master's key nudge is not re-followed then a lit KEY SYNC shows a wrong key
 * - if a master reassignment is not re-followed then a lit KEY SYNC tracks the old master
 * - if a master track swap leaves the follower 'following' a deck that is no longer master then the light lies
 * - if the follower becomes master and still reports 'following' then the light lies
 * - if a manual follower nudge is overwritten by the follow loop then the nudge is silently undone
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://key-sync-follow.example.test';
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url)
);
const TRACKS = {
	['1'.repeat(40)]: '8A',
	['2'.repeat(40)]: '3A',
	['3'.repeat(40)]: '11B',
	['4'.repeat(40)]: '5A'
};
const [SID_8A, SID_3A, SID_11B, SID_5A] = Object.keys(TRACKS);

let audio;
let originalFetch;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function makeAudioParam(value = 1) {
	return {
		value,
		setValueAtTime() {},
		setTargetAtTime() {},
		linearRampToValueAtTime() {}
	};
}

function makeNode(extra = {}) {
	return {
		connect() {},
		disconnect() {},
		gain: makeAudioParam(),
		frequency: makeAudioParam(1000),
		Q: makeAudioParam(1),
		type: 'lowpass',
		delayTime: makeAudioParam(0),
		channelCount: 2,
		channelCountMode: 'max',
		channelInterpretation: 'speakers',
		...extra
	};
}

function installBrowserAudioGlobals() {
	const localStorage = {
		getItem: () => null,
		setItem: () => {},
		removeItem: () => {}
	};
	defineGlobal('window', {
		location: { href: `${API_BASE}/performance`, search: '' },
		localStorage,
		addEventListener() {},
		removeEventListener() {}
	});
	defineGlobal('navigator', { userAgent: 'key-sync-follow-master-test' });
	defineGlobal('localStorage', localStorage);
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {
		return {
			connect() {},
			disconnect() {},
			port: { onmessage: null },
			addEventListener() {},
			removeEventListener() {}
		};
	});
	defineGlobal('Audio', function Audio() {
		return {
			pause() {},
			play: async () => {},
			srcObject: null,
			setSinkId: async () => {}
		};
	});
	defineGlobal('AudioContext', class AudioContext {
		constructor() {
			this.state = 'running';
			this.sampleRate = 48_000;
			this.currentTime = 0;
			this.baseLatency = 128 / 48_000;
			this.outputLatency = 0.032;
			this.destination = {
				maxChannelCount: 2,
				channelCount: 2,
				channelInterpretation: 'speakers',
				connect() {},
				disconnect() {}
			};
			this.audioWorklet = {
				addModule: async () => {}
			};
		}
		#node(extra = {}) {
			return { context: this, connect() {}, disconnect() {}, gain: makeAudioParam(), frequency: makeAudioParam(1000), Q: makeAudioParam(1), type: 'lowpass', delayTime: makeAudioParam(0), channelCount: 2, channelCountMode: 'max', channelInterpretation: 'speakers', ...extra };
		}
		createGain() {
			return this.#node();
		}
		createAnalyser() {
			return this.#node();
		}
		createBiquadFilter() {
			return this.#node();
		}
		createChannelSplitter() {
			return this.#node();
		}
		createChannelMerger() {
			return this.#node();
		}
		createDelay() {
			return this.#node();
		}
		createMediaStreamDestination() {
			return {
				context: this,
				stream: { getTracks: () => [] },
				connect() {},
				disconnect() {}
			};
		}
		createBuffer(channels, length, sampleRate) {
			return {
				numberOfChannels: channels,
				length,
				sampleRate,
				duration: length / sampleRate,
				getChannelData: () => new Float32Array(length)
			};
		}
		async decodeAudioData(bytes) {
			const byteLength = bytes.byteLength ?? bytes.length ?? 0;
			const sampleRate = 48_000;
			const length = Math.max(1, Math.floor(byteLength / 4));
			return {
				duration: length / sampleRate,
				length,
				numberOfChannels: 2,
				sampleRate,
				getChannelData: () => new Float32Array(length)
			};
		}
		getOutputTimestamp() {
			return { contextTime: 0, performanceTime: performance.now() };
		}
		addEventListener() {}
		removeEventListener() {}
		async resume() {}
		async close() {}
	});
}
function json(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function stubFetch() {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		const sid = Object.keys(TRACKS).find((candidate) => url.includes(candidate));
		if (sid === undefined) throw new Error(`unexpected request ${url}`);
		if (url.includes('/anlz?')) {
			const bands = { length: 0, low: [], mid: [], high: [] };
			return json({
				stable_id: sid,
				points: 38_400,
				waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
				beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
				beatgrid_source: 'rekordbox',
				cues: [],
				phrases: [],
				vocals: { status: 'not_analyzed' },
				local_waveform: { status: 'decoded' }
			});
		}
		if (url.endsWith('/hot-cues')) {
			return json(
				['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` }))
			);
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array(4096));
		if (url.endsWith(`/tracks/${sid}/stems`)) {
			return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		}
		if (url.endsWith(`/tracks/${sid}`)) {
			return json({ stable_id: sid, title: `key ${TRACKS[sid]}`, artist: 'Fixture', bpm: null, key: TRACKS[sid] });
		}
		throw new Error(`unexpected request ${url}`);
	};
}

/** Let queued follow work (microtasks + the paused immediate path) settle. */
async function settle() {
	await new Promise((resolve) => setTimeout(resolve, 0));
}

const deck = (id) => audio.getDeckState(id);

/** The target an independent derivation says the follower must sit at, from
 * the published paused-deck controls (tempo, Master Tempo, key shift). */
function effectiveSemitones(id) {
	const st = deck(id);
	return audio.composeStretchSemitones(st.pitch, st.master_tempo_enabled, st.key_shift_semitones);
}

function expectedFollowShift(follower, master) {
	return audio.deriveKeySyncTargetManualShift(
		deck(follower).key,
		deck(master).key,
		effectiveSemitones(follower),
		effectiveSemitones(master),
		deck(follower).key_shift_semitones
	);
}

function assertFollowing(follower, master, message) {
	assert.equal(audio.keySyncStatus(follower), 'following', `${message}: status`);
	assert.equal(deck(master).is_master, true, `${message}: deck ${master} is master`);
	assert.equal(
		deck(follower).key_shift_semitones,
		expectedFollowShift(follower, master),
		`${message}: follower shift must equal the derived target for the CURRENT master`
	);
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserAudioGlobals();
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
});

afterEach(async () => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	installBrowserAudioGlobals();
	await audio.engine.dispose();
});

async function loadPair() {
	stubFetch();
	await audio.engine.load(1, SID_8A);
	await audio.engine.load(2, SID_3A);
	assert.equal(deck(1).key, '8A');
	assert.equal(deck(2).key, '3A');
	assert.equal(deck(1).is_master || deck(2).is_master, false, 'paused loads elect no master');
}

async function followDeck1() {
	await loadPair();
	await audio.engine.setDeckMaster(1);
	await audio.engine.setKeySync(2, true);
	await settle();
	assertFollowing(2, 1, 'precondition');
}

test('restore order: enabling KEY SYNC before any master exists arms it instead of throwing', async () => {
	await loadPair();
	await audio.engine.setKeySync(2, true);
	assert.equal(deck(2).key_sync_enabled, true);
	assert.equal(audio.keySyncStatus(2), 'waiting-no-master');
	assert.equal(deck(2).key_shift_semitones, 0, 'armed KEY SYNC must not move the key');
	const target = expectedFollowShift(2, 1);
	assert.notEqual(target, 0, 'fixture keys must need a nudge or this test cannot bite');
	await audio.engine.setDeckMaster(1);
	await settle();
	assertFollowing(2, 1, 'armed follower after master elected');
	assert.equal(deck(2).key_shift_semitones, target);
});

test('master key nudge: the follower re-follows the master new audible key', async () => {
	await followDeck1();
	await audio.engine.load(3, SID_11B);
	const before = deck(2).key_shift_semitones;
	await audio.engine.nudgeKey(1, 1);
	await audio.engine.nudgeKey(1, 1);
	await settle();
	assert.equal(deck(1).key_shift_semitones, 2);
	assertFollowing(2, 1, 'after master nudge');
	assert.notEqual(deck(2).key_shift_semitones, before, 'a +2 master nudge must move the follower');
	assert.equal(audio.keySyncStatus(3), 'off');
	assert.equal(deck(3).key_shift_semitones, 0, 'an unarmed deck must never be moved by the follow pass');
});

test('between a master change and the follow landing, the follower reads syncing, never following', async () => {
	await followDeck1();
	// The immediate (paused) key-shift path publishes synchronously inside
	// nudgeKey; the follow pass is queued, so this is the in-flight window a
	// live deck sits in until its schedule is presented.
	const pending = audio.engine.nudgeKey(1, 1);
	assert.equal(deck(1).key_shift_semitones, 1);
	assert.notEqual(deck(2).key_shift_semitones, expectedFollowShift(2, 1), 'fixture must need a re-follow');
	assert.equal(audio.keySyncStatus(2), 'syncing', 'a lit KEY SYNC here would show a key that is not playing');
	await pending;
	await settle();
	assertFollowing(2, 1, 'after the follow lands');
});

test('master tempo with Master Tempo OFF: the follower re-follows the varispeed key change', async () => {
	await followDeck1();
	const before = deck(2).key_shift_semitones;
	await audio.engine.setMasterTempo(1, false);
	await audio.engine.setTempoRatio(1, 1.12);
	await settle();
	assert.equal(deck(1).master_tempo_enabled, false);
	assertFollowing(2, 1, 'after master varispeed');
	assert.notEqual(deck(2).key_shift_semitones, before, '+12% varispeed is ~+2 semitones and must move the follower');
});

test('master reassignment: the follower follows the newly selected master', async () => {
	await followDeck1();
	await audio.engine.load(3, SID_11B);
	const before = deck(2).key_shift_semitones;
	await audio.engine.setDeckMaster(3);
	await settle();
	assertFollowing(2, 3, 'after reassignment to deck 3');
	assert.notEqual(deck(2).key_shift_semitones, before, '11B must need a different shift than 8A');
});

test('master track swap: the follower never stays lit against a stale master key', async () => {
	await followDeck1();
	await audio.engine.load(1, SID_5A);
	await settle();
	assert.equal(deck(1).key, '5A');
	const status = audio.keySyncStatus(2);
	if (status === 'following') {
		assertFollowing(2, 1, 'after master track swap');
	} else {
		assert.equal(status, 'waiting-no-master', 'a swap that drops the master must read as waiting');
		assert.equal(deck(2).key_sync_enabled, true, 'the arm survives so a new master is followed');
		await audio.engine.setDeckMaster(1);
		await settle();
		assertFollowing(2, 1, 'after master re-selected on the swapped deck');
	}
});

test('follower becomes master: KEY SYNC stops reporting following and keeps the key', async () => {
	await followDeck1();
	const shift = deck(2).key_shift_semitones;
	await audio.engine.setDeckMaster(2);
	await settle();
	assert.equal(audio.keySyncStatus(2), 'waiting-is-master');
	assert.equal(deck(2).key_shift_semitones, shift, 'becoming master must not jump the audible key');
	await audio.engine.setDeckMaster(1);
	await settle();
	assertFollowing(2, 1, 'after master handed back');
});

test('a manual nudge on the follower disengages KEY SYNC instead of being undone', async () => {
	await followDeck1();
	const shift = deck(2).key_shift_semitones;
	await audio.engine.nudgeKey(2, 1);
	await settle();
	assert.equal(deck(2).key_sync_enabled, false);
	assert.equal(audio.keySyncStatus(2), 'off');
	assert.equal(deck(2).key_shift_semitones, shift + 1);
});

test('a refused out-of-range nudge leaves KEY SYNC armed (PR #5404 Devin P2)', async () => {
	await loadPair();
	for (let i = 0; i < 12; i += 1) await audio.engine.nudgeKey(2, 1);
	assert.equal(deck(2).key_shift_semitones, 12);
	await audio.engine.setKeySync(2, true);
	assert.equal(audio.keySyncStatus(2), 'waiting-no-master');
	await assert.rejects(audio.engine.nudgeKey(2, 1));
	assert.equal(deck(2).key_sync_enabled, true, 'a nudge that never applied must not disarm');
	assert.equal(deck(2).key_shift_semitones, 12);
});

test('turning KEY SYNC off restores the pre-sync manual shift', async () => {
	await followDeck1();
	await audio.engine.nudgeKey(1, 1);
	await settle();
	await audio.engine.setKeySync(2, false);
	assert.equal(deck(2).key_shift_semitones, 0);
	assert.equal(audio.keySyncStatus(2), 'off');
});

test('enabling twice keeps the original baseline', async () => {
	await followDeck1();
	await audio.engine.setKeySync(2, true);
	await audio.engine.setKeySync(2, false);
	assert.equal(deck(2).key_shift_semitones, 0);
});

test('explicitly enabling KEY SYNC on the selected master still refuses', async () => {
	await loadPair();
	await audio.engine.setDeckMaster(1);
	await assert.rejects(audio.engine.setKeySync(1, true), /selected master deck/);
	assert.equal(deck(1).key_sync_enabled, false);
});

test('a live deck re-follows when a newly presented schedule revision crosses output', () => {
	// Live decks publish key/tempo changes at output presentation, which a
	// paused-deck test cannot reach, so this pins the hook in source.
	const body = engineBlockAfter('function _publishPresentedTransport(\n\tdeck: DeckId,\n\toutputTimestamp: { contextTime: number; performanceTime: number }\n): PresentedTransportObservation | null {');
	const before = body.indexOf('const presentedRevisionBefore = rt.presentation.presented_revision;');
	const observe = body.indexOf('observePresentedTransportTimeline(');
	const queue = body.indexOf('if (rt.presentation.presented_revision !== presentedRevisionBefore) _queueKeySyncFollow();');
	assert.ok(before !== -1 && observe !== -1 && queue !== -1, 'if the presented-revision follow hook is gone then a live master nudge is never followed');
	assert.ok(before < observe && observe < queue, 'the revision must be read before observing and compared after');
});
