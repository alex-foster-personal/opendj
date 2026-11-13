/**
 * Mod FX kernels (FXMOD) as ONE AudioWorkletProcessor, `mdt-mod-fx`.
 *
 * Runs inside AudioWorkletGlobalScope, so this file is deliberately plain: no
 * imports, no TypeScript. Every number arrives in `processorOptions` from
 * `mod-fx-graph.ts`. Why a worklet: a native DelayNode inside a feedback cycle
 * is clamped to one 128-frame quantum (about 2.7 ms), and a flanger lives below
 * that (spec `(private path)` s.2).
 *
 * The shell here is shared by every kernel: the beat-locked LFO (phase from the
 * anchor posted by `mod-fx.svelte.ts`, a mirror of `lfoPhase` in `mod-fx-lfo.ts`,
 * change one, change the other), the level ramp, the re-anchor crossfade and the
 * dry/wet law. A kernel is one entry in `KERNELS`: its option names, `init` (all
 * allocation, constructor only), `tick` (one stereo frame, allocates nothing) and
 * optional hooks: `endBlock` (once per render quantum) and `fork` + `tickOld`, which
 * make a re-anchor crossfade the wet output of the old and new phase instead of
 * gliding the LFO value. A gate kernel (TRANS) has `gain` instead of `tick`: one
 * output gain per frame for both channels, from the phase and the ramped level,
 * crossfaded old to new on a re-anchor. It owns its depth law, so the 50/50 law
 * below does not apply to it; level 0 is still the dry input, bit for bit.
 *
 * Level law: level 0 is the dry input, bit for bit; level 1 is 50/50 dry/wet at
 * the full sweep. The level ramps linearly (`rampSec`) toward depth while active
 * and anchored, else toward 0. A seek re-anchor glides the LFO value from the old
 * phase to the new one over `crossfadeSec` (or, with `fork`, crossfades the wet
 * of both phases: a fast phaser glide sweeps its notches audibly), so nothing steps.
 */

/* global AudioWorkletProcessor, registerProcessor, sampleRate, currentTime */

const SHELL_OPTION_KEYS = ['rampSec', 'crossfadeSec', 'spreadDeg', 'denormalFlush'];

//----------------------------------------------------------------------------- LFO

/** Phase in [0, 1) of `anchor` at context time `t`; 0 is the sweep's trough. */
function anchorPhase(anchor, t) {
	const elapsed = t - anchor.contextTime;
	const cycles =
		anchor.kind === 'beat'
			? (anchor.beatIndex + elapsed * anchor.beatsPerSec - anchor.cycleOriginBeat) /
				anchor.cycleBeats
			: elapsed / anchor.periodSec;
	return cycles - Math.floor(cycles);
}

/** LFO cycles advanced per frame under `anchor`. */
function anchorRate(anchor) {
	return anchor.kind === 'beat'
		? anchor.beatsPerSec / (anchor.cycleBeats * sampleRate)
		: 1 / (anchor.periodSec * sampleRate);
}

/** Triangle 0 -> 1 -> 0 over one cycle; constant sweep speed. */
function triangle(phase) {
	const p = phase - Math.floor(phase);
	return p < 0.5 ? 2 * p : 2 - 2 * p;
}

const ANCHOR_KEYS = {
	beat: ['contextTime', 'beatIndex', 'beatsPerSec', 'cycleOriginBeat', 'cycleBeats'],
	time: ['contextTime', 'periodSec']
};

/** Throw naming the first of `keys` on `obj` that is not a finite number. */
function assertFinite(what, obj, keys) {
	if (!keys) throw new RangeError(`${what} is unknown`);
	for (const key of keys) {
		if (typeof obj[key] !== 'number' || !Number.isFinite(obj[key]))
			throw new RangeError(`${what} ${key} must be a finite number; got ${String(obj[key])}`);
	}
}

//----------------------------------------------------------------------------- kernels

/** One channel through the phaser's allpass cascade at `sweep`; `s` holds stage state, `last` the fed-back output. */
function allpassCascade(p, s, last, ch, x, sweep) {
	const t = Math.tan(p.wMin * Math.exp(p.logSpan * sweep));
	const a = (t - 1) / (t + 1);
	const base = ch * p.stages;
	let v = p.loopGain * x + p.feedback * last[ch];
	for (let i = base; i < base + p.stages; i++) {
		const y = a * v + s[i];
		s[i] = v - a * y;
		v = y;
	}
	if (v < p.flush && v > -p.flush) v = 0;
	last[ch] = v;
	return v;
}

