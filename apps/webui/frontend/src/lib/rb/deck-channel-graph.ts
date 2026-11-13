import { autoGainLinear } from '$lib/rb/auto-gain.svelte';
import { beatFxRackEnabled } from '$lib/rb/beat-fx-flag';
import {
	buildBeatFxInsert,
	clearLiveBeatFxInsert,
	wireBeatFxChannel
} from '$lib/rb/beat-fx-insert';
import type { MeterTapSource } from '$lib/rb/meter-tap';
import { colorFilterParamsForBuild, colorFxChannelBuilt } from '$lib/rb/color-fx.svelte';
import {
	buildDeckChannelGraphCore,
	type DeckChannelGraphDeps,
	type DeckChannelNodes
} from '$lib/rb/deck-channel-graph-core';
import type { LoopState } from '$lib/rb/deck-state-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { StretchDeckProcessor } from '$lib/rb/stretch-adapter';
import { TRIM_MAX_GAIN } from '$lib/player/constants';

export type { DeckChannelNodes } from '$lib/rb/deck-channel-graph-core';

/** AUTOGAIN-02: the trim stage law, one place for the graph build and every
 * knob move: TRIM knob (0..1, 0.5 unity) x TRIM_MAX_GAIN x the deck's auto
 * gain, so the knob rides on top of auto gain. */
export function trimStageGain(trimKnob: number, deck: DeckId): number {
	return trimKnob * TRIM_MAX_GAIN * autoGainLinear(deck);
}

/** Set a deck's trim stage from its knob and auto gain (no-op before the graph exists). */
export function applyTrimStage(
	nodes: DeckChannelNodes | null,
	trimKnob: number,
	deck: DeckId,
	setParam: (param: AudioParam, value: number) => void
): void {
	if (nodes !== null) setParam(nodes.trim.gain, trimStageGain(trimKnob, deck));
}

// The engine reaches the trim stage's auto gain input through the module that owns the stage.
export {
	autoGainLinear,
	deckAutoGain,
	freshTrackAutoGain,
	publishDeckAutoGain,
	registerAutoGainApplier,
	setAutoGainLiveGuard,
	setDeckAutoGainUserOffset,
	settleTrackAutoGain
} from '$lib/rb/auto-gain.svelte';

/** Build the live deck channel graph through the shared builder the meter harness also runs (#12665). */
export function buildDeckChannelGraph(deps: DeckChannelGraphDeps): MeterTapSource[] {
	// Issue #9782: the beat FX unit hangs off each fader only with `fx.beat_rack` on.
	const beatFx = beatFxRackEnabled() ? buildBeatFxInsert(deps.ctx, deps.masterGain) : null;
	if (beatFx === null) clearLiveBeatFxInsert();
	return buildDeckChannelGraphCore({
		...deps,
		trimStageGain,
		colorFilterParamsForBuild,
		colorFxChannelBuilt,
		postFaderInsert: (deck, fader) =>
			beatFx === null ? fader : wireBeatFxChannel(beatFx, deck, fader)
	});
}

export function copyAudioBufferToContext(buffer: AudioBuffer, ctx: AudioContext): AudioBuffer {
	const copy = ctx.createBuffer(buffer.numberOfChannels, buffer.length, buffer.sampleRate);
	for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
		copy.copyToChannel(buffer.getChannelData(ch), ch);
	}
	return copy;
}

export interface LoadedDeckRecreateSnap {
	deck: DeckId;
	buffer: AudioBuffer;
	positionSec: number;
	playing: boolean;
	tempoRatio: number;
	masterTempoEnabled: boolean;
	keyShiftSemitones: number;
	loop: LoopState | null;
	stableId: string;
	stemsReady: boolean;
	/** A stem upgrade was in flight (STEM-37 can wait minutes on R2): the
	 * rebuild must restart it on the new context, or stems never land. */
	stemsLoading: boolean;
}

