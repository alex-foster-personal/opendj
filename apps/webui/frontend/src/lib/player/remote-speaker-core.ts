/**
 * Remote speaker (I18, REMOTE-SPK-8119..8119h): the pure half.
 *
 * Another Open DJ machine plays this machine's MAIN over WebRTC. This module
 * holds what can be decided without a browser: the build flag, the Opus SDP
 * parameters that make the link sound like music rather than a voice call,
 * the address and code checks, and the status lines the panel shows. The
 * WebRTC runtime is `remote-speaker.ts`; the decision record is
 * (private path).
 */

/**
 * Opus for music: stereo both ways, and a 256 kbit/s ceiling instead of the
 * voice-call default (about 32 kbit/s mono). `stereo` says what this side
 * wants to RECEIVE, `sprop-stereo` what it SENDS, `maxaveragebitrate` the most
 * it accepts; each side writes them into its own description.
 */
export const OPUS_MUSIC_FMTP: Readonly<Record<string, string>> = Object.freeze({
	stereo: '1',
	'sprop-stereo': '1',
	maxaveragebitrate: '256000'
});
export const REMOTE_SPEAKER_MAX_BITRATE_BPS = 256_000;
export const REMOTE_SPEAKER_CODE_DIGITS = 6;
export const REMOTE_SPEAKER_ROUTES = '/api/v1/performance/remote-speaker';

/** Shown on every remote speaker surface: it is a delayed MAIN. */
export const REMOTE_SPEAKER_DELAY_TITLE =
	'Remote speaker (delayed): the speaker plays later than this machine\'s MAIN (WebRTC jitter buffer, inferred about 0.1-0.3 s on Wi-Fi, not measured on a LAN). Cue in your local headphones.';

export const REMOTE_SPEAKER_OFF_MESSAGE =
	'Remote speaker is off in this build (VITE_REMOTE_SPEAKER=off); set MUSIC_DJ_REMOTE_SPEAKER=on to build it in';

/** The RTP payload type Opus is offered on, or null when there is none. */
export function opusPayloadType(sdp: string): string | null {
	const match = /^a=rtpmap:(\d+) opus\/48000\/2\s*$/im.exec(sdp);
	return match === null ? null : match[1];
}

/**
 * `sdp` with the Opus format line carrying {@link OPUS_MUSIC_FMTP}. Existing
 * parameters (`minptime`, `useinbandfec`) are kept; ours win on a clash. A
 * description with no Opus is an error: the link would not be the codec the
 * decision names, so it is refused rather than sent as whatever was offered.
 */
/**
 * The id the speaker's `<audio>.setSinkId` gets for the MAIN output picked on
 * this machine. `enumerateDevices` lists the system output as `"default"`, which
 * Chrome rejects with NotFoundError unless the page holds media permission; the
 * empty string is the spec's id for the default output (as `browserSinkId` in
 * headphones.ts, CUEOUT-27). Kept here so remote-speaker.ts stays free of the
 * headphones module the loopback harness does not load.
 */
export function speakerSinkId(sinkId: string): string {
	return sinkId === 'default' ? '' : sinkId;
}

export function withOpusMusicParams(sdp: string): string {
	const pt = opusPayloadType(sdp);
	if (pt === null) throw new Error('the WebRTC description offers no Opus codec, so it cannot carry MAIN');
	const eol = sdp.includes('\r\n') ? '\r\n' : '\n';
	const trailing = sdp.endsWith(eol);
	const lines = sdp.split(/\r\n|\n/);
	if (trailing) lines.pop();
	const fmtpPrefix = `a=fmtp:${pt} `;
	const merged = (existing: string): string => {
		const params = new Map<string, string>();
		for (const pair of existing.split(';')) {
			const trimmed = pair.trim();
			if (trimmed === '') continue;
			const eq = trimmed.indexOf('=');
			if (eq === -1) params.set(trimmed, '');
			else params.set(trimmed.slice(0, eq), trimmed.slice(eq + 1));
		}
		for (const [key, value] of Object.entries(OPUS_MUSIC_FMTP)) params.set(key, value);
		return [...params].map(([key, value]) => (value === '' ? key : `${key}=${value}`)).join(';');
	};
	let hasFmtp = false;
	const out = lines.map((line) => {
		if (!line.startsWith(fmtpPrefix)) return line;
		hasFmtp = true;
		return fmtpPrefix + merged(line.slice(fmtpPrefix.length));
	});
	if (!hasFmtp) {
		const rtpmap = new RegExp(`^a=rtpmap:${pt} `, 'i');
		const at = out.findIndex((line) => rtpmap.test(line));
		out.splice(at + 1, 0, fmtpPrefix + merged(''));
	}
	return out.join(eol) + (trailing ? eol : '');
}

