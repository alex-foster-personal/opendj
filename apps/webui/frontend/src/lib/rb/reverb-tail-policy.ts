/**
 * Reverb tail policy (FXVERB-05, #9822): what a reverb tail does on each deck
 * event, in one table that every reverb host calls, so a tail never leaks into
 * the next track and is never cut where it should ring. Hygiene, the house
 * table of `(private path)` section 4
 * ("Tails on events"), which closes item 6 of (private path).
 *
 *   track load, eject  -> cut: the return ramps from its current gain to 0
 *                         over 10 ms, the reverb node detaches (its tail state
 *                         goes with it), the return gain is restored for the
 *                         next send
 *   stop, pause        -> ring out on the reverb's own RT60
 *   effect switch      -> ring out (COLORFX-8341: only the inputs crossfade)
 *   page teardown      -> dispose at once
 *
 * The channel fader and crossfader need no row: the wet return sits before
 * the fader (`color-fx-graph.ts`), so the fader already cuts it.
 * No imports: a host hands in its return gain and a detach, so the color
 * knob's SPACE (`color-fx.svelte.ts`) and the FDN reverb host share it.
 */

export type ReverbTailEvent =
	'track_load' | 'eject' | 'stop' | 'pause' | 'effect_switch' | 'teardown';
export type ReverbTailAction = 'cut' | 'ring_out' | 'dispose';

/** The one event-to-action table (exhaustive over ReverbTailEvent by type). */
export const REVERB_TAIL_POLICY: Readonly<Record<ReverbTailEvent, ReverbTailAction>> =
	Object.freeze({
		track_load: 'cut',
		eject: 'cut',
		stop: 'ring_out',
		pause: 'ring_out',
		effect_switch: 'ring_out',
		teardown: 'dispose'
	});

/** Length of the cut's linear ramp to silence (the spec's 10 ms). */
export const REVERB_TAIL_CUT_SEC = 0.01;

/** A reverb return as the policy drives it: SPACE's convolver today, the FDN reverb later. */
export interface ReverbTailHost {
	/** The wet return gain the cut ramps to 0, then restores. */
	returnGain: AudioParam;
	/** The return's resting gain, restored once the reverb node is detached. */
	restoreGain: number;
	/** Whether a reverb node (and so possibly a tail) is attached. */
	attached(): boolean;
	/** Disconnect the reverb node, dropping its tail. */
	detach(): void;
}

/** Runs `done` with the context time once `delaySec` of audio time has passed. */
export type AfterAudio = (delaySec: number, done: (now: number) => void) => void;

const _cutting = new WeakSet<AudioParam>();

/** Run `done` once `clock` has advanced `delaySec`, re-checking every `pollMs`, so a suspended context never jumps ahead. */
export function afterAudioClock(
	clock: { readonly currentTime: number },
	delaySec: number,
	pollMs: number,
	done: (now: number) => void
): void {
	const due = clock.currentTime + delaySec;
	const check = (): void => {
		if (clock.currentTime >= due) done(clock.currentTime);
		else setTimeout(check, pollMs);
	};
	setTimeout(check, delaySec * 1000);
}

/** The action the table names for `event`. */
export function reverbTailAction(event: ReverbTailEvent): ReverbTailAction {
	return REVERB_TAIL_POLICY[event];
}

/** Apply the table's action for `event` to `host` at context time `now`; returns the action. */
export function applyReverbTailEvent(
	event: ReverbTailEvent,
	host: ReverbTailHost,
	now: number,
	afterAudio: AfterAudio
): ReverbTailAction {
	const action = reverbTailAction(event);
	if (!host.attached()) return action;
	switch (action) {
		case 'ring_out':
			return action;
		case 'cut':
			_cut(host, now, afterAudio);
			return action;
		case 'dispose':
			host.detach();
			return action;
		default: {
			const _exhaustive: never = action;
			throw new Error(`Unhandled reverb tail action: ${String(_exhaustive)}`);
		}
	}
}

/** Ramp the return to 0, detach once it is silent, then restore the return for the next send. */
function _cut(host: ReverbTailHost, now: number, afterAudio: AfterAudio): void {
	const gain = host.returnGain;
	// A second load inside the ramp joins the cut in flight, never restarts it from full.
	if (_cutting.has(gain)) return;
	_cutting.add(gain);
	// Ramp from where the return is now, never from its resting gain: a return
	// mid-crossfade or mid-restore would step to restoreGain first and click.
	const from = gain.value;
	gain.cancelScheduledValues(now);
	gain.setValueAtTime(from, now);
	gain.linearRampToValueAtTime(0, now + REVERB_TAIL_CUT_SEC);
	afterAudio(REVERB_TAIL_CUT_SEC, (at) => {
		_cutting.delete(gain);
		host.detach();
		gain.cancelScheduledValues(at);
		gain.setValueAtTime(host.restoreGain, at);
	});
}
