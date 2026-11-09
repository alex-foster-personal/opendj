/**
 * Pure inference for MIDI learn: classify a captured message window and build
 * binding flags from it (build unit: midi panel).
 *
 * No WebMIDI, runes or engine imports - safe to load from midi-ui-state on
 * first paint without tripping midi-lazy-engine.test.mjs.
 *
 * Requirements (mini-PRD):
 *   ✔︎ inferControlKind classifies absolute, relative-twos, relative-centred,
 *     button-cc, absolute-14bit, button-note or ambiguous from a window.
 *     [if] a 0..127 fader sweep isn't absolute [then ⛔️] broken
 *     [if] alternating 0x01/0x7F isn't relative-twos [then ⛔️] broken
 *   ✔︎ LearnWindow collects 600 ms after the first CC; notes bind immediately.
 *     [if] a second cc push restarts the timer [then ⛔️] broken
 *   ✔︎ ambiguous is never turned into a binding by buildLearnedBinding.
 *     [if] ambiguous yields a MidiBinding [then ⛔️] broken
 */

import { decodeRelative, decodeRelativeCentered } from './decode';
import type { LearnLogEntry, MidiAction, MidiBinding, MidiSource } from './midi-types';

export const LEARN_WINDOW_MS = 600;
export const MIN_LEARN_ENTRIES = 4;
/** MIDI-standard MSB CC n / LSB CC n+32. */
export const LSB_PAIR_OFFSET = 32;

export type InferredControl =
	| { kind: 'button-note'; source: MidiSource }
	| { kind: 'button-cc'; source: MidiSource }
	| { kind: 'absolute'; source: MidiSource }
	| { kind: 'relative-twos'; source: MidiSource }
	| { kind: 'relative-centred'; source: MidiSource }
	| { kind: 'absolute-14bit'; source: MidiSource; lsbOffset: number }
	| { kind: 'ambiguous'; reason: string; source: MidiSource | null };
export type InferredControlKind = InferredControl['kind'];

// ----------------------------------------------------------------- helpers

function _sourceKey(src: MidiSource): string {
	return `${src.ch}|${src.kind}|${src.id}`;
}

function _sameSource(a: MidiSource, b: MidiSource): boolean {
	return a.ch === b.ch && a.kind === b.kind && a.id === b.id;
}

function _countForSource(entries: LearnLogEntry[], src: MidiSource): number {
	return entries.filter((e) => e.decoded !== null && _sameSource(e.decoded, src)).length;
}

function _entriesForSource(entries: LearnLogEntry[], src: MidiSource): LearnLogEntry[] {
	return entries.filter((e) => e.decoded !== null && _sameSource(e.decoded, src));
}

function _fitsTwos(values: number[]): boolean {
	return values.every((v) => v !== 0 && Math.abs(decodeRelative(v)) <= 30);
}

function _fitsCentred(values: number[]): boolean {
	return values.every((v) => {
		const d = Math.abs(decodeRelativeCentered(v));
		return d >= 1 && d <= 30;
	});
}

function _absoluteLike(values: number[]): boolean {
	const distinct = new Set(values);
	if (distinct.size < 3) return false;
	for (let i = 1; i < values.length; i++) {
		if (values[i] === values[i - 1]) return false;
		if (Math.abs(values[i] - values[i - 1]) > 32) return false;
	}
	return true;
}

function _lsbPairingOk(msbEntries: LearnLogEntry[], lsbEntries: LearnLogEntry[]): boolean {
	if (msbEntries.length === 0) return false;
	let paired = 0;
	for (let i = 0; i < msbEntries.length; i++) {
		const msbTs = msbEntries[i].ts;
		const nextMsbTs = i + 1 < msbEntries.length ? msbEntries[i + 1].ts : Infinity;
		const hasLsb = lsbEntries.some((l) => l.ts > msbTs && l.ts < nextMsbTs);
		if (hasLsb) paired++;
	}
	return paired >= Math.ceil(msbEntries.length / 2);
}

function _describeSource(src: MidiSource): string {
	if (src.kind === 'note') return `ch ${src.ch} note ${src.id}`;
	if (src.kind === 'cc') return `ch ${src.ch} CC ${src.id}`;
	return `ch ${src.ch} pitchbend`;
}

