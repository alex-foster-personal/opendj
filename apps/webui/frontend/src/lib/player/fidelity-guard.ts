/**
 * AUDFID-03: the live fidelity guard. Compares what the master bus plays with
 * the source decode at the same place, while the deck is in the neutral state
 * (stem controls untouched, EQ flat, one deck audible, no tempo or key change),
 * so a degraded path is told to the operator instead of only heard.
 *
 * Failures it exists for (silver, Tue 3 Nov 2026):
 *   - output_mode silently became split_cable: the master went mono, LEFT ONLY
 *     (R energy at the floor, 3-6 dB lost, no stereo width).
 *   - stems through four stretchers: residual only 4 dB below source above
 *     8 kHz (Tank, 64/96 kbps stems), heard as "tinny".
 *
 * Gain invariance: the fader, trim, crossfader and master volume move the level
 * of every band together, so bands are compared as a TILT relative to the mids
 * (highs - mids, lows - mids, in dB), never as absolute levels.
 *
 * Thresholds (each is a CFG constant below):
 *   - TILT_DB 3: a broadband shelf of 3 dB is clearly audible (about 3x the
 *     ~1 dB detection threshold for a broad tilt), and it sits above the
 *     window-to-window wobble of a 0.34 s window on music whose master and
 *     source windows are offset by the output latency (measured on synthetic
 *     noise below 1 dB). The stems failure (highs residual 4 dB below signal)
 *     moves the high tilt by 2 to 6 dB, so it trips on bright material.
 *   - CORRELATION_RISE 0.3 above the source, to at least 0.97: a mono fold of
 *     a stereo source (typical L/R correlation 0.3 to 0.8) reads 1.0; a mono
 *     source (correlation already ~1) can never trip it.
 *   - BALANCE_DB 10: a left-only master reads R at the floor (> 40 dB down);
 *     real mixes sit within 2 dB, so 10 dB has no musical false positives.
 *   - PERSIST_WINDOWS 3 (6 s at the 2 s tick): one transient, a cue point jump
 *     or a misaligned window cannot raise it alone; RECOVER_WINDOWS 2 ends the
 *     episode, so one fault is one toast.
 *   - SILENCE_DB -60: below this either side carries no music worth judging.
 *
 *   ✔︎ ✅ 🎯 Gain-invariant band tilt, correlation and balance comparison.
 *       [if] the master is the source at -6 dB [then ⛔️] it reads degraded
 *       [if] the master is left only [then ⛔️] it reads ok
 */

export const FIDELITY_GUARD_CFG = Object.freeze({
	LOWS_MAX_HZ: 250,
	HIGHS_MIN_HZ: 4000,
	TILT_DB: 3,
	CORRELATION_RISE: 0.3,
	CORRELATION_MONO: 0.97,
	BALANCE_DB: 10,
	SILENCE_DB: -60,
	PERSIST_WINDOWS: 3,
	RECOVER_WINDOWS: 2,
	TICK_MS: 2000,
	WINDOW_FRAMES: 16384
});

export interface StereoWindow {
	left: Float32Array;
	right: Float32Array;
	sampleRate: number;
}

export interface FidelityStats {
	lowsDb: number;
	midsDb: number;
	highsDb: number;
	/** Pearson correlation of L and R; 1 for mono, NaN when a side is silent. */
	correlation: number;
	/** L energy minus R energy, dB. */
	balanceDb: number;
	levelDb: number;
}

export type FidelityVerdict =
	{ kind: 'ok' } | { kind: 'degraded'; reason: string } | { kind: 'skip'; reason: string };

const EPS = 1e-12;
const toDb = (power: number): number => 10 * Math.log10(power + EPS);

//------------------------------------------------------------------ helpers

function _assertWindow(w: StereoWindow, name: string): void {
	if (!(w.left instanceof Float32Array) || !(w.right instanceof Float32Array))
		throw new TypeError(`${name}: channels must be Float32Array`);
	if (w.left.length !== w.right.length || w.left.length === 0)
		throw new RangeError(`${name}: L/R must be equal, non-empty lengths`);
	if (!Number.isFinite(w.sampleRate) || w.sampleRate <= 0)
		throw new RangeError(`${name}: sampleRate must be positive`);
}

/** In-place radix-2 FFT. `re.length` must be a power of two. */
function _fft(re: Float64Array, im: Float64Array): void {
	const n = re.length;
	for (let i = 1, j = 0; i < n; i += 1) {
		let bit = n >> 1;
		for (; j & bit; bit >>= 1) j ^= bit;
		j ^= bit;
		if (i < j) {
			[re[i], re[j]] = [re[j], re[i]];
			[im[i], im[j]] = [im[j], im[i]];
		}
	}
	for (let len = 2; len <= n; len <<= 1) {
		const ang = (-2 * Math.PI) / len;
		const wr = Math.cos(ang);
		const wi = Math.sin(ang);
		for (let i = 0; i < n; i += len) {
			let cr = 1;
			let ci = 0;
			for (let k = 0; k < len / 2; k += 1) {
				const a = i + k;
				const b = a + len / 2;
				const tr = re[b] * cr - im[b] * ci;
				const ti = re[b] * ci + im[b] * cr;
				re[b] = re[a] - tr;
				im[b] = im[a] - ti;
				re[a] += tr;
				im[a] += ti;
				const next = cr * wr - ci * wi;
				ci = cr * wi + ci * wr;
				cr = next;
			}
		}
	}
}

function _largestPowerOfTwo(n: number): number {
	return 2 ** Math.floor(Math.log2(n));
}