/** Zero every value of `arr` with magnitude under `floor`. */
function flushBelow(arr, floor) {
	for (let i = 0; i < arr.length; i++) if (arr[i] < floor && arr[i] > -floor) arr[i] = 0;
}

//----------------------------------------------------------------------------- gate

/** One edge is at most a quarter of the cycle (TRANS_EDGE_MAX_FRAC in trans-gate.ts). */
const TRANS_EDGE_MAX_FRAC = 0.25;

/** Mirror of `transGain` in trans-gate.ts (change one, change the other): open [0, duty), raised-cosine edges. */
function transGateGain(phase, duty, edgeFrac) {
	if (phase < duty) return 1;
	const edge = Math.min(edgeFrac, (1 - duty) / 2);
	const fallEnd = duty + edge;
	if (phase < fallEnd) return 0.5 * (1 + Math.cos((Math.PI * (phase - duty)) / edge));
	const riseStart = 1 - edge;
	if (phase < riseStart) return 0;
	return 0.5 * (1 - Math.cos((Math.PI * (phase - riseStart)) / edge));
}

const TRANS_DEPTH_LAWS = ['balance', 'balance_then_duty'];

const TRANS_KERNEL = {
	options: ['duty', 'edgeSec'],
	init(o) {
		if (!(o.duty >= 0 && o.duty <= 1))
			throw new RangeError(`trans duty must be in 0..1; got ${o.duty}`);
		if (!(o.edgeSec > 0)) throw new RangeError(`trans edgeSec must be > 0; got ${o.edgeSec}`);
		if (!TRANS_DEPTH_LAWS.includes(o.depthLaw))
			throw new RangeError(
				`trans depthLaw must be one of ${TRANS_DEPTH_LAWS.join(', ')}; got ${String(o.depthLaw)}`
			);
		return { duty: o.duty, edgeSec: o.edgeSec, thenDuty: o.depthLaw === 'balance_then_duty' };
	},
	/** Output gain at LFO `phase` and knob `level`, the depth law of `transMix` (trans-gate.ts); `rate` sizes the edge. */
	gain(g, phase, level, rate) {
		let wet = level;
		let duty = g.duty;
		if (g.thenDuty && level <= 0.5) wet = level * 2;
		else if (g.thenDuty) {
			wet = 1;
			duty = g.duty * (1 - (level - 0.5));
		}
		const edgeFrac = Math.min(g.edgeSec * rate * sampleRate, TRANS_EDGE_MAX_FRAC);
		return 1 - wet * (1 - transGateGain(phase - Math.floor(phase), duty, edgeFrac));
	}
};

