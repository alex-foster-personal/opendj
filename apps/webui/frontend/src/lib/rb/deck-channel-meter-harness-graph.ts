/**
 * Production channel graph for the meter-artifact browser harness only.
 *
 * Same fader tap and routing as `buildDeckChannelGraph`, without importing
 * color-fx.svelte or auto-gain.svelte (Svelte runes cannot run in a Playwright
 * esbuild bundle). The harness uses neutral FILTER / trim settings, which match
 * the production graph at defaults.
 */
import { colorFxTargets } from '$lib/rb/color-fx';
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
	FILTER_Q,
	TRIM_MAX_GAIN,
	eqDbFromKnob
} from '$lib/player/constants';

import type { DeckChannelNodes } from '$lib/rb/deck-channel-graph';

export function buildDeckChannelMeterHarnessGraph(deps: {
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
		const { lpHz, hpHz, dryGain, lpWetGain, hpWetGain } = colorFxTargets(
			'filter',
			ch.filter,
			false
		).filter;
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
		const colorBus = deps.ctx.createGain();
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
			colorBus,
			cue,
			fader,
			xf,
			extsplit
		};
		const tap = createMeterTap();
		deps.onDeck(deck, nodes, tap);
		meterSources.push({ tap, source: fader, heardSource: extsplit === null ? xf : fader });
	}
	return meterSources;
}
