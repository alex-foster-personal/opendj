/**
 * R8 (the maintainer, Wed 7 Oct 2026): "ctrl hotkey when held switch EQs to STEMs (as
 * if pressing the STEM button in every channel's mixer then clicking it off
 * again when released)".
 *
 * Pure state machine, no DOM and no engine imports, so it is testable with
 * injected timers. Ctrl held ALONE for STEM_HOLD_CFG.engageAfterMs engages:
 * every deck with stems ready and not already in STEM is switched on. Release,
 * window blur, a hidden tab, another key or a pointer press restores each of
 * THOSE decks to its own prior mode; a deck that was already in STEM, or has no
 * stems, is never touched. Another key or a press before the delay cancels the
 * pending engage, so Ctrl+Z or Ctrl+click never flashes the mode.
 */

export const STEM_HOLD_CFG = {
	/** Ctrl must be held alone this long before the mode engages. */
	engageAfterMs: 150
} as const;

export interface StemHoldDeps<D> {
	decks: readonly D[];
	stemsReady: (deck: D) => boolean;
	stemEqMode: (deck: D) => boolean;
	/** The same typed command path the STEM button uses. Resolves true only
	 * when the command ran; a preset lock or a dead session resolves false. */
	setStemEqMode: (deck: D, enabled: boolean) => boolean | Promise<boolean>;
	/** A restore (disable) the dispatcher refused or that threw. The deck
	 * stays in STEM; the hold keeps it as unrestored and says so here. */
	reportRestoreRefused: (deck: D, cause: unknown) => void;
	/** An enable that threw or rejected (an IPC or engine error, not a
	 * refusal, which resolves false and is already toasted by the command
	 * path). The deck is not switched on and is never restored. */
	reportEnableFailed: (deck: D, cause: unknown) => void;
	setTimer: (fn: () => void, ms: number) => unknown;
	clearTimer: (handle: unknown) => void;
	engageAfterMs?: number;
}

export interface StemHold<D> {
	/** Ctrl keydown with no other modifier. Repeats are ignored. */
	ctrlDown: () => void;
	/** Ctrl keyup, window blur, tab hidden, another key, or a pointer press. */
	release: () => void;
	readonly engaged: boolean;
	/** Decks this hold switched on (command confirmed), and so will switch back off. */
	readonly switchedOn: readonly D[];
	/** Decks a hold switched on whose restore was refused: still owed a switch
	 * back off, which the next hold's release retries if they still read STEM. */
	readonly unrestored: readonly D[];
}

