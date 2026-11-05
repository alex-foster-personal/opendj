// requirement: AUDFID-03 (live fidelity guard: master bus vs the source decode in the neutral state)
// [if] the master is the source at another level [then] it reads ok (gain invariant) ⛔️
// [if] the master is left only, or a mono fold of a stereo source [then] it reads degraded with that reason ⛔️
// [if] the highs are 4 dB below the original [then] degraded; 2 dB stays ok (3 dB threshold) ⛔️
// [if] one degraded window, or an episode that persists [then] no toast, then exactly one toast "Playback degraded vs original: <reason>" and one client_errors entry ⛔️
// [if] the deck is not neutral or two decks play [then] the tick is a named skip, never a pass ⛔️
// [if] the audio-headset-mode verdict changes [then] it is logged; a repeat is not ⛔️
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let g;
let headset;
before(async () => {
	g = await loadTypeScriptModule('tests/unit/fixtures/fidelity-guard-entry.ts');
	headset = await loadTypeScriptModule('src/lib/rb/bluetooth-headset-mode.svelte.ts');
});

const SR = 48000;
const N = 16384;

/** Deterministic noise: a seeded LCG, so every run sees the same signal. */
function noise(seed) {
	let x = seed >>> 0;
	return () => {
		x = (1664525 * x + 1013904223) >>> 0;
		return x / 2 ** 32 - 0.5;
	};
}

/** A stereo, broadband, partly correlated window (correlation ~0.6). */
function stereo(seed = 1) {
	const common = noise(seed);
	const l = noise(seed + 7);
	const r = noise(seed + 13);
	const left = new Float32Array(N);
	const right = new Float32Array(N);
	for (let i = 0; i < N; i += 1) {
		const c = common();
		left[i] = 0.3 * (c + 0.8 * l());
		right[i] = 0.3 * (c + 0.8 * r());
	}
	return { left, right, sampleRate: SR };
}

const map = (w, f) => ({ left: w.left.map(f), right: w.right.map(f), sampleRate: w.sampleRate });

/** Two bands built apart so a high-band cut is exact: smoothed noise (energy
 * mostly under 4 kHz) and the same kind of noise shifted to Nyquist by
 * (-1)^n (energy above 23 kHz). `cutHighs` scales only the second. */
function twoBand(seed = 3) {
	const smooth = (rnd) => {
		let y = 0;
		return () => (y = 0.1 * rnd() + 0.9 * y);
	};
	const lowSrc = stereo(seed);
	const hiSrc = stereo(seed + 100);
	const lowL = smooth(
		(
			(i) => () =>
				lowSrc.left[i++]
		)(0)
	);
	const lowR = smooth(
		(
			(i) => () =>
				lowSrc.right[i++]
		)(0)
	);
	const hiL = smooth(
		(
			(i) => () =>
				hiSrc.left[i++]
		)(0)
	);
	const hiR = smooth(
		(
			(i) => () =>
				hiSrc.right[i++]
		)(0)
	);
	const low = { left: new Float32Array(N), right: new Float32Array(N), sampleRate: SR };
	const high = { left: new Float32Array(N), right: new Float32Array(N), sampleRate: SR };
	for (let i = 0; i < N; i += 1) {
		const sign = i % 2 === 0 ? 1 : -1;
		low.left[i] = lowL();
		low.right[i] = lowR();
		high.left[i] = sign * hiL();
		high.right[i] = sign * hiR();
	}
	return { low, high };
}

function mixBands(bands, highDb) {
	const k = 10 ** (-highDb / 20);
	const out = { left: new Float32Array(N), right: new Float32Array(N), sampleRate: SR };
	for (let i = 0; i < N; i += 1) {
		out.left[i] = bands.low.left[i] + k * bands.high.left[i];
		out.right[i] = bands.low.right[i] + k * bands.high.right[i];
	}
	return out;
}

const verdictOf = (master, source) =>
	g.compareFidelity(g.fidelityStats(master), g.fidelityStats(source));

test('if a fader move reads as degraded then every level change raises a false alarm', () => {
	const src = stereo();
	assert.deepEqual(
		verdictOf(
			map(src, (v) => v * 0.5),
			src
		),
		{ kind: 'ok' }
	);
	assert.deepEqual(verdictOf(src, src), { kind: 'ok' });
});

test('if a left-only master reads ok then the 13:37Z mono split goes unseen', () => {
	const src = stereo();
	const leftOnly = { left: src.left, right: new Float32Array(N), sampleRate: SR };
	const verdict = verdictOf(leftOnly, src);
	assert.equal(verdict.kind, 'degraded');
	assert.match(verdict.reason, /^left channel only/);
});

test('if a mono fold of a stereo source reads ok then lost width goes unseen', () => {
	const src = stereo();
	const mid = src.left.map((v, i) => (v + src.right[i]) / 2);
	const verdict = verdictOf({ left: mid, right: mid.slice(), sampleRate: SR }, src);
	assert.equal(verdict.kind, 'degraded');
	assert.match(verdict.reason, /stereo width lost/);
	// Control: a mono source played mono is not a fold.
	assert.deepEqual(
		verdictOf(
			{ left: mid, right: mid.slice(), sampleRate: SR },
			{ left: mid, right: mid.slice(), sampleRate: SR }
		),
		{ kind: 'ok' }
	);
});

