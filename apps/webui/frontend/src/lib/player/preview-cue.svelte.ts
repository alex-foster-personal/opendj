/**
 * The library preview player: the app's fifth voice, and the only one that is
 * not a deck.
 *
 * Mini-PRD
 * --------
 * R1 (CUEOUT-15) Clicking a library mini-waveform plays that track from that
 *    point through the headphone CUE bus, without loading it onto a deck and
 *    without touching the room mix.
 * R2 (CUEOUT-15) A preview that cannot be heard is refused with a named
 *    cause, never started into silence (`preview-cue-policy.ts` decides).
 * R3 (CUEOUT-15) Decoded preview audio is capped by BYTES, not by track
 *    count, the sounding track is never evicted, and an idle preview releases
 *    everything rather than holding PCM for the rest of the session.
 *
 *   [if] a track is clicked at 50% [then] audio starts near the half-way
 *        point on the cue bus ⛔️
 *   [if] a cue device is selected [then] nothing of the preview reaches the
 *        MAIN output, because the only connection made is to `cueSum` ⛔️
 *   [if] enough tracks are previewed to pass the budget [then] the least
 *        recently used decoded tracks are dropped until it fits ⛔️
 *   [if] the track that is SOUNDING is the eviction candidate [then] it is
 *        kept, whatever the budget says ⛔️
 *   [if] a preview is left stopped for the idle interval [then] every decoded
 *        track is released ⛔️
 *   [if] a recently previewed strip is clicked again [then] no second decode
 *        happens and playback starts from the cached PCM ⛔️
 *   [if] the file cannot be decoded [then] the decoder's own error is
 *        reported as a toast, never swallowed ⛔️
 *
 * R6 (CUEOUT-15) A preview tempo-matches the playing master deck when the
 *    setting is on and the match fits the preview pitch range.
 *    `preview-beat-sync.ts` owns that decision and its acceptance tests.
 *
 * R4 (CUEOUT-15) A click pays as little of the fetch and decode as it can:
 *    bytes already held by the row-select prefetch are reused, and a dwelt
 *    hover warms both halves ahead of the click.
 *
 *   [if] the row-select prefetch already holds the bytes [then] the click does
 *        not re-fetch them ⛔️
 *   [if] a row is hovered past the dwell and no AudioContext exists yet [then]
 *        the bytes are warmed and NO audio engine is started ⛔️
 *   [if] a row is hovered past the dwell and a context already exists [then]
 *        the track is decoded into the cache, so the click starts from PCM ⛔️
 *   [if] the pointer leaves before the dwell [then] nothing is fetched, so
 *        scrolling a long list costs no requests ⛔️
 *   [if] `stopPreviewCue` is called [then] the playhead disappears from the
 *        strip, the source stops, and the decoded audio is released ⛔️
 *
 * Why the decoder rather than an HTMLAudioElement
 * -----------------------------------------------
 * A media element streaming `/tracks/{id}/audio` over Range was the first
 * design and is the cheaper one on paper: no PCM, a bounded buffer, seeking
 * as a ranged request. The preview uses the same
 * decoder as the four decks so its format support follows theirs rather
 * than a second media-element pipeline. The private browser investigation
 * and track timing measurements are not included in the public source.
 *
 * Decoded PCM has a byte cost proportional to duration, channel count,
 * sample rate and sample width. Holding a long preview until an explicit
 * stop or another selection retains its buffer after playback ends. Hence the byte budget, the LRU and the idle
 * release below. The decks may hold four decoded tracks of their own already,
 * which is why the budget here is deliberately modest.
 *
 * Status: ✔︎ ✅
 */

