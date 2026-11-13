/**
 * REVERSE DELAY beat FX kernel (FXECHO, issue #10006), as an AudioWorkletProcessor.
 *
 * Each window of the input, `timeSec` long, plays back reversed during the next
 * window, so the reversed repeat ends on the boundary. Feedback 0 gives the single
 * reversed repeat the DJM-800 documents (Operating Instructions, Types of beat effects,
 * p. 14, mimicked); above 0 the loop re-captures the repeat, so repeats alternate
 * reversed and forward. Wet only: the caller's dry path stays at 0 samples latency.
 *
 * Window law: the boundary grid starts where the current `timeSec` took effect (the
 * first quantum, then each boundary where a new value lands). A time change waits for
 * the next boundary, never jumps mid-grain. Each grain carries a raised-cosine edge
 * (`REVERSE_GRAIN_EDGE_SEC`, halved on windows shorter than two edges). The loop sum
 * passes the #9813 echo soft clip (identity below the knee, tanh to the ceiling, both
 * from `processorOptions`), so no output sample can exceed the ceiling.
 *
 * Plain JS with no imports: a worklet scope cannot resolve them. The 4.0 s stereo ring
 * and the one-window scratch are allocated in the constructor; `process()` allocates
 * nothing. A parameter outside its range is refused through the port by name and the
 * last accepted value holds; Web Audio's own descriptor clamp is not used.
 */

/* global AudioWorkletProcessor, registerProcessor, sampleRate */

export const REVERSE_DELAY_PROCESSOR_NAME = 'mdt-reverse-delay';
export const REVERSE_GRAIN_EDGE_SEC = 0.005;
export const REVERSE_DELAY_REFUSAL = 'REVERSE_DELAY_PARAM_OUT_OF_RANGE';
export const REVERSE_DELAY_PARAMS = Object.freeze({
	timeSec: Object.freeze({ min: 0.0027, max: 4.0, default: 0.5 }),
	feedback: Object.freeze({ min: 0, max: 0.95, default: 0 }),
	wet: Object.freeze({ min: 0, max: 1, default: 1 })
});
const PARAM_NAMES = ['timeSec', 'feedback', 'wet'];
const OPTION_KEYS = ['clipKnee', 'clipCeiling', 'denormalFlush'];

function _windowError(len, ringLength, outLength) {
	return new RangeError(`reverseWindow len ${len} exceeds ring ${ringLength} or out ${outLength}`);
}

/** Write ring[start .. start + len) into out[0 .. len) in reverse order, wrapping the ring. */
export function reverseWindow(ring, start, len, out) {
	const n = ring.length;
	if (len > n || len > out.length) throw _windowError(len, n, out.length);
	for (let j = 0; j < len; j++) out[j] = ring[(start + len - 1 - j) % n];
}

/** The #9813 loop soft clip: identity at or below `knee`, tanh knee toward `ceiling` above it. */
export function reverseDelaySoftClip(x, knee, ceiling) {
	const mag = x < 0 ? -x : x;
	if (mag <= knee) return x;
	const span = ceiling - knee;
	const y = knee + span * Math.tanh((mag - knee) / span);
	return x < 0 ? -y : y;
}

class ReverseDelayProcessor extends AudioWorkletProcessor {
	static get parameterDescriptors() {
		return PARAM_NAMES.map((name) => ({
			name,
			defaultValue: REVERSE_DELAY_PARAMS[name].default,
			automationRate: 'k-rate'
		}));
	}

	constructor(options) {
		super();
		const o = options && options.processorOptions;
		if (!o) throw new RangeError('reverse delay processor needs processorOptions');
		for (const key of OPTION_KEYS) {
			if (typeof o[key] !== 'number' || !Number.isFinite(o[key]) || o[key] <= 0) {
				throw new RangeError(`reverse delay option ${key} must be > 0; got ${String(o[key])}`);
			}
		}
		if (o.clipKnee >= o.clipCeiling)
			throw new RangeError('reverse delay clipKnee must be < clipCeiling');
		this.knee = o.clipKnee;
		this.ceiling = o.clipCeiling;
		this.flush = o.denormalFlush;
		const ringLength = Math.ceil(REVERSE_DELAY_PARAMS.timeSec.max * sampleRate);
		this.ring = [new Float32Array(ringLength), new Float32Array(ringLength)];
		this.scratch = [new Float32Array(ringLength), new Float32Array(ringLength)];
		this.edgeMax = Math.round(REVERSE_GRAIN_EDGE_SEC * sampleRate);
		this.refusal = {
			type: 'refusal',
			reason: REVERSE_DELAY_REFUSAL,
			param: '',
			value: 0,
			min: 0,
			max: 0
		};
		this.seen = { timeSec: NaN, feedback: NaN, wet: NaN };
		this.accepted = { timeSec: REVERSE_DELAY_PARAMS.timeSec.default, feedback: 0, wet: 1 };
		this.write = 0;
		this.windowStart = 0;
		this.len = 0;
		this.pos = 0;
		this.grain = 0;
		this.edge = 0;
		this.feedback = 0;
		this.wet = 1;
	}

