/**
 * Client-side Web Audio engine for the /performance rekordbox-parity build
 * (build unit: audio-engine). Implements the AudioEngine contract from
 * $lib/rb/types and owns the per-deck DeckState rune stores.
 *
 * Graph per deck (COMPONENT-MAP 1.6):
 *   AudioBufferSourceNode -> TRIM gain -> lowshelf (250 Hz) ->
 *   peaking (1200 Hz) -> highshelf (5 kHz) -> channel fader gain ->
 *   crossfader gain -> master gain -> destination
 *
 * Requirements (mini-PRD):
 *   ✔︎ load(): fetch audio via api-rb -> decodeAudioData -> per-deck chain;
 *     backend 404 (AUDIO_FILE_MISSING etc) -> pushToast + deckLoadErrors set
 *     + reject. Never a silent fallback.
 *     [if] load() of a stable_id whose file is missing [then] toast shown,
 *       deckLoadErrors[deck] holds the code, promise rejects ⛔️
 *     [if] load() succeeds [then] DeckState title/bpm/key/duration populated
 *     [if] a second load() starts before the first resolves [then] the stale
 *       result never clobbers the newer one ⛔️
 *   ✔︎ Reactive transport: position_ms advances via rAF clock math while
 *     playing; remaining time derivable from duration_ms - position_ms;
 *     deckEffectiveBpm() = bpm * pitch.
 *     [if] play() then 1s elapses [then] position_ms ~= 1000 * pitch
 *   ✔︎ CUE semantics: pause() stores the cue at the pause position;
 *     pressCue() while playing returns-to-cue and pauses; while paused it
 *     jumps the playhead to the cue; play() resumes from there.
 *   ✔︎ Loop: engageBeatLoop(beats) converts beats -> seconds from track bpm;
 *     seamless audio via buffer-source loopStart/loopEnd; UI clock wraps the
 *     position manually with the same bounds.
 *     [if] loop engaged and linear clock passes out point [then] position_ms
 *       wraps to in point, audio does not glitch
 *   ✔︎ Pitch: setPitch validates 0 < ratio and that it fits the selected
 *     +-8 / +-16 / WIDE range; rebases the clock so position stays correct.
 *     [if] setPitch(1.2) while range is 16 [then ⛔️] RangeError
 *   ✔︎ Mixer: TRIM / 3-band EQ / channel fader / crossfader (A-B assign
 *     matrix with THRU bypass) / master all drive real AudioNodes.
 *
 * Rune module: this file MUST stay .svelte.ts (rune_outside_svelte
 * otherwise - RECON-FRONTEND 10.1, same bug class as stores.svelte.ts fix).
 */

import { pushToast } from '$lib/stores.svelte';
import { fetchAnlz, fetchAudioArrayBuffer, getTrack, RbApiError } from '$lib/rb/api-rb';
import type { Track } from '$lib/rb/api-rb';
import type {
	AnlzCue,
	AudioEngine,
	CrossfaderAssign,
	DeckId,
	DeckState,
	EqBand,
	HotCue,
	LoopState,
	MixerChannelState,
	MixerState
} from '$lib/rb/types';

// -------------------------------------------------------------- constants

export const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4] as const;

/** Pitch fader range in percent; 100 renders as WIDE in the jog readout. */
export type PitchRange = 8 | 16 | 100;
export const PITCH_RANGES: readonly PitchRange[] = [8, 16, 100] as const;

const EQ_FREQ_LOW_HZ = 250;
const EQ_FREQ_MID_HZ = 1200;
const EQ_FREQ_HIGH_HZ = 5000;
const EQ_MID_Q = 1.0;
/** Knob 0 -> full cut (DJ-mixer style deep cut), knob 1 -> gentle boost. */
const EQ_MIN_DB = -26;
const EQ_MAX_DB = 6;
/** TRIM knob 0..1 maps linearly to 0..2x amplitude (0.5 = unity). */
const TRIM_MAX_GAIN = 2;
/** Smoothing time-constant for AudioParam changes (anti-zipper). */
const PARAM_SMOOTH_S = 0.01;

// ------------------------------------------------------------ rune stores

function _emptyDeckState(deck_id: DeckId): DeckState {
	return {
		deck_id,
		stable_id: null,
		title: null,
		artist: null,
		bpm: null,
		key: null,
		duration_ms: null,
		position_ms: 0,
		playing: false,
		cue_ms: null,
		pitch: 1,
		loop: null,
		hot_cues: [],
		anlz: null,
		anlz_error: null,
		// MASTER lit on decks 1 + 2 per SCREENSHOT-SPEC 3 (static v1).
		is_master: deck_id === 1 || deck_id === 2
	};
}

