/**
 * NATIVE-5886: the engine reads the page's xrun count as a live counter only
 * while the page says its detector is armed (apps/webui/server/ahead_analysis.py
 * `mirror_xruns`, XRUN_DETECTOR_ARMED). A detector that never installed or
 * that threw keeps its last count, zero included, and that number can never
 * rise. So the page must say which of the two it is publishing.
 *
 * The sentinel module runs here for real against a stand-in AudioContext:
 * node:test has no audio thread, and none of this needs one.
 *
 * Regression lines:
 *   - if a failed install still publishes detector 'armed' then broken
 *   - if a processor that threw still publishes detector 'armed' then broken
 *   - if a healthy install does not publish 'armed' then while-playing analysis never runs
 *   - if a detached sentinel still publishes 'armed' then broken
 *   - if an installed node publishes 'armed' before its worklet reports that
 *     cadence warmup is over then the engine runs while-playing analysis during
 *     the ~1.5 s in which every gap is ignored, and an underrun there is missed
 *   - if the warmup gate never lifts then while-playing analysis never runs
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeWorkletNode {
	static last = null;
	constructor() {
		this.port = { onmessage: null, postMessage() {} };
		this.listeners = new Map();
		FakeWorkletNode.last = this;
	}
	addEventListener(type, listener) {
		this.listeners.set(type, listener);
	}
	connect() {}
	disconnect() {}
	/** A worklet window report, delivered the way the real port delivers one. */
	report({ judging }) {
		assert.ok(this.port.onmessage, 'control: the sentinel listens on its port');
		this.port.onmessage({
			data: {
				xruns: 0,
				parked: 0,
				callbacks: 690,
				worst_gap_ms: 3,
				window_ms: 2000,
				threshold_ms: 9.7,
				clock: 'audio',
				judging
			}
		});
	}
	fire(type) {
		const listener = this.listeners.get(type);
		assert.ok(listener, `control: the sentinel listens for ${type}`);
		listener();
	}
}

function fakeContext({ addModule = async () => {} } = {}) {
	return {
		audioWorklet: { addModule },
		baseLatency: 0,
		sampleRate: 48000,
		destination: {},
		createGain: () => ({ gain: { value: 1 }, connect() {} })
	};
}

async function loadSentinel() {
	globalThis.AudioWorkletNode = FakeWorkletNode;
	FakeWorkletNode.last = null;
	return loadTypeScriptModule('src/lib/rb/xrun-sentinel.ts');
}

test('before any install the detector is unarmed, and the count is still published', async () => {
	const sentinel = await loadSentinel();
	assert.deepEqual(sentinel.readXrunSentinelMirror(), {
		...sentinel.readXrunSessionCounter(),
		detector: 'unarmed'
	});
	assert.equal(sentinel.readXrunSentinelMirror().xruns, 0);
});

test('an installed node stays unarmed until its worklet reports that warmup is over', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	assert.equal(
		sentinel.readXrunSentinelMirror().detector,
		'unarmed',
		'installed is not judging yet'
	);
	FakeWorkletNode.last.report({ judging: false });
	assert.equal(
		sentinel.readXrunSentinelMirror().detector,
		'unarmed',
		'a warmup window arms nothing'
	);
	assert.equal(
		sentinel.readXrunSentinelMirror().reports,
		1,
		'control: the warmup report was folded'
	);
});

test('control: a healthy install publishes armed once its worklet is judging', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	FakeWorkletNode.last.report({ judging: false });
	FakeWorkletNode.last.report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
	FakeWorkletNode.last.report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed', 'later windows keep it armed');
});

test('a report without the judging field arms nothing', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	FakeWorkletNode.last.report({ judging: undefined });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'unarmed');
});

test('a judging report from a node that threw does not re-arm it', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	FakeWorkletNode.last.fire('processorerror');
	FakeWorkletNode.last.report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});

test('a judging report from a replaced node does not arm the node installed now', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	const oldNode = FakeWorkletNode.last;
	const oldOnMessage = oldNode.port.onmessage;
	await sentinel.installXrunSentinel(fakeContext());
	oldOnMessage({
		data: {
			xruns: 0,
			parked: 0,
			callbacks: 690,
			worst_gap_ms: 3,
			window_ms: 2000,
			threshold_ms: 9.7,
			clock: 'audio',
			judging: true
		}
	});
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'unarmed');
	FakeWorkletNode.last.report({ judging: true });
	assert.equal(
		sentinel.readXrunSentinelMirror().detector,
		'armed',
		'control: the current node still arms'
	);
});

test('an install that rejects publishes failed, and still rejects to its caller', async () => {
	const sentinel = await loadSentinel();
	const refused = new Error('addModule refused');
	await assert.rejects(
		sentinel.installXrunSentinel(fakeContext({ addModule: async () => Promise.reject(refused) })),
		refused
	);
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});

test('a context with no AudioWorklet publishes failed', async () => {
	const sentinel = await loadSentinel();
	await assert.rejects(sentinel.installXrunSentinel({ ...fakeContext(), audioWorklet: undefined }));
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});

test('a processor that throws while rendering publishes failed', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	FakeWorkletNode.last.report({ judging: true });
	FakeWorkletNode.last.fire('processorerror');
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});

