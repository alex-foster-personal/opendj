/**
 * AutoPlay's load-then-play handoff, with the arming re-checked between steps.
 *
 * Mini-PRD
 *   ✔︎ The handoff loads, beat-syncs and plays the follower in that order, and
 *     says whether it finished or was abandoned.
 *     [if] an armed handoff does not load and play the follower [then ⛔️] broken
 *   ✔︎ Switching AutoPlay off while a handoff is in flight abandons it: nothing
 *     is loaded after the switch, nothing is played after it, and a track it
 *     already loaded is unloaded again, so no half-done deck is left behind.
 *     [if] a deck loads after AutoPlay was switched off [then ⛔️] broken
 *     [if] a deck starts playing after AutoPlay was switched off [then ⛔️] broken
 *     [if] a track loaded for an abandoned handoff stays on the deck [then ⛔️] broken
 *
 * Why: every step here awaits the engine, and under load a single step has
 * been measured taking seconds. `_tick` checked the arming once, before the
 * first step, so an off pressed mid-handoff still loaded and played a track.
 *
 * Pure apart from the injected steps, so it is unit-tested without an engine.
 * The controller (auto-play.svelte.ts) supplies the real dispatches.
 */
import { AutoPlayHandoffError } from '$lib/rb/auto-play-handoff-error';

export type AutoPlayHandoffOutcome = 'handed-off' | 'abandoned-disarmed';

export interface AutoPlayHandoffSteps {
	/** True while the arming that started this handoff is still current. */
	stillArmed: () => boolean;
	/** The follower deck's current track, read fresh at each step. */
	followerStableId: () => string | null;
	unload: () => Promise<unknown>;
	load: () => Promise<unknown>;
	/** Resolves to a skip message to show, or null. */
	beatSync: () => Promise<string | null>;
	play: () => Promise<unknown>;
	notifySyncSkip: (message: string) => void;
	onPlayDispatched: () => void;
}

/**
 * Load + start the follower. Throws AutoPlayHandoffError('load') when nothing
 * landed on the deck and ('commit') once something did, as before.
 *
 * Abandon cleanup unloads ONLY a track this handoff loaded itself (Sol P1 on
 * #5646): if `nextId` was already on the follower, it belongs to whoever put it
 * there, and an off must leave it alone. A track that was on the follower before
 * the handoff is unloaded on purpose BEFORE the load; an off before that unload
 * leaves it untouched.
 */
export async function runAutoPlayHandoff(
	nextId: string,
	steps: AutoPlayHandoffSteps
): Promise<AutoPlayHandoffOutcome> {
	let loadedHere = false;
	try {
		if (!steps.stillArmed()) return 'abandoned-disarmed';
		const occupied = steps.followerStableId();
		if (occupied !== null && occupied !== nextId) {
			await steps.unload();
			if (!steps.stillArmed()) return 'abandoned-disarmed';
		}
		if (steps.followerStableId() !== nextId) {
			await steps.load();
			loadedHere = true;
		}
	} catch (error: unknown) {
		throw new AutoPlayHandoffError('load', error);
	}
	// ---- commit point: nextId is on the follower deck from here down ----
	const abandon = async (): Promise<AutoPlayHandoffOutcome> => {
		if (loadedHere) await steps.unload();
		return 'abandoned-disarmed';
	};
	try {
		if (!steps.stillArmed()) return await abandon();
		const syncSkip = await steps.beatSync();
		if (!steps.stillArmed()) return await abandon();
		if (syncSkip !== null) steps.notifySyncSkip(syncSkip);
		await steps.play();
		steps.onPlayDispatched();
	} catch (error: unknown) {
		throw new AutoPlayHandoffError('commit', error);
	}
	return 'handed-off';
}
