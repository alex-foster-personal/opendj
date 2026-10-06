/**
 * AGENT-18: everything on /performance that WRITES shared state, or loads deck
 * audio on open, runs only in the leader tab.
 *
 * Mon 5 Oct 2026: a freshly opened second tab read the browser's shared
 * session snapshot (localStorage), loaded all four of the playing tab's decks
 * (URL gained d1..d4) and began fetching their audio; its session writer then
 * overwrote that snapshot with its own state. A follower is now a pure viewer:
 * it restores nothing, loads no deck, and writes no session, rescue ring, play
 * or set-recorder event until it becomes the leader.
 *
 * Rescue auto-restore runs on EVERY promotion: a demoted tab is silenced (bug
 * #31), so on regaining the lease it continues the set from the engine's rescue
 * ring rather than from its own stale decks. The browser-local session snapshot
 * restores decks on the FIRST promotion only, unless that first deck restore
 * never finished: then it loads the URL's still-empty decks first, so the writer
 * cannot publish a half-restored engine and drop d3/d4 from the URL (#5491).
 *
 * The real installers are injected so this ordering is unit tested without the
 * audio engine (`tests/unit/leader-only-writers.test.mjs`).
 */
import { whileLeader, type TabLeadership } from './tab-leadership';

export interface LeaderOnlyWriterDeps {
	leadership: Pick<TabLeadership, 'isLeader' | 'subscribe'>;
	/** Loads decks from the rescue ring; resolves true when it handled the restore. */
	runRescueAutoRestore: () => Promise<boolean>;
	/** Restores the session snapshot (unless skipped) and starts its writer. */
	installSessionRestore: (opts: {
		skipDeckRestore: boolean;
		resumeInterruptedRestore: boolean;
		onDeckRestoreSettled: () => void;
	}) => () => void;
	installRescueRingWriter: () => () => void;
	installDeckObserver: () => () => void;
	installPlayCounter: () => () => void;
}

/** The synchronous writers, installed at mount while this tab leads. */
export function installLeaderOnlyEventWriters(
	deps: Pick<LeaderOnlyWriterDeps, 'leadership' | 'installDeckObserver' | 'installPlayCounter'>
): () => void {
	return whileLeader(deps.leadership, () => {
		const uninstallDeckObserver = deps.installDeckObserver();
		const uninstallPlayCounter = deps.installPlayCounter();
		return () => {
			uninstallDeckObserver();
			uninstallPlayCounter();
		};
	});
}

/** Deck restore plus the session and rescue writers; call once the page IPC is up. */
export function installLeaderOnlyRestore(
	deps: Pick<
		LeaderOnlyWriterDeps,
		'leadership' | 'runRescueAutoRestore' | 'installSessionRestore' | 'installRescueRingWriter'
	>
): () => void {
	// True once a deck restore ran to its end in THIS tab. Until then a
	// re-promotion resumes it rather than skipping it (`_resumeUrlDecks`).
	let deckRestoreSettled = false;
	return whileLeader(deps.leadership, (priorRuns) => {
		let stopped = false;
		let uninstallSession: (() => void) | null = null;
		let uninstallRescueWriter: (() => void) | null = null;
		void (async () => {
			// Every promotion resumes from the engine's rescue ring (written every 2 s by
			// the previous leader), so Take control continues the set rather than starting
			// it again; a demoted tab was silenced, so there is nothing local to keep. The
			// browser-local session snapshot only restores decks on the first promotion.
			const rescueHandled = await deps.runRescueAutoRestore();
			// Leadership can be lost (or the route unmount) while the rescue
			// fetch is in flight; nothing installed after that would be undone.
			if (stopped) return;
			const resume = priorRuns > 0 && !deckRestoreSettled;
			uninstallSession = deps.installSessionRestore({
				// A re-promotion resumed from the rescue ring above; the session snapshot only
				// restores decks on the first promotion, or to finish one cut short (#5491).
				skipDeckRestore: (rescueHandled || priorRuns > 0) && !resume,
				resumeInterruptedRestore: resume,
				onDeckRestoreSettled: () => {
					deckRestoreSettled = true;
				}
			});
			uninstallRescueWriter = deps.installRescueRingWriter();
		})();
		return () => {
			stopped = true;
			uninstallSession?.();
			uninstallRescueWriter?.();
		};
	});
}

/**
 * Bug #31: a tab that stops leading goes silent at once, so the operator never
 * hears two sets. `silence` stops this tab's OWN output only (decks, stems,
 * preview cue, AutoPlay's armed handoff); it writes nothing shared, because the
 * new leader owns the mirror, the session and the rescue ring. It runs in a
 * microtask, after every leader-only writer has been uninstalled by the same
 * notification, so the pause it causes cannot be recorded as the operator's.
 */
export function installDemotionSilencer(deps: {
	leadership: Pick<TabLeadership, 'isLeader' | 'subscribe'>;
	silence: () => void;
}): () => void {
	let wasLeader = deps.leadership.isLeader();
	let disposed = false;
	const unsubscribe = deps.leadership.subscribe(() => {
		const leads = deps.leadership.isLeader();
		if (wasLeader && !leads) {
			queueMicrotask(() => {
				if (!disposed && !deps.leadership.isLeader()) deps.silence();
			});
		}
		wasLeader = leads;
	});
	return () => {
		disposed = true;
		unsubscribe();
	};
}

/** Bug #31: what `installDemotionSilencer` runs on /performance, with its effects injected. */
export function silenceDemotedTab(deps: {
	pauseDeck: (deck: 1 | 2 | 3 | 4) => Promise<void>;
	stopPreviewCue: () => void;
	cancelAutoPlayNext: () => void;
	reportError: (deck: 1 | 2 | 3 | 4, error: unknown) => void;
}): void {
	// AutoPlay first, so no handoff can start a deck while the others are pausing.
	deps.cancelAutoPlayNext();
	deps.stopPreviewCue();
	// Every deck, not only the ones reading playing: a quantized launch armed on a
	// paused deck would otherwise start it a beat later. pause() also clears that.
	for (const deck of [1, 2, 3, 4] as const) {
		void deps.pauseDeck(deck).catch((error: unknown) => deps.reportError(deck, error));
	}
}
