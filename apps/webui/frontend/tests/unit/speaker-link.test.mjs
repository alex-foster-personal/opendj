/**
 * Requirements: AUDIO-DEVICE-04, AUDIO-DEVICE-05, AUDIO-DEVICE-06.
 *
 * Regression lines:
 * - if a Bluetooth stall with auto-recover on does not reconnect then the HYPERBOOM stays silent
 * - if a second stall inside 60 s reconnects again then a flapping link loops the speaker
 * - if the reconnect does not recreate the room graph then the page keeps a dead stream
 * - if auto-recover runs in gig posture by default then a room hears a 5 s drop unasked
 * - if a mic window that clearly hears the master reads missed then the proof cries wolf
 * - if a silent mic reads heard then the stall stays a false positive
 * - if a Bluetooth input is ever a proof mic then HFP wrecks the speaker under test
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let rec;
let proof;
let prefs;

before(async () => {
	rec = await loadTypeScriptModule('src/lib/rb/speaker-link-recovery.ts');
	proof = await loadTypeScriptModule('src/lib/rb/speaker-acoustic-proof.ts');
	prefs = await loadTypeScriptModule('src/lib/rb/speaker-link-prefs.ts');
});

function harness({ auto = true, reconnectResult = { reconnected: true, device_name: 'HYPERBOOM', elapsed_ms: 5100 }, recreate = async () => {} } = {}) {
	let now = 0;
	const calls = { reconnect: 0, recreate: 0, toasts: [], perf: [], statuses: [] };
	const r = rec.createSpeakerLinkRecovery({
		reconnect: async () => {
			calls.reconnect += 1;
			return reconnectResult;
		},
		recreateGraph: async () => {
			calls.recreate += 1;
			await recreate();
		},
		pushToast: (message, kind) => calls.toasts.push({ message, kind }),
		recordPerfEvent: (kind) => calls.perf.push(kind),
		autoRecoverEnabled: () => auto,
		now: () => now,
		onStatus: (status) => calls.statuses.push(status.phase)
	});
	return { r, calls, advance: (ms) => (now += ms) };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('speaker link recovery (AUDIO-DEVICE-05)', () => {
	it('a Bluetooth stall reconnects, recreates the room graph and shows the toast', async () => {
		const { r, calls } = harness();
		assert.equal(r.onStall('shell', 'bluetooth'), 'reconnect', 'if a BT stall does not reconnect then auto-recover is broken');
		await settle();
		await settle();
		assert.equal(calls.reconnect, 1);
		assert.equal(calls.recreate, 1, 'if the room graph is not recreated then recover is broken');
		assert.deepEqual(calls.toasts, [{ message: 'Speaker link stalled, reconnected (≈5 s)', kind: 'info' }]);
		assert.equal(r.status().phase, 'reconnected');
	});

	it('auto reconnect is once per 60 s, then a manual prompt', async () => {
		const { r, calls, advance } = harness();
		r.onStall('shell', 'bluetooth');
		await settle();
		await settle();
		advance(30_000);
		assert.equal(r.onStall('acoustic', 'bluetooth'), 'prompt', 'if a second stall inside 60 s reconnects then the rate limit is broken');
		assert.equal(calls.reconnect, 1);
		assert.equal(r.status().phase, 'prompt');
		assert.equal(calls.toasts.at(-1).message, rec.SPEAKER_LINK_MANUAL_PROMPT);
		advance(30_001);
		assert.equal(r.onStall('shell', 'bluetooth'), 'reconnect', 'if the cooldown never ends then recovery is lost for the session');
	});

	it('auto-recover off prompts, and a manual press still reconnects', async () => {
		const { r, calls } = harness({ auto: false });
		assert.equal(r.onStall('shell', 'bluetooth'), 'prompt');
		assert.equal(calls.reconnect, 0);
		const result = await r.reconnect('manual');
		assert.equal(result.reconnected, true);
		assert.equal(calls.reconnect, 1);
	});

	it('a non-Bluetooth main output is ignored (the switch-output path owns it)', () => {
		const { r, calls } = harness();
		assert.equal(r.onStall('shell', 'usb'), 'ignore');
		assert.equal(r.onStall('shell', null), 'ignore');
		assert.equal(calls.reconnect, 0);
		assert.deepEqual(calls.toasts, []);
	});

	it('a failed shell reconnect is an error toast and never recreates or claims success', async () => {
		const { r, calls } = harness({ reconnectResult: { reconnected: false, error: 'blueutil not found at /opt/homebrew/bin/blueutil' } });
		const result = await r.reconnect('manual');
		assert.equal(result.reconnected, false);
		assert.equal(calls.recreate, 0);
		assert.equal(calls.toasts[0].kind, 'error');
		assert.match(calls.toasts[0].message, /blueutil not found/);
		assert.equal(r.status().phase, 'failed');
	});

	it('a recreate failure is reported, not swallowed', async () => {
		const { r, calls } = harness({ recreate: async () => { throw new Error('addModule timed out'); } });
		const result = await r.reconnect('manual');
		assert.equal(result.reconnected, false);
		assert.match(calls.toasts[0].message, /addModule timed out/);
	});

	it('concurrent presses share one reconnect', async () => {
		const { r, calls } = harness();
		await Promise.all([r.reconnect('manual'), r.reconnect('manual')]);
		assert.equal(calls.reconnect, 1);
	});
});

function envelope(n, seed) {
	let x = seed;
	const out = [];
	for (let i = 0; i < n; i++) {
		x = (x * 1103515245 + 12345) % 2147483648;
		out.push(0.05 + 0.1 * (x / 2147483648));
	}
	return out;
}

describe('acoustic proof (AUDIO-DEVICE-04)', () => {
	const N = 100; // ACOUSTIC_WINDOW_FRAMES, asserted below

	it('a window is 2 s of 20 ms frames', () => {
		assert.equal(proof.ACOUSTIC_WINDOW_FRAMES, N);
	});

	it('a mic that hears the master 200 ms late, quieter and noisy, reads heard', () => {
		const master = envelope(N, 7);
		const noise = envelope(N, 99);
		const lag = 10;
		const mic = master.map((_, i) => 0.3 * (master[i - lag] ?? master[0]) + 0.05 * noise[i]);
		const judged = proof.judgeWindow({ master, mic, deckPlayedThroughout: true });
		assert.equal(judged.kind, 'heard', 'if a delayed copy reads missed then the proof cries wolf');
		assert.ok(judged.peak > 0.8);
	});

	it('a mic hearing only unrelated room noise reads missed', () => {
		const judged = proof.judgeWindow({ master: envelope(N, 7), mic: envelope(N, 12345), deckPlayedThroughout: true });
		assert.equal(judged.kind, 'missed', 'if a silent room reads heard then the stall stays a false positive');
	});

	it('a quiet, flat or not-playing window proves nothing', () => {
		const master = envelope(N, 7);
		assert.equal(proof.judgeWindow({ master, mic: master, deckPlayedThroughout: false }).kind, 'inconclusive');
		assert.equal(proof.judgeWindow({ master: master.map(() => 0.0001), mic: master, deckPlayedThroughout: true }).kind, 'inconclusive');
		assert.equal(proof.judgeWindow({ master: master.map(() => 0.2), mic: master, deckPlayedThroughout: true }).kind, 'inconclusive');
	});

	it('N consecutive misses report not_delivering once; a heard window resets', () => {
		const t = proof.createAcousticTracker(3);
		const miss = { kind: 'missed', peak: 0.01 };
		assert.equal(t.push(miss).verdict, 'inconclusive');
		assert.equal(t.push({ kind: 'inconclusive', peak: null }).failed_windows, 1, 'a quiet window neither clears nor adds');
		assert.equal(t.push(miss).verdict, 'inconclusive');
		const third = t.push(miss);
		assert.equal(third.verdict, 'not_delivering');
		assert.equal(third.newly_not_delivering, true);
		assert.equal(t.push(miss).newly_not_delivering, false, 'if every later miss re-fires then recovery loops');
		assert.equal(t.push({ kind: 'heard', peak: 0.9 }).verdict, 'heard');
		assert.throws(() => proof.createAcousticTracker(0), /windowsRequired/);
	});

	it('only wired or USB inputs the shell lists are proof mics, never Bluetooth', () => {
		const inputs = [
			{ uid: 'b', name: 'MX Brio', transport: 'usb', channels: 2, acoustic_proof_allowed: true },
			{ uid: 's', name: 'af-sony-fun', transport: 'bluetooth', channels: 1, acoustic_proof_allowed: false },
			{ uid: 'h', name: 'BlackHole 2ch', transport: 'virtual', channels: 2, acoustic_proof_allowed: false }
		];
		const mics = [
			{ deviceId: 'd1', label: 'MX Brio (046d:0944)' },
			{ deviceId: 'd2', label: 'af-sony-fun' },
			{ deviceId: 'd3', label: 'BlackHole 2ch' },
			{ deviceId: 'd4', label: '' },
			{ deviceId: 'd5', label: 'Unknown Mic' }
		];
		assert.deepEqual(proof.proofMicCandidates(inputs, mics), [{ deviceId: 'd1', label: 'MX Brio (046d:0944)', transport: 'usb' }]);
		const lying = [{ ...inputs[1], acoustic_proof_allowed: true }];
		assert.deepEqual(proof.proofMicCandidates(lying, mics), [], 'if a Bluetooth transport passes on the flag alone then HFP can trigger');
	});
});

describe('speaker auto-recover pref (AUDIO-DEVICE-06)', () => {
	it('defaults to auto: on at home (prep), off in gig', () => {
		assert.deepEqual(prefs.SPEAKER_LINK_PREF_DEFAULTS, { speaker_auto_recover: 'auto' });
		assert.equal(prefs.resolveSpeakerAutoRecover('auto', 'prep'), true);
		assert.equal(prefs.resolveSpeakerAutoRecover('auto', 'gig'), false, 'if auto runs in gig then a room hears an unasked drop');
		assert.equal(prefs.resolveSpeakerAutoRecover('on', 'gig'), true);
		assert.equal(prefs.resolveSpeakerAutoRecover('off', 'prep'), false);
	});

	it('the setter persists and PUTs; malformed values are refused', () => {
		const state = { ...prefs.SPEAKER_LINK_PREF_DEFAULTS };
		const patches = [];
		const s = prefs.makeSpeakerLinkSetters(state, () => {}, (p) => patches.push(p));
		s.setSpeakerAutoRecover('off');
		assert.deepEqual(patches, [{ speaker_auto_recover: 'off' }]);
		assert.throws(() => s.setSpeakerAutoRecover('sometimes'), /speaker_auto_recover/);
		assert.deepEqual(prefs.validateSpeakerLinkPrefFields({}, 'k'), {});
		assert.throws(() => prefs.validateSpeakerLinkPrefFields({ speaker_auto_recover: 1 }, 'mdt.test'), /mdt\.test.*speaker_auto_recover/);
	});
});
