/**
 * Remote speaker (I18): the WebRTC runtime, one link out and one link in.
 *
 * SENDER: MAIN (the master bus) is tapped into a MediaStreamAudioDestinationNode
 * and sent as one Opus stereo track. The offer goes to THIS machine's engine,
 * which relays it to the speaker's LAN listener and hands back the answer.
 * RECEIVER: the offer arrives from this machine's engine; the remote track
 * plays on an <audio> element routed to the selected MAIN output.
 *
 * LAN only: `iceServers: []`, so only host candidates are gathered and no
 * STUN/TURN server or cloud hop is ever contacted. Candidates are gathered in
 * full before the description is sent (no trickle), because the signaling is
 * one request each way.
 *
 * Dependencies are passed in (engine base URL, fetch, the master node) so the
 * two-Chromium loopback proof (tests/live/remote-speaker-loopback.mjs) runs
 * this exact module.
 */
import {
	ICE_GATHER_TIMEOUT_MS,
	REMOTE_SPEAKER_MAX_BITRATE_BPS,
	iceGatheringVerdict,
	REMOTE_SPEAKER_ROUTES,
	peerReportState,
	speakerSinkId,
	withOpusMusicParams
} from './remote-speaker-core';
import { closeWithPerformanceRoute } from './route-owned-links';

/** From the answer to a connected DTLS/SRTP transport. */
export const CONNECT_TIMEOUT_MS = 10_000;

export interface EngineLink {
	/** This machine's engine origin ('' for same-origin). */
	apiBase: string;
	fetch: typeof fetch;
}

/** No peer reachable over a host candidate, and no server to fall back on. */
const LAN_ONLY: RTCConfiguration = { iceServers: [] };

function _requireWebRtc(): void {
	if (typeof RTCPeerConnection === 'undefined') {
		throw new Error('this browser has no WebRTC (RTCPeerConnection), so it cannot be or send to a remote speaker');
	}
}

async function _json(response: Response): Promise<Record<string, unknown> | null> {
	try {
		const body: unknown = await response.json();
		return body !== null && typeof body === 'object' ? (body as Record<string, unknown>) : null;
	} catch {
		return null;
	}
}

/**
 * Resolve once gathering is finished enough to send (see iceGatheringVerdict).
 * Must be armed BEFORE setLocalDescription, which is when candidates start.
 * Polling stops as soon as the connection is closed, and the returned promise
 * is already observed: when setLocalDescription throws first and the caller
 * never awaits it, it neither keeps polling nor rejects unhandled.
 */
function _iceGathering(pc: RTCPeerConnection): Promise<void> {
	const startedAt = performance.now();
	let candidates = 0;
	let lastAt: number | null = null;
	const onCandidate = (event: RTCPeerConnectionIceEvent): void => {
		if (event.candidate === null || event.candidate.candidate === '') return;
		candidates += 1;
		lastAt = performance.now();
	};
	pc.addEventListener('icecandidate', onCandidate);
	const gathering = new Promise<void>((resolve, reject) => {
		const check = (): void => {
			if (pc.signalingState === 'closed') {
				pc.removeEventListener('icecandidate', onCandidate);
				reject(new Error('the WebRTC connection closed while gathering ICE candidates'));
				return;
			}
			const now = performance.now();
			const verdict = iceGatheringVerdict({
				state: pc.iceGatheringState,
				candidates,
				msSinceStart: now - startedAt,
				msSinceLastCandidate: lastAt === null ? null : now - lastAt
			});
			if (verdict === 'wait') {
				setTimeout(check, 50);
				return;
			}
			pc.removeEventListener('icecandidate', onCandidate);
			if (verdict === 'done') resolve();
			else reject(new Error(`no LAN (host) ICE candidate was gathered within ${ICE_GATHER_TIMEOUT_MS} ms, so no speaker could reach this machine`));
		};
		setTimeout(check, 50);
	});
	gathering.catch(() => undefined);
	return gathering;
}

function _localSdp(pc: RTCPeerConnection): string {
	const sdp = pc.localDescription?.sdp;
	if (sdp === undefined || sdp === '') throw new Error('the WebRTC connection has no local description');
	if (!/^a=candidate:/m.test(sdp)) {
		throw new Error('no LAN (host) ICE candidate was gathered, so no speaker could reach this machine');
	}
	return sdp;
}