import {
	headphoneMixGains,
	peekCueBus,
	practiceCueWithGain,
	practiceMainGains,
	splitCableGains
} from '$lib/player/headphones';
import { previewCacheEvictions } from '$lib/player/preview-cue-cache';
import {
	previewCueVerdict,
	type PreviewCueVerdict,
	type PreviewRoute
} from '$lib/player/preview-cue-policy';
import { previewRateFor } from '$lib/player/preview-beat-sync-live';
import { mixerState } from '$lib/player/state.svelte';
import { ensureAudioGraphForCue } from '$lib/rb/audio-engine.svelte';
import { fetchAudioArrayBuffer } from '$lib/rb/api-rb';
import {
	copyPrefetchedAudio,
	ensureAudioPrefetch,
	provideDecodedAudio
} from '$lib/rb/audio-prefetch-cache.svelte';
import { previewPcmByteCapForPosture } from '$lib/rb/app-posture';
import { activeScalers } from '$lib/rb/perf-tier';
import {
	pressureScaledPreviewPcmByteCap,
	previewWarmIsShed
} from '$lib/rb/prefetch-pressure-caps';
import { registerCapsConsumer } from '$lib/rb/cache-caps-registry';

/** Playhead publish interval. 10 Hz is the repo's existing readout cadence
 * (StageOverlay samples at the same rate) and costs no rAF while idle. */
const POSITION_TICK_MS = 100;

/**
 * Decoded-PCM budget for the preview cache, in bytes, RIGHT NOW.
 *
 * Decoded cost depends on frames, channels and Float32 sample width. A
 * count-based cache does not bound memory when track durations differ,
 * so the budget is in bytes.
 *
 * It is a function, not a constant, because a fixed number would have made
 * this the only cache in the page that ignores the machine. It is the tier's
 * `preview_pcm_bytes` (PERFMODE-01: 64 / 160 / 256 MiB for LOW / STANDARD /
 * HIGH), floored by the Gig posture (PERFMODE-03), then stepped down by the
 * same closed-loop pressure stepper the prefetch caps use (PERFMODE-04 Q29).
 * The decks may already hold four decoded tracks of their own, which is why
 * even the HIGH figure is not generous.
 */
function previewCacheBudgetBytes(): number {
	return pressureScaledPreviewPcmByteCap(
		previewPcmByteCapForPosture(activeScalers().preview_pcm_bytes)
	);
}

/**
 * Drop every cached track after this long with nothing playing.
 *
 * Without it the cache is a leak with good manners: the previous revision held
 * its one buffer until the operator previewed something else or pressed stop,
 * so previewing a long track and walking away retained its decoded buffer
 * for the rest of the session. Nothing ever released on the track simply ending.
 */
const PREVIEW_IDLE_RELEASE_MS = 90_000;

/** Live preview read model. `stable_id` null means nothing is previewing. */
export const previewCue: {
	stable_id: string | null;
	position_ms: number;
	duration_ms: number | null;
	playing: boolean;
	route: PreviewRoute | null;
	/** Playback rate the sounding preview runs at. 1 unless it tempo-matched. */
	rate: number;
	error: string | null;
} = $state({
	stable_id: null,
	position_ms: 0,
	duration_ms: null,
	playing: false,
	route: null,
	rate: 1,
	error: null
});

/**
 * Decoded tracks, most-recently-used last, under `previewCacheBudgetBytes()`.
 *
 * A Map preserves insertion order, so re-inserting on every hit makes the
 * first entry the eviction candidate with no separate bookkeeping. The track
 * that is currently SOUNDING is never evicted, whatever the budget says:
 * freeing the buffer under a running `AudioBufferSourceNode` is the one
 * eviction that could be heard.
 */
const _cache = new Map<string, AudioBuffer>();
let _playingSid: string | null = null;
let _idleTimer: ReturnType<typeof setTimeout> | null = null;
let _source: AudioBufferSourceNode | null = null;
let _gain: GainNode | null = null;
let _ticker: ReturnType<typeof setInterval> | null = null;
/** Context time the current source started, and the offset it started from,
 * so the playhead is read from the audio clock rather than counted. */
