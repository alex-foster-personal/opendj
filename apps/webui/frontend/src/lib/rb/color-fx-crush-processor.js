/**
 * CRUSH color FX kernel (SNDCOL-10655), as an AudioWorkletProcessor.
 *
 * Runs inside AudioWorkletGlobalScope, so this file is deliberately plain: no
 * imports (a worklet scope cannot resolve them), no TypeScript. Same split as
 * `meter-processor.js`: every constant arrives in `processorOptions` from
 * `color-fx-crush.ts`, which also holds a pure mirror of this arithmetic that
 * the unit test compares sample for sample. Change one, change the other.
 *
 * Decimation needs state across samples, which is why CRUSH is a worklet and
 * not a native node: ONE fractional phase accumulator (shared by both channels,
 * so the stereo image holds) clocks a sample-and-hold, and each channel keeps
 * its OWN held value, so a left-only input leaves the right output silent. The
 * held value is quantised round-to-nearest to the current bit depth. Bits and
 * decimation follow their targets on a one-pole smoother per sample, so a knob
 * move never steps. No make-up gain: round-to-nearest plus sample-and-hold has
 * no systematic level loss (asserted by the unit test).
 *
 * The arithmetic is re-expressed from parked PR #492's `kernels/crush.ts`. Its
 * shape is corroborated by the Apache-2.0 web-audio-samples bit-crusher; Mixxx
 * and musicdsp were behaviour references only, nothing is copied from them.
 *
 * `process()` allocates nothing: the held values are sized in the constructor
 * and everything else is a scalar.
 */

/* global AudioWorkletProcessor, registerProcessor, sampleRate */

const TAPERS = { exponential: 0, steep: 1, late: 2 };
const OPTION_KEYS = [
	'maxBits',
	'minBits',
	'minDecimation',
	'maxDecimation',
	'cwFixedAmount',
	'parameterNominal',
	'controlSmoothSec',
	'denormalFlush'
];

class ColorFxCrushProcessor extends AudioWorkletProcessor {
	static get parameterDescriptors() {
		return [
			{ name: 'travel', defaultValue: 0, minValue: -1, maxValue: 1, automationRate: 'a-rate' },
			{ name: 'parameter', defaultValue: 0.5, minValue: 0, maxValue: 1, automationRate: 'k-rate' }
		];
	}

	constructor(options) {
		super();
		const o = options && options.processorOptions;
		if (!o) throw new RangeError('crush processor needs processorOptions');
		for (const key of OPTION_KEYS) {
			if (typeof o[key] !== 'number' || !Number.isFinite(o[key])) {
				throw new RangeError(
					`crush processor option ${key} must be a finite number; got ${String(o[key])}`
				);
			}
		}
		if (o.minDecimation < 1)
			throw new RangeError(
				`crush processor option minDecimation must be >= 1; got ${o.minDecimation}`
			);
		if (o.minBits <= 1)
			throw new RangeError(`crush processor option minBits must be > 1; got ${o.minBits}`);
		if (!(o.taper in TAPERS))
			throw new RangeError(`crush processor option taper is unknown: ${String(o.taper)}`);
		this.maxBits = o.maxBits;
		this.minBits = o.minBits;
		this.minDecimation = o.minDecimation;
		this.decimationRatio = o.maxDecimation / o.minDecimation;
		this.cwFixedAmount = o.cwFixedAmount;
		this.parameterNominal = o.parameterNominal;
		this.denormalFlush = o.denormalFlush;
		this.taper = TAPERS[o.taper];
		this.smoothRate = 1 - Math.exp(-1 / (o.controlSmoothSec * sampleRate));
		this.held = new Float64Array(2);
		this.phase = 0;
		this.decimation = o.minDecimation;
		this.bits = o.maxBits;
	}

	process(inputs, outputs, parameters) {
		const input = inputs[0];
		const output = outputs[0];
		const outL = output[0];
		const outR = output[1];
		const inL = input.length > 0 ? input[0] : null;
		const inR = input.length > 1 ? input[1] : null;
		const travel = parameters.travel;
		const constantTravel = travel.length === 1;
		const scale = parameters.parameter[0] / this.parameterNominal;
		const held = this.held;
		const rate = this.smoothRate;
		const frames = outL.length;
		let phase = this.phase;
		let decimation = this.decimation;
		let bits = this.bits;
		for (let i = 0; i < frames; i++) {
			const t = constantTravel ? travel[0] : travel[i];
			const base = t < 0 ? -t : t > 0 ? this.cwFixedAmount : 0;
			let amount = base * scale;
			if (amount > 1) amount = 1;
			const warped =
				this.taper === 0 ? amount : this.taper === 1 ? Math.pow(amount, 0.5) : amount * amount;
			const decimationTarget = this.minDecimation * Math.pow(this.decimationRatio, warped);
			const bitsTarget = this.maxBits + amount * (this.minBits - this.maxBits);
			decimation += (decimationTarget - decimation) * rate;
			bits += (bitsTarget - bits) * rate;
			const step = 2 / (Math.pow(2, bits) - 1);
			phase += 1;
			if (phase >= decimation) {
				phase -= decimation;
				held[0] = inL === null ? 0 : inL[i];
				held[1] = inR === null ? 0 : inR[i];
			}
			outL[i] = Math.round(held[0] / step) * step;
			if (outR !== undefined) outR[i] = Math.round(held[1] / step) * step;
		}
		if (Math.abs(held[0]) < this.denormalFlush) held[0] = 0;
		if (Math.abs(held[1]) < this.denormalFlush) held[1] = 0;
		this.phase = phase;
		this.decimation = decimation;
		this.bits = bits;
		return true;
	}
}

registerProcessor('mdt-color-fx-crush', ColorFxCrushProcessor);
