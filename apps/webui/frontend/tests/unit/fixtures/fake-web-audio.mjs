/**
 * A recording stand-in for Web Audio and window, for the IOPIN-12 WIRING
 * checks only. Nothing built on it is acceptance evidence.
 *
 * node:test has no AudioContext. Every node here records its edges on the
 * owning context so a check can assert which calls the engine made and what
 * it connected to what, and a connect across contexts throws as the real API
 * does. It renders no audio, decodes nothing and runs no worklet, so it cannot
 * show that a browser's graph builds or plays. Those claims are proved in a
 * real browser: tests/e2e/iopin-12-real-audio.spec.ts and
 * tests/e2e/audio-output-topology.spec.ts.
 */

export class FakeParam {
	value = 0;
	setValueAtTime(value) { this.value = value; return this; }
	setTargetAtTime(value) { this.value = value; return this; }
	linearRampToValueAtTime(value) { this.value = value; return this; }
	exponentialRampToValueAtTime(value) { this.value = value; return this; }
	cancelScheduledValues() { return this; }
	cancelAndHoldAtTime() { return this; }
}

export function fakeNode(ctx, kind, params = [], extra = {}) {
	const node = {
		kind,
		context: ctx,
		channelCount: 2,
		channelCountMode: 'max',
		channelInterpretation: 'speakers',
		connect(to, output = 0, input = 0) {
			// The real API throws InvalidAccessError across contexts, which is
			// how a stale node from a discarded graph would surface.
			if (to.kind !== undefined && to.context !== ctx) {
				throw new Error(`InvalidAccessError: ${node.kind} -> ${to.kind} crosses AudioContexts`);
			}
			ctx.edges.push({ from: node, to, output, input });
			return to;
		},
		disconnect() {
			ctx.disconnected.add(node);
		},
		addEventListener() {},
		removeEventListener() {},
		...extra
	};
	for (const name of params) node[name] = new FakeParam();
	return node;
}

export class FakeAudioContext {
	/** Channels the fake output device exposes, read by each NEW context. */
	static maxChannelCount = 2;
	/** Optional injected construction failure: (ctx, kind, nthCallOfKind) => boolean. */
	static failWhen = null;
	static instances = [];

	constructor(options) {
		this.options = options;
		this.state = 'running';
		this.sampleRate = 48000;
		this.baseLatency = 256 / 48000;
		this.outputLatency = 0.02;
		this.currentTime = 0;
		this.edges = [];
		this.disconnected = new Set();
		this.closed = false;
		this.calls = new Map();
		/** Every node made through `_create`, in creation order. */
		this.created = [];
		this.destination = fakeNode(this, 'destination', [], {
			maxChannelCount: FakeAudioContext.maxChannelCount
		});
		this.audioWorklet = {
			addModule: async () => {
				throw new Error('AudioWorklet is not available under node:test');
			}
		};
		FakeAudioContext.instances.push(this);
	}

	_create(kind, params, extra) {
		const nth = (this.calls.get(kind) ?? 0) + 1;
		this.calls.set(kind, nth);
		if (FakeAudioContext.failWhen?.(this, kind, nth) === true) {
			throw new Error(`injected ${kind} construction failure`);
		}
		const node = fakeNode(this, kind, params, extra);
		this.created.push(node);
		return node;
	}

	createGain() { return this._create('gain', ['gain']); }
	createAnalyser() {
		return this._create('analyser', [], {
			fftSize: 2048,
			frequencyBinCount: 1024,
			getFloatTimeDomainData() {},
			getByteTimeDomainData() {},
			getFloatFrequencyData() {}
		});
	}
	createDelay(maxDelayTime) { return this._create('delay', ['delayTime'], { maxDelayTime }); }
	createBiquadFilter() { return this._create('biquad', ['frequency', 'Q', 'gain', 'detune'], { type: 'lowpass' }); }
	createChannelSplitter(outputs = 6) { return this._create('splitter', [], { numberOfOutputs: outputs }); }
	createChannelMerger(inputs = 6) { return this._create('merger', [], { numberOfInputs: inputs }); }
	createStereoPanner() { return this._create('panner', ['pan']); }
	createConstantSource() { return this._create('constant', ['offset'], { start() {}, stop() {} }); }
	createOscillator() { return this._create('oscillator', ['frequency', 'detune'], { start() {}, stop() {} }); }
	createMediaStreamDestination() {
		return this._create('msd', [], { stream: { getTracks: () => [], getAudioTracks: () => [] } });
	}
	createBuffer(numberOfChannels, length, sampleRate) {
		return { numberOfChannels, length, sampleRate, duration: length / sampleRate, getChannelData: () => new Float32Array(length) };
	}
	/** Decodes to silence of a length set by the byte count, as the real call yields PCM of the file's length. */
	async decodeAudioData(bytes) {
		const length = Math.max(1, Math.floor((bytes.byteLength ?? 0) / 4));
		return this.createBuffer(2, length, this.sampleRate);
	}
	addEventListener() {}
	removeEventListener() {}
	getOutputTimestamp() { return { contextTime: 0, performanceTime: 0 }; }
	async resume() { this.state = 'running'; }
	async suspend() { this.state = 'suspended'; }
	async close() {
		this.closed = true;
		this.state = 'closed';
	}
}

/** Everything upstream of `target` in one context's recorded edges. */
export function ancestorsOf(ctx, target) {
	const seen = new Set();
	const stack = [target];
	while (stack.length > 0) {
		const current = stack.pop();
		for (const edge of ctx.edges) {
			if (edge.to === current && !seen.has(edge.from)) {
				seen.add(edge.from);
				stack.push(edge.from);
			}
		}
	}
	return seen;
}

export function installWindow(search) {
	const store = new Map();
	globalThis.window = {
		location: { search, href: `http://127.0.0.1:9427/performance${search}` },
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		},
		addEventListener() {},
		removeEventListener() {},
		setTimeout,
		clearTimeout,
		setInterval,
		clearInterval
	};
}