let _startedAtSec = 0;
let _startedOffsetSec = 0;
/** Rate the running source plays at, so the playhead advances with it. */
let _startedRate = 1;
/** Bumped by every new request and every stop, so a slow decode for a track
 * the operator has already moved on from cannot start or publish over the
 * newer one. The same generation guard `headphones.ts` uses for its async
 * device selections. */
let _generation = 0;

/**
 * The LINEAR gain the preview will reach the ear at on the current route.
 * Derived from `headphones.ts`'s own published maths rather than a second
 * copy: MIX and GAIN mean exactly what they mean for a channel's PFL.
 */
export function previewCuePathGain(): number {
	const hp = mixerState.headphones;
	switch (hp.output_mode) {
		case 'two_outputs':
			return headphoneMixGains(hp.mix).cue * hp.level;
		case 'split_cable':
			return splitCableGains('split_cable', hp.mix, hp.level).rightCue;
		case 'practice':
			return practiceCueWithGain(
				practiceMainGains('practice', hp.selected_output_device_id, hp.mix).cue,
				hp.level
			);
		default: {
			const _exhaustive: never = hp.output_mode;
			throw new Error(`unhandled headphone output mode: ${String(_exhaustive)}`);
		}
	}
}

/** The label of the selected cue device, for a refusal that names it. */
function _cueDeviceLabel(): string | null {
	const id = mixerState.headphones.selected_output_device_id;
	if (id === null) return null;
	return mixerState.headphones.outputs.find((o) => o.id === id)?.label ?? null;
}

/** Read-only verdict for the current mixer state; exported so a caller can ask
 * before it commits to a gesture (and so a test can drive it). */
export function previewCueReadiness(): PreviewCueVerdict {
	return previewCueVerdict({
		graph_built: peekCueBus() !== null,
		// `supported` is deliberately NOT consulted: it only means the I/O menu
		// has enumerated devices, and practice and split-cable previews need no
		// device selection at all. Gating on it made the feature unreachable on
		// any page where the operator had never opened I/O.
		active: mixerState.headphones.active,
		output_mode: mixerState.headphones.output_mode,
		cue_device_label: _cueDeviceLabel(),
		cue_path_gain: previewCuePathGain()
	});
}

/** The preview's own gain into the cue bus, built once per graph. */
function _ensureGain(): { gain: GainNode; context: AudioContext } | null {
	const bus = peekCueBus();
	if (bus === null) return null;
	if (_gain !== null && _gain.context === bus.context) {
		return { gain: _gain, context: bus.context };
	}
	const gain = bus.context.createGain();
	gain.gain.value = 1;
	// The ONLY connection this player makes. Nothing reaches the master bus
	// except through the cue routing the operator already chose.
	gain.connect(bus.cueSum);
	_gain = gain;
	return { gain, context: bus.context };
}

function _stopTicker(): void {
	if (_ticker === null) return;
	clearInterval(_ticker);
	_ticker = null;
}

function _stopSource(): void {
	if (_source === null) return;
	_source.onended = null;
	try {
		_source.stop();
	} catch {
		// Already stopped or never started; either way it is done with.
	}
	_source.disconnect();
	_source = null;
}

/** Resident cost of a decoded track, in bytes. */
function _bufferBytes(buffer: AudioBuffer): number {
	return buffer.length * buffer.numberOfChannels * Float32Array.BYTES_PER_ELEMENT;
}

function _cacheBytes(): number {
	let total = 0;
	for (const buffer of _cache.values()) total += _bufferBytes(buffer);
	return total;
}

/**
 * Drop least-recently-used tracks until the cache fits the budget.
 *
 * `_playingSid` is skipped unconditionally. Dropping a buffer under a running
 * `AudioBufferSourceNode` is the one eviction an operator could HEAR, and a
 * single track larger than the whole budget must still be allowed to play
 * rather than being evicted the instant it is decoded.
 */
function _evictToBudget(): void {
	const entries = [..._cache.entries()].map(([stable_id, buffer]) => ({
		stable_id,
		bytes: _bufferBytes(buffer)
	}));
	for (const sid of previewCacheEvictions(entries, previewCacheBudgetBytes(), _playingSid)) {
		_cache.delete(sid);
	}
}

