/**
 * Browser entry exposing the REAL deck-channel graph and meter-tap module to
 * the meter-artifact Playwright suite (issue #3529).
 *
 * WHY THIS EXISTS. A source-level grep that `meterSources.push({ tap, source:
 * fader })` exists proves the wiring is written down, not that a live
 * AudioContext graph actually moves the meter when `fader.gain` changes. That
 * is the same "test the shape, not the code" failure the master-meter harness
 * was added to close (Sol thread 3967976238). This entry bundles the shipped
 * `buildDeckChannelGraph` and `readMeterTap` so the suite can drive a real
 * oscillator through the production channel chain and assert post-fader metering.
 *
 * One entry, not several imports: meter-tap.ts keeps module-level state, so
 * two bundles would give two unrelated copies (see master-meter-browser-entry.ts).
 */
import { buildDeckChannelGraph, type DeckChannelNodes } from '$lib/rb/deck-channel-graph';
import type { DeckId } from '$lib/rb/deck-slots';
import { dbfsFromAmplitude } from '$lib/rb/meter-math';
import {
	SILENT_METER_READING,
	attachMeterTaps,
	meterClockMs,
	readMeterTap,
	teardownMeterTaps,
	type MeterTap
} from '$lib/rb/meter-tap';
import type { CrossfaderAssign, MixerChannelState, MixerState } from '$lib/rb/mixer-types';

const TEST_DECK = 1 as DeckId;

function xfGainFor(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}

function neutralChannel(deck_id: DeckId, fader: number): MixerChannelState {
	return {
		deck_id,
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		filter: 0.5,
		fader,
		assign: 'THRU',
		cue_enabled: false,
		stem_eq_mode: false
	};
}

function neutralMixerState(fader: number): MixerState {
	return {
		channels: {
			1: neutralChannel(1, fader),
			2: neutralChannel(2, fader),
			3: neutralChannel(3, fader),
			4: neutralChannel(4, fader)
		},
		crossfader: 0.5,
		master: 1,
		headphones: {
			mix: 0,
			level: 0.5,
			selected_output_device_id: null,
			selected_master_output_device_id: null,
			selected_input_device_id: null,
			output_mode: 'practice',
			head_delay_ms: 0,
			alignment_mode: 'headphones_only',
			master_delay_ms: 0,
			calibration: {
				step: 'idle',
				cue_latency_ms: null,
				master_latency_ms: null,
				offset_ms: null,
				error: null
			},
			outputs: [],
			inputs: [],
			supported: false,
			active: false,
			error: null
		}
	};
}

async function settleChannelDb(
	tap: MeterTap,
	targetDb: number,
	timeoutMs = 20_000
): Promise<number> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const reading = readMeterTap(tap, meterClockMs());
		if (Math.abs(reading.db - targetDb) <= 0.5) return reading.db;
		if (Date.now() > deadline) {
			throw new Error(`channel meter never settled near ${targetDb} dBFS; last reading was ${reading.db}`);
		}
		await new Promise((r) => setTimeout(r, 20));
	}
}

/** Peak below which a worklet observation counts as digital silence. */
const SILENT_OBSERVATION_PEAK = 1e-6;

/**
 * Wait for the worklet's transition-into-silence post after `afterSeq`. The
 * processor posts exactly one silent observation when signal ends, then stops
 * posting until signal returns, so this must not wait for a later seq.
 */
async function waitForSilentObservationAfter(
	tap: MeterTap,
	afterSeq: number,
	timeoutMs = 200
): Promise<{ seq: number; peak: number }> {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		const observation = tap.latest;
		if (
			observation !== null &&
			observation.seq > afterSeq &&
			observation.peak <= SILENT_OBSERVATION_PEAK
		) {
			return observation;
		}
		if (Date.now() > deadline) {
			throw new Error(
				`channel meter never posted silence after seq ${afterSeq}; latest=${JSON.stringify(tap.latest)}`
			);
		}
		await new Promise((r) => setTimeout(r, 5));
	}
}

/**
 * Drive the production deck channel graph at each requested channel-fader gain
 * and return the settled dBFS readings from the production meter tap.
 */
