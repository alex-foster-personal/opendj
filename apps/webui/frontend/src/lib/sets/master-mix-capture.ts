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

/** What one tap sent, for checking the WAV against it exactly (SET-12):
 *  the daemon's `master_mix_closed` timeline event must accept the same
 *  frames, and the WAV segments must hold them, with nothing padded or
 *  repeated. Also left on `globalThis.__mdtMasterMix` for agents. */
export interface MasterMixStats {
	frames_sent: number;
	chunks_sent: number;
	tap_started_ms: number;
	tap_stopped_ms: number | null;
}

export interface MasterMixCapture {
	/** Flush the last partial chunk, wait for every POST, detach. */
	stop(): Promise<MasterMixStats>;
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
	post: typeof fetch = fetch,
	onended: () => void = () => {},
	signal?: AbortSignal
): Promise<MasterMixCapture> {
	const unavailable = masterMixUnavailableReason();
	if (unavailable !== null) throw new Error(unavailable);

	const queue: Chunk[] = [];
	const stats: MasterMixStats = { frames_sent: 0, chunks_sent: 0, tap_started_ms: 0, tap_stopped_ms: null };
	(globalThis as { __mdtMasterMix?: MasterMixStats }).__mdtMasterMix = stats;
	let tap: Tap | null = null;
	let sending: Promise<void> | null = null;
	let failed: string | null = null;
	let stopped = false;
	let retapping = false;
	// Started only once the first tap is connected: a watch that ran during
	// a slow first attach (tap still null) started a SECOND tap, whose orphaned
	// worklet kept feeding a stopped recording (SET-12, reproduced live).
	let watch: ReturnType<typeof setInterval> | undefined;

	function detach(): void {
		if (tap === null) return;
		tap.worklet.port.onmessage = null;
		tap.node.disconnect(tap.worklet);
		tap.worklet.disconnect();
		tap.sink.disconnect();
		tap = null;
	}

	function end(): void {
		// Always drop what is queued, even when already stopped: a queue left
		// behind is re-posted by the next kick (SET-12, seen live under load).
		queue.length = 0;
		if (stopped) return;
		stopped = true;
		stats.tap_stopped_ms = Date.now();
		clearInterval(watch);
		detach();
		queue.length = 0;
		onended();
	}

	function fail(reason: string): void {
		queue.length = 0;
		if (failed !== null || stopped) return;
		failed = reason;
		clearInterval(watch);
		detach();
		queue.length = 0;
		onfailure(reason);
		onended();
	}

	async function pump(): Promise<void> {
		while (queue.length > 0 && failed === null && !stopped) {
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
			if (response.status === 200) {
				// The recording was stopped cleanly while this chunk was in
				// flight (an agent or another tab stopped it): the daemon says
				// so with a 200 and drops it. Expected, so no toast (SET-12).
				const body = (await response.json().catch(() => null)) as { dropped?: string } | null;
				if (body?.dropped === 'recording_stopped') {
					console.debug(`master mix: recording ${sessionId} stopped; tap ends`);
					end();
					return;
				}
				fail(`the recorder answered 200 to master-mix audio without saying it was dropped`);
				return;
			}
			if (!response.ok) {
				const detail = await response.text().catch(() => '');
				fail(`the recorder refused master-mix audio (${response.status}): ${detail}`);
				return;
			}
			queue.shift();
			stats.frames_sent += chunk.frames;
			stats.chunks_sent += 1;
			// Back to its worklet's pool, so steady state allocates nothing.
			chunk.port.postMessage({ reuse: chunk.pcm }, [chunk.pcm]);
		}
	}

	function kick(): void {
		sending ??= pump().finally(() => {
			sending = null;
			if (queue.length > 0 && failed === null && !stopped) kick();
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
			if (stopped || failed !== null || tap !== next) {
				// A tap that is no longer this capture's live one never posts.
				worklet.port.onmessage = null;
				node.disconnect(worklet);
				worklet.disconnect();
				return;
			}
			queue.push({ pcm, frames, stream: next.stream, seq: next.seq++, sampleRate: next.sampleRate, port: worklet.port });
			if (queue.length > MAX_QUEUED_CHUNKS) {
				fail(`the recorder fell ${queue.length * CHUNK_SECONDS} s behind the master mix`);
				return;
			}
			kick();
		};
		if (stopped || failed !== null || signal?.aborted) {
			// Cancelled (or ended) while the module loaded: never go live.
			worklet.port.onmessage = null;
			node.disconnect(worklet);
			worklet.disconnect();
			sink.disconnect();
			if (signal?.aborted) throw new MasterMixCancelled(sessionId);
			throw new Error('the master tap ended before it connected');
		}
		tap = next;
	}

	// The graph is rebuilt on an output-device change or a context failure;
	// follow the master bus to its new context.
	function watchForRebuilds(): void {
		watch = setInterval(() => {
			if (failed !== null || stopped || retapping || tap === null) return;
			const current = masterMixTapPoint(false);
			if (current === null || current.node === tap?.node) return;
			retapping = true;
			detach();
			attach(false)
				.catch((error: unknown) => fail(`the master mix tap could not follow the rebuilt audio graph: ${String(error)}`))
				.finally(() => (retapping = false));
		}, RETAP_CHECK_MS);
	}

	try {
		if (signal?.aborted) throw new MasterMixCancelled(sessionId);
		signal?.addEventListener('abort', () => end(), { once: true });
		await attach(true);
		watchForRebuilds();
		stats.tap_started_ms = Date.now();
	} catch (error) {
		clearInterval(watch);
		detach();
		throw error;
	}

	return {
		async stop(): Promise<MasterMixStats> {
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
			stats.tap_stopped_ms = Date.now();
			detach();
			return { ...stats };
		}
	};
}

/** The daemon withdrew this recording's start (SET-12): its 503 already told
 *  whoever started it, so a tap that ends this way is not a new failure. */
export class MasterMixCancelled extends Error {
	constructor(sessionId: string) {
		super(`recording ${sessionId} was cancelled by the recorder before its master tap connected`);
		this.name = 'MasterMixCancelled';
	}
}

// ------------------------------------------------------- one tap per page ---

type Running = {
	sessionId: string;
	capture: Promise<MasterMixCapture>;
	notify: (why: string) => void;
	abort: AbortController;
};

let current: Running | null = null;
/** Recordings the daemon cancelled (its start timed out): a tap order that
 *  arrives late for one of these never starts. Bounded, newest last. */
const cancelled: string[] = [];

/** The page's one master tap for `sessionId`: started on first ask, the same
 *  tap on every later ask (the start route's pushed order and the REC rail
 *  both ask, SET-12). `notify` reports a tap that fails mid-recording; the
 *  REC rail passes its toast, an agent order has none (the rail's status read
 *  still toasts the daemon's failed capture). */
export function ensureMasterMixCapture(
	sessionId: string,
	notify?: (why: string) => void
): Promise<MasterMixCapture> {
	if (cancelled.includes(sessionId)) {
		return Promise.reject(new MasterMixCancelled(sessionId));
	}
	if (current?.sessionId === sessionId) {
		if (notify) current.notify = notify;
		return current.capture;
	}
	if (current !== null) void stopMasterMixCapture();
	const abort = new AbortController();
	const entry = {
		sessionId,
		abort,
		notify: notify ?? ((why: string) => console.error(`master mix: ${why}`))
	};
	const capture = startMasterMixCapture(
		sessionId,
		(why) => entry.notify(why),
		fetch,
		() => {
			if (current?.capture === capture) current = null;
		},
		abort.signal
	);
	current = { ...entry, capture };
	capture.catch(() => {
		if (current?.capture === capture) current = null;
	});
	return capture;
}

/** Stop the page's tap, flushing its last chunk; null when none is running. */
export async function stopMasterMixCapture(): Promise<MasterMixStats | null> {
	const running = current;
	current = null;
	if (running === null) return null;
	const capture = await running.capture.catch(() => null);
	return capture === null ? null : capture.stop();
}

/** The daemon cancelled `sessionId` (its start gave up on the tap): tear the
 *  tap down at once, even mid-attach, with no flush and no further POST, and
 *  refuse a tap order for it that arrives later (SET-12). */
export function cancelMasterMixCapture(sessionId: string): void {
	cancelled.push(sessionId);
	if (cancelled.length > 16) cancelled.shift();
	if (current?.sessionId !== sessionId) return;
	current.abort.abort();
	current = null;
}