/** Insert as most-recently-used, then evict down to the budget. */
function _cachePut(stable_id: string, buffer: AudioBuffer): void {
	_cache.delete(stable_id);
	_cache.set(stable_id, buffer);
	_evictToBudget();
}

/** Read and promote to most-recently-used, or null on a miss. */
function _cacheGet(stable_id: string): AudioBuffer | null {
	const buffer = _cache.get(stable_id);
	if (buffer === undefined) return null;
	_cache.delete(stable_id);
	_cache.set(stable_id, buffer);
	return buffer;
}

/** Release every decoded track. The gain node stays, it is graph furniture. */
function _releaseBuffers(): void {
	_cache.clear();
	_playingSid = null;
}

function _cancelIdleRelease(): void {
	if (_idleTimer === null) return;
	clearTimeout(_idleTimer);
	_idleTimer = null;
}

/**
 * Start the countdown to dropping the cache. Called on every transition to
 * not-playing, including a track simply reaching its end, which is the case
 * the previous revision never released on at all.
 */
function _armIdleRelease(): void {
	_cancelIdleRelease();
	_idleTimer = setTimeout(() => {
		_idleTimer = null;
		if (previewCue.playing) return;
		_releaseBuffers();
	}, PREVIEW_IDLE_RELEASE_MS);
}

/**
 * What the cache is holding right now: `{ tracks, bytes, budget_bytes }`.
 *
 * Exported so a test can assert the budget is enforced against REAL decoded
 * audio rather than against a re-implementation of the arithmetic, and so an
 * agent can read the residency it is causing.
 */
export function previewCacheStats(): { tracks: number; bytes: number; budget_bytes: number } {
	return { tracks: _cache.size, bytes: _cacheBytes(), budget_bytes: previewCacheBudgetBytes() };
}

/**
 * Re-run eviction against a budget that has just changed (tier resolved,
 * posture switched, pressure stepped). Registered with the caps registry, so
 * a cap change takes effect at once rather than on the next preview.
 */
function applyPreviewCaps(): void {
	_evictToBudget();
}
registerCapsConsumer('preview-pcm', applyPreviewCaps);
// A deck load of a track previewed or hover-warmed here reuses this decode.
// A plain get: loading a track onto a deck is not a preview hit, so it must not
// refresh the preview LRU.
provideDecodedAudio((stable_id) => _cache.get(stable_id) ?? null);

/**
 * The outcome of one preview request. It is RETURNED as well as toasted
 * because the toast is for the operator and the return value is for the
 * agent-native path: `preview_cue` over IPC/HTTP turns `ok: false` into a
 * failed command step, so a refused preview is a 400 carrying its own reason
 * rather than a 200 that started nothing.
 */
export interface PreviewCueOptions {
	/** The track's own BPM, for the R6 tempo match. The library row already
	 * holds it, so the click path pays no metadata request. Null = no match. */
	trackBpm?: number | null;
}

export type PreviewCueOutcome =
	| { ok: true; warning: string | null }
	| { ok: false; reason: string };

/** Callers own the toast (both already import the toast store), which keeps
 * this module off `stores.svelte.ts`'s fan-in allowance. */
function _refuse(reason: string): PreviewCueOutcome {
	return { ok: false, reason };
}

/** Refusal reason for a request a newer one replaced. Not operator-facing. */
export const PREVIEW_SUPERSEDED = 'superseded';

/**
 * Preview `stable_id` from `ratio` (0..1) of its length, through the cue bus.
 * Refuses loudly rather than starting something nobody can hear.
 */
