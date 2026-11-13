// requirement: COLORFX-12718
// requirement: FXVERB-14655
// requirement: FXVERB-14813
// requirement: COLORFX-01
// requirement: COLORFX-02
// requirement: COLORFX-04
// requirement: COLORFX-05
// requirement: COLORFX-8341
// requirement: FXECHO-13830
// [if] the effect switches (by name or effect next) [then] the old effect is removed and the knob returns to 12 o'clock unless the command names a value
// [if] the current effect is re-picked or only echo_beats is sent [then] the knob stays where it is
// [if] effect next is sent [then] it cycles FILTER, SPACE, ECHO, CRUSH and back to FILTER
// [if] the color knob sits at its center detent with SPACE or ECHO selected [then] every send is 0 and the filter is a dry bypass
// [if] the knob moves away from center with SPACE or ECHO [then] the send grows monotonically on that side only
// [if] ECHO is selected and the knob returns to center [then] the send closes but the echo input, feedback and return stay open, so the tail rings out
// [if] the effect type changes [then] every gain moves on a smoothing ramp, never a step
// [if] ECHO runs on 3/4 beat at 128 BPM [then] its delay line is 0.3515625 s, and it follows a tempo change
// [if] tempo or division changes on ECHO [then] delay times step and line gains crossfade, never delayTime ramps
// [if] the color graph is built [then] ECHO is the shared kernel in echo mode and a feedback override past 0.95 throws
// [if] the deck has no tempo [then] ECHO is inert and the view names why
// [if] the selector (or an agent) dispatches color_fx [then] the effect changes, the knob reaches setFilter and query() reads it back
// [if] an agent sends color_fx [then] the typed dispatcher validates it and the Rust engine mode refuses it by name
// [if] a SPACE send has been closed past its tail plus a hold [then] the convolver detaches, and the next turn re-attaches it
// [if] a load or unload command runs under a ringing SPACE [then] the return ramps from its current gain to 0 over 10 ms, the convolver detaches, the return is restored, and no polling timer is installed
// [if] Rust engine mode is on and a channel is on SPACE or ECHO [then] its knob is refused by name, never sent to the Rust FILTER
import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, describe, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pure;
let graph;
let live;
let channel;
let guards;
let player;
let ipc;
let rust;
let engine;
let mixerState;
let banner;

