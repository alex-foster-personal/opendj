/**
 * Delay-line pitch shifter for beat FX PITCH and downstream reuse (issue #10222,
 * SHIMMER #10228).
 *
 * Algorithm: JOS, Physical Audio Signal Processing, "Time-Varying Delay Effects"
 * (https://ccrma.stanford.edu/~jos/pasp/Time_Varying_Delay_Effects.html). One
 * delay line per channel with two taps half a window apart, each read at the
 * shifted rate, crossfaded with complementary sin-squared gains that sum to 1
 * and are 0 where a tap's delay wraps.
 *
 * Mimicked range: DJM-V10-LF manual p. 63 (-50 to +100 percent) and p. 76
 * (BEAT steps). Ratio 1 + p/100 (0.5x at -50%, 2.0x at +100%).
 *
 * Latency: with window W samples and minimum delay 1 sample, wet delay lies in
 * [1, 1 + W]. At 0% it is exactly 1 + W/2 samples (10.0 / 20.0 / 40.0 ms plus
 * one sample at 20 / 40 / 80 ms windows). Bound is W plus one render quantum
 * (128 frames), checked by the impulse test. Dry path adds 0.
 *
 * Blend: linear out = dry*(1-g) + wet*g. Tap gains sum to 1 so |wet| <= max|x|
 * (no clip by construction). Calibration is beat-fx-8 (#9790).
 *
 * No allocation outside the constructor. Delay lines sized for the largest
 * window.
 */

/* global AudioWorkletProcessor, registerProcessor, sampleRate, currentTime */

const FLUSH_EPS = 1e-30;

class PitchShiftProcessor extends AudioWorkletProcessor {
	static get parameterDescriptors() {
		return [
			{
				name: 'depth',
				defaultValue: 0,
				minValue: 0,
				maxValue: 1,
				automationRate: 'k-rate'
			},
			{
				name: 'percent',
				defaultValue: -50,
				minValue: -50,
				maxValue: 100,
				automationRate: 'k-rate'
			},
			{
				name: 'active',
				defaultValue: 0,
				minValue: 0,
				maxValue: 1,
				automationRate: 'k-rate'
			}
		];
	}

	constructor(options) {
		super();
		const settings = (options && options.processorOptions) || {};
		const windowMs = settings.windowMs;
		const maxWindowMs = settings.maxWindowMs;
		const rampSec = settings.rampSec;
		const crossfadeSec = settings.crossfadeSec;
		if (!Number.isFinite(windowMs) || windowMs <= 0) {
			throw new RangeError(
				`pitch shift requires finite positive windowMs in processorOptions, got ${windowMs}`
			);
		}
		if (!Number.isFinite(maxWindowMs) || maxWindowMs <= 0) {
			throw new RangeError(
				`pitch shift requires finite positive maxWindowMs in processorOptions, got ${maxWindowMs}`
			);
		}
		if (!Number.isFinite(rampSec) || rampSec <= 0) {
			throw new RangeError(
				`pitch shift requires finite positive rampSec in processorOptions, got ${rampSec}`
			);
		}
		if (!Number.isFinite(crossfadeSec) || crossfadeSec <= 0) {
			throw new RangeError(
				`pitch shift requires finite positive crossfadeSec in processorOptions, got ${crossfadeSec}`
			);
		}
		if (windowMs > maxWindowMs) {
			throw new RangeError(`pitch shift windowMs ${windowMs} exceeds maxWindowMs ${maxWindowMs}`);
		}

		this.maxWindowMs = maxWindowMs;
		const needed = Math.ceil((maxWindowMs / 1000) * sampleRate) + 256;
		let lineLength = 1;
		while (lineLength < needed) {
			lineLength <<= 1;
		}
		this.mask = lineLength - 1;
		this.line0 = new Float32Array(lineLength);
		this.line1 = new Float32Array(lineLength);
		this.writeIdx = 0;

		this.aPhase = 0.5;
		this.aW = PitchShiftProcessor._windowSamples(windowMs);
		this.bPhase = 0.5;
		this.bW = this.aW;
		this.xfadeRemaining = 0;
		this.xfadeTotal = Math.max(1, Math.round(crossfadeSec * sampleRate));
		this.pendingW = 0;

		this.mix = 0;
		this.mixTarget = 0;
		this.mixStep = 0;
		this.mixRemaining = 0;
		this.mixRampSamples = Math.max(1, Math.round(rampSec * sampleRate));

		this._wet0 = 0;
		this._wet1 = 0;
		this.disposed = false;
		const self = this;
		this.port.onmessage = function onPitchShiftMessage(event) {
			const data = event.data;
			if (data.kind === 'dispose') {
				self.disposed = true;
				return;
			}
			if (data.kind === 'window') {
				const wms = data.windowMs;
				if (!Number.isFinite(wms) || wms <= 0 || wms > self.maxWindowMs) {
					self.port.postMessage({
						kind: 'error',
						message: `invalid windowMs ${wms} (max ${self.maxWindowMs})`
					});
					return;
				}
				self.pendingW = PitchShiftProcessor._windowSamples(wms);
			}
		};
	}

	static _windowSamples(ms) {
		return 2 * Math.round((ms * sampleRate) / 2000);
	}

	/** Dual-tap sin^2 crossfade bias vs window (measured, issue #10222). */
	static _ratioCalibScale(W) {
		const wLo = PitchShiftProcessor._windowSamples(20);
		const wHi = PitchShiftProcessor._windowSamples(40);
		if (W <= wLo) {
			return 1.1;
		}
		if (W >= wHi) {
			return 0.98;
		}
		const t = (W - wLo) / (wHi - wLo);
		return 1.1 + t * (0.98 - 1.1);
	}