function _connected(pc: RTCPeerConnection): Promise<void> {
	if (pc.connectionState === 'connected') return Promise.resolve();
	return new Promise<void>((resolve, reject) => {
		const timer = setTimeout(() => {
			pc.removeEventListener('connectionstatechange', check);
			reject(new Error(`the speaker answered but the link did not connect within ${CONNECT_TIMEOUT_MS} ms (${pc.connectionState})`));
		}, CONNECT_TIMEOUT_MS);
		function check(): void {
			if (pc.connectionState === 'connected') {
				clearTimeout(timer);
				pc.removeEventListener('connectionstatechange', check);
				resolve();
			} else if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
				clearTimeout(timer);
				pc.removeEventListener('connectionstatechange', check);
				reject(new Error(`the link ${pc.connectionState} before it connected`));
			}
		}
		pc.addEventListener('connectionstatechange', check);
	});
}

/** Send one report to this machine's engine; never fatal to the link. */
function _report(
	engine: EngineLink,
	route: '/link/report' | '/speaker/report',
	linkId: string,
	state: string,
	error: string | null = null
): void {
	void engine
		.fetch(`${engine.apiBase}${REMOTE_SPEAKER_ROUTES}${route}`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			// The id lets the engine ignore a report from a replaced link.
			body: JSON.stringify(error === null ? { state, link_id: linkId } : { state, link_id: linkId, error })
		})
		.then((response) => {
			if (!response.ok) console.error(`remote speaker: ${route} answered ${response.status}`);
		})
		.catch((error: unknown) => console.error(`remote speaker: ${route} failed: ${String(error)}`));
}

/** Tell this machine's engine what the link is doing; never fatal to the link. */
function _reportStates(
	pc: RTCPeerConnection,
	engine: EngineLink,
	route: '/link/report' | '/speaker/report',
	linkId: string
): void {
	pc.addEventListener('connectionstatechange', () => {
		const state = peerReportState(pc.connectionState);
		if (state !== null) _report(engine, route, linkId, state);
	});
}

// ------------------------------------------------------------------ sender --

interface SenderLink {
	pc: RTCPeerConnection;
	source: AudioNode;
	destination: MediaStreamAudioDestinationNode;
	address: string;
	linkId: string;
	engine: EngineLink;
	/** Reported to the engine as linked, so closing it must be reported too. */
	reported: boolean;
}

let _sender: SenderLink | null = null;

export interface LinkOptions {
	address: string;
	code: string;
	/** This attempt's id from the engine; the offer and every report carry it. */
	linkId: string;
	/** The context MAIN runs in and the node that carries MAIN. */
	context: AudioContext;
	source: AudioNode;
	engine: EngineLink;
}

/** Link MAIN to the speaker at `address`. Resolves once the link is connected. */
export async function linkToSpeaker(options: LinkOptions): Promise<void> {
	_requireWebRtc();
	unlinkFromSpeaker();
	const { context, source, engine, address, code, linkId } = options;
	if (context.state !== 'running') await context.resume();
	if (context.state !== 'running') {
		throw new Error(`the audio engine will not run (${context.state}), so there is no MAIN to send`);
	}
	const pc = new RTCPeerConnection(LAN_ONLY);
	const destination = context.createMediaStreamDestination();
	destination.channelCount = 2;
	destination.channelCountMode = 'explicit';
	source.connect(destination);
	const link: SenderLink = { pc, source, destination, address, linkId, engine, reported: false };
	_sender = link;
	try {
		const [track] = destination.stream.getAudioTracks();
		if (track === undefined) throw new Error('the MAIN tap produced no audio track');
		track.contentHint = 'music';
		const rtpSender = pc.addTrack(track, destination.stream);
		const offer = await pc.createOffer();
		const gathered = _iceGathering(pc);
		await pc.setLocalDescription({ type: 'offer', sdp: withOpusMusicParams(offer.sdp ?? '') });
		await gathered;
		const response = await engine.fetch(`${engine.apiBase}${REMOTE_SPEAKER_ROUTES}/link/offer`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ address, code, link_id: linkId, sdp: _localSdp(pc) })
		});
		const body = await _json(response);
		if (!response.ok) {
			throw new Error(String(body?.detail ?? `the engine refused the offer (${response.status})`));
		}
		if (typeof body?.sdp !== 'string') throw new Error('the engine relayed no answer from the speaker');
		await pc.setRemoteDescription({ type: 'answer', sdp: body.sdp });
		const parameters = rtpSender.getParameters();
		if (parameters.encodings.length === 0) parameters.encodings = [{}];
		parameters.encodings[0].maxBitrate = REMOTE_SPEAKER_MAX_BITRATE_BPS;
		await rtpSender.setParameters(parameters);
		await _connected(pc);
		_reportStates(pc, engine, '/link/report', linkId);
		link.reported = true;
	} catch (error) {
		if (_sender === link) unlinkFromSpeaker();
		throw error;
	}
}