export async function reattachLoadedDeckProcessors(deps: {
	ctx: AudioContext;
	snaps: readonly LoadedDeckRecreateSnap[];
	getNodes: (deck: DeckId) => DeckChannelNodes | null;
	createMixProcessor: (
		deck: DeckId,
		ctx: AudioContext,
		buffer: AudioBuffer
	) => Promise<{
		processor: { connect(node: AudioNode): void; latencySec(): Promise<number> };
		buffer: AudioBuffer;
	}>;
	onPresentationReset: (deck: DeckId, positionSec: number) => void;
	onProcessorAttached: (
		deck: DeckId,
		processor: { connect(node: AudioNode): void; latencySec(): Promise<number> },
		buffer: AudioBuffer,
		latencySec: number
	) => void;
	schedulePlayingDeck: (snap: LoadedDeckRecreateSnap, ctx: AudioContext) => Promise<void>;
	maybeUpgradeStems: (snap: LoadedDeckRecreateSnap, buffer: AudioBuffer, ctx: AudioContext) => void;
}): Promise<void> {
	for (const snap of deps.snaps) {
		const nodes = deps.getNodes(snap.deck);
		if (nodes === null) throw new Error(`recreate: deck ${snap.deck} channel graph is missing`);
		const { processor, buffer } = await deps.createMixProcessor(snap.deck, deps.ctx, snap.buffer);
		processor.connect(nodes.analyser);
		const latencySec = await processor.latencySec();
		deps.onPresentationReset(snap.deck, snap.positionSec);
		deps.onProcessorAttached(snap.deck, processor, buffer, latencySec);
		if (snap.playing) await deps.schedulePlayingDeck(snap, deps.ctx);
		deps.maybeUpgradeStems(snap, buffer, deps.ctx);
	}
}

export async function rebuildKeepingLoadedDecks(deps: {
	decks: readonly DeckId[];
	readSnap: (deck: DeckId) => LoadedDeckRecreateSnap | null;
	releasePendingStemUpgrade: (deck: DeckId) => void;
	detachProcessor: (deck: DeckId) => unknown;
	collectNodes: (deck: DeckId) => readonly AudioNode[];
	clearDeckGraph: (deck: DeckId) => void;
	disarmInstrumentation: () => void;
	extraDisposeNodes: () => readonly AudioNode[];
	disposeResources: (resources: { processors: unknown[]; nodes: AudioNode[] }) => Promise<void>;
	resetGraphState: () => void;
	ensureGraph: () => AudioContext;
	reattach: (ctx: AudioContext, snaps: readonly LoadedDeckRecreateSnap[]) => Promise<void>;
}): Promise<void> {
	const snaps: LoadedDeckRecreateSnap[] = [];
	const processors: unknown[] = [];
	const nodes: AudioNode[] = [];
	for (const deck of deps.decks) {
		deps.releasePendingStemUpgrade(deck);
		const snap = deps.readSnap(deck);
		if (snap === null) continue;
		snaps.push(snap);
		const proc = deps.detachProcessor(deck);
		if (proc !== null) processors.push(proc);
		nodes.push(...deps.collectNodes(deck));
		deps.clearDeckGraph(deck);
	}
	deps.disarmInstrumentation();
	nodes.push(...deps.extraDisposeNodes());
	try {
		await deps.disposeResources({ processors, nodes });
	} finally {
		// IOPIN-12: reset even when the old graph's teardown rejects. Every deck above
		// has had its nodes cleared, so a skipped reset keeps the old context installed
		// and each later load fails "audio graph is missing" for the rest of the session.
		deps.resetGraphState();
	}
	const ctx = deps.ensureGraph();
	await deps.reattach(ctx, snaps);
}

type MixProcessor = Awaited<ReturnType<typeof StretchDeckProcessor.create>>;
type DisposableProcessor = Pick<MixProcessor, 'disconnect' | 'dispose'>;

export function snapLoadedDeck(
	deck: DeckId,
	rt: {
		audioBuffer: AudioBuffer | null;
		desiredActive: boolean;
		controlTempoRatio: number;
		controlMasterTempoEnabled: boolean;
		controlKeyShiftSemitones: number;
		controlLoop: LoopState | null;
	},
	st: { position_ms: number; stable_id: string | null; playing: boolean; stems: { status: string } }
): LoadedDeckRecreateSnap | null {
	if (rt.audioBuffer === null) return null;
	return {
		deck,
		buffer: rt.audioBuffer,
		positionSec: st.position_ms / 1000,
		playing: rt.desiredActive || st.playing,
		tempoRatio: rt.controlTempoRatio,
		masterTempoEnabled: rt.controlMasterTempoEnabled,
		keyShiftSemitones: rt.controlKeyShiftSemitones,
		loop: rt.controlLoop,
		stableId: st.stable_id ?? '',
		stemsReady: st.stems.status === 'ready',
		stemsLoading: st.stems.status === 'loading'
	};
}

