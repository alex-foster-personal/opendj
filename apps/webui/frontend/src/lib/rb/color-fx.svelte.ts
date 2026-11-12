/**
 * Color FX (COLORFX-01..05), live state: which effect each channel's color knob
 * plays, the echo division, the tempo the echo is timed to, and the splice of
 * the SPACE / ECHO nodes into a channel the first time it needs them.
 *
 * The knob itself is still the `filter` command (`AudioEngine.setFilter`), which
 * calls `applyColorKnob` here; the `color_fx` command changes the effect or the
 * echo division (and may carry a knob `value`), reaching
 * `applyColorFxSettings` through the typed dispatcher, so the channel strip's
 * selector, HTTP (`POST /api/v1/commands`) and `opendj color_fx` drive one path.
 *
 * Selection is per channel and sticky across track loads. FILTER (the default)
 * never builds the SPACE / ECHO nodes, so a channel that never leaves FILTER
 * renders exactly as before plus one unity gain (the color bus).
 *
 * CRUSH (SNDCOL-10655) loads its AudioWorklet module once per AudioContext, the
 * first time a channel selects it. Until the module is ready (or if it fails)
 * the channel plays dry, FILTER-bypassed, and the view's `inert_reason` says why.
 */
import { DECK_IDS, type DeckId } from '$lib/player/constants';
import { deckEffectiveBpm } from '$lib/player/state.svelte';
import {
	COLOR_FX_LABELS,
	COLOR_FX_MAX_BPM,
	COLOR_FX_MIN_BPM,
	DEFAULT_COLOR_FX_SETTINGS,
	colorFxTargets,
	echoDelaySeconds,
	type ColorFxCommand,
	type ColorFxEffect,
	type ColorFxSettings
} from './color-fx';
import {
	CRUSH_PARAMETER_NOMINAL,
	crushLoadState,
	loadCrushWorklet,
	type CrushGraph
} from './color-fx-crush';
import { pushToast } from '$lib/stores.svelte';
import crushProcessorUrl from './color-fx-crush-processor.js?url';
import {
	detachIdleSpace,
	driveColorChannel,
	spliceCrush,
	spaceDetachDelaySec,
	type ColorChannelNodes,
	type ColorChannelRuntime,
	type ColorFxGraph
} from './color-fx-graph';

/** How often a channel on ECHO re-reads its deck tempo. Polled, not reactive:
 * the effective BPM is derived from the playhead (see beat-fx.svelte.ts). */
const TEMPO_POLL_MS = 250;
const TEMPO_EPSILON_BPM = 0.05;

export const ECHO_NO_TEMPO_REASON =
	'echo is timed in beats and this deck has no tempo: load a track with a BPM or beatgrid';

export const CRUSH_LOADING_REASON = 'crush is loading its audio worklet; the channel plays dry until it is ready';
export const CRUSH_UNAVAILABLE = 'CRUSH_UNAVAILABLE';

/** CRUSH's PARAMETER, fixed at the calibrated curve until #9796 brings the channel's `param`. */
const CRUSH_PARAMETER = CRUSH_PARAMETER_NOMINAL;

export interface ColorFxDeckView extends ColorFxSettings {
	/** Whether this channel's SPACE / ECHO nodes are in the current graph. */
	spliced: boolean;
	/** Whether the SPACE convolver is attached (it detaches when idle, COLORFX-08). */
	space_attached: boolean;
	/** The tempo the echo is timed to, or null with no tempo. */
	tempo_bpm: number | null;
	/** The echo line in ms, or null when the echo has no tempo. */
	echo_ms: number | null;
	/** Why the selected effect does nothing right now, or null. */
	inert_reason: string | null;
}

export const colorFx = $state<Record<DeckId, ColorFxSettings & { tempo_bpm: number | null }>>(
	Object.fromEntries(DECK_IDS.map((deck) => [deck, { ...DEFAULT_COLOR_FX_SETTINGS, tempo_bpm: null }])) as Record<
		DeckId,
		ColorFxSettings & { tempo_bpm: number | null }
	>
);

