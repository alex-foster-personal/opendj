/**
 * AUDIO-DEVICE-04 browser runtime: tap the master bus and a wired or USB mic,
 * sample both loudness envelopes every 20 ms, judge each 2 s window while a
 * deck plays, post the verdict to the engine (where it folds into
 * output-health) and report a stall on the not-delivering edge.
 *
 * Opens ONLY a mic the shell lists on an allowed transport, by exact
 * deviceId: never the default input, which on a Bluetooth headset would flip
 * it to HFP. Survives a room graph recreate by re-tapping the new context.
 */
import { RB_API_BASE } from './api-rb';
import { DECK_IDS, deckStates, masterMixTapPoint } from './audio-engine.svelte';
import {
	ACOUSTIC_SAMPLE_MS,
	ACOUSTIC_WINDOW_FRAMES,
	createAcousticTracker,
	judgeWindow,
	proofMicCandidates,
	type AcousticTrackerState,
	type ProofMic
} from './speaker-acoustic-proof';
import { fetchShellInputDevices, reportSpeakerLinkStall, speakerLink } from './speaker-link.svelte';

/** Mics the proof may use right now. Names withheld (no mic grant yet) yields none. */
export async function listProofMics(): Promise<ProofMic[]> {
	if (typeof navigator === 'undefined' || navigator.mediaDevices === undefined) {
		throw new Error('this browser has no navigator.mediaDevices; the acoustic proof cannot run');
	}
	const [inputs, devices] = await Promise.all([fetchShellInputDevices(), navigator.mediaDevices.enumerateDevices()]);
	const mics = devices
		.filter((device) => device.kind === 'audioinput')
		.map((device) => ({ deviceId: device.deviceId, label: device.label }));
	return proofMicCandidates(inputs, mics);
}

interface Tap {
	context: AudioContext;
	master: AnalyserNode;
	mic: AnalyserNode;
	detach: () => void;
}

function rms(analyser: AnalyserNode, scratch: Float32Array<ArrayBuffer>): number {
	analyser.getFloatTimeDomainData(scratch);
	let sum = 0;
	for (const sample of scratch) sum += sample * sample;
	return Math.sqrt(sum / scratch.length);
}

function attachTap(stream: MediaStream): Tap {
	const point = masterMixTapPoint(false);
	if (point === null) throw new Error('no room audio graph to tap; load a deck first');
	const { context, node } = point;
	const master = context.createAnalyser();
	const mic = context.createAnalyser();
	master.fftSize = 1024;
	mic.fftSize = 1024;
	const source = context.createMediaStreamSource(stream);
	node.connect(master);
	source.connect(mic);
	return {
		context,
		master,
		mic,
		detach: () => {
			node.disconnect(master);
			source.disconnect();
		}
	};
}

async function postProof(state: AcousticTrackerState, mic: ProofMic): Promise<void> {
	const r = await fetch(`${RB_API_BASE}/api/v1/audio/acoustic-proof`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({
			verdict: state.verdict,
			failed_windows: state.failed_windows,
			windows_required: state.windows_required,
			peak_correlation: state.peak_correlation,
			mic_label: mic.label,
			mic_transport: mic.transport
		})
	});
	if (!r.ok) throw new Error(`POST /api/v1/audio/acoustic-proof failed: HTTP ${r.status} ${await r.text()}`);
}

let _stop: (() => void) | null = null;

export function acousticProofRunning(): boolean {
	return _stop !== null;
}

export function stopAcousticProof(): void {
	_stop?.();
	_stop = null;
	speakerLink.acoustic = null;
}

/** Start listening on `mic` (one of `listProofMics()`). Throws on any refusal. */
export async function startAcousticProof(mic: ProofMic): Promise<void> {
	stopAcousticProof();
	const allowed = await listProofMics();
	if (!allowed.some((candidate) => candidate.deviceId === mic.deviceId)) {
		throw new Error(`${mic.label} is not a wired or USB input the shell lists; refusing to open it`);
	}
	const stream = await navigator.mediaDevices.getUserMedia({
		audio: {
			deviceId: { exact: mic.deviceId },
			echoCancellation: false,
			noiseSuppression: false,
			autoGainControl: false
		}
	});
	const tracker = createAcousticTracker();
	const scratch = new Float32Array(1024);
	let tap = attachTap(stream);
	let master: number[] = [];
	let heard: number[] = [];
	let playedThroughout = true;
	speakerLink.acoustic_error = null;

	const tick = (): void => {
		const point = masterMixTapPoint(false);
		if (point === null) return;
		if (point.context !== tap.context) {
			// The room graph was recreated (a reconnect, a stall rebuild): re-tap.
			tap.detach();
			tap = attachTap(stream);
			master = [];
			heard = [];
			playedThroughout = true;
			return;
		}
		playedThroughout &&= DECK_IDS.some((deck) => deckStates[deck].playing);
		master.push(rms(tap.master, scratch));
		heard.push(rms(tap.mic, scratch));
		if (master.length < ACOUSTIC_WINDOW_FRAMES) return;
		const state = tracker.push(judgeWindow({ master, mic: heard, deckPlayedThroughout: playedThroughout }));
		master = [];
		heard = [];
		playedThroughout = true;
		speakerLink.acoustic = { ...state, mic_label: mic.label };
		postProof(state, mic).catch((error: unknown) => {
			speakerLink.acoustic_error = error instanceof Error ? error.message : String(error);
		});
		if (state.newly_not_delivering) reportSpeakerLinkStall('acoustic');
	};
	const handle = setInterval(tick, ACOUSTIC_SAMPLE_MS);
	_stop = () => {
		clearInterval(handle);
		tap.detach();
		for (const track of stream.getTracks()) track.stop();
	};
}