const KERNELS = {
	flanger: {
		options: ['minDelaySec', 'maxDelaySec', 'headroomFrames', 'feedback'],
		init(o) {
			if (!(o.minDelaySec > 0 && o.maxDelaySec > o.minDelaySec))
				throw new RangeError('flanger needs 0 < minDelaySec < maxDelaySec');
			if (!(Math.abs(o.feedback) < 1))
				throw new RangeError(`flanger feedback must be in (-1, 1); got ${o.feedback}`);
			const len = Math.ceil(o.maxDelaySec * sampleRate) + o.headroomFrames;
			return {
				buf: new Float64Array(2 * len),
				len,
				write: 0,
				minDelay: o.minDelaySec * sampleRate,
				range: (o.maxDelaySec - o.minDelaySec) * sampleRate,
				feedback: o.feedback,
				// (1 - |fb|) into the loop keeps the wet path's L1 gain at 1, so it never exceeds the input peak.
				loopGain: 1 - Math.abs(o.feedback),
				flush: o.denormalFlush
			};
		},
		/** Swept fractional delay with feedback; `sweepL/R` in [0, 1] map to minDelay..maxDelay. */
		tick(k, xL, xR, sweepL, sweepR, wet) {
			for (let ch = 0; ch < 2; ch++) {
				const base = ch * k.len;
				let read = k.write + k.len - k.minDelay - (ch === 0 ? sweepL : sweepR) * k.range;
				if (read >= k.len) read -= k.len;
				const i0 = Math.floor(read);
				const i1 = i0 + 1 === k.len ? 0 : i0 + 1;
				const frac = read - i0;
				const y = k.buf[base + i0] * (1 - frac) + k.buf[base + i1] * frac;
				let v = k.loopGain * (ch === 0 ? xL : xR) + k.feedback * y;
				if (v < k.flush && v > -k.flush) v = 0;
				k.buf[base + k.write] = v;
				wet[ch] = y;
			}
			k.write = k.write + 1 === k.len ? 0 : k.write + 1;
		}
	},
	phaser: {
		options: ['stages', 'minHz', 'maxHz', 'feedback'],
		init(o) {
			if (o.stages !== 4 && o.stages !== 8)
				throw new RangeError(`phaser stages must be 4 or 8; got ${o.stages}`);
			if (!(o.minHz > 0 && o.maxHz > o.minHz && o.maxHz < sampleRate / 2))
				throw new RangeError('phaser needs 0 < minHz < maxHz < Nyquist');
			if (!(Math.abs(o.feedback) < 1))
				throw new RangeError(`phaser feedback must be in (-1, 1); got ${o.feedback}`);
			return {
				stages: o.stages,
				state: new Float64Array(2 * o.stages),
				last: new Float64Array(2),
				ghost: new Float64Array(2 * o.stages),
				ghostLast: new Float64Array(2),
				wMin: (Math.PI * o.minHz) / sampleRate,
				logSpan: Math.log(o.maxHz / o.minHz),
				feedback: o.feedback,
				// Same law as the flanger: (1 - |fb|) in keeps the resonant peaks at unity gain.
				loopGain: 1 - Math.abs(o.feedback),
				flush: o.denormalFlush
			};
		},
		/** Cascade of first-order allpasses; break frequency minHz * (maxHz/minHz)^sweep, last output fed back. */
		tick(p, xL, xR, sweepL, sweepR, wet) {
			wet[0] = allpassCascade(p, p.state, p.last, 0, xL, sweepL);
			wet[1] = allpassCascade(p, p.state, p.last, 1, xR, sweepR);
		},
		/** Re-anchor: the ghost carries on at the old phase while the shell fades its wet out. */
		fork(p) {
			p.ghost.set(p.state);
			p.ghostLast.set(p.last);
		},
		tickOld(p, xL, xR, sweepL, sweepR, wet) {
			wet[0] = allpassCascade(p, p.ghost, p.ghostLast, 0, xL, sweepL);
			wet[1] = allpassCascade(p, p.ghost, p.ghostLast, 1, xR, sweepR);
		},
		/** Once per render quantum: zero allpass and feedback state below the flush floor. */
		endBlock(p) {
			flushBelow(p.state, p.flush);
			flushBelow(p.last, p.flush);
			flushBelow(p.ghost, p.flush);
			flushBelow(p.ghostLast, p.flush);
		}
	},
	trans: TRANS_KERNEL
};

//----------------------------------------------------------------------------- processor

class ModFxProcessor extends AudioWorkletProcessor {
	static get parameterDescriptors() {
		return [{ name: 'depth', defaultValue: 0, minValue: 0, maxValue: 1, automationRate: 'k-rate' }];
	}

	constructor(options) {
		super();
		const o = options && options.processorOptions;
		if (!o) throw new RangeError('mod fx processor needs processorOptions');
		const kernel = KERNELS[o.kernel];
		if (!kernel) throw new RangeError(`mod fx processor kernel is unknown: ${String(o.kernel)}`);
		assertFinite('mod fx processor option', o, [...SHELL_OPTION_KEYS, ...kernel.options]);
		if (!(o.spreadDeg >= 0 && o.spreadDeg <= 180))
			throw new RangeError(
				`mod fx processor option spreadDeg must be in 0..180; got ${o.spreadDeg}`
			);
		this.kernel = kernel;
		this.state = kernel.init(o);
		this.spread = o.spreadDeg / 360;
		this.rampStep = 1 / (o.rampSec * sampleRate);
		this.crossfadeFrames = Math.max(1, Math.round(o.crossfadeSec * sampleRate));
		this.wet = new Float64Array(2);
		this.wetOld = new Float64Array(2);
		this.level = 0;
		this.active = false;
		this.anchor = null;
		this.previousAnchor = null;
		this.crossfadeLeft = 0;
		this.port.onmessage = (event) => this.receive(event.data);
	}

