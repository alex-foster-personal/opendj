// requirement: CUEOUT-14 (room delay node and the presentation clock that must lag with it)
// [if] master delay is set to N ms [then] a click on the master bus reaches ctx.destination N ms later (loopback, clickTrainLagMs pattern) and the headphone monitor tap is NOT delayed
// [if] master delay is N ms [then] the presented playhead lags the render clock by N ms (unit test on the timeline offset)
// [if] the master delay node is not the LAST node before ctx.destination, after the mute gain [then] ?muted=1 or a meter tap is delayed - broken
// [if] press-audible's input_to_output_ms ignores the room delay [then] the budget number flatters the room by N ms - broken
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SAMPLE_RATE = 48000;
const BUFFER_SIZE = 128;
const CLICK_OPTS = { sampleRate: SAMPLE_RATE, bufferSize: BUFFER_SIZE, clickPeriodMs: 100, clickCount: 4 };
const BUFFER_TOLERANCE_MS = (BUFFER_SIZE / SAMPLE_RATE) * 1000;

function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

/** A recording AudioContext: every node remembers its edges, params record writes. */
function fakeAudioContext() {
	const edges = [];
	const ctx = { currentTime: 0, sampleRate: SAMPLE_RATE };
	const param = () => {
		const p = { value: 0, setTargetAtTime(v) { p.value = v; }, setValueAtTime(v) { p.value = v; } };
		return p;
	};
	const node = (kind, extra = {}) => ({
		kind,
		context: ctx,
		gain: param(),
		connect(to) { edges.push([this, to]); return to; },
		disconnect() {},
		...extra
	});
	ctx.destination = node('destination', { maxChannelCount: 2 });
	ctx.createGain = () => node('gain');
	ctx.createDelay = (maxDelayTime) => node('delay', { maxDelayTime, delayTime: param() });
	ctx.createChannelSplitter = () => node('splitter');
	ctx.createChannelMerger = () => node('merger');
	ctx.createMediaStreamDestination = () => node('msd', { stream: { getTracks: () => [] } });
	ctx.createAnalyser = () => node('analyser');
	return { ctx, edges };
}

function ancestorsOf(target, edges) {
	const seen = new Set();
	const stack = [target];
	while (stack.length > 0) {
		const current = stack.pop();
		for (const [from, to] of edges) {
			if (to === current && !seen.has(from)) {
				seen.add(from);
				stack.push(from);
			}
		}
	}
	return seen;
}

let headphones;
let presentation;
let pressAudible;
let scheduleMath;

