// requirement: AUDFID-03a (output-stall rebuild tap teardown, plus /performance route-unmount `engine.dispose()`)
// [if] the graph teardown has already disconnected the master bus, and the tap's disconnect throws [then] every output-stall rebuild fails and the decks are unloaded ⛔️
// [if] the tap's disconnect leaves its splitter feeding the analysers [then] the dropped tap keeps pulling audio ⛔️
// [if] `/performance` unmounts (`engine.dispose()`) after the graph has built the AUDFID-03 tap [then] `_fidelityTap` is disconnected and null ⛔️
// [if] `/performance` remounts after that unmount [then] `_ensureGraph` creates exactly one new tap ⛔️
import assert from 'node:assert/strict';
import { after, before, beforeEach, describe, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let tap;
before(async () => {
	tap = await loadTypeScriptModule('src/lib/rb/fidelity-guard-tap.ts');
});

/** Web Audio's connect/disconnect rules, as Chromium enforces them: disconnect(dest) from a node
 * not connected to dest throws InvalidAccessError; bare disconnect() always succeeds. */
class FakeNode {
	outputs = new Set();
	connect(dest) {
		this.outputs.add(dest);
		return dest;
	}
	disconnect(dest) {
		if (dest === undefined) {
			this.outputs.clear();
			return;
		}
		if (!this.outputs.has(dest)) {
			const error = new Error("Failed to execute 'disconnect' on 'AudioNode': the given destination is not connected.");
			error.name = 'InvalidAccessError';
			throw error;
		}
		this.outputs.delete(dest);
	}
}

function fakeContext() {
	const made = { splitter: null, analysers: [] };
	const ctx = {
		sampleRate: 48000,
		createChannelSplitter: () => (made.splitter = new FakeNode()),
		createAnalyser: () => {
			const node = Object.assign(new FakeNode(), { fftSize: 0, getFloatTimeDomainData() {} });
			made.analysers.push(node);
			return node;
		}
	};
	return { ctx, made };
}

test('if the teardown already disconnected the master bus then the tap disconnect does not throw', () => {
	const { ctx, made } = fakeContext();
	const bus = new FakeNode();
	const master = tap.createMasterStereoTap(ctx, bus);
	assert.ok(bus.outputs.has(made.splitter), 'control: the tap is fed from the bus');
	bus.disconnect(); // disposeAudioResources: every graph node, the mute bus included, before _resetGraphState
	assert.doesNotThrow(() => master.disconnect(), 'a throw here fails every output-stall rebuild (IOPIN-12)');
});

test('if the tap is disconnected then its splitter no longer feeds the analysers', () => {
	const { ctx, made } = fakeContext();
	const master = tap.createMasterStereoTap(ctx, new FakeNode());
	assert.equal(made.splitter.outputs.size, 2, 'control: the splitter feeds both analysers');
	master.disconnect();
	assert.equal(made.splitter.outputs.size, 0);
});

describe('AUDFID-03a: route unmount drops the fidelity tap like stall-rebuild teardown', () => {
	let audio;
	let FG;

	before(async () => {
		installWindow('');
		globalThis.AudioContext = FakeAudioContext;
		FG = await loadTypeScriptModule('src/lib/player/fidelity-guard.ts');
		const entry = await loadTypeScriptModule('tests/unit/fixtures/djio-fallback-entry.ts');
		audio = entry.audio;
	});

	beforeEach(async () => {
		await audio.engine.dispose();
		FakeAudioContext.instances = [];
	});

	after(async () => {
		await audio.engine.dispose();
		delete globalThis.AudioContext;
		delete globalThis.location;
		delete globalThis.window;
	});

	test('after dispose the module tap is null and disconnect ran once', async () => {
		await audio.ensureAudioGraphForCue();
		const first = audio._fidelityTapForTests();
		assert.ok(first, 'graph build should install the AUDFID-03 tap');
		let disconnectCalls = 0;
		const orig = first.disconnect;
		first.disconnect = () => {
			disconnectCalls += 1;
			return orig.call(first);
		};
		await audio.engine.dispose();
		assert.equal(audio._fidelityTapForTests(), null);
		assert.equal(disconnectCalls, 1);

		const ctx = FakeAudioContext.instances[0];
		const splitters = ctx.created.filter((n) => n.kind === 'splitter');
		assert.ok(
			splitters.some((s) => ctx.disconnected.has(s)),
			'FakeAudioContext records splitter.disconnect from the tap teardown'
		);
	});

	test('remount after unmount creates exactly one new tap (two 16384-frame analysers on the new context)', async () => {
		await audio.ensureAudioGraphForCue();
		const first = audio._fidelityTapForTests();
		assert.ok(first);
		await audio.engine.dispose();
		assert.equal(audio._fidelityTapForTests(), null);

		await audio.ensureAudioGraphForCue();
		const second = audio._fidelityTapForTests();
		assert.ok(second);
		assert.notStrictEqual(second, first);

		const ctx = FakeAudioContext.instances.at(-1);
		const windowFrames = FG.FIDELITY_GUARD_CFG.WINDOW_FRAMES;
		const fidelityAnalysers = ctx.created.filter(
			(n) => n.kind === 'analyser' && n.fftSize === windowFrames
		);
		assert.equal(
			fidelityAnalysers.length,
			2,
			'createMasterStereoTap is the only graph site that sets the fidelity window size; two channels means one tap'
		);
	});
});