function _defaultChannel(deck_id: DeckId): MixerChannelState {
	return {
		deck_id,
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		fader: 1,
		// Screenshot assign-matrix default: odd decks -> bus A, even -> bus B.
		assign: deck_id % 2 === 1 ? 'A' : 'B'
	};
}

/** Per-deck reactive UI state, keyed 1-4. Deep-reactive $state proxy. */
export const deckStates: Record<DeckId, DeckState> = $state({
	1: _emptyDeckState(1),
	2: _emptyDeckState(2),
	3: _emptyDeckState(3),
	4: _emptyDeckState(4)
});

/** Explicit audio-load error per deck (backend code or decode message);
 * null = no failed load. DeckState has no audio-error field by contract,
 * so the failure state lives here, never swallowed. */
export const deckLoadErrors: Record<DeckId, string | null> = $state({
	1: null,
	2: null,
	3: null,
	4: null
});

/** Selected pitch range per deck (jog dial readout: +-8 / +-16 / WIDE). */
export const pitchRanges: Record<DeckId, PitchRange> = $state({
	1: 16,
	2: 16,
	3: 16,
	4: 16
});

/** Whole mixer surface (channel order on screen: 3 1 2 4). */
export const mixerState: MixerState = $state({
	channels: {
		1: _defaultChannel(1),
		2: _defaultChannel(2),
		3: _defaultChannel(3),
		4: _defaultChannel(4)
	},
	crossfader: 0.5,
	master: 1
});

/** Per-deck store accessor (contract: singleton engine + accessor). */
export function getDeckState(deck: DeckId): DeckState {
	return deckStates[deck];
}

/** Pitch-adjusted BPM for the jog readout; null until a track with a BPM
 * is loaded. Reactive when read inside $derived. */
export function deckEffectiveBpm(deck: DeckId): number | null {
	const st = deckStates[deck];
	return st.bpm === null ? null : st.bpm * st.pitch;
}

/** Remaining track time in ms (for the -MM:SS.d readout); null until a
 * track is loaded. */
export function deckRemainingMs(deck: DeckId): number | null {
	const st = deckStates[deck];
	return st.duration_ms === null ? null : Math.max(0, st.duration_ms - st.position_ms);
}

// -------------------------------------------- non-reactive audio runtime
// AudioNodes and AudioBuffers stay OUT of $state on purpose: proxying
// native audio objects breaks identity checks and buys nothing reactive.

interface _ChannelNodes {
	trim: GainNode;
	low: BiquadFilterNode;
	mid: BiquadFilterNode;
	high: BiquadFilterNode;
	fader: GainNode;
	xf: GainNode;
}

interface _DeckRuntime {
	buffer: AudioBuffer | null;
	source: AudioBufferSourceNode | null;
	/** ctx.currentTime at the moment the current source segment started. */
	startCtxTime: number;
	/** Track offset (seconds) at the moment the segment started. */
	startOffsetSec: number;
	/** Monotonic token guarding against stale load() results. */
	loadToken: number;
	nodes: _ChannelNodes | null;
}

function _emptyRuntime(): _DeckRuntime {
	return { buffer: null, source: null, startCtxTime: 0, startOffsetSec: 0, loadToken: 0, nodes: null };
}

let _ctx: AudioContext | null = null;
let _masterGain: GainNode | null = null;
let _rafId: number | null = null;
const _rt: Record<DeckId, _DeckRuntime> = {
	1: _emptyRuntime(),
	2: _emptyRuntime(),
	3: _emptyRuntime(),
	4: _emptyRuntime()
};

// ---------------------------------------------------------------- _helpers

function _assertUnit(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`${name} must be within 0..1, got ${value}`);
	}
}

function _eqDbFromKnob(value: number): number {
	// 0 -> EQ_MIN_DB, 0.5 -> 0 dB (flat), 1 -> EQ_MAX_DB. Piecewise linear.
	if (value <= 0.5) return EQ_MIN_DB * (1 - value * 2);
	return EQ_MAX_DB * (value * 2 - 1);
}

function _setParam(param: AudioParam, value: number): void {
	if (_ctx === null) throw new Error('audio graph not initialised');
	param.setTargetAtTime(value, _ctx.currentTime, PARAM_SMOOTH_S);
}

