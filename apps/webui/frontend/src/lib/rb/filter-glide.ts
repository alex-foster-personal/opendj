/**
 * FILTER corner glide (FXFILT-10611): schedules a knob move's corner frequency on
 * an AudioParam, in Hz (today's `setTargetAtTime`) or in octaves (a one-pole on
 * log2 of the corner, `(private path)` section 7.4).
 *
 * The octave glide is a per-frame curve (`setValueCurveAtTime`, one point per
 * frame, so the browser's linear interpolation never acts between frames) of
 * the same recurrence the Rust engine runs (`dsp::CornerGlide`):
 *
 *   L[n+1] = L[n] + (T - L[n]) * k,  k = 1 - exp(-1 / (tau * sr)),  hz = 2^L
 *
 * Frame 0 holds the start value, as `setTargetAtTime` does. The curve is sent
 * as a Float32Array, which rounds the corner to about 1e-7 relative; the web
 * vs Rust parity tolerance (1e-6) is set against that.
 *
 * A retarget's `cancelScheduledValues(now)` removes the curve active at `now`
 * (Web Audio spec), so the start value comes from the recorded curve
 * (`cornerHzAt`), not the AudioParam; tests/e2e/filter-glide-real-audio.spec.ts
 * proves it on Chromium.
 */
import type { FilterGlideDomain } from '$lib/player/constants';

/** The AudioParam surface a corner glide touches. */
export interface CornerGlideParam {
	value: number;
	cancelScheduledValues(t: number): unknown;
	setTargetAtTime(v: number, t: number, tau: number): unknown;
	setValueCurveAtTime(curve: Float32Array, t: number, duration: number): unknown;
}

/** Octaves; a glide this close to its target snaps to it (matches `dsp::Smoothed`). */
export const OCTAVE_GLIDE_SETTLE = 1e-9;

interface RecordedGlide {
	curve: Float64Array;
	start: number;
	sampleRate: number;
}

const _glides = new WeakMap<CornerGlideParam, RecordedGlide>();

function _assertPositive(name: string, v: number): void {
	if (!Number.isFinite(v) || v <= 0)
		throw new RangeError(`${name} must be finite and > 0, got ${v}`);
}

/** Per-frame log2 one-pole from `fromHz` to `toHz`: frame 0 = fromHz, last = toHz exactly. */
export function octaveGlideCurve(
	fromHz: number,
	toHz: number,
	sampleRate: number,
	tauS: number
): Float64Array {
	_assertPositive('fromHz', fromHz);
	_assertPositive('toHz', toHz);
	_assertPositive('sampleRate', sampleRate);
	_assertPositive('tauS', tauS);
	const k = 1 - Math.exp(-1 / (tauS * sampleRate));
	const target = Math.log2(toHz);
	const points: number[] = [fromHz];
	let l = Math.log2(fromHz);
	while (l !== target) {
		l += (target - l) * k;
		if (Math.abs(target - l) < OCTAVE_GLIDE_SETTLE) l = target;
		points.push(l === target ? toHz : 2 ** l);
	}
	return Float64Array.from(points);
}

/** Corner value the scheduled glide has at `t` (the recorded octave curve, else `param.value`). */
export function cornerHzAt(param: CornerGlideParam, t: number, sampleRate: number): number {
	const g = _glides.get(param);
	if (g === undefined) return param.value;
	if (g.sampleRate !== sampleRate)
		throw new RangeError(`sampleRate changed mid-glide: ${g.sampleRate} -> ${sampleRate}`);
	const frame = Math.min(Math.max(Math.round((t - g.start) * sampleRate), 0), g.curve.length - 1);
	return g.curve[frame];
}

/** Glide `param` to `hz` from `now`, in `domain`, with time constant `tauS`. */
export function glideCorner(
	param: CornerGlideParam,
	hz: number,
	now: number,
	sampleRate: number,
	domain: FilterGlideDomain,
	tauS: number
): void {
	_assertPositive('hz', hz);
	_assertPositive('sampleRate', sampleRate);
	_assertPositive('tauS', tauS);
	switch (domain) {
		case 'hz':
			param.cancelScheduledValues(now);
			param.setTargetAtTime(hz, now, tauS);
			return;
		case 'octave': {
			const from = cornerHzAt(param, now, sampleRate);
			if (Math.abs(Math.log2(hz) - Math.log2(from)) < OCTAVE_GLIDE_SETTLE) return;
			const curve = octaveGlideCurve(from, hz, sampleRate, tauS);
			param.cancelScheduledValues(now);
			param.setValueCurveAtTime(Float32Array.from(curve), now, (curve.length - 1) / sampleRate);
			_glides.set(param, { curve, start: now, sampleRate });
			return;
		}
		default: {
			const _exhaustive: never = domain;
			throw new Error(`Unhandled filter glide domain: ${_exhaustive}`);
		}
	}
}
