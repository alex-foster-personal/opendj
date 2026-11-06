/**
 * Installable runners for automatic Beat Sync handoffs (re-join and master election).
 *
 * Split from master-election.ts so unit tests can load this module through Vite SSR
 * without importing `$lib/rb/auto-gain.svelte` (runes), which the ratchet treats as
 * coupling to `.svelte` source.
 *
 * The dispatcher installs the shared runners here; see performance-ipc.svelte.ts.
 */

/** Supersedes the separate automatic-rejoin.ts runner: master election owns handoffs. */
/** Runs an automatic master handoff's follower re-join. The dispatcher installs
 * one that takes every deck's scope plus 'sync' (installed rather than
 * imported, since this module is imported FROM there); until then it runs now. */
export type AutomaticRejoinRunner = (work: () => Promise<void>) => Promise<void>;
let _automaticRejoinRunner: AutomaticRejoinRunner = (work) => work();

export function installAutomaticRejoinRunner(runner: AutomaticRejoinRunner): () => void {
	const previous = _automaticRejoinRunner;
	_automaticRejoinRunner = runner;
	return () => {
		_automaticRejoinRunner = previous;
	};
}

export function runAutomaticRejoin(work: () => Promise<void>): Promise<void> {
	return _automaticRejoinRunner(work);
}

/** Automatic election runs under the dispatcher's all-deck plus sync claim. */
export type AutomaticMasterElectionRunner = (work: () => Promise<void>) => Promise<void>;
let automaticMasterElectionRunner: AutomaticMasterElectionRunner = (work) => work();

export function installAutomaticMasterElectionRunner(runner: AutomaticMasterElectionRunner): () => void {
	const previous = automaticMasterElectionRunner;
	automaticMasterElectionRunner = runner;
	return () => {
		automaticMasterElectionRunner = previous;
	};
}

export function runAutomaticMasterElection(work: () => Promise<void>): Promise<void> {
	return automaticMasterElectionRunner(work);
}