/** Per channel: the nodes last driven, the knob value, the spliced graph and its idle clock. */
interface ChannelRuntime extends ColorChannelRuntime {
	value: number;
	/** Whether a splice is already waiting on this context's CRUSH load. */
	crushPending: boolean;
}
const _runtime = new Map<DeckId, ChannelRuntime>();
let _tempoTimer: ReturnType<typeof setInterval> | null = null;

function _deckTempo(deck: DeckId): number | null {
	const bpm = deckEffectiveBpm(deck);
	return bpm !== null && bpm >= COLOR_FX_MIN_BPM && bpm <= COLOR_FX_MAX_BPM ? bpm : null;
}

const _detachTimers = new Map<DeckId, ReturnType<typeof setTimeout>>();

/** On CRUSH: load the worklet (once per context) and splice when it is ready. */
function _ensureCrush(deck: DeckId, rt: ChannelRuntime): void {
	// A failed node is never rebuilt on the same context.
	if (colorFx[deck].effect !== 'crush' || rt.crush !== null) return;
	const load = loadCrushWorklet(rt.nodes.colorBus.context, crushProcessorUrl);
	if (load.state === 'ready') spliceCrush(rt, undefined, (graph) => _crushProcessorFailed(deck, rt, graph));
	else if (load.state === 'loading' && !rt.crushPending) {
		rt.crushPending = true;
		void load.promise.then(() => {
			rt.crushPending = false;
			if (_runtime.get(deck) === rt && load.state === 'ready') _drive(deck, rt);
		});
	}
}

function _crushFailureReason(message: string): string {
	return `${CRUSH_UNAVAILABLE}: the crush audio worklet stopped with a processor error: ${message}`;
}

/** Terminal processorerror: log, toast, and re-drive so the channel plays dry at once. */
function _crushProcessorFailed(deck: DeckId, rt: ChannelRuntime, graph: CrushGraph): void {
	const reason = _crushFailureReason(graph.failure ?? '');
	console.error(`[color-fx] ${reason}`);
	pushToast(`Deck ${deck}: ${reason}`, 'error');
	if (_runtime.get(deck) === rt) _drive(deck, rt);
}

function _drive(deck: DeckId, rt: ChannelRuntime): void {
	const settings = colorFx[deck];
	_ensureCrush(deck, rt);
	driveColorChannel(rt, settings, settings.tempo_bpm, rt.value, CRUSH_PARAMETER);
	_scheduleSpaceDetach(deck, rt);
}

/** One timer per channel, armed while an attached SPACE has a closed send. It
 * re-checks against the audio clock, so a suspended context never detaches early. */
function _scheduleSpaceDetach(deck: DeckId, rt: ChannelRuntime): void {
	const pending = _detachTimers.get(deck);
	if (pending !== undefined) clearTimeout(pending);
	_detachTimers.delete(deck);
	if (rt.graph === null || rt.graph.space === null || rt.spaceQuietSince === null) return;
	const ctx = rt.nodes.colorBus.context;
	const waitSec = Math.max(0, rt.spaceQuietSince + spaceDetachDelaySec() - ctx.currentTime);
	const timer = setTimeout(() => {
		_detachTimers.delete(deck);
		if (_runtime.get(deck) !== rt) return;
		if (!detachIdleSpace(rt, ctx.currentTime)) _scheduleSpaceDetach(deck, rt);
	}, waitSec * 1000 + 50);
	_detachTimers.set(deck, timer);
}

/**
 * COLORFX-09: in Rust engine mode the engine has only the FILTER, so a knob on
 * a SPACE or ECHO channel must be refused visibly, never sent there as a
 * filter move. Returns the refusal, or null when the knob may go to the engine.
 */
