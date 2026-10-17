/**
 * Synthesized media for sandbox stories. Recorded track audio is megabytes
 * (the lyrics fixture's WAV is 10 MB), too big to commit, and a contract about
 * UI behavior only needs a stream the browser can open. So this builds a short
 * silent PCM WAV in memory instead. Sandbox only; never imported by app code.
 */
import { http, HttpResponse, type HttpHandler } from 'msw';

/** A mono 16-bit PCM WAV of `seconds` of silence at `rate` Hz. */
function silentWav(seconds: number, rate = 8000): ArrayBuffer {
	const samples = Math.round(seconds * rate);
	const buffer = new ArrayBuffer(44 + samples * 2);
	const view = new DataView(buffer);
	const ascii = (at: number, text: string) => {
		for (let i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i));
	};
	ascii(0, 'RIFF');
	view.setUint32(4, 36 + samples * 2, true);
	ascii(8, 'WAVE');
	ascii(12, 'fmt ');
	view.setUint32(16, 16, true);
	view.setUint16(20, 1, true); // PCM
	view.setUint16(22, 1, true); // mono
	view.setUint32(24, rate, true);
	view.setUint32(28, rate * 2, true);
	view.setUint16(32, 2, true);
	view.setUint16(34, 16, true);
	ascii(36, 'data');
	view.setUint32(40, samples * 2, true);
	return buffer;
}

/** Serves silence for GET /api/v1/tracks/{stable_id}/audio. */
export function silentTrackAudio(stableId: string, seconds = 1): HttpHandler {
	return http.get(`/api/v1/tracks/${stableId}/audio`, () =>
		HttpResponse.arrayBuffer(silentWav(seconds), { headers: { 'Content-Type': 'audio/wav' } })
	);
}
