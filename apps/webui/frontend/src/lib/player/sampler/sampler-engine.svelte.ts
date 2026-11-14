/**
 * Edge sampler: the Web Audio engine (spec 4.2, 4.3, 5.3). Issue #10780 (S3), ported from the v1
 * reference. The voice bank (sampler-voice-bank.ts) decides; this module gives it the real ports:
 * the master mix tap and the headphone cue bus, the master deck's tempo, key and grid, the clip
 * pool (sampler-pool.ts) and Web Audio voices (sampler-voice.ts) with warm re-use per voice.
 * The Rust engine refuses: the sampler plays on the Web Audio graph only.
 *
 * Registered with the store by the edge sampler overlay (`registerSamplerEngine(samplerEngine)`).
 */

import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
import { DECK_IDS } from '$lib/player/constants';
import { peekCueBus } from '$lib/player/headphones';
import { deckEffectiveBpm, getDeckState } from '$lib/player/state.svelte';
import {
	audioContextNowSec,
	deckMasterAtContextSec,
	masterMixTapPoint
} from '$lib/rb/audio-engine.svelte';
import { resolvedTier } from '$lib/rb/perf-tier';
import { fetchClipBytes } from './api-sampler';
import type { SamplerEngine } from './sampler-engine-seam';
import { SamplerClipPool } from './sampler-pool';
import { hotPoolBudgetBytes, residencyPins, residencyPlan } from './sampler-residency';
import { createResidencyFeed } from './sampler-residency-feed';
import type { SamplerSlot } from './sampler-types';
import { SAMPLER_WARM_VOICES, SamplerVoice } from './sampler-voice';
import type { VoicePlan } from './sampler-voice-plan';
import {
	SamplerVoiceBank,
	VOICE_IDS,
	idleVoiceState,
	type BankMaster,
	type Bus,
	type PreviewState,
	type VoiceId
} from './sampler-voice-bank';

/** The voices and the preview, as $state so tabs and the mirror re-render on every change. */
export const samplerVoices = $state({
	voices: VOICE_IDS.map(idleVoiceState),
	preview: null as PreviewState | null
});

//----- outputs and clock

function _out(bus: Bus): { context: AudioContext; node: AudioNode } {
	if (rustMode.enabled)
		throw new Error(
			'The sampler needs the Web Audio engine; it is not available while the Rust engine is on'
		);
	if (bus === 'cue') {
		const cue = peekCueBus();
		if (cue === null) throw new Error('Sampler preview needs a headphone cue output');
		return { context: cue.context, node: cue.cueSum };
	}
	const tap = masterMixTapPoint(true);
	if (tap === null) throw new Error('Sampler: the master output is not ready');
	return tap;
}

function _now(): number {
	const now = audioContextNowSec();
	if (now === null) throw new Error('Sampler: audio graph not initialised');
	return now;
}

function _master(): BankMaster | null {
	const deck = DECK_IDS.find((d) => getDeckState(d).is_master && getDeckState(d).playing);
	if (deck === undefined) return null;
	const bpm = deckEffectiveBpm(deck);
	if (bpm === null) throw new Error(`Master deck ${deck} has no tempo`);
	const st = getDeckState(deck);
	return {
		deck,
		bpm,
		key: st.key,
		beats: st.anlz?.beatgrid?.beats ?? [],
		at: (now) => deckMasterAtContextSec(deck, now)
	};
}

//----- clips and voices

/** the store's pinned tabs, from its last residency view */
let _padPins: readonly string[] = [];
const _pool = new SamplerClipPool(
	(slotId, part) => fetchClipBytes(slotId, part),
	() => residencyPins(samplerVoices.voices, samplerVoices.preview?.slot_id ?? null, _padPins),
	() => hotPoolBudgetBytes(resolvedTier())
);
/** Throttled promotion of the residency view; no master graph yet means nothing is audible, so no work. */
const _reside = createResidencyFeed<SamplerSlot>(
	async (view) => {
		const ctx = masterMixTapPoint(false)?.context;
		if (ctx === undefined) return;
		await _pool.promote(residencyPlan(view), ctx, (e) =>
			console.error('sampler residency failed', e)
		);
	},
	{
		setTimeout: (fn, ms) => setTimeout(fn, ms),
		clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
		now: () => performance.now()
	}
);
/** voice id -> its parked warm stretch voice (perf round 2), kept across stops */
const _warm = new Map<VoiceId, SamplerVoice>();

async function _prepared(voice: SamplerVoice, plan: VoicePlan): Promise<SamplerVoice> {
	await voice.prepare(plan);
	return voice;
}

/** The voice for a trigger: the parked warm one when it holds this clip, else a fresh prepared one. */
function _voice(
	id: VoiceId | 'preview',
	plan: VoicePlan,
	bus: Bus,
	onError: (e: Error) => void
): Promise<SamplerVoice> {
	const out = _out(bus);
	if (id === 'preview') return _prepared(new SamplerVoice(out.context, out.node, onError), plan);
	const parked = _warm.get(id);
	if (parked?.canRetrigger(out.context, plan.buffer, out.node) && plan.masterTempo) {
		parked.rearm(plan, onError);
		return Promise.resolve(parked);
	}
	_warm.delete(id);
	parked?.retire();
	const warm = SAMPLER_WARM_VOICES && plan.masterTempo;
	const voice = new SamplerVoice(out.context, out.node, onError, warm);
	if (warm) _warm.set(id, voice);
	return _prepared(voice, plan);
}

const _bank = new SamplerVoiceBank(
	{
		now: _now,
		master: _master,
		buffer: (slot, bus) => _pool.buffer(slot, _out(bus).context),
		voice: _voice,
		holdsClip: (id, buffer) => _warm.get(id)?.holds(buffer) === true,
		cueOffsetSec: () => _out('cue').context.currentTime - _now(),
		report: (what, e) => console.error(what, e)
	},
	samplerVoices
);

/** The engine the store plays through (sampler-engine-seam.ts). */
export const samplerEngine: SamplerEngine = {
	toggle: (slot) => _bank.toggle(slot),
	stop: (slotId) => _bank.stop(slotId),
	preview: (slot) => _bank.preview(slot),
	stopPreview: () => _bank.stopPreview(),
	reside: (view, padPins) => {
		_padPins = padPins;
		_reside(view);
	},
	phase: (slotId) => _bank.phase(slotId),
	playhead: (slotId) => _bank.playhead(slotId)
};

/** Stop every voice and the preview (the overlay unmounting). */
export function stopSamplerVoices(): void {
	_bank.stopAll();
}