export function colorFxRustRefusal(deck: DeckId): string | null {
	const effect = colorFx[deck].effect;
	if (effect === 'filter') return null;
	return `RUST_ENGINE_UNAVAILABLE: deck ${deck}'s color knob is on ${COLOR_FX_LABELS[effect]}, which plays in Web Audio only, so the Rust engine would filter instead; the selection returns to FILTER when the page reloads - see PARITY-TODO`;
}

/**
 * The color knob moved (the `filter` command). Drives the FILTER nodes, or the
 * SPACE / ECHO sends, per this channel's selected effect.
 */
export type { ColorChannelNodes } from './color-fx-graph';

export function applyColorKnob(deck: DeckId, nodes: ColorChannelNodes, value: number): void {
	const current = _runtime.get(deck);
	const rt: ChannelRuntime =
		current !== undefined && current.nodes === nodes ? current : { nodes, value, graph: null, crush: null, crushPending: false, spaceQuietSince: null };
	rt.value = value;
	_runtime.set(deck, rt);
	if (colorFx[deck].effect === 'echo') colorFx[deck].tempo_bpm = _deckTempo(deck) ?? colorFx[deck].tempo_bpm;
	_drive(deck, rt);
	_syncTempoPoll();
}

/**
 * A channel graph was (re)built: a new context, so the old SPACE / ECHO nodes
 * went with the old one. Re-splice at once when the channel is not on FILTER.
 */
export function colorFxChannelBuilt(deck: DeckId, nodes: ColorChannelNodes, value: number): void {
	_runtime.delete(deck);
	if (colorFx[deck].effect !== 'filter') applyColorKnob(deck, nodes, value);
}

/** The FILTER part of the targets for a channel being built, so a rebuilt
 * channel on SPACE / ECHO starts bypassed rather than as FILTER at its value. */
export function colorFilterParamsForBuild(deck: DeckId, value: number): ReturnType<typeof colorFxTargets>['filter'] {
	return colorFxTargets(colorFx[deck].effect, value, false).filter;
}

/** A `color_fx` command after `planColorFxSwitch`: a concrete effect, and the
 * knob position the switch rule chose (COLORFX-8341). */
export interface ColorFxSettingsUpdate {
	deck: DeckId;
	effect?: ColorFxEffect;
	echo_beats?: ColorFxCommand['echo_beats'];
	value?: number;
}

/**
 * Execute a resolved `color_fx` command's settings half (effect, echo
 * division). The knob half (`value`) is the dispatcher's `setFilter`, which
 * then drives the channel with these settings; `value` is applied here too, so
 * a switch drives the new effect at the chosen position in the same ramp
 * rather than briefly at the depth the old effect was left at.
 */
export function applyColorFxSettings(update: ColorFxSettingsUpdate): ColorFxDeckView {
	const settings = colorFx[update.deck];
	if (update.effect !== undefined) settings.effect = update.effect;
	if (update.echo_beats !== undefined) settings.echo_beats = update.echo_beats;
	settings.tempo_bpm = settings.effect === 'echo' ? _deckTempo(update.deck) : null;
	const rt = _runtime.get(update.deck);
	if (rt !== undefined) {
		if (update.value !== undefined) rt.value = update.value;
		_drive(update.deck, rt);
	}
	_syncTempoPoll();
	return colorFxView()[update.deck];
}

/** Acknowledged settings and knob depth, without copying graph nodes. */
export interface ColorFxSnapshot {
	settings: ColorFxSettings & { tempo_bpm: number | null };
	value: number;
}

export function snapshotColorFx(deck: DeckId, acknowledgedValue: number): ColorFxSnapshot {
	return { settings: { ...colorFx[deck] }, value: _runtime.get(deck)?.value ?? acknowledgedValue };
}

/** Restore the affected channel and its timer ownership after a failed command. */
export function restoreColorFx(deck: DeckId, snapshot: ColorFxSnapshot): void {
	Object.assign(colorFx[deck], snapshot.settings);
	const rt = _runtime.get(deck);
	try {
		if (rt !== undefined) {
			rt.value = snapshot.value;
			_drive(deck, rt);
		}
	} finally {
		_syncTempoPoll();
	}
}

