/** Non-reactive native objects and acknowledged transport state owned by one deck. */
import type { LoopState } from '$lib/rb/deck-state-types';
import type { StretchDeckProcessor } from '$lib/rb/stretch-adapter';
import type { AlignedStemDeckProcessor } from '$lib/rb/stem-graph';
import type { DeckChannelNodes as _ChannelNodes } from '$lib/rb/deck-channel-graph';
import { createPresentedTransportTimeline, type PresentedTransportTimeline, type SlipAnchor, type SlipTempoBoundary } from '$lib/player/transport/presentation';
import type { StemDeckState } from '$lib/rb/stem-types';

export interface DeckTransportClock {
	source: 'paused_cursor' | 'audio_output';
	presentation_context_time_s: number | null;
	desired_revision: number;
	presented_revision: number;
}

export interface _PendingSegment {
	active: boolean;
	loop: LoopState | null;
	startContextTime: number;
	startPositionSec: number;
	tempoRatio: number;
	masterTempoEnabled?: boolean;
	keyShiftSemitones?: number;
}

export type _DeckProcessor = StretchDeckProcessor | AlignedStemDeckProcessor;
export interface _DeckRuntime {
	processor: _DeckProcessor | null;
	durationSec: number;
	latencySec: number;
	controlActive: boolean;
	controlLoop: LoopState | null;
	controlTempoRatio: number;
	controlMasterTempoEnabled: boolean;
	controlKeyShiftSemitones: number;
	/** ctx.currentTime at the moment the current processor segment starts. */
	startCtxTime: number;
	/** Track offset (seconds) at the moment the segment started. */
	startOffsetSec: number;
	/** Monotonic token guarding against stale load() results. */
	loadToken: number;
	nodes: _ChannelNodes | null;
	pending: _PendingSegment[];
	presentation: PresentedTransportTimeline;
	nextScheduleRevision: number;
	desiredActive: boolean;
	scheduleIntentCount: number;
	scheduleTail: Promise<void>;
	swapTail: Promise<void>;
	slipAnchor: SlipAnchor | null;
	slipTempoBoundaries: SlipTempoBoundary[];
	/** Manual key-shift baseline captured when KEY SYNC latches on; restored
	 * on disable so the Camelot offset cannot drift away from the latch. */
	keySyncBaselineSemitones: number | null;
	audioBuffer: AudioBuffer | null; // decoded mix, retained for sync-seek crossfades and read by deckMixBuffer
	/** Library-listed track duration; decoded buffer duration lives in deck state. */
	metadataDurationMs: number | null;
	/** Monotonic token; superseding transport/sync commands bump this deck's generation. */
	reanchorOperationGeneration: number;
	/** When set, equals the generation of the ramp that owns `transport_pending`. */
	reanchorRampOwnerGeneration: number | null;
	/**
	 * LAZY-STEMS. A fully built AlignedStemDeckProcessor waiting for the deck to
	 * be replaceable, held here because the engine forbids swapping a deck's
	 * processor while it is playing or audible (assertDeckReplacementAllowed).
	 * Set by _landStems when a live handoff (STEM-47) found the deck busy on
	 * every attempt; drained by _drainPendingStemUpgrade on the next stop, or by
	 * retryStems. Null the rest of the time. `token` pins it to the load that produced it so a track swap during
	 * the fetch cannot graft one track's stems onto another's mix.
	 */
	pendingStemUpgrade: {
		token: number;
		processor: AlignedStemDeckProcessor;
		state: StemDeckState;
	} | null;
}

export function _emptyRuntime(): _DeckRuntime {
	return {
		processor: null,
		durationSec: 0,
		latencySec: 0,
		controlActive: false,
		controlLoop: null,
		controlTempoRatio: 1,
		controlMasterTempoEnabled: true,
		controlKeyShiftSemitones: 0,
		startCtxTime: 0,
		startOffsetSec: 0,
		loadToken: 0,
		nodes: null,
		pending: [],
		presentation: createPresentedTransportTimeline(0),
		nextScheduleRevision: 0,
		desiredActive: false,
		scheduleIntentCount: 0,
		scheduleTail: Promise.resolve(),
		swapTail: Promise.resolve(),
		slipAnchor: null,
		slipTempoBoundaries: [],
		keySyncBaselineSemitones: null,
		audioBuffer: null,
		metadataDurationMs: null,
		reanchorOperationGeneration: 0,
		reanchorRampOwnerGeneration: null,
		pendingStemUpgrade: null
	};
}