function _ensureGraph(): AudioContext {
	if (typeof window === 'undefined') {
		throw new Error('AudioEngine requires a browser AudioContext (no SSR usage)');
	}
	if (_ctx !== null) return _ctx;
	_ctx = new AudioContext();
	_masterGain = _ctx.createGain();
	_masterGain.gain.value = mixerState.master;
	_masterGain.connect(_ctx.destination);
	for (const deck of DECK_IDS) {
		const ch = mixerState.channels[deck];
		const trim = _ctx.createGain();
		trim.gain.value = ch.trim * TRIM_MAX_GAIN;
		const low = _ctx.createBiquadFilter();
		low.type = 'lowshelf';
		low.frequency.value = EQ_FREQ_LOW_HZ;
		low.gain.value = _eqDbFromKnob(ch.eq_low);
		const mid = _ctx.createBiquadFilter();
		mid.type = 'peaking';
		mid.frequency.value = EQ_FREQ_MID_HZ;
		mid.Q.value = EQ_MID_Q;
		mid.gain.value = _eqDbFromKnob(ch.eq_mid);
		const high = _ctx.createBiquadFilter();
		high.type = 'highshelf';
		high.frequency.value = EQ_FREQ_HIGH_HZ;
		high.gain.value = _eqDbFromKnob(ch.eq_high);
		const fader = _ctx.createGain();
		fader.gain.value = ch.fader;
		const xf = _ctx.createGain();
		xf.gain.value = _xfGainFor(ch.assign, mixerState.crossfader);
		trim.connect(low);
		low.connect(mid);
		mid.connect(high);
		high.connect(fader);
		fader.connect(xf);
		xf.connect(_masterGain);
		_rt[deck].nodes = { trim, low, mid, high, fader, xf };
	}
	return _ctx;
}

/** Equal-power crossfade gain for one bus assignment at position x (0..1). */
function _xfGainFor(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}

function _applyCrossfader(): void {
	const x = mixerState.crossfader;
	for (const deck of DECK_IDS) {
		const nodes = _rt[deck].nodes;
		if (nodes === null) continue;
		_setParam(nodes.xf.gain, _xfGainFor(mixerState.channels[deck].assign, x));
	}
}

function _requireLoaded(deck: DeckId, op: string): { st: DeckState; rt: _DeckRuntime } {
	const rt = _rt[deck];
	const st = deckStates[deck];
	if (rt.buffer === null || st.stable_id === null) {
		throw new Error(`${op}: no track loaded on deck ${deck}`);
	}
	return { st, rt };
}

function _durationSec(deck: DeckId): number {
	const rt = _rt[deck];
	if (rt.buffer === null) throw new Error(`deck ${deck} has no decoded buffer`);
	return rt.buffer.duration;
}

/** Engine-clock position in seconds, with manual wrap while a loop is
 * engaged (mirrors the source node's native loopStart/loopEnd jump). */
function _currentPosSec(deck: DeckId): number {
	const st = deckStates[deck];
	const rt = _rt[deck];
	if (!st.playing || _ctx === null) return st.position_ms / 1000;
	const linear = rt.startOffsetSec + (_ctx.currentTime - rt.startCtxTime) * st.pitch;
	const loop = st.loop;
	if (loop !== null && loop.engaged) {
		const inS = loop.in_ms / 1000;
		const outS = loop.out_ms / 1000;
		if (linear > outS) return inS + ((linear - outS) % (outS - inS));
		return linear;
	}
	return Math.min(linear, _durationSec(deck));
}

function _stopSource(deck: DeckId): void {
	const rt = _rt[deck];
	if (rt.source === null) return;
	rt.source.onended = null;
	rt.source.stop();
	rt.source.disconnect();
	rt.source = null;
}

function _startSource(deck: DeckId, offsetSec: number): void {
	const ctx = _ensureGraph();
	const st = deckStates[deck];
	const rt = _rt[deck];
	if (rt.buffer === null || rt.nodes === null) {
		throw new Error(`_startSource: deck ${deck} has no buffer/graph`);
	}
	_stopSource(deck);
	if (ctx.state === 'suspended') void ctx.resume();
	const source = ctx.createBufferSource();
	source.buffer = rt.buffer;
	source.playbackRate.value = st.pitch;
	const loop = st.loop;
	if (loop !== null && loop.engaged) {
		source.loop = true;
		source.loopStart = loop.in_ms / 1000;
		source.loopEnd = loop.out_ms / 1000;
	}
	source.connect(rt.nodes.trim);
	source.onended = () => {
		// Natural end-of-track only; manual stops null the handler first.
		if (_rt[deck].source !== source) return;
		_rt[deck].source = null;
		const endedSt = deckStates[deck];
		endedSt.playing = false;
		endedSt.position_ms = _durationSec(deck) * 1000;
	};
	source.start(0, offsetSec);
	rt.source = source;
	rt.startCtxTime = ctx.currentTime;
	rt.startOffsetSec = offsetSec;
	st.playing = true;
	st.position_ms = offsetSec * 1000;
	_ensureRaf();
}

