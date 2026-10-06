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
 * Deck restore (rescue auto-restore, then the session snapshot) happens on the
 * FIRST promotion only. A tab that loses leadership and later regains it keeps
 * its decks as they are and only restarts its writers.
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
	installSessionRestore: (opts: { skipDeckRestore: boolean }) => () => void;
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
	return whileLeader(deps.leadership, (priorRuns) => {
		let stopped = false;
		let uninstallSession: (() => void) | null = null;
		let uninstallRescueWriter: (() => void) | null = null;
		void (async () => {
			const rescueHandled = priorRuns === 0 ? await deps.runRescueAutoRestore() : true;
			// Leadership can be lost (or the route unmount) while the rescue
			// fetch is in flight; nothing installed after that would be undone.
			if (stopped) return;
			uninstallSession = deps.installSessionRestore({ skipDeckRestore: rescueHandled });
			uninstallRescueWriter = deps.installRescueRingWriter();
		})();
		return () => {
			stopped = true;
			uninstallSession?.();
			uninstallRescueWriter?.();
		};
	});
}
