// requirement: COLORFX-10754
// [if] anything but trim follows the deck analyser (the stem sum), or a color FX node sits upstream of the high shelf [then] the send no longer hears the post-EQ stem balance
// [if] colorBus is fed by anything but filterDry, filterLpWet and filterHpWet after the high shelf [then] the send tap moved
// [if] the SPACE or ECHO return feeds anything but the channel's cue and fader [then] the insert is no longer pre-fader
// [if] a color FX node is reachable downstream of the fader (thru or USB routing) [then] the fader no longer cuts the wet tail
// [if] cue is fed by anything but colorBus and the two returns [then] the phones no longer hear the effect pre-fader
// [if] a post-fader splice passes the after-the-fader check [then] the detector is blind
import assert from 'node:assert/strict';
import { after, before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let graph;
let live;
let channel;

// Same fakes as color-fx.test.mjs. Nodes keep their context NON-enumerable:
// compare fake nodes with assert.ok(a === b), never deepEqual on nodes.
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
}
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
		createChannelSplitter() {
			return ctx._node('splitter');
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
		createConvolver() {
			const n = ctx._node('convolver');
			n.buffer = null;
			return n;
		},
		createBuffer(channels, length, sampleRate) {
			const data = Array.from({ length: channels }, () => new Float32Array(length));
			return {
				length,
				sampleRate,
				numberOfChannels: channels,
				copyToChannel: (src, ch) => data[ch].set(src),
				data
			};
		}
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
}

function channelState() {
	return {
		trim: 0.5,
		eq_low: 0.5,
		eq_mid: 0.5,
		eq_high: 0.5,
		filter: 0.5,
		fader: 1,
		cue_enabled: true,
		assign: 'thru'
	};
}

/** Build the real four-deck channel graph; returns deck 1's nodes plus the shared sinks. */
function buildDeck1(ctx, usb = false) {
	const byDeck = {};
	const masterGain = ctx.createGain();
	const cueSum = ctx.createGain();
	const externalMerger = usb ? ctx.createGain() : null;
	const channels = { 1: channelState(), 2: channelState(), 3: channelState(), 4: channelState() };
	channel.buildDeckChannelGraph({
		ctx,
		mixerState: { channels, crossfader: 0.5 },
		masterGain,
		externalMerger,
		routing: usb ? new Map([[1, 1]]) : null,
		cueSum,
		xfGainFor: () => 1,
		onDeck: (deck, nodes) => {
			byDeck[deck] = nodes;
		}
	});
	return { nodes: byDeck[1], masterGain, cueSum, externalMerger };
}

/** Splice the real way: SPACE first (attaches the convolver), then ECHO. */
function spliceSpaceAndEcho(nodes) {
	const rt = { nodes, graph: null, spaceQuietSince: null };
	graph.driveColorChannel(rt, { effect: 'space', echo_beats: 0.75 }, 124, 1);
	graph.driveColorChannel(rt, { effect: 'echo', echo_beats: 0.75 }, 124, 1);
	assert.ok(rt.graph !== null, 'the color FX graph spliced');
	assert.ok(rt.graph.space !== null, 'the SPACE convolver attached');
	return rt;
}

function inbound(ctx, node) {
	return ctx.created.filter((n) => n.outputs.has(node));
}

function assertSameSet(actual, expected, what) {
	assert.equal(
		actual.length,
		expected.length,
		`${what}: ${actual.length} nodes, expected ${expected.length}`
	);
	for (const n of expected) assert.ok(actual.includes(n), `${what}: missing a ${n.kind}`);
}

/** Every node reachable downstream of `start` (BFS over outputs). */
function downstream(start) {
	const seen = new Set();
	const queue = [...start.outputs];
	while (queue.length > 0) {
		const n = queue.shift();
		if (seen.has(n)) continue;
		seen.add(n);
		queue.push(...n.outputs);
	}
	return [...seen];
}

/** Color FX nodes after the fader (downstream of it, or feeding a node that is):
 * empty when the insert is pre-fader. */
function colorFxAfterFader(nodes, fxGraph) {
	const post = downstream(nodes.fader);
	return fxGraph.nodes.filter(
		(n) => post.includes(n) || [...n.outputs].some((o) => post.includes(o))
	);
}