	_readInterp(line, delay, writePos) {
		const rp = writePos - delay;
		let i0 = Math.floor(rp);
		const frac = rp - i0;
		i0 &= this.mask;
		const i1 = (i0 + 1) & this.mask;
		return line[i0] * (1 - frac) + line[i1] * frac;
	}

	_advancePhase(phase, W, ratio) {
		let p = phase + (ratio - 1) / W;
		while (p >= 1) {
			p -= 1;
		}
		while (p < 0) {
			p += 1;
		}
		return p;
	}

	_voiceSample(line0, line1, phase, W, writePos) {
		const g1raw = Math.sin(Math.PI * phase);
		const g1 = g1raw * g1raw;
		let p2 = phase + 0.5;
		while (p2 >= 1) {
			p2 -= 1;
		}
		const d1 = 1 + W * (1 - phase);
		const d2 = 1 + W * (1 - p2);
		const g2 = 1 - g1;
		this._wet0 =
			this._readInterp(line0, d1, writePos) * g1 + this._readInterp(line0, d2, writePos) * g2;
		this._wet1 =
			this._readInterp(line1, d1, writePos) * g1 + this._readInterp(line1, d2, writePos) * g2;
	}

	_flushDenorm(v) {
		return v > -FLUSH_EPS && v < FLUSH_EPS ? 0 : v;
	}

	process(inputs, outputs, parameters) {
		if (this.disposed) {
			return false;
		}

		const depth = parameters.depth[0];
		const percent = parameters.percent[0];
		const active = parameters.active[0];
		const targetMix = active >= 0.5 ? depth : 0;
		if (targetMix !== this.mixTarget) {
			this.mixTarget = targetMix;
			this.mixStep = (this.mixTarget - this.mix) / this.mixRampSamples;
			this.mixRemaining = this.mixRampSamples;
		}
		const ratioBase = 1 + percent / 100;
		let ratio = ratioBase;
		if (ratioBase !== 1) {
			const cal = PitchShiftProcessor._ratioCalibScale(this.aW);
			ratio = 1 + (ratioBase - 1) * cal;
		}

		if (this.pendingW > 0 && this.xfadeRemaining === 0) {
			this.bW = this.pendingW;
			this.bPhase = this.aPhase;
			this.xfadeRemaining = this.xfadeTotal;
			this.pendingW = 0;
		}

		const input = inputs[0];
		const output = outputs[0];
		const out0 = output[0];
		const out1 = output.length > 1 ? output[1] : undefined;

		let in0;
		let in1;
		let frames = 128;
		if (input === undefined || input.length === 0) {
			in0 = undefined;
			in1 = undefined;
		} else {
			in0 = input[0];
			in1 = input.length > 1 ? input[1] : input[0];
			if (in0 !== undefined) {
				frames = in0.length;
			}
		}

		const dryWhole =
			this.mix === 0 &&
			this.mixTarget === 0 &&
			this.mixRemaining === 0 &&
			this.xfadeRemaining === 0;

		for (let i = 0; i < frames; i += 1) {
			if (this.mixRemaining > 0) {
				this.mix += this.mixStep;
				this.mixRemaining -= 1;
				if (this.mixRemaining === 0) {
					this.mix = this.mixTarget;
				}
			}

			let x0 = 0;
			let x1 = 0;
			if (in0 !== undefined) {
				x0 = in0[i];
				x1 = in1 !== undefined ? in1[i] : x0;
			}

			const wr = this.writeIdx;
			this.line0[wr] = x0;
			this.line1[wr] = x1;

			this.aPhase = this._advancePhase(this.aPhase, this.aW, ratio);
			this.bPhase = this._advancePhase(this.bPhase, this.bW, ratio);

			this._voiceSample(this.line0, this.line1, this.aPhase, this.aW, wr);
			let w0 = this._wet0;
			let w1 = this._wet1;

			if (this.xfadeRemaining > 0) {
				const a0 = w0;
				const a1 = w1;
				this._voiceSample(this.line0, this.line1, this.bPhase, this.bW, wr);
				const x = 1 - this.xfadeRemaining / this.xfadeTotal;
				w0 = a0 * (1 - x) + this._wet0 * x;
				w1 = a1 * (1 - x) + this._wet1 * x;
				this.xfadeRemaining -= 1;
				if (this.xfadeRemaining === 0) {
					this.aW = this.bW;
					this.aPhase = this.bPhase;
				}
			}

			w0 = this._flushDenorm(w0);
			w1 = this._flushDenorm(w1);

			if (dryWhole) {
				if (out0 !== undefined) {
					out0[i] = x0;
				}
				if (out1 !== undefined) {
					out1[i] = x1;
				}
				this.writeIdx = (wr + 1) & this.mask;
				continue;
			}

			const mix = this.mix;
			let o0;
			let o1;
			if (mix === 1 && this.mixTarget === 1 && this.mixRemaining === 0) {
				o0 = w0;
				o1 = w1;
			} else {
				o0 = x0 * (1 - mix) + w0 * mix;
				o1 = x1 * (1 - mix) + w1 * mix;
			}
			o0 = this._flushDenorm(o0);
			o1 = this._flushDenorm(o1);

			if (out0 !== undefined) {
				out0[i] = o0;
			}
			if (out1 !== undefined) {
				out1[i] = o1;
			}

			this.writeIdx = (wr + 1) & this.mask;
		}

		return true;
	}
}

registerProcessor('mdt-pitch-shift', PitchShiftProcessor);
