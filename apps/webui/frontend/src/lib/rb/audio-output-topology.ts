/**
 * Physical output topology for performance audio.
 *
 * Kept out of the engine's transport/DSP owner because selecting an output
 * layout is one bounded concern: ordinary stereo, external-mixer USB pairs,
 * or the Mixtour Pro's master 1/2 plus cue 3/4 layout.
 */

import { DECK_IDS } from '$lib/player/constants';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { MixerState } from '$lib/rb/mixer-types';

export type DjOutputProfile = 'master12-cue34';

export interface AudioOutputTopology {
	externalMerger: ChannelMergerNode | null;
	externalRouteAnalyser: AnalyserNode | null;
	ownedNodes: AudioNode[];
	multichannelMonitorActive: boolean;
}

export function parseDjOutputProfile(search: string): DjOutputProfile | null {
	const value = new URLSearchParams(search).get('djio');
	if (value === null || value === '') return null;
	if (value !== 'master12-cue34') {
		throw new Error(`djio: unsupported profile '${value}' (expected master12-cue34)`);
	}
	return value;
}

function _makeDiscrete(node: AudioNode & { channelCount: number }): void {
	node.channelCountMode = 'explicit';
	node.channelInterpretation = 'discrete';
}

export function wireAudioOutputTopology(input: {
	context: AudioContext;
	routing: Map<DeckId, number> | null;
	profile: DjOutputProfile | null;
	masterGain: GainNode;
	masterMuteGain: GainNode;
	masterDelay: DelayNode;
	headphoneDelay: DelayNode;
}): AudioOutputTopology {
	const { context, routing, profile, masterGain, masterMuteGain, masterDelay, headphoneDelay } =
		input;
	if (routing !== null && profile !== null) {
		throw new Error('extroute and djio are mutually exclusive output topologies');
	}
	if (routing === null && profile === null) {
		masterMuteGain.connect(masterDelay);
		masterDelay.connect(context.destination);
		return {
			externalMerger: null,
			externalRouteAnalyser: null,
			ownedNodes: [],
			multichannelMonitorActive: false
		};
	}
	if (routing !== null) {
		const highestUsbChannel = Math.max(...[...routing.values()].map((left) => left + 1));
		const dest = context.destination;
		if (dest.maxChannelCount < highestUsbChannel) {
			throw new Error(
				`extroute needs ${highestUsbChannel} output channels but the current output device exposes ` +
					`${dest.maxChannelCount} - select the multichannel interface as the system output device and reload`
			);
		}
		dest.channelCount = dest.maxChannelCount;
		dest.channelInterpretation = 'discrete';
		masterMuteGain.channelCount = dest.channelCount;
		_makeDiscrete(masterMuteGain);
		masterDelay.channelCount = dest.channelCount;
		_makeDiscrete(masterDelay);
		masterMuteGain.connect(masterDelay);
		masterDelay.connect(dest);
		const externalMerger = context.createChannelMerger(dest.channelCount);
		externalMerger.channelInterpretation = 'discrete';
		externalMerger.connect(masterMuteGain);
		const externalRouteAnalyser = context.createAnalyser();
		externalMerger.connect(externalRouteAnalyser);
		return {
			externalMerger,
			externalRouteAnalyser,
			ownedNodes: [],
			multichannelMonitorActive: false
		};
	}

	const dest = context.destination;
	if (dest.maxChannelCount < 4) {
		throw new Error(
			'djio master12-cue34 needs four output channels - select the Mixtour Pro as the macOS output device and reload'
		);
	}
	dest.channelCount = 4;
	dest.channelInterpretation = 'discrete';
	masterMuteGain.channelCount = 4;
	_makeDiscrete(masterMuteGain);
	masterDelay.channelCount = 4;
	_makeDiscrete(masterDelay);
	const merger = context.createChannelMerger(4);
	merger.channelInterpretation = 'discrete';
	const masterSplitter = context.createChannelSplitter(2);
	const cueSplitter = context.createChannelSplitter(2);
	masterGain.connect(masterSplitter);
	masterSplitter.connect(merger, 0, 0);
	masterSplitter.connect(merger, 1, 1);
	headphoneDelay.connect(cueSplitter);
	cueSplitter.connect(merger, 0, 2);
	cueSplitter.connect(merger, 1, 3);
	merger.connect(masterMuteGain);
	masterMuteGain.connect(masterDelay);
	masterDelay.connect(dest);
	return {
		externalMerger: null,
		externalRouteAnalyser: null,
		ownedNodes: [masterSplitter, cueSplitter, merger],
		multichannelMonitorActive: true
	};
}

/** True when a live deck is deliberately heard only through cue channels 3/4. */
export function cueOnlyMonitoringActive(
	profile: DjOutputProfile | null,
	mixer: MixerState,
	decks: Record<DeckId, DeckState>
): boolean {
	return (
		profile === 'master12-cue34' &&
		mixer.master <= 0 &&
		mixer.headphones.level > 0 &&
		mixer.headphones.mix < 1 &&
		DECK_IDS.some(
			(deck) => mixer.channels[deck].cue_enabled && (decks[deck].playing || decks[deck].audible)
		)
	);
}
