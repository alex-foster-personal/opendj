// requirement: BEATFX-13805, BEATFX-13882
// [if] fx.beat_rack is off and the channel graph builds any beat FX node [then] the off path is not today's graph, [else stop].
// [if] the flag is on but the unit is off or at level 0 and a send opens or a dry leaves 1 [then] the dry path is not bit-exact, [else stop].
// [if] the channel echo is not timed to the division at the tempo, or a tempo change slides delayTime [then] the echo is mistimed, [else stop].
// [if] the send is not fed after the fader, or the wet reaches the cue or a fader [then] a fader cut kills the tail or leaks it to cue, [else stop].
// [if] the master target's wet does not join masterGain alone, or can reach the unit input [then] the master send can feed back, [else stop].
// [if] a target switch while on does not ramp the old send to 0 and the new to 1 over 12 ms [then] switching clicks, [else stop].
// [if] the echo row's slot kernel lacks input, output, setTiming, setDepth or dispose [then] the BeatFxKernel contract is broken, [else stop].
// [if] beat_fx delay, echo or pingpong builds the slot's echo kernel in any other mode or routing [then] the three effects sound alike, [else stop].
// [if] the slot kernel's dispose skips the echo kernel's own dispose or disconnects a node twice [then] the slot leaks or double-frees, [else stop].
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let channel, insert, live, fx, buildFlags;

class FakeParam {
	constructor(value) {
		this.value = value;
		this.events = [];
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e[2] < t);
	}
	setValueAtTime(v, t) {
		this.events.push(['set', v, t]);
	}
	linearRampToValueAtTime(v, t) {
		this.events.push(['linear', v, t]);
	}
	/** The value the newest event leaves, or the static value. */
	settled() {
		return this.events.length === 0 ? this.value : this.events[this.events.length - 1][1];
	}
}
// Fake nodes keep their context non-enumerable so a failing assert never inspects the whole graph.
class FakeNode {
	constructor(kind, ctx) {
		this.kind = kind;
		Object.defineProperty(this, 'context', { value: ctx, enumerable: false });
		this.outputs = new Set();
		this.disconnects = 0;
	}
	connect(node) {
		this.outputs.add(node);
		return node;
	}
	disconnect(node) {
		this.disconnects += 1;
		if (node === undefined) this.outputs.clear();
		else this.outputs.delete(node);
	}
}
function fakeContext() {
	const ctx = {
		currentTime: 10,
		sampleRate: 48000,
		_node(kind, params) {
			const n = new FakeNode(kind, ctx);
			for (const [name, value] of Object.entries(params)) n[name] = new FakeParam(value);
			ctx.created.push(n);
			return n;
		},
		createGain: () => ctx._node('gain', { gain: 1 }),
		createAnalyser: () => ctx._node('analyser', {}),
		createDelay: (max) =>
			Object.assign(ctx._node('delay', { delayTime: 0 }), { maxDelayTime: max }),
		createBiquadFilter: () => ctx._node('biquad', { frequency: 350, Q: 1, gain: 0 }),
		createWaveShaper: () => Object.assign(ctx._node('waveshaper', {}), { curve: null }),
		createChannelMerger: () => ctx._node('merger', {})
	};
	Object.defineProperty(ctx, 'created', { value: [], enumerable: false });
	return ctx;
}
const STRIP = {
	trim: 0.5,
	eq_low: 0.5,
	eq_mid: 0.5,
	eq_high: 0.5,
	filter: 0.5,
	fader: 1,
	cue_enabled: true,
	assign: 'thru'
};

/** Build the real four-deck channel graph with `fx.beat_rack` set to `on`. */
function buildChannels(on) {
	buildFlags.flags = [{ flag_id: 'fx.beat_rack', enabled: on }];
	const ctx = fakeContext();
	const byDeck = {};
	const masterGain = ctx.createGain();
	const cueSum = ctx.createGain();
	channel.buildDeckChannelGraph({
		ctx,
		mixerState: { channels: { 1: STRIP, 2: STRIP, 3: STRIP, 4: STRIP }, crossfader: 0.5 },
		masterGain,
		externalMerger: null,
		routing: null,
		cueSum,
		xfGainFor: () => 1,
		onDeck: (deck, nodes) => {
			byDeck[deck] = nodes;
		}
	});
	return { ctx, byDeck, masterGain, cueSum, unit: insert.liveBeatFxInsert() };
}
function reaches(from, to) {
	const seen = new Set();
	const queue = [from];
	while (queue.length > 0) {
		const node = queue.shift();
		if (node === to) return true;
		if (seen.has(node)) continue;
		seen.add(node);
		queue.push(...node.outputs);
	}
	return false;
}
const echoLines = (ctx) => ctx.created.filter((n) => n.kind === 'delay');

before(async () => {
	({ channel, insert, live, fx, buildFlags } = await loadTypeScriptModule(
		'tests/unit/fixtures/beat-fx-unit-entry.ts',
		{
			viteApiBase: 'http://127.0.0.1:9'
		}
	));
});
beforeEach(() =>
	live.applyBeatFx({ enabled: false, level: 0.5, target: 'master', effect: 'echo', beats: 1 })
);

