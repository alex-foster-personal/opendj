/**
 * CMDK-08: the Cmd K headphone preview of the highlighted track.
 *
 * The first ← or → starts the track in the headphone cue at bar 1, in phase
 * with the master; each press after that moves it one step on the jump grid
 * (`cmdk-preview-grid.ts`). Playback goes through the typed dispatcher's
 * `preview_cue` and `preview_stop` commands, the same ones an agent sends, so
 * the bar has no audio path of its own. The ports are injected so the unit
 * suite drives this with a recording dispatcher and a fixed master phase.
 */
import {
	PREVIEW_BIG_STEP_BARS,
	PREVIEW_STEP_BARS,
	inPhaseStartSec,
	jumpGrid,
	jumpTarget,
	latestOnly,
	type SnapStop
} from './cmdk-preview-grid';

export type CuePreviewCommand =
	| { type: 'preview_cue'; stable_id: string; ratio: number; bpm?: number }
	| { type: 'preview_stop' };

export interface CuePreviewPorts {
	/** Sends one command through the dispatcher; false when it was refused (the dispatcher reports why). */
	dispatch: (command: CuePreviewCommand) => Promise<boolean>;
	/**
	 * How far the master deck is into its bar (0 up to 1); null with no master playing (the
	 * preview starts on the downbeat); 'unknown' when a master is playing but its phase cannot
	 * be read (no beat grid at its position), and then the preview does not start out of phase.
	 */
	masterPhase: () => number | null | 'unknown';
	/** True while the preview voice is sounding this track. */
	sounding: (stable_id: string) => boolean;
	now: () => number;
}

/** What a jump needs to know about the highlighted track. */
export interface CuePreviewTrack {
	stable_id: string;
	/** Downbeat times in seconds; empty means no beat grid yet. */
	downbeats: readonly number[];
	stops: readonly SnapStop[];
	duration_s: number | null;
	bpm: number | null;
	/** True while the track's beat grid is still being read (its /anlz has not answered yet). */
	gridPending: boolean;
	/** The analysis says its bar numbers are untrusted, so the grid cannot be jumped by bars (Sol P1, #6687). */
	untrusted?: boolean;
}

/** Shown while a press waits for the highlighted track's beat grid (Codex r4280683906). */
export const GRID_LOADING = 'Loading the beat grid';
/** Shown when the analysis marks the grid's bar numbers as untrusted: the bars are not derived from it. */
export const BAR_PHASE_UNTRUSTED = "The beat grid's bar numbers are untrusted, so the preview cannot jump by bars";

/** Shown when the dispatcher refuses a start: the bar keeps where it was (Sol P2, #6687). */
export const PREVIEW_REFUSED = 'The preview was refused, so the bar stays where it was';

/** Shown when a master is playing but its phase cannot be read (Sol r4280385227). */
export const MASTER_PHASE_UNKNOWN = "The master's beat grid is not readable where it is playing, so the preview cannot start in phase";

/** A start that took longer than this (a cold decode) is restarted once from the cache, in phase. */
export const COLD_START_RESEEK_MS = 40;