// -------------------------------------------------------- inferControlKind

/** Classify a learn capture window. Deterministic, total, never throws. */
export function inferControlKind(entries: LearnLogEntry[]): InferredControl {
	const decoded = entries
		.filter((e) => e.decoded !== null)
		.slice()
		.sort((a, b) => a.ts - b.ts);

	if (decoded.length === 0) {
		return { kind: 'ambiguous', reason: 'no decodable MIDI message was captured', source: null };
	}

	const deviceId = decoded[0].deviceId;
	const sameDevice = decoded.filter((e) => e.deviceId === deviceId);
	if (sameDevice.length === 0) {
		return { kind: 'ambiguous', reason: 'no decodable MIDI message was captured', source: null };
	}

	let primary = sameDevice[0].decoded!;

	if (primary.kind === 'note') {
		return { kind: 'button-note', source: primary };
	}

	if (primary.kind === 'pitchbend') {
		return { kind: 'absolute', source: primary };
	}

	// CC path
	if (primary.kind === 'cc') {
		// LSB-first retarget
		if (primary.id >= LSB_PAIR_OFFSET) {
			const msbId = primary.id - LSB_PAIR_OFFSET;
			const msbSrc: MidiSource = { ch: primary.ch, kind: 'cc', id: msbId };
			if (_countForSource(sameDevice, msbSrc) > 0) {
				primary = msbSrc;
			}
		}

		const lsbSrc: MidiSource = { ch: primary.ch, kind: 'cc', id: primary.id + LSB_PAIR_OFFSET };
		const lsbCount = _countForSource(sameDevice, lsbSrc);

		// 14-bit pair
		if (primary.id < LSB_PAIR_OFFSET && lsbCount >= 2) {
			const msbEntries = _entriesForSource(sameDevice, primary);
			const lsbEntries = _entriesForSource(sameDevice, lsbSrc);
			if (_lsbPairingOk(msbEntries, lsbEntries)) {
				return { kind: 'absolute-14bit', source: primary, lsbOffset: LSB_PAIR_OFFSET };
			}
			return {
				kind: 'ambiguous',
				reason: `CC ${primary.id + LSB_PAIR_OFFSET} traffic does not pair with CC ${primary.id}`,
				source: primary
			};
		}

		// Two controls moved
		const bySource = new Map<string, { src: MidiSource; count: number }>();
		for (const e of sameDevice) {
			const src = e.decoded!;
			if (_sameSource(src, primary) || _sameSource(src, lsbSrc)) continue;
			const key = _sourceKey(src);
			const prev = bySource.get(key);
			if (prev) prev.count++;
			else bySource.set(key, { src, count: 1 });
		}
		for (const { src, count } of bySource.values()) {
			if (count >= MIN_LEARN_ENTRIES) {
				return {
					kind: 'ambiguous',
					reason: `two controls moved (${_describeSource(primary)} and ${_describeSource(src)}); move one control only`,
					source: primary
				};
			}
		}

		const primaryEntries = _entriesForSource(sameDevice, primary);
		const values = primaryEntries.map((e) => e.data2);

		if (values.length < MIN_LEARN_ENTRIES) {
			return {
				kind: 'ambiguous',
				reason: `only ${values.length} message(s) captured from ${_describeSource(primary)}; move the control further`,
				source: primary
			};
		}

		const distinct = [...new Set(values)].sort((a, b) => a - b);
		if (distinct.length === 2 && distinct[0] === 0 && distinct[1] > 0) {
			return { kind: 'button-cc', source: primary };
		}

		const twos = _fitsTwos(values);
		const centred = _fitsCentred(values);
		const absLike = _absoluteLike(values);

		if (absLike && !twos && !centred) {
			return { kind: 'absolute', source: primary };
		}
		if (twos && !absLike) {
			return { kind: 'relative-twos', source: primary };
		}
		if (centred && !absLike) {
			return { kind: 'relative-centred', source: primary };
		}
		if (absLike && (twos || centred)) {
			return {
				kind: 'ambiguous',
				reason:
					'values fit both a fader near its end/centre and a relative encoder; sweep the full range or turn one detent at a time',
				source: primary
			};
		}
		return {
			kind: 'ambiguous',
			reason: 'CC values jump without an absolute or relative pattern',
			source: primary
		};
	}

	return { kind: 'ambiguous', reason: 'no decodable MIDI message was captured', source: null };
}

