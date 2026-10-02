/**
 * Minimal real-graph-capable `AudioContext` stand-in for tests that need
 * `engine.dispose()`'s ACTUAL teardown path (not a fake `disposeEngine`
 * reproducing its side effects by hand) to run against a genuinely built
 * graph -- see gig-teardown-remount.test.mjs, discussion_r4130444130.
 *
 * Deliberately narrower than deck-load-no-analysis-path.test.mjs's fixture:
 * this one never loads a deck (no fetch, no decodeAudioData, no worklet), it
 * only has to satisfy `_ensureGraph()` / `_resumeContext()` in
 * audio-engine.svelte.ts and the node surface `disposeAudioResources()`
 * disconnects and closes. Plain JS (not `.ts`): loaded directly by node:test,
 * not through the esbuild `loadTypeScriptModule` bundle, since it holds no
 * `$lib` imports of its own.
 */
function makeAudioParam(value = 1) {
	return {
		value,
		setValueAtTime() {},
		setTargetAtTime() {},
		linearRampToValueAtTime() {}
	};
}

function makeNode(context, extra = {}) {
	return {
		context,
		connect() {},
		disconnect() {},
		gain: makeAudioParam(),
		delayTime: makeAudioParam(0),
		...extra
	};
}

let _lastCreated = null;

/** The most recently constructed fake context, so a test can reach in and
 * force its `close()` to reject before triggering disposal. */
export function lastCreatedFakeAudioContext() {
	if (_lastCreated === null) throw new Error('no FakeAudioContext has been constructed yet');
	return _lastCreated;
}

class FakeAudioContext {
	state = 'running';
	sampleRate = 48_000;
	currentTime = 0;
	baseLatency = 128 / 48_000;
	outputLatency = 0.032;
	destination = { connect() {}, disconnect() {} };
	audioWorklet = { addModule: async () => {} };

	constructor() {
		_lastCreated = this;
	}
	createGain() {
		return makeNode(this);
	}
	createAnalyser() {
		return makeNode(this);
	}
	createDelay(_maxDelaySeconds) {
		return makeNode(this);
	}
	createChannelSplitter(_numberOfOutputs) {
		return makeNode(this);
	}
	createChannelMerger(_numberOfInputs) {
		return makeNode(this);
	}
	createBiquadFilter() {
		return makeNode(this, { type: 'lowpass', frequency: makeAudioParam(1000), Q: makeAudioParam(1) });
	}
	createMediaStreamDestination() {
		return { context: this, stream: { getTracks: () => [] }, connect() {}, disconnect() {} };
	}
	addEventListener() {}
	removeEventListener() {}
	async resume() {
		this.state = 'running';
	}
	async close() {
		this.state = 'closed';
	}
}

const _installedGlobals = ['window', 'navigator', 'localStorage', 'AudioContext'];

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

/** Installs just enough of `window`/`navigator`/`localStorage`/`AudioContext`
 * for `_ensureGraph()` to build a real graph. Returns the cleanup. */
export function installFakeAudioGraphGlobals() {
	const localStorage = {
		getItem: () => null,
		setItem: () => {},
		removeItem: () => {}
	};
	defineGlobal('window', {
		location: { href: 'https://fake-audio-context.example.test/performance', search: '' },
		localStorage,
		addEventListener() {},
		removeEventListener() {}
	});
	defineGlobal('navigator', { userAgent: 'fake-audio-context-fixture' });
	defineGlobal('localStorage', localStorage);
	defineGlobal('AudioContext', FakeAudioContext);
	return () => {
		for (const name of _installedGlobals) {
			delete globalThis[name];
		}
		_lastCreated = null;
	};
}
