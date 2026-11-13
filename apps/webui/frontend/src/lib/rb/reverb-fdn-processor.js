/**
 * FDN reverb kernel (FXVERB, issue #9817), as an AudioWorkletProcessor.
 *
 * Plain JS with no imports (a worklet scope cannot resolve them). Every
 * constant arrives in `processorOptions` from `reverb-fdn.ts`, and the
 * coefficient law in `_updateCoefficients` mirrors `reverbFdnLoopCoefficients`
 * there. Change one, change the other: the unit test compares them.
 *
 * Signal path per sample: stereo in -> pre-delay -> input gate (closes on
 * freeze) -> Hadamard column per channel into 8 lines -> each line read is
 * damped by its one-pole (gain = RT60 decay at 500 Hz, RT60 x damping at
 * 4 kHz) -> Householder reflection (v - 2/8 sum v) -> written back with the
 * input. Outputs are two other Hadamard rows of the damped reads, scaled to a
 * unit-energy impulse response, then by `wet`. The output is the wet return
 * only; the host graph keeps the dry.
 *
 * Freeze ramps over `rampSec`: the input gate closes and the loop crossfades
 * from the damped reads to the raw reads, so the loop is lossless (Householder
 * is orthogonal) and the tail holds. `wet` ramps linearly across each quantum.
 * `size` snaps to the size grid and swaps the prime length set (unramped);
 * `predelay` steps by whole samples (unramped).
 *
 * `process()` allocates nothing: every buffer is sized in the constructor.
 */

/* global AudioWorkletProcessor, registerProcessor, sampleRate */

const LINES = 8;
const OPTION_KEYS = [
	'maxPredelaySamples',
	'lowHz',
	'highHz',
	'rampSec',
	'denormalFlush',
	'outputTrim'
];
/** name, default, min, max: mirrors REVERB_FDN_PARAMS in reverb-fdn.ts. */
const PARAMS = [
	['rt60', 2.6, 0.3, 10],
	['size', 1, 0.5, 1.5],
	['predelay', 0.02, 0, 0.1],
	['damping', 0.5, 0.5, 1],
	['wet', 1, 0, 1],
	['freeze', 0, 0, 1]
];

function hadamardSign(row, col) {
	let bits = row & col;
	let sign = 1;
	for (; bits; bits &= bits - 1) sign = -sign;
	return sign;
}

function powerOfTwoAbove(n) {
	let p = 1;
	while (p < n + 1) p <<= 1;
	return p;
}

class ReverbFdnProcessor extends AudioWorkletProcessor {
	static get parameterDescriptors() {
		const rate = 'k-rate';
		return PARAMS.map(([name, defaultValue, minValue, maxValue]) => ({
			name,
			defaultValue,
			minValue,
			maxValue,
			automationRate: rate
		}));
	}

	constructor(options) {
		super();
		const o = options && options.processorOptions;
		if (!o) throw new RangeError('reverb processor needs processorOptions');
		for (const key of OPTION_KEYS) {
			if (!Number.isFinite(o[key])) throw new RangeError(`reverb option ${key} must be finite`);
		}
		const tableOk = Array.isArray(o.sizes) && Array.isArray(o.lengthTable);
		if (!tableOk || o.lengthTable.length !== o.sizes.length * LINES) {
			throw new RangeError(`reverb lengthTable needs ${LINES} lengths per size`);
		}
		this.sizes = Float64Array.from(o.sizes);
		this.lengthTable = Int32Array.from(o.lengthTable);
		const n = powerOfTwoAbove(Math.max(...this.lengthTable));
		this.lineSize = n;
		this.lineMask = n - 1;
		this.lines = new Float32Array(LINES * n);
		const p = powerOfTwoAbove(o.maxPredelaySamples);
		this.preMask = p - 1;
		this.maxPredelay = o.maxPredelaySamples;
		this.preL = new Float32Array(p);
		this.preR = new Float32Array(p);
		this.c1 = Math.cos((2 * Math.PI * o.lowHz) / sampleRate);
		this.c2 = Math.cos((2 * Math.PI * o.highHz) / sampleRate);
		this.flush = o.denormalFlush;
		this.trim = o.outputTrim;
		this.freezeStep = 1 / Math.max(1, Math.round(o.rampSec * sampleRate));
		const norm = 1 / Math.sqrt(LINES);
		// Each channel enters at -3 dB so a mono (L = R) input injects unit energy.
		const inject = norm * Math.SQRT1_2;
		this.inL = new Float64Array(LINES);
		this.inR = new Float64Array(LINES);
		this.outL = new Float64Array(LINES);
		this.outR = new Float64Array(LINES);
		for (let k = 0; k < LINES; k++) {
			this.inL[k] = hadamardSign(1, k) * inject;
			this.inR[k] = hadamardSign(2, k) * inject;
			this.outL[k] = hadamardSign(5, k) * norm;
			this.outR[k] = hadamardSign(6, k) * norm;
		}
		this.len = new Int32Array(LINES);
		this.b = new Float64Array(LINES);
		this.a = new Float64Array(LINES);
		this.lp = new Float64Array(LINES);
		this.v = new Float64Array(LINES);
		this.outputGain = 0;
		this.write = 0;
		this.preWrite = 0;
		this.freezeMix = 0;
		this.wet = -1;
		this.rt60 = -1;
		this.damping = -1;
		this.sizeIndex = -1;
	}

