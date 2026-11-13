/**
 * The ONE per-deck channel graph builder (issue #12665).
 *
 * Production (`buildDeckChannelGraph` in deck-channel-graph.ts) and the
 * channel-meter browser harness both call this, so the harness exercises the
 * shipped node wiring and fader tap rather than a separately written copy.
 * It imports no Svelte rune module: the trim law, the FILTER build params and
 * the colour-FX build hook arrive through `deps`, because Svelte runes cannot
 * run in the Playwright esbuild bundle.
 */
import { wireFilterSide } from '$lib/rb/filter-slope';
import { createMeterTap, type MeterTap, type MeterTapSource } from '$lib/rb/meter-tap';
import type { DeckId } from '$lib/rb/deck-slots';
import type { CrossfaderAssign, MixerState } from '$lib/rb/mixer-types';
import {
	ANALYSER_FFT_SIZE,
	DECK_IDS,
	EQ_FREQ_HIGH_HZ,
	EQ_FREQ_LOW_HZ,
	EQ_FREQ_MID_HZ,
	EQ_MID_Q,
	FILTER_Q_DB,
	FILTER_SLOPE_DB_OCT,
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
	/** FXFILT-09: each side's second section at 24 dB/oct, null at 12 (`filter-slope.ts`). */
	filterLp2: BiquadFilterNode | null;
	filterHp2: BiquadFilterNode | null;
	filterDry: GainNode;
	filterLpWet: GainNode;
	filterHpWet: GainNode;
	/** COLORFX-01: unity sum of the FILTER branches; the SPACE / ECHO sends tap it. */
	colorBus: GainNode;
	cue: GainNode;
	fader: GainNode;
	xf: GainNode;
	/** STEMPERF-11663: unity sum of the deck's per-stem FX sends; the colour FX graph feeds it into fxIn when spliced. */
	stemFxSend: GainNode;
	extsplit: ChannelSplitterNode | null;
}

/** FILTER branch values a channel is built with (lowpass / highpass cutoffs and branch gains). */
export interface DeckChannelFilterParams {
	lpHz: number;
	hpHz: number;
	dryGain: number;
	lpWetGain: number;
	hpWetGain: number;
}

export interface DeckChannelGraphDeps {
	ctx: AudioContext;
	mixerState: MixerState;
	masterGain: GainNode;
	externalMerger: ChannelMergerNode | null;
	routing: ReadonlyMap<DeckId, number> | null;
	cueSum: AudioNode;
	xfGainFor: (assign: CrossfaderAssign, x: number) => number;
	onDeck: (deck: DeckId, nodes: DeckChannelNodes, tap: MeterTap) => void;
}

/** The rune-backed pieces production injects; the harness passes neutral defaults. */
export interface DeckChannelGraphHooks {
	trimStageGain: (trimKnob: number, deck: DeckId) => number;
	colorFilterParamsForBuild: (deck: DeckId, value: number) => DeckChannelFilterParams;
	colorFxChannelBuilt: (deck: DeckId, nodes: DeckChannelNodes, value: number) => void;
	/** Issue #9782: what feeds the crossfader on the master route; the fader itself, or the beat FX unit's channel insert. */
	postFaderInsert: (deck: DeckId, fader: GainNode) => AudioNode;
}

/** Build every deck's channel strip, wire it to master / cue / external outs, and return its meter taps. */
export function buildDeckChannelGraphCore(
	deps: DeckChannelGraphDeps & DeckChannelGraphHooks
): MeterTapSource[] {
	const { trimStageGain, colorFilterParamsForBuild, colorFxChannelBuilt, postFaderInsert } = deps;
	const meterSources: MeterTapSource[] = [];
	for (const deck of DECK_IDS) {
		const ch = deps.mixerState.channels[deck];
		const analyser = deps.ctx.createAnalyser();
		analyser.fftSize = ANALYSER_FFT_SIZE;
		analyser.minDecibels = -120;
		analyser.maxDecibels = 0;
		analyser.smoothingTimeConstant = 0;
		const trim = deps.ctx.createGain();
		trim.gain.value = trimStageGain(ch.trim, deck);
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
		const { lpHz, hpHz, dryGain, lpWetGain, hpWetGain } = colorFilterParamsForBuild(
			deck,
			ch.filter
		);
		const filterLp = deps.ctx.createBiquadFilter();
		filterLp.type = 'lowpass';
		filterLp.Q.value = FILTER_Q_DB;
		filterLp.frequency.value = lpHz;
		const filterHp = deps.ctx.createBiquadFilter();
		filterHp.type = 'highpass';
		filterHp.Q.value = FILTER_Q_DB;
		filterHp.frequency.value = hpHz;
		const filterDry = deps.ctx.createGain();
		filterDry.gain.value = dryGain;
		const filterLpWet = deps.ctx.createGain();
		filterLpWet.gain.value = lpWetGain;
		const filterHpWet = deps.ctx.createGain();
		filterHpWet.gain.value = hpWetGain;
		const colorBus = deps.ctx.createGain();
		const stemFxSend = deps.ctx.createGain();
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
		const filterLp2 = wireFilterSide(deps.ctx, filterLp, filterLpWet, FILTER_SLOPE_DB_OCT);
		const filterHp2 = wireFilterSide(deps.ctx, filterHp, filterHpWet, FILTER_SLOPE_DB_OCT);
		for (const branch of [filterDry, filterLpWet, filterHpWet]) branch.connect(colorBus);
		colorBus.connect(cue);
		cue.connect(deps.cueSum);
		colorBus.connect(fader);
		const usbLeft = deps.routing?.get(deck) ?? null;
		let extsplit: ChannelSplitterNode | null = null;
		if (usbLeft !== null && deps.externalMerger !== null) {
			extsplit = deps.ctx.createChannelSplitter(2);
			fader.connect(extsplit);
			extsplit.connect(deps.externalMerger, 0, usbLeft - 1);
			extsplit.connect(deps.externalMerger, 1, usbLeft);
		} else {
			postFaderInsert(deck, fader).connect(xf);
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
			filterLp2,
			filterHp2,
			filterDry,
			filterLpWet,
			filterHpWet,
			colorBus,
			cue,
			fader,
			xf,
			stemFxSend,
			extsplit
		};
		const tap = createMeterTap();
		deps.onDeck(deck, nodes, tap);
		colorFxChannelBuilt(deck, nodes, ch.filter);
		// The channel fader is the deck-volume control mounted with this meter.
		// Tap its output so the physical slider and visible light describe the
		// same audible channel level. The clip pip judges the level heard after
		// the crossfader, measured on the audio thread from `xf` itself, so the
		// gain is the one those samples played under (Sol P1, PR #6829); a deck
		// routed out past the crossfader is heard at its fader.
		meterSources.push({ tap, source: fader, heardSource: extsplit === null ? xf : fader });
	}
	return meterSources;
}
