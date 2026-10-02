/**
 * The cue bridge: an AudioWorklet sender on the main context and a receiver on
 * the cue context, joined by a SharedArrayBuffer ring when the page is
 * cross-origin isolated and by a MessageChannel otherwise. Split from
 * headphones.ts and imported on demand from _ensureCueBridge, so this module and
 * the worklet file it names are fetched only when a cue output is used.
 */

import { CUE_BRIDGE_RING_CAPACITY } from '$lib/player/cue-bridge-ring';
import cueBridgeProcessorUrl from '$lib/player/cue-bridge-processor.js?url';

/** Wired cue-bridge worklet pair for the main and cue AudioContexts. */
export interface CueBridgeWireResult {
	bridgeSender: AudioWorkletNode;
	bridgeReceiver: AudioWorkletNode;
	bridgeControl: Int32Array | null;
}

/** What the bridge reports back. Both are required: a caller that could omit
 * `onProcessorError` could wire a bridge that dies into silent output unseen. */
export interface CueBridgeHandlers {
	onUnderrun: () => void;
	/** A worklet threw; its node now outputs silence for good. */
	onProcessorError: (side: 'sender' | 'receiver') => void;
}

/** Load the bridge module and construct sender/receiver nodes (MessageChannel or SAB). */
export async function wireCueBridgeNodes(
	mainContext: AudioContext,
	cueContext: AudioContext,
	handlers: CueBridgeHandlers
): Promise<CueBridgeWireResult> {
	if (typeof AudioWorkletNode === 'undefined' || typeof mainContext.audioWorklet?.addModule !== 'function') {
		throw new Error('AudioWorkletNode is unavailable');
	}
	await mainContext.audioWorklet.addModule(cueBridgeProcessorUrl);
	await cueContext.audioWorklet.addModule(cueBridgeProcessorUrl);
	const useSab = globalThis.crossOriginIsolated === true;
	let senderOptions: Record<string, unknown>;
	let receiverOptions: Record<string, unknown>;
	let bridgeControl: Int32Array | null = null;
	let connectChannel: MessageChannel | null = null;
	if (useSab) {
		const sab = new SharedArrayBuffer(16 + CUE_BRIDGE_RING_CAPACITY * 2 * 4);
		bridgeControl = new Int32Array(sab, 0, 4);
		bridgeControl[0] = 0;
		bridgeControl[1] = 0;
		bridgeControl[2] = 0;
		bridgeControl[3] = 0;
		senderOptions = { mode: 'sab', controlBuffer: sab };
		receiverOptions = { mode: 'sab', controlBuffer: sab };
	} else {
		connectChannel = new MessageChannel();
		connectChannel.port1.start();
		connectChannel.port2.start();
		senderOptions = { mode: 'port' };
		receiverOptions = { mode: 'port', capacity: CUE_BRIDGE_RING_CAPACITY };
	}
	const bridgeSender = new AudioWorkletNode(mainContext, 'cue-bridge-sender', {
		numberOfInputs: 1,
		numberOfOutputs: 0,
		channelCount: 2,
		channelCountMode: 'explicit',
		processorOptions: senderOptions
	});
	const bridgeReceiver = new AudioWorkletNode(cueContext, 'cue-bridge-receiver', {
		numberOfInputs: 0,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		processorOptions: receiverOptions
	});
	bridgeSender.onprocessorerror = () => handlers.onProcessorError('sender');
	bridgeReceiver.onprocessorerror = () => handlers.onProcessorError('receiver');
	if (connectChannel !== null) {
		bridgeSender.port.postMessage({ type: 'connect', port: connectChannel.port1 }, [connectChannel.port1]);
		bridgeReceiver.port.postMessage({ type: 'connect', port: connectChannel.port2 }, [connectChannel.port2]);
		bridgeReceiver.port.onmessage = (event: MessageEvent<{ type?: string }>) => {
			if (event.data?.type === 'underrun') handlers.onUnderrun();
		};
	}
	return { bridgeSender, bridgeReceiver, bridgeControl };
}

/** CUEOUT-22: the sender half alone, for the Mac app's native cue output. The
 * sender runs in port mode and its port is handed to the relay worker
 * (cue-native-sink.ts) in place of a receiver worklet on a pinned cue context. */
export async function wireNativeCueSender(
	mainContext: AudioContext,
	onProcessorError: () => void
): Promise<{ bridgeSender: AudioWorkletNode; relayPort: MessagePort }> {
	if (typeof AudioWorkletNode === 'undefined' || typeof mainContext.audioWorklet?.addModule !== 'function') {
		throw new Error('AudioWorkletNode is unavailable');
	}
	await mainContext.audioWorklet.addModule(cueBridgeProcessorUrl);
	const channel = new MessageChannel();
	const bridgeSender = new AudioWorkletNode(mainContext, 'cue-bridge-sender', {
		numberOfInputs: 1,
		numberOfOutputs: 0,
		channelCount: 2,
		channelCountMode: 'explicit',
		processorOptions: { mode: 'port' }
	});
	bridgeSender.onprocessorerror = onProcessorError;
	bridgeSender.port.postMessage({ type: 'connect', port: channel.port1 }, [channel.port1]);
	return { bridgeSender, relayPort: channel.port2 };
}