	/** Messages: {type:'anchor', anchor}, {type:'crossfade'} (after a re-lock anchor), {type:'active', on}. */
	receive(message) {
		if (message.type === 'anchor') {
			const anchor = message.anchor;
			assertFinite(
				`mod fx anchor kind ${String(anchor && anchor.kind)}`,
				anchor,
				anchor && ANCHOR_KEYS[anchor.kind]
			);
			this.previousAnchor = this.anchor;
			this.anchor = anchor;
		} else if (message.type === 'crossfade') {
			if (this.previousAnchor === null) return;
			this.crossfadeLeft = this.crossfadeFrames;
			if (this.kernel.fork !== undefined) this.kernel.fork(this.state);
		} else if (message.type === 'active') {
			this.active = message.on === true;
		} else {
			throw new RangeError(`mod fx processor message type is unknown: ${String(message.type)}`);
		}
	}

	process(inputs, outputs, parameters) {
		const input = inputs[0];
		const output = outputs[0];
		const outL = output[0];
		const outR = output.length > 1 ? output[1] : null;
		const inL = input.length > 0 ? input[0] : null;
		const inR = input.length > 1 ? input[1] : inL;
		const frames = outL.length;
		const anchor = this.anchor;
		const old = this.previousAnchor;
		const target = this.active && anchor !== null ? parameters.depth[0] : 0;
		let phase = anchor === null ? 0 : anchorPhase(anchor, currentTime);
		const rate = anchor === null ? 0 : anchorRate(anchor);
		let oldPhase = this.crossfadeLeft > 0 ? anchorPhase(old, currentTime) : 0;
		const oldRate = this.crossfadeLeft > 0 ? anchorRate(old) : 0;
		const wet = this.wet;
		const wetOld = this.wetOld;
		const wetFade = this.kernel.fork !== undefined;
		const gate = this.kernel.gain;
		const spread = this.spread;
		let level = this.level;
		for (let i = 0; i < frames; i++) {
			const xL = inL === null ? 0 : inL[i];
			const xR = inR === null ? 0 : inR[i];
			if (level < target) level = Math.min(target, level + this.rampStep);
			else if (level > target) level = Math.max(target, level - this.rampStep);
			if (gate !== undefined) {
				let g = gate(this.state, phase, level, rate);
				if (this.crossfadeLeft > 0) {
					const mix = 1 - this.crossfadeLeft / this.crossfadeFrames;
					g = mix * g + (1 - mix) * gate(this.state, oldPhase, level, oldRate);
					oldPhase += oldRate;
					this.crossfadeLeft--;
				}
				phase += rate;
				// Knob 0 is wet 0, so g is exactly 1 and the output is the input bit for bit.
				outL[i] = g * xL;
				if (outR !== null) outR[i] = g * xR;
				continue;
			}
			let modL = triangle(phase);
			let modR = triangle(phase + spread);
			let fade = 1;
			if (this.crossfadeLeft > 0) {
				fade = 1 - this.crossfadeLeft / this.crossfadeFrames;
				const oldL = triangle(oldPhase);
				const oldR = triangle(oldPhase + spread);
				if (wetFade) this.kernel.tickOld(this.state, xL, xR, level * oldL, level * oldR, wetOld);
				else {
					modL = fade * modL + (1 - fade) * oldL;
					modR = fade * modR + (1 - fade) * oldR;
				}
				oldPhase += oldRate;
				this.crossfadeLeft--;
			}
			phase += rate;
			this.kernel.tick(this.state, xL, xR, level * modL, level * modR, wet);
			if (wetFade && fade < 1) {
				wet[0] = fade * wet[0] + (1 - fade) * wetOld[0];
				wet[1] = fade * wet[1] + (1 - fade) * wetOld[1];
			}
			const dryGain = 1 - 0.5 * level;
			const wetGain = 0.5 * level;
			outL[i] = level === 0 ? xL : dryGain * xL + wetGain * wet[0];
			if (outR !== null) outR[i] = level === 0 ? xR : dryGain * xR + wetGain * wet[1];
		}
		this.level = level;
		if (this.kernel.endBlock !== undefined) this.kernel.endBlock(this.state);
		return true;
	}
}

registerProcessor('mdt-mod-fx', ModFxProcessor);
