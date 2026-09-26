import { createMeterTap, type MeterTap, type MeterTapSource } from '$lib/rb/meter-tap';
import { filterParamsFromKnob } from '$lib/rb/audio-engine-guards';
import type { LoopState } from '$lib/rb/deck-state-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { StretchDeckProcessor } from '$lib/rb/stretch-adapter';
import type { CrossfaderAssign, MixerState } from '$lib/rb/mixer-types';
import {
	ANALYSER_FFT_SIZE,
	DECK_IDS,
	EQ_FREQ_HIGH_HZ,
	EQ_FREQ_LOW_HZ,
	EQ_FREQ_MID_HZ,
	EQ_MID_Q,
	FILTER_Q,
	TRIM_MAX_GAIN,
	eqDbFromKnob
} from '$lib/player/constants';

export interface DeckChannelNodes {
	analyser: AnalyserNode;
	trim: GainNode;
	low: BiquadFilterNode;
	mid: BiquadFilterNode;
	high: BiquadFilterNode;
	filterLp: BiquadFilterNode;
	filterHp: BiquadFilterNode;
	filterDry: GainNode;
	filterLpWet: GainNode;
	filterHpWet: GainNode;
	cue: GainNode;
	fader: GainNode;
	xf: GainNode;
	extsplit: ChannelSplitterNode | null;
}