before(async () => {
	installFakeWindow();
	globalThis.Audio = class { pause() {} };
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	presentation = await loadTypeScriptModule('src/lib/player/transport/presentation.ts');
	pressAudible = await loadTypeScriptModule('src/lib/player/transport/press-audible.ts');
	scheduleMath = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

describe('the room delay node', () => {
	test('click train lag on the master bus matches the room delay within one buffer, up to 1500 ms', () => {
		for (const delayMs of [0, 700, 1500]) {
			const lagMs = headphones.clickTrainLagMs({ delayMs, bus: 'master', ...CLICK_OPTS });
			assert.ok(Math.abs(lagMs - delayMs) <= BUFFER_TOLERANCE_MS, `room delay ${delayMs} ms measured ${lagMs} ms`);
		}
		assert.throws(() => headphones.clickTrainLagMs({ delayMs: 1501, bus: 'master', ...CLICK_OPTS }), /master delay/);
		assert.throws(() => headphones.clickTrainLagMs({ delayMs: 700, ...CLICK_OPTS }), /head delay/, 'the cue bus keeps its 500 ms contract');
	});

	test('createMasterDelayNode: 2 s line, seeded from persisted master_delay_ms, written by setMasterDelayMs via setValueAtTime', () => {
		const { ctx } = fakeAudioContext();
		headphones.setMasterDelayMs(250);
		const node = headphones.createMasterDelayNode(ctx);
		assert.equal(node.kind, 'delay');
		assert.equal(node.maxDelayTime, 2);
		assert.equal(node.delayTime.value, 0.25, 'if the node is not seeded from the persisted value then a reload plays the room undelayed until the next setter call - broken');
		headphones.setMasterDelayMs(700);
		assert.equal(node.delayTime.value, 0.7);
		headphones.setMasterDelayMs(0);
		assert.equal(node.delayTime.value, 0);
	});

	test('provisional calibration delays move the graph without reaching storage', () => {
		const { ctx } = fakeAudioContext();
		headphones.setMasterDelayMs(100);
		headphones.setHeadDelayMs(0);
		const node = headphones.createMasterDelayNode(ctx);
		const stored = () => JSON.parse(globalThis.window.localStorage.getItem('mdt.rb.mixer-config.v1') ?? '{}');
		const before = stored();
		headphones.applyUnsavedAlignmentDelays({ head_delay_ms: 40, master_delay_ms: 900 });
		assert.equal(node.delayTime.value, 0.9, 'if the provisional room delay is not applied then verification measures the old plan - broken');
		assert.deepEqual([stored().master_delay_ms, stored().head_delay_ms], [before.master_delay_ms, before.head_delay_ms],
			'if a provisional delay reaches storage then a reload mid-verification boots with an unverified room delay - broken');
		headphones.applyUnsavedAlignmentDelays({ head_delay_ms: 0, master_delay_ms: 100 });
		assert.equal(node.delayTime.value, 0.1);
	});

	test('the headphone monitor tap is upstream of the room delay: the phones are never delayed by ROOM', () => {
		const { ctx, edges } = fakeAudioContext();
		headphones.disposeHeadphoneMonitor();
		const masterGain = ctx.createGain();
		const nodes = headphones.ensureHeadphoneGraph(ctx, masterGain);
		const mute = ctx.createGain();
		const roomDelay = headphones.createMasterDelayNode(ctx);
		// The engine's wiring, replayed on the fake: master -> mute -> room delay -> destination.
		masterGain.connect(mute);
		mute.connect(roomDelay);
		roomDelay.connect(ctx.destination);
		const monitorAncestors = ancestorsOf(nodes.bridgeInput, edges);
		assert.ok(monitorAncestors.has(masterGain), 'the monitor must still hear the master bus');
		assert.ok(!monitorAncestors.has(roomDelay), 'if the room delay sits upstream of the monitor tap then HEAD DELAY and ROOM stack and the phones lag the room - broken');
		assert.ok(!monitorAncestors.has(mute), 'the monitor tap is upstream of the mute belt too, as before');
		assert.ok(ancestorsOf(ctx.destination, edges).has(roomDelay));
		headphones.disposeHeadphoneMonitor();
	});

	test('engine wiring (source guard): mute -> room delay -> destination in BOTH routing branches, no createDelay in the engine', async () => {
		const engine = await readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8');
		assert.match(engine, /_masterDelay = createMasterDelayNode\(_ctx\)/);
		assert.match(engine, /_masterMuteGain\.connect\(_masterDelay\)/);
		assert.match(engine, /_masterDelay\.connect\(_ctx\.destination\)/, 'default routing: delay is the last node before the destination');
		assert.match(engine, /_masterDelay\.connect\(dest\)/, 'extroute routing: delay is the last node before the multichannel destination');
		assert.doesNotMatch(engine, /_masterMuteGain\.connect\(_ctx\.destination\)/, 'if the mute still connects straight to the destination then the room has an undelayed path beside the delayed one - broken');
		assert.doesNotMatch(engine, /_masterMuteGain\.connect\(dest\)/);
		assert.doesNotMatch(engine, /createDelay/);
		assert.match(engine, /export function masterDelayNode\(\)/);
		assert.match(engine, /masterDelaySeconds\(mixerState\.headphones\.master_delay_ms\)/, 'if the engine does not hand the room delay to the presentation clock then the playhead leads the room - broken');
	});
});

describe('the presentation clock lags by the room delay', () => {
	function playingTimeline() {
		const timeline = presentation.createPresentedTransportTimeline(0);
		presentation.acknowledgePresentedTransportSchedule(timeline, {
			revision: 1,
			active: true,
			loop: null,
			startContextTime: 1,
			startPositionSec: 0,
			tempoRatio: 1
		});
		return timeline;
	}

	test('with a 700 ms room delay the presented position is 0.7 s behind the render clock', () => {
		const undelayed = playingTimeline();
		const delayed = playingTimeline();
		const frame = { contextTime: 3, performanceTime: 3000 };
		const plain = presentation.observePresentedTransportTimeline(undelayed, frame, 600, 3.02);
		const lagged = presentation.observePresentedTransportTimeline(delayed, frame, 600, 3.02, 0.7);
		assert.equal(plain.position_sec, 2);
		assert.ok(Math.abs(lagged.position_sec - 1.3) < 1e-9, `if the playhead does not lag the room delay then the waveform runs 700 ms ahead of what the room hears - got ${lagged.position_sec}`);
		assert.equal(lagged.audible, true);
		assert.equal(lagged.accepted, true);
	});

	test('a schedule that has crossed the render clock but not yet the room is still pending', () => {
		const timeline = playingTimeline();
		const started = presentation.observePresentedTransportTimeline(timeline, { contextTime: 1.5, performanceTime: 1500 }, 600, 1.52, 0.7);
		assert.equal(started.audible, false, 'if the room delay does not hold the start back then PLAY lights 700 ms before the room hears it - broken');
		assert.equal(started.transport_pending, true);
		const heard = presentation.observePresentedTransportTimeline(timeline, { contextTime: 1.8, performanceTime: 1800 }, 600, 1.82, 0.7);
		assert.equal(heard.audible, true);
		assert.ok(Math.abs(heard.position_sec - 0.1) < 1e-9);
	});

	test('a zero lag is the existing behaviour and a bad lag throws', () => {
		const timeline = playingTimeline();
		const frame = { contextTime: 3, performanceTime: 3000 };
		assert.equal(presentation.observePresentedTransportTimeline(timeline, frame, 600, 3.02, 0).position_sec, 2);
		for (const bad of [-0.1, Number.NaN, Infinity]) {
			assert.throws(() => presentation.observePresentedTransportTimeline(playingTimeline(), frame, 600, 3.02, bad), /presentation lag/);
		}
	});

	test('raising the room delay mid-play degrades to the render clock for a frame rather than blipping to the paused cursor', () => {
		// A seek (rev 2) was presented at 3.04 with no lag; the older play (rev 1)
		// is pruned. The operator then sets ROOM to 1500: the lagged instant 1.55
		// precedes rev 2's start and rev 1 no longer exists, so a naive lag would
		// select nothing and paint the paused cursor over live audio.
		const timeline = playingTimeline();
		presentation.observePresentedTransportTimeline(timeline, { contextTime: 3, performanceTime: 3000 }, 600, 3.02, 0);
		presentation.acknowledgePresentedTransportSchedule(timeline, {
			revision: 2,
			active: true,
			loop: null,
			startContextTime: 3.02,
			startPositionSec: 100,
			tempoRatio: 1
		});
		const seeked = presentation.observePresentedTransportTimeline(timeline, { contextTime: 3.04, performanceTime: 3040 }, 600, 3.06, 0);
		assert.ok(Math.abs(seeked.position_sec - 100.02) < 1e-9);
		const raised = presentation.observePresentedTransportTimeline(timeline, { contextTime: 3.05, performanceTime: 3050 }, 600, 3.07, 1.5);
		assert.equal(raised.audible, true, 'if a live ROOM change paints the paused cursor over live audio then the waveform blips on every calibration - broken');
		assert.ok(Math.abs(raised.position_sec - 100.03) < 1e-9, `expected the render-clock position for this frame, got ${raised.position_sec}`);
	});
});

describe('press-audible floor carries the room delay', () => {
	test('inputToOutputMs adds master_delay_ms on top of the device floor', () => {
		const base = { pressToScheduleMs: 4, scheduledOffsetMs: 8, baseLatencySec: 0.005805, outputLatencySec: 0.032 };
		const plain = pressAudible.inputToOutputMs(base);
		const withRoom = pressAudible.inputToOutputMs({ ...base, masterDelayMs: 700 });
		assert.ok(Math.abs(withRoom - plain - 700) < 1e-9, 'if the room delay is left out then the press-to-audible number flatters the room by 700 ms - broken');
		assert.equal(pressAudible.inputToOutputMs({ ...base, masterDelayMs: 0 }), plain);
		assert.throws(() => pressAudible.inputToOutputMs({ ...base, masterDelayMs: -1 }), /master delay/);
		assert.throws(() => pressAudible.inputToOutputMs({ ...base, masterDelayMs: Number.NaN }), /master delay/);
	});

	test('scheduleOffsetStages forwards masterDelayMs into input_to_output_ms', () => {
		const input = {
			contextTimeSec: 10,
			requestedWhenSec: 10.008,
			effectiveWhenSec: 10.008,
			processorLeadSec: 0.002,
			processorLatencySec: 0.01,
			baseLatencySec: 0.005805,
			outputLatencySec: 0.032,
			active: true,
			pressToScheduleMs: 4
		};
		const plain = scheduleMath.scheduleOffsetStages(input);
		const withRoom = scheduleMath.scheduleOffsetStages({ ...input, masterDelayMs: 700 });
		assert.ok(Math.abs(withRoom.input_to_output_ms - plain.input_to_output_ms - 700) < 1e-6);
	});
});
