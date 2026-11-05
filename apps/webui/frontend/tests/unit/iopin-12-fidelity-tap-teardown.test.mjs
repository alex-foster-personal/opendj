// requirement: AUDFID-03a (the output-stall rebuild must not fail on the AUDFID-03 master tap's teardown)
// [if] the graph teardown has already disconnected the master bus, and the tap's disconnect throws [then] every output-stall rebuild fails and the decks are unloaded ⛔️
// [if] the tap's disconnect leaves its splitter feeding the analysers [then] the dropped tap keeps pulling audio ⛔️
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

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
