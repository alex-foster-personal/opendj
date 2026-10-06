/** "Master mix (internal)" set recording, the page half (SET-12).
 *
 * Taps the master bus (`masterMixTapPoint`: post master fader, never the cue)
 * with an AudioWorklet, and posts its int16 chunks in order to
 * `POST /api/sets/recorder/{id}/master-pcm`, where the daemon writes the WAV.
 * An AudioWorklet and fetch work the same in Chrome and the desktop app's
 * WKWebView; MediaRecorder was not used because it cannot produce lossless
 * PCM and its codecs differ between the two (docs/decisions, ADR-NEW
 * set-recording-master-mix-tap).
 *
 * Loaded lazily (by the REC rail only once a master recording is live), so
 * none of this is in the /performance bundle.
 *
 * Fails loudly, never quietly: a refused or failed POST, a backlog the daemon
 * cannot keep up with, or a context that will not run, stops the tap and
 * reports the reason through `onfailure`; the daemon then marks the capture
 * failed too, after its 5 s stall window. */

import { API_BASE } from '$lib/api/client';
import { masterMixUnavailableReason } from '$lib/sets/record-input-choice';
import { masterMixTapPoint } from '$lib/rb/audio-engine.svelte';
import processorUrl from '$lib/sets/master-capture-processor.js?url';

export const CHUNK_SECONDS = 0.5;
/** Ten seconds of audio waiting on the daemon is a backlog, not a hiccup. */
export const MAX_QUEUED_CHUNKS = 20;
const PROCESSOR_NAME = 'mdt-master-capture';
const FLUSH_TIMEOUT_MS = 2000;
const RETAP_CHECK_MS = 1000;

export interface MasterMixCapture {
	/** Flush the last partial chunk, wait for every POST, detach. */
	stop(): Promise<void>;
}

export function chunkFramesFor(sampleRate: number): number {
	return Math.round(sampleRate * CHUNK_SECONDS);
}

export function masterPcmUrl(
	base: string,
	sessionId: string,
	stream: string,
	seq: number,
	sampleRate: number
): string {
	const query = new URLSearchParams({ stream, seq: String(seq), sample_rate: String(sampleRate) });
	return `${base}/api/sets/recorder/${encodeURIComponent(sessionId)}/master-pcm?${query}`;
}

interface Chunk {
	pcm: ArrayBuffer;
	frames: number;
	stream: string;
	seq: number;
	sampleRate: number;
	port: MessagePort;
}

interface Tap {
	node: GainNode;
	worklet: AudioWorkletNode;
	sink: GainNode;
	stream: string;
	sampleRate: number;
	seq: number;
}

const loadedContexts = new WeakSet<BaseAudioContext>();

/** Start streaming the master mix into recording `sessionId`. Rejects with
 * the reason when the tap cannot start; nothing is left attached then. */