export function createCuePreview(ports: CuePreviewPorts) {
	const state = $state({
		/** The track the preview belongs to, null when none is set. */
		stable_id: null as string | null,
		/** The grid bar the last jump landed on. */
		anchor: 0,
		/** True between a start this controller sent and its stop. */
		started: false,
		/** One line for the strip: where it landed, or why it cannot play. */
		message: ''
	});
	let track: CuePreviewTrack | null = null;
	/** Bumped by every jump and every stop, so a slow start does not reseek over a newer ask. */
	let ask = 0;
	/** Starts sent and not yet answered: a cold decode is not sounding yet but is under way. */
	let inFlight = 0;
	/**
	 * The ask a stop bumped to, last. A start sent before it (its ask is lower) and answered ok after
	 * it is stopped on return, even when a newer jump has started since: that jump runs only after
	 * this one returns, so the stop cannot reach it, and a refused newer jump would otherwise leave
	 * the old start sounding with nothing owning it (Sol r4280385212).
	 */
	let stoppedAt = 0;
	/**
	 * True once a start this controller sent has landed and no stop has ended it. Only that source
	 * is owned: a jump queued behind a first start still in flight owns nothing, so its refusal
	 * must not send a stop that could hit another client's newer preview (Sol P1, #6687).
	 */
	let sourceLanded = false;
	/**
	 * A press made while the track's beat grid was still being read: it is held, not dropped,
	 * and replayed once the grid arrives for the same track (Codex r4280683906). A track
	 * change or a stop drops it, and so does a grid read that ends with no grid.
	 */
	let held: { stable_id: string; dir: 1 | -1; big: boolean } | null = null;
	/**
	 * The anchors a forward jump left, newest last. ← retraces them, so a reverse jump lands where
	 * the press came from even when Shift changed the grid size between the two (Sol r4285715528).
	 * Cleared with the anchor on a fresh start and on a track change.
	 */
	let history: number[] = [];

	/** The bar as it stood before a jump: a refused jump puts it back (Sol P2, #6687). Null for a fresh start. */
	type JumpRestore = { anchor: number; history: number[]; message: string; landed: string } | null;

	const seek = latestOnly(async (job: { stable_id: string; bar: number; ask: number; fresh: boolean; restore: JumpRestore; owned: boolean }) => {
		const current = track;
		if (job.ask !== ask) return;
		if (current === null || current.stable_id !== job.stable_id || current.duration_s === null) return;
		const send = async (): Promise<boolean> => {
			// Every read here is of the live track, not the one the job began with: the grid, the length and the bpm
			// of this same track can be replaced while the job waits (Codex P2 BLOCKING, #6691). A shorter or empty
			// grid no longer has the bar, so say so and play nothing rather than throw (Sol P1, #6691).
			const live = track;
			if (live === null || live.stable_id !== current.stable_id || live.duration_s === null) {
				if (job.ask === ask) state.message = 'The beat grid changed while this jump waited';
				return false;
			}
			if (live.downbeats.length === 0) {
				if (job.ask === ask) state.message = 'No beat grid yet, so it cannot jump by bars';
				return false;
			}
			if (job.bar >= live.downbeats.length) {
				if (job.ask === ask) state.message = 'The beat grid changed while this jump waited';
				return false;
			}
			const phase = ports.masterPhase();
			if (phase === 'unknown') {
				if (job.ask === ask) state.message = MASTER_PHASE_UNKNOWN;
				return false;
			}
			const start = inPhaseStartSec(live.downbeats, job.bar, phase ?? 0);
			const ratio = Math.max(0, Math.min(1, start / live.duration_s));
			inFlight += 1;
			try {
				const sent = await ports.dispatch({
					type: 'preview_cue',
					stable_id: live.stable_id,
					ratio,
					...(live.bpm !== null && live.bpm > 0 ? { bpm: live.bpm } : {})
				});
				// The refusal's reason stays on the strip: a refused jump does not overwrite it (Sol P2, #6687).
				if (!sent && job.ask === ask) state.message = PREVIEW_REFUSED;
				return sent;
			} finally {
				inFlight -= 1;
			}
		};
		/** After a start returns: false when a stop or a newer jump came in meanwhile. */
		const stillWanted = async (started: boolean): Promise<boolean> => {
			if (job.ask === ask) return true;
			// Stopped while this start was in flight: a stop sent then would have cancelled
			// the decode as a failure, so the stop is sent now, but only for a source this controller
			// owns: its own start landed, or it was already playing its preview before this jump
			// (a refused seek can leave that one sounding, Sol r4285715521). A stop for another
			// client's newer preview is never sent (Sol P2, #6687).
			if (stoppedAt > job.ask && (started || job.owned)) await ports.dispatch({ type: 'preview_stop' });
			return false;
		};
		const t0 = ports.now();
		const ok = await send();
		if (ok) sourceLanded = true;
		if (!(await stillWanted(ok))) return;
		// A refused first start owns nothing. A refused later jump can leave the preview that was
		// already sounding in place (the dispatcher returns before replacing it), so the controller
		// keeps owning it and Esc or closing the bar still stops it (Codex r4279062913).
		if (!ok) {
			if (job.fresh) {
				state.started = false;
				sourceLanded = false;
			}
			// A refused jump puts back the bar that still sounds: its anchor and trail, and its line unless
			// something newer has written one, such as the reason send() set (Sol P2 and Codex, #6687).
			else if (job.restore !== null) {
				state.anchor = job.restore.anchor;
				history = job.restore.history;
				if (state.message === job.restore.landed) state.message = job.restore.message;
			}
			return;
		}
		// A first press decodes the track first, and the master moved on meanwhile;
		// the decoded audio is cached now, so one more start lands back in phase.
		// Only a start of a preview that was not playing decodes cold; a slow later jump is not restarted.
		if (job.fresh && ports.now() - t0 > COLD_START_RESEEK_MS) {
			const reseeked = await send();
			if (!(await stillWanted(reseeked))) return;
			// A refused corrective reseek leaves the first source at the stale pre-decode phase,
			// so it is stopped and the controller drops ownership of it (Sol r4285715552).
			if (!reseeked) {
				await ports.dispatch({ type: 'preview_stop' });
				state.started = false;
				sourceLanded = false;
				state.message = 'The preview could not be set in phase, so it stopped';
			}
		}
	});

	function stopSounding(): void {
		ask += 1;
		stoppedAt = ask;
		if (inFlight === 0 && state.started && state.stable_id !== null && ports.sounding(state.stable_id)) {
			void ports.dispatch({ type: 'preview_stop' });
		}
		state.started = false;
		sourceLanded = false;
	}

	/** ← (dir -1) or → (dir 1); Shift takes the 8-bar grid. */
	function jump(dir: 1 | -1, big: boolean): Promise<void> {
		const current = track;
		if (current === null) return Promise.resolve();
		if (current.downbeats.length === 0) {
			if (current.gridPending) {
				held = { stable_id: current.stable_id, dir, big };
				state.message = GRID_LOADING;
			} else {
				state.message = current.untrusted === true ? BAR_PHASE_UNTRUSTED : 'No beat grid yet, so it cannot jump by bars';
			}
			return Promise.resolve();
		}
		held = null;
		if (current.duration_s === null || !(current.duration_s > 0)) {
			state.message = 'No track length yet, so it cannot be previewed';
			return Promise.resolve();
		}
		// Still running means started here and either sounding or on its way (a
		// cold decode); a preview that ended or was stopped elsewhere starts over.
		const fresh = !state.started || (inFlight === 0 && !ports.sounding(current.stable_id));
		// What a refused jump puts back: the bar as it stood before this press.
		const before = { anchor: state.anchor, history: [...history], message: state.message };
		if (fresh) {
			state.anchor = 0;
			history = [];
			state.message = 'Bar 1';
		} else {
			const stops = current.stops.map((stop) => stop.bar);
			const grid = jumpGrid(current.downbeats.length, stops, big ? PREVIEW_BIG_STEP_BARS : PREVIEW_STEP_BARS);
			if (dir === 1) {
				history.push(state.anchor);
				state.anchor = jumpTarget(grid, state.anchor, 1);
			} else if (history.length > 0) {
				state.anchor = history.pop() as number;
			} else {
				state.anchor = jumpTarget(grid, state.anchor, -1);
			}
			const stop = current.stops.find((s) => s.bar === state.anchor);
			state.message = stop !== undefined ? `${stop.label} at bar ${state.anchor + 1}` : `Bar ${state.anchor + 1}`;
		}
		state.started = true;
		ask += 1;
		const restore: JumpRestore = fresh ? null : { ...before, landed: state.message };
		return seek({ stable_id: current.stable_id, bar: state.anchor, ask, fresh, restore, owned: !fresh && sourceLanded });
	}

	return {
		state,
		/** The highlighted track changed (or went away): a preview of the old one stops. */
		setTrack(next: CuePreviewTrack | null): void {
			if (next?.stable_id !== state.stable_id) {
				stopSounding();
				held = null;
				history = [];
				state.anchor = 0;
				state.message = '';
				state.stable_id = next?.stable_id ?? null;
			}
			track = next;
			if (held === null || next === null) return;
			if (next.downbeats.length > 0) {
				const press = held;
				held = null;
				void jump(press.dir, press.big);
			} else if (!next.gridPending) {
				held = null;
				state.message = next.untrusted === true ? BAR_PHASE_UNTRUSTED : 'No beat grid yet, so it cannot jump by bars';
			}
		},
		jump,
		/** Esc, closing the bar, or leaving the track. True when something was sounding or on its way (a cold decode). */
		stop(): boolean {
			// A held press is a preview the DJ has asked for but nothing sounds yet: Esc consumes it too (Sol P1, #6687).
			const heldPress = held !== null;
			const was = heldPress || (state.started && state.stable_id !== null && (inFlight > 0 || ports.sounding(state.stable_id)));
			held = null;
			stopSounding();
			return was;
		}
	};
}

export type CuePreview = ReturnType<typeof createCuePreview>;