/** Close the sending link and release the MAIN tap. Idempotent. */
export function unlinkFromSpeaker(): void {
	const link = _sender;
	_sender = null;
	if (link === null) return;
	try {
		link.source.disconnect(link.destination);
	} catch {
		// Already disconnected (the engine rebuilt its graph): nothing to release.
	}
	for (const track of link.destination.stream.getTracks()) track.stop();
	link.pc.close();
}

/** The sending link's WebRTC stats (codec, bytes sent), or null with no link. */
export async function remoteSpeakerSenderStats(): Promise<RTCStatsReport | null> {
	return _sender === null ? null : _sender.pc.getStats();
}

// ---------------------------------------------------------------- receiver --

interface ReceiverLink {
	pc: RTCPeerConnection;
	linkId: string;
	engine: EngineLink;
	audio: HTMLAudioElement;
	stream: MediaStream | null;
	meter: { context: AudioContext; analyser: AnalyserNode; samples: Float32Array<ArrayBuffer> } | null;
	playError: string | null;
}

/** The engine session this page answers for; null when not a speaker. */
let _epoch: string | null = null;
/** The engine that enabled `_epoch`, told to close its listener on route teardown. */
let _speakerEngine: EngineLink | null = null;
let _receiver: ReceiverLink | null = null;

/**
 * Agree to be a speaker for session `epoch`. The engine opens its listener
 * only after this. `engine` is the engine that asked; route teardown tells it
 * to stop this session, so its listener does not outlive the page.
 */
export function enableSpeaker(epoch: string, engine: EngineLink | null = null): void {
	_requireWebRtc();
	if (epoch === '') throw new TypeError('enableSpeaker needs the session epoch');
	_closeReceiver();
	_epoch = epoch;
	_speakerEngine = engine;
}

/** Stop being a speaker and drop any link. Idempotent; an answer still in flight is dropped. */
export function disableSpeaker(): void {
	_epoch = null;
	_speakerEngine = null;
	_closeReceiver();
}

/** The engine gave up on link `linkId` (timeout, failure, stop): drop it if it is ours. */
export function cancelSpeakerAnswer(linkId: string): void {
	if (_receiver !== null && _receiver.linkId === linkId) _closeReceiver();
}

function _closeReceiver(): void {
	const link = _receiver;
	_receiver = null;
	if (link === null) return;
	link.audio.pause();
	link.audio.srcObject = null;
	void link.meter?.context.close();
	link.pc.close();
}

export interface AnswerOptions {
	sdp: string;
	/** The engine session the offer arrived in; must be the one this page enabled. */
	epoch: string;
	linkId: string;
	/** The MAIN output picked on this machine, or null for the system default. */
	sinkId: string | null;
	engine: EngineLink;
}

