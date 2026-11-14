/**
 * Edge sampler: the four voices and the cue preview (spec 4.2, 4.3, 4.4). Issue #10780 (S3), the
 * logic of the v1 engine with its Web Audio parts behind injected ports, so node tests drive it.
 *
 * - Voices 5..8 on the master bus. A trigger takes a free voice (one already holding the clip
 *   first), else steals the oldest with the stop ramp. A trigger on an armed or playing slot stops it.
 * - Launch: the master's next quantise point. Gate starts on press. No playing master: the voice
 *   starts after the safe lead at native tempo (rate 1) and reports `free`.
 * - While voices sound a 10 Hz tick marks started voices playing, follows master tempo moves and
 *   keeps quantised matched voices phase locked (sampler-phase-lock.ts, #10781): a bounded trim,
 *   or a re-phase at the master's next beat after a jump, jog, loop or change of master.
 * - Preview: one at a time, cue bus only, beat and key matched like a trigger.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 a trigger with a playing master starts on its next downbeat, with no await in between
 *     [if] the master is mid-bar [then] the voice starts at the next downbeat's context time [else ⛔️]
 *   ✔︎ ✅ 🎯 no master: free at native tempo
 *     [if] no deck plays as master [then] rate 1 and the voice state says free [else ⛔️]
 *   ✔︎ ✅ 🎯 a fifth trigger steals the oldest voice; a second trigger of a sounding slot stops it
 *     [if] four voices sound and a fifth slot fires [then] the first is stopped and reused [else ⛔️]
 *   ✔︎ ✅ 🎯 a locked voice is trimmed on the tick and re-phased at the next beat after a master jump
 *     [if] the master seeks while a loop sounds [then] the voice re-phases inside its loop window [else ⛔️]
 *   ✔︎ ✅ 🎯 a preview never takes a master voice
 *     [if] a slot previews [then] no voice 5..8 is busy and the cue clock offset is applied [else ⛔️]
 */

import {
	LAUNCH_LEAD_SEC,
	keyPlan,
	nextLaunchCtxSec,
	playbackRate,
	playheadFraction
} from './sampler-launch';
import type { GridBeat } from './sampler-launch';
import type { SlotPhase } from './sampler-engine-seam';
import {
	phaseLockTick,
	rephaseClipSec,
	startPhaseLock,
	type MasterView,
	type PhaseLockState
} from './sampler-phase-lock';
import { stemsPending } from './sampler-store-logic';
import type { SamplerSlot, SlotSettings, VoiceState } from './sampler-types';
import {
	buildVoicePlan,
	passSecOf,
	type MasterTempoKey,
	type VoicePlan
} from './sampler-voice-plan';
import { pickFreeVoice } from './sampler-warm';

export const VOICE_IDS = [5, 6, 7, 8] as const;
export type VoiceId = (typeof VOICE_IDS)[number];
/** Master follow tick (spec 4.3: 10 Hz). */
export const FOLLOW_TICK_MS = 100;

/** The playing master as the bank reads it: tempo, key, grid and its position on the context clock. */
export interface BankMaster extends MasterTempoKey {
	/** the master deck's number */
	deck: number;
	beats: readonly GridBeat[];
	/** master track seconds and tempo ratio at context time `nowCtx` */
	at(nowCtx: number): { positionSec: number; tempoRatio: number };
}

/** A prepared voice: everything async is done, `start` schedules synchronously. */
export interface BankVoice {
	applyVector(vector: SamplerSlot['vector'], level: number): void;
	start(at: number): void;
	setRate(rate: number, semitones: number): void;
	/** a sounding loop plays clip second `clipSec` from context time `at` (phase lock re-phase) */
	rephase(at: number, clipSec: number): void;
	stop(): void;
}

export type Bus = 'master' | 'cue';

/** The Web Audio side, injected (sampler-engine.svelte.ts in the app, fakes in tests). */
export interface VoiceBankPorts {
	/** master context seconds now */
	now(): number;
	master(): BankMaster | null;
	buffer(slot: SamplerSlot, bus: Bus): Promise<AudioBuffer>;
	/** a prepared voice for voice `id` (or the preview) on `bus` */
	voice(
		id: VoiceId | 'preview',
		plan: VoicePlan,
		bus: Bus,
		onError: (e: Error) => void
	): Promise<BankVoice>;
	/** true when voice `id` parks a warm voice already holding `buffer` */
	holdsClip(id: VoiceId, buffer: AudioBuffer): boolean;
	/** cue context seconds minus master context seconds */
	cueOffsetSec(): number;
	report(what: string, e: unknown): void;
}