test('flag off: no beat FX node, each fader feeds only its crossfader gain', () => {
	const { byDeck, unit } = buildChannels(false);
	assert.equal(unit, null);
	for (const deck of [1, 2, 3, 4]) {
		const outs = [...byDeck[deck].fader.outputs];
		assert.equal(outs.length, 1);
		assert.ok(outs[0] === byDeck[deck].xf);
	}
});

test('flag on, unit off or level 0: every dry is exactly 1, every send exactly 0, no kernel joins', () => {
	const { unit } = buildChannels(true);
	for (const command of [
		{ enabled: true, level: 0 },
		{ enabled: false, level: 0.8 }
	]) {
		live.applyBeatFx(command);
		for (const dry of unit.drys.values()) assert.equal(dry.gain.settled(), 1);
		for (const send of unit.sends.values()) assert.equal(send.gain.settled(), 0);
		assert.equal(unit.slot, null);
	}
});

test('channel echo is timed to the division at the tempo; a tempo change steps the other line', () => {
	const { ctx, unit } = buildChannels(true);
	live.beatFxUnit.tempo_bpm = 128;
	live.applyBeatFx({ target: '2', beats: 1, level: 0.7, enabled: true });
	const [line0, line1] = echoLines(ctx);
	assert.ok(Math.abs(line0.delayTime.settled() - 60 / 128) < 0.002);
	assert.equal(unit.slot.effect, 'echo');
	live.beatFxUnit.tempo_bpm = 126;
	live.applyBeatFx({ level: 0.7 });
	assert.ok(Math.abs(line1.delayTime.settled() - 60 / 126) < 0.002);
	for (const line of [line0, line1]) assert.ok(line.delayTime.events.every((e) => e[0] === 'set'));
});

test('send is post-fader and the wet bypasses every fader and cue, so a fader cut leaves the tail', () => {
	const { byDeck, cueSum, masterGain, unit } = buildChannels(true);
	live.applyBeatFx({ target: '1', level: 0.7, enabled: true });
	assert.ok(byDeck[1].fader.outputs.has(unit.sends.get(1)));
	assert.ok(byDeck[1].colorBus.outputs.has(byDeck[1].cue));
	assert.equal(reaches(unit.unitIn, cueSum), false);
	assert.deepEqual([...unit.slot.kernel.output.outputs].length, 1);
	assert.ok([...unit.slot.kernel.output.outputs][0] === masterGain);
	for (const deck of [1, 2, 3, 4])
		assert.equal(reaches(unit.slot.kernel.output, byDeck[deck].fader), false);
});

test('master target opens every send; its wet joins masterGain only and never reaches the unit input', () => {
	const { masterGain, unit } = buildChannels(true);
	live.applyBeatFx({ target: 'master', level: 0.7, enabled: true });
	for (const send of unit.sends.values()) assert.equal(send.gain.settled(), 1);
	assert.ok([...unit.slot.kernel.output.outputs][0] === masterGain);
	assert.equal(reaches(unit.slot.kernel.output, unit.unitIn), false);
});

test('target switch while on ramps the old send to 0 and the new to 1 over 12 ms', () => {
	const { ctx, unit } = buildChannels(true);
	live.applyBeatFx({ target: '1', level: 0.7, enabled: true });
	ctx.currentTime = 11;
	live.applyBeatFx({ target: '3' });
	const ramp = (deck) => unit.sends.get(deck).gain.events.at(-1);
	assert.deepEqual(ramp(1).slice(0, 2), ['linear', 0]);
	assert.deepEqual(ramp(3).slice(0, 2), ['linear', 1]);
	for (const deck of [1, 3]) assert.ok(Math.abs(ramp(deck)[2] - 11.012) < 1e-9);
});

test('the echo row exports a BeatFxKernel with input, output, setTiming, setDepth and dispose', () => {
	const kernel = fx.beatFxEffectDef('echo').createKernel(fakeContext());
	assert.ok(kernel.input instanceof FakeNode && kernel.output instanceof FakeNode);
	for (const name of ['setTiming', 'setDepth', 'dispose'])
		assert.equal(typeof kernel[name], 'function');
	assert.equal(fx.beatFxEffectDef('spiral').createKernel, undefined);
});

test('beat_fx delay, echo and pingpong each build the slot echo kernel in their own mode', () => {
	const { unit } = buildChannels(true);
	for (const effect of ['delay', 'echo', 'pingpong']) {
		live.applyBeatFx({ effect, target: '1', level: 0.7, enabled: true });
		const echo = unit.slot.kernel.echo;
		assert.equal(echo.mode(), effect);
		assert.equal(echo.feedback.gain.value === 0, effect === 'delay');
		assert.equal(echo.pingWet.gain.value, effect === 'pingpong' ? 1 : 0);
		assert.equal(echo.wet.gain.value, effect === 'pingpong' ? 0 : 1);
	}
});

test('the slot kernel dispose runs the echo kernel dispose, so every node disconnects exactly once', () => {
	const ctx = fakeContext();
	const kernel = fx.beatFxEffectDef('delay').createKernel(ctx);
	kernel.dispose();
	assert.ok(ctx.created.length > 0);
	for (const node of ctx.created) assert.equal(node.disconnects, 1, node.kind);
	assert.throws(() => kernel.echo.setTime(0.5, 10), /disposed/);
});
