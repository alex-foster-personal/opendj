/** Supersedes the Web Audio-only runner; both engines share the dispatcher claim. */
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