/** A voice with nothing on it. */
export const idleVoiceState = (voice: VoiceId): VoiceState => ({
	voice,
	slot_id: null,
	phase: 'free',
	free: false,
	start_ctx_sec: null,
	pass_sec: null,
	rate: 1,
	semitones: 0,
	stems_pending: false
});

/** The voice a trigger takes: a free one (holding the clip first), else the oldest live one, stolen. */
export function chooseVoice(
	busy: (id: VoiceId) => boolean,
	holdsClip: (id: VoiceId) => boolean,
	ageOf: ReadonlyMap<VoiceId, number>
): { voice: VoiceId; steal: boolean } {
	const free = pickFreeVoice(
		VOICE_IDS,
		(id) => busy(id as VoiceId),
		(id) => holdsClip(id as VoiceId)
	);
	if (free !== undefined) return { voice: free as VoiceId, steal: false };
	const oldest = [...ageOf.entries()].sort((a, b) => a[1] - b[1])[0];
	if (oldest === undefined) throw new Error('every sampler voice is still loading a clip');
	return { voice: oldest[0], steal: true };
}

/** Context seconds a slot launches at; `free` when no master plays (native tempo, no quantise). */
export function launchFor(
	now: number,
	master: BankMaster | null,
	settings: Pick<SlotSettings, 'launch' | 'downbeat_offset' | 'mode'>
): { at: number; free: boolean } {
	if (master === null) return { at: now + LAUNCH_LEAD_SEC, free: true };
	if (settings.mode === 'gate') return { at: now + LAUNCH_LEAD_SEC, free: false };
	if (master.beats.length < 2) throw new Error('the master deck has no beat grid');
	const { positionSec, tempoRatio } = master.at(now);
	const at = nextLaunchCtxSec(
		master.beats,
		positionSec,
		now,
		tempoRatio,
		settings.launch,
		settings.downbeat_offset
	);
	return { at, free: false };
}

/** The new rate and key shift when a matched voice must follow a master tempo move, else null. */
export function followMaster(
	slot: Pick<SamplerSlot, 'native_bpm' | 'key' | 'settings'>,
	rate: number,
	master: MasterTempoKey | null
): { rate: number; semitones: number } | null {
	if (master === null || slot.settings.tempo !== 'match') return null;
	const next = playbackRate(slot.native_bpm, master.bpm, 'match');
	if (Math.abs(next - rate) < 1e-4) return null;
	return {
		rate: next,
		semitones: keyPlan(slot.key, master.key, slot.settings.key, next).semitones
	};
}

interface Live {
	voice: BankVoice;
	slot: SamplerSlot;
	plan: VoicePlan;
	token: number;
	/** tempo-matched rate before any phase trim */
	base: number;
	/** null: not locked (free, gate, unquantised or own tempo) */
	lock: PhaseLockState | null;
}

/**
 * True when a voice can phase lock: it launched on a master beat, follows the master tempo and
 * loops (a play-once voice cannot re-phase; it ends on its own).
 */
export function locksToMaster(
	settings: Pick<SlotSettings, 'launch' | 'mode' | 'tempo'>,
	free: boolean,
	loop: boolean
): boolean {
	return (
		loop &&
		!free &&
		settings.tempo === 'match' &&
		settings.mode !== 'gate' &&
		settings.launch !== 'off'
	);
}

/** The master as the lock reads it at context time `ctx`. */
export function masterViewAt(master: BankMaster, ctx: number): MasterView {
	return { deck: master.deck, beats: master.beats, ...master.at(ctx) };
}

export interface PreviewState {
	slot_id: string;
	start_ctx_sec: number;
	pass_sec: number;
}

export class SamplerVoiceBank {
	readonly #ports: VoiceBankPorts;
	/** published state; the engine passes a $state object so the UI re-renders */
	readonly state: { voices: VoiceState[]; preview: PreviewState | null };
	readonly #live = new Map<VoiceId, Live>();
	/** slot id -> trigger token while its clip loads or its voice prepares */
	readonly #loading = new Map<string, number>();
	readonly #reserved = new Set<VoiceId>();
	#preview: { voice: BankVoice | null; token: number } | null = null;
	#token = 0;
	#tick: ReturnType<typeof setInterval> | null = null;