export interface GraphRecreateEngineAccess {
	decks: readonly DeckId[];
	runtime(deck: DeckId): {
		audioBuffer: AudioBuffer | null;
		nodes: DeckChannelNodes | null;
		processor: { connect(destination: AudioNode): void; latencySec(): Promise<number> } | null;
		desiredActive: boolean;
		controlTempoRatio: number;
		controlMasterTempoEnabled: boolean;
		controlKeyShiftSemitones: number;
		controlLoop: LoopState | null;
		loadToken: number;
		pending: unknown[];
		presentation: unknown;
		nextScheduleRevision: number;
	};
	state(deck: DeckId): {
		position_ms: number;
		stable_id: string | null;
		playing: boolean;
		stems: { status: string };
	};
	releasePendingStemUpgrade(deck: DeckId): void;
	detachProcessor(deck: DeckId): DisposableProcessor | null;
	disarmInstrumentation(): void;
	muteNode(): GainNode | null;
	disposeResources(resources: {
		processors: DisposableProcessor[];
		nodes: AudioNode[];
	}): Promise<void>;
	resetGraphState(): void;
	ensureGraph(): AudioContext;
	resetPresentation(deck: DeckId, positionSec: number): void;
	attachProcessor(
		deck: DeckId,
		processor: MixProcessor,
		buffer: AudioBuffer,
		latencySec: number
	): void;
	processorFailed(deck: DeckId, processor: MixProcessor, error: unknown): void;
	schedulePlayingDeck(snap: LoadedDeckRecreateSnap, ctx: AudioContext): Promise<void>;
	maybeUpgradeStems(snap: LoadedDeckRecreateSnap, buffer: AudioBuffer, ctx: AudioContext): void;
}

export async function recreateFromEngineAccess(access: GraphRecreateEngineAccess): Promise<void> {
	await recreateEngineGraph({
		decks: access.decks,
		readSnap: (deck) => snapLoadedDeck(deck, access.runtime(deck), access.state(deck)),
		releasePendingStemUpgrade: (deck) => access.releasePendingStemUpgrade(deck),
		detachProcessor: (deck) => access.detachProcessor(deck),
		collectNodes: (deck) => {
			const nodes = access.runtime(deck).nodes;
			return nodes === null
				? []
				: Object.values(nodes).filter((node): node is AudioNode => node !== null);
		},
		clearDeckGraph: (deck) => {
			const rt = access.runtime(deck);
			rt.processor = null;
			rt.nodes = null;
			rt.pending = [];
		},
		getNodes: (deck) => access.runtime(deck).nodes,
		onPresentationReset: (deck, positionSec) => access.resetPresentation(deck, positionSec),
		onProcessorAttached: (deck, processor, buffer, latencySec) =>
			access.attachProcessor(deck, processor, buffer, latencySec),
		onProcessorFailure: (deck, processor, error) => access.processorFailed(deck, processor, error),
		schedulePlayingDeck: (snap, ctx) => access.schedulePlayingDeck(snap, ctx),
		maybeUpgradeStems: (snap, buffer, ctx) => access.maybeUpgradeStems(snap, buffer, ctx),
		disarmInstrumentation: () => access.disarmInstrumentation(),
		extraDisposeNodes: () => (access.muteNode() === null ? [] : [access.muteNode()!]),
		disposeResources: (resources) => access.disposeResources(resources),
		resetGraphState: () => access.resetGraphState(),
		ensureGraph: () => access.ensureGraph()
	});
}

export interface EngineGraphRecreateBindings {
	decks: readonly DeckId[];
	readSnap(deck: DeckId): LoadedDeckRecreateSnap | null;
	releasePendingStemUpgrade(deck: DeckId): void;
	detachProcessor(deck: DeckId): DisposableProcessor | null;
	collectNodes(deck: DeckId): readonly AudioNode[];
	clearDeckGraph(deck: DeckId): void;
	getNodes(deck: DeckId): DeckChannelNodes | null;
	onPresentationReset(deck: DeckId, positionSec: number): void;
	onProcessorAttached(
		deck: DeckId,
		processor: MixProcessor,
		buffer: AudioBuffer,
		latencySec: number
	): void;
	onProcessorFailure(deck: DeckId, processor: MixProcessor, error: unknown): void;
	schedulePlayingDeck(snap: LoadedDeckRecreateSnap, ctx: AudioContext): Promise<void>;
	maybeUpgradeStems(snap: LoadedDeckRecreateSnap, buffer: AudioBuffer, ctx: AudioContext): void;
	disarmInstrumentation(): void;
	extraDisposeNodes(): readonly AudioNode[];
	disposeResources(resources: {
		processors: DisposableProcessor[];
		nodes: AudioNode[];
	}): Promise<void>;
	resetGraphState(): void;
	ensureGraph(): AudioContext;
}