	_readParams(parameters) {
		for (let p = 0; p < PARAM_NAMES.length; p++) {
			const name = PARAM_NAMES[p];
			const value = parameters[name][0];
			if (Object.is(value, this.seen[name])) continue;
			this.seen[name] = value;
			const range = REVERSE_DELAY_PARAMS[name];
			if (value >= range.min && value <= range.max) this.accepted[name] = value;
			else this._refuse(name, value, range);
		}
	}

	_refuse(name, value, range) {
		const r = this.refusal;
		r.param = name;
		r.value = value;
		r.min = range.min;
		r.max = range.max;
		this.port.postMessage(r);
	}

	_boundary() {
		reverseWindow(this.ring[0], this.windowStart, this.len, this.scratch[0]);
		reverseWindow(this.ring[1], this.windowStart, this.len, this.scratch[1]);
		const captured = this.len;
		this.len = Math.max(1, Math.round(this.accepted.timeSec * sampleRate));
		this.grain = Math.min(captured, this.len);
		this.edge = Math.min(this.edgeMax, Math.floor(this.grain / 2));
		this.windowStart = this.write;
		this.pos = 0;
	}

	_edgeGain(j) {
		const e = this.edge;
		const k = j < e ? j : this.grain - 1 - j;
		return k >= e ? 1 : 0.5 - 0.5 * Math.cos((Math.PI * (k + 0.5)) / e);
	}

	process(inputs, outputs, parameters) {
		this._readParams(parameters);
		const input = inputs[0];
		const outL = outputs[0][0];
		const outR = outputs[0][1];
		const inL = input && input.length > 0 ? input[0] : null;
		const inR = input && input.length > 1 ? input[1] : null;
		const ringL = this.ring[0];
		const ringR = this.ring[1];
		const scratchL = this.scratch[0];
		const scratchR = this.scratch[1];
		const frames = outL.length;
		const fbStep = (this.accepted.feedback - this.feedback) / frames;
		const wetStep = (this.accepted.wet - this.wet) / frames;
		const ringLength = ringL.length;
		if (this.len === 0) {
			this.len = Math.max(1, Math.round(this.accepted.timeSec * sampleRate));
			this.windowStart = this.write;
		}
		for (let i = 0; i < frames; i++) {
			if (this.pos === this.len) this._boundary();
			const j = this.pos;
			let pl = 0;
			let pr = 0;
			if (j < this.grain) {
				const g = this._edgeGain(j);
				pl = scratchL[j] * g;
				pr = scratchR[j] * g;
			}
			const fb = this.feedback + fbStep * (i + 1);
			const wet = this.wet + wetStep * (i + 1);
			let wl = reverseDelaySoftClip((inL === null ? 0 : inL[i]) + fb * pl, this.knee, this.ceiling);
			let wr = reverseDelaySoftClip((inR === null ? 0 : inR[i]) + fb * pr, this.knee, this.ceiling);
			if ((wl < 0 ? -wl : wl) < this.flush) wl = 0;
			if ((wr < 0 ? -wr : wr) < this.flush) wr = 0;
			ringL[this.write] = wl;
			ringR[this.write] = wr;
			outL[i] = wet * pl;
			if (outR !== undefined) outR[i] = wet * pr;
			this.write = this.write + 1 === ringLength ? 0 : this.write + 1;
			this.pos = j + 1;
		}
		this.feedback = this.accepted.feedback;
		this.wet = this.accepted.wet;
		return true;
	}
}

registerProcessor(REVERSE_DELAY_PROCESSOR_NAME, ReverseDelayProcessor);
