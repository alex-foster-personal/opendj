/**
 * Edge sampler: one Web Audio voice (spec 4.1, 4.4). Issue #10780 (S3), ported from the v1 reference.
 *   source (AudioBufferSourceNode when pitch follows rate, else Signalsmith stretcher)
 *   -> trim -> low/mid/high biquads -> LP/HP colour filter (dry/wet) -> level -> destination
 *
 * Built with the channel strip's own constants and laws (deck-channel-graph.ts), so a captured
 * vector sounds as it did on the deck. Stems in the vector are not applied here: the voice plays
 * the mix clip (spec 5.1, stem parts are a later slice). The plan math lives in
 * sampler-voice-plan.ts; `start()` is synchronous so no await sits between the launch time and
 * the source start.
 */

import {
	EQ_FREQ_HIGH_HZ,
	EQ_FREQ_LOW_HZ,
	EQ_FREQ_MID_HZ,
	EQ_MID_Q,
	FILTER_Q_DB
} from '$lib/player/constants';
import { applyEqRamp } from '$lib/player/eq-apply';
import { filterParamsFromKnob } from '$lib/rb/audio-engine-guards';
import { StretchDeckProcessor } from '$lib/rb/stretch-adapter';
import type { StretchProcessorError } from '$lib/rb/stretch-errors';
import { filterKnobFromBipolar } from './sampler-capture-vector';
import type { SamplerVector } from './sampler-types';
import {
	VOICE_STOP_RAMP_SEC,
	plainSourceArgs,
	stretchLaunchChange,
	stretchStopAt,
	type VoicePlan
} from './sampler-voice-plan';
import { assertLaunchClearsStopRamp, canReuseWarm } from './sampler-warm';

/**
 * perf round 2 (EDGESAMP-11872i): a stretch voice keeps its processor and loaded clip after stop,
 * so a re-trigger of the same clip is one schedule message. false = the cold path (a new
 * processor, reset, drop, add and schedule on every trigger), for a listening A/B.
 */
export const SAMPLER_WARM_VOICES = true;
const PARAM_RAMP_SEC = 0.02;
/** Margin after the stop ramp before the nodes are released. */
const DISPOSE_MARGIN_SEC = 0.05;

const asError = (e: unknown): Error => (e instanceof Error ? e : new Error(String(e)));

export class SamplerVoice {
	readonly #ctx: AudioContext;
	readonly #trim: GainNode;
	readonly #low: BiquadFilterNode;
	readonly #mid: BiquadFilterNode;
	readonly #high: BiquadFilterNode;
	readonly #lp: BiquadFilterNode;
	readonly #hp: BiquadFilterNode;
	readonly #dry: GainNode;
	readonly #lpWet: GainNode;
	readonly #hpWet: GainNode;
	readonly #level: GainNode;
	#plain: AudioBufferSourceNode | null = null;
	#stretch: StretchDeckProcessor | null = null;
	#plan: VoicePlan | null = null;
	#levelLinear = 1;
	#disposed = false;
	/** warm: stop() parks the stretch processor instead of disposing it */
	#warm: boolean;
	#loaded: AudioBuffer | null = null;
	/** context time the last stop ramp ends; a warm re-trigger must launch after it */
	#idleAt = 0;
	#onError: (e: Error) => void;
	readonly #destination: AudioNode;