/** Freeze the reactive clock at "now" and rebase the runtime segment so a
 * pitch/loop change keeps position continuous. Playing decks only. */
function _rebaseClock(deck: DeckId): number {
	const rt = _rt[deck];
	if (_ctx === null) throw new Error('audio graph not initialised');
	const pos = _currentPosSec(deck);
	rt.startOffsetSec = pos;
	rt.startCtxTime = _ctx.currentTime;
	deckStates[deck].position_ms = pos * 1000;
	return pos;
}

function _tick(): void {
	let anyPlaying = false;
	for (const deck of DECK_IDS) {
		const st = deckStates[deck];
		if (!st.playing) continue;
		anyPlaying = true;
		st.position_ms = _currentPosSec(deck) * 1000;
	}
	_rafId = anyPlaying ? requestAnimationFrame(_tick) : null;
}

function _ensureRaf(): void {
	if (_rafId === null) _rafId = requestAnimationFrame(_tick);
}

function _hotCuesFrom(cues: AnlzCue[]): HotCue[] {
	return cues
		.filter((c): c is AnlzCue & { slot: NonNullable<AnlzCue['slot']> } => c.slot !== null)
		.map((c) => ({
			slot: c.slot,
			in_ms: c.in_ms,
			out_ms: c.out_ms,
			is_loop: c.is_loop,
			color_table_index: c.color_table_index,
			comment: c.comment
		}))
		.sort((a, b) => a.slot.localeCompare(b.slot));
}

/** Display-only stored loop (COMPONENT-MAP 1.3: loop chips are display at
 * v1): the rekordbox active loop when one exists, engaged: false. */
function _displayLoopFrom(cues: AnlzCue[]): LoopState | null {
	const active = cues.find((c) => c.active_loop && c.out_ms !== null);
	if (active === undefined || active.out_ms === null) return null;
	return {
		in_ms: active.in_ms,
		out_ms: active.out_ms,
		engaged: false,
		beat_length: active.beat_loop_size
	};
}

// ------------------------------------------------------------- the engine

/** Singleton Web Audio engine implementing the AudioEngine contract, plus
 * the extras the topbar/deck units need (setMaster, pressCue, beat loops,
 * pitch ranges). All methods fail fast: invalid input or a missing backing
 * track throws; backend 404s reject with their explicit code. */
class RbAudioEngine implements AudioEngine {
	async load(deck: DeckId, stable_id: string): Promise<void> {
		if (stable_id.length === 0) throw new Error('load: stable_id must be non-empty');
		const st = deckStates[deck];
		const rt = _rt[deck];
		const token = ++rt.loadToken;
		_stopSource(deck);
		st.playing = false;
		deckLoadErrors[deck] = null;
		let track: Track;
		let buffer: AudioBuffer;
		try {
			const ctx = _ensureGraph();
			const [trackRes, audioBytes] = await Promise.all([
				getTrack(stable_id),
				fetchAudioArrayBuffer(stable_id)
			]);
			track = trackRes.track;
			buffer = await ctx.decodeAudioData(audioBytes);
		} catch (exc) {
			if (token !== rt.loadToken) throw exc; // superseded; still reject
			const msg =
				exc instanceof RbApiError ? `${exc.code}: ${exc.message}` : String(exc);
			deckLoadErrors[deck] = msg;
			pushToast(`Deck ${deck} load failed - ${msg}`, 'error');
			throw exc;
		}
		if (token !== rt.loadToken) return; // a newer load() won the race
		rt.buffer = buffer;
		st.stable_id = stable_id;
		st.title = track.title;
		st.artist = track.artist;
		st.bpm = track.bpm;
		st.key = track.key;
		// TrackOut carries duration_ms (server models.py); the hand-written
		// Track interface omits it, so read it via a typed extension. When
		// absent, the decoded buffer length is the ground truth.
		const trackDurationMs = (track as Track & { duration_ms?: number | null }).duration_ms;
		st.duration_ms =
			typeof trackDurationMs === 'number' ? trackDurationMs : Math.round(buffer.duration * 1000);
		st.position_ms = 0;
		st.cue_ms = null;
		st.pitch = 1;
		st.loop = null;
		st.hot_cues = [];
		st.anlz = null;
		st.anlz_error = null;
		// Analysis payload: a 404 here (ANALYSIS_NOT_FOUND) is a REAL library
		// state, surfaced as anlz_error - it must not fail the audio load.
		// Any other failure (network, bad payload) still throws.
		try {
			const anlz = await fetchAnlz(stable_id);
			if (token !== rt.loadToken) return;
			st.anlz = anlz;
			st.hot_cues = _hotCuesFrom(anlz.cues);
			st.loop = _displayLoopFrom(anlz.cues);
		} catch (exc) {
			if (token !== rt.loadToken) return;
			if (exc instanceof RbApiError) {
				st.anlz_error = exc.code;
			} else {
				throw exc;
			}
		}
	}