/** A recording stand-in for the AudioParam methods the color FX touch. */
class FakeParam {
	constructor(value = 0) {
		this.value = value;
		this.events = [];
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e[2] < t);
	}
	setValueAtTime(v, t) {
		this.events.push(['set', v, t]);
	}
	setTargetAtTime(v, t, tau) {
		this.events.push(['target', v, t, tau]);
	}
	linearRampToValueAtTime(v, t) {
		this.events.push(['linear', v, t]);
	}
	/** Where the scheduled events leave the parameter. */
	landing() {
		let out = this.value;
		for (const e of this.events) out = e[1];
		return out;
	}
}
// Fake nodes keep their context NON-enumerable, and the context its node list:
// node:assert inspects both sides of a failing comparison, and inspecting a node
// that reaches the whole fake graph (with its impulse buffers) once grew a test
// process past 40 GB. Compare fake nodes with assert.ok(a === b), never equal().
class FakeNode {
	constructor(kind, ctx) {
		this.kind = kind;
		Object.defineProperty(this, 'context', { value: ctx, enumerable: false });
		this.outputs = new Set();
	}
	connect(node) {
		this.outputs.add(node);
		return node;
	}
	disconnect(node) {
		if (node === undefined) this.outputs.clear();
		else this.outputs.delete(node);
	}
}
function fakeContext() {
	const ctx = {
		currentTime: 10,
		sampleRate: 8000,
		_node(kind) {
			const n = new FakeNode(kind, ctx);
			ctx.created.push(n);
			return n;
		},
		createGain() {
			const n = ctx._node('gain');
			n.gain = new FakeParam(1);
			return n;
		},
		createAnalyser() {
			return ctx._node('analyser');
		},
		createDelay(max) {
			const n = ctx._node('delay');
			n.maxDelayTime = max;
			n.delayTime = new FakeParam(0);
			return n;
		},
		createBiquadFilter() {
			const n = ctx._node('biquad');
			n.type = 'lowpass';
			n.frequency = new FakeParam(350);
			n.Q = new FakeParam(1);
			n.gain = new FakeParam(0);
			return n;
		},
		createWaveShaper() {
			const n = ctx._node('waveshaper');
			n.curve = null;
			n.oversample = 'none';
			return n;
		},
		createChannelMerger() {
			return ctx._node('merger');
		},
		createConvolver() {
			const n = ctx._node('convolver');
			n.buffer = null;
			return n;
		},
		createBuffer(channels, length, sampleRate) {
			const data = Array.from({ length: channels }, () => new Float32Array(length));
			return { length, sampleRate, numberOfChannels: channels, copyToChannel: (src, ch) => data[ch].set(src), data };
		}
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
}

function channelState(filter = 0.5) {
	return { trim: 0.5, eq_low: 0.5, eq_mid: 0.5, eq_high: 0.5, filter, fader: 1, cue_enabled: false, assign: 'thru' };
}

/** Build the real four-deck channel graph on a fake context; returns deck 1's nodes. */
function buildChannels(ctx, filter = 0.5) {
	const byDeck = {};
	const channels = { 1: channelState(filter), 2: channelState(), 3: channelState(), 4: channelState() };
	channel.buildDeckChannelGraph({
		ctx,
		mixerState: { channels, crossfader: 0.5 },
		masterGain: ctx.createGain(),
		externalMerger: null,
		routing: null,
		cueSum: ctx.createGain(),
		xfGainFor: () => 1,
		onDeck: (deck, nodes) => {
			byDeck[deck] = nodes;
		}
	});
	return byDeck;
}

function setDeckTempo(deck, bpm) {
	const st = player.deckStates[deck];
	st.bpm = bpm;
	st.pitch = 1;
	st.anlz = null;
	st.stable_id = bpm === null ? null : `track-${deck}`;
}

before(async () => {
	({ pure, graph, live, channel, guards, player, ipc, rust, engine, mixerState } = await loadTypeScriptModule(
		'tests/unit/fixtures/color-fx-entry.ts',
		{ viteApiBase: 'http://127.0.0.1:9' }
	));
	banner = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as DeckErrorBanner } from '$lib/components/rb/deck/DeckErrorBanner.svelte';
	`);
});

after(() => {
	live._resetColorFxForTests();
});

describe('pure half', () => {
	test('the wire validator takes partial commands and refuses anything it would have to guess', () => {
		assert.deepEqual(pure.parseColorFxCommand({ type: 'color_fx', deck: 1, effect: 'space' }), {
			type: 'color_fx', deck: 1, effect: 'space'
		});
		assert.deepEqual(pure.parseColorFxCommand({ type: 'color_fx', deck: 4, effect: 'echo', echo_beats: 0.75, value: 0.9 }), {
			type: 'color_fx', deck: 4, effect: 'echo', echo_beats: 0.75, value: 0.9
		});
		assert.deepEqual(pure.parseColorFxCommand({ type: 'color_fx', deck: 'all', effect: 'space' }), {
			type: 'color_fx', deck: 'all', effect: 'space'
		});
		for (const bad of [
			{ type: 'color_fx', deck: 1 },
			{ type: 'color_fx', effect: 'space' },
			{ type: 'color_fx', deck: 5, effect: 'space' },
			{ type: 'color_fx', deck: 1, effect: 'flanger' },
			{ type: 'color_fx', deck: 1, echo_beats: 3 },
			{ type: 'color_fx', deck: 1, value: 1.01 },
			{ type: 'color_fx', deck: 1, value: Number.NaN },
			{ type: 'color_fx', deck: 1, effect: 'space', level: 1 }
		]) {
			assert.throws(() => pure.parseColorFxCommand(bad), TypeError, JSON.stringify(bad));
		}
	});

	test('FILTER is exactly the shipped filter law, with every send closed', () => {
		for (const value of [0, 0.2, 0.49, 0.5, 0.51, 0.8, 1]) {
			const t = pure.colorFxTargets('filter', value, true);
			assert.deepEqual(t.filter, guards.filterParamsFromKnob(value), `filter at ${value}`);
			assert.equal(t.darkSend + t.brightSend + t.echoIn + t.spaceIn, 0, `no send at ${value}`);
		}
	});

	test('SPACE and ECHO at the detent are exactly dry: no send, filter bypassed', () => {
		const bypass = guards.filterParamsFromKnob(0.5);
		for (const effect of ['space', 'echo']) {
			for (const value of [0.5, 0.505, 0.495]) {
				const t = pure.colorFxTargets(effect, value, true);
				assert.equal(t.darkSend, 0, `${effect} dark at ${value}`);
				assert.equal(t.brightSend, 0, `${effect} bright at ${value}`);
				assert.deepEqual(t.filter, bypass, `${effect} filter at ${value}`);
			}
			// Control: just past the detent the send does open, so the zeros above
			// are the detent and not a dead knob.
			assert.ok(pure.colorFxTargets(effect, 0.6, true).brightSend > 0, `${effect} opens right of center`);
			assert.ok(pure.colorFxTargets(effect, 0.4, true).darkSend > 0, `${effect} opens left of center`);
		}
	});

	test('the send grows monotonically on one side only, reaching 1 at the stops', () => {
		for (const effect of ['space', 'echo']) {
			let last = 0;
			for (let v = 0.52; v <= 1.0001; v += 0.02) {
				const t = pure.colorFxTargets(effect, Math.min(1, v), true);
				assert.equal(t.darkSend, 0, `${effect} right turn leaves dark closed`);
				assert.ok(t.brightSend > last, `${effect} bright rises at ${v.toFixed(2)}`);
				last = t.brightSend;
			}
			assert.equal(pure.colorFxTargets(effect, 1, true).brightSend, 1);
			last = 0;
			for (let v = 0.48; v >= -0.0001; v -= 0.02) {
				const t = pure.colorFxTargets(effect, Math.max(0, v), true);
				assert.equal(t.brightSend, 0, `${effect} left turn leaves bright closed`);
				assert.ok(t.darkSend > last, `${effect} dark rises at ${v.toFixed(2)}`);
				last = t.darkSend;
			}
			assert.equal(pure.colorFxTargets(effect, 0, true).darkSend, 1);
		}
	});

	test('the tone sweeps deeper as the knob turns further', () => {
		const darkHz = [0.45, 0.3, 0.1].map((v) => pure.colorFxTargets('space', v, true).darkHz);
		const brightHz = [0.55, 0.7, 0.9].map((v) => pure.colorFxTargets('space', v, true).brightHz);
		assert.ok(darkHz[0] > darkHz[1] && darkHz[1] > darkHz[2], `dark lowpass falls: ${darkHz}`);
		assert.ok(brightHz[0] < brightHz[1] && brightHz[1] < brightHz[2], `bright highpass rises: ${brightHz}`);
	});

	test('the effect selects its input: ECHO only when timed, SPACE always', () => {
		assert.deepEqual(
			[pure.colorFxTargets('echo', 0.8, true).echoIn, pure.colorFxTargets('echo', 0.8, true).spaceIn],
			[1, 0]
		);
		assert.equal(pure.colorFxTargets('echo', 0.8, false).echoIn, 0, 'untimed echo is inert');
		assert.deepEqual(
			[pure.colorFxTargets('space', 0.8, false).spaceIn, pure.colorFxTargets('space', 0.8, false).echoIn],
			[1, 0]
		);
	});

	test('echo time is the beat division at the tempo, inside the delay line', () => {
		assert.equal(pure.echoDelaySeconds(0.5, 125), 0.24);
		assert.equal(pure.echoDelaySeconds(0.75, 128), 0.3515625);
		assert.equal(pure.echoDelaySeconds(1, null), null);
		assert.equal(pure.echoDelaySeconds(1, 20), null, 'below the accepted tempo range');
		assert.equal(pure.echoDelaySeconds(1, 400), null, 'above the accepted tempo range');
		const longest = Math.max(...pure.COLOR_FX_ECHO_BEATS) * (60 / pure.COLOR_FX_MIN_BPM);
		assert.ok(graph.COLOR_FX_ECHO_MAX_DELAY_SEC >= longest, 'the line holds the longest division');
		assert.ok(graph.ECHO_FEEDBACK > 0 && graph.ECHO_FEEDBACK < 1, 'feedback decays, never runs away');
	});

	// [if] the color graph is built [then] ECHO is the shared kernel in echo mode and a feedback override past 0.95 throws, [else stop].
	test('color ECHO runs the shared kernel in echo mode; the feedback override is clamped', () => {
		const ctx = fakeContext();
		const g = graph.buildColorFxGraph(ctx, ctx.createGain(), [], { echoFeedback: 0.95 });
		assert.equal(g.echo.mode(), 'echo');
		assert.equal(g.echoFeedback.gain.value, 0.95);
		const bad = () => graph.buildColorFxGraph(ctx, ctx.createGain(), [], { echoFeedback: 1 });
		assert.throws(bad, /echo feedback 1 is outside \[0, 0.95\]/);
	});
});

describe('live channel', () => {
	let ctx;
	let nodes;

	beforeEach(() => {
		live._resetColorFxForTests();
		for (const deck of [1, 2, 3, 4]) setDeckTempo(deck, null);
		ctx = fakeContext();
		nodes = buildChannels(ctx)[1];
	});

	test('the filter branches feed one color bus, which alone feeds the cue and the fader', () => {
		for (const branch of [nodes.filterDry, nodes.filterLpWet, nodes.filterHpWet]) {
			assert.ok(branch.outputs.has(nodes.colorBus), 'branch feeds the color bus');
			assert.equal(branch.outputs.has(nodes.fader), false, 'branch no longer feeds the fader directly');
			assert.equal(branch.outputs.has(nodes.cue), false, 'branch no longer feeds the cue directly');
		}
		assert.ok(nodes.colorBus.outputs.has(nodes.fader));
		assert.ok(nodes.colorBus.outputs.has(nodes.cue));
		assert.equal(nodes.colorBus.gain.value, 1, 'unity, so FILTER mode is untouched');
	});

	test('FILTER never builds the space/echo nodes', () => {
		live.applyColorKnob(1, nodes, 0.9);
		assert.equal(nodes.filterHpWet.gain.landing(), 1, 'the filter itself moved (control)');
		assert.equal(live.colorFxView()[1].spliced, false);
		assert.equal(ctx.created.some((n) => n.kind === 'convolver' || n.kind === 'delay'), false);
	});

	test('SPACE splices on first use, opens the bright send and bypasses the filter', () => {
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		assert.ok(g !== null, 'spliced');
		assert.equal(live.colorFxView()[1].spliced, true);
		assert.ok(nodes.colorBus.outputs.has(g.toneDark) && nodes.colorBus.outputs.has(g.toneBright));
		assert.ok(g.spaceReturn.outputs.has(nodes.fader) && g.spaceReturn.outputs.has(nodes.cue));
		assert.ok(g.echoReturn.outputs.has(nodes.fader) && g.echoReturn.outputs.has(nodes.cue));
		assert.ok(g.space.buffer !== null, 'the impulse response is loaded once, at splice');
		const expected = pure.colorFxTargets('space', 0.9, false);
		assert.ok(Math.abs(g.brightSend.gain.landing() - expected.brightSend) < 1e-12);
		assert.equal(g.darkSend.gain.landing(), 0);
		assert.equal(g.spaceIn.gain.landing(), 1);
		assert.equal(nodes.filterDry.gain.landing(), 1, 'filter bypassed');
		assert.equal(nodes.filterHpWet.gain.landing(), 0, 'filter bypassed');
	});

	test('ECHO returning to center closes the send and leaves the loop ringing', () => {
		setDeckTempo(1, 128);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo', echo_beats: 0.75 });
		live.applyColorKnob(1, nodes, 0.1);
		const g = live._colorFxGraphForTests(1);
		assert.ok(g.darkSend.gain.landing() > 0.75, 'left turn opens the dark send (control)');
		assert.equal(g.echo.time(), 0.3515625);
		assert.equal(g.echo.lines[0].delayTime.landing(), 0.3515625, 'cold start steps the live line');
		live.applyColorKnob(1, nodes, 0.5);
		assert.equal(g.darkSend.gain.landing(), 0, 'send closed at center');
		assert.equal(g.brightSend.gain.landing(), 0);
		assert.equal(g.echoIn.gain.landing(), 1, 'echo input stays open, so the line keeps what it holds');
		assert.equal(g.echoFeedback.gain.landing(), graph.ECHO_FEEDBACK, 'feedback untouched');
		assert.equal(g.echoReturn.gain.landing(), graph.ECHO_WET_GAIN, 'return untouched');
	});

	test('ECHO alone never builds the convolver; SPACE attaches exactly one', () => {
		setDeckTempo(1, 128);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo' });
		live.applyColorKnob(1, nodes, 0.8);
		const g = live._colorFxGraphForTests(1);
		assert.ok(ctx.created.some((n) => n.kind === 'delay'), 'the echo line exists (control)');
		assert.equal(ctx.created.filter((n) => n.kind === 'convolver').length, 0);
		assert.ok(g.space === null);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.7);
		live.applyColorKnob(1, nodes, 0.9);
		assert.equal(ctx.created.filter((n) => n.kind === 'convolver').length, 1);
		assert.ok(g.spaceIn.outputs.has(g.space) && g.space.outputs.has(g.spaceReturn));
	});

	test('an idle SPACE detaches only after its tail and the hold, and re-attaches on the next turn', () => {
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		const first = g.space;
		assert.ok(first !== null, 'attached on the turn (control)');
		const delay = graph.spaceDetachDelaySec();
		assert.ok(delay > graph.SPACE_RT60_SEC, 'the hold starts after the whole tail');
		// Held open: no amount of time detaches a channel whose send is open.
		ctx.currentTime = 10 + 10 * delay;
		assert.equal(live._detachIdleSpaceForTests(1), false, 'an open send never detaches');
		live.applyColorKnob(1, nodes, 0.5);
		const quietAt = ctx.currentTime;
		ctx.currentTime = quietAt + delay - 0.05;
		assert.equal(live._detachIdleSpaceForTests(1), false, 'still inside the tail plus hold');
		assert.ok(g.space === first, 'still the same convolver');
		ctx.currentTime = quietAt + delay + 0.05;
		assert.equal(live._detachIdleSpaceForTests(1), true);
		assert.ok(g.space === null);
		assert.equal(g.spaceIn.outputs.has(first), false, 'its input is disconnected');
		assert.equal(first.outputs.size, 0, 'its output is disconnected');
		assert.equal(live.colorFxView()[1].space_attached, false);
		// Moving within the detent does not bring it back; a real turn does.
		live.applyColorKnob(1, nodes, 0.505);
		assert.ok(g.space === null, 'a nudge inside the detent stays detached');
		live.applyColorKnob(1, nodes, 0.8);
		assert.ok(g.space !== null && g.space !== first, 're-attached as a fresh convolver');
		assert.ok(g.spaceIn.outputs.has(g.space) && g.space.outputs.has(g.spaceReturn));
		assert.equal(live.colorFxView()[1].space_attached, true);
		for (const e of g.brightSend.gain.events) assert.equal(e[0], 'target', 'the re-turn ramps the send');
	});

	test('a load or unload through the command hook cuts a ringing SPACE from its current gain, with no watch timer', async () => {
		const hadWindow = 'window' in globalThis;
		if (!hadWindow) globalThis.window = {};
		const hadLocation = 'location' in globalThis;
		if (!hadLocation) globalThis.location = new URL('http://127.0.0.1/performance');
		const realSetInterval = globalThis.setInterval;
		const realFetch = globalThis.fetch;
		const { load, unload } = engine;
		const intervals = [];
		const engineCalls = [];
		globalThis.setInterval = (fn, ms, ...rest) => {
			intervals.push(ms);
			return realSetInterval(fn, ms, ...rest);
		};
		globalThis.fetch = async () => {
			throw new Error('no daemon in this test');
		};
		engine.load = async (deck, stableId) => {
			engineCalls.push(['load', deck, stableId]);
		};
		engine.unload = async (deck) => {
			engineCalls.push(['unload', deck]);
		};
		const uninstall = ipc.installPerformanceBrowserIpc();
		const dispatch = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
		try {
			player.deckStates[1].stable_id = 'track-a';
			live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
			live.applyColorKnob(1, nodes, 0.9);
			const g = live._colorFxGraphForTests(1);
			const first = g.space;
			assert.ok(first !== null, 'attached on the turn (control)');
			assert.deepEqual(intervals, [], 'an attached SPACE installs no polling timer');
			// The return is not at its resting gain (mid-crossfade): the cut starts from here.
			g.spaceReturn.gain.value = 0.3;
			g.spaceReturn.gain.events = [];
			const t0 = ctx.currentTime;
			await dispatch({ type: 'load', deck: 1, stable_id: 'track-b' });
			assert.deepEqual(engineCalls, [['load', 1, 'track-b']], 'the load reached the engine (control)');
			assert.deepEqual(g.spaceReturn.gain.events, [
				['set', 0.3, t0],
				['linear', 0, t0 + 0.01]
			], 'ramp from the current gain, never a snap to SPACE_WET_GAIN');
			assert.ok(g.space === first, 'held until the ramp lands');
			ctx.currentTime = t0 + 0.02;
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.equal(first.outputs.size, 0, 'the ringing convolver is disconnected');
			assert.ok(g.space !== null && g.space !== first, 'the open send re-attaches a fresh convolver');
			assert.equal(g.spaceReturn.gain.landing(), graph.SPACE_WET_GAIN, 'the return is restored');
			// Eject (unload) with the knob at center: cut, and nothing re-attaches.
			live.applyColorKnob(1, nodes, 0.5);
			await dispatch({ type: 'unload', deck: 1 });
			assert.deepEqual(engineCalls.at(-1), ['unload', 1]);
			ctx.currentTime += 0.02;
			await new Promise((resolve) => setTimeout(resolve, 40));
			assert.ok(g.space === null, 'ejected: the convolver stays detached');
			assert.equal(live.colorFxView()[1].space_attached, false);
			assert.deepEqual(intervals, [], 'still no polling timer');
		} finally {
			uninstall();
			engine.load = load;
			engine.unload = unload;
			globalThis.setInterval = realSetInterval;
			globalThis.fetch = realFetch;
			if (!hadWindow) delete globalThis.window;
			if (!hadLocation) delete globalThis.location;
		}
	});

	test('a pause leaves a ringing SPACE alone: only load and eject cut', async () => {
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		const first = g.space;
		g.spaceReturn.gain.events = [];
		player.deckStates[1].playing = false;
		await new Promise((resolve) => setTimeout(resolve, 40));
		assert.ok(g.space === first, 'a pause never cuts');
		assert.deepEqual(g.spaceReturn.gain.events, [], 'a pause never moves the return');
	});

	test('leaving SPACE for ECHO starts the idle clock even off center', () => {
		setDeckTempo(1, 128);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo' });
		ctx.currentTime += graph.spaceDetachDelaySec() + 0.05;
		assert.equal(live._detachIdleSpaceForTests(1), true);
		assert.ok(g.space === null);
		assert.ok(g.echoIn.gain.landing() === 1, 'the echo is untouched (control)');
	});

	test('every gain the effect switch moves is ramped, never stepped', () => {
		setDeckTempo(1, 128);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo' });
		live.applyColorKnob(1, nodes, 0.8);
		const g = live._colorFxGraphForTests(1);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.8);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'filter' });
		live.applyColorKnob(1, nodes, 0.8);
		const gains = [g.darkSend, g.brightSend, g.echoIn, g.spaceIn, nodes.filterDry, nodes.filterLpWet, nodes.filterHpWet];
		for (const node of gains) {
			assert.ok(node.gain.events.length > 0, `${node.kind} was driven (control)`);
			for (const e of node.gain.events) assert.equal(e[0], 'target', `stepped ${JSON.stringify(e)}`);
		}
		assert.equal(g.echoIn.gain.landing(), 0, 'leaving echo closes its input; the tail rings out on feedback');
		assert.equal(g.echoFeedback.gain.landing(), graph.ECHO_FEEDBACK);
	});

	test('the echo line follows the deck tempo while ECHO is selected', () => {
		setDeckTempo(1, 125);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo', echo_beats: 0.5 });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		assert.equal(g.echo.time(), 0.24);
		setDeckTempo(1, 120);
		live._tickColorFxTempoForTests();
		assert.equal(g.echo.time(), 0.25);
		const idle = g.echo.lines[1].delayTime;
		const last = idle.events[idle.events.length - 1];
		assert.equal(last[0], 'set');
		assert.equal(last[1], 0.25);
		assert.equal(g.echo.lines[0].delayTime.landing(), 0.24, 'old line untouched');
		assert.equal(live.colorFxView()[1].tempo_bpm, 120);
		assert.equal(live.colorFxView()[1].echo_ms, 250);
	});

	test('a tempo change crossfades the echo lines and never ramps a delay time', () => {
		setDeckTempo(1, 125);
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo', echo_beats: 0.5 });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		setDeckTempo(1, 120);
		live._tickColorFxTempoForTests();
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo', echo_beats: 1 });
		live._tickColorFxTempoForTests();
		for (const line of g.echo.lines) {
			for (const e of line.delayTime.events) {
				assert.equal(e[0], 'set', `delayTime must not ramp: ${JSON.stringify(e)}`);
				assert.notEqual(e[0], 'target');
				assert.notEqual(e[0], 'linear');
			}
		}
		let linearCount = 0;
		for (const lg of g.echo.lineGains) {
			if (lg.gain.events.some((e) => e[0] === 'linear')) linearCount++;
		}
		assert.ok(linearCount >= 2, 'line gains crossfade (control)');
	});

	test('with no tempo ECHO is inert and the view names why', () => {
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'echo' });
		live.applyColorKnob(1, nodes, 0.9);
		const g = live._colorFxGraphForTests(1);
		assert.equal(g.echoIn.gain.landing(), 0);
		const view = live.colorFxView()[1];
		assert.equal(view.tempo_bpm, null);
		assert.match(view.inert_reason, /tempo/);
		// Control: once a tempo appears the poll opens the echo.
		setDeckTempo(1, 126);
		live._tickColorFxTempoForTests();
		assert.equal(g.echoIn.gain.landing(), 1);
		assert.equal(live.colorFxView()[1].inert_reason, null);
	});

	test('a rebuilt channel graph re-splices on the new context with the selected effect', () => {
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'space' });
		live.applyColorKnob(1, nodes, 0.9);
		const old = live._colorFxGraphForTests(1);
		const ctx2 = fakeContext();
		const nodes2 = buildChannels(ctx2, 0.9)[1];
		const fresh = live._colorFxGraphForTests(1);
		assert.ok(fresh !== null && fresh !== old, 'a new graph');
		assert.ok(fresh.space !== null && fresh.space.context === ctx2, 'built on the new context');
		assert.ok(nodes2.colorBus.outputs.has(fresh.toneBright));
		assert.equal(nodes2.filterDry.gain.value, 1, 'the new filter starts bypassed, not as FILTER at 0.9');
	});

	test('the selection is sticky per deck and leaves the other decks alone', () => {
		live.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'space' });
		const view = live.colorFxView();
		assert.equal(view[2].effect, 'space');
		assert.equal(view[1].effect, 'filter');
		assert.equal(view[2].echo_beats, pure.DEFAULT_COLOR_FX_SETTINGS.echo_beats);
	});
});

describe('agent parity', () => {
	test('COLORFX-8341: the switch rule, pure', () => {
		assert.equal(pure.nextColorFxEffect('filter'), 'space');
		assert.equal(pure.nextColorFxEffect('space'), 'echo');
		assert.equal(pure.nextColorFxEffect('echo'), 'crush');
		assert.equal(pure.nextColorFxEffect('crush'), 'filter');
		const cmd = (rest) => ({ type: 'color_fx', deck: 1, ...rest });
		assert.deepEqual(pure.planColorFxSwitch('filter', 0.8333, cmd({ effect: 'echo' })), { effect: 'echo', value: 0.5, switched: true });
		assert.deepEqual(pure.planColorFxSwitch('filter', 0.8333, cmd({ effect: 'next' })), { effect: 'space', value: 0.5, switched: true });
		assert.deepEqual(pure.planColorFxSwitch('filter', 0.8333, cmd({ effect: 'echo', value: 0.2 })), { effect: 'echo', value: 0.2, switched: true });
		// Controls: no switch keeps the knob.
		assert.deepEqual(pure.planColorFxSwitch('echo', 0.2, cmd({ effect: 'echo' })), { effect: 'echo', value: 0.2, switched: false });
		assert.deepEqual(pure.planColorFxSwitch('echo', 0.2, cmd({ echo_beats: 0.5 })), { effect: 'echo', value: 0.2, switched: false });
	});

	test('COLORFX-8341: the cycle gesture is Cmd on a Mac, Ctrl elsewhere, primary button only', () => {
		const ev = (over) => ({ button: 0, metaKey: false, ctrlKey: false, ...over });
		assert.equal(pure.isColorFxCycleGesture(ev({ metaKey: true }), 'Meta'), true);
		assert.equal(pure.isColorFxCycleGesture(ev({ ctrlKey: true }), 'Meta'), false, 'Ctrl-click on a Mac is the context menu');
		assert.equal(pure.isColorFxCycleGesture(ev({ ctrlKey: true }), 'Control'), true);
		assert.equal(pure.isColorFxCycleGesture(ev({ metaKey: true, button: 2 }), 'Meta'), false);
		assert.equal(pure.isColorFxCycleGesture(ev({}), 'Meta'), false, 'a plain click still turns or opens');
	});

	test('COLORFX-8341: the parser accepts effect next and still refuses unknown effects', () => {
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'color_fx', deck: 1, effect: 'next' }), {
			type: 'color_fx', deck: 1, effect: 'next'
		});
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'color_fx', deck: 1, effect: 'prev' }), /filter\|space\|echo\|crush\|next/);
	});

	test('COLORFX-8341: dispatched next cycles, removes the old effect and recentres the knob; same-effect keeps it', async () => {
		live._resetColorFxForTests();
		const hadWindow = 'window' in globalThis;
		if (!hadWindow) globalThis.window = {};
		const hadLocation = 'location' in globalThis;
		if (!hadLocation) globalThis.location = new URL('http://127.0.0.1/performance');
		const uninstall = ipc.installPerformanceBrowserIpc();
		const dispatch = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
		const knob = () => ipc.queryPerformanceState().mixer.channels[4].filter;
		try {
			// FILTER at 3 o'clock, then Cmd-click: SPACE, knob back to 12 o'clock.
			await dispatch({ type: 'filter', deck: 4, value: 5 / 6 });
			await dispatch({ type: 'color_fx', deck: 4, effect: 'next' });
			assert.equal(live.colorFx[4].effect, 'space');
			assert.equal(knob(), 0.5, 'the switch hands the knob to SPACE at center, the FILTER is gone');
			await dispatch({ type: 'filter', deck: 4, value: 1 / 6 });
			await dispatch({ type: 'color_fx', deck: 4, effect: 'next' });
			assert.equal(live.colorFx[4].effect, 'echo');
			assert.equal(knob(), 0.5);
			await dispatch({ type: 'color_fx', deck: 4, effect: 'next' });
			assert.equal(live.colorFx[4].effect, 'crush');
			assert.equal(knob(), 0.5);
			await dispatch({ type: 'color_fx', deck: 4, effect: 'next', value: 0.7 });
			assert.equal(live.colorFx[4].effect, 'filter', 'wraps round');
			assert.equal(knob(), 0.7, 'a named value wins over the recentre');
			// Control: re-picking the current effect leaves the knob alone.
			await dispatch({ type: 'color_fx', deck: 4, effect: 'filter' });
			assert.equal(knob(), 0.7);
			assert.equal(live.colorFx[1].effect, 'filter', 'other decks untouched');
		} finally {
			await dispatch({ type: 'filter', deck: 4, value: 0.5 });
			uninstall();
			if (!hadWindow) delete globalThis.window;
			if (!hadLocation) delete globalThis.location;
			live._resetColorFxForTests();
		}
	});

	test('the dispatcher routes color_fx through the same validator', () => {
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'color_fx', deck: 2, effect: 'echo' }), {
			type: 'color_fx', deck: 2, effect: 'echo'
		});
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'color_fx', deck: 2, effect: 'echo', extra: 1 }), TypeError);
		assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'color_fx', deck: 2, effect: 'flanger' }), TypeError);
	});

	test('a dispatched color_fx, the selector path, changes the effect, moves the knob and reads back from query()', async () => {
		live._resetColorFxForTests();
		const hadWindow = 'window' in globalThis;
		if (!hadWindow) globalThis.window = {};
		const hadLocation = 'location' in globalThis;
		if (!hadLocation) globalThis.location = new URL('http://127.0.0.1/performance');
		const uninstall = ipc.installPerformanceBrowserIpc();
		const dispatch = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
		try {
			assert.equal(ipc.queryPerformanceState().color_fx[3].effect, 'filter', 'control: starts on FILTER');
			await dispatch({ type: 'color_fx', deck: 3, effect: 'space', value: 0.7 });
			assert.equal(live.colorFx[3].effect, 'space');
			assert.equal(ipc.queryPerformanceState().color_fx[3].effect, 'space');
			assert.equal(ipc.queryPerformanceState().mixer.channels[3].filter, 0.7, 'the knob half reached engine.setFilter');
			assert.equal(live.colorFx[1].effect, 'filter', 'other decks untouched');
			await dispatch({ type: 'color_fx', deck: 3, effect: 'filter', value: 0.5 });
			assert.equal(ipc.queryPerformanceState().color_fx[3].effect, 'filter');
		} finally {
			uninstall();
			if (!hadWindow) delete globalThis.window;
			if (!hadLocation) delete globalThis.location;
			live._resetColorFxForTests();
		}
	});

	test('Rust engine mode refuses the knob of a SPACE or ECHO channel by name, never filtering instead', async () => {
		live._resetColorFxForTests();
		rust.rustMode.enabled = true;
		try {
			live.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'space' });
			await assert.rejects(
				rust.executeInRustEngine({ type: 'filter', deck: 2, value: 0.8 }),
				/RUST_ENGINE_UNAVAILABLE: .*deck 2.*SPACE/
			);
			live.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'echo' });
			await assert.rejects(rust.executeInRustEngine({ type: 'filter', deck: 2, value: 0.8 }), /ECHO/);
			// Control: the same guard passes a FILTER channel and other decks.
			live.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'filter' });
			assert.equal(live.colorFxRustRefusal(2), null);
			assert.equal(live.colorFxRustRefusal(1), null);
			live.applyColorFxSettings({ type: 'color_fx', deck: 3, effect: 'space' });
			assert.match(live.colorFxRustRefusal(3), /deck 3/);
		} finally {
			rust.rustMode.enabled = false;
			live._resetColorFxForTests();
		}
	});

	test('Rust engine mode refuses color_fx by name rather than filtering instead', () => {
		assert.ok(rust.WEB_AUDIO_ONLY.has('color_fx'));
	});
});

// Rejection and recovery execute the real dispatcher; only the late-failing
// engine dependency below is substituted. Rust refusal uses the real adapter.
describe('Color FX command failure recovery', () => {
	let uninstall;
	let oldWindow;
	let oldLocation;
	beforeEach(() => {
		live._resetColorFxForTests();
		oldWindow = globalThis.window;
		oldLocation = globalThis.location;
		globalThis.window = {};
		globalThis.location = new URL('http://127.0.0.1/performance');
		uninstall = ipc.installPerformanceBrowserIpc();
	});
	afterEach(() => {
		rust.rustMode.enabled = false;
		uninstall();
		if (oldWindow === undefined) delete globalThis.window;
		else globalThis.window = oldWindow;
		if (oldLocation === undefined) delete globalThis.location;
		else globalThis.location = oldLocation;
		live._resetColorFxForTests();
	});

	const dispatch = (rest) => window.musicDjToolsPerformance.dispatch({ type: 'color_fx', deck: 2, ...rest });
	const snapshot = () => ipc.queryPerformanceState();
	function assertSettled() {
		assert.equal(snapshot().decks[2].command_pending, false);
		assert.equal(ipc.performanceCommandStatus.deck_pending[2], 0);
		assert.equal(ipc.performanceCommandStatus.active, 0);
		assert.equal(ipc.performanceCommandStatus.queued, 0);
	}

	test('real Rust refusal publishes one deck error, renders its banner, and recovers on retry', async () => {
		const before = snapshot();
		rust.rustMode.enabled = true;
		const refused = dispatch({ effect: 'space', value: 0.7 });
		assert.equal(snapshot().decks[2].command_pending, true);
		await assert.rejects(refused, /RUST_ENGINE_UNAVAILABLE.*color_fx/);
		const failed = snapshot();
		assert.match(failed.decks[2].command_error, /RUST_ENGINE_UNAVAILABLE.*color_fx/);
		assertSettled();
		assert.deepEqual(failed.color_fx[2], before.color_fx[2]);
		const html = banner.render(banner.DeckErrorBanner, { props: {
			deckId: 2, error: failed.decks[2].command_error, errorId: null,
			tone: 'fault', onDismiss: () => {}
		} }).body;
		assert.match(html, /role="alert"/);
		assert.match(html, /data-performance-error="2"/);
		assert.match(html, /color_fx/);
		const ids = new Set(window.musicDjToolsPerformance.toasts().map((toast) => toast.id));
		const result = await ipc.runPerformanceCommandFromUi({ type: 'color_fx', deck: 2, echo_beats: 0.75 });
		assert.equal(result.ok, false);
		assert.equal(result.reason, 'failed');
		const introduced = window.musicDjToolsPerformance.toasts().filter((toast) => !ids.has(toast.id));
		assert.equal(introduced.length, 1);
		assertSettled();
		rust.rustMode.enabled = false;
		await dispatch({ effect: 'echo', echo_beats: 0.75, value: 0.8 });
		const recovered = snapshot();
		assert.equal(recovered.decks[2].command_error, null);
		assert.equal(recovered.color_fx[2].effect, 'echo');
		assert.equal(recovered.color_fx[2].echo_beats, 0.75);
		assert.equal(recovered.mixer.channels[2].filter, 0.8);
		assertSettled();
		for (const deck of [1, 3, 4]) {
			assert.equal(recovered.decks[deck].command_error, before.decks[deck].command_error);
			assert.equal(recovered.decks[deck].command_pending, before.decks[deck].command_pending);
			assert.deepEqual(recovered.color_fx[deck], before.color_fx[deck]);
			assert.equal(recovered.mixer.channels[deck].filter, before.mixer.channels[deck].filter);
		}
	});

	test('synchronous failure retains its cause and exposes a restoration failure explicitly', async () => {
		await dispatch({ effect: 'space', value: 0.8 });
		const nodes = buildChannels(fakeContext())[2];
		live.applyColorKnob(2, nodes, 0.8);
		const original = engine.setFilter;
		const originalRamp = nodes.filterDry.gain.setTargetAtTime;
		const cause = new Error('output refused');
		engine.setFilter = () => { throw cause; };
		try {
			await assert.rejects(dispatch({ effect: 'next' }), (error) => {
				assert.equal(error.cause, cause);
				assert.match(error.message, /Color FX.*deck 2.*output refused/);
				return true;
			});
			assert.equal(snapshot().color_fx[2].effect, 'space');
			engine.setFilter = () => {
				nodes.filterDry.gain.setTargetAtTime = () => { throw new Error('graph disconnected'); };
				throw cause;
			};
			await assert.rejects(dispatch({ effect: 'next' }), (error) => {
				assert.ok(error instanceof AggregateError);
				assert.equal(error.errors[0], cause);
				assert.match(error.message, /output refused; restoration failed: .*graph disconnected/);
				return true;
			});
			assert.match(snapshot().decks[2].command_error, /restoration failed/);
			assertSettled();
		} finally {
			engine.setFilter = original;
			nodes.filterDry.gain.setTargetAtTime = originalRamp;
		}
	});

	for (const command of [{ effect: 'next' }, { echo_beats: 0.75, value: 0.9 }]) {
		test(`late engine rejection restores acknowledged settings and graph: ${JSON.stringify(command)}`, async () => {
			setDeckTempo(2, 120);
			await dispatch({ effect: 'echo', echo_beats: 0.5, value: 0.8 });
			const nodes = buildChannels(fakeContext())[2];
			live.applyColorKnob(2, nodes, 0.8);
			const before = snapshot();
			const original = engine.setFilter;
			let rejectCall;
			let entered;
			const called = new Promise((resolve) => { entered = resolve; });
			engine.setFilter = (deck, value) => {
				mixerState.channels[deck].filter = value;
				live.applyColorKnob(deck, nodes, value);
				entered();
				return new Promise((_resolve, reject) => { rejectCall = reject; });
			};
			try {
				const pending = dispatch(command);
				const rejected = assert.rejects(pending, /Color FX.*deck 2.*output refused/);
				await called;
				const wasPending = snapshot().decks[2].command_pending;
				rejectCall(new Error('output refused'));
				await rejected;
				assert.equal(wasPending, true);
				assertSettled();
				const failed = snapshot();
				assert.match(failed.decks[2].command_error, /Color FX.*deck 2.*output refused/);
				assert.deepEqual(failed.color_fx[2], before.color_fx[2]);
				assert.equal(failed.mixer.channels[2].filter, 0.8);
				assert.equal(live._colorFxGraphForTests(2).echoIn.gain.landing(),
					pure.colorFxTargets('echo', 0.8, true).echoIn);
				engine.setFilter = original;
				await dispatch(command);
				assertSettled();
				assert.equal(snapshot().decks[2].command_error, null);
				assert.equal(snapshot().color_fx[2].effect, command.effect === 'next' ? 'crush' : 'echo');
				assert.equal(snapshot().color_fx[2].echo_beats, command.echo_beats ?? 0.5);
				assert.equal(snapshot().mixer.channels[2].filter, command.value ?? 0.5);
			} finally {
				engine.setFilter = original;
			}
		});
	}
});