export async function previewCueSeek(
	stable_id: string,
	ratio: number,
	options: PreviewCueOptions = {}
): Promise<PreviewCueOutcome> {
	// Build the graph FIRST: before any deck has ever been loaded there is no
	// AudioContext and therefore no cue bus, and "load a deck once first" is
	// not an acceptable precondition for browsing a library.
	try {
		await ensureAudioGraphForCue();
	} catch (exc) {
		const detail = exc instanceof Error ? exc.message : String(exc);
		return _refuse(`preview: the audio engine could not start (${detail})`);
	}
	const verdict = previewCueReadiness();
	if (!verdict.audible) return _refuse(verdict.refusal);
	const nodes = _ensureGain();
	if (nodes === null) {
		return _refuse('preview: the audio engine is not running - load a deck once to start it');
	}
	const { gain, context } = nodes;
	const generation = ++_generation;
	const r = Math.max(0, Math.min(1, ratio));
	previewCue.error = null;
	try {
		_cancelIdleRelease();
		let buffer = _cacheGet(stable_id);
		if (buffer === null) {
			// Not decoded yet. Stop what is sounding first: the old track stays
			// in the cache, but nothing should keep playing over the wait.
			_stopTicker();
			_stopSource();
			_playingSid = null;
			previewCue.playing = false;
			// Reuse the bytes the row-select prefetch may already hold, the
			// same way a deck load does. `copyPrefetchedAudio` slices because
			// `decodeAudioData` DETACHES the buffer it is given, which would
			// empty the shared cache entry for everyone else.
			const prefetched = copyPrefetchedAudio(stable_id);
			const bytes = prefetched ?? (await fetchAudioArrayBuffer(stable_id));
			if (generation !== _generation) return { ok: false, reason: PREVIEW_SUPERSEDED };
			const decoded = await context.decodeAudioData(bytes);
			if (generation !== _generation) return { ok: false, reason: PREVIEW_SUPERSEDED };
			_cachePut(stable_id, decoded);
			buffer = decoded;
		}
		_stopTicker();
		_stopSource();
		const source = context.createBufferSource();
		source.buffer = buffer;
		// CUEOUT-15 R6: pitch moves with tempo here, hence the bounded match.
		const rate = previewRateFor(options.trackBpm ?? null);
		source.playbackRate.value = rate;
		source.connect(gain);
		const offsetSec = r * buffer.duration;
		source.start(0, offsetSec);
		source.onended = () => {
			if (_source !== source) return;
			_stopTicker();
			previewCue.playing = false;
			_armIdleRelease();
		};
		_source = source;
		_startedAtSec = context.currentTime;
		_startedOffsetSec = offsetSec;
		_startedRate = rate;
		previewCue.stable_id = stable_id;
		previewCue.duration_ms = buffer.duration * 1000;
		previewCue.position_ms = offsetSec * 1000;
		previewCue.playing = true;
		previewCue.route = verdict.route;
		previewCue.rate = rate;
		_playingSid = stable_id;
		// Now that this track is pinned, re-check the budget: the eviction at
		// decode time could not drop whatever was playing a moment ago.
		_evictToBudget();
		_ticker = setInterval(() => {
			if (_source !== source) return;
			// Context time is wall clock; the track moves through itself at the
			// playback rate, so a tempo-matched preview would otherwise show a
			// playhead that drifts from what is sounding.
			const elapsed = (context.currentTime - _startedAtSec) * _startedRate;
			previewCue.position_ms = (_startedOffsetSec + elapsed) * 1000;
		}, POSITION_TICK_MS);
		return { ok: true, warning: verdict.warning };
	} catch (exc) {
		if (generation !== _generation) return { ok: false, reason: PREVIEW_SUPERSEDED };
		const detail = exc instanceof Error ? exc.message : String(exc);
		previewCue.error = detail;
		previewCue.playing = false;
		previewCue.stable_id = null;
		_stopTicker();
		_stopSource();
		_releaseBuffers();
		return _refuse(`preview failed: ${detail}`);
	}
}

/** Stop the preview and release its decoded track. The gain node stays. */
export function stopPreviewCue(): void {
	_generation += 1;
	_stopTicker();
	_stopSource();
	_releaseBuffers();
	_cancelIdleRelease();
	previewCue.stable_id = null;
	previewCue.playing = false;
	previewCue.rate = 1;
	previewCue.position_ms = 0;
	previewCue.duration_ms = null;
	previewCue.route = null;
}

