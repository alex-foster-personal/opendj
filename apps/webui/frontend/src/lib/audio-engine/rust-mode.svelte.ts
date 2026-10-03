/**
 * Rust engine mode: the performance page plays through `odj-audio` instead of
 * the in-page Web Audio engine (GSD phase 20, toward switch-over 20-07).
 *
 * Opt-in and off by default. Turn it on with `?engine=rust` (remembered in
 * this browser) and off with `?engine=webaudio`. `?engine_clock=wall` runs the
 * engine without an output device, for CI and headless boxes; the default is
 * `device`.
 *
 * This module is what every page loads: the choice, the command sets, and the
 * mode's reactive state. The engine code itself (`rust-engine.ts`,
 * `rust-transport.ts`, `rust-link.ts`, `client.ts`) is imported only once the
 * mode is on, so pages that never use it do not download it.
 *
 * How it fits without rebuilding the page:
 *
 * - Every performance control already goes through the typed dispatcher in
 *   `performance-ipc.svelte.ts`. In this mode `_execute` hands audio commands
 *   to `executeInRustEngine` first. The engine speaks the same command names in
 *   the same 0..1 units, so most commands are forwarded as they are.
 * - The UI keeps reading the same stores (`deckStates`, `mixerState`). This
 *   module writes them: track, waveform, cues and grid from the same fetches
 *   the Web Audio load uses; playhead, play state, tempo, cue and loop from the
 *   engine's state feed; knob positions from each acknowledged command.
 * - `load` goes through `POST /api/v1/audio-engine/load`, so the engine gets
 *   the file and the beatgrid the waveform draws (own or rekordbox, per the
 *   analysis-source selection).
 *
 * - Beat sync, master election, quantize, CUE and hot-cue triggers are
 *   DECISIONS the page already makes with pure functions over our own
 *   beatgrids (`rust-sync.ts` names them). This mode makes the same decisions
 *   and sends the engine the result: a tempo ratio, a seek, a play.
 *
 * What the Rust engine does not do yet is refused, never faked: its own
 * commands it has not built (keylock, stems, the headphone cue bus) come back
 * `not_implemented` naming the plan that brings them, the engine lists them in
 * its hello so the page grays those controls out (`rustCommandUnsupported`),
 * and page features that are bound to Web Audio internals throw
 * `RUST_ENGINE_UNAVAILABLE` naming the command. Everything else (library,
 * panels, feedback, hot-cue editing, preview) runs on the page as before.
 */

import type { PerformanceCommand } from '$lib/rb/performance-ipc.svelte';

export const ENGINE_PREF_KEY = 'odj.audioEngine';

/** Commands the engine plays as the page sends them. The ones this build has
 * not made yet come back `not_implemented` naming the plan that brings them,
 * and the engine lists them in its hello (`rustMode.notBuilt`). */
export const FORWARDED: ReadonlySet<string> = new Set([
	'unload',
	'loop',
	'beat_loop',
	'beat_jump',
	'pitch_range',
	'master_tempo',
	'key_nudge',
	'trim',
	'eq',
	'filter',
	'fader',
	'assign',
	'crossfader',
	'master_volume',
	'master_mute',
	// Engine commands for later plans (protocol.rs LATER).
	'key_sync',
	'stem_mute',
	'stem_solo',
	'stem_gain',
	'stem_eq_mode',
	'slip',
	'channel_cue',
	'headphone_mix',
	'headphone_level',
	'head_delay_ms',
	'output_mode',
	'headphone_output_select',
	'headphone_master_select'
]);

/** Commands whose decisions the page makes on its own beatgrids (the same pure
 * functions the Web Audio engine uses), sending the engine the outcome. */
export const PAGE_DECIDED: ReadonlySet<string> = new Set([
	'play',
	'cue',
	'seek',
	'tempo',
	'beat_sync',
	'sync_mode',
	'master',
	'quantize',
	'quantize_grid'
]);

/** Every command this mode takes from the page. Hot-cue save, clear, restore
 * and trigger stay the page's own and reach the engine through
 * `rustHotCueDriver`; preview plays on the page's own preview player. */
export const ENGINE_COMMANDS: ReadonlySet<string> = new Set(['load', ...FORWARDED, ...PAGE_DECIDED]);

/** Page features whose implementation reaches into the Web Audio engine's
 * own clock, buffers or devices. Refused by name in this mode. */
export const WEB_AUDIO_ONLY: ReadonlySet<string> = new Set([
	'load_play_intent',
	'loop_interval_mode',
	'loop_interval_base',
	'safety_loop_save',
	'safety_loop_arm',
	'safety_loop_clear',
	'stem_load',
	'headphone_outputs_refresh',
	'headphone_output_acquire',
	'headphone_input_select',
	'headphone_calibrate',
	'headphone_calibrate_abort',
	'headphone_alignment_mode',
	'master_delay_ms',
	'auto_play_two_track',
	'auto_play_next_arm',
	'auto_play_next_cancel',
	'rescue_resume',
	'rescue_stop_all',
	'pairing_snapshot_save'
]);