/** Band powers of the mid (L+R)/2 signal, Hann-windowed. */
function _bandPowers(w: StereoWindow): { lows: number; mids: number; highs: number } {
	const n = _largestPowerOfTwo(w.left.length);
	const re = new Float64Array(n);
	const im = new Float64Array(n);
	for (let i = 0; i < n; i += 1) {
		const hann = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
		re[i] = ((w.left[i] + w.right[i]) / 2) * hann;
	}
	_fft(re, im);
	let lows = 0;
	let mids = 0;
	let highs = 0;
	const hzPerBin = w.sampleRate / n;
	for (let k = 1; k < n / 2; k += 1) {
		const power = re[k] * re[k] + im[k] * im[k];
		const hz = k * hzPerBin;
		if (hz < FIDELITY_GUARD_CFG.LOWS_MAX_HZ) lows += power;
		else if (hz < FIDELITY_GUARD_CFG.HIGHS_MIN_HZ) mids += power;
		else highs += power;
	}
	return { lows, mids, highs };
}

//------------------------------------------------------------------ public

export function fidelityStats(w: StereoWindow): FidelityStats {
	_assertWindow(w, 'fidelityStats');
	let ll = 0;
	let rr = 0;
	let lr = 0;
	let sl = 0;
	let sr = 0;
	const n = w.left.length;
	for (let i = 0; i < n; i += 1) {
		const l = w.left[i];
		const r = w.right[i];
		ll += l * l;
		rr += r * r;
		lr += l * r;
		sl += l;
		sr += r;
	}
	const covariance = lr / n - (sl / n) * (sr / n);
	const varL = ll / n - (sl / n) ** 2;
	const varR = rr / n - (sr / n) ** 2;
	const correlation = varL > EPS && varR > EPS ? covariance / Math.sqrt(varL * varR) : Number.NaN;
	const bands = _bandPowers(w);
	return {
		lowsDb: toDb(bands.lows),
		midsDb: toDb(bands.mids),
		highsDb: toDb(bands.highs),
		correlation,
		balanceDb: toDb(ll / n) - toDb(rr / n),
		levelDb: toDb((ll + rr) / (2 * n))
	};
}

/** One window's verdict: the master against the source at the same place. */
export function compareFidelity(master: FidelityStats, source: FidelityStats): FidelityVerdict {
	const cfg = FIDELITY_GUARD_CFG;
	if (source.levelDb < cfg.SILENCE_DB) return { kind: 'skip', reason: 'the source is silent here' };
	if (master.levelDb < cfg.SILENCE_DB)
		return { kind: 'skip', reason: 'the master is silent (fader down or a gap)' };
	const sideGap = Math.abs(master.balanceDb) - Math.abs(source.balanceDb);
	if (sideGap > cfg.BALANCE_DB) {
		const side = master.balanceDb > 0 ? 'left' : 'right';
		return {
			kind: 'degraded',
			reason: `${side} channel only (other side ${Math.abs(master.balanceDb).toFixed(0)} dB down; mono split cue?)`
		};
	}
	if (
		Number.isFinite(master.correlation) &&
		Number.isFinite(source.correlation) &&
		master.correlation >= cfg.CORRELATION_MONO &&
		master.correlation - source.correlation > cfg.CORRELATION_RISE
	) {
		return {
			kind: 'degraded',
			reason: `stereo width lost (L/R correlation ${master.correlation.toFixed(2)} vs ${source.correlation.toFixed(2)} in the original)`
		};
	}
	const highTilt = master.highsDb - master.midsDb - (source.highsDb - source.midsDb);
	if (Math.abs(highTilt) > cfg.TILT_DB) {
		return {
			kind: 'degraded',
			reason: `highs ${Math.abs(highTilt).toFixed(1)} dB ${highTilt < 0 ? 'below' : 'above'} the original`
		};
	}
	const lowTilt = master.lowsDb - master.midsDb - (source.lowsDb - source.midsDb);
	if (Math.abs(lowTilt) > cfg.TILT_DB) {
		return {
			kind: 'degraded',
			reason: `lows ${Math.abs(lowTilt).toFixed(1)} dB ${lowTilt < 0 ? 'below' : 'above'} the original`
		};
	}
	return { kind: 'ok' };
}

export interface FidelityEpisode {
	degradedRun: number;
	okRun: number;
	/** The reason shown for the episode in progress; null when none is. */
	firing: string | null;
}

export const FIDELITY_EPISODE_IDLE: FidelityEpisode = Object.freeze({
	degradedRun: 0,
	okRun: 0,
	firing: null
});

/** Persistence: `fire` is the reason exactly once per episode. A skip neither
 * builds nor ends one. */
export function stepFidelityEpisode(
	state: FidelityEpisode,
	verdict: FidelityVerdict
): { state: FidelityEpisode; fire: string | null } {
	const cfg = FIDELITY_GUARD_CFG;
	switch (verdict.kind) {
		case 'skip':
			return { state, fire: null };
		case 'ok': {
			const okRun = state.okRun + 1;
			const ended = okRun >= cfg.RECOVER_WINDOWS;
			return {
				state: {
					degradedRun: ended ? 0 : state.degradedRun,
					okRun,
					firing: ended ? null : state.firing
				},
				fire: null
			};
		}
		case 'degraded': {
			const degradedRun = state.degradedRun + 1;
			const fire =
				state.firing === null && degradedRun >= cfg.PERSIST_WINDOWS ? verdict.reason : null;
			return { state: { degradedRun, okRun: 0, firing: state.firing ?? fire }, fire };
		}
		default: {
			const unhandled: never = verdict;
			throw new Error(`unhandled fidelity verdict: ${String(unhandled)}`);
		}
	}
}

export function fidelityToastText(reason: string): string {
	return `Playback degraded vs original: ${reason}`;
}