async function readChannelMeterAtFaderGains(faderGains: number[]): Promise<{ readings: number[] }> {
	const ctx = new AudioContext();
	if (ctx.state === 'suspended') await ctx.resume();

	const masterGain = new GainNode(ctx, { gain: 1 });
	const cueSum = new GainNode(ctx, { gain: 0 });
	const sink = new GainNode(ctx, { gain: 0 });
	masterGain.connect(sink);
	cueSum.connect(sink);
	sink.connect(ctx.destination);

	let capturedNodes: DeckChannelNodes | undefined;
	let capturedTap: MeterTap | undefined;

	const meterSources = buildDeckChannelGraph({
		ctx,
		mixerState: neutralMixerState(faderGains[0] ?? 1),
		masterGain,
		externalMerger: null,
		routing: null,
		cueSum,
		xfGainFor,
		onDeck: (deck, nodes, tap) => {
			if (deck === TEST_DECK) {
				capturedNodes = nodes;
				capturedTap = tap;
			}
		}
	});

	if (capturedNodes === undefined || capturedTap === undefined) {
		throw new Error('buildDeckChannelGraph did not register the test deck');
	}
	const deckNodes = capturedNodes;
	const deckTap = capturedTap;

	await attachMeterTaps(ctx, meterSources);

	const osc = new OscillatorNode(ctx, { frequency: 1000 });
	osc.connect(deckNodes.analyser);
	osc.start();

	const readings: number[] = [];
	for (const value of faderGains) {
		deckNodes.fader.gain.setValueAtTime(value, ctx.currentTime);
		const peak = Math.max(value, 1e-7);
		const targetDb = Math.max(20 * Math.log10(peak), SILENT_METER_READING.db);
		readings.push(await settleChannelDb(deckTap, targetDb));
	}

	osc.stop();
	teardownMeterTaps();
	await ctx.close();
	return { readings };
}

/**
 * Close the channel fader and return the first worklet observation after the
 * close. METERCAL-05 requires the floor on the next meter observation, not
 * after eventual PPM decay.
 */
async function readChannelMeterFloorsOnNextObservation(): Promise<{
	unityDb: number;
	closedObservationPeak: number;
	closedObservationDb: number;
}> {
	const ctx = new AudioContext();
	if (ctx.state === 'suspended') await ctx.resume();

	const masterGain = new GainNode(ctx, { gain: 1 });
	const cueSum = new GainNode(ctx, { gain: 0 });
	const sink = new GainNode(ctx, { gain: 0 });
	masterGain.connect(sink);
	cueSum.connect(sink);
	sink.connect(ctx.destination);

	let capturedNodes: DeckChannelNodes | undefined;
	let capturedTap: MeterTap | undefined;

	const meterSources = buildDeckChannelGraph({
		ctx,
		mixerState: neutralMixerState(1),
		masterGain,
		externalMerger: null,
		routing: null,
		cueSum,
		xfGainFor,
		onDeck: (deck, nodes, tap) => {
			if (deck === TEST_DECK) {
				capturedNodes = nodes;
				capturedTap = tap;
			}
		}
	});

	if (capturedNodes === undefined || capturedTap === undefined) {
		throw new Error('buildDeckChannelGraph did not register the test deck');
	}
	const deckNodes = capturedNodes;
	const deckTap = capturedTap;

	await attachMeterTaps(ctx, meterSources);

	const osc = new OscillatorNode(ctx, { frequency: 1000 });
	osc.connect(deckNodes.analyser);
	osc.start();

	const unityDb = await settleChannelDb(deckTap, 0);
	const afterSeq = deckTap.latest?.seq ?? 0;

	// Close the fader and wait for the worklet's one transition-into-silence
	// post. That is the next meter observation carrying the closed-fader level;
	// sustained silence posts nothing further, and PPM display decay is separate.
	deckNodes.fader.gain.setValueAtTime(0, ctx.currentTime);
	const observation = await waitForSilentObservationAfter(deckTap, afterSeq);
	const closedObservationDb = dbfsFromAmplitude(observation.peak);

	osc.stop();
	teardownMeterTaps();
	await ctx.close();
	return {
		unityDb,
		closedObservationPeak: observation.peak,
		closedObservationDb
	};
}

const harness = {
	SILENT_METER_READING,
	readChannelMeterAtFaderGains,
	readChannelMeterFloorsOnNextObservation
} as const;

export type ChannelMeterHarness = typeof harness;

declare global {
	interface Window {
		__channelMeterHarness?: ChannelMeterHarness;
	}
}

window.__channelMeterHarness = harness;