export type EngineChoice = 'webaudio' | 'rust';

export const rustMode = $state({
	enabled: false,
	clock: 'device' as 'device' | 'wall',
	status: 'off' as 'off' | 'starting' | 'connected' | 'error',
	error: null as string | null,
	/** What the connected engine build refuses (its hello). */
	notBuilt: [] as string[],
	/** Whether the engine reports key lock state (a build with the stretcher). */
	keyLock: false
});

/**
 * Whether a control that sends `type` does nothing in this mode, so the UI
 * renders it inert with the PARITY-TODO title instead of failing on press.
 * False whenever the Rust engine mode is off.
 */
export function rustCommandUnsupported(type: string): boolean {
	initRustMode();
	if (!rustMode.enabled) return false;
	if (WEB_AUDIO_ONLY.has(type)) return true;
	if (type === 'master_tempo') return !rustMode.keyLock;
	return FORWARDED.has(type) && rustMode.notBuilt.includes(type);
}

/** Read the choice from `?engine=` (remembered) or the remembered value. */
export function readEngineChoice(search: string, stored: string | null): EngineChoice {
	const q = new URLSearchParams(search).get('engine');
	const v = q ?? stored;
	if (v === null || v === 'webaudio') return 'webaudio';
	if (v === 'rust') return 'rust';
	throw new Error(`engine=${v} is not one of webaudio, rust`);
}

function _stored(): string | null {
	try {
		return window.localStorage?.getItem(ENGINE_PREF_KEY) ?? null;
	} catch {
		// Storage can be blocked; the URL still works.
		return null;
	}
}

/** The engine the performance page uses on its next load (the setting). */
export function storedEngineChoice(): EngineChoice {
	if (typeof window === 'undefined') return 'webaudio';
	return readEngineChoice('', _stored());
}

/** Choose the engine for the performance page's next load. A page already
 * playing keeps its engine: switching mid-set would cut the audio. */
export function setStoredEngineChoice(choice: EngineChoice): void {
	if (choice !== 'webaudio' && choice !== 'rust') {
		throw new Error(`audio_engine must be webaudio|rust, got ${String(choice)}`);
	}
	window.localStorage.setItem(ENGINE_PREF_KEY, choice);
}

let initialized = false;

/** Read the choice for this page. Runs once; later calls are no-ops. */
export function initRustMode(): void {
	if (initialized || typeof window === 'undefined') return;
	initialized = true;
	const stored = _stored();
	// Test stand-ins for window often carry no location.
	const search = window.location?.search ?? '';
	const params = new URLSearchParams(search);
	const choice = readEngineChoice(search, stored);
	if (params.has('engine')) {
		try {
			window.localStorage?.setItem(ENGINE_PREF_KEY, choice);
		} catch {
			// Not remembered; this page still uses the choice.
		}
	}
	const clock = params.get('engine_clock');
	if (clock !== null && clock !== 'wall' && clock !== 'device') {
		throw new Error(`engine_clock=${clock} is not one of wall, device`);
	}
	rustMode.clock = clock ?? 'device';
	rustMode.enabled = choice === 'rust';
}

/** How the dispatcher toasts; the mode's code keeps it for timers too. */
type RustToast = (message: string, kind: 'info' | 'error') => void;

let loading: Promise<typeof import('./rust-engine')> | null = null;

/** Load the mode's engine code (once) and let it take the hot-cue seam. */
async function loadRustEngineMode(
	toast: RustToast | null = null
): Promise<typeof import('./rust-engine')> {
	loading ??= import('./rust-engine');
	const engine = await loading;
	engine.activateRustEngineMode(toast);
	return engine;
}

/** Connect (starting the engine if needed). Reused while the socket lives. */
export async function ensureRustEngine(): Promise<void> {
	let engine: typeof import('./rust-engine');
	try {
		engine = await loadRustEngineMode();
	} catch (e) {
		// The mode's code did not load (offline dev server, a stale build), so
		// the badge says why rather than sitting on "off".
		loading = null;
		rustMode.status = 'error';
		rustMode.error = `the Rust engine mode code did not load: ${e instanceof Error ? e.message : String(e)}`;
		throw e;
	}
	await engine.connectRustEngine();
}

/**
 * Run `command` on the Rust engine when this mode owns it.
 * Returns false when the command is the page's own (the caller runs it as usual).
 */
export async function executeInRustEngine(
	command: PerformanceCommand,
	toast: RustToast | null = null
): Promise<boolean> {
	initRustMode();
	if (!rustMode.enabled) return false;
	const type = command.type;
	if (WEB_AUDIO_ONLY.has(type)) {
		throw new Error(
			`RUST_ENGINE_UNAVAILABLE: ${type} is not available with the Rust engine yet - see PARITY-TODO`
		);
	}
	// Loaded before the page runs its own commands too: hot-cue triggers are
	// the page's, and must reach this mode's driver, not the Web Audio one.
	const engine = await loadRustEngineMode(toast);
	if (!ENGINE_COMMANDS.has(type)) return false;
	await engine.executeRustCommand(command);
	return true;
}