export async function startMasterMixCapture(
	sessionId: string,
	onfailure: (reason: string) => void,
	post: typeof fetch = fetch
): Promise<MasterMixCapture> {
	const unavailable = masterMixUnavailableReason();
	if (unavailable !== null) throw new Error(unavailable);

	const queue: Chunk[] = [];
	let tap: Tap | null = null;
	let sending: Promise<void> | null = null;
	let failed: string | null = null;
	let stopped = false;
	let retapping = false;

	function detach(): void {
		if (tap === null) return;
		tap.worklet.port.onmessage = null;
		tap.node.disconnect(tap.worklet);
		tap.worklet.disconnect();
		tap.sink.disconnect();
		tap = null;
	}

	function fail(reason: string): void {
		if (failed !== null || stopped) return;
		failed = reason;
		clearInterval(watch);
		detach();
		queue.length = 0;
		onfailure(reason);
	}

	async function pump(): Promise<void> {
		while (queue.length > 0 && failed === null) {
			const chunk = queue[0];
			let response: Response;
			try {
				response = await post(
					masterPcmUrl(API_BASE, sessionId, chunk.stream, chunk.seq, chunk.sampleRate),
					{
						method: 'POST',
						headers: { 'content-type': 'application/octet-stream' },
						body: new Uint8Array(chunk.pcm, 0, chunk.frames * 4)
					}
				);
			} catch (error) {
				fail(`the master mix could not reach the recorder: ${String(error)}`);
				return;
			}
			if (!response.ok) {
				const detail = await response.text().catch(() => '');
				fail(`the recorder refused master-mix audio (${response.status}): ${detail}`);
				return;
			}
			queue.shift();
			// Back to its worklet's pool, so steady state allocates nothing.
			chunk.port.postMessage({ reuse: chunk.pcm }, [chunk.pcm]);
		}
	}

	function kick(): void {
		sending ??= pump().finally(() => {
			sending = null;
			if (queue.length > 0 && failed === null) kick();
		});
	}

	async function attach(build: boolean): Promise<void> {
		const point = masterMixTapPoint(build);
		if (point === null) throw new Error('the audio engine has no master bus to record');
		const { context, node } = point;
		if (context.state !== 'running') await context.resume();
		if (context.state !== 'running') {
			throw new Error(`the audio engine will not run (${context.state}), so the master mix is silent`);
		}
		if (!loadedContexts.has(context)) {
			await context.audioWorklet.addModule(processorUrl);
			loadedContexts.add(context);
		}
		const worklet = new AudioWorkletNode(context, PROCESSOR_NAME, {
			numberOfInputs: 1,
			numberOfOutputs: 1,
			outputChannelCount: [1],
			channelCount: 2,
			channelCountMode: 'explicit',
			channelInterpretation: 'speakers',
			processorOptions: { chunkFrames: chunkFramesFor(context.sampleRate) }
		});
		const sink = context.createGain();
		sink.gain.value = 0;
		worklet.connect(sink).connect(context.destination);
		node.connect(worklet);
		// A new tap follows a gap, so it is a new stream: the daemon starts a
		// new segment for it rather than splicing across the hole.
		const next: Tap = {
			node,
			worklet,
			sink,
			stream: crypto.randomUUID(),
			sampleRate: context.sampleRate,
			seq: 0
		};
		worklet.port.onmessage = (event: MessageEvent<{ pcm?: ArrayBuffer; frames?: number }>) => {
			const { pcm, frames } = event.data;
			if (!(pcm instanceof ArrayBuffer) || typeof frames !== 'number') return;
			queue.push({ pcm, frames, stream: next.stream, seq: next.seq++, sampleRate: next.sampleRate, port: worklet.port });
			if (queue.length > MAX_QUEUED_CHUNKS) {
				fail(`the recorder fell ${queue.length * CHUNK_SECONDS} s behind the master mix`);
				return;
			}
			kick();
		};
		tap = next;
	}

	// The graph is rebuilt on an output-device change or a context failure;
	// follow the master bus to its new context.
	const watch = setInterval(() => {
		if (failed !== null || stopped || retapping) return;
		const current = masterMixTapPoint(false);
		if (current === null || current.node === tap?.node) return;
		retapping = true;
		detach();
		attach(false)
			.catch((error: unknown) => fail(`the master mix tap could not follow the rebuilt audio graph: ${String(error)}`))
			.finally(() => (retapping = false));
	}, RETAP_CHECK_MS);

	try {
		await attach(true);
	} catch (error) {
		clearInterval(watch);
		detach();
		throw error;
	}

	return {
		async stop(): Promise<void> {
			clearInterval(watch);
			const last = tap;
			if (last !== null && failed === null) {
				await new Promise<void>((resolve) => {
					const timer = setTimeout(resolve, FLUSH_TIMEOUT_MS);
					last.worklet.port.addEventListener('message', (event: MessageEvent<{ flushed?: boolean }>) => {
						if (event.data?.flushed) {
							clearTimeout(timer);
							resolve();
						}
					});
					last.worklet.port.start();
					last.worklet.port.postMessage({ flush: true });
				});
			}
			while (sending !== null) await sending;
			stopped = true;
			detach();
		}
	};
}