before(async () => {
	({ graph, live, channel } = await loadTypeScriptModule('tests/unit/fixtures/color-fx-entry.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	}));
});

after(() => {
	live._resetColorFxForTests();
});

describe('color FX placement (COLORFX-10754)', () => {
	test('the stems sum upstream of trim and EQ, with no color FX node before the high shelf', () => {
		const ctx = fakeContext();
		const { nodes } = buildDeck1(ctx);
		const rt = spliceSpaceAndEcho(nodes);
		assertSameSet([...nodes.analyser.outputs], [nodes.trim], 'analyser outputs');
		assertSameSet([...nodes.trim.outputs], [nodes.low], 'trim outputs');
		assertSameSet([...nodes.low.outputs], [nodes.mid], 'low outputs');
		assertSameSet([...nodes.mid.outputs], [nodes.high], 'mid outputs');
		const upstream = new Set();
		const queue = [nodes.high];
		while (queue.length > 0) {
			for (const n of inbound(ctx, queue.shift())) {
				if (upstream.has(n)) continue;
				upstream.add(n);
				queue.push(n);
			}
		}
		assert.equal(
			[...upstream].filter((n) => rt.graph.nodes.includes(n)).length,
			0,
			'a color FX node feeds the EQ'
		);
	});

	test('colorBus is fed only by the FILTER branches after the high shelf', () => {
		const ctx = fakeContext();
		const { nodes } = buildDeck1(ctx);
		const rt = spliceSpaceAndEcho(nodes);
		assertSameSet(
			inbound(ctx, nodes.colorBus),
			[nodes.filterDry, nodes.filterLpWet, nodes.filterHpWet],
			'colorBus inputs'
		);
		assertSameSet(
			[...nodes.high.outputs],
			[nodes.filterDry, nodes.filterLp, nodes.filterHp],
			'high outputs'
		);
		assertSameSet(inbound(ctx, nodes.filterDry), [nodes.high], 'filterDry inputs');
		assertSameSet(inbound(ctx, nodes.filterLpWet), [nodes.filterLp], 'filterLpWet inputs');
		assertSameSet(inbound(ctx, nodes.filterHpWet), [nodes.filterHp], 'filterHpWet inputs');
		assertSameSet(
			[...nodes.colorBus.outputs],
			[nodes.cue, nodes.fader, rt.graph.toneDark, rt.graph.toneBright],
			'colorBus outputs'
		);
	});

	test('the SPACE and ECHO returns connect to exactly cue and fader', () => {
		const ctx = fakeContext();
		const { nodes, masterGain, cueSum } = buildDeck1(ctx);
		const rt = spliceSpaceAndEcho(nodes);
		assertSameSet([...rt.graph.echoReturn.outputs], [nodes.cue, nodes.fader], 'echoReturn outputs');
		assertSameSet(
			[...rt.graph.spaceReturn.outputs],
			[nodes.cue, nodes.fader],
			'spaceReturn outputs'
		);
		const sinks = [nodes.cue, nodes.fader, nodes.xf, masterGain, cueSum];
		const feeders = rt.graph.nodes.filter((n) => sinks.some((s) => n.outputs.has(s)));
		assertSameSet(
			feeders,
			[rt.graph.echoReturn, rt.graph.spaceReturn],
			'color FX nodes feeding the channel tail'
		);
	});

	test('nothing of the color FX sits after the fader, on thru and USB routing', () => {
		const ctx = fakeContext();
		const thru = buildDeck1(ctx);
		const rtThru = spliceSpaceAndEcho(thru.nodes);
		assertSameSet([...thru.nodes.fader.outputs], [thru.nodes.xf], 'fader outputs (thru)');
		assertSameSet(
			downstream(thru.nodes.fader),
			[thru.nodes.xf, thru.masterGain],
			'after the fader (thru)'
		);
		assert.equal(colorFxAfterFader(thru.nodes, rtThru.graph).length, 0);

		const usbCtx = fakeContext();
		const usb = buildDeck1(usbCtx, true);
		const rtUsb = spliceSpaceAndEcho(usb.nodes);
		assert.ok(usb.nodes.extsplit !== null, 'USB routing built a splitter');
		assertSameSet(
			downstream(usb.nodes.fader),
			[usb.nodes.extsplit, usb.externalMerger],
			'after the fader (USB)'
		);
		assert.equal(colorFxAfterFader(usb.nodes, rtUsb.graph).length, 0);
	});

	test('the cue is a pre-fader tap after the color FX', () => {
		const ctx = fakeContext();
		const { nodes, cueSum } = buildDeck1(ctx);
		const rt = spliceSpaceAndEcho(nodes);
		assertSameSet(
			inbound(ctx, nodes.cue),
			[nodes.colorBus, rt.graph.echoReturn, rt.graph.spaceReturn],
			'cue inputs'
		);
		assertSameSet([...nodes.cue.outputs], [cueSum], 'cue outputs');
	});

	test('control: a post-fader splice fails the after-the-fader check', () => {
		const ctx = fakeContext();
		const { nodes } = buildDeck1(ctx);
		const postFader = graph.buildColorFxGraph(ctx, nodes.colorBus, [nodes.cue, nodes.xf]);
		const offending = colorFxAfterFader(nodes, postFader);
		assertSameSet(
			offending,
			[postFader.echoReturn, postFader.spaceReturn],
			'post-fader returns flagged'
		);
	});
});