/** Host candidates arrive in milliseconds; this long with no new one means done. */
export const ICE_SETTLE_MS = 500;
/** The ceiling on gathering; with no candidate by then there is no LAN path. */
export const ICE_GATHER_TIMEOUT_MS = 5_000;

/**
 * Whether candidate gathering is finished enough to send the description.
 *
 * Chrome does not always reach `complete` with no STUN server: measured in a
 * headless Linux sandbox, one host candidate arrived at 5 ms and the state sat
 * at `gathering` for 12 s and more. So a description with at least one host
 * candidate is sent once no new candidate has arrived for ICE_SETTLE_MS;
 * none by the ceiling is a failure, never an empty offer.
 */
export function iceGatheringVerdict(probe: {
	state: string;
	candidates: number;
	msSinceStart: number;
	msSinceLastCandidate: number | null;
}): 'done' | 'wait' | 'fail' {
	if (probe.state === 'complete') return probe.candidates > 0 ? 'done' : 'fail';
	if (probe.candidates > 0 && probe.msSinceLastCandidate !== null && probe.msSinceLastCandidate >= ICE_SETTLE_MS) return 'done';
	if (probe.msSinceStart >= ICE_GATHER_TIMEOUT_MS) return probe.candidates > 0 ? 'done' : 'fail';
	return 'wait';
}

export function isSpeakerCode(value: unknown): value is string {
	return typeof value === 'string' && new RegExp(`^\\d{${REMOTE_SPEAKER_CODE_DIGITS}}$`).test(value);
}

/** `host:port` or `[v6]:port`, port 1..65535: the same rule the engine applies. */
export function isSpeakerAddress(value: unknown): value is string {
	if (typeof value !== 'string') return false;
	const match = /^(?:\[([^\]]+)\]|([^:\s[\]]+)):(\d{1,5})$/.exec(value.trim());
	if (match === null) return false;
	const port = Number(match[3]);
	return port >= 1 && port <= 65535;
}

/** What the page reports about a link: the engine accepts exactly these. */
export type PeerReportState = 'connecting' | 'connected' | 'disconnected' | 'failed' | 'closed';

/** `RTCPeerConnectionState` to the report the engine takes; `new` reports nothing. */
export function peerReportState(state: string): PeerReportState | null {
	switch (state) {
		case 'connecting':
		case 'connected':
		case 'disconnected':
		case 'failed':
		case 'closed':
			return state;
		case 'new':
			return null;
		default:
			throw new TypeError(`unknown RTCPeerConnection state ${state}`);
	}
}

/** GET /speaker, as the engine sends it. */
export interface RemoteSpeakerState {
	status: 'off' | 'listening' | 'answering' | 'linked' | 'locked_out';
	code: string | null;
	address: string | null;
	address_error: string | null;
	port: number | null;
	peer: string | null;
	wrong_codes: number;
	max_wrong_codes: number;
	last_error: string | null;
}

/** GET /link, as the engine sends it. */
export interface RemoteLinkState {
	status: 'idle' | 'linking' | 'linked' | 'failed' | 'disconnected';
	address: string | null;
	error: string | null;
}

export function speakerStatusLine(speaker: RemoteSpeakerState): string {
	const where =
		speaker.address === null
			? `port ${speaker.port ?? '?'} (no LAN address: ${speaker.address_error ?? 'unknown'})`
			: `${speaker.address}:${speaker.port ?? '?'}`;
	switch (speaker.status) {
		case 'off':
			return 'Not a speaker.';
		case 'listening':
			return `Listening on ${where}, code ${speaker.code ?? '?'}.`;
		case 'answering':
			return `Answering a sender on ${where}...`;
		case 'linked':
			return `Playing MAIN from ${speaker.peer ?? 'a sender'}.`;
		case 'locked_out':
			return `Locked out after ${speaker.wrong_codes} wrong codes; the listener closed. Start again for a new code.`;
	}
}

export function linkStatusLine(link: RemoteLinkState): string {
	switch (link.status) {
		case 'idle':
			return 'Not sending.';
		case 'linking':
			return `Linking to ${link.address ?? '?'}...`;
		case 'linked':
			return `Sending MAIN to ${link.address ?? '?'} (delayed).`;
		case 'failed':
			return `Link to ${link.address ?? '?'} failed: ${link.error ?? 'no reason given'}`;
		case 'disconnected':
			return `Link to ${link.address ?? '?'} ended: ${link.error ?? 'no reason given'}`;
	}
}