/** Issue #2155: close the live graph and rebuild it with loaded decks attached. */
export async function recreateEngineGraph(bindings: EngineGraphRecreateBindings): Promise<void> {
	await rebuildKeepingLoadedDecks({
		decks: bindings.decks,
		readSnap: (deck) => bindings.readSnap(deck),
		releasePendingStemUpgrade: (deck) => bindings.releasePendingStemUpgrade(deck),
		detachProcessor: (deck) => bindings.detachProcessor(deck),
		collectNodes: (deck) => bindings.collectNodes(deck),
		clearDeckGraph: (deck) => bindings.clearDeckGraph(deck),
		disarmInstrumentation: () => bindings.disarmInstrumentation(),
		extraDisposeNodes: () => bindings.extraDisposeNodes(),
		disposeResources: (resources) =>
			bindings.disposeResources({
				processors: resources.processors as DisposableProcessor[],
				nodes: resources.nodes
			}),
		resetGraphState: () => bindings.resetGraphState(),
		ensureGraph: () => bindings.ensureGraph(),
		reattach: (ctx, snaps) =>
			reattachLoadedDeckProcessors({
				ctx,
				snaps,
				getNodes: (deck) => bindings.getNodes(deck),
				createMixProcessor: async (deck, mixCtx, buffer) => {
					let mixBuffer = buffer;
					let processor = await StretchDeckProcessor.create(mixCtx, {
						onProcessorError: (error: unknown) => {
							bindings.onProcessorFailure(deck, processor, error);
						}
					});
					try {
						await processor.load(mixBuffer);
					} catch {
						mixBuffer = copyAudioBufferToContext(mixBuffer, mixCtx);
						await processor.load(mixBuffer);
					}
					return { processor, buffer: mixBuffer };
				},
				onPresentationReset: (deck, positionSec) => bindings.onPresentationReset(deck, positionSec),
				onProcessorAttached: (deck, processor, buffer, latencySec) =>
					bindings.onProcessorAttached(deck, processor as MixProcessor, buffer, latencySec),
				schedulePlayingDeck: (snap, scheduleCtx) => bindings.schedulePlayingDeck(snap, scheduleCtx),
				maybeUpgradeStems: (snap, buffer, stemCtx) =>
					bindings.maybeUpgradeStems(snap, buffer, stemCtx)
			})
	});
}

// Re-exported so audio-engine.svelte.ts, already coupled to this module for
// its channel graph, does not take a separate direct fan-out edge for DJ
// output-topology wiring (same engine-graph concern, different file).
export {
	cueOnlyMonitoringActive,
	masterSilenceIsCommanded,
	masterBusSilenceIsCommanded,
	masterBusPathGain,
	parseDjOutputProfile,
	resolveDjOutputProfile,
	wireAudioOutputTopology,
	type DjOutputProfile
} from '$lib/rb/audio-output-topology';
export {
	clearDjOutputResolution,
	publishDjOutputResolution
} from '$lib/rb/audio-output-status.svelte';
// Bug #58 graph-rebuild retry and the engine-recovery stop marker, re-exported
// for audio-engine.svelte.ts so that file stays inside the import fan-out ratchet.
export {
	ENGINE_RECOVERED_PRESS_PLAY,
	GRAPH_REBUILD_EXHAUSTED_MESSAGE,
	GRAPH_REBUILD_RETRY_DELAYS_MS,
	planResumeAfterRebuild,
	rebuildGraphWithRetries,
	reattachingDeckMessage
} from '$lib/rb/graph-rebuild-retry';
export { markEngineRecoveryStop } from '$lib/rb/engine-recovery-stop';
export {
	createGraphRebuildRetryController,
	type GraphRebuildRetryController,
	type GraphRebuildRetryControllerPorts,
	type GraphRebuildRetryDeckRuntime,
	type GraphRebuildRetryDeckState
} from '$lib/rb/graph-rebuild-retry-controller';
export {
	resetStretchAddModuleTimeoutMs,
	setStretchAddModuleTimeoutMs
} from '$lib/rb/stretch-worklet-ready';
// COLORFX-01: the color knob's per-effect drive, re-exported for the same reason.
export { applyColorKnob } from '$lib/rb/color-fx.svelte';