export function createStemHold<D>(deps: StemHoldDeps<D>): StemHold<D> {
	let timer: unknown = null;
	let switchedOn: D[] = [];
	let engaged = false;
	/** Bumped by every engage and release, so an enable answer can tell
	 * whether the hold that sent it is still the current one. */
	let generation = 0;
	/** The current hold's enables whose answer has not come back yet, with
	 * that answer (true only when the enable ran). */
	let pending = new Map<D, Promise<boolean>>();
	/** Decks with a restore (disable) still in flight. The mixer state can
	 * still read STEM for them, so a new hold must not skip them as "already
	 * in STEM": it re-sends the enable, which lands after the disable. */
	const restoring = new Map<D, number>();
	/** Restores the dispatcher refused (Sol P1, PR #6834): kept, not forgotten. */
	const unrestored = new Set<D>();

	/** `enabled` is set for a deck whose enable was still unanswered at the
	 * release: a refused restore is owed only if that enable did run. When it
	 * did not, this hold never switched the deck on, so nothing is owed and a
	 * later hold must not adopt a STEM mode someone else set (Sol P1, #6834). */
	function settleRestore(deck: D, ran: boolean, cause: unknown, enabled?: Promise<boolean>): void {
		if (ran) {
			unrestored.delete(deck);
			return;
		}
		const owe = () => {
			unrestored.add(deck);
			deps.reportRestoreRefused(deck, cause);
		};
		if (enabled === undefined) owe();
		else void enabled.then((on) => (on ? owe() : undefined));
	}

	function restore(deck: D, enabled?: Promise<boolean>): void {
		let result: boolean | Promise<boolean>;
		try {
			result = deps.setStemEqMode(deck, false);
		} catch (exc) {
			settleRestore(deck, false, exc, enabled);
			return;
		}
		if (typeof result === 'boolean') {
			settleRestore(deck, result, 'refused', enabled);
			return;
		}
		restoring.set(deck, (restoring.get(deck) ?? 0) + 1);
		const done = () => {
			const held = restoring.get(deck);
			// Every done() pairs with the set above; a missing count is lost
			// bookkeeping, which must surface rather than read as a clean end.
			if (held === undefined) throw new Error(`stem-hold: deck ${String(deck)} restore count missing`);
			if (held > 1) restoring.set(deck, held - 1);
			else restoring.delete(deck);
		};
		void result.then(
			(ran) => {
				settleRestore(deck, ran, 'refused', enabled);
				done();
			},
			(exc: unknown) => {
				settleRestore(deck, false, exc, enabled);
				done();
			}
		);
	}

	/** A deck joins the restore set only once its enable actually ran: a
	 * rejected enable (a preset lock, say) must not be "restored" to off over
	 * whatever mode the preset set. An answer from a hold that has already
	 * ended is ignored: that release restored the deck when it happened. */
	function settleEnable(deck: D, ran: boolean, sentBy: number): void {
		if (sentBy !== generation) return;
		pending.delete(deck);
		if (ran && !switchedOn.includes(deck)) switchedOn.push(deck);
	}

	function engage(): void {
		timer = null;
		engaged = true;
		generation += 1;
		switchedOn = [];
		pending = new Map();
		const sentBy = generation;
		// A deck an earlier hold left in STEM (its restore was refused) is
		// still owed its switch back off: this hold adopts it, so its release
		// retries the restore. One that no longer reads STEM was settled
		// elsewhere and is dropped.
		for (const d of [...unrestored]) {
			unrestored.delete(d);
			if (deps.stemEqMode(d)) switchedOn.push(d);
		}
		for (const d of deps.decks.filter((d) => deps.stemsReady(d) && (!deps.stemEqMode(d) || restoring.has(d)))) {
			let result: boolean | Promise<boolean>;
			try {
				result = deps.setStemEqMode(d, true);
			} catch (exc) {
				// One deck's error must not stop the others from engaging.
				deps.reportEnableFailed(d, exc);
				continue;
			}
			if (typeof result === 'boolean') settleEnable(d, result, sentBy);
			else {
				const answer = result.then(
					(ran) => ran,
					(exc: unknown) => {
						deps.reportEnableFailed(d, exc);
						return false;
					}
				);
				pending.set(d, answer);
				void answer.then((ran) => settleEnable(d, ran, sentBy));
			}
		}
	}

	return {
		ctrlDown() {
			if (engaged || timer !== null) return;
			timer = deps.setTimer(engage, deps.engageAfterMs ?? STEM_HOLD_CFG.engageAfterMs);
		},
		/** Restores happen here, never later: the engine applies commands in the
		 * order they are sent, so a disable sent now lands before anything the
		 * DJ does next (a STEM click after the pointer press that ended the
		 * hold). An enable still unanswered is restored too, and not later when
		 * it answers: a pointer press both ends the hold and clicks STEM, and a
		 * deferred disable would land after that click and undo it. A pending
		 * enable has already passed the dispatcher's preset-claim check, which
		 * runs on entry, and the disable queues right behind it on the deck's
		 * scope; if the enable then fails, the deck is not owed a retry. */
		release() {
			if (timer !== null) {
				deps.clearTimer(timer);
				timer = null;
			}
			if (!engaged) return;
			engaged = false;
			generation += 1;
			const confirmed = switchedOn;
			const unanswered = pending;
			switchedOn = [];
			pending = new Map();
			for (const d of confirmed) restore(d);
			for (const [d, answer] of unanswered) restore(d, answer);
		},
		get engaged() {
			return engaged;
		},
		get switchedOn() {
			return switchedOn;
		},
		get unrestored() {
			return [...unrestored];
		}
	};
}

/** Keys other than Control currently held down, by `KeyboardEvent.code` (so a
 * Shift-changed `key` still matches its keyup). The Ctrl hold arms only when
 * this is empty: modifier flags on the Control event cannot see a plain key,
 * so holding Z and then pressing Control would otherwise engage (Sol P1,
 * PR #6834). Cleared on blur and on a hidden tab, where keyups are lost. */
export interface HeldKeys {
	down: (code: string) => void;
	up: (code: string) => void;
	clear: () => void;
	readonly othersHeld: boolean;
}

export function createHeldKeys(): HeldKeys {
	const held = new Set<string>();
	return {
		down: (code) => void held.add(code),
		up: (code) => void held.delete(code),
		clear: () => held.clear(),
		get othersHeld() {
			return held.size > 0;
		}
	};
}
