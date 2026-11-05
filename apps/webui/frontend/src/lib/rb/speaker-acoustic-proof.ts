/**
 * AUDIO-DEVICE-04: acoustic proof that the room hears the master bus.
 *
 * The shell's HAL reads called a stalled HYPERBOOM link healthy (Tue 3 Nov
 * 2026), so the only check that separates a stall is one that listens. While a
 * deck plays, the master bus and a WIRED or USB microphone (the MX Brio on
 * silver) are each reduced to a 50 Hz loudness envelope, and every 2 s window
 * the best Pearson correlation between them over 0 to 500 ms of lag (Bluetooth
 * latency plus the air) decides heard or missed. A Bluetooth input is never
 * opened: it would flip a headset into HFP and wreck the output under test.
 * N consecutive missed windows report "not delivering".
 *
 * Pure: the browser runtime (speaker-acoustic-proof-runtime.ts) feeds it.
 */

export const ACOUSTIC_SAMPLE_MS = 20;
export const ACOUSTIC_WINDOW_MS = 2_000;
export const ACOUSTIC_WINDOW_FRAMES = ACOUSTIC_WINDOW_MS / ACOUSTIC_SAMPLE_MS;
export const ACOUSTIC_MAX_LAG_FRAMES = 25; // 500 ms
export const ACOUSTIC_MIN_OVERLAP_FRAMES = 60;
export const ACOUSTIC_HEARD_CORRELATION = 0.35;
export const ACOUSTIC_WINDOWS_REQUIRED = 3;
/** Below this master RMS, or with a flat envelope, a window proves nothing. */
export const ACOUSTIC_MASTER_RMS_FLOOR = 0.003;
export const ACOUSTIC_MIN_ENVELOPE_CV = 0.05;
/** Mirrors the shell's ACOUSTIC_MIC_TRANSPORTS (speaker_link.rs). */
export const ACOUSTIC_MIC_TRANSPORTS: readonly string[] = ['usb', 'builtin', 'thunderbolt'];

export type WindowKind = 'heard' | 'missed' | 'inconclusive';
export type AcousticVerdict = 'heard' | 'not_delivering' | 'inconclusive';

export interface WindowJudgement {
	kind: WindowKind;
	peak: number | null;
}

function mean(values: readonly number[]): number {
	return values.reduce((sum, value) => sum + value, 0) / values.length;
}

export function pearson(a: readonly number[], b: readonly number[]): number | null {
	if (a.length !== b.length || a.length < 2) return null;
	const ma = mean(a);
	const mb = mean(b);
	let num = 0;
	let da = 0;
	let db = 0;
	for (let i = 0; i < a.length; i++) {
		const x = a[i] - ma;
		const y = b[i] - mb;
		num += x * y;
		da += x * x;
		db += y * y;
	}
	if (da === 0 || db === 0) return null;
	return num / Math.sqrt(da * db);
}

/** Best correlation of `mic` lagging `master` by 0..maxLag frames. */
export function peakLaggedCorrelation(
	master: readonly number[],
	mic: readonly number[],
	maxLag: number = ACOUSTIC_MAX_LAG_FRAMES,
	minOverlap: number = ACOUSTIC_MIN_OVERLAP_FRAMES
): number | null {
	if (master.length !== mic.length) throw new Error('master and mic envelopes must be the same length');
	let best: number | null = null;
	for (let lag = 0; lag <= maxLag; lag++) {
		const overlap = master.length - lag;
		if (overlap < minOverlap) break;
		const r = pearson(master.slice(0, overlap), mic.slice(lag, lag + overlap));
		if (r !== null && (best === null || r > best)) best = r;
	}
	return best;
}

export function judgeWindow(input: {
	master: readonly number[];
	mic: readonly number[];
	deckPlayedThroughout: boolean;
}): WindowJudgement {
	if (!input.deckPlayedThroughout || input.master.length < ACOUSTIC_MIN_OVERLAP_FRAMES) {
		return { kind: 'inconclusive', peak: null };
	}
	const level = mean(input.master);
	const spread = Math.sqrt(mean(input.master.map((value) => (value - level) ** 2)));
	if (level < ACOUSTIC_MASTER_RMS_FLOOR || spread / level < ACOUSTIC_MIN_ENVELOPE_CV) {
		return { kind: 'inconclusive', peak: null };
	}
	const peak = peakLaggedCorrelation(input.master, input.mic);
	return { kind: peak !== null && peak >= ACOUSTIC_HEARD_CORRELATION ? 'heard' : 'missed', peak };
}

export interface AcousticTrackerState {
	verdict: AcousticVerdict;
	failed_windows: number;
	windows_required: number;
	peak_correlation: number | null;
	/** True on the one window that turned the verdict into not_delivering. */
	newly_not_delivering: boolean;
}

export function createAcousticTracker(windowsRequired: number = ACOUSTIC_WINDOWS_REQUIRED): {
	push(judgement: WindowJudgement): AcousticTrackerState;
} {
	if (!Number.isInteger(windowsRequired) || windowsRequired < 1) {
		throw new Error(`windowsRequired must be an integer >= 1, got ${windowsRequired}`);
	}
	let failed = 0;
	let reported = false;
	return {
		push(judgement) {
			if (judgement.kind === 'heard') {
				failed = 0;
				reported = false;
			} else if (judgement.kind === 'missed') {
				failed += 1;
			} else if (judgement.kind === 'inconclusive') {
				// A quiet passage neither clears nor adds to a run of misses.
			} else {
				const _exhaustive: never = judgement.kind;
				throw new Error(`Unhandled window kind: ${String(_exhaustive)}`);
			}
			const notDelivering = failed >= windowsRequired;
			const newly = notDelivering && !reported;
			if (notDelivering) reported = true;
			return {
				verdict: notDelivering ? 'not_delivering' : judgement.kind === 'heard' ? 'heard' : 'inconclusive',
				failed_windows: failed,
				windows_required: windowsRequired,
				peak_correlation: judgement.peak,
				newly_not_delivering: newly
			};
		}
	};
}

export interface ShellInputDevice {
	uid: string;
	name: string;
	transport: string;
	channels: number;
	acoustic_proof_allowed: boolean;
}

export interface BrowserMic {
	deviceId: string;
	label: string;
}

export interface ProofMic {
	deviceId: string;
	label: string;
	transport: string;
}

/**
 * Browser mics the proof may open: the label must name a CoreAudio input the
 * shell lists on a wired or USB transport. Chromium appends `(vid:pid)` to USB
 * labels, so `MX Brio (046d:0944)` matches `MX Brio`. A label the shell does
 * not list, or one with names withheld (empty), is never a candidate.
 */
export function proofMicCandidates(inputs: readonly ShellInputDevice[], mics: readonly BrowserMic[]): ProofMic[] {
	const out: ProofMic[] = [];
	for (const mic of mics) {
		if (mic.label === '' || mic.deviceId === '') continue;
		const match = inputs.find((input) => mic.label === input.name || mic.label.startsWith(`${input.name} (`));
		if (match === undefined) continue;
		if (!match.acoustic_proof_allowed || !ACOUSTIC_MIC_TRANSPORTS.includes(match.transport)) continue;
		out.push({ deviceId: mic.deviceId, label: mic.label, transport: match.transport });
	}
	return out;
}