test('if a 4 dB high cut is not caught, or a 2 dB one is, then the 3 dB tilt threshold is not what the spec says', () => {
	const bands = twoBand();
	const src = mixBands(bands, 0);
	const bad = verdictOf(mixBands(bands, 4), src);
	assert.equal(bad.kind, 'degraded', 'the measured stems residual (4 dB) must trip');
	assert.match(bad.reason, /^highs \d\.\d dB below the original/);
	assert.deepEqual(verdictOf(mixBands(bands, 2), src), { kind: 'ok' });
});

test('if silence is judged then a fader at zero reads as degraded', () => {
	const src = stereo();
	assert.equal(
		verdictOf(
			map(src, () => 0),
			src
		).kind,
		'skip'
	);
});

function guardWith(samples) {
	const toasts = [];
	const errors = [];
	const published = [];
	let i = 0;
	const guard = g.createFidelityGuard({
		sample: () => samples[Math.min(i++, samples.length - 1)],
		toast: (text) => toasts.push(text),
		reportClientError: (reason) => errors.push(reason),
		publish: (state) => published.push(state),
		now: () => 1000 + i
	});
	return { guard, toasts, errors, published };
}

test('if one bad window toasts, or a lasting fault toasts every tick, then the warning is noise', () => {
	const src = stereo();
	const bad = {
		master: { left: src.left, right: new Float32Array(N), sampleRate: SR },
		source: src
	};
	const good = { master: src, source: src };
	const { guard, toasts, errors, published } = guardWith([
		bad,
		good,
		good,
		bad,
		bad,
		bad,
		bad,
		bad,
		good,
		good,
		bad,
		bad,
		bad
	]);
	for (let t = 0; t < 13; t += 1) guard.tick();
	assert.equal(toasts.length, 2, 'one toast per persistent episode, none for the lone window');
	assert.match(toasts[0], /^Playback degraded vs original: left channel only/);
	assert.equal(errors.length, 2, 'one client_errors entry per toast');
	assert.equal(published.at(-1).status, 'degraded');
});

test('if a skipped tick counts as a pass or a fault then the guard lies about what it checked', () => {
	const { guard, toasts, published } = guardWith([
		{ skip: '2 decks audible on MAIN (needs exactly one)' }
	]);
	for (let t = 0; t < 5; t += 1) assert.equal(guard.tick().kind, 'skip');
	assert.equal(toasts.length, 0);
	assert.deepEqual(
		{ status: published.at(-1).status, reason: published.at(-1).reason },
		{ status: 'skipped', reason: '2 decks audible on MAIN (needs exactly one)' }
	);
});

function deckView(overrides = {}) {
	const buffer = {
		sampleRate: SR,
		length: SR * 10,
		numberOfChannels: 2,
		getChannelData: () => new Float32Array(SR * 10)
	};
	return {
		playing: true,
		audible: true,
		stems: { status: 'ready', controls: g.createDefaultStemControls() },
		channel: { eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.5 },
		tempoRatio: 1,
		keyShiftSemitones: 0,
		buffer,
		positionSec: 5,
		...overrides
	};
}

test('if a non-neutral deck, two decks or an operator split is compared then the guard raises false alarms', () => {
	const tap = { read: () => stereo(), disconnect() {} };
	const base = { tap, contextSampleRate: SR, operatorSplit: false, cueBlendOnMain: false };
	assert.ok(
		'master' in g.sampleFidelity({ ...base, decks: [deckView(), deckView({ playing: false })] })
	);
	const touched = g.createDefaultStemControls();
	touched.vocal.muted = true;
	const skips = [
		{ ...base, decks: [deckView(), deckView()] },
		{ ...base, decks: [deckView({ stems: { status: 'ready', controls: touched } })] },
		{
			...base,
			decks: [deckView({ channel: { eq_low: 0.5, eq_mid: 0.5, eq_high: 0.7, filter: 0.5 } })]
		},
		{
			...base,
			decks: [deckView({ channel: { eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter: 0.6 } })]
		},
		{ ...base, decks: [deckView({ tempoRatio: 1.02 })] },
		{ ...base, decks: [deckView({ positionSec: 0.1 })] },
		{ ...base, operatorSplit: true, decks: [deckView()] },
		{ ...base, tap: null, decks: [deckView()] }
	];
	for (const args of skips)
		assert.ok('skip' in g.sampleFidelity(args), JSON.stringify(Object.keys(args)));
});

test('if a headset verdict change is not logged, or a repeat is, then the BT call-profile flip is invisible or noisy', () => {
	const log = [];
	const record = (kind, message, deck, level) => log.push({ kind, message, level });
	const a2dp = { verdict: 'clear', output: 'HYPERBOOM', input: null };
	const hfp = { verdict: 'headset_mode', output: 'HYPERBOOM', input: 'HYPERBOOM' };
	assert.equal(headset.logHeadsetVerdictChange(null, a2dp, record), true);
	assert.equal(headset.logHeadsetVerdictChange(a2dp, a2dp, record), false);
	assert.equal(headset.logHeadsetVerdictChange(a2dp, hfp, record), true);
	assert.deepEqual(
		log.map((e) => [e.kind, e.level]),
		[
			['audio-headset-mode-change', 'info'],
			['audio-headset-mode-change', 'warn']
		]
	);
	assert.match(log[1].message, /^audio-headset-mode clear -> headset_mode/);
});