	play(deck: DeckId): void {
		const { st } = _requireLoaded(deck, 'play');
		if (st.playing) return; // transport already running is a valid state
		_startSource(deck, st.position_ms / 1000);
	}

	pause(deck: DeckId): void {
		const { st } = _requireLoaded(deck, 'pause');
		if (!st.playing) return; // already paused is a valid state
		const pos = _currentPosSec(deck);
		_stopSource(deck);
		st.playing = false;
		st.position_ms = pos * 1000;
		// CUE semantics (SCREENSHOT-SPEC 7): the cue point sits at the
		// position playback was paused at.
		st.cue_ms = st.position_ms;
	}

	cueJump(deck: DeckId, ms: number): void {
		const { st } = _requireLoaded(deck, 'cueJump');
		const durMs = _durationSec(deck) * 1000;
		if (!Number.isFinite(ms) || ms < 0 || ms > durMs) {
			throw new RangeError(`cueJump: ms must be within 0..${Math.round(durMs)}, got ${ms}`);
		}
		if (st.playing) {
			_startSource(deck, ms / 1000);
		} else {
			st.position_ms = ms;
		}
	}

	/** The physical CUE button. Playing: return to the cue point and pause.
	 * Paused with a cue set: jump the playhead to it. Paused with no cue:
	 * set the cue at the current position. */
	pressCue(deck: DeckId): void {
		const { st } = _requireLoaded(deck, 'pressCue');
		if (st.playing) {
			const target = st.cue_ms ?? 0;
			_stopSource(deck);
			st.playing = false;
			st.position_ms = target;
			return;
		}
		if (st.cue_ms === null) {
			st.cue_ms = st.position_ms;
		} else {
			st.position_ms = st.cue_ms;
		}
	}

	setPitch(deck: DeckId, ratio: number): void {
		const { st, rt } = _requireLoaded(deck, 'setPitch');
		if (!Number.isFinite(ratio) || ratio <= 0) {
			throw new RangeError(`setPitch: ratio must be > 0, got ${ratio}`);
		}
		const rangePct = pitchRanges[deck];
		if (Math.abs(ratio - 1) * 100 > rangePct + 1e-9) {
			throw new RangeError(
				`setPitch: ratio ${ratio} outside the selected +-${rangePct}% range on deck ${deck}`
			);
		}
		if (st.playing && rt.source !== null) {
			_rebaseClock(deck);
			rt.source.playbackRate.value = ratio;
		}
		st.pitch = ratio;
	}

	/** Select the pitch fader range. Throws when the current pitch no longer
	 * fits - reset pitch toward 1.0 first (explicit, never a silent clamp). */
	setPitchRange(deck: DeckId, range: PitchRange): void {
		if (!PITCH_RANGES.includes(range)) {
			throw new RangeError(`setPitchRange: invalid range ${range}`);
		}
		const st = deckStates[deck];
		if (Math.abs(st.pitch - 1) * 100 > range + 1e-9) {
			throw new RangeError(
				`setPitchRange: current pitch ${st.pitch} exceeds +-${range}% - reset pitch first`
			);
		}
		pitchRanges[deck] = range;
	}

