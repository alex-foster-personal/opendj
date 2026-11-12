/**
 * Practice drill state machine (#10251): reach one target control, count other presses as
 * misses. Pure: no DOM, no stores, no runes, so /practice and the performance view share it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ start/restart begin timing with reach and misses cleared.
 *     [if] restart leaves misses or reachMs from the last run [then] ⛔️
 *   ✔︎ The first target press records reachMs; other presses before it are misses.
 *     [if] a target hit 1200 ms after start, after two other presses, is not reach 1200 misses 2 [then] ⛔️
 *   ✔︎ Presses after the hit, before start, or with no target change nothing.
 *     [if] a press after the hit changes misses or reachMs [then] ⛔️
 */

export interface DrillState {
	readonly target: string | null;
	readonly reachMs: number | null;
	readonly misses: number;
	/** null until start(): no timer runs, so presses are ignored. */
	readonly startedMs: number | null;
}

export interface Drill {
	readonly state: DrillState;
	start(nowMs: number): DrillState;
	press(controlId: string, nowMs: number): DrillState;
	restart(nowMs: number): DrillState;
}

export function createDrill(target: string | null): Drill {
	let state: DrillState = Object.freeze({ target, reachMs: null, misses: 0, startedMs: null });
	const start = (nowMs: number): DrillState => {
		state = Object.freeze({ target, reachMs: null, misses: 0, startedMs: nowMs });
		return state;
	};
	const press = (controlId: string, nowMs: number): DrillState => {
		if (target === null || state.startedMs === null || state.reachMs !== null) return state;
		state =
			controlId === target
				? Object.freeze({ ...state, reachMs: Math.round(nowMs - state.startedMs) })
				: Object.freeze({ ...state, misses: state.misses + 1 });
		return state;
	};
	return {
		get state() {
			return state;
		},
		start,
		press,
		restart: start
	};
}