export function buildDeckChannelGraph(deps: {
	ctx: AudioContext;
	mixerState: MixerState;
	masterGain: GainNode;
	externalMerger: ChannelMergerNode | null;
	routing: ReadonlyMap<DeckId, number> | null;
	cueSum: AudioNode;
	xfGainFor: (assign: CrossfaderAssign, x: number) => number;
	onDeck: (deck: DeckId, nodes: DeckChannelNodes, tap: MeterTap) => void;
}): MeterTapSource[] {
	const meterSources: MeterTapSource[] = [];
	for (const deck of DECK_IDS) {
		const ch = deps.mixerState.channels[deck];
		const analyser = deps.ctx.createAnalyser();
		analyser.fftSize = ANALYSER_FFT_SIZE;
		analyser.minDecibels = -120;
		analyser.maxDecibels = 0;
		analyser.smoothingTimeConstant = 0;
		const trim = deps.ctx.createGain();
		trim.gain.value = ch.trim * TRIM_MAX_GAIN;
		const low = deps.ctx.createBiquadFilter();
		low.type = 'lowshelf';
		low.frequency.value = EQ_FREQ_LOW_HZ;
		low.gain.value = eqDbFromKnob(ch.eq_low);
		const mid = deps.ctx.createBiquadFilter();
		mid.type = 'peaking';
		mid.frequency.value = EQ_FREQ_MID_HZ;
		mid.Q.value = EQ_MID_Q;
		mid.gain.value = eqDbFromKnob(ch.eq_mid);
		const high = deps.ctx.createBiquadFilter();
		high.type = 'highshelf';
		high.frequency.value = EQ_FREQ_HIGH_HZ;
		high.gain.value = eqDbFromKnob(ch.eq_high);
		const { lpHz, hpHz, dryGain, lpWetGain, hpWetGain } = filterParamsFromKnob(ch.filter);
		const filterLp = deps.ctx.createBiquadFilter();
		filterLp.type = 'lowpass';
		filterLp.Q.value = FILTER_Q;
		filterLp.frequency.value = lpHz;
		const filterHp = deps.ctx.createBiquadFilter();
		filterHp.type = 'highpass';
		filterHp.Q.value = FILTER_Q;
		filterHp.frequency.value = hpHz;
		const filterDry = deps.ctx.createGain();
		filterDry.gain.value = dryGain;
		const filterLpWet = deps.ctx.createGain();
		filterLpWet.gain.value = lpWetGain;
		const filterHpWet = deps.ctx.createGain();
		filterHpWet.gain.value = hpWetGain;
		const cue = deps.ctx.createGain();
		cue.gain.value = ch.cue_enabled ? 1 : 0;
		const fader = deps.ctx.createGain();
		fader.gain.value = ch.fader;
		const xf = deps.ctx.createGain();
		xf.gain.value = deps.xfGainFor(ch.assign, deps.mixerState.crossfader);
		analyser.connect(trim);
		trim.connect(low);
		low.connect(mid);
		mid.connect(high);
		for (const stage of [filterDry, filterLp, filterHp]) high.connect(stage);
		filterLp.connect(filterLpWet);
		filterHp.connect(filterHpWet);
		for (const branch of [filterDry, filterLpWet, filterHpWet]) branch.connect(cue);
		cue.connect(deps.cueSum);
		for (const branch of [filterDry, filterLpWet, filterHpWet]) branch.connect(fader);
		const usbLeft = deps.routing?.get(deck) ?? null;
		let extsplit: ChannelSplitterNode | null = null;
		if (usbLeft !== null && deps.externalMerger !== null) {
			extsplit = deps.ctx.createChannelSplitter(2);
			fader.connect(extsplit);
			extsplit.connect(deps.externalMerger, 0, usbLeft - 1);
			extsplit.connect(deps.externalMerger, 1, usbLeft);
		} else {
			fader.connect(xf);
			xf.connect(deps.masterGain);
		}
		const nodes: DeckChannelNodes = {
			analyser,
			trim,
			low,
			mid,
			high,
			filterLp,
			filterHp,
			filterDry,
			filterLpWet,
			filterHpWet,
			cue,
			fader,
			xf,
			extsplit
		};
		const tap = createMeterTap();
		deps.onDeck(deck, nodes, tap);
		// The channel fader is the deck-volume control mounted with this meter.
		// Tap its output so the physical slider and visible light describe the
		// same audible channel level.
		meterSources.push({ tap, source: fader });
	}
	return meterSources;
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
	) => Promise<{ processor: { connect(node: AudioNode): void; latencySec(): Promise<number> }; buffer: AudioBuffer }>;
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
	disposeResources: (resources: {
		processors: unknown[];
		nodes: AudioNode[];
	}) => Promise<void>;
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
	await deps.disposeResources({ processors, nodes });
	deps.resetGraphState();
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
	disposeResources(resources: { processors: DisposableProcessor[]; nodes: AudioNode[] }): Promise<void>;
	resetGraphState(): void;
	ensureGraph(): AudioContext;
	resetPresentation(deck: DeckId, positionSec: number): void;
	attachProcessor(deck: DeckId, processor: MixProcessor, buffer: AudioBuffer, latencySec: number): void;
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
			return nodes === null ? [] : Object.values(nodes).filter((node): node is AudioNode => node !== null);
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
	onProcessorAttached(deck: DeckId, processor: MixProcessor, buffer: AudioBuffer, latencySec: number): void;
	onProcessorFailure(deck: DeckId, processor: MixProcessor, error: unknown): void;
	schedulePlayingDeck(snap: LoadedDeckRecreateSnap, ctx: AudioContext): Promise<void>;
	maybeUpgradeStems(snap: LoadedDeckRecreateSnap, buffer: AudioBuffer, ctx: AudioContext): void;
	disarmInstrumentation(): void;
	extraDisposeNodes(): readonly AudioNode[];
	disposeResources(resources: { processors: DisposableProcessor[]; nodes: AudioNode[] }): Promise<void>;
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
				maybeUpgradeStems: (snap, buffer, stemCtx) => bindings.maybeUpgradeStems(snap, buffer, stemCtx)
			})
	});
}

// Re-exported so audio-engine.svelte.ts, already coupled to this module for
// its channel graph, does not take a separate direct fan-out edge for DJ
// output-topology wiring (same engine-graph concern, different file).
export {
	cueOnlyMonitoringActive,
	parseDjOutputProfile,
	wireAudioOutputTopology,
	type DjOutputProfile
} from '$lib/rb/audio-output-topology';