test('detaching the sentinel publishes unarmed until the next install arms it again', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	FakeWorkletNode.last.report({ judging: true });
	sentinel.detachXrunSentinel();
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'unarmed');
	await sentinel.installXrunSentinel(fakeContext());
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'unarmed', 'a remount warms up again');
	FakeWorkletNode.last.report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
});

test('a late failure from a replaced context does not mark the armed one failed', async () => {
	const sentinel = await loadSentinel();
	let rejectOld = () => {};
	const oldInstall = sentinel.installXrunSentinel(
		fakeContext({ addModule: () => new Promise((_resolve, reject) => (rejectOld = reject)) })
	);
	await sentinel.installXrunSentinel(fakeContext());
	const armedNode = FakeWorkletNode.last;
	armedNode.report({ judging: true });
	rejectOld(new Error('old context closed mid-install'));
	await assert.rejects(oldInstall);
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
	// The control for that exemption: the armed node's own error still counts.
	armedNode.fire('processorerror');
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});

test('an error from a node that was already replaced does not mark the new one failed', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	const oldNode = FakeWorkletNode.last;
	sentinel.detachXrunSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	FakeWorkletNode.last.report({ judging: true });
	oldNode.fire('processorerror');
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
});

test('a flush acknowledgement that says judging arms too, and one that does not arms nothing', async () => {
	const sentinel = await loadSentinel();
	await sentinel.installXrunSentinel(fakeContext());
	const node = FakeWorkletNode.last;
	const posted = [];
	node.port.postMessage = (message) => posted.push(message);
	globalThis.window ??= globalThis;
	const early = sentinel.flushXrunSessionCounter();
	node.port.onmessage({
		data: { kind: 'xrun-flush-ack', requestId: posted[0].requestId, judging: false }
	});
	await assert.rejects(early, /has not completed cadence warmup/);
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'unarmed');
	const late = sentinel.flushXrunSessionCounter();
	node.port.onmessage({
		data: { kind: 'xrun-flush-ack', requestId: posted[1].requestId, judging: true }
	});
	await late;
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
});

/** An addModule the test resolves by hand, so two installs can finish in either order. */
function heldContext() {
	let release = () => {};
	const ctx = fakeContext({ addModule: () => new Promise((resolve) => (release = resolve)) });
	return { ctx, release: () => release() };
}

function countNodes() {
	const made = [];
	globalThis.AudioWorkletNode = class extends FakeWorkletNode {
		constructor(...args) {
			super(...args);
			made.push(this);
		}
	};
	return made;
}

test('an older install that resolves after a newer one does not replace the newer node', async () => {
	const sentinel = await loadSentinel();
	const made = countNodes();
	const old = heldContext();
	const current = heldContext();
	const oldInstall = sentinel.installXrunSentinel(old.ctx);
	const currentInstall = sentinel.installXrunSentinel(current.ctx);
	current.release();
	await currentInstall;
	assert.equal(made.length, 1, 'control: the newer install built its node');
	const armedNode = made[0];
	armedNode.report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
	old.release();
	await oldInstall;
	assert.equal(made.length, 1, 'the superseded context got a node of its own');
	assert.equal(
		sentinel.readXrunSentinelMirror().detector,
		'armed',
		'the late install reset the armed detector'
	);
	// The node on the current context is still the one that arms and fails.
	armedNode.fire('processorerror');
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});

test('CONTROL: when the older install resolves first, the newer one still builds and arms', async () => {
	const sentinel = await loadSentinel();
	const made = countNodes();
	const old = heldContext();
	const current = heldContext();
	const oldInstall = sentinel.installXrunSentinel(old.ctx);
	const currentInstall = sentinel.installXrunSentinel(current.ctx);
	old.release();
	await oldInstall;
	current.release();
	await currentInstall;
	assert.equal(made.length, 1, 'only the newest install builds a node');
	made[0].report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
});

test('CONTROL: a single install still arms, and a superseded install that rejects does not mark it failed', async () => {
	const sentinel = await loadSentinel();
	const made = countNodes();
	await sentinel.installXrunSentinel(fakeContext());
	assert.equal(made.length, 1);
	made[0].report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
	// The route remounts: the old graph detaches, then two installs race. The
	// older failing while the newer is still loading speaks for nothing.
	sentinel.detachXrunSentinel();
	let rejectOld = () => {};
	const oldInstall = sentinel.installXrunSentinel(
		fakeContext({ addModule: () => new Promise((_resolve, reject) => (rejectOld = reject)) })
	);
	const pending = heldContext();
	const pendingInstall = sentinel.installXrunSentinel(pending.ctx);
	rejectOld(new Error('old context closed mid-install'));
	await assert.rejects(oldInstall);
	assert.equal(
		sentinel.readXrunSentinelMirror().detector,
		'unarmed',
		'a superseded failure marked the detector failed'
	);
	pending.release();
	await pendingInstall;
	made.at(-1).report({ judging: true });
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'armed');
	// And the latest install's own rejection still publishes failed.
	sentinel.detachXrunSentinel();
	await assert.rejects(
		sentinel.installXrunSentinel(
			fakeContext({ addModule: async () => Promise.reject(new Error('refused')) })
		)
	);
	assert.equal(sentinel.readXrunSentinelMirror().detector, 'failed');
});