	constructor(ports: VoiceBankPorts, state: SamplerVoiceBank['state']) {
		this.#ports = ports;
		this.state = state;
	}

	/** Arm a slot on the master bus, or stop it when it is armed, playing or still loading. */
	async toggle(slot: SamplerSlot): Promise<void> {
		const existing = this.#voiceOf(slot.slot_id);
		if (existing !== null) return this.#stopVoice(existing);
		if (this.#loading.delete(slot.slot_id)) return; // a second press cancels the load
		const token = ++this.#token;
		this.#loading.set(slot.slot_id, token);
		const current = (): boolean => this.#loading.get(slot.slot_id) === token;
		let id: VoiceId | null = null;
		try {
			const buffer = await this.#ports.buffer(slot, 'master');
			if (!current()) return;
			const plan = buildVoicePlan(slot, buffer, this.#ports.master());
			const ages = new Map([...this.#live].map(([k, l]) => [k, l.token] as const));
			const pick = chooseVoice(
				(v) => this.#live.has(v) || this.#reserved.has(v),
				(v) => this.#ports.holdsClip(v, buffer),
				ages
			);
			if (pick.steal) this.#stopVoice(pick.voice);
			id = pick.voice;
			this.#reserved.add(id);
			const voiceId = id;
			const voice = await this.#ports.voice(id, plan, 'master', (e) =>
				this.#voiceFailed(voiceId, token, e)
			);
			if (!current()) return voice.stop();
			voice.applyVector(slot.vector, slot.settings.level);
			// No await between computing the launch time and scheduling it (spec 4.3).
			const { at, free } = launchFor(this.#ports.now(), this.#ports.master(), slot.settings);
			voice.start(at);
			const master = this.#ports.master();
			const lock =
				master !== null && locksToMaster(slot.settings, free, plan.loop)
					? startPhaseLock(at, plan.rate, plan.regionSec / slot.beats, masterViewAt(master, at))
					: null;
			this.#live.set(id, { voice, slot, plan, token, base: plan.rate, lock });
			this.#set(id, {
				slot_id: slot.slot_id,
				phase: 'armed',
				free,
				start_ctx_sec: at,
				pass_sec: passSecOf(plan),
				rate: plan.rate,
				semitones: plan.semitones,
				stems_pending: stemsPending(slot)
			});
			this.#ensureTick();
			if (!plan.loop) this.#stopAfterPass(id, token, at, plan);
		} finally {
			if (current()) this.#loading.delete(slot.slot_id);
			if (id !== null) this.#reserved.delete(id);
		}
	}

	/** Stop the slot's voice with the stop ramp. */
	stop(slotId: string): void {
		const id = this.#voiceOf(slotId);
		if (id !== null) this.#stopVoice(id);
	}

	/** Stop every voice and the preview, and drop pending loads. */
	stopAll(): void {
		this.#loading.clear();
		for (const id of [...this.#live.keys()]) this.#stopVoice(id);
		this.stopPreview();
	}

	/** Play the slot on the cue bus only, replacing any preview. */
	async preview(slot: SamplerSlot): Promise<void> {
		this.stopPreview();
		const token = ++this.#token;
		this.#preview = { voice: null, token };
		const current = (): boolean => this.#preview?.token === token;
		const buffer = await this.#ports.buffer(slot, 'cue');
		if (!current()) return;
		const plan = buildVoicePlan(slot, buffer, this.#ports.master());
		const voice = await this.#ports.voice('preview', plan, 'cue', (e) => {
			this.#ports.report('sampler preview failed', e);
			if (current()) this.stopPreview();
		});
		if (!current()) return voice.stop();
		voice.applyVector(slot.vector, slot.settings.level);
		const { at } = launchFor(this.#ports.now(), this.#ports.master(), {
			...slot.settings,
			mode: 'loop'
		});
		// The cue bus may run on its own context: launch on the master clock, shifted to the cue clock.
		voice.start(at + this.#ports.cueOffsetSec());
		this.#preview = { voice, token };
		this.state.preview = { slot_id: slot.slot_id, start_ctx_sec: at, pass_sec: passSecOf(plan) };
	}

	stopPreview(): void {
		this.#preview?.voice?.stop();
		this.#preview = null;
		this.state.preview = null;
	}

	/** Reads only the published state, so a template re-runs when a voice takes the slot. */
	phase(slotId: string): SlotPhase {
		const v = this.state.voices.find((x) => x.slot_id === slotId);
		if (v !== undefined) {
			if (v.phase === 'free') throw new Error(`voice ${v.voice} holds ${slotId} but reads free`);
			return v.phase;
		}
		return this.state.preview?.slot_id === slotId ? 'previewing' : 'free';
	}

	/** 0..1 through the current pass of the slot's voice or preview, null when it is not sounding. */
	playhead(slotId: string): number | null {
		const v = this.state.voices.find((x) => x.slot_id === slotId) ?? null;
		const p = this.state.preview?.slot_id === slotId ? this.state.preview : null;
		const start = v?.start_ctx_sec ?? p?.start_ctx_sec ?? null;
		const pass = v?.pass_sec ?? p?.pass_sec ?? null;
		return start === null || pass === null
			? null
			: playheadFraction(this.#ports.now(), start, pass);
	}

	/** One follow tick: started voices read playing, matched voices follow and lock to the master. */
	followTick(): void {
		const master = this.#ports.master();
		const now = this.#ports.now();
		for (const [id, l] of this.#live) {
			const v = this.#find(id);
			if (v.phase === 'armed' && now >= (v.start_ctx_sec ?? Infinity)) v.phase = 'playing';
			const next = followMaster(l.slot, l.base, master);
			if (next !== null) {
				l.base = next.rate;
				l.plan.semitones = next.semitones;
				this.#set(id, { ...next, pass_sec: passSecOf({ ...l.plan, rate: next.rate }) });
			}
			const rate = l.lock === null ? (next?.rate ?? null) : this.#lockStep(l, l.lock, now, master);
			if (rate === null) continue;
			l.plan.rate = rate;
			l.voice.setRate(rate, l.plan.semitones);
		}
	}

	/** One phase lock tick for a locked voice: schedules any re-phase, returns the rate to send. */
	#lockStep(l: Live, lock: PhaseLockState, now: number, master: BankMaster | null): number | null {
		// A master with no grid cannot be phase read: the voice holds its tempo-matched rate.
		const view = master !== null && master.beats.length >= 2 ? masterViewAt(master, now) : null;
		const step = phaseLockTick(lock, now, l.base, view);
		l.lock = step.state;
		const r = step.rephase;
		const clipSec = r === null ? null : rephaseClipSec(l.plan, r.voiceBeat, lock.voiceBeatSec);
		if (r !== null && clipSec !== null) l.voice.rephase(r.at, clipSec);
		return step.rate;
	}

	#stopAfterPass(id: VoiceId, token: number, at: number, plan: VoicePlan): void {
		const endMs = (at - this.#ports.now() + plan.regionSec / plan.rate) * 1000;
		setTimeout(() => {
			if (this.#live.get(id)?.token === token) this.#stopVoice(id);
		}, endMs);
	}

	#voiceFailed(id: VoiceId, token: number, e: Error): void {
		this.#ports.report(`sampler voice ${id} failed`, e);
		if (this.#live.get(id)?.token === token) this.#stopVoice(id);
	}

	#voiceOf(slotId: string): VoiceId | null {
		for (const [id, l] of this.#live) if (l.slot.slot_id === slotId) return id;
		return null;
	}

	#find(id: VoiceId): VoiceState {
		const v = this.state.voices.find((x) => x.voice === id);
		if (v === undefined) throw new Error(`no sampler voice ${id}`);
		return v;
	}

	#set(id: VoiceId, patch: Partial<VoiceState>): void {
		Object.assign(this.#find(id), patch);
	}

	#stopVoice(id: VoiceId): void {
		const l = this.#live.get(id);
		if (l === undefined) return;
		l.voice.stop();
		this.#live.delete(id);
		this.#set(id, idleVoiceState(id));
		if (this.#live.size === 0 && this.#tick !== null) {
			clearInterval(this.#tick);
			this.#tick = null;
		}
	}

	#ensureTick(): void {
		if (this.#tick !== null) return;
		this.#tick = setInterval(() => {
			try {
				this.followTick();
			} catch (e) {
				this.#ports.report('sampler master follow failed', e);
			}
		}, FOLLOW_TICK_MS);
	}
}