function _crushInertReason(deck: DeckId): string | null {
	const rt = _runtime.get(deck);
	if (rt === undefined) return null;
	if (rt.crush !== null) return rt.crush.failure === null ? null : _crushFailureReason(rt.crush.failure);
	const load = crushLoadState(rt.nodes.colorBus.context);
	if (load === null) return null;
	if (load.state === 'failed') return `${CRUSH_UNAVAILABLE}: the crush audio worklet failed to load: ${load.error}`;
	return CRUSH_LOADING_REASON;
}

function _deckView(deck: DeckId): ColorFxDeckView {
	const settings = colorFx[deck];
	const echoSeconds = echoDelaySeconds(settings.echo_beats, settings.tempo_bpm);
	const echo = settings.effect === 'echo';
	return {
		effect: settings.effect,
		echo_beats: settings.echo_beats,
		spliced: (_runtime.get(deck)?.graph ?? null) !== null,
		space_attached: (_runtime.get(deck)?.graph?.space ?? null) !== null,
		tempo_bpm: echo ? settings.tempo_bpm : null,
		echo_ms: echo && echoSeconds !== null ? Math.round(echoSeconds * 100000) / 100 : null,
		inert_reason:
			echo && echoSeconds === null
				? ECHO_NO_TEMPO_REASON
				: settings.effect === 'crush'
					? _crushInertReason(deck)
					: null
	};
}

/** Per-deck view for the ui-mirror and the channel strip. */
export function colorFxView(): Record<DeckId, ColorFxDeckView> {
	return Object.fromEntries(DECK_IDS.map((deck) => [deck, _deckView(deck)])) as Record<DeckId, ColorFxDeckView>;
}

function _tick(): void {
	for (const deck of DECK_IDS) {
		const settings = colorFx[deck];
		if (settings.effect !== 'echo') continue;
		const tempo = _deckTempo(deck);
		// The tempo went away mid-echo (a deck unload): keep the last timing so the
		// tail rings out at the time it was played, rather than guessing a new one.
		if (tempo === null) continue;
		if (settings.tempo_bpm !== null && Math.abs(settings.tempo_bpm - tempo) < TEMPO_EPSILON_BPM) continue;
		settings.tempo_bpm = tempo;
		const rt = _runtime.get(deck);
		if (rt !== undefined) _drive(deck, rt);
	}
}

function _syncTempoPoll(): void {
	const want = DECK_IDS.some((deck) => colorFx[deck].effect === 'echo');
	if (want && _tempoTimer === null) _tempoTimer = setInterval(_tick, TEMPO_POLL_MS);
	if (!want && _tempoTimer !== null) {
		clearInterval(_tempoTimer);
		_tempoTimer = null;
	}
}

/** Test seams. */
export function _tickColorFxTempoForTests(): void {
	_tick();
}
export function _detachIdleSpaceForTests(deck: DeckId): boolean {
	const rt = _runtime.get(deck);
	return rt !== undefined && detachIdleSpace(rt, rt.nodes.colorBus.context.currentTime);
}
export function _colorFxGraphForTests(deck: DeckId): ColorFxGraph | null {
	return _runtime.get(deck)?.graph ?? null;
}
export function _crushGraphForTests(deck: DeckId): CrushGraph | null {
	return _runtime.get(deck)?.crush ?? null;
}
export function _resetColorFxForTests(): void {
	if (_tempoTimer !== null) clearInterval(_tempoTimer);
	_tempoTimer = null;
	for (const timer of _detachTimers.values()) clearTimeout(timer);
	_detachTimers.clear();
	_runtime.clear();
	for (const deck of DECK_IDS) Object.assign(colorFx[deck], DEFAULT_COLOR_FX_SETTINGS, { tempo_bpm: null });
}
