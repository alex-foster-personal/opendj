/**
 * OJ-13717: transition presets on the performance bus.
 *
 * `transition_preset {preset}` calls the same `runTransitionPreset` that the
 * `preset.run.<id>` registry command (hotkey, Cmd-K) calls, so `opendj
 * transition_preset <id>`, MCP `command` and OJ's `command` tool all reach one
 * runner. The host reads decks from the performance
 * query and writes through the dispatcher (never setKnobAbsolute, which is silent
 * on an unmounted mixer); the dispatcher passes both in, so this module does not
 * import it back.
 *
 * OJ-14133: the performance query's `transition_preset` field reads the
 * runner's active set, so a run started by hotkey shows too; a run this module
 * started also carries its start time and progress.
 */

import {
	abortTransitionPreset,
	activeTransitionPresetIds,
	runTransitionPreset,
	TRANSITION_PRESETS,
	type PresetDecksInput,
	type PresetPlan,
	type PresetWrite,
	type TransitionPresetHost,
	type TransitionPresetId
} from '$lib/rb/transition-presets';
import { frameOrTimeout } from '$lib/rb/frame-backstop';
import type { PerformanceState, TransitionPresetRunSnapshot } from '$lib/rb/performance-ipc-types';

export interface TransitionPresetBusDeps {
	query: () => PerformanceState;
	dispatch: (command: PresetWrite) => Promise<unknown>;
}

/** The host's clock: one tick per animation frame, with a timer backstop for a hidden page. */
export type TransitionPresetClock = () => Promise<void>;

export const TRANSITION_PRESET_IDS: readonly TransitionPresetId[] = TRANSITION_PRESETS.map(
	(p) => p.id
);

interface BusRun {
	id: TransitionPresetId;
	startedAtMs: number;
	plan: PresetPlan;
	progress: number;
}

/** The last run the bus started; cleared when that run completes or aborts. */
let _busRun: BusRun | null = null;

//-----------------------------------------------------------------------------
// wire
//-----------------------------------------------------------------------------

/** Accept a known preset id from the wire, else throw naming every allowed id. */
export function parseTransitionPresetId(raw: unknown): TransitionPresetId {
	if (typeof raw === 'string' && TRANSITION_PRESET_IDS.includes(raw)) return raw;
	throw new Error(
		`transition_preset: unknown preset ${JSON.stringify(raw)}; allowed: ${TRANSITION_PRESET_IDS.join(', ')}`
	);
}

//-----------------------------------------------------------------------------
// host
//-----------------------------------------------------------------------------

/** The preset runner's deck snapshot, read off one performance query. */
export function presetDecksFromState(state: PerformanceState): PresetDecksInput {
	return {
		decks: Object.values(state.decks).map((deck) => {
			const channel = state.mixer.channels[deck.deck_id];
			return {
				id: deck.deck_id,
				loaded: deck.stable_id !== null,
				playing: deck.playing,
				isMaster: deck.is_master,
				position_ms: deck.position_ms,
				beatgrid: deck.beatgrid,
				controls: {
					low: channel.eq_low,
					mid: channel.eq_mid,
					high: channel.eq_high,
					filter: channel.filter,
					fader: channel.fader
				}
			};
		})
	};
}

/** Production preset host for one run: query in, dispatcher out, one tick per frame. */
export function busTransitionPresetHost(
	id: TransitionPresetId,
	deps: TransitionPresetBusDeps,
	nextFrame: TransitionPresetClock
): TransitionPresetHost {
	return {
		readDecks: () => {
			const input = presetDecksFromState(deps.query());
			_noteBusRunPosition(id, input);
			return input;
		},
		// The dispatcher persists its own failure; a refused write stops the run
		// rather than letting the rest of the preset keep moving controls.
		write: (command) => {
			deps.dispatch(command).catch(() => abortTransitionPreset(id));
		},
		schedule: (tick) => {
			let live = true;
			void nextFrame().then(() => {
				if (live) tick();
			});
			return () => {
				live = false;
			};
		}
	};
}

//-----------------------------------------------------------------------------
// progress
//-----------------------------------------------------------------------------

function _progressAt(plan: PresetPlan, positionMs: number): number {
	const span = plan.endMs - plan.startMs;
	if (span <= 0) return 1;
	return Math.min(1, Math.max(0, (positionMs - plan.startMs) / span));
}

function _noteBusRunPosition(id: TransitionPresetId, input: PresetDecksInput): void {
	const run = _busRun;
	if (run === null || run.id !== id) return;
	const clock = input.decks.find((deck) => deck.id === run.plan.clockDeck);
	if (clock !== undefined) run.progress = _progressAt(run.plan, clock.position_ms);
}

/** The query's `transition_preset` field: the newest active run, with timing when the bus started it. */
export function transitionPresetSnapshot(): TransitionPresetRunSnapshot {
	const active = activeTransitionPresetIds();
	const running = active[active.length - 1] as TransitionPresetId | undefined;
	if (running === undefined) return { running: null, started_at_ms: null, progress: null };
	const run = _busRun?.id === running ? _busRun : null;
	return {
		running,
		started_at_ms: run?.startedAtMs ?? null,
		progress: run?.progress ?? null
	};
}

//-----------------------------------------------------------------------------
// run
//-----------------------------------------------------------------------------

/** Run (or, while it runs, abort) a preset through the runner its `preset.run.<id>` hotkey calls. */
export function runTransitionPresetFromBus(
	id: TransitionPresetId,
	deps: TransitionPresetBusDeps
): void {
	const result = runTransitionPreset(id, busTransitionPresetHost(id, deps, frameOrTimeout));
	if (result.status !== 'started') return;
	const run: BusRun = { id, startedAtMs: Date.now(), plan: result.plan, progress: 0 };
	_busRun = run;
	void result.done.then(() => {
		if (_busRun === run) _busRun = null;
	});
}
