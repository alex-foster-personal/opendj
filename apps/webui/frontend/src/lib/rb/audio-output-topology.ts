/**
 * Physical output topology for performance audio.
 *
 * Kept out of the engine's transport/DSP owner because selecting an output
 * layout is one bounded concern: ordinary stereo, external-mixer USB pairs,
 * or the Mixtour Pro's master 1/2 plus cue 3/4 layout.
 */

import { DECK_IDS } from '$lib/player/constants';
import type { DeckState } from '$lib/rb/deck-state-types';
import type { DeckId } from '$lib/rb/deck-id';
import type { MixerState } from '$lib/rb/mixer-types';

export type DjOutputProfile = 'master12-cue34';

/** master12-cue34 carries master on 1/2 and cue on 3/4. */
export const DJIO_REQUIRED_OUTPUT_CHANNELS = 4;

export interface AudioOutputTopology {
	externalMerger: ChannelMergerNode | null;
	externalRouteAnalyser: AnalyserNode | null;
	ownedNodes: AudioNode[];
	multichannelMonitorActive: boolean;
}

/** Why a djio request is playing as ordinary stereo master instead. */
export interface DjOutputFallback {
	requested: DjOutputProfile;
	/** `AudioDestinationNode.maxChannelCount` of the output the context opened on. */
	available_channels: number;
	message: string;
}

export interface DjOutputResolution {
	requested: DjOutputProfile | null;
	/** The profile actually wired; null is ordinary stereo master. */
	profile: DjOutputProfile | null;
	fallback: DjOutputFallback | null;
}

export function parseDjOutputProfile(search: string): DjOutputProfile | null {
	const value = new URLSearchParams(search).get('djio');
	if (value === null || value === '') return null;
	if (value !== 'master12-cue34') {
		throw new Error(`djio: unsupported profile '${value}' (expected master12-cue34)`);
	}
	return value;
}

/**
 * IOPIN-12: decide what a djio request can actually wire on this output.
 *
 * The app cannot choose the macOS output device (WKWebView has no setSinkId),
 * so a connected Mixtour map can request master12-cue34 while the laptop
 * speakers are selected. That used to throw mid graph-build and take every
 * deck down with it; it now degrades to stereo master and names the fix.
 */
export function resolveDjOutputProfile(
	requested: DjOutputProfile | null,
	maxChannelCount: number
): DjOutputResolution {
	if (requested === null) return { requested, profile: null, fallback: null };
	if (maxChannelCount >= DJIO_REQUIRED_OUTPUT_CHANNELS) {
		return { requested, profile: requested, fallback: null };
	}
	const message =
		`Mixtour 4-channel output unavailable: this output has ${maxChannelCount} channels and ` +
		`${requested} needs ${DJIO_REQUIRED_OUTPUT_CHANNELS}, so master plays in stereo and cue 3/4 is off. ` +
		'Select the Mixtour Pro as the macOS output device and reload.';
	return {
		requested,
		profile: null,
		fallback: { requested, available_channels: maxChannelCount, message }
	};
}

/** Hover text for the channel count a fallback quotes (numeric readouts carry a title). */
export function djioFallbackTitle(fallback: DjOutputFallback): string {
	return (
		`${fallback.available_channels} = output channels the current macOS output device exposes to the app ` +
		`(AudioDestinationNode.maxChannelCount). ${fallback.requested} needs ${DJIO_REQUIRED_OUTPUT_CHANNELS}: ` +
		'master on 1/2, cue on 3/4.'
	);
}

/**
 * Where a connected controller's audio profile should send the page, or null
 * when the page must stay put. A page that already names djio (a stereo
 * fallback page keeps its param) is never redirected again, so the reload
 * cannot loop; an explicit empty djio is the operator opting out; extroute is
 * an explicit topology that djio would contradict.
 */
export function djioRedirectTarget(href: string, profile: DjOutputProfile): string | null {
	const url = new URL(href);
	if (url.searchParams.has('djio') || url.searchParams.has('extroute')) return null;
	url.searchParams.set('djio', profile);
	return url.toString();
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
	// The room delay (CUEOUT-14) is for master 1/2 only: cue 3/4 already
	// carries its own headphone delay, and a second delay there makes cue
	// monitoring late. So the four muted channels are split, the master pair
	// alone passes through masterDelay, and the pairs are merged again. The
	// mute stays ahead of the split so it still silences every channel, and
	// masterDelay stays the last node before the device on the master pair, so
	// a signal injected there (the cue-align chirp) is still past every gain.
	masterDelay.channelCount = 2;
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
	const mutedSplitter = context.createChannelSplitter(4);
	masterMuteGain.connect(mutedSplitter);
	const roomMerger = context.createChannelMerger(2);
	mutedSplitter.connect(roomMerger, 0, 0);
	mutedSplitter.connect(roomMerger, 1, 1);
	roomMerger.connect(masterDelay);
	const roomSplitter = context.createChannelSplitter(2);
	masterDelay.connect(roomSplitter);
	const outputMerger = context.createChannelMerger(4);
	outputMerger.channelInterpretation = 'discrete';
	roomSplitter.connect(outputMerger, 0, 0);
	roomSplitter.connect(outputMerger, 1, 1);
	mutedSplitter.connect(outputMerger, 2, 2);
	mutedSplitter.connect(outputMerger, 3, 3);
	outputMerger.connect(dest);
	return {
		externalMerger: null,
		externalRouteAnalyser: null,
		ownedNodes: [
			masterSplitter,
			cueSplitter,
			merger,
			mutedSplitter,
			roomMerger,
			roomSplitter,
			outputMerger
		],
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