	setLoop(deck: DeckId, loop: { in_ms: number; out_ms: number } | null): void {
		const { st, rt } = _requireLoaded(deck, 'setLoop');
		if (loop === null) {
			if (st.playing && rt.source !== null && st.loop !== null && st.loop.engaged) {
				// Rebase to the wrapped position, then let the source run on.
				_rebaseClock(deck);
				rt.source.loop = false;
			}
			st.loop = null;
			return;
		}
		const durMs = _durationSec(deck) * 1000;
		if (
			!Number.isFinite(loop.in_ms) ||
			!Number.isFinite(loop.out_ms) ||
			loop.in_ms < 0 ||
			loop.out_ms <= loop.in_ms ||
			loop.out_ms > durMs
		) {
			throw new RangeError(
				`setLoop: need 0 <= in_ms < out_ms <= ${Math.round(durMs)}, got ${loop.in_ms}..${loop.out_ms}`
			);
		}
		st.loop = { in_ms: loop.in_ms, out_ms: loop.out_ms, engaged: true, beat_length: null };
		if (st.playing) {
			const posMs = _currentPosSec(deck) * 1000;
			if (posMs > loop.out_ms) {
				// Past the loop already: restart inside it.
				_startSource(deck, loop.in_ms / 1000);
			} else if (rt.source !== null) {
				_rebaseClock(deck);
				rt.source.loopStart = loop.in_ms / 1000;
				rt.source.loopEnd = loop.out_ms / 1000;
				rt.source.loop = true;
			}
		}
	}

	/** Engage a beat loop from the current position. Beats -> seconds via the
	 * track bpm (playbackRate scales audio and loop identically, so the
	 * native bpm is the correct base). */
	engageBeatLoop(deck: DeckId, beats: number): void {
		const { st } = _requireLoaded(deck, 'engageBeatLoop');
		if (!Number.isFinite(beats) || beats <= 0) {
			throw new RangeError(`engageBeatLoop: beats must be > 0, got ${beats}`);
		}
		if (st.bpm === null || st.bpm <= 0) {
			throw new Error(`engageBeatLoop: deck ${deck} track has no BPM - cannot size a beat loop`);
		}
		const in_ms = st.playing ? _currentPosSec(deck) * 1000 : st.position_ms;
		const lenMs = (beats * 60000) / st.bpm;
		const out_ms = Math.min(in_ms + lenMs, _durationSec(deck) * 1000);
		if (out_ms <= in_ms) {
			throw new RangeError(`engageBeatLoop: loop would be empty at position ${in_ms}ms`);
		}
		this.setLoop(deck, { in_ms, out_ms });
		if (st.loop !== null) st.loop.beat_length = beats;
	}

	setTrim(deck: DeckId, value: number): void {
		_assertUnit('setTrim value', value);
		mixerState.channels[deck].trim = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.trim.gain, value * TRIM_MAX_GAIN);
	}

	setEq(deck: DeckId, band: EqBand, value: number): void {
		_assertUnit('setEq value', value);
		const ch = mixerState.channels[deck];
		const nodes = _rt[deck].nodes;
		const db = _eqDbFromKnob(value);
		if (band === 'low') {
			ch.eq_low = value;
			if (nodes !== null) _setParam(nodes.low.gain, db);
		} else if (band === 'mid') {
			ch.eq_mid = value;
			if (nodes !== null) _setParam(nodes.mid.gain, db);
		} else if (band === 'high') {
			ch.eq_high = value;
			if (nodes !== null) _setParam(nodes.high.gain, db);
		} else {
			const _exhaustive: never = band;
			throw new Error(`Unhandled EQ band: ${_exhaustive}`);
		}
	}

	setFader(deck: DeckId, value: number): void {
		_assertUnit('setFader value', value);
		mixerState.channels[deck].fader = value;
		const nodes = _rt[deck].nodes;
		if (nodes !== null) _setParam(nodes.fader.gain, value);
	}

	setCrossfader(value: number): void {
		_assertUnit('setCrossfader value', value);
		mixerState.crossfader = value;
		if (_ctx !== null) _applyCrossfader();
	}

	assignChannel(deck: DeckId, assign: CrossfaderAssign): void {
		if (assign !== 'A' && assign !== 'B' && assign !== 'THRU') {
			const _exhaustive: never = assign;
			throw new Error(`Unhandled crossfader assign: ${_exhaustive}`);
		}
		mixerState.channels[deck].assign = assign;
		if (_ctx !== null) _applyCrossfader();
	}

	/** Topbar master-volume slider -> master GainNode (COMPONENT-MAP 1.1). */
	setMaster(value: number): void {
		_assertUnit('setMaster value', value);
		mixerState.master = value;
		if (_masterGain !== null) _setParam(_masterGain.gain, value);
	}
}

/** The singleton engine every /performance unit imports. */
export const engine: RbAudioEngine = new RbAudioEngine();