/**
 * Dwell before a hover is treated as intent to preview.
 *
 * The lyrics hover load uses 500 ms for the same reason and the strip fires
 * `pointermove` continuously, so anything shorter turns a scroll past twenty
 * rows into twenty aborted multi-megabyte fetches: `ensureAudioPrefetch` is
 * newest-intent-wins, so each one cancels the last and none of them finish.
 */
const PREVIEW_WARM_DWELL_MS = 500;

const _warmTimers = new Map<string, ReturnType<typeof setTimeout>>();

/**
 * Warm a hovered track so a click is not a cold start.
 *
 * Warming only the bytes leaves decoding on the click path. Private
 * timing measurements are not included here. This warms in two stages:
 *
 * 1. Always: the bytes, through the row-select prefetch cache. It warms
 *    through that cache rather than one of its own, which is what keeps this
 *    honest under memory pressure: it is already byte-capped, posture-scaled
 *    and shed-aware, so a hover can never push the page past the budget the
 *    rest of the app agreed to.
 * 2. Only when an AudioContext ALREADY exists: the decode. `peekCueBus`
 *    returns null rather than building a graph, which is exactly the gate
 *    needed here, because a mouse passing over a row must never start the
 *    audio engine. In practice that means the first preview of a session pays
 *    the decode and every hovered one after it does not.
 *
 * The decode result lands in the same LRU as a played preview, so it is under
 * the same budget and the same idle release. A warm is never allowed to evict
 * the track that is sounding.
 */
export function previewWarmOnHover(stable_id: string): void {
	if (stable_id === '' || _warmTimers.has(stable_id)) return;
	if (_cache.has(stable_id)) return;
	_warmTimers.set(
		stable_id,
		setTimeout(() => {
			_warmTimers.delete(stable_id);
			ensureAudioPrefetch(stable_id);
			void _warmDecode(stable_id);
		}, PREVIEW_WARM_DWELL_MS)
	);
}

/**
 * Decode a hovered track into the cache, if and only if there is already an
 * audio context to decode with. Silent on every failure: a hover that cannot
 * be warmed is not an error the operator did anything to cause, and the click
 * path reports its own failures loudly with the real reason.
 */
async function _warmDecode(stable_id: string): Promise<void> {
	// Nobody asked for this work, so it is the first thing to give up under
	// pressure. SKIPPED rather than deferred: by the time signals clear the
	// operator has clicked or moved on, and owed speculative work would land
	// as pure cost on a machine that just told us it had none to spare.
	if (previewWarmIsShed()) return;
	const bus = peekCueBus();
	if (bus === null) return;
	if (_cache.has(stable_id)) return;
	try {
		const prefetched = copyPrefetchedAudio(stable_id);
		const bytes = prefetched ?? (await fetchAudioArrayBuffer(stable_id));
		// The operator may have clicked this very track while the warm was in
		// flight, in which case the click's own decode already cached it.
		if (_cache.has(stable_id)) return;
		_cachePut(stable_id, await bus.context.decodeAudioData(bytes));
	} catch {
		// Nothing to report: this was speculative work nobody asked for.
	}
}

/** Abandon a warm that never reached its dwell. Call on pointer leave. */
export function previewCancelWarm(stable_id: string): void {
	const timer = _warmTimers.get(stable_id);
	if (timer === undefined) return;
	clearTimeout(timer);
	_warmTimers.delete(stable_id);
}

/** Playhead ratio for `stable_id`, or null when it is not the preview. */
export function previewCueRatioFor(stable_id: string): number | null {
	if (previewCue.stable_id !== stable_id) return null;
	const dur = previewCue.duration_ms;
	if (dur === null || dur <= 0) return null;
	return Math.min(1, Math.max(0, previewCue.position_ms / dur));
}