/** Answer a sender's offer; MAIN from the sender then plays on `sinkId`. */
export async function answerSpeakerOffer(options: AnswerOptions): Promise<string> {
	if (_epoch === null) throw new Error('this page is not being a speaker; POST /speaker/start first');
	if (options.epoch !== _epoch) throw new Error('this offer belongs to a speaker session that has ended');
	_requireWebRtc();
	_closeReceiver();
	const pc = new RTCPeerConnection(LAN_ONLY);
	const audio = new Audio();
	audio.autoplay = true;
	const link: ReceiverLink = {
		pc,
		linkId: options.linkId,
		engine: options.engine,
		audio,
		stream: null,
		meter: null,
		playError: null
	};
	_receiver = link;
	try {
		if (options.sinkId !== null) {
			if (typeof audio.setSinkId !== 'function') {
				throw new Error('this browser cannot route the speaker to the selected MAIN output (no setSinkId)');
			}
			await audio.setSinkId(speakerSinkId(options.sinkId));
		}
		pc.addEventListener('track', (event) => {
			// One meter per link: a second track, or one after the link closed,
			// must not open another AudioContext that nothing closes.
			if (_receiver !== link || link.meter !== null) return;
			const stream = event.streams[0] ?? new MediaStream([event.track]);
			link.stream = stream;
			audio.srcObject = stream;
			audio.play().catch((error: unknown) => {
				// A silent speaker is a failed link, never a linked one: tell the
				// engine why and drop the peer, so both ends stop reading linked.
				link.playError = `the speaker cannot start playback (${String(error)}); click Be a speaker on this machine to allow audio`;
				console.error(`remote speaker: ${link.playError}`);
				if (_receiver !== link) return;
				_report(link.engine, '/speaker/report', link.linkId, 'failed', link.playError);
				_closeReceiver();
			});
			// A level reading for the panel and the loopback proof: one analyser
			// per link, read on demand, never per frame.
			const context = new AudioContext();
			const analyser = context.createAnalyser();
			analyser.fftSize = 2048;
			context.createMediaStreamSource(stream).connect(analyser);
			link.meter = { context, analyser, samples: new Float32Array(analyser.fftSize) };
		});
		await pc.setRemoteDescription({ type: 'offer', sdp: options.sdp });
		const answer = await pc.createAnswer();
		const gathered = _iceGathering(pc);
		await pc.setLocalDescription({ type: 'answer', sdp: withOpusMusicParams(answer.sdp ?? '') });
		await gathered;
		if (link.playError !== null) throw new Error(link.playError);
		// Stopped, cancelled or replaced while answering: answer nothing.
		if (_receiver !== link || options.epoch !== _epoch) {
			throw new Error('the speaker session ended while this offer was being answered');
		}
		_reportStates(pc, options.engine, '/speaker/report', options.linkId);
		return _localSdp(pc);
	} catch (error) {
		if (_receiver === link) _closeReceiver();
		// A refused playback closes the peer mid-answer; its reason is the real one.
		throw link.playError !== null ? new Error(link.playError) : error;
	}
}

/**
 * The received MAIN's RMS (0..1, all channels mixed) right now, or null with
 * the reason there is no reading. A measurement, never a stand-in value.
 */
export function remoteSpeakerReceiverLevel(): { rms: number | null; reason: string | null } {
	const link = _receiver;
	if (link === null) return { rms: null, reason: 'no link' };
	if (link.playError !== null) return { rms: null, reason: link.playError };
	if (link.meter === null) return { rms: null, reason: 'no track received yet' };
	if (link.meter.context.state !== 'running') {
		return { rms: null, reason: `the level meter is ${link.meter.context.state}` };
	}
	const { analyser, samples } = link.meter;
	analyser.getFloatTimeDomainData(samples);
	let sum = 0;
	for (const sample of samples) sum += sample * sample;
	return { rms: Math.sqrt(sum / samples.length), reason: null };
}

/** The received MAIN as a stream (for a second meter), or null with no link. */
export function remoteSpeakerReceiverStream(): MediaStream | null {
	return _receiver?.stream ?? null;
}

/**
 * Route teardown: close both links and stop being a speaker, so nothing plays
 * off /performance and the engine stops reading either link as linked. Run by
 * the /performance route's teardown, before the audio engine (and the MAIN it
 * taps) is disposed. Idempotent.
 */
export function closeRemoteSpeakerLinks(): void {
	const sender = _sender;
	if (sender?.reported === true) _report(sender.engine, '/link/report', sender.linkId, 'closed');
	unlinkFromSpeaker();
	const receiver = _receiver;
	if (receiver !== null) _report(receiver.engine, '/speaker/report', receiver.linkId, 'closed');
	const epoch = _epoch;
	const engine = _speakerEngine;
	disableSpeaker();
	// The engine's session and LAN listener would otherwise outlive the page:
	// back on /performance it would read listening while this page refuses
	// every offer, and a new start would answer 409. The epoch stops only the
	// session this page enabled.
	if (epoch !== null && engine !== null) _stopSpeakerSession(engine, epoch);
}

/** Ask the engine to close speaker session `epoch`; never fatal to the teardown. */
function _stopSpeakerSession(engine: EngineLink, epoch: string): void {
	void engine
		.fetch(`${engine.apiBase}${REMOTE_SPEAKER_ROUTES}/speaker/stop`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ epoch }),
			// The page may be unloading, not just leaving the route.
			keepalive: true
		})
		.then((response) => {
			if (!response.ok) console.error(`remote speaker: /speaker/stop answered ${response.status}`);
		})
		.catch((error: unknown) => console.error(`remote speaker: /speaker/stop failed: ${String(error)}`));
}

closeWithPerformanceRoute(closeRemoteSpeakerLinks);
