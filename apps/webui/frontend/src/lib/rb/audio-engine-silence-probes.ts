/**
 * Silence watchdog deck probes and silence e2e seams kept out of
 * audio-engine.svelte.ts (file_size.max_frontend, frontend.max_fan_in).
 */

import { DECK_IDS } from '$lib/player/constants';
import { deckStates, mixerState } from '$lib/player/state.svelte';
import { noteMasterSilence } from '$lib/rb/engine-clock-reports';
import {
	masterBusSilenceIsCommanded,
	masterSilenceIsCommanded,
	type DjOutputProfile
} from '$lib/rb/deck-channel-graph';
import type { PeakEnvelope } from '$lib/rb/silence-source-pcm';
import {
	AlignedStemDeckProcessor,
	playingStemsIntentionallySilent,
	type StemPart
} from '$lib/rb/stem-graph';
import type { _DeckProcessor } from '$lib/rb/deck-runtime';

type DeckId = 1 | 2 | 3 | 4;

type DeckRuntimeSlice = {
	processor: _DeckProcessor | null;
};

type GraphSlice = {
	ctx: AudioContext | null;
	masterAnalyser: AnalyserNode | null;
	externalRouteAnalyser: AnalyserNode | null;
	profile: DjOutputProfile | null;
	externallyRouted: ReadonlyMap<DeckId, number> | null;
	masterCeilingGain: number;
};

let _deckRuntime: ((deck: DeckId) => DeckRuntimeSlice) | null = null;
let _graph: (() => GraphSlice) | null = null;

export function bindAudioEngineSilenceProbes(input: {
	deckRuntime: (deck: DeckId) => DeckRuntimeSlice;
	graph: () => GraphSlice;
}): void {
	_deckRuntime = input.deckRuntime;
	_graph = input.graph;
}

export function deckStemEnvelopes(deck: DeckId): Partial<Record<StemPart, PeakEnvelope>> | null {
	const p = _deckRuntime?.(deck).processor ?? null;
	return p instanceof AlignedStemDeckProcessor ? p.sourcePeakEnvelopes : null;
}

export function disconnectDeckProcessorForTest(deck: DeckId): void {
	_deckRuntime?.(deck).processor?.disconnect();
}

export function sampleMasterSilenceForSilenceE2e(): void {
	const { ctx, masterAnalyser, externalRouteAnalyser, profile, externallyRouted, masterCeilingGain } =
		_graph?.() ?? {
			ctx: null,
			masterAnalyser: null,
			externalRouteAnalyser: null,
			profile: null,
			externallyRouted: null,
			masterCeilingGain: 1
		};
	if (ctx === null || masterAnalyser === null) return;
	const anyTransport = DECK_IDS.some((d) => deckStates[d].playing || deckStates[d].audible);
	const stemsSilent = playingStemsIntentionallySilent(Object.values(deckStates));
	const intentionalSilence =
		masterSilenceIsCommanded(profile, mixerState, deckStates, externallyRouted, masterCeilingGain) ||
		stemsSilent;
	const busSilence =
		masterBusSilenceIsCommanded(profile, mixerState, deckStates, externallyRouted, masterCeilingGain) ||
		stemsSilent;
	noteMasterSilence(
		masterAnalyser,
		externalRouteAnalyser,
		anyTransport,
		Date.now(),
		intentionalSilence,
		busSilence
	);
}