	_updateCoefficients(rt60, damping, sizeIndex) {
		let mean = 0;
		for (let k = 0; k < LINES; k++) {
			const length = this.lengthTable[sizeIndex * LINES + k];
			this.len[k] = length;
			mean += length / LINES;
			const low = Math.pow(10, (-3 * length) / (rt60 * sampleRate));
			const high = Math.pow(10, (-3 * length) / (rt60 * damping * sampleRate));
			const r = (low * low) / (high * high);
			let pole = 0;
			if (r - 1 > 1e-12) {
				const mid = (r * this.c1 - this.c2) / (r - 1);
				pole = mid - Math.sqrt(mid * mid - 1);
			}
			this.a[k] = pole;
			this.b[k] = low * Math.sqrt(1 - 2 * pole * this.c1 + pole * pole);
		}
		const g = Math.pow(10, (-3 * mean) / (rt60 * sampleRate));
		this.outputGain = Math.sqrt(LINES * (1 - g * g)) * this.trim;
		this.rt60 = rt60;
		this.damping = damping;
		this.sizeIndex = sizeIndex;
	}

	process(inputs, outputs, parameters) {
		const input = inputs[0];
		const output = outputs[0];
		const outL = output[0];
		const outR = output.length > 1 ? output[1] : null;
		const srcL = input.length > 0 ? input[0] : null;
		const srcR = input.length > 1 ? input[1] : srcL;
		const rt60 = parameters.rt60[0];
		const damping = parameters.damping[0];
		const sizes = this.sizes;
		let sizeIndex = Math.round((parameters.size[0] - sizes[0]) / (sizes[1] - sizes[0]));
		sizeIndex = Math.max(0, Math.min(sizes.length - 1, sizeIndex));
		if (rt60 !== this.rt60 || damping !== this.damping || sizeIndex !== this.sizeIndex) {
			this._updateCoefficients(rt60, damping, sizeIndex);
		}
		const predelay = Math.min(this.maxPredelay, Math.round(parameters.predelay[0] * sampleRate));
		const freezeTarget = parameters.freeze[0] >= 0.5 ? 1 : 0;
		const frames = outL.length;
		const wetTarget = parameters.wet[0];
		const wetStart = this.wet < 0 ? wetTarget : this.wet;
		const wetStep = (wetTarget - wetStart) / frames;
		const lines = this.lines;
		const size = this.lineSize;
		const mask = this.lineMask;
		const v = this.v;
		const lp = this.lp;
		const flush = this.flush;
		let write = this.write;
		let preWrite = this.preWrite;
		let mix = this.freezeMix;
		for (let i = 0; i < frames; i++) {
			if (mix < freezeTarget) mix = Math.min(freezeTarget, mix + this.freezeStep);
			else if (mix > freezeTarget) mix = Math.max(freezeTarget, mix - this.freezeStep);
			this.preL[preWrite] = srcL === null ? 0 : srcL[i];
			this.preR[preWrite] = srcR === null ? 0 : srcR[i];
			const readPre = (preWrite - predelay) & this.preMask;
			const gate = 1 - mix;
			const xL = this.preL[readPre] * gate;
			const xR = this.preR[readPre] * gate;
			preWrite = (preWrite + 1) & this.preMask;
			let sum = 0;
			let yL = 0;
			let yR = 0;
			for (let k = 0; k < LINES; k++) {
				const raw = lines[k * size + ((write - this.len[k]) & mask)];
				let damped = this.b[k] * raw + this.a[k] * lp[k];
				if (damped < flush && damped > -flush) damped = 0;
				lp[k] = damped;
				const value = damped + (raw - damped) * mix;
				v[k] = value;
				sum += value;
				yL += this.outL[k] * value;
				yR += this.outR[k] * value;
			}
			const reflect = (2 / LINES) * sum;
			for (let k = 0; k < LINES; k++) {
				let next = v[k] - reflect + this.inL[k] * xL + this.inR[k] * xR;
				if (next < flush && next > -flush) next = 0;
				lines[k * size + write] = next;
			}
			write = (write + 1) & mask;
			const level = (wetStart + wetStep * (i + 1)) * this.outputGain;
			outL[i] = yL * level;
			if (outR !== null) outR[i] = yR * level;
		}
		this.write = write;
		this.preWrite = preWrite;
		this.freezeMix = mix;
		this.wet = wetTarget;
		return true;
	}
}

registerProcessor('mdt-reverb-fdn', ReverbFdnProcessor);