// -------------------------------------------------- binding flag builders

/** Binding flags the inferred kind implies. relativeUnit is never inferred:
 * it is a per-action detent preference, not a wire property. */
export function learnedBindingFlags(
	result: InferredControl
): Pick<MidiBinding, 'relative' | 'relativeCentered' | 'ccButton'> | null {
	if (result.kind === 'ambiguous') return null;
	switch (result.kind) {
		case 'absolute':
		case 'absolute-14bit':
		case 'button-note':
			return { relative: false, relativeCentered: false, ccButton: false };
		case 'relative-twos':
			return { relative: true, relativeCentered: false, ccButton: false };
		case 'relative-centred':
			return { relative: true, relativeCentered: true, ccButton: false };
		case 'button-cc':
			return { relative: false, relativeCentered: false, ccButton: true };
		default: {
			const _exhaustive: never = result;
			return _exhaustive;
		}
	}
}

/** Build a MidiBinding from a learn target and inferred control. Returns null
 * for ambiguous. A 14-bit window only sets deck_pitch.lsbOffset; other actions
 * bind to the MSB as 7-bit. */
export function buildLearnedBinding(
	target: { action: MidiAction; shift?: boolean },
	result: InferredControl
): MidiBinding | null {
	if (result.kind === 'ambiguous') return null;
	const flags = learnedBindingFlags(result);
	if (flags === null) return null;

	let action: MidiAction = target.action;
	if (action.type === 'deck_pitch') {
		const lsbOffset = result.kind === 'absolute-14bit' ? result.lsbOffset : null;
		action = { ...action, lsbOffset };
	}

	return {
		source: result.source,
		action,
		...flags,
		...(target.shift ? { shift: true } : {})
	};
}

// ----------------------------------------------------------- LearnWindow

export interface LearnTimers {
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
}

export class LearnWindow {
	private _onDone: (result: InferredControl, entries: LearnLogEntry[]) => void;
	private _timers: LearnTimers;
	private _windowMs: number;
	private _stored: LearnLogEntry[] = [];
	private _deviceId: string | null = null;
	private _timerHandle: unknown = null;
	_capturing = false;
	_done = false;
	private _cancelled = false;

	constructor(
		onDone: (result: InferredControl, entries: LearnLogEntry[]) => void,
		timers: LearnTimers = globalThis,
		windowMs = LEARN_WINDOW_MS
	) {
		this._onDone = onDone;
		this._timers = timers;
		this._windowMs = windowMs;
	}

	get capturing(): boolean {
		return this._capturing;
	}

	get done(): boolean {
		return this._done;
	}

	push(entry: LearnLogEntry): boolean {
		if (this._done || this._cancelled) return this._done;
		if (entry.decoded === null) return false;

		if (this._deviceId === null) {
			this._deviceId = entry.deviceId;
			this._stored.push(entry);
			const src = entry.decoded;

			if (src.kind === 'note') {
				this._close({ kind: 'button-note', source: src }, [entry]);
				return true;
			}
			if (src.kind === 'pitchbend') {
				this._close({ kind: 'absolute', source: src }, [entry]);
				return true;
			}
			// cc: start window
			this._capturing = true;
			this._timerHandle = this._timers.setTimeout(() => {
				this._close(inferControlKind(this._stored), this._stored.slice());
			}, this._windowMs);
			return false;
		}

		if (entry.deviceId !== this._deviceId) return false;
		this._stored.push(entry);
		return false;
	}

	cancel(): void {
		if (this._done || this._cancelled) return;
		this._cancelled = true;
		if (this._timerHandle !== null) {
			this._timers.clearTimeout(this._timerHandle);
			this._timerHandle = null;
		}
		this._capturing = false;
	}

	private _close(result: InferredControl, entries: LearnLogEntry[]): void {
		if (this._done) return;
		this._done = true;
		this._capturing = false;
		if (this._timerHandle !== null) {
			this._timers.clearTimeout(this._timerHandle);
			this._timerHandle = null;
		}
		this._onDone(result, entries);
	}
}
