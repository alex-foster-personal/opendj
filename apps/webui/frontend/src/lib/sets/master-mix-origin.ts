/** Where the live master-mix tap's first frame sits on its AudioContext clock (SETREC-08).
 *
 * Control events carry `sample` = the frame index into the recorded WAV
 * ((private path)), not frames since the context was created. The
 * master tap records its origin here when it attaches (and again on each
 * graph rebuild, carrying the frames already captured), so the control-event
 * recorder can stamp against the recording without importing the lazily
 * loaded capture module. */

export interface MasterMixOrigin {
	sessionId: string;
	context: BaseAudioContext;
	/** Context frame at which this tap started feeding the recording. */
	contextFrame: number;
	/** Recorded frames that came before this tap (earlier taps of the same recording). */
	recordedFrames: number;
	sampleRate: number;
}

let _origin: MasterMixOrigin | null = null;

/** Publish (or clear, with null) the live master tap's origin. */
export function setMasterMixOrigin(origin: MasterMixOrigin | null): void {
	_origin = origin;
}

/** Clear the origin only when it still belongs to `sessionId`. */
export function clearMasterMixOrigin(sessionId: string): void {
	if (_origin?.sessionId === sessionId) _origin = null;
}

/** The live master tap's origin, or null when no tap is feeding a recording. */
export function masterMixOrigin(): MasterMixOrigin | null {
	return _origin;
}

/** Frame index into the recorded WAV at context time `contextSec`, or null when it falls before the tap started. */
export function recordedSampleAt(origin: MasterMixOrigin, contextSec: number): number | null {
	const frame = Math.round(contextSec * origin.sampleRate) - origin.contextFrame;
	if (!Number.isFinite(frame) || frame < 0) return null;
	return origin.recordedFrames + frame;
}