	constructor(
		ctx: AudioContext,
		destination: AudioNode,
		onError: (e: Error) => void,
		warm = false
	) {
		this.#ctx = ctx;
		this.#onError = onError;
		this.#warm = warm;
		this.#destination = destination;
		this.#trim = ctx.createGain();
		const band = (type: BiquadFilterType, hz: number): BiquadFilterNode => {
			const b = ctx.createBiquadFilter();
			b.type = type;
			b.frequency.value = hz;
			b.gain.value = 0;
			return b;
		};
		this.#low = band('lowshelf', EQ_FREQ_LOW_HZ);
		this.#mid = band('peaking', EQ_FREQ_MID_HZ);
		this.#mid.Q.value = EQ_MID_Q;
		this.#high = band('highshelf', EQ_FREQ_HIGH_HZ);
		this.#lp = band('lowpass', 20000);
		this.#lp.Q.value = FILTER_Q_DB;
		this.#hp = band('highpass', 20);
		this.#hp.Q.value = FILTER_Q_DB;
		this.#dry = ctx.createGain();
		this.#lpWet = ctx.createGain();
		this.#hpWet = ctx.createGain();
		this.#level = ctx.createGain();
		this.#level.gain.value = 0;
		this.#trim.connect(this.#low).connect(this.#mid).connect(this.#high);
		for (const stage of [this.#dry, this.#lp, this.#hp]) this.#high.connect(stage);
		this.#lp.connect(this.#lpWet);
		this.#hp.connect(this.#hpWet);
		for (const branch of [this.#dry, this.#lpWet, this.#hpWet]) branch.connect(this.#level);
		this.#level.connect(destination);
		this.#applyFilter(0, 0);
	}

	/** Trim/EQ/filter from a captured vector (null = flat), level 0..100 (linear law). */
	applyVector(vector: SamplerVector | null, level: number): void {
		if (!(level >= 0 && level <= 100)) throw new Error(`voice level must be 0..100, got ${level}`);
		const now = this.#ctx.currentTime;
		const v = vector ?? { trim_db: 0, eq_db: { low: 0, mid: 0, high: 0 }, filter: 0 };
		this.#trim.gain.setTargetAtTime(10 ** (v.trim_db / 20), now, PARAM_RAMP_SEC / 3);
		applyEqRamp(this.#low.gain, v.eq_db.low, now, PARAM_RAMP_SEC);
		applyEqRamp(this.#mid.gain, v.eq_db.mid, now, PARAM_RAMP_SEC);
		applyEqRamp(this.#high.gain, v.eq_db.high, now, PARAM_RAMP_SEC);
		this.#applyFilter(v.filter, now);
		this.#levelLinear = level / 100;
	}

	#applyFilter(filter: number, now: number): void {
		const p = filterParamsFromKnob(filterKnobFromBipolar(filter));
		this.#lp.frequency.setValueAtTime(p.lpHz, now);
		this.#hp.frequency.setValueAtTime(p.hpHz, now);
		this.#dry.gain.setValueAtTime(p.dryGain, now);
		this.#lpWet.gain.setValueAtTime(p.lpWetGain, now);
		this.#hpWet.gain.setValueAtTime(p.hpWetGain, now);
	}

	/** Build the source. Async work (worklet, load) happens here, BEFORE the launch time is computed. */
	async prepare(plan: VoicePlan): Promise<void> {
		this.#assertLive();
		if (this.#plan !== null) throw new Error('voice already prepared; dispose it first');
		this.#plan = plan;
		if (!plan.masterTempo) {
			this.#plain = this.#plainSource(plan);
			return;
		}
		const stretch = await StretchDeckProcessor.create(this.#ctx, {
			onProcessorError: (e: StretchProcessorError) => this.#fail(e)
		});
		await stretch.load(plan.buffer);
		stretch.connect(this.#trim);
		this.#stretch = stretch;
		this.#loaded = plan.buffer;
	}

	#plainSource(plan: VoicePlan): AudioBufferSourceNode {
		const args = plainSourceArgs(plan);
		const src = this.#ctx.createBufferSource();
		src.buffer = plan.buffer;
		src.playbackRate.value = plan.rate;
		src.loop = plan.loop;
		src.loopStart = args.loopStartSec;
		src.loopEnd = args.loopEndSec;
		src.connect(this.#trim);
		return src;
	}

	/** True when this parked warm voice can replay `buffer` on `ctx` with one schedule message. */
	canRetrigger(ctx: AudioContext, buffer: AudioBuffer, destination?: AudioNode): boolean {
		return canReuseWarm(
			{
				warm: this.#warm,
				disposed: this.#disposed,
				armed: this.#plan !== null,
				hasProcessor: this.#stretch !== null,
				ctx: this.#ctx,
				loaded: this.#loaded,
				destination: this.#destination
			},
			ctx,
			buffer,
			destination
		);
	}

	/** Re-arm a parked warm voice for a new trigger of its loaded clip (no worklet round trip). */
	rearm(plan: VoicePlan, onError: (e: Error) => void): void {
		if (!this.canRetrigger(this.#ctx, plan.buffer) || !plan.masterTempo)
			throw new Error('voice cannot re-trigger this plan warm');
		this.#onError = onError;
		this.#plan = plan;
	}

	/** Holds a loaded stretch clip (warm voices only). */
	holds(buffer: AudioBuffer): boolean {
		return this.#warm && !this.#disposed && this.#loaded === buffer;
	}

	/** Drop a warm voice for good: dispose once any stop ramp has finished. */
	retire(): void {
		this.#warm = false;
		const waitSec = Math.max(0, this.#idleAt - this.#ctx.currentTime) + DISPOSE_MARGIN_SEC;
		setTimeout(() => void this.dispose(), waitSec * 1000);
	}

	/** Schedule the start at `at` (context seconds). Synchronous for the plain source. */
	start(at: number): void {
		this.#assertLive();
		const plan = this.#plan;
		if (plan === null) throw new Error('voice started before prepare');
		// The launch lead (>= 50 ms) always clears the stop ramp; overlapping it would fight the fade.
		assertLaunchClearsStopRamp(at, this.#idleAt);
		this.#level.gain.cancelScheduledValues(at);
		this.#level.gain.setValueAtTime(0, at);
		this.#level.gain.linearRampToValueAtTime(this.#levelLinear, at + VOICE_STOP_RAMP_SEC);
		if (this.#plain !== null) {
			const args = plainSourceArgs(plan);
			if (args.durationSec === null) this.#plain.start(at, args.offsetSec);
			else this.#plain.start(at, args.offsetSec, args.durationSec);
			return;
		}
		const stretch = this.#stretch;
		if (stretch === null) throw new Error('voice has no source');
		const stopAt = stretchStopAt(at, plan);
		void stretch
			.schedule(at, stretchLaunchChange(plan))
			.then(() => (stopAt === null ? undefined : stretch.stop(stopAt)))
			.catch((e: unknown) => this.#fail(asError(e)));
	}

	/** Follow a master tempo move (10 Hz tick). */
	setRate(rate: number, semitones: number): void {
		const plan = this.#plan;
		if (plan === null || this.#disposed) return;
		plan.rate = rate;
		plan.semitones = semitones;
		const now = this.#ctx.currentTime;
		if (this.#plain !== null) this.#plain.playbackRate.setTargetAtTime(rate, now, 0.02);
		else if (this.#stretch !== null)
			void this.#stretch
				.schedule(now, { rate, semitones })
				.catch((e: unknown) => this.#fail(asError(e)));
	}

	/**
	 * Phase lock re-phase (#10781): from `at` the loop plays clip second `clipSec`. The level dips
	 * to 0 over the stop ramp before `at` and back over the same ramp after it, so the jump never
	 * clicks (the deck's 5 ms ramp, mimicked).
	 */
	rephase(at: number, clipSec: number): void {
		const plan = this.#plan;
		if (plan === null || this.#disposed) return;
		if (!plan.loop) throw new Error('only a looping voice re-phases');
		const g = this.#level.gain;
		g.cancelScheduledValues(at - VOICE_STOP_RAMP_SEC);
		g.setValueAtTime(this.#levelLinear, at - VOICE_STOP_RAMP_SEC);
		g.linearRampToValueAtTime(0, at);
		g.linearRampToValueAtTime(this.#levelLinear, at + VOICE_STOP_RAMP_SEC);
		const old = this.#plain;
		if (old !== null) {
			const src = this.#plainSource(plan);
			src.start(at, clipSec);
			old.stop(at);
			old.onended = () => old.disconnect();
			this.#plain = src;
			return;
		}
		const stretch = this.#stretch;
		if (stretch === null) throw new Error('voice has no source');
		void stretch.schedule(at, { input: clipSec }).catch((e: unknown) => this.#fail(asError(e)));
	}

	/** Stop with the stop ramp (no click), then release every node (or park a warm stretch voice). */
	stop(): void {
		if (this.#disposed) return;
		const now = this.#ctx.currentTime;
		const g = this.#level.gain;
		g.cancelScheduledValues(now);
		g.setValueAtTime(g.value, now);
		g.linearRampToValueAtTime(0, now + VOICE_STOP_RAMP_SEC);
		const end = now + VOICE_STOP_RAMP_SEC;
		if (this.#plain !== null) {
			try {
				this.#plain.stop(end);
			} catch (e) {
				// InvalidStateError: never started (armed voice). Anything else is real.
				if (!(e instanceof DOMException && e.name === 'InvalidStateError')) throw e;
			}
		}
		this.#idleAt = end;
		const stretch = this.#stretch;
		if (this.#warm && stretch !== null) {
			// Park: silence the processor after the ramp, keep it and its clip for a re-trigger.
			this.#plan = null;
			void stretch.stop(end).catch((e: unknown) => this.#fail(asError(e)));
			return;
		}
		setTimeout(() => void this.dispose(), (VOICE_STOP_RAMP_SEC + DISPOSE_MARGIN_SEC) * 1000);
	}

	/** Release the source and the chain's output; a stretch processor is disposed too. */
	async dispose(): Promise<void> {
		if (this.#disposed) return;
		this.#disposed = true;
		this.#plain?.disconnect();
		this.#level.disconnect();
		const stretch = this.#stretch;
		this.#stretch = null;
		if (stretch !== null) await stretch.dispose();
	}

	/** A failed processor is never re-used: drop warmth, then report. */
	#fail(e: Error): void {
		this.#warm = false;
		this.#onError(e);
	}

	#assertLive(): void {
		if (this.#disposed) throw new Error('sampler voice is disposed');
	}
}
